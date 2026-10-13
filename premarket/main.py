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
import os
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


# 盤後排程只在 21:23／22:17 觸發，所以「21 點前執行的盤後」一律是被延遲的那一次，
# 處理的是前一天的場次。原本這個值是 5（凌晨 5 點），當時實測延遲 63~143 分鐘;
# 2026-10 實測已經變成 5~9 小時，#88 在 05:45、#89 在 06:07 執行，雙雙跨過 5 點那條線,
# 於是跑去抓一個還沒開盤的交易日，九個資料源全空而中止。
#
# 用 21 還有一個好處：盤後的關鍵資料融資餘額約 21:00 才公布，所以 21 點前執行
# 無論如何都不該標成當天 —— 這條線同時擋掉延遲與手動誤觸兩種情況。
POSTMARKET_ROLLOVER_HOUR = 21


def _target_date(now: datetime, session: str) -> date:
    """
    盤後場次的目標日期。

    GitHub Actions 的排程延遲時數不定，21:23 的盤後排程經常被推過午夜甚至到清晨。
    若直接用執行當下的日期，報告會標成隔天,而隔天的收盤資料當時還不存在 ——
    產出一份幾乎全空的報告。往回算一天,才是那次排程原本要處理的交易日。
    """
    if session == "postmarket" and now.hour < POSTMARKET_ROLLOVER_HOUR:
        shifted = now.date() - timedelta(days=1)
        log.info("%02d:%02d 執行盤後（早於場次時間 %02d:00），目標日期回推為 %s",
                 now.hour, now.minute, POSTMARKET_ROLLOVER_HOUR, shifted)
        return shifted
    return now.date()


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


def prev_trade_date_from_history(today: date) -> date | None:
    """
    後備：從已產出的 payload 檔名反推前一交易日。

    連續問不到加權指數，比起「這幾天全都休市」，更可能是證交所端點故障 ——
    2026-08-11 盤前就是這樣：12 次請求全回 HTTP 200 加一頁 HTML，整份報告中止。
    docs/data/ 下每有一個 payload 檔，就代表那天跑成過一份報告,也就是交易日;
    取最近一個當前一交易日，比直接放棄好。

    這只影響「往回比較」的基準日,不會讓報告多出數字：真的抓不到的資料源
    仍會各自進 missing,超過半數失敗照樣中止。
    """
    dates = []
    for f in DATA.glob("*payload-*.json"):
        try:
            d = date.fromisoformat(f.stem.split("-", 1)[1])
        except ValueError:
            continue
        if d < today:
            dates.append(d)
    return max(dates) if dates else None


def is_trading_day(today: date) -> bool:
    """今天是否可能開盤。只排除週末 —— 國定假日由 market_traded_on() 事後確認。"""
    return today.weekday() < 5


def market_traded_on(day: date) -> bool | None:
    """
    那天到底有沒有開盤 —— 問證交所有沒有那天的指數，不自己維護行事曆。
    回 True 有開盤、False 確定休市、None 問不出來。

    只給盤後用。盤後的目標日期是「已經收盤的那天」,所以沒有資料就等於沒開盤;
    盤前的目標日期是「今天、開盤前」,本來就查不到，不能用這個判斷。

    2026-10-09 國慶補假休市，但 is_trading_day() 只濾週末，於是整份照跑、
    九個資料源全空、撞上「超過半數失敗」而以失敗收場（#86／#87）。
    休市不是故障,該安靜結束,不該每逢假日就寄兩封失敗通知。

    三態是必要的：fetch_taiex_ohlc 回 None 時，可能是休市，也可能是證交所正在
    鬧脾氣（回 HTTP 200 加一頁 HTML，重試三次都拿不到）。兩者混為一談的話,
    端點一掛就會把真正的交易日當成假日安靜跳過 —— 那比失敗更糟,
    因為備援那次會做出同樣的誤判,而且沒有任何人會收到通知。
    問不出來就回 None,讓主流程照常往下跑,真的沒資料會被既有的
    「超過半數資料源失敗」攔下來，那條路徑至少是響亮的。
    """
    rows = fetch.fetch_taiex_ohlc(day, lookback=1)
    if not rows:
        return None  # 端點給不出答案，不要假裝知道
    # lookback=1 回的是「≤ day 的最後一根 K 棒」：等於 day 就是有開盤，
    # 早於 day 就表示證交所答得出來、但那天沒有交易。
    return rows[-1].get("date") == day.isoformat()


def emit_error(msg: str) -> None:
    """
    把致命錯誤同時送進 GitHub 的 annotation。

    log.error 只寫到日誌，而日誌要登入才看得到；run 頁面上只會留下一句
    「Process completed with exit code 1」。2026-10 那次金鑰過期,訊息其實
    從第一天就印得清清楚楚（「API 金鑰無效或已過期…請更新 ANTHROPIC_API_KEY」），
    卻因為沒人會去翻日誌，連續失敗了九個交易日才被發現。
    ::error:: 會直接顯示在 run 頁面最上方,而且不必登入就看得到。
    """
    log.error("%s", msg)
    if os.getenv("GITHUB_ACTIONS") == "true":
        # annotation 不能跨行，換行會被截斷
        print(f"::error::{' '.join(msg.split())}", flush=True)


def report_exists(today: date, spec: SessionSpec) -> bool:
    """那天的報告是不是已經產出過了。"""
    return (DOCS / f"{spec.out_stem}-{today.isoformat()}.md").exists()


def signal_skipped() -> None:
    """
    告訴 workflow「這次沒產出新報告」。

    LINE 通知那步的條件是 success(),光是提早 return 0 擋不住它 ——
    備援那次會再廣播一則一模一樣的通知給所有訂閱者。
    """
    out = os.getenv("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write("skipped=true\n")


def load_prev_payload(prefix: str = "payload") -> dict | None:
    """前一份「同類型」報告的 payload —— 盤前盤後各自一組,不互相汙染。"""
    files = sorted(DATA.glob(f"{prefix}-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def load_history(today: date, prefix: str = "payload", days: int = 60) -> list[dict]:
    """
    過去已產出的 payload，供計算「基準」用。

    ADR 溢價、外資期貨部位水位、槓桿變化這三項的共同問題是：單看今天的數字
    答不出「這算大嗎」。溢價 9.89% 是高是低，要跟它自己的常態比才知道。
    這裡把歷史讀進來,壓成最小欄位後交給 fetch._derive。

    讀壞的檔案跳過即可 —— 基準少一天不影響結論，讓整份報告掛掉才是問題。
    """
    out: list[dict] = []
    for f in sorted(DATA.glob(f"{prefix}-*.json")):
        try:
            d = date.fromisoformat(f.stem.split("-", 1)[1])
        except ValueError:
            continue
        if d >= today:
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            log.warning("歷史 payload 讀取失敗，略過: %s", f.name)
    return fetch.history_from_payloads(out[-days:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不呼叫 API，只輸出 JSON")
    ap.add_argument("--date", help="覆寫目標日期 YYYY-MM-DD（回測／補跑用）")
    ap.add_argument(
        "--force",
        action="store_true",
        help="報告已存在也重新產生（會再花一次 API 費用）",
    )
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
        else _target_date(datetime.now(TPE), args.session)
    )

    if not is_trading_day(today):
        log.info("%s 非交易日（週末），結束。", today)
        return 0

    # 國定假日：盤後才查得出來（盤前的目標日期還沒開盤，查不到是正常的）。
    # 放在抓資料之前,假日當天可以省下整整兩分鐘的徒勞抓取。
    # 只有「確定休市」才跳過；問不出來（None）一律照常往下跑。
    if args.session == "postmarket" and market_traded_on(today) is False:
        log.info("%s 沒有開盤（國定假日），結束。", today)
        return 0

    # 備援排程用的守衛。GitHub 偶爾會把 schedule 事件整個丟掉 —— 2026-08-27 盤前
    # 就是這樣,連一筆 run 記錄都沒有,那天等於開天窗。對策是同一天排兩次,
    # 由這道守衛讓第二次在報告已存在時直接結束。
    #
    # 位置刻意放在抓資料與 API 之前：正常日子的備援那次不花 API 費用、
    # 也不會多打證交所一輪。要重新產生同一天的報告請加 --force。
    if not args.force and not args.dry_run and report_exists(today, spec):
        log.info("%s 的報告已存在，略過（備援排程不重複產出；要重跑請加 --force）。", today)
        signal_skipped()
        return 0

    prev = find_prev_trade_date(today)
    if prev is None:
        prev = prev_trade_date_from_history(today)
        if prev is None:
            emit_error("問不到加權指數，也沒有歷史 payload 可回推前一交易日，中止。")
            return 1
        log.warning(
            "連續多天問不到加權指數（證交所端點可能故障），"
            "改用最近一份 payload 的日期 %s 當前一交易日。",
            prev,
        )

    log.info("抓取資料中…")
    if args.session == "postmarket":
        try:
            from . import fetch_post
        except ImportError as exc:  # noqa: BLE001
            emit_error(f"盤後資料層缺失（premarket/fetch_post.py）: {exc}")
            return 1
        # 盤後看的是今天收盤後的結果,所以 session_date 是今天、prev 供計算變化量
        payload = fetch_post.build_postmarket_payload(today, prev)
    else:
        history = load_history(today, spec.payload_prefix)
        log.info("歷史基準樣本: %d 日", len(history))
        payload = fetch.build_payload(prev, today, history=history)
    if payload.missing:
        log.warning("缺少資料源: %s", "、".join(payload.missing))
    if len(payload.missing) >= 5:
        emit_error(
            "超過半數資料源失敗，中止以免產生誤導性報告。缺少: "
            + "、".join(payload.missing)
        )
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
        emit_error(f"報告不完整，不寫出檔案: {exc}")
        return 1
    except analyze.BriefUnavailable as exc:
        # 金鑰過期／限流／過載。訊息已經是人話，不需要 traceback。
        emit_error(str(exc))
        return 1

    # 檢查放在寫檔之前：截斷的報告一旦寫出去就會被 workflow commit 並發布，
    # 而且 exit code 0 會讓 Actions 顯示綠勾,沒有人會發現。
    if len(brief_md) < analyze.MIN_BRIEF_CHARS:
        emit_error(
            f"報告只有 {len(brief_md)} 字元（低於 {analyze.MIN_BRIEF_CHARS}），"
            f"視為產出失敗，不寫出檔案。內容: {brief_md[:120]!r}"
        )
        return 1

    out_md = DOCS / f"{spec.out_stem}-{today.isoformat()}.md"
    out_md.write_text(
        f"# {spec.title} {today.isoformat()}\n\n{brief_md}\n", encoding="utf-8"
    )

    html_doc = render.render(payload_dict, brief_md, session=args.session)
    (DOCS / f"{spec.out_stem}-{today.isoformat()}.html").write_text(html_doc, encoding="utf-8")
    (DOCS / spec.latest_html).write_text(html_doc, encoding="utf-8")  # 最新一份

    log.info("完成: %s", out_md.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
