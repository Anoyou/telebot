"""System Agent 会话、消息与主流程编排。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.system_agent import (
    CHANNEL_BOT,
    CHANNEL_WEB,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_TOOL,
    MESSAGE_ROLE_USER,
    MESSAGE_RUN_COMPLETED,
    MESSAGE_RUN_FAILED,
    MESSAGE_RUN_PENDING,
    MESSAGE_RUN_SUCCEEDED,
    SESSION_ORIGIN_INTERACTIVE,
    SESSION_ORIGINS,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    SystemAgentMessage,
    SystemAgentSession,
)
from .actions import clear_expired_secrets
from .config import (
    load_config,
    load_system_context_flags,
    resolve_agent_providers,
    save_config,
)
from .memory import clear_session_memory, should_compress_summary, update_session_memory
from .memory_compress import schedule_summary_compression
from .prompts import session_title_from_message
from .registry import get_registry
from .runtime import SystemAgentRuntime
from .secrets import (
    contains_request_header_context,
    extract_plaintext_secrets,
    redact_known_secrets,
    redact_user_message,
)
from .turn_context import (
    clear_failed_turn,
    failed_turn_state,
    is_retry_reference,
    remember_failed_turn,
)

log = logging.getLogger(__name__)

# Runtime 单轮允许运行 10 分钟；失活回收必须留出路由与落库余量，
# 避免历史页把仍在执行的长请求提前暴露为可重试状态。
STALE_PENDING_AFTER = timedelta(minutes=15)


def _public_tool_approval(value: dict[str, Any]) -> dict[str, Any]:
    """审批记录只保留调用身份与工具元数据，绝不持久化参数。"""

    out: dict[str, Any] = {}
    domains = value.get("domains")
    if isinstance(domains, list):
        out["domains"] = [str(item) for item in domains[:3]]
    for field in ("calls", "tools"):
        raw_items = value.get(field)
        if not isinstance(raw_items, list):
            continue
        items: list[dict[str, Any]] = []
        for raw in raw_items[:64]:
            if not isinstance(raw, dict):
                continue
            item = {
                key: raw[key]
                for key in ("call_id", "name", "description", "read_only", "risk")
                if key in raw
            }
            if item:
                items.append(item)
        out[field] = items
    return out


def _approved_tool_calls_from_retry(
    retry_message: SystemAgentMessage | None,
    approved_tools: list[str] | None,
) -> list[dict[str, Any]]:
    # 审批记录只持久化工具名，不保存参数。批准后让模型根据原问题重新生成
    # 只读调用；写工具另有 Action 确认，不走这层审批。
    del retry_message, approved_tools
    return []


async def is_latest_completed_pair(
    db: AsyncSession,
    *,
    session_id: str,
    user_message_id: int,
    assistant_message_id: int,
) -> bool:
    latest_user_result = await db.execute(
        select(SystemAgentMessage)
        .where(
            SystemAgentMessage.session_id == session_id,
            SystemAgentMessage.role == MESSAGE_ROLE_USER,
        )
        .order_by(desc(SystemAgentMessage.id))
        .limit(1)
    )
    user_message = latest_user_result.scalar_one_or_none()
    assistant_message = await db.get(SystemAgentMessage, assistant_message_id)
    if (
        user_message is None
        or user_message.id != user_message_id
        or user_message.run_status != MESSAGE_RUN_SUCCEEDED
        or assistant_message is None
        or assistant_message.session_id != session_id
        or assistant_message.role != MESSAGE_ROLE_ASSISTANT
        or assistant_message.run_status != MESSAGE_RUN_COMPLETED
        or assistant_message.id <= user_message.id
    ):
        return False
    paired_assistant_result = await db.execute(
        select(SystemAgentMessage.id)
        .where(
            SystemAgentMessage.session_id == session_id,
            SystemAgentMessage.role == MESSAGE_ROLE_ASSISTANT,
            SystemAgentMessage.id > user_message.id,
        )
        .order_by(SystemAgentMessage.id)
        .limit(1)
    )
    return paired_assistant_result.scalar_one_or_none() == assistant_message_id


class SystemAgentService:
    def __init__(self) -> None:
        self.runtime = SystemAgentRuntime()

    # ── 配置 ──────────────────────────────────────────────────────
    async def get_config(self, db: AsyncSession) -> dict[str, Any]:
        return await load_config(db)

    async def update_config(self, db: AsyncSession, patch: dict[str, Any]) -> dict[str, Any]:
        return await save_config(db, patch)

    async def get_capabilities(
        self,
        db: AsyncSession,
        *,
        channel: str = CHANNEL_WEB,
        role: str = "admin",
    ) -> dict[str, Any]:
        cfg = await load_config(db)
        flags = await load_system_context_flags(db)
        registry = get_registry()
        resolved = await resolve_agent_providers(db, cfg)
        provider_name = None
        resolved_model = None
        if not isinstance(resolved, str):
            provider_name = resolved.primary.name
            resolved_model = resolved.model
        # 模型能力矩阵：声明字段 + 实测缓存 + 运行时健康
        model_matrix: list[dict[str, Any]] = []
        try:
            from sqlalchemy import select as sa_select

            from ...db.models.command import LLMProvider
            from ...db.models.system import SystemSetting as SS
            from ..provider_health import get_health

            cache_row = await db.get(SS, "system_agent_model_capability_cache")
            cap_cache = cache_row.value if cache_row and isinstance(cache_row.value, dict) else {}
            result = await db.execute(sa_select(LLMProvider).order_by(LLMProvider.id.asc()))
            for prov in result.scalars().all():
                models = list(prov.models or []) if isinstance(prov.models, list) else []
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id") or "").strip()
                    if not mid:
                        continue
                    cache_key = f"{prov.id}:{mid}"
                    probe = cap_cache.get(cache_key) if isinstance(cap_cache, dict) else None
                    health = get_health(int(prov.id), mid)
                    model_matrix.append(
                        {
                            "provider_id": prov.id,
                            "provider_name": prov.name,
                            "model": mid,
                            "enabled": bool(item.get("enabled", True)),
                            "declared_supports_tools": item.get("supports_tools"),
                            "declared_supports_images": item.get("supports_images"),
                            "declared_reasoning_efforts": item.get("reasoning_efforts"),
                            "probed_supports_tools": (
                                probe.get("supports_tools") if isinstance(probe, dict) else None
                            ),
                            "probed_status": probe.get("status") if isinstance(probe, dict) else None,
                            "health": health,
                        }
                    )
        except Exception:  # noqa: BLE001
            log.debug("build model matrix failed", exc_info=True)
            model_matrix = []

        return {
            "enabled": bool(cfg.get("enabled")),
            "provider_id": cfg.get("provider_id"),
            "model": cfg.get("model"),
            "provider_name": provider_name,
            "resolved_model": resolved_model,
            "ai_enabled": bool(flags["ai_enabled"]),
            "timezone": flags["timezone"],
            "tools": registry.capabilities(channel=channel, role=role),
            "stage": 4,
            "write_tools_available": True,
            "secret_chat_input": True,
            "model_matrix": model_matrix,
        }

    # ── 会话 CRUD ─────────────────────────────────────────────────
    async def create_session(
        self,
        db: AsyncSession,
        *,
        channel: str,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        account_id: int | None = None,
        title: str | None = None,
        origin: str | None = None,
    ) -> SystemAgentSession:
        if channel not in {CHANNEL_WEB, CHANNEL_BOT}:
            raise ValueError(f"invalid channel: {channel}")
        if channel == CHANNEL_BOT and account_id is None:
            raise ValueError("Bot 会话必须绑定 account_id")
        origin_value = str(origin or SESSION_ORIGIN_INTERACTIVE)
        if origin_value not in SESSION_ORIGINS:
            raise ValueError(f"invalid origin: {origin_value}")
        session = SystemAgentSession(
            id=str(uuid.uuid4()),
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            account_id=account_id,
            channel=channel,
            title=title,
            origin=origin_value,
            status=SESSION_STATUS_ACTIVE,
        )
        db.add(session)
        await db.flush()
        return session

    async def list_sessions(
        self,
        db: AsyncSession,
        *,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        account_id: int | None = None,
        status: str | None = SESSION_STATUS_ACTIVE,
        origin: str | None = None,
        include_bot_sessions: bool = False,
        limit: int = 50,
    ) -> list[SystemAgentSession]:
        q = (
            select(SystemAgentSession)
            .order_by(desc(SystemAgentSession.updated_at))
            .limit(max(1, min(limit, 200)))
        )
        if web_user_id is not None:
            if include_bot_sessions:
                q = q.where(
                    or_(
                        SystemAgentSession.web_user_id == web_user_id,
                        SystemAgentSession.channel == CHANNEL_BOT,
                    )
                )
            else:
                q = q.where(SystemAgentSession.web_user_id == web_user_id)
        if bot_tg_user_id is not None:
            q = q.where(SystemAgentSession.bot_tg_user_id == bot_tg_user_id)
        if account_id is not None:
            q = q.where(SystemAgentSession.account_id == account_id)
        if status:
            q = q.where(SystemAgentSession.status == status)
        if origin:
            q = q.where(SystemAgentSession.origin == origin)
        result = await db.execute(q)
        return list(result.scalars().all())

    async def get_session(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        allow_bot_session: bool = False,
    ) -> SystemAgentSession | None:
        session = await db.get(SystemAgentSession, session_id)
        if session is None:
            return None
        if web_user_id is not None and session.web_user_id != web_user_id:
            if not (allow_bot_session and session.channel == CHANNEL_BOT):
                return None
        if bot_tg_user_id is not None and session.bot_tg_user_id != bot_tg_user_id:
            return None
        return session

    async def update_session(
        self,
        db: AsyncSession,
        session: SystemAgentSession,
        *,
        title: str | None = None,
        status: str | None = None,
        account_id: int | None = ...,  # type: ignore[assignment]
    ) -> SystemAgentSession:
        if title is not None:
            session.title = title[:64] if title else None
        if status is not None:
            if status not in {SESSION_STATUS_ACTIVE, SESSION_STATUS_ARCHIVED}:
                raise ValueError(f"invalid status: {status}")
            session.status = status
        if account_id is not ...:
            session.account_id = account_id
        session.updated_at = datetime.now(UTC)
        await db.flush()
        return session

    async def delete_session(self, db: AsyncSession, session: SystemAgentSession) -> None:
        await db.delete(session)
        await db.flush()

    async def delete_all_sessions(
        self,
        db: AsyncSession,
        *,
        web_user_id: int,
    ) -> int:
        result = await db.execute(
            delete(SystemAgentSession).where(SystemAgentSession.web_user_id == web_user_id)
        )
        await db.flush()
        return int(result.rowcount or 0)

    async def clear_messages(self, db: AsyncSession, session: SystemAgentSession) -> int:
        result = await db.execute(
            delete(SystemAgentMessage).where(SystemAgentMessage.session_id == session.id)
        )
        session.updated_at = datetime.now(UTC)
        clear_session_memory(session)
        await db.flush()
        return int(result.rowcount or 0)

    async def list_messages(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[SystemAgentMessage]:
        q = (
            select(SystemAgentMessage)
            .where(SystemAgentMessage.session_id == session_id)
            .order_by(desc(SystemAgentMessage.id))
            .limit(max(1, min(limit, 500)))
        )
        if before_id is not None:
            q = q.where(SystemAgentMessage.id < before_id)
        result = await db.execute(q)
        rows = list(result.scalars().all())
        rows.reverse()
        return rows

    async def reconcile_stale_messages(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        stale_after: timedelta = STALE_PENDING_AFTER,
    ) -> int:
        """把连接中断后遗留的 pending 消息恢复为可重试失败态。"""

        rows = list(
            (
                await db.execute(
                    select(SystemAgentMessage)
                    .where(
                        SystemAgentMessage.session_id == session_id,
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                        SystemAgentMessage.run_status == MESSAGE_RUN_PENDING,
                    )
                    .order_by(SystemAgentMessage.id)
                )
            )
            .scalars()
            .all()
        )
        session = await db.get(SystemAgentSession, session_id)
        now = datetime.now(UTC)
        changed = 0
        for row in rows:
            started_at = row.created_at
            if isinstance(row.usage, dict):
                raw_started = row.usage.get("run_started_at")
                if isinstance(raw_started, str):
                    try:
                        started_at = datetime.fromisoformat(raw_started)
                    except ValueError:
                        pass
            if started_at is None:
                continue
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            if now - started_at < stale_after:
                continue
            row.run_status = MESSAGE_RUN_FAILED
            row.error_code = "AGENT_STREAM_INTERRUPTED"
            row.error_message = "连接中断，上一轮未完成，请重试本轮。"
            row.usage = None
            if session is not None:
                content = row.content if isinstance(row.content, dict) else {}
                remember_failed_turn(
                    session,
                    message_id=row.id,
                    user_goal=str(content.get("text") or ""),
                    error_code=row.error_code,
                )
            changed += 1
        if changed:
            await db.flush()
        return changed

    async def get_message(
        self,
        db: AsyncSession,
        message_id: int,
        *,
        session_id: str,
    ) -> SystemAgentMessage | None:
        row = await db.get(SystemAgentMessage, int(message_id))
        if row is None or row.session_id != session_id:
            return None
        return row

    async def is_latest_completed_pair(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        user_message_id: int,
        assistant_message_id: int,
    ) -> bool:
        return await is_latest_completed_pair(
            db,
            session_id=session_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
        )

    async def get_or_create_active_session(
        self,
        db: AsyncSession,
        *,
        channel: str,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        account_id: int | None = None,
    ) -> SystemAgentSession:
        # 定时会话不进入复用池：只复用 interactive 活跃会话
        sessions = await self.list_sessions(
            db,
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            account_id=account_id if channel == CHANNEL_BOT else None,
            status=SESSION_STATUS_ACTIVE,
            origin=SESSION_ORIGIN_INTERACTIVE,
            limit=1,
        )
        if sessions:
            return sessions[0]
        return await self.create_session(
            db,
            channel=channel,
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            account_id=account_id,
            origin=SESSION_ORIGIN_INTERACTIVE,
        )

    # ── 对话 ──────────────────────────────────────────────────────
    async def stream_message(
        self,
        db: AsyncSession,
        *,
        session: SystemAgentSession,
        text: str,
        role: str,
        channel: str,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        retry_message: SystemAgentMessage | None = None,
        regenerate_message: SystemAgentMessage | None = None,
        regenerate_assistant_message: SystemAgentMessage | None = None,
        fallback_provider_id: int | None = None,
        approved_tools: list[str] | None = None,
        run_id: str | None = None,
        model_selection: dict[str, Any] | None = None,
        read_only_only: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        await self.reconcile_stale_messages(db, session.id)
        incoming_text = str(text or "").strip()
        regenerating = (
            regenerate_message is not None
            or regenerate_assistant_message is not None
        )
        if regenerating and (
            regenerate_message is None or regenerate_assistant_message is None
        ):
            raise ValueError("原位重新生成需要同时指定用户消息和助手消息")
        if regenerating and retry_message is not None:
            raise ValueError("失败重试与原位重新生成不能同时执行")
        if not regenerating and retry_message is None and is_retry_reference(incoming_text):
            retry_message = await self._resolve_failed_retry_message(db, session)
        run_started_at = datetime.now(UTC).isoformat()
        if regenerating:
            assert regenerate_message is not None
            assert regenerate_assistant_message is not None
            if (
                regenerate_message.session_id != session.id
                or regenerate_message.role != MESSAGE_ROLE_USER
                or regenerate_message.run_status != MESSAGE_RUN_SUCCEEDED
                or regenerate_assistant_message.session_id != session.id
                or regenerate_assistant_message.role != MESSAGE_ROLE_ASSISTANT
                or regenerate_assistant_message.run_status != MESSAGE_RUN_COMPLETED
                or regenerate_assistant_message.id <= regenerate_message.id
            ):
                raise ValueError("只能编辑或重新生成当前会话最新完成的一轮")
            content = (
                regenerate_message.content
                if isinstance(regenerate_message.content, dict)
                else {}
            )
            raw_text = incoming_text or str(content.get("text") or "").strip()
        elif retry_message is not None:
            if retry_message.role != MESSAGE_ROLE_USER:
                raise ValueError("只能重试用户消息")
            if retry_message.run_status != MESSAGE_RUN_FAILED:
                raise ValueError("只有失败消息可以重试")
            content = retry_message.content if isinstance(retry_message.content, dict) else {}
            raw_text = str(content.get("text") or "").strip()
        else:
            raw_text = incoming_text
        if not raw_text:
            yield {
                "type": "error",
                "code": "EMPTY_MESSAGE",
                "message": "消息不能为空",
                "session_id": session.id,
            }
            return

        try:
            await clear_expired_secrets(db)
        except Exception:  # noqa: BLE001
            log.debug("clear expired action secrets failed", exc_info=True)

        chat_secrets = (
            []
            if retry_message is not None and not regenerating
            else extract_plaintext_secrets(raw_text)
        )
        persisted_user_text = redact_user_message(raw_text, chat_secrets)
        model_user_text = (
            persisted_user_text
            if contains_request_header_context(raw_text)
            else raw_text
        )
        if not session.title:
            session.title = session_title_from_message(persisted_user_text)
        session.updated_at = datetime.now(UTC)

        approved_tool_calls: list[dict[str, Any]] = []
        requested_approved_tools = {
            str(name) for name in (approved_tools or []) if str(name)
        }
        effective_approved_tools: set[str] = set()
        # 新消息先落库；重试则复用原消息，避免重复污染历史。
        if regenerating:
            assert regenerate_message is not None
            user_msg = regenerate_message
            if incoming_text:
                user_msg.content = {
                    "text": persisted_user_text,
                }
        elif retry_message is None:
            user_msg = SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_USER,
                content={"text": persisted_user_text},
                usage={"run_started_at": run_started_at},
                run_status=MESSAGE_RUN_PENDING,
            )
            db.add(user_msg)
        else:
            user_msg = retry_message
            previous_usage = user_msg.usage if isinstance(user_msg.usage, dict) else {}
            previous_approved = previous_usage.get("approved_tools")
            if isinstance(previous_approved, list):
                effective_approved_tools.update(
                    str(name) for name in previous_approved if str(name)
                )
            pending_approval = previous_usage.get("tool_approval")
            pending_tools = pending_approval.get("tools") if isinstance(pending_approval, dict) else []
            pending_tool_names = {
                str(item.get("name"))
                for item in pending_tools
                if isinstance(item, dict) and str(item.get("name") or "")
            }
            unexpected_approvals = requested_approved_tools - pending_tool_names
            if unexpected_approvals:
                raise ValueError(
                    "批准工具不在当前待确认列表："
                    + "、".join(sorted(unexpected_approvals))
                )
            effective_approved_tools.update(requested_approved_tools)
            approved_tools = sorted(effective_approved_tools)
            approved_tool_calls = _approved_tool_calls_from_retry(retry_message, approved_tools)
            claim = await db.execute(
                update(SystemAgentMessage)
                .where(
                    SystemAgentMessage.id == user_msg.id,
                    SystemAgentMessage.session_id == session.id,
                    SystemAgentMessage.run_status == MESSAGE_RUN_FAILED,
                )
                .values(
                    run_status=MESSAGE_RUN_PENDING,
                    error_code=None,
                    error_message=None,
                    usage={"run_started_at": run_started_at},
                    retry_count=SystemAgentMessage.retry_count + 1,
                )
            )
            if int(claim.rowcount or 0) != 1:
                raise ValueError("本轮已被重试或状态已变化，请刷新会话")
            await db.refresh(user_msg)
        await db.flush()

        # 历史最后一条是刚写入的打码用户消息；模型当次请求通常使用原始文本，
        # 自定义请求头配置则只追加安全提示，强制密钥改走 Provider 设置页。
        if regenerating:
            history = await self.list_messages(
                db,
                session.id,
                limit=32,
                before_id=user_msg.id,
            )
            history_for_model = [
                message
                for message in history
                if _message_available_to_context(message)
            ][-8:]
        else:
            history = await self.list_messages(db, session.id, limit=32)
            history_for_model = [
                message
                for message in history
                if message.id != user_msg.id and _message_available_to_context(message)
            ][-8:]

        # 用户消息先独立落库。即使浏览器断线或上游模型超时，刷新后仍能看到本轮输入。
        await db.commit()

        assistant_text = ""
        assistant_reasoning = ""
        usage: dict[str, Any] | None = None
        tool_events: list[dict[str, Any]] = []
        memory_tool_events: list[dict[str, Any]] = []
        buffered_events: list[dict[str, Any]] = []
        done_ok = False
        error_code: str | None = None
        error_message: str | None = None
        route_domains: list[str] = []
        failure_context: dict[str, Any] | None = None
        cancelled = False

        done_received = False
        try:
            async for event in self.runtime.stream_turn(
                db,
                session=session,
                user_text=model_user_text,
                role=role,
                channel=channel,
                web_user_id=web_user_id,
                bot_tg_user_id=bot_tg_user_id,
                history_messages=history_for_model,
                chat_secrets=chat_secrets,
                fallback_provider_id=fallback_provider_id,
                approved_tools=approved_tools,
                approved_tool_calls=approved_tool_calls,
                model_selection=model_selection,
                read_only_only=read_only_only,
                exclude_latest_session_memory=regenerating,
            ):
                et = event.get("type")
                if et == "assistant_message":
                    assistant_text = redact_known_secrets(str(event.get("content") or ""), chat_secrets)
                    event["content"] = assistant_text
                    assistant_reasoning = redact_known_secrets(
                        str(event.get("reasoning") or ""),
                        chat_secrets,
                    )
                    event["reasoning"] = assistant_reasoning
                    usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
                    if usage is not None and usage.get("stream_fallback"):
                        event["stream_fallback"] = True
                elif et in {"tool_started", "tool_finished"}:
                    tool_events.append(event)
                elif et == "route_selected":
                    route_domains = [str(item) for item in (event.get("domains") or [])][:3]
                elif et == "error":
                    error_code = str(event.get("code") or "AGENT_RUN_FAILED")[:64]
                    error_message = str(event.get("message") or "助手运行失败")[:1024]
                    provider_switch = event.get("provider_switch")
                    if isinstance(provider_switch, dict):
                        failure_context = {
                            **(failure_context or {}),
                            "provider_switch": provider_switch,
                        }
                    tool_approval = event.get("tool_approval")
                    if isinstance(tool_approval, dict):
                        tool_approval = _public_tool_approval(tool_approval)
                        event["tool_approval"] = tool_approval
                        failure_context = {
                            **(failure_context or {}),
                            "tool_approval": tool_approval,
                        }
                elif et == "done":
                    done_received = True
                    done_ok = bool(event.get("ok"))
                # 运行状态立即发给客户端；业务结果等消息落库成功后再发，
                # 避免客户端收到最终答案后断线却无法从历史恢复。
                if et in {
                    "run_started",
                    "provider_selected",
                    "skill_selected",
                    "model_capability_check",
                    "heartbeat",
                    "model_attempt",
                    "retry_scheduled",
                    "model_exhausted",
                    "tool_started",
                    "tool_finished",
                    "assistant_delta",
                    "assistant_reasoning_delta",
                    "assistant_delta_reset",
                }:
                    yield event
                else:
                    buffered_events.append(event)
        except asyncio.CancelledError:
            cancelled = True
            error_code = "AGENT_STREAM_CANCELLED"
            error_message = "连接已中断，本轮未完成，请重试本轮。"
            done_received = True
            done_ok = False
        except Exception as exc:  # noqa: BLE001
            log.exception("system agent stream crashed session=%s", session.id)
            error_code = "AGENT_STREAM_FAILED"
            error_message = f"助手运行异常（{type(exc).__name__}），请重试本轮。"
            buffered_events.extend(
                [
                    {
                        "type": "error",
                        "code": error_code,
                        "message": error_message,
                        "session_id": session.id,
                    },
                    {"type": "done", "ok": False, "session_id": session.id},
                ]
            )
            done_received = True
            done_ok = False

        if not done_received:
            error_code = error_code or "AGENT_STREAM_INCOMPLETE"
            error_message = error_message or "助手响应提前结束，请重试本轮。"
            buffered_events.extend(
                [
                    {
                        "type": "error",
                        "code": error_code,
                        "message": error_message,
                        "session_id": session.id,
                    },
                    {"type": "done", "ok": False, "session_id": session.id},
                ]
            )
            done_ok = False

        if not done_ok:
            # 失败轮次不持久化助手答案，也不应把未提交的最终答案发给客户端。
            buffered_events = [event for event in buffered_events if event.get("type") != "assistant_message"]

        if assistant_text and done_ok:
            usage_payload = dict(usage or {})
            # 关联 Durable Run + 耗时（零迁移，写进既有 usage JSON）
            if run_id:
                usage_payload["run_id"] = str(run_id)
            started_raw = run_started_at if regenerating else None
            if not regenerating and isinstance(user_msg.usage, dict):
                started_raw = user_msg.usage.get("run_started_at")
            if started_raw:
                try:
                    started_dt = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=UTC)
                    usage_payload["elapsed_ms"] = max(
                        0,
                        int((datetime.now(UTC) - started_dt).total_seconds() * 1000),
                    )
                except Exception:  # noqa: BLE001
                    pass
            if int(getattr(user_msg, "retry_count", 0) or 0) > 0:
                usage_payload["retry_count"] = int(user_msg.retry_count)
            usage = usage_payload
            for event in buffered_events:
                if event.get("type") == "assistant_message":
                    event["usage"] = usage_payload
                    if usage_payload.get("stream_fallback"):
                        event["stream_fallback"] = True
            assistant_content = {
                "text": redact_known_secrets(assistant_text, chat_secrets),
                **({"reasoning": assistant_reasoning} if assistant_reasoning else {}),
            }
            if regenerating:
                assert regenerate_assistant_message is not None
                regenerate_assistant_message.content = assistant_content
                regenerate_assistant_message.usage = usage_payload
                regenerate_assistant_message.run_status = MESSAGE_RUN_COMPLETED
                regenerate_assistant_message.error_code = None
                regenerate_assistant_message.error_message = None
                await db.execute(
                    delete(SystemAgentMessage).where(
                        SystemAgentMessage.session_id == session.id,
                        SystemAgentMessage.role == MESSAGE_ROLE_TOOL,
                        SystemAgentMessage.id > user_msg.id,
                    )
                )
            else:
                db.add(SystemAgentMessage(
                    session_id=session.id,
                    role=MESSAGE_ROLE_ASSISTANT,
                    content=assistant_content,
                    usage=usage_payload,
                    run_status=MESSAGE_RUN_COMPLETED,
                ))
        for tev in tool_events:
            if tev.get("type") != "tool_finished":
                continue
            summary = tev.get("result_summary")
            # 工具摘要落库前再对当轮已知密钥打码
            if chat_secrets and isinstance(summary, str):
                summary = redact_known_secrets(summary, chat_secrets)
            elif chat_secrets and isinstance(summary, dict):
                try:
                    import json

                    raw = json.dumps(summary, ensure_ascii=False, default=str)
                    red = redact_known_secrets(raw, chat_secrets)
                    summary = json.loads(red) if red.startswith("{") else {"preview": red}
                except Exception:  # noqa: BLE001
                    summary = {"preview": redact_known_secrets(str(summary), chat_secrets)}
            if not regenerating or done_ok:
                db.add(SystemAgentMessage(
                    session_id=session.id,
                    role=MESSAGE_ROLE_TOOL,
                    content={
                        "tool_name": tev.get("tool_name"),
                        "call_id": tev.get("call_id"),
                        "is_error": tev.get("is_error"),
                        "result_summary": summary,
                    },
                    run_status=MESSAGE_RUN_COMPLETED if done_ok else MESSAGE_RUN_FAILED,
                ))
            memory_event = dict(tev)
            memory_event["result_summary"] = summary
            memory_tool_events.append(memory_event)
        if assistant_text and done_ok:
            user_msg.run_status = MESSAGE_RUN_SUCCEEDED
            user_msg.error_code = None
            user_msg.error_message = None
            user_msg.usage = None
            update_session_memory(
                session,
                user_text=persisted_user_text,
                assistant_text=redact_known_secrets(assistant_text, chat_secrets),
                domains=route_domains,
                tool_events=memory_tool_events,
                replace_latest=regenerating,
            )
            # 主链路不阻塞：摘要过长时后台 LLM 压缩（失败静默降级为条目裁剪结果）
            try:
                state = session.memory_state if isinstance(session.memory_state, dict) else {}
                rev = int(state.get("summary_rev") or 0)
                if should_compress_summary(session.memory_summary):
                    schedule_summary_compression(int(session.id), summary_rev=rev)
            except Exception:  # noqa: BLE001
                log.debug("schedule summary compression failed", exc_info=True)
            if retry_message is not None:
                clear_failed_turn(session, message_id=user_msg.id)
        elif not regenerating:
            user_msg.run_status = MESSAGE_RUN_FAILED
            user_msg.error_code = error_code or "AGENT_RUN_FAILED"
            user_msg.error_message = redact_known_secrets(
                error_message or "助手未能完成本轮请求，请稍后重试。",
                chat_secrets,
            )[:1024]
            failure_usage = dict(failure_context or {})
            if effective_approved_tools:
                failure_usage["approved_tools"] = sorted(effective_approved_tools)
            if run_id:
                failure_usage["run_id"] = str(run_id)
            user_msg.usage = failure_usage or None
            content = user_msg.content if isinstance(user_msg.content, dict) else {}
            remember_failed_turn(
                session,
                message_id=user_msg.id,
                user_goal=str(content.get("text") or ""),
                error_code=user_msg.error_code,
            )
        session.updated_at = datetime.now(UTC)
        await db.flush()
        await db.commit()

        if cancelled:
            raise asyncio.CancelledError

        for event in buffered_events:
            yield event

    async def _resolve_failed_retry_message(
        self,
        db: AsyncSession,
        session: SystemAgentSession,
    ) -> SystemAgentMessage | None:
        anchor = failed_turn_state(session)
        if anchor is not None:
            row = await self.get_message(
                db,
                anchor["message_id"],
                session_id=session.id,
            )
            if row is not None and row.role == MESSAGE_ROLE_USER and row.run_status == MESSAGE_RUN_FAILED:
                return row
            if row is None or row.run_status != MESSAGE_RUN_PENDING:
                clear_failed_turn(session, message_id=anchor["message_id"])
            return None

        # 兼容升级前已经失败、但尚未写入结构化锚点的会话。
        result = await db.execute(
            select(SystemAgentMessage)
            .where(
                SystemAgentMessage.session_id == session.id,
                SystemAgentMessage.role == MESSAGE_ROLE_USER,
                SystemAgentMessage.run_status == MESSAGE_RUN_FAILED,
            )
            .order_by(desc(SystemAgentMessage.id))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        content = row.content if isinstance(row.content, dict) else {}
        remember_failed_turn(
            session,
            message_id=row.id,
            user_goal=str(content.get("text") or ""),
            error_code=str(row.error_code or "AGENT_RUN_FAILED"),
        )
        return row


def _message_available_to_context(message: SystemAgentMessage) -> bool:
    return message.run_status in {MESSAGE_RUN_COMPLETED, MESSAGE_RUN_SUCCEEDED}


_SERVICE: SystemAgentService | None = None


def get_system_agent_service() -> SystemAgentService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SystemAgentService()
    return _SERVICE


__all__ = [
    "SystemAgentService",
    "get_system_agent_service",
    "is_latest_completed_pair",
]
