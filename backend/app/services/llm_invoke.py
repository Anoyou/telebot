"""Unified helper for standard LLM invocations."""
from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..db.models.command import (
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
    LLM_PROVIDER_OPENAI,
    LLM_WEB_SEARCH_API_FORMAT_AUTO,
)
from . import llm_account_budget, llm_client, llm_runtime
from .llm_client import LLMCallFailed, LLMResult
from .llm_dto import LLMProviderDTO
from .llm_runtime import (
    UsageRecord,
    build_fallback_chain,
    call_with_fallback,
    preview_text_for_usage,
)


async def invoke(
    primary_provider: LLMProviderDTO,
    providers: dict[int, LLMProviderDTO],
    system: str,
    user: str,
    *,
    override_model: str | None = None,
    max_tokens: int = 512,
    images: list[bytes] | None = None,
    web_search: bool = False,
    web_search_context_size: str | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int | None = None,
    native_image: bool = False,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
    source: str | None = None,
    fallback_provider_id: int | None = None,
    matched_tag: str | None = None,
    client_factory: Callable[..., Any | Awaitable[Any]] | None = None,
) -> tuple[LLMResult, LLMProviderDTO, bool]:
    """Call a standard LLM provider with shared fallback / retry / usage logic."""

    chain = build_fallback_chain(
        primary_provider,
        providers=providers,
        fallback_provider_id=fallback_provider_id,
        matched_tag=matched_tag,
    )

    def _build_runtime_client(
        provider_dto: LLMProviderDTO,
        *,
        override_model: str | None = None,
        proxy_url: str | None = None,
    ):
        api_format_override = _api_format_for_call(
            provider_dto,
            web_search=web_search,
            native_image=native_image,
            override_model=override_model,
        )
        if client_factory is not None:
            kwargs = {
                "override_model": override_model,
                "proxy_url": proxy_url or provider_dto.proxy_url,
            }
            if _accepts_kwarg(client_factory, "api_format_override"):
                kwargs["api_format_override"] = api_format_override
            return client_factory(provider_dto, **kwargs)
        kwargs = {
            "override_model": override_model,
            "proxy_url": proxy_url or provider_dto.proxy_url,
        }
        if _accepts_kwarg(llm_client.build_client, "api_format_override"):
            kwargs["api_format_override"] = api_format_override
        return llm_client.build_client(provider_dto, **kwargs)

    return await call_with_fallback(
        chain,
        system,
        user,
        override_model=override_model,
        max_tokens=max_tokens,
        images=images,
        web_search=web_search,
        web_search_context_size=web_search_context_size,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        native_image=native_image,
        client_factory=_build_runtime_client,
        account_id=account_id,
        triggered_by_account_id=triggered_by_account_id,
        source=source,
    )


async def transcribe(
    provider: LLMProviderDTO,
    audio: bytes,
    *,
    model: str,
    override_model: str | None = None,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
    source: str = "stt",
    client_factory: Callable[..., Any | Awaitable[Any]] | None = None,
) -> str:
    """Transcribe audio with shared usage and account-budget accounting.

    STT providers do not return token counts. We reserve a conservative token
    estimate from audio bytes before the call, then settle with the same audio
    estimate plus an approximate token count for the transcript text. This keeps
    request/minute/premium budget gates strict while making the daily-token
    dimension a best-effort approximation for audio work.
    """

    started = time.monotonic()
    estimated_input_tokens = _estimate_audio_token_units(audio)
    try:
        ticket = await llm_account_budget.acquire(account_id, provider, estimated_input_tokens)
    except llm_account_budget.LLMAccountBudgetExceeded as exc:
        await _emit_transcribe_usage(
            provider=provider,
            model=model,
            account_id=account_id,
            triggered_by_account_id=triggered_by_account_id,
            source=source,
            started=started,
            input_tokens=estimated_input_tokens,
            success=False,
            error_type="budget_exceeded",
            audio_bytes=len(audio or b""),
        )
        raise LLMCallFailed(
            str(exc),
            provider_id=provider.id,
            provider_name=provider.name,
            error_type="budget_exceeded",
            retryable=False,
        ) from exc

    try:
        builder = client_factory or llm_client.build_client
        client = builder(
            provider,
            override_model=override_model,
            proxy_url=provider.proxy_url,
        )
        if inspect.isawaitable(client):
            client = await client
        text = await client.transcribe(audio, model=model)
    except Exception as exc:
        error_type = "unsupported" if isinstance(exc, NotImplementedError) else _classify_transcribe_error(exc)
        await _emit_transcribe_usage(
            provider=provider,
            model=model,
            account_id=account_id,
            triggered_by_account_id=triggered_by_account_id,
            source=source,
            started=started,
            input_tokens=estimated_input_tokens,
            success=False,
            error_type=error_type,
            audio_bytes=len(audio or b""),
        )
        await llm_account_budget.settle(
            ticket,
            actual_tokens=0,
            actual_provider=None,
            success=False,
        )
        raise

    output_tokens = _estimate_text_token_units(text)
    await _emit_transcribe_usage(
        provider=provider,
        model=model,
        account_id=account_id,
        triggered_by_account_id=triggered_by_account_id,
        source=source,
        started=started,
        input_tokens=estimated_input_tokens,
        output_tokens=output_tokens,
        success=True,
        audio_bytes=len(audio or b""),
        response_text=text,
    )
    await llm_account_budget.settle(
        ticket,
        actual_tokens=estimated_input_tokens + output_tokens,
        actual_provider=provider,
        success=True,
    )
    return text


async def _emit_transcribe_usage(
    *,
    provider: LLMProviderDTO,
    model: str,
    account_id: int | None,
    triggered_by_account_id: int | None,
    source: str,
    started: float,
    input_tokens: int,
    success: bool,
    audio_bytes: int,
    output_tokens: int = 0,
    error_type: str | None = None,
    response_text: str | None = None,
) -> None:
    request_preview = preview_text_for_usage(
        f"stt model={model} audio_bytes={max(0, int(audio_bytes or 0))}"
    )
    await llm_runtime._emit_usage(
        UsageRecord(
            provider_id=provider.id,
            account_id=account_id,
            triggered_by_account_id=triggered_by_account_id,
            provider_name=provider.name,
            model=model or provider.default_model,
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            success=success,
            error_type=error_type,
            source=source,
            used_fallback=False,
            fallback_chain=[provider.name],
            request_preview=request_preview,
            response_preview=preview_text_for_usage(response_text),
        )
    )


def _estimate_audio_token_units(audio: bytes) -> int:
    # Audio byte size is only a proxy for STT cost; keep a floor so request
    # budget accounting never becomes a zero-token no-op.
    return max(64, int(len(audio or b"") / 1024) + 1)


def _estimate_text_token_units(text: str | None) -> int:
    return max(1, int(len(text or "") / 4) + 1) if text else 0


def _classify_transcribe_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "timeout" in msg:
        return "timeout"
    if "429" in msg or "限流" in msg:
        return "rate_limit"
    if "401" in msg or "403" in msg or "auth" in msg or "unauthorized" in msg:
        return "auth"
    if "connect" in msg or "network" in msg or "proxy" in msg:
        return "network"
    return type(exc).__name__.lower()


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return name in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def _api_format_for_call(
    provider: LLMProviderDTO,
    *,
    web_search: bool,
    native_image: bool = False,
    override_model: str | None = None,
) -> str | None:
    """Return a per-call API format override.

    Default chat can stay on /chat/completions while web-search calls switch to
    /responses for OpenAI-compatible providers that support both protocols.
    """
    if native_image:
        current = (provider.api_format or "").strip().lower()
        model = (override_model or provider.default_model or "").strip().lower()
        if (
            provider.provider.lower() == LLM_PROVIDER_OPENAI
            and current == LLM_API_FORMAT_CHAT_COMPLETIONS
            and not _is_images_api_model(model)
        ):
            return LLM_API_FORMAT_RESPONSES

    if not web_search:
        return None

    configured = (provider.web_search_api_format or LLM_WEB_SEARCH_API_FORMAT_AUTO).strip().lower()
    if configured and configured != LLM_WEB_SEARCH_API_FORMAT_AUTO:
        return configured

    current = (provider.api_format or "").strip().lower()
    if provider.provider.lower() == LLM_PROVIDER_OPENAI and current == LLM_API_FORMAT_CHAT_COMPLETIONS:
        return LLM_API_FORMAT_RESPONSES
    return None


def resolved_api_format_for_call(provider: LLMProviderDTO, *, web_search: bool) -> str:
    """Return the effective API format after per-call overrides are applied."""
    override = _api_format_for_call(provider, web_search=web_search)
    if override:
        return override
    configured = (provider.api_format or "").strip().lower()
    if configured:
        return configured
    from ..db.models.command import default_api_format_for

    return default_api_format_for(provider.provider)


def _is_images_api_model(model: str) -> bool:
    """True when the model should be sent to OpenAI-compatible Images API."""
    normalized = model.strip().lower()
    return normalized.startswith("gpt-image-") or normalized.startswith("dall-e-")
