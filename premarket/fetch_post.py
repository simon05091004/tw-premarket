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

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .fetch import (  # 共用既有工具，不重複實作
    MIRROR_RATIO,
    _get_json,
    _get_text,
    _num,
    _post_csv,
    _roc_to_iso,
    TAIFEX_DAILY,
    TAIFEX_INST,
    fetch_institutional_cash,
    fetch_margin,
    fetch_taifex_tx,
    fetch_futures_oi_series,
    fetch_taiex_ohlc,
    fetch_taiex_turnover,
)

log = logging.getLogger(__name__)

TWSE_MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INDEX = "https://www.tpex.org.tw/openapi/v1/tpex_index"
TPEX_TRADING = "https://www.tpex.org.tw/openapi/v1/tpex_daily_trading_index"


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

# 名稱必須與證交所完全一致 —— 對不上會靜默漏抓。
# 曾用「電子類指數 / 航運業類指數 / 塑膠工業類指數」，實際名稱是下列這些。
WATCH_SECTORS = (
    "發行量加權股價指數",
    "電子工業類指數",
    "金融保險類指數",
    "半導體類指數",
    "電腦及週邊設備類指數",
    "其他電子類指數",
    "航運類指數",
    "塑膠類指數",
)

_HTML_TAG = re.compile(r"<[^>]*>")


def _sign_from_cell(cell: Any) -> int:
    """
    漲跌符號欄回傳的是 HTML：<p style ='color:green'>-</p>。
    直接拿去比對 "-" 永遠不成立，會把所有跌勢當成漲勢。
    """
    return -1 if _HTML_TAG.sub("", str(cell)).strip() in {"-", "－"} else 1


def fetch_sector_indices(d: date) -> dict[str, dict] | None:
    """各類股收盤指數與漲跌點。"""
    js = _get_json(TWSE_MI_INDEX, {"date": d.strftime("%Y%m%d"), "type": "IND", "response": "json"})
    if not js:
        return None
    # 這支回傳多張表（價格指數／報酬指數／跨市場…），類股在價格指數那張。
    # 報酬指數的名稱不同（「發行量加權股價報酬指數」），不會誤配。
    rows: list[list[str]] = []
    for t in js.get("tables", []) or []:
        rows.extend(t.get("data") or [])
    if not rows:
        return None

    out: dict[str, dict] = {}
    for r in rows:
        if len(r) < 5:
            continue
        name = r[0].strip()
        if name not in WATCH_SECTORS or name in out:
            continue
        close = _num(r[1])
        pts = _num(r[3])
        sign = _sign_from_cell(r[2])
        out[name] = {
            "close": close,
            "change": round(pts * sign, 2) if pts is not None else None,
            # 漲跌百分比欄位本身已帶正負號，直接採用，不要自己回推
            "change_pct": _num(r[4]),
        }
    return out or None


# ---------------------------------------------------------------------------
# 漲跌家數（市場廣度）
# ---------------------------------------------------------------------------


_BREADTH_CELL = re.compile(r"([\d,]+)(?:\s*\(([\d,]+)\))?")


def fetch_market_breadth(d: date) -> dict | None:
    """
    上漲/下跌/持平家數，以及漲停、跌停家數。
    大盤漲 3% 但只有 400 家上漲，跟 1,200 家上漲是完全不同的行情。

    回傳格式是 ['上漲(漲停)', '6,466(253)', '530(20)']：
    家數與漲停數擠在同一格,舊版直接 _num() 會因為括號而回 None。
    欄位有「整體市場」與「股票」兩欄,這裡取**股票**——
    整體市場含權證與 ETF（六千多筆）,拿來談市場廣度會嚴重失真。
    """
    js = _get_json(TWSE_MI_INDEX, {"date": d.strftime("%Y%m%d"), "type": "MS", "response": "json"})
    rows = _rows_from(js, "漲跌證券數")
    if not rows:
        return None

    def parse(cell: str) -> tuple[float | None, float | None]:
        m = _BREADTH_CELL.search(str(cell))
        if not m:
            return None, None
        return _num(m.group(1)), (_num(m.group(2)) if m.group(2) else None)

    out: dict[str, float | None] = {}
    for r in rows:
        if len(r) < 3:
            continue
        label = str(r[0]).replace(" ", "")
        count, limit = parse(r[2])  # r[2] = 股票欄；r[1] 是整體市場
        if label.startswith("上漲"):
            out["上漲家數"], out["漲停家數"] = count, limit
        elif label.startswith("下跌"):
            out["下跌家數"], out["跌停家數"] = count, limit
        elif label.startswith("持平") or label.startswith("平盤"):
            out["持平家數"] = count
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


# ---------------------------------------------------------------------------
# 櫃買指數
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 指標一：外資現貨買賣超排除 ETF
# ---------------------------------------------------------------------------

TWSE_ISIN = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
ETF_CACHE = Path(__file__).resolve().parent.parent / "docs" / "data" / "etf-codes.json"
ETF_CACHE_DAYS = 7
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _cells(tr: str) -> list[str]:
    return [_HTML_TAG.sub("", c).replace("　", " ").strip() for c in _TD.findall(tr)]


def fetch_etf_codes(force: bool = False) -> set[str] | None:
    """
    上市 ETF 代號集合，取自證交所 ISIN 對照表。

    判定依據是表中的「證券種類」分類標頭（單格列：股票／ETF／ETN／特別股…），
    標頭以下的列即屬該類別 —— 不用代號 regex，因為 00 開頭的不全是 ETF，
    而主動式 ETF（00xxxA）與 ETN 的編碼規則也在變。

    頁面約 7MB，快取 7 天。
    """
    if not force and ETF_CACHE.exists():
        try:
            cached = json.loads(ETF_CACHE.read_text(encoding="utf-8"))
            age = datetime.now() - datetime.fromisoformat(cached["fetched_at"])
            if age < timedelta(days=ETF_CACHE_DAYS):
                return set(cached["codes"])
            log.info("ETF 名單快取已過期（%d 天），重抓", age.days)
        except Exception as exc:  # noqa: BLE001
            log.warning("ETF 名單快取讀取失敗，重抓: %s", exc)

    html = _get_text(TWSE_ISIN, encoding="big5")
    if not html:
        return None
    codes: set[str] = set()
    category = ""
    for tr in _TR.findall(html):
        c = _cells(tr)
        if len(c) == 1 and c[0]:
            category = c[0]
            continue
        if category != "ETF" or len(c) < 2:
            continue
        code = c[0].split()[0].strip() if c[0].split() else ""
        if code:
            codes.add(code)
    if not codes:
        log.warning("ISIN 表解析不到 ETF，格式可能已變更")
        return None

    try:
        ETF_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ETF_CACHE.write_text(
            json.dumps(
                {"fetched_at": datetime.now().isoformat(timespec="seconds"),
                 "codes": sorted(codes)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("ETF 名單快取寫入失敗（不影響本次）: %s", exc)
    log.info("ETF 名單：%d 檔", len(codes))
    return codes


def fetch_stock_closes(d: date) -> dict[str, float] | None:
    """全市場個股收盤價（不含權證），供股數換算金額。"""
    js = _get_json(
        TWSE_MI_INDEX, {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}
    )
    if not js:
        return None
    out: dict[str, float] = {}
    for t in js.get("tables", []) or []:
        fields = [str(f) for f in (t.get("fields") or [])]
        if "收盤價" not in fields:
            continue
        i_code, i_close = fields.index("證券代號"), fields.index("收盤價")
        for r in t.get("data") or []:
            if len(r) <= i_close:
                continue
            close = _num(r[i_close])
            if close is not None:
                out[str(r[i_code]).strip()] = close
    return out or None


def fetch_foreign_ex_etf(d: date, top_n: int = 10) -> dict | None:
    """
    外資買賣超拆成 ETF 與個股兩部分。

    T86 只有股數、沒有金額，所以金額是「買賣超股數 × 當日收盤價」換算的近似值
    （實際成交均價不等於收盤價）。總額另外取自 BFI82U —— 那是證交所公布的
    權威數字，用它來對帳，換算誤差才不會悄悄累積成一個看起來很合理的錯誤。
    """
    js = _get_json(
        TWSE_T86, {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"}
    )
    if not js or not js.get("data"):
        return None
    fields = [str(f) for f in (js.get("fields") or [])]
    try:
        i_net = next(i for i, f in enumerate(fields) if "外陸資買賣超股數" in f)
    except StopIteration:
        return None

    etf_codes = fetch_etf_codes()
    closes = fetch_stock_closes(d)
    if etf_codes is None or closes is None:
        log.warning("ETF 名單或收盤價缺漏，無法拆分 ETF")
        return None

    etf_amt = stock_amt = 0.0
    stock_rows: list[dict] = []
    for r in js["data"]:
        if len(r) <= i_net:
            continue
        code, name = str(r[0]).strip(), str(r[1]).strip()
        shares = _num(r[i_net])
        close = closes.get(code)
        if shares is None or close is None:
            continue
        amt = shares * close
        if code in etf_codes:
            etf_amt += amt
        else:
            stock_amt += amt
            stock_rows.append(
                {"code": code, "name": name, "net_lots": round(shares / 1000),
                 "net_amount_yi": round(amt / 1e8, 2)}
            )

    cash = fetch_institutional_cash(d) or {}
    total_yi = cash.get("外資及陸資買賣超_億")
    etf_yi, stock_yi = round(etf_amt / 1e8, 2), round(stock_amt / 1e8, 2)
    stock_rows.sort(key=lambda x: x["net_amount_yi"])

    out = {
        "外資買賣超總額_億": total_yi,
        "ETF部分_億_換算": etf_yi,
        "排除ETF後淨額_億": (round(total_yi - etf_yi, 2) if total_yi is not None else None),
        "ETF佔比_pct": (
            round(abs(etf_yi) / abs(total_yi) * 100, 1)
            if total_yi not in (None, 0) else None
        ),
        "排除ETF後賣超前N": stock_rows[:top_n],
        "排除ETF後買超前N": list(reversed(stock_rows[-top_n:])),
        "換算說明": "ETF/個股金額為買賣超股數×當日收盤價之近似值；總額取自 BFI82U",
    }
    # 對帳：換算總和應接近權威總額，差太多代表欄位或名單取錯
    if total_yi is not None:
        drift = abs((etf_yi + stock_yi) - total_yi)
        out["換算與總額差額_億"] = round((etf_yi + stock_yi) - total_yi, 2)
        if drift > abs(total_yi) * 0.15 + 20:
            log.warning("ETF 拆分換算與 BFI82U 總額差 %.2f 億，請檢查欄位", drift)
    return out


# ---------------------------------------------------------------------------
# 指標二：融資餘額連續增減天數
# ---------------------------------------------------------------------------


def fetch_margin_series(trade_dates: list[str], closes_by_date: dict[str, float]) -> dict | None:
    """
    近 N 個交易日的融資餘額，以及兩種連續天數。

    交易日清單直接沿用 K 棒的日期，不自己推算行事曆；
    每次執行重抓、重算，不寫狀態檔 —— 補跑任一天結果都相同。
    MI_MARGN 是單日端點，N 天就是 N 次請求（節流 3 秒，30 天約 90 秒）。
    """
    series: list[dict] = []
    for ds in trade_dates:
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        m = fetch_margin(d)
        if not m:
            continue
        series.append({"date": ds, "融資餘額_億": m["融資餘額_億"],
                       "前日餘額_億": m["融資前日餘額_億"]})
    if len(series) < 2:
        return None

    for i, row in enumerate(series):
        row["日增減_億"] = round(row["融資餘額_億"] - row["前日餘額_億"], 2)
        close = closes_by_date.get(row["date"])
        prev_close = closes_by_date.get(series[i - 1]["date"]) if i else None
        row["指數漲跌"] = (
            round(close - prev_close, 2) if close is not None and prev_close is not None else None
        )

    def streak(pred) -> int:
        """從最後一天往回數，連續符合條件的天數。"""
        n = 0
        for row in reversed(series):
            if pred(row):
                n += 1
            else:
                break
        return n

    last = series[-1]["日增減_億"]
    direction = 1 if last > 0 else (-1 if last < 0 else 0)
    consecutive = direction * streak(
        lambda r: (r["日增減_億"] > 0) if direction > 0 else (r["日增減_億"] < 0)
    ) if direction else 0

    # 指數跌 + 融資增：籌碼由法人流向散戶，連續多天才有意義,所以獨立計算
    down_up = streak(
        lambda r: r["指數漲跌"] is not None and r["指數漲跌"] < 0 and r["日增減_億"] > 0
    )

    # 連續段若填滿整個視窗，真實天數可能更長 —— 標記出來，
    # 否則「連續 10 天」會被當成精確值，實際上是「至少 10 天」。
    window_capped = max(abs(consecutive), down_up) >= len(series)

    return {
        "序列": series,
        "融資連續增減天數": consecutive,
        "指數跌但融資增_連續天數": down_up,
        "最新融資餘額_億": series[-1]["融資餘額_億"],
        "最新日增減_億": last,
        "序列天數": len(series),
        "連續天數觸及視窗上限": window_capped,
    }


# ---------------------------------------------------------------------------
# 指標三：期現價差 5 日變化
# ---------------------------------------------------------------------------

BASIS_ABNORMAL_POINTS = 150.0
# 融資序列只用來數連續天數,不需要長序列。
# MI_MARGN 是單日端點,每多一天就多一次請求（節流 3 秒）—— 30 天要 100 秒,
# 逼近 workflow 的 timeout。10 天足以涵蓋絕大多數連續段。
MARGIN_TREND_DAYS = 10


def fetch_basis_series(end: date, closes_by_date: dict[str, float], days: int = 5) -> dict | None:
    """
    近 N 日期現價差（台指期日盤收盤 − 加權指數收盤）。

    期交所支援區間查詢，一次請求就能取回整段,不必每天打一次。
    近月合約取到期月份最小者，與 fetch_taifex_tx 同規則。
    """
    start = end - timedelta(days=days * 3 + 10)  # 交易日換算日曆日,寬鬆取
    rows = _post_csv(
        TAIFEX_DAILY,
        {
            "down_type": "1",
            "commodity_id": "TX",
            "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d"),
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

    i_date, i_month, i_close = col("交易日期"), col("到期月份(週別)", "契約月份"), col("收盤價")
    i_sess = col("交易時段")
    if None in (i_date, i_month, i_close):
        return None

    by_date: dict[str, dict] = {}
    for r in rows[1:]:
        if len(r) <= max(x for x in (i_date, i_month, i_close, i_sess) if x is not None):
            continue
        if i_sess is not None and not r[i_sess].strip().startswith("一般"):
            continue  # 只取日盤
        month = r[i_month].strip()
        if "/" in month or not month[:6].isdigit():
            continue  # 排除價差／週合約
        close = _num(r[i_close])
        if close is None:
            continue
        ds = r[i_date].strip().replace("/", "-")
        cur = by_date.get(ds)
        if cur is None or month < cur["month"]:
            by_date[ds] = {"month": month, "future": close}

    series = []
    for ds in sorted(by_date):
        spot = closes_by_date.get(ds)
        if spot is None:
            continue
        series.append(
            {"date": ds, "期貨收盤": by_date[ds]["future"], "現貨收盤": spot,
             "價差": round(by_date[ds]["future"] - spot, 2)}
        )
    series = series[-days:]
    if not series:
        return None

    values = [x["價差"] for x in series]
    avg = round(sum(values) / len(values), 2)
    today_basis = values[-1]
    prev_basis = values[-2] if len(values) > 1 else None
    change = round(today_basis - prev_basis, 2) if prev_basis is not None else None
    crossed = prev_basis is not None and (today_basis > 0) != (prev_basis > 0)
    abnormal = bool(
        (change is not None and abs(change) > BASIS_ABNORMAL_POINTS) or crossed
    )
    return {
        "序列": series,
        "當日價差": today_basis,
        "前日價差": prev_basis,
        "當日變動": change,
        "5日均價差": avg,
        "對5日均偏離": round(today_basis - avg, 2),
        "跨越正負號": crossed,
        "異常": abnormal,
        "異常門檻_點": BASIS_ABNORMAL_POINTS,
    }


def fetch_tpex_index(d: date) -> dict | None:
    """
    櫃買指數（OHLC + 漲跌）與當日成交量值。

    原本接的 tpex_mainboard_daily_close_quotes 是個股逐檔報價（上萬筆），
    不是指數。指數在 /openapi/v1/tpex_index。

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
    foreign_ex_etf: dict | None = None
    margin: dict | None = None
    margin_trend: dict | None = None
    basis_trend: dict | None = None
    short_watchlist: dict | None = None
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

    p.taiex_ohlc = fetch_taiex_ohlc(session_date, lookback=60)  # 盤後 prompt 第 7 節要 60MA
    p.taiex_turnover = fetch_taiex_turnover(session_date)
    p.sector_indices = fetch_sector_indices(session_date)
    p.market_breadth = fetch_market_breadth(session_date)
    p.institutional_cash = fetch_institutional_cash(session_date)
    p.futures_oi_series = fetch_futures_oi_series(session_date)
    p.foreign_top_stocks = fetch_foreign_top_stocks(session_date)
    p.taifex_tx = fetch_taifex_tx(session_date)
    p.margin = fetch_margin(session_date)   # 21:00 後才會有當日資料
    p.tpex_index = fetch_tpex_index(session_date)

    # 三個趨勢型指標：都以 K 棒日期為準重抓重算，不寫狀態檔，補跑結果一致
    closes_by_date = {
        b["date"]: b["close"]
        for b in (p.taiex_ohlc or [])
        if b.get("close") is not None
    }
    p.foreign_ex_etf = fetch_foreign_ex_etf(session_date)
    p.basis_trend = fetch_basis_series(session_date, closes_by_date)
    p.margin_trend = fetch_margin_series(
        sorted(closes_by_date)[-MARGIN_TREND_DAYS:], closes_by_date
    )

    # 隔日觀察清單：個股層級的篩選，資料量最大（每天一次全市場行情），放最後
    from .shortlist import build_short_watchlist

    p.short_watchlist = build_short_watchlist(sorted(closes_by_date))

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

            # 近 20 日高低點：K 棒現在有 60 根（供 60MA），這裡只取最後 20 根,
            # 否則「近 20 日」會變成「近 60 日」。prompt 第 7 節要用。
            recent = bars[-20:]
            highs = [b["high"] for b in recent if b.get("high") is not None]
            lows = [b["low"] for b in recent if b.get("low") is not None]
            if highs and lows:
                d["近20日最高"] = max(highs)
                d["近20日最低"] = min(lows)
                d["距近20日高點_pct"] = round((max(highs) - spot) / spot * 100, 2)

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
        foreign, trust = last.get("外資淨未平倉口數"), last.get("投信淨未平倉口數")
        if foreign is not None and trust is not None:
            # 「方向相反且量級接近」的門檻定在程式碼裡（見 fetch.MIRROR_RATIO），
            # 交給模型自己判斷「接近」的話，同一組數字每天可以講出不同結論。
            d["外資投信淨部位合計口數"] = round(foreign + trust)
            big = max(abs(foreign), abs(trust))
            if big:
                ratio = round(min(abs(foreign), abs(trust)) / big, 2)
                d["外資投信量級比"] = ratio
                d["外資投信鏡像對峙"] = bool(foreign * trust < 0 and ratio >= MIRROR_RATIO)

    day_s = (p.taifex_tx or {}).get("day_session") or {}
    if day_s.get("close") and d.get("加權指數收盤"):
        d["日盤台指期收盤"] = day_s["close"]
        d["日盤期現價差"] = round(day_s["close"] - d["加權指數收盤"], 2)

    if p.market_breadth:
        up, down = p.market_breadth.get("上漲家數"), p.market_breadth.get("下跌家數")
        if up and down:
            d["漲跌家數比"] = round(up / down, 2)

    if p.tpex_index and p.tpex_index.get("漲跌幅_pct") is not None:
        d["櫃買指數收盤"] = p.tpex_index["收盤"]
        d["櫃買漲跌幅_pct"] = p.tpex_index["漲跌幅_pct"]
        if d.get("當日漲跌幅") is not None:
            # 櫃買以中小型股為主,與加權（權值股主導）的落差就是資金往哪邊跑
            d["櫃買相對加權強弱_pct"] = round(
                p.tpex_index["漲跌幅_pct"] - d["當日漲跌幅"], 2
            )

    if p.foreign_ex_etf:
        for k in ("外資買賣超總額_億", "排除ETF後淨額_億", "ETF部分_億_換算", "ETF佔比_pct"):
            if p.foreign_ex_etf.get(k) is not None:
                d[k] = p.foreign_ex_etf[k]

    if p.margin_trend:
        d["融資連續增減天數"] = p.margin_trend["融資連續增減天數"]
        d["指數跌但融資增_連續天數"] = p.margin_trend["指數跌但融資增_連續天數"]
        # 連續 3 天以上才有訊號意義,由這裡判定,prompt 只負責照著寫
        d["融資背離警示"] = p.margin_trend["指數跌但融資增_連續天數"] >= 3

    if p.basis_trend:
        for k in ("當日價差", "前日價差", "當日變動", "5日均價差", "對5日均偏離",
                  "跨越正負號", "異常"):
            d[f"期現價差_{k}"] = p.basis_trend[k]

    p.derived = d

    for name, val in [
        ("加權指數OHLC", p.taiex_ohlc),
        ("成交量值", p.taiex_turnover),
        ("類股指數", p.sector_indices),
        ("漲跌家數", p.market_breadth),
        ("三大法人現貨買賣超", p.institutional_cash),
        ("三大法人期貨未平倉", p.futures_oi_series),
        ("外資買賣超個股", p.foreign_top_stocks),
        ("外資買賣超排除ETF", p.foreign_ex_etf),
        ("融資餘額", p.margin),
        ("融資連續趨勢", p.margin_trend),
        ("期現價差5日", p.basis_trend),
        ("隔日觀察清單", p.short_watchlist),
        ("櫃買指數", p.tpex_index),
    ]:
        if not val:
            p.missing.append(name)
    return p
