"""LLM 错误事实与诊断分类的唯一入口。

把 HTTP 响应、异常与响应体统一分类为 ``diagnostic_status``，供协议检测与
模型测活复用。诊断状态**不等于**数据库启用状态，也不直接改变生产 runtime 健康。

安全红线：所有面向前端 / 插件的错误文本必须脱敏——不得回传 api_key、
Base URL、代理地址或完整敏感响应体。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .redactor import redact_text

# ── 诊断状态枚举 ────────────────────────────────────────────
DIAG_HEALTHY = "healthy"
DIAG_EMPTY_RESPONSE = "empty_response"
DIAG_RATE_LIMITED = "rate_limited"
DIAG_AUTH_FAILED = "auth_failed"
DIAG_PERMISSION_DENIED = "permission_denied"
DIAG_CLIENT_REJECTED = "client_rejected"
DIAG_OFFICIAL_ACCOUNT_REQUIRED = "official_account_required"
DIAG_ACCOUNT_POLICY = "account_policy"
DIAG_PROTOCOL_REJECTED = "protocol_rejected"
DIAG_MODEL_MISSING = "model_missing"
DIAG_ENDPOINT_MISSING = "endpoint_missing"
DIAG_REQUEST_INVALID = "request_invalid"
DIAG_CONTEXT_LIMIT = "context_limit"
DIAG_QUOTA_EXHAUSTED = "quota_exhausted"
DIAG_TIMEOUT = "timeout"
DIAG_UPSTREAM_ERROR = "upstream_error"
DIAG_GATEWAY_UNAVAILABLE = "gateway_unavailable"
DIAG_GATEWAY_OVERLOADED = "gateway_overloaded"
DIAG_INVALID_RESPONSE = "invalid_response"
DIAG_SKIPPED_DISABLED = "skipped_disabled"
DIAG_SKIPPED_PROVIDER_MISSING = "skipped_provider_missing"
DIAG_CONFIG_ERROR = "config_error"
DIAG_CANCELLED = "cancelled"
DIAG_NETWORK_ERROR = "network_error"

ALL_DIAGNOSTIC_STATUSES = {
    DIAG_HEALTHY,
    DIAG_EMPTY_RESPONSE,
    DIAG_RATE_LIMITED,
    DIAG_AUTH_FAILED,
    DIAG_PERMISSION_DENIED,
    DIAG_CLIENT_REJECTED,
    DIAG_OFFICIAL_ACCOUNT_REQUIRED,
    DIAG_ACCOUNT_POLICY,
    DIAG_PROTOCOL_REJECTED,
    DIAG_MODEL_MISSING,
    DIAG_ENDPOINT_MISSING,
    DIAG_REQUEST_INVALID,
    DIAG_CONTEXT_LIMIT,
    DIAG_QUOTA_EXHAUSTED,
    DIAG_TIMEOUT,
    DIAG_UPSTREAM_ERROR,
    DIAG_GATEWAY_UNAVAILABLE,
    DIAG_GATEWAY_OVERLOADED,
    DIAG_INVALID_RESPONSE,
    DIAG_SKIPPED_DISABLED,
    DIAG_SKIPPED_PROVIDER_MISSING,
    DIAG_CONFIG_ERROR,
    DIAG_CANCELLED,
    DIAG_NETWORK_ERROR,
}

# 状态 → 中文修复建议（脱敏，可直接展示给用户）。
_SUGGESTIONS = {
    DIAG_HEALTHY: "",
    DIAG_EMPTY_RESPONSE: "请求成功但未返回有效内容，可能是模型或参数限制；换用自然提示词或提高输出上限重试。",
    DIAG_RATE_LIMITED: "触发频率或额度限制；稍后重试或降低该 Provider 的并发。",
    DIAG_AUTH_FAILED: "鉴权失败：请检查 API Key 是否有效、是否有该模型权限。",
    DIAG_PERMISSION_DENIED: "上游已识别凭据但拒绝访问；请检查模型、项目、区域或账号权限。",
    DIAG_CLIENT_REJECTED: "上游限制了当前客户端；Responses Provider 可改用内置 Gateway，若仍被拒绝则需使用上游支持的官方接入方式。",
    DIAG_OFFICIAL_ACCOUNT_REQUIRED: "上游要求官方账号运行时；TelePilot 不会伪造或导入账号身份凭据。",
    DIAG_ACCOUNT_POLICY: "请求被账号或内容策略拒绝；请检查上游账号策略与请求内容。",
    DIAG_PROTOCOL_REJECTED: "该端点不接受此协议的请求格式；确认 Base URL 与 API Format 是否匹配。",
    DIAG_MODEL_MISSING: "模型不存在或当前 Key 无权限；核对模型 ID。",
    DIAG_ENDPOINT_MISSING: "API 路径不存在；请核对 Base URL 与 API Format。",
    DIAG_REQUEST_INVALID: "请求参数或工具定义无效；请检查协议、参数和工具 schema。",
    DIAG_CONTEXT_LIMIT: "请求超过模型上下文或输出限制；请缩短输入或降低输出上限。",
    DIAG_QUOTA_EXHAUSTED: "账号余额、月度额度或项目配额已耗尽；请检查上游计费与额度。",
    DIAG_TIMEOUT: "连接或读取超时；检查网络、代理与上游可用性后重试。",
    DIAG_UPSTREAM_ERROR: "上游服务错误（5xx）；通常为临时故障，可稍后重试。",
    DIAG_GATEWAY_UNAVAILABLE: "内置 Gateway 当前不可用；请检查 Gateway 健康状态和版本兼容性。",
    DIAG_GATEWAY_OVERLOADED: "内置 Gateway 已达到并发上限；请稍后重试或降低并发。",
    DIAG_INVALID_RESPONSE: "上游返回了无法解析或缺少终态的响应；请检查协议兼容性。",
    DIAG_CONFIG_ERROR: "Provider 配置不完整（如缺少 API Key）。",
    DIAG_NETWORK_ERROR: "网络异常：无法连接目标域名 / SSL 握手失败 / 代理未生效。",
}


def suggestion_for(status: str) -> str:
    """返回某诊断状态对应的中文修复建议。"""
    return _SUGGESTIONS.get(status, "")


# 明确指向"需要特定客户端身份"的上游信号（小写子串匹配）。
_CLIENT_REJECT_HINTS = (
    "only allows codex official clients",
    "requires the codex cli",
    "cli client",
    "unsupported client",
    "client not allowed",
    "must use the official",
    "user-agent",
    "x-app",
    "originator",
)

_OFFICIAL_ACCOUNT_HINTS = (
    "official account required",
    "chatgpt account required",
    "oauth account required",
    "requires chatgpt oauth",
)
_POLICY_HINTS = ("account policy", "safety policy", "content policy", "moderation", "policy violation")
_CONTEXT_HINTS = ("context_length_exceeded", "context window", "maximum context length", "too many tokens")
_QUOTA_HINTS = ("insufficient_quota", "quota exceeded", "billing", "credit balance", "monthly limit")


@dataclass(frozen=True, slots=True)
class LLMDiagnostic:
    """可持久化、可展示且不含凭据的错误事实。"""

    category: str
    retryable: bool
    scope: str
    safe_message: str
    status_code: int | None = None
    upstream_status_code: int | None = None
    upstream_error_code: str | None = None
    upstream_error_message: str | None = None
    upstream_error_detail: str | None = None
    upstream_request_id: str | None = None
    client_request_id: str | None = None
    request_id: str | None = None
    gateway_stage: str | None = None
    upstream_summary: str | None = None


@dataclass(frozen=True, slots=True)
class _StructuredError:
    code: str | None
    message: str
    upstream_status_code: int | None = None
    upstream_error_code: str | None = None
    upstream_error_message: str | None = None
    upstream_error_detail: str | None = None
    upstream_request_id: str | None = None
    client_request_id: str | None = None


def _safe_code(value: Any) -> str | None:
    return re.sub(r"[^a-zA-Z0-9_.-]", "", str(value or ""))[:80] or None


def _safe_status(value: Any) -> int | None:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _safe_request_id(value: Any) -> str | None:
    text = str(value or "").strip()[:128]
    return text if text and re.fullmatch(r"[a-zA-Z0-9._:-]+", text) else None


def _detail_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).strip() or None


def _first_value(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def _structured_error_facts(body: str | Mapping[str, Any] | None) -> _StructuredError:
    if isinstance(body, Mapping):
        payload: Any = body
        raw = json.dumps(body, ensure_ascii=False)
    else:
        raw = str(body or "")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return _StructuredError(code=None, message=raw)
    if not isinstance(payload, Mapping):
        return _StructuredError(code=None, message=raw)
    error = payload.get("error")
    response = payload.get("response")
    response = response if isinstance(response, Mapping) else {}
    response_error = response.get("error")
    source = (
        error
        if isinstance(error, Mapping)
        else response_error
        if isinstance(response_error, Mapping)
        else response
        if response
        else payload
    )
    nested_upstream: Mapping[str, Any] = {}
    upstream_errors = next(
        (
            value
            for container in (source, response, payload)
            if isinstance((value := container.get("upstream_errors")), list)
        ),
        None,
    )
    if isinstance(upstream_errors, list) and upstream_errors and isinstance(upstream_errors[0], Mapping):
        nested_upstream = upstream_errors[0]

    upstream_status_code = _safe_status(
        _first_value(source, "upstream_status_code")
        or _first_value(response, "upstream_status_code")
        or _first_value(payload, "upstream_status_code")
        or _first_value(nested_upstream, "upstream_status_code")
    )
    upstream_error_code = _safe_code(
        _first_value(source, "upstream_error_code")
        or _first_value(response, "upstream_error_code")
        or _first_value(payload, "upstream_error_code")
        or _first_value(nested_upstream, "upstream_error_code", "code")
    )
    upstream_error_message = _detail_text(
        _first_value(source, "upstream_error_message")
        or _first_value(response, "upstream_error_message")
        or _first_value(payload, "upstream_error_message")
        or _first_value(nested_upstream, "upstream_error_message", "message")
    )
    upstream_error_detail = _detail_text(
        _first_value(source, "upstream_error_detail")
        or _first_value(response, "upstream_error_detail")
        or _first_value(payload, "upstream_error_detail")
        or _first_value(
            nested_upstream,
            "upstream_error_detail",
            "detail",
            "upstream_response_body",
        )
    )
    return _StructuredError(
        code=_safe_code(source.get("code") or source.get("type") or payload.get("code")),
        message=str(
            source.get("message")
            or source.get("detail")
            or payload.get("message")
            or raw
        ),
        upstream_status_code=upstream_status_code,
        upstream_error_code=upstream_error_code,
        upstream_error_message=upstream_error_message,
        upstream_error_detail=upstream_error_detail,
        # ``request_id`` 属于当前 TelePilot/Gateway 层，绝不能在这里冒充上游 ID。
        upstream_request_id=_safe_request_id(
            _first_value(source, "upstream_request_id")
            or _first_value(response, "upstream_request_id")
            or _first_value(payload, "upstream_request_id")
            or _first_value(nested_upstream, "upstream_request_id", "request_id")
        ),
        client_request_id=_safe_request_id(
            _first_value(source, "client_request_id")
            or _first_value(response, "client_request_id")
            or _first_value(payload, "client_request_id")
            or _first_value(nested_upstream, "client_request_id")
        ),
    )


def _structured_error(body: str | Mapping[str, Any] | None) -> tuple[str | None, str]:
    """兼容旧调用者；分类新逻辑使用完整结构化事实。"""

    facts = _structured_error_facts(body)
    return facts.upstream_error_code or facts.code, facts.upstream_error_message or facts.message


def _category_from_code(code: str | None) -> str | None:
    normalized = (code or "").lower()
    exact = {
        "invalid_api_key": DIAG_AUTH_FAILED,
        "authentication_error": DIAG_AUTH_FAILED,
        "permission_denied": DIAG_PERMISSION_DENIED,
        "client_rejected": DIAG_CLIENT_REJECTED,
        "official_account_required": DIAG_OFFICIAL_ACCOUNT_REQUIRED,
        "account_policy": DIAG_ACCOUNT_POLICY,
        "model_not_found": DIAG_MODEL_MISSING,
        "model_missing": DIAG_MODEL_MISSING,
        "endpoint_missing": DIAG_ENDPOINT_MISSING,
        "context_length_exceeded": DIAG_CONTEXT_LIMIT,
        "insufficient_quota": DIAG_QUOTA_EXHAUSTED,
        "quota_exhausted": DIAG_QUOTA_EXHAUSTED,
        "rate_limit_exceeded": DIAG_RATE_LIMITED,
        "gateway_unavailable": DIAG_GATEWAY_UNAVAILABLE,
        "gateway_overloaded": DIAG_GATEWAY_OVERLOADED,
        "cancelled": DIAG_CANCELLED,
    }
    if normalized in exact:
        return exact[normalized]
    if "rate_limit" in normalized:
        return DIAG_RATE_LIMITED
    return None


def _looks_like_client_rejection(body_lower: str) -> bool:
    return any(hint in body_lower for hint in _CLIENT_REJECT_HINTS)


def _looks_like_model_missing(body_lower: str) -> bool:
    english_hint = (
        "model" in body_lower
        and ("not found" in body_lower or "does not exist" in body_lower or "no such model" in body_lower)
    )
    chinese_hint = "模型" in body_lower and any(
        hint in body_lower for hint in ("不存在", "未找到", "无此模型", "不支持所选模型")
    )
    return english_hint or chinese_hint


def classify_status_code(status_code: int, body: str | Mapping[str, Any]) -> str:
    """把 HTTP 状态码 + 响应体分类为 diagnostic_status。"""
    facts = _structured_error_facts(body)
    status_code = facts.upstream_status_code or status_code
    if facts.upstream_status_code:
        code = facts.upstream_error_code
        message = facts.upstream_error_message or facts.upstream_error_detail or ""
    else:
        code = facts.upstream_error_code or facts.code
        message = facts.upstream_error_message or facts.message
    if category := _category_from_code(code):
        return category
    body_lower = f"{code or ''} {message}".lower()
    if any(hint in body_lower for hint in _OFFICIAL_ACCOUNT_HINTS):
        return DIAG_OFFICIAL_ACCOUNT_REQUIRED
    if any(hint in body_lower for hint in _CONTEXT_HINTS):
        return DIAG_CONTEXT_LIMIT
    if any(hint in body_lower for hint in _QUOTA_HINTS):
        return DIAG_QUOTA_EXHAUSTED
    if any(hint in body_lower for hint in _POLICY_HINTS):
        return DIAG_ACCOUNT_POLICY
    if status_code == 401:
        return DIAG_AUTH_FAILED
    if status_code == 403:
        # 403 可能是身份限制，也可能是纯权限问题；命中身份信号才归为 client_rejected。
        if _looks_like_client_rejection(body_lower):
            return DIAG_CLIENT_REJECTED
        return DIAG_PERMISSION_DENIED
    if status_code == 404:
        # 区分"模型不存在"与"协议路径不存在"。
        if _looks_like_model_missing(body_lower):
            return DIAG_MODEL_MISSING
        return DIAG_ENDPOINT_MISSING
    if status_code == 429:
        return DIAG_RATE_LIMITED
    if status_code in (400, 422):
        if _looks_like_model_missing(body_lower):
            return DIAG_MODEL_MISSING
        if _looks_like_client_rejection(body_lower):
            return DIAG_CLIENT_REJECTED
        return DIAG_REQUEST_INVALID
    if status_code == 504:
        return DIAG_TIMEOUT
    if status_code >= 500:
        return DIAG_UPSTREAM_ERROR
    if status_code >= 400:
        return DIAG_PROTOCOL_REJECTED
    return DIAG_HEALTHY


def classify_exception(exc: BaseException) -> str:
    """把 httpx 异常分类为 diagnostic_status。"""
    if isinstance(exc, httpx.TimeoutException):
        return DIAG_TIMEOUT
    if isinstance(exc, httpx.HTTPError):
        return DIAG_NETWORK_ERROR
    if isinstance(exc, OSError):
        return DIAG_NETWORK_ERROR
    if "cancel" in type(exc).__name__.lower():
        return DIAG_CANCELLED
    return DIAG_INVALID_RESPONSE


def scope_for(category: str) -> str:
    if category in {DIAG_RATE_LIMITED, DIAG_TIMEOUT, DIAG_NETWORK_ERROR, DIAG_UPSTREAM_ERROR, DIAG_GATEWAY_UNAVAILABLE, DIAG_GATEWAY_OVERLOADED}:
        return "transient"
    if category in {DIAG_AUTH_FAILED, DIAG_PERMISSION_DENIED, DIAG_CLIENT_REJECTED, DIAG_OFFICIAL_ACCOUNT_REQUIRED, DIAG_MODEL_MISSING, DIAG_ENDPOINT_MISSING, DIAG_QUOTA_EXHAUSTED}:
        return "provider_local"
    if category == DIAG_ACCOUNT_POLICY:
        return "account_policy"
    if category in {DIAG_REQUEST_INVALID, DIAG_CONTEXT_LIMIT}:
        return "request_invalid"
    return "unknown"


def is_retryable(category: str) -> bool:
    return category in {DIAG_RATE_LIMITED, DIAG_TIMEOUT, DIAG_NETWORK_ERROR, DIAG_UPSTREAM_ERROR, DIAG_GATEWAY_UNAVAILABLE, DIAG_GATEWAY_OVERLOADED}


def diagnose_http_error(
    status_code: int,
    body: str | Mapping[str, Any],
    *,
    request_id: str | None = None,
    gateway_stage: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMDiagnostic:
    facts = _structured_error_facts(body)
    effective_status = facts.upstream_status_code or status_code
    category = classify_status_code(effective_status, body)
    upstream_message = facts.upstream_error_message
    summary = upstream_message or facts.upstream_error_detail or facts.message
    exposed_error_code = (
        facts.upstream_error_code
        if facts.upstream_status_code
        else facts.upstream_error_code or facts.code
    )
    return LLMDiagnostic(
        category=category,
        retryable=is_retryable(category),
        scope=scope_for(category),
        safe_message=suggestion_for(category),
        status_code=status_code,
        upstream_status_code=facts.upstream_status_code,
        upstream_error_code=exposed_error_code,
        upstream_error_message=(
            redact(upstream_message, api_key=api_key, base_url=base_url, limit=500)
            if upstream_message
            else None
        ),
        upstream_error_detail=(
            redact(facts.upstream_error_detail, api_key=api_key, base_url=base_url, limit=1000)
            if facts.upstream_error_detail
            else None
        ),
        upstream_request_id=facts.upstream_request_id,
        client_request_id=facts.client_request_id,
        request_id=_safe_request_id(request_id),
        gateway_stage=(gateway_stage or "").strip()[:64] or None,
        upstream_summary=redact(summary, api_key=api_key, base_url=base_url, limit=500) or None,
    )


def diagnose_exception(exc: BaseException, *, request_id: str | None = None, gateway_stage: str | None = None) -> LLMDiagnostic:
    category = classify_exception(exc)
    return LLMDiagnostic(
        category=category,
        retryable=is_retryable(category),
        scope=scope_for(category),
        safe_message=suggestion_for(category),
        request_id=_safe_request_id(request_id),
        gateway_stage=gateway_stage,
        upstream_summary=redact(str(exc)) or None,
    )


def format_diagnostic_error(
    value: LLMDiagnostic | BaseException,
    *,
    fallback: str = "模型请求失败",
) -> str:
    """用已核实的结构化事实生成用户可见错误，不从包装文本猜测原因。"""

    upstream_status = getattr(value, "upstream_status_code", None)
    direct_status = getattr(value, "status_code", None)
    message = (
        getattr(value, "upstream_error_message", None)
        or getattr(value, "upstream_summary", None)
        or (str(value) if isinstance(value, BaseException) else "")
    )
    message = redact(str(message or ""), limit=500).strip()
    if upstream_status:
        return f"上游 HTTP {upstream_status}：{message or fallback}"
    if direct_status:
        return f"HTTP {direct_status}：{message or fallback}"
    return message or fallback


def classify_message(message: str, *, retryable: bool = False) -> str:
    """最后兜底的文本分类；只在没有结构化状态和异常类型时使用。"""

    value = str(message or "")
    lowered = value.lower()
    # 5xx 只能来自 HTTP 状态或结构化 upstream_status_code；包装文本中的
    # “502/503/504”不是可核实事实，不能据此提示临时故障可重试。
    for status in (401, 403, 404, 429):
        if re.search(rf"(?:^|\D){status}(?:\D|$)", value):
            return classify_status_code(status, value)
    if "timeout" in lowered or "timed out" in lowered:
        return DIAG_TIMEOUT
    if retryable or any(token in lowered for token in ("network", "connect", "proxy", "ssl")):
        return DIAG_NETWORK_ERROR
    return DIAG_INVALID_RESPONSE


def is_valid_json(text: str) -> bool:
    """响应体是否为合法 JSON（用于识别非 JSON 响应）。"""
    try:
        json.loads(text)
        return True
    except (ValueError, TypeError):
        return False


def redact(
    text: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    limit: int = 220,
) -> str:
    """脱敏错误文本：剥离凭据、网络位置与已知 Provider 地址并截断。"""
    if not text:
        return ""
    out = text
    if api_key:
        out = out.replace(api_key, "<redacted>")
    if base_url:
        out = out.replace(base_url, "<redacted-url>")
    out = redact_text(out)
    # 错误回显中的测试凭据可能很短，也不能因为未达到生产 Token 的常见长度而泄漏。
    out = re.sub(
        r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._+/=-]{4,}",
        "<redacted-auth>",
        out,
    )
    # 上游 detail 可能回显另一层 Base URL、代理 URL 或带凭据地址。诊断 API
    # 不需要公开网络拓扑；保留错误语义即可。
    out = re.sub(
        r"(?i)\b(?:https?|socks5?|mtproxy)://[^\s\"'<>}\]),;]+",
        "<redacted-url>",
        out,
    )
    return out[: max(1, min(int(limit), 2000))]


__all__ = [
    "ALL_DIAGNOSTIC_STATUSES",
    "DIAG_AUTH_FAILED",
    "DIAG_PERMISSION_DENIED",
    "DIAG_CANCELLED",
    "DIAG_CLIENT_REJECTED",
    "DIAG_OFFICIAL_ACCOUNT_REQUIRED",
    "DIAG_ACCOUNT_POLICY",
    "DIAG_CONFIG_ERROR",
    "DIAG_EMPTY_RESPONSE",
    "DIAG_HEALTHY",
    "DIAG_MODEL_MISSING",
    "DIAG_ENDPOINT_MISSING",
    "DIAG_REQUEST_INVALID",
    "DIAG_CONTEXT_LIMIT",
    "DIAG_QUOTA_EXHAUSTED",
    "DIAG_NETWORK_ERROR",
    "DIAG_PROTOCOL_REJECTED",
    "DIAG_RATE_LIMITED",
    "DIAG_SKIPPED_DISABLED",
    "DIAG_SKIPPED_PROVIDER_MISSING",
    "DIAG_TIMEOUT",
    "DIAG_UPSTREAM_ERROR",
    "DIAG_GATEWAY_UNAVAILABLE",
    "DIAG_GATEWAY_OVERLOADED",
    "DIAG_INVALID_RESPONSE",
    "LLMDiagnostic",
    "classify_exception",
    "classify_message",
    "classify_status_code",
    "diagnose_exception",
    "diagnose_http_error",
    "format_diagnostic_error",
    "is_retryable",
    "is_valid_json",
    "redact",
    "scope_for",
    "suggestion_for",
]
