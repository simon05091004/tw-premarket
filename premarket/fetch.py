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
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from statistics import median, pstdev
from typing import Any, Callable
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

TIMEOUT = 20
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# 每個網域的最小請求間隔（秒）。證交所建議 3 秒以上，短時間連打會被擋。
MIN_INTERVAL = {"www.twse.com.tw": 3.0, "isin.twse.com.tw": 3.0}
DEFAULT_INTERVAL = 0.5
RETRIES = 3
_last_request: dict[str, float] = {}


def _throttle(url: str) -> None:
    """同一網域的請求之間強制留間隔；不同網域互不影響。"""
    host = urlparse(url).netloc
    gap = MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
    wait = gap - (time.monotonic() - _last_request.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()


def _fetch(
    method: str, url: str, parse: Callable[[requests.Response], Any], **kw: Any
) -> Any | None:
    """
    帶節流與重試的請求。失敗回 None —— 呼叫端一律把 None 當成「這項沒資料」，
    讓單一資料源掛掉不會拖垮整份報告（既有設計）。

    parse 在重試迴圈「之內」執行，它拋例外就跟連線失敗一樣會重試:
    證交所忙碌時不是回 5xx，而是回 HTTP 200 加一頁 HTML,raise_for_status()
    攔不到。原本只在迴圈外解析,這種故障連一次都不會重試 —— 2026-08-11 盤前
    12 次請求全中，整份報告因為找不到前一交易日而中止。
    """
    for attempt in range(1, RETRIES + 1):
        _throttle(url)
        try:
            r = requests.request(method, url, headers=UA, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return parse(r)
        except Exception as exc:  # noqa: BLE001
            if attempt == RETRIES:
                log.warning("%s %s 失敗（第 %d 次，放棄）: %s", method, url, attempt, exc)
                return None
            backoff = 2.0 * attempt
            log.info("%s %s 失敗（第 %d 次），%.0f 秒後重試: %s", method, url, attempt, backoff, exc)
            time.sleep(backoff)
    return None


def _get_json(url: str, params: dict | None = None) -> Any | None:
    # 注意：證交所「查無資料」是合法 JSON（stat 非 OK）,由各 fetcher 自行判讀,
    # 不會走到這裡的重試 —— 只有「根本不是 JSON」才算故障。
    return _fetch("GET", url, lambda r: r.json(), params=params)


def _get_text(url: str, encoding: str = "utf-8") -> str | None:
    def parse(r: requests.Response) -> str:
        r.encoding = encoding
        return r.text

    return _fetch("GET", url, parse)


def _post_csv(url: str, data: dict) -> list[list[str]] | None:
    def parse(r: requests.Response) -> list[list[str]]:
        r.encoding = "big5"
        return list(csv.reader(io.StringIO(r.text)))

    return _fetch("POST", url, parse, data=data)


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


def fetch_futures_oi_series(end: date, days: int = 6) -> list[dict] | None:
    """
    抓一段區間的三大法人台指期未平倉，讓下游能算出單日增減。
    外資空單 8.7 萬口是水位；今天加空 3,000 口還是回補 5,000 口，才是訊號。
    """
    start = end - timedelta(days=days)
    rows = _post_csv(
        TAIFEX_INST,
        {
            "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d"),
            "commodityId": "TXF",
        },
    )
    if not rows or len(rows) < 2:
        return None

    header = [c.strip() for c in rows[0]]
    try:
        i_date = next(i for i, h in enumerate(header) if "日期" in h)
        i_ident = next(i for i, h in enumerate(header) if "身份別" in h or "身分別" in h)
    except StopIteration:
        return None

    i_net = None
    for i, h in enumerate(header):
        if "未平倉" in h and "淨額" in h and "口數" in h:
            i_net = i
    if i_net is None:
        lot_cols = [i for i, h in enumerate(header) if h == "口數"]
        i_net = lot_cols[-1] if lot_cols else None
    if i_net is None:
        return None

    by_date: dict[str, dict] = {}
    for r in rows[1:]:
        if len(r) <= max(i_date, i_ident, i_net):
            continue
        ds = r[i_date].strip().replace("/", "-")
        ident = r[i_ident].strip()
        val = _num(r[i_net])
        if val is None:
            continue
        entry = by_date.setdefault(ds, {"date": ds})
        for key, label in (("外資", "外資"), ("投信", "投信"), ("自營", "自營商")):
            if ident.startswith(key):
                entry[f"{label}淨未平倉口數"] = val

    series = sorted(by_date.values(), key=lambda x: x["date"])
    # 補上單日變化
    for i in range(1, len(series)):
        for label in ("外資", "投信", "自營商"):
            k = f"{label}淨未平倉口數"
            if k in series[i] and k in series[i - 1]:
                series[i][f"{label}單日增減"] = round(series[i][k] - series[i - 1][k])
    return series or None


def fetch_taifex_institutional(trade_date: date) -> dict | None:
    """
    三大法人台指期未平倉：水位（淨口數）與動作（較前日增減）。

    只給水位的話，「外資淨空 8.3 萬口」在連續三天都是 8.3 萬口時讀起來
    跟今天剛加空兩萬口一模一樣 —— 增減才是當天發生的事。所以這裡抓一小段
    區間而非單日，用前一個交易日相減補出增減。
    """
    # 抓 10 天而非預設的 6：碰上農曆年這種長假，6 個日曆天內可能只剩 trade_date
    # 一個交易日，增減就整欄消失。多抓幾天不多花一次請求（期交所支援區間查詢）。
    series = fetch_futures_oi_series(trade_date, days=10)
    if not series:
        return None
    last = series[-1]
    out: dict[str, float | None] = {}
    for label in ("外資", "投信", "自營商"):
        lots = last.get(f"{label}淨未平倉口數")
        if lots is None:
            continue
        out[f"{label}台指期淨未平倉口數"] = lots
        chg = last.get(f"{label}單日增減")
        if chg is not None:
            out[f"{label}台指期淨未平倉_較前日增減"] = chg
    if out:
        out["未平倉日期"] = last.get("date")
    return out or None


# ---------------------------------------------------------------------------
# 證交所 TWSE
# ---------------------------------------------------------------------------

TPEX_INDEX = "https://www.tpex.org.tw/openapi/v1/tpex_index"
TPEX_TRADING = "https://www.tpex.org.tw/openapi/v1/tpex_daily_trading_index"
TWSE_TAIEX_HIST = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
TWSE_FMTQIK = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
TWSE_BFI82U = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TWSE_STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"


def _roc_to_iso(s: str) -> str | None:
    """民國 115/08/04 -> 2026-08-04"""
    try:
        y, m, d = s.strip().split("/")
        return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"
    except Exception:  # noqa: BLE001
        return None


def _months_back(d: date, n: int) -> date:
    """往回 n 個月，回傳該月的某一天（證交所月報表只看年月）。"""
    for _ in range(n):
        d = d.replace(day=1) - timedelta(days=1)
    return d


def fetch_taiex_ohlc(trade_date: date, lookback: int = 20) -> list[dict] | None:
    """
    加權指數日 OHLC，供計算均線與波段幅度。

    證交所這支是「月」報表，一次要抓幾個月由 lookback 決定：
    每月約 20 個交易日,多抓一個月當緩衝（遇長假時月交易日會少於 20）。
    lookback=60（盤後算 60MA）會抓四個月。
    """
    months = max(2, lookback // 20 + 2)
    frames: list[dict] = []
    for offset in range(months - 1, -1, -1):
        d = _months_back(trade_date, offset)
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
    # 證交所回傳整個月，含 trade_date 之後的日期。
    # 不過濾的話，--date 回測時最後一根 K 棒會是未來的交易日。
    cutoff = trade_date.isoformat()
    frames = [f for f in frames if f["date"] <= cutoff]
    if not frames:
        return None
    frames.sort(key=lambda x: x["date"])
    return frames[-lookback:]


def fetch_taiex_turnover(trade_date: date, lookback: int = 10) -> list[dict] | None:
    """每日成交量值（億元）。"""
    js = _get_json(TWSE_FMTQIK, {"date": trade_date.strftime("%Y%m%d"), "response": "json"})
    if not js or js.get("stat") != "OK":
        return None
    cutoff = trade_date.isoformat()  # 同上：月表會含 trade_date 之後的日期
    out = []
    for r in js.get("data", []):
        iso = _roc_to_iso(r[0])
        amt = _num(r[2])
        if iso and amt is not None and iso <= cutoff:
            out.append({"date": iso, "turnover_yi": round(amt / 1e8, 2)})
    return out[-lookback:] or None


def fetch_stock_close(trade_date: date, stock_no: str = "2330") -> dict | None:
    """
    個股日收盤（預設台積電 2330）。

    ADR 隱含價沒有現貨對照就只是一個孤零零的數字 —— 算不出溢價幅度，
    那一整段等於白寫。

    與加權指數同屬「月」報表家族：回傳整個月、含 trade_date 之後的日期，
    一樣要先裁掉。漲跌價差欄不取用,改由前一根 K 棒相減 —— 少信一個欄位,
    就少一個「格式對但語意錯」的機會（證交所的漲跌符號欄是 HTML）。
    """
    js = _get_json(
        TWSE_STOCK_DAY,
        {"date": trade_date.strftime("%Y%m%d"), "stockNo": stock_no, "response": "json"},
    )
    if not js or js.get("stat") != "OK":
        return None
    cutoff = trade_date.isoformat()
    bars: list[dict] = []
    for r in js.get("data", []):
        if len(r) < 7:
            continue
        iso = _roc_to_iso(r[0])
        close = _num(r[6])
        if iso and close is not None and iso <= cutoff:
            bars.append({"date": iso, "close": close})
    if not bars:
        return None
    bars.sort(key=lambda x: x["date"])
    last = bars[-1]
    out: dict[str, Any] = {"代號": stock_no, "日期": last["date"], "收盤": last["close"]}
    if len(bars) >= 2:
        prev = bars[-2]["close"]
        out["前一日收盤"] = prev
        out["漲跌"] = round(last["close"] - prev, 2)
        out["漲跌幅_pct"] = round((last["close"] - prev) / prev * 100, 2) if prev else None
    return out


def fetch_tpex_index(d: date) -> dict | None:
    """
    櫃買指數（OHLC + 漲跌）與當日成交金額。

    原本只有盤後版在用（fetch_post.py）。盤前也要：權值股單獨承壓的日子，
    櫃買相對加權的強弱是判斷「單一族群事件 vs 全面 risk-off」最直接的驗證 ——
    指數開低幾點看不出這件事，中小型股有沒有跟著塌才看得出來。

    兩支端點的日期格式不同 —— 指數是西元 20260807，量值是民國 1150807，
    這裡各自轉換。兩支都只提供最近 6 個交易日的滾動視窗，
    補跑更早的日期會取不到（回 None，列入 missing）。
    """
    want_ad = d.strftime("%Y%m%d")
    want_roc = f"{d.year - 1911}{d.month:02d}{d.day:02d}"

    rows = _get_json(TPEX_INDEX)
    row = next(
        (r for r in rows if str(r.get("Date")) == want_ad), None
    ) if isinstance(rows, list) else None
    if not row:
        log.info("櫃買指數：%s 不在端點的滾動視窗內", d)
        return None

    close, chg = _num(row.get("Close")), _num(row.get("Change"))
    out: dict[str, Any] = {
        "收盤": close,
        "開盤": _num(row.get("Open")),
        "最高": _num(row.get("High")),
        "最低": _num(row.get("Low")),
        "漲跌": chg,
        "漲跌幅_pct": (
            round(chg / (close - chg) * 100, 2)
            if close is not None and chg is not None and close != chg
            else None
        ),
    }

    vol = _get_json(TPEX_TRADING)
    vrow = next(
        (r for r in vol if str(r.get("Date")) == want_roc), None
    ) if isinstance(vol, list) else None
    if vrow:
        amt = _num(vrow.get("TradeAmount"))
        out["成交金額_億"] = round(amt / 1e8, 2) if amt is not None else None
    return out


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
    tsmc_spot: dict | None = None
    tpex_index: dict | None = None
    derived: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    # 歷史序列（由 main.py 從 docs/data/payload-*.json 讀入）。
    # 「今天這個數字算不算大」沒有歷史就答不出來 —— 溢價基準、外資部位水位、
    # 槓桿變化三項都需要它。不寫進 to_dict()，避免把歷史再送進 API。
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        out = asdict(self)
        out.pop("history", None)
        return out


def _sma(values: list[float], n: int) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


def _series(hist: list[dict], key: str, last: int | None = None) -> list[float]:
    """歷史序列中某個欄位的非空值，依日期排序後取最後 last 筆。"""
    vals = [h[key] for h in hist if h.get(key) is not None]
    return vals[-last:] if last else vals


def _pct_rank(x: float, hist: list[float]) -> float | None:
    """x 在歷史樣本中的百分位（0–100）。樣本為空回 None。"""
    if not hist:
        return None
    return round(100.0 * sum(1 for h in hist if h <= x) / len(hist), 1)


def _confidence(n: int) -> str:
    """樣本數換成一句可信度標註 —— 報告要能講出「這個基準有多站得住」。"""
    if n >= 20:
        return f"高（樣本 {n} 日）"
    if n >= 10:
        return f"中（樣本 {n} 日）"
    return f"低（樣本僅 {n} 日，基準暫定）"


HISTORY_KEYS = (
    "台積電ADR溢價_pct_內部用",
    "外資台指期淨未平倉口數",
    "融資餘額_億",
    "加權指數收盤",
    "櫃買相對加權強弱_pp",
)


def history_from_payloads(payloads: list[dict]) -> list[dict]:
    """
    把過去的 payload JSON 壓成算基準需要的最小欄位。

    只留 HISTORY_KEYS —— 歷史序列的用途只有「今天這個數字在自己的分布裡站在哪」，
    其餘欄位一概不需要,留著只會讓記憶體與後續除錯變吵。
    舊 payload 還沒有新欄位時自然是 None，由各計算段自行降級。
    """
    out: list[dict] = []
    for p in payloads:
        d = (p or {}).get("derived") or {}
        row: dict[str, Any] = {"date": (p or {}).get("target_session")}
        for k in HISTORY_KEYS:
            row[k] = d.get(k)
        # 融資餘額原本只在 margin 區塊，derived 是這次才加的
        if row.get("融資餘額_億") is None:
            row["融資餘額_億"] = ((p or {}).get("margin") or {}).get("融資餘額_億")
        out.append(row)
    out.sort(key=lambda r: r["date"] or "")
    return out


# VIX 分級門檻。只看漲跌% 會出事：VIX 從 12 漲 20% 到 14.4 仍是低波動，
# 從 22 漲 20% 到 26.4 已經是恐慌區 —— 同樣的漲幅，兩件事。
VIX_BANDS = ((15.0, "低（<15）"), (20.0, "中性（15–20）"), (25.0, "偏高（20–25）"))
VIX_TOP = "恐慌（≥25）"

# 外資與投信部位方向相反、且小的一邊 ≥ 大的一邊的這個比例 = 鏡像對峙。
# 盤前版已停用（見 _derive 的籌碼段註解）；保留常數是因為盤後版 fetch_post 還在引用。
MIRROR_RATIO = 0.85

# --- 台積電 ADR 溢價（第 3 節）---
ADR_SHARES_PER_UNIT = 5  # 1 ADR = 5 股普通股
PREMIUM_BASELINE_DAYS = 60
PREMIUM_MIN_SAMPLES = 3       # 少於這個樣本數就不算基準,整段降級為「資料不足」
PREMIUM_FLAT_PP = 0.30        # 樣本不足以估 σ 時的固定門檻（百分點）
PREMIUM_SIGMA_K = 1.0         # 有樣本時改用 k×σ，取兩者較大者
# 台積電佔加權指數的權重。證交所沒有穩定的日更端點，這裡當成參數處理,
# 並且一定要輸出到 derived —— 報告裡的點數歸因對這個數字很敏感,
# 讀者要知道它是假設值而不是實測值。
TSMC_INDEX_WEIGHT_PCT = float(os.getenv("TSMC_INDEX_WEIGHT_PCT", "30"))

# --- 籌碼門檻（第 4 節）---
# 未平倉「變動」小於自身水位的這個百分比、或小於這個絕對口數 → 標「持平」，
# 不給任何方向性形容。8 萬口部位變動 65 口是雜訊,寫成「力道略緩」
# 等於憑空生出一個不存在的方向。
OI_FLAT_PCT = 1.0
OI_FLAT_LOTS = 500
CASH_FLAT_YI = 50.0     # 現貨買賣超小於此金額（億）→ 持平
MARGIN_FLAT_PCT = 0.5   # 融資餘額日變動小於此百分比 → 持平

# --- 櫃買 vs 上市（第 5 節）---
OTC_DIVERGE_PP = 0.5    # 漲跌幅差距在此範圍內 → 同步，不解讀分化


def _derive(p: Payload) -> dict[str, Any]:
    """
    衍生欄位：全部由已抓到的數字算出，不引入外部假設。

    抽成純函式（完全不碰網路）是為了能單獨測試 —— 這裡每一條都是「判斷」，
    而判斷邏輯一律放程式碼，prompt 只負責照著寫。
    """
    d: dict[str, Any] = {}

    spot: float | None = None
    if p.taiex_ohlc:
        closes = [x["close"] for x in p.taiex_ohlc if x["close"] is not None]
        if closes:
            spot = closes[-1]
            d["加權指數收盤"] = spot
            if len(closes) >= 2 and closes[-2]:
                # 櫃買分化要拿加權當日漲跌幅當對照，原本 derived 沒有這一欄。
                d["加權指數漲跌_點"] = round(spot - closes[-2], 2)
                d["加權指數漲跌幅_pct"] = round((spot - closes[-2]) / closes[-2] * 100, 2)
            for n in (5, 10, 20):
                ma = _sma(closes, n)
                d[f"{n}日均線"] = ma
                # 站上均線只有「有／沒有」兩種狀態，乖離率才看得出站多遠、
                # 以及回測均線要付出多少代價。均線排列本身資訊量不如這個。
                if ma:
                    d[f"對{n}日均線乖離率_pct"] = round((spot - ma) / ma * 100, 2)
            highs = [x["high"] for x in p.taiex_ohlc if x["high"] is not None]
            lows = [x["low"] for x in p.taiex_ohlc if x["low"] is not None]
            if highs and lows:
                hi, lo = max(highs), min(lows)
                d["近20日最高"] = hi
                d["近20日最低"] = lo
                # 兩者都以現價為分母：一個是「還要漲多少才到高點」，
                # 一個是「已經比低點高多少」，方向不同但基準一致。
                d["距近20日高點_pct"] = round((hi - spot) / spot * 100, 2)
                d["距近20日低點_pct"] = round((spot - lo) / spot * 100, 2)

    tx = p.taifex_tx or {}
    day_s = tx.get("day_session") or {}
    night = tx.get("night_session") or {}
    # 夜盤過期時整組不輸出：算得出來不代表算了有意義,
    # 留著會讓 renderer 與 prompt 兩邊都拿去用（見 missing 的過期標註）。
    night_fresh = tx.get("night_session_date") == p.target_session

    if day_s.get("close"):
        d["日盤台指期收盤"] = day_s["close"]
        if spot is not None:
            # 日盤期現價差才是真的價差：同一個交易時段的期貨與現貨相減。
            # 正數 = 正價差，負數 = 逆價差。
            d["日盤期現價差"] = round(day_s["close"] - spot, 2)

    if night.get("close") and night_fresh:
        d["夜盤台指期收盤"] = night["close"]
        if spot is not None:
            # 夜盤收盤與現貨昨收不同時段，相減得到的不是期現價差 ——
            # 現貨那一端早在 13:30 就停止報價了。命名照實寫，避免被當成價差解讀。
            d["夜盤相對現貨昨收偏離"] = round(night["close"] - spot, 2)
        if day_s.get("close"):
            # 夜盤期貨對日盤收盤的漲跌，才是期貨端隔夜真正走了多少。
            chg = night["close"] - day_s["close"]
            d["夜盤較日盤收盤漲跌點"] = round(chg, 2)
            d["夜盤較日盤收盤漲跌_pct"] = round(chg / day_s["close"] * 100, 2)

    # ------------------------------------------------------------------
    # 籌碼
    #
    # 這一段原本有兩個問題,都是「把雜訊寫成訊號」:
    #   1. 未平倉變動沒有門檻。外資 8 萬口部位變動 65 口（0.08%）被寫成
    #      「偏空但力道略緩」—— 憑空給了一個不存在的方向。
    #   2. 「外資投信鏡像對峙」把兩個結構性部位當成方向對賭。外資淨空長期是
    #      避險／套利／選擇權對沖的常態部位,投信大額多單多與 ETF 的期貨替代
    #      部位有關,兩邊都不是在押指數方向,「一方回補就放大波動」推不出來。
    # 現在改成：先過門檻才給方向，水位只跟自己的歷史比。
    # ------------------------------------------------------------------
    inst = p.institutional_futures or {}
    d["期貨部位動作門檻"] = (
        f"|較前日增減| < 自身水位的 {OI_FLAT_PCT}% 或 < {OI_FLAT_LOTS} 口 → 判定為持平，"
        "不得給任何方向性形容"
    )
    for label in ("外資", "投信", "自營商"):
        level = inst.get(f"{label}台指期淨未平倉口數")
        chg = inst.get(f"{label}台指期淨未平倉_較前日增減")
        if level is not None:
            d[f"{label}台指期淨未平倉口數"] = level
        if chg is not None:
            d[f"{label}台指期淨未平倉_較前日增減"] = chg
        if level and chg is not None:
            pct = chg / abs(level) * 100
            d[f"{label}台指期淨未平倉_較前日增減_pct"] = round(pct, 2)
            if abs(pct) < OI_FLAT_PCT or abs(chg) < OI_FLAT_LOTS:
                verdict = "持平"
            elif level < 0:
                verdict = "減空（回補）" if chg > 0 else "加空"
            else:
                verdict = "加多" if chg > 0 else "減多"
            d[f"{label}台指期淨未平倉_動作判定"] = verdict

    # 水位只跟自己的歷史比 —— 8 萬口是常態還是極端，看分布，不看絕對值。
    foreign = inst.get("外資台指期淨未平倉口數")
    if foreign is not None:
        hist = _series(p.history or [], "外資台指期淨未平倉口數", PREMIUM_BASELINE_DAYS)
        d["外資台指期淨未平倉_水位性質"] = (
            "結構性部位（避險／套利／選擇權對沖），水位本身不代表方向觀點；"
            "有訊息的是相對自身歷史的位置與當日變動是否過門檻"
        )
        if hist:
            rank = _pct_rank(foreign, hist)
            d["外資台指期淨未平倉_水位百分位"] = rank
            d["外資台指期淨未平倉_水位樣本"] = _confidence(len(hist))
            if rank is not None:
                d["外資台指期淨未平倉_水位分級"] = (
                    "淨空偏極端" if rank <= 10 else
                    "淨空偏高" if rank <= 30 else
                    "淨空偏低" if rank >= 70 else
                    "常態區間"
                )

    # 現貨買賣超同樣先過門檻
    cash = p.institutional_cash or {}
    for src, label in (
        ("外資及陸資買賣超_億", "外資"),
        ("投信買賣超_億", "投信"),
        ("自營商買賣超_億", "自營商"),
    ):
        v = cash.get(src)
        if v is not None:
            d[f"{label}現貨買賣超_億"] = v
            d[f"{label}現貨買賣超_動作判定"] = (
                "持平" if abs(v) < CASH_FLAT_YI else ("買超" if v > 0 else "賣超")
            )
    d["現貨買賣超門檻"] = f"|買賣超| < {CASH_FLAT_YI} 億 → 判定為持平"

    # 「現貨買、期貨加空」這種組合只有在雙邊都過門檻時才成立。
    f_cash = cash.get("外資及陸資買賣超_億")
    f_chg = inst.get("外資台指期淨未平倉_較前日增減")
    if f_cash is not None and foreign and f_chg is not None:
        cash_sig = abs(f_cash) >= CASH_FLAT_YI
        oi_sig = not (
            abs(f_chg / abs(foreign) * 100) < OI_FLAT_PCT or abs(f_chg) < OI_FLAT_LOTS
        )
        if cash_sig and oi_sig:
            combo = f"現貨{'買超' if f_cash > 0 else '賣超'}＋期貨{d.get('外資台指期淨未平倉_動作判定')}"
            if f_cash > 0 and foreign < 0 and f_chg < 0:
                combo += "：參與反彈但不信任反彈"
        elif cash_sig:
            combo = f"僅現貨過門檻（{'買超' if f_cash > 0 else '賣超'} {abs(f_cash)} 億），期貨部位持平"
        elif oi_sig:
            combo = "僅期貨部位過門檻，現貨買賣超持平"
        else:
            combo = "現貨與期貨皆未過門檻，當日無外資籌碼訊號"
        d["外資現貨期貨組合判定"] = combo

    # 融資：日變動幾乎永遠是雜訊，真正有訊息的是「這波漲了多少、槓桿跟上沒有」
    mg = p.margin or {}
    bal, prev_bal = mg.get("融資餘額_億"), mg.get("融資前日餘額_億")
    if bal is not None:
        d["融資餘額_億"] = bal
        if prev_bal:
            chg_yi = bal - prev_bal
            pct = chg_yi / prev_bal * 100
            d["融資餘額_較前日增減_億"] = round(chg_yi, 2)
            d["融資餘額_較前日增減_pct"] = round(pct, 2)
            d["融資餘額_動作判定"] = (
                "持平" if abs(pct) < MARGIN_FLAT_PCT else ("增加" if chg_yi > 0 else "去化")
            )
    base = next(
        (h for h in (p.history or []) if h.get("融資餘額_億") and h.get("加權指數收盤")),
        None,
    )
    if base and bal and spot:
        m_chg = (bal - base["融資餘額_億"]) / base["融資餘額_億"] * 100
        i_chg = (spot - base["加權指數收盤"]) / base["加權指數收盤"] * 100
        d["槓桿觀察起點"] = base["date"]
        d["槓桿觀察起點說明"] = (
            "起點為現有 payload 歷史的最早一日，不等於本波低點；"
            "更早的融資餘額未留存，跨越低點的槓桿變化無法計算"
        )
        d["融資餘額_較起點變化_pct"] = round(m_chg, 2)
        d["指數_較起點變化_pct"] = round(i_chg, 2)
        if abs(i_chg) >= 1.0:
            if i_chg > 0:
                d["槓桿判定"] = (
                    "指數上漲、融資去化（槓桿未膨脹）" if m_chg < 0 else
                    "指數上漲、融資增幅小於指數（槓桿相對收斂）" if m_chg < i_chg else
                    "指數上漲、融資增幅大於指數（槓桿膨脹）"
                )
            else:
                d["槓桿判定"] = (
                    "指數下跌、融資同步去化" if m_chg <= i_chg else
                    "指數下跌、融資去化幅度小於指數（槓桿未隨跌勢退場）"
                )

    # ------------------------------------------------------------------
    # 台積電 ADR：只講「相對結構基準的偏離」，不講絕對溢價水準
    #
    # ADR 溢價長期就在 10% 上下（流動性、稅、借券成本造成的結構性價差），
    # 不會回歸零 —— 「溢價 9.89% 很大」這句話沒有任何訊息量。
    # 有訊息的是「今天的溢價相對它自己的常態站在哪」,以及那個偏離換算成
    # 台積電現貨開盤該走多少、對指數是幾點。
    # ------------------------------------------------------------------
    adr = p.us_market.get("台積電ADR")
    fx = p.us_market.get("美元兌台幣")
    if adr and fx and adr.get("close") and fx.get("close"):
        implied = round(adr["close"] * fx["close"] / ADR_SHARES_PER_UNIT, 1)
        d["台積電ADR隱含台股價"] = implied
        tsmc = p.tsmc_spot or {}
        # 收盤價過期就不算溢價：拿上週的現貨去比今天的 ADR，得到的是一個
        # 看起來很精確、實際上沒有意義的百分比。
        spot_2330 = tsmc.get("收盤") if tsmc.get("日期") == p.prev_trade_date else None
        if spot_2330:
            d["台積電現貨收盤"] = spot_2330
            prem = (implied - spot_2330) / spot_2330 * 100
            # 保留原始值只為了累積歷史算基準。鍵名自帶「內部用」,
            # prompt 端明文禁止把這個數字寫進報告。
            d["台積電ADR溢價_pct_內部用"] = round(prem, 2)

            hist = _series(p.history or [], "台積電ADR溢價_pct_內部用", PREMIUM_BASELINE_DAYS)
            if len(hist) >= PREMIUM_MIN_SAMPLES:
                baseline = median(hist)
                sigma = pstdev(hist) if len(hist) >= 2 else 0.0
                gap = prem - baseline
                thr = max(PREMIUM_FLAT_PP, PREMIUM_SIGMA_K * sigma)
                d["台積電ADR溢價基準_pct"] = round(baseline, 2)
                d["台積電ADR溢價基準樣本"] = _confidence(len(hist))
                d["台積電ADR溢價偏離基準_pp"] = round(gap, 2)
                d["台積電ADR溢價偏離門檻_pp"] = round(thr, 2)
                d["台積電ADR溢價偏離判定"] = (
                    "持平（在雜訊區間內，不作方向解讀）" if abs(gap) < thr
                    else ("低於基準（ADR 相對現貨轉弱）" if gap < 0 else "高於基準（ADR 相對現貨轉強）")
                )
                if abs(gap) >= thr:
                    # 溢價要回到基準，缺口由現貨這一端補：現貨該走多少。
                    move = ((1 + prem / 100) / (1 + baseline / 100) - 1) * 100
                    d["台積電隱含現貨開盤變動_pct"] = round(move, 2)
                    if spot is not None:
                        w = TSMC_INDEX_WEIGHT_PCT / 100
                        pts = spot * w * move / 100
                        d["台積電指數權重_pct"] = TSMC_INDEX_WEIGHT_PCT
                        d["台積電權重來源"] = "參數假設值（環境變數 TSMC_INDEX_WEIGHT_PCT），非當日實測"
                        d["台積電隱含指數影響_點"] = round(pts, 1)
                        night_pts = d.get("夜盤較日盤收盤漲跌點")
                        if night_pts:
                            share = pts / night_pts * 100
                            d["台積電可解釋夜盤跌點_pct"] = round(share, 1)
                            d["台積電歸因判定"] = (
                                "夜盤幾乎可由台積電單獨解釋（弱勢高度集中）" if share >= 80 else
                                "台積電解釋大部分夜盤變動（弱勢偏集中）" if share >= 50 else
                                "台積電只解釋部分夜盤變動，其餘來自其他成分股" if share >= 20 else
                                "台積電無法解釋夜盤變動，壓力來自其他成分股"
                            )
            else:
                d["台積電ADR溢價基準_pct"] = None
                d["台積電ADR溢價基準樣本"] = f"不足（僅 {len(hist)} 日，需 {PREMIUM_MIN_SAMPLES} 日以上）"
                d["台積電ADR溢價偏離判定"] = "歷史樣本不足，本節不作溢價判讀"

            # 三腳拆解：溢價變化 = ADR 漲跌 + 匯率變動 − 現貨漲跌。
            # 這是「ADR 比現貨多跌／多漲多少」的乾淨數字，也順帶把匯率放進報告。
            adr_pct = adr.get("change_pct")
            fx_pct = fx.get("change_pct")
            spot_pct = tsmc.get("漲跌幅_pct")
            if None not in (adr_pct, fx_pct, spot_pct):
                d["台積電ADR漲跌幅_pct"] = adr_pct
                d["美元兌台幣漲跌_pct"] = fx_pct
                d["台積電現貨漲跌幅_pct"] = spot_pct
                d["ADR減現貨報酬差_pct"] = round(adr_pct + fx_pct - spot_pct, 2)

    # ------------------------------------------------------------------
    # 櫃買 vs 上市：權值股單獨承壓的日子，中小型股有沒有跟著塌
    # 才分得出「單一族群事件」與「全面 risk-off」。
    # ------------------------------------------------------------------
    otc = p.tpex_index or {}
    if otc.get("漲跌幅_pct") is not None:
        d["櫃買指數收盤"] = otc.get("收盤")
        d["櫃買漲跌幅_pct"] = otc["漲跌幅_pct"]
        twse_pct = d.get("加權指數漲跌幅_pct")
        if twse_pct is not None:
            div = round(otc["漲跌幅_pct"] - twse_pct, 2)
            d["櫃買相對加權強弱_pp"] = div
            d["櫃買分化門檻_pp"] = OTC_DIVERGE_PP
            d["櫃買相對加權強弱_判定"] = (
                "同步（無分化訊號）" if abs(div) <= OTC_DIVERGE_PP
                else ("櫃買相對強（資金留在中小型股，偏類股輪動）" if div > 0
                      else "櫃買相對弱（賣壓不限於權值股，偏全面性）")
            )
            hist_div = _series(p.history or [], "櫃買相對加權強弱_pp", 5)
            if hist_div:
                d["櫃買相對加權強弱_近5日累計_pp"] = round(sum(hist_div) + div, 2)
                d["櫃買相對加權強弱_近5日樣本"] = _confidence(len(hist_div) + 1)
    if otc.get("成交金額_億") is not None:
        d["櫃買成交金額_億"] = otc["成交金額_億"]
        turn = p.taiex_turnover or []
        twse_amt = turn[-1]["turnover_yi"] if turn else None
        if twse_amt:
            d["上市成交金額_億"] = twse_amt
            d["櫃買上市成交值比"] = round(otc["成交金額_億"] / twse_amt, 3)

    vix = (p.us_market.get("VIX") or {}).get("close")
    if vix is not None:
        d["VIX收盤"] = vix
        d["VIX水位分級"] = next(
            (label for cut, label in VIX_BANDS if vix < cut), VIX_TOP
        )

    return d


def build_payload(
    prev_trade_date: date, today: date, history: list[dict] | None = None
) -> Payload:
    p = Payload(
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        target_session=today.isoformat(),
        prev_trade_date=prev_trade_date.isoformat(),
        history=history or [],
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
    p.tsmc_spot = fetch_stock_close(prev_trade_date)
    p.tpex_index = fetch_tpex_index(prev_trade_date)

    p.derived = _derive(p)

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
        ("台積電現貨收盤", p.tsmc_spot),
        ("櫃買指數", p.tpex_index),
    ]:
        if not val:
            p.missing.append(name)

    tsmc_date = (p.tsmc_spot or {}).get("日期")
    if p.tsmc_spot and tsmc_date != p.prev_trade_date:
        # 收盤價不是前一交易日的 = 拿舊價去算 ADR 溢價，溢價幅度會整個歪掉。
        p.missing.append(f"最新台積電現貨收盤（現有資料為 {tsmc_date}，已過期）")

    # 夜盤抓到的不是今天那一段 = 用的是舊資料（隔了一個完整交易日）。
    # 標進 missing,讓 prompt 端知道要調低開盤推估的確定性。
    night_date_s = tx.get("night_session_date")
    if tx.get("night_session") and night_date_s != p.target_session:
        p.missing.append(f"最新夜盤台指期（現有資料為 {night_date_s} 夜盤，已過期）")

    return p
