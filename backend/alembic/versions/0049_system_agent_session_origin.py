"""system agent session origin (scheduled vs interactive)

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_agent_session",
        sa.Column(
            "origin",
            sa.String(length=16),
            nullable=False,
            server_default="interactive",
        ),
    )
    op.create_index(
        "ix_system_agent_session_origin",
        "system_agent_session",
        ["origin"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_agent_session_origin", table_name="system_agent_session")
    op.drop_column("system_agent_session", "origin")
