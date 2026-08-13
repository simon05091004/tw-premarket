"""
隔日當沖放空清單的回測。

問的問題只有一個：**今天命中條件的標的，隔天當沖放空的報酬分布長什麼樣？**

進出場假設（刻意寫得保守而明確）：
    進場  隔日開盤放空
    出場  隔日收盤回補
    報酬  (開盤 - 收盤) / 開盤 —— 股價跌則為正

同時記錄兩個風險數字，因為平均報酬會騙人：
    隔日跳空  (隔日開盤 - 今日收盤) / 今日收盤，跳空開低會吃掉大半利潤
    盤中逆行  (隔日最高 - 隔日開盤) / 隔日開盤，這是放空當下要扛的最大帳面虧損

判定邏輯直接呼叫 shortlist.evaluate_candidate —— 與正式產出的清單同一份程式碼。

用法:
    python -m premarket.backtest --days 60
資料會快取在 data/cache/，重跑不必重抓。
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import datetime
from pathlib import Path

from . import fetch, shortlist

log = logging.getLogger("backtest")

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "backtest"
# 手續費 0.1425% x 折數 + 當沖證交稅 0.15%，來回粗估。
# 不含滑價 —— 開盤放空實際成交價往往比開盤價差，所以結果仍偏樂觀。
COST_PCT = 0.25


def _cached(name: str, fetch_fn):
    """磁碟快取。回測要反覆調門檻重跑，每次重抓全市場行情不切實際。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    data = fetch_fn()
    if data:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def load_history(dates: list[str]) -> tuple[dict, dict]:
    """逐日取得個股行情與外資買賣超，兩者都走快取。"""
    quotes, foreign = {}, {}
    for i, ds in enumerate(dates, 1):
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        q = _cached(f"quotes-{ds}", lambda: shortlist.fetch_daily_quotes(d))
        f = _cached(f"foreign-{ds}", lambda: shortlist.fetch_foreign_net(d))
        if q:
            quotes[ds] = q
        if f:
            foreign[ds] = f
        if i % 10 == 0:
            log.info("已載入 %d/%d 天", i, len(dates))
    return quotes, foreign


def run(dates: list[str]) -> dict:
    quotes, foreign = load_history(dates)
    ordered = sorted(quotes)
    if len(ordered) < shortlist.QUOTE_DAYS + 2:
        raise SystemExit("交易日不足，無法回測")

    trades: list[dict] = []
    # 從第 QUOTE_DAYS 天開始（前面要當歷史），最後一天不做（沒有隔日資料）
    for i in range(shortlist.QUOTE_DAYS, len(ordered) - 1):
        today_ds, next_ds = ordered[i], ordered[i + 1]
        today, nxt = quotes[today_ds], quotes[next_ds]
        hist_dates = ordered[i - shortlist.QUOTE_DAYS : i]
        foreign_dates = ordered[max(0, i - shortlist.FOREIGN_DAYS + 1) : i + 1]

        for code, bar in today.items():
            hist = [quotes[ds][code] for ds in hist_dates if code in quotes[ds]]
            if len(hist) < 5:
                continue
            sell_days = shortlist.count_sell_days(code, foreign_dates, foreign)
            cand = shortlist.evaluate_candidate(code, bar, hist, sell_days)
            if cand is None:
                continue
            nb = nxt.get(code)
            if not nb or not nb.get("open") or not nb.get("close"):
                continue

            open_, close_, high = nb["open"], nb["close"], nb.get("high") or nb["open"]
            gross = (open_ - close_) / open_ * 100
            trades.append(
                {
                    "date": today_ds,
                    "code": code,
                    "name": cand["name"],
                    "命中條件": cand["命中條件"],
                    "命中數": cand["命中數"],
                    "隔日跳空_pct": round((open_ - bar["close"]) / bar["close"] * 100, 2),
                    "毛報酬_pct": round(gross, 2),
                    "淨報酬_pct": round(gross - COST_PCT, 2),
                    "盤中逆行_pct": round((high - open_) / open_ * 100, 2),
                }
            )
    return {"trades": trades, "期間": f"{ordered[0]} ~ {ordered[-1]}", "交易日數": len(ordered)}


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {}
    net = [r["淨報酬_pct"] for r in rows]
    return {
        "樣本數": len(rows),
        "平均淨報酬_pct": round(statistics.mean(net), 3),
        "中位數_pct": round(statistics.median(net), 3),
        "勝率_pct": round(sum(1 for x in net if x > 0) / len(net) * 100, 1),
        "標準差": round(statistics.pstdev(net), 2) if len(net) > 1 else 0.0,
        "最好_pct": round(max(net), 2),
        "最差_pct": round(min(net), 2),
        "平均跳空_pct": round(statistics.mean(r["隔日跳空_pct"] for r in rows), 2),
        "平均盤中逆行_pct": round(statistics.mean(r["盤中逆行_pct"] for r in rows), 2),
    }


def report(result: dict) -> str:
    trades = result["trades"]
    lines = [
        f"# 隔日當沖放空回測  {result['期間']}（{result['交易日數']} 個交易日）",
        "",
        f"進場：隔日開盤放空　出場：隔日收盤回補　成本假設：來回 {COST_PCT}%（不含滑價）",
        "",
        "## 整體",
        json.dumps(_stats(trades), ensure_ascii=False, indent=2),
        "",
        "## 依命中條件數",
    ]
    for n in sorted({t["命中數"] for t in trades}):
        rows = [t for t in trades if t["命中數"] == n]
        lines.append(f"### 命中 {n} 項  {json.dumps(_stats(rows), ensure_ascii=False)}")
    lines.append("")
    lines.append("## 依單一條件（可重複計入）")
    for cond in ("量價背離", "籌碼轉弱", "過熱回落"):
        rows = [t for t in trades if cond in t["命中條件"]]
        lines.append(f"### {cond}  {json.dumps(_stats(rows), ensure_ascii=False)}")
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - CLI
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="回測涵蓋的交易日數")
    ap.add_argument("--end", help="結束日期 YYYY-MM-DD（預設今天）")
    args = ap.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else datetime.now().date()
    bars = fetch.fetch_taiex_ohlc(end, lookback=args.days + shortlist.QUOTE_DAYS + 5)
    if not bars:
        raise SystemExit("取不到加權指數 K 棒，無法決定交易日")
    dates = [b["date"] for b in bars]
    log.info("回測日期範圍 %s ~ %s（%d 天）", dates[0], dates[-1], len(dates))

    result = run(dates)
    text = report(result)
    print("\n" + text)
    out = Path("data") / "backtest-report.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    log.info("已寫出 %s（%d 筆交易）", out, len(result["trades"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
