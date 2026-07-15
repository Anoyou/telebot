from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import llm_agent_observability as observability
from app.services.llm_agent_observability import AgentObservationContext
from app.services.llm_protocol import ModelUsage, ToolCall, ToolResult


@pytest.mark.asyncio
async def test_agent_callbacks_emit_action_events_and_spans(monkeypatch) -> None:
    emit = AsyncMock()
    spans: list[tuple[str, dict]] = []

    async def span(event: str, detail: dict) -> None:
        spans.append((event, detail))

    monkeypatch.setattr(observability, "emit_action_event", emit)
    callbacks = observability.build_agent_observability_callbacks(
        AgentObservationContext(
            account_id=7,
            plugin_key="agent",
            entry_key="chat",
            session_key="session-1",
            trace_id="trace-1",
        ),
        span_recorder=span,
    )
    call = ToolCall("call-1", "lookup", {})
    await callbacks.on_step(1)
    await callbacks.on_usage(ModelUsage(input_tokens=2, output_tokens=1))
    await callbacks.on_tool_start(call)
    await callbacks.on_tool_finish(
        call,
        ToolResult("call-1", "lookup", {"value": 1}),
    )

    assert emit.await_count == 2
    assert emit.await_args_list[0].kwargs["status"] == "PENDING"
    assert emit.await_args_list[1].kwargs["status"] == "OK"
    assert [event for event, _detail in spans] == [
        "agent_step",
        "agent_usage",
        "agent_tool_start",
        "agent_tool_finish",
    ]
