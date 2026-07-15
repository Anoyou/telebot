"""add event trace tables

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-29

链路日志重构：
- event_trace 记录一条 Telegram 事件的主链路。
- event_span 记录链路阶段。
- event_action 记录插件动作与平台执行结果。
- plugin_runtime_status 记录插件加载和最近调用状态。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_trace",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=80), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("source_channel", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("update_id", sa.BigInteger(), nullable=True),
        sa.Column("callback_query_id", sa.String(length=160), nullable=True),
        sa.Column("sender_user_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name", sa.String(length=256), nullable=True),
        sa.Column("text_preview", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("raw_summary", sa.JSON(), nullable=True),
        sa.Column("payload_snapshot", sa.JSON(), nullable=True),
        sa.Column("native_raw_meta", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id"),
    )
    op.create_index("ix_event_trace_account_started", "event_trace", ["account_id", "started_at"])
    op.create_index("ix_event_trace_account_chat_message", "event_trace", ["account_id", "chat_id", "message_id"])
    op.create_index("ix_event_trace_account_update", "event_trace", ["account_id", "update_id"])
    op.create_index("ix_event_trace_status_started", "event_trace", ["status", "started_at"])

    op.create_table(
        "event_span",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("span_id", sa.String(length=80), nullable=False),
        sa.Column("trace_id", sa.String(length=80), nullable=False),
        sa.Column("parent_span_id", sa.String(length=80), nullable=True),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("component", sa.String(length=128), nullable=True),
        sa.Column("plugin_key", sa.String(length=128), nullable=True),
        sa.Column("entry_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["trace_id"], ["event_trace.trace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("span_id"),
    )
    op.create_index("ix_event_span_trace_started", "event_span", ["trace_id", "started_at"])
    op.create_index("ix_event_span_plugin_started", "event_span", ["plugin_key", "started_at"])

    op.create_table(
        "event_action",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("action_id", sa.String(length=80), nullable=False),
        sa.Column("trace_id", sa.String(length=80), nullable=False),
        sa.Column("plugin_key", sa.String(length=128), nullable=True),
        sa.Column("action_type", sa.String(length=80), nullable=False),
        sa.Column("requested_send_via", sa.String(length=160), nullable=True),
        sa.Column("actual_send_via", sa.String(length=80), nullable=True),
        sa.Column("target_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("target_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("inline_result_count", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["trace_id"], ["event_trace.trace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id"),
    )
    op.create_index("ix_event_action_trace", "event_action", ["trace_id"])
    op.create_index("ix_event_action_plugin_status_created", "event_action", ["plugin_key", "status", "created_at"])

    op.create_table(
        "plugin_runtime_status",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plugin_key", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("installed_version", sa.String(length=64), nullable=True),
        sa.Column("load_status", sa.String(length=32), nullable=False),
        sa.Column("last_load_error", sa.Text(), nullable=True),
        sa.Column("last_invoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_invocation_status", sa.String(length=32), nullable=True),
        sa.Column("last_trace_id", sa.String(length=80), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "plugin_key", name="uq_plugin_runtime_status_account_plugin"),
    )
    op.create_index("ix_plugin_runtime_status_plugin", "plugin_runtime_status", ["plugin_key"])


def downgrade() -> None:
    op.drop_index("ix_plugin_runtime_status_plugin", table_name="plugin_runtime_status")
    op.drop_table("plugin_runtime_status")
    op.drop_index("ix_event_action_plugin_status_created", table_name="event_action")
    op.drop_index("ix_event_action_trace", table_name="event_action")
    op.drop_table("event_action")
    op.drop_index("ix_event_span_plugin_started", table_name="event_span")
    op.drop_index("ix_event_span_trace_started", table_name="event_span")
    op.drop_table("event_span")
    op.drop_index("ix_event_trace_status_started", table_name="event_trace")
    op.drop_index("ix_event_trace_account_update", table_name="event_trace")
    op.drop_index("ix_event_trace_account_chat_message", table_name="event_trace")
    op.drop_index("ix_event_trace_account_started", table_name="event_trace")
    op.drop_table("event_trace")
