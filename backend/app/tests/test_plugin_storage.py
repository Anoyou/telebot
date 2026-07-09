from __future__ import annotations

import fnmatch
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.worker.plugins.storage import PluginStorage


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _purge_if_expired(self, key: str) -> None:
        expires_at = self.expires_at.get(key)
        if expires_at is not None and expires_at <= self.now:
            self.values.pop(key, None)
            self.expires_at.pop(key, None)

    async def get(self, key: str) -> str | None:
        key = str(key)
        self._purge_if_expired(key)
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        key = str(key)
        self.values[key] = value
        ex = kwargs.get("ex")
        if ex is None:
            self.expires_at.pop(key, None)
        else:
            self.expires_at[key] = self.now + int(ex)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for raw_key in keys:
            key = str(raw_key)
            self._purge_if_expired(key)
            if key in self.values:
                removed += 1
                self.values.pop(key, None)
                self.expires_at.pop(key, None)
        return removed

    async def incrby(self, key: str, amount: int) -> int:
        key = str(key)
        self._purge_if_expired(key)
        value = int(self.values.get(key, "0")) + int(amount)
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        key = str(key)
        self._purge_if_expired(key)
        if key not in self.values:
            return False
        self.expires_at[key] = self.now + int(seconds)
        return True

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [await self.get(key) for key in keys]

    async def scan_iter(self, match: str):
        for key in list(self.values):
            self._purge_if_expired(key)
            if key in self.values and fnmatch.fnmatch(key, match):
                yield key


@pytest.mark.asyncio
async def test_set_get_delete_json_roundtrip() -> None:
    redis = _FakeRedis()
    storage = PluginStorage(account_id=7, plugin_key="lottery_plus", redis=redis)
    value = {
        "round": 3,
        "players": [1001, 1002],
        "meta": {"title": "第3局", "active": True},
    }

    assert await storage.get("state") is None
    assert await storage.set("state", value) is True

    raw_key = "plugin_store:7:lottery_plus:state"
    assert redis.values[raw_key] == json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert await storage.get("state") == value
    assert await storage.delete("state") == 1
    assert await storage.get("state", default={"missing": True}) == {"missing": True}


@pytest.mark.asyncio
async def test_incr_returns_integer_and_remains_json_readable() -> None:
    redis = _FakeRedis()
    storage = PluginStorage(account_id=7, plugin_key="lottery_plus", redis=redis)

    assert await storage.incr("counter") == 1
    assert await storage.get("counter") == 1
    assert await storage.incr("counter", 4, ttl=30) == 5
    assert await storage.get("counter") == 5

    raw_key = "plugin_store:7:lottery_plus:counter"
    assert redis.values[raw_key] == "5"
    assert redis.expires_at[raw_key] == 30

    with pytest.raises(ValueError):
        await storage.incr("invalid_ttl", ttl=0)
    assert await storage.get("invalid_ttl") is None


@pytest.mark.asyncio
async def test_incr_ttl_is_sliding_window_semantics() -> None:
    redis = _FakeRedis()
    storage = PluginStorage(account_id=7, plugin_key="lottery_plus", redis=redis)

    assert await storage.incr("counter", ttl=30) == 1
    redis.advance(10)
    assert await storage.incr("counter", ttl=30) == 2

    raw_key = "plugin_store:7:lottery_plus:counter"
    assert redis.expires_at[raw_key] == 40


@pytest.mark.asyncio
async def test_ttl_expiration_removes_value_from_get_and_get_all() -> None:
    redis = _FakeRedis()
    storage = PluginStorage(account_id=7, plugin_key="lottery_plus", redis=redis)

    await storage.set("temporary", {"status": "open"}, ttl=10)
    assert await storage.get("temporary") == {"status": "open"}

    redis.advance(9.5)
    assert await storage.get("temporary") == {"status": "open"}

    redis.advance(0.5)
    assert await storage.get("temporary", default="expired") == "expired"
    assert await storage.get_all() == {}


@pytest.mark.asyncio
async def test_namespace_isolated_by_account_and_plugin() -> None:
    redis = _FakeRedis()
    account_7_lottery = PluginStorage(account_id=7, plugin_key="lottery_plus", redis=redis)
    account_8_lottery = PluginStorage(account_id=8, plugin_key="lottery_plus", redis=redis)
    account_7_quiz = PluginStorage(account_id=7, plugin_key="quiz", redis=redis)

    await account_7_lottery.set("state", {"account": 7, "plugin": "lottery"})
    await account_8_lottery.set("state", {"account": 8, "plugin": "lottery"})
    await account_7_quiz.set("state", {"account": 7, "plugin": "quiz"})

    assert await account_7_lottery.get("state") == {"account": 7, "plugin": "lottery"}
    assert await account_8_lottery.get("state") == {"account": 8, "plugin": "lottery"}
    assert await account_7_quiz.get("state") == {"account": 7, "plugin": "quiz"}
    assert await account_7_lottery.get_all() == {
        "state": {"account": 7, "plugin": "lottery"}
    }


@pytest.mark.asyncio
async def test_from_context_uses_account_feature_and_redis() -> None:
    redis = _FakeRedis()
    ctx = SimpleNamespace(account_id=42, feature_key="game24", redis=redis)

    storage = PluginStorage.from_context(ctx)
    await storage.set("round:100", {"answer": 24})
    await storage.set(0, "zero")

    assert await storage.get("round:100") == {"answer": 24}
    assert "plugin_store:42:game24:round:100" in redis.values
    assert await storage.get(0) == "zero"


@pytest.mark.asyncio
async def test_missing_redis_degrades_without_raising() -> None:
    storage = PluginStorage(account_id=7, plugin_key="lottery_plus", redis=None)

    assert storage.available is False
    assert await storage.get("state", default={"missing": True}) == {"missing": True}
    assert await storage.set("state", {"round": 1}) is False
    assert await storage.delete("state") == 0
    assert await storage.incr("counter") is None
    assert await storage.get_all() == {}
