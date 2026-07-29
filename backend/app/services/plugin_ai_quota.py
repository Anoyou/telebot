"""Plugin AI token quota pre-reservation.

MVP 设计：
- 限额配置读取 ``system_setting.plugin_ai_quota``。
- Redis 可用时，用 Lua 原子预扣 estimated tokens，并在调用结束后按实际用量修正。
- Redis 不可用时，降级为基于 ``llm_usage`` 的 DB 汇总检查；无法做到并发预扣，但不阻断业务。
- 跨日边界按 acquire 时的自然日记账：例如 23:59 acquire、00:00 release 时，daily 计数回滚到 acquire 当天，
  今天的预算不会被这次释放影响。这里是软上限语义，目标是用 quota 防爆，不要求精确到秒。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from ..db.base import AsyncSessionLocal
from ..db.models.llm_usage import LLMUsage
from ..db.models.system import SystemSetting
from ..redis_client import get_redis

log = logging.getLogger(__name__)

SETTING_KEY = "plugin_ai_quota"
_DEFAULT_LIMITS = {"per_minute": 0, "daily": 0}

_ACQUIRE_SCRIPT = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[6] - ARGV[7])
for _, id in ipairs(expired) do
  redis.call('HDEL', KEYS[2], id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[6] - ARGV[7])

local minute_used = 0
local ids = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, id in ipairs(ids) do
  minute_used = minute_used + tonumber(redis.call('HGET', KEYS[2], id) or '0')
end

local daily_used = tonumber(redis.call('GET', KEYS[3]) or '0')
local estimate = tonumber(ARGV[1]) or 0
local per_minute = tonumber(ARGV[2]) or 0
local daily = tonumber(ARGV[3]) or 0
local minute_ttl = tonumber(ARGV[4]) or 120
local daily_ttl = tonumber(ARGV[5]) or 172800
local now_ms = tonumber(ARGV[6]) or 0
local reservation_id = ARGV[8]

if per_minute > 0 and (minute_used + estimate) > per_minute then
  return {0, 'per_minute', minute_used, per_minute}
end
if daily > 0 and (daily_used + estimate) > daily then
  return {0, 'daily', daily_used, daily}
end

redis.call('ZADD', KEYS[1], now_ms, reservation_id)
redis.call('HSET', KEYS[2], reservation_id, estimate)
redis.call('EXPIRE', KEYS[1], minute_ttl)
redis.call('EXPIRE', KEYS[2], minute_ttl)
redis.call('INCRBY', KEYS[3], estimate)
redis.call('EXPIRE', KEYS[3], daily_ttl)
return {1, 'ok', minute_used + estimate, daily_used + estimate}
"""

_RELEASE_SCRIPT = """
local delta = tonumber(ARGV[1]) or 0
local reservation_id = ARGV[2]

local current = redis.call('HGET', KEYS[2], reservation_id)
if current then
  local next_amount = tonumber(current or '0') + delta
  if next_amount <= 0 then
    redis.call('HDEL', KEYS[2], reservation_id)
    redis.call('ZREM', KEYS[1], reservation_id)
  else
    redis.call('HSET', KEYS[2], reservation_id, next_amount)
  end
end

if redis.call('EXISTS', KEYS[3]) == 1 then
  local next_daily = tonumber(redis.call('INCRBY', KEYS[3], delta) or '0')
  if next_daily < 0 then
    redis.call('SET', KEYS[3], 0)
  end
end
return {1}
"""


class PluginAIQuotaExceeded(RuntimeError):
    """Raised when plugin AI token quota blocks a call."""


@dataclass(frozen=True)
class PluginAIQuotaTicket:
    """Reservation handle returned by ``acquire`` and consumed by ``release``."""

    plugin_key: str
    account_id: int | None
    estimated_tokens: int
    minute_key: str | None = None
    minute_amount_key: str | None = None
    daily_key: str | None = None
    reservation_id: str | None = None
    backend: str = "disabled"
    limited: bool = False


async def acquire(
    plugin_key: str,
    account_id: int | None,
    estimated_tokens: int,
) -> PluginAIQuotaTicket:
    """Reserve estimated tokens for a plugin AI call.

    A returned ticket always means the caller may continue. If quota is exceeded,
    ``PluginAIQuotaExceeded`` is raised and an error usage row is written so the
    Usage page can explain why no provider call happened.
    """

    plugin = _normalize_plugin_key(plugin_key)
    estimate = _positive_int(estimated_tokens, 1)
    limits = await _load_quota_limits(plugin)
    per_minute = int(limits["per_minute"])
    daily = int(limits["daily"])
    limited = per_minute > 0 or daily > 0
    if not limited:
        return PluginAIQuotaTicket(plugin, account_id, estimate, limited=False)

    minute_key, minute_amount_key, daily_key = _quota_keys(plugin, account_id)
    reservation_id = uuid.uuid4().hex
    try:
        await _try_redis_acquire(
            minute_key,
            minute_amount_key,
            daily_key,
            estimate,
            per_minute,
            daily,
            reservation_id,
        )
        return PluginAIQuotaTicket(
            plugin,
            account_id,
            estimate,
            minute_key=minute_key,
            minute_amount_key=minute_amount_key,
            daily_key=daily_key,
            reservation_id=reservation_id,
            backend="redis",
            limited=True,
        )
    except PluginAIQuotaExceeded:
        await _write_quota_error_usage(plugin, account_id)
        raise
    except Exception:  # noqa: BLE001
        log.warning(
            "plugin AI quota Redis unavailable; falling back to DB usage check plugin=%s account=%s",
            plugin,
            account_id,
            exc_info=True,
        )

    try:
        await _check_db_usage(plugin, account_id, estimate, per_minute, daily)
    except PluginAIQuotaExceeded:
        await _write_quota_error_usage(plugin, account_id)
        raise
    except Exception:  # noqa: BLE001
        log.error(
            "plugin AI quota DB fallback failed; allowing call plugin=%s account=%s",
            plugin,
            account_id,
            exc_info=True,
        )
    return PluginAIQuotaTicket(plugin, account_id, estimate, backend="db-degraded", limited=True)


async def release(ticket: PluginAIQuotaTicket | None, actual_tokens: int) -> None:
    """Settle a previous reservation.

    Redis counters hold reserved+actual usage. Releasing applies
    ``actual_tokens - estimated_tokens`` so failures with 0 tokens fully roll
    back the reservation, while successful calls keep only real usage.
    """

    if (
        ticket is None
        or ticket.backend != "redis"
        or not ticket.minute_key
        or not ticket.minute_amount_key
        or not ticket.daily_key
        or not ticket.reservation_id
    ):
        return
    actual = max(0, int(actual_tokens or 0))
    delta = actual - int(ticket.estimated_tokens or 0)
    if delta == 0:
        return
    try:
        redis = get_redis()
        await redis.eval(
            _RELEASE_SCRIPT,
            3,
            ticket.minute_key,
            ticket.minute_amount_key,
            ticket.daily_key,
            delta,
            ticket.reservation_id,
        )
    except Exception:  # noqa: BLE001
        log.error(
            "plugin AI quota release failed plugin=%s account=%s actual=%s estimate=%s",
            ticket.plugin_key,
            ticket.account_id,
            actual,
            ticket.estimated_tokens,
            exc_info=True,
        )


async def _try_redis_acquire(
    minute_key: str,
    minute_amount_key: str,
    daily_key: str,
    estimate: int,
    per_minute: int,
    daily: int,
    reservation_id: str,
) -> None:
    redis = get_redis()
    await redis.ping()
    result = await redis.eval(
        _ACQUIRE_SCRIPT,
        3,
        minute_key,
        minute_amount_key,
        daily_key,
        estimate,
        per_minute,
        daily,
        120,
        172800,
        int(time.time() * 1000),
        60_000,
        reservation_id,
    )
    ok = int(result[0] if result else 0)
    if ok == 1:
        return
    scope = str(result[1] if len(result) > 1 else "quota")
    used = int(result[2] if len(result) > 2 else 0)
    limit = int(result[3] if len(result) > 3 else 0)
    raise PluginAIQuotaExceeded(_quota_message(scope, used, estimate, limit))


async def _check_db_usage(
    plugin_key: str,
    account_id: int | None,
    estimate: int,
    per_minute: int,
    daily: int,
) -> None:
    now = datetime.now(UTC)
    minute_start = now - timedelta(minutes=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    source = f"plugin:{plugin_key}"
    async with AsyncSessionLocal() as db:
        # 失败/取消流也可能已经产生上游费用；只要 usage 记录了保守 token，
        # DB 降级检查就必须计入，不能让主动 aclose 绕过 quota。
        # Agent calls use ``plugin:{key}:agent`` while text calls use the base
        # source.  The DB fallback must count both, otherwise a Redis outage
        # lets agent usage bypass the plugin quota.
        filters = [
            (LLMUsage.source == source) | LLMUsage.source.startswith(f"{source}:")
        ]
        if account_id is None:
            filters.append(LLMUsage.account_id.is_(None))
        else:
            filters.append(LLMUsage.account_id == account_id)
        token_expr = func.coalesce(func.sum(LLMUsage.input_tokens + LLMUsage.output_tokens), 0)

        if per_minute > 0:
            minute_used = await db.scalar(
                select(token_expr).where(*filters, LLMUsage.created_at >= minute_start)
            )
            if int(minute_used or 0) + estimate > per_minute:
                raise PluginAIQuotaExceeded(
                    _quota_message("per_minute", int(minute_used or 0), estimate, per_minute)
                )

        if daily > 0:
            daily_used = await db.scalar(select(token_expr).where(*filters, LLMUsage.created_at >= day_start))
            if int(daily_used or 0) + estimate > daily:
                raise PluginAIQuotaExceeded(_quota_message("daily", int(daily_used or 0), estimate, daily))


async def _load_quota_limits(plugin_key: str) -> dict[str, int]:
    """Load global/per-plugin token limits from ``system_setting.plugin_ai_quota``."""

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, SETTING_KEY)
    raw = row.value if row is not None else None
    if not isinstance(raw, dict):
        return dict(_DEFAULT_LIMITS)

    merged: dict[str, Any] = dict(raw)
    plugins = raw.get("plugins")
    if isinstance(plugins, dict):
        override = plugins.get(plugin_key)
        if isinstance(override, dict):
            merged.update(override)
    return {
        "per_minute": _non_negative_int(
            merged.get("per_minute_tokens", merged.get("per_minute")),
            _DEFAULT_LIMITS["per_minute"],
        ),
        "daily": _non_negative_int(
            merged.get("daily_tokens", merged.get("daily")),
            _DEFAULT_LIMITS["daily"],
        ),
    }


async def _write_quota_error_usage(plugin_key: str, account_id: int | None) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                LLMUsage(
                    account_id=account_id,
                    provider_id=None,
                    provider_name=None,
                    model=None,
                    source=f"plugin:{plugin_key}",
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    success=False,
                    error_type="plugin_quota_exceeded",
                    used_fallback=False,
                    fallback_chain="[]",
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("plugin AI quota usage error write failed", exc_info=True)


def _quota_keys(plugin_key: str, account_id: int | None) -> tuple[str, str, str]:
    now = datetime.now(UTC)
    account = "none" if account_id is None else str(int(account_id))
    base = f"plugin_ai_quota:{account}:{plugin_key}"
    return (
        f"{base}:m",
        f"{base}:ma",
        f"{base}:d:{now:%Y%m%d}",
    )


def _quota_message(scope: str, used: int, estimate: int, limit: int) -> str:
    if scope == "per_minute":
        return f"插件 AI 每分钟 token 配额不足（已用/预扣 {used}+{estimate}，上限 {limit}）。"
    if scope == "daily":
        return f"插件 AI 今日 token 配额不足（已用/预扣 {used}+{estimate}，上限 {limit}）。"
    return "插件 AI token 配额不足。"


def _normalize_plugin_key(plugin_key: str) -> str:
    key = str(plugin_key or "unknown").strip()
    return key[:48] or "unknown"


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
        return int(default)


__all__ = [
    "PluginAIQuotaExceeded",
    "PluginAIQuotaTicket",
    "acquire",
    "release",
]
