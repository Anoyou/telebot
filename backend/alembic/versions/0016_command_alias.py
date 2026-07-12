"""command_alias table.

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_alias",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alias", sa.String(64), nullable=False, unique=True),
        sa.Column("target", sa.String(128), nullable=False),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("command_alias")
