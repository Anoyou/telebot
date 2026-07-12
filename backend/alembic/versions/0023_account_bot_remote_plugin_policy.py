"""account_bot 增加远程插件高风险操作开关。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_bot",
        sa.Column(
            "remote_plugin_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("account_bot", "remote_plugin_policy")
