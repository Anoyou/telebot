"""Session chat reverse-index tests."""

from __future__ import annotations

import pytest

from app.services.interaction import session_index


class _FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    async def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(str(key), set())
        before = len(bucket)
        bucket.update(str(m) for m in members)
        return len(bucket) - before

    async def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(str(key), set())
        removed = 0
        for m in members:
            if str(m) in bucket:
                bucket.discard(str(m))
                removed += 1
        return removed

    async def smembers(self, key: str):
        return set(self.sets.get(str(key), set()))

    async def exists(self, key: str) -> int:
        return 1 if str(key) in self.sets else 0

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[str(key)] = int(seconds)
        return True

    async def delete(self, key: str) -> int:
        existed = 1 if str(key) in self.sets else 0
        self.sets.pop(str(key), None)
        self.ttls.pop(str(key), None)
        return existed


@pytest.mark.asyncio
async def test_index_and_list_session_keys() -> None:
    redis = _FakeRedis()
    await session_index.index_session_key(
        redis,
        account_id=7,
        chat_id=-1001,
        session_key="account_bot:interaction_session:7:rule: -1001",
        ttl_seconds=120,
    )
    keys = await session_index.list_indexed_session_keys(redis, account_id=7, chat_id=-1001)
    assert keys is not None
    assert keys == ["account_bot:interaction_session:7:rule: -1001"]
    assert redis.ttls[session_index.chat_index_key(7, -1001)] == 120


@pytest.mark.asyncio
async def test_missing_index_returns_none_for_rebuild() -> None:
    redis = _FakeRedis()
    keys = await session_index.list_indexed_session_keys(redis, account_id=1, chat_id=2)
    assert keys is None


@pytest.mark.asyncio
async def test_rebuild_replaces_membership() -> None:
    redis = _FakeRedis()
    await session_index.index_session_key(
        redis, account_id=1, chat_id=9, session_key="old"
    )
    await session_index.rebuild_chat_index_from_scan(
        redis, account_id=1, chat_id=9, scan_keys=["a", "b"]
    )
    keys = await session_index.list_indexed_session_keys(redis, account_id=1, chat_id=9)
    assert set(keys or []) == {"a", "b"}
