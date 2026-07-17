"""System Agent 消息与工具结果打码。"""

from __future__ import annotations

from typing import Any

from ..redactor import redact_text, redact_value


def redact_message_text(text: str) -> str:
    """对用户/助手文本做基础敏感信息打码。"""

    return redact_text(str(text or ""))


def redact_content(value: Any) -> Any:
    """对 JSON 内容做递归打码。"""

    return redact_value(value)


def summarize_tool_result(value: Any, *, max_chars: int = 4000) -> Any:
    """限制工具结果体积，避免把无界日志写进会话。"""

    redacted = redact_value(value)
    try:
        import json

        raw = json.dumps(redacted, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(redacted)
    if len(raw) <= max_chars:
        return redacted
    if isinstance(redacted, dict):
        return {
            "truncated": True,
            "preview": raw[:max_chars],
            "keys": list(redacted.keys())[:50],
        }
    if isinstance(redacted, list):
        return {
            "truncated": True,
            "count": len(redacted),
            "preview": raw[:max_chars],
        }
    return {"truncated": True, "preview": raw[:max_chars]}


__all__ = [
    "redact_content",
    "redact_message_text",
    "summarize_tool_result",
]
