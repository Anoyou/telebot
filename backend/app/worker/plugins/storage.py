"""Persistent, namespaced key-value storage exposed to plugins as ``ctx.storage``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

_KEY_PREFIX = "plugin_store"


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _decode_redis_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _normalize_user_key(key: Any) -> str:
    normalized = "" if key is None else str(key).strip()
    if not normalized:
        raise ValueError("ctx.storage key must not be empty")
    return normalized


def _normalize_ttl(ttl: int | float | None) -> int | None:
    if ttl is None:
        return None
    try:
        seconds = int(ttl)
    except (TypeError, ValueError) as exc:
        raise ValueError("ctx.storage ttl must be a positive number of seconds") from exc
    if seconds <= 0:
        raise ValueError("ctx.storage ttl must be a positive number of seconds")
    return seconds


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any) -> Any:
    return json.loads(_decode_redis_text(value))


class PluginStorage:
    """Small Redis-backed storage facade scoped to one account and one plugin.

    Error semantics are intentionally conservative and backward compatible:
    ``get`` returns ``default`` for missing/unavailable/unreadable JSON values,
    while ``set`` surfaces JSON serialization errors from ``json.dumps``.
    """

    __slots__ = ("account_id", "plugin_key", "_redis", "_prefix")

    def __init__(
        self,
        *,
        account_id: int,
        plugin_key: str,
        redis: Any = None,
    ) -> None:
        self.account_id = int(account_id)
        self.plugin_key = str(plugin_key or "").strip()
        if not self.plugin_key:
            raise ValueError("ctx.storage plugin_key must not be empty")
        self._redis = redis
        self._prefix = f"{_KEY_PREFIX}:{self.account_id}:{self.plugin_key}:"

    @classmethod
    def from_context(cls, ctx: Any) -> PluginStorage:
        """Build storage from a ``PluginContext``-like object."""

        return cls(
            account_id=int(ctx.account_id),
            plugin_key=str(getattr(ctx, "feature_key", "") or ""),
            redis=getattr(ctx, "redis", None),
        )

    def _key(self, key: Any) -> str:
        return f"{self._prefix}{_normalize_user_key(key)}"

    def _user_key_from_redis_key(self, key: Any) -> str | None:
        raw = _decode_redis_text(key)
        if not raw.startswith(self._prefix):
            return None
        return raw[len(self._prefix):]

    @property
    def available(self) -> bool:
        """Whether a Redis backend is attached."""

        return self._redis is not None

    async def get(self, key: Any, default: Any = None) -> Any:
        """Return a JSON value by user key, or ``default`` when absent/unavailable."""

        if self._redis is None:
            return default
        raw = await _maybe_await(self._redis.get(self._key(key)))
        if raw is None:
            return default
        try:
            return _json_loads(raw)
        except (TypeError, ValueError):
            return default

    async def set(self, key: Any, value: Any, *, ttl: int | float | None = None) -> bool:
        """Store a JSON value. ``ttl`` is an optional expiry in seconds."""

        if self._redis is None:
            return False
        redis_key = self._key(key)
        payload = _json_dumps(value)
        ttl_seconds = _normalize_ttl(ttl)
        if ttl_seconds is None:
            result = await _maybe_await(self._redis.set(redis_key, payload))
        else:
            result = await _maybe_await(self._redis.set(redis_key, payload, ex=ttl_seconds))
        return True if result is None else bool(result)

    async def delete(self, *keys: Any) -> int:
        """Delete one or more user keys and return the number removed."""

        if self._redis is None or not keys:
            return 0
        redis_keys = [self._key(key) for key in keys]
        result = await _maybe_await(self._redis.delete(*redis_keys))
        return int(result or 0)

    async def incr(
        self,
        key: Any,
        amount: int = 1,
        *,
        ttl: int | float | None = None,
    ) -> int | None:
        """Increment an integer value and return the new value.

        Redis ``INCRBY`` is atomic, but when ``ttl`` is provided the following
        ``EXPIRE`` is a separate operation. The ttl is refreshed on every
        increment, so callers should treat it as sliding-window storage rather
        than a fixed-window counter. A process crash between the two commands
        can leave a newly incremented key without expiry.
        """

        if self._redis is None:
            return None
        redis_key = self._key(key)
        amount_int = int(amount)
        ttl_seconds = _normalize_ttl(ttl)
        incrby = getattr(self._redis, "incrby", None)
        if callable(incrby):
            value = await _maybe_await(incrby(redis_key, amount_int))
        else:
            incr = getattr(self._redis, "incr", None)
            if callable(incr):
                value = await _maybe_await(incr(redis_key, amount_int))
            else:
                current = await self.get(key, 0)
                value = int(current or 0) + amount_int
                await self.set(key, value)
        if ttl_seconds is not None:
            expire = getattr(self._redis, "expire", None)
            if callable(expire):
                await _maybe_await(expire(redis_key, ttl_seconds))
            else:
                await self.set(key, int(value), ttl=ttl_seconds)
        return int(value)

    async def get_all(self) -> dict[str, Any]:
        """Return all JSON values in this account/plugin namespace."""

        if self._redis is None:
            return {}
        keys = await self._scan_keys()
        if not keys:
            return {}

        mget = getattr(self._redis, "mget", None)
        if callable(mget):
            raw_values = await _maybe_await(mget(keys))
        else:
            raw_values = [await _maybe_await(self._redis.get(key)) for key in keys]

        out: dict[str, Any] = {}
        for redis_key, raw_value in zip(keys, raw_values, strict=False):
            if raw_value is None:
                continue
            user_key = self._user_key_from_redis_key(redis_key)
            if user_key is None:
                continue
            try:
                out[user_key] = _json_loads(raw_value)
            except (TypeError, ValueError):
                continue
        return out

    async def items(self) -> dict[str, Any]:
        """Alias for ``get_all`` for plugin authors who prefer mapping wording."""

        return await self.get_all()

    async def _scan_keys(self) -> list[str]:
        pattern = f"{self._prefix}*"
        scan_iter = getattr(self._redis, "scan_iter", None)
        if callable(scan_iter):
            iterator = scan_iter(match=pattern)
            iterator = await _maybe_await(iterator)
            if hasattr(iterator, "__aiter__"):
                keys: list[str] = []
                async for key in iterator:
                    keys.append(_decode_redis_text(key))
                return keys
            if isinstance(iterator, Iterable):
                return [_decode_redis_text(key) for key in iterator]

        keys_method = getattr(self._redis, "keys", None)
        if callable(keys_method):
            raw_keys = await _maybe_await(keys_method(pattern))
            return [_decode_redis_text(key) for key in (raw_keys or [])]
        return []


__all__ = ["PluginStorage"]
