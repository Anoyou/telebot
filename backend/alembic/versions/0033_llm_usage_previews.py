"""add request and response previews to llm usage

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("request_preview", sa.Text(), nullable=True))
    op.add_column("llm_usage", sa.Column("response_preview", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "response_preview")
    op.drop_column("llm_usage", "request_preview")
