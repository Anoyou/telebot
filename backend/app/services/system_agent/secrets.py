"""聊天密钥抽取与 Action 敏感字段处理（阶段 3）。"""

from __future__ import annotations

import re
from typing import Any

from .actions import encrypt_secret_payload, split_secret_arguments
from .redactor import redact_message_text

# Provider 粘贴路由和持久化前密钥抽取必须共用这些模式。
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+", re.I)
_KNOWN_KEY_RE = re.compile(
    r"\b(?:sk-ant-|sk-proj-|sk-or-|sk-|xai-|gsk_|AIza|ghp_|hf_)[A-Za-z0-9_\-]{8,}"
)
_GENERIC_KEY_RE = re.compile(
    r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}\b"
)
_DOTTED_KEY_RE = re.compile(r"\b[A-Za-z0-9]{12,}\.[A-Za-z0-9]{12,}\b")
_LABELED_SECRET_RE = re.compile(
    r'''(?ix)
    (?:["']?)
    (?:api[_-]?key|apikey|token|authorization|password|secret)
    (?:["']?)\s*[:=]\s*
    (?P<value>"[^"\r\n]{8,}"|'[^'\r\n]{8,}'|[^\s,;}\]]{8,})
    '''
)
_REQUEST_HEADERS_CONTEXT_RE = re.compile(
    r"(?i)(?:\b(?:request|http|custom|compatibility)[\s_-]*headers?\b|请求头|兼容请求头)"
)
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_URL_PASSWORD_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s@/]+)@",
    re.I,
)
_NON_PROVIDER_URL_HINTS = (
    "github.com",
    "gitlab.com",
    "gitee.com",
    ".git",
    "插件",
    "仓库",
    "plugin",
    "repo",
)
_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    _KNOWN_KEY_RE,
    _BOT_TOKEN_RE,
    re.compile(r"(?i)\b(api[_-]?key|token|authorization|password)\s*[:=]\s*([^\s,;]{12,})"),
)

_SECRET_PLACEHOLDERS = {
    "***",
    "[redacted]",
    "<redacted>",
    "redacted",
    "[masked]",
    "masked",
}


def _is_secret_placeholder(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in _SECRET_PLACEHOLDERS:
        return True
    return bool(normalized) and "***" in normalized


def _provider_credential_remainder(text: str) -> str | None:
    raw = str(text or "")
    lowered = raw.lower()
    urls = _URL_RE.findall(raw)
    if not urls and "base_url" not in lowered and "baseurl" not in lowered:
        return None
    if any(hint in lowered for hint in _NON_PROVIDER_URL_HINTS):
        return None
    return _URL_RE.sub(" ", raw)


def looks_like_provider_credential_paste(text: str) -> bool:
    """判断 Base URL 与 Provider Key 是否同现。"""

    remainder = _provider_credential_remainder(text)
    if remainder is None:
        return False
    return bool(
        _KNOWN_KEY_RE.search(remainder)
        or _DOTTED_KEY_RE.search(remainder)
        or _GENERIC_KEY_RE.search(remainder)
        or _LABELED_SECRET_RE.search(remainder)
    )


def looks_like_standalone_provider_key(text: str) -> bool:
    """严格判断整条消息是否只有一个可信的 Provider Key。

    该判断只用于上一轮已经进入 Provider 任务后的安全续接。整条消息必须是单一
    token，避免把普通说明中的长 ID、链接或其他领域凭据误认成 Provider Key。
    """

    candidate = str(text or "").strip().strip("\"'`")
    if not candidate or any(ch.isspace() for ch in candidate):
        return False
    return bool(
        _KNOWN_KEY_RE.fullmatch(candidate)
        or _DOTTED_KEY_RE.fullmatch(candidate)
        or _GENERIC_KEY_RE.fullmatch(candidate)
    )


def extract_plaintext_secrets(text: str) -> list[str]:
    """从用户消息中提取疑似密钥明文（去重，保序）。"""

    found: list[str] = []
    seen: set[str] = set()
    raw = str(text or "")
    for match in _LABELED_SECRET_RE.finditer(raw):
        value = str(match.group("value") or "").strip().strip("\"'`")
        if len(value) >= 8 and value not in seen:
            seen.add(value)
            found.append(value)
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
    for match in _URL_PASSWORD_RE.finditer(raw):
        value = match.group(1)
        if value and value not in seen:
            seen.add(value)
            found.append(value)
    if looks_like_standalone_provider_key(raw):
        value = raw.strip().strip("\"'`")
        if value not in seen:
            seen.add(value)
            found.append(value)
    remainder = _provider_credential_remainder(raw)
    if remainder is not None and looks_like_provider_credential_paste(raw):
        for pat in (_DOTTED_KEY_RE, _GENERIC_KEY_RE):
            for match in pat.finditer(remainder):
                value = match.group(0)
                if value not in seen:
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


def contains_request_header_context(text: str) -> bool:
    """自定义请求头值禁止经聊天传递；其自然语言形态无法可靠逐值抽取。"""

    return bool(_REQUEST_HEADERS_CONTEXT_RE.search(str(text or "")))


def redact_user_message(text: str, secrets: list[str] | None = None) -> str:
    """生成可持久化的用户消息；请求头配置整段替换为安全提示。"""

    if contains_request_header_context(text):
        return "[安全提示：自定义请求头不能通过聊天传入，请在 AI Provider 设置中填写并轮换密钥。]"
    return redact_known_secrets(text, secrets)


def merge_secret_into_arguments(
    arguments: dict[str, Any],
    *,
    secret_names: tuple[str, ...],
    chat_secrets: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """合并工具参数中的密钥与聊天提取的密钥。

    若敏感字段均为空或只有掩码占位符，使用当前聊天提取到的真实密钥；
    已有任一真实值时不再跨字段补注入。
    """

    args = dict(arguments or {})
    secrets_list = list(chat_secrets or [])
    if secrets_list and secret_names:
        # 聊天抽取结果没有可靠的字段归属信息：优先替换模型明确给出的脱敏占位符；
        # 仅当所有敏感字段都为空时，才回填约定中的第一个字段。若已有任一真实值，
        # 继续把同一密钥塞入另一个缺失字段会把 API Key 误当成请求头、Token 等。
        has_concrete_secret = any(
            args.get(name) not in (None, "")
            and not _is_secret_placeholder(args.get(name))
            for name in secret_names
        )
        target_name = None
        if not has_concrete_secret:
            target_name = next(
                (name for name in secret_names if _is_secret_placeholder(args.get(name))),
                secret_names[0],
            )
        if target_name is not None:
            args[target_name] = secrets_list[0]
    return split_secret_arguments(args, secret_names)


def encrypt_secrets_dict(secrets: dict[str, Any]) -> str | None:
    return encrypt_secret_payload(secrets)


__all__ = [
    "contains_request_header_context",
    "encrypt_secrets_dict",
    "extract_plaintext_secrets",
    "looks_like_provider_credential_paste",
    "looks_like_standalone_provider_key",
    "merge_secret_into_arguments",
    "redact_known_secrets",
    "redact_user_message",
]
