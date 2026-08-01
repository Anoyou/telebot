"""System Agent 对 Provider 的 quick verify 封装（不落库）。"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...crypto import decrypt_str
from ...db.models.command import LLMProvider
from ...llm_probe_defaults import (
    QUICK_VERIFY_MAX_TOKENS,
    QUICK_VERIFY_MESSAGE,
    QUICK_VERIFY_SYSTEM_PROMPT,
)
from ...services import llm_quick_verify
from ...services.llm_request_headers import decrypt_request_headers
from .registry import ActionKeepPendingError

_HTTP_STATUS_RE = re.compile(r"接口返回\s+(\d{3})\b")


def _verification_failure(
    message: str,
    *,
    using_saved_key: bool,
    retain_temporary_key: bool,
) -> ActionKeepPendingError:
    match = _HTTP_STATUS_RE.search(message)
    status_code = int(match.group(1)) if match else None
    if status_code in {401, 403}:
        return ActionKeepPendingError(
            f"Provider 验证失败：{message}。鉴权失败；如需更换密钥，请重新输入后再确认。已保存的 Provider 配置未修改。",
            code="API_KEY_REJECTED",
            clear_secret_names=("api_key",),
        )
    if using_saved_key:
        suffix = "已保存的 Provider 配置未修改，无需重新输入 API Key；请根据上游错误调整模型或协议，或稍后重试。"
    elif retain_temporary_key:
        suffix = "本操作的临时密钥仍在 Action 有效期内加密暂存；上游错误不代表密钥无效，可稍后直接再次确认。"
    else:
        suffix = "本次测活未创建待确认操作且密钥未落库；上游错误不代表密钥无效，重新发起测活时需再次提供。"
    return ActionKeepPendingError(
        f"Provider 验证失败：{message}。{suffix}",
        code="PROVIDER_VERIFY_FAILED",
    )


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
    proxy_url: str | None = None,
    timeout_seconds: int = 45,
    using_saved_key: bool = False,
    retain_temporary_key: bool = False,
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
            clear_secret_names=("api_key",),
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
        proxy_url=proxy_url,
        model=model,
        reasoning_effort=None,
        system_prompt=QUICK_VERIFY_SYSTEM_PROMPT,
        message=QUICK_VERIFY_MESSAGE,
        max_tokens=QUICK_VERIFY_MAX_TOKENS,
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
            raise _verification_failure(
                last_error,
                using_saved_key=using_saved_key,
                retain_temporary_key=retain_temporary_key,
            )
        if et == "done" and event.get("ok"):
            final = {
                "ok": True,
                "model": event.get("model"),
                "requested_model": event.get("requested_model") or event.get("model"),
                "latency_ms": event.get("latency_ms"),
                "api_format": event.get("api_format") or fmt,
                "base_url": event.get("base_url") or url,
                "provider": event.get("provider") or provider,
                "suggested_name": event.get("suggested_name"),
                "models": list(event.get("models") or [])[:100],
                "response_preview": str(event.get("response") or "")[:120],
                "business_changed": False,
            }
    if final is None:
        raise _verification_failure(
            last_error,
            using_saved_key=using_saved_key,
            retain_temporary_key=retain_temporary_key,
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
        "protocol_profile": args.get("protocol_profile"),
        "client_identity_profile": args.get("client_identity_profile"),
        "request_headers": args.get("request_headers"),
        "proxy_id": args.get("proxy_id"),
    }
    if provider_id in (None, ""):
        base["protocol_profile"] = base.get("protocol_profile") or "standard"
        base["client_identity_profile"] = base.get("client_identity_profile") or "auto"
        if bool(args.get("clear_proxy")):
            base["proxy_id"] = None
        from ...services.llm_proxy_service import resolve_proxy_url

        base["proxy_url"] = await resolve_proxy_url(db, base.get("proxy_id"))
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
    if "proxy_id" not in args and not bool(args.get("clear_proxy")):
        base["proxy_id"] = getattr(row, "proxy_id", None)
    elif bool(args.get("clear_proxy")):
        base["proxy_id"] = None
    if not base.get("api_key") and row.api_key_enc:
        try:
            base["api_key"] = decrypt_str(row.api_key_enc)
        except Exception:  # noqa: BLE001
            raise ActionKeepPendingError(
                "已保存的 API Key 无法解密，请重新输入密钥。",
                code="API_KEY_DECRYPT_FAILED",
                clear_secret_names=("api_key",),
            ) from None
    if not base.get("protocol_profile"):
        base["protocol_profile"] = getattr(row, "protocol_profile", None) or "standard"
    if not base.get("client_identity_profile"):
        base["client_identity_profile"] = getattr(row, "client_identity_profile", None) or "auto"
    if base.get("request_headers") is None:
        try:
            base["request_headers"] = decrypt_request_headers(
                getattr(row, "request_headers_enc", None)
            )
        except Exception:  # noqa: BLE001
            raise ActionKeepPendingError(
                "已保存的 Provider 兼容请求头无法解密，请在 Web 配置中重新保存。",
                code="REQUEST_HEADERS_DECRYPT_FAILED",
            ) from None
    from ...services.llm_proxy_service import resolve_proxy_url

    base["proxy_url"] = await resolve_proxy_url(db, base.get("proxy_id"))
    return base


__all__ = [
    "resolve_provider_verify_args",
    "run_quick_verify",
]
