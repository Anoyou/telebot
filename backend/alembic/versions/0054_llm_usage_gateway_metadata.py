"""增加 LLM usage Gateway 诊断字段。

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧记录保持 NULL，避免把未知历史事实伪造为 direct。
    op.add_column("llm_usage", sa.Column("execution_backend", sa.String(length=32), nullable=True))
    op.add_column("llm_usage", sa.Column("gateway_version", sa.String(length=64), nullable=True))
    op.add_column("llm_usage", sa.Column("gateway_request_id", sa.String(length=128), nullable=True))
    op.add_column("llm_usage", sa.Column("gateway_stage", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_llm_usage_execution_backend",
        "llm_usage",
        "execution_backend IS NULL OR execution_backend IN ('direct', 'codex_gateway')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_llm_usage_execution_backend", "llm_usage", type_="check")
    op.drop_column("llm_usage", "gateway_stage")
    op.drop_column("llm_usage", "gateway_request_id")
    op.drop_column("llm_usage", "gateway_version")
    op.drop_column("llm_usage", "execution_backend")
