"""风控相关：模板 / 规则 / 事件 / 临时覆盖。

PRD §L 完整实现。三层叠加由 service 层做，DB 只负责持久化。
"""

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
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

# RateLimitRule.scope
SCOPE_TEMPLATE = "template"
SCOPE_ACCOUNT = "account"
SCOPE_RULE = "rule"

# 抑制策略
POLICY_DROP = "drop"
POLICY_QUEUE = "queue"
POLICY_BACKOFF = "backoff"
POLICY_PAUSE = "pause"
POLICY_NOTIFY = "notify"

# 事件 outcome
OUTCOME_OK = "ok"
OUTCOME_DROP = "drop"
OUTCOME_QUEUED = "queued"
OUTCOME_BACKOFF = "backoff"
OUTCOME_PAUSE = "pause"
OUTCOME_FLOODWAIT = "floodwait"
OUTCOME_PEERFLOOD = "peerflood"
OUTCOME_SLOWMODE = "slowmode"


# 所有可配置动作（PRD §L.1）
ACTION_KEYS: tuple[str, ...] = (
    "send_message_private",
    "send_message_group",
    "same_peer_send",
    "edit_message",
    "delete_message",
    "forward_message",
    "callback_query",
    "read_history",
    "join_chat",
    "leave_chat",
    "create_chat",
    "invite_user",
    "dm_stranger",
    "update_profile",
    "upload_file",
    "download_file",
    "search",
    "webhook_deliver",
    "api_total",
)


class RateLimitTemplate(Base):
    """风控模板：可应用到多个账号作为默认。"""

    __tablename__ = "rate_limit_template"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateLimitRule(Base):
    """单条动作的限速配置，支持模板 / 账号 / 规则三种作用域。"""

    __tablename__ = "rate_limit_rule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)

    per_second: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    same_peer_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    policy: Mapped[str] = mapped_column(String, nullable=False, default=POLICY_QUEUE)
    backoff_base_seconds: Mapped[int] = mapped_column(Integer, default=5)
    backoff_max_seconds: Mapped[int] = mapped_column(Integer, default=1800)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("scope", "scope_id", "action", name="uq_rl_scope_action"),)


class RateLimitEvent(Base):
    """限速事件流（仪表盘 + 24h 事件流来源）。"""

    __tablename__ = "rate_limit_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    action: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_rl_event_account_ts", "account_id", "ts"),
        Index("ix_rl_event_account_action_ts", "account_id", "action", "ts"),
    )


class RateLimitOverride(Base):
    """临时阈值衰减（FloodWait 触发的 ×0.7 等），TTL 到期由后台清理。"""

    __tablename__ = "rate_limit_override"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    multiplier: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_rl_override_account_expires", "account_id", "expires_at"),)
