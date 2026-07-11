"""Adapters from agent lifecycle callbacks to TelePilot observability."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..db.models.action_event import (
    ACTION_EVENT_STATUS_FAILED,
    ACTION_EVENT_STATUS_OK,
    ACTION_EVENT_STATUS_PENDING,
)
from .action_tap import emit_action_event
from .llm_agent import AgentCallbacks
from .llm_protocol import ModelUsage, ToolCall, ToolResult

SpanRecorder = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class AgentObservationContext:
    account_id: int
    plugin_key: str | None = None
    entry_key: str | None = None
    session_key: str | None = None
    trace_id: str | None = None


def build_agent_observability_callbacks(
    context: AgentObservationContext,
    *,
    span_recorder: SpanRecorder | None = None,
) -> AgentCallbacks:
    """Record tools as ActionEvents and optionally mirror lifecycle spans."""

    async def record_span(event: str, detail: dict[str, Any]) -> None:
        if span_recorder is not None:
            await span_recorder(event, {**detail, "trace_id": context.trace_id})

    async def on_step(step: int) -> None:
        await record_span("agent_step", {"step": step})

    async def on_usage(usage: ModelUsage) -> None:
        await record_span(
            "agent_usage",
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
        )

    async def on_tool_start(call: ToolCall) -> None:
        await emit_action_event(
            account_id=context.account_id,
            action={
                "type": "agent_tool",
                "tool_name": call.name,
                "context": {
                    "plugin_key": context.plugin_key,
                    "entry_key": context.entry_key,
                    "session_key": context.session_key,
                    "trace_id": context.trace_id,
                },
            },
            status=ACTION_EVENT_STATUS_PENDING,
            plugin_key=context.plugin_key,
            entry_key=context.entry_key,
            session_key=context.session_key,
        )
        await record_span("agent_tool_start", {"tool_name": call.name, "call_id": call.id})

    async def on_tool_finish(call: ToolCall, result: ToolResult) -> None:
        await emit_action_event(
            account_id=context.account_id,
            action={
                "type": "agent_tool",
                "tool_name": call.name,
                "context": {
                    "plugin_key": context.plugin_key,
                    "entry_key": context.entry_key,
                    "session_key": context.session_key,
                    "trace_id": context.trace_id,
                },
            },
            status=ACTION_EVENT_STATUS_FAILED if result.is_error else ACTION_EVENT_STATUS_OK,
            error=result.content if result.is_error else None,
            result=result.content if not result.is_error else None,
            plugin_key=context.plugin_key,
            entry_key=context.entry_key,
            session_key=context.session_key,
        )
        await record_span(
            "agent_tool_finish",
            {"tool_name": call.name, "call_id": call.id, "ok": not result.is_error},
        )

    return AgentCallbacks(
        on_step=on_step,
        on_usage=on_usage,
        on_tool_start=on_tool_start,
        on_tool_finish=on_tool_finish,
    )


__all__ = [
    "AgentObservationContext",
    "build_agent_observability_callbacks",
]
