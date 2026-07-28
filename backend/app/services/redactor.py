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
_AUTHORIZATION_KV_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?P<key_quote>["']?)
        (?P<key>(?:[a-z0-9.-]+[_-])*authorization)
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?:
        (?P<value_quote>["'])(?P<quoted_value>[^"'\r\n]*)(?P=value_quote)|
        (?P<bare_value>[^\r\n}\]]+)
    )
    """
)
_PROVIDER_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
)
_TELEGRAM_BOT_TOKEN_RE = re.compile(
    r"((?:https?://)?api\.telegram\.org/bot)[^/\s\"']+",
    re.IGNORECASE,
)
_KV_SECRET_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?P<key_quote>["']?)
        (?P<key>
            (?:[a-z0-9.-]+[_-])*
            (?:
                api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|
                bearer[_-]?token|bot[_-]?token|token|password|secret(?:[_-]?key)?|
                credentials?|cookie|proxy[_-]?(?:user|pass)|
                session(?:[_-]?string)?|totp|[a-z0-9_.-]+_enc
            )
        )
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?:
        (?P<scheme>(?:Bearer|Basic)\s+[^\s,;}\]]+)|
        (?P<value_quote>["'])(?P<quoted_value>[^"'\r\n]*)(?P=value_quote)|
        (?P<bare_value>[^\s,;}\]]{4,})
    )
    """
)


def is_sensitive_key(key: str) -> bool:
    camel_split = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", camel_split)
    return normalized in SENSITIVE_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_text(text: str) -> str:
    out = _URL_CREDENTIAL_RE.sub(r"\1***:***@", text)
    out = _TELEGRAM_BOT_TOKEN_RE.sub(r"\1***", out)
    for pattern in _PROVIDER_TOKEN_PATTERNS:
        out = pattern.sub(REDACTED, out)

    def _redact_authorization(match: re.Match[str]) -> str:
        quote = match.group("value_quote") or ""
        raw_value = match.group("quoted_value") or match.group("bare_value") or ""
        scheme_match = re.match(r"([A-Za-z][A-Za-z0-9._-]*)\s+", raw_value.strip())
        replacement = f"{scheme_match.group(1)} {REDACTED}" if scheme_match else REDACTED
        return f"{match.group('prefix')}{quote}{replacement}{quote}"

    out = _AUTHORIZATION_KV_RE.sub(_redact_authorization, out)

    def _redact_kv(match: re.Match[str]) -> str:
        scheme = match.group("scheme")
        if scheme:
            scheme_name = scheme.split(None, 1)[0]
            return f"{match.group('prefix')}{scheme_name} {REDACTED}"
        quote = match.group("value_quote") or ""
        return f"{match.group('prefix')}{quote}{REDACTED}{quote}"

    out = _KV_SECRET_RE.sub(_redact_kv, out)
    out = _AUTH_HEADER_RE.sub(r"\1***", out)
    return out


def redact_value(value: Any, *, drop_sensitive_keys: bool = False) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = str(key)
            # has_api_key / has_token 等仅表示凭据是否存在。只放行严格布尔值，
            # 字符串仍按敏感字段处理，避免借字段名前缀绕过脱敏。
            if k.lower().startswith("has_") and isinstance(item, bool):
                out[k] = item
                continue
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
