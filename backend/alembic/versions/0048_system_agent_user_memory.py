"""system agent long-term user memory

Revision ID: 0048
Revises: 0047
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_agent_user_memory",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.BigInteger(), nullable=False),
        sa.Column("content", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="user_set"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_system_agent_user_memory_scope",
        "system_agent_user_memory",
        ["scope_type", "scope_id"],
    )
    op.create_index(
        "ix_system_agent_user_memory_scope_enabled",
        "system_agent_user_memory",
        ["scope_type", "scope_id", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_agent_user_memory_scope_enabled", table_name="system_agent_user_memory")
    op.drop_index("ix_system_agent_user_memory_scope", table_name="system_agent_user_memory")
    op.drop_table("system_agent_user_memory")
