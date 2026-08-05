"""
盤前分析主程式。

執行流程:
  1. 判斷今天是否為交易日（用證交所有無「前一交易日」資料反推，不維護行事曆）
  2. 抓資料 -> 存 JSON
  3. 呼叫 Anthropic API 產生分析
  4. 輸出 Markdown + HTML 到 docs/，供 GitHub Pages 服務

用法:
  python -m premarket.main            # 正常執行
  python -m premarket.main --dry-run  # 只抓資料，不呼叫 API（省錢，除錯用）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import analyze, fetch, render

TPE = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("premarket")


def find_prev_trade_date(today: date, max_back: int = 10) -> date | None:
    """
    往回找最近一個有加權指數資料的日期。
    這同時處理了週末、國定假日與颱風假 —— 不需要自己維護行事曆。
    """
    for i in range(1, max_back + 1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if fetch.fetch_taiex_ohlc(d, lookback=1):
            log.info("前一交易日: %s", d)
            return d
    return None


def is_trading_day(today: date) -> bool:
    """今天是否可能開盤。週末直接排除；國定假日交給 workflow 的容錯（多跑一次不會壞）。"""
    return today.weekday() < 5


def load_prev_payload() -> dict | None:
    files = sorted(DATA.glob("payload-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不呼叫 API，只輸出 JSON")
    ap.add_argument("--date", help="覆寫目標日期 YYYY-MM-DD（回測／補跑用）")
    args = ap.parse_args()

    today = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else datetime.now(TPE).date()
    )

    if not is_trading_day(today):
        log.info("%s 非交易日，結束。", today)
        return 0

    prev = find_prev_trade_date(today)
    if prev is None:
        log.error("找不到前一交易日資料，中止。")
        return 1

    log.info("抓取資料中…")
    payload = fetch.build_payload(prev, today)
    if payload.missing:
        log.warning("缺少資料源: %s", "、".join(payload.missing))
    if len(payload.missing) >= 5:
        log.error("超過半數資料源失敗，中止以免產生誤導性報告。")
        return 1

    DATA.mkdir(parents=True, exist_ok=True)
    prev_payload = load_prev_payload()
    payload_dict = payload.to_dict()
    (DATA / f"payload-{today.isoformat()}.json").write_text(
        json.dumps(payload_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.dry_run:
        log.info("dry-run 完成，JSON 已寫出。")
        print(json.dumps(payload_dict.get("derived", {}), ensure_ascii=False, indent=2))
        return 0

    log.info("呼叫 Anthropic API…")
    brief_md = analyze.generate_brief(payload_dict, prev_payload)

    out_md = DOCS / f"premarket-{today.isoformat()}.md"
    out_md.write_text(f"# 台股盤前分析 {today.isoformat()}\n\n{brief_md}\n", encoding="utf-8")

    html_doc = render.render(payload_dict, brief_md)
    (DOCS / f"premarket-{today.isoformat()}.html").write_text(html_doc, encoding="utf-8")
    (DOCS / "index.html").write_text(html_doc, encoding="utf-8")  # 最新一份

    log.info("完成: %s", out_md.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
