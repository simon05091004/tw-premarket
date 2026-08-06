"""
盤前／盤後分析主程式。

執行流程:
  1. 判斷今天是否為交易日（用證交所有無「前一交易日」資料反推，不維護行事曆）
  2. 抓資料 -> 存 JSON
  3. 呼叫 Anthropic API 產生分析
  4. 輸出 Markdown + HTML 到 docs/，供 GitHub Pages 服務

用法:
  python -m premarket.main                        # 盤前，正常執行
  python -m premarket.main --dry-run              # 只抓資料，不呼叫 API（省錢，除錯用）
  python -m premarket.main --session postmarket   # 盤後籌碼版
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SessionSpec:
    """兩種 session 的差異全部集中在這裡,主流程只有一條路。"""

    payload_prefix: str  # docs/data/<prefix>-YYYY-MM-DD.json
    out_stem: str        # docs/<stem>-YYYY-MM-DD.md / .html
    latest_html: str     # 固定網址的最新一份
    title: str


SPECS = {
    "premarket": SessionSpec("payload", "premarket", "index.html", "台股盤前分析"),
    # 盤後另存一組檔名:index.html 是盤前用的,蓋掉會讓首頁在盤後變成籌碼報告
    "postmarket": SessionSpec(
        "postpayload", "postmarket", "latest-postmarket.html", "台股盤後籌碼"
    ),
}


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


def load_prev_payload(prefix: str = "payload") -> dict | None:
    """前一份「同類型」報告的 payload —— 盤前盤後各自一組,不互相汙染。"""
    files = sorted(DATA.glob(f"{prefix}-*.json"))
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
    ap.add_argument(
        "--session",
        choices=sorted(SPECS),
        default="premarket",
        help="premarket=盤前分析（預設）／postmarket=盤後籌碼",
    )
    args = ap.parse_args()
    spec = SPECS[args.session]

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
    if args.session == "postmarket":
        try:
            from . import fetch_post
        except ImportError as exc:  # noqa: BLE001
            log.error("盤後資料層缺失（premarket/fetch_post.py）: %s", exc)
            return 1
        # 盤後看的是今天收盤後的結果,所以 session_date 是今天、prev 供計算變化量
        payload = fetch_post.build_postmarket_payload(today, prev)
    else:
        payload = fetch.build_payload(prev, today)
    if payload.missing:
        log.warning("缺少資料源: %s", "、".join(payload.missing))
    if len(payload.missing) >= 5:
        log.error("超過半數資料源失敗，中止以免產生誤導性報告。")
        return 1

    DATA.mkdir(parents=True, exist_ok=True)
    prev_payload = load_prev_payload(spec.payload_prefix)
    payload_dict = payload.to_dict()
    (DATA / f"{spec.payload_prefix}-{today.isoformat()}.json").write_text(
        json.dumps(payload_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.dry_run:
        log.info("dry-run 完成，JSON 已寫出。")
        print(json.dumps(payload_dict.get("derived", {}), ensure_ascii=False, indent=2))
        return 0

    log.info("呼叫 Anthropic API…")
    try:
        brief_md = analyze.generate_brief(payload_dict, prev_payload, session=args.session)
    except analyze.BriefTruncated as exc:
        log.error("報告不完整，不寫出檔案: %s", exc)
        return 1

    # 檢查放在寫檔之前：截斷的報告一旦寫出去就會被 workflow commit 並發布，
    # 而且 exit code 0 會讓 Actions 顯示綠勾,沒有人會發現。
    if len(brief_md) < analyze.MIN_BRIEF_CHARS:
        log.error(
            "報告只有 %d 字元（低於 %d），視為產出失敗，不寫出檔案。內容: %r",
            len(brief_md),
            analyze.MIN_BRIEF_CHARS,
            brief_md[:120],
        )
        return 1

    out_md = DOCS / f"{spec.out_stem}-{today.isoformat()}.md"
    out_md.write_text(
        f"# {spec.title} {today.isoformat()}\n\n{brief_md}\n", encoding="utf-8"
    )

    html_doc = render.render(payload_dict, brief_md)
    (DOCS / f"{spec.out_stem}-{today.isoformat()}.html").write_text(html_doc, encoding="utf-8")
    (DOCS / spec.latest_html).write_text(html_doc, encoding="utf-8")  # 最新一份

    log.info("完成: %s", out_md.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
