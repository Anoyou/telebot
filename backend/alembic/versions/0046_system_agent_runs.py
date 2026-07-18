"""persist system agent runs and resumable events

Revision ID: 0046
Revises: 0045
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_agent_run",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("system_agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "web_user_id",
            sa.BigInteger(),
            sa.ForeignKey("web_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_message_id",
            sa.BigInteger(),
            sa.ForeignKey("system_agent_message.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_system_agent_run_session_request",
        ),
    )
    op.create_index(
        "ix_system_agent_run_session_created",
        "system_agent_run",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_system_agent_run_user_status",
        "system_agent_run",
        ["web_user_id", "status"],
    )
    op.create_index(
        "ix_system_agent_run_status_updated",
        "system_agent_run",
        ["status", "updated_at"],
    )

    op.create_table(
        "system_agent_run_event",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("system_agent_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_system_agent_run_event_seq"),
    )
    op.create_index(
        "ix_system_agent_run_event_run_seq",
        "system_agent_run_event",
        ["run_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_agent_run_event_run_seq", table_name="system_agent_run_event")
    op.drop_table("system_agent_run_event")
    op.drop_index("ix_system_agent_run_status_updated", table_name="system_agent_run")
    op.drop_index("ix_system_agent_run_user_status", table_name="system_agent_run")
    op.drop_index("ix_system_agent_run_session_created", table_name="system_agent_run")
    op.drop_table("system_agent_run")
