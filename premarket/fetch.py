"""
資料抓取層。

設計原則:
1. 每個 fetcher 都獨立 try/except，任何一項失敗回傳 None，不讓整份報告掛掉。
2. 只回傳「原始數字」，不做任何解讀 —— 解讀交給 analyze.py。
3. 所有欄位缺值一律用 None，讓 prompt 端明確知道「這項沒有資料」。
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

TIMEOUT = 20
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _get_json(url: str, params: dict | None = None) -> Any | None:
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("GET %s 失敗: %s", url, exc)
        return None


def _post_csv(url: str, data: dict) -> list[list[str]] | None:
    try:
        r = requests.post(url, data=data, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        r.encoding = "big5"
        return list(csv.reader(io.StringIO(r.text)))
    except Exception as exc:  # noqa: BLE001
        log.warning("POST %s 失敗: %s", url, exc)
        return None


def _num(s: Any) -> float | None:
    """把證交所/期交所那種帶逗號、括號負號、破折號的字串轉成 float。"""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("+", "")
    if t in {"", "-", "--", "---", "N/A", "不適用"}:
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


# ---------------------------------------------------------------------------
# 美股 / 國際盤 (yfinance)
# ---------------------------------------------------------------------------

# 說明: 盤前報告在台北時間 07:00 執行，美股 04:00 已收盤，資料為最終值。
US_TICKERS = {
    "道瓊": "^DJI",
    "那斯達克": "^IXIC",
    "標普500": "^GSPC",
    "費城半導體": "^SOX",
    "VIX": "^VIX",
    "台積電ADR": "TSM",
    "聯電ADR": "UMC",
    "日月光ADR": "ASX",
    "WTI原油": "CL=F",
    "美債10年殖利率": "^TNX",
    "美債30年殖利率": "^TYX",
    "美元指數": "DX-Y.NYB",
    "美元兌台幣": "TWD=X",
    "日經225": "^N225",
    "韓國KOSPI": "^KS11",
}


def fetch_us_market() -> dict[str, dict]:
    """回傳 {名稱: {close, prev_close, change, change_pct, open, high, low}}。"""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance 未安裝")
        return {}

    out: dict[str, dict] = {}
    try:
        data = yf.download(
            list(US_TICKERS.values()),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("yfinance 下載失敗: %s", exc)
        return {}

    for name, sym in US_TICKERS.items():
        try:
            df = data[sym].dropna(subset=["Close"])
            if len(df) < 2:
                continue
            last, prev = df.iloc[-1], df.iloc[-2]
            close, pclose = float(last["Close"]), float(prev["Close"])
            out[name] = {
                "close": round(close, 2),
                "prev_close": round(pclose, 2),
                "change": round(close - pclose, 2),
                "change_pct": round((close - pclose) / pclose * 100, 2) if pclose else None,
                "open": round(float(last["Open"]), 2),
                "high": round(float(last["High"]), 2),
                "low": round(float(last["Low"]), 2),
                "date": df.index[-1].strftime("%Y-%m-%d"),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("解析 %s (%s) 失敗: %s", name, sym, exc)
    return out


# ---------------------------------------------------------------------------
# 期交所 TAIFEX
# ---------------------------------------------------------------------------

TAIFEX_DAILY = "https://www.taifex.com.tw/cht/3/futDataDown"
TAIFEX_INST = "https://www.taifex.com.tw/cht/3/futContractsDateDown"


def fetch_taifex_tx(trade_date: date) -> dict | None:
    """
    台指期日盤 + 夜盤(盤後交易時段)收盤。

    CSV 欄位含「交易時段」，值為「一般」或「盤後」。近月合約取到期月份最小者。
    """
    ds = trade_date.strftime("%Y/%m/%d")
    rows = _post_csv(
        TAIFEX_DAILY,
        {
            "down_type": "1",
            "commodity_id": "TX",
            "queryStartDate": ds,
            "queryEndDate": ds,
        },
    )
    if not rows or len(rows) < 2:
        return None

    header = [c.strip() for c in rows[0]]

    def col(*names: str) -> int | None:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_month = col("到期月份(週別)", "契約月份")
    i_close = col("收盤價")
    i_open = col("開盤價")
    i_high = col("最高價")
    i_low = col("最低價")
    i_sess = col("交易時段")
    i_vol = col("成交量")
    i_oi = col("未沖銷契約數")
    if i_month is None or i_close is None:
        return None

    parsed: list[dict] = []
    for r in rows[1:]:
        if len(r) <= max(x for x in [i_month, i_close, i_sess, i_vol] if x is not None):
            continue
        month = r[i_month].strip()
        # 排除週選/價差商品（月份欄含 "/" 者為價差、含 "W" 者為週合約）
        if "/" in month or not month[:6].isdigit():
            continue
        close = _num(r[i_close])
        if close is None:
            continue
        parsed.append(
            {
                "month": month,
                "session": r[i_sess].strip() if i_sess is not None else "一般",
                "open": _num(r[i_open]) if i_open is not None else None,
                "high": _num(r[i_high]) if i_high is not None else None,
                "low": _num(r[i_low]) if i_low is not None else None,
                "close": close,
                "volume": _num(r[i_vol]) if i_vol is not None else None,
                "open_interest": _num(r[i_oi]) if i_oi is not None else None,
            }
        )
    if not parsed:
        return None

    near_month = min(p["month"] for p in parsed)
    near = [p for p in parsed if p["month"] == near_month]
    day = next((p for p in near if p["session"].startswith("一般")), None)
    night = next((p for p in near if p["session"].startswith("盤後")), None)
    return {"near_month": near_month, "day_session": day, "night_session": night}


def fetch_taifex_institutional(trade_date: date) -> dict | None:
    """三大法人台指期未平倉（口數與契約金額），重點是外資淨部位。"""
    ds = trade_date.strftime("%Y/%m/%d")
    rows = _post_csv(
        TAIFEX_INST,
        {
            "queryStartDate": ds,
            "queryEndDate": ds,
            "commodityId": "TXF",
        },
    )
    if not rows or len(rows) < 2:
        return None

    header = [c.strip() for c in rows[0]]
    try:
        i_ident = next(
            i for i, h in enumerate(header) if "身份別" in h or "身分別" in h
        )
    except StopIteration:
        return None
    # 未平倉多空淨額口數通常是倒數第 2 欄的「口數」群組
    i_net_lots = None
    for i, h in enumerate(header):
        if "未平倉" in h and "淨額" in h and "口數" in h:
            i_net_lots = i
    if i_net_lots is None:
        # 退而求其次：最後一組口數欄位
        lot_cols = [i for i, h in enumerate(header) if h == "口數"]
        i_net_lots = lot_cols[-1] if lot_cols else None
    if i_net_lots is None:
        return None

    out: dict[str, float | None] = {}
    label_map = {"外資": "外資", "投信": "投信", "自營": "自營商"}
    for r in rows[1:]:
        if len(r) <= max(i_ident, i_net_lots):
            continue
        ident = r[i_ident].strip()
        for key, label in label_map.items():
            if ident.startswith(key):
                out[f"{label}台指期淨未平倉口數"] = _num(r[i_net_lots])
    return out or None


# ---------------------------------------------------------------------------
# 證交所 TWSE
# ---------------------------------------------------------------------------

TWSE_TAIEX_HIST = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
TWSE_FMTQIK = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
TWSE_BFI82U = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"


def _roc_to_iso(s: str) -> str | None:
    """民國 115/08/04 -> 2026-08-04"""
    try:
        y, m, d = s.strip().split("/")
        return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"
    except Exception:  # noqa: BLE001
        return None


def fetch_taiex_ohlc(trade_date: date, lookback: int = 20) -> list[dict] | None:
    """加權指數日 OHLC（含前一個月，供計算均線與波段幅度）。"""
    frames: list[dict] = []
    for offset in (1, 0):
        d = (trade_date.replace(day=1) - timedelta(days=1)) if offset else trade_date
        js = _get_json(TWSE_TAIEX_HIST, {"date": d.strftime("%Y%m%d"), "response": "json"})
        if not js or js.get("stat") != "OK":
            continue
        for r in js.get("data", []):
            iso = _roc_to_iso(r[0])
            if not iso:
                continue
            frames.append(
                {
                    "date": iso,
                    "open": _num(r[1]),
                    "high": _num(r[2]),
                    "low": _num(r[3]),
                    "close": _num(r[4]),
                }
            )
    if not frames:
        return None
    frames.sort(key=lambda x: x["date"])
    return frames[-lookback:]


def fetch_taiex_turnover(trade_date: date, lookback: int = 10) -> list[dict] | None:
    """每日成交量值（億元）。"""
    js = _get_json(TWSE_FMTQIK, {"date": trade_date.strftime("%Y%m%d"), "response": "json"})
    if not js or js.get("stat") != "OK":
        return None
    out = []
    for r in js.get("data", []):
        iso = _roc_to_iso(r[0])
        amt = _num(r[2])
        if iso and amt is not None:
            out.append({"date": iso, "turnover_yi": round(amt / 1e8, 2)})
    return out[-lookback:] or None


def fetch_institutional_cash(trade_date: date) -> dict | None:
    """三大法人現貨買賣超（億元）。"""
    js = _get_json(
        TWSE_BFI82U,
        {"dayDate": trade_date.strftime("%Y%m%d"), "type": "day", "response": "json"},
    )
    if not js or js.get("stat") != "OK":
        return None
    out: dict[str, float] = {}
    key_map = {
        "自營商": "自營商",
        "投信": "投信",
        "外資及陸資": "外資及陸資",
    }
    for r in js.get("data", []):
        label = r[0].strip()
        net = _num(r[-1])
        if net is None:
            continue
        for k, v in key_map.items():
            if label.startswith(k) and "自營商(" not in label:
                out[f"{v}買賣超_億"] = round(net / 1e8, 2)
    return out or None


def fetch_margin(trade_date: date) -> dict | None:
    """
    融資融券餘額。注意: 證交所此表通常於當日晚間才更新，
    盤前 07:00 執行時抓到的是「前一交易日」的最終數字 —— 這正是盤前需要的。
    """
    js = _get_json(
        TWSE_MARGIN,
        {"date": trade_date.strftime("%Y%m%d"), "selectType": "MS", "response": "json"},
    )
    if not js or js.get("stat") != "OK":
        return None
    rows = js.get("data", []) + js.get("creditData", [])
    for t in js.get("tables", []):  # 新版 rwd API 把資料包在 tables 裡
        rows += t.get("data", [])
    for r in rows:
        # 欄位: 項目, 買進, 賣出, 現金償還, 前日餘額, 今日餘額
        if r and "融資金額" in str(r[0]) and len(r) >= 6:
            return {
                "融資餘額_億": round((_num(r[5]) or 0) / 1e5, 2),  # 單位: 仟元 -> 億元
                "融資前日餘額_億": round((_num(r[4]) or 0) / 1e5, 2),
            }
    return None


# ---------------------------------------------------------------------------
# 組裝
# ---------------------------------------------------------------------------


@dataclass
class Payload:
    generated_at: str
    target_session: str
    prev_trade_date: str | None = None
    us_market: dict = field(default_factory=dict)
    taiex_ohlc: list | None = None
    taiex_turnover: list | None = None
    institutional_cash: dict | None = None
    institutional_futures: dict | None = None
    margin: dict | None = None
    taifex_tx: dict | None = None
    derived: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _sma(values: list[float], n: int) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


def build_payload(prev_trade_date: date, today: date) -> Payload:
    p = Payload(
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        target_session=today.isoformat(),
        prev_trade_date=prev_trade_date.isoformat(),
    )

    p.us_market = fetch_us_market()

    # 期交所的「盤後」時段記在結束那天的交易日：昨天 15:00 到今晨 05:00 那段
    # 掛在 today 底下,不在 prev_trade_date。07:00 執行時要的是這一段。
    # 日盤則是 prev_trade_date 當天的。
    tx_prev = fetch_taifex_tx(prev_trade_date) or {}
    tx_today = fetch_taifex_tx(today) or {}
    night = tx_today.get("night_session")
    night_date = today
    if not night:
        # today 的夜盤還沒出來（假日、期交所延遲）—— 退回前一交易日的。
        # 這種情況會在最後由 night_session_date != target_session 標進 missing。
        night = tx_prev.get("night_session")
        night_date = prev_trade_date
    if tx_prev or night:
        p.taifex_tx = {
            "near_month": (tx_today if night_date == today else tx_prev).get("near_month")
            or tx_prev.get("near_month"),
            "day_session": tx_prev.get("day_session"),
            "day_session_date": prev_trade_date.isoformat(),
            "night_session": night,
            "night_session_date": night_date.isoformat() if night else None,
        }

    p.institutional_futures = fetch_taifex_institutional(prev_trade_date)
    p.taiex_ohlc = fetch_taiex_ohlc(prev_trade_date)
    p.taiex_turnover = fetch_taiex_turnover(prev_trade_date)
    p.institutional_cash = fetch_institutional_cash(prev_trade_date)
    p.margin = fetch_margin(prev_trade_date)

    # ---- 衍生欄位：全部由已抓到的數字算出，不引入外部假設 ----
    d: dict[str, Any] = {}
    if p.taiex_ohlc:
        closes = [x["close"] for x in p.taiex_ohlc if x["close"] is not None]
        if closes:
            spot = closes[-1]
            d["加權指數收盤"] = spot
            d["5日均線"] = _sma(closes, 5)
            d["10日均線"] = _sma(closes, 10)
            d["20日均線"] = _sma(closes, 20)
            highs = [x["high"] for x in p.taiex_ohlc if x["high"] is not None]
            lows = [x["low"] for x in p.taiex_ohlc if x["low"] is not None]
            if highs and lows:
                d["近20日最高"] = max(highs)
                d["近20日最低"] = min(lows)
            # 期現價差：正數 = 正價差，負數 = 逆價差
            night = (p.taifex_tx or {}).get("night_session") or {}
            day_s = (p.taifex_tx or {}).get("day_session") or {}
            # 夜盤過期時整組不輸出：算得出來不代表算了有意義,
            # 留著會讓 renderer 與 prompt 兩邊都拿去用（見 missing 的過期標註）。
            night_fresh = (p.taifex_tx or {}).get("night_session_date") == p.target_session
            if night.get("close") and night_fresh:
                d["夜盤台指期收盤"] = night["close"]
                d["夜盤期現價差"] = round(night["close"] - spot, 2)
            if day_s.get("close"):
                d["日盤台指期收盤"] = day_s["close"]
                d["日盤期現價差"] = round(day_s["close"] - spot, 2)

    # 台積電 ADR 隱含台股價格（供判斷開盤溢價收斂空間）
    adr = p.us_market.get("台積電ADR")
    fx = p.us_market.get("美元兌台幣")
    if adr and fx and adr.get("close") and fx.get("close"):
        # 1 ADR = 5 股普通股
        d["台積電ADR隱含台股價"] = round(adr["close"] * fx["close"] / 5, 1)

    p.derived = d

    tx = p.taifex_tx or {}
    for name, val in [
        ("美股行情", p.us_market),
        ("台指期日盤", tx.get("day_session")),
        ("台指期夜盤", tx.get("night_session")),
        ("三大法人期貨未平倉", p.institutional_futures),
        ("加權指數OHLC", p.taiex_ohlc),
        ("成交量值", p.taiex_turnover),
        ("三大法人現貨買賣超", p.institutional_cash),
        ("融資餘額", p.margin),
    ]:
        if not val:
            p.missing.append(name)

    # 夜盤抓到的不是今天那一段 = 用的是舊資料（隔了一個完整交易日）。
    # 標進 missing,讓 prompt 端知道要調低開盤推估的確定性。
    night_date_s = tx.get("night_session_date")
    if tx.get("night_session") and night_date_s != p.target_session:
        p.missing.append(f"最新夜盤台指期（現有資料為 {night_date_s} 夜盤，已過期）")

    return p
