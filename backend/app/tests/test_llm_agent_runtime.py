from __future__ import annotations

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
