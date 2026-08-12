"""
兩套選股評分程式。

A 套「積極穩健型」：高 ROE + 成長動能 + 合理估值，單一股權重上限 12%。
B 套「退休規畫型」：高股息 + 低波動 + 配息可持續，單一股權重上限 8%。

資料來自 fundamentals.build_universe()。相對於原始設計，有三處為了接真實
資料而調整，都是因為證交所／櫃買公開 API 拿不到原欄位：

1. interest_coverage（利息保障倍數）→ 改用 operating_margin > 0。
   OpenAPI 的綜合損益表沒有單列利息費用，無法還原這個倍數。
2. free_cash_flow（自由現金流）→ 改用 payout_coverage（可分配盈餘 ÷ 現金
   股利總額）。OpenAPI 沒有現金流量表；就「配息發不發得下去」這個問題而言，
   盈餘覆蓋率是更直接的指標。
3. 缺值不再被當成 0。原本 NaN 會讓比較運算回 False 而被靜默篩掉，或讓
   total_score 整個變成 NaN；現在缺值的因子以中性分 0.5 計入，並在
   data_completeness 欄位記錄該檔有多少比例的因子拿得到真實資料。

輸出是量化篩選結果，不是投資建議。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# 這些欄位缺值就無法判斷基本面，該檔直接排除，不進評分。
CORE_FIELDS = ["market_cap", "price", "avg_volume", "roe", "debt_ratio"]


def _normalize_score(series: pd.Series, min_val: float, max_val: float, higher_better: bool):
    """線性映射到 0–1，超出區間夾住。NaN 維持 NaN，交由評分階段處理。"""
    s = pd.to_numeric(series, errors="coerce")
    normalized = ((s - min_val) / (max_val - min_val)).clip(0, 1)
    return normalized if higher_better else 1 - normalized


def _percentile_score(series: pd.Series, higher_better: bool):
    """
    同期百分位排名，0–1。

    絕對區間（例如 ROE 5–30%）在極端年份會失去區辨力：2026 上半年記憶體
    循環讓入選股 ROE 普遍 40–135%，全部打到區間上限同分 1.0，前段排序其實
    只剩估值尾差在決定。百分位改成「跟同期其他候選股比」，不會被打頂。

    代價是分數變成相對值 —— 換一個年份，同一檔股票的分數會不一樣。
    """
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() <= 1:
        return pd.Series(np.where(s.notna(), 0.5, np.nan), index=s.index)
    ranked = s.rank(pct=True, na_option="keep")
    return ranked if higher_better else 1 - ranked


def _at_least(series: pd.Series, threshold: float, allow_missing: bool) -> pd.Series:
    """>= 比較。allow_missing=True 時缺值視為通過（缺值會在評分端被扣分）。"""
    s = pd.to_numeric(series, errors="coerce")
    ok = s >= threshold
    return ok | s.isna() if allow_missing else ok.fillna(False)


def _at_most(series: pd.Series, threshold: float, allow_missing: bool) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    ok = s <= threshold
    return ok | s.isna() if allow_missing else ok.fillna(False)


def _cap_weights(weights: pd.Series, cap: float) -> pd.Series:
    """
    夾上限後重新正規化，反覆到收斂。

    原本的寫法是 clip 一次再除以總和 —— 除法會把剩下的權重又推回上限之上，
    12% 的上限實際上守不住。這裡改成迭代：每輪把超標的固定在上限，剩餘額度
    按比例分配給未超標的，直到沒有人超標。

    檔數 × 上限 < 100% 時無解（例如 8 檔配 12% 上限最多只能配到 96%）。
    這種情況全部給到上限、其餘留現金，而不是硬把權重推過上限。
    """
    w = weights.astype(float).copy()
    if len(w) * cap <= 100:
        log.info("檔數 %d × 上限 %.1f%% = %.1f%%，不足 100%%，其餘留現金", len(w), cap, len(w) * cap)
        return pd.Series(cap, index=w.index).round(2)

    w = w / w.sum() * 100
    for _ in range(100):
        over = w > cap + 1e-9
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        room = w[~over]
        if room.empty or room.sum() <= 0:
            break
        w[~over] = room + excess * room / room.sum()
    return w.round(2)


class _BaseSelector:
    """兩套共用的篩選 → 評分 → 配置流程。"""

    name = ""
    weight_cap = 10.0
    weight_exponent = 1.0
    top_n = 15
    tiers: list[tuple[float, str]] = []

    def __init__(self, df: pd.DataFrame, normalize: str = "absolute"):
        if normalize not in {"absolute", "percentile"}:
            raise ValueError(f"normalize 只能是 absolute 或 percentile，收到 {normalize!r}")
        self.df = df.copy()
        self.normalize = normalize
        self.filtered: pd.DataFrame | None = None
        self.scores: pd.DataFrame | None = None
        self.selected: pd.DataFrame | None = None

    def _score(self, series: pd.Series, min_val: float, max_val: float, higher_better: bool):
        """依 normalize 模式評分。百分位模式下 min_val/max_val 不使用。"""
        if self.normalize == "percentile":
            return _percentile_score(series, higher_better)
        return _normalize_score(series, min_val, max_val, higher_better)

    # -- 子類別實作 --------------------------------------------------------
    def filter_basic(self) -> pd.DataFrame:
        raise NotImplementedError

    def _factors(self, df: pd.DataFrame) -> dict[str, tuple[pd.Series, float]]:
        """回傳 {因子名稱: (0–1 分數, 權重)}。"""
        raise NotImplementedError

    # -- 共用流程 ----------------------------------------------------------
    def _drop_incomplete(self, df: pd.DataFrame) -> pd.DataFrame:
        before = len(df)
        out = df.dropna(subset=[c for c in CORE_FIELDS if c in df])
        if before != len(out):
            log.info("%s 核心欄位缺值排除：%d 檔", self.name, before - len(out))
        return out

    def calculate_scores(self, filtered_df: pd.DataFrame) -> pd.DataFrame:
        df = filtered_df.copy()
        factors = self._factors(df)

        total = pd.Series(0.0, index=df.index)
        available = pd.Series(0.0, index=df.index)
        for key, (score, weight) in factors.items():
            df[f"score_{key}"] = score
            # 缺值以中性分 0.5 計入，避免整列變成 NaN 或被當成 0 分。
            total += score.fillna(0.5) * weight
            available += score.notna().astype(float) * weight

        df["total_score"] = (total * 100).clip(0, 100)
        df["data_completeness"] = (available / sum(w for _, w in factors.values()) * 100).round(1)
        return df.sort_values("total_score", ascending=False)

    def _pick(self, top_n: int, max_per_industry: int | None) -> pd.DataFrame:
        """依分數由高到低取 top_n 檔；有產業上限時，額滿的產業跳過往下取。"""
        if not max_per_industry:
            return self.scores.head(top_n).copy()

        industries = self.scores.get("industry")
        if industries is None:
            log.warning("資料沒有 industry 欄位，忽略產業上限")
            return self.scores.head(top_n).copy()

        picked: list = []
        counts: dict[str, int] = {}
        for idx, industry in industries.items():
            key = str(industry) if pd.notna(industry) and str(industry).strip() else "未分類"
            if counts.get(key, 0) >= max_per_industry:
                continue
            counts[key] = counts.get(key, 0) + 1
            picked.append(idx)
            if len(picked) >= top_n:
                break

        if len(picked) < top_n:
            log.info(
                "%s 產業上限 %d 檔，候選池只湊到 %d 檔（目標 %d）",
                self.name,
                max_per_industry,
                len(picked),
                top_n,
            )
        return self.scores.loc[picked].copy()

    def allocate_positions(
        self, top_n: int | None = None, max_per_industry: int | None = None
    ) -> pd.DataFrame:
        top_n = top_n or self.top_n
        selected = self._pick(top_n, max_per_industry)
        if selected.empty:
            return selected

        selected["weight_pct"] = _cap_weights(
            selected["total_score"] ** self.weight_exponent, self.weight_cap
        )

        conds = [selected["total_score"] >= t for t, _ in self.tiers]
        selected["position_type"] = np.select(
            conds, [label for _, label in self.tiers], default="觀察名單"
        )
        self.selected = selected
        return selected

    def run(
        self, top_n: int | None = None, max_per_industry: int | None = None
    ) -> pd.DataFrame:
        self.filtered = self.filter_basic()
        if self.filtered.empty:
            log.warning("%s 篩選後沒有任何標的", self.name)
            self.scores = self.filtered
            return self.filtered
        self.scores = self.calculate_scores(self.filtered)
        return self.allocate_positions(top_n, max_per_industry)


class AggressiveStableSelector(_BaseSelector):
    """A 套：積極穩健型 —— 成長與穩健平衡，持有 3–5 年。"""

    name = "A 套 積極穩健型"
    weight_cap = 12.0
    weight_exponent = 1.5
    tiers = [(70, "核心持倉"), (55, "主要持倉"), (40, "衛星持倉")]

    def filter_basic(self) -> pd.DataFrame:
        df = self._drop_incomplete(self.df)
        mask = (
            _at_least(df["market_cap"], 500, False)  # 市值 >= 5 億（單位：百萬元）
            & _at_least(df["roe"], 8, False)
            & _at_most(df["debt_ratio"], 70, False)
            & _at_least(df["current_ratio"], 80, True)  # 金融業無流動比，缺值放行
            & _at_least(df["operating_margin"], 0, True)  # 取代利息保障倍數
            & _at_least(df["avg_volume"], 300_000, False)
            & _at_least(df["revenue_growth_yoy"], -10, True)
        )
        out = df[mask].copy()
        log.info("%s 基本條件：%d → %d 檔", self.name, len(df), len(out))
        return out

    def _factors(self, df):
        rev_growth = self._score(df["revenue_growth_yoy"], -10, 40, True)
        eps_growth = self._score(df["eps_growth_yoy"], -20, 60, True)

        pe = self._score(df["pe_ratio"], 5, 50, False)
        pb = self._score(df["pb_ratio"], 0.3, 10, False)
        valuation = pe.fillna(0.5) * 0.6 + pb.fillna(0.5) * 0.4
        valuation[pe.isna() & pb.isna()] = np.nan

        debt = self._score(df["debt_ratio"], 10, 90, False)
        current = self._score(df["current_ratio"], 60, 350, True)
        # 原設計此處是自由現金流；OpenAPI 無現金流量表，改用稅後淨利率作為
        # 現金產生能力的代理指標。
        profitability = self._score(df["net_margin"], 0, 30, True)
        health = debt.fillna(0.5) * 0.3 + current.fillna(0.5) * 0.3 + profitability.fillna(0.5) * 0.4
        health[debt.isna() & current.isna() & profitability.isna()] = np.nan

        momentum = rev_growth.fillna(0.5) * 0.5 + eps_growth.fillna(0.5) * 0.5
        momentum[rev_growth.isna() & eps_growth.isna()] = np.nan

        return {
            "roe": (self._score(df["roe"], 5, 30, True), 0.20),
            "revenue_growth": (rev_growth, 0.15),
            "eps_growth": (eps_growth, 0.15),
            "gross_margin": (self._score(df["gross_margin"], 5, 60, True), 0.10),
            "valuation": (valuation, 0.15),
            "financial_health": (health, 0.15),
            "momentum": (momentum, 0.10),
        }


class RetirementPlanningSelector(_BaseSelector):
    """B 套：退休規畫型 —— 穩定現金流與資本保全，持有 10 年以上。"""

    name = "B 套 退休規畫型"
    weight_cap = 8.0
    weight_exponent = 1.2
    tiers = [(65, "核心收息"), (50, "主要收息"), (35, "衛星收息")]

    def filter_basic(self) -> pd.DataFrame:
        df = self._drop_incomplete(self.df)
        mask = (
            _at_least(df["market_cap"], 500, False)
            & _at_least(df["roe"], 6, False)
            & _at_most(df["debt_ratio"], 75, False)
            & _at_least(df["current_ratio"], 80, True)
            & _at_least(df["operating_margin"], 0, True)
            & df["net_income_positive"].fillna(False)  # 取代「自由現金流為正」
            & _at_least(df["dividend_yield"], 2.0, False)
            & _at_most(df["beta"], 1.2, True)  # beta 缺值放行，評分端扣分
            & _at_least(df["avg_volume"], 300_000, False)
            & _at_least(df["revenue_growth_yoy"], -15, True)
        )
        out = df[mask].copy()
        log.info("%s 基本條件：%d → %d 檔", self.name, len(df), len(out))
        return out

    def _factors(self, df):
        debt = self._score(df["debt_ratio"], 10, 90, False)
        current = self._score(df["current_ratio"], 50, 350, True)
        stability = debt.fillna(0.5) * 0.5 + current.fillna(0.5) * 0.5
        stability[debt.isna() & current.isna()] = np.nan

        beta = self._score(df["beta"], 0.2, 1.8, False)
        size = self._score(df["market_cap"], 300, 100_000, True)
        low_vol = beta.fillna(0.5) * 0.6 + size.fillna(0.5) * 0.4
        low_vol[beta.isna() & size.isna()] = np.nan

        roe = self._score(df["roe"], 3, 30, True)
        margin = self._score(df["net_margin"], 0, 30, True)
        growth_stability = self._score(df["revenue_growth_yoy"].abs(), 0, 40, False)
        roe_stability = (
            roe.fillna(0.5) * 0.4 + margin.fillna(0.5) * 0.3 + growth_stability.fillna(0.5) * 0.3
        )
        roe_stability[roe.isna() & margin.isna() & growth_stability.isna()] = np.nan

        pe = self._score(df["pe_ratio"], 5, 50, False)
        pb = self._score(df["pb_ratio"], 0.3, 8, False)
        valuation = pe.fillna(0.5) * 0.5 + pb.fillna(0.5) * 0.5
        valuation[pe.isna() & pb.isna()] = np.nan

        return {
            "dividend": (self._score(df["dividend_yield"], 1.5, 10, True), 0.25),
            # 原設計是 FCF yield；改用可分配盈餘對現金股利的覆蓋倍數。
            "sustainability": (self._score(df["payout_coverage"], 0.8, 5, True), 0.20),
            "financial_stability": (stability, 0.20),
            "low_volatility": (low_vol, 0.15),
            "roe_stability": (roe_stability, 0.10),
            "valuation_safety": (valuation, 0.10),
        }


REPORT_COLUMNS = [
    "ticker",
    "name",
    "market",
    "industry",
    "price",
    "total_score",
    "weight_pct",
    "position_type",
    "roe",
    "dividend_yield",
    "pe_ratio",
    "pb_ratio",
    "debt_ratio",
    "revenue_growth_yoy",
    "beta",
    "data_completeness",
]


def portfolio_summary(portfolio: pd.DataFrame) -> dict:
    """組合層級指標 —— 對照策略規格表的預估股息率與 Beta。"""
    if portfolio.empty:
        return {}
    w = portfolio["weight_pct"] / 100

    def wavg(col):
        s = pd.to_numeric(portfolio[col], errors="coerce")
        mask = s.notna()
        return float((s[mask] * w[mask]).sum() / w[mask].sum()) if mask.any() else float("nan")

    summary = {
        "檔數": len(portfolio),
        "加權股息率(%)": round(wavg("dividend_yield"), 2),
        "加權 Beta": round(wavg("beta"), 2),
        "加權 ROE(%)": round(wavg("roe"), 2),
        "加權本益比": round(wavg("pe_ratio"), 2),
        "最大單一權重(%)": round(portfolio["weight_pct"].max(), 2),
        "總配置(%)": round(portfolio["weight_pct"].sum(), 2),
    }
    if "industry" in portfolio:
        by_industry = portfolio.groupby("industry")["weight_pct"].sum().sort_values(ascending=False)
        top = by_industry.index[0]
        summary["最大產業"] = f"{top} {by_industry.iloc[0]:.1f}%（{(portfolio['industry'] == top).sum()} 檔）"
    return summary


def _format(portfolio: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in REPORT_COLUMNS if c in portfolio.columns]
    out = portfolio[cols].copy()
    for c in out.select_dtypes("number").columns:
        out[c] = out[c].round(2)
    return out


def main() -> None:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description="台股兩套選股程式")
    parser.add_argument("--universe", default="data/universe.csv", help="基本面資料 CSV")
    parser.add_argument("--refresh", action="store_true", help="重新抓資料，不用既有 CSV")
    parser.add_argument("--no-beta", action="store_true", help="跳過 beta 計算")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--outdir", default="data")
    parser.add_argument(
        "--normalize",
        choices=["absolute", "percentile"],
        default="absolute",
        help="absolute=固定區間（原設計）；percentile=同期百分位排名，不會被打頂",
    )
    parser.add_argument(
        "--max-per-industry",
        type=int,
        default=None,
        help="單一產業最多幾檔；不指定則不限制",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    universe_path = Path(args.universe)
    if args.refresh or not universe_path.exists():
        from .fundamentals import build_universe

        df, cov = build_universe(with_beta=not args.no_beta)
        universe_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(universe_path, index=False)
        print(cov.as_text(), "\n")
    else:
        df = pd.read_csv(universe_path, dtype={"ticker": str})
        print(f"讀取既有資料 {universe_path}（{len(df)} 檔）；要更新請加 --refresh\n")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for selector_cls, tag in (
        (AggressiveStableSelector, "a_aggressive"),
        (RetirementPlanningSelector, "b_retirement"),
    ):
        selector = selector_cls(df, normalize=args.normalize)
        portfolio = selector.run(top_n=args.top, max_per_industry=args.max_per_industry)
        print(f"\n{'=' * 70}\n{selector.name}（{args.normalize} 評分）\n{'=' * 70}")
        if portfolio.empty:
            print("篩選後沒有符合條件的標的。")
            continue
        print(_format(portfolio).to_string(index=False))
        print("\n組合摘要：")
        for k, v in portfolio_summary(portfolio).items():
            print(f"  {k}: {v}")
        path = outdir / f"portfolio_{tag}.csv"
        _format(portfolio).to_csv(path, index=False)
        print(f"  → {path}")

    print("\n※ 以上為公開資料的量化篩選結果，不是投資建議。")


if __name__ == "__main__":  # pragma: no cover
    main()
