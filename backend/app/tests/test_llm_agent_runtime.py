from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import llm_account_budget, llm_runtime
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_invoke import invoke_structured
from app.services.llm_protocol import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TextContent,
    ToolSpec,
)


@pytest.mark.asyncio
async def test_structured_invoke_uses_budget_usage_and_provider_runtime(monkeypatch) -> None:
    provider = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="model",
        api_key_enc="encrypted",
    )
    ticket = llm_account_budget.LLMAccountBudgetTicket(
        account_id=7,
        estimated_tokens=20,
        provider_id=1,
    )
    acquire = AsyncMock(return_value=ticket)
    settle = AsyncMock()
    usage_records = []

    async def emit(record) -> None:
        usage_records.append(record)

    class _Client:
        async def invoke(self, request: ModelRequest) -> ModelResponse:
            assert request.max_output_tokens == 20
            return ModelResponse(
                model=request.model,
                content=(TextContent("ok"),),
                usage=ModelUsage(input_tokens=5, output_tokens=3),
            )

    monkeypatch.setattr(llm_account_budget, "acquire", acquire)
    monkeypatch.setattr(llm_account_budget, "settle", settle)
    monkeypatch.setattr(llm_runtime, "_emit_usage", emit)

    response, used_provider, used_fallback = await invoke_structured(
        provider,
        {1: provider},
        ModelRequest(
            model="model",
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            max_output_tokens=20,
        ),
        account_id=7,
        source="agent:test",
        client_factory=lambda *_args, **_kwargs: _Client(),
    )

    assert response.text == "ok"
    assert used_provider.id == 1
    assert used_fallback is False
    acquire.assert_awaited_once()
    settle.assert_awaited_once_with(
        ticket,
        actual_tokens=8,
        actual_provider=provider,
        success=True,
    )
    assert usage_records[0].source == "agent:test"
    assert usage_records[0].input_tokens == 5


@pytest.mark.asyncio
async def test_structured_fallback_uses_each_provider_default_when_model_is_not_pinned(
    monkeypatch,
) -> None:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="fallback-model",
    )
    models: list[str] = []

    async def invoke(provider, request, **_kwargs):  # noqa: ANN001
        models.append(request.model)
        if provider.id == primary.id:
            from app.services.llm_client import LLMError

            raise LLMError("temporary", retryable=True)
        return ModelResponse(
            model=request.model,
            content=(TextContent("ok"),),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(llm_runtime, "_check_budget", AsyncMock(return_value=llm_runtime.BudgetCheck()))
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    response, used_provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(primary, [fallback]),
        ModelRequest(
            model=primary.default_model,
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            metadata={"model_pinned": False},
        ),
    )

    assert response.text == "ok"
    assert used_provider.id == fallback.id
    assert used_fallback is True
    assert models == ["primary-model", "fallback-model"]


@pytest.mark.asyncio
async def test_structured_fallback_selects_enabled_tools_model(monkeypatch) -> None:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="fallback-no-tools",
        models=[
            {
                "id": "fallback-no-tools",
                "enabled": True,
                "supports_tools": False,
            },
            {
                "id": "fallback-tools",
                "enabled": True,
                "supports_tools": True,
            },
        ],
    )
    invoked: list[tuple[int, str]] = []

    async def invoke(provider, request, **_kwargs):  # noqa: ANN001
        invoked.append((provider.id, request.model))
        if provider.id == primary.id:
            from app.services.llm_client import LLMError

            raise LLMError("temporary", retryable=True)
        return ModelResponse(
            model=request.model,
            content=(TextContent("ok"),),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(llm_runtime, "_check_budget", AsyncMock(return_value=llm_runtime.BudgetCheck()))
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    response, used_provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(primary, [fallback]),
        ModelRequest(
            model=primary.default_model,
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            tools=(
                ToolSpec(
                    name="logs_recent",
                    description="logs",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
            metadata={"model_pinned": False},
        ),
    )

    assert response.text == "ok"
    assert used_provider.id == fallback.id
    assert used_fallback is True
    assert invoked == [(primary.id, "primary-model"), (fallback.id, "fallback-tools")]


@pytest.mark.asyncio
async def test_structured_fallback_prefilters_incompatible_provider(monkeypatch) -> None:
    """web_search 请求应跳过 Anthropic 候选，继续尝试后续 Responses。"""
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
    )
    incompatible = LLMProviderDTO(
        id=2,
        name="anthropic",
        provider="anthropic",
        api_format="anthropic_messages",
        default_model="claude",
    )
    compatible = LLMProviderDTO(
        id=3,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="fallback-model",
    )
    invoked: list[int] = []

    async def invoke(provider, request, **_kwargs):  # noqa: ANN001
        invoked.append(provider.id)
        if provider.id == primary.id:
            from app.services.llm_client import LLMError

            raise LLMError("temporary", retryable=True)
        return ModelResponse(
            model=request.model,
            content=(TextContent("ok"),),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(llm_runtime, "_check_budget", AsyncMock(return_value=llm_runtime.BudgetCheck()))
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    response, used_provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(primary, [incompatible, compatible]),
        ModelRequest(
            model=primary.default_model,
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            web_search=True,
            metadata={"model_pinned": False},
        ),
    )

    assert response.text == "ok"
    assert used_provider.id == compatible.id
    assert used_fallback is True
    assert invoked == [primary.id, compatible.id]


@pytest.mark.asyncio
async def test_structured_empty_success_falls_back(monkeypatch) -> None:
    """结构化路径与 legacy 路径共享空产物合同。"""
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="fallback-model",
    )
    invoked: list[int] = []

    async def invoke(provider, request, **_kwargs):  # noqa: ANN001
        invoked.append(provider.id)
        return ModelResponse(
            model=request.model,
            content=() if provider.id == primary.id else (TextContent("ok"),),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(llm_runtime, "_check_budget", AsyncMock(return_value=llm_runtime.BudgetCheck()))
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    response, provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(primary, [fallback]),
        ModelRequest(
            model=primary.default_model,
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            metadata={"model_pinned": False},
        ),
    )

    assert response.text == "ok"
    assert provider.id == fallback.id
    assert used_fallback is True
    assert invoked == [primary.id, fallback.id]


@pytest.mark.asyncio
async def test_structured_fallback_tries_same_provider_models_first(monkeypatch) -> None:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="model-a",
        models=[
            {"id": "model-a", "enabled": True, "supports_tools": True},
            {"id": "model-b", "enabled": True, "supports_tools": True},
        ],
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="model-c",
    )
    invoked: list[tuple[int, str]] = []

    async def invoke(provider, request, **_kwargs):  # noqa: ANN001
        invoked.append((provider.id, request.model))
        if request.model == "model-a":
            from app.services.llm_client import LLMError, LLMErrorScope

            raise LLMError("upstream failed", scope=LLMErrorScope.PROVIDER_LOCAL)
        return ModelResponse(
            model=request.model,
            content=(TextContent("ok"),),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(llm_runtime, "_check_budget", AsyncMock(return_value=llm_runtime.BudgetCheck()))
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    response, provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(primary, [fallback]),
        ModelRequest(
            model="model-a",
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            metadata={"model_pinned": False},
        ),
    )

    assert response.model == "model-b"
    assert provider.id == primary.id
    assert used_fallback is False
    assert invoked == [(primary.id, "model-a"), (primary.id, "model-b")]


@pytest.mark.asyncio
async def test_structured_cross_provider_requires_explicit_confirmation(monkeypatch) -> None:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="model-a",
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="model-b",
    )
    invoked: list[int] = []

    async def invoke(provider, request, **_kwargs):  # noqa: ANN001
        invoked.append(provider.id)
        if provider.id == primary.id:
            from app.services.llm_client import LLMError, LLMErrorScope

            raise LLMError("upstream failed", scope=LLMErrorScope.PROVIDER_LOCAL)
        return ModelResponse(
            model=request.model,
            content=(TextContent("ok"),),
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(llm_runtime, "_check_budget", AsyncMock(return_value=llm_runtime.BudgetCheck()))
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    request = ModelRequest(
        model="model-a",
        messages=(ModelMessage.text(MessageRole.USER, "question"),),
        metadata={"model_pinned": False, "confirm_provider_switch": True},
    )
    with pytest.raises(llm_runtime.ProviderSwitchRequired) as exc_info:
        await llm_runtime.invoke_model_with_fallback(
            llm_runtime.FallbackChain(primary, [fallback]),
            request,
        )
    assert invoked == [primary.id]
    assert exc_info.value.candidates == [
        {"provider_id": 2, "provider_name": "fallback", "model": "model-b"}
    ]

    response, provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(primary, [fallback]),
        ModelRequest(
            model="model-a",
            messages=request.messages,
            metadata={
                "model_pinned": False,
                "confirm_provider_switch": True,
                "allowed_cross_provider_ids": [fallback.id],
            },
        ),
    )
    assert response.text == "ok"
    assert provider.id == fallback.id
    assert used_fallback is True
    assert invoked == [primary.id, primary.id, fallback.id]


@pytest.mark.asyncio
async def test_cross_provider_prompt_names_last_attempted_provider(monkeypatch) -> None:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="model-a",
    )
    incompatible = LLMProviderDTO(
        id=2,
        name="disabled-middle",
        provider="openai",
        api_format="responses",
        default_model="model-disabled",
        models=[{"id": "model-disabled", "enabled": False, "supports_tools": True}],
    )
    fallback = LLMProviderDTO(
        id=3,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="model-b",
    )

    async def invoke(provider, _request, **_kwargs):  # noqa: ANN001
        from app.services.llm_client import LLMError, LLMErrorScope

        assert provider.id == primary.id
        raise LLMError("temporary", retryable=True, scope=LLMErrorScope.PROVIDER_LOCAL)

    monkeypatch.setattr(llm_runtime, "_invoke_model_with_retry", invoke)
    monkeypatch.setattr(
        llm_runtime,
        "_check_budget",
        AsyncMock(return_value=llm_runtime.BudgetCheck()),
    )
    monkeypatch.setattr(llm_runtime, "_emit_usage", AsyncMock())
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    with pytest.raises(llm_runtime.ProviderSwitchRequired) as exc_info:
        await llm_runtime.invoke_model_with_fallback(
            llm_runtime.FallbackChain(primary, [incompatible, fallback]),
            ModelRequest(
                model=primary.default_model,
                messages=(ModelMessage.text(MessageRole.USER, "question"),),
                metadata={"model_pinned": False, "confirm_provider_switch": True},
            ),
        )

    assert exc_info.value.provider_name == "primary"
    assert exc_info.value.candidates[0]["provider_name"] == "fallback"


@pytest.mark.asyncio
async def test_agent_retries_same_model_five_times_before_same_provider_fallback(
    monkeypatch,
) -> None:
    from app.services.llm_client import LLMError, LLMErrorScope

    provider = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="model-a",
        api_key_enc="encrypted",
        models=[
            {"id": "model-a", "enabled": True, "supports_tools": True},
            {"id": "model-b", "enabled": True, "supports_tools": True},
        ],
    )
    attempts: list[str] = []
    sleeps: list[float] = []
    progress: list[dict] = []

    class _Client:
        async def invoke(self, request: ModelRequest) -> ModelResponse:
            attempts.append(request.model)
            if request.model == "model-a":
                raise LLMError(
                    "upstream unavailable",
                    retryable=True,
                    scope=LLMErrorScope.PROVIDER_LOCAL,
                    status_code=503,
                )
            return ModelResponse(
                model=request.model,
                content=(TextContent("ok"),),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def on_progress(event: dict) -> None:
        progress.append(event)

    monkeypatch.setattr(llm_runtime.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        llm_runtime,
        "_check_budget",
        AsyncMock(return_value=llm_runtime.BudgetCheck()),
    )
    emit_usage = AsyncMock()
    monkeypatch.setattr(llm_runtime, "_emit_usage", emit_usage)
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock())

    response, used_provider, used_fallback = await llm_runtime.invoke_model_with_fallback(
        llm_runtime.FallbackChain(provider),
        ModelRequest(
            model="model-a",
            messages=(ModelMessage.text(MessageRole.USER, "question"),),
            tools=(
                ToolSpec(
                    name="scheduler.list",
                    description="list schedules",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
            metadata={
                "model_pinned": False,
                "max_retries_per_model": 5,
                "retry_delay_seconds": 3.0,
            },
        ),
        client_factory=lambda *_args, **_kwargs: _Client(),
        progress_callback=on_progress,
    )

    assert response.model == "model-b"
    assert used_provider.id == provider.id
    assert used_fallback is False
    assert attempts == ["model-a"] * 6 + ["model-b"]
    assert sleeps == [3.0] * 5
    assert [event["retry_number"] for event in progress if event["type"] == "retry_scheduled"] == [
        1,
        2,
        3,
        4,
        5,
    ]
    exhausted = [event for event in progress if event["type"] == "model_exhausted"]
    assert [event["model"] for event in exhausted] == ["model-a"]
    usage_records = [call.args[0] for call in emit_usage.await_args_list]
    failed_record = next(record for record in usage_records if not record.success)
    assert failed_record.error_type == "server_error"
    assert failed_record.response_preview == "LLMError: upstream unavailable"


@pytest.mark.asyncio
async def test_agent_retry_sleep_can_be_cancelled(monkeypatch) -> None:
    from app.services.llm_client import LLMError

    provider = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="model-a",
    )
    sleep_started = asyncio.Event()

    class _Client:
        async def invoke(self, _request: ModelRequest) -> ModelResponse:
            raise LLMError("503", retryable=True, status_code=503)

    async def blocked_sleep(_delay: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(llm_runtime.asyncio, "sleep", blocked_sleep)
    task = asyncio.create_task(
        llm_runtime._invoke_model_with_retry(
            provider,
            ModelRequest(
                model="model-a",
                messages=(ModelMessage.text(MessageRole.USER, "question"),),
            ),
            client_factory=lambda *_args, **_kwargs: _Client(),
            max_retries=5,
            retry_delay_seconds=3.0,
        )
    )
    await sleep_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
