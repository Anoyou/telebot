from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.system import SystemSetting
from app.services.llm_client import LLMError, LLMErrorScope
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import ModelResponse, ToolCall
from app.services.system_agent import model_capability
from app.services.system_agent.config import ResolvedAgentProviders


@pytest.fixture
async def setting_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SystemSetting.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _provider(provider_id: int, *, models: list[str]) -> LLMProviderDTO:
    return LLMProviderDTO(
        id=provider_id,
        name=f"provider-{provider_id}",
        provider="openai",
        api_format="responses",
        base_url="https://example.invalid/v1",
        default_model=models[0],
        api_key_enc="encrypted",
        models=[
            {"id": model, "enabled": True, "supports_tools": True}
            for model in models
        ],
    )


@pytest.mark.asyncio
async def test_probe_requires_expected_tool_call(monkeypatch) -> None:
    captured = None

    class Client:
        async def invoke(self, request):  # noqa: ANN001
            nonlocal captured
            captured = request
            return ModelResponse(
                model=request.model,
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name=model_capability.PROBE_TOOL_NAME,
                        arguments={"nonce": model_capability.PROBE_NONCE},
                    ),
                ),
            )

    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(_provider(1, models=["m1"]), "m1")

    assert result.supported is True
    assert captured.tool_choice.name == model_capability.PROBE_TOOL_NAME
    assert len(captured.tools) == 1


@pytest.mark.asyncio
async def test_probe_rejects_plain_text_success(monkeypatch) -> None:
    class Client:
        async def invoke(self, request):  # noqa: ANN001
            return ModelResponse(model=request.model)

    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(_provider(1, models=["m1"]), "m1")
    assert result.status == "unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMError("503", retryable=True, status_code=503), "unavailable"),
        (
            LLMError("tools unsupported", scope=LLMErrorScope.CAPABILITY_MISMATCH),
            "unsupported",
        ),
    ],
)
async def test_probe_distinguishes_transient_and_capability_errors(
    monkeypatch,
    error: Exception,
    expected: str,
) -> None:
    class Client:
        async def invoke(self, _request):  # noqa: ANN001
            raise error

    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(_provider(1, models=["m1"]), "m1")
    assert result.status == expected


@pytest.mark.asyncio
async def test_verify_filters_models_and_reuses_persistent_cache(setting_db, monkeypatch) -> None:
    primary = _provider(1, models=["good", "bad"])
    fallback = _provider(2, models=["fallback"])
    resolved = ResolvedAgentProviders(
        primary=primary,
        model="good",
        providers={1: primary, 2: fallback},
    )
    calls: list[tuple[int, str]] = []

    async def probe(provider, model):  # noqa: ANN001
        calls.append((provider.id, model))
        return model_capability.CapabilityProbeResult(
            "unsupported" if model == "bad" else "supported",
            datetime.now(UTC),
        )

    monkeypatch.setattr(model_capability, "probe_model_tool_capability", probe)
    async with setting_db() as db:
        first = await model_capability.verify_resolved_agent_providers(db, resolved)
        await db.commit()
        assert not isinstance(first, str)
        assert first.model == "good"
        assert first.primary.enabled_model_ids() == ["good"]
        assert first.providers[2].enabled_model_ids() == ["fallback"]
        assert calls == [(1, "good"), (1, "bad"), (2, "fallback")]

    calls.clear()
    async with setting_db() as db:
        second = await model_capability.verify_resolved_agent_providers(db, resolved)
        assert not isinstance(second, str)
        assert calls == []
        assert await db.get(SystemSetting, model_capability.CACHE_KEY) is not None


@pytest.mark.asyncio
async def test_verify_rejects_unsupported_primary(setting_db, monkeypatch) -> None:
    primary = _provider(1, models=["bad"])
    resolved = ResolvedAgentProviders(primary=primary, model="bad", providers={1: primary})

    async def probe(_provider, _model):  # noqa: ANN001
        return model_capability.CapabilityProbeResult("unsupported", datetime.now(UTC))

    monkeypatch.setattr(model_capability, "probe_model_tool_capability", probe)
    async with setting_db() as db:
        result = await model_capability.verify_resolved_agent_providers(db, resolved)
    assert result == "Provider「provider-1」的候选模型不支持 Agent 工具调用。"
