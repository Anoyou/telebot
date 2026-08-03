"""增加 LLM Provider execution_backend。

Revision ID: 0053
Revises: 0052
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column("execution_backend", sa.String(length=32), server_default="direct", nullable=False),
    )
    op.create_check_constraint(
        "ck_llm_provider_execution_backend",
        "llm_provider",
        "execution_backend IN ('direct', 'codex_gateway')",
    )


def downgrade() -> None:
    op.execute("UPDATE llm_provider SET execution_backend = 'direct' WHERE execution_backend != 'direct'")
    op.drop_constraint("ck_llm_provider_execution_backend", "llm_provider", type_="check")
    op.drop_column("llm_provider", "execution_backend")
