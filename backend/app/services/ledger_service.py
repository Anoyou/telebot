"""资金台账服务。

数据源：
- ``action_event``：出账取 payout 类 action，入账取 payment_confirmed 类事件。
- ``payout_compensation``：只展示 pending / abandoned 的补付挂账，人工核销后标记 compensated。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.action_event import ACTION_EVENT_STATUS_COMPENSATED, ACTION_EVENT_STATUS_OK, ActionEvent
from ..db.models.log import EventTrace
from ..db.models.payout_compensation import (
    PAYOUT_COMPENSATION_STATUS_ABANDONED,
    PAYOUT_COMPENSATION_STATUS_COMPENSATED,
    PAYOUT_COMPENSATION_STATUS_PENDING,
    PayoutCompensation,
)
from . import audit
from .action_tap import emit_compensated_payout_event

LEDGER_DIRECTION_IN = "in"
LEDGER_DIRECTION_OUT = "out"
LEDGER_DIRECTIONS = {LEDGER_DIRECTION_IN, LEDGER_DIRECTION_OUT}

LEDGER_SOURCE_ACTION_EVENT = "action_event"
LEDGER_SUMMARY_STATUSES = frozenset({ACTION_EVENT_STATUS_OK, ACTION_EVENT_STATUS_COMPENSATED})
DEFAULT_LEDGER_SUMMARY_WINDOW_DAYS = 30

COMPENSATION_OPEN_STATUSES = {
    PAYOUT_COMPENSATION_STATUS_PENDING,
    PAYOUT_COMPENSATION_STATUS_ABANDONED,
}


@dataclass(slots=True)
class LedgerFilters:
    since: datetime | None = None
    until: datetime | None = None
    account_id: int | None = None
    chat_id: int | None = None
    plugin_key: str | None = None
    direction: str | None = None
    amount: Decimal | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    status: str | None = None
    statuses: frozenset[str] | None = None
    limit: int | None = 100


@dataclass(slots=True)
class LedgerEntry:
    id: int
    source: str
    source_id: int
    direction: str
    amount: str
    signed_amount: str
    status: str
    account_id: int
    chat_id: int | None
    chat_title: str | None
    payer_user_id: int | None
    payer_name: str | None
    receiver_user_id: int | None
    receiver_name: str | None
    receiver_username: str | None
    plugin_key: str | None
    entry_key: str | None
    channel: str | None
    session_key: str | None
    action_type: str
    payout_key: str | None
    error_code: str | None
    created_at: datetime
    params_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LedgerSummaryBucket:
    key: str
    label: str
    income: str
    payout: str
    net: str
    count: int


@dataclass(slots=True)
class LedgerRecipientBucket:
    key: str
    label: str
    user_id: int | None
    username: str | None
    received: str
    income: str
    payout: str
    count: int


@dataclass(slots=True)
class LedgerSummary:
    income: str
    payout: str
    net: str
    count: int
    by_day: list[LedgerSummaryBucket]
    by_chat: list[LedgerSummaryBucket]
    by_recipient: list[LedgerRecipientBucket]


@dataclass(slots=True)
class LedgerCompensation:
    id: int
    payout_key: str
    account_id: int
    trace_id: str | None
    plugin_key: str | None
    entry_key: str | None
    origin: str
    chat_id: int
    chat_title: str | None
    receiver_user_id: int | None
    receiver_name: str | None
    amount: str
    status: str
    error_code_first: str | None
    error_code_last: str | None
    error_last: str | None
    ambiguous: bool
    retry_count: int
    next_attempt_at: datetime
    sent_message_id: int | None
    sent_at: datetime | None
    notified_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class LedgerResetResult:
    deleted_action_events: int
    deleted_compensations: int


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def list_ledger_entries(db: AsyncSession, filters: LedgerFilters | None = None) -> list[LedgerEntry]:
    """查询资金流水，金额过滤使用绝对金额。"""

    active = _normalize_filters(filters)
    if active.status is None and not active.statuses:
        active.statuses = LEDGER_SUMMARY_STATUSES
    entries: list[LedgerEntry] = []
    cursor: tuple[datetime, int] | None = None
    batch_size = None if active.limit is None else max(200, active.limit * 4)
    while True:
        rows = await _load_action_rows(db, active, cursor=cursor, batch_size=batch_size)
        if not rows:
            break
        for row in rows:
            entry = _entry_from_action_event(row)
            if entry is None or not _entry_matches(entry, active):
                continue
            entries.append(entry)
            if active.limit is not None and len(entries) >= active.limit:
                break
        if active.limit is not None and len(entries) >= active.limit:
            break
        if batch_size is None or len(rows) < batch_size:
            break
        last = rows[-1]
        cursor = (last.created_at, int(last.id))
    await _hydrate_ledger_metadata(db, entries)
    return entries


async def summarize_ledger(db: AsyncSession, filters: LedgerFilters | None = None) -> LedgerSummary:
    """按日和按群汇总真实资金净盈亏。"""

    active = _normalize_filters(filters)
    if active.status is not None and active.status not in LEDGER_SUMMARY_STATUSES:
        return _empty_summary()
    if active.status is None:
        active.statuses = LEDGER_SUMMARY_STATUSES
    _apply_default_summary_window(active)
    active.limit = None
    entries = await list_ledger_entries(db, active)

    income = Decimal("0")
    payout = Decimal("0")
    day_buckets: dict[str, _SummaryAccumulator] = {}
    chat_buckets: dict[str, _SummaryAccumulator] = {}
    recipient_buckets: dict[str, _RecipientAccumulator] = {}

    for entry in entries:
        amount = _decimal_from_any(entry.amount) or Decimal("0")
        if entry.direction == LEDGER_DIRECTION_IN:
            income += amount
        else:
            payout += amount

        day_key = entry.created_at.date().isoformat()
        _add_to_bucket(day_buckets, day_key, day_key, entry.direction, amount)

        chat_key = str(entry.chat_id) if entry.chat_id is not None else "unknown"
        chat_label = entry.chat_title or (str(entry.chat_id) if entry.chat_id is not None else "未知群")
        _add_to_bucket(chat_buckets, chat_key, chat_label, entry.direction, amount)

        recipient_key = _recipient_key(entry)
        if recipient_key is not None:
            _add_to_recipient_bucket(recipient_buckets, recipient_key, entry, amount)

    return LedgerSummary(
        income=_decimal_text(income),
        payout=_decimal_text(payout),
        net=_decimal_text(income - payout),
        count=len(entries),
        by_day=_summary_buckets(day_buckets, sort_key=lambda item: item.key),
        by_chat=_summary_buckets(
            chat_buckets,
            sort_key=lambda item: (_decimal_from_any(item.net) or Decimal("0"), item.key),
            reverse=True,
        ),
        by_recipient=_recipient_buckets(recipient_buckets),
    )


async def reset_ledger_data(db: AsyncSession, *, user_id: int | None) -> LedgerResetResult:
    """清空资金台账、补付队列及本页运营统计所依赖的动作。"""

    action_result = await db.execute(
        delete(ActionEvent).where(
            or_(
                ActionEvent.action_type.in_(("payment_confirmed", "payout", "start_session")),
                ActionEvent.action_type.endswith("_payout", autoescape=True),
                ActionEvent.action_type.startswith("payout_", autoescape=True),
            )
        )
    )
    compensation_result = await db.execute(delete(PayoutCompensation))
    deleted_action_events = int(getattr(action_result, "rowcount", 0) or 0)
    deleted_compensations = int(getattr(compensation_result, "rowcount", 0) or 0)
    await audit.write(
        db,
        user_id,
        "ledger.reset",
        target="ledger",
        detail={
            "deleted_action_events": deleted_action_events,
            "deleted_compensations": deleted_compensations,
        },
    )
    await db.flush()
    return LedgerResetResult(
        deleted_action_events=deleted_action_events,
        deleted_compensations=deleted_compensations,
    )


async def list_compensations(
    db: AsyncSession,
    *,
    account_id: int | None = None,
    chat_id: int | None = None,
    plugin_key: str | None = None,
    limit: int = 100,
) -> list[LedgerCompensation]:
    """返回待处理和已放弃的补付挂账。"""

    stmt = select(PayoutCompensation).where(PayoutCompensation.status.in_(COMPENSATION_OPEN_STATUSES))
    if account_id is not None:
        stmt = stmt.where(PayoutCompensation.account_id == int(account_id))
    if chat_id is not None:
        stmt = stmt.where(PayoutCompensation.chat_id == int(chat_id))
    if plugin_key:
        stmt = stmt.where(PayoutCompensation.plugin_key == plugin_key)
    stmt = stmt.order_by(PayoutCompensation.created_at.desc(), PayoutCompensation.id.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [_compensation_from_row(row) for row in rows]


async def mark_compensation_manual_paid(
    db: AsyncSession,
    compensation_id: int,
    *,
    user_id: int | None,
    note: str | None = None,
) -> LedgerCompensation:
    """人工确认补付已完成，并写审计日志。

    事务由调用方提交；函数会 flush 以便测试和 API 能立即观察到变更。
    """

    now = datetime.now(UTC)
    current_status = await db.scalar(
        select(PayoutCompensation.status).where(PayoutCompensation.id == int(compensation_id))
    )
    if current_status is None:
        raise _err("LEDGER_COMPENSATION_NOT_FOUND", "挂账记录不存在", 404)
    if current_status not in COMPENSATION_OPEN_STATUSES:
        raise _err("LEDGER_COMPENSATION_CLOSED", "该挂账记录已处理，不能重复核销", 409)
    result = await db.execute(
        update(PayoutCompensation)
        .where(
            PayoutCompensation.id == int(compensation_id),
            PayoutCompensation.status == current_status,
        )
        .values(
            status=PAYOUT_COMPENSATION_STATUS_COMPENSATED,
            sent_at=func.coalesce(PayoutCompensation.sent_at, now),
            updated_at=now,
        )
        .returning(PayoutCompensation)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise _err("LEDGER_COMPENSATION_CLOSED", "该挂账记录已处理，不能重复核销", 409)

    previous_status = current_status

    payload = dict(row.payload or {})
    ledger_action = {
        "type": "payout",
        "action_type": "payout",
        "payout_key": row.payout_key,
        "chat_id": row.chat_id,
        "amount": row.amount,
        "text": str(payload.get("text") or f"+{int(row.amount)}").strip(),
    }
    for key in ("receiver_user_id", "receiver_name", "receiver_username", "chat_title", "reply_to_user_id"):
        if payload.get(key) not in (None, ""):
            ledger_action[key] = payload.get(key)
    await emit_compensated_payout_event(
        account_id=int(row.account_id),
        payout_key=str(row.payout_key),
        amount=row.amount,
        chat_id=int(row.chat_id),
        plugin_key=row.plugin_key,
        entry_key=row.entry_key,
        channel="manual_compensation",
        compensation_source="manual_paid",
        previous_error_code=row.error_code_last or row.error_code_first,
        result={
            "message_id": row.sent_message_id,
            "chat_id": row.chat_id,
            "manual_note": (str(note or "").strip()[:500] or None),
        },
        action=ledger_action,
        db=db,
    )

    detail = {
        "previous_status": previous_status,
        "status": PAYOUT_COMPENSATION_STATUS_COMPENSATED,
        "payout_key": row.payout_key,
        "account_id": row.account_id,
        "chat_id": row.chat_id,
        "amount": _decimal_text(_decimal_from_any(row.amount) or Decimal("0")),
        "plugin_key": row.plugin_key,
        "entry_key": row.entry_key,
    }
    cleaned_note = str(note or "").strip()
    if cleaned_note:
        detail["note"] = cleaned_note[:500]
    await audit.write(
        db,
        user_id,
        "ledger.compensation.manual_paid",
        target=f"payout_compensation:{row.id}",
        detail=detail,
    )
    await db.flush()
    return _compensation_from_row(row)


@dataclass(slots=True)
class _SummaryAccumulator:
    key: str
    label: str
    income: Decimal = Decimal("0")
    payout: Decimal = Decimal("0")
    count: int = 0


@dataclass(slots=True)
class _RecipientAccumulator:
    key: str
    label: str
    user_id: int | None
    username: str | None
    received: Decimal = Decimal("0")
    income: Decimal = Decimal("0")
    payout: Decimal = Decimal("0")
    count: int = 0


def _normalize_filters(filters: LedgerFilters | None) -> LedgerFilters:
    active = replace(filters) if filters is not None else LedgerFilters()
    if active.direction:
        direction = str(active.direction).strip().lower()
        if direction not in LEDGER_DIRECTIONS:
            raise _err("LEDGER_BAD_DIRECTION", "方向只能是 in 或 out")
        active.direction = direction
    if active.plugin_key:
        active.plugin_key = str(active.plugin_key).strip() or None
    if active.status is not None:
        active.status = str(active.status).strip().upper() or None
    if active.statuses:
        active.statuses = frozenset(str(status).strip().upper() for status in active.statuses if str(status).strip())
    if active.limit is not None:
        active.limit = max(1, min(int(active.limit), 500))
    return active


def _apply_default_summary_window(filters: LedgerFilters) -> None:
    if filters.since is not None:
        return
    window_end = filters.until or datetime.now(UTC)
    filters.since = window_end - timedelta(days=DEFAULT_LEDGER_SUMMARY_WINDOW_DAYS)


def _empty_summary() -> LedgerSummary:
    return LedgerSummary(income="0", payout="0", net="0", count=0, by_day=[], by_chat=[], by_recipient=[])


async def _load_action_rows(
    db: AsyncSession,
    filters: LedgerFilters,
    *,
    cursor: tuple[datetime, int] | None = None,
    batch_size: int | None = None,
) -> list[ActionEvent]:
    stmt = select(ActionEvent)
    if filters.since is not None:
        stmt = stmt.where(ActionEvent.created_at >= filters.since)
    if filters.until is not None:
        stmt = stmt.where(ActionEvent.created_at <= filters.until)
    if filters.account_id is not None:
        stmt = stmt.where(ActionEvent.account_id == int(filters.account_id))
    if filters.plugin_key:
        stmt = stmt.where(ActionEvent.plugin_key == filters.plugin_key)
    if filters.status:
        stmt = stmt.where(ActionEvent.status == filters.status)
    elif filters.statuses:
        stmt = stmt.where(ActionEvent.status.in_(filters.statuses))
    if cursor is not None:
        created_at, row_id = cursor
        stmt = stmt.where(
            or_(
                ActionEvent.created_at < created_at,
                and_(ActionEvent.created_at == created_at, ActionEvent.id < int(row_id)),
            )
        )
    stmt = stmt.order_by(ActionEvent.created_at.desc(), ActionEvent.id.desc())
    if batch_size is not None:
        stmt = stmt.limit(max(1, int(batch_size)))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def _hydrate_ledger_metadata(db: AsyncSession, entries: list[LedgerEntry]) -> None:
    if not entries:
        return
    account_ids = {entry.account_id for entry in entries}
    chat_ids = {entry.chat_id for entry in entries if entry.chat_id is not None and not entry.chat_title}
    user_ids = {
        user_id
        for entry in entries
        for user_id in (entry.payer_user_id, entry.receiver_user_id)
        if user_id is not None
        and (
            (user_id == entry.payer_user_id and not entry.payer_name)
            or (user_id == entry.receiver_user_id and not entry.receiver_name)
        )
    }

    chat_labels: dict[tuple[int, int], str] = {}
    if chat_ids:
        rows = (
            await db.execute(
                select(EventTrace.account_id, EventTrace.chat_id, EventTrace.payload_snapshot)
                .where(
                    EventTrace.account_id.in_(account_ids),
                    EventTrace.chat_id.in_(chat_ids),
                )
                .order_by(EventTrace.started_at.desc(), EventTrace.id.desc())
                .limit(5000)
            )
        ).all()
        for account_id, chat_id, payload in rows:
            if account_id is None or chat_id is None:
                continue
            key = (int(account_id), int(chat_id))
            if key in chat_labels:
                continue
            payload_data = payload if isinstance(payload, dict) else {}
            title = _text_or_none(
                _nested(payload_data, "chat", "title"),
                payload_data.get("chat_title"),
            )
            if title:
                chat_labels[key] = title

    user_labels: dict[tuple[int, int], str] = {}
    if user_ids:
        rows = (
            await db.execute(
                select(EventTrace.account_id, EventTrace.sender_user_id, EventTrace.sender_name)
                .where(
                    EventTrace.account_id.in_(account_ids),
                    EventTrace.sender_user_id.in_(user_ids),
                    EventTrace.sender_name.is_not(None),
                )
                .order_by(EventTrace.started_at.desc(), EventTrace.id.desc())
                .limit(5000)
            )
        ).all()
        for account_id, user_id, sender_name in rows:
            if account_id is None or user_id is None:
                continue
            key = (int(account_id), int(user_id))
            if key not in user_labels:
                label = _text_or_none(sender_name)
                if label:
                    user_labels[key] = label

    for entry in entries:
        if entry.chat_id is not None and not entry.chat_title:
            entry.chat_title = chat_labels.get((entry.account_id, entry.chat_id))
        if entry.payer_user_id is not None and not entry.payer_name:
            entry.payer_name = user_labels.get((entry.account_id, entry.payer_user_id))
        if entry.receiver_user_id is not None and not entry.receiver_name:
            entry.receiver_name = user_labels.get((entry.account_id, entry.receiver_user_id))


def _entry_from_action_event(row: ActionEvent) -> LedgerEntry | None:
    summary = dict(row.params_summary or {})
    direction = _direction_from_action(row.action_type, summary)
    if direction is None:
        return None
    amount = _extract_amount(summary)
    if amount is None:
        return None
    signed = amount if direction == LEDGER_DIRECTION_IN else -amount
    return LedgerEntry(
        id=int(row.id),
        source=LEDGER_SOURCE_ACTION_EVENT,
        source_id=int(row.id),
        direction=direction,
        amount=_decimal_text(amount.copy_abs()),
        signed_amount=_decimal_text(signed),
        status=row.status,
        account_id=int(row.account_id),
        chat_id=_int_or_none(
            summary.get("chat_id"),
            _nested(summary, "source", "chat_id"),
            _nested(summary, "chat", "id"),
            _nested(summary, "payment", "chat_id"),
        ),
        chat_title=_text_or_none(
            summary.get("chat_title"),
            _nested(summary, "chat", "title"),
            _nested(summary, "payment", "chat_title"),
        ),
        payer_user_id=_int_or_none(summary.get("payer_user_id"), _nested(summary, "payment", "payer", "user_id")),
        payer_name=_text_or_none(summary.get("payer_name"), _nested(summary, "payment", "payer", "display_name")),
        receiver_user_id=_int_or_none(
            summary.get("receiver_user_id"),
            summary.get("reply_to_user_id"),
            _nested(summary, "payment", "receiver", "user_id"),
        ),
        receiver_name=_text_or_none(
            summary.get("receiver_name"),
            _nested(summary, "payment", "receiver", "display_name"),
        ),
        receiver_username=_text_or_none(
            summary.get("receiver_username"),
            _nested(summary, "payment", "receiver", "username"),
        ),
        plugin_key=row.plugin_key,
        entry_key=row.entry_key,
        channel=row.channel,
        session_key=row.session_key,
        action_type=row.action_type,
        payout_key=_text_or_none(getattr(row, "payout_key", None), summary.get("payout_key")),
        error_code=row.error_code,
        created_at=row.created_at,
        params_summary=summary,
    )


def _direction_from_action(action_type: str, summary: dict[str, Any]) -> str | None:
    action = str(action_type or "").strip().lower()
    summary_type = str(summary.get("type") or summary.get("action_type") or "").strip().lower()
    event_type = str(summary.get("event_type") or "").strip().lower()
    source_type = str(_nested(summary, "source", "type") or "").strip().lower()
    raw_event_type = str(_nested(summary, "raw", "event_type") or "").strip().lower()
    if action == "payment_confirmed" or "payment_confirmed" in {summary_type, event_type, source_type, raw_event_type}:
        return LEDGER_DIRECTION_IN
    if action == "payout" or summary_type == "payout" or action.endswith("_payout") or action.startswith("payout_"):
        return LEDGER_DIRECTION_OUT
    return None


def _extract_amount(summary: dict[str, Any]) -> Decimal | None:
    return _decimal_from_any(
        summary.get("amount"),
        _nested(summary, "payment", "amount"),
        _nested(summary, "parsed", "amount"),
        _nested(summary, "raw", "parsed", "amount"),
        _nested(summary, "result", "amount"),
    )


def _entry_matches(entry: LedgerEntry, filters: LedgerFilters) -> bool:
    if filters.chat_id is not None and entry.chat_id != int(filters.chat_id):
        return False
    if filters.direction is not None and entry.direction != filters.direction:
        return False
    amount = _decimal_from_any(entry.amount)
    if amount is None:
        return False
    if filters.amount is not None and amount != filters.amount:
        return False
    if filters.amount_min is not None and amount < filters.amount_min:
        return False
    if filters.amount_max is not None and amount > filters.amount_max:
        return False
    return True


def _add_to_bucket(
    buckets: dict[str, _SummaryAccumulator],
    key: str,
    label: str,
    direction: str,
    amount: Decimal,
) -> None:
    bucket = buckets.setdefault(key, _SummaryAccumulator(key=key, label=label))
    if bucket.label in {bucket.key, "未知群"} and label not in {key, "未知群"}:
        bucket.label = label
    if direction == LEDGER_DIRECTION_IN:
        bucket.income += amount
    else:
        bucket.payout += amount
    bucket.count += 1


def _recipient_key(entry: LedgerEntry) -> str | None:
    if entry.receiver_user_id is not None:
        return str(entry.receiver_user_id)
    name = str(entry.receiver_name or entry.receiver_username or "").strip()
    return f"name:{name.casefold()}" if name else None


def _add_to_recipient_bucket(
    buckets: dict[str, _RecipientAccumulator],
    key: str,
    entry: LedgerEntry,
    amount: Decimal,
) -> None:
    label = entry.receiver_name or (f"@{entry.receiver_username.lstrip('@')}" if entry.receiver_username else None)
    label = label or (str(entry.receiver_user_id) if entry.receiver_user_id is not None else "未知收款方")
    bucket = buckets.setdefault(
        key,
        _RecipientAccumulator(
            key=key,
            label=label,
            user_id=entry.receiver_user_id,
            username=entry.receiver_username,
        ),
    )
    if bucket.label in {bucket.key, "未知收款方"} and label not in {key, "未知收款方"}:
        bucket.label = label
    bucket.received += amount
    if entry.direction == LEDGER_DIRECTION_IN:
        bucket.income += amount
    else:
        bucket.payout += amount
    bucket.count += 1


def _recipient_buckets(buckets: dict[str, _RecipientAccumulator]) -> list[LedgerRecipientBucket]:
    out = [
        LedgerRecipientBucket(
            key=item.key,
            label=item.label,
            user_id=item.user_id,
            username=item.username,
            received=_decimal_text(item.received),
            income=_decimal_text(item.income),
            payout=_decimal_text(item.payout),
            count=item.count,
        )
        for item in buckets.values()
    ]
    return sorted(out, key=lambda item: (_decimal_from_any(item.received) or Decimal("0"), item.key), reverse=True)


def _summary_buckets(
    buckets: dict[str, _SummaryAccumulator],
    *,
    sort_key: Any,
    reverse: bool = False,
) -> list[LedgerSummaryBucket]:
    out = [
        LedgerSummaryBucket(
            key=item.key,
            label=item.label,
            income=_decimal_text(item.income),
            payout=_decimal_text(item.payout),
            net=_decimal_text(item.income - item.payout),
            count=item.count,
        )
        for item in buckets.values()
    ]
    return sorted(out, key=sort_key, reverse=reverse)


def _compensation_from_row(row: PayoutCompensation) -> LedgerCompensation:
    return LedgerCompensation(
        id=int(row.id),
        payout_key=row.payout_key,
        account_id=int(row.account_id),
        trace_id=row.trace_id,
        plugin_key=row.plugin_key,
        entry_key=row.entry_key,
        origin=row.origin,
        chat_id=int(row.chat_id),
        chat_title=_text_or_none((row.payload or {}).get("chat_title")),
        receiver_user_id=_int_or_none((row.payload or {}).get("receiver_user_id"), (row.payload or {}).get("reply_to_user_id")),
        receiver_name=_text_or_none((row.payload or {}).get("receiver_name")),
        amount=_decimal_text(_decimal_from_any(row.amount) or Decimal("0")),
        status=row.status,
        error_code_first=row.error_code_first,
        error_code_last=row.error_code_last,
        error_last=row.error_last,
        ambiguous=bool(row.ambiguous),
        retry_count=int(row.retry_count or 0),
        next_attempt_at=row.next_attempt_at,
        sent_message_id=row.sent_message_id,
        sent_at=row.sent_at,
        notified_at=row.notified_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _decimal_from_any(*values: Any) -> Decimal | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            if isinstance(value, Decimal):
                return value
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            continue
    return None


def _decimal_text(value: Decimal) -> str:
    return str(value)


def _nested(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _int_or_none(*values: Any) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _text_or_none(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def today() -> date:
    return datetime.now(UTC).date()


__all__ = [
    "COMPENSATION_OPEN_STATUSES",
    "LEDGER_DIRECTION_IN",
    "LEDGER_DIRECTION_OUT",
    "LEDGER_DIRECTIONS",
    "LedgerCompensation",
    "LedgerEntry",
    "LedgerFilters",
    "LedgerSummary",
    "LedgerSummaryBucket",
    "list_compensations",
    "list_ledger_entries",
    "mark_compensation_manual_paid",
    "reset_ledger_data",
    "summarize_ledger",
    "today",
]
