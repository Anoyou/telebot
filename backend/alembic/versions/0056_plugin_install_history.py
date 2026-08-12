"""新增插件安装历史时间线。

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_install_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plugin_key", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("previous_version", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("source_label", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("signature_ok", sa.Boolean(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_install_history_key_created",
        "plugin_install_history",
        ["plugin_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plugin_install_history_key_created",
        table_name="plugin_install_history",
    )
    op.drop_table("plugin_install_history")
