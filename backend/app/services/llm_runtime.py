"""LLM Runtime —— 调用层封装：fallback、retry、usage 记录。

设计目标：
1. **Runtime Fallback**：provider 失败后自动尝试 fallback chain
2. **Retry 策略**：只对 timeout/ConnectError/429/5xx 重试，指数退避
3. **Usage 记录**：记录每次调用的 provider/model/input/output tokens
4. **隐私安全**：日志不记录完整 prompt，只记录元数据

Fallback 优先级（从高到低）：
1. 显式 inline provider（用户 @provider 指定）
2. command/template configured provider
3. router fallback_provider_id
4. tag/capability 匹配且 cost_tier 更低的 provider
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm_client import LLMResult
    from .llm_dto import LLMProviderDTO

from ..settings import settings
from . import llm_account_budget
from .llm_client import build_client_from_dto
from .llm_protocol import (
    ApiFormat,
    ImageContent,
    ModelRequest,
    ModelResponse,
    StopReason,
    UnsupportedCapabilityError,
)
from .redactor import redact_text

log = logging.getLogger(__name__)

# 最大重试次数（不含首次调用）
_MAX_RETRIES = 3
# 重试延迟基数（秒）
_RETRY_BASE_DELAY = 1.0
# 最大退避时间（秒）
_RETRY_MAX_DELAY = 30.0
_USAGE_PREVIEW_CHARS = 2000


# ── Usage Record ────────────────────────────────────────────

@dataclass
class UsageRecord:
    """单次 LLM 调用的 usage 记录。"""
    provider_id: int | None = None
    account_id: int | None = None
    triggered_by_account_id: int | None = None
    provider_name: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    success: bool = False
    error_type: str | None = None
    source: str | None = None
    used_fallback: bool = False
    fallback_chain: list[str] = field(default_factory=list)
    request_preview: str | None = None
    response_preview: str | None = None


@dataclass(frozen=True)
class BudgetCheck:
    """Result of the account-level LLM budget gate."""

    error: str | None = None
    scope: str | None = None
    ticket: llm_account_budget.LLMAccountBudgetTicket | None = None


# 全局 usage 回调（可注入到 DB / Redis / 日志）
_usage_callbacks: list[Callable[[UsageRecord], Coroutine[Any, Any, None]]] = []


def register_usage_callback(cb: Callable[[UsageRecord], Coroutine[Any, Any, None]]) -> None:
    """注册 usage 记录回调。

    用法：
        async def on_usage(record: UsageRecord):
            await db.save(record)

        register_usage_callback(on_usage)
    """
    if cb not in _usage_callbacks:
        _usage_callbacks.append(cb)


async def _emit_usage(record: UsageRecord) -> None:
    """将 usage 记录发送到所有注册的回调。"""
    for cb in _usage_callbacks:
        try:
            await cb(record)
        except Exception:
            # 不应因 usage 记录失败影响主流程
            log.exception("usage callback 失败")


def preview_text_for_usage(value: Any, *, limit: int = _USAGE_PREVIEW_CHARS) -> str | None:
    """Return a redacted, bounded preview suitable for the usage table."""
    text = str(value or "").strip()
    if not text:
        return None
    redacted = redact_text(text)
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[:limit]}...[truncated]"


def request_preview_for_usage(system: str, user: str) -> str | None:
    parts: list[str] = []
    system_preview = preview_text_for_usage(system, limit=900)
    user_preview = preview_text_for_usage(user, limit=1400)
    if system_preview:
        parts.append(f"system:\n{system_preview}")
    if user_preview:
        parts.append(f"user:\n{user_preview}")
    return preview_text_for_usage("\n\n".join(parts))


# ── Retry 计算 ──────────────────────────────────────────────

def _compute_retry_delay(attempt: int) -> float:
    """计算指数退避延迟：base * 2^(attempt-1)，加抖动后限制在 max_delay 内。"""
    import random
    delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
    jitter = delay * 0.25 * (2 * random.random() - 1)
    return min(delay + jitter, _RETRY_MAX_DELAY)


# ── Error 分类 ───────────────────────────────────────────────

def _is_retryable_error(exc: Exception) -> bool:
    """判断错误是否可重试。

    可重试：timeout / ConnectError / 网络错误 / 429 / 5xx
    不可重试：400 / 401 / 403 / 404（认证/配置错误，重试无意义）
    """
    from .llm_client import LLMCallFailed, LLMError

    if isinstance(exc, LLMCallFailed):
        return exc.retryable
    if isinstance(exc, LLMError):
        return exc.retryable

    exc_name = type(exc).__name__
    retryable_types = {
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "ConnectError", "ReadError", "WriteError",
        "ProxyError", "SSLError", "ProtocolError", "HTTPError",
        "asyncio.TimeoutError",
    }
    return exc_name in retryable_types


def _classify_error(exc: Exception) -> str:
    """分类错误类型（用于日志）。"""
    from .llm_client import LLMCallFailed, LLMError

    if isinstance(exc, LLMCallFailed):
        return exc.error_type or "unknown"
    if isinstance(exc, LLMError):
        msg = str(exc).lower()
        if "timeout" in msg:
            return "timeout"
        if "connect" in msg or "network" in msg or "proxy" in msg:
            return "network"
        if "429" in msg or "限流" in msg:
            return "rate_limit"
        if "401" in msg or "403" in msg or "auth" in msg or "unauthorized" in msg:
            return "auth"
        if "5" in msg[:3]:
            return "server_error"
        return "unknown"
    return type(exc).__name__.lower()


def _error_scope(exc: Exception) -> str:
    from .llm_client import LLMCallFailed, LLMError, LLMErrorScope

    if isinstance(exc, UnsupportedCapabilityError):
        return LLMErrorScope.CAPABILITY_MISMATCH.value
    if isinstance(exc, (LLMCallFailed, LLMError)):
        return exc.scope.value
    return LLMErrorScope.UNKNOWN.value


def _should_try_next_provider(exc: Exception) -> bool:
    from .llm_client import LLMErrorScope

    if _is_retryable_error(exc):
        return True
    return _error_scope(exc) in {
        LLMErrorScope.PROVIDER_LOCAL.value,
        LLMErrorScope.CAPABILITY_MISMATCH.value,
    }


def _ensure_legacy_result_product(result: LLMResult) -> None:
    """Reject only semantically empty successes; tool/image/refusal remain valid."""
    from .llm_client import LLMError, LLMErrorScope

    if str(result.text or "").strip():
        return
    if result.tool_calls or result.image_urls or result.image_data:
        return
    if result.stop_reason in {StopReason.REFUSAL, StopReason.CONTENT_FILTER}:
        return
    raise LLMError(
        "Provider 返回 HTTP 200 但没有文本、工具调用、图片或合法终止语义",
        scope=LLMErrorScope.PROVIDER_LOCAL,
    )


def _ensure_model_response_product(response: ModelResponse) -> None:
    from .llm_client import LLMError, LLMErrorScope

    if response.text or response.tool_calls:
        return
    if response.stop_reason in {StopReason.REFUSAL, StopReason.CONTENT_FILTER}:
        return
    raise LLMError(
        "Provider 返回 HTTP 200 但没有文本、工具调用或合法终止语义",
        scope=LLMErrorScope.PROVIDER_LOCAL,
    )


def _estimate_text_tokens(value: str) -> int:
    raw = value.encode("utf-8")
    return max(1, (len(raw) + 3) // 4) if raw else 0


def _estimate_legacy_request_tokens(
    system: str,
    user: str,
    max_output_tokens: int,
    images: list[bytes] | None,
) -> int:
    image_units = 1024 * len(images or [])
    return max(
        1,
        _estimate_text_tokens(system) + _estimate_text_tokens(user) + image_units
        + max(0, int(max_output_tokens or 0)),
    )


def _estimate_structured_request_tokens(request: ModelRequest) -> int:
    text_units = sum(
        _estimate_text_tokens(message.text_content()) for message in request.messages
    )
    image_units = 1024 * sum(
        isinstance(block, ImageContent)
        for message in request.messages
        for block in message.content
    )
    tool_units = sum(
        _estimate_text_tokens(tool.name)
        + _estimate_text_tokens(tool.description)
        + _estimate_text_tokens(str(tool.parameters))
        for tool in request.tools
    )
    return max(1, text_units + image_units + tool_units + request.max_output_tokens)


def _legacy_capability_errors(
    provider: LLMProviderDTO,
    *,
    model: str,
    images: list[bytes] | None,
    web_search: bool,
    reasoning_effort: str | None,
    native_image: bool,
) -> list[str]:
    capabilities = provider.capabilities_for_model(model)
    errors: list[str] = []
    if images and not capabilities.images:
        errors.append("provider 不支持图片输入")
    if web_search:
        fmt = str(provider.api_format or "chat_completions").strip().lower()
        search_fmt = str(provider.web_search_api_format or "auto").strip().lower()
        can_switch_to_responses = (
            provider.provider.lower() == "openai"
            and search_fmt in {"auto", "responses"}
            and fmt in {"chat_completions", "responses"}
        )
        if not capabilities.web_search and not can_switch_to_responses:
            errors.append("provider 不支持原生联网搜索")
    if reasoning_effort:
        if not capabilities.reasoning:
            errors.append("provider 不支持 reasoning")
        elif (
            capabilities.reasoning_efforts
            and reasoning_effort not in capabilities.reasoning_efforts
        ):
            errors.append(f"provider 不支持 reasoning_effort={reasoning_effort}")
    if native_image and provider.provider.lower() != "openai":
        errors.append("provider 不支持原生图片生成")
    return errors


# ── Call with Fallback ───────────────────────────────────────

@dataclass
class FallbackChain:
    """Fallback provider 链。"""
    primary: LLMProviderDTO
    fallbacks: list[LLMProviderDTO] = field(default_factory=list)

    @property
    def all_providers(self) -> list[LLMProviderDTO]:
        """返回所有可用 provider（primary + fallbacks）。"""
        return [self.primary] + self.fallbacks

    def get_provider_names(self) -> list[str]:
        """返回 provider 名称列表（用于日志）。"""
        return [p.name for p in self.all_providers]


async def call_with_fallback(
    chain: FallbackChain,
    system: str,
    user: str,
    override_model: str | None = None,
    max_tokens: int = 512,
    images: list[bytes] | None = None,
    web_search: bool = False,
    web_search_context_size: str | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int | None = None,
    native_image: bool = False,
    *,
    # 隐私控制
    log_prompt_preview: bool = False,  # 设为 True 时只记录前 100 字符
    client_factory: Callable[..., Any | Awaitable[Any]] | None = None,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
    source: str | None = None,
    # 调试
    _debug: bool = False,
) -> tuple[LLMResult, LLMProviderDTO, bool]:
    """使用 fallback 链调用 LLM。

    策略：
    1. 先用 primary provider
    2. 如果失败且可 fallback，尝试 fallback chain 中的 provider
    3. 每个 provider 最多重试 _MAX_RETRIES 次（指数退避）
    4. 最终返回 (result, used_provider, used_fallback)

    Args:
        chain: FallbackChain，包含 primary 和 fallback providers
        system: 系统提示词
        user: 用户消息
        override_model: 覆盖模型名
        max_tokens: 最大输出 token 数
        images: 图片字节列表（vision 模型用）
        native_image: True 时调用 provider 的原生生图入口，而不是普通文本 complete
        log_prompt_preview: 是否在日志中记录 prompt 预览

    Returns:
        (LLMResult, used_provider, used_fallback)
        - LLMResult: 成功时返回
        - used_provider: 实际使用的 provider
        - used_fallback: 是否使用了 fallback（非 primary）

    Raises:
        LLMCallFailed: 所有 provider 都失败时抛出
    """
    from .llm_client import LLMCallFailed, LLMError, LLMErrorScope

    all_providers = chain.all_providers
    max_tokens = _apply_output_token_cap(max_tokens)
    estimated_tokens = _estimate_legacy_request_tokens(system, user, max_tokens, images)
    last_error: Exception | None = None

    for idx, provider_dto in enumerate(all_providers):
        is_fallback = idx > 0
        model = override_model or provider_dto.default_model
        capability_errors = _legacy_capability_errors(
            provider_dto,
            model=model,
            images=images,
            web_search=web_search,
            reasoning_effort=reasoning_effort,
            native_image=native_image,
        )
        if capability_errors:
            last_error = LLMError(
                "; ".join(capability_errors),
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
            log.info(
                "[llm-runtime] 跳过不兼容 provider=%s reason=%s",
                provider_dto.name,
                str(last_error),
            )
            continue

        budget_check = await _check_budget(account_id, provider_dto, estimated_tokens)
        if budget_check.error:
            last_error = LLMCallFailed(
                budget_check.error,
                provider_id=provider_dto.id,
                provider_name=provider_dto.name,
                error_type="budget_exceeded",
                retryable=False,
            )
            await _emit_usage(
                UsageRecord(
                    provider_id=provider_dto.id,
                    account_id=account_id,
                    triggered_by_account_id=triggered_by_account_id,
                    provider_name=provider_dto.name,
                    model=model,
                    success=False,
                    error_type="budget_exceeded",
                    source=source,
                    used_fallback=is_fallback,
                    fallback_chain=chain.get_provider_names(),
                    request_preview=request_preview_for_usage(system, user),
                )
            )
            if budget_check.scope == "premium_daily" and idx < len(all_providers) - 1:
                continue
            raise last_error

        # 记录当前尝试的 provider（不记录完整 prompt）
        log.info(
            "[llm-runtime] 尝试 provider=%s (fallback=%s) model=%s",
            provider_dto.name,
            is_fallback,
            override_model or provider_dto.default_model,
        )

        # 记录本次 provider 尝试的墙钟耗时（含重试退避），成功/失败都写入 usage.latency_ms
        attempt_start = time.monotonic()
        try:
            result = await _call_with_retry(
                provider_dto,
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
                log_prompt_preview=log_prompt_preview,
                client_factory=client_factory,
            )
            _ensure_legacy_result_product(result)
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            # 成功
            used_fallback = is_fallback
            # 记录 usage
            usage_record = UsageRecord(
                provider_id=provider_dto.id,
                account_id=account_id,
                triggered_by_account_id=triggered_by_account_id,
                provider_name=provider_dto.name,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
                success=True,
                source=source,
                used_fallback=used_fallback,
                fallback_chain=chain.get_provider_names(),
                request_preview=request_preview_for_usage(system, user),
                response_preview=preview_text_for_usage(result.text),
            )
            await _emit_usage(usage_record)
            await llm_account_budget.settle(
                budget_check.ticket,
                actual_tokens=result.input_tokens + result.output_tokens,
                actual_provider=provider_dto,
                success=True,
            )

            if _debug:
                log.debug(
                    "[llm-runtime] 成功 provider=%s tokens=%d/%d",
                    provider_dto.name,
                    result.input_tokens,
                    result.output_tokens,
                )

            return result, provider_dto, used_fallback

        except Exception as exc:
            last_error = exc
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            error_type = _classify_error(exc)
            retryable = _is_retryable_error(exc)
            scope = _error_scope(exc)

            log.warning(
                "[llm-runtime] provider=%s 调用失败 error=%s retryable=%s scope=%s",
                provider_dto.name,
                error_type,
                retryable,
                scope,
            )

            await llm_account_budget.settle(
                budget_check.ticket,
                actual_tokens=0,
                actual_provider=None,
                success=False,
            )

            if idx == len(all_providers) - 1 or not _should_try_next_provider(exc):
                # 记录失败 usage
                usage_record = UsageRecord(
                    provider_id=provider_dto.id,
                    account_id=account_id,
                    triggered_by_account_id=triggered_by_account_id,
                    provider_name=provider_dto.name,
                    model=override_model or provider_dto.default_model,
                    latency_ms=latency_ms,
                    success=False,
                    error_type=error_type,
                    source=source,
                    used_fallback=is_fallback,
                    fallback_chain=chain.get_provider_names(),
                    request_preview=request_preview_for_usage(system, user),
                )
                await _emit_usage(usage_record)
                raise LLMCallFailed(
                    f"所有 provider 都失败。最后错误: {type(last_error).__name__}: {last_error}",
                    provider_id=provider_dto.id,
                    provider_name=provider_dto.name,
                    error_type=error_type,
                    retryable=False,
                    scope=scope,
                ) from last_error

    raise LLMCallFailed(
        f"所有 provider 均不兼容或调用失败: {last_error}",
        provider_id=all_providers[-1].id if all_providers else None,
        error_type="exhausted",
        retryable=False,
        scope=_error_scope(last_error) if last_error else LLMErrorScope.UNKNOWN,
    )


async def invoke_model_with_fallback(
    chain: FallbackChain,
    request: ModelRequest,
    *,
    account_id: int | None = None,
    triggered_by_account_id: int | None = None,
    source: str | None = None,
    client_factory: Callable[..., Any | Awaitable[Any]] | None = None,
) -> tuple[ModelResponse, LLMProviderDTO, bool]:
    """Invoke a structured model request through existing budget and fallback gates."""

    from .llm_client import LLMCallFailed, LLMErrorScope

    providers = chain.all_providers
    capped_request = replace(
        request,
        max_output_tokens=_apply_output_token_cap(request.max_output_tokens),
    )
    request_preview = _structured_request_preview(capped_request)
    estimated_tokens = _estimate_structured_request_tokens(capped_request)
    last_error: Exception | None = None
    model_pinned = bool(capped_request.metadata.get("model_pinned", True))
    for index, provider in enumerate(providers):
        provider_request = replace(
            capped_request,
            model=capped_request.model if model_pinned else provider.default_model,
        )
        capability_errors = provider.capabilities_for_model(
            provider_request.model
        ).validation_errors(provider_request)
        if capability_errors:
            last_error = UnsupportedCapabilityError(
                ApiFormat(provider.api_format or "chat_completions"),
                tuple(capability_errors),
            )
            log.info(
                "[llm-runtime] 跳过不兼容 provider=%s reason=%s",
                provider.name,
                last_error,
            )
            continue

        budget_check = await _check_budget(account_id, provider, estimated_tokens)
        if budget_check.error:
            last_error = LLMCallFailed(
                budget_check.error,
                provider_id=provider.id,
                provider_name=provider.name,
                error_type="budget_exceeded",
            )
            if budget_check.scope == "premium_daily" and index < len(providers) - 1:
                continue
            raise last_error

        started = time.monotonic()
        try:
            response = await _invoke_model_with_retry(
                provider,
                provider_request,
                client_factory=client_factory,
            )
            _ensure_model_response_product(response)
            used_fallback = index > 0
            await _emit_usage(
                UsageRecord(
                    provider_id=provider.id,
                    account_id=account_id,
                    triggered_by_account_id=triggered_by_account_id,
                    provider_name=provider.name,
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=True,
                    source=source,
                    used_fallback=used_fallback,
                    fallback_chain=chain.get_provider_names(),
                    request_preview=request_preview,
                    response_preview=preview_text_for_usage(response.text),
                )
            )
            await llm_account_budget.settle(
                budget_check.ticket,
                actual_tokens=response.usage.total_tokens,
                actual_provider=provider,
                success=True,
            )
            return response, provider, used_fallback
        except Exception as exc:
            last_error = exc
            await llm_account_budget.settle(
                budget_check.ticket,
                actual_tokens=0,
                actual_provider=None,
                success=False,
            )
            if index < len(providers) - 1 and _should_try_next_provider(exc):
                continue
            await _emit_usage(
                UsageRecord(
                    provider_id=provider.id,
                    account_id=account_id,
                    triggered_by_account_id=triggered_by_account_id,
                    provider_name=provider.name,
                    model=provider_request.model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_type=_classify_error(exc),
                    source=source,
                    used_fallback=index > 0,
                    fallback_chain=chain.get_provider_names(),
                    request_preview=request_preview,
                )
            )
            raise LLMCallFailed(
                f"所有 provider 都失败。最后错误: {type(exc).__name__}: {exc}",
                provider_id=provider.id,
                provider_name=provider.name,
                error_type=_classify_error(exc),
                retryable=False,
                scope=_error_scope(exc),
            ) from exc

    raise LLMCallFailed(
        f"所有 provider 均不兼容或调用失败: {last_error}",
        error_type="exhausted",
        scope=_error_scope(last_error) if last_error else LLMErrorScope.UNKNOWN,
    )


async def _invoke_model_with_retry(
    provider: LLMProviderDTO,
    request: ModelRequest,
    *,
    client_factory: Callable[..., Any | Awaitable[Any]] | None = None,
    max_retries: int = _MAX_RETRIES,
) -> ModelResponse:
    provider.capabilities_for_model(request.model).validate(
        request,
        provider.api_format or "chat_completions",
    )
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            builder = client_factory or build_client_from_dto
            client = builder(
                provider,
                override_model=request.model,
                proxy_url=provider.proxy_url,
            )
            if inspect.isawaitable(client):
                client = await client
            return await client.invoke(request)
        except Exception as exc:
            last_error = exc
            if not _is_retryable_error(exc) or attempt >= max_retries:
                raise
            await asyncio.sleep(_compute_retry_delay(attempt + 1))
    raise last_error or RuntimeError("结构化调用重试耗尽")


def _structured_request_preview(request: ModelRequest) -> str | None:
    system = "\n".join(
        message.text_content() for message in request.messages if message.role.value == "system"
    )
    user = "\n".join(
        message.text_content() for message in request.messages if message.role.value == "user"
    )
    return request_preview_for_usage(system, user)


def _apply_output_token_cap(max_tokens: int) -> int:
    """应用全局 LLM 输出 token 上限；0 表示不限制。"""
    cap = int(getattr(settings, "llm_max_output_tokens", 0) or 0)
    if cap <= 0:
        return max_tokens
    if max_tokens <= 0:
        return cap
    return min(max_tokens, cap)


async def _check_budget(
    account_id: int | None,
    provider_dto: LLMProviderDTO,
    estimated_tokens: int,
) -> BudgetCheck:
    """Atomically reserve account-level budget before one provider candidate."""
    try:
        ticket = await llm_account_budget.acquire(account_id, provider_dto, estimated_tokens)
    except llm_account_budget.LLMAccountBudgetExceeded as exc:
        return BudgetCheck(error=str(exc), scope=exc.scope)
    return BudgetCheck(ticket=ticket)


async def _call_with_retry(
    provider_dto: LLMProviderDTO,
    system: str,
    user: str,
    override_model: str | None,
    max_tokens: int,
    images: list[bytes] | None,
    web_search: bool,
    web_search_context_size: str | None,
    temperature: float | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    native_image: bool,
    log_prompt_preview: bool,
    client_factory: Callable[..., Any | Awaitable[Any]] | None = None,
    max_retries: int = _MAX_RETRIES,
) -> LLMResult:
    """使用指数退避重试调用单个 provider。"""
    from .llm_client import (
        LLMError,
    )

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        start_time = time.monotonic()

        try:
            builder = client_factory or build_client_from_dto
            client = builder(
                provider_dto,
                override_model=override_model,
                proxy_url=provider_dto.proxy_url,
            )
            if inspect.isawaitable(client):
                client = await client
            kwargs = {
                "max_tokens": max_tokens,
                "images": images,
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "timeout_seconds": timeout_seconds,
            }
            if web_search:
                kwargs["web_search"] = True
                kwargs["web_search_context_size"] = web_search_context_size
            if native_image:
                result = await _call_generate_image_compat(client, system, user, kwargs)
            else:
                result = await _call_complete_compat(client, system, user, kwargs)
            latency_ms = int((time.monotonic() - start_time) * 1000)

            if attempt > 0:
                log.info(
                    "[llm-runtime] 重试成功 provider=%s attempt=%d latency=%dms",
                    provider_dto.name,
                    attempt,
                    latency_ms,
                )

            return result

        except LLMError as exc:
            last_error = exc

            if not _is_retryable_error(exc):
                # 不可重试的错误（如 401/403）直接抛出
                raise

            if attempt < max_retries:
                delay = _compute_retry_delay(attempt + 1)
                log.warning(
                    "[llm-runtime] provider=%s attempt=%d/%d 失败 error=%s 等待 %.1fs",
                    provider_dto.name,
                    attempt + 1,
                    max_retries + 1,
                    str(exc)[:100],
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            else:
                raise

        except Exception as exc:
            last_error = exc
            if not _is_retryable_error(exc):
                raise

            if attempt < max_retries:
                delay = _compute_retry_delay(attempt + 1)
                log.warning(
                    "[llm-runtime] provider=%s 网络错误 attempt=%d/%d 等待 %.1fs",
                    provider_dto.name,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            else:
                raise

    # 理论上不会走到这里
    raise last_error or RuntimeError("重试耗尽但无错误信息")


async def _call_complete_compat(
    client: Any,
    system: str,
    user: str,
    kwargs: dict[str, Any],
) -> LLMResult:
    """Call ``complete`` while tolerating older test doubles with fewer kwargs."""
    complete = client.complete
    try:
        sig = inspect.signature(complete)
    except (TypeError, ValueError):
        return await complete(system, user, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return await complete(system, user, **kwargs)
    allowed = set(sig.parameters)
    filtered = {key: value for key, value in kwargs.items() if key in allowed}
    return await complete(system, user, **filtered)


async def _call_generate_image_compat(
    client: Any,
    system: str,
    user: str,
    kwargs: dict[str, Any],
) -> LLMResult:
    """Call ``generate_image`` while tolerating test doubles with fewer kwargs."""
    generate_image = getattr(client, "generate_image", None)
    if generate_image is None:
        raise NotImplementedError("当前 provider client 没有 generate_image 方法")
    try:
        sig = inspect.signature(generate_image)
    except (TypeError, ValueError):
        return await generate_image(system, user, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return await generate_image(system, user, **kwargs)
    allowed = set(sig.parameters)
    filtered = {key: value for key, value in kwargs.items() if key in allowed}
    return await generate_image(system, user, **filtered)


# ── 辅助函数 ────────────────────────────────────────────────

def build_fallback_chain(
    primary: LLMProviderDTO,
    providers: dict[int, LLMProviderDTO] | None = None,
    fallback_provider_id: int | None = None,
    matched_tag: str | None = None,
) -> FallbackChain:
    """根据配置构建 fallback chain。

    优先级：
    1. primary（显式指定）
    2. fallback_provider_id（router 配置）
    3. 同 tag 但 cost_tier 更低的 provider

    Args:
        primary: 主要 provider
        providers: 所有可用 provider 字典
        fallback_provider_id: 路由配置的 fallback
        matched_tag: 匹配的 tag（用于找同 tag 低价 provider）
    """
    fallbacks: list[LLMProviderDTO] = []

    # 1. fallback_provider_id
    if providers and fallback_provider_id is not None:
        fb = providers.get(fallback_provider_id)
        if fb and fb.id != primary.id and fb.has_api_key:
            fallbacks.append(fb)

    # 2. 同 tag 低价 provider
    if providers and matched_tag:
        same_tag = [
            p for p in providers.values()
            if p.id != primary.id
            and matched_tag in p.tags
            and p.cost_tier < primary.cost_tier
            and p.has_api_key
        ]
        same_tag.sort(key=lambda p: p.cost_tier)
        for p in same_tag:
            if p not in fallbacks:
                fallbacks.append(p)

    # 3. 其他有 key 的 provider
    if providers:
        others = [
            p for p in providers.values()
            if p.id != primary.id
            and p not in fallbacks
            and p.has_api_key
        ]
        others.sort(key=lambda p: p.cost_tier)
        fallbacks.extend(others[:2])  # 最多再加 2 个通用 fallback

    return FallbackChain(primary=primary, fallbacks=fallbacks)


__all__ = [
    "FallbackChain",
    "UsageRecord",
    "build_fallback_chain",
    "call_with_fallback",
    "invoke_model_with_fallback",
    "register_usage_callback",
]
