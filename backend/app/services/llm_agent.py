"""Bounded, provider-neutral agent loop built on structured LLM requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .llm_protocol import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
)

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
ModelCall = Callable[[ModelRequest], Awaitable[ModelResponse]]


@dataclass(frozen=True)
class AgentTool:
    spec: ToolSpec
    handler: ToolHandler
    read_only: bool = True


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 8
    max_tool_calls: int = 24
    max_calls_per_turn: int = 8
    max_same_call: int = 3
    max_total_tokens: int = 50_000
    timeout_seconds: float = 180.0


@dataclass
class AgentCallbacks:
    on_step: Callable[[int], Awaitable[None]] | None = None
    on_usage: Callable[[ModelUsage], Awaitable[None]] | None = None
    on_tool_start: Callable[[ToolCall], Awaitable[None]] | None = None
    on_tool_finish: Callable[[ToolCall, ToolResult], Awaitable[None]] | None = None


@dataclass(frozen=True)
class AgentResult:
    text: str
    model: str
    messages: tuple[ModelMessage, ...]
    usage: ModelUsage
    steps: int
    tool_calls: int
    stop_reason: StopReason


class AgentLimitError(RuntimeError):
    pass


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def tools_from_manifest(
    manifest: Mapping[str, Any],
    handlers: Mapping[str, ToolHandler],
) -> dict[str, AgentTool]:
    """Build tools from manifest metadata, limited to host-registered handlers."""

    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return {}
    declaration = capabilities.get("agent_tools")
    if declaration is not True and not (
        isinstance(declaration, Mapping) and declaration.get("enabled") is True
    ):
        return {}
    raw_tools = manifest.get("agent_tools")
    if not isinstance(raw_tools, list):
        return {}
    result: dict[str, AgentTool] = {}
    for raw in raw_tools:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        handler = handlers.get(name)
        parameters = raw.get("parameters")
        if (
            handler is None
            or not _TOOL_NAME_RE.fullmatch(name)
            or not isinstance(parameters, dict)
            or parameters.get("type") != "object"
        ):
            continue
        result[name] = AgentTool(
            spec=ToolSpec(
                name=name,
                description=str(raw.get("description") or name).strip()[:500],
                parameters=dict(parameters),
                strict=bool(raw.get("strict", True)),
            ),
            handler=handler,
            read_only=bool(raw.get("read_only", True)),
        )
    return result


def _sum_usage(current: ModelUsage, value: ModelUsage) -> ModelUsage:
    return ModelUsage(
        input_tokens=current.input_tokens + value.input_tokens,
        output_tokens=current.output_tokens + value.output_tokens,
        cache_read_tokens=current.cache_read_tokens + value.cache_read_tokens,
        cache_write_tokens=current.cache_write_tokens + value.cache_write_tokens,
        reasoning_tokens=current.reasoning_tokens + value.reasoning_tokens,
    )


def _fingerprint(call: ToolCall) -> str:
    canonical = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{call.name}:{canonical}".encode()).hexdigest()


async def _notify(callback: Callable[..., Awaitable[None]] | None, *args: object) -> None:
    if callback is not None:
        await callback(*args)


async def _execute_tool(call: ToolCall, tool: AgentTool, callbacks: AgentCallbacks) -> ToolResult:
    await _notify(callbacks.on_tool_start, call)
    try:
        value = await tool.handler(dict(call.arguments))
        result = ToolResult(call_id=call.id, name=call.name, content=value)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        result = ToolResult(
            call_id=call.id,
            name=call.name,
            content={"error": type(exc).__name__, "message": str(exc)[:500]},
            is_error=True,
        )
    await _notify(callbacks.on_tool_finish, call, result)
    return result


async def _execute_selected_calls(
    calls: list[ToolCall],
    tools: Mapping[str, AgentTool],
    callbacks: AgentCallbacks,
) -> tuple[ToolResult, ...]:
    async def run(call: ToolCall) -> ToolResult:
        tool = tools.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content={"error": "tool_not_allowed"},
                is_error=True,
            )
        return await _execute_tool(call, tool, callbacks)

    if calls and all(tools.get(call.name) and tools[call.name].read_only for call in calls):
        return tuple(await asyncio.gather(*(run(call) for call in calls)))
    results: list[ToolResult] = []
    for call in calls:
        results.append(await run(call))
    return tuple(results)


async def run_agent(
    model_call: ModelCall,
    request: ModelRequest,
    tools: Mapping[str, AgentTool],
    *,
    limits: AgentLimits | None = None,
    callbacks: AgentCallbacks | None = None,
) -> AgentResult:
    """Run a bounded tool loop; only explicitly supplied tools are executable."""

    limits = limits or AgentLimits()
    callbacks = callbacks or AgentCallbacks()
    if min(
        limits.max_steps,
        limits.max_tool_calls,
        limits.max_calls_per_turn,
        limits.max_same_call,
        limits.max_total_tokens,
    ) <= 0:
        raise ValueError("agent limits must be positive")
    declared = {tool.name for tool in request.tools}
    if declared != set(tools):
        raise ValueError("ModelRequest.tools 必须与可执行工具白名单完全一致")

    messages = list(request.messages)
    usage = ModelUsage()
    fingerprints: dict[str, int] = {}
    tool_call_count = 0
    started = time.monotonic()

    async def call(current: ModelRequest) -> ModelResponse:
        remaining = limits.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Agent 会话已超时")
        async with asyncio.timeout(remaining):
            return await model_call(current)

    for step in range(1, limits.max_steps + 1):
        await _notify(callbacks.on_step, step)
        response = await call(replace(request, messages=tuple(messages), stream=False))
        usage = _sum_usage(usage, response.usage)
        await _notify(callbacks.on_usage, usage)
        if usage.total_tokens > limits.max_total_tokens:
            raise AgentLimitError("Agent 会话 token 总量超过限制")
        if not response.tool_calls:
            if not response.text:
                raise RuntimeError("模型既未返回文本，也未调用工具")
            messages.append(
                ModelMessage(
                    role=MessageRole.ASSISTANT,
                    content=response.content,
                )
            )
            return AgentResult(
                text=response.text,
                model=response.model,
                messages=tuple(messages),
                usage=usage,
                steps=step,
                tool_calls=tool_call_count,
                stop_reason=response.stop_reason,
            )

        remaining_calls = limits.max_tool_calls - tool_call_count
        selected = list(response.tool_calls[: min(limits.max_calls_per_turn, remaining_calls)])
        if not selected:
            raise AgentLimitError("Agent 工具调用总量超过限制")
        messages.append(
            ModelMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=tuple(selected),
            )
        )

        executable: list[ToolCall] = []
        blocked_results: list[ToolResult] = []
        for tool_call in selected:
            fingerprint = _fingerprint(tool_call)
            count = fingerprints.get(fingerprint, 0) + 1
            fingerprints[fingerprint] = count
            if count > limits.max_same_call:
                blocked_results.append(
                    ToolResult(
                        call_id=tool_call.id,
                        name=tool_call.name,
                        content={"error": "repeated_tool_call", "limit": limits.max_same_call},
                        is_error=True,
                    )
                )
            else:
                executable.append(tool_call)
        executed_results = await _execute_selected_calls(executable, tools, callbacks)
        ordered = {
            result.call_id: result for result in (*executed_results, *blocked_results)
        }
        messages.append(
            ModelMessage(
                role=MessageRole.TOOL,
                tool_results=tuple(ordered[call.id] for call in selected),
            )
        )
        tool_call_count += len(selected)

    final_prompt = ModelMessage.text(
        MessageRole.USER,
        "已达到工具调用轮数上限。禁止继续调用工具，请仅根据已有观察给出最终结果，并明确未完成项。",
    )
    final_response = await call(
        replace(
            request,
            messages=tuple([*messages, final_prompt]),
            tools=(),
            stream=False,
        )
    )
    usage = _sum_usage(usage, final_response.usage)
    await _notify(callbacks.on_usage, usage)
    if usage.total_tokens > limits.max_total_tokens:
        raise AgentLimitError("Agent 会话 token 总量超过限制")
    if final_response.tool_calls:
        raise AgentLimitError("Agent 最终总结轮仍尝试调用工具")
    if not final_response.text:
        raise RuntimeError("Agent 最终总结轮未返回文本")
    return AgentResult(
        text=final_response.text,
        model=final_response.model,
        messages=tuple([*messages, final_prompt]),
        usage=usage,
        steps=limits.max_steps,
        tool_calls=tool_call_count,
        stop_reason=final_response.stop_reason,
    )


__all__ = [
    "AgentCallbacks",
    "AgentLimitError",
    "AgentLimits",
    "AgentResult",
    "AgentTool",
    "run_agent",
    "tools_from_manifest",
]
