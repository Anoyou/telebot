"""Bounded, provider-neutral agent loop built on structured LLM requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .llm_protocol import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
)

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
ModelCall = Callable[[ModelRequest], Awaitable[ModelResponse]]
StreamModelCall = Callable[[ModelRequest], AsyncIterator[ModelStreamEvent]]


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
    # 0 表示不限制本轮 token 预算
    max_total_tokens: int = 50_000
    timeout_seconds: float = 180.0


@dataclass
class AgentCallbacks:
    on_step: Callable[[int], Awaitable[None]] | None = None
    on_usage: Callable[[ModelUsage], Awaitable[None]] | None = None
    on_safe_boundary: Callable[[], Awaitable[tuple[ModelMessage, ...]]] | None = None
    on_tool_batch: Callable[[tuple[ToolCall, ...]], Awaitable[None]] | None = None
    on_tool_start: Callable[[ToolCall], Awaitable[None]] | None = None
    on_tool_finish: Callable[[ToolCall, ToolResult], Awaitable[None]] | None = None
    on_text_delta: Callable[[str], Awaitable[None]] | None = None
    on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None
    on_text_reset: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True)
class AgentResult:
    text: str
    model: str
    messages: tuple[ModelMessage, ...]
    usage: ModelUsage
    steps: int
    tool_calls: int
    stop_reason: StopReason
    reasoning_content: str | None = None


class AgentLimitError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        used_tokens: int | None = None,
        limit_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.used_tokens = used_tokens
        self.limit_tokens = limit_tokens


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEXT_TOOL_PROTOCOL_RE = re.compile(
    r"^\s*<(?:search_tool|tool_calls?|tool_call)\b[\s\S]*"
    r"<tool_call\b[\s\S]*</(?:search_tool|tool_calls?|tool_call)>\s*$",
    re.IGNORECASE,
)


def _looks_like_text_tool_protocol(text: str) -> bool:
    """识别模型把工具调用协议作为整段普通文本输出的情况。"""

    return bool(_TEXT_TOOL_PROTOCOL_RE.fullmatch(str(text or "").strip()))


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
    stream_model_call: StreamModelCall | None = None,
    resume_tool_calls: tuple[ToolCall, ...] | None = None,
) -> AgentResult:
    """Run a bounded tool loop; only explicitly supplied tools are executable.

    ``resume_tool_calls`` 用于工具批准后从待执行工具继续，而不是重跑首轮模型决策。
    """

    limits = limits or AgentLimits()
    callbacks = callbacks or AgentCallbacks()
    if (
        min(
            limits.max_steps,
            limits.max_tool_calls,
            limits.max_calls_per_turn,
            limits.max_same_call,
        )
        <= 0
    ):
        raise ValueError("agent limits must be positive")
    if limits.max_total_tokens < 0:
        raise ValueError("agent token budget must be >= 0 (0 means unlimited)")
    declared = {tool.name for tool in request.tools}
    if declared != set(tools):
        raise ValueError("ModelRequest.tools 必须与可执行工具白名单完全一致")

    messages = list(request.messages)
    usage = ModelUsage()
    # 限额用「增量口径」：每步只计 output + 相对上一步 input 的增长，
    # 不重复计 system/记忆/工具 Schema 每步重发的前缀。usage/_sum_usage 仍全量累计（AI 页面口径不变）。
    limit_budget_used = 0
    # 首次请求的 input 是进入本轮前已经存在的 system prompt、工具定义与历史上下文，
    # 它决定模型上下文窗口，但不应重复消耗“本轮新增工作预算”。后续步骤只计 input
    # 相对这个基线的增长（工具结果等）以及每一步真实 output。
    previous_step_input: int | None = None
    fingerprints: dict[str, int] = {}
    tool_call_count = 0
    text_protocol_repairs = 0
    reasoning_parts: list[str] = []
    started = time.monotonic()

    def _apply_limit_budget(step_usage: ModelUsage) -> None:
        nonlocal limit_budget_used, previous_step_input
        # 上游无 usage 的步骤跳过累计（与「无 usage 不估 token」现状一致）
        if step_usage.total_tokens == 0:
            return
        step_input = int(step_usage.input_tokens or 0)
        step_output = int(step_usage.output_tokens or 0)
        incremental_input = 0 if previous_step_input is None else max(0, step_input - previous_step_input)
        incremental = step_output + incremental_input
        limit_budget_used += incremental
        previous_step_input = step_input
        if limits.max_total_tokens > 0 and limit_budget_used > limits.max_total_tokens:
            raise AgentLimitError(
                f"Agent 本轮 token 预算超过限制（已用 {limit_budget_used:,} / 上限 {limits.max_total_tokens:,}）",
                used_tokens=limit_budget_used,
                limit_tokens=limits.max_total_tokens,
            )

    async def call(current: ModelRequest) -> ModelResponse:
        remaining = limits.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Agent 会话已超时")
        async with asyncio.timeout(remaining):
            if stream_model_call is None:
                return await model_call(current)
            terminal: ModelResponse | None = None
            async for event in stream_model_call(current):
                if event.delta:
                    await _notify(callbacks.on_text_delta, event.delta)
                if event.reasoning_delta:
                    await _notify(callbacks.on_reasoning_delta, event.reasoning_delta)
                if event.response is not None:
                    terminal = event.response
                    break
            if terminal is None:
                raise RuntimeError("模型流式调用没有返回最终响应")
            return terminal

    resumed_once = False
    for step in range(1, limits.max_steps + 1):
        await _notify(callbacks.on_step, step)
        if callbacks.on_safe_boundary is not None:
            messages.extend(await callbacks.on_safe_boundary())
        if resume_tool_calls and not resumed_once and step == 1:
            resumed_once = True
            response = ModelResponse(
                model=request.model,
                content=(),
                tool_calls=tuple(resume_tool_calls),
                usage=ModelUsage(),
                stop_reason=StopReason.TOOL_CALLS,
            )
        else:
            response = await call(replace(request, messages=tuple(messages), stream=False))
            if response.reasoning_content:
                reasoning_parts.append(response.reasoning_content)
            usage = _sum_usage(usage, response.usage)
            await _notify(callbacks.on_usage, usage)
            _apply_limit_budget(response.usage)
            if callbacks.on_safe_boundary is not None:
                steering_messages = await callbacks.on_safe_boundary()
                if steering_messages:
                    # 模型响应已经完成、工具尚未执行，是可安全转向的边界。
                    # 丢弃尚未生效的工具调用，把已生成文本仅作为上下文，再按
                    # 用户的新指令重新决策；已经完成的上一轮工具副作用不会回滚。
                    if response.text:
                        messages.append(
                            ModelMessage(
                                role=MessageRole.ASSISTANT,
                                content=response.content,
                                reasoning_content=response.reasoning_content,
                            )
                        )
                    messages.extend(steering_messages)
                    await _notify(callbacks.on_text_reset)
                    continue
        if not response.tool_calls:
            if not response.text:
                raise RuntimeError("模型既未返回文本，也未调用工具")
            if (
                request.metadata.get("repair_text_tool_protocol") is True
                and _looks_like_text_tool_protocol(response.text)
            ):
                if text_protocol_repairs >= 1 or step >= limits.max_steps:
                    await _notify(callbacks.on_text_reset)
                    raise RuntimeError("模型连续返回文本伪工具调用，已拒绝作为最终答案")
                text_protocol_repairs += 1
                await _notify(callbacks.on_text_reset)
                messages.append(
                    ModelMessage(
                        role=MessageRole.ASSISTANT,
                        content=response.content,
                        reasoning_content=response.reasoning_content,
                    )
                )
                correction = (
                    "你刚才把工具调用协议作为普通文本输出了。"
                    + (
                        "本轮没有提供任何工具；不要输出 XML、tool_call、search_tool "
                        "或其它伪工具标签，请直接回答最初的用户问题。"
                        if not request.tools
                        else "需要调用工具时只能使用 API 提供的结构化工具调用；"
                        "不要输出 XML、tool_call 或 search_tool 标签。请重新完成最初请求。"
                    )
                )
                messages.append(ModelMessage.text(MessageRole.USER, correction))
                continue
            messages.append(
                ModelMessage(
                    role=MessageRole.ASSISTANT,
                    content=response.content,
                    reasoning_content=response.reasoning_content,
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
                reasoning_content="\n\n".join(reasoning_parts) or None,
            )

        # 对工具调用前可能已抵达的自然语言草稿只作临时预览。确认本轮要
        await _notify(callbacks.on_text_reset)

        remaining_calls = limits.max_tool_calls - tool_call_count
        selected = list(response.tool_calls[: min(limits.max_calls_per_turn, remaining_calls)])
        if not selected:
            raise AgentLimitError("Agent 工具调用总量超过限制")
        messages.append(
            ModelMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=tuple(selected),
                # DeepSeek 思考+工具：后续轮次必须回传本轮 reasoning_content
                reasoning_content=response.reasoning_content,
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
        allowed_batch = tuple(call for call in executable if call.name in tools)
        if allowed_batch:
            # 批准、审计等前置门禁必须在整批工具开始执行前完成，
            # 避免并行只读工具或混合工具产生部分执行。
            await _notify(callbacks.on_tool_batch, allowed_batch)
        executed_results = await _execute_selected_calls(executable, tools, callbacks)
        ordered = {result.call_id: result for result in (*executed_results, *blocked_results)}
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
    if callbacks.on_safe_boundary is not None:
        messages.extend(await callbacks.on_safe_boundary())
    final_response = await call(
        replace(
            request,
            messages=tuple([*messages, final_prompt]),
            tools=(),
            stream=False,
        )
    )
    if final_response.reasoning_content:
        reasoning_parts.append(final_response.reasoning_content)
    usage = _sum_usage(usage, final_response.usage)
    await _notify(callbacks.on_usage, usage)
    _apply_limit_budget(final_response.usage)
    if final_response.tool_calls:
        raise AgentLimitError("Agent 最终总结轮仍尝试调用工具")
    if not final_response.text:
        raise RuntimeError("Agent 最终总结轮未返回文本")
    if (
        request.metadata.get("repair_text_tool_protocol") is True
        and _looks_like_text_tool_protocol(final_response.text)
    ):
        await _notify(callbacks.on_text_reset)
        raise RuntimeError("Agent 最终总结轮返回文本伪工具调用")
    return AgentResult(
        text=final_response.text,
        model=final_response.model,
        messages=tuple([*messages, final_prompt]),
        usage=usage,
        steps=limits.max_steps,
        tool_calls=tool_call_count,
        stop_reason=final_response.stop_reason,
        reasoning_content="\n\n".join(reasoning_parts) or None,
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
