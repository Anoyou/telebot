"""System Agent 会话与消息模型（阶段 1）。

阶段 2 再引入 ``SystemAgentAction``；本文件只覆盖只读助手所需的持久化。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_ARCHIVED = "archived"
SESSION_STATUSES = {SESSION_STATUS_ACTIVE, SESSION_STATUS_ARCHIVED}

CHANNEL_WEB = "web"
CHANNEL_BOT = "bot"
CHANNELS = {CHANNEL_WEB, CHANNEL_BOT}

MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"
MESSAGE_ROLE_TOOL = "tool"
MESSAGE_ROLE_SYSTEM_EVENT = "system_event"
MESSAGE_ROLES = {
    MESSAGE_ROLE_USER,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_TOOL,
    MESSAGE_ROLE_SYSTEM_EVENT,
}


class SystemAgentSession(Base):
    """System Agent 会话。"""

    __tablename__ = "system_agent_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    web_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("web_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    bot_tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SESSION_STATUS_ACTIVE,
        server_default=SESSION_STATUS_ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_system_agent_session_web_user_updated", "web_user_id", "updated_at"),
        Index("ix_system_agent_session_bot_user_updated", "bot_tg_user_id", "updated_at"),
        Index("ix_system_agent_session_account_updated", "account_id", "updated_at"),
        Index("ix_system_agent_session_status", "status"),
    )


class SystemAgentMessage(Base):
    """System Agent 消息（落库为打码后内容）。"""

    __tablename__ = "system_agent_message"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("system_agent_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_system_agent_message_session_created", "session_id", "created_at"),
    )


__all__ = [
    "CHANNEL_BOT",
    "CHANNEL_WEB",
    "CHANNELS",
    "MESSAGE_ROLE_ASSISTANT",
    "MESSAGE_ROLE_SYSTEM_EVENT",
    "MESSAGE_ROLE_TOOL",
    "MESSAGE_ROLE_USER",
    "MESSAGE_ROLES",
    "SESSION_STATUS_ACTIVE",
    "SESSION_STATUS_ARCHIVED",
    "SESSION_STATUSES",
    "SystemAgentMessage",
    "SystemAgentSession",
]
