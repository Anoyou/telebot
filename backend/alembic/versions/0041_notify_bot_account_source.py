"""allow notification routes to reference an account management bot

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notify_bot",
        sa.Column("source_account_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_notify_bot_source_account_id",
        "notify_bot",
        ["source_account_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_notify_bot_source_account_id_account",
        "notify_bot",
        "account",
        ["source_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_notify_bot_source_account_id_account",
        "notify_bot",
        type_="foreignkey",
    )
    op.drop_index("ix_notify_bot_source_account_id", table_name="notify_bot")
    op.drop_column("notify_bot", "source_account_id")
