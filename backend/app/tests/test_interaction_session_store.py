"""Unit tests for atomic interaction session updates (Lua CAS helper)."""

from __future__ import annotations

import json
import math

import pytest

from app.services.interaction import session_store
from app.services.interaction.session_store import (
    DEFAULT_SESSION_TTL_GRACE_SECONDS,
    SessionNotFoundError,
    SessionRevisionConflictError,
    SessionUpdateResult,
    claim_expired_interaction_session,
    finish_expired_interaction_session,
    merge_session_update,
    session_redis_ttl_seconds,
    update_interaction_session,
)


class _EvalRedis:
    """In-memory redis that executes the session Lua script via the pure merge path.

    Production uses redis.eval + cjson; tests exercise the same merge/CAS
    semantics through the Python fallback when ``eval`` is absent, and also
    through an ``eval`` implementation that delegates to ``merge_session_update``.
    """

    def __init__(self, *, use_eval: bool = True) -> None:
        self.data: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, dict[str, object]]] = []
        self.eval_calls = 0
        self._use_eval = use_eval

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.set_calls.append((key, value, dict(kwargs)))
        self.data[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *args):
        if not self._use_eval:
            raise AttributeError("eval disabled")
        self.eval_calls += 1
        assert numkeys == 1
        assert "revision" in script
        key = str(args[0])
        patch = json.loads(args[1])
        extend = int(args[2])
        has_expected = int(args[3])
        expected = int(args[4])
        now = float(args[5])
        grace = int(args[6])
        do_extend = int(args[7])
        raw = self.data.get(key)
        if raw is None:
            return ["missing", ""]
        existing = json.loads(raw)
        try:
            payload, _ttl = merge_session_update(
                existing,
                data=patch if isinstance(patch, dict) else {},
                extend_seconds=extend if do_extend else None,
                expected_revision=expected if has_expected else None,
                now=now,
            )
        except SessionNotFoundError:
            return ["missing", ""]
        except SessionRevisionConflictError as exc:
            return ["conflict", str(exc.current_revision)]
        except session_store.SessionInvalidError as exc:
            return ["invalid", str(exc)]
        ttl = session_redis_ttl_seconds(
            float(payload["expires_at"]),
            now=now,
            grace_seconds=grace,
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        await self.set(key, encoded, ex=ttl)
        return ["ok", encoded]


def _seed(
    redis: _EvalRedis,
    key: str,
    *,
    now: float,
    expires_in: float = 120.0,
    data: dict | None = None,
    revision: int | None = None,
    extra: dict | None = None,
) -> None:
    payload = {
        "account_id": 1,
        "chat_id": -100,
        "channel": "interaction_bot",
        "created_at": now - 60,
        "updated_at": now - 10,
        "expires_at": now + expires_in,
        "data": dict(data or {}),
        "module_key": "demo",
        "entry_key": "start",
    }
    if revision is not None:
        payload["revision"] = revision
    if extra:
        payload.update(extra)
    redis.data[key] = json.dumps(payload)


@pytest.mark.asyncio
async def test_sequential_updates_preserve_distinct_data_keys() -> None:
    redis = _EvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    _seed(redis, key, now=now, data={"round": 1})

    first = await update_interaction_session(
        redis, key, data={"score": 3}, now=now + 1
    )
    second = await update_interaction_session(
        redis, key, data={"phase": "betting"}, now=now + 2
    )

    assert first.data == {"round": 1, "score": 3}
    assert second.data == {"round": 1, "score": 3, "phase": "betting"}
    stored = json.loads(redis.data[key])
    assert stored["data"] == {"round": 1, "score": 3, "phase": "betting"}
    assert stored["module_key"] == "demo"
    assert stored["channel"] == "interaction_bot"
    assert redis.eval_calls == 2


@pytest.mark.asyncio
async def test_extend_seconds_only_extends_not_shrinks() -> None:
    redis = _EvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    original_expires = now + 120
    _seed(redis, key, now=now, expires_in=120.0, data={"round": 1})

    # No extend: expiry unchanged.
    no_extend = await update_interaction_session(
        redis, key, data={"a": 1}, extend_seconds=None, now=now
    )
    assert no_extend.expires_at == original_expires

    # extend_seconds=0 is a no-op for expiry (same as existing delivery semantics).
    zero = await update_interaction_session(
        redis, key, data={"b": 2}, extend_seconds=0, now=now
    )
    assert zero.expires_at == original_expires

    # Positive extend: max(expires, now) + extend.
    extended = await update_interaction_session(
        redis, key, data={"c": 3}, extend_seconds=45, now=now
    )
    assert extended.expires_at == original_expires + 45
    remaining = extended.expires_at - now
    expected_ttl = max(1, int(math.ceil(remaining))) + DEFAULT_SESSION_TTL_GRACE_SECONDS
    assert redis.set_calls[-1][2]["ex"] == expected_ttl


@pytest.mark.asyncio
async def test_revision_increments_from_missing_and_existing() -> None:
    redis = _EvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    _seed(redis, key, now=now, data={"round": 1})

    first = await update_interaction_session(redis, key, data={"x": 1}, now=now)
    assert first.revision == 1
    assert isinstance(first, SessionUpdateResult)

    second = await update_interaction_session(redis, key, data={"y": 2}, now=now + 1)
    assert second.revision == 2

    redis2 = _EvalRedis()
    _seed(redis2, key, now=now, data={}, revision=7)
    bumped = await update_interaction_session(redis2, key, data={"z": 1}, now=now)
    assert bumped.revision == 8


@pytest.mark.asyncio
async def test_cas_conflict_when_expected_revision_mismatches() -> None:
    redis = _EvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    _seed(redis, key, now=now, data={"round": 1}, revision=3)

    ok = await update_interaction_session(
        redis, key, data={"ok": True}, expected_revision=3, now=now
    )
    assert ok.revision == 4

    with pytest.raises(SessionRevisionConflictError) as exc_info:
        await update_interaction_session(
            redis, key, data={"bad": True}, expected_revision=3, now=now + 1
        )
    assert exc_info.value.current_revision == 4
    assert exc_info.value.expected_revision == 3
    # Conflicting write must not apply.
    stored = json.loads(redis.data[key])
    assert "bad" not in stored["data"]
    assert stored["revision"] == 4


class _NoEvalRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, dict[str, object]]] = []

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.set_calls.append((key, value, dict(kwargs)))
        self.data[key] = value
        return True

    async def delete(self, key: str):
        return 1 if self.data.pop(key, None) is not None else 0


@pytest.mark.asyncio
async def test_python_fallback_without_eval() -> None:
    redis = _NoEvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    redis.data[key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100,
            "channel": "interaction_bot",
            "created_at": now - 60,
            "updated_at": now - 10,
            "expires_at": now + 120,
            "data": {"round": 1},
        }
    )

    result = await update_interaction_session(redis, key, data={"score": 9}, now=now)
    assert result.data == {"round": 1, "score": 9}
    assert result.revision == 1
    assert redis.set_calls


@pytest.mark.asyncio
async def test_missing_session_raises() -> None:
    redis = _EvalRedis()
    with pytest.raises(SessionNotFoundError):
        await update_interaction_session(redis, "missing-key", data={"a": 1})


def test_session_redis_ttl_matches_grace_constants() -> None:
    now = 1000.0
    assert session_redis_ttl_seconds(now + 120, now=now) == 120 + DEFAULT_SESSION_TTL_GRACE_SECONDS
    assert DEFAULT_SESSION_TTL_GRACE_SECONDS == 90


@pytest.mark.asyncio
async def test_expiry_claim_allows_only_one_scanner_and_failure_releases() -> None:
    redis = _NoEvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    redis.data[key] = json.dumps({"revision": 4, "expires_at": now - 1, "data": {}})

    first = await claim_expired_interaction_session(
        redis, key, expected_revision=4, now=now, token="scanner-a"
    )
    second = await claim_expired_interaction_session(
        redis, key, expected_revision=4, now=now, token="scanner-b"
    )

    assert first is not None
    assert second is None
    assert await finish_expired_interaction_session(redis, key, first, success=False) is False
    retry = await claim_expired_interaction_session(
        redis, key, expected_revision=4, now=now + 1, token="scanner-b"
    )
    assert retry is not None
    assert await finish_expired_interaction_session(redis, key, retry, success=True) is True
    assert key not in redis.data


@pytest.mark.asyncio
async def test_expiry_compare_delete_preserves_concurrent_renewal() -> None:
    redis = _NoEvalRedis()
    key = "account_bot:interaction_session:1:demo:-100"
    now = 1_720_000_000.0
    redis.data[key] = json.dumps({"revision": 2, "expires_at": now - 1, "data": {"round": 1}})
    claim = await claim_expired_interaction_session(
        redis, key, expected_revision=2, now=now, token="scanner-a"
    )
    assert claim is not None

    renewed = await update_interaction_session(
        redis,
        key,
        data={"renewed": True},
        extend_seconds=120,
        expected_revision=2,
        now=now,
    )
    assert renewed.revision == 3
    assert await finish_expired_interaction_session(redis, key, claim, success=True) is False

    stored = json.loads(redis.data[key])
    assert stored["revision"] == 3
    assert stored["data"] == {"round": 1, "renewed": True}
    assert stored["expires_at"] == now + 120
    assert "_expiry_claim" not in stored
