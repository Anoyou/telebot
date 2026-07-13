"""Account-level LLM budget reservations backed by Redis.

The runtime acquires one reservation immediately before each actual provider
candidate. Failed candidates release their reservation before fallback, so the
successful call is charged once while every premium candidate is atomically
gated before it can reach the network.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..db.base import AsyncSessionLocal
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..settings import settings

log = logging.getLogger(__name__)

SETTING_KEY = "llm_limits"
_MINUTE_WINDOW_MS = 60_000
_MINUTE_TTL_SECONDS = 120
_DAILY_TTL_SECONDS = 172_800

_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, tonumber(ARGV[7]) - tonumber(ARGV[8]))

local per_minute = tonumber(ARGV[1]) or 0
local daily_requests = tonumber(ARGV[2]) or 0
local daily_tokens = tonumber(ARGV[3]) or 0
local premium_daily = tonumber(ARGV[4]) or 0
local estimate = tonumber(ARGV[5]) or 0
local reserve_premium = tonumber(ARGV[6]) or 0
local now_ms = tonumber(ARGV[7]) or 0
local minute_ttl = tonumber(ARGV[9]) or 120
local daily_ttl = tonumber(ARGV[10]) or 172800
local reservation_id = ARGV[11]

local minute_used = tonumber(redis.call('ZCARD', KEYS[1]) or '0')
local request_used = tonumber(redis.call('GET', KEYS[2]) or '0')
local token_used = tonumber(redis.call('GET', KEYS[3]) or '0')
local premium_used = tonumber(redis.call('GET', KEYS[4]) or '0')

if per_minute > 0 and (minute_used + 1) > per_minute then
  return {0, 'per_minute', minute_used, per_minute}
end
if daily_requests > 0 and (request_used + 1) > daily_requests then
  return {0, 'daily_requests', request_used, daily_requests}
end
if daily_tokens > 0 and (token_used + estimate) > daily_tokens then
  return {0, 'daily_tokens', token_used, daily_tokens}
end
if premium_daily > 0 and (premium_used + reserve_premium) > premium_daily then
  return {0, 'premium_daily', premium_used, premium_daily}
end

if per_minute > 0 then
  redis.call('ZADD', KEYS[1], now_ms, reservation_id)
  redis.call('EXPIRE', KEYS[1], minute_ttl)
end

redis.call(
  'HSET',
  KEYS[5],
  'minute_active', per_minute > 0 and 1 or 0,
  'daily_request_units', daily_requests > 0 and 1 or 0,
  'token_limited', daily_tokens > 0 and 1 or 0,
  'token_units', daily_tokens > 0 and estimate or 0,
  'premium_limited', premium_daily > 0 and 1 or 0,
  'premium_units', premium_daily > 0 and reserve_premium or 0
)
redis.call('EXPIRE', KEYS[5], daily_ttl)

if daily_requests > 0 then
  redis.call('INCRBY', KEYS[2], 1)
  redis.call('EXPIRE', KEYS[2], daily_ttl)
end
if daily_tokens > 0 then
  redis.call('INCRBY', KEYS[3], estimate)
  redis.call('EXPIRE', KEYS[3], daily_ttl)
end
if premium_daily > 0 and reserve_premium > 0 then
  redis.call('INCRBY', KEYS[4], reserve_premium)
  redis.call('EXPIRE', KEYS[4], daily_ttl)
end

return {1, 'ok', minute_used + 1, token_used + estimate}
"""

_SETTLE_SCRIPT = """
local actual_tokens = tonumber(ARGV[1]) or 0
local actual_premium = tonumber(ARGV[2]) or 0
local keep_request = tonumber(ARGV[3]) or 0
local reservation_id = ARGV[4]
local daily_ttl = tonumber(ARGV[5]) or 172800

if redis.call('EXISTS', KEYS[5]) == 0 then
  return {0, 'missing'}
end

local minute_active = tonumber(redis.call('HGET', KEYS[5], 'minute_active') or '0')
local request_units = tonumber(redis.call('HGET', KEYS[5], 'daily_request_units') or '0')
local token_limited = tonumber(redis.call('HGET', KEYS[5], 'token_limited') or '0')
local token_units = tonumber(redis.call('HGET', KEYS[5], 'token_units') or '0')
local premium_limited = tonumber(redis.call('HGET', KEYS[5], 'premium_limited') or '0')
local premium_units = tonumber(redis.call('HGET', KEYS[5], 'premium_units') or '0')

if keep_request <= 0 then
  if minute_active > 0 then
    redis.call('ZREM', KEYS[1], reservation_id)
  end
  if request_units > 0 then
    local next_requests = tonumber(redis.call('INCRBY', KEYS[2], -request_units) or '0')
    if next_requests < 0 then
      redis.call('SET', KEYS[2], 0)
    end
    redis.call('EXPIRE', KEYS[2], daily_ttl)
  end
end

if token_limited > 0 then
  local next_tokens = tonumber(redis.call('INCRBY', KEYS[3], actual_tokens - token_units) or '0')
  if next_tokens < 0 then
    redis.call('SET', KEYS[3], 0)
  end
  redis.call('EXPIRE', KEYS[3], daily_ttl)
end

if premium_limited > 0 then
  if KEYS[6] == KEYS[4] then
    local next_premium = tonumber(redis.call('INCRBY', KEYS[4], actual_premium - premium_units) or '0')
    if next_premium < 0 then
      redis.call('SET', KEYS[4], 0)
    end
    redis.call('EXPIRE', KEYS[4], daily_ttl)
  else
    if premium_units > 0 then
      local next_reserved_premium = tonumber(redis.call('INCRBY', KEYS[4], -premium_units) or '0')
      if next_reserved_premium < 0 then
        redis.call('SET', KEYS[4], 0)
      end
      redis.call('EXPIRE', KEYS[4], daily_ttl)
    end
    if actual_premium > 0 then
      redis.call('INCRBY', KEYS[6], actual_premium)
      redis.call('EXPIRE', KEYS[6], daily_ttl)
    end
  end
end

redis.call('DEL', KEYS[5])
return {1, 'ok'}
"""


class LLMAccountBudgetExceeded(RuntimeError):
    """Raised when an account-level LLM budget reservation is rejected."""

    def __init__(self, message: str, *, scope: str = "budget") -> None:
        super().__init__(message)
        self.scope = scope


@dataclass(frozen=True)
class LLMAccountBudgetTicket:
    """Reservation handle used to settle a previous account-level budget check."""

    account_id: int | None
    provider_id: int | None
    estimated_tokens: int
    minute_key: str | None = None
    daily_requests_key: str | None = None
    daily_tokens_key: str | None = None
    daily_premium_key: str | None = None
    reservation_key: str | None = None
    reservation_id: str | None = None
    backend: str = "disabled"
    limited: bool = False


async def acquire(
    account_id: int | None,
    provider_dto: Any,
    estimated_tokens: int,
) -> LLMAccountBudgetTicket:
    """Reserve one LLM call for an account.

    Budget backend failures are fail-closed.  A configured cost boundary must
    never disappear merely because Redis or the settings table is unavailable.
    """

    estimate = _positive_int(estimated_tokens, 1)
    provider_id = int(getattr(provider_dto, "id", 0) or 0) or None
    if account_id is None:
        return LLMAccountBudgetTicket(None, provider_id, estimate)

    try:
        limits = await _load_budget_limits()
    except Exception:  # noqa: BLE001
        log.error("LLM budget limit load failed; rejecting account=%s", account_id, exc_info=True)
        raise LLMAccountBudgetExceeded(
            "LLM 预算服务暂不可用，已为避免失控费用拒绝本次调用。",
            scope="backend_unavailable",
        ) from None

    per_minute = int(limits["per_minute"])
    daily_requests = int(limits["daily_requests"])
    daily_tokens = int(limits["daily_tokens"])
    premium_daily = int(limits["premium_daily"])
    limited = per_minute > 0 or daily_requests > 0 or daily_tokens > 0 or premium_daily > 0
    if not limited:
        return LLMAccountBudgetTicket(account_id, provider_id, estimate)

    reservation_id = uuid.uuid4().hex
    keys = _budget_keys(account_id, provider_id, reservation_id)
    reserve_premium = 1 if int(getattr(provider_dto, "cost_tier", 2) or 2) >= 3 else 0
    try:
        await _try_redis_acquire(
            keys,
            estimate,
            reserve_premium,
            per_minute,
            daily_requests,
            daily_tokens,
            premium_daily,
            reservation_id,
        )
    except LLMAccountBudgetExceeded:
        raise
    except Exception:  # noqa: BLE001
        log.error("LLM budget Redis reservation failed; rejecting account=%s", account_id, exc_info=True)
        raise LLMAccountBudgetExceeded(
            "LLM 预算服务暂不可用，已为避免失控费用拒绝本次调用。",
            scope="backend_unavailable",
        ) from None

    return LLMAccountBudgetTicket(
        account_id,
        provider_id,
        estimate,
        minute_key=keys[0],
        daily_requests_key=keys[1],
        daily_tokens_key=keys[2],
        daily_premium_key=keys[3],
        reservation_key=keys[4],
        reservation_id=reservation_id,
        backend="redis",
        limited=True,
    )


async def settle(
    ticket: LLMAccountBudgetTicket | None,
    *,
    actual_tokens: int,
    actual_provider: Any | None,
    success: bool,
) -> None:
    """Settle or release a previous reservation."""

    if (
        ticket is None
        or ticket.backend != "redis"
        or not ticket.minute_key
        or not ticket.daily_requests_key
        or not ticket.daily_tokens_key
        or not ticket.daily_premium_key
        or not ticket.reservation_key
        or not ticket.reservation_id
    ):
        return

    if success:
        tokens = max(0, int(actual_tokens or 0))
        actual_premium = 1 if int(getattr(actual_provider, "cost_tier", 2) or 2) >= 3 else 0
        keep_request = 1
    else:
        tokens = 0
        actual_premium = 0
        keep_request = 0

    try:
        redis = get_redis()
        actual_premium_key = _settlement_premium_key(ticket, actual_provider, success=success)
        await redis.eval(
            _SETTLE_SCRIPT,
            6,
            ticket.minute_key,
            ticket.daily_requests_key,
            ticket.daily_tokens_key,
            ticket.daily_premium_key,
            ticket.reservation_key,
            actual_premium_key,
            tokens,
            actual_premium,
            keep_request,
            ticket.reservation_id,
            _DAILY_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        log.error(
            "LLM budget reservation settle failed account=%s provider=%s success=%s",
            ticket.account_id,
            ticket.provider_id,
            success,
            exc_info=True,
        )


async def _try_redis_acquire(
    keys: tuple[str, str, str, str, str],
    estimate: int,
    reserve_premium: int,
    per_minute: int,
    daily_requests: int,
    daily_tokens: int,
    premium_daily: int,
    reservation_id: str,
) -> None:
    redis = get_redis()
    await redis.ping()
    result = await redis.eval(
        _ACQUIRE_SCRIPT,
        5,
        *keys,
        per_minute,
        daily_requests,
        daily_tokens,
        premium_daily,
        estimate,
        reserve_premium,
        int(time.time() * 1000),
        _MINUTE_WINDOW_MS,
        _MINUTE_TTL_SECONDS,
        _DAILY_TTL_SECONDS,
        reservation_id,
    )
    ok = int(result[0] if result else 0)
    if ok == 1:
        return
    scope = str(result[1] if len(result) > 1 else "budget")
    limit = int(result[3] if len(result) > 3 else 0)
    raise LLMAccountBudgetExceeded(_budget_message(scope, limit), scope=scope)


async def _load_budget_limits() -> dict[str, int]:
    """Read DB overrides for LLM limits, falling back to environment settings."""

    limits = {
        "per_minute": int(getattr(settings, "llm_per_minute_request_limit_per_account", 0) or 0),
        "daily_requests": int(getattr(settings, "llm_daily_request_limit_per_account", 0) or 0),
        "daily_tokens": int(getattr(settings, "llm_daily_token_limit_per_account", 0) or 0),
        "premium_daily": int(getattr(settings, "llm_premium_daily_request_limit_per_account", 0) or 0),
    }
    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None else None
    if isinstance(value, dict):
        limits["per_minute"] = _non_negative_int(value.get("per_minute"), limits["per_minute"])
        limits["daily_requests"] = _non_negative_int(value.get("daily_requests"), limits["daily_requests"])
        limits["daily_tokens"] = _non_negative_int(value.get("daily_tokens"), limits["daily_tokens"])
        limits["premium_daily"] = _non_negative_int(value.get("premium_daily"), limits["premium_daily"])
    return limits


def _budget_keys(account_id: int, provider_id: int | None, reservation_id: str) -> tuple[str, str, str, str, str]:
    today = datetime.now(UTC).strftime("%Y%m%d")
    account = int(account_id)
    base = f"llm_budget:{account}"
    return (
        f"{base}:minute",
        f"{base}:daily_requests:{today}",
        f"{base}:daily_tokens:{today}",
        _daily_premium_key(account, provider_id, today=today),
        f"{base}:reservation:{reservation_id}",
    )


def _daily_premium_key(account_id: int, provider_id: int | None, *, today: str | None = None) -> str:
    day = today or datetime.now(UTC).strftime("%Y%m%d")
    provider = int(provider_id or 0)
    return f"llm_budget:{int(account_id)}:daily_premium:{provider}:{day}"


def _settlement_premium_key(
    ticket: LLMAccountBudgetTicket,
    actual_provider: Any | None,
    *,
    success: bool,
) -> str:
    if not success or ticket.account_id is None:
        return ticket.daily_premium_key or ""
    if int(getattr(actual_provider, "cost_tier", 2) or 2) < 3:
        return ticket.daily_premium_key or ""
    actual_provider_id = int(getattr(actual_provider, "id", 0) or 0) or None
    return _daily_premium_key(ticket.account_id, actual_provider_id)


def _budget_message(scope: str, limit: int) -> str:
    if scope == "per_minute":
        return f"LLM 每分钟调用次数已达上限（{limit}/min），请稍后再试。"
    if scope == "daily_requests":
        return f"LLM 今日调用次数已达上限（{limit}/day）。"
    if scope == "daily_tokens":
        return f"LLM 今日 token 用量已达上限（{limit}/day）。"
    if scope == "premium_daily":
        return f"高价 LLM 今日调用次数已达上限（{limit}/day）。"
    return "LLM 账号预算已达上限。"


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "LLMAccountBudgetExceeded",
    "LLMAccountBudgetTicket",
    "acquire",
    "settle",
]
