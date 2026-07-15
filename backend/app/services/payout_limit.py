"""账号级 payout 限额，基于 Redis 做日累计。

payout 实质是 userbot 给群内第三方记账 bot 发 "+{amount}" 文本。这里在两个最终
发送点之前加一道风控：

- 单笔上限（single_max）：纯比较，超限直接拒，不触碰 Redis、不消费日累计。
- 日累计上限（daily_max）：Redis 原子 check-and-consume（未超才 INCRBY + 当日过期
  key），风格照抄 ``llm_account_budget`` 的日累计写法。

资金发送采用 fail-closed：配置或 Redis 不可用时拒绝本笔 payout，并返回可辨识的
基础设施错误说明。这样不会在风控依赖失效时绕过用户已经配置的资金保护。

注意：check-and-consume 在发送前消费，若随后发送失败，该笔仍已计入日累计（偏保守，
不做释放补偿）。发送失败属小概率，且与 fail-closed 的保守方向一致。日累计 key 以 UTC
日期分桶，与 ``llm_account_budget`` 的日限保持同一天界。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..db.base import AsyncSessionLocal
from ..db.models.system import SystemSetting
from ..redis_client import get_redis

log = logging.getLogger(__name__)

SETTING_KEY = "payout_limits"
_DAILY_TTL_SECONDS = 172_800  # 48h：key 已按日期分桶，多留一天冗余便于跨零点
_COUNTED_KEY_PREFIX = "payout_limit:counted:"
# use_counted=0 时脚本不碰 KEYS[2]，仅为占位保证 numkeys 恒为 2。
_COUNTED_KEY_UNUSED = f"{_COUNTED_KEY_PREFIX}__unused__"

# 日累计 check-and-consume：先判断"已用 + 本笔"是否超限，未超才 INCRBY，保证原子，
# 避免并发下先各自读到未超再各自累加导致击穿上限。
#
# 幂等（KEYS[2] = payout_limit:counted:{idempotency_key}，use_counted=1 时启用）：
# 该键已存在说明这笔 payout 之前已计入过日累计 → 直接放行、不再 INCRBY；否则正常
# check-and-consume，成功后原子 SET 该键（EX 48h）标记已计。用于将来补偿重放同一笔
# payout 时日累计只计一次。use_counted=0（idempotency_key=None）时完全不碰 KEYS[2]，
# 行为与无幂等键时一致。
_CONSUME_SCRIPT = """
local daily_max = tonumber(ARGV[1]) or 0
local amount = tonumber(ARGV[2]) or 0
local ttl = tonumber(ARGV[3]) or 172800
local use_counted = tonumber(ARGV[4]) or 0
if use_counted == 1 and redis.call('EXISTS', KEYS[2]) == 1 then
  local counted_used = tonumber(redis.call('GET', KEYS[1]) or '0')
  return {1, counted_used, daily_max}
end
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if daily_max > 0 and (used + amount) > daily_max then
  return {0, used, daily_max}
end
local total = redis.call('INCRBY', KEYS[1], amount)
redis.call('EXPIRE', KEYS[1], ttl)
if use_counted == 1 then
  redis.call('SET', KEYS[2], '1', 'EX', ttl)
end
return {1, total, daily_max}
"""


class PayoutLimitExceeded(RuntimeError):
    """payout 超限。调用方可选择据此走各自的失败路径。"""


PAYOUT_LIMIT_CONFIG_UNAVAILABLE = "payout 风控配置不可用，请检查数据库连接后重试。"
PAYOUT_LIMIT_COUNTER_UNAVAILABLE = "payout 日累计风控不可用，请检查 Redis 连接后重试。"


async def check_and_consume(
    account_id: int | None,
    amount: int,
    idempotency_key: str | None = None,
) -> tuple[bool, str | None]:
    """校验并消费一次 payout 额度。

    返回 ``(allowed, reason)``：allowed=False 时 reason 为中文超限说明（含当前值与
    限额值）。single_max / daily_max 为 0 表示不限。金额校验由上游承担，这里对非正
    金额 / 缺账号直接放行（不承担越权校验，也不消费额度）。
    """

    try:
        amount_int = int(amount)
    except (TypeError, ValueError):
        amount_int = 0
    if amount_int <= 0 or account_id is None:
        return True, None

    try:
        limits = await _load_payout_limits()
    except Exception:  # noqa: BLE001
        log.error("payout 限额读取失败，fail-closed 拒绝 account=%s", account_id, exc_info=True)
        return False, PAYOUT_LIMIT_CONFIG_UNAVAILABLE

    single_max = int(limits["single_max"])
    daily_max = int(limits["daily_max"])

    if single_max > 0 and amount_int > single_max:
        return False, f"payout 单笔上限超限：本笔 {amount_int}，单笔上限 {single_max}。"

    if daily_max <= 0:
        return True, None

    try:
        allowed, used, limit = await _try_consume_daily(
            account_id,
            amount_int,
            daily_max,
            idempotency_key=idempotency_key,
        )
    except Exception:  # noqa: BLE001
        log.error("payout 日累计 Redis 校验失败，fail-closed 拒绝 account=%s", account_id, exc_info=True)
        return False, PAYOUT_LIMIT_COUNTER_UNAVAILABLE
    if allowed:
        return True, None
    return False, f"payout 日累计上限超限：今日已用 {used}，本笔 {amount_int}，日累计上限 {limit}。"


async def _try_consume_daily(
    account_id: int,
    amount: int,
    daily_max: int,
    *,
    idempotency_key: str | None = None,
) -> tuple[bool, int, int]:
    redis = get_redis()
    await redis.ping()
    counted_key = _counted_key(account_id, idempotency_key)
    use_counted = 1 if counted_key != _COUNTED_KEY_UNUSED else 0
    result = await redis.eval(
        _CONSUME_SCRIPT,
        2,
        _daily_key(account_id),
        counted_key,
        daily_max,
        amount,
        _DAILY_TTL_SECONDS,
        use_counted,
    )
    ok = int(result[0] if result else 0)
    used = int(result[1] if result and len(result) > 1 else 0)
    limit = int(result[2] if result and len(result) > 2 else daily_max)
    return ok == 1, used, limit


async def _load_payout_limits() -> dict[str, int]:
    """读 DB 里的 ``payout_limits`` 覆盖值；缺省不限（0）。"""

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, SETTING_KEY)
    value = row.value if row is not None else None
    limits = {"single_max": 0, "daily_max": 0}
    if isinstance(value, dict):
        limits["single_max"] = _non_negative_int(value.get("single_max"), 0)
        limits["daily_max"] = _non_negative_int(value.get("daily_max"), 0)
    return limits


def _daily_key(account_id: int) -> str:
    today = datetime.now(UTC).strftime("%Y%m%d")
    return f"payout_limit:{int(account_id)}:daily:{today}"


def _counted_key(account_id: int, idempotency_key: str | None) -> str:
    key = str(idempotency_key or "").strip()
    if not key:
        return _COUNTED_KEY_UNUSED
    return f"{_COUNTED_KEY_PREFIX}{int(account_id)}:{key[:160]}"


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "PAYOUT_LIMIT_CONFIG_UNAVAILABLE",
    "PAYOUT_LIMIT_COUNTER_UNAVAILABLE",
    "PayoutLimitExceeded",
    "check_and_consume",
]
