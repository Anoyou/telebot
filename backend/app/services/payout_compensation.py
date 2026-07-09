"""Payout compensation enqueue service.

Retryable payout failures are persisted here as pending tickets; the worker
scan loop replays them using the same ``payout_key`` that is threaded into
payout-limit consumption and the sent-marker, so one logical payout is counted
and sent at most once.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db.base import AsyncSessionLocal
from ..db.models.payout_compensation import (
    PAYOUT_COMPENSATION_STATUS_PENDING,
    PayoutCompensation,
)

log = logging.getLogger(__name__)

PAYOUT_KEY_PREFIX = "pay_"
PAYOUT_KEY_MAX_LENGTH = 80
DEFAULT_RETRY_BACKOFF_SECONDS = 60
SETTING_KEY = "payout_compensation"
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_retries": 5,
    "backoff_base_seconds": 60,
    "backoff_max_seconds": 3600,
    "scan_interval_seconds": 60,
    "batch_size": 20,
    "ambiguous_probe": True,
    "replay_drop_reply_anchor": True,
    "notify_channel": None,
}

ERROR_USERBOT_OFFLINE = "userbot_offline"
ERROR_TELEGRAM_API = "telegram_api_error"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_INVALID_PAYOUT_AMOUNT = "invalid_payout_amount"
ERROR_EMPTY_MESSAGE_TEXT = "empty_message_text"
ERROR_SCOPE_NOT_MATCHED = "scope_not_matched"
ERROR_ACTION_LIMIT_EXCEEDED = "action_limit_exceeded"
ERROR_REPLY_ANCHOR_MISSING = "reply_anchor_missing"
ERROR_PAYOUT_LIMIT_EXCEEDED = "payout_limit_exceeded"

RETRYABLE_ERROR_CODES = {
    ERROR_USERBOT_OFFLINE,
    ERROR_TELEGRAM_API,
    ERROR_RATE_LIMITED,
}

NON_COMPENSABLE_ERROR_CODES = {
    ERROR_INVALID_PAYOUT_AMOUNT,
    ERROR_EMPTY_MESSAGE_TEXT,
    ERROR_SCOPE_NOT_MATCHED,
    ERROR_ACTION_LIMIT_EXCEEDED,
    ERROR_REPLY_ANCHOR_MISSING,
    ERROR_PAYOUT_LIMIT_EXCEEDED,
}

_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "超时",
)
_NETWORK_MARKERS = (
    "connectionerror",
    "connectionreseterror",
    "connectionabortederror",
    "connectionrefusederror",
    "network",
    "socket",
    "disconnected",
    "连接",
    "网络",
)
_NON_AMBIGUOUS_TELEGRAM_MARKERS = (
    "floodwait",
    "flood wait",
    "peerflood",
    "rpcerror",
)


@dataclass(frozen=True, slots=True)
class PayoutErrorClassification:
    error_code: str
    should_enqueue: bool
    retryable: bool
    ambiguous: bool
    initial_delay_seconds: int


def ensure_payout_key(carrier: dict[str, Any]) -> str:
    """Return an existing key or inject a new stage-2 idempotency key."""

    value = _str_or_none(carrier.get("payout_key"))
    if value and len(value) <= PAYOUT_KEY_MAX_LENGTH:
        return value
    payout_key = f"{PAYOUT_KEY_PREFIX}{token_hex(8)}"
    carrier["payout_key"] = payout_key
    return payout_key


def normalize_payout_error_code(error_code: Any, error: Any = None) -> str:
    code = _str_or_none(error_code)
    text = f"{code or ''} {type(error).__name__ if error is not None else ''} {error or ''}".lower()
    if ERROR_RATE_LIMITED in text or "rate limited" in text:
        return ERROR_RATE_LIMITED
    if ERROR_PAYOUT_LIMIT_EXCEEDED in text or "payoutlimitexceeded" in text:
        return ERROR_PAYOUT_LIMIT_EXCEEDED
    if code:
        return code
    return "action_failed"


def classify_payout_error(error_code: Any, error: Any = None) -> PayoutErrorClassification:
    code = normalize_payout_error_code(error_code, error)
    retryable = code in RETRYABLE_ERROR_CODES
    should_enqueue = retryable
    delay = 0 if code == ERROR_USERBOT_OFFLINE else DEFAULT_RETRY_BACKOFF_SECONDS
    ambiguous = retryable and _is_ambiguous_error(error)
    return PayoutErrorClassification(
        error_code=code,
        should_enqueue=should_enqueue,
        retryable=retryable,
        ambiguous=ambiguous,
        initial_delay_seconds=delay,
    )


async def enqueue_payout_compensation(
    *,
    account_id: int,
    origin: str,
    payload: Mapping[str, Any],
    error_code: Any,
    error: Any = None,
    trace_id: str | None = None,
    plugin_key: str | None = None,
    entry_key: str | None = None,
    chat_id: int | None = None,
    amount: int | None = None,
    now: datetime | None = None,
) -> PayoutCompensation | None:
    """Create or update one compensation row for a retryable payout failure."""

    classification = classify_payout_error(error_code, error)
    if not classification.should_enqueue:
        return None

    created_at = now or datetime.now(UTC)
    payload_snapshot = _payload_snapshot(payload)
    payout_key = ensure_payout_key(payload_snapshot)
    context = _dict(payload_snapshot.get("context"))
    normalized_trace_id = _str_or_none(trace_id) or _str_or_none(context.get("trace_id")) or _str_or_none(
        payload_snapshot.get("trace_id")
    )
    normalized_plugin_key = _str_or_none(plugin_key) or _str_or_none(context.get("plugin_key")) or _str_or_none(
        payload_snapshot.get("plugin_key")
    )
    normalized_entry_key = _str_or_none(entry_key) or _str_or_none(context.get("entry_key")) or _str_or_none(
        payload_snapshot.get("entry_key")
    )
    target_chat_id = _int_or_none(chat_id) or _int_or_none(payload_snapshot.get("chat_id"))
    payout_amount = _int_or_none(amount) or _int_or_none(payload_snapshot.get("amount"))
    if target_chat_id is None or payout_amount is None:
        log.debug(
            "skip payout compensation enqueue: missing chat_id/amount account=%s payout_key=%s",
            account_id,
            payout_key,
        )
        return None

    next_attempt_at = created_at + timedelta(seconds=classification.initial_delay_seconds)
    error_text = _error_text(error)

    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(PayoutCompensation).where(PayoutCompensation.payout_key == payout_key))
        if existing is not None:
            existing.error_code_last = classification.error_code
            existing.error_last = error_text
            existing.ambiguous = bool(existing.ambiguous or classification.ambiguous)
            existing.updated_at = created_at
            await db.commit()
            await db.refresh(existing)
            return existing

        row = PayoutCompensation(
            payout_key=payout_key,
            account_id=int(account_id),
            trace_id=normalized_trace_id,
            plugin_key=normalized_plugin_key,
            entry_key=normalized_entry_key,
            origin=str(origin or "")[:32],
            chat_id=int(target_chat_id),
            amount=int(payout_amount),
            payload=payload_snapshot,
            status=PAYOUT_COMPENSATION_STATUS_PENDING,
            error_code_first=classification.error_code,
            error_code_last=classification.error_code,
            error_last=error_text,
            ambiguous=classification.ambiguous,
            retry_count=0,
            next_attempt_at=next_attempt_at,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.scalar(select(PayoutCompensation).where(PayoutCompensation.payout_key == payout_key))
            if existing is not None:
                existing.error_code_last = classification.error_code
                existing.error_last = error_text
                existing.ambiguous = bool(existing.ambiguous or classification.ambiguous)
                existing.updated_at = created_at
                await db.commit()
                await db.refresh(existing)
                return existing
            raise
        await db.refresh(row)
        return row


def _payload_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = dict(payload)
    snapshot.setdefault("action_type", "payout")
    amount = _int_or_none(snapshot.get("amount"))
    if amount is not None and not str(snapshot.get("text") or "").strip():
        snapshot["text"] = f"+{amount}"
    return snapshot


def _is_ambiguous_error(error: Any) -> bool:
    if error is None:
        return False
    text = f"{type(error).__name__}: {error}".lower()
    if any(marker in text for marker in _NON_AMBIGUOUS_TELEGRAM_MARKERS):
        return False
    return any(marker in text for marker in _TIMEOUT_MARKERS) or any(marker in text for marker in _NETWORK_MARKERS)


def _error_text(error: Any) -> str | None:
    if error in (None, ""):
        return None
    return f"{type(error).__name__}: {error}" if not isinstance(error, str) else error


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_config(value: Any) -> dict[str, Any]:
    """Normalize ``system_setting.payout_compensation`` with safe defaults."""

    config = dict(DEFAULT_CONFIG)
    if not isinstance(value, dict):
        return config
    config["enabled"] = bool(value.get("enabled", config["enabled"]))
    config["max_retries"] = _non_negative_int(value.get("max_retries"), config["max_retries"])
    config["backoff_base_seconds"] = _positive_int(
        value.get("backoff_base_seconds"),
        config["backoff_base_seconds"],
    )
    config["backoff_max_seconds"] = _positive_int(
        value.get("backoff_max_seconds"),
        config["backoff_max_seconds"],
    )
    config["scan_interval_seconds"] = _positive_int(
        value.get("scan_interval_seconds"),
        config["scan_interval_seconds"],
    )
    config["batch_size"] = _positive_int(value.get("batch_size"), config["batch_size"])
    config["ambiguous_probe"] = bool(value.get("ambiguous_probe", config["ambiguous_probe"]))
    config["replay_drop_reply_anchor"] = bool(
        value.get("replay_drop_reply_anchor", config["replay_drop_reply_anchor"])
    )
    notify_channel = _str_or_none(value.get("notify_channel"))
    config["notify_channel"] = notify_channel
    return config


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return int(default)


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "ERROR_ACTION_LIMIT_EXCEEDED",
    "ERROR_EMPTY_MESSAGE_TEXT",
    "ERROR_INVALID_PAYOUT_AMOUNT",
    "ERROR_PAYOUT_LIMIT_EXCEEDED",
    "ERROR_RATE_LIMITED",
    "ERROR_REPLY_ANCHOR_MISSING",
    "ERROR_SCOPE_NOT_MATCHED",
    "ERROR_TELEGRAM_API",
    "ERROR_USERBOT_OFFLINE",
    "NON_COMPENSABLE_ERROR_CODES",
    "DEFAULT_CONFIG",
    "PAYOUT_KEY_PREFIX",
    "PayoutErrorClassification",
    "RETRYABLE_ERROR_CODES",
    "SETTING_KEY",
    "classify_payout_error",
    "enqueue_payout_compensation",
    "ensure_payout_key",
    "normalize_config",
    "normalize_payout_error_code",
]
