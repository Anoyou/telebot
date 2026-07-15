"""llm_provider 增加 Anthropic 请求兼容档案。

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column(
            "protocol_profile",
            sa.String(length=32),
            nullable=False,
            server_default="standard",
        ),
    )
    op.create_check_constraint(
        "ck_llm_provider_protocol_profile",
        "llm_provider",
        "protocol_profile IN ('standard', 'claude_code_proxy')",
    )
    # 0.55.13 及更早版本对所有 Anthropic Messages 请求都会发送 Claude
    # Code/Anyrouter 兼容头。已有记录必须保留历史行为；只有新建 Provider
    # 才使用列默认值 standard，避免升级后现有反代立即失效。
    op.execute(
        sa.text(
            "UPDATE llm_provider "
            "SET protocol_profile = 'claude_code_proxy' "
            "WHERE api_format = 'anthropic_messages'"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_llm_provider_protocol_profile",
        "llm_provider",
        type_="check",
    )
    op.drop_column("llm_provider", "protocol_profile")
