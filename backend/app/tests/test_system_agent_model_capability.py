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
    assert captured.tools[0].strict is False
    assert captured.max_output_tokens >= 256
    assert captured.metadata.get("thinking") is None


@pytest.mark.asyncio
async def test_probe_disables_thinking_for_deepseek_providers(monkeypatch) -> None:
    """DeepSeek V4 默认 thinking=enabled，探测应显式关闭以便强制 tool_choice。"""

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

    deepseek = LLMProviderDTO(
        id=9,
        name="deepseek-O",
        provider="openai",
        api_format="chat_completions",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-pro",
        api_key_enc="encrypted",
        models=[{"id": "deepseek-v4-pro", "enabled": True, "supports_tools": True}],
    )
    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(deepseek, "deepseek-v4-pro")

    assert result.supported is True
    assert captured.metadata.get("thinking") == "disabled"


@pytest.mark.asyncio
async def test_probe_accepts_correct_tool_even_if_nonce_wrong(monkeypatch) -> None:
    """DeepSeek 等兼容站强制 tool_choice 后可能改写参数，仍应视为支持 tools。"""

    class Client:
        async def invoke(self, request):  # noqa: ANN001
            return ModelResponse(
                model=request.model,
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name=model_capability.PROBE_TOOL_NAME,
                        arguments={"nonce": "not-the-expected-value"},
                    ),
                ),
            )

    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(_provider(1, models=["m1"]), "m1")
    assert result.supported is True


@pytest.mark.asyncio
async def test_probe_rejects_plain_text_success(monkeypatch) -> None:
    class Client:
        async def invoke(self, request):  # noqa: ANN001
            return ModelResponse(model=request.model)

    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(_provider(1, models=["m1"]), "m1")
    assert result.status == "unsupported"


@pytest.mark.asyncio
async def test_probe_falls_back_to_auto_when_thinking_rejects_forced_tool_choice(
    monkeypatch,
) -> None:
    """DeepSeek 思考模式拒绝强制 tool_choice 时，应降级 auto 并仍判 supported。"""

    from app.services.llm_protocol import NamedToolChoice, ToolChoiceMode

    choices: list[object] = []

    class Client:
        async def invoke(self, request):  # noqa: ANN001
            choices.append(request.tool_choice)
            if isinstance(request.tool_choice, NamedToolChoice):
                raise LLMError(
                    'Anthropic 接口返回 400: {"error":{"message":'
                    '"Thinking mode does not support this tool_choice"}}',
                    scope=LLMErrorScope.CAPABILITY_MISMATCH,
                    status_code=400,
                )
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
    assert isinstance(choices[0], NamedToolChoice)
    assert choices[1] is ToolChoiceMode.AUTO


@pytest.mark.asyncio
async def test_probe_marks_thinking_tool_choice_error_unavailable_without_success(
    monkeypatch,
) -> None:
    """强制与 auto 都因思考模式 tool_choice 失败时，标 unavailable 而非永久 unsupported。"""

    class Client:
        async def invoke(self, _request):  # noqa: ANN001
            raise LLMError(
                "Thinking mode does not support this tool_choice",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
                status_code=400,
            )

    monkeypatch.setattr(model_capability, "build_client_from_dto", lambda *_a, **_k: Client())
    result = await model_capability.probe_model_tool_capability(_provider(1, models=["m1"]), "m1")
    assert result.status == "unavailable"
    assert result.error_type == "LLMError"


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
    assert isinstance(result, str)
    assert "provider-1" in result
    assert "工具调用" in result


@pytest.mark.asyncio
async def test_verify_keeps_temporarily_unavailable_fallback_candidate(
    setting_db,
    monkeypatch,
) -> None:
    primary = _provider(1, models=["primary"])
    fallback = _provider(2, models=["fallback"])
    fallback.models[0]["supports_tools"] = None
    resolved = ResolvedAgentProviders(
        primary=primary,
        model="primary",
        providers={1: primary, 2: fallback},
    )

    async def probe(provider, _model):  # noqa: ANN001
        return model_capability.CapabilityProbeResult(
            "supported" if provider.id == 1 else "unavailable",
            datetime.now(UTC),
            "LLMError" if provider.id == 2 else None,
        )

    monkeypatch.setattr(model_capability, "probe_model_tool_capability", probe)
    async with setting_db() as db:
        result = await model_capability.verify_resolved_agent_providers(db, resolved)

    assert not isinstance(result, str)
    assert result.primary.id == 1
    assert result.providers[2].enabled_model_ids() == ["fallback"]
    assert result.providers[2].models[0].get("supports_tools") is None


@pytest.mark.asyncio
async def test_verify_still_excludes_unsupported_fallback(setting_db, monkeypatch) -> None:
    primary = _provider(1, models=["primary"])
    fallback = _provider(2, models=["fallback"])
    resolved = ResolvedAgentProviders(
        primary=primary,
        model="primary",
        providers={1: primary, 2: fallback},
    )

    async def probe(provider, _model):  # noqa: ANN001
        return model_capability.CapabilityProbeResult(
            "supported" if provider.id == 1 else "unsupported",
            datetime.now(UTC),
        )

    monkeypatch.setattr(model_capability, "probe_model_tool_capability", probe)
    async with setting_db() as db:
        result = await model_capability.verify_resolved_agent_providers(db, resolved)

    assert not isinstance(result, str)
    assert set(result.providers) == {1}


@pytest.mark.asyncio
async def test_verify_non_blocking_skips_probe_and_schedules_refresh(
    setting_db,
    monkeypatch,
) -> None:
    primary = _provider(1, models=["m1"])
    resolved = ResolvedAgentProviders(primary=primary, model="m1", providers={1: primary})
    calls: list[tuple[int, str]] = []
    scheduled: list[list[tuple[int, str]]] = []

    async def probe(provider, model):  # noqa: ANN001
        calls.append((provider.id, model))
        return model_capability.CapabilityProbeResult("supported", datetime.now(UTC))

    def schedule(items):  # noqa: ANN001
        scheduled.append([(p.id, m) for p, m, _s in items])

    monkeypatch.setattr(model_capability, "probe_model_tool_capability", probe)
    monkeypatch.setattr(model_capability, "schedule_capability_refresh", schedule)

    async with setting_db() as db:
        result = await model_capability.verify_resolved_agent_providers(
            db, resolved, non_blocking=True
        )

    assert not isinstance(result, str)
    assert result.model == "m1"
    assert calls == []  # 请求路径不探测
    assert scheduled and scheduled[0] == [(1, "m1")]
