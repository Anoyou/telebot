"""有界工具调用循环与 NDJSON 事件发射。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.system_agent import (
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_TOOL,
    MESSAGE_ROLE_USER,
    SystemAgentMessage,
    SystemAgentSession,
)
from ..llm_agent import AgentCallbacks, AgentLimits, AgentTool, run_agent
from ..llm_dto import LLMProviderDTO
from ..llm_invoke import invoke_structured
from ..llm_protocol import MessageRole, ModelMessage, ModelRequest, ModelUsage, ToolCall, ToolResult
from ..llm_protocol import ToolSpec as LlmToolSpec
from ..llm_runtime import ProviderSwitchRequired
from .config import load_system_context_flags, resolve_agent_providers
from .context import ToolContext
from .events import make_event
from .memory import memory_context
from .model_capability import verify_resolved_agent_providers
from .prompts import build_system_prompt, provider_setup_hint
from .redactor import summarize_tool_result
from .registry import ToolRegistry, get_registry
from .skills import SkillRegistry, get_skill_registry
from .tool_routing import (
    ToolRoute,
    available_domains,
    parse_model_route,
    route_locally,
    router_system_prompt,
    select_tool_specs,
)

log = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


class ToolApprovalRequired(RuntimeError):
    """模型已选择具体工具，但 Web 用户尚未批准执行。"""

    def __init__(self, tool_names: tuple[str, ...]) -> None:
        self.tool_names = tool_names
        super().__init__("已理解你的需求，准备调用系统能力，请批准后继续。")


class SystemAgentRuntime:
    """封装 Provider 解析、工具过滤、run_agent 与事件发射。"""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.registry = registry or get_registry()
        self.skill_registry = skill_registry or get_skill_registry()

    async def stream_turn(
        self,
        db: AsyncSession,
        *,
        session: SystemAgentSession,
        user_text: str,
        role: str,
        channel: str,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        history_messages: list[SystemAgentMessage] | None = None,
        chat_secrets: list[str] | None = None,
        fallback_provider_id: int | None = None,
        approved_tools: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """执行一轮对话，逐条 yield NDJSON 事件。"""

        run_id = str(uuid.uuid4())
        seq = 0

        def next_event(event_type: str, **payload: Any) -> dict[str, Any]:
            nonlocal seq
            seq += 1
            return make_event(
                event_type,
                run_id=run_id,
                session_id=session.id,
                seq=seq,
                **payload,
            )

        yield next_event("run_started", channel=channel, account_id=session.account_id)

        from ..llm_usage_service import ensure_llm_usage_callback_registered

        ensure_llm_usage_callback_registered()

        flags = await load_system_context_flags(db)
        cfg = flags["agent_config"]
        resolved = await resolve_agent_providers(db, cfg)
        if isinstance(resolved, str):
            err = resolved
            hint = provider_setup_hint()
            yield next_event(
                "error",
                code="PROVIDER_UNAVAILABLE",
                message=err,
                hint=hint,
            )
            yield next_event("done", ok=False)
            return

        yield next_event(
            "model_capability_check",
            provider_id=resolved.primary.id,
            provider_name=resolved.primary.name,
            model=resolved.model,
        )
        verify_task = asyncio.create_task(verify_resolved_agent_providers(db, resolved))
        try:
            while not verify_task.done():
                done, _pending = await asyncio.wait({verify_task}, timeout=10.0)
                if verify_task not in done:
                    yield next_event(
                        "heartbeat",
                        stage="model_capability_check",
                        provider_id=resolved.primary.id,
                        provider_name=resolved.primary.name,
                        model=resolved.model,
                    )
            verified = await verify_task
        finally:
            if not verify_task.done():
                verify_task.cancel()
                with suppress(asyncio.CancelledError):
                    await verify_task
        if isinstance(verified, str):
            yield next_event(
                "error",
                code="MODEL_TOOLS_UNAVAILABLE",
                message=verified,
                hint="请在 AI 中心选择真正支持结构化工具调用的模型。",
            )
            yield next_event("done", ok=False)
            return
        resolved = verified

        provider_dto = resolved.primary
        model = resolved.model
        providers = resolved.providers
        yield next_event(
            "provider_selected",
            provider_id=provider_dto.id,
            provider_name=provider_dto.name,
            model=model,
            reason="configured",
        )

        progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def emit_model_progress(progress: dict[str, Any]) -> None:
            payload = dict(progress)
            event_type = str(payload.pop("type", "model_progress"))
            await progress_queue.put(next_event(event_type, **payload))

        async def wait_for_progress(task: asyncio.Task[Any]) -> tuple[str, Any]:
            progress_task = asyncio.create_task(progress_queue.get())
            try:
                done, _pending = await asyncio.wait(
                    {task, progress_task},
                    timeout=10.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
                raise
            if progress_task in done:
                return "event", progress_task.result()
            progress_task.cancel()
            with suppress(asyncio.CancelledError):
                await progress_task
            if task in done:
                return "done", task.result()
            return "heartbeat", None

        from ... import __version__

        system_prompt = build_system_prompt(
            timezone_name=str(flags["timezone"]),
            channel=channel,
            role=role,
            account_id=session.account_id,
            bot_tg_user_id=bot_tg_user_id,
            version=__version__,
            agent_enabled=bool(cfg.get("enabled")),
            ai_enabled=bool(flags["ai_enabled"]),
            command_prefix=str(flags["command_prefix"]),
        )

        memory_block = memory_context(session)
        if memory_block:
            system_prompt = f"{system_prompt}\n\n{memory_block}"

        all_tool_specs = self.registry.list_for(
            channel=channel,
            role=role,
            read_only_only=False,  # 阶段 2：只读 + 写（写工具只产生待确认 Action）
        )
        route_task = asyncio.create_task(
            self._resolve_tool_route(
                provider_dto=provider_dto,
                providers=providers,
                model=model,
                user_text=user_text,
                memory_state=session.memory_state,
                all_tool_specs=all_tool_specs,
                account_id=session.account_id,
                fallback_provider_id=fallback_provider_id,
                progress_callback=emit_model_progress,
            )
        )
        try:
            while True:
                state, value = await wait_for_progress(route_task)
                if state == "event":
                    yield value
                    continue
                if state == "heartbeat":
                    yield next_event(
                        "heartbeat",
                        provider_id=provider_dto.id,
                        provider_name=provider_dto.name,
                        model=model,
                    )
                    continue
                route = value
                break
        finally:
            if not route_task.done():
                route_task.cancel()
                with suppress(asyncio.CancelledError):
                    await route_task
        while not progress_queue.empty():
            yield progress_queue.get_nowait()
        routed_tool_specs = select_tool_specs(all_tool_specs, route)
        selected_skills = self.skill_registry.select(route)
        tool_specs = self.skill_registry.narrow_tools(routed_tool_specs, selected_skills)
        yield next_event(
            "route_selected",
            domains=list(route.domains),
            route_source=route.source,
            route_reason=route.reason,
            tool_count=len(tool_specs),
            available_tool_count=len(all_tool_specs),
        )
        if selected_skills:
            skill_names = [skill.name for skill in selected_skills]
            yield next_event(
                "skill_selected",
                skills=skill_names,
                skill_names=skill_names,
                understanding_summary=self.skill_registry.understanding_summary(
                    selected_skills
                ),
                tool_count=len(tool_specs),
            )
            skill_prompt = self.skill_registry.render_prompt(selected_skills)
            if skill_prompt:
                system_prompt = f"{system_prompt}\n\n{skill_prompt}"
        approved_tool_names = {str(name) for name in (approved_tools or []) if str(name)}
        tool_specs_by_name = {spec.name: spec for spec in tool_specs}

        def tool_approval_payload(tool_names: tuple[str, ...] | set[str]) -> dict[str, Any] | None:
            selected_specs = [
                tool_specs_by_name[name]
                for name in tool_names
                if name in tool_specs_by_name
            ]
            if not selected_specs:
                return None
            return {
                "domains": list(route.domains),
                "tools": [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "read_only": bool(spec.read_only),
                        "risk": str(spec.risk),
                    }
                    for spec in selected_specs
                ],
            }

        tool_ctx = ToolContext(
            db=db,
            channel=channel,
            role=role,
            session=session,
            account_id=session.account_id,
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            chat_secrets=list(chat_secrets or []),
        )

        deferred_events: list[dict[str, Any]] = []

        agent_tools: dict[str, AgentTool] = {}
        for spec in tool_specs:
            if spec.read_only:
                handler = self._bind_read_handler(spec, tool_ctx)
                read_only = True
            else:
                handler = self._bind_write_handler(spec, tool_ctx, deferred_events, next_event)
                read_only = False
            agent_tools[spec.name] = AgentTool(
                spec=LlmToolSpec(
                    name=spec.name,
                    description=spec.description,
                    parameters=spec.input_schema,
                    strict=False,
                ),
                handler=handler,
                read_only=read_only,
            )

        messages = self._build_messages(
            system_prompt=system_prompt,
            history=history_messages or [],
            user_text=user_text,
            token_budget=int(cfg.get("session_token_limit") or 16_384),
        )
        request = ModelRequest(
            model=model,
            messages=tuple(messages),
            tools=tuple(t.spec for t in agent_tools.values()),
            max_output_tokens=2048,
            metadata={
                "model_pinned": False,
                "confirm_provider_switch": True,
                "allowed_cross_provider_ids": (
                    [fallback_provider_id] if fallback_provider_id is not None else []
                ),
                "max_retries_per_model": 5,
                "retry_delay_seconds": 3.0,
            },
        )
        limits = AgentLimits(
            max_steps=int(cfg.get("max_steps") or 8),
            max_tool_calls=int(cfg.get("max_tool_calls") or 24),
            max_total_tokens=int(cfg.get("session_token_limit") or 16_384),
            # 单模型最多 6 次请求，且同 Provider 还会继续尝试其它模型；
            # 总时限需覆盖完整 fallback 链，用户可随时从 Web 手动停止。
            timeout_seconds=600.0,
        )

        async def on_tool_batch(calls: tuple[ToolCall, ...]) -> None:
            if channel != "web" or not bool(cfg.get("require_tool_approval")):
                return
            requested_names = tuple(
                dict.fromkeys(call.name for call in calls if call.name in tool_specs_by_name)
            )
            if set(requested_names).issubset(approved_tool_names):
                return
            combined_names = tuple(
                dict.fromkeys(
                    [
                        *(name for name in approved_tools or [] if name in tool_specs_by_name),
                        *requested_names,
                    ]
                )
            )
            raise ToolApprovalRequired(combined_names)

        async def on_tool_start(call: ToolCall) -> None:
            spec = tool_specs_by_name.get(call.name)
            await progress_queue.put(
                next_event(
                    "tool_started",
                    tool_name=call.name,
                    tool_description=spec.description if spec else call.name,
                    call_id=call.id,
                    arguments_summary=summarize_tool_result(call.arguments, max_chars=800),
                )
            )

        async def on_tool_finish(call: ToolCall, result: ToolResult) -> None:
            spec = tool_specs_by_name.get(call.name)
            await progress_queue.put(
                next_event(
                    "tool_finished",
                    tool_name=call.name,
                    tool_description=spec.description if spec else call.name,
                    call_id=call.id,
                    is_error=bool(result.is_error),
                    result_summary=summarize_tool_result(result.content, max_chars=1200),
                )
            )

        callbacks = AgentCallbacks(
            on_tool_batch=on_tool_batch,
            on_tool_start=on_tool_start,
            on_tool_finish=on_tool_finish,
        )

        active_provider = provider_dto
        active_model = model
        last_used_provider = provider_dto
        used_fallback = False

        async def model_call(current: ModelRequest):
            nonlocal active_model, active_provider, last_used_provider, used_fallback
            provider_request = replace(current, model=active_model)
            response, used, fallback = await invoke_structured(
                active_provider,
                providers,
                provider_request,
                account_id=session.account_id,
                source="system_agent",
                fallback_provider_id=fallback_provider_id,
                progress_callback=emit_model_progress,
            )
            previous_provider = active_provider
            previous_model = active_model
            last_used_provider = used
            used_fallback = used_fallback or fallback or used.id != provider_dto.id
            # 本轮内粘住最近一次成功的 Provider/模型，避免每个工具步骤都重新
            # 从已知不稳定的主 Provider 开始重试。
            active_provider = used
            active_model = response.model or provider_request.model
            if used.id != previous_provider.id or active_model != previous_model:
                await progress_queue.put(
                    next_event(
                        "provider_selected",
                        provider_id=used.id,
                        provider_name=used.name,
                        model=active_model,
                        reason=(
                            "provider_fallback"
                            if used.id != previous_provider.id
                            else "model_fallback"
                        ),
                    )
                )
            return response

        agent_task = asyncio.create_task(
            run_agent(
                model_call,
                request,
                agent_tools,
                limits=limits,
                callbacks=callbacks,
            )
        )
        try:
            while True:
                state, value = await wait_for_progress(agent_task)
                if state == "event":
                    yield value
                    continue
                if state == "done":
                    result = value
                    break
                yield next_event(
                    "heartbeat",
                    provider_id=active_provider.id,
                    provider_name=active_provider.name,
                    model=active_model,
                )
        except ToolApprovalRequired as exc:
            log.info(
                "system agent tool approval required session=%s tools=%s",
                session.id,
                ",".join(exc.tool_names),
            )
            yield next_event(
                "error",
                code="AGENT_TOOL_APPROVAL_REQUIRED",
                message=str(exc),
                tool_approval=tool_approval_payload(exc.tool_names),
            )
            yield next_event("done", ok=False)
            return
        except ProviderSwitchRequired as exc:
            log.warning(
                "system agent provider switch confirmation required session=%s provider=%s",
                session.id,
                exc.provider_name,
            )
            yield next_event(
                "error",
                code="AGENT_PROVIDER_SWITCH_REQUIRED",
                message=str(exc)[:500],
                provider_switch={
                    "from_provider_name": exc.provider_name,
                    "candidates": exc.candidates,
                },
                tool_approval=(
                    tool_approval_payload(approved_tool_names)
                    if channel == "web"
                    and bool(cfg.get("require_tool_approval"))
                    and approved_tool_names
                    else None
                ),
            )
            yield next_event("done", ok=False)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("system agent run failed session=%s", session.id)
            yield next_event(
                "error",
                code="AGENT_RUN_FAILED",
                message=str(exc)[:500],
            )
            yield next_event("done", ok=False)
            return
        finally:
            if not agent_task.done():
                agent_task.cancel()
                with suppress(asyncio.CancelledError):
                    await agent_task

        while not progress_queue.empty():
            yield progress_queue.get_nowait()
        for ev in deferred_events:
            yield ev

        assistant_text = result.text or ""
        usage_payload = _usage_payload(result.usage, last_used_provider, result.model)
        usage_payload["used_fallback"] = used_fallback
        usage_payload["route_domains"] = list(route.domains)
        usage_payload["tool_count"] = len(tool_specs)

        yield next_event(
            "assistant_message",
            content=assistant_text,
            usage=usage_payload,
        )
        yield next_event(
            "done",
            ok=True,
            steps=result.steps,
            tool_calls=result.tool_calls,
            route_domains=list(route.domains),
            tool_count=len(tool_specs),
            used_fallback=used_fallback,
        )

    def _bind_read_handler(self, spec: Any, tool_ctx: ToolContext):
        async def _handler(arguments: dict[str, Any]) -> Any:
            if spec.read_handler is None:
                return {"error": "handler_missing", "message": f"工具 {spec.name} 未实现"}
            try:
                return await spec.read_handler(tool_ctx, arguments or {})
            except PermissionError as exc:
                return {"error": "permission_denied", "message": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("tool %s failed", spec.name)
                return {
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                    "business_changed": False,
                }

        return _handler

    async def _resolve_tool_route(
        self,
        *,
        provider_dto: LLMProviderDTO,
        providers: dict[int, LLMProviderDTO],
        model: str,
        user_text: str,
        memory_state: dict[str, Any] | None,
        all_tool_specs: list[Any],
        account_id: int | None,
        fallback_provider_id: int | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> ToolRoute:
        domains = available_domains(all_tool_specs)
        local = route_locally(user_text, available=domains, memory_state=memory_state)
        if local is not None:
            return local
        try:
            state_hint = ""
            if isinstance(memory_state, dict) and memory_state:
                state_hint = json.dumps(
                    {
                        "last_domains": memory_state.get("last_domains"),
                        "last_user_goal": memory_state.get("last_user_goal"),
                    },
                    ensure_ascii=False,
                    default=str,
                )
            response, _used, _fallback = await invoke_structured(
                provider_dto,
                providers,
                ModelRequest(
                    model=model,
                    messages=(
                        ModelMessage.text(MessageRole.SYSTEM, router_system_prompt(domains)),
                        ModelMessage.text(
                            MessageRole.USER,
                            f"当前请求：{user_text}\n最近状态：{state_hint or '无'}",
                        ),
                    ),
                    max_output_tokens=160,
                    metadata={
                        "model_pinned": False,
                        "confirm_provider_switch": True,
                        "allowed_cross_provider_ids": (
                            [fallback_provider_id] if fallback_provider_id is not None else []
                        ),
                        "max_retries_per_model": 5,
                        "retry_delay_seconds": 3.0,
                    },
                ),
                account_id=account_id,
                source="system_agent_router",
                progress_callback=progress_callback,
            )
            parsed = parse_model_route(response.text, available=domains)
            if parsed is not None:
                return parsed
        except Exception:  # noqa: BLE001
            log.warning("system agent tool router failed; using safe fallback", exc_info=True)
        previous = []
        if isinstance(memory_state, dict):
            previous = [
                str(item)
                for item in (memory_state.get("last_domains") or [])
                if str(item) in domains
            ]
        if previous:
            return ToolRoute(tuple(previous[:3]), "fallback", "router_failed_use_memory")
        return ToolRoute((), "fallback", "router_failed_direct_answer")

    def _bind_write_handler(
        self,
        spec: Any,
        tool_ctx: ToolContext,
        event_queue: list[dict[str, Any]],
        next_event: Callable[..., dict[str, Any]],
    ):
        from .actions import action_to_dict, create_pending_action
        from .secrets import merge_secret_into_arguments

        async def _handler(arguments: dict[str, Any]) -> Any:
            if spec.preview_handler is None:
                return {
                    "error": "handler_missing",
                    "message": f"写工具 {spec.name} 未实现 preview",
                    "business_changed": False,
                }
            try:
                args = dict(arguments or {})
                # 阶段 3：把聊天中提取的密钥注入工具参数（仅内存）
                chat_secrets = getattr(tool_ctx, "chat_secrets", None) or []
                if spec.secret_argument_names and chat_secrets:
                    public, secrets, _fields = merge_secret_into_arguments(
                        args,
                        secret_names=spec.secret_argument_names,
                        chat_secrets=list(chat_secrets),
                    )
                    args = {**public, **secrets}
                preview = await spec.preview_handler(tool_ctx, args)
                if not isinstance(preview, dict):
                    preview = {"value": preview}
                # 把 preview 中的 account_id 回填，便于运行时同步
                if preview.get("account_id") is not None and args.get("account_id") is None:
                    args["account_id"] = preview["account_id"]
                summary = str(preview.get("summary") or spec.description)
                action = await create_pending_action(
                    tool_ctx.db,
                    ctx=tool_ctx,
                    spec=spec,
                    arguments=args,
                    preview=preview,
                    summary=summary,
                )
                # 立即提交，使其他会话（Web 确认 / Bot 回调）能立刻读到 pending Action
                try:
                    await tool_ctx.db.commit()
                except Exception:  # noqa: BLE001
                    log.exception("commit pending action failed tool=%s", spec.name)
                    raise
                payload = {
                    "status": "pending_confirmation",
                    "action_id": action.id,
                    "summary": action.summary,
                    "risk": action.risk,
                    "preview": action.preview,
                    "expires_at": action.expires_at.isoformat() if action.expires_at else None,
                    "business_changed": False,
                    "message": "已生成待确认操作，需用户确认后才会执行；在确认前业务数据未变化。",
                }
                event_queue.append(
                    next_event(
                        "action_proposed",
                        action=action_to_dict(action),
                    )
                )
                return payload
            except PermissionError as exc:
                return {
                    "error": "permission_denied",
                    "message": str(exc),
                    "business_changed": False,
                }
            except Exception as exc:  # noqa: BLE001
                log.exception("write tool preview %s failed", spec.name)
                return {
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                    "business_changed": False,
                }

        return _handler

    def _build_messages(
        self,
        *,
        system_prompt: str,
        history: list[SystemAgentMessage],
        user_text: str,
        token_budget: int,
    ) -> list[ModelMessage]:
        messages: list[ModelMessage] = [ModelMessage.text(MessageRole.SYSTEM, system_prompt)]
        # 粗略按字符预算滑窗：约 4 字符 ~ 1 token
        budget_chars = max(1000, token_budget * 3)
        selected: list[ModelMessage] = []
        used = 0
        for msg in reversed(history):
            text = _message_text(msg)
            if not text:
                continue
            role = _map_role(msg.role)
            if role is None:
                continue
            cost = len(text)
            if used + cost > budget_chars and selected:
                break
            selected.append(ModelMessage.text(role, text))
            used += cost
        selected.reverse()
        messages.extend(selected)
        messages.append(ModelMessage.text(MessageRole.USER, user_text))
        return messages


def _map_role(role: str) -> MessageRole | None:
    if role == MESSAGE_ROLE_USER:
        return MessageRole.USER
    if role == MESSAGE_ROLE_ASSISTANT:
        return MessageRole.ASSISTANT
    if role == MESSAGE_ROLE_TOOL:
        # 工具结果摘要并入 assistant 侧文本上下文，避免协议复杂度
        return MessageRole.ASSISTANT
    return None


def _message_text(msg: SystemAgentMessage) -> str:
    content = msg.content if isinstance(msg.content, dict) else {}
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        return text
    if msg.role == MESSAGE_ROLE_TOOL:
        summary = content.get("result_summary") or content.get("summary")
        name = content.get("tool_name") or "tool"
        try:
            return f"[tool:{name}] {json.dumps(summary, ensure_ascii=False, default=str)[:1500]}"
        except (TypeError, ValueError):
            return f"[tool:{name}] {str(summary)[:1500]}"
    return ""


def _usage_payload(usage: ModelUsage, provider: LLMProviderDTO, model: str) -> dict[str, Any]:
    return {
        "provider_id": provider.id,
        "provider_name": provider.name,
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


__all__ = ["SystemAgentRuntime"]
