"""System Agent 会话、消息与 Action 模型。

阶段 1：session + message；阶段 2：action。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_ARCHIVED = "archived"
SESSION_STATUSES = {SESSION_STATUS_ACTIVE, SESSION_STATUS_ARCHIVED}

CHANNEL_WEB = "web"
CHANNEL_BOT = "bot"
CHANNELS = {CHANNEL_WEB, CHANNEL_BOT}

SESSION_ORIGIN_INTERACTIVE = "interactive"
SESSION_ORIGIN_SCHEDULED = "scheduled"
SESSION_ORIGINS = {SESSION_ORIGIN_INTERACTIVE, SESSION_ORIGIN_SCHEDULED}

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

MESSAGE_RUN_PENDING = "pending"
MESSAGE_RUN_SUCCEEDED = "succeeded"
MESSAGE_RUN_FAILED = "failed"
MESSAGE_RUN_COMPLETED = "completed"
MESSAGE_RUN_STATUSES = {
    MESSAGE_RUN_PENDING,
    MESSAGE_RUN_SUCCEEDED,
    MESSAGE_RUN_FAILED,
    MESSAGE_RUN_COMPLETED,
}

AGENT_RUN_QUEUED = "queued"
AGENT_RUN_RUNNING = "running"
AGENT_RUN_SUCCEEDED = "succeeded"
AGENT_RUN_FAILED = "failed"
AGENT_RUN_CANCELLED = "cancelled"
AGENT_RUN_TERMINAL_STATUSES = {
    AGENT_RUN_SUCCEEDED,
    AGENT_RUN_FAILED,
    AGENT_RUN_CANCELLED,
}
AGENT_RUN_ACTIVE_STATUSES = {AGENT_RUN_QUEUED, AGENT_RUN_RUNNING}

AGENT_RUN_KIND_MESSAGE = "message"
AGENT_RUN_KIND_RETRY = "retry"
AGENT_RUN_KIND_REGENERATE = "regenerate"

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
    origin: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SESSION_ORIGIN_INTERACTIVE,
        server_default=SESSION_ORIGIN_INTERACTIVE,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SESSION_STATUS_ACTIVE,
        server_default=SESSION_STATUS_ACTIVE,
    )
    memory_summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
    )
    memory_state: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
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
        Index("ix_system_agent_session_origin", "origin"),
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
    run_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MESSAGE_RUN_COMPLETED,
        server_default=MESSAGE_RUN_COMPLETED,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_system_agent_message_session_created", "session_id", "created_at"),
    )


class SystemAgentRun(Base):
    """Web Agent 的持久运行句柄；原始输入仍只由消息服务负责脱敏落库。"""

    __tablename__ = "system_agent_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("system_agent_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    web_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("web_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("system_agent_message.id", ondelete="SET NULL"),
        nullable=True,
    )
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=AGENT_RUN_QUEUED,
        server_default=AGENT_RUN_QUEUED,
    )
    last_seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_system_agent_run_session_request",
        ),
        Index("ix_system_agent_run_session_created", "session_id", "created_at"),
        Index("ix_system_agent_run_user_status", "web_user_id", "status"),
        Index("ix_system_agent_run_status_updated", "status", "updated_at"),
    )


class SystemAgentRunEvent(Base):
    """可按单调序号重放的 Agent 运行事件。"""

    __tablename__ = "system_agent_run_event"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("system_agent_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_system_agent_run_event_seq"),
        Index("ix_system_agent_run_event_run_seq", "run_id", "seq"),
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



class SystemAgentUserMemory(Base):
    """跨会话长期偏好（用户可见、可编辑）。"""

    __tablename__ = "system_agent_user_memory"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="user_set")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
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
        Index("ix_system_agent_user_memory_scope", "scope_type", "scope_id"),
        Index("ix_system_agent_user_memory_scope_enabled", "scope_type", "scope_id", "enabled"),
    )

__all__ = [
    "AGENT_RUN_ACTIVE_STATUSES",
    "AGENT_RUN_CANCELLED",
    "AGENT_RUN_FAILED",
    "AGENT_RUN_KIND_MESSAGE",
    "AGENT_RUN_KIND_REGENERATE",
    "AGENT_RUN_KIND_RETRY",
    "AGENT_RUN_QUEUED",
    "AGENT_RUN_RUNNING",
    "AGENT_RUN_SUCCEEDED",
    "AGENT_RUN_TERMINAL_STATUSES",
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
    "MESSAGE_RUN_COMPLETED",
    "MESSAGE_RUN_FAILED",
    "MESSAGE_RUN_PENDING",
    "MESSAGE_RUN_STATUSES",
    "MESSAGE_RUN_SUCCEEDED",
    "RISK_DANGEROUS",
    "RISK_NORMAL",
    "RUNTIME_SYNC_FAILED",
    "RUNTIME_SYNC_NOT_REQUIRED",
    "RUNTIME_SYNC_PENDING",
    "RUNTIME_SYNC_SUCCEEDED",
    "SESSION_ORIGIN_INTERACTIVE",
    "SESSION_ORIGIN_SCHEDULED",
    "SESSION_ORIGINS",
    "SESSION_STATUS_ACTIVE",
    "SESSION_STATUS_ARCHIVED",
    "SESSION_STATUSES",
    "SystemAgentAction",
    "SystemAgentMessage",
    "SystemAgentRun",
    "SystemAgentRunEvent",
    "SystemAgentSession",
    "SystemAgentUserMemory",
]
