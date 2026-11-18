"""
外匯追蹤：玉山、台新、臺灣銀行牌告（現鈔／即期）＋國際市場中價 → 價差、偏離、套利路徑。

    python -m premarket.fx              # 抓一次，寫 docs/data/fx/
    python -m premarket.fx --dry-run    # 只抓、只印，不寫檔
    python -m premarket.fx --force      # 牌告沒變也寫（預設沒變就不寫，免得洗 commit）

和盤前／盤後報告無關，不呼叫模型、不花 API 費用。頁面是 docs/fx.html。

模型很小，但有三件事先講清楚，免得「套利」兩個字帶來錯誤期待：

1. 同一家銀行的牌告之內不存在無風險套利。買進價永遠低於賣出價，任何
   繞一圈回到台幣的路徑都是負的。這裡算的是「繞一圈要賠多少」——
   它是換匯成本的量尺，數字越接近 0，表示那條路越便宜。
2. 真正會出現正值的只有兩種情況：(a) 兩家銀行之間，A 的賣出價低於 B 的
   買進價；(b) 牌告滯後——國際盤已經動了，牌告還沒改，市場中價跑到
   牌告買賣價之外。(b) 不是無風險的（你得在別處用市場價平倉），但它是
   散戶實際上碰得到的訊號，所以單獨算出來。
3. 兩家銀行的外幣帳戶之間靠「跨行匯款」相通，成本是設定檔裡的
   transfer_cost_bp（把一次匯款的費用換算成佔交易金額的萬分之幾）。
   現鈔存提的最低手續費這類固定費用不在路徑計算裡，金額小的時候
   它們才是主角，頁面與 README 都有註明。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .fetch import _fetch

log = logging.getLogger("fx")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data" / "fx"
CONFIG_PATH = ROOT / "fx_config.json"
TPE = timezone(timedelta(hours=8))

FOREIGN = ("USD", "JPY", "EUR")
FIELDS = ("cash_buy", "cash_sell", "spot_buy", "spot_sell")
SOURCE_NAMES = {"bot": "臺灣銀行", "esun": "玉山銀行", "taishin": "台新銀行"}
CCY_NAMES = {"TWD": "台幣", "USD": "美元", "JPY": "日圓", "EUR": "歐元"}

BOT_CSV = "https://rate.bot.com.tw/xrt/flcsv/0/day"
ESUN_API = "https://www.esunbank.com/api/client/ExchangeRate/LastRateInfo"
# 台新「即時匯率」頁用 <script src> 引入的匯出檔：一串 document.writeln()，把牌告表格寫進網頁。
TAISHIN_EXPORT = "https://www.taishinbank.com.tw/eServiceA/transactionrate/transactionrateExport.jsp?no=5"
# 向 Yahoo 抓的四個；JPYTWD、EURTWD 由它們合成（見 fetch_market）。
YAHOO_SYMBOLS = ("USDTWD", "USDJPY", "EURUSD", "EURJPY")
# 前三個是「一單位外幣值多少台幣」，用來對牌告；後三個是交叉盤，用來對牌告隱含的交叉匯率。
MARKET_SYMBOLS = ("USDTWD", "JPYTWD", "EURTWD", "USDJPY", "EURUSD", "EURJPY")

# 牌告與市場報價的時間差超過這個分鐘數，就不判「滯後」——
# 收盤後牌告停在 16:00，國際盤繼續走，那個差距不是機會，是銀行下班了。
FRESH_MIN = 30
# 市場中價超出牌告不到這麼多 bp 就不算滯後。Yahoo 的中價是參考價，USD/TWD 一分鐘內
# 自己就會跳 3bp 左右；玉山美元套用網銀優惠後離中價只有 6bp，不設門檻會一直閃。
LAG_MIN_BP = 3.0
# 歷史：最近這幾天保留盤中每一筆，更早的每天只留最後一筆。
INTRADAY_DAYS = 10
KEEP_DAYS = 400
# 沒有任何牌告變動時，最多隔多久還是寫一次（讓頁面上的市場中價不至於太舊）。
HEARTBEAT_MIN = 55

DEFAULT_CONFIG: dict[str, Any] = {
    # 要抓哪幾家。臺銀（"bot"）對機房 IP 回驗證頁，GitHub Actions 上抓不到，
    # 所以預設不抓；在自己的電腦上跑的話可以加回來。
    "sources": ["esun", "taishin"],
    # {"bot": {"USD": 0.03}}：網銀讓分，每單位外幣讓幾元台幣。
    # 玉山的 API 自己會帶當下的網銀優惠，這裡有寫才會蓋掉它（寫 0 = 不套用優惠）。
    "discount": {},
    "alerts": [],          # [{"ccy":"JPY","field":"spot_sell","op":"<=","value":0.2}]
    # 外幣從一家銀行匯到另一家的成本，佔交易金額的萬分之幾。匯款費是固定金額，
    # 所以要配合你的單筆金額換算：費用 1,000 元、一次換 100 萬台幣 = 10bp。
    # None = 不考慮跨行匯款，兩家之間只剩台幣與現鈔可以搬。
    "transfer_cost_bp": 10,
    "arb_alert_bp": None,  # 繞一圈的報酬 ≥ 這個 bp 就通知；None = 不通知
    "lag_alert_bp": None,  # 市場中價超出牌告買賣價 ≥ 這個 bp 就通知；None = 不通知
}


class ChallengeError(RuntimeError):
    """對方回的是防機器人驗證頁，不是資料。"""


# ───────────────────────── 抓取 ─────────────────────────


def parse_bot_csv(text: str) -> dict[str, dict[str, float | None]]:
    """
    臺銀牌告 CSV → {"USD": {cash_buy, cash_sell, spot_buy, spot_sell}, ...}

    每列長這樣（買入一段、賣出一段，各自是 現金, 即期, 遠期…）：
        USD,本行買入,31.665,32.015,…,本行賣出,32.335,32.115,…
    不用固定欄位序號，改找「買入／賣出」那一格再往右取兩格：遠期天期的欄數
    增減過，序號會跟著位移，但「標記的右邊是現金、再右邊是即期」沒變過。
    沒掛牌的項目（例如某些幣別不收現鈔）是 0，轉成 None。
    """
    head = text.lstrip("﻿ \r\n")[:200].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        raise ChallengeError("臺銀回傳的是 HTML 驗證頁，不是 CSV")

    out: dict[str, dict[str, float | None]] = {}
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        cells = [c.strip() for c in row]
        if not cells or cells[0] not in FOREIGN:
            continue
        q: dict[str, float | None] = {}
        for i, c in enumerate(cells):
            side = "buy" if "買入" in c else "sell" if "賣出" in c else None
            if side and i + 2 < len(cells):
                q[f"cash_{side}"] = _pos(cells[i + 1])
                q[f"spot_{side}"] = _pos(cells[i + 2])
        if all(k in q for k in FIELDS) and (q["spot_buy"] or q["cash_buy"]):
            out[cells[0]] = q
    if not out:
        raise ValueError("CSV 裡找不到 USD/JPY/EUR 的牌告列")
    return out


def _pos(s: str) -> float | None:
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def fetch_bot() -> dict | None:
    """回 {"quotes": {...}, "board_time": ISO 或 None}；失敗回 None。"""

    def parse(r: requests.Response) -> dict:
        r.encoding = "utf-8"
        quotes = parse_bot_csv(r.text)
        # 檔名帶牌告時間：ExchangeRate@202611161600.csv
        m = re.search(r"(\d{12})", r.headers.get("Content-Disposition", ""))
        board = None
        if m:
            board = datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=TPE).isoformat()
        return {"quotes": quotes, "board_time": board}

    return _fetch("GET", BOT_CSV, parse)


def parse_esun(data: dict) -> dict:
    """
    玉山牌告 API 的回應 → {"quotes": {...}, "board_time": ISO, "bonus": {"USD": 0.031, …}}

    每筆長這樣（即期是數字，現鈔與優惠是字串，沒掛牌的現鈔是空字串）：
        {"CCY":"USD/TWD","BBoardRate":32.88,"SBoardRate":32.98,
         "CashBBoardRate":"32.63","CashSBoardRate":"33.18","Bonus":"0.031",
         "UpdateTime":"/Date(1794837560000)/"}
    Bonus 是銀行公布的網銀／App 換匯讓分：BuyIncreaseRate = 買入 + Bonus、
    SellDecreaseRate = 賣出 − Bonus。只有和這兩個優惠價對得起來的 Bonus 才採用。

    最外層的 DiscontFlag 不要拿來當開關：實測連打八次，它在 "T" 和 "" 之間
    隨機跳（看起來是不同節點回的），而 Bonus 與優惠價完全沒變。
    """
    quotes: dict[str, dict] = {}
    bonus: dict[str, float] = {}
    times: list[int] = []
    for r in data["Rates"]:
        ccy, _, base = str(r.get("CCY", "")).partition("/")
        if ccy not in FOREIGN or base != "TWD":
            continue
        q = {
            "cash_buy": _pos(str(r.get("CashBBoardRate") or "")),
            "cash_sell": _pos(str(r.get("CashSBoardRate") or "")),
            "spot_buy": _pos(str(r.get("BBoardRate") or "")),
            "spot_sell": _pos(str(r.get("SBoardRate") or "")),
        }
        if not (q["spot_buy"] or q["cash_buy"]):
            continue
        quotes[ccy] = q
        b = _pos(str(r.get("Bonus") or ""))
        better_buy = _pos(str(r.get("BuyIncreaseRate") or ""))
        if b and q["spot_buy"] and better_buy and abs(q["spot_buy"] + b - better_buy) < 1e-6:
            bonus[ccy] = b
        m = re.search(r"(\d{10,})", str(r.get("UpdateTime") or ""))
        if m:
            times.append(int(m.group(1)))
    if not quotes:
        raise ValueError("玉山的回應裡找不到 USD/JPY/EUR")
    board = datetime.fromtimestamp(max(times) / 1000, TPE).replace(microsecond=0).isoformat() if times else None
    return {"quotes": quotes, "board_time": board, "bonus": bonus}


def fetch_esun() -> dict | None:
    def parse(r: requests.Response) -> dict:
        if "json" not in r.headers.get("Content-Type", "").lower():
            raise ChallengeError("玉山回傳的不是 JSON")
        return parse_esun(r.json())

    return _fetch("POST", ESUN_API, parse, json={})


_TAISHIN_FIELDS = {"即期買入": "spot_buy", "即期賣出": "spot_sell", "現鈔買入": "cash_buy", "現鈔賣出": "cash_sell"}


def parse_taishin(js: str) -> dict:
    """
    台新的牌告匯出檔 → {"quotes": {...}, "board_time": ISO}

    內容是一行一行的 document.writeln('…')，接起來是一段 HTML，中文用 \\uXXXX 表示。
    裡面同一個幣別出現三次：桌機版一張四欄的表，手機版即期、現鈔各一張兩欄的表。
    只讀桌機版那張，每一列長這樣：
        <td class="currency">32.8650<br><label class="highest">近30日最高</label></td>
        … 共四格 …
        <td><a onclick="queryhistory('USD')">查詢</a></td>
    幣別從 queryhistory('USD') 取，四格對到哪個欄位看表頭那一列的文字（不寫死順序）。
    每一格只認第一個詞：後面可能跟著「近30日最高」這種標籤，沒掛牌的是 "-"。
    """
    lines = re.findall(r"document\.writeln\('(.*)'\);", js)
    if not lines:
        raise ChallengeError("台新回傳的不是牌告匯出檔")
    html = "\n".join(lines).replace('\\"', '"').replace("\\/", "/").replace("\\'", "'")
    html = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), html)

    def cell_text(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()

    fields: list[str] | None = None
    quotes: dict[str, dict] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        if fields is None:
            labels = [cell_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            got = [_TAISHIN_FIELDS[x] for x in labels if x in _TAISHIN_FIELDS]
            if len(got) == 4:
                fields = got
            continue
        m = re.search(r"queryhistory\('([A-Z]{3})'\)", row)
        cells = re.findall(r'<td class="currency"[^>]*>(.*?)</td>', row, re.S)
        if not m or m.group(1) not in FOREIGN or len(cells) != len(fields) or m.group(1) in quotes:
            continue
        q = {f: _pos((cell_text(c).split() or [""])[0]) for f, c in zip(fields, cells)}
        if q["spot_buy"] or q["cash_buy"]:
            quotes[m.group(1)] = {k: q[k] for k in FIELDS}
    if not quotes:
        raise ValueError("台新的匯出檔裡找不到 USD/JPY/EUR 的牌告列")

    board = None
    t = re.search(r"更新時間\s*[:：]\s*(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", html)
    if t:
        board = datetime.strptime(t.group(1), "%Y/%m/%d %H:%M:%S").replace(tzinfo=TPE).isoformat()
    return {"quotes": quotes, "board_time": board}


def fetch_taishin() -> dict | None:
    def parse(r: requests.Response) -> dict:
        r.encoding = "utf-8"
        return parse_taishin(r.text)

    # 台新的站台有一部分連線會在 TLS 交握時被重設（實測約三成），靠 _fetch 的重試吸收。
    return _fetch("GET", TAISHIN_EXPORT, parse)


FETCHERS = {"bot": fetch_bot, "esun": fetch_esun, "taishin": fetch_taishin}


def fetch_market() -> dict[str, dict]:
    """
    Yahoo 匯率中價（1 分鐘線最後一筆）。回 {"USDTWD": {"price":…, "time": ISO}, …}。

    JPYTWD、EURTWD 不直接抓：Yahoo 的 JPYTWD=X 只給四位小數（0.2044），
    一跳就是 5bp，拿來量牌告偏離太粗。改用 USDTWD ÷ USDJPY、EURUSD × USDTWD 合成。
    """
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance 未安裝")
        return {}
    try:
        data = yf.download(
            [s + "=X" for s in YAHOO_SYMBOLS], period="1d", interval="1m",
            group_by="ticker", auto_adjust=False, progress=False, threads=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("yfinance 下載失敗: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for sym in YAHOO_SYMBOLS:
        try:
            close = data[sym + "=X"]["Close"].dropna()
            t = close.index[-1].to_pydatetime().astimezone(TPE).replace(microsecond=0)
            out[sym] = {"price": round(float(close.iloc[-1]), 6), "time": t.isoformat()}
        except Exception as exc:  # noqa: BLE001
            log.warning("解析 %s 失敗: %s", sym, exc)
    return synthesize_twd_crosses(out)


def synthesize_twd_crosses(mkt: dict[str, dict]) -> dict[str, dict]:
    """補上 JPYTWD、EURTWD。時間取兩腿中較舊的那個。"""
    out = dict(mkt)
    usd = mkt.get("USDTWD")
    for sym, leg, fn in (
        ("JPYTWD", "USDJPY", lambda u, x: u / x),
        ("EURTWD", "EURUSD", lambda u, x: u * x),
    ):
        other = mkt.get(leg)
        if usd and other:
            out[sym] = {
                "price": round(fn(usd["price"], other["price"]), 6),
                "time": min(usd["time"], other["time"]),
            }
    return out


# ───────────────────────── 套利引擎 ─────────────────────────
#
# 節點是「你手上拿著什麼」：
#   TWD            台幣（現金與帳戶不分，轉來轉去幾乎不用錢）
#   USD.cash       美元現鈔（哪家銀行領的都一樣）
#   USD.acct@bot   放在臺銀外幣帳戶裡的美元（跨行搬動要匯費，所以每家各一個節點）
# 邊是一次換匯或搬動，rate 是「1 單位起點換到幾單位終點」。
# 兩家的外幣帳戶之間有一條「跨行匯款」的邊，rate = 1 − 匯款成本。少了它，
# 「A 行即期買、B 行即期賣」這條最實際的套利路徑根本不在圖上，只剩現鈔可繞。
# docs/fx.html 裡有一份一模一樣的 JS 版；改這裡要一起改那邊，
# tools/check_fx_page.py 會比對兩邊算出來的最佳路徑。


def apply_discount(q: dict, d: float | None) -> dict:
    """網銀讓分只給即期：賣出價少收 d、買進價多給 d。"""
    if not d:
        return dict(q)
    out = dict(q)
    if out.get("spot_sell"):
        out["spot_sell"] = round(out["spot_sell"] - d, 6)
    if out.get("spot_buy"):
        out["spot_buy"] = round(out["spot_buy"] + d, 6)
    return out


def merge_discount(auto: dict, cfg: dict) -> dict:
    """銀行自己公布的優惠（auto）打底，fx_config.json 有寫的幣別蓋過去。"""
    out: dict[str, dict] = {}
    for src in set(auto) | set(cfg):
        merged = {**(auto.get(src) or {}), **(cfg.get(src) or {})}
        if merged:
            out[src] = merged
    return out


def node_label(node: str) -> str:
    """"USD.acct@esun" → "美元帳戶·玉山銀行"。提醒文字用。"""
    if node == "TWD":
        return "台幣"
    ccy, _, rest = node.partition(".")
    if rest == "cash":
        return CCY_NAMES.get(ccy, ccy) + "現鈔"
    src = rest.partition("@")[2]
    return f"{CCY_NAMES.get(ccy, ccy)}帳戶·{SOURCE_NAMES.get(src, src)}"


def build_edges(sources: dict[str, dict[str, dict]], transfer_bp: float | None = None) -> list[dict]:
    """
    sources = {"bot": {"USD": {cash_buy,…}, …}, …} → 邊的清單。
    transfer_bp 是外幣跨行匯款的成本（bp）；None 表示不放跨行匯款的邊。
    """
    edges: list[dict] = []
    accts: dict[str, list[str]] = {}   # 幣別 → 有外幣帳戶（有即期報價）的銀行

    def add(frm: str, to: str, rate: float, how: str, src: str) -> None:
        if rate and rate > 0:
            edges.append({"from": frm, "to": to, "rate": rate, "how": how, "source": src})

    for src, quotes in sources.items():
        for ccy, q in quotes.items():
            cb, cs, sb, ss = (q.get(k) for k in FIELDS)
            acct, cash = f"{ccy}.acct@{src}", f"{ccy}.cash"
            if ss:
                add("TWD", acct, 1 / ss, "即期賣出", src)
            if sb:
                add(acct, "TWD", sb, "即期買入", src)
            if cs:
                add("TWD", cash, 1 / cs, "現鈔賣出", src)
            if cb:
                add(cash, "TWD", cb, "現鈔買入", src)
            # 外幣帳戶 ↔ 現鈔：銀行收的是現鈔與即期的價差（匯差手續費）。
            if ss and cs:
                add(acct, cash, ss / cs, "提領現鈔", src)
            if sb and cb:
                add(cash, acct, cb / sb, "存入現鈔", src)
            if ss or sb:
                accts.setdefault(ccy, []).append(src)
    if transfer_bp is not None:
        keep = 1 - transfer_bp / 1e4
        for ccy, banks in accts.items():
            for a in banks:
                for b in banks:
                    if a != b:
                        add(f"{ccy}.acct@{a}", f"{ccy}.acct@{b}", keep, "跨行匯款", a)
    return edges


def _walk(edges: list[dict], start: str, goal: str, max_len: int) -> list[dict]:
    """列舉 start → goal 的簡單路徑（start == goal 時就是迴圈），依倍率由大到小。"""
    by_from: dict[str, list[dict]] = {}
    for e in edges:
        by_from.setdefault(e["from"], []).append(e)
    found: list[dict] = []

    def dfs(node: str, factor: float, steps: list[dict], seen: set[str]) -> None:
        for e in by_from.get(node, ()):
            nxt = e["to"]
            f = factor * e["rate"]
            if nxt == goal:
                found.append({"factor": f, "steps": steps + [e]})
                continue
            if nxt in seen or len(steps) + 1 >= max_len:
                continue
            dfs(nxt, f, steps + [e], seen | {nxt})

    dfs(start, 1.0, [], {start})
    # 倍率相同的只留步數最少的那條。銀行對「帳戶 ↔ 現鈔」收的正好是現鈔與即期的
    # 價差，所以「買即期再提現鈔」和「直接買現鈔」算出來一模一樣；不收斂的話
    # 排行榜前幾名會是同一件事的不同寫法。
    found.sort(key=lambda p: (-p["factor"], len(p["steps"])))
    out: list[dict] = []
    for p in found:
        if not out or abs(p["factor"] / out[-1]["factor"] - 1) > 1e-9:
            out.append(p)
    return out


def best_cycles(edges: list[dict], start: str = "TWD", max_len: int = 6, top: int = 8) -> list[dict]:
    """從 start 出發繞回 start 的路徑，bp = (倍率 − 1) × 10000。正值才是套利。"""
    out = []
    for p in _walk(edges, start, start, max_len)[:top]:
        out.append({"bp": round((p["factor"] - 1) * 1e4, 2), "factor": p["factor"], "steps": p["steps"]})
    return out


def best_paths(edges: list[dict], src: str, dst: str, max_len: int = 5, top: int = 4) -> list[dict]:
    return _walk(edges, src, dst, max_len)[:top]


# ───────────────────────── 衍生指標 ─────────────────────────


def _mid(a: float | None, b: float | None) -> float | None:
    return (a + b) / 2 if a and b else None


def _minutes_apart(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) / 60


def quote_metrics(q: dict, mkt: dict | None, board_time: str | None) -> dict:
    """單一幣別：買賣價差、相對市場中價的偏離、是否滯後。"""
    cb, cs, sb, ss = (q.get(k) for k in FIELDS)
    m: dict[str, Any] = {
        "spot_spread_pct": round((ss - sb) / _mid(sb, ss) * 100, 3) if sb and ss else None,
        "cash_spread_pct": round((cs - cb) / _mid(cb, cs) * 100, 3) if cb and cs else None,
        "market_mid": None, "dev_bp": None, "lag": None,
    }
    if not (mkt and sb and ss):
        return m
    px = mkt["price"]
    m["market_mid"] = px
    m["dev_bp"] = round((_mid(sb, ss) / px - 1) * 1e4, 1)
    gap = _minutes_apart(board_time, mkt["time"])
    if gap is None or gap > FRESH_MIN:
        return m
    # 市場中價跑到牌告之外：銀行還在用舊價。
    side, over = ("buy", px / ss - 1) if px > ss else ("sell", sb / px - 1)  # buy = 跟銀行買便宜
    if over * 1e4 >= LAG_MIN_BP:
        m["lag"] = {"side": side, "bp": round(over * 1e4, 1)}
    return m


def cross_metrics(quotes: dict[str, dict], market: dict[str, dict]) -> list[dict]:
    """
    牌告隱含的交叉匯率 vs 國際盤。
    implied 用即期中價相除；cost_bp 是真的透過台幣換一趟（賣 base 買 quote）
    比國際盤多付多少。
    """
    out = []
    for sym in ("USDJPY", "EURUSD", "EURJPY"):
        base, quote = sym[:3], sym[3:]
        qb, qq, mk = quotes.get(base), quotes.get(quote), market.get(sym)
        if not (qb and qq):
            continue
        mb, mq = _mid(qb.get("spot_buy"), qb.get("spot_sell")), _mid(qq.get("spot_buy"), qq.get("spot_sell"))
        if not (mb and mq):
            continue
        row: dict[str, Any] = {"pair": sym, "implied": mb / mq, "market": None, "diff_bp": None, "cost_bp": None}
        if mk:
            row["market"] = mk["price"]
            row["diff_bp"] = round((row["implied"] / mk["price"] - 1) * 1e4, 1)
            got = qb["spot_buy"] / qq["spot_sell"]  # 1 base 實際換到幾 quote
            row["cost_bp"] = round((1 - got / mk["price"]) * 1e4, 1)
        out.append(row)
    return out


def history_stats(rows: list[list], source: str, ccy: str, today: str, days: int = 90) -> dict | None:
    """近 N 天每日最後一筆即期賣出價：目前落在第幾百分位、區間高低、較前一日變動。"""
    daily: dict[str, float] = {}
    for ts, src, c, _cb, _cs, _sb, ss in rows:
        if src == source and c == ccy and ss:
            daily[ts[:10]] = ss  # rows 依時間排序，後面的蓋掉前面的
    cutoff = (datetime.fromisoformat(today) - timedelta(days=days)).date().isoformat()
    series = [(d, v) for d, v in sorted(daily.items()) if d >= cutoff]
    if len(series) < 2:
        return None
    cur = series[-1][1]
    vals = [v for _, v in series]
    prev = series[-2][1]
    return {
        "days": len(series),
        "pct_rank": round(sum(1 for v in vals if v <= cur) / len(vals) * 100),
        "low": min(vals), "high": max(vals),
        "prev_close": prev,
        "chg_pct": round((cur / prev - 1) * 100, 3),
    }


def check_alerts(cfg: dict, sources: dict, derived: dict) -> list[dict]:
    """回 [{"key":…, "text":…}]。key 用來做「同一天同一則只發一次」。"""
    hits: list[dict] = []
    ops = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}
    label = {"cash_buy": "現鈔買入", "cash_sell": "現鈔賣出", "spot_buy": "即期買入", "spot_sell": "即期賣出"}

    for a in cfg.get("alerts") or []:
        src = a.get("source", "bot")
        q = (sources.get(src) or {}).get(a.get("ccy"))
        v = q.get(a.get("field")) if q else None
        if v is None or a.get("op") not in ops:
            continue
        if ops[a["op"]](v, a["value"]):
            hits.append({
                "key": f"px:{src}:{a['ccy']}:{a['field']}:{a['op']}:{a['value']}",
                "text": f"{SOURCE_NAMES.get(src, src)} {a['ccy']} {label.get(a['field'], a['field'])} {v:g}"
                        f"（門檻 {a['op']} {a['value']:g}）",
            })

    thr = cfg.get("arb_alert_bp")
    best = (derived.get("cycles") or [None])[0]
    if thr is not None and best and best["bp"] >= thr and best["bp"] > 0:
        key = "→".join(f"{s['to']}@{s['source']}" for s in best["steps"])
        route = "台幣" + "".join(
            f" →（{SOURCE_NAMES.get(s['source'], s['source'])}{s['how']}）{node_label(s['to'])}" for s in best["steps"])
        wired = any(s["how"] == "跨行匯款" for s in best["steps"])
        note = f"（已扣跨行匯款成本 {cfg.get('transfer_cost_bp'):g}bp）" if wired else ""
        hits.append({"key": f"arb:{key}", "text": f"套利路徑 +{best['bp']:.1f}bp{note}：{route}"})

    thr = cfg.get("lag_alert_bp")
    if thr is not None:
        for row in derived.get("quotes") or []:
            lag = row.get("lag")
            if lag and lag["bp"] >= thr:
                verb = "向銀行買" if lag["side"] == "buy" else "賣給銀行"
                hits.append({
                    "key": f"lag:{row['source']}:{row['ccy']}:{lag['side']}",
                    "text": f"{SOURCE_NAMES.get(row['source'], row['source'])} {row['ccy']} "
                            f"牌告滯後 {lag['bp']:.1f}bp（{verb}優於市場中價）",
                })
    return hits


def derive(sources: dict, board_times: dict, market: dict, rows: list[list], today: str,
           transfer_bp: float | None = None) -> dict:
    quotes = []
    for src, qs in sources.items():
        for ccy in FOREIGN:
            q = qs.get(ccy)
            if not q:
                continue
            row = {"source": src, "ccy": ccy, **{k: q.get(k) for k in FIELDS}}
            row.update(quote_metrics(q, market.get(ccy + "TWD"), board_times.get(src)))
            row["stats"] = history_stats(rows, src, ccy, today)
            quotes.append(row)
    edges = build_edges(sources, transfer_bp)
    crosses = []
    for src, qs in sources.items():
        for c in cross_metrics(qs, market):
            crosses.append({"source": src, **c})
    return {"quotes": quotes, "crosses": crosses, "cycles": best_cycles(edges)}


# ───────────────────────── 歷史與輸出 ─────────────────────────


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_config() -> dict:
    return {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}


def append_history(rows: list[list], ts: str, sources: dict, market: dict) -> list[list]:
    """
    加上這一輪的報價，然後瘦身。市場中價以 source="mkt" 存成買賣同價的一列。
    和該來源上一筆完全相同就不加——牌告一天只改十幾次，不必每 15 分鐘存一筆。
    """
    last: dict[tuple, list] = {}
    for r in rows:
        last[(r[1], r[2])] = r[3:]

    def push(src: str, ccy: str, vals: list) -> None:
        if last.get((src, ccy)) != vals:
            rows.append([ts, src, ccy, *vals])

    for src, qs in sources.items():
        for ccy, q in qs.items():
            push(src, ccy, [q.get(k) for k in FIELDS])
    for ccy in FOREIGN:
        m = market.get(ccy + "TWD")
        if m:
            px = round(m["price"], 6)
            push("mkt", ccy, [None, None, px, px])
    return trim_history(rows, ts)


def trim_history(rows: list[list], now_ts: str) -> list[list]:
    now = datetime.fromisoformat(now_ts)
    intraday_from = (now - timedelta(days=INTRADAY_DAYS)).date().isoformat()
    keep_from = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    rows = sorted(rows, key=lambda r: r[0])
    last_of_day: dict[tuple, int] = {}
    for i, r in enumerate(rows):
        last_of_day[(r[0][:10], r[1], r[2])] = i
    keep_idx = set(last_of_day.values())
    return [
        r for i, r in enumerate(rows)
        if r[0][:10] >= keep_from and (r[0][:10] >= intraday_from or i in keep_idx)
    ]


def run(dry_run: bool = False, force: bool = False) -> int:
    now = datetime.now(TPE).replace(microsecond=0)
    ts = now.isoformat()
    cfg = load_config()
    prev = load_json(DATA_DIR / "latest.json", {})
    rows = load_json(DATA_DIR / "history.json", {}).get("rows", [])

    missing: list[str] = []
    raw: dict[str, dict] = {}
    board_times: dict[str, str] = {}
    auto_disc: dict[str, dict] = {}
    for src in cfg.get("sources") or []:
        got = FETCHERS[src]() if src in FETCHERS else None
        if not got:
            missing.append(src)
            continue
        raw[src] = got["quotes"]
        board_times[src] = got["board_time"] or ts
        if got.get("bonus"):
            auto_disc[src] = got["bonus"]
    market = fetch_market()
    missing += [s for s in MARKET_SYMBOLS if s not in market]

    if not raw and not market:
        log.error("牌告與市場報價都沒抓到，不寫檔")
        return 1

    # 讓分只影響「算路徑」，存進歷史的是原始牌告。
    disc = merge_discount(auto_disc, cfg.get("discount") or {})
    adjusted = {
        src: {ccy: apply_discount(q, (disc.get(src) or {}).get(ccy)) for ccy, q in qs.items()}
        for src, qs in raw.items()
    }

    new_rows = append_history([list(r) for r in rows], ts, raw, market)
    derived = derive(adjusted, board_times, market, new_rows, ts, cfg.get("transfer_cost_bp"))

    # 提醒：同一天同一則只發一次。
    state = {k: d for k, d in (prev.get("alert_state") or {}).items() if d == ts[:10]}
    fresh = [h for h in check_alerts(cfg, adjusted, derived) if h["key"] not in state]
    for h in fresh:
        state[h["key"]] = ts[:10]

    latest = {
        "fetched_at": ts,
        "sources": {
            src: {"name": SOURCE_NAMES.get(src, src), "board_time": board_times[src], "quotes": qs}
            for src, qs in raw.items()
        },
        "discount": disc,
        "transfer_cost_bp": cfg.get("transfer_cost_bp"),
        "market": market,
        "missing": missing,
        "derived": derived,
        "alert_state": state,
    }

    for row in derived["quotes"]:
        log.info(
            "%s %s 現鈔 %s/%s 即期 %s/%s 偏離 %s bp",
            row["source"], row["ccy"], row["cash_buy"], row["cash_sell"],
            row["spot_buy"], row["spot_sell"], row["dev_bp"],
        )
    if derived["cycles"]:
        c = derived["cycles"][0]
        log.info("最佳迴圈 %+.1f bp：%s", c["bp"], " → ".join(["TWD"] + [s["to"] for s in c["steps"]]))
    if missing:
        log.warning("沒抓到：%s", ", ".join(missing))
        banks = [SOURCE_NAMES[m] for m in missing if m in SOURCE_NAMES]
        if os.environ.get("GITHUB_ACTIONS") and banks:
            # 這一輪照樣成功結束（其他來源還是會更新），但要在 Actions 頁面上看得到。
            print(f"::warning::{'、'.join(banks)}的牌告沒抓到，頁面會沿用上一次的牌告。連續出現請看 README 第十一節。")

    if dry_run:
        print(json.dumps(latest, ensure_ascii=False, indent=2))
        return 0

    # 牌告沒抓到時，頁面要繼續顯示上一次的牌告（並標示時間），不能被空的蓋掉。
    prev_sources = prev.get("sources") or {}
    for src in cfg.get("sources") or []:
        if src not in raw and src in prev_sources:
            latest["sources"][src] = {**prev_sources[src], "stale": True}
    # 依設定檔的順序排好：頁面照這個順序排卡片，不能因為哪一家這輪沒抓到就換位置。
    latest["sources"] = {s: latest["sources"][s] for s in cfg.get("sources") or [] if s in latest["sources"]}

    # 只比這一輪真的抓到的來源：沒抓到的不算「有變」，否則每 15 分鐘都會寫一次。
    changed = any((prev_sources.get(s) or {}).get("quotes") != q for s, q in raw.items())
    age = _minutes_apart(prev.get("fetched_at"), ts)
    if not (force or fresh or changed or age is None or age >= HEARTBEAT_MIN):
        log.info("牌告沒變、距上次寫檔 %.0f 分鐘，這次不寫", age)
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    # 一列一行：歷史檔每次只多幾行，git diff 才看得懂。
    body = ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in new_rows)
    (DATA_DIR / "history.json").write_text('{"rows":[\n' + body + "\n]}\n", encoding="utf-8")
    log.info("已寫入 %s（歷史 %d 列）", DATA_DIR, len(new_rows))

    if fresh:
        emit_alert(now, [h["text"] for h in fresh])
    return 0


def emit_alert(now: datetime, lines: list[str], title: str = "外匯提醒") -> str:
    """
    組出 LINE 通知的文字，交給 workflow 的「LINE 提醒」步驟（寫進 GITHUB_OUTPUT 的
    alert_text）。不在 Actions 裡跑時只印出來。回傳組好的文字。
    """
    text = f"{title} {now.strftime('%m/%d %H:%M')}\n" + "\n".join("・" + t for t in lines)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("alert_text<<__FX__\n" + text + "\n__FX__\n")
    else:
        print(text)
    return text


def send_test_alert() -> int:
    """
    只送一則測試通知，走的是和真正提醒完全一樣的路徑（GITHUB_OUTPUT → LINE 步驟）。
    不抓牌告、不寫檔，所以 workflow 後面的 commit 步驟會因為沒有變動而直接結束。
    換了 LINE 權杖、或想確認通知還收得到時用。
    """
    now = datetime.now(TPE).replace(microsecond=0)
    emit_alert(now, [
        "這是測試訊息，不是套利機會。",
        "之後玉山、台新之間扣掉跨行匯款成本後出現正報酬，就會用這個格式通知你。",
    ], title="【測試】外匯提醒")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="外匯牌告追蹤")
    ap.add_argument("--dry-run", action="store_true", help="只抓、只印，不寫檔")
    ap.add_argument("--force", action="store_true", help="牌告沒變也寫檔")
    ap.add_argument("--test-alert", action="store_true", help="只送一則 LINE 測試通知，不抓牌告、不寫檔")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.test_alert:
        return send_test_alert()
    return run(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
