"""system agent action table (stage 2)

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_agent_action",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("system_agent_session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("account.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_bot_user_id", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("secret_fields", sa.JSON(), nullable=True),
        sa.Column("secret_payload_enc", sa.Text(), nullable=True),
        sa.Column("summary", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column(
            "runtime_sync_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_required",
        ),
        sa.Column("runtime_sync_error", sa.String(length=1024), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_system_agent_action_session_created",
        "system_agent_action",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_system_agent_action_status_expires",
        "system_agent_action",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_system_agent_action_account_created",
        "system_agent_action",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_agent_action_account_created", table_name="system_agent_action")
    op.drop_index("ix_system_agent_action_status_expires", table_name="system_agent_action")
    op.drop_index("ix_system_agent_action_session_created", table_name="system_agent_action")
    op.drop_table("system_agent_action")
