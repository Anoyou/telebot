"""llm_usage records the effective client identity

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_usage",
        sa.Column("client_identity_profile", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "client_identity_profile")
