"""Structured action ledger for plugin/userbot delivery results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

ACTION_EVENT_STATUS_PENDING = "PENDING"
ACTION_EVENT_STATUS_OK = "OK"
ACTION_EVENT_STATUS_FAILED = "FAILED"
ACTION_EVENT_STATUS_DRY_RUN = "DRY_RUN"
ACTION_EVENT_STATUS_COMPENSATED = "COMPENSATED"
ACTION_EVENT_STATUSES = {
    ACTION_EVENT_STATUS_PENDING,
    ACTION_EVENT_STATUS_OK,
    ACTION_EVENT_STATUS_FAILED,
    ACTION_EVENT_STATUS_DRY_RUN,
    ACTION_EVENT_STATUS_COMPENSATED,
}


class ActionEvent(Base):
    """Append-only action result event.

    This is intentionally separate from ``EventAction``: traces describe the
    request lifecycle, while this table is a structured action ledger consumed
    by replay/debug/ledger surfaces.
    """

    __tablename__ = "action_event"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    plugin_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entry_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    params_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_action_event_account_created", "account_id", "created_at"),
        Index("ix_action_event_plugin_created", "account_id", "plugin_key", "created_at"),
        Index("ix_action_event_status_created", "status", "created_at"),
    )


__all__ = [
    "ACTION_EVENT_STATUS_COMPENSATED",
    "ACTION_EVENT_STATUS_DRY_RUN",
    "ACTION_EVENT_STATUS_FAILED",
    "ACTION_EVENT_STATUS_OK",
    "ACTION_EVENT_STATUS_PENDING",
    "ACTION_EVENT_STATUSES",
    "ActionEvent",
]
