"""呼叫 Anthropic API 產生盤前／盤後分析文字。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
# max_tokens 是「思考 + 回應文字」的總上限,不是回應長度上限。
# Sonnet 5 預設開 adaptive thinking,盤後 payload 大、思考量高,
# 8000 會被思考吃掉大半,導致正文寫到一半就撞上限。
MAX_TOKENS = 16000
MIN_BRIEF_CHARS = 500  # 低於此長度視為產出失敗（正常報告 1500 字元以上）


class BriefTruncated(RuntimeError):
    """回應撞到 max_tokens —— 報告不完整,不可發布。"""
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

    system_prompt = _load_system_prompt(session)
    user_content = "\n".join(parts)
    log.info(
        "送出 API：system %d 字元、payload %d 字元（其中今日數據 %d）、max_tokens=%d",
        len(system_prompt),
        len(user_content),
        len(parts[1]),
        MAX_TOKENS,
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    log.info(
        "API 回應：stop_reason=%s input_tokens=%s output_tokens=%s → 產出 %d 字元",
        resp.stop_reason,
        resp.usage.input_tokens,
        resp.usage.output_tokens,
        len(text),
    )
    if resp.stop_reason == "max_tokens":
        raise BriefTruncated(
            f"回應撞到 max_tokens={MAX_TOKENS}（output_tokens={resp.usage.output_tokens}），"
            f"只產出 {len(text)} 字元"
        )
    return text
