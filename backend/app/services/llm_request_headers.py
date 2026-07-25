"""LLM Provider 兼容请求头的校验、加密存储与请求规划。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..crypto import decrypt_str, encrypt_str

REQUEST_SCOPE_INFERENCE = "inference"
REQUEST_SCOPE_LIVENESS = "liveness"
REQUEST_SCOPE_MODELS = "models"
ALL_REQUEST_HEADER_SCOPES = frozenset({REQUEST_SCOPE_INFERENCE, REQUEST_SCOPE_LIVENESS, REQUEST_SCOPE_MODELS})

MAX_REQUEST_HEADERS = 16
MAX_HEADER_NAME_LENGTH = 64
MAX_HEADER_VALUE_LENGTH = 2048

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PROTECTED_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "forwarded",
        "content-type",
        "accept",
        "anthropic-version",
        "anthropic-beta",
        "user-agent",
        "originator",
        "x-app",
        "session-id",
        "session_id",
        "thread-id",
        "thread_id",
        "x-client-request-id",
        "conversation-id",
        "conversation_id",
        "x-claude-code-session-id",
        "x-grok-conv-id",
        "x-grok-req-id",
        "x-grok-session-id",
        "x-grok-agent-id",
        "x-grok-turn-idx",
        "x-grok-model-override",
        "x-xai-token-auth",
        "x-authenticateresponse",
    }
)
_PROTECTED_HEADER_PREFIXES = ("x-forwarded-", "x-stainless-", "x-grok-client-")


class RequestHeaderConfigError(ValueError):
    """兼容请求头配置不合法；消息绝不包含请求头值。"""


def _item_dict(item: object) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _validated_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise RequestHeaderConfigError("请求头名称不能为空")
    if len(name) > MAX_HEADER_NAME_LENGTH or not _HEADER_NAME_RE.fullmatch(name):
        raise RequestHeaderConfigError(f"请求头名称必须是最多 {MAX_HEADER_NAME_LENGTH} 个 RFC token 字符")
    lowered = name.casefold()
    if lowered in _PROTECTED_HEADER_NAMES or any(
        lowered.startswith(prefix) for prefix in _PROTECTED_HEADER_PREFIXES
    ):
        raise RequestHeaderConfigError(f"请求头 {name} 由系统管理，不能自定义")
    return name


def _validated_scopes(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise RequestHeaderConfigError("请求头作用域必须是数组")
    scopes: list[str] = []
    for raw in value:
        scope = str(raw or "").strip().lower()
        if scope not in ALL_REQUEST_HEADER_SCOPES:
            raise RequestHeaderConfigError(f"未知请求头作用域：{scope or '空值'}")
        if scope not in scopes:
            scopes.append(scope)
    if not scopes:
        raise RequestHeaderConfigError("请求头至少选择一个作用域")
    return scopes


def normalize_request_headers(
    items: Iterable[object] | None,
    *,
    existing: Iterable[object] | None = None,
) -> list[dict[str, Any]]:
    """校验并规范化请求头；值为 None 时按名称保留 existing 中的密文值。"""

    incoming = list(items or [])
    if len(incoming) > MAX_REQUEST_HEADERS:
        raise RequestHeaderConfigError(f"每个 Provider 最多配置 {MAX_REQUEST_HEADERS} 个请求头")

    existing_values: dict[str, str] = {}
    for raw in existing or []:
        item = _item_dict(raw)
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if name and isinstance(value, str):
            existing_values[name.casefold()] = value

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in incoming:
        item = _item_dict(raw)
        name = _validated_name(item.get("name"))
        lowered = name.casefold()
        if lowered in seen:
            raise RequestHeaderConfigError(f"请求头名称重复：{name}")
        seen.add(lowered)

        value = item.get("value")
        if value is None:
            value = existing_values.get(lowered)
        if not isinstance(value, str) or not value:
            raise RequestHeaderConfigError(f"请求头 {name} 缺少值")
        if len(value) > MAX_HEADER_VALUE_LENGTH:
            raise RequestHeaderConfigError(f"请求头 {name} 的值不能超过 {MAX_HEADER_VALUE_LENGTH} 个字符")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise RequestHeaderConfigError(f"请求头 {name} 的值不能包含换行或控制字符")

        output.append(
            {
                "name": name,
                "value": value,
                "scopes": _validated_scopes(item.get("scopes")),
            }
        )
    return output


def encrypt_request_headers(
    items: Iterable[object] | None, *, existing_token: str | None = None
) -> str | None:
    existing = decrypt_request_headers(existing_token)
    normalized = normalize_request_headers(items, existing=existing)
    if not normalized:
        return None
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return encrypt_str(payload)


def decrypt_request_headers(token: str | None) -> list[dict[str, Any]]:
    if not token:
        return []
    try:
        raw = json.loads(decrypt_str(token))
    except Exception as exc:  # noqa: BLE001 - 仅返回不含密文的配置错误
        raise RequestHeaderConfigError("Provider 请求头配置无法解密或解析") from exc
    if not isinstance(raw, list):
        raise RequestHeaderConfigError("Provider 请求头配置格式无效")
    return normalize_request_headers(raw)


def request_header_summaries(token: str | None) -> list[dict[str, Any]]:
    return [
        {
            "name": item["name"],
            "scopes": list(item["scopes"]),
            "has_value": True,
        }
        for item in decrypt_request_headers(token)
    ]


def request_headers_for_scope(
    token_or_items: str | Iterable[object] | None,
    scope: str,
    *,
    existing_token: str | None = None,
) -> dict[str, str]:
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in ALL_REQUEST_HEADER_SCOPES:
        raise RequestHeaderConfigError(f"未知请求头作用域：{normalized_scope or '空值'}")
    if isinstance(token_or_items, str) or token_or_items is None:
        items = decrypt_request_headers(token_or_items)
    else:
        items = normalize_request_headers(
            token_or_items,
            existing=decrypt_request_headers(existing_token),
        )
    return {str(item["name"]): str(item["value"]) for item in items if normalized_scope in item["scopes"]}


def plan_request_headers(
    *,
    system_headers: Mapping[str, str] | None = None,
    identity_headers: Mapping[str, str] | None = None,
    runtime_headers: Mapping[str, str] | None = None,
    compatibility_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """按系统、身份、运行态、Provider 兼容层的固定顺序合并请求头。"""

    planned: dict[str, str] = {}
    owners: dict[str, str] = {}
    values: dict[str, str] = {}
    for owner, layer in (
        ("系统", system_headers),
        ("客户端身份", identity_headers),
        ("运行态", runtime_headers),
        ("Provider 兼容", compatibility_headers),
    ):
        for name, value in (layer or {}).items():
            lowered = name.casefold()
            if lowered in owners:
                if values[lowered] == value:
                    continue
                raise RequestHeaderConfigError(f"请求头 {name} 与{owners[lowered]}管理的字段冲突")
            planned[name] = value
            owners[lowered] = owner
            values[lowered] = value
    return planned


__all__ = [
    "ALL_REQUEST_HEADER_SCOPES",
    "MAX_REQUEST_HEADERS",
    "REQUEST_SCOPE_INFERENCE",
    "REQUEST_SCOPE_LIVENESS",
    "REQUEST_SCOPE_MODELS",
    "RequestHeaderConfigError",
    "decrypt_request_headers",
    "encrypt_request_headers",
    "normalize_request_headers",
    "plan_request_headers",
    "request_header_summaries",
    "request_headers_for_scope",
]
