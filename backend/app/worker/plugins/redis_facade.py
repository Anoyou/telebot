"""Namespaced Redis facade for installed plugins.

Builtin plugins may keep a raw client; installed plugins only receive this
facade (or None). Keys are always prefixed with
``plugin_store:{account_id}:{plugin_key}:``.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_KEY_PREFIX = "plugin_store"
_ALLOWED_METHODS = frozenset(
    {
        "get",
        "set",
        "delete",
        "exists",
        "expire",
        "ttl",
        "incr",
        "incrby",
        "decr",
        "decrby",
        "hget",
        "hset",
        "hdel",
        "hgetall",
        "hincrby",
        "hexists",
        "lpush",
        "rpush",
        "lrange",
        "llen",
        "ltrim",
        "sadd",
        "srem",
        "smembers",
        "sismember",
    }
)
_BLOCKED_METHODS = frozenset(
    {
        "keys",
        "scan",
        "scan_iter",
        "flushdb",
        "flushall",
        "pubsub",
        "publish",
        "subscribe",
        "psubscribe",
        "eval",
        "evalsha",
        "script",
        "pipeline",
        "multi",
        "exec",
        "watch",
        "unwatch",
        "migrate",
        "move",
        "config_set",
        "config_get",
        "client_list",
        "client_kill",
        "shutdown",
        "replicaof",
        "slaveof",
        "bgsave",
        "save",
        "debug",
    }
)


class PluginRedisPermissionError(PermissionError):
    """Raised when an installed plugin attempts a disallowed Redis operation."""


class PluginRedisFacade:
    """Minimal namespaced Redis proxy for third-party plugins."""

    __slots__ = ("account_id", "plugin_key", "_redis", "_prefix")

    def __init__(self, *, account_id: int, plugin_key: str, redis: Any) -> None:
        self.account_id = int(account_id)
        self.plugin_key = str(plugin_key or "").strip()
        if not self.plugin_key:
            raise ValueError("plugin_key must not be empty")
        if redis is None:
            raise ValueError("redis client is required")
        self._redis = redis
        self._prefix = f"{_KEY_PREFIX}:{self.account_id}:{self.plugin_key}:"

    @classmethod
    def from_context(cls, ctx: Any, redis: Any) -> PluginRedisFacade:
        return cls(
            account_id=int(ctx.account_id),
            plugin_key=str(getattr(ctx, "feature_key", "") or ""),
            redis=redis,
        )

    def _ns(self, key: Any) -> str:
        text = "" if key is None else str(key)
        if not text.strip():
            raise ValueError("redis key must not be empty")
        # 已带本插件前缀时不重复包装，防止双重前缀。
        if text.startswith(self._prefix):
            return text
        if text.startswith(f"{_KEY_PREFIX}:") and not text.startswith(self._prefix):
            raise PluginRedisPermissionError(
                f"plugin {self.plugin_key} cannot access keys outside its namespace"
            )
        return f"{self._prefix}{text}"

    def _ns_keys(self, keys: list[Any]) -> list[str]:
        return [self._ns(key) for key in keys]

    async def get(self, key: Any) -> Any:
        return await self._redis.get(self._ns(key))

    async def set(self, key: Any, value: Any, **kwargs: Any) -> Any:
        # 拒绝 nx/xx 以外的危险参数扩展：只转发常见 TTL/条件参数。
        allowed_kw = {}
        for name in ("ex", "px", "nx", "xx", "keepttl", "get"):
            if name in kwargs:
                allowed_kw[name] = kwargs[name]
        return await self._redis.set(self._ns(key), value, **allowed_kw)

    async def delete(self, *keys: Any) -> Any:
        if not keys:
            return 0
        return await self._redis.delete(*self._ns_keys(list(keys)))

    async def exists(self, *keys: Any) -> Any:
        if not keys:
            return 0
        return await self._redis.exists(*self._ns_keys(list(keys)))

    async def expire(self, key: Any, seconds: int) -> Any:
        return await self._redis.expire(self._ns(key), int(seconds))

    async def ttl(self, key: Any) -> Any:
        return await self._redis.ttl(self._ns(key))

    async def incr(self, key: Any) -> Any:
        return await self._redis.incr(self._ns(key))

    async def incrby(self, key: Any, amount: int = 1) -> Any:
        return await self._redis.incrby(self._ns(key), int(amount))

    async def decr(self, key: Any) -> Any:
        return await self._redis.decr(self._ns(key))

    async def decrby(self, key: Any, amount: int = 1) -> Any:
        return await self._redis.decrby(self._ns(key), int(amount))

    async def hget(self, key: Any, field: Any) -> Any:
        return await self._redis.hget(self._ns(key), field)

    async def hset(self, key: Any, field: Any = None, value: Any = None, mapping: Any = None) -> Any:
        namespaced = self._ns(key)
        if mapping is not None:
            return await self._redis.hset(namespaced, mapping=mapping)
        return await self._redis.hset(namespaced, field, value)

    async def hdel(self, key: Any, *fields: Any) -> Any:
        return await self._redis.hdel(self._ns(key), *fields)

    async def hgetall(self, key: Any) -> Any:
        return await self._redis.hgetall(self._ns(key))

    async def hincrby(self, key: Any, field: Any, amount: int = 1) -> Any:
        return await self._redis.hincrby(self._ns(key), field, int(amount))

    async def hexists(self, key: Any, field: Any) -> Any:
        return await self._redis.hexists(self._ns(key), field)

    async def lpush(self, key: Any, *values: Any) -> Any:
        return await self._redis.lpush(self._ns(key), *values)

    async def rpush(self, key: Any, *values: Any) -> Any:
        return await self._redis.rpush(self._ns(key), *values)

    async def lrange(self, key: Any, start: int, end: int) -> Any:
        return await self._redis.lrange(self._ns(key), int(start), int(end))

    async def llen(self, key: Any) -> Any:
        return await self._redis.llen(self._ns(key))

    async def ltrim(self, key: Any, start: int, end: int) -> Any:
        return await self._redis.ltrim(self._ns(key), int(start), int(end))

    async def sadd(self, key: Any, *members: Any) -> Any:
        return await self._redis.sadd(self._ns(key), *members)

    async def srem(self, key: Any, *members: Any) -> Any:
        return await self._redis.srem(self._ns(key), *members)

    async def smembers(self, key: Any) -> Any:
        return await self._redis.smembers(self._ns(key))

    async def sismember(self, key: Any, member: Any) -> Any:
        return await self._redis.sismember(self._ns(key), member)

    def __getattr__(self, name: str) -> Any:
        if name in _BLOCKED_METHODS or name not in _ALLOWED_METHODS:
            raise PluginRedisPermissionError(
                f"plugin {self.plugin_key} redis operation blocked: {name}"
            )
        raise AttributeError(name)


__all__ = [
    "PluginRedisFacade",
    "PluginRedisPermissionError",
]
