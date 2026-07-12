"""llm_provider 增加客户端身份档案。

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-12

0.57.0 阶段 A：新增 ``client_identity_profile`` 列，控制 UA 与身份相关的安全
请求头，与 ``protocol_profile``（协议语义 / beta 头）相互独立。

升级语义：
- 所有现有 Provider 迁移为 ``auto``；``auto`` 按本次实际协议解析身份，因此升级后
  不再发送 TelePilot 产品 UA，而是对应协议的真实客户端身份。
- 现有 ``protocol_profile=claude_code_proxy`` 的 Anthropic Provider 仍保留协议档案，
  并由 ``auto`` 身份解析为 Claude Code。
- 增加 check 约束限定合法取值。

降级语义：
- 只删除 check 约束和新列，不改动 ``api_format / protocol_profile / models``，
  避免破坏已有 Provider 配置。旧版本恢复其历史请求头行为（发送 TelePilot UA）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


_ALLOWED_IDENTITIES = (
    "auto",
    "minimal",
    "openai_sdk",
    "codex_cli",
    "codex_desktop",
    "claude_code",
    "claude_desktop",
)


def _backfill_client_identity(conn) -> None:
    """现有 Provider 一律迁移为 auto：不再发送 TelePilot UA，改由 auto 按本次实际
    协议解析真实客户端身份。已有 claude_code_proxy 协议档案不受影响。

    抽成独立函数便于在不依赖 alembic op 上下文的情况下做单元测试。
    """
    conn.execute(sa.text("UPDATE llm_provider SET client_identity_profile = 'auto'"))


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column(
            "client_identity_profile",
            sa.String(length=32),
            nullable=False,
            server_default="auto",
        ),
    )
    values = ", ".join(f"'{value}'" for value in _ALLOWED_IDENTITIES)
    op.create_check_constraint(
        "ck_llm_provider_client_identity_profile",
        "llm_provider",
        f"client_identity_profile IN ({values})",
    )
    _backfill_client_identity(op.get_bind())


def downgrade() -> None:
    op.drop_constraint(
        "ck_llm_provider_client_identity_profile",
        "llm_provider",
        type_="check",
    )
    op.drop_column("llm_provider", "client_identity_profile")
