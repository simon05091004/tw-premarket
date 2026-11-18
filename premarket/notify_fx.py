"""
外匯 workflow 的 Telegram 出錯通知。

外匯每 15 分鐘跑一次。每輪出錯都推的話，壞掉一整天會收到快 30 則，第三則之後
就沒人會看了。所以只在「狀態改變」時推：

  ⚠️ 剛壞掉   —— 這段故障的第一輪（之後連續失敗都不再推）
  ✅ 恢復了   —— 故障後第一次正常

會推的狀況：
  - 整輪失敗：測試沒過、環境裝不起來、牌告與國際報價全都沒抓到、推回 repo 失敗、
    超過 timeout。這些時候頁面沒有更新。
  - 某一家銀行連續 MISSING_THRESHOLD 輪沒抓到。這種時候 run 是綠的、只有一條
    黃色警告，最容易沒人發現 —— 跨行套利掃描少了一家，頁面卻看起來一切正常。
    只差一輪不推：台新的連線偶爾會被重設，下一輪通常就好了。
  - 外匯提醒沒送到 LINE（權杖失效或額度用完）：把原本要送的內容一起轉過來。
    這種不用去重：提醒本身一天同一則只會發一次。

「這段故障通知過了沒」記在 STATE_FILE，由 workflow 用 actions/cache 在每輪之間
傳遞。不 commit 進 repo：推回 repo 失敗正是要通知的狀況之一。快取取不到時從
空的狀態開始 —— 最壞是同一段故障多推一次，不會漏推。

和 notify_telegram 一樣只用標準庫：環境裝不起來那輪也要送得出去。

用法（在 workflow 裡；輸入都從環境變數來，見 fx.yml）:
  python3 -m premarket.notify_fx
  python3 -m premarket.notify_fx --dry-run   # 只印訊息與新狀態，不送、不寫檔
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from premarket.notify_telegram import describe, emit_error, emit_warning, esc, send

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".fx_notify_state.json"
TPE = timezone(timedelta(hours=8))

# 同一家銀行連續幾輪沒抓到才通知。排程 15 分鐘一輪，2 輪 ≈ 半小時。
MISSING_THRESHOLD = 2

EMPTY_STATE: dict = {"down_since": None, "missing": {}}


# --------------------------------------------------------------------------
# 這一輪發生了什麼
# --------------------------------------------------------------------------
def hard_failure(job_status: str, tests: str, fx: str, push: str) -> str | None:
    """整輪失敗的原因（頁面沒有更新）。沒有就回 None。

    每種失敗的下一步不一樣，訊息要講對是哪一種 ——「程式掛了」和「超時被砍」
    在 Actions 上一樣紅，但前者要改程式、後者要調 timeout。
    LINE 那步失敗不算：牌告照樣更新了，那種另外處理。"""
    if job_status == "cancelled" or "cancelled" in (tests, fx, push):
        return "執行被中止（超過 5 分鐘的 timeout，或被手動取消），頁面停在上一次的牌告。"
    if tests == "failure":
        return "測試沒過，這輪刻意不抓牌告（壞掉的程式不該寫出資料）。"
    if job_status == "failure" and tests in ("", "skipped"):
        return "準備執行環境失敗（setup-python 或 pip install），這輪沒有抓牌告。"
    if fx == "failure":
        return (
            "抓牌告的程式失敗：銀行牌告與國際報價全都沒抓到，或程式本身出錯。"
            "頁面停在上一次的牌告，原因看執行紀錄最上方那行紅字。"
        )
    if push == "failure":
        return "牌告抓到了，但推回 repo 失敗（重試三次都沒成功），頁面沒有更新。"
    return None


def parse_missing(raw: str) -> dict[str, str] | None:
    """fx.py 寫的 missing_sources：{"esun": "玉山銀行"}。

    空字串代表「這輪沒去抓」（測試通知、或還沒抓就失敗了），回 None ——
    和 {}（全部抓到）不一樣，不能拿來把連續沒抓到的次數歸零。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    return {str(k): str(v) for k, v in d.items()}


# --------------------------------------------------------------------------
# 狀態 → 要推的訊息
# --------------------------------------------------------------------------
def _clock(stamp: str, now: datetime) -> str:
    """'2026-11-18 10:07' → 同一天只寫 10:07，跨天寫 11/17 15:52。"""
    try:
        t = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(stamp)
    if t.date() == now.date():
        return t.strftime("%H:%M")
    return f"{t.month:02d}/{t.day:02d} {t:%H:%M}"


def decide(
    state: dict,
    now: datetime,
    *,
    hard: str | None,
    missing: dict[str, str] | None,
    line_failed: bool = False,
    alert_text: str = "",
    test: bool = False,
) -> tuple[list[str], bool, dict]:
    """回 (訊息段落, 其中有沒有「出錯」的段落, 新狀態)。不碰網路、不碰檔案。"""
    stamp = now.strftime("%Y-%m-%d %H:%M")
    new = {
        "down_since": state.get("down_since"),
        "missing": {k: dict(v) for k, v in (state.get("missing") or {}).items()},
    }
    parts: list[str] = []
    bad = False

    if hard:
        bad = True
        if not new["down_since"]:
            parts.append(
                "⚠️ <b>外匯牌告停止更新</b>\n"
                f"{esc(hard)}\n\n"
                "這段故障期間不會重複通知，恢復時會再通知一次。"
            )
            new["down_since"] = stamp
        # 整輪失敗時不知道哪家銀行抓不抓得到，連續沒抓到的次數維持原樣。
    elif missing is not None:
        # missing 是 None 代表這輪沒去抓（測試通知）：不能拿來宣告恢復，
        # 也不能把連續沒抓到的次數歸零。
        if new["down_since"]:
            parts.append(
                "✅ <b>外匯牌告恢復更新</b>\n"
                f"從 {esc(_clock(new['down_since'], now))} 起中斷，這輪已正常更新。"
            )
            new["down_since"] = None

        kept: dict[str, dict] = {}
        for src, name in missing.items():
            prev = new["missing"].get(src) or {"since": stamp, "count": 0, "alerted": False}
            cur = {
                "name": name,
                "since": prev.get("since") or stamp,
                "count": int(prev.get("count") or 0) + 1,
                "alerted": bool(prev.get("alerted")),
            }
            if not cur["alerted"] and cur["count"] >= MISSING_THRESHOLD:
                bad = True
                parts.append(
                    f"⚠️ <b>{esc(name)}的牌告連續 {cur['count']} 輪沒抓到</b>\n"
                    f"從 {esc(_clock(cur['since'], now))} 起，頁面一直沿用舊牌告，"
                    "跨行套利掃描少了這一家。可能是銀行網頁改版，或擋下了 GitHub 的連線，"
                    "處理方式見 README 第十一節。\n\n"
                    "恢復時會再通知一次。"
                )
                cur["alerted"] = True
            kept[src] = cur
        for src, prev in new["missing"].items():
            if src not in missing and prev.get("alerted"):
                parts.append(
                    f"✅ <b>{esc(prev.get('name') or src)}的牌告恢復了</b>\n"
                    f"從 {esc(_clock(prev.get('since'), now))} 起沒抓到，這輪已經抓到。"
                )
        new["missing"] = kept

    if line_failed:
        bad = True
        body = alert_text.strip()
        parts.append(
            "⚠️ <b>外匯提醒沒送到 LINE</b>\n"
            "LINE 權杖失效，或本月的免費額度用完了。牌告本身有更新。"
            + (f"\n\n原本要送的內容：\n{esc(body)}" if body else "")
        )

    if test:
        parts.append(
            "🔔 <b>【測試】外匯出錯通知</b>\n"
            "收到這則代表設定正確。之後外匯排程出錯（頁面停止更新、某家銀行連續沒抓到、"
            "LINE 提醒沒送出）都會推到這裡。"
        )

    return parts, bad, new


# --------------------------------------------------------------------------
# 狀態檔
# --------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(EMPTY_STATE)
    if not isinstance(d, dict):
        return dict(EMPTY_STATE)
    return {"down_since": d.get("down_since"), "missing": d.get("missing") or {}}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _set_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description="外匯 workflow 的 Telegram 出錯通知")
    ap.add_argument("--dry-run", action="store_true", help="只印訊息與新狀態，不送出、不寫檔")
    args = ap.parse_args(argv)
    env = os.environ.get

    token = (env("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env("TELEGRAM_CHAT_ID") or "").strip()
    if not args.dry_run and not (token and chat_id):
        print("沒有 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID，略過 Telegram 出錯通知。", flush=True)
        return 0

    now = now or datetime.now(TPE)
    path = Path(env("FX_NOTIFY_STATE") or STATE_FILE)
    state = load_state(path)
    parts, bad, new = decide(
        state,
        now,
        hard=hard_failure(env("JOB_STATUS", ""), env("TESTS", ""), env("FX", ""), env("PUSH", "")),
        missing=parse_missing(env("MISSING", "")),
        line_failed=env("LINE", "") == "failure",
        alert_text=env("ALERT_TEXT", ""),
        test=env("TEST_ALERT", "") == "true",
    )

    if args.dry_run:
        print("\n\n".join(parts) if parts else "（這輪不用通知）")
        print("--- 新狀態 ---")
        print(json.dumps(new, ensure_ascii=False, indent=1))
        return 0

    if parts:
        text = f"💱 <b>外匯牌告追蹤</b> {now:%m/%d %H:%M}\n\n" + "\n\n".join(parts)
        run_url = (env("RUN_URL") or "").strip()
        pages = (env("PAGES_URL") or "").strip().rstrip("/")
        # 出錯就連到執行紀錄；只有「恢復了」的話，連到頁面看牌告是不是新的。
        if bad or not pages:
            button = ("看執行紀錄", run_url)
        else:
            button = ("看外匯頁面", f"{pages}/fx.html")
        code, body = send(text, token=token, chat_id=chat_id, button=button)
        if not 200 <= code < 300:
            # 沒送出去就不更新狀態：下一輪會再試一次，不能讓「已通知」記成真的。
            desc = describe(body)
            if code in (400, 401, 403, 404):
                emit_error(
                    f"Telegram 出錯通知送不出去（HTTP {code}）：{desc}。"
                    "請檢查 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID。"
                )
                return 1
            emit_warning(f"Telegram 出錯通知送不出去（HTTP {code}）：{desc}。下一輪會再試。")
            return 0
        print(f"Telegram 出錯通知已送出（{len(parts)} 段）", flush=True)

    if new != state:
        save_state(path, new)
        _set_output("state_changed", "true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
