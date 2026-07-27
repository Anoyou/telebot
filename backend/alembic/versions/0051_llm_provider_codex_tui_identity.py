"""将 LLM Provider 的 Codex CLI 身份迁移为 Codex TUI。

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-27

运行时已把旧 ``codex_cli`` / ``codex_exec`` 规范化为 ``codex_tui``，但 0047
创建的数据库检查约束仍只允许 ``codex_cli``。这会导致任何相关 Provider 保存时
触发 ``ck_llm_provider_client_identity_profile``。本迁移同步数据与约束。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0051"
down_revision = "0050"
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
    "grok_cli",
)
_ALLOWED_IDENTITIES = (
    "auto",
    "minimal",
    "openai_sdk",
    "codex_tui",
    "codex_desktop",
    "claude_code",
    "claude_desktop",
    "grok_cli",
)


def _constraint_expression(identities: tuple[str, ...]) -> str:
    values = ", ".join(f"'{identity}'" for identity in identities)
    return f"client_identity_profile IN ({values})"


def _drop_constraint() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "llm_provider", type_="check")


def _create_constraint(identities: tuple[str, ...]) -> None:
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "llm_provider",
        _constraint_expression(identities),
    )


def upgrade() -> None:
    _drop_constraint()
    op.execute(
        sa.text(
            "UPDATE llm_provider SET client_identity_profile = 'codex_tui' "
            "WHERE client_identity_profile IN ('codex_cli', 'codex_exec')"
        )
    )
    _create_constraint(_ALLOWED_IDENTITIES)


def downgrade() -> None:
    _drop_constraint()
    op.execute(
        sa.text(
            "UPDATE llm_provider SET client_identity_profile = 'codex_cli' "
            "WHERE client_identity_profile = 'codex_tui'"
        )
    )
    _create_constraint(_PREVIOUS_IDENTITIES)
