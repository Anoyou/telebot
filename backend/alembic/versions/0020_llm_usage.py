"""create llm_usage table

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("account.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider_id", sa.BigInteger(), sa.ForeignKey("llm_provider.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider_name", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("used_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fallback_chain", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_usage_account_id", "llm_usage", ["account_id"])
    op.create_index("ix_llm_usage_provider_id", "llm_usage", ["provider_id"])
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_index("ix_llm_usage_provider_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_account_id", table_name="llm_usage")
    op.drop_table("llm_usage")

