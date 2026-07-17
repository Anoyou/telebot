"""System Agent 会话、消息与 Action 模型。

阶段 1：session + message；阶段 2：action。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
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

ACTION_STATUS_PENDING = "pending"
ACTION_STATUS_EXECUTING = "executing"
ACTION_STATUS_EXECUTED = "executed"
ACTION_STATUS_REJECTED = "rejected"
ACTION_STATUS_EXPIRED = "expired"
ACTION_STATUS_FAILED = "failed"
ACTION_STATUSES = {
    ACTION_STATUS_PENDING,
    ACTION_STATUS_EXECUTING,
    ACTION_STATUS_EXECUTED,
    ACTION_STATUS_REJECTED,
    ACTION_STATUS_EXPIRED,
    ACTION_STATUS_FAILED,
}

RISK_NORMAL = "normal"
RISK_DANGEROUS = "dangerous"

RUNTIME_SYNC_NOT_REQUIRED = "not_required"
RUNTIME_SYNC_PENDING = "pending"
RUNTIME_SYNC_SUCCEEDED = "succeeded"
RUNTIME_SYNC_FAILED = "failed"


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


class SystemAgentAction(Base):
    """写操作预览与确认记录。"""

    __tablename__ = "system_agent_action"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("system_agent_session.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_bot_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secret_fields: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    secret_payload_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default=RISK_NORMAL)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ACTION_STATUS_PENDING,
        server_default=ACTION_STATUS_PENDING,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    runtime_sync_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RUNTIME_SYNC_NOT_REQUIRED,
        server_default=RUNTIME_SYNC_NOT_REQUIRED,
    )
    runtime_sync_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_system_agent_action_session_created", "session_id", "created_at"),
        Index("ix_system_agent_action_status_expires", "status", "expires_at"),
        Index("ix_system_agent_action_account_created", "account_id", "created_at"),
    )


__all__ = [
    "ACTION_STATUS_EXECUTED",
    "ACTION_STATUS_EXECUTING",
    "ACTION_STATUS_EXPIRED",
    "ACTION_STATUS_FAILED",
    "ACTION_STATUS_PENDING",
    "ACTION_STATUS_REJECTED",
    "ACTION_STATUSES",
    "CHANNEL_BOT",
    "CHANNEL_WEB",
    "CHANNELS",
    "MESSAGE_ROLE_ASSISTANT",
    "MESSAGE_ROLE_SYSTEM_EVENT",
    "MESSAGE_ROLE_TOOL",
    "MESSAGE_ROLE_USER",
    "MESSAGE_ROLES",
    "RISK_DANGEROUS",
    "RISK_NORMAL",
    "RUNTIME_SYNC_FAILED",
    "RUNTIME_SYNC_NOT_REQUIRED",
    "RUNTIME_SYNC_PENDING",
    "RUNTIME_SYNC_SUCCEEDED",
    "SESSION_STATUS_ACTIVE",
    "SESSION_STATUS_ARCHIVED",
    "SESSION_STATUSES",
    "SystemAgentAction",
    "SystemAgentMessage",
    "SystemAgentSession",
]
