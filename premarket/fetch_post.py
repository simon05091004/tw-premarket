"""
盤後籌碼版資料抓取（附加模組）。

刻意寫成獨立檔案，只從 fetch.py 匯入共用工具，不修改既有程式碼。

資料公布時程（台北時間）:
    13:30  收盤
    ~14:00 指數 OHLC、成交量、漲跌家數、類股指數
    ~15:00 期交所三大法人期貨未平倉
    ~15:30 證交所三大法人買賣超（總額 + 個股）
    ~21:00 融資融券餘額          <- 這一項決定了報告要排 21:30
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any

from .fetch import (  # 共用既有工具，不重複實作
    _get_json,
    _num,
    _post_csv,
    _roc_to_iso,
    TAIFEX_INST,
    fetch_institutional_cash,
    fetch_margin,
    fetch_taifex_tx,
    fetch_taiex_ohlc,
    fetch_taiex_turnover,
)

log = logging.getLogger(__name__)

TWSE_MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INDEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _rows_from(js: Any, want: str) -> list[list[str]] | None:
    """
    證交所 RWD API 近年改成 tables[] 結構，舊格式則是頂層 data/fields。
    兩種都吃，靠 title 或 fields 內容判斷是哪一張表。
    """
    if not js:
        return None
    for t in js.get("tables", []) or []:
        blob = str(t.get("title", "")) + str(t.get("fields", ""))
        if want in blob:
            return t.get("data")
    if want in str(js.get("fields", "")) or want in str(js.get("title", "")):
        return js.get("data")
    return None


# ---------------------------------------------------------------------------
# 類股指數（電子、金融、櫃買對照用）
# ---------------------------------------------------------------------------

WATCH_SECTORS = (
    "發行量加權股價指數",
    "電子類指數",
    "金融保險類指數",
    "半導體類指數",
    "電腦及週邊設備類指數",
    "其他電子類指數",
    "航運業類指數",
    "塑膠工業類指數",
)


def fetch_sector_indices(d: date) -> dict[str, dict] | None:
    """各類股收盤指數與漲跌點。"""
    js = _get_json(TWSE_MI_INDEX, {"date": d.strftime("%Y%m%d"), "type": "IND", "response": "json"})
    rows = _rows_from(js, "指數")
    if not rows:
        return None
    out: dict[str, dict] = {}
    for r in rows:
        if len(r) < 4:
            continue
        name = r[0].strip()
        if name not in WATCH_SECTORS:
            continue
        close = _num(r[1])
        chg = _num(r[3]) if len(r) > 3 else None
        # 漲跌欄位常見為「+/-」符號另存一欄，這裡容錯處理
        sign = r[2].strip() if len(r) > 2 else ""
        if chg is not None and sign in {"-", "－"}:
            chg = -abs(chg)
        out[name] = {
            "close": close,
            "change": chg,
            "change_pct": round(chg / (close - chg) * 100, 2)
            if close is not None and chg not in (None, 0) and close != chg
            else None,
        }
    return out or None


# ---------------------------------------------------------------------------
# 漲跌家數（市場廣度）
# ---------------------------------------------------------------------------


def fetch_market_breadth(d: date) -> dict | None:
    """
    上漲/下跌/持平家數，以及漲停、跌停家數。
    大盤漲 3% 但只有 400 家上漲，跟 1,200 家上漲是完全不同的行情。
    """
    js = _get_json(TWSE_MI_INDEX, {"date": d.strftime("%Y%m%d"), "type": "MS", "response": "json"})
    rows = _rows_from(js, "漲跌")
    if not rows:
        return None
    out: dict[str, float | None] = {}
    for r in rows:
        if len(r) < 2:
            continue
        label = str(r[0]).replace(" ", "")
        joined = " ".join(str(x) for x in r[1:])
        if "上漲" in label:
            out["上漲家數"] = _num(r[1])
            if "漲停" in joined or len(r) > 2:
                out["漲停家數"] = _num(r[2]) if len(r) > 2 else None
        elif "下跌" in label:
            out["下跌家數"] = _num(r[1])
            out["跌停家數"] = _num(r[2]) if len(r) > 2 else None
        elif "持平" in label or "平盤" in label:
            out["持平家數"] = _num(r[1])
    return out or None


# ---------------------------------------------------------------------------
# 外資買賣超個股（早報沒有的關鍵資訊）
# ---------------------------------------------------------------------------


def fetch_foreign_top_stocks(d: date, top_n: int = 15) -> dict | None:
    """
    三大法人買賣超個股。重點看外資買的是 ETF 還是權值股 ——
    買 ETF = 整體部位調整；買個股 = 選股。兩者的訊號強度差很多。
    """
    js = _get_json(
        TWSE_T86, {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"}
    )
    rows = _rows_from(js, "外陸資")
    if not rows:
        rows = js.get("data") if js else None
    if not rows:
        return None

    fields = js.get("fields") or []
    if not fields:
        for t in js.get("tables", []) or []:
            if t.get("data") is rows:
                fields = t.get("fields", [])
                break

    def idx(*names: str) -> int | None:
        for n in names:
            for i, f in enumerate(fields):
                if n in str(f):
                    return i
        return None

    i_code, i_name = 0, 1
    i_net = idx("外陸資買賣超股數(不含外資自營商)", "外資買賣超股數", "外陸資買賣超股數")
    if i_net is None:
        return None

    parsed = []
    for r in rows:
        if len(r) <= i_net:
            continue
        net = _num(r[i_net])
        if net is None:
            continue
        parsed.append(
            {
                "code": str(r[i_code]).strip(),
                "name": str(r[i_name]).strip(),
                "net_lots": round(net / 1000),  # 股 -> 張
            }
        )
    if not parsed:
        return None
    parsed.sort(key=lambda x: x["net_lots"], reverse=True)
    return {"買超前N": parsed[:top_n], "賣超前N": list(reversed(parsed[-top_n:]))}


# ---------------------------------------------------------------------------
# 期貨未平倉「變化量」—— 水位是狀態，增減才是動作
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 櫃買指數
# ---------------------------------------------------------------------------


def fetch_tpex_index(d: date) -> dict | None:
    """
    櫃買指數。TPEx 端點近年改版頻繁 ——
    若回 None，請用 Claude Code probe 一次實際回傳結構後修正（同 fetch_margin 的處理方式）。
    """
    js = _get_json(TPEX_INDEX)
    if not isinstance(js, list) or not js:
        return None
    log.info("TPEX 端點回傳 %d 筆，需確認欄位結構", len(js))
    return None  # 待 probe 後實作


# ---------------------------------------------------------------------------
# 組裝
# ---------------------------------------------------------------------------


@dataclass
class PostPayload:
    generated_at: str
    session_date: str
    prev_trade_date: str | None = None
    taiex_ohlc: list | None = None
    taiex_turnover: list | None = None
    sector_indices: dict | None = None
    market_breadth: dict | None = None
    institutional_cash: dict | None = None
    futures_oi_series: list | None = None
    foreign_top_stocks: dict | None = None
    margin: dict | None = None
    taifex_tx: dict | None = None
    tpex_index: dict | None = None
    derived: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _sma(values: list[float], n: int) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None


def build_postmarket_payload(session_date: date, prev_date: date | None = None) -> PostPayload:
    p = PostPayload(
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        session_date=session_date.isoformat(),
        prev_trade_date=prev_date.isoformat() if prev_date else None,
    )

    p.taiex_ohlc = fetch_taiex_ohlc(session_date)
    p.taiex_turnover = fetch_taiex_turnover(session_date)
    p.sector_indices = fetch_sector_indices(session_date)
    p.market_breadth = fetch_market_breadth(session_date)
    p.institutional_cash = fetch_institutional_cash(session_date)
    p.futures_oi_series = fetch_futures_oi_series(session_date)
    p.foreign_top_stocks = fetch_foreign_top_stocks(session_date)
    p.taifex_tx = fetch_taifex_tx(session_date)
    p.margin = fetch_margin(session_date)   # 21:00 後才會有當日資料
    p.tpex_index = fetch_tpex_index(session_date)

    d: dict[str, Any] = {}
    if p.taiex_ohlc:
        bars = [b for b in p.taiex_ohlc if b.get("close") is not None]
        closes = [b["close"] for b in bars]
        if bars:
            today = bars[-1]
            spot = today["close"]
            d["加權指數收盤"] = spot
            d["當日開盤"] = today.get("open")
            d["當日最高"] = today.get("high")
            d["當日最低"] = today.get("low")
            if len(closes) >= 2:
                d["當日漲跌"] = round(spot - closes[-2], 2)
                d["當日漲跌幅"] = round((spot - closes[-2]) / closes[-2] * 100, 2)
            # K 線形態：上下影線長度是判斷攻防成敗的關鍵
            o, h, lo = today.get("open"), today.get("high"), today.get("low")
            if None not in (o, h, lo):
                d["上影線"] = round(h - max(o, spot), 2)
                d["下影線"] = round(min(o, spot) - lo, 2)
                d["實體"] = round(abs(spot - o), 2)
                d["振幅點數"] = round(h - lo, 2)
                d["開盤即最低"] = abs(o - lo) < 1
                d["開盤即最高"] = abs(o - h) < 1
            for n in (5, 10, 20, 60):
                d[f"{n}日均線"] = _sma(closes, n)
            ma5, ma10, ma20 = d.get("5日均線"), d.get("10日均線"), d.get("20日均線")
            if None not in (ma5, ma10, ma20):
                d["均線多頭排列"] = ma5 > ma10 > ma20
                d["指數對20日均線乖離率"] = round((spot - ma20) / ma20 * 100, 2)

    if p.taiex_turnover and len(p.taiex_turnover) >= 2:
        d["當日成交金額_億"] = p.taiex_turnover[-1]["turnover_yi"]
        d["前一日成交金額_億"] = p.taiex_turnover[-2]["turnover_yi"]
        prev_amt = p.taiex_turnover[-2]["turnover_yi"]
        if prev_amt:
            d["量能變化率"] = round(
                (p.taiex_turnover[-1]["turnover_yi"] - prev_amt) / prev_amt * 100, 2
            )
        d["近5日均量_億"] = round(
            sum(x["turnover_yi"] for x in p.taiex_turnover[-5:]) / min(5, len(p.taiex_turnover)), 2
        )

    if p.futures_oi_series:
        last = p.futures_oi_series[-1]
        for label in ("外資", "投信", "自營商"):
            for suffix in ("淨未平倉口數", "單日增減"):
                k = f"{label}{suffix}"
                if k in last:
                    d[k] = last[k]

    day_s = (p.taifex_tx or {}).get("day_session") or {}
    if day_s.get("close") and d.get("加權指數收盤"):
        d["日盤台指期收盤"] = day_s["close"]
        d["日盤期現價差"] = round(day_s["close"] - d["加權指數收盤"], 2)

    if p.market_breadth:
        up, down = p.market_breadth.get("上漲家數"), p.market_breadth.get("下跌家數")
        if up and down:
            d["漲跌家數比"] = round(up / down, 2)

    p.derived = d

    for name, val in [
        ("加權指數OHLC", p.taiex_ohlc),
        ("成交量值", p.taiex_turnover),
        ("類股指數", p.sector_indices),
        ("漲跌家數", p.market_breadth),
        ("三大法人現貨買賣超", p.institutional_cash),
        ("三大法人期貨未平倉", p.futures_oi_series),
        ("外資買賣超個股", p.foreign_top_stocks),
        ("融資餘額", p.margin),
        ("櫃買指數", p.tpex_index),
    ]:
        if not val:
            p.missing.append(name)
    return p
