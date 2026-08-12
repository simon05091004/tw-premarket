"""
個股基本面資料層 —— 上市（TWSE）+ 上櫃（TPEx）全市場。

設計原則沿用 fetch.py:
1. 每個 fetcher 獨立 try/except，單一資料源失敗回 None，不讓整份篩選掛掉。
2. 只回傳原始數字，解讀（評分、排序）交給 screener.py。
3. 缺值一律 NaN，讓評分端明確知道「這項沒有資料」而不是「這項是 0」。

資料全部來自證交所與櫃買中心的公開 OpenAPI，不需要任何 token。

—— 公開 API 拿不到的欄位，以及本模組的處理方式 ————————————————
* 利息保障倍數：OpenAPI 的綜合損益表沒有把「利息費用」單獨列出（只有
  「營業外收入及支出」淨額），無法還原。→ 不產生此欄位，篩選端改用
  「營業利益為正 + 負債比」這組可得的等價條件。
* 自由現金流：OpenAPI 完全沒有現金流量表。→ 改用股利分派表裡的
  「可分配盈餘 ÷ 現金股利總額」（payout_coverage）衡量配息可持續性，
  這比 FCF yield 更直接對應「這個股息發得下去嗎」。
* EPS 年增率：每個財報 endpoint 只給「最新一期」，沒有去年同期。
  → 本模組會把每次抓到的財報存進 data/cache/statements.csv，累積四季
  之後就能算出真實 YoY；在那之前 eps_growth_yoy 為 NaN。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .fetch import _fetch, _num

log = logging.getLogger(__name__)

TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
TWSE_MI_INDEX_HIST = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_OTC_HIST = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"

CACHE_DIR = Path("data/cache")

# 財報依行業別拆成六張表，欄位名稱不一致，統一映射到內部名稱。
SECTOR_SUFFIXES = ["ci", "mim", "basi", "fh", "ins", "bd"]

BS_FIELDS = {
    "total_assets": ["資產總計", "資產總額"],
    "total_liabilities": ["負債總計", "負債總額"],
    "total_equity": ["權益總計", "權益總額"],
    "equity_parent": ["歸屬於母公司業主之權益合計", "歸屬於母公司業主之權益"],
    "current_assets": ["流動資產"],
    "current_liabilities": ["流動負債"],
    "book_value_per_share": ["每股參考淨值"],
}

IS_FIELDS = {
    "revenue": ["營業收入", "淨收益"],
    "gross_profit": ["營業毛利（毛損）淨額", "營業毛利（毛損）"],
    "operating_income": ["營業利益（損失）"],
    "pretax_income": [
        "稅前淨利（淨損）",
        "繼續營業單位稅前淨利（淨損）",
        "繼續營業單位稅前損益",
    ],
    "net_income": ["本期淨利（淨損）", "本期稅後淨利（淨損）"],
    "net_income_parent": ["淨利（淨損）歸屬於母公司業主", "淨利（損）歸屬於母公司業主"],
    "eps": ["基本每股盈餘（元）"],
}

CODE_FIELDS = ["公司代號", "SecuritiesCompanyCode"]
NAME_FIELDS = ["公司名稱", "CompanyName"]
YEAR_FIELDS = ["年度", "Year"]
SEASON_FIELDS = ["季別", "Season"]


def _get_json(url: str, params: dict | None = None):
    return _fetch("GET", url, lambda r: r.json(), params=params)


def _pick(row: dict, names: list[str]):
    """財報欄位名在不同行業別的表裡不一樣，取第一個存在且有值的。"""
    for n in names:
        if n in row:
            v = _num(row[n])
            if v is not None:
                return v
    return np.nan


def _pick_raw(row: dict, names: list[str]) -> str | None:
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return None


def _safe_div(a, b):
    """b 為 0、NaN 或負數（權益為負等）時回 NaN，不讓比率變成無意義的極端值。"""
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    return np.where((b.isna()) | (b <= 0) | (a.isna()), np.nan, a / b.replace(0, np.nan))


# ---------------------------------------------------------------------------
# 1. 每日行情：股價、成交量、本益比、股價淨值比、殖利率
# ---------------------------------------------------------------------------


def fetch_quotes() -> pd.DataFrame:
    """上市 + 上櫃當日收盤行情與評價指標。"""
    rows: list[dict] = []

    twse = _get_json(f"{TWSE_OPENAPI}/exchangeReport/STOCK_DAY_ALL") or []
    for r in twse:
        rows.append(
            {
                "ticker": r.get("Code", "").strip(),
                "name": r.get("Name", "").strip(),
                "market": "上市",
                "price": _num(r.get("ClosingPrice")),
                "day_volume": _num(r.get("TradeVolume")),
            }
        )

    tpex = _get_json(f"{TPEX_OPENAPI}/tpex_mainboard_daily_close_quotes") or []
    for r in tpex:
        rows.append(
            {
                "ticker": r.get("SecuritiesCompanyCode", "").strip(),
                "name": r.get("CompanyName", "").strip(),
                "market": "上櫃",
                "price": _num(r.get("Close")),
                "day_volume": _num(r.get("TradingShares")),
            }
        )

    df = pd.DataFrame(rows)
    log.info("行情: 上市 %d 檔、上櫃 %d 檔", len(twse), len(tpex))

    # 評價指標（本益比、殖利率、股價淨值比）
    val_rows: list[dict] = []
    for r in _get_json(f"{TWSE_OPENAPI}/exchangeReport/BWIBBU_ALL") or []:
        val_rows.append(
            {
                "ticker": r.get("Code", "").strip(),
                "pe_ratio": _num(r.get("PEratio")),
                "pb_ratio": _num(r.get("PBratio")),
                "dividend_yield": _num(r.get("DividendYield")),
            }
        )
    for r in _get_json(f"{TPEX_OPENAPI}/tpex_mainboard_peratio_analysis") or []:
        val_rows.append(
            {
                "ticker": r.get("SecuritiesCompanyCode", "").strip(),
                "pe_ratio": _num(r.get("PriceEarningRatio")),
                "pb_ratio": _num(r.get("PriceBookRatio")),
                "dividend_yield": _num(r.get("YieldRatio")),
            }
        )
    val = pd.DataFrame(val_rows).drop_duplicates("ticker")

    df = df.merge(val, on="ticker", how="left")
    return df[df["ticker"].str.match(r"^\d{4}$", na=False)].copy()


def fetch_monthly_volume() -> pd.DataFrame:
    """上市個股月成交資訊 —— 用來算日均量，比單日成交量穩健。上櫃無對應 API。"""
    rows = _get_json(f"{TWSE_OPENAPI}/exchangeReport/FMSRFK_ALL") or []
    if not rows:
        return pd.DataFrame(columns=["ticker", "monthly_volume"])
    df = pd.DataFrame(
        [
            {
                "ticker": r.get("Code", "").strip(),
                "month": r.get("Month", ""),
                "volume": _num(r.get("TradeVolumeB")),
            }
            for r in rows
        ]
    )
    latest = df["month"].max()
    df = df[df["month"] == latest]
    # 一個月約 20 個交易日；月報本身不提供交易日數。
    df["monthly_volume"] = df["volume"] / 20.0
    return df[["ticker", "monthly_volume"]]


# ---------------------------------------------------------------------------
# 2. 公司基本資料：股本、發行股數、產業別
# ---------------------------------------------------------------------------


def fetch_profiles() -> pd.DataFrame:
    rows: list[dict] = []
    for r in _get_json(f"{TWSE_OPENAPI}/opendata/t187ap03_L") or []:
        rows.append(
            {
                "ticker": r.get("公司代號", "").strip(),
                "short_name": r.get("公司簡稱", "").strip(),
                "shares_outstanding": _num(r.get("已發行普通股數或TDR原股發行股數")),
                "listing_date": r.get("上市日期", "").strip(),
            }
        )
    for r in _get_json(f"{TPEX_OPENAPI}/mopsfin_t187ap03_O") or []:
        rows.append(
            {
                "ticker": r.get("SecuritiesCompanyCode", "").strip(),
                "short_name": r.get("CompanyAbbreviation", "").strip(),
                "shares_outstanding": _num(r.get("IssueShares")),
                "listing_date": r.get("DateOfListing", "").strip(),
            }
        )
    return pd.DataFrame(rows).drop_duplicates("ticker")


# ---------------------------------------------------------------------------
# 3. 月營收 —— 營收年增率與產業別（中文）
# ---------------------------------------------------------------------------


def fetch_revenue() -> pd.DataFrame:
    rows: list[dict] = []
    for url in (f"{TWSE_OPENAPI}/opendata/t187ap05_L", f"{TPEX_OPENAPI}/mopsfin_t187ap05_O"):
        for r in _get_json(url) or []:
            rows.append(
                {
                    "ticker": _pick_raw(r, CODE_FIELDS) or "",
                    "industry": r.get("產業別", "").strip(),
                    "revenue_month": r.get("資料年月", "").strip(),
                    # 用「累計營收 YoY」而非單月：單月營收波動大，累計較能反映趨勢。
                    "revenue_growth_yoy": _num(r.get("累計營業收入-前期比較增減(%)")),
                    "revenue_growth_mom_yoy": _num(r.get("營業收入-去年同月增減(%)")),
                    "monthly_revenue": _num(r.get("營業收入-當月營收")),
                }
            )
    return pd.DataFrame(rows).drop_duplicates("ticker")


# ---------------------------------------------------------------------------
# 4. 財報：資產負債表 + 綜合損益表（六種行業別合併）
# ---------------------------------------------------------------------------


def _fetch_statement_group(kind: str) -> pd.DataFrame:
    """kind: 'bs'（資產負債表 t187ap07）或 'is'（綜合損益表 t187ap06）。"""
    prefix = "t187ap07" if kind == "bs" else "t187ap06"
    fields = BS_FIELDS if kind == "bs" else IS_FIELDS
    rows: list[dict] = []

    urls = [f"{TWSE_OPENAPI}/opendata/{prefix}_L_{s}" for s in SECTOR_SUFFIXES]
    urls += [f"{TPEX_OPENAPI}/mopsfin_{prefix}_O_{s}" for s in SECTOR_SUFFIXES]

    for url in urls:
        data = _get_json(url)
        if not data:
            continue
        sector = url.rsplit("_", 1)[-1]
        for r in data:
            code = _pick_raw(r, CODE_FIELDS)
            if not code:
                continue
            rec = {
                "ticker": code,
                "sector_type": sector,
                "fiscal_year": _pick(r, YEAR_FIELDS),
                "fiscal_season": _pick(r, SEASON_FIELDS),
            }
            for out, names in fields.items():
                rec[out] = _pick(r, names)
            rows.append(rec)

    df = pd.DataFrame(rows)
    log.info("%s: 取得 %d 筆", "資產負債表" if kind == "bs" else "綜合損益表", len(df))
    return df


def fetch_statements(cache_dir: Path = CACHE_DIR) -> pd.DataFrame:
    """
    合併資產負債表與綜合損益表，並與歷史快取合併。

    為什麼要快取：每個 endpoint 只給「最新一期」，而財報是陸續公布的
    （季報截止日前只有部分公司送件）。只用當期會讓還沒公布的公司整批消失。
    快取讓這些公司沿用上一期數字，同時累積出計算 YoY 需要的去年同期。
    """
    bs = _fetch_statement_group("bs")
    is_ = _fetch_statement_group("is")

    if bs.empty and is_.empty:
        log.warning("財報資料全部抓取失敗")
        cur = pd.DataFrame()
    else:
        key = ["ticker", "fiscal_year", "fiscal_season"]
        cur = bs.merge(is_.drop(columns=["sector_type"]), on=key, how="outer")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "statements.csv"

    if cache_file.exists():
        hist = pd.read_csv(cache_file, dtype={"ticker": str})
        combined = pd.concat([hist, cur], ignore_index=True)
    else:
        combined = cur

    if combined.empty:
        return combined

    combined = combined.dropna(subset=["fiscal_year", "fiscal_season"])
    combined = combined.drop_duplicates(
        subset=["ticker", "fiscal_year", "fiscal_season"], keep="last"
    )
    combined.to_csv(cache_file, index=False)
    log.info("財報快取: %d 筆（%d 家公司）", len(combined), combined["ticker"].nunique())
    return combined


def _latest_and_yoy(statements: pd.DataFrame) -> pd.DataFrame:
    """每家公司取最新一期，並附上去年同季的 EPS/淨利以計算 YoY。"""
    if statements.empty:
        return pd.DataFrame()

    df = statements.copy()
    df["period"] = df["fiscal_year"] * 10 + df["fiscal_season"]
    df = df.sort_values(["ticker", "period"])

    latest = df.groupby("ticker", as_index=False).last()

    # 去年同季 = 年度 -1、季別相同
    prior = df.copy()
    prior["period"] = (prior["fiscal_year"] + 1) * 10 + prior["fiscal_season"]
    prior = prior.groupby(["ticker", "period"], as_index=False).last()
    prior = prior[["ticker", "period", "eps", "net_income", "revenue"]].rename(
        columns={"eps": "eps_prior", "net_income": "net_income_prior", "revenue": "revenue_prior"}
    )

    out = latest.merge(prior, on=["ticker", "period"], how="left")
    return out


# ---------------------------------------------------------------------------
# 5. 股利分派 —— 殖利率的可持續性
# ---------------------------------------------------------------------------


def fetch_dividends() -> pd.DataFrame:
    rows: list[dict] = []

    for r in _get_json(f"{TWSE_OPENAPI}/opendata/t187ap45_L") or []:
        cash = (_num(r.get("股東配發-盈餘分配之現金股利(元/股)")) or 0.0) + (
            _num(r.get("股東配發-法定盈餘公積發放之現金(元/股)")) or 0.0
        ) + (_num(r.get("股東配發-資本公積發放之現金(元/股)")) or 0.0)
        rows.append(
            {
                "ticker": r.get("公司代號", "").strip(),
                "dividend_year": _num(r.get("股利年度")),
                "cash_dividend": cash,
                "distributable": _num(r.get("可分配盈餘(元)")),
                "cash_total": _num(r.get("股東配發-股東配發之現金(股利)總金額(元)")),
            }
        )

    for r in _get_json(f"{TPEX_OPENAPI}/mopsfin_t187ap39_O") or []:
        cash = (_num(r.get("股東配發內容-盈餘分配之現金股利(元/股)")) or 0.0) + (
            _num(r.get("股東配發內容-法定盈餘公積、資本公積發放之現金(元/股)")) or 0.0
        )
        rows.append(
            {
                "ticker": r.get("公司代號", "").strip(),
                "dividend_year": _num(r.get("股利年度")),
                "cash_dividend": cash,
                "distributable": _num(r.get("可分配盈餘(元)")),
                "cash_total": _num(r.get("股東配發內容-股東配發之現金(股利)總金額(元)")),
            }
        )

    if not rows:
        return pd.DataFrame(columns=["ticker", "cash_dividend", "payout_coverage"])

    df = pd.DataFrame(rows).dropna(subset=["dividend_year"])
    # 同一年度可能有多期（期別 1/2/…），先加總每股現金股利再取最新年度。
    latest_year = df.groupby("ticker")["dividend_year"].transform("max")
    df = df[df["dividend_year"] == latest_year]
    agg = df.groupby("ticker", as_index=False).agg(
        dividend_year=("dividend_year", "max"),
        cash_dividend=("cash_dividend", "sum"),
        distributable=("distributable", "max"),
        cash_total=("cash_total", "sum"),
    )
    # 配息可持續性：可分配盈餘能覆蓋這次現金股利幾倍。<1 表示在動用歷年累積。
    agg["payout_coverage"] = _safe_div(agg["distributable"], agg["cash_total"])
    return agg[["ticker", "dividend_year", "cash_dividend", "payout_coverage"]]


# ---------------------------------------------------------------------------
# 6. Beta —— 用月末全市場快照算月報酬，對加權指數回歸
# ---------------------------------------------------------------------------


def _month_ends(months: int) -> list[date]:
    """回傳最近 months 個月的月底日期（由舊到新，不含本月）。"""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    ends = []
    cursor = first_of_this_month
    for _ in range(months):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        nxt = (cursor + timedelta(days=32)).replace(day=1)
        ends.append(nxt - timedelta(days=1))
    return sorted(ends)


def _twse_closes_on(d: date) -> tuple[dict[str, float], float | None]:
    """某日全上市收盤價 + 加權指數。非交易日回 ({}, None)。"""
    data = _get_json(
        TWSE_MI_INDEX_HIST,
        {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"},
    )
    if not data or data.get("stat") != "OK":
        return {}, None

    closes: dict[str, float] = {}
    index_value: float | None = None
    for tb in data.get("tables", []):
        fields = tb.get("fields", [])
        if "證券代號" in fields:
            i_code, i_close = fields.index("證券代號"), fields.index("收盤價")
            for row in tb.get("data", []):
                code = str(row[i_code]).strip()
                px = _num(row[i_close])
                if len(code) == 4 and code.isdigit() and px:
                    closes[code] = px
        elif "指數" in fields and index_value is None:
            i_name, i_close = fields.index("指數"), fields.index("收盤指數")
            for row in tb.get("data", []):
                if str(row[i_name]).strip() == "發行量加權股價指數":
                    index_value = _num(row[i_close])
                    break
    return closes, index_value


def _tpex_closes_on(d: date) -> dict[str, float]:
    data = _get_json(
        TPEX_OTC_HIST, {"date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json"}
    )
    if not data:
        return {}
    closes: dict[str, float] = {}
    for tb in data.get("tables", []):
        fields = [str(f).strip() for f in tb.get("fields", [])]
        if "代號" not in fields:
            continue
        i_code = fields.index("代號")
        i_close = next((i for i, f in enumerate(fields) if f.startswith("收盤")), None)
        if i_close is None:
            continue
        for row in tb.get("data", []):
            code = str(row[i_code]).strip()
            px = _num(row[i_close])
            if len(code) == 4 and code.isdigit() and px:
                closes[code] = px
    return closes


def fetch_price_history(
    months: int = 25, cache_dir: Path = CACHE_DIR, include_tpex: bool = True
) -> pd.DataFrame:
    """
    月末全市場收盤價（long format: date, ticker, close, index）。

    一天一個請求就能拿到整個市場，所以 25 個月只要 ~25 次請求；證交所端有
    3 秒節流，首次執行約 1–2 分鐘，之後從快取增量補齊。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "price_history.csv"

    if cache_file.exists():
        hist = pd.read_csv(cache_file, dtype={"ticker": str})
        have = set(hist["date"].unique())
    else:
        hist = pd.DataFrame(columns=["date", "ticker", "close", "index"])
        have = set()

    new_rows: list[dict] = []
    for target in _month_ends(months):
        # 月底可能是假日，往前找最近的交易日（最多退 6 天）。
        if any(
            (target - timedelta(days=k)).isoformat() in have for k in range(7)
        ):
            continue
        for back in range(7):
            d = target - timedelta(days=back)
            closes, idx = _twse_closes_on(d)
            if not closes:
                continue
            for code, px in closes.items():
                new_rows.append(
                    {"date": d.isoformat(), "ticker": code, "close": px, "index": idx}
                )
            if include_tpex:
                for code, px in _tpex_closes_on(d).items():
                    new_rows.append(
                        {"date": d.isoformat(), "ticker": code, "close": px, "index": idx}
                    )
            log.info("價格快照 %s: %d 檔", d, len(closes))
            break
        else:
            log.warning("找不到 %s 附近的交易日，略過", target)

    if new_rows:
        hist = pd.concat([hist, pd.DataFrame(new_rows)], ignore_index=True)
        hist = hist.drop_duplicates(subset=["date", "ticker"], keep="last")
        hist.to_csv(cache_file, index=False)

    return hist


def compute_beta(history: pd.DataFrame, min_points: int = 12) -> pd.DataFrame:
    """對加權指數月報酬做 OLS 回歸，取斜率作為 beta。"""
    if history.empty:
        return pd.DataFrame(columns=["ticker", "beta"])

    df = history.copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["index"] = pd.to_numeric(df["index"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date")

    wide = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    idx = df.groupby("date")["index"].max().reindex(wide.index)

    stock_ret = wide.pct_change()
    mkt_ret = idx.pct_change()

    valid = mkt_ret.notna()
    stock_ret, mkt_ret = stock_ret[valid], mkt_ret[valid]
    if len(mkt_ret) < min_points:
        log.warning("價格樣本只有 %d 期，不足以估 beta（需 %d）", len(mkt_ret), min_points)
        return pd.DataFrame(columns=["ticker", "beta"])

    mkt_var = mkt_ret.var(ddof=1)
    betas = {}
    for ticker in stock_ret.columns:
        s = stock_ret[ticker]
        pair = pd.concat([s, mkt_ret], axis=1).dropna()
        if len(pair) < min_points or mkt_var in (0, np.nan):
            continue
        cov = pair.iloc[:, 0].cov(pair.iloc[:, 1])
        betas[ticker] = cov / mkt_var

    return pd.DataFrame({"ticker": list(betas), "beta": list(betas.values())})


# ---------------------------------------------------------------------------
# 7. 組裝
# ---------------------------------------------------------------------------


@dataclass
class Coverage:
    """每個欄位有多少檔股票拿得到值 —— 用來判斷篩選結果可不可信。"""

    total: int
    fields: dict[str, int]

    def as_text(self) -> str:
        lines = [f"universe: {self.total} 檔"]
        for k, v in sorted(self.fields.items(), key=lambda x: -x[1]):
            pct = v / self.total * 100 if self.total else 0
            lines.append(f"  {k:24s} {v:5d} ({pct:5.1f}%)")
        return "\n".join(lines)


def build_universe(
    with_beta: bool = True, cache_dir: Path = CACHE_DIR
) -> tuple[pd.DataFrame, Coverage]:
    """回傳 screener.py 需要的完整欄位表。"""
    quotes = fetch_quotes()
    profiles = fetch_profiles()
    revenue = fetch_revenue()
    statements = _latest_and_yoy(fetch_statements(cache_dir))
    dividends = fetch_dividends()
    monthly_vol = fetch_monthly_volume()

    df = quotes.merge(profiles, on="ticker", how="left")
    df = df.merge(revenue, on="ticker", how="left")
    df = df.merge(monthly_vol, on="ticker", how="left")
    if not statements.empty:
        df = df.merge(statements, on="ticker", how="left")
    else:
        for col in list(BS_FIELDS) + list(IS_FIELDS) + [
            "fiscal_year",
            "fiscal_season",
            "eps_prior",
            "net_income_prior",
            "revenue_prior",
        ]:
            df[col] = np.nan
    if not dividends.empty:
        df = df.merge(dividends, on="ticker", how="left")
    else:
        df["cash_dividend"] = np.nan
        df["payout_coverage"] = np.nan

    if with_beta:
        history = fetch_price_history(cache_dir=cache_dir)
        df = df.merge(compute_beta(history), on="ticker", how="left")
    else:
        df["beta"] = np.nan

    df = _derive_metrics(df)

    tracked = [
        "price",
        "market_cap",
        "roe",
        "gross_margin",
        "net_margin",
        "revenue_growth_yoy",
        "eps_growth_yoy",
        "debt_ratio",
        "current_ratio",
        "pe_ratio",
        "pb_ratio",
        "dividend_yield",
        "payout_coverage",
        "beta",
        "avg_volume",
    ]
    cov = Coverage(
        total=len(df), fields={c: int(df[c].notna().sum()) for c in tracked if c in df}
    )
    return df, cov


def _derive_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """把原始財報數字換算成選股程式需要的比率。單位：金額百萬元、比率百分比。"""
    df = df.copy()

    # 財報金額單位是「千元」，統一換算成百萬元。
    for col in [
        "total_assets",
        "total_liabilities",
        "total_equity",
        "equity_parent",
        "current_assets",
        "current_liabilities",
        "revenue",
        "gross_profit",
        "operating_income",
        "pretax_income",
        "net_income",
        "net_income_parent",
        "revenue_prior",
        "net_income_prior",
    ]:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce") / 1000.0

    # 市值（百萬元）= 收盤價 × 已發行股數
    df["market_cap"] = df["price"] * df["shares_outstanding"] / 1e6

    # 季報是「累計至當季」，年化倍數 = 4 / 季別。
    season = pd.to_numeric(df.get("fiscal_season"), errors="coerce")
    annualize = np.where(season.notna() & (season > 0), 4.0 / season, np.nan)

    equity = df["equity_parent"].fillna(df["total_equity"])
    net_income = df["net_income_parent"].fillna(df["net_income"])

    df["roe"] = _safe_div(net_income * annualize, equity) * 100
    df["roa"] = _safe_div(net_income * annualize, df["total_assets"]) * 100
    df["gross_margin"] = _safe_div(df["gross_profit"], df["revenue"]) * 100
    df["operating_margin"] = _safe_div(df["operating_income"], df["revenue"]) * 100
    df["net_margin"] = _safe_div(net_income, df["revenue"]) * 100

    df["debt_ratio"] = _safe_div(df["total_liabilities"], df["total_assets"]) * 100
    df["current_ratio"] = _safe_div(df["current_assets"], df["current_liabilities"]) * 100

    # EPS YoY：要有去年同季才算得出來（快取累積滿一年後才有值）。
    eps, eps_prior = pd.to_numeric(df["eps"], errors="coerce"), pd.to_numeric(
        df.get("eps_prior"), errors="coerce"
    )
    df["eps_growth_yoy"] = np.where(
        eps_prior.notna() & (eps_prior > 0), (eps - eps_prior) / eps_prior * 100, np.nan
    )
    df["eps_annualized"] = eps * annualize

    # 流動性：上市用月均量，上櫃只有當日量。
    df["avg_volume"] = df["monthly_volume"].fillna(df["day_volume"])

    df["net_income_positive"] = net_income > 0
    return df


def main() -> None:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description="抓取台股全市場基本面資料")
    parser.add_argument("--out", default="data/universe.csv")
    parser.add_argument("--no-beta", action="store_true", help="跳過 beta（省 1-2 分鐘）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df, cov = build_universe(with_beta=not args.no_beta)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(cov.as_text())
    print(f"\n已寫入 {out}（{len(df)} 檔）")


if __name__ == "__main__":  # pragma: no cover
    main()
