"""sudo_user table.

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sudo_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("allowed_chat_ids", sa.JSON(), nullable=True),
        sa.Column("allowed_commands", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sudo_user_account_tg",
        "sudo_user",
        ["account_id", "tg_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sudo_user_account_tg", table_name="sudo_user")
    op.drop_table("sudo_user")
