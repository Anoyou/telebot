from __future__ import annotations

import asyncio

import pytest

from app.services.llm_agent import (
    AgentCallbacks,
    AgentLimitError,
    AgentLimits,
    AgentTool,
    run_agent,
    tools_from_manifest,
)
from app.services.llm_protocol import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    StopReason,
    TextContent,
    ToolCall,
    ToolSpec,
)


def _request(*tools: ToolSpec) -> ModelRequest:
    return ModelRequest(
        model="test-model",
        messages=(
            ModelMessage.text(MessageRole.SYSTEM, "system"),
            ModelMessage.text(MessageRole.USER, "question"),
        ),
        tools=tuple(tools),
    )


@pytest.mark.asyncio
async def test_agent_executes_tool_then_returns_answer() -> None:
    spec = ToolSpec("lookup", "lookup", {"type": "object", "properties": {}})
    seen_requests: list[ModelRequest] = []

    async def model_call(request: ModelRequest) -> ModelResponse:
        seen_requests.append(request)
        if len(seen_requests) == 1:
            return ModelResponse(
                model="test-model",
                tool_calls=(ToolCall("call-1", "lookup", {"id": 7}),),
                usage=ModelUsage(input_tokens=2, output_tokens=1),
                stop_reason=StopReason.TOOL_CALLS,
            )
        assert request.messages[-1].tool_results[0].content == {"name": "item-7"}
        return ModelResponse(
            model="test-model",
            content=(TextContent("done"),),
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            stop_reason=StopReason.COMPLETED,
        )

    async def lookup(arguments: dict) -> object:
        return {"name": f"item-{arguments['id']}"}

    result = await run_agent(
        model_call,
        _request(spec),
        {"lookup": AgentTool(spec=spec, handler=lookup)},
    )

    assert result.text == "done"
    assert result.tool_calls == 1
    assert result.usage.total_tokens == 8


@pytest.mark.asyncio
async def test_agent_streams_real_deltas_and_reconciles_final_text() -> None:
    deltas: list[str] = []

    async def model_call(_request: ModelRequest) -> ModelResponse:
        raise AssertionError("configured stream path must be used")

    async def stream_model_call(_request: ModelRequest):
        yield ModelStreamEvent(delta="真")
        yield ModelStreamEvent(delta="流")
        yield ModelStreamEvent(
            response=ModelResponse(
                model="m",
                content=(TextContent("真流"),),
                usage=ModelUsage(input_tokens=2, output_tokens=2),
                stop_reason=StopReason.COMPLETED,
            )
        )

    result = await run_agent(
        model_call,
        _request(),
        {},
        stream_model_call=stream_model_call,
        callbacks=AgentCallbacks(on_text_delta=lambda delta: _append(deltas, delta)),
    )

    assert deltas == ["真", "流"]
    assert result.text == "真流"
    assert result.usage.total_tokens == 4


@pytest.mark.asyncio
async def test_agent_resets_tool_preface_before_streaming_final_answer() -> None:
    spec = ToolSpec("lookup", "lookup", {"type": "object", "properties": {}})
    events: list[str] = []
    calls = 0

    async def model_call(_request: ModelRequest) -> ModelResponse:
        raise AssertionError("configured stream path must be used")

    async def stream_model_call(_request: ModelRequest):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield ModelStreamEvent(delta="我先查询")
            yield ModelStreamEvent(
                response=ModelResponse(
                    model="m",
                    content=(TextContent("我先查询"),),
                    tool_calls=(ToolCall("call-1", "lookup", {}),),
                    stop_reason=StopReason.TOOL_CALLS,
                )
            )
            return
        yield ModelStreamEvent(delta="最终答案")
        yield ModelStreamEvent(
            response=ModelResponse(
                model="m",
                content=(TextContent("最终答案"),),
                stop_reason=StopReason.COMPLETED,
            )
        )

    async def lookup(_arguments: dict) -> str:
        return "ok"

    result = await run_agent(
        model_call,
        _request(spec),
        {"lookup": AgentTool(spec, lookup)},
        stream_model_call=stream_model_call,
        callbacks=AgentCallbacks(
            on_text_delta=lambda delta: _append(events, delta),
            on_text_reset=lambda: _append(events, "<reset>"),
        ),
    )

    assert events == ["我先查询", "<reset>", "最终答案"]
    assert result.text == "最终答案"


async def _append(target: list[str], value: str) -> None:
    target.append(value)


@pytest.mark.asyncio
async def test_agent_parallelizes_read_only_tools_and_emits_callbacks() -> None:
    left = ToolSpec("left", "left", {"type": "object"})
    right = ToolSpec("right", "right", {"type": "object"})
    active = 0
    max_active = 0
    events: list[str] = []

    async def handler(_arguments: dict) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    calls = 0

    async def model_call(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                model="m",
                tool_calls=(
                    ToolCall("1", "left", {}),
                    ToolCall("2", "right", {}),
                ),
            )
        return ModelResponse(model="m", content=(TextContent("done"),))

    async def on_start(call: ToolCall) -> None:
        events.append(f"start:{call.name}")

    result = await run_agent(
        model_call,
        _request(left, right),
        {
            "left": AgentTool(left, handler, read_only=True),
            "right": AgentTool(right, handler, read_only=True),
        },
        callbacks=AgentCallbacks(on_tool_start=on_start),
    )

    assert result.text == "done"
    assert max_active == 2
    assert set(events) == {"start:left", "start:right"}


@pytest.mark.asyncio
async def test_agent_checks_entire_tool_batch_before_any_handler_starts() -> None:
    left = ToolSpec("left", "left", {"type": "object"})
    right = ToolSpec("right", "right", {"type": "object"})
    executions: list[str] = []
    requested: list[tuple[str, ...]] = []

    async def model_call(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            model="m",
            tool_calls=(
                ToolCall("1", "left", {}),
                ToolCall("2", "right", {}),
            ),
        )

    async def handler(arguments: dict) -> str:
        executions.append(str(arguments))
        return "unexpected"

    async def reject_batch(calls: tuple[ToolCall, ...]) -> None:
        requested.append(tuple(call.name for call in calls))
        raise PermissionError("approval required")

    with pytest.raises(PermissionError, match="approval required"):
        await run_agent(
            model_call,
            _request(left, right),
            {
                "left": AgentTool(left, handler, read_only=True),
                "right": AgentTool(right, handler, read_only=True),
            },
            callbacks=AgentCallbacks(on_tool_batch=reject_batch),
        )

    assert requested == [("left", "right")]
    assert executions == []


@pytest.mark.asyncio
async def test_agent_repeated_call_is_blocked_without_reexecution() -> None:
    spec = ToolSpec("lookup", "lookup", {"type": "object"})
    executions = 0

    async def handler(_arguments: dict) -> str:
        nonlocal executions
        executions += 1
        return "same"

    async def model_call(request: ModelRequest) -> ModelResponse:
        if not request.tools:
            return ModelResponse(model="m", content=(TextContent("final"),))
        return ModelResponse(
            model="m",
            tool_calls=(ToolCall(f"call-{len(request.messages)}", "lookup", {"q": "same"}),),
        )

    result = await run_agent(
        model_call,
        _request(spec),
        {"lookup": AgentTool(spec, handler)},
        limits=AgentLimits(max_steps=4, max_same_call=2),
    )

    assert result.text == "final"
    assert executions == 2


@pytest.mark.asyncio
async def test_agent_enforces_session_token_limit() -> None:
    async def model_call(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            model="m",
            content=(TextContent("too much"),),
            usage=ModelUsage(input_tokens=10, output_tokens=10),
        )

    with pytest.raises(AgentLimitError):
        await run_agent(
            model_call,
            _request(),
            {},
            limits=AgentLimits(max_total_tokens=5),
        )


@pytest.mark.asyncio
async def test_agent_enforces_token_limit_on_forced_final_summary() -> None:
    spec = ToolSpec("lookup", "lookup", {"type": "object", "properties": {}})

    async def handler(_arguments: dict) -> str:
        return "ok"

    async def model_call(request: ModelRequest) -> ModelResponse:
        if request.tools:
            return ModelResponse(
                model="m",
                tool_calls=(ToolCall("call-1", "lookup", {}),),
                usage=ModelUsage(input_tokens=2, output_tokens=1),
            )
        return ModelResponse(
            model="m",
            content=(TextContent("final"),),
            usage=ModelUsage(input_tokens=4, output_tokens=3),
        )

    with pytest.raises(AgentLimitError):
        await run_agent(
            model_call,
            _request(spec),
            {"lookup": AgentTool(spec, handler)},
            limits=AgentLimits(max_steps=1, max_total_tokens=5),
        )


@pytest.mark.asyncio
async def test_agent_token_limit_uses_incremental_input_not_full_resend() -> None:
    """每步重发前缀时 input 全额很大，但增量口径只计增长+输出，不应误杀。"""
    step = 0
    usages_reported: list[ModelUsage] = []
    spec = ToolSpec("lookup", "lookup", {"type": "object", "properties": {}})

    async def handler(_arguments: dict) -> str:
        return "ok"

    async def model_call(request: ModelRequest) -> ModelResponse:
        nonlocal step
        step += 1
        # 3 步工具循环：每步 input 6k（前缀重发）、output 0.5k
        if step <= 2:
            return ModelResponse(
                model="m",
                tool_calls=(ToolCall(f"call-{step}", "lookup", {}),),
                usage=ModelUsage(input_tokens=6_000, output_tokens=500),
            )
        return ModelResponse(
            model="m",
            content=(TextContent("done"),),
            usage=ModelUsage(input_tokens=6_000, output_tokens=500),
        )

    async def on_usage(usage: ModelUsage) -> None:
        usages_reported.append(usage)

    result = await run_agent(
        model_call,
        _request(spec),
        {"lookup": AgentTool(spec, handler)},
        limits=AgentLimits(max_steps=5, max_total_tokens=16_384),
        callbacks=AgentCallbacks(on_usage=on_usage),
    )
    assert result.text == "done"
    assert step == 3
    # 全量累计仍上报：3 * (6000+500) = 19500（AI 页面口径不变）
    assert usages_reported[-1].total_tokens == 19_500
    # 若按旧口径 total 累计会在第 3 步前就超 16384；增量口径能跑完


@pytest.mark.asyncio
async def test_agent_token_limit_incremental_still_blocks_real_overuse() -> None:
    """输出持续累积真实超限时仍应抛 AgentLimitError。"""
    calls = 0

    async def model_call(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        # 每步 input 稳定 100，output 3000 → 增量约 3000/步，3 步后必超 5000
        return ModelResponse(
            model="m",
            content=(TextContent("x" * 10),) if calls >= 3 else (),
            tool_calls=(ToolCall(f"c{calls}", "lookup", {}),) if calls < 3 else (),
            usage=ModelUsage(input_tokens=100, output_tokens=3_000),
        )

    async def handler(_arguments: dict) -> str:
        return "ok"

    spec = ToolSpec("lookup", "lookup", {"type": "object", "properties": {}})
    with pytest.raises(AgentLimitError):
        await run_agent(
            model_call,
            _request(spec),
            {"lookup": AgentTool(spec, handler)},
            limits=AgentLimits(max_steps=5, max_total_tokens=5_000),
        )


@pytest.mark.asyncio
async def test_agent_token_limit_skips_zero_usage_steps() -> None:
    async def model_call(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            model="m",
            content=(TextContent("ok"),),
            usage=ModelUsage(),
        )

    result = await run_agent(
        model_call,
        _request(),
        {},
        limits=AgentLimits(max_total_tokens=1),
    )
    assert result.text == "ok"


def test_manifest_tools_require_capability_and_registered_handler() -> None:
    async def handler(_arguments: dict) -> str:
        return "ok"

    manifest = {
        "capabilities": {"agent_tools": {"enabled": True}},
        "agent_tools": [
            {
                "name": "lookup",
                "description": "Lookup",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "unregistered",
                "parameters": {"type": "object"},
            },
        ],
    }

    tools = tools_from_manifest(manifest, {"lookup": handler})

    assert list(tools) == ["lookup"]
    assert tools["lookup"].spec.strict is True
