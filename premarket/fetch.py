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
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
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
    derived: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _sma(values: list[float], n: int) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


# VIX 分級門檻。只看漲跌% 會出事：VIX 從 12 漲 20% 到 14.4 仍是低波動，
# 從 22 漲 20% 到 26.4 已經是恐慌區 —— 同樣的漲幅，兩件事。
VIX_BANDS = ((15.0, "低（<15）"), (20.0, "中性（15–20）"), (25.0, "偏高（20–25）"))
VIX_TOP = "恐慌（≥25）"

# 外資與投信部位方向相反、且小的一邊 ≥ 大的一邊的這個比例 = 鏡像對峙。
# 門檻定在程式碼裡：讓模型自己判斷「量級接近」，同一組數字每天可以講出不同結論。
MIRROR_RATIO = 0.85


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

    inst = p.institutional_futures or {}
    for label in ("外資", "投信", "自營商"):
        for suffix in ("台指期淨未平倉口數", "台指期淨未平倉_較前日增減"):
            if inst.get(f"{label}{suffix}") is not None:
                d[f"{label}{suffix}"] = inst[f"{label}{suffix}"]
    foreign, trust = (
        inst.get("外資台指期淨未平倉口數"),
        inst.get("投信台指期淨未平倉口數"),
    )
    if foreign is not None and trust is not None:
        d["外資投信淨部位合計口數"] = round(foreign + trust)
        big = max(abs(foreign), abs(trust))
        if big:
            ratio = round(min(abs(foreign), abs(trust)) / big, 2)
            d["外資投信量級比"] = ratio
            d["外資投信鏡像對峙"] = bool(foreign * trust < 0 and ratio >= MIRROR_RATIO)

    # 台積電 ADR 隱含台股價格（供判斷開盤溢價收斂空間）
    adr = p.us_market.get("台積電ADR")
    fx = p.us_market.get("美元兌台幣")
    if adr and fx and adr.get("close") and fx.get("close"):
        # 1 ADR = 5 股普通股
        implied = round(adr["close"] * fx["close"] / 5, 1)
        d["台積電ADR隱含台股價"] = implied
        tsmc = p.tsmc_spot or {}
        # 收盤價過期就不算溢價：拿上週的現貨去比今天的 ADR，得到的是一個
        # 看起來很精確、實際上沒有意義的百分比。
        spot_2330 = tsmc.get("收盤") if tsmc.get("日期") == p.prev_trade_date else None
        if spot_2330:
            d["台積電現貨收盤"] = spot_2330
            d["台積電ADR溢價_pct"] = round((implied - spot_2330) / spot_2330 * 100, 2)

    vix = (p.us_market.get("VIX") or {}).get("close")
    if vix is not None:
        d["VIX收盤"] = vix
        d["VIX水位分級"] = next(
            (label for cut, label in VIX_BANDS if vix < cut), VIX_TOP
        )

    return d


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
    p.tsmc_spot = fetch_stock_close(prev_trade_date)

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
