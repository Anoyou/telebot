"""临时阈值衰减（FloodWait/PeerFlood 等触发后短期收紧）。

写：DB ``rate_limit_override`` 表 + Redis 缓存（带 TTL，便于 engine 高频读取）。
读：engine 在 ``acquire`` 流程里调 ``get_multiplier`` 拿到当前 multiplier，按比例
折算各窗口阈值后再做 token bucket 检查；multiplier=0 等同临时禁用该动作。

清理：``cleanup_expired`` 由主进程每分钟调用一次，删 DB 里 expires_at 已过期的行；
Redis 端依赖 EXPIRE 自动到期，无需显式清理。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.base import AsyncSessionLocal
from ...db.models.rate_limit import RateLimitOverride


def _redis_key(account_id: int, action: str) -> str:
    """Redis 缓存 key，与 DB 表行一一对应。"""
    return f"rlovr:{account_id}:{action}"


async def add_override(
    db: AsyncSession,
    redis,
    account_id: int,
    action: str,
    multiplier: float,
    ttl_seconds: int,
    reason: str = "",
) -> RateLimitOverride:
    """新增（或覆盖）一条临时阈值衰减。

    - 同一 (account_id, action) 已有未过期 override 时直接覆盖（保留最严的策略：
      取较小的 multiplier 与较大的 ttl，避免 FloodWait 多次重叠时被悄悄放宽）。
    - DB 与 Redis **同步写入**：DB 失败时整个操作回滚，Redis 不会留下脏数据。
    """
    now = datetime.now(UTC)
    new_expires = now + timedelta(seconds=ttl_seconds)

    # 取已有未过期 override（如果有）做"取严"合并
    existing = (
        await db.execute(
            select(RateLimitOverride).where(
                RateLimitOverride.account_id == account_id,
                RateLimitOverride.action == action,
                RateLimitOverride.expires_at > now,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # 取较小 multiplier（更严格） + 较大 expires_at（更长 TTL）
        existing.multiplier = min(float(existing.multiplier), float(multiplier))
        if new_expires > existing.expires_at:
            existing.expires_at = new_expires
        if reason:
            existing.reason = reason
        record = existing
    else:
        record = RateLimitOverride(
            account_id=account_id,
            action=action,
            multiplier=float(multiplier),
            expires_at=new_expires,
            reason=reason or None,
        )
        db.add(record)

    await db.commit()

    # 同步写 Redis 缓存。multiplier 用 str 存，读侧 float() 转回。
    effective_ttl = max(1, int((record.expires_at - now).total_seconds()))
    await redis.set(_redis_key(account_id, action), str(float(record.multiplier)), ex=effective_ttl)
    return record


async def get_multiplier(redis, account_id: int, action: str) -> float:
    """Read the multiplier, falling back to the durable DB source on cache miss."""

    key = _redis_key(account_id, action)
    try:
        val = await redis.get(key)
    except Exception:  # noqa: BLE001
        val = None
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass

    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        record = await db.scalar(
            select(RateLimitOverride)
            .where(
                RateLimitOverride.account_id == int(account_id),
                RateLimitOverride.action == str(action),
                RateLimitOverride.expires_at > now,
            )
            .order_by(RateLimitOverride.multiplier.asc(), RateLimitOverride.expires_at.desc())
            .limit(1)
        )
    multiplier = float(record.multiplier) if record is not None else 1.0
    ttl = 30
    if record is not None:
        ttl = max(1, int((record.expires_at - now).total_seconds()))
    try:
        await redis.set(key, str(multiplier), ex=ttl)
    except Exception:  # noqa: BLE001
        pass
    return multiplier


async def list_active(db: AsyncSession, account_id: int) -> list[RateLimitOverride]:
    """查询某账号所有未过期 override，给 API ``GET .../overrides`` 用。"""
    now = datetime.now(UTC)
    res = await db.execute(
        select(RateLimitOverride)
        .where(
            RateLimitOverride.account_id == account_id,
            RateLimitOverride.expires_at > now,
        )
        .order_by(RateLimitOverride.expires_at.desc())
    )
    return list(res.scalars().all())


async def cleanup_expired(db: AsyncSession) -> int:
    """删除所有已过期 override，返回删除条数。

    Redis 端的 key 自带 TTL 不需要清理；DB 端只是定期回收，避免 ``rate_limit_override``
    表无限增长。建议主进程每 60s 调一次。
    """
    now = datetime.now(UTC)
    res = await db.execute(delete(RateLimitOverride).where(RateLimitOverride.expires_at < now))
    await db.commit()
    return int(res.rowcount or 0)


async def drop_override(db: AsyncSession, redis, account_id: int, action: str) -> int:
    """显式撤销某 action 的临时衰减（手动放开 FloodWait 等）。"""
    res = await db.execute(
        delete(RateLimitOverride).where(
            RateLimitOverride.account_id == account_id,
            RateLimitOverride.action == action,
        )
    )
    await db.commit()
    await redis.delete(_redis_key(account_id, action))
    return int(res.rowcount or 0)
