"""system agent session and message tables (stage 1)

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_agent_session",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("web_user_id", sa.BigInteger(), sa.ForeignKey("web_user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("bot_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("account.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
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
    )
    op.create_index(
        "ix_system_agent_session_web_user_updated",
        "system_agent_session",
        ["web_user_id", "updated_at"],
    )
    op.create_index(
        "ix_system_agent_session_bot_user_updated",
        "system_agent_session",
        ["bot_tg_user_id", "updated_at"],
    )
    op.create_index(
        "ix_system_agent_session_account_updated",
        "system_agent_session",
        ["account_id", "updated_at"],
    )
    op.create_index(
        "ix_system_agent_session_status",
        "system_agent_session",
        ["status"],
    )

    op.create_table(
        "system_agent_message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("system_agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_system_agent_message_session_created",
        "system_agent_message",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_agent_message_session_created", table_name="system_agent_message")
    op.drop_table("system_agent_message")
    op.drop_index("ix_system_agent_session_status", table_name="system_agent_session")
    op.drop_index("ix_system_agent_session_account_updated", table_name="system_agent_session")
    op.drop_index("ix_system_agent_session_bot_user_updated", table_name="system_agent_session")
    op.drop_index("ix_system_agent_session_web_user_updated", table_name="system_agent_session")
    op.drop_table("system_agent_session")
