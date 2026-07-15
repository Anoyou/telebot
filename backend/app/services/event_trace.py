"""Trace service for Telegram event lifecycles.

Trace writes must never become part of the critical Telegram/plugin path.  Every
public helper catches storage failures and returns a best-effort context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm.attributes import flag_modified

from ..db.base import AsyncSessionLocal
from ..db.models.log import LEVEL_ERROR, EventAction, EventSpan, EventTrace, PluginRuntimeStatus, RuntimeLog
from ..db.models.system import SystemSetting
from .event_bus import EVENT_REASON_CODES
from .redactor import redact_text, redact_value

log = logging.getLogger(__name__)

TRACE_ID_PREFIX = "evt_"
SPAN_ID_PREFIX = "spn_"
ACTION_ID_PREFIX = "act_"
TEXT_PREVIEW_LIMIT = 240
SNAPSHOT_TEXT_LIMIT = 1200
SNAPSHOT_LIST_LIMIT = 80
SNAPSHOT_DICT_LIMIT = 120
TRACE_WRITE_QUEUE_MAX_SIZE = 5000
TRACE_WRITE_BATCH_SIZE = 200
TRACE_WRITE_BATCH_INTERVAL_SECONDS = 0.2
TRACE_WRITE_DRAIN_TIMEOUT_SECONDS = 5.0

TRACE_STATUS_RUNNING = "running"
TRACE_STATUS_OK = "ok"
TRACE_STATUS_SKIPPED = "skipped"
TRACE_STATUS_WARNING = "warning"
TRACE_STATUS_FAILED = "failed"
TRACE_WRITE_FAILED_REASON_CODE = "trace_write_failed"
assert TRACE_WRITE_FAILED_REASON_CODE in EVENT_REASON_CODES


@dataclass(slots=True)
class TraceContext:
    trace_id: str
    account_id: int | None = None
    event_type: str = "message"
    source_channel: str | None = None
    started_at: float = 0.0


@dataclass(slots=True)
class _TraceWrite:
    kind: str
    payload: Any
    dedupe: bool = False


_TRACE_WRITE_QUEUE: asyncio.Queue[_TraceWrite] | None = None
_TRACE_WRITE_TASK: asyncio.Task[None] | None = None
_TRACE_WRITE_DROPPED = 0
_NATIVE_RAW_TRACE_POLICY_DEFAULTS = {"persist_enabled": False, "retention_days": 1}
_NATIVE_RAW_TRACE_POLICY_CACHE: dict[str, Any] = dict(_NATIVE_RAW_TRACE_POLICY_DEFAULTS)


async def start_trace(event: dict[str, Any] | Any) -> TraceContext:
    """Create an event trace and return the context used by downstream spans."""

    payload = event if isinstance(event, dict) else _object_payload(event)
    raw_trace_id = str(payload.get("trace_id") or "").strip()
    trace_id = _trace_id(raw_trace_id)
    source = _dict(payload.get("source"))
    message = _dict(payload.get("message"))
    chat = _dict(payload.get("chat"))
    sender = _dict(payload.get("sender") or payload.get("source_actor"))
    raw = _dict(payload.get("raw"))
    native_raw_meta = _dict(payload.get("native_raw_meta"))
    now = datetime.now(UTC)
    ctx = TraceContext(
        trace_id=trace_id,
        account_id=_int_or_none(source.get("account_id") or payload.get("account_id")),
        event_type=str(source.get("type") or payload.get("event_type") or "message"),
        source_channel=str(source.get("channel") or source.get("bot_role") or "") or None,
        started_at=time.time(),
    )
    native_raw_policy = _cached_native_raw_trace_policy()
    native_raw_in_trace = bool(native_raw_policy["persist_enabled"] and payload.get("native_raw") is not None)
    native_raw_meta_row = redact_payload_snapshot(native_raw_meta) if native_raw_meta else None
    if isinstance(native_raw_meta_row, dict):
        native_raw_meta_row["stored_in_trace"] = native_raw_in_trace
        native_raw_meta_row["retention_days"] = native_raw_policy["retention_days"]
    row = EventTrace(
        trace_id=trace_id,
        account_id=ctx.account_id,
        source_channel=ctx.source_channel,
        event_type=ctx.event_type,
        chat_id=_int_or_none(message.get("chat_id") or chat.get("id") or source.get("chat_id") or payload.get("chat_id")),
        message_id=_int_or_none(message.get("message_id") or source.get("message_id") or payload.get("message_id")),
        update_id=_int_or_none(source.get("update_id") or payload.get("source_update_id")),
        callback_query_id=str(source.get("callback_query_id") or payload.get("callback_query_id") or "") or None,
        sender_user_id=_int_or_none(sender.get("user_id") or payload.get("sender_user_id")),
        sender_name=str(sender.get("display_name") or payload.get("sender_name") or "")[:256] or None,
        text_preview=redact_text(str(message.get("text") or payload.get("message_text") or "")[:TEXT_PREVIEW_LIMIT]) or None,
        status=TRACE_STATUS_RUNNING,
        started_at=now,
        raw_summary=redact_payload_snapshot(raw) if raw else None,
        payload_snapshot=redact_payload_snapshot(payload, include_native_raw=native_raw_in_trace),
        native_raw_meta=native_raw_meta_row,
    )
    await _enqueue_trace_write(
        "trace",
        row,
        trace_id=trace_id,
        account_id=ctx.account_id,
        phase="start",
        dedupe=bool(raw_trace_id),
    )
    return ctx


async def record_span(
    trace: TraceContext | dict[str, Any] | str | None,
    phase: str,
    status: str = TRACE_STATUS_OK,
    **detail: Any,
) -> EventSpan | None:
    """Record a completed span for a trace."""

    trace_id = _context_trace_id(trace)
    if not trace_id:
        return None
    duration_ms = _int_or_none(detail.pop("duration_ms", None))
    span = EventSpan(
        span_id=_new_id(SPAN_ID_PREFIX),
        trace_id=trace_id,
        parent_span_id=_str_or_none(detail.pop("parent_span_id", None)),
        phase=str(phase or "unknown"),
        component=_str_or_none(detail.pop("component", None)),
        plugin_key=_str_or_none(detail.pop("plugin_key", None)),
        entry_key=_str_or_none(detail.pop("entry_key", None)),
        status=str(status or TRACE_STATUS_OK),
        reason_code=_str_or_none(detail.pop("reason_code", None)),
        message=_str_or_none(detail.pop("message", None)),
        detail=redact_payload_snapshot(detail) if detail else None,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        duration_ms=duration_ms,
    )
    await _enqueue_trace_write("span", span, trace_id=trace_id, account_id=None, phase=str(phase or "unknown"))
    return span


async def record_action(
    trace: TraceContext | dict[str, Any] | str | None,
    action: dict[str, Any],
    status: str = "pending",
    **detail: Any,
) -> EventAction | None:
    """Record a plugin action request or delivery result."""

    trace_id = _context_trace_id(trace) or _context_trace_id(_dict(action.get("context")))
    if not trace_id:
        return None
    action_type = str(action.get("type") or detail.pop("action_type", "") or "unknown")
    requested_send_via = action.get("send_via_options") or action.get("send_via") or action.get("channel_selector")
    result = _dict(detail.get("result"))
    row = EventAction(
        action_id=_new_id(ACTION_ID_PREFIX),
        trace_id=trace_id,
        plugin_key=_str_or_none(detail.pop("plugin_key", None) or _dict(action.get("context")).get("plugin_key")),
        action_type=action_type,
        requested_send_via=_compact_json(requested_send_via),
        actual_send_via=_str_or_none(detail.pop("actual_send_via", None) or action.get("send_via")),
        target_chat_id=_int_or_none(action.get("chat_id") or detail.pop("target_chat_id", None)),
        target_message_id=_int_or_none(action.get("message_id") or action.get("reply_to_message_id") or detail.pop("target_message_id", None)),
        status=str(status or "pending"),
        telegram_message_id=_int_or_none(detail.pop("telegram_message_id", None) or result.get("message_id")),
        inline_result_count=_inline_result_count(action),
        error_code=_str_or_none(detail.pop("error_code", None)),
        error_message=_str_or_none(detail.pop("error_message", None) or detail.pop("error", None)),
        detail=redact_payload_snapshot({"action": action, **detail}),
    )
    await _enqueue_trace_write(
        "action",
        row,
        trace_id=trace_id,
        account_id=None,
        phase="action",
        action_type=action_type,
    )
    return row


async def finish_trace(
    trace: TraceContext | dict[str, Any] | str | None,
    status: str = TRACE_STATUS_OK,
    **summary: Any,
) -> None:
    """Mark a trace as completed."""

    trace_id = _context_trace_id(trace)
    if not trace_id:
        return
    ended_at = datetime.now(UTC)
    duration_ms = _int_or_none(summary.pop("duration_ms", None))
    if duration_ms is None and isinstance(trace, TraceContext) and trace.started_at:
        duration_ms = max(0, int((time.time() - trace.started_at) * 1000))
    values: dict[str, Any] = {
        "status": str(status or TRACE_STATUS_OK),
        "ended_at": ended_at,
        "duration_ms": duration_ms,
    }
    await _enqueue_trace_write(
        "finish",
        {"trace_id": trace_id, "values": values, "summary": dict(summary or {})},
        trace_id=trace_id,
        account_id=None,
        phase="finish",
    )


async def refresh_trace_settings() -> dict[str, Any]:
    """Refresh cached trace settings outside the message critical path."""

    global _NATIVE_RAW_TRACE_POLICY_CACHE
    try:
        async with AsyncSessionLocal() as db:
            _NATIVE_RAW_TRACE_POLICY_CACHE = await _native_raw_trace_policy(db)
    except Exception:  # noqa: BLE001
        log.debug("refresh trace settings failed, using cached/default policy", exc_info=True)
    return dict(_NATIVE_RAW_TRACE_POLICY_CACHE)


async def flush_trace_writes(timeout: float = TRACE_WRITE_DRAIN_TIMEOUT_SECONDS) -> None:
    """Wait until queued trace writes have been persisted.

    This is intentionally not used by the hot path. It exists for tests and
    graceful shutdown so normal message handling can remain latency-free.
    """

    queue = _TRACE_WRITE_QUEUE
    if queue is None:
        return
    if not _queue_uses_current_loop(queue):
        return
    await asyncio.wait_for(queue.join(), timeout=max(0.1, float(timeout or TRACE_WRITE_DRAIN_TIMEOUT_SECONDS)))


async def stop_trace_writer(timeout: float = TRACE_WRITE_DRAIN_TIMEOUT_SECONDS) -> None:
    """Flush queued trace writes and stop the background writer task."""

    global _TRACE_WRITE_QUEUE, _TRACE_WRITE_TASK
    await flush_trace_writes(timeout=timeout)
    task = _TRACE_WRITE_TASK
    if task is None:
        _TRACE_WRITE_QUEUE = None
        return
    if not _task_uses_current_loop(task):
        task.cancel()
        _TRACE_WRITE_TASK = None
        _TRACE_WRITE_QUEUE = None
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        _TRACE_WRITE_TASK = None
        _TRACE_WRITE_QUEUE = None


def trace_writer_stats() -> dict[str, int]:
    queue = _TRACE_WRITE_QUEUE
    return {
        "queued": queue.qsize() if queue is not None else 0,
        "dropped": _TRACE_WRITE_DROPPED,
    }


def trace_log_context(
    trace: TraceContext | dict[str, Any] | str | None,
    plugin_key: str | None = None,
    entry_key: str | None = None,
) -> dict[str, Any]:
    """Return fields that should be copied into runtime/plugin logs."""

    trace_id = _context_trace_id(trace)
    out: dict[str, Any] = {}
    if trace_id:
        out["trace_id"] = trace_id
    if plugin_key:
        out["plugin_key"] = plugin_key
    if entry_key:
        out["entry_key"] = entry_key
    return out


def redact_payload_snapshot(payload: Any, *, include_native_raw: bool = False) -> Any:
    """Make a JSON-compatible, redacted payload snapshot.

    Full ``native_raw`` is intentionally removed from trace snapshots.  Its
    metadata remains visible through ``native_raw_meta``.
    """

    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in list(payload.items())[:SNAPSHOT_DICT_LIMIT]:
            k = str(key)
            if k == "native_raw":
                out[k] = redact_payload_snapshot(value, include_native_raw=include_native_raw) if include_native_raw else "[omitted]"
                continue
            out[k] = redact_payload_snapshot(value, include_native_raw=include_native_raw)
        return redact_value(out)
    if isinstance(payload, list):
        return [redact_payload_snapshot(item, include_native_raw=include_native_raw) for item in payload[:SNAPSHOT_LIST_LIMIT]]
    if isinstance(payload, tuple):
        return [redact_payload_snapshot(item, include_native_raw=include_native_raw) for item in list(payload)[:SNAPSHOT_LIST_LIMIT]]
    if isinstance(payload, set):
        return [redact_payload_snapshot(item, include_native_raw=include_native_raw) for item in list(payload)[:SNAPSHOT_LIST_LIMIT]]
    if isinstance(payload, str):
        return redact_text(payload[:SNAPSHOT_TEXT_LIMIT])
    if isinstance(payload, (int, float, bool)) or payload is None:
        return payload
    if isinstance(payload, datetime):
        return payload.isoformat()
    return redact_text(str(payload)[:SNAPSHOT_TEXT_LIMIT])


async def update_plugin_runtime_status(
    *,
    account_id: int | None,
    plugin_key: str,
    enabled: bool | None = None,
    installed_version: str | None = None,
    load_status: str | None = None,
    last_load_error: str | None = None,
    last_invocation_status: str | None = None,
    last_trace_id: str | None = None,
) -> None:
    """Best-effort upsert for plugin diagnostics."""

    key = str(plugin_key or "").strip()
    if not key:
        return
    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(PluginRuntimeStatus).where(
                        PluginRuntimeStatus.account_id == account_id,
                        PluginRuntimeStatus.plugin_key == key,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = PluginRuntimeStatus(
                    account_id=account_id,
                    plugin_key=key,
                    enabled=bool(enabled),
                    installed_version=installed_version,
                    load_status=load_status or "unknown",
                )
                db.add(row)
            if enabled is not None:
                row.enabled = bool(enabled)
            if installed_version is not None:
                row.installed_version = installed_version
            if load_status is not None:
                row.load_status = load_status
            if last_load_error is not None:
                row.last_load_error = redact_text(str(last_load_error))
            elif load_status is not None and str(load_status).lower() in {"active", "loaded", "ok"}:
                row.last_load_error = None
            if last_invocation_status is not None:
                row.last_invocation_status = last_invocation_status
                row.last_invoked_at = datetime.now(UTC)
            if last_trace_id is not None:
                row.last_trace_id = last_trace_id
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("plugin runtime status update failed plugin=%s account=%s", key, account_id, exc_info=True)


async def cleanup_event_traces(
    *,
    trace_retention_days: int = 30,
    payload_snapshot_retention_days: int = 7,
    native_raw_retention_days: int = 1,
) -> dict[str, int]:
    """Prune old trace rows and clear expired heavy snapshots.

    ``native_raw`` is not persisted by default; this cleanup keeps the main trace
    row for the shorter payload-retention window and deletes full trace/span/action
    rows only after the longer trace retention window.
    """

    trace_days = max(0, int(trace_retention_days or 0))
    payload_days = max(0, int(payload_snapshot_retention_days or 0))
    native_raw_days = max(0, int(native_raw_retention_days or 0))
    deleted_traces = 0
    cleared_payloads = 0
    cleared_native_raw = 0
    now = datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as db:
            if native_raw_days > 0:
                native_raw_cutoff = now - timedelta(days=native_raw_days)
                rows = (
                    await db.execute(
                        select(EventTrace).where(
                            EventTrace.started_at < native_raw_cutoff,
                            EventTrace.payload_snapshot.is_not(None),
                        )
                    )
                ).scalars().all()
                for row in rows:
                    if _clear_native_raw_snapshot(row):
                        cleared_native_raw += 1
                        flag_modified(row, "payload_snapshot")
                        flag_modified(row, "native_raw_meta")
            if payload_days > 0:
                payload_cutoff = now - timedelta(days=payload_days)
                result = await db.execute(
                    update(EventTrace)
                    .where(
                        EventTrace.started_at < payload_cutoff,
                        EventTrace.payload_snapshot.is_not(None),
                    )
                    .values(payload_snapshot=None)
                )
                cleared_payloads = int(result.rowcount or 0)
            if trace_days > 0:
                trace_cutoff = now - timedelta(days=trace_days)
                result = await db.execute(delete(EventTrace).where(EventTrace.started_at < trace_cutoff))
                deleted_traces = int(result.rowcount or 0)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("event trace cleanup failed", exc_info=True)
        await _write_trace_runtime_error(
            "event trace cleanup failed",
            trace_id=None,
            account_id=None,
            phase="cleanup",
        )
    return {
        "deleted_traces": deleted_traces,
        "cleared_payload_snapshots": cleared_payloads,
        "cleared_native_raw": cleared_native_raw,
    }


async def _native_raw_trace_policy(db: Any) -> dict[str, Any]:
    """Read native_raw persistence settings without making Trace writes critical."""

    defaults = {"persist_enabled": False, "retention_days": 1}
    try:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == "log_retention"))
        row = result.scalar_one_or_none()
        raw = row.value if row is not None and isinstance(row.value, dict) else {}
        return {
            "persist_enabled": bool(raw.get("native_raw_persist_enabled", defaults["persist_enabled"])),
            "retention_days": max(0, int(raw.get("native_raw_retention_days", defaults["retention_days"]) or 0)),
        }
    except Exception:  # noqa: BLE001
        log.debug("native_raw trace policy read failed, using defaults", exc_info=True)
        return defaults


def _cached_native_raw_trace_policy() -> dict[str, Any]:
    return dict(_NATIVE_RAW_TRACE_POLICY_CACHE or _NATIVE_RAW_TRACE_POLICY_DEFAULTS)


async def _enqueue_trace_write(
    kind: str,
    payload: Any,
    *,
    trace_id: str | None,
    account_id: int | None,
    phase: str,
    action_type: str | None = None,
    dedupe: bool = False,
) -> bool:
    global _TRACE_WRITE_DROPPED
    try:
        queue = _ensure_trace_writer()
        queue.put_nowait(_TraceWrite(kind=kind, payload=payload, dedupe=dedupe))
        return True
    except asyncio.QueueFull:
        _TRACE_WRITE_DROPPED += 1
        if _TRACE_WRITE_DROPPED <= 3 or _TRACE_WRITE_DROPPED % 100 == 0:
            log.warning(
                "event trace queue full; dropping trace write kind=%s trace_id=%s dropped=%s",
                kind,
                trace_id,
                _TRACE_WRITE_DROPPED,
            )
        return False
    except RuntimeError:
        await _write_trace_item_direct(kind, payload, trace_id=trace_id, account_id=account_id, phase=phase, action_type=action_type)
        return True
    except Exception:  # noqa: BLE001
        log.debug("event trace enqueue failed kind=%s trace_id=%s", kind, trace_id, exc_info=True)
        return False


def _ensure_trace_writer() -> asyncio.Queue[_TraceWrite]:
    global _TRACE_WRITE_QUEUE, _TRACE_WRITE_TASK
    task = _TRACE_WRITE_TASK
    if (
        (_TRACE_WRITE_QUEUE is not None and not _queue_uses_current_loop(_TRACE_WRITE_QUEUE))
        or (task is not None and not _task_uses_current_loop(task))
    ):
        _TRACE_WRITE_QUEUE = None
        _TRACE_WRITE_TASK = None
    if _TRACE_WRITE_QUEUE is None:
        _TRACE_WRITE_QUEUE = asyncio.Queue(maxsize=TRACE_WRITE_QUEUE_MAX_SIZE)
    if _TRACE_WRITE_TASK is None or _TRACE_WRITE_TASK.done():
        _TRACE_WRITE_TASK = asyncio.create_task(_trace_writer_loop(), name="telepilot-event-trace-writer")
    return _TRACE_WRITE_QUEUE


def _queue_uses_current_loop(queue: asyncio.Queue[_TraceWrite]) -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    bound_loop = getattr(queue, "_loop", None)
    return bound_loop is None or bound_loop is loop


def _task_uses_current_loop(task: asyncio.Task[Any]) -> bool:
    try:
        loop = asyncio.get_running_loop()
        return task.get_loop() is loop
    except RuntimeError:
        return False


async def _trace_writer_loop() -> None:
    while True:
        queue = _TRACE_WRITE_QUEUE
        if queue is None:
            await asyncio.sleep(TRACE_WRITE_BATCH_INTERVAL_SECONDS)
            continue
        first = await queue.get()
        batch = [first]
        try:
            deadline = time.monotonic() + TRACE_WRITE_BATCH_INTERVAL_SECONDS
            while len(batch) < TRACE_WRITE_BATCH_SIZE:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(queue.get(), timeout=timeout))
                except TimeoutError:
                    break
            await _flush_trace_batch(batch)
        finally:
            for _item in batch:
                queue.task_done()


async def _flush_trace_batch(batch: list[_TraceWrite], *, split_on_error: bool = True) -> None:
    if not batch:
        return
    try:
        async with AsyncSessionLocal() as db:
            existing_trace_ids = await _existing_trace_ids(db, batch)
            new_trace_ids: set[str] = set()
            for item in batch:
                if item.kind == "trace":
                    trace_id = _trace_write_trace_id(item)
                    if not trace_id or trace_id in existing_trace_ids or trace_id in new_trace_ids:
                        continue
                    db.add(item.payload)
                    new_trace_ids.add(trace_id)
                    continue
                if item.kind in {"span", "action", "runtime_error"}:
                    db.add(item.payload)
                    continue
                if item.kind == "finish":
                    await _apply_finish_trace_write(db, item.payload)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("event trace batch write failed size=%s", len(batch), exc_info=True)
        if split_on_error and len(batch) > 1:
            for item in batch:
                await _flush_trace_batch([item], split_on_error=False)
            return
        item = batch[0]
        trace_id = _trace_write_trace_id(item)
        await _write_trace_runtime_error(
            "event trace write failed",
            trace_id=trace_id,
            account_id=_trace_write_account_id(item),
            phase=item.kind,
            action_type=_trace_write_action_type(item),
        )


async def _write_trace_item_direct(
    kind: str,
    payload: Any,
    *,
    trace_id: str | None,
    account_id: int | None,
    phase: str,
    action_type: str | None = None,
) -> None:
    try:
        await _flush_trace_batch([_TraceWrite(kind=kind, payload=payload)], split_on_error=False)
    except Exception:  # noqa: BLE001
        log.debug("event trace direct write failed kind=%s trace_id=%s", kind, trace_id, exc_info=True)
        await _write_trace_runtime_error(
            "event trace direct write failed",
            trace_id=trace_id,
            account_id=account_id,
            phase=phase,
            action_type=action_type,
        )


async def _existing_trace_ids(db: Any, batch: list[_TraceWrite]) -> set[str]:
    trace_ids = {
        trace_id
        for item in batch
        if item.kind == "trace" and item.dedupe and (trace_id := _trace_write_trace_id(item))
    }
    if not trace_ids:
        return set()
    result = await db.execute(select(EventTrace.trace_id).where(EventTrace.trace_id.in_(trace_ids)))
    try:
        rows = result.scalars().all()
    except AttributeError:
        row = result.scalar_one_or_none()
        rows = [row] if row is not None else []
    return {str(row) for row in rows if row}


async def _apply_finish_trace_write(db: Any, payload: Any) -> None:
    data = payload if isinstance(payload, dict) else {}
    trace_id = str(data.get("trace_id") or "").strip()
    if not trace_id:
        return
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    if summary:
        current = (
            await db.execute(select(EventTrace).where(EventTrace.trace_id == trace_id))
        ).scalar_one_or_none()
        if current is not None:
            snap = dict(current.payload_snapshot or {})
            snap["trace_summary"] = redact_payload_snapshot(summary)
            current.payload_snapshot = snap
            current.status = str(values.get("status") or TRACE_STATUS_OK)
            current.ended_at = values.get("ended_at")
            current.duration_ms = values.get("duration_ms")
            return
    await db.execute(update(EventTrace).where(EventTrace.trace_id == trace_id).values(**values))


def _trace_write_trace_id(item: _TraceWrite) -> str | None:
    payload = item.payload
    if hasattr(payload, "trace_id"):
        return str(payload.trace_id or "") or None
    if isinstance(payload, dict):
        return str(payload.get("trace_id") or "") or None
    return None


def _trace_write_account_id(item: _TraceWrite) -> int | None:
    payload = item.payload
    if hasattr(payload, "account_id"):
        return _int_or_none(payload.account_id)
    return None


def _trace_write_action_type(item: _TraceWrite) -> str | None:
    payload = item.payload
    if hasattr(payload, "action_type"):
        return _str_or_none(payload.action_type)
    return None


def _clear_native_raw_snapshot(row: EventTrace) -> bool:
    snapshot = row.payload_snapshot if isinstance(row.payload_snapshot, dict) else {}
    value = snapshot.get("native_raw")
    if "native_raw" not in snapshot or value is None or value in ("[omitted]", "[expired]"):
        return False
    snapshot = dict(snapshot)
    snapshot["native_raw"] = "[expired]"
    row.payload_snapshot = snapshot
    meta = dict(row.native_raw_meta or {})
    meta["stored_in_trace"] = False
    meta["expired_from_trace"] = True
    row.native_raw_meta = meta
    return True


async def _write_trace_runtime_error(
    message: str,
    *,
    trace_id: str | None,
    account_id: int | None,
    phase: str,
    action_type: str | None = None,
) -> None:
    """Best-effort fallback so Trace storage failures are visible in old logs."""

    try:
        async with AsyncSessionLocal() as db:
            db.add(
                RuntimeLog(
                    account_id=account_id,
                    level=LEVEL_ERROR,
                    source="system",
                    message=message,
                    detail={
                        "trace_id": trace_id,
                        "phase": phase,
                        "action_type": action_type,
                        "component": "event_trace",
                        "reason_code": TRACE_WRITE_FAILED_REASON_CODE,
                    },
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("event trace runtime fallback log failed trace_id=%s phase=%s", trace_id, phase, exc_info=True)


def _trace_id(raw: Any) -> str:
    value = str(raw or "").strip()
    return value if value else _new_id(TRACE_ID_PREFIX)


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _object_payload(value: Any) -> dict[str, Any]:
    return {
        "account_id": getattr(value, "account_id", None),
        "event_type": getattr(value, "type", None) or getattr(value, "event_type", None),
        "chat_id": getattr(value, "chat_id", None),
        "message_id": getattr(value, "message_id", None),
        "source_update_id": getattr(value, "update_id", None),
        "sender_user_id": getattr(value, "user_id", None),
        "sender_name": getattr(value, "display_name", None),
        "message_text": getattr(value, "text", None),
        "callback_query_id": getattr(value, "callback_id", None),
    }


def _context_trace_id(value: TraceContext | dict[str, Any] | str | None) -> str | None:
    if isinstance(value, TraceContext):
        return value.trace_id
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return str(value.get("trace_id") or "").strip() or None
    return None


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


def _compact_json(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value[:160]
    try:
        return json.dumps(redact_payload_snapshot(value), ensure_ascii=False, separators=(",", ":"))[:160]
    except (TypeError, ValueError):
        return str(value)[:160]


def _inline_result_count(action: dict[str, Any]) -> int | None:
    if str(action.get("type") or "") != "answer_inline_query":
        return None
    results = action.get("results")
    return len(results) if isinstance(results, list) else 0


__all__ = [
    "ACTION_ID_PREFIX",
    "SPAN_ID_PREFIX",
    "TRACE_ID_PREFIX",
    "TRACE_STATUS_FAILED",
    "TRACE_STATUS_OK",
    "TRACE_STATUS_RUNNING",
    "TRACE_STATUS_SKIPPED",
    "TRACE_STATUS_WARNING",
    "TRACE_WRITE_FAILED_REASON_CODE",
    "TraceContext",
    "finish_trace",
    "flush_trace_writes",
    "record_action",
    "record_span",
    "redact_payload_snapshot",
    "refresh_trace_settings",
    "cleanup_event_traces",
    "start_trace",
    "stop_trace_writer",
    "trace_log_context",
    "trace_writer_stats",
    "update_plugin_runtime_status",
]
