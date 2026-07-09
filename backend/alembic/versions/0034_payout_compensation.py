"""add payout compensation table

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-09

payout 失败补偿阶段 1：仅入队，不做扫描重放和通知。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payout_compensation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("payout_key", sa.String(length=80), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=80), nullable=True),
        sa.Column("plugin_key", sa.String(length=128), nullable=True),
        sa.Column("entry_key", sa.String(length=128), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("error_code_first", sa.String(length=120), nullable=True),
        sa.Column("error_code_last", sa.String(length=120), nullable=True),
        sa.Column("error_last", sa.Text(), nullable=True),
        sa.Column("ambiguous", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_message_id", sa.BigInteger(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payout_key"),
    )
    op.create_index(
        "ix_payout_comp_acct_status_next",
        "payout_compensation",
        ["account_id", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_payout_comp_acct_status_next", table_name="payout_compensation")
    op.drop_table("payout_compensation")
