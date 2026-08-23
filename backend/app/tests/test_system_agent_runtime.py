from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm_agent import AgentResult
from app.services.llm_client import LLMError
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import (
    ImageContent,
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    StopReason,
    TextContent,
    ToolCall,
    ToolResult,
)
from app.services.system_agent import runtime as runtime_module
from app.services.system_agent.config import ResolvedAgentProviders
from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import ToolRegistry, ToolSpec
from app.services.system_agent.runtime import SystemAgentRuntime


def test_request_image_metadata_excludes_payload_and_url() -> None:
    request = ModelRequest(
        model="vision-model",
        messages=(
            ModelMessage(
                role=MessageRole.USER,
                content=(
                    ImageContent(data=b"inline-image", mime_type="image/png"),
                    ImageContent(url="https://example.com/private.png?token=secret"),
                ),
            ),
        ),
    )

    metadata = runtime_module._request_image_metadata(request)

    assert metadata == [
        {
            "source": "inline",
            "mime_type": "image/png",
            "bytes": 12,
            "sha256": hashlib.sha256(b"inline-image").hexdigest(),
        },
        {
            "source": "url",
            "mime_type": None,
            "bytes": None,
            "sha256": None,
        },
    ]
    assert "secret" not in str(metadata)


def _registry() -> ToolRegistry:
    async def read_handler(_ctx, _args):  # noqa: ANN001
        return {"ok": True}

    registry = ToolRegistry()
    for name in ("logs.recent", "scheduler.list"):
        registry.register(
            ToolSpec(
                name=name,
                description=("读取最近运行日志。" if name == "logs.recent" else "列出定时任务。"),
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


def test_run_trace_usage_includes_gateway_transport_facts() -> None:
    provider = LLMProviderDTO(
        id=9,
        name="gateway",
        provider="openai",
        execution_backend="codex_gateway",
        api_format="responses",
        default_model="gpt-x",
    )

    usage = runtime_module._usage_payload(
        ModelUsage(input_tokens=3, output_tokens=2),
        provider,
        "gpt-x",
        execution_backend="codex_gateway",
        gateway_version="0.1.0-beta.1",
        gateway_request_id="gw-trace-1",
        gateway_stage=None,
    )

    assert usage["execution_backend"] == "codex_gateway"
    assert usage["gateway_version"] == "0.1.0-beta.1"
    assert usage["gateway_request_id"] == "gw-trace-1"
    assert usage["gateway_stage"] is None


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

    async def verify(_db, resolved, **_kwargs):  # noqa: ANN001
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

    db = AsyncMock()
    events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            db,
            session=_session(),  # type: ignore[arg-type]
            user_text="帮我看看定时任务",
            role="admin",
            channel="web",
        )
    ]

    db.commit.assert_awaited_once_with()
    assert calls == [(primary.id, primary.default_model), (fallback.id, fallback.default_model)]
    route = next(event for event in events if event["type"] == "route_selected")
    assert route["domains"] == ["scheduler"]
    assert route["tool_count"] == 1
    done = next(event for event in events if event["type"] == "done")
    assert done["used_fallback"] is True


@pytest.mark.asyncio
async def test_runtime_does_not_reuse_upstream_model_alias(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    calls: list[str] = []

    async def invoke(provider, _providers, request, **kwargs):  # noqa: ANN001
        calls.append(request.model)
        progress = kwargs["progress_callback"]
        await progress(
            {
                "type": "model_attempt",
                "provider_id": provider.id,
                "provider_name": provider.name,
                "model": request.model,
                "attempt": 1,
                "max_retries": 5,
            }
        )
        return (
            ModelResponse(
                model="upstream-backend-alias",
                content=(TextContent("done"),),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            provider,
            False,
        )

    async def run(model_call, request, _tools, **_kwargs):  # noqa: ANN001
        await model_call(request)
        second = await model_call(request)
        return AgentResult(
            text=second.text,
            model=second.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=2, output_tokens=2),
            steps=2,
            tool_calls=1,
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

    assert calls == [primary.default_model, primary.default_model]
    usage = next(event for event in events if event["type"] == "assistant_message")["usage"]
    assert usage["requested_model"] == primary.default_model
    assert usage["model"] == "upstream-backend-alias"
    assert not any(
        event.get("type") == "provider_selected" and event.get("reason") == "model_fallback"
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_runtime_does_not_reuse_upstream_model_alias(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    calls: list[str] = []

    async def stream(provider, _providers, request, **kwargs):  # noqa: ANN001
        calls.append(request.model)
        await kwargs["progress_callback"](
            {
                "type": "model_attempt",
                "provider_id": provider.id,
                "provider_name": provider.name,
                "model": request.model,
                "attempt": 1,
                "max_retries": 5,
            }
        )
        yield (
            ModelStreamEvent(
                response=ModelResponse(
                    model="upstream-stream-alias",
                    content=(TextContent("done"),),
                    usage=ModelUsage(input_tokens=1, output_tokens=1),
                )
            ),
            provider,
            False,
        )

    async def run(_model_call, request, _tools, *, stream_model_call, **_kwargs):  # noqa: ANN001
        responses = []
        for _ in range(2):
            async for event in stream_model_call(request):
                if event.response is not None:
                    responses.append(event.response)
        second = responses[-1]
        return AgentResult(
            text=second.text,
            model=second.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=2, output_tokens=2),
            steps=2,
            tool_calls=1,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "stream_structured", stream)
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

    assert calls == [primary.default_model, primary.default_model]
    usage = next(event for event in events if event["type"] == "assistant_message")["usage"]
    assert usage["requested_model"] == primary.default_model
    assert usage["model"] == "upstream-stream-alias"


@pytest.mark.asyncio
async def test_runtime_general_help_sends_zero_tool_definitions(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    run_called = False

    async def run(_model_call, request, tools, **_kwargs):  # noqa: ANN001
        nonlocal run_called
        run_called = True
        assert request.tools == ()
        assert tools == {}
        assert request.metadata["repair_text_tool_protocol"] is True
        assert request.metadata["retry_false_image_refusal"] is True
        assert "本轮未提供任何工具" in request.messages[0].text_content()
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
    assert run_called is True


@pytest.mark.asyncio
async def test_runtime_marks_uploaded_images_as_native_visual_input(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)

    async def run(_model_call, request, tools, **_kwargs):  # noqa: ANN001
        assert tools == {}
        assert "图片已经作为模型原生视觉输入提供，不属于工具" in request.messages[
            0
        ].text_content()
        assert isinstance(request.messages[-1].content[-1], ImageContent)
        return AgentResult(
            text="图中是一只猫。",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=10, output_tokens=5),
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
            user_text="图里是什么？",
            user_images=(ImageContent(data=b"image", mime_type="image/png"),),
            role="admin",
            channel="web",
        )
    ]

    answer = next(event for event in events if event["type"] == "assistant_message")
    assert answer["content"] == "图中是一只猫。"
    assert any(event["type"] == "assistant_message" for event in events)
    capability = next(event for event in events if event["type"] == "model_capability_check")
    assert capability["provider_name"] == primary.name
    assert capability["model"] == primary.default_model


@pytest.mark.asyncio
async def test_pinned_selection_bypasses_stale_global_provider_config(monkeypatch) -> None:
    selected, _fallback = _providers()
    resolve_calls = 0
    pinned_calls: list[dict[str, object]] = []

    async def load_flags(_db):  # noqa: ANN001
        return {
            "timezone": "UTC",
            "command_prefix": "/",
            "ai_enabled": True,
            "agent_config": {
                "enabled": True,
                "provider_id": 999,
                "model": "stale-global-model",
                "max_steps": 8,
                "max_tool_calls": 24,
                "session_token_limit": 16_384,
                "require_tool_approval": False,
            },
        }

    async def resolve(_db, _cfg):  # noqa: ANN001
        nonlocal resolve_calls
        resolve_calls += 1
        return "全局默认模型已经失效"

    async def apply_pinned(_db, resolved, selection):  # noqa: ANN001
        assert resolved is None
        pinned_calls.append(dict(selection))
        return ResolvedAgentProviders(
            primary=selected,
            model=selected.default_model,
            providers={selected.id: selected},
        )

    async def verify(_db, resolved, **_kwargs):  # noqa: ANN001
        return resolved

    async def run(_model_call, request, _tools, **_kwargs):  # noqa: ANN001
        return AgentResult(
            text="帮助",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "load_system_context_flags", load_flags)
    monkeypatch.setattr(runtime_module, "resolve_agent_providers", resolve)
    monkeypatch.setattr(runtime_module, "_apply_pinned_selection", apply_pinned)
    monkeypatch.setattr(runtime_module, "verify_resolved_agent_providers", verify)
    monkeypatch.setattr(runtime_module, "run_agent", run)

    pinned_events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="你能做什么？",
            role="admin",
            channel="web",
            model_selection={
                "mode": "pinned",
                "provider_id": selected.id,
                "model": selected.default_model,
            },
        )
    ]

    assert resolve_calls == 0
    assert pinned_calls == [
        {
            "mode": "pinned",
            "provider_id": selected.id,
            "model": selected.default_model,
        }
    ]
    provider_event = next(event for event in pinned_events if event["type"] == "provider_selected")
    assert provider_event["provider_id"] == selected.id
    assert provider_event["model"] == selected.default_model
    assert provider_event["selection_mode"] == "pinned"
    assert pinned_events[-1]["type"] == "done"
    assert pinned_events[-1]["ok"] is True

    auto_events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="你能做什么？",
            role="admin",
            channel="web",
        )
    ]
    assert resolve_calls == 1
    auto_error = next(event for event in auto_events if event["type"] == "error")
    assert auto_error["code"] == "PROVIDER_UNAVAILABLE"
    assert auto_error["message"] == "全局默认模型已经失效"


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
            last_error=LLMError(
                "gateway unavailable",
                category="gateway_unavailable",
                request_id="gw-switch-1",
                gateway_stage="transport",
                gateway_version="0.1.0-beta.1",
                execution_backend="codex_gateway",
            ),
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
    assert error["execution_backend"] == "codex_gateway"
    assert error["gateway_version"] == "0.1.0-beta.1"
    assert error["gateway_request_id"] == "gw-switch-1"
    assert error["gateway_stage"] == "transport"
    assert events[-1]["type"] == "done"
    assert events[-1]["ok"] is False


@pytest.mark.asyncio
async def test_runtime_emits_verified_upstream_error_facts(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)

    async def run(_model_call, _request, _tools, **_kwargs):  # noqa: ANN001
        raise LLMError(
            "Upstream request failed",
            status_code=502,
            upstream_status_code=400,
            upstream_error_message="Unsupported parameter: max_output_tokens",
            upstream_error_detail='{"detail":"Unsupported parameter: max_output_tokens"}',
            upstream_request_id="sub2api-request",
            client_request_id="sub2api-client-request",
            request_id="gateway-request",
            execution_backend="codex_gateway",
        )

    monkeypatch.setattr(runtime_module, "run_agent", run)

    events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="测试模型",
            role="admin",
            channel="web",
        )
    ]

    error = next(event for event in events if event["type"] == "error")
    assert error["code"] == "AGENT_RUN_FAILED"
    assert error["message"] == "上游 HTTP 400：Unsupported parameter: max_output_tokens"
    assert error["status_code"] == 502
    assert error["error_category"] == "request_invalid"
    assert error["upstream_status_code"] == 400
    assert error["upstream_error_message"] == "Unsupported parameter: max_output_tokens"
    assert error["upstream_error_detail"] == '{"detail":"Unsupported parameter: max_output_tokens"}'
    assert error["upstream_request_id"] == "sub2api-request"
    assert error["client_request_id"] == "sub2api-client-request"
    assert error["gateway_request_id"] == "gateway-request"
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
        await callbacks.on_tool_batch((ToolCall(id="call-1", name="scheduler.list", arguments={"limit": 5}),))
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
    assert [tool["name"] for tool in error["tool_approval"]["tools"]] == ["scheduler.list"]
    assert error["tool_approval"]["tools"][0]["description"] == "列出定时任务。"
    assert error["tool_approval"]["tools"][0]["call_id"] == "call-1"
    assert error["tool_approval"]["calls"] == [
        {"call_id": "call-1", "name": "scheduler.list"}
    ]
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
        await callbacks.on_tool_start(ToolCall(id="call-1", name="scheduler.list", arguments={}))
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
async def test_runtime_redacts_secret_added_by_steer_from_all_events(monkeypatch) -> None:
    primary, fallback = _providers()
    await _patch_runtime_config(monkeypatch, primary, fallback)
    secret = "sk-proj-steer-secret-1234567890"
    provider_calls = 0

    async def run(_model_call, request, _tools, *, callbacks, **_kwargs):  # noqa: ANN001
        steering = await callbacks.on_safe_boundary()
        assert len(steering) == 1
        assert secret in steering[0].text_content()
        assert any(isinstance(item, ImageContent) for item in steering[0].content)
        await callbacks.on_tool_start(
            ToolCall(
                id="call-steer-secret",
                name="scheduler.list",
                arguments={"api_key": secret},
            )
        )
        await callbacks.on_tool_finish(
            ToolCall(
                id="call-steer-secret",
                name="scheduler.list",
                arguments={"api_key": secret},
            ),
            ToolResult(
                call_id="call-steer-secret",
                name="scheduler.list",
                content={"echo": secret},
            ),
        )
        await callbacks.on_text_delta(f"流式回显 {secret}")
        return AgentResult(
            text=f"最终回显 {secret}",
            reasoning_content=f"思考回显 {secret}",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=1,
            stop_reason=StopReason.COMPLETED,
        )

    async def provide_steer() -> list[str | dict]:
        nonlocal provider_calls
        provider_calls += 1
        return [
            {
                "content": f"改用备用配置，token={secret}",
                "attachments": [
                    {
                        "kind": "image",
                        "source": "data_url",
                        "data_url": "data:image/png;base64,iVBORw0KGgo=",
                    }
                ],
            }
        ] if provider_calls == 1 else []

    monkeypatch.setattr(runtime_module, "run_agent", run)
    events = [
        event
        async for event in SystemAgentRuntime(_registry()).stream_turn(
            None,  # type: ignore[arg-type]
            session=_session(),  # type: ignore[arg-type]
            user_text="帮我看看定时任务",
            role="admin",
            channel="web",
            run_input_provider=provide_steer,
        )
    ]

    assert secret not in repr(events)
    assert "[REDACTED]" in repr(events)
    steer_event = next(event for event in events if event["type"] == "steer_applied")
    assert steer_event["summary"] == "改用备用配置，token=[REDACTED]"
    assistant = next(event for event in events if event["type"] == "assistant_message")
    assert assistant["content"] == "最终回显 [REDACTED]"
    assert assistant["reasoning"] == "思考回显 [REDACTED]"


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
                content=(TextContent('{"needs_tools":true,"domains":["scheduler"],"reason":"lookup"}'),),
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


@pytest.mark.asyncio
async def test_model_router_timeout_keeps_explicit_web_intent(monkeypatch) -> None:
    primary, fallback = _providers()

    async def invoke(*_args, **_kwargs):  # noqa: ANN001
        raise TimeoutError("router timeout")

    async def read_handler(_ctx, _args):  # noqa: ANN001
        return {}

    monkeypatch.setattr(runtime_module, "invoke_structured", invoke)
    web_spec = ToolSpec(
        name="web.search",
        description="搜索公开互联网。",
        input_schema={"type": "object", "properties": {}},
        read_handler=read_handler,
    )

    route = await SystemAgentRuntime()._resolve_tool_route(
        provider_dto=primary,
        providers={primary.id: primary, fallback.id: fallback},
        model=primary.default_model,
        user_text="查一下 Sam Altman 最近说了什么",
        memory_state={},
        all_tool_specs=[web_spec],
        account_id=None,
        fallback_provider_id=fallback.id,
    )

    assert route == runtime_module.ToolRoute(
        ("web",),
        "fallback",
        "router_failed_explicit_web_intent",
    )


@pytest.mark.asyncio
async def test_read_tool_result_is_redacted_before_model_context() -> None:
    seen_db = None

    async def read_handler(handler_ctx, _args):  # noqa: ANN001
        nonlocal seen_db
        seen_db = handler_ctx.db
        return {"api_key": "plain-tool-secret", "value": "ok"}

    spec = ToolSpec(
        name="demo.read",
        description="demo",
        input_schema={"type": "object"},
        read_handler=read_handler,
    )
    owner_db = AsyncMock()
    tool_db = AsyncMock()
    tool_db.__aenter__.return_value = tool_db
    tool_db.__aexit__.return_value = False
    ctx = ToolContext(
        db=owner_db,
        channel="web",
        role="admin",
    )

    runtime = SystemAgentRuntime(tool_session_factory=lambda: tool_db)  # type: ignore[arg-type]
    result = await runtime._bind_read_handler(spec, ctx)({})  # noqa: SLF001

    assert result["api_key"] == "***"
    assert "plain-tool-secret" not in str(result)
    assert seen_db is tool_db
    tool_db.commit.assert_awaited_once_with()
    tool_db.rollback.assert_not_awaited()
    owner_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_exception_redacts_opaque_chat_secret() -> None:
    secret = "opaque-secret-from-chat"

    async def read_handler(_ctx, _args):  # noqa: ANN001
        raise RuntimeError(f"upstream echoed {secret}")

    spec = ToolSpec(
        name="demo.read",
        description="demo",
        input_schema={"type": "object"},
        read_handler=read_handler,
    )
    owner_db = AsyncMock()
    tool_db = AsyncMock()
    tool_db.__aenter__.return_value = tool_db
    tool_db.__aexit__.return_value = False
    ctx = ToolContext(
        db=owner_db,
        channel="web",
        role="admin",
        chat_secrets=[secret],
    )

    runtime = SystemAgentRuntime(tool_session_factory=lambda: tool_db)  # type: ignore[arg-type]
    result = await runtime._bind_read_handler(spec, ctx)({})  # noqa: SLF001

    assert secret not in str(result)
    assert "[REDACTED]" in result["message"]
    tool_db.rollback.assert_awaited_once_with()
    tool_db.commit.assert_not_awaited()
    owner_db.rollback.assert_not_awaited()
