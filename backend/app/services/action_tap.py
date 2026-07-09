"""Best-effort structured action tap.

The tap writes an append-only action ledger row and publishes the same payload
to the worker event channel. It must never decide delivery behavior.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..db.base import AsyncSessionLocal
from ..db.models.action_event import (
    ACTION_EVENT_STATUS_COMPENSATED,
    ACTION_EVENT_STATUS_DRY_RUN,
    ACTION_EVENT_STATUS_FAILED,
    ACTION_EVENT_STATUS_OK,
    ACTION_EVENT_STATUS_PENDING,
    ACTION_EVENT_STATUSES,
    ActionEvent,
)
from ..redis_client import get_redis
from ..worker.ipc import event_channel, make_event
from .redactor import redact_text, redact_value

log = logging.getLogger(__name__)

ACTION_TAP_EVENT_TYPE = "action_event"
ACTION_TAP_ERROR_LIMIT = 500
ACTION_TAP_TEXT_LIMIT = 500
ACTION_TAP_LIST_LIMIT = 20
ACTION_TAP_DICT_LIMIT = 40
_ACTION_TAP_CIRCUIT_SECONDS = 30.0
_DB_DISABLED_UNTIL = 0.0
_REDIS_DISABLED_UNTIL = 0.0
RECORDINGS_DIR = Path(__file__).resolve().parents[3] / "data" / "recordings"

_SUMMARY_KEYS = {
    "amount",
    "callback_query_id",
    "chat_id",
    "filename",
    "inline_query_id",
    "message_id",
    "message_id_key",
    "payout_key",
    "reply_to_message_id",
    "reply_to_search_limit",
    "reply_to_user_id",
    "save_message_id_key",
    "send_via",
    "send_via_options",
    "channel_selector",
    "show_alert",
    "text",
    "type",
}


def dev_mode_dry_run_enabled(config: Any) -> bool:
    """Return true only for ``{"dev_mode": {"dry_run": true}}`` style config."""

    if not isinstance(config, dict):
        return False
    raw = config.get("dev_mode")
    if isinstance(raw, dict):
        return bool(raw.get("dry_run", False))
    return False


def dev_mode_recording_enabled(config: Any) -> bool:
    """Return true only for ``{"dev_mode": {"recording": true}}`` style config."""

    if not isinstance(config, dict):
        return False
    raw = config.get("dev_mode")
    if isinstance(raw, dict):
        return bool(raw.get("recording", False))
    return False


def action_context(action: dict[str, Any] | None) -> dict[str, Any]:
    context = (action or {}).get("context")
    return dict(context) if isinstance(context, dict) else {}


def action_context_dry_run_enabled(action: dict[str, Any] | None) -> bool:
    """Read dry_run from action context without consulting external state."""

    context = action_context(action)
    return dev_mode_dry_run_enabled(context) or dev_mode_dry_run_enabled(context.get("account_config"))


async def emit_action_event(
    *,
    account_id: int | None,
    action: dict[str, Any] | None,
    status: str,
    channel: str | None = None,
    session_key: str | None = None,
    plugin_key: str | None = None,
    entry_key: str | None = None,
    error_code: str | None = None,
    error: Any = None,
    result: Any = None,
    redis: Any | None = None,
) -> ActionEvent | None:
    """Persist and publish one structured action event.

    All failures are swallowed after debug logging. ``record_action`` remains
    the trace source of truth; this tap is an additional structured ledger.
    """

    account = _int_or_none(account_id)
    if account is None:
        return None
    normalized_status = _normalize_status(status)
    if normalized_status is None:
        return None
    action_payload = dict(action or {})
    context = action_context(action_payload)
    action_type = str(action_payload.get("type") or action_payload.get("action_type") or "unknown").strip() or "unknown"
    resolved_channel = _first_text(
        channel,
        action_payload.get("actual_send_via"),
        action_payload.get("send_via"),
        action_payload.get("channel"),
    )
    resolved_session_key = _first_text(
        session_key,
        context.get("session_key"),
        action_payload.get("session_key"),
    )
    resolved_plugin_key = _first_text(plugin_key, context.get("plugin_key"))
    resolved_entry_key = _first_text(entry_key, context.get("entry_key"))
    error_summary = _error_summary(error)
    params_summary = summarize_action_params(action_payload, result=result)
    row = ActionEvent(
        account_id=account,
        channel=resolved_channel,
        session_key=resolved_session_key,
        plugin_key=resolved_plugin_key,
        entry_key=resolved_entry_key,
        action_type=action_type,
        params_summary=params_summary,
        status=normalized_status,
        error_code=_first_text(error_code),
        error_summary=error_summary,
    )
    persisted = await _persist_action_event(row)
    await _publish_action_event(
        account,
        row,
        redis=redis,
    )
    return persisted


async def emit_inbound_event(
    *,
    account_id: int | None,
    envelope: dict[str, Any] | None,
    account_config: dict[str, Any] | None = None,
    recordings_dir: Path | str | None = None,
) -> Path | None:
    """Append one normalized inbound envelope to the account recording JSONL."""

    account = _int_or_none(account_id)
    if account is None or not dev_mode_recording_enabled(account_config):
        return None
    if not isinstance(envelope, dict):
        return None
    try:
        root = Path(recordings_dir) if recordings_dir is not None else RECORDINGS_DIR
        account_dir = root / str(account)
        account_dir.mkdir(parents=True, exist_ok=True)
        path = account_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str))
            fh.write("\n")
        return path
    except Exception:  # noqa: BLE001
        log.debug("inbound recording write failed account=%s", account, exc_info=True)
        return None


def summarize_action_params(action: dict[str, Any] | None, *, result: Any = None) -> dict[str, Any]:
    """Return a compact, redacted, JSON-safe summary of action params."""

    payload = dict(action or {})
    summary: dict[str, Any] = {}
    for key in sorted(_SUMMARY_KEYS):
        if key not in payload:
            continue
        summary[key] = _json_safe(payload.get(key), key=key)
    if "amount" in payload:
        summary["amount"] = _amount_text(payload.get("amount"))
    if result is not None:
        result_summary = _result_summary(result)
        if result_summary:
            summary["result"] = result_summary
    return redact_value(summary, drop_sensitive_keys=True)


async def _persist_action_event(row: ActionEvent) -> ActionEvent | None:
    global _DB_DISABLED_UNTIL
    now = time.monotonic()
    if _DB_DISABLED_UNTIL > now:
        return None
    try:
        async with AsyncSessionLocal() as db:
            db.add(row)
            await db.commit()
            return row
    except Exception:  # noqa: BLE001
        _DB_DISABLED_UNTIL = time.monotonic() + _ACTION_TAP_CIRCUIT_SECONDS
        log.debug("action tap DB write failed", exc_info=True)
        return None


async def _publish_action_event(account_id: int, row: ActionEvent, *, redis: Any | None = None) -> None:
    global _REDIS_DISABLED_UNTIL
    now = time.monotonic()
    if redis is None and _REDIS_DISABLED_UNTIL > now:
        return
    payload = {
        "id": getattr(row, "id", None),
        "account_id": row.account_id,
        "channel": row.channel,
        "session_key": row.session_key,
        "plugin_key": row.plugin_key,
        "entry_key": row.entry_key,
        "action_type": row.action_type,
        "params_summary": row.params_summary,
        "status": row.status,
        "error_code": row.error_code,
        "error_summary": row.error_summary,
    }
    try:
        client = redis or get_redis()
        await client.publish(event_channel(account_id), make_event(ACTION_TAP_EVENT_TYPE, **payload))
    except Exception:  # noqa: BLE001
        if redis is None:
            _REDIS_DISABLED_UNTIL = time.monotonic() + _ACTION_TAP_CIRCUIT_SECONDS
        log.debug("action tap redis publish failed account=%s", account_id, exc_info=True)


def _normalize_status(status: str) -> str | None:
    value = str(status or "").strip().upper()
    if value == "SUCCESS":
        value = ACTION_EVENT_STATUS_OK
    elif value == "ERROR":
        value = ACTION_EVENT_STATUS_FAILED
    if value not in ACTION_EVENT_STATUSES:
        return None
    return value


def _result_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("message_id", "chat_id", "reply_to_message_id", "reply_to_user_id", "error_code", "not_modified"):
        if key in result:
            out[key] = _json_safe(result.get(key), key=key)
    return out


def _json_safe(value: Any, *, key: str | None = None) -> Any:
    if key == "amount":
        return _amount_text(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return redact_text(value[:ACTION_TAP_TEXT_LIMIT])
    if isinstance(value, (int, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_safe(item) for item in value[:ACTION_TAP_LIST_LIMIT]]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in list(value)[:ACTION_TAP_LIST_LIMIT]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (raw_key, raw_item) in enumerate(value.items()):
            if index >= ACTION_TAP_DICT_LIMIT:
                break
            item_key = str(raw_key)
            out[item_key] = _json_safe(raw_item, key=item_key)
        return out
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)[:ACTION_TAP_TEXT_LIMIT]


def _amount_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _error_summary(error: Any) -> str | None:
    if error in (None, ""):
        return None
    return redact_text(str(error))[:ACTION_TAP_ERROR_LIMIT]


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ACTION_EVENT_STATUS_COMPENSATED",
    "ACTION_EVENT_STATUS_DRY_RUN",
    "ACTION_EVENT_STATUS_FAILED",
    "ACTION_EVENT_STATUS_OK",
    "ACTION_EVENT_STATUS_PENDING",
    "ACTION_TAP_EVENT_TYPE",
    "action_context",
    "action_context_dry_run_enabled",
    "dev_mode_dry_run_enabled",
    "dev_mode_recording_enabled",
    "emit_action_event",
    "emit_inbound_event",
    "summarize_action_params",
]
