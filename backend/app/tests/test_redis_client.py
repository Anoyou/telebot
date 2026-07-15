from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as redis_async

from app import redis_client
from app.settings import settings


def test_worker_redis_pool_has_fifteen_connection_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEBOT_WORKER_PROC", "1")
    monkeypatch.setattr(settings, "redis_max_connections_worker", 4)

    assert redis_client._max_connections() == 15


@pytest.mark.asyncio
async def test_redis_pool_waits_for_released_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_client, "_pool", None)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    monkeypatch.setattr(settings, "redis_max_connections", 4)
    monkeypatch.delenv("TELEBOT_WORKER_PROC", raising=False)

    pool = redis_client.get_pool()
    assert isinstance(pool, redis_async.BlockingConnectionPool)
    assert pool.max_connections == 4
    assert pool.timeout == redis_client._POOL_WAIT_TIMEOUT_SECONDS
    monkeypatch.setattr(pool, "ensure_connection", AsyncMock())

    connections = [pool.get_available_connection() for _ in range(pool.max_connections)]
    waiter = asyncio.create_task(pool.get_connection())
    await asyncio.sleep(0)
    assert not waiter.done()

    await pool.release(connections.pop())
    acquired = await asyncio.wait_for(waiter, timeout=1)
    await pool.release(acquired)
    for connection in connections:
        await pool.release(connection)
    await redis_client.close_redis()
