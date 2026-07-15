"""Cross-pipeline dedupe helpers for interaction session messages."""

from __future__ import annotations

import logging
from typing import Any

from ...redis_client import get_redis

log = logging.getLogger(__name__)

INTERACTION_MESSAGE_CLAIM_PREFIX = "account_bot:interaction_msg_claim:"
INTERACTION_MESSAGE_CLAIM_TTL_SECONDS = 90


def _claim_ttl(raw: int | None = None) -> int:
    try:
        value = int(raw or INTERACTION_MESSAGE_CLAIM_TTL_SECONDS)
    except (TypeError, ValueError):
        value = INTERACTION_MESSAGE_CLAIM_TTL_SECONDS
    return min(max(value, 60), 120)


def interaction_message_claim_key(
    account_id: int,
    chat_id: int,
    message_id: int | None,
    rule_id: Any,
    event_key: Any | None = None,
) -> str | None:
    if message_id is None:
        return None
    rule_key = str(rule_id or "legacy").strip() or "legacy"
    suffix = str(event_key or "").strip()
    base = f"{INTERACTION_MESSAGE_CLAIM_PREFIX}{int(account_id)}:{int(chat_id)}:{int(message_id)}:{rule_key}"
    return f"{base}:{suffix}" if suffix else base


async def claim_interaction_message(
    *,
    account_id: int,
    chat_id: int,
    message_id: int | None,
    rule_id: Any,
    event_key: Any | None = None,
    redis: Any | None = None,
    ttl_seconds: int | None = None,
    fail_open: bool = True,
) -> bool:
    """Claim a message for one interaction rule.

    Missing message ids cannot be correlated safely across Bot API and
    Telethon, so they intentionally pass through. Callers that may execute
    session or financial side effects must pass ``fail_open=False``.
    """

    key = interaction_message_claim_key(account_id, chat_id, message_id, rule_id, event_key)
    if key is None:
        return True
    try:
        client = redis or get_redis()
        return bool(await client.set(key, "1", ex=_claim_ttl(ttl_seconds), nx=True))
    except Exception:  # noqa: BLE001
        log.debug(
            "claim interaction message failed aid=%s chat=%s message=%s rule=%s",
            account_id,
            chat_id,
            message_id,
            rule_id,
            exc_info=True,
        )
        return bool(fail_open)


async def release_interaction_message(
    *,
    account_id: int,
    chat_id: int,
    message_id: int | None,
    rule_id: Any,
    event_key: Any | None = None,
    redis: Any | None = None,
) -> None:
    """仅用于"成功调用但零动作"场景，把消息让还给另一条管道。"""

    key = interaction_message_claim_key(account_id, chat_id, message_id, rule_id, event_key)
    if key is None:
        return
    try:
        client = redis or get_redis()
        await client.delete(key)
    except Exception:  # noqa: BLE001
        log.debug(
            "release interaction message failed aid=%s chat=%s message=%s rule=%s",
            account_id,
            chat_id,
            message_id,
            rule_id,
            exc_info=True,
        )


__all__ = [
    "INTERACTION_MESSAGE_CLAIM_PREFIX",
    "INTERACTION_MESSAGE_CLAIM_TTL_SECONDS",
    "claim_interaction_message",
    "interaction_message_claim_key",
    "release_interaction_message",
]
