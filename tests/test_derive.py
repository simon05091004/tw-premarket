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
    def test_mirror_flagged_when_opposite_and_similar_size(self):
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -83474,
            "投信台指期淨未平倉口數": 78013,
        }))
        self.assertEqual(d["外資投信淨部位合計口數"], -5461)
        self.assertEqual(d["外資投信量級比"], 0.93)
        self.assertTrue(d["外資投信鏡像對峙"])

    def test_same_direction_is_not_a_mirror(self):
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -80000,
            "投信台指期淨未平倉口數": -79000,
        }))
        self.assertFalse(d["外資投信鏡像對峙"])

    def test_opposite_but_lopsided_is_not_a_mirror(self):
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -80000,
            "投信台指期淨未平倉口數": 12000,
        }))
        self.assertEqual(d["外資投信量級比"], 0.15)
        self.assertFalse(d["外資投信鏡像對峙"])

    def test_daily_change_passes_through(self):
        d = fetch._derive(_payload(institutional_futures={
            "外資台指期淨未平倉口數": -83474,
            "外資台指期淨未平倉_較前日增減": -2100,
        }))
        self.assertEqual(d["外資台指期淨未平倉_較前日增減"], -2100)


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
    US = {"台積電ADR": {"close": 430.97}, "美元兌台幣": {"close": 31.86}}

    def test_premium_needs_a_fresh_spot_close(self):
        d = fetch._derive(_payload(
            us_market=self.US,
            tsmc_spot={"代號": "2330", "日期": PREV, "收盤": 2700.0},
        ))
        self.assertEqual(d["台積電ADR隱含台股價"], 2746.1)
        self.assertEqual(d["台積電現貨收盤"], 2700.0)
        self.assertEqual(d["台積電ADR溢價_pct"], 1.71)

    def test_stale_spot_close_yields_no_premium(self):
        d = fetch._derive(_payload(
            us_market=self.US,
            tsmc_spot={"代號": "2330", "日期": "2026-08-11", "收盤": 2700.0},
        ))
        self.assertIn("台積電ADR隱含台股價", d)
        self.assertNotIn("台積電ADR溢價_pct", d)


if __name__ == "__main__":
    unittest.main()
