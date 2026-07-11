"""Reverse index: account+chat → interaction session Redis keys.

Hot path replaces dual SCAN with SMEMBERS + GET. Index is best-effort and
rebuilt from SCAN when missing/stale.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

SESSION_KEY_PREFIX = "account_bot:interaction_session:"
CHAT_INDEX_PREFIX = "account_bot:interaction_session_chat:"
CHAT_INDEX_TTL_SECONDS = 7 * 24 * 3600


def chat_index_key(account_id: int, chat_id: int) -> str:
    return f"{CHAT_INDEX_PREFIX}{int(account_id)}:{int(chat_id)}"


async def index_session_key(
    redis: Any,
    *,
    account_id: int,
    chat_id: int | None,
    session_key: str,
    ttl_seconds: int | None = None,
) -> None:
    if redis is None or chat_id is None or not session_key:
        return
    key = chat_index_key(account_id, int(chat_id))
    try:
        await redis.sadd(key, session_key)
        ex = int(ttl_seconds or CHAT_INDEX_TTL_SECONDS)
        if ex > 0:
            await redis.expire(key, ex)
    except Exception:  # noqa: BLE001
        log.debug(
            "index session key failed account=%s chat=%s key=%s",
            account_id,
            chat_id,
            session_key,
            exc_info=True,
        )


async def unindex_session_key(
    redis: Any,
    *,
    account_id: int,
    chat_id: int | None,
    session_key: str,
) -> None:
    if redis is None or chat_id is None or not session_key:
        return
    try:
        await redis.srem(chat_index_key(account_id, int(chat_id)), session_key)
    except Exception:  # noqa: BLE001
        log.debug(
            "unindex session key failed account=%s chat=%s key=%s",
            account_id,
            chat_id,
            session_key,
            exc_info=True,
        )


async def list_indexed_session_keys(
    redis: Any,
    *,
    account_id: int,
    chat_id: int,
) -> list[str] | None:
    """Return indexed keys, or ``None`` if the index key is missing (caller may rebuild)."""

    if redis is None:
        return None
    key = chat_index_key(account_id, chat_id)
    try:
        exists = await redis.exists(key)
        if not int(exists or 0):
            return None
        members = await redis.smembers(key)
    except Exception:  # noqa: BLE001
        log.debug("list indexed session keys failed account=%s chat=%s", account_id, chat_id, exc_info=True)
        return None
    out: list[str] = []
    for raw in members or []:
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
        if text:
            out.append(text)
    return out


async def rebuild_chat_index_from_scan(
    redis: Any,
    *,
    account_id: int,
    chat_id: int,
    scan_keys: list[str],
) -> list[str]:
    """Replace chat index membership with ``scan_keys`` (may be empty)."""

    if redis is None:
        return list(scan_keys)
    key = chat_index_key(account_id, chat_id)
    try:
        pipe = getattr(redis, "pipeline", None)
        if callable(pipe):
            p = redis.pipeline()
            p.delete(key)
            if scan_keys:
                p.sadd(key, *scan_keys)
                p.expire(key, CHAT_INDEX_TTL_SECONDS)
            await p.execute()
        else:
            await redis.delete(key)
            if scan_keys:
                await redis.sadd(key, *scan_keys)
                await redis.expire(key, CHAT_INDEX_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        log.debug("rebuild chat index failed account=%s chat=%s", account_id, chat_id, exc_info=True)
    return list(scan_keys)


def session_still_active(session: dict[str, Any], *, now: float | None = None) -> bool:
    if not isinstance(session, dict) or not session:
        return False
    expires_at = session.get("expires_at")
    try:
        if expires_at is not None and float(expires_at) <= (now if now is not None else time.time()):
            return False
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "CHAT_INDEX_PREFIX",
    "SESSION_KEY_PREFIX",
    "chat_index_key",
    "index_session_key",
    "list_indexed_session_keys",
    "rebuild_chat_index_from_scan",
    "session_still_active",
    "unindex_session_key",
]
