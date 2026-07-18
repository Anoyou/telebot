"""允许 LLM Provider 使用 Grok CLI 客户端身份档案。

Revision ID: 0047
Revises: 0046
Create Date: 2026-07-19

``grok_cli`` 已在 API Schema、运行时身份目录和模型层允许，但 0040 的数据库
检查约束遗漏了该值，导致创建 Provider 时被数据库拒绝。本迁移只替换约束，不改动
任何既有 Provider 配置。
"""

from __future__ import annotations

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


_CONSTRAINT_NAME = "ck_llm_provider_client_identity_profile"
_PREVIOUS_IDENTITIES = (
    "auto",
    "minimal",
    "openai_sdk",
    "codex_cli",
    "codex_desktop",
    "claude_code",
    "claude_desktop",
)
_ALLOWED_IDENTITIES = (*_PREVIOUS_IDENTITIES, "grok_cli")


def _constraint_expression(identities: tuple[str, ...]) -> str:
    values = ", ".join(f"'{identity}'" for identity in identities)
    return f"client_identity_profile IN ({values})"


def _replace_constraint(identities: tuple[str, ...]) -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "llm_provider", type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "llm_provider",
        _constraint_expression(identities),
    )


def upgrade() -> None:
    _replace_constraint(_ALLOWED_IDENTITIES)


def downgrade() -> None:
    _replace_constraint(_PREVIOUS_IDENTITIES)
