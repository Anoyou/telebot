"""扩展 LLM Provider protocol_profile 数据库约束。

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_llm_provider_protocol_profile"
_TABLE_NAME = "llm_provider"
_CURRENT_CONDITION = (
    "protocol_profile IN ("
    "'standard', 'openai_responses', 'deepseek_responses', "
    "'codex_responses', 'claude_code_proxy'"
    ")"
)
_LEGACY_CONDITION = "protocol_profile IN ('standard', 'claude_code_proxy')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        _CURRENT_CONDITION,
    )


def downgrade() -> None:
    # 旧版本只认识 standard / claude_code_proxy。先把 Responses 档案收敛为
    # standard，再恢复旧约束，避免 downgrade 因已有数据而中断。
    op.execute(
        "UPDATE llm_provider SET protocol_profile = 'standard' "
        "WHERE protocol_profile NOT IN ('standard', 'claude_code_proxy')"
    )
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        _LEGACY_CONDITION,
    )
