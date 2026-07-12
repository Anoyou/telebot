"""llm_provider 增加联网搜索协议覆盖。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_provider",
        sa.Column(
            "web_search_api_format",
            sa.String(length=32),
            nullable=False,
            server_default="auto",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_provider", "web_search_api_format")
