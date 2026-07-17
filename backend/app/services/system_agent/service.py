"""System Agent 会话、消息与主流程编排。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.system_agent import (
    CHANNEL_BOT,
    CHANNEL_WEB,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_TOOL,
    MESSAGE_ROLE_USER,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    SystemAgentMessage,
    SystemAgentSession,
)
from .actions import clear_expired_secrets
from .config import load_config, load_system_context_flags, save_config
from .prompts import session_title_from_message
from .registry import get_registry
from .runtime import SystemAgentRuntime
from .secrets import extract_plaintext_secrets, redact_known_secrets

log = logging.getLogger(__name__)


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
        return {
            "enabled": bool(cfg.get("enabled")),
            "provider_id": cfg.get("provider_id"),
            "model": cfg.get("model"),
            "ai_enabled": bool(flags["ai_enabled"]),
            "timezone": flags["timezone"],
            "tools": registry.capabilities(channel=channel, role=role),
            "stage": 3,
            "write_tools_available": True,
            "secret_chat_input": True,
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
    ) -> SystemAgentSession:
        if channel not in {CHANNEL_WEB, CHANNEL_BOT}:
            raise ValueError(f"invalid channel: {channel}")
        if channel == CHANNEL_BOT and account_id is None:
            raise ValueError("Bot 会话必须绑定 account_id")
        session = SystemAgentSession(
            id=str(uuid.uuid4()),
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            account_id=account_id,
            channel=channel,
            title=title,
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
        limit: int = 50,
    ) -> list[SystemAgentSession]:
        q = select(SystemAgentSession).order_by(desc(SystemAgentSession.updated_at)).limit(
            max(1, min(limit, 200))
        )
        if web_user_id is not None:
            q = q.where(SystemAgentSession.web_user_id == web_user_id)
        if bot_tg_user_id is not None:
            q = q.where(SystemAgentSession.bot_tg_user_id == bot_tg_user_id)
        if account_id is not None:
            q = q.where(SystemAgentSession.account_id == account_id)
        if status:
            q = q.where(SystemAgentSession.status == status)
        result = await db.execute(q)
        return list(result.scalars().all())

    async def get_session(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
    ) -> SystemAgentSession | None:
        session = await db.get(SystemAgentSession, session_id)
        if session is None:
            return None
        if web_user_id is not None and session.web_user_id != web_user_id:
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

    async def get_or_create_active_session(
        self,
        db: AsyncSession,
        *,
        channel: str,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
        account_id: int | None = None,
    ) -> SystemAgentSession:
        sessions = await self.list_sessions(
            db,
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            account_id=account_id if channel == CHANNEL_BOT else None,
            status=SESSION_STATUS_ACTIVE,
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
    ) -> AsyncIterator[dict[str, Any]]:
        raw_text = str(text or "").strip()
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

        if not session.title:
            session.title = session_title_from_message(raw_text)
        session.updated_at = datetime.now(UTC)

        # 用户消息：原始文本参与模型请求；落库前替换已提取密钥并基础打码
        chat_secrets = extract_plaintext_secrets(raw_text)
        redacted_user = redact_known_secrets(raw_text, chat_secrets)
        user_msg = SystemAgentMessage(
            session_id=session.id,
            role=MESSAGE_ROLE_USER,
            content={"text": redacted_user},
        )
        db.add(user_msg)
        await db.flush()

        history = await self.list_messages(db, session.id, limit=40)
        # 历史最后一条是刚写入的打码用户消息；模型当次请求使用原始文本，
        # 因此从 history 去掉最后一条 user，由 runtime 追加 raw_text。
        history_for_model = [m for m in history if m.id != user_msg.id]

        assistant_text = ""
        usage: dict[str, Any] | None = None
        tool_events: list[dict[str, Any]] = []

        async for event in self.runtime.stream_turn(
            db,
            session=session,
            user_text=raw_text,
            role=role,
            channel=channel,
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            history_messages=history_for_model,
            chat_secrets=chat_secrets,
        ):
            et = event.get("type")
            if et == "assistant_message":
                assistant_text = str(event.get("content") or "")
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
            elif et in {"tool_started", "tool_finished"}:
                tool_events.append(event)
            yield event

        if assistant_text:
            db.add(
                SystemAgentMessage(
                    session_id=session.id,
                    role=MESSAGE_ROLE_ASSISTANT,
                    content={"text": redact_known_secrets(assistant_text, chat_secrets)},
                    usage=usage,
                )
            )
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
            db.add(
                SystemAgentMessage(
                    session_id=session.id,
                    role=MESSAGE_ROLE_TOOL,
                    content={
                        "tool_name": tev.get("tool_name"),
                        "call_id": tev.get("call_id"),
                        "is_error": tev.get("is_error"),
                        "result_summary": summary,
                    },
                )
            )
        session.updated_at = datetime.now(UTC)
        await db.flush()


_SERVICE: SystemAgentService | None = None


def get_system_agent_service() -> SystemAgentService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SystemAgentService()
    return _SERVICE


__all__ = ["SystemAgentService", "get_system_agent_service"]
