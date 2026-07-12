"""LLM 诊断状态与错误分类（0.57.0 阶段 B / C 共用）。

把 HTTP 响应、异常与响应体统一分类为 ``diagnostic_status``，供协议检测与
模型测活复用。诊断状态**不等于**数据库启用状态，也不直接改变生产 runtime 健康。

安全红线：所有面向前端 / 插件的错误文本必须脱敏——不得回传 api_key、
Base URL、代理地址或完整敏感响应体。
"""

from __future__ import annotations

import json

import httpx

# ── 诊断状态枚举 ────────────────────────────────────────────
DIAG_HEALTHY = "healthy"
DIAG_EMPTY_RESPONSE = "empty_response"
DIAG_RATE_LIMITED = "rate_limited"
DIAG_AUTH_FAILED = "auth_failed"
DIAG_CLIENT_REJECTED = "client_rejected"
DIAG_PROTOCOL_REJECTED = "protocol_rejected"
DIAG_MODEL_MISSING = "model_missing"
DIAG_TIMEOUT = "timeout"
DIAG_UPSTREAM_ERROR = "upstream_error"
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
    DIAG_CLIENT_REJECTED,
    DIAG_PROTOCOL_REJECTED,
    DIAG_MODEL_MISSING,
    DIAG_TIMEOUT,
    DIAG_UPSTREAM_ERROR,
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
    DIAG_CLIENT_REJECTED: "上游拒绝当前客户端身份；尝试切换该协议支持的客户端身份（如 Codex CLI / Claude Code）。",
    DIAG_PROTOCOL_REJECTED: "该端点不接受此协议的请求格式；确认 Base URL 与 API Format 是否匹配。",
    DIAG_MODEL_MISSING: "模型不存在或当前 Key 无权限；核对模型 ID。",
    DIAG_TIMEOUT: "连接或读取超时；检查网络、代理与上游可用性后重试。",
    DIAG_UPSTREAM_ERROR: "上游服务错误（5xx）；通常为临时故障，可稍后重试。",
    DIAG_CONFIG_ERROR: "Provider 配置不完整（如缺少 API Key）。",
    DIAG_NETWORK_ERROR: "网络异常：无法连接目标域名 / SSL 握手失败 / 代理未生效。",
}


def suggestion_for(status: str) -> str:
    """返回某诊断状态对应的中文修复建议。"""
    return _SUGGESTIONS.get(status, "")


# 明确指向"需要特定客户端身份"的上游信号（小写子串匹配）。
_CLIENT_REJECT_HINTS = (
    "codex",
    "claude code",
    "claude-code",
    "cli client",
    "unsupported client",
    "client not allowed",
    "must use the official",
    "user-agent",
    "x-app",
    "originator",
)


def _looks_like_client_rejection(body_lower: str) -> bool:
    return any(hint in body_lower for hint in _CLIENT_REJECT_HINTS)


def _looks_like_model_missing(body_lower: str) -> bool:
    return (
        "model" in body_lower
        and ("not found" in body_lower or "does not exist" in body_lower or "no such model" in body_lower)
    )


def classify_status_code(status_code: int, body: str) -> str:
    """把 HTTP 状态码 + 响应体分类为 diagnostic_status。"""
    body_lower = (body or "").lower()
    if status_code == 401:
        return DIAG_AUTH_FAILED
    if status_code == 403:
        # 403 可能是身份限制，也可能是纯权限问题；命中身份信号才归为 client_rejected。
        if _looks_like_client_rejection(body_lower):
            return DIAG_CLIENT_REJECTED
        return DIAG_AUTH_FAILED
    if status_code == 404:
        # 区分"模型不存在"与"协议路径不存在"。
        if _looks_like_model_missing(body_lower):
            return DIAG_MODEL_MISSING
        return DIAG_PROTOCOL_REJECTED
    if status_code == 429:
        return DIAG_RATE_LIMITED
    if status_code in (400, 422):
        if _looks_like_model_missing(body_lower):
            return DIAG_MODEL_MISSING
        if _looks_like_client_rejection(body_lower):
            return DIAG_CLIENT_REJECTED
        return DIAG_PROTOCOL_REJECTED
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
    return DIAG_NETWORK_ERROR


def is_valid_json(text: str) -> bool:
    """响应体是否为合法 JSON（用于识别非 JSON 响应）。"""
    try:
        json.loads(text)
        return True
    except (ValueError, TypeError):
        return False


def redact(text: str, *, api_key: str | None = None, base_url: str | None = None) -> str:
    """脱敏错误文本：剥离 api_key / base_url / Bearer / sk- 片段并截断。"""
    if not text:
        return ""
    out = text
    if api_key:
        out = out.replace(api_key, "<redacted-key>")
    if base_url:
        out = out.replace(base_url, "<redacted-url>")
    # 兜底剥离常见密钥前缀（保留少量上下文）。
    import re

    out = re.sub(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+", r"\1<redacted>", out)
    out = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer <redacted>", out)
    return out[:220]


__all__ = [
    "ALL_DIAGNOSTIC_STATUSES",
    "DIAG_AUTH_FAILED",
    "DIAG_CANCELLED",
    "DIAG_CLIENT_REJECTED",
    "DIAG_CONFIG_ERROR",
    "DIAG_EMPTY_RESPONSE",
    "DIAG_HEALTHY",
    "DIAG_MODEL_MISSING",
    "DIAG_NETWORK_ERROR",
    "DIAG_PROTOCOL_REJECTED",
    "DIAG_RATE_LIMITED",
    "DIAG_SKIPPED_DISABLED",
    "DIAG_SKIPPED_PROVIDER_MISSING",
    "DIAG_TIMEOUT",
    "DIAG_UPSTREAM_ERROR",
    "classify_exception",
    "classify_status_code",
    "is_valid_json",
    "redact",
    "suggestion_for",
]
