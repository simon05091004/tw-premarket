"""LINE 廣播通知，盤前、盤後、外匯三個 workflow 共用。

    LINE_TOKEN=… LINE_MESSAGE="…" python -m tools.line_notify

發送前先查本月額度（免費的輕用量方案每月 200 則，按「則數 × 好友數」計算）：

- 剩不到兩成時，在訊息最後附一行剩餘則數。趁還發得出去時提醒，額度用完之後
  LINE 這條路就沒了。
- 真的用完時，LINE 回 429「You have reached your monthly limit.」。這時讓 step
  失敗（離開碼 1），整個 run 變紅，GitHub 會寄失敗通知信——這是唯一還送得到你
  手上的管道。以前這種情況只亮黃色警告，通知默默停到月底也沒人發現。

離開碼：
  0  送出了；或是暫時性的失敗（5xx、連線問題、每秒請求太多），不值得把 run 變紅
  1  權杖無效（401/403），或本月額度用完（429 monthly limit）
"""

from __future__ import annotations

import os
import sys

import requests

API = "https://api.line.me/v2/bot"
TIMEOUT = 15
# 剩餘額度不到總額的這個比例，就在訊息後面附上剩餘則數。
WARN_RATIO = 0.2


def quota(session: requests.Session) -> tuple[int, int] | None:
    """回 (本月上限, 已用則數)。沒有上限（付費方案）或查不到時回 None。"""
    try:
        q = session.get(f"{API}/message/quota", timeout=TIMEOUT)
        q.raise_for_status()
        info = q.json()
        if info.get("type") != "limited":
            return None
        used = session.get(f"{API}/message/quota/consumption", timeout=TIMEOUT)
        used.raise_for_status()
        return int(info["value"]), int(used.json()["totalUsage"])
    except Exception as exc:  # noqa: BLE001 —— 查不到額度不該擋住通知本身
        print(f"查不到 LINE 額度（略過，照常發送）：{exc}")
        return None


def with_quota_note(message: str, q: tuple[int, int] | None) -> str:
    if not q:
        return message
    limit, used = q
    left = max(limit - used, 0)
    if left > limit * WARN_RATIO:
        return message
    return (
        f"{message}\n\n⚠️ LINE 本月免費額度剩 {left}／{limit} 則。"
        "用完後通知會停到月底，GitHub 會改寄錯誤信通知你。"
    )


def send(token: str, message: str, session: requests.Session | None = None) -> int:
    s = session or requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    q = quota(s)
    if q:
        print(f"LINE 本月額度：已用 {q[1]}／{q[0]} 則")
    text = with_quota_note(message, q)
    try:
        r = s.post(f"{API}/message/broadcast", json={"messages": [{"type": "text", "text": text}]},
                   timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"::warning::LINE 廣播連線失敗，可能是暫時性問題：{exc}")
        return 0

    print(f"LINE 回應碼 {r.status_code}")
    if 200 <= r.status_code < 300:
        return 0
    body = r.text[:300]
    if r.status_code in (401, 403):
        print("::error::LINE 權杖無效或已過期，請更新 LINE_CHANNEL_ACCESS_TOKEN secret。"
              "資料與報告已照常發布，只有通知沒送出。")
        return 1
    if r.status_code == 429 and "monthly limit" in body.lower():
        limit = f"（上限 {q[0]} 則）" if q else ""
        print(f"::error::LINE 本月訊息額度已用完{limit}，到月底前所有 LINE 通知都送不出去。"
              "資料與報告照常更新；要恢復通知，請減少好友數、等下個月，或改用付費方案。")
        return 1
    print(f"::warning::LINE 廣播失敗（HTTP {r.status_code}），可能是暫時性問題：{body}")
    return 0


def main() -> int:
    token = os.environ.get("LINE_TOKEN", "")
    message = os.environ.get("LINE_MESSAGE", "")
    if not token:
        print("沒有 LINE_TOKEN，略過通知")
        return 0
    if not message.strip():
        print("::warning::LINE_MESSAGE 是空的，沒有發送")
        return 0
    return send(token, message)


if __name__ == "__main__":
    sys.exit(main())
