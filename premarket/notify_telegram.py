"""
Telegram 推播：把剛產出的報告摘要送到手機。

為什麼不是只送一行連結 —— LINE 那步送的是「已更新 + 網址」，手機通知欄看到
之後還要點進去、等網頁載入,才知道今天到底是偏多還是偏空。摘要直接寫在訊息裡,
鎖定畫面就看得完,連結留給想看全文的時候。

刻意只用標準庫（urllib），不依賴 requests：失敗警告那步要在 pip install 掛掉
之後仍然送得出去,那時 runner 上只有系統的 python。同理不 import premarket.main
（它會一路拉進 pandas／yfinance）。

用法:
  python -m premarket.notify_telegram --session premarket --date 2026-11-04
  python -m premarket.notify_telegram --session postmarket           # 留空=最新一份
  python -m premarket.notify_telegram --session premarket --failure \
      --reason "分析產出失敗" --run-url "https://github.com/x/y/actions/runs/1"
  python -m premarket.notify_telegram --session premarket --dry-run  # 只印不送

環境變數:
  TELEGRAM_BOT_TOKEN  BotFather 給的權杖；沒設就安靜略過（fork 的人不會被影響）
  TELEGRAM_CHAT_ID    自己跟 bot 的對話 id
  PAGES_URL           GitHub Pages 網址，用來組「看完整報告」的連結
  GITHUB_REPOSITORY   沒有 Pages 時的退路：連到 repo 裡的 .md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

API = "https://api.telegram.org"
HTTP_TIMEOUT = 20

# Telegram 單則訊息上限 4096 字元。下面兩個額度加起來遠低於上限,
# 真正的理由不是 API 而是手機：一則通知要一個畫面看得完。
SUMMARY_LIMIT = 140
DETAIL_LIMIT = 300
HARD_LIMIT = 3900

SUMMARY_KEY = "一句話總結"
WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class Session:
    """兩種報告的差異全部集中在這裡（和 main.py 的 SessionSpec 同樣的做法）。"""

    stem: str          # docs/<stem>-YYYY-MM-DD.md
    title: str
    detail_key: str    # 第二段要抓的章節：用關鍵字比對標題
    icon: str


SESSIONS = {
    # 章節標題是 prompts/*_system.md 規定的固定章節，但編號會隨章節數變動
    # （盤前 8 節、盤後 10 節），所以一律用關鍵字比對，不看編號。
    "premarket": Session("premarket", "台股盤前分析", "開盤推估", "📈"),
    "postmarket": Session("postmarket", "台股盤後籌碼", "明日觀察", "📊"),
}

_HEADING = re.compile(r"^##\s+(.*\S)\s*$")
_NUM_PREFIX = re.compile(r"^\d+\s*[.、)]\s*")
_SENTENCE_ENDS = "。！？!?"


# --------------------------------------------------------------------------
# Actions annotation：和 main.py 一樣,訊息要在 run 頁面上看得到,
# 不能只寫進要登入才翻得到的日誌。
# --------------------------------------------------------------------------
def _annotate(kind: str, msg: str) -> None:
    one_line = " ".join(msg.split())
    print(one_line, flush=True)
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{one_line}", flush=True)


def emit_error(msg: str) -> None:
    _annotate("error", msg)


def emit_warning(msg: str) -> None:
    _annotate("warning", msg)


# --------------------------------------------------------------------------
# 報告解析
# --------------------------------------------------------------------------
def split_sections(md: str) -> list[tuple[str, str]]:
    """把 Markdown 切成 (標題, 內文)。標題的編號前綴會拿掉。"""
    out: list[tuple[str, str]] = []
    heading: str | None = None
    buf: list[str] = []
    for raw in md.splitlines():
        m = _HEADING.match(raw)
        if m:
            if heading is not None:
                out.append((heading, "\n".join(buf).strip()))
            heading = _NUM_PREFIX.sub("", m.group(1)).strip()
            buf = []
        elif heading is not None:
            buf.append(raw)
    if heading is not None:
        out.append((heading, "\n".join(buf).strip()))
    return out


def find_section(sections: list[tuple[str, str]], key: str) -> tuple[str, str] | None:
    """第一個標題含關鍵字的章節。比對標題而非內文 —— 「開盤推估」這四個字
    也會出現在第 2 節的內文裡,抓錯段落比抓不到更難發現。"""
    for heading, body in sections:
        if key in heading:
            return heading, body
    return None


def clean_body(body: str) -> list[str]:
    """內文整理成可讀的行：丟掉免責聲明與表格，去掉 Markdown 記號。"""
    lines: list[str] = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s.startswith(">"):          # 空行與免責聲明
            continue
        if s.startswith("|") or set(s) <= set("-|: "):  # 表格：手機上擠成一團
            continue
        s = s.replace("**", "").replace("`", "")
        lines.append(s)
    return lines


def trim(text: str, limit: int) -> str:
    """超過額度就截斷，但盡量切在句尾 —— 半句話比短一點更難讀。"""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = -1
    for i, ch in enumerate(text[:limit]):
        if ch in _SENTENCE_ENDS:
            cut = i
    if cut >= limit // 2:                        # 太靠前的句點不值得切
        return text[: cut + 1]
    return text[: limit - 1].rstrip() + "…"


def summarize(lines: list[str], limit: int) -> str:
    """把一節內文壓成一段訊息。條列保持條列,不要黏成一團。"""
    if not lines:
        return ""
    bullets = [ln for ln in lines if ln.startswith(("- ", "* "))]
    if bullets:
        return _pick_bullets(bullets, limit)
    return trim(" ".join(lines), limit)


def _pick_bullets(bullets: list[str], limit: int) -> str:
    """整條保留或整條不要 —— 砍半的條列看起來像資料掉了。"""
    kept: list[str] = []
    used = 0
    for b in bullets:
        text = trim(b[2:].strip(), limit)
        if kept and used + len(text) > limit:
            break
        kept.append(text)
        used += len(text)
    out = "\n".join("• " + t for t in kept)
    dropped = len(bullets) - len(kept)
    if dropped:
        out += f"\n…另 {dropped} 項見完整報告"
    return out


def esc(s: str) -> str:
    """Telegram parse_mode=HTML 只認這三個實體。"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def when_label(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.month:02d}/{d.day:02d}（{WEEKDAYS[d.weekday()]}）"


# --------------------------------------------------------------------------
# 訊息組裝
# --------------------------------------------------------------------------
def build_report_message(sess: Session, iso: str, md: str) -> tuple[str, list[str]]:
    """回 (訊息, 抓不到的章節)。抓不到不是致命錯誤,但要讓它出聲。"""
    sections = split_sections(md)
    missing: list[str] = []
    parts = [f"{sess.icon} <b>{esc(sess.title)} {esc(when_label(iso))}</b>"]

    for key, limit in ((SUMMARY_KEY, SUMMARY_LIMIT), (sess.detail_key, DETAIL_LIMIT)):
        found = find_section(sections, key)
        if not found:
            missing.append(key)
            continue
        heading, body = found
        text = summarize(clean_body(body), limit)
        if not text:
            missing.append(key)
            continue
        parts.append(f"<b>{esc(heading)}</b>\n{esc(text)}")

    if len(parts) == 1:
        # 章節名稱被改掉的話會走到這裡。訊息照送（至少連結是對的），
        # 但 run 頁面要留一條 warning,否則下次報告格式變動沒有人會知道。
        parts.append("（摘要段落抓不到，請直接看完整報告）")
    return "\n\n".join(parts)[:HARD_LIMIT], missing


def build_failure_message(sess: Session, reason: str, iso: str = "") -> str:
    head = f"⚠️ <b>{esc(sess.title)} 沒有發布</b>"
    if iso:
        head = f"⚠️ <b>{esc(sess.title)} {esc(when_label(iso))} 沒有發布</b>"
    # 細節一律由 --reason 帶進來：產出失敗、測試沒過、push 失敗、被中止，
    # 四種情況的下一步不一樣，寫死一句「報告沒有產出」會有時候是錯的。
    body = reason.strip() or "執行失敗，原因不明。"
    return f"{head}\n\n{esc(body)}\n\n按下面的按鈕看執行紀錄。"


def report_url(sess: Session, iso: str) -> str:
    """「看完整報告」按鈕的連結。Pages 優先,沒有就退回 repo 裡的 .md。"""
    base = (os.getenv("PAGES_URL") or "").strip().rstrip("/")
    if base:
        return f"{base}/{sess.stem}-{iso}.html"
    repo = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    if repo:
        return f"https://github.com/{repo}/blob/main/docs/{sess.stem}-{iso}.md"
    return ""


def latest_report(sess: Session, docs: Path | None = None) -> tuple[str, Path] | None:
    """最新一份報告。手動觸發時（gen 因報告已存在而略過）用這個重送。

    docs 不寫成預設參數 DOCS：預設值在 import 時就綁定，測試改了模組的 DOCS
    也換不掉，等於那條路徑永遠跑在真實的 docs/ 上。"""
    root = docs if docs is not None else DOCS
    best: tuple[str, Path] | None = None
    for f in root.glob(f"{sess.stem}-*.md"):
        iso = f.stem[len(sess.stem) + 1:]
        try:
            date.fromisoformat(iso)
        except ValueError:
            continue
        if best is None or iso > best[0]:
            best = (iso, f)
    return best


# --------------------------------------------------------------------------
# 送出
# --------------------------------------------------------------------------
def _http_post(url: str, payload: dict) -> tuple[int, str]:
    """回 (HTTP 狀態碼, 回應內容)。連不上就回 0。

    注意：url 內含權杖，任何情況都不要把它印出來。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001  連線問題、DNS、超時
        return 0, f"{type(exc).__name__}: {exc}"


def send(
    text: str,
    *,
    token: str,
    chat_id: str,
    button: tuple[str, str] | None = None,
) -> tuple[int, str]:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button and button[1]:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button[0], "url": button[1]}]]
        }
    return _http_post(f"{API}/bot{token}/sendMessage", payload)


def describe(body: str) -> str:
    """Telegram 失敗時回 {"ok":false,"description":"..."}，把那句話挖出來。"""
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return " ".join(body.split())[:200]
    if isinstance(d, dict) and d.get("description"):
        return str(d["description"])
    return " ".join(body.split())[:200]


def deliver(text: str, button: tuple[str, str] | None, token: str, chat_id: str) -> int:
    """送出並把結果翻成 exit code。

    一定要看回應碼：Telegram 權杖打錯會回 404、chat id 錯會回 400，
    兩種都還是一次正常的 HTTP 往返 —— 不檢查的話通知默默停掉,
    而 step 是綠的。這正是 LINE 那次九個交易日沒人發現的原因。"""
    code, body = send(text, token=token, chat_id=chat_id, button=button)
    if 200 <= code < 300:
        print(f"Telegram 已送出（HTTP {code}）", flush=True)
        return 0
    desc = describe(body)
    if code in (400, 401, 403, 404):
        # 設定錯誤：重試也不會好，要有人去改 secret。
        emit_error(
            f"Telegram 推播失敗（HTTP {code}）：{desc}。"
            "請檢查 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID。報告本身不受影響。"
        )
        return 1
    # 5xx 與連線問題屬暫時性，不值得為它把整個 run 變紅。
    emit_warning(
        f"Telegram 推播失敗（HTTP {code}）：{desc}。可能是暫時性問題，報告本身不受影響。"
    )
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把報告摘要推到 Telegram")
    ap.add_argument("--session", choices=sorted(SESSIONS), default="premarket")
    ap.add_argument("--date", default="", help="報告日期 YYYY-MM-DD（留空=最新一份）")
    ap.add_argument("--failure", action="store_true", help="改送失敗警告")
    ap.add_argument("--reason", default="", help="失敗原因（配合 --failure）")
    ap.add_argument("--run-url", default="", help="執行紀錄連結（配合 --failure）")
    ap.add_argument("--dry-run", action="store_true", help="只印訊息，不送出")
    args = ap.parse_args(argv)
    sess = SESSIONS[args.session]

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not args.dry_run and not (token and chat_id):
        # fork 的人沒設 secret 就安靜略過，不要讓他的 workflow 變紅。
        print("沒有 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID，略過 Telegram 推播。", flush=True)
        return 0

    if args.failure:
        text = build_failure_message(sess, args.reason, args.date.strip())
        button = ("看執行紀錄", args.run_url.strip())
    else:
        iso = args.date.strip()
        path = DOCS / f"{sess.stem}-{iso}.md" if iso else None
        if path is None or not path.exists():
            found = latest_report(sess)
            if iso and found:
                # 指定日期卻找不到檔案：這是 bug，不要默默改送別天的報告。
                emit_error(
                    f"找不到 {sess.title} {iso} 的報告（docs/{sess.stem}-{iso}.md），"
                    "改送失敗警告。"
                )
                text = build_failure_message(sess, f"找不到報告檔案 {sess.stem}-{iso}.md", iso)
                button = ("看執行紀錄", args.run_url.strip())
                if args.dry_run:
                    print(text)
                    return 1
                deliver(text, button, token, chat_id)
                return 1
            if not found:
                emit_error(f"docs/ 下找不到任何 {sess.title} 報告，沒有東西可推播。")
                return 1
            iso, path = found
            print(f"沒有指定日期，改送最新一份：{path.name}", flush=True)

        text, missing = build_report_message(sess, iso, path.read_text(encoding="utf-8"))
        if missing:
            emit_warning(
                "報告裡抓不到這些章節：" + "、".join(missing)
                + "。章節名稱可能被改了，推播摘要會缺一段（prompts/ 與 notify_telegram.py 要同步）。"
            )
        button = ("看完整報告", report_url(sess, iso))

    if args.dry_run:
        print(f"--- {len(text)} 字元 ---")
        print(text)
        print(f"--- 按鈕: {button} ---")
        return 0

    return deliver(text, button, token, chat_id)


if __name__ == "__main__":
    raise SystemExit(main())
