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
