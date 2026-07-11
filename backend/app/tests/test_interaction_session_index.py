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
    # 索引 TTL 取 max(会话 TTL, 默认 7 天)，避免短会话 TTL 把索引冲掉。
    assert redis.ttls[session_index.chat_index_key(7, -1001)] == session_index.CHAT_INDEX_TTL_SECONDS


@pytest.mark.asyncio
async def test_missing_index_returns_none_for_rebuild() -> None:
    redis = _FakeRedis()
    keys = await session_index.list_indexed_session_keys(redis, account_id=1, chat_id=2)
    assert keys is None


@pytest.mark.asyncio
async def test_rebuild_merges_scan_without_deleting_concurrent_adds() -> None:
    redis = _FakeRedis()
    await session_index.index_session_key(
        redis, account_id=1, chat_id=9, session_key="old-session"
    )
    # 模拟：SCAN 只看到 old；并发新会话已写入索引
    await session_index.index_session_key(
        redis, account_id=1, chat_id=9, session_key="new-session"
    )
    merged = await session_index.rebuild_chat_index_from_scan(
        redis, account_id=1, chat_id=9, scan_keys=["old-session"]
    )
    keys = await session_index.list_indexed_session_keys(redis, account_id=1, chat_id=9)
    assert set(keys or []) == {"old-session", "new-session"}
    assert set(merged) == {"old-session", "new-session"}


@pytest.mark.asyncio
async def test_rebuild_adds_scan_keys_into_empty_index() -> None:
    redis = _FakeRedis()
    merged = await session_index.rebuild_chat_index_from_scan(
        redis, account_id=2, chat_id=3, scan_keys=["a", "b"]
    )
    keys = await session_index.list_indexed_session_keys(redis, account_id=2, chat_id=3)
    assert set(keys or []) == {"a", "b"}
    assert set(merged) == {"a", "b"}
