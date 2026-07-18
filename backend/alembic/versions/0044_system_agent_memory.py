"""system agent memory and retry state

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_agent_session",
        sa.Column("memory_summary", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "system_agent_session",
        sa.Column("memory_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "system_agent_message",
        sa.Column("run_status", sa.String(length=16), nullable=False, server_default="completed"),
    )
    op.add_column(
        "system_agent_message",
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "system_agent_message",
        sa.Column("error_message", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "system_agent_message",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("system_agent_message", "retry_count")
    op.drop_column("system_agent_message", "error_message")
    op.drop_column("system_agent_message", "error_code")
    op.drop_column("system_agent_message", "run_status")
    op.drop_column("system_agent_session", "memory_state")
    op.drop_column("system_agent_session", "memory_summary")
