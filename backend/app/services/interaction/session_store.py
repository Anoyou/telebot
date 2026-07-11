"""Atomic interaction session updates via Redis Lua CAS.

Unifies the ``update_session`` write path across userbot (E1) and interaction-bot
delivery (E2). Callers own validation of action shape; this module owns
read-merge-write of the Redis session JSON record.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

# Matches account_bot_runtime._INTERACTION_SESSION_TTL_GRACE_SECONDS
# and loader._USERBOT_SESSION_TTL_GRACE_SECONDS.
DEFAULT_SESSION_TTL_GRACE_SECONDS = 90
_FALLBACK_REMAINING_SECONDS = 600

# KEYS[1] = session key
# ARGV[1] = data patch JSON object
# ARGV[2] = extend_seconds (number; only applied when ARGV[7] == 1)
# ARGV[3] = has_expected_revision (0/1)
# ARGV[4] = expected_revision
# ARGV[5] = now (unix float seconds)
# ARGV[6] = grace_seconds
# ARGV[7] = do_extend (0/1; 1 only when extend_seconds > 0)
#
# Returns: {status, detail}
#   ok       -> detail is updated session JSON
#   missing  -> session key absent
#   conflict -> detail is current revision string
#   invalid  -> detail is reason
_UPDATE_SESSION_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  return {'missing', ''}
end

local ok, session = pcall(cjson.decode, raw)
if not ok or type(session) ~= 'table' then
  return {'invalid', 'session json invalid'}
end

local current_rev = tonumber(session['revision']) or 0
local has_expected = tonumber(ARGV[3]) or 0
local expected_revision = tonumber(ARGV[4]) or 0
if has_expected == 1 and current_rev ~= expected_revision then
  return {'conflict', tostring(current_rev)}
end

local data = session['data']
if type(data) ~= 'table' then
  data = {}
end
-- cjson may decode empty JSON objects as arrays; normalize to a map.
if #data > 0 then
  data = {}
end

local patch_ok, patch = pcall(cjson.decode, ARGV[1])
if patch_ok and type(patch) == 'table' then
  for k, v in pairs(patch) do
    -- skip array-index keys from empty/array decode quirks
    if type(k) == 'string' then
      data[k] = v
    end
  end
end
session['data'] = data

local now = tonumber(ARGV[5]) or 0
session['updated_at'] = now

local expires_at = tonumber(session['expires_at'])
if expires_at == nil then
  return {'invalid', 'session expires_at missing'}
end

local do_extend = tonumber(ARGV[7]) or 0
local extend_seconds = tonumber(ARGV[2]) or 0
if do_extend == 1 and extend_seconds > 0 then
  if expires_at < now then
    expires_at = now
  end
  expires_at = expires_at + extend_seconds
  session['expires_at'] = expires_at
end

local new_rev = current_rev + 1
if new_rev < 1 then
  new_rev = 1
end
session['revision'] = new_rev

local grace = tonumber(ARGV[6]) or 90
local remaining = expires_at - now
if remaining < 0 then
  remaining = 0
end
local ttl = math.ceil(remaining)
if ttl < 1 then
  ttl = 1
end
ttl = ttl + grace

-- Prefer object encoding for empty tables when supported.
if cjson.encode_empty_table_as_object then
  cjson.encode_empty_table_as_object(true)
end
local payload = cjson.encode(session)
redis.call('SET', KEYS[1], payload, 'EX', ttl)
return {'ok', payload}
"""


class SessionUpdateError(Exception):
    """Base error for atomic session updates."""


class SessionNotFoundError(SessionUpdateError):
    """Session key is missing in Redis."""


class SessionRevisionConflictError(SessionUpdateError):
    """CAS expected_revision does not match the stored revision."""

    def __init__(self, current_revision: int, expected_revision: int) -> None:
        self.current_revision = int(current_revision)
        self.expected_revision = int(expected_revision)
        super().__init__(
            f"session revision conflict: current={self.current_revision} expected={self.expected_revision}"
        )


class SessionInvalidError(SessionUpdateError):
    """Session payload is unusable (bad JSON / missing expires_at)."""


@dataclass(slots=True, frozen=True)
class SessionUpdateResult:
    session: dict[str, Any]
    revision: int
    expires_at: float
    data: dict[str, Any]
    ttl_seconds: int


def session_redis_ttl_seconds(
    expires_at: float | None,
    *,
    now: float | None = None,
    grace_seconds: int = DEFAULT_SESSION_TTL_GRACE_SECONDS,
) -> int:
    """TTL = remaining-until-expires (ceil, min 1) + grace; fallback 600+grace."""

    now_ts = time.time() if now is None else float(now)
    grace = max(0, int(grace_seconds))
    if expires_at is None:
        return max(1, _FALLBACK_REMAINING_SECONDS) + grace
    try:
        remaining = float(expires_at) - now_ts
    except (TypeError, ValueError):
        remaining = float(_FALLBACK_REMAINING_SECONDS)
    return max(1, int(math.ceil(max(0.0, remaining)))) + grace


def merge_session_update(
    existing: dict[str, Any],
    *,
    data: dict[str, Any] | None = None,
    extend_seconds: int | None = None,
    expected_revision: int | None = None,
    now: float | None = None,
) -> tuple[dict[str, Any], int]:
    """Pure read-merge for tests and non-Lua fallbacks.

    Returns ``(payload, ttl_seconds)`` where ttl excludes nothing — full Redis EX.
    Raises the same error types as the atomic helper.
    """

    if not isinstance(existing, dict) or not existing:
        raise SessionNotFoundError("session not found")

    current_rev = _as_int(existing.get("revision"), default=0) or 0
    if expected_revision is not None and current_rev != int(expected_revision):
        raise SessionRevisionConflictError(current_rev, int(expected_revision))

    now_ts = time.time() if now is None else float(now)
    expires_at = _as_float(existing.get("expires_at"))
    if expires_at is None:
        raise SessionInvalidError("session expires_at missing")

    do_extend = extend_seconds is not None and int(extend_seconds) > 0
    if do_extend:
        expires_at = max(expires_at, now_ts) + int(extend_seconds)

    existing_data = existing.get("data")
    merged_data = dict(existing_data) if isinstance(existing_data, dict) else {}
    if data:
        merged_data.update(dict(data))

    payload = dict(existing)
    payload["data"] = merged_data
    payload["updated_at"] = now_ts
    payload["expires_at"] = expires_at
    new_rev = current_rev + 1
    payload["revision"] = new_rev if new_rev >= 1 else 1

    ttl = session_redis_ttl_seconds(expires_at, now=now_ts)
    return payload, ttl


async def update_interaction_session(
    redis: Any,
    session_key: str,
    *,
    data: dict[str, Any] | None = None,
    extend_seconds: int | None = None,
    expected_revision: int | None = None,
    grace_seconds: int = DEFAULT_SESSION_TTL_GRACE_SECONDS,
    now: float | None = None,
) -> SessionUpdateResult:
    """Atomically merge ``data`` into a session record and bump ``revision``.

    Parameters
    ----------
    redis:
        Async Redis client (must support ``eval`` in production).
    session_key:
        Full Redis key for the interaction session.
    data:
        Shallow-merged into ``session["data"]``. ``None`` / empty means no data change.
    extend_seconds:
        When > 0, ``expires_at = max(expires_at, now) + extend_seconds``.
        ``None`` or ``<= 0`` leaves expiry unchanged (does not shrink).
    expected_revision:
        Optional CAS token. When set, update fails with
        :class:`SessionRevisionConflictError` if the stored revision differs.
    grace_seconds:
        Added to Redis TTL after remaining lifetime (default 90).
    now:
        Optional clock override for tests.
    """

    key = str(session_key or "").strip()
    if not key:
        raise SessionNotFoundError("session key missing")

    now_ts = time.time() if now is None else float(now)
    patch = dict(data) if isinstance(data, dict) else {}
    extend = int(extend_seconds) if extend_seconds is not None else 0
    do_extend = 1 if extend > 0 else 0
    has_expected = 1 if expected_revision is not None else 0
    expected = int(expected_revision) if expected_revision is not None else 0
    grace = int(grace_seconds)

    eval_fn = getattr(redis, "eval", None)
    if callable(eval_fn):
        result = await eval_fn(
            _UPDATE_SESSION_SCRIPT,
            1,
            key,
            json.dumps(patch, ensure_ascii=False),
            extend,
            has_expected,
            expected,
            now_ts,
            grace,
            do_extend,
        )
        update_result = _result_from_lua(result, expected_revision=expected_revision)
    else:
        update_result = await _update_session_python(
            redis,
            key,
            data=patch,
            extend_seconds=extend if do_extend else None,
            expected_revision=expected_revision,
            grace_seconds=grace,
            now=now_ts,
        )
    await _maybe_index_session(
        redis,
        key,
        update_result.session,
        ttl_seconds=update_result.ttl_seconds,
    )
    return update_result


async def _update_session_python(
    redis: Any,
    session_key: str,
    *,
    data: dict[str, Any],
    extend_seconds: int | None,
    expected_revision: int | None,
    grace_seconds: int,
    now: float,
) -> SessionUpdateResult:
    raw = await redis.get(session_key)
    existing = _json_dict(raw)
    if not existing:
        raise SessionNotFoundError("session not found")

    payload, _ttl_default = merge_session_update(
        existing,
        data=data,
        extend_seconds=extend_seconds,
        expected_revision=expected_revision,
        now=now,
    )
    ttl = session_redis_ttl_seconds(
        _as_float(payload.get("expires_at")),
        now=now,
        grace_seconds=grace_seconds,
    )
    await redis.set(session_key, json.dumps(payload, ensure_ascii=False), ex=ttl)
    return _result_from_payload(payload, ttl_seconds=ttl)


async def _maybe_index_session(
    redis: Any,
    session_key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int,
) -> None:
    try:
        from .session_index import index_session_key

        account_id = _as_int(payload.get("account_id"))
        chat_id = _as_int(payload.get("chat_id"))
        if account_id is None or chat_id is None:
            return
        await index_session_key(
            redis,
            account_id=account_id,
            chat_id=chat_id,
            session_key=session_key,
            ttl_seconds=ttl_seconds,
        )
    except Exception:  # noqa: BLE001
        pass


def _result_from_lua(
    result: Any,
    *,
    expected_revision: int | None,
) -> SessionUpdateResult:
    status = ""
    detail = ""
    if isinstance(result, (list, tuple)) and result:
        status = _decode(result[0])
        detail = _decode(result[1]) if len(result) > 1 else ""
    else:
        status = _decode(result)

    if status == "ok":
        payload = _json_dict(detail)
        if not payload:
            raise SessionInvalidError("session update returned empty payload")
        expires_at = _as_float(payload.get("expires_at"))
        if expires_at is None:
            raise SessionInvalidError("session expires_at missing")
        revision = _as_int(payload.get("revision"), default=1) or 1
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        ttl = session_redis_ttl_seconds(expires_at)
        return SessionUpdateResult(
            session=payload,
            revision=revision,
            expires_at=expires_at,
            data=dict(data),
            ttl_seconds=ttl,
        )
    if status == "missing":
        raise SessionNotFoundError("session not found")
    if status == "conflict":
        current = _as_int(detail, default=0) or 0
        raise SessionRevisionConflictError(current, int(expected_revision or 0))
    if status == "invalid":
        raise SessionInvalidError(detail or "session invalid")
    raise SessionUpdateError(f"unexpected session update status: {status!r}")


def _result_from_payload(payload: dict[str, Any], *, ttl_seconds: int) -> SessionUpdateResult:
    expires_at = _as_float(payload.get("expires_at"))
    if expires_at is None:
        raise SessionInvalidError("session expires_at missing")
    revision = _as_int(payload.get("revision"), default=1) or 1
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return SessionUpdateResult(
        session=payload,
        revision=revision,
        expires_at=expires_at,
        data=dict(data),
        ttl_seconds=int(ttl_seconds),
    )


def _json_dict(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value if value is not None else "")


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
