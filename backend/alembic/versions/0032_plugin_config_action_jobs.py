"""add plugin config action jobs

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-30

插件配置动作后台化：
- plugin_config_action_job 记录配置页动作的后台任务状态。
- 具体过程日志复用 runtime_log，并通过 detail.config_action_job_id 关联。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_config_action_job",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=80), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_key", sa.String(length=128), nullable=False),
        sa.Column("action_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_preview", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("config_patch", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_plugin_config_action_job_account_created",
        "plugin_config_action_job",
        ["account_id", "created_at"],
    )
    op.create_index(
        "ix_plugin_config_action_job_plugin_status_created",
        "plugin_config_action_job",
        ["plugin_key", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_plugin_config_action_job_plugin_status_created", table_name="plugin_config_action_job")
    op.drop_index("ix_plugin_config_action_job_account_created", table_name="plugin_config_action_job")
    op.drop_table("plugin_config_action_job")
