"""统一敏感信息脱敏 helper。"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***"

SENSITIVE_KEYS = {
    "secret",
    "secret_key",
    "api_key",
    "apikey",
    "token",
    "password",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer_token",
    "bot_token",
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "proxy_user",
    "proxy_pass",
    "session",
    "session_string",
    "totp",
}
SENSITIVE_KEY_SUFFIXES = tuple(f"_{key}" for key in sorted(SENSITIVE_KEYS)) + ("_enc",)

_URL_CREDENTIAL_RE = re.compile(
    r"((?:https?|socks5?|mtproxy)://)([^:/@\s]+):([^/@\s]+)@",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(r"((?:Bearer|Basic)\s+)[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_TELEGRAM_BOT_TOKEN_RE = re.compile(
    r"((?:https?://)?api\.telegram\.org/bot)[^/\s\"']+",
    re.IGNORECASE,
)
_KV_TOKEN_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\b(\s*[:=]\s*)([^\s,;\"']{4,})"
)


def is_sensitive_key(key: str) -> bool:
    camel_split = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_split)
    return normalized in SENSITIVE_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_text(text: str) -> str:
    out = _URL_CREDENTIAL_RE.sub(r"\1***:***@", text)
    out = _TELEGRAM_BOT_TOKEN_RE.sub(r"\1***", out)
    out = _AUTH_HEADER_RE.sub(r"\1***", out)
    out = _SK_TOKEN_RE.sub(REDACTED, out)
    out = _KV_TOKEN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out


def redact_value(value: Any, *, drop_sensitive_keys: bool = False) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = str(key)
            if is_sensitive_key(k):
                if drop_sensitive_keys:
                    continue
                out[k] = REDACTED if item not in (None, "") else ""
                continue
            out[k] = redact_value(item, drop_sensitive_keys=drop_sensitive_keys)
        return out
    if isinstance(value, list):
        return [redact_value(item, drop_sensitive_keys=drop_sensitive_keys) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, drop_sensitive_keys=drop_sensitive_keys) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
