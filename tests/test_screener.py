"""
選股程式的評分與配置測試。

背景：原始版本有兩個在模擬資料上看不出來、接上真實資料才會咬人的問題 ——
權重上限 clip 完再正規化會把權重推回上限之上（12% 的上限守不住），
以及任何一個因子缺值會讓整檔的 total_score 變成 NaN 或被靜默篩掉。
這兩件事都不會拋例外，只有測試攔得住。

不連網路：所有測試都用手寫的 DataFrame。

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from premarket.screener import (  # noqa: E402
    AggressiveStableSelector,
    RetirementPlanningSelector,
    _cap_weights,
    portfolio_summary,
)


def make_row(ticker: str, **overrides) -> dict:
    """一檔各項條件都寬鬆過關的股票，測試再針對單一欄位覆寫。"""
    row = {
        "ticker": ticker,
        "name": f"測試{ticker}",
        "market": "上市",
        "industry": "半導體業",
        "price": 100.0,
        "market_cap": 50_000.0,
        "avg_volume": 5_000_000.0,
        "roe": 15.0,
        "roa": 8.0,
        "gross_margin": 30.0,
        "operating_margin": 15.0,
        "net_margin": 12.0,
        "revenue_growth_yoy": 10.0,
        "eps_growth_yoy": 15.0,
        "debt_ratio": 40.0,
        "current_ratio": 180.0,
        "pe_ratio": 15.0,
        "pb_ratio": 2.0,
        "dividend_yield": 4.0,
        "payout_coverage": 3.0,
        "beta": 0.9,
        "net_income_positive": True,
    }
    row.update(overrides)
    return row


def make_df(n: int = 20, **overrides) -> pd.DataFrame:
    return pd.DataFrame([make_row(f"{1000 + i}", **overrides) for i in range(n)])


class CapWeightsTest(unittest.TestCase):
    def test_cap_is_respected_after_renormalisation(self):
        # 原本的寫法（clip 後直接除以總和）在這組數字上會讓最大權重回到
        # 12% 以上；迭代版本必須守住上限。分數差距刻意拉大以逼出這個情況。
        raw = pd.Series([100.0, 90.0, 20.0, 10.0, 5.0, 3.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        w = _cap_weights(raw, cap=12.0)
        self.assertLessEqual(w.max(), 12.0 + 1e-6)
        self.assertAlmostEqual(w.sum(), 100.0, places=1)

    def test_equal_scores_split_evenly(self):
        w = _cap_weights(pd.Series([50.0] * 10), cap=12.0)
        self.assertAlmostEqual(w.sum(), 100.0, places=1)
        self.assertTrue(np.allclose(w, 10.0, atol=0.05))

    def test_infeasible_cap_leaves_cash(self):
        # 10 檔 × 8% 上限最多只能配 80%，不該為了湊 100% 把權重推過上限。
        w = _cap_weights(pd.Series([50.0] * 10), cap=8.0)
        self.assertLessEqual(w.max(), 8.0 + 1e-6)
        self.assertAlmostEqual(w.sum(), 80.0, places=1)
        self.assertTrue(np.isfinite(w).all())


class ScoringTest(unittest.TestCase):
    def test_missing_factor_does_not_produce_nan_score(self):
        df = make_df(20, eps_growth_yoy=np.nan, pe_ratio=np.nan)
        portfolio = AggressiveStableSelector(df).run(top_n=15)
        self.assertFalse(portfolio["total_score"].isna().any())
        self.assertTrue((portfolio["data_completeness"] < 100).all())

    def test_completeness_is_100_when_all_factors_present(self):
        portfolio = AggressiveStableSelector(make_df(20)).run(top_n=15)
        self.assertTrue((portfolio["data_completeness"] == 100).all())

    def test_core_field_missing_is_excluded(self):
        df = make_df(19)
        df = pd.concat([df, pd.DataFrame([make_row("9999", roe=np.nan)])], ignore_index=True)
        portfolio = AggressiveStableSelector(df).run(top_n=20)
        self.assertNotIn("9999", portfolio["ticker"].tolist())

    def test_higher_roe_scores_higher(self):
        df = pd.DataFrame([make_row("1001", roe=10.0), make_row("1002", roe=28.0)])
        portfolio = AggressiveStableSelector(df).run(top_n=2)
        self.assertEqual(portfolio.iloc[0]["ticker"], "1002")


class FilterTest(unittest.TestCase):
    def test_aggressive_rejects_low_roe(self):
        df = make_df(5, roe=3.0)
        self.assertTrue(AggressiveStableSelector(df).run().empty)

    def test_retirement_rejects_high_beta(self):
        df = make_df(5, beta=1.8)
        self.assertTrue(RetirementPlanningSelector(df).run().empty)

    def test_retirement_allows_missing_beta(self):
        # beta 缺值放行（評分端扣分），否則上櫃新股會整批消失。
        df = make_df(5, beta=np.nan)
        self.assertEqual(len(RetirementPlanningSelector(df).run()), 5)

    def test_retirement_requires_positive_earnings(self):
        df = make_df(5, net_income_positive=False)
        self.assertTrue(RetirementPlanningSelector(df).run().empty)

    def test_missing_current_ratio_passes_for_financials(self):
        # 金融業資產負債表沒有流動資產／流動負債欄位，不能因此被篩掉。
        df = make_df(5, current_ratio=np.nan)
        self.assertEqual(len(AggressiveStableSelector(df).run()), 5)


class WeightCapPerStrategyTest(unittest.TestCase):
    def test_strategy_caps_match_spec(self):
        a = AggressiveStableSelector(make_df(20)).run(top_n=15)
        b = RetirementPlanningSelector(make_df(20)).run(top_n=15)
        self.assertLessEqual(a["weight_pct"].max(), 12.0 + 1e-6)
        self.assertLessEqual(b["weight_pct"].max(), 8.0 + 1e-6)
        self.assertAlmostEqual(a["weight_pct"].sum(), 100.0, places=0)
        self.assertAlmostEqual(b["weight_pct"].sum(), 100.0, places=0)

    def test_summary_weights_by_position_size(self):
        df = pd.DataFrame(
            [make_row("1001", dividend_yield=8.0, roe=20.0), make_row("1002", dividend_yield=2.5)]
        )
        portfolio = RetirementPlanningSelector(df).run(top_n=2)
        summary = portfolio_summary(portfolio)
        self.assertEqual(summary["檔數"], 2)
        self.assertTrue(2.5 <= summary["加權股息率(%)"] <= 8.0)


class PercentileNormalizeTest(unittest.TestCase):
    def test_percentile_discriminates_when_absolute_tops_out(self):
        # ROE 的絕對區間上限是 30%；三檔 ROE 都遠超過 30% 時，absolute 模式
        # 在這個因子上完全同分，percentile 模式必須還分得出高低。
        df = pd.DataFrame(
            [
                make_row("1001", roe=45.0),
                make_row("1002", roe=80.0),
                make_row("1003", roe=130.0),
            ]
        )
        absolute = AggressiveStableSelector(df, normalize="absolute").run(top_n=3)
        percentile = AggressiveStableSelector(df, normalize="percentile").run(top_n=3)

        self.assertEqual(absolute["score_roe"].nunique(), 1)
        self.assertEqual(percentile["score_roe"].nunique(), 3)
        self.assertEqual(percentile.iloc[0]["ticker"], "1003")

    def test_percentile_keeps_missing_values_missing(self):
        df = make_df(20, eps_growth_yoy=np.nan)
        portfolio = AggressiveStableSelector(df, normalize="percentile").run(top_n=15)
        self.assertFalse(portfolio["total_score"].isna().any())
        self.assertTrue((portfolio["data_completeness"] < 100).all())

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            AggressiveStableSelector(make_df(3), normalize="zscore")


class IndustryCapTest(unittest.TestCase):
    def _mixed_industries(self) -> pd.DataFrame:
        # 半導體 10 檔分數最高，其他產業各 3 檔分數較低。
        rows = [make_row(f"2{i:03d}", industry="半導體業", roe=25.0 - i * 0.1) for i in range(10)]
        for j, ind in enumerate(["金融業", "食品工業", "水泥工業"]):
            rows += [make_row(f"{3 + j}{i:03d}", industry=ind, roe=15.0 - i * 0.1) for i in range(3)]
        return pd.DataFrame(rows)

    def test_without_cap_one_industry_dominates(self):
        portfolio = AggressiveStableSelector(self._mixed_industries()).run(top_n=10)
        self.assertEqual((portfolio["industry"] == "半導體業").sum(), 10)

    def test_cap_limits_each_industry(self):
        portfolio = AggressiveStableSelector(self._mixed_industries()).run(
            top_n=10, max_per_industry=3
        )
        self.assertLessEqual(portfolio["industry"].value_counts().max(), 3)
        self.assertGreater(portfolio["industry"].nunique(), 1)

    def test_cap_still_prefers_higher_scores_within_industry(self):
        portfolio = AggressiveStableSelector(self._mixed_industries()).run(
            top_n=10, max_per_industry=2
        )
        semis = portfolio[portfolio["industry"] == "半導體業"]
        self.assertEqual(semis["ticker"].tolist(), ["2000", "2001"])

    def test_cap_shortfall_does_not_crash(self):
        # 產業上限太嚴，湊不到 top_n 檔時要正常回傳較少檔數。
        df = make_df(10, industry="半導體業")
        portfolio = AggressiveStableSelector(df).run(top_n=10, max_per_industry=2)
        self.assertEqual(len(portfolio), 2)
        self.assertTrue(np.isfinite(portfolio["weight_pct"]).all())


class EmptyUniverseTest(unittest.TestCase):
    def test_empty_input_does_not_crash(self):
        cols = list(make_row("1000").keys())
        empty = pd.DataFrame(columns=cols)
        self.assertTrue(AggressiveStableSelector(empty).run().empty)
        self.assertEqual(portfolio_summary(pd.DataFrame()), {})


if __name__ == "__main__":
    unittest.main()
