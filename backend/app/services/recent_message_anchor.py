"""UserBot 近期真实用户消息锚点。

只缓存 Telegram 消息自身明确携带的 ``PeerUser``。匿名管理员和频道身份的
``PeerChannel`` 不得反向绑定到按钮点击者，否则会泄露匿名管理员身份。
"""

from __future__ import annotations

import logging
from typing import Any

from telethon.tl.types import PeerUser

log = logging.getLogger(__name__)

DEFAULT_SEARCH_LIMIT = 5000
MAX_SEARCH_LIMIT = 5000
ANCHOR_TTL_SECONDS = 7 * 24 * 60 * 60


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_search_limit(raw: Any) -> int:
    value = _int_or_none(raw)
    if value is None:
        value = DEFAULT_SEARCH_LIMIT
    return max(1, min(MAX_SEARCH_LIMIT, value))


def anchor_key(account_id: Any, chat_id: Any, user_id: Any) -> str | None:
    account = _int_or_none(account_id)
    chat = _int_or_none(chat_id)
    user = _int_or_none(user_id)
    if account is None or chat is None or user is None or chat >= 0 or user <= 0:
        return None
    return f"tp:recent-user-message:{account}:{chat}:{user}"


def genuine_user_message_identity(event: Any) -> tuple[int, int, int] | None:
    """提取群消息中的真实发送者；匿名/频道身份明确返回空。"""

    message = getattr(event, "message", None)
    from_id = getattr(message, "from_id", None)
    if not isinstance(from_id, PeerUser):
        return None
    chat_id = _int_or_none(getattr(event, "chat_id", None))
    message_id = _int_or_none(getattr(message, "id", None) or getattr(event, "id", None))
    user_id = _int_or_none(getattr(from_id, "user_id", None))
    if chat_id is None or chat_id >= 0 or message_id is None or message_id <= 0 or user_id is None or user_id <= 0:
        return None
    return chat_id, user_id, message_id


async def cache_incoming_user_message(redis: Any | None, account_id: Any, event: Any) -> bool:
    identity = genuine_user_message_identity(event)
    if redis is None or identity is None:
        return False
    chat_id, user_id, message_id = identity
    key = anchor_key(account_id, chat_id, user_id)
    if key is None:
        return False
    try:
        await redis.set(key, str(message_id), ex=ANCHOR_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        log.debug(
            "cache recent user message anchor failed account=%s chat=%s user=%s",
            account_id,
            chat_id,
            user_id,
            exc_info=True,
        )
        return False
    return True


async def read_cached_message_id(
    redis: Any | None,
    account_id: Any,
    chat_id: Any,
    user_id: Any,
) -> int | None:
    if redis is None:
        return None
    key = anchor_key(account_id, chat_id, user_id)
    if key is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception:  # noqa: BLE001
        log.debug("read recent user message anchor failed key=%s", key, exc_info=True)
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    message_id = _int_or_none(raw)
    return message_id if message_id is not None and message_id > 0 else None


def _message_id(message: Any) -> int | None:
    return _int_or_none(getattr(message, "id", None) or getattr(message, "message_id", None))


def _genuine_message_user_id(message: Any) -> int | None:
    from_id = getattr(message, "from_id", None)
    if not isinstance(from_id, PeerUser):
        return None
    return _int_or_none(getattr(from_id, "user_id", None))


async def find_recent_message_id_for_user(
    client: Any,
    chat_id: int,
    user_id: int,
    *,
    limit: int,
    redis: Any | None = None,
    account_id: int | None = None,
) -> int | None:
    """缓存优先，其次 Telegram 精确搜索，最后扫描最近 5000 条。"""

    cached = await read_cached_message_id(redis, account_id, chat_id, user_id)
    if cached is not None:
        return cached

    try:
        async for message in client.iter_messages(chat_id, from_user=user_id, limit=limit):
            message_id = _message_id(message)
            if message_id is not None:
                return message_id
    except Exception:  # noqa: BLE001
        log.debug(
            "recent participant message search via from_user failed chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )

    try:
        async for message in client.iter_messages(chat_id, limit=limit):
            if _genuine_message_user_id(message) != user_id:
                continue
            message_id = _message_id(message)
            if message_id is not None:
                return message_id
    except Exception:  # noqa: BLE001
        log.debug(
            "recent participant message fallback search failed chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )
    return None


__all__ = [
    "ANCHOR_TTL_SECONDS",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_SEARCH_LIMIT",
    "anchor_key",
    "cache_incoming_user_message",
    "find_recent_message_id_for_user",
    "genuine_user_message_identity",
    "normalize_search_limit",
    "read_cached_message_id",
]
