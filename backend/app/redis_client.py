"""Redis 异步客户端封装。"""

from __future__ import annotations

import os

import redis.asyncio as redis_async

from .settings import settings

# 全局共享实例（按 use case 复用连接池）
_pool: redis_async.ConnectionPool | None = None
_POOL_WAIT_TIMEOUT_SECONDS = 5.0


def _max_connections() -> int:
    """主进程 vs worker 子进程使用不同上限。

    Worker 常驻命令与全局 Pub/Sub 会固定占用 2 条连接，RPC、周期任务和
    消息插件还会并发访问 Redis。保留 15 条连接可避免正常突发流量耗尽池子。
    """
    if os.environ.get("TELEBOT_WORKER_PROC") == "1":
        return max(15, int(settings.redis_max_connections_worker or 15))
    return max(4, int(settings.redis_max_connections or 16))


def get_pool() -> redis_async.ConnectionPool:
    global _pool
    if _pool is None:
        # 普通 ConnectionPool 在短时并发达到上限时会立即抛 MaxConnectionsError。
        # 阻塞池允许请求短暂等待空闲连接，同时用超时避免 Redis 故障时无限挂起。
        _pool = redis_async.BlockingConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=_max_connections(),
            timeout=_POOL_WAIT_TIMEOUT_SECONDS,
        )
    return _pool


def get_redis() -> redis_async.Redis:
    """每次返回一个 Redis 客户端（共享 pool）。"""
    return redis_async.Redis(connection_pool=get_pool())


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
