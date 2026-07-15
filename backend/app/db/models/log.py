"""操作日志（Web 端动作）与运行日志（worker 输出）。"""

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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# RuntimeLog level
LEVEL_DEBUG = "debug"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"


class AuditLog(Base):
    """Web 端操作日志，由依赖中间件写入。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("web_user.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class RuntimeLog(Base):
    """worker 运行时日志，由主进程从 IPC 收到后批量落库。"""

    __tablename__ = "runtime_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_runtime_log_account_ts", "account_id", "ts"),
        Index("ix_runtime_log_account_level_ts", "account_id", "level", "ts"),
    )


class PluginConfigActionJob(Base):
    """插件配置页后台动作任务。"""

    __tablename__ = "plugin_config_action_job"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    plugin_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_preview: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    config_patch: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_plugin_config_action_job_account_created", "account_id", "created_at"),
        Index("ix_plugin_config_action_job_plugin_status_created", "plugin_key", "status", "created_at"),
    )


class EventTrace(Base):
    """一条 Telegram 事件在 TelePilot 内的完整链路。"""

    __tablename__ = "event_trace"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=True
    )
    source_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    callback_query_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sender_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    text_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    native_raw_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_event_trace_account_started", "account_id", "started_at"),
        Index("ix_event_trace_account_chat_message", "account_id", "chat_id", "message_id"),
        Index("ix_event_trace_account_update", "account_id", "update_id"),
        Index("ix_event_trace_status_started", "status", "started_at"),
    )


class EventSpan(Base):
    """Trace 中的一个阶段。"""

    __tablename__ = "event_span"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    span_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("event_trace.trace_id", ondelete="CASCADE"), nullable=False
    )
    parent_span_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    component: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plugin_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entry_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_event_span_trace_started", "trace_id", "started_at"),
        Index("ix_event_span_plugin_started", "plugin_key", "started_at"),
    )


class EventAction(Base):
    """插件请求动作与平台执行结果。"""

    __tablename__ = "event_action"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("event_trace.trace_id", ondelete="CASCADE"), nullable=False
    )
    plugin_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    requested_send_via: Mapped[str | None] = mapped_column(String(160), nullable=True)
    actual_send_via: Mapped[str | None] = mapped_column(String(80), nullable=True)
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inline_result_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_event_action_trace", "trace_id"),
        Index("ix_event_action_plugin_status_created", "plugin_key", "status", "created_at"),
    )


class PluginRuntimeStatus(Base):
    """插件加载和最近调用状态，用于日志中心诊断。"""

    __tablename__ = "plugin_runtime_status"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plugin_key: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    installed_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    load_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    last_load_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_invoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_invocation_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_trace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "plugin_key", name="uq_plugin_runtime_status_account_plugin"),
        Index("ix_plugin_runtime_status_plugin", "plugin_key"),
    )
