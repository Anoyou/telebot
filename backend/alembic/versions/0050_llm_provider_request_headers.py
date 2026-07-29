"""LLM Provider encrypted compatibility request headers

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column("request_headers_enc", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_provider", "request_headers_enc")
