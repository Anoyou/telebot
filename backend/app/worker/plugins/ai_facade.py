"""Safe text-completion facade exposed to plugins as ``ctx.ai``.

The facade is intentionally thin: provider selection, token clamping and
metadata redaction live here, while the actual model invocation reuses the
shared LLM runtime so fallback, retries, usage logging and account budgets stay
consistent with first-party AI commands.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ...crypto import decrypt_str
from ...db.base import AsyncSessionLocal
from ...db.models.account import Proxy
from ...db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_RESPONSES,
    LLMProvider,
    default_api_format_for,
)
from ...services import llm_account_budget, llm_runtime, plugin_ai_quota
from ...services.ai_feature import is_ai_enabled
from ...services.llm_client import (
    LLMCallFailed,
    LLMError,
    LLMResult,
)
from ...services.llm_client import (
    build_client_from_dto as build_llm_client,
)
from ...services.llm_dto import LLMProviderDTO
from ...services.llm_invoke import invoke as invoke_ai_runtime
from ...settings import settings

ProviderLoader = Callable[[], Awaitable[Mapping[int, LLMProviderDTO]]]

DEFAULT_PLUGIN_AI_MAX_TOKENS = 4096
DEFAULT_PLUGIN_AI_TIMEOUT_SECONDS = 600
_SAFE_MODEL_KEYS = frozenset(
    {
        "id",
        "name",
        "label",
        "display_name",
        "modality",
        "max_tokens",
        "context_window",
    }
)


class PluginAIError(RuntimeError):
    """Base class for plugin AI facade failures."""


class AIUnavailableError(PluginAIError):
    """Raised when no provider is available or the LLM runtime fails."""


class AIQuotaError(PluginAIError):
    """Raised when account/plugin LLM quota or provider rate limits block a call."""


@dataclass(frozen=True)
class AIResult:
    """Desensitized result returned by ``PluginAI.complete``."""

    text: str
    model: str
    provider_id: int
    provider_name: str
    used_fallback: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AIProviderInfo:
    """Desensitized provider metadata returned by ``PluginAI.list_providers``."""

    id: int
    name: str
    provider: str
    default_model: str
    api_format: str | None = None
    modality: str = "text"
    tags: list[str] = field(default_factory=list)
    cost_tier: int = 2
    models: list[dict[str, Any]] = field(default_factory=list)
    has_api_key: bool = False


class PluginAI:
    """MVP plugin AI facade for safe text completion."""

    def __init__(
        self,
        *,
        account_id: int | None,
        plugin_key: str,
        provider_loader: ProviderLoader | None = None,
        max_tokens_limit: int | None = None,
        timeout_limit_seconds: int | None = None,
    ) -> None:
        self.account_id = account_id
        self.plugin_key = plugin_key
        self._provider_loader = provider_loader or load_llm_providers
        self.max_tokens_limit = _positive_int(
            max_tokens_limit,
            _positive_int(
                getattr(settings, "plugin_ai_max_output_tokens", None),
                DEFAULT_PLUGIN_AI_MAX_TOKENS,
            ),
        )
        self.timeout_limit_seconds = _positive_int(
            timeout_limit_seconds,
            _positive_int(
                getattr(settings, "plugin_ai_timeout_seconds", None),
                DEFAULT_PLUGIN_AI_TIMEOUT_SECONDS,
            ),
        )

    @classmethod
    def from_context(cls, ctx: Any) -> PluginAI:
        """Build the facade from a ``PluginContext``-like object."""

        return cls(
            account_id=getattr(ctx, "account_id", None),
            plugin_key=str(getattr(ctx, "feature_key", "") or "unknown"),
        )

    async def list_providers(self) -> list[AIProviderInfo]:
        """Return providers without encrypted API keys, proxy URLs or base URLs."""

        providers = await self._load_providers()
        return [_provider_info(dto) for dto in sorted(providers.values(), key=lambda p: p.id)]

    async def complete(
        self,
        system: str,
        user: str,
        *,
        provider: int | str | None = None,
        provider_tag: str | None = None,
        tag: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        model: str | None = None,
        override_model: str | None = None,
        max_tokens: int = 512,
        timeout: int = DEFAULT_PLUGIN_AI_TIMEOUT_SECONDS,
        timeout_seconds: int | None = None,
        **_ignored: Any,
    ) -> AIResult:
        """Call a text LLM through TelePilot's shared LLM runtime.

        ``provider`` accepts an id or provider name. ``provider_tag`` /
        ``tag`` / first ``tags`` item select the cheapest usable provider with
        that tag.
        """

        if tag is not None or tags:
            import warnings

            warnings.warn(
                "ctx.ai.complete tag/tags 是兼容别名，新模块请使用 provider_tag",
                DeprecationWarning,
                stacklevel=2,
            )
        system_prompt = str(system or "")
        user_prompt = str(user or "")
        if not system_prompt.strip() and not user_prompt.strip():
            raise AIUnavailableError("ctx.ai.complete 需要 system 或 user 内容")

        providers = await self._load_providers()
        selected_tag = provider_tag or tag
        if selected_tag is None and tags:
            selected_tag = str(tags[0]) if tags[0] else None
        primary, matched_tag = _select_provider(
            providers,
            provider=provider,
            provider_tag=selected_tag,
        )
        clamped_tokens = self._clamp_max_tokens(max_tokens)
        clamped_timeout = self._clamp_timeout(timeout_seconds if timeout_seconds is not None else timeout)
        selected_model = str(model or override_model or "").strip() or None
        quota_ticket: plugin_ai_quota.PluginAIQuotaTicket | None = None
        try:
            estimated_tokens = _estimate_total_tokens(system_prompt, user_prompt, clamped_tokens)
            quota_ticket = await plugin_ai_quota.acquire(
                self.plugin_key,
                self.account_id,
                estimated_tokens=estimated_tokens,
            )
            # The shared runtime enforces account budgets and records actual usage.
            result, used_provider, used_fallback = await invoke_ai_runtime(
                primary,
                providers,
                system_prompt,
                user_prompt,
                override_model=selected_model,
                max_tokens=clamped_tokens,
                timeout_seconds=clamped_timeout,
                account_id=self.account_id,
                source=f"plugin:{self.plugin_key}",
                matched_tag=matched_tag,
            )
            await plugin_ai_quota.release(
                quota_ticket,
                int(result.input_tokens or 0) + int(result.output_tokens or 0),
            )
        except LLMCallFailed as exc:
            await plugin_ai_quota.release(quota_ticket, 0)
            raise _facade_error_from_llm_call(exc) from exc
        # acquire() 抛 PluginAIQuotaExceeded 时 ticket 仍为 None，Redis 计数也未 ZADD，无需 release
        except plugin_ai_quota.PluginAIQuotaExceeded as exc:
            raise AIQuotaError(str(exc)) from exc
        except (LLMError, ValueError) as exc:
            await plugin_ai_quota.release(quota_ticket, 0)
            raise AIUnavailableError(str(exc)) from exc
        except Exception:
            await plugin_ai_quota.release(quota_ticket, 0)
            raise

        return _result_from_llm(result, used_provider, used_fallback)

    async def stream_complete(
        self,
        system: str,
        user: str,
        *,
        provider: int | str | None = None,
        provider_tag: str | None = None,
        tag: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        model: str | None = None,
        override_model: str | None = None,
        max_tokens: int = 512,
        timeout: int = DEFAULT_PLUGIN_AI_TIMEOUT_SECONDS,
        timeout_seconds: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        **_ignored: Any,
    ) -> AsyncIterator[str]:
        """Yield text deltas from providers with native streaming support.

        The plugin-facing contract is intentionally narrow: each yielded item
        is a text delta string. Provider fallback is not attempted mid-stream,
        because partial text may already have been delivered to the plugin.
        """

        if tag is not None or tags:
            import warnings

            warnings.warn(
                "ctx.ai.stream_complete tag/tags 是兼容别名，新模块请使用 provider_tag",
                DeprecationWarning,
                stacklevel=2,
            )
        system_prompt = str(system or "")
        user_prompt = str(user or "")
        if not system_prompt.strip() and not user_prompt.strip():
            raise AIUnavailableError("ctx.ai.stream_complete 需要 system 或 user 内容")

        providers = await self._load_providers()
        selected_tag = provider_tag or tag
        if selected_tag is None and tags:
            selected_tag = str(tags[0]) if tags[0] else None
        primary, _matched_tag = _select_provider(
            providers,
            provider=provider,
            provider_tag=selected_tag,
        )
        api_format = _effective_api_format(primary)
        if api_format not in {LLM_API_FORMAT_RESPONSES, LLM_API_FORMAT_ANTHROPIC_MESSAGES}:
            raise AIUnavailableError(
                f"provider {primary.name} 暂不支持 streaming；请使用 responses 或 anthropic_messages provider"
            )

        clamped_tokens = self._clamp_max_tokens(max_tokens)
        clamped_timeout = self._clamp_timeout(timeout_seconds if timeout_seconds is not None else timeout)
        selected_model = str(model or override_model or "").strip() or None
        quota_ticket: plugin_ai_quota.PluginAIQuotaTicket | None = None
        budget_ticket: llm_account_budget.LLMAccountBudgetTicket | None = None
        quota_settled = False
        budget_settled = False
        actual_tokens = 0
        final_input_tokens = 0
        final_output_tokens = 0
        final_model = selected_model or primary.default_model
        response_preview_parts: list[str] = []
        response_preview_chars = 0
        started_at = time.monotonic()
        try:
            estimated_tokens = _estimate_total_tokens(system_prompt, user_prompt, clamped_tokens)
            quota_ticket = await plugin_ai_quota.acquire(
                self.plugin_key,
                self.account_id,
                estimated_tokens=estimated_tokens,
            )
            budget_ticket = await llm_account_budget.acquire(
                self.account_id,
                primary,
                estimated_tokens,
            )
            client = build_llm_client(
                primary,
                override_model=selected_model,
                proxy_url=primary.proxy_url,
                api_format_override=None,
            )
            if inspect.isawaitable(client):
                client = await client
            async for chunk in client.stream_complete(
                system_prompt,
                user_prompt,
                max_tokens=clamped_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                timeout_seconds=clamped_timeout,
            ):
                if getattr(chunk, "done", False):
                    final_input_tokens = int(getattr(chunk, "input_tokens", None) or 0)
                    final_output_tokens = int(getattr(chunk, "output_tokens", None) or 0)
                    final_model = str(getattr(chunk, "model", None) or final_model or "")
                    continue
                final_model = str(getattr(chunk, "model", None) or final_model or "")
                delta = str(getattr(chunk, "delta", "") or "")
                if delta:
                    if response_preview_chars < 2000:
                        response_preview_parts.append(delta[: max(0, 2000 - response_preview_chars)])
                        response_preview_chars += len(delta)
                    yield delta
            actual_tokens = final_input_tokens + final_output_tokens
            if actual_tokens <= 0 and quota_ticket is not None:
                actual_tokens = int(quota_ticket.estimated_tokens or 0)
                final_input_tokens = actual_tokens
                final_output_tokens = 0
            await llm_account_budget.settle(
                budget_ticket,
                actual_tokens=actual_tokens,
                actual_provider=primary,
                success=True,
            )
            budget_settled = True
            await plugin_ai_quota.release(quota_ticket, actual_tokens)
            quota_settled = True
            await _emit_stream_usage(
                account_id=self.account_id,
                plugin_key=self.plugin_key,
                provider=primary,
                model=final_model,
                input_tokens=final_input_tokens,
                output_tokens=final_output_tokens,
                success=True,
                started_at=started_at,
                request_preview=llm_runtime.request_preview_for_usage(system_prompt, user_prompt),
                response_preview=llm_runtime.preview_text_for_usage("".join(response_preview_parts)),
            )
        except plugin_ai_quota.PluginAIQuotaExceeded as exc:
            raise AIQuotaError(str(exc)) from exc
        except llm_account_budget.LLMAccountBudgetExceeded as exc:
            await plugin_ai_quota.release(quota_ticket, 0)
            quota_settled = True
            await _emit_stream_usage(
                account_id=self.account_id,
                plugin_key=self.plugin_key,
                provider=primary,
                model=final_model,
                success=False,
                error_type="budget_exceeded",
                started_at=started_at,
                request_preview=llm_runtime.request_preview_for_usage(system_prompt, user_prompt),
            )
            raise AIQuotaError(str(exc)) from exc
        except (LLMError, ValueError, NotImplementedError) as exc:
            await llm_account_budget.settle(
                budget_ticket,
                actual_tokens=0,
                actual_provider=None,
                success=False,
            )
            budget_settled = True
            await plugin_ai_quota.release(quota_ticket, 0)
            quota_settled = True
            await _emit_stream_usage(
                account_id=self.account_id,
                plugin_key=self.plugin_key,
                provider=primary,
                model=final_model,
                success=False,
                error_type=type(exc).__name__,
                started_at=started_at,
                request_preview=llm_runtime.request_preview_for_usage(system_prompt, user_prompt),
                response_preview=llm_runtime.preview_text_for_usage("".join(response_preview_parts)),
            )
            raise AIUnavailableError(str(exc)) from exc
        except Exception:
            await llm_account_budget.settle(
                budget_ticket,
                actual_tokens=0,
                actual_provider=None,
                success=False,
            )
            budget_settled = True
            await plugin_ai_quota.release(quota_ticket, 0)
            quota_settled = True
            await _emit_stream_usage(
                account_id=self.account_id,
                plugin_key=self.plugin_key,
                provider=primary,
                model=final_model,
                success=False,
                error_type="unexpected_error",
                started_at=started_at,
                request_preview=llm_runtime.request_preview_for_usage(system_prompt, user_prompt),
                response_preview=llm_runtime.preview_text_for_usage("".join(response_preview_parts)),
            )
            raise
        finally:
            if budget_ticket is not None and not budget_settled:
                await llm_account_budget.settle(
                    budget_ticket,
                    actual_tokens=0,
                    actual_provider=None,
                    success=False,
                )
            if quota_ticket is not None and not quota_settled:
                await plugin_ai_quota.release(quota_ticket, 0)

    async def _load_providers(self) -> dict[int, LLMProviderDTO]:
        if not await is_ai_enabled():
            raise AIUnavailableError("AI 能力已在系统设置中关闭")
        providers = dict(await self._provider_loader())
        if not providers:
            raise AIUnavailableError("没有可用的 LLM provider")
        return providers

    def _clamp_max_tokens(self, value: int) -> int:
        requested = _positive_int(value, 512)
        return max(1, min(requested, self.max_tokens_limit))

    def _clamp_timeout(self, value: int) -> int:
        requested = _positive_int(value, self.timeout_limit_seconds)
        return max(1, min(requested, self.timeout_limit_seconds))


async def load_llm_providers() -> dict[int, LLMProviderDTO]:
    """Load provider DTOs from DB without exposing decrypted keys to plugins."""

    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(select(LLMProvider))).scalars().all())
        proxy_ids = {int(row.proxy_id) for row in rows if getattr(row, "proxy_id", None) is not None}
        proxies: dict[int, Proxy] = {}
        if proxy_ids:
            proxy_rows = list(
                (await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))).scalars().all()
            )
            proxies = {int(row.id): row for row in proxy_rows}

    providers: dict[int, LLMProviderDTO] = {}
    for row in rows:
        dto = LLMProviderDTO.from_orm_row(row)
        proxy_id = getattr(row, "proxy_id", None)
        if proxy_id is not None:
            dto.proxy_url = _proxy_url_from_row(proxies.get(int(proxy_id)))
        providers[int(dto.id)] = dto
    return providers


def _select_provider(
    providers: Mapping[int, LLMProviderDTO],
    *,
    provider: int | str | None,
    provider_tag: str | None,
) -> tuple[LLMProviderDTO, str | None]:
    usable = [p for p in providers.values() if p.has_api_key]
    if not usable:
        raise AIUnavailableError("没有已配置 API key 的 LLM provider")

    if provider is not None:
        selected = _find_provider(usable, provider)
        if selected is None:
            raise AIUnavailableError(f"找不到可用 provider: {provider}")
        return selected, None

    tag = str(provider_tag or "").strip()
    if tag:
        tagged = [p for p in usable if tag in set(p.tags or [])]
        if not tagged:
            raise AIUnavailableError(f"找不到带有 tag={tag} 的可用 provider")
        tagged.sort(key=lambda p: (p.cost_tier, p.id))
        return tagged[0], tag

    chat = [p for p in usable if "chat" in set(p.tags or [])]
    pool = chat or usable
    pool.sort(key=lambda p: (p.cost_tier, p.id))
    return pool[0], "chat" if chat else None


def _find_provider(providers: list[LLMProviderDTO], provider: int | str) -> LLMProviderDTO | None:
    raw = str(provider).strip()
    if raw.isdigit():
        pid = int(raw)
        for item in providers:
            if item.id == pid:
                return item
    lowered = raw.lower()
    for item in providers:
        if item.name.lower() == lowered:
            return item
    return None


def _provider_info(dto: LLMProviderDTO) -> AIProviderInfo:
    return AIProviderInfo(
        id=dto.id,
        name=dto.name,
        provider=dto.provider,
        default_model=dto.default_model,
        api_format=dto.api_format,
        modality=dto.modality,
        tags=list(dto.tags or []),
        cost_tier=int(dto.cost_tier or 2),
        models=[_safe_model_metadata(item) for item in (dto.models or []) if isinstance(item, dict)],
        has_api_key=bool(dto.has_api_key),
    )


def _safe_model_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in item.items() if str(key) in _SAFE_MODEL_KEYS}


def _result_from_llm(result: LLMResult, provider: LLMProviderDTO, used_fallback: bool) -> AIResult:
    sources = [dict(item) for item in (getattr(result, "sources", None) or []) if isinstance(item, dict)]
    return AIResult(
        text=str(result.text or ""),
        model=str(result.model or provider.default_model or ""),
        provider_id=int(provider.id),
        provider_name=provider.name,
        used_fallback=bool(used_fallback),
        input_tokens=int(result.input_tokens or 0),
        output_tokens=int(result.output_tokens or 0),
        sources=sources,
    )


async def _emit_stream_usage(
    *,
    account_id: int | None,
    plugin_key: str,
    provider: LLMProviderDTO,
    model: str | None,
    success: bool,
    started_at: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_type: str | None = None,
    request_preview: str | None = None,
    response_preview: str | None = None,
) -> None:
    await llm_runtime._emit_usage(
        llm_runtime.UsageRecord(
            provider_id=int(provider.id),
            account_id=account_id,
            provider_name=provider.name,
            model=model or provider.default_model,
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
            latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            success=success,
            error_type=error_type,
            source=f"plugin:{plugin_key}",
            used_fallback=False,
            fallback_chain=[provider.name],
            request_preview=request_preview,
            response_preview=response_preview,
        )
    )


def _effective_api_format(provider: LLMProviderDTO) -> str:
    configured = str(provider.api_format or "").strip().lower()
    if configured:
        return configured
    return default_api_format_for(provider.provider)


def _facade_error_from_llm_call(exc: LLMCallFailed) -> PluginAIError:
    message = str(exc)
    if exc.error_type in {"budget_exceeded", "rate_limit"}:
        return AIQuotaError(message)
    return AIUnavailableError(message)


def _estimate_total_tokens(system_prompt: str, user_prompt: str, max_output_tokens: int) -> int:
    """Conservative quota reservation: prompt estimate + requested output cap."""

    prompt_bytes = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
    prompt_estimate = max(1, (prompt_bytes + 3) // 4)
    return max(1, int(max_output_tokens or 0) + prompt_estimate)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _proxy_url_from_row(proxy: Proxy | None) -> str | None:
    if proxy is None:
        return None
    ptype = str(proxy.type or "").lower()
    if ptype == "socks5":
        scheme = "socks5"
    elif ptype in {"http", "https"}:
        scheme = "http"
    else:
        return None

    password = ""
    if proxy.password_enc:
        try:
            password = decrypt_str(proxy.password_enc)
        except Exception:  # noqa: BLE001
            password = ""

    from urllib.parse import quote

    auth = ""
    if proxy.username:
        auth = quote(str(proxy.username), safe="")
        if password:
            auth = f"{auth}:{quote(password, safe='')}"
        auth = f"{auth}@"
    return f"{scheme}://{auth}{proxy.host}:{int(proxy.port)}"


__all__ = [
    "AIProviderInfo",
    "AIQuotaError",
    "AIResult",
    "AIUnavailableError",
    "PluginAI",
    "PluginAIError",
    "load_llm_providers",
]
