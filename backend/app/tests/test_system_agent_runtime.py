from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.llm_agent import AgentResult
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import ModelResponse, ModelUsage, StopReason, TextContent, ToolCall
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
                description=(
                    "读取最近运行日志。"
                    if name == "logs.recent"
                    else "列出定时任务。"
                ),
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


async def _patch_runtime_config(  # noqa: ANN001
    monkeypatch,
    primary,
    fallback,
    *,
    require_tool_approval: bool = False,
) -> None:
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
                "require_tool_approval": require_tool_approval,
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
    async def verify(_db, resolved):  # noqa: ANN001
        return resolved

    monkeypatch.setattr(runtime_module, "verify_resolved_agent_providers", verify)


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
    capability = next(event for event in events if event["type"] == "model_capability_check")
    assert capability["provider_name"] == primary.name
    assert capability["model"] == primary.default_model


@pytest.mark.asyncio
async def test_runtime_emits_provider_switch_confirmation(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)

    async def invoke(*_args, **_kwargs):  # noqa: ANN001
        raise runtime_module.ProviderSwitchRequired(
            provider_name=primary.name,
            candidates=[
                {
                    "provider_id": fallback.id,
                    "provider_name": fallback.name,
                    "model": fallback.default_model,
                }
            ],
        )

    async def run(model_call, request, _tools, **_kwargs):  # noqa: ANN001
        await model_call(request)
        raise AssertionError("provider switch should stop this turn")

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

    error = next(event for event in events if event["type"] == "error")
    assert error["code"] == "AGENT_PROVIDER_SWITCH_REQUIRED"
    assert error["provider_switch"]["candidates"][0]["provider_id"] == fallback.id
    assert events[-1]["type"] == "done"
    assert events[-1]["ok"] is False


@pytest.mark.asyncio
async def test_provider_switch_keeps_existing_tool_approval(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(
        monkeypatch,
        primary,
        fallback,
        require_tool_approval=True,
    )

    async def invoke(*_args, **_kwargs):  # noqa: ANN001
        raise runtime_module.ProviderSwitchRequired(
            provider_name=primary.name,
            candidates=[
                {
                    "provider_id": fallback.id,
                    "provider_name": fallback.name,
                    "model": fallback.default_model,
                }
            ],
        )

    async def run(model_call, request, _tools, **_kwargs):  # noqa: ANN001
        await model_call(request)
        raise AssertionError("provider switch should stop this turn")

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
            approved_tools=["scheduler.list"],
        )
    ]

    error = next(event for event in events if event["type"] == "error")
    assert error["code"] == "AGENT_PROVIDER_SWITCH_REQUIRED"
    assert error["tool_approval"]["tools"][0]["name"] == "scheduler.list"


@pytest.mark.asyncio
async def test_runtime_emits_heartbeat_while_provider_is_waiting(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    real_wait = runtime_module.asyncio.wait
    wait_calls = 0

    async def wait_once_pending(tasks, *, timeout, **kwargs):  # noqa: ANN001
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            return set(), set(tasks)
        return await real_wait(tasks, timeout=timeout, **kwargs)

    async def run(_model_call, request, _tools, **_kwargs):  # noqa: ANN001
        return AgentResult(
            text="ok",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module.asyncio, "wait", wait_once_pending)
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

    heartbeat = next(event for event in events if event["type"] == "heartbeat")
    assert heartbeat["provider_name"] == primary.name
    assert heartbeat["model"] == primary.default_model


@pytest.mark.asyncio
async def test_runtime_requires_and_accepts_web_tool_approval(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(
        monkeypatch,
        primary,
        fallback,
        require_tool_approval=True,
    )
    run_calls = 0

    async def run(_model_call, request, _tools, *, callbacks, **_kwargs):  # noqa: ANN001
        nonlocal run_calls
        run_calls += 1
        assert request.metadata["max_retries_per_model"] == 5
        assert request.metadata["retry_delay_seconds"] == 3.0
        assert callbacks.on_tool_batch is not None
        await callbacks.on_tool_batch(
            (ToolCall(id="call-1", name="scheduler.list", arguments={}),)
        )
        return AgentResult(
            text="ok",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "run_agent", run)
    blocked = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="帮我看看定时任务",
            role="admin",
            channel="web",
        )
    ]

    error = next(event for event in blocked if event["type"] == "error")
    assert error["code"] == "AGENT_TOOL_APPROVAL_REQUIRED"
    assert [tool["name"] for tool in error["tool_approval"]["tools"]] == [
        "scheduler.list"
    ]
    assert error["tool_approval"]["tools"][0]["description"] == "列出定时任务。"
    assert run_calls == 1

    approved = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="帮我看看定时任务",
            role="admin",
            channel="web",
            approved_tools=["scheduler.list"],
        )
    ]
    assert run_calls == 2
    assert approved[-1]["type"] == "done"
    assert approved[-1]["ok"] is True


@pytest.mark.asyncio
async def test_runtime_streams_tool_started_before_agent_finishes(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    release = runtime_module.asyncio.Event()

    async def run(_model_call, request, _tools, *, callbacks, **_kwargs):  # noqa: ANN001
        assert callbacks.on_tool_start is not None
        await callbacks.on_tool_start(
            ToolCall(id="call-1", name="scheduler.list", arguments={})
        )
        await release.wait()
        return AgentResult(
            text="ok",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=1,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "run_agent", run)
    stream = SystemAgentRuntime(_registry()).stream_turn(
        None,  # type: ignore[arg-type]
        session=_session(),  # type: ignore[arg-type]
        user_text="帮我看看定时任务",
        role="admin",
        channel="web",
    )
    seen: list[dict] = []
    while True:
        event = await anext(stream)
        seen.append(event)
        if event["type"] == "tool_started":
            break

    assert release.is_set() is False
    assert seen[-1]["tool_name"] == "scheduler.list"
    release.set()
    remaining = [event async for event in stream]
    assert remaining[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_runtime_registers_usage_callback_at_agent_entry(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    calls: list[bool] = []

    from app.services import llm_usage_service

    monkeypatch.setattr(
        llm_usage_service,
        "ensure_llm_usage_callback_registered",
        lambda: calls.append(True),
    )

    async def run(_model_call, request, _tools, **_kwargs):  # noqa: ANN001
        return AgentResult(
            text="ok",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "run_agent", run)
    _events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="你能做什么？",
            role="admin",
            channel="web",
        )
    ]
    assert calls == [True]


@pytest.mark.asyncio
async def test_model_router_only_allows_confirmed_cross_provider(monkeypatch) -> None:
    primary, fallback = _providers()
    captured_metadata: list[dict] = []

    async def invoke(_provider, _providers, request, **_kwargs):  # noqa: ANN001
        captured_metadata.append(dict(request.metadata))
        return (
            ModelResponse(
                model=request.model,
                content=(
                    TextContent(
                        '{"needs_tools":true,"domains":["scheduler"],"reason":"lookup"}'
                    ),
                ),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            primary,
            False,
        )

    monkeypatch.setattr(runtime_module, "invoke_structured", invoke)
    runtime = SystemAgentRuntime(_registry())
    route = await runtime._resolve_tool_route(
        provider_dto=primary,
        providers={primary.id: primary, fallback.id: fallback},
        model=primary.default_model,
        user_text="帮我查一下相关配置",
        memory_state={},
        all_tool_specs=_registry().list_for(channel="web", role="admin"),
        account_id=None,
        fallback_provider_id=fallback.id,
    )

    assert route.domains == ("scheduler",)
    assert captured_metadata[0]["confirm_provider_switch"] is True
    assert captured_metadata[0]["allowed_cross_provider_ids"] == [fallback.id]
