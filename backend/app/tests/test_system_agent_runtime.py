from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.llm_agent import AgentResult
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import ModelResponse, ModelUsage, StopReason, TextContent
from app.services.system_agent import runtime as runtime_module
from app.services.system_agent.config import ResolvedAgentProviders
from app.services.system_agent.registry import ToolRegistry, ToolSpec
from app.services.system_agent.runtime import SystemAgentRuntime


def _registry() -> ToolRegistry:
    async def read_handler(_ctx, _args):  # noqa: ANN001
        return {"ok": True}

    registry = ToolRegistry()
    for name in ("logs.recent", "scheduler.list"):
        registry.register(
            ToolSpec(
                name=name,
                description=name,
                input_schema={"type": "object", "properties": {}},
                read_handler=read_handler,
            )
        )
    return registry


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id="session-1",
        account_id=None,
        memory_summary="",
        memory_state={},
    )


def _providers() -> tuple[LLMProviderDTO, LLMProviderDTO]:
    primary = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
        api_key_enc="encrypted",
    )
    fallback = LLMProviderDTO(
        id=2,
        name="fallback",
        provider="openai",
        api_format="responses",
        default_model="fallback-model",
        api_key_enc="encrypted",
    )
    return primary, fallback


async def _patch_runtime_config(monkeypatch, primary, fallback) -> None:  # noqa: ANN001
    async def load_flags(_db):  # noqa: ANN001
        return {
            "timezone": "UTC",
            "command_prefix": "/",
            "ai_enabled": True,
            "agent_config": {
                "enabled": True,
                "max_steps": 8,
                "max_tool_calls": 24,
                "session_token_limit": 16_384,
            },
        }

    async def resolve(_db, _cfg):  # noqa: ANN001
        return ResolvedAgentProviders(
            primary=primary,
            model=primary.default_model,
            providers={primary.id: primary, fallback.id: fallback},
        )

    monkeypatch.setattr(runtime_module, "load_system_context_flags", load_flags)
    monkeypatch.setattr(runtime_module, "resolve_agent_providers", resolve)


@pytest.mark.asyncio
async def test_runtime_exposes_only_routed_domain_and_sticks_to_fallback(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    calls: list[tuple[int, str]] = []

    async def invoke(provider, _providers, request, **_kwargs):  # noqa: ANN001
        calls.append((provider.id, request.model))
        if len(calls) == 1:
            return (
                ModelResponse(
                    model=fallback.default_model,
                    content=(TextContent("fallback step"),),
                    usage=ModelUsage(input_tokens=1, output_tokens=1),
                ),
                fallback,
                True,
            )
        return (
            ModelResponse(
                model=fallback.default_model,
                content=(TextContent("done"),),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            fallback,
            False,
        )

    async def run(model_call, request, tools, **_kwargs):  # noqa: ANN001
        assert [tool.name for tool in request.tools] == ["scheduler.list"]
        assert list(tools) == ["scheduler.list"]
        await model_call(request)
        second = await model_call(request)
        return AgentResult(
            text=second.text,
            model=second.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=2, output_tokens=2),
            steps=2,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "invoke_structured", invoke)
    monkeypatch.setattr(runtime_module, "run_agent", run)

    events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="帮我看看定时任务",
            role="admin",
            channel="web",
        )
    ]

    assert calls == [(primary.id, primary.default_model), (fallback.id, fallback.default_model)]
    route = next(event for event in events if event["type"] == "route_selected")
    assert route["domains"] == ["scheduler"]
    assert route["tool_count"] == 1
    done = next(event for event in events if event["type"] == "done")
    assert done["used_fallback"] is True


@pytest.mark.asyncio
async def test_runtime_general_help_sends_zero_tool_definitions(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)

    async def run(_model_call, request, tools, **_kwargs):  # noqa: ANN001
        assert request.tools == ()
        assert tools == {}
        return AgentResult(
            text="帮助",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "run_agent", run)

    events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="你能做什么？",
            role="admin",
            channel="web",
        )
    ]

    route = next(event for event in events if event["type"] == "route_selected")
    assert route["tool_count"] == 0
