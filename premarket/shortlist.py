"""
隔日當沖放空「觀察」清單。

這支程式做的是篩選,不是建議：它列出今天符合客觀條件的個股,並標明每檔命中
哪些條件、以及能不能執行（可當沖、是否暫停先賣後買、融券與借券狀況）。
「該不該放空」不在這裡回答 —— 條件命中只代表值得看一眼。

三組訊號（可在下方常數調整門檻）：
  量價背離  爆量但收黑,或攻高失敗留長上影 -> 買方接手意願不足
  籌碼轉弱  外資連續賣超 -> 賣壓有延續性,不是單日情緒
  過熱回落  乖離過大後今日收黑 -> 短線漲多拉回

刻意不做的事：不計算預期報酬、不排名「最該放空」、不回測。
排序只用「命中條件數」與「量比」,兩者都是描述性的。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from .fetch import _get_json, _num

log = logging.getLogger(__name__)

TWSE_QUOTES = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_DAYTRADE = "https://openapi.twse.com.tw/v1/exchangeReport/TWTB4U"
TWSE_MARGIN_STOCK = "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U"

QUOTE_DAYS = 10        # 個股日 K 取幾天（要夠算 5 日均量與 10 日均線）
FOREIGN_DAYS = 3       # 法人買賣超取幾天（判斷連續賣超）
TOP_N = 15             # 清單最多列幾檔

VOLUME_SPIKE = 1.5     # 量比門檻：成交量 / 5 日均量
UPPER_SHADOW_RATIO = 1.5   # 上影線 / 實體，攻高失敗的判準
DEVIATION_PCT = 8.0    # 對 10 日均線乖離率門檻（%）
MIN_TURNOVER_YI = 1.0  # 成交金額下限（億），過濾流動性不足者
CONSECUTIVE_SELL_DAYS = 2  # 外資連續賣超天數門檻


def fetch_daily_quotes(d: date) -> dict[str, dict] | None:
    """全市場個股當日開高低收與成交量值（不含權證）。"""
    js = _get_json(
        TWSE_QUOTES, {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}
    )
    if not js:
        return None
    out: dict[str, dict] = {}
    for t in js.get("tables", []) or []:
        fields = [str(f) for f in (t.get("fields") or [])]
        if "收盤價" not in fields:
            continue
        idx = {k: fields.index(k) for k in
               ("證券代號", "證券名稱", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價")
               if k in fields}
        if len(idx) < 8:
            continue
        for r in t.get("data") or []:
            if len(r) <= max(idx.values()):
                continue
            close = _num(r[idx["收盤價"]])
            if close is None:
                continue
            out[str(r[idx["證券代號"]]).strip()] = {
                "name": str(r[idx["證券名稱"]]).strip(),
                "open": _num(r[idx["開盤價"]]),
                "high": _num(r[idx["最高價"]]),
                "low": _num(r[idx["最低價"]]),
                "close": close,
                "volume": _num(r[idx["成交股數"]]),
                "turnover": _num(r[idx["成交金額"]]),
            }
    return out or None


def fetch_foreign_net(d: date) -> dict[str, float] | None:
    """個股外資買賣超股數（正=買超）。"""
    js = _get_json(TWSE_T86, {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
    if not js or not js.get("data"):
        return None
    fields = [str(f) for f in (js.get("fields") or [])]
    try:
        i_net = next(i for i, f in enumerate(fields) if "外陸資買賣超股數" in f)
    except StopIteration:
        return None
    out: dict[str, float] = {}
    for r in js["data"]:
        if len(r) <= i_net:
            continue
        v = _num(r[i_net])
        if v is not None:
            out[str(r[0]).strip()] = v
    return out or None


def fetch_daytrade_eligibility() -> dict[str, bool] | None:
    """
    可現股當沖標的。值為「能否先賣後買」——
    Suspension 有值代表暫停先賣後買,那就不能當沖放空,只能先買後賣。
    這支端點只給最新一份清單,沒有歷史。
    """
    js = _get_json(TWSE_DAYTRADE)
    if not isinstance(js, list) or not js:
        return None
    return {
        str(r.get("Code", "")).strip(): not str(r.get("Suspension", "")).strip()
        for r in js
        if r.get("Code")
    }


def fetch_short_margin(d: date) -> dict[str, dict] | None:
    """個股融券餘額與次一營業日可限額（軋空風險的粗略判斷）。"""
    js = _get_json(
        TWSE_MARGIN_STOCK, {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"}
    )
    if not js or not js.get("data"):
        return None
    out: dict[str, dict] = {}
    for r in js["data"]:
        if len(r) < 14:
            continue
        out[str(r[0]).strip()] = {
            "融券今日餘額": _num(r[12]),
            "次一營業日可限額": _num(r[13]),
        }
    return out or None


def _shape(bar: dict) -> dict[str, float | None]:
    """K 線形態：上影線與實體的比例，用來判斷攻高是否失敗。"""
    o, h, lo, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
    if None in (o, h, lo, c):
        return {"上影線": None, "實體": None, "上影線實體比": None}
    upper = h - max(o, c)
    body = abs(c - o)
    return {
        "上影線": round(upper, 2),
        "實體": round(body, 2),
        # 實體極小時（十字線）比例會爆掉，用最小值防呆
        "上影線實體比": round(upper / max(body, c * 0.001), 2),
    }


def evaluate_candidate(
    code: str, bar: dict, hist: list[dict], sell_days: int
) -> dict | None:
    """
    單檔的條件判定。純函式、不做 I/O ——
    回測必須跑這一份，另寫一份就等於在測不同的東西。

    bar  : 當日 K 棒；hist: 之前的 K 棒（由舊到新，至少 5 根）
    sell_days: 外資連續賣超天數（由呼叫端算好）
    """
    turnover_yi = (bar.get("turnover") or 0) / 1e8
    if turnover_yi < MIN_TURNOVER_YI:
        return None  # 流動性不足，當沖進出成本過高

    closes = [b["close"] for b in hist if b.get("close") is not None]
    vols = [b["volume"] for b in hist[-5:] if b.get("volume")]
    if len(closes) < 5 or not vols:
        return None

    avg_vol = sum(vols) / len(vols)
    vol_ratio = round((bar.get("volume") or 0) / avg_vol, 2) if avg_vol else None
    window = closes[-10:]
    ma10 = sum(window) / len(window)
    deviation = round((bar["close"] - ma10) / ma10 * 100, 2) if ma10 else None
    prev_close = closes[-1]
    change_pct = round((bar["close"] - prev_close) / prev_close * 100, 2) if prev_close else None
    shape = _shape(bar)
    is_black = bar.get("open") is not None and bar["close"] < bar["open"]

    hits: list[str] = []
    if vol_ratio is not None and vol_ratio >= VOLUME_SPIKE and (
        is_black or (shape["上影線實體比"] or 0) >= UPPER_SHADOW_RATIO
    ):
        hits.append("量價背離")
    if sell_days >= CONSECUTIVE_SELL_DAYS:
        hits.append("籌碼轉弱")
    if deviation is not None and deviation >= DEVIATION_PCT and is_black:
        hits.append("過熱回落")
    if not hits:
        return None

    return {
        "code": code,
        "name": bar.get("name", ""),
        "開盤": bar.get("open"),
        "收盤": bar["close"],
        "漲跌幅_pct": change_pct,
        # 條件裡的「收黑」指 K 線實體為黑（收盤 < 開盤），與漲跌幅正負是兩件事：
        # 開高走低可以「漲 0.1% 但收黑」。不把這個布林值放進清單，讀者只看到
        # 漲跌幅是正的，就會判定命中條件算錯。
        "收黑": is_black,
        "成交金額_億": round(turnover_yi, 2),
        "量比": vol_ratio,
        "上影線實體比": shape["上影線實體比"],
        "對10日均線乖離_pct": deviation,
        "外資連續賣超天數": sell_days,
        "命中條件": hits,
        "命中數": len(hits),
    }


def count_sell_days(code: str, dates: list[str], foreign: dict[str, dict[str, float]]) -> int:
    """外資連續賣超天數，由最後一天往回數。"""
    n = 0
    for ds in reversed(dates):
        v = foreign.get(ds, {}).get(code)
        if v is not None and v < 0:
            n += 1
        else:
            break
    return n


def build_short_watchlist(trade_dates: list[str]) -> dict | None:
    """
    trade_dates 由呼叫端提供（沿用加權指數的 K 棒日期），最後一天即今日。
    每次執行重抓重算，不寫狀態檔。
    """
    if len(trade_dates) < 6:
        log.warning("交易日不足 %d 天，無法計算均量與乖離", 6)
        return None

    days = trade_dates[-QUOTE_DAYS:]
    quotes: dict[str, dict[str, dict]] = {}
    for ds in days:
        q = fetch_daily_quotes(datetime.strptime(ds, "%Y-%m-%d").date())
        if q:
            quotes[ds] = q
    if len(quotes) < 6:
        log.warning("個股行情只取到 %d 天，不足以計算", len(quotes))
        return None

    ordered = sorted(quotes)
    today_ds = ordered[-1]
    today = quotes[today_ds]

    foreign: dict[str, dict[str, float]] = {}
    for ds in ordered[-FOREIGN_DAYS:]:
        f = fetch_foreign_net(datetime.strptime(ds, "%Y-%m-%d").date())
        if f:
            foreign[ds] = f

    eligible = fetch_daytrade_eligibility() or {}
    margin = fetch_short_margin(datetime.strptime(today_ds, "%Y-%m-%d").date()) or {}

    candidates: list[dict] = []
    for code, bar in today.items():
        hist = [quotes[ds][code] for ds in ordered[:-1] if code in quotes[ds]]
        if len(hist) < 5:
            continue
        sell_days = count_sell_days(code, ordered[-FOREIGN_DAYS:], foreign)
        cand = evaluate_candidate(code, bar, hist, sell_days)
        if cand is None:
            continue
        cand["可當沖先賣後買"] = eligible.get(code)
        cand["融券餘額"] = (margin.get(code) or {}).get("融券今日餘額")
        candidates.append(cand)

    if not candidates:
        return {"清單": [], "說明": "今日無標的同時符合任一組條件"}

    # 排序只看訊號強度。可先賣後買是「能不能當沖放空」的資格，不是盤勢訊號 ——
    # 拿它當第一排序鍵，等於讓一檔命中 3 條件但不可先賣後買的股票，
    # 排在命中 1 條件的可先賣後買標的後面，然後被 TOP_N 切掉。
    # 清單定位是觀察，資格性限制留在欄位裡標註即可。
    candidates.sort(key=lambda x: (-x["命中數"], -(x["量比"] or 0)))
    shortlist = candidates[:TOP_N]
    return {
        "清單": shortlist,
        "檢查檔數": len(today),
        "命中檔數": len(candidates),
        "清單檔數": len(shortlist),
        "清單上限": TOP_N,
        # 兩個母數分開報：一個是全部命中檔，一個是清單內。混在一起講，
        # 「命中 156、可先賣後買 15」會被讀成資格過濾砍掉 141 檔。
        "命中檔中可先賣後買檔數": sum(1 for c in candidates if c["可當沖先賣後買"]),
        "清單中可先賣後買檔數": sum(1 for c in shortlist if c["可當沖先賣後買"]),
        "命中數分佈": {
            f"命中{n}": sum(1 for c in candidates if c["命中數"] == n) for n in (3, 2, 1)
        },
        "條件": {
            "收黑定義": "收盤 < 開盤（K 線實體為黑）；與漲跌幅正負無關，開高走低也算收黑",
            "量價背離": f"量比 ≥ {VOLUME_SPIKE} 且（收黑 或 上影線/實體 ≥ {UPPER_SHADOW_RATIO}）",
            "籌碼轉弱": f"外資連續賣超 ≥ {CONSECUTIVE_SELL_DAYS} 天",
            "過熱回落": f"對 10 日均線乖離 ≥ {DEVIATION_PCT}% 且收黑",
            "流動性下限": f"成交金額 ≥ {MIN_TURNOVER_YI} 億",
        },
        "說明": (
            "條件命中僅代表值得觀察；未回測、不含預期報酬。排序只依命中數與量比，"
            "可當沖先賣後買僅為標註，不影響入選。清單為命中檔中的前 N 檔，非全部命中檔。"
        ),
    }
