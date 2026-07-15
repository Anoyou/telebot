"""enforce single web user registration

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    user_count = op.get_bind().execute(sa.text("SELECT count(*) FROM web_user")).scalar_one()
    if int(user_count or 0) > 1:
        raise RuntimeError(
            "web_user 存在多条历史记录，无法自动启用单管理员约束；"
            "请先人工确认并合并重复管理员账号"
        )
    op.add_column(
        "web_user",
        sa.Column("singleton_key", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        "ck_web_user_singleton_key",
        "web_user",
        "singleton_key = 1",
    )
    op.create_unique_constraint(
        "uq_web_user_singleton_key",
        "web_user",
        ["singleton_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_web_user_singleton_key", "web_user", type_="unique")
    op.drop_constraint("ck_web_user_singleton_key", "web_user", type_="check")
    op.drop_column("web_user", "singleton_key")
