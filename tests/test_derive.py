"""
盤前衍生欄位的計算測試。

這些欄位全部是「判斷」——鏡像對峙、VIX 分級、乖離率、夜盤漲跌 ——
判斷寫錯不會拋例外，只會在報告裡變成一句講得很篤定的錯話。
_derive 不碰網路，所以可以直接餵假 payload 驗證。

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from premarket import fetch  # noqa: E402

TODAY = "2026-08-18"
PREV = "2026-08-17"


def _payload(**kw) -> fetch.Payload:
    p = fetch.Payload(
        generated_at="2026-08-18T06:38:00+08:00",
        target_session=TODAY,
        prev_trade_date=PREV,
    )
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _bars(closes: list[float]) -> list[dict]:
    return [
        {"date": f"2026-07-{i + 1:02d}", "open": c, "high": c + 100, "low": c - 100, "close": c}
        for i, c in enumerate(closes)
    ]


class TestFutures(unittest.TestCase):
    """
    未平倉「變動」要先過門檻才准給方向。

    8 萬口部位變動 65 口是 0.08%，原本會被寫成「偏空但力道略緩」——
    憑空生出一個不存在的方向。門檻擋掉的就是這個。
    """

    def test_tiny_change_on_a_huge_position_is_flat(self):
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -82529,
            "外資台指期淨未平倉_較前日增減": 65,
        }))
        self.assertEqual(d["外資台指期淨未平倉_較前日增減_pct"], 0.08)
        self.assertEqual(d["外資台指期淨未平倉_動作判定"], "持平")

    def test_change_below_the_lot_floor_is_flat_even_at_high_pct(self):
        # 自營商部位基數小：616 口是 26%，但 616 口本身就過了絕對門檻 → 有訊號
        d = fetch._derive(_payload(institutional_futures={
            "自營商台指期淨未平倉口數": 2315,
            "自營商台指期淨未平倉_較前日增減": 616,
        }))
        self.assertEqual(d["自營商台指期淨未平倉_動作判定"], "加多")
        # 同樣的百分比但只有 300 口 → 絕對量太小，仍判持平
        d = fetch._derive(_payload(institutional_futures={
            "自營商台指期淨未平倉口數": 1150,
            "自營商台指期淨未平倉_較前日增減": 300,
        }))
        self.assertEqual(d["自營商台指期淨未平倉_動作判定"], "持平")

    def test_direction_words_follow_the_sign_of_the_position(self):
        short = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -80000,
            "外資台指期淨未平倉_較前日增減": 2100,
        }))
        self.assertEqual(short["外資台指期淨未平倉_動作判定"], "減空（回補）")
        long = fetch._derive(_payload(institutional_futures={
            "投信台指期淨未平倉口數": 75808,
            "投信台指期淨未平倉_較前日增減": -2100,
        }))
        self.assertEqual(long["投信台指期淨未平倉_動作判定"], "減多")

    def test_mirror_standoff_is_gone(self):
        """外資淨空與投信多單都是結構性部位，不是方向對賭 —— 不得再輸出對峙。"""
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -83474,
            "投信台指期淨未平倉口數": 78013,
        }))
        for k in ("外資投信鏡像對峙", "外資投信量級比", "外資投信淨部位合計口數"):
            self.assertNotIn(k, d)
        self.assertIn("外資台指期淨未平倉_水位性質", d)

    def test_position_level_ranked_against_its_own_history(self):
        hist = [{"date": f"2026-08-{i:02d}", "外資台指期淨未平倉口數": v}
                for i, v in enumerate([-83078, -81501, -82423, -82594], start=19)]
        d = fetch._derive(_payload(
            institutional_futures={"外資台指期淨未平倉口數": -82529},
            history=hist,
        ))
        self.assertEqual(d["外資台指期淨未平倉_水位百分位"], 50.0)
        self.assertEqual(d["外資台指期淨未平倉_水位分級"], "常態區間")

    def test_daily_change_passes_through(self):
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -83474,
            "外資台指期淨未平倉_較前日增減": -2100,
        }))
        self.assertEqual(d["外資台指期淨未平倉_較前日增減"], -2100)


class TestCashAndMargin(unittest.TestCase):
    def test_cash_flow_gate(self):
        d = fetch._derive(_payload(institutional_cash={
            "外資及陸資買賣超_億": -157.36,
            "投信買賣超_億": 30.55,
        }))
        self.assertEqual(d["外資現貨買賣超_動作判定"], "賣超")
        self.assertEqual(d["投信現貨買賣超_動作判定"], "持平")

    def test_combo_needs_both_sides_over_the_gate(self):
        d = fetch._derive(_payload(
            institutional_cash={"外資及陸資買賣超_億": -157.36},
            institutional_futures={
                "外資台指期淨未平倉口數": -82529,
                "外資台指期淨未平倉_較前日增減": 65,
            },
        ))
        self.assertIn("僅現貨過門檻", d["外資現貨期貨組合判定"])

    def test_leverage_compared_against_index_over_the_same_window(self):
        hist = [{"date": "2026-08-05", "融資餘額_億": 5211.35, "加權指數收盤": 43360.66}]
        d = fetch._derive(_payload(
            taiex_ohlc=_bars([44762.32]),
            margin={"融資餘額_億": 5452.59, "融資前日餘額_億": 5469.39},
            history=hist,
        ))
        self.assertEqual(d["融資餘額_動作判定"], "持平")  # -0.31%，日變化是雜訊
        self.assertEqual(d["融資餘額_較起點變化_pct"], 4.63)
        self.assertEqual(d["指數_較起點變化_pct"], 3.23)
        self.assertIn("槓桿膨脹", d["槓桿判定"])
        self.assertIn("不等於本波低點", d["槓桿觀察起點說明"])


class TestOtcDivergence(unittest.TestCase):
    TPEX = {"收盤": 268.5, "漲跌幅_pct": -0.16, "成交金額_億": 1980.4}

    def test_divergence_flagged_when_over_the_gate(self):
        d = fetch._derive(_payload(
            taiex_ohlc=_bars([45224.29, 44762.32]),
            taiex_turnover=[{"date": PREV, "turnover_yi": 6562.81}],
            tpex_index=self.TPEX,
        ))
        self.assertEqual(d["加權指數漲跌幅_pct"], -1.02)
        self.assertEqual(d["櫃買相對加權強弱_pp"], 0.86)
        self.assertIn("櫃買相對強", d["櫃買相對加權強弱_判定"])
        self.assertEqual(d["櫃買上市成交值比"], 0.302)

    def test_small_gap_is_in_sync_not_divergence(self):
        d = fetch._derive(_payload(
            taiex_ohlc=_bars([45000.0, 44775.0]),  # -0.50%
            tpex_index={"收盤": 268.5, "漲跌幅_pct": -0.30},
        ))
        self.assertEqual(d["櫃買相對加權強弱_pp"], 0.2)
        self.assertIn("同步", d["櫃買相對加權強弱_判定"])


class TestNightSession(unittest.TestCase):
    TX = {
        "day_session": {"close": 45888.0},
        "day_session_date": PREV,
        "night_session": {"close": 45811.0},
        "night_session_date": TODAY,
    }

    def test_night_move_is_measured_against_the_day_session(self):
        d = fetch._derive(_payload(taiex_ohlc=_bars([45857.27]), taifex_tx=self.TX))
        self.assertEqual(d["夜盤較日盤收盤漲跌點"], -77.0)
        self.assertEqual(d["夜盤較日盤收盤漲跌_pct"], -0.17)
        # 對現貨昨收的距離仍然給，但名稱不再自稱價差
        self.assertEqual(d["夜盤相對現貨昨收偏離"], -46.27)
        self.assertNotIn("夜盤期現價差", d)
        # 真正的期現價差只有日盤這一個
        self.assertEqual(d["日盤期現價差"], 30.73)

    def test_stale_night_session_yields_nothing(self):
        tx = dict(self.TX, night_session_date=PREV)
        d = fetch._derive(_payload(taiex_ohlc=_bars([45857.27]), taifex_tx=tx))
        for k in ("夜盤台指期收盤", "夜盤較日盤收盤漲跌點", "夜盤相對現貨昨收偏離"):
            self.assertNotIn(k, d)


class TestLevels(unittest.TestCase):
    def test_deviation_and_distance_to_range(self):
        # 19 天橫盤後最後一天急拉，短天期乖離會小於長天期（拉抬把短均帶了上去）
        closes = [43000.0] * 19 + [45857.27]
        d = fetch._derive(_payload(taiex_ohlc=_bars(closes)))
        self.assertEqual(d["對5日均線乖離率_pct"], 5.25)
        self.assertEqual(d["對10日均線乖離率_pct"], 5.94)
        self.assertEqual(d["對20日均線乖離率_pct"], 6.29)
        # 高低點取自 K 棒的 high/low，兩個百分比都以現價為分母
        self.assertEqual(d["近20日最高"], 45957.27)
        self.assertEqual(d["近20日最低"], 42900.0)
        self.assertEqual(d["距近20日高點_pct"], 0.22)
        self.assertEqual(d["距近20日低點_pct"], 6.45)


class TestVix(unittest.TestCase):
    def test_bands(self):
        for close, want in [
            (12.4, "低（<15）"),
            (15.19, "中性（15–20）"),
            (22.0, "偏高（20–25）"),
            (31.5, "恐慌（≥25）"),
        ]:
            d = fetch._derive(_payload(us_market={"VIX": {"close": close}}))
            self.assertEqual(d["VIX水位分級"], want, close)


class TestTsmc(unittest.TestCase):
    """
    ADR 溢價的絕對水準是結構性的（10% 上下），沒有訊息量。
    有訊息的是「相對它自己的常態偏離多少」，以及那個偏離值幾點。
    """

    US = {"台積電ADR": {"close": 430.97}, "美元兌台幣": {"close": 31.86}}
    # 2026-08-25 的實況：ADR 410.12、匯率 31.82、台積電現貨 2375
    REAL_US = {
        "台積電ADR": {"close": 410.12, "change_pct": -2.11},
        "美元兌台幣": {"close": 31.82, "change_pct": -0.06},
    }
    REAL_SPOT = {"代號": "2330", "日期": PREV, "收盤": 2375.0, "漲跌幅_pct": -1.45}
    HIST = [
        {"date": d, "台積電ADR溢價_pct_內部用": v}
        for d, v in [("2026-08-19", 10.75), ("2026-08-20", 11.70),
                     ("2026-08-21", 11.61), ("2026-08-24", 10.63)]
    ]

    def test_premium_needs_a_fresh_spot_close(self):
        d = fetch._derive(_payload(
            us_market=self.US,
            tsmc_spot={"代號": "2330", "日期": PREV, "收盤": 2700.0},
        ))
        self.assertEqual(d["台積電ADR隱含台股價"], 2746.1)
        self.assertEqual(d["台積電現貨收盤"], 2700.0)
        # 絕對溢價只留內部欄位，鍵名自帶「內部用」，prompt 端禁止引用
        self.assertEqual(d["台積電ADR溢價_pct_內部用"], 1.71)
        self.assertNotIn("台積電ADR溢價_pct", d)

    def test_stale_spot_close_yields_no_premium(self):
        d = fetch._derive(_payload(
            us_market=self.US,
            tsmc_spot={"代號": "2330", "日期": "2026-08-11", "收盤": 2700.0},
        ))
        self.assertIn("台積電ADR隱含台股價", d)
        self.assertNotIn("台積電ADR溢價_pct_內部用", d)

    def test_deviation_from_baseline_drives_the_index_attribution(self):
        d = fetch._derive(_payload(
            us_market=self.REAL_US,
            tsmc_spot=self.REAL_SPOT,
            taiex_ohlc=_bars([44762.32]),
            taifex_tx={
                "day_session": {"close": 44762.0}, "day_session_date": PREV,
                "night_session": {"close": 44532.0}, "night_session_date": TODAY,
            },
            history=self.HIST,
        ))
        self.assertEqual(d["台積電ADR溢價基準_pct"], 11.18)     # 4 日中位數
        self.assertEqual(d["台積電ADR溢價偏離基準_pp"], -1.29)
        self.assertIn("低於基準", d["台積電ADR溢價偏離判定"])
        self.assertEqual(d["台積電隱含現貨開盤變動_pct"], -1.16)
        self.assertEqual(d["台積電隱含指數影響_點"], -155.2)
        # 夜盤 -230 點中台積電可解釋約 68% —— 不是 100%，其餘來自其他成分股
        self.assertEqual(d["台積電可解釋夜盤跌點_pct"], 67.5)
        self.assertIn("偏集中", d["台積電歸因判定"])
        # 三腳拆解：ADR 比現貨多跌 0.72%
        self.assertEqual(d["ADR減現貨報酬差_pct"], -0.72)

    def test_deviation_inside_the_noise_band_yields_no_attribution(self):
        hist = [{"date": f"2026-08-{i:02d}", "台積電ADR溢價_pct_內部用": 9.9}
                for i in range(19, 25)]
        d = fetch._derive(_payload(
            us_market=self.REAL_US, tsmc_spot=self.REAL_SPOT,
            taiex_ohlc=_bars([44762.32]), history=hist,
        ))
        self.assertIn("持平", d["台積電ADR溢價偏離判定"])
        self.assertNotIn("台積電隱含指數影響_點", d)

    def test_too_little_history_downgrades_the_whole_section(self):
        d = fetch._derive(_payload(
            us_market=self.REAL_US, tsmc_spot=self.REAL_SPOT,
            history=[{"date": "2026-08-24", "台積電ADR溢價_pct_內部用": 10.63}],
        ))
        self.assertIsNone(d["台積電ADR溢價基準_pct"])
        self.assertIn("樣本不足", d["台積電ADR溢價偏離判定"])


if __name__ == "__main__":
    unittest.main()
