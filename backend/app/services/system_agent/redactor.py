"""System Agent 消息与工具结果打码。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..redactor import redact_text, redact_value

_PARTIAL_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Keep a token-shaped tail until a non-token delimiter arrives.  This avoids
    # exposing a credential merely because a provider split it across SSE
    # frames.  A normal word is not retained unless it starts with a known key
    # prefix.
    re.compile(r"(?i)(?:\b(?:sk-ant-|sk-or-|sk-|xai-|gsk_|AIza)[A-Za-z0-9_-]*)$"),
    re.compile(
        r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*[^\s,;\"']*)$"
    ),
    re.compile(r"(?i)(?:\b(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]*)$"),
    re.compile(r"(?i)(?:api\.telegram\.org/bot[^/\s\"']*)$"),
)

_PARTIAL_SECRET_PREFIXES: tuple[str, ...] = (
    "sk-ant-",
    "sk-or-",
    "sk-",
    "xai-",
    "gsk_",
    "aiza",
    "api_key",
    "api-key",
    "apikey",
    "access_token",
    "access-token",
    "token",
    "password",
    "secret",
    "bearer ",
    "basic ",
    "api.telegram.org/bot",
)


def redact_message_text(text: str) -> str:
    """对用户/助手文本做基础敏感信息打码。"""

    return redact_text(str(text or ""))


def redact_content(value: Any) -> Any:
    """对 JSON 内容做递归打码。"""

    return redact_value(value)


@dataclass
class StreamingMessageRedactor:
    """Redact text before it becomes an incremental durable event.

    A final message can be redacted as a whole.  A delta stream needs one extra
    step: retain any suffix that could still complete a known or conventional
    credential, then redact only the stable prefix.  The class never invents
    text and only delays a small ambiguous suffix.
    """

    secrets: list[str] = field(default_factory=list)
    _pending: str = ""

    def push(self, value: str) -> str:
        self._pending += str(value or "")
        hold = self._hold_length()
        if hold:
            stable = self._pending[:-hold]
            self._pending = self._pending[-hold:]
        else:
            stable = self._pending
            self._pending = ""
        return self._redact(stable)

    def reset(self) -> None:
        self._pending = ""

    def finish(self) -> str:
        stable = self._pending
        self._pending = ""
        return self._redact(stable)

    def _redact(self, value: str) -> str:
        from .secrets import redact_known_secrets

        redacted = redact_known_secrets(value, self.secrets)
        # Durable/event consumers use [REDACTED] consistently; the generic
        # redactor uses *** for patterns it discovers independently.
        return redacted.replace("***", "[REDACTED]")

    def _hold_length(self) -> int:
        value = self._pending
        hold = 0
        for secret in self.secrets:
            if not secret:
                continue
            limit = min(len(secret) - 1, len(value))
            for size in range(limit, 0, -1):
                if value.endswith(secret[:size]):
                    hold = max(hold, size)
                    break
        lowered = value.lower()
        for prefix in _PARTIAL_SECRET_PREFIXES:
            limit = min(len(prefix) - 1, len(lowered))
            for size in range(limit, 0, -1):
                if lowered.endswith(prefix[:size]):
                    hold = max(hold, size)
                    break
        for pattern in _PARTIAL_SECRET_PATTERNS:
            match = pattern.search(value)
            if match is not None:
                hold = max(hold, len(match.group(0)))
        return hold


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
    "StreamingMessageRedactor",
    "summarize_tool_result",
]
