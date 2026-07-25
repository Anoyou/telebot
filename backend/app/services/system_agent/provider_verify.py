"""System Agent 对 Provider 的 quick verify 封装（不落库）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...crypto import decrypt_str
from ...db.models.command import LLMProvider
from ...services import llm_quick_verify
from ...services.llm_request_headers import decrypt_request_headers
from .registry import ActionKeepPendingError


async def run_quick_verify(
    *,
    base_url: str | None,
    api_key: str | None,
    api_format: str | None,
    default_model: str | None,
    provider: str | None = None,
    protocol_profile: str = "standard",
    client_identity_profile: str = "auto",
    request_headers: list[object] | None = None,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    """执行一次真实上游验证，返回摘要；失败抛 ActionKeepPendingError。"""

    key = str(api_key or "").strip()
    fmt = str(api_format or "chat_completions").strip() or "chat_completions"
    model = str(default_model or "").strip() or None
    url = llm_quick_verify.normalize_quick_verify_base_url(str(base_url or "").strip())
    is_ollama = str(provider or "").lower() == "ollama" or "ollama" in (url or "").lower()

    if not key and not is_ollama:
        raise ActionKeepPendingError(
            "缺少 API Key。请在聊天中重新发送密钥，或在 Web Action 卡片补填后再确认。",
            code="API_KEY_REQUIRED",
        )
    if not url and not is_ollama:
        # 允许部分官方默认；quick_verify 仍需要 base_url
        if str(provider or "") == "openai":
            url = "https://api.openai.com/v1"
        elif str(provider or "") == "anthropic":
            url = "https://api.anthropic.com"
        else:
            raise ActionKeepPendingError(
                "缺少 Base URL，请补充后再确认。",
                code="BASE_URL_REQUIRED",
            )

    last_error = "验证失败"
    final: dict[str, Any] | None = None
    async for event in llm_quick_verify.quick_verify_events(
        base_url=url or "",
        api_key=key or "ollama",
        api_format=fmt,
        protocol_profile=protocol_profile or "standard",
        client_identity_profile=client_identity_profile or "auto",
        request_headers=request_headers,
        proxy_url=None,
        model=model,
        reasoning_effort=None,
        system_prompt="You are a connectivity probe. Reply with a short OK.",
        message="ping",
        max_tokens=32,
        timeout_seconds=timeout_seconds,
    ):
        if not isinstance(event, dict):
            continue
        et = event.get("type")
        if et == "error":
            last_error = str(event.get("error") or "上游验证失败")
            # 确保错误里不带回 key
            if key and key in last_error:
                last_error = last_error.replace(key, "[REDACTED]")
            raise ActionKeepPendingError(
                f"Provider 验证失败：{last_error}。密钥已清除，请重新输入后再确认。",
                code="PROVIDER_VERIFY_FAILED",
            )
        if et == "done" and event.get("ok"):
            final = {
                "ok": True,
                "model": event.get("model"),
                "latency_ms": event.get("latency_ms"),
                "api_format": event.get("api_format") or fmt,
                "base_url": event.get("base_url") or url,
                "provider": event.get("provider") or provider,
                "response_preview": str(event.get("response") or "")[:120],
                "business_changed": False,
            }
    if final is None:
        raise ActionKeepPendingError(
            f"Provider 验证失败：{last_error}。密钥已清除，请重新输入后再确认。",
            code="PROVIDER_VERIFY_FAILED",
        )
    return final


async def resolve_provider_verify_args(
    db: AsyncSession,
    args: dict[str, Any],
) -> dict[str, Any]:
    """合并已有 Provider 与参数，得到 verify 所需字段。"""

    provider_id = args.get("id") or args.get("provider_id")
    base: dict[str, Any] = {
        "provider": args.get("provider"),
        "base_url": args.get("base_url"),
        "default_model": args.get("default_model") or args.get("model"),
        "api_format": args.get("api_format"),
        "api_key": args.get("api_key"),
        "protocol_profile": args.get("protocol_profile") or "standard",
        "client_identity_profile": args.get("client_identity_profile") or "auto",
        "request_headers": None,
    }
    if provider_id in (None, ""):
        return base
    row = await db.get(LLMProvider, int(provider_id))
    if row is None:
        raise ActionKeepPendingError(f"Provider #{provider_id} 不存在", code="NOT_FOUND")
    if not base.get("provider"):
        base["provider"] = row.provider
    if not base.get("base_url"):
        base["base_url"] = row.base_url
    if not base.get("default_model"):
        base["default_model"] = row.default_model
    if not base.get("api_format"):
        base["api_format"] = row.api_format
    if not base.get("api_key") and row.api_key_enc:
        try:
            base["api_key"] = decrypt_str(row.api_key_enc)
        except Exception:  # noqa: BLE001
            raise ActionKeepPendingError(
                "已保存的 API Key 无法解密，请重新输入密钥。",
                code="API_KEY_DECRYPT_FAILED",
            ) from None
    if not base.get("protocol_profile"):
        base["protocol_profile"] = getattr(row, "protocol_profile", None) or "standard"
    if not base.get("client_identity_profile"):
        base["client_identity_profile"] = getattr(row, "client_identity_profile", None) or "auto"
    try:
        base["request_headers"] = decrypt_request_headers(getattr(row, "request_headers_enc", None))
    except Exception:  # noqa: BLE001
        raise ActionKeepPendingError(
            "已保存的 Provider 兼容请求头无法解密，请在 Web 配置中重新保存。",
            code="REQUEST_HEADERS_DECRYPT_FAILED",
        ) from None
    return base


__all__ = [
    "resolve_provider_verify_args",
    "run_quick_verify",
]
