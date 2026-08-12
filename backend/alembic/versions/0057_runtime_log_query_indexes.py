"""优化多账号运行日志筛选索引。

Revision ID: 0057
Revises: 0056
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_runtime_log_source_ts",
        "runtime_log",
        ["source", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_log_account_source_ts",
        "runtime_log",
        ["account_id", "source", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_log_account_source_ts", table_name="runtime_log")
    op.drop_index("ix_runtime_log_source_ts", table_name="runtime_log")
