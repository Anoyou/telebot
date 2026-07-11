"""Structured action tap with explicit degraded-state observability.

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

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.base import AsyncSessionLocal
from ..db.models.action_event import (
    ACTION_EVENT_COUNTABLE_PAYOUT_STATUSES,
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
_DB_WRITE_FAILURES = 0
_DB_DROPPED_EVENTS = 0
_DB_LAST_ERROR: str | None = None
RECORDINGS_DIR = Path(__file__).resolve().parents[3] / "data" / "recordings"
# session.info key：按事务层级分层的 publish 队列栈。
# stack[0] = 最外层事务；stack[-1] = 当前 SAVEPOINT 层。
# nested commit → 合并到父层；nested rollback → 丢弃该层；outer commit → 发布。
_PENDING_ACTION_EVENT_STACK = "telepilot_pending_action_event_stack"

_SUMMARY_KEYS = {
    "amount",
    "callback_query_id",
    "chat_id",
    "chat_title",
    "filename",
    "inline_query_id",
    "message_id",
    "message_id_key",
    "payout_key",
    "paid_user_ids",
    "participant_user_ids",
    "payer_name",
    "payer_username",
    "payer_user_id",
    "player_user_ids",
    "reply_to_message_id",
    "reply_to_search_limit",
    "reply_to_user_id",
    "receiver_name",
    "receiver_username",
    "receiver_user_id",
    "save_message_id_key",
    "send_via",
    "send_via_options",
    "session_key",
    "started_by_user_id",
    "channel_selector",
    "entry_key",
    "event_type",
    "module_key",
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


def _extract_payout_key(action: dict[str, Any] | None, result: Any = None) -> str | None:
    payload = dict(action or {})
    for candidate in (
        payload.get("payout_key"),
        (result or {}).get("payout_key") if isinstance(result, dict) else None,
    ):
        text = str(candidate or "").strip()
        if text:
            return text[:80]
    return None


def _session_info_target(session: Any) -> dict[str, Any]:
    """Resolve the sync Session.info used by AsyncSession wrappers."""

    sync_session = getattr(session, "sync_session", None)
    if sync_session is not None:
        return sync_session.info
    return session.info


def _publish_payload_from_row(row: ActionEvent, *, redis: Any | None = None) -> dict[str, Any]:
    return {
        "account_id": int(row.account_id),
        "row_id": getattr(row, "id", None),
        "channel": row.channel,
        "session_key": row.session_key,
        "plugin_key": row.plugin_key,
        "entry_key": row.entry_key,
        "action_type": row.action_type,
        "params_summary": dict(row.params_summary or {}),
        "status": row.status,
        "error_code": row.error_code,
        "error_summary": row.error_summary,
        "payout_key": row.payout_key,
        "redis": redis,
    }


def _pending_stack(session_or_info: Any) -> list[list[dict[str, Any]]]:
    info = session_or_info if isinstance(session_or_info, dict) else _session_info_target(session_or_info)
    stack = info.get(_PENDING_ACTION_EVENT_STACK)
    if not isinstance(stack, list):
        stack = []
        info[_PENDING_ACTION_EVENT_STACK] = stack
    return stack


def _ensure_root_pending_layer(session: Any) -> list[dict[str, Any]]:
    """Ensure at least the outermost layer exists for events outside begin_nested."""

    stack = _pending_stack(session)
    if not stack:
        stack.append([])
    return stack[-1]


def _schedule_action_event_publish(
    session: Any,
    row: ActionEvent,
    *,
    redis: Any | None = None,
) -> None:
    """Queue Redis publish on the current transaction layer."""

    layer = _ensure_root_pending_layer(session)
    layer.append(_publish_payload_from_row(row, redis=redis))


def _drain_pending_payloads(pending: list[dict[str, Any]]) -> None:
    if not pending:
        return
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        for item in pending:
            try:
                import asyncio as _asyncio

                _asyncio.run(_publish_action_event_payload(item))
            except Exception:  # noqa: BLE001
                log.debug("action tap deferred publish failed", exc_info=True)
        return
    for item in pending:
        loop.create_task(_publish_action_event_payload(item))


def _install_action_event_after_commit_hooks() -> None:
    """Layered pending queue: nested rollback drops only that layer's events.

    - ``after_begin(nested)``: push a new empty layer.
    - ``after_commit`` with layers > 1: pop nested layer and merge into parent
      (SAVEPOINT released successfully).
    - ``after_commit`` with single layer: outermost commit → publish all.
    - ``after_rollback`` with layers > 1: pop and discard nested layer.
    - ``after_rollback`` with single/empty layer: outer rollback → clear all.
    """

    if getattr(Session, "_telepilot_action_event_hooks", False):
        return

    @sa_event.listens_for(Session, "after_begin")
    def _after_begin(session: Session, transaction: Any, connection: Any) -> None:  # noqa: ARG001
        stack = _pending_stack(session)
        if getattr(transaction, "nested", False):
            stack.append([])
        elif not stack:
            # Root transaction begin: ensure a root layer exists.
            stack.append([])

    @sa_event.listens_for(Session, "after_commit")
    def _after_commit(session: Session) -> None:
        stack = _pending_stack(session)
        if len(stack) > 1:
            # Nested SAVEPOINT commit: merge child events into parent, do not publish.
            child = stack.pop()
            stack[-1].extend(child)
            return
        # Outermost commit.
        pending = list(stack.pop()) if stack else []
        session.info.pop(_PENDING_ACTION_EVENT_STACK, None)
        _drain_pending_payloads(pending)

    @sa_event.listens_for(Session, "after_rollback")
    def _after_rollback(session: Session) -> None:
        stack = _pending_stack(session)
        if len(stack) > 1:
            # Nested SAVEPOINT rollback: drop only this layer's queued events.
            stack.pop()
            return
        # Outermost rollback (or empty): discard everything.
        session.info.pop(_PENDING_ACTION_EVENT_STACK, None)

    Session._telepilot_action_event_hooks = True  # type: ignore[attr-defined]


_install_action_event_after_commit_hooks()


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
    db: Any | None = None,
) -> ActionEvent | None:
    """Persist and publish one structured action event.

    Delivery is not blocked by tap failures, but persistence failures are
    surfaced at ERROR level and counted so this append-only view cannot be
    mistaken for a reliable ledger while it is degraded.

    When ``db`` is provided the row is added to that session without commit
    (caller owns the transaction). Redis publish is deferred until after the
    session successfully commits, so rollbacks never emit ghost events.
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
    payout_key = _extract_payout_key(action_payload, result)
    if payout_key and "payout_key" not in params_summary:
        params_summary["payout_key"] = payout_key
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
        payout_key=payout_key,
    )
    if db is not None:
        db.add(row)
        await db.flush()
        _schedule_action_event_publish(db, row, redis=redis)
        return row
    persisted = await _persist_action_event(row)
    if persisted is not None:
        await _publish_action_event(account, persisted, redis=redis)
    return persisted


async def find_payout_ledger_event(
    db: Any,
    *,
    account_id: int | None = None,
    payout_key: str,
) -> ActionEvent | None:
    """Return an existing ledger-countable payout ActionEvent for ``payout_key``."""

    key = str(payout_key or "").strip()
    if not key:
        return None
    stmt = select(ActionEvent).where(
        ActionEvent.payout_key == key,
        ActionEvent.action_type == "payout",
        ActionEvent.status.in_(tuple(ACTION_EVENT_COUNTABLE_PAYOUT_STATUSES)),
    )
    if account_id is not None:
        stmt = stmt.where(ActionEvent.account_id == int(account_id))
    stmt = stmt.order_by(ActionEvent.id.desc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def emit_compensated_payout_event(
    *,
    account_id: int,
    payout_key: str,
    amount: Any,
    chat_id: int | None = None,
    plugin_key: str | None = None,
    entry_key: str | None = None,
    channel: str | None = "userbot_reply",
    result: Any = None,
    compensation_source: str | None = None,
    previous_error_code: str | None = None,
    db: Any | None = None,
    redis: Any | None = None,
    action: dict[str, Any] | None = None,
) -> ActionEvent | None:
    """Idempotently write one COMPENSATED payout ActionEvent for the ledger.

    Uses the indexed ``payout_key`` column + partial unique constraint so
    concurrent writers cannot double-count. Redis publish is deferred until
    the owning transaction commits.
    """

    key = str(payout_key or "").strip()
    if not key or account_id is None:
        return None

    action_payload = dict(action or {})
    action_payload.setdefault("type", "payout")
    action_payload.setdefault("action_type", "payout")
    action_payload["payout_key"] = key
    if amount is not None and action_payload.get("amount") in (None, ""):
        action_payload["amount"] = amount
    if chat_id is not None and action_payload.get("chat_id") in (None, ""):
        action_payload["chat_id"] = chat_id
    result_payload = dict(result or {})
    result_payload.setdefault("payout_key", key)
    if compensation_source:
        result_payload.setdefault("compensation_source", compensation_source)
    if previous_error_code:
        result_payload.setdefault("previous_error_code", previous_error_code)

    async def _write(session: Any) -> ActionEvent | None:
        existing = await find_payout_ledger_event(session, account_id=int(account_id), payout_key=key)
        if existing is not None:
            return existing
        try:
            async with session.begin_nested():
                return await emit_action_event(
                    account_id=int(account_id),
                    action=action_payload,
                    status=ACTION_EVENT_STATUS_COMPENSATED,
                    channel=channel,
                    plugin_key=plugin_key or action_payload.get("plugin_key"),
                    entry_key=entry_key or action_payload.get("entry_key"),
                    result=result_payload,
                    redis=redis,
                    db=session,
                )
        except IntegrityError:
            # 并发下 partial unique 命中：回滚 savepoint 后返回已存在行。
            existing = await find_payout_ledger_event(session, account_id=int(account_id), payout_key=key)
            if existing is not None:
                return existing
            raise

    if db is not None:
        return await _write(db)
    try:
        async with AsyncSessionLocal() as session:
            row = await _write(session)
            await session.commit()
            return row
    except Exception:  # noqa: BLE001
        log.error(
            "补偿 ActionEvent 写入失败 account=%s payout_key=%s",
            account_id,
            key,
            exc_info=True,
        )
        return None


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
    global _DB_DISABLED_UNTIL, _DB_DROPPED_EVENTS, _DB_LAST_ERROR, _DB_WRITE_FAILURES
    now = time.monotonic()
    if _DB_DISABLED_UNTIL > now:
        _DB_DROPPED_EVENTS += 1
        return None
    try:
        async with AsyncSessionLocal() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row
    except IntegrityError:
        # 可计账 payout 唯一约束冲突：返回已有行，不计入 dropped。
        if row.payout_key and row.action_type == "payout":
            try:
                async with AsyncSessionLocal() as db:
                    existing = await find_payout_ledger_event(db, payout_key=str(row.payout_key))
                    if existing is not None:
                        return existing
            except Exception:  # noqa: BLE001
                log.debug("lookup existing payout ActionEvent after integrity error failed", exc_info=True)
        _DB_WRITE_FAILURES += 1
        _DB_DROPPED_EVENTS += 1
        _DB_LAST_ERROR = "IntegrityError"
        _DB_DISABLED_UNTIL = time.monotonic() + _ACTION_TAP_CIRCUIT_SECONDS
        log.error("ActionEvent 唯一约束冲突且无法读取已有行 payout_key=%s", row.payout_key, exc_info=True)
        return None
    except Exception as exc:  # noqa: BLE001
        _DB_WRITE_FAILURES += 1
        _DB_DROPPED_EVENTS += 1
        _DB_LAST_ERROR = f"{type(exc).__name__}: {exc}"[:ACTION_TAP_ERROR_LIMIT]
        _DB_DISABLED_UNTIL = time.monotonic() + _ACTION_TAP_CIRCUIT_SECONDS
        log.error(
            "ActionEvent 持久化失败，结构化资金视图已降级；dropped=%s failures=%s",
            _DB_DROPPED_EVENTS,
            _DB_WRITE_FAILURES,
            exc_info=True,
        )
        return None


def action_tap_health() -> dict[str, Any]:
    """Return process-local persistence health for diagnostics/tests."""

    now = time.monotonic()
    return {
        "db_available": _DB_DISABLED_UNTIL <= now,
        "db_write_failures": _DB_WRITE_FAILURES,
        "db_dropped_events": _DB_DROPPED_EVENTS,
        "db_last_error": _DB_LAST_ERROR,
        "db_retry_after_seconds": max(0.0, _DB_DISABLED_UNTIL - now),
    }


async def _publish_action_event_payload(item: dict[str, Any]) -> None:
    global _REDIS_DISABLED_UNTIL
    now = time.monotonic()
    redis = item.get("redis")
    if redis is None and _REDIS_DISABLED_UNTIL > now:
        return
    account_id = int(item["account_id"])
    payload = {
        "id": item.get("row_id"),
        "account_id": account_id,
        "channel": item.get("channel"),
        "session_key": item.get("session_key"),
        "plugin_key": item.get("plugin_key"),
        "entry_key": item.get("entry_key"),
        "action_type": item.get("action_type"),
        "params_summary": item.get("params_summary"),
        "status": item.get("status"),
        "error_code": item.get("error_code"),
        "error_summary": item.get("error_summary"),
        "payout_key": item.get("payout_key"),
    }
    try:
        client = redis or get_redis()
        await client.publish(event_channel(account_id), make_event(ACTION_TAP_EVENT_TYPE, **payload))
    except Exception:  # noqa: BLE001
        if redis is None:
            _REDIS_DISABLED_UNTIL = time.monotonic() + _ACTION_TAP_CIRCUIT_SECONDS
        log.debug("action tap redis publish failed account=%s", account_id, exc_info=True)


async def _publish_action_event(account_id: int, row: ActionEvent, *, redis: Any | None = None) -> None:
    await _publish_action_event_payload(
        {
            "account_id": account_id,
            "row_id": getattr(row, "id", None),
            "channel": row.channel,
            "session_key": row.session_key,
            "plugin_key": row.plugin_key,
            "entry_key": row.entry_key,
            "action_type": row.action_type,
            "params_summary": dict(row.params_summary or {}),
            "status": row.status,
            "error_code": row.error_code,
            "error_summary": row.error_summary,
            "payout_key": row.payout_key,
            "redis": redis,
        }
    )


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
    for key in (
        "message_id",
        "chat_id",
        "reply_to_message_id",
        "reply_to_user_id",
        "error_code",
        "not_modified",
        "participant_user_ids",
        "paid_user_ids",
        "player_user_ids",
        "started_by_user_id",
        "amount",
        "session_key",
        "payout_key",
        "compensation_source",
        "previous_error_code",
        "manual_note",
        "replay_recovered",
        "ambiguous_probe",
        "post_send_bookkeeping_failed",
    ):
        if key in result and result.get(key) is not None:
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
    "emit_compensated_payout_event",
    "emit_inbound_event",
    "find_payout_ledger_event",
    "summarize_action_params",
]
