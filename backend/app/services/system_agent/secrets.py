"""聊天密钥抽取与 Action 敏感字段处理（阶段 3）。"""

from __future__ import annotations

import re
from typing import Any

from .actions import encrypt_secret_payload, split_secret_arguments
from .redactor import redact_message_text

# 常见 API Key / Token 形态（保守匹配，避免吞掉普通短词）
_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})\b"),
    re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{16,})\b"),
    re.compile(r"\b(sk-or-[A-Za-z0-9_\-]{16,})\b"),
    re.compile(r"\b(xai-[A-Za-z0-9_\-]{16,})\b"),
    re.compile(r"\b(gsk_[A-Za-z0-9_\-]{16,})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z_\-]{20,})\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|authorization|password)\s*[:=]\s*([^\s,;]{12,})"),
)


def extract_plaintext_secrets(text: str) -> list[str]:
    """从用户消息中提取疑似密钥明文（去重，保序）。"""

    found: list[str] = []
    seen: set[str] = set()
    raw = str(text or "")
    for pat in _KEY_PATTERNS:
        for match in pat.finditer(raw):
            # 带捕获组的模式取最后一组
            value = match.group(match.lastindex) if match.lastindex else match.group(0)
            value = str(value or "").strip().strip("\"'`")
            if len(value) < 12:
                continue
            if value.lower() in {"api_key", "token", "authorization", "password"}:
                continue
            if value in seen:
                continue
            seen.add(value)
            found.append(value)
    return found


def redact_known_secrets(text: str, secrets: list[str] | None = None) -> str:
    """先替换已知明文密钥，再做基础字段打码。"""

    out = str(text or "")
    for secret in secrets or []:
        if secret and secret in out:
            out = out.replace(secret, "[REDACTED]")
    return redact_message_text(out)


def merge_secret_into_arguments(
    arguments: dict[str, Any],
    *,
    secret_names: tuple[str, ...],
    chat_secrets: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """合并工具参数中的密钥与聊天提取的密钥。

    若 arguments 缺少 secret 字段且聊天里恰好有一个密钥，填入第一个 secret_names。
    """

    args = dict(arguments or {})
    secrets_list = list(chat_secrets or [])
    if secrets_list and secret_names:
        for name in secret_names:
            if args.get(name) in (None, ""):
                args[name] = secrets_list[0]
                break
    return split_secret_arguments(args, secret_names)


def encrypt_secrets_dict(secrets: dict[str, Any]) -> str | None:
    return encrypt_secret_payload(secrets)


__all__ = [
    "encrypt_secrets_dict",
    "extract_plaintext_secrets",
    "merge_secret_into_arguments",
    "redact_known_secrets",
]
