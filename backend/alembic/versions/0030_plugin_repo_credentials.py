"""add plugin repo credentials

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-27

插件仓库支持 GitHub 私有仓库凭证：
- ``auth_type`` 记录凭证类型，默认 ``none``。
- ``credential_enc`` 使用 MASTER_KEY 加密保存，不回显给前端。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plugin_repo",
        sa.Column("auth_type", sa.String(length=32), nullable=False, server_default="none"),
    )
    op.add_column("plugin_repo", sa.Column("credential_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plugin_repo", "credential_enc")
    op.drop_column("plugin_repo", "auth_type")
