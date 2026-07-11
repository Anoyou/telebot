"""Installed 插件 Redis facade 权限边界测试。"""

from __future__ import annotations

import pytest

from app.worker.plugins.redis_facade import PluginRedisFacade, PluginRedisPermissionError


class _FakeRedis:
    def __init__(self) -> None:
        self.ops: list[tuple] = []
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        self.ops.append(("get", key))
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.ops.append(("set", key, value, kwargs))
        self.values[key] = value
        return True

    async def delete(self, *keys: str):
        self.ops.append(("delete", keys))
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def incr(self, key: str):
        self.ops.append(("incr", key))
        cur = int(self.values.get(key, "0")) + 1
        self.values[key] = str(cur)
        return cur


@pytest.mark.asyncio
async def test_facade_namespaces_keys() -> None:
    redis = _FakeRedis()
    facade = PluginRedisFacade(account_id=7, plugin_key="game", redis=redis)
    await facade.set("score", "10")
    assert redis.values["plugin_store:7:game:score"] == "10"
    assert await facade.get("score") == "10"


@pytest.mark.asyncio
async def test_facade_blocks_keys_and_cross_namespace() -> None:
    redis = _FakeRedis()
    facade = PluginRedisFacade(account_id=7, plugin_key="game", redis=redis)
    with pytest.raises(PluginRedisPermissionError):
        _ = facade.keys  # type: ignore[attr-defined]
    with pytest.raises(PluginRedisPermissionError):
        _ = facade.scan_iter  # type: ignore[attr-defined]
    with pytest.raises(PluginRedisPermissionError):
        await facade.get("plugin_store:7:other:secret")


@pytest.mark.asyncio
async def test_facade_allows_incr_and_delete() -> None:
    redis = _FakeRedis()
    facade = PluginRedisFacade(account_id=1, plugin_key="demo", redis=redis)
    assert await facade.incr("c") == 1
    assert await facade.incr("c") == 2
    await facade.delete("c")
    assert "plugin_store:1:demo:c" not in redis.values
