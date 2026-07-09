"""风控相关 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..db.models.rate_limit import (
    ACTION_KEYS,
    POLICY_BACKOFF,
    POLICY_DROP,
    POLICY_NOTIFY,
    POLICY_PAUSE,
    POLICY_QUEUE,
)

POLICIES = (POLICY_DROP, POLICY_QUEUE, POLICY_BACKOFF, POLICY_PAUSE, POLICY_NOTIFY)


class RateLimitRuleConfig(BaseModel):
    """单条动作的限速配置（前端表格一行）。"""
    action: str
    per_second: int | None = None
    per_minute: int | None = None
    per_hour: int | None = None
    per_day: int | None = None
    same_peer_per_minute: int | None = None
    policy: str = POLICY_QUEUE
    backoff_base_seconds: int = 5
    backoff_max_seconds: int = 1800
    enabled: bool = True

    model_config = ConfigDict(from_attributes=True)


class TemplateOut(BaseModel):
    id: int
    name: str
    is_default: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TemplateCreate(BaseModel):
    name: str
    is_default: bool = False


class AccountRateLimitOut(BaseModel):
    """账号级合并后的有效配置（含继承标记）。"""

    template_id: int | None
    rules: list[RateLimitRuleConfig]


class UsageBucket(BaseModel):
    action: str
    used: float
    limit: int | None
    pct: float
    warn: bool = False


class UsageResponse(BaseModel):
    window: str
    buckets: list[UsageBucket]
    active_overrides: list[dict[str, Any]] = Field(default_factory=list)


class StrictRequest(BaseModel):
    """一键调严：阈值 ×multiplier，TTL 秒。"""
    multiplier: float = 0.5
    ttl_seconds: int = 7200


class EstimateRequest(BaseModel):
    action: str
    target_count: int
    total_count: int


class EstimateResponse(BaseModel):
    eta_seconds: int
    exceeds_limit: bool


class HumanizeOut(BaseModel):
    jitter_pct: int
    typing_simulate: bool
    typing_min_ms: int
    typing_max_ms: int
    typing_probability: int
    read_before_reply: bool
    active_window_start: str | None = None
    active_window_end: str | None = None
    cold_start_days: int

    model_config = ConfigDict(from_attributes=True)


class HumanizeUpdate(BaseModel):
    jitter_pct: int | None = None
    typing_simulate: bool | None = None
    typing_min_ms: int | None = None
    typing_max_ms: int | None = None
    typing_probability: int | None = None
    read_before_reply: bool | None = None
    active_window_start: str | None = None
    active_window_end: str | None = None
    cold_start_days: int | None = None


class KillSwitchRequest(BaseModel):
    enabled: bool


__all__ = [
    "ACTION_KEYS",
    "AccountRateLimitOut",
    "EstimateRequest",
    "EstimateResponse",
    "HumanizeOut",
    "HumanizeUpdate",
    "KillSwitchRequest",
    "POLICIES",
    "RateLimitRuleConfig",
    "StrictRequest",
    "TemplateCreate",
    "TemplateOut",
    "UsageBucket",
    "UsageResponse",
]
