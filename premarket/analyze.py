"""呼叫 Anthropic API 產生盤前／盤後分析文字。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = 8000  # Sonnet 5 預設開 adaptive thinking，思考與回應共用這個額度
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_FILES = {
    "premarket": "premarket_system.md",
    "postmarket": "postmarket_system.md",
}


def _load_system_prompt(session: str = "premarket") -> str:
    try:
        name = PROMPT_FILES[session]
    except KeyError:
        raise ValueError(f"未知的 session: {session!r}") from None
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def generate_brief(
    payload: dict,
    prev_payload: dict | None = None,
    session: str = "premarket",
) -> str:
    """
    payload      : 今天的數據
    prev_payload : 前一份同類型報告的數據（用來寫「變化」而不只是「數值」）
    session      : premarket / postmarket，決定讀哪一份 system prompt
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    scope = "盤前" if session == "premarket" else "盤後"
    parts = [
        f"以下是今日{scope}的結構化數據。請嚴格依據這份 JSON 撰寫分析，"
        "不得引入任何 JSON 以外的數字或消息。\n",
        "## 今日數據\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```",
    ]
    if prev_payload:
        parts.append(
            "\n## 前一份報告的數據（僅供計算變化量，不要直接複述）\n```json\n"
            + json.dumps(prev_payload, ensure_ascii=False, indent=2)
            + "\n```"
        )
    if payload.get("missing"):
        parts.append(
            "\n## 注意\n以下項目資料缺漏或非最新，請依規則處理：\n- "
            + "\n- ".join(payload["missing"])
        )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_load_system_prompt(session),
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()
