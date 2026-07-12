"""Payout failure compensation ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

PAYOUT_COMPENSATION_STATUS_PENDING = "pending"
PAYOUT_COMPENSATION_STATUS_SENDING = "sending"
PAYOUT_COMPENSATION_STATUS_SENT = "sent"
PAYOUT_COMPENSATION_STATUS_COMPENSATED = "compensated"
PAYOUT_COMPENSATION_STATUS_ABANDONED = "abandoned"


class PayoutCompensation(Base):
    """Pending payout compensation ticket.

    Rows are enqueued on payout failure and driven to a terminal state
    (``sent`` / ``abandoned``) by the worker replay scan; replay, sent markers
    and notifications are all live.
    """

    __tablename__ = "payout_compensation"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    payout_key: Mapped[str] = mapped_column(String(80), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plugin_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entry_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    delivery_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PAYOUT_COMPENSATION_STATUS_PENDING,
        server_default=PAYOUT_COMPENSATION_STATUS_PENDING,
    )
    error_code_first: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code_last: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_last: Mapped[str | None] = mapped_column(Text, nullable=True)
    ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("account_id", "payout_key", name="uq_payout_compensation_account_key"),
        Index("ix_payout_comp_acct_status_next", "account_id", "status", "next_attempt_at"),
    )


__all__ = [
    "PAYOUT_COMPENSATION_STATUS_ABANDONED",
    "PAYOUT_COMPENSATION_STATUS_COMPENSATED",
    "PAYOUT_COMPENSATION_STATUS_PENDING",
    "PAYOUT_COMPENSATION_STATUS_SENDING",
    "PAYOUT_COMPENSATION_STATUS_SENT",
    "PayoutCompensation",
]
