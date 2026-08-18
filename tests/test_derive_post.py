"""
盤後衍生欄位與篩選條件的測試。

每一條都對應一次真實的誤讀：60MA 被均線排列蓋掉、396 口被當成回補訊號、
逆價差回到常態被寫成方向翻轉、收黑被當成漲跌幅為負。
_derive_post 與 evaluate_candidate 都不碰網路，可以直接餵假資料驗證。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from premarket import fetch_post, shortlist  # noqa: E402

DATE = "2026-08-18"


def _payload(**kw) -> fetch_post.PostPayload:
    p = fetch_post.PostPayload(generated_at=f"{DATE}T21:23:00+08:00", session_date=DATE)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _bars(closes: list[float]) -> list[dict]:
    return [
        {"date": f"2026-0{6 + i // 30}-{i % 30 + 1:02d}", "open": c, "high": c, "low": c,
         "close": c}
        for i, c in enumerate(closes)
    ]


class TestMovingAverages(unittest.TestCase):
    def test_short_term_stack_does_not_imply_a_full_bull_stack(self):
        # 20MA 被前段暴跌拉低，仍在 60MA 之下：5>10>20 成立，但中期結構沒轉多
        closes = [46000.0] * 40 + [40000.0] * 5 + [45000.0 + i * 20 for i in range(15)]
        d = fetch_post._derive_post(_payload(taiex_ohlc=_bars(closes)))
        self.assertTrue(d["均線多頭排列"])
        self.assertFalse(d["20日均線在60日均線之上"])
        self.assertFalse(d["完整多頭排列_含60MA"])
        self.assertIn("指數對60日均線乖離率", d)

    def test_full_stack_when_60ma_is_below(self):
        closes = [40000.0 + i * 50 for i in range(60)]
        d = fetch_post._derive_post(_payload(taiex_ohlc=_bars(closes)))
        self.assertTrue(d["完整多頭排列_含60MA"])
        self.assertTrue(d["20日均線在60日均線之上"])


class TestFuturesNoise(unittest.TestCase):
    SERIES = [{
        "date": DATE,
        "外資淨未平倉口數": -83078, "外資單日增減": 396,
        "投信淨未平倉口數": 76112, "投信單日增減": -1901,
    }]

    def test_change_is_expressed_as_a_share_of_the_position(self):
        d = fetch_post._derive_post(_payload(futures_oi_series=self.SERIES))
        self.assertEqual(d["外資單日增減佔水位_pct"], 0.48)   # 雜訊
        self.assertEqual(d["投信單日增減佔水位_pct"], 2.5)    # 有訊號

    def test_mirror_still_flagged(self):
        d = fetch_post._derive_post(_payload(futures_oi_series=self.SERIES))
        self.assertEqual(d["外資投信淨部位合計口數"], -6966)
        self.assertTrue(d["外資投信鏡像對峙"])


class TestBasisContext(unittest.TestCase):
    def _trend(self, values: list[float]) -> dict:
        return {
            "序列": [{"date": f"2026-08-{10 + i:02d}", "價差": v} for i, v in enumerate(values)],
            "當日價差": values[-1], "前日價差": values[-2],
            "當日變動": round(values[-1] - values[-2], 2),
            "5日均價差": round(sum(values) / len(values), 2),
            "對5日均偏離": round(values[-1] - sum(values) / len(values), 2),
            "跨越正負號": (values[-1] > 0) != (values[-2] > 0),
            "異常": True,
        }

    def test_crossing_back_into_the_prevailing_direction_is_not_a_flip(self):
        # 今天以前多數為逆價差，前一日的正價差才是異常值
        d = fetch_post._derive_post(_payload(
            basis_trend=self._trend([-60.0, -50.0, -80.0, 30.73, -223.68])))
        self.assertEqual(d["期現價差_常態方向"], "逆價差")
        self.assertTrue(d["期現價差_今日為常態方向"])
        self.assertEqual(d["期現價差_今日前逆價差天數"], 3)
        # 回到常態方向，但幅度已經走出前幾日的區間，兩件事要分開講
        self.assertTrue(d["期現價差_今日走出前期區間"])

    def test_leaving_the_prevailing_direction_is_a_flip(self):
        d = fetch_post._derive_post(_payload(
            basis_trend=self._trend([40.0, 50.0, 60.0, 30.0, -100.0])))
        self.assertEqual(d["期現價差_常態方向"], "正價差")
        self.assertFalse(d["期現價差_今日為常態方向"])

    def test_todays_own_value_cannot_define_the_norm(self):
        # 前四日皆為正價差，今天一根大逆價差把 5 日均拉成負的 ——
        # 常態必須維持「正價差」，否則會用今天證明今天不異常
        vals = [9.93, -16.48, 29.99, 30.73, -223.68]
        d = fetch_post._derive_post(_payload(basis_trend=self._trend(vals)))
        self.assertLess(d["期現價差_5日均價差"], 0)        # 含今天：-33.9
        self.assertGreater(d["期現價差_今日前均價差"], 0)   # 不含今天：+13.54
        self.assertEqual(d["期現價差_常態方向"], "正價差")
        self.assertFalse(d["期現價差_今日為常態方向"])


class TestDividendDrag(unittest.TestCase):
    def test_sign_comes_from_the_html_colour_column(self):
        # 漲跌點數欄不帶正負號，方向只在 HTML 顏色欄裡
        self.assertEqual(fetch_post._signed("548.59", "<p style='color:green'>-</p>"), -548.59)
        self.assertEqual(fetch_post._signed("45.49", "<p style='color:red'>+</p>"), 45.49)
        self.assertIsNone(fetch_post._signed("--", "<p style='color:red'>+</p>"))

    def test_drag_flows_into_derived(self):
        d = fetch_post._derive_post(_payload(dividend_drag={
            "除息影響_pct": 0.04, "除息影響點數": 16.98, "有顯著除息": True,
        }))
        self.assertEqual(d["除息影響點數"], 16.98)
        self.assertTrue(d["有顯著除息"])


class TestScreenerBlackBar(unittest.TestCase):
    """「收黑」是收盤 < 開盤，不是漲跌幅為負 —— 這個混淆會讓人以為命中數算錯。"""

    HIST = [{"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": 1000, "turnover": 2e8} for _ in range(9)]

    def _bar(self, o: float, c: float, vol: int = 1000) -> dict:
        return {"open": o, "high": max(o, c), "low": min(o, c), "close": c,
                "volume": vol, "turnover": 2e8, "name": "測試"}

    def test_up_day_can_still_be_a_black_bar(self):
        # 開 120 收 110：漲 10%（前收 100）但 K 線收黑，乖離 +10% ≥ 8%
        got = shortlist.evaluate_candidate("9999", self._bar(120.0, 110.0), self.HIST, 0)
        self.assertTrue(got["收黑"])
        self.assertGreater(got["漲跌幅_pct"], 0)
        self.assertIn("過熱回落", got["命中條件"])

    def test_down_day_can_still_be_a_red_bar(self):
        # 開 95 收 99：跌 1%（前收 100）但 K 線收紅，過熱回落不成立
        got = shortlist.evaluate_candidate("9999", self._bar(95.0, 99.0), self.HIST, 0)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
