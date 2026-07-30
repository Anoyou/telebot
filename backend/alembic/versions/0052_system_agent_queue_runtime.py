"""System Agent 持久队列、运行输入、等待状态与 worker lease。

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_agent_pending_turn",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("web_user_id", sa.BigInteger(), nullable=True),
        sa.Column("bot_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("blocked_reason", sa.String(length=64), nullable=True),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("content_enc", sa.Text(), nullable=False),
        sa.Column("request_payload", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("dispatch_run_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["system_agent_session.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["web_user_id"], ["web_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_system_agent_pending_turn_session_request",
        ),
    )
    op.create_index(
        "ix_system_agent_pending_turn_session_status_position",
        "system_agent_pending_turn",
        ["session_id", "status", "position"],
    )
    op.create_index(
        "ix_system_agent_pending_turn_owner",
        "system_agent_pending_turn",
        ["web_user_id", "status"],
    )

    with op.batch_alter_table("system_agent_run") as batch:
        batch.add_column(sa.Column("bot_tg_user_id", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("channel", sa.String(length=16), server_default="web", nullable=False)
        )
        batch.add_column(sa.Column("pending_turn_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column("phase", sa.String(length=32), server_default="queued", nullable=False)
        )
        batch.add_column(sa.Column("paused_reason", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("claimed_by", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("usage", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("elapsed_ms", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_system_agent_run_pending_turn",
            "system_agent_pending_turn",
            ["pending_turn_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute(sa.text("UPDATE system_agent_run SET phase = status"))
    op.create_index("ix_system_agent_run_bot_status", "system_agent_run", ["bot_tg_user_id", "status"])
    op.create_index("ix_system_agent_run_lease", "system_agent_run", ["status", "lease_expires_at"])
    op.create_index("ix_system_agent_run_pending_turn", "system_agent_run", ["pending_turn_id"])

    op.create_table(
        "system_agent_run_input",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["system_agent_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "client_request_id", name="uq_system_agent_run_input_request"),
    )
    op.create_index(
        "ix_system_agent_run_input_pending",
        "system_agent_run_input",
        ["run_id", "status", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_agent_run_input_pending", table_name="system_agent_run_input")
    op.drop_table("system_agent_run_input")
    op.drop_index("ix_system_agent_run_pending_turn", table_name="system_agent_run")
    op.drop_index("ix_system_agent_run_lease", table_name="system_agent_run")
    op.drop_index("ix_system_agent_run_bot_status", table_name="system_agent_run")
    with op.batch_alter_table("system_agent_run") as batch:
        batch.drop_constraint("fk_system_agent_run_pending_turn", type_="foreignkey")
        for column in (
            "elapsed_ms",
            "usage",
            "heartbeat_at",
            "lease_expires_at",
            "claimed_by",
            "paused_reason",
            "phase",
            "pending_turn_id",
            "channel",
            "bot_tg_user_id",
        ):
            batch.drop_column(column)
    op.drop_index(
        "ix_system_agent_pending_turn_owner",
        table_name="system_agent_pending_turn",
    )
    op.drop_index(
        "ix_system_agent_pending_turn_session_status_position",
        table_name="system_agent_pending_turn",
    )
    op.drop_table("system_agent_pending_turn")
