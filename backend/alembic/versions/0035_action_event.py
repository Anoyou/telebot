"""add structured action event ledger

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=True),
        sa.Column("session_key", sa.String(length=200), nullable=True),
        sa.Column("plugin_key", sa.String(length=128), nullable=True),
        sa.Column("entry_key", sa.String(length=128), nullable=True),
        sa.Column("action_type", sa.String(length=80), nullable=False),
        sa.Column("params_summary", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_event_account_created", "action_event", ["account_id", "created_at"])
    op.create_index("ix_action_event_plugin_created", "action_event", ["account_id", "plugin_key", "created_at"])
    op.create_index("ix_action_event_status_created", "action_event", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_action_event_status_created", table_name="action_event")
    op.drop_index("ix_action_event_plugin_created", table_name="action_event")
    op.drop_index("ix_action_event_account_created", table_name="action_event")
    op.drop_table("action_event")
