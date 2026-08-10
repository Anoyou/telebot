"""游戏运营统计聚合。

统计口径只使用已经落库的结构化动作账和资金台账：
- 开局数：``action_event`` 中 OK 的 ``start_session``，按 ``session_key`` 去重。
- 派奖成功率：``action_event`` 中 payout 类动作的 OK / (OK + FAILED)。
- 净盈亏：直接复用 ``ledger_service.summarize_ledger``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.action_event import ACTION_EVENT_STATUS_FAILED, ACTION_EVENT_STATUS_OK, ActionEvent
from . import ledger_service

METRIC_STATUS_AVAILABLE = "available"
METRIC_STATUS_NEEDS_INSTRUMENTATION = "needs_instrumentation"


@dataclass(slots=True)
class StatsFilters:
    since: datetime | None = None
    until: datetime | None = None
    account_id: int | None = None
    chat_id: int | None = None
    plugin_key: str | None = None


@dataclass(slots=True)
class MetricAvailability:
    key: str
    label: str
    status: str
    source: str
    note: str


@dataclass(slots=True)
class OperationalStatsTotal:
    started_sessions: int
    participant_count: int | None
    payout_success_count: int
    payout_failure_count: int
    payout_attempt_count: int
    payout_success_rate: str | None
    ledger_income: str
    ledger_payout: str
    ledger_net: str
    ledger_count: int


@dataclass(slots=True)
class OperationalStatsBucket:
    key: str
    label: str
    started_sessions: int
    payout_success_count: int
    payout_failure_count: int
    payout_attempt_count: int
    payout_success_rate: str | None
    ledger_income: str
    ledger_payout: str
    ledger_net: str
    ledger_count: int


@dataclass(slots=True)
class OperationalStats:
    total: OperationalStatsTotal
    by_day: list[OperationalStatsBucket]
    by_chat: list[OperationalStatsBucket]
    source_matrix: list[MetricAvailability]


@dataclass(slots=True)
class _StatsAccumulator:
    key: str
    label: str
    session_start_keys: set[str] = field(default_factory=set)
    participant_user_ids: set[int] = field(default_factory=set)
    payout_success_count: int = 0
    payout_failure_count: int = 0
    ledger_income: Decimal = Decimal("0")
    ledger_payout: Decimal = Decimal("0")
    ledger_net: Decimal = Decimal("0")
    ledger_count: int = 0


SOURCE_MATRIX = [
    MetricAvailability(
        key="started_sessions",
        label="开局数",
        status=METRIC_STATUS_AVAILABLE,
        source="action_event.action_type=start_session, status=OK, canonical 事件实例",
        note="按首次进入活跃状态时写入的结构化会话事件统计。",
    ),
    MetricAvailability(
        key="participant_count",
        label="参与人数",
        status=METRIC_STATUS_AVAILABLE,
        source="action_event 会话/付款参与者与成功派奖接收者 User ID 去重",
        note="优先读取结构化参与者字段，并兼容从成功派奖接收者恢复历史赢家。",
    ),
    MetricAvailability(
        key="payout_success_rate",
        label="派奖成功率",
        status=METRIC_STATUS_AVAILABLE,
        source="action_event payout OK/(OK+FAILED)",
        note="只把 OK 和 FAILED 放入分母，PENDING、DRY_RUN 等不计入成功率。",
    ),
    MetricAvailability(
        key="net_profit",
        label="净盈亏",
        status=METRIC_STATUS_AVAILABLE,
        source="ledger_service.summarize_ledger",
        note="与资金台账汇总同源，等于入账减出账。",
    ),
]


async def summarize_operational_stats(
    db: AsyncSession,
    filters: StatsFilters | None = None,
) -> OperationalStats:
    """按日和按群聚合运营指标，净盈亏复用资金台账口径。"""

    active = _normalize_filters(filters)
    ledger_filters = ledger_service.resolve_summary_filters(_ledger_filters(active))
    # 运营动作与资金台账必须复用同一个已解析时间窗口，避免默认查询时
    # 出现“全部历史动作 + 最近 30 天资金”的混合口径。
    active.since = ledger_filters.since
    active.until = ledger_filters.until
    ledger_summary = await ledger_service.summarize_ledger(db, ledger_filters)
    rows = await _load_action_rows(db, active)

    total_acc = _StatsAccumulator(key="total", label="全部")
    day_buckets: dict[str, _StatsAccumulator] = {}
    chat_buckets: dict[str, _StatsAccumulator] = {}

    for row in rows:
        summary = dict(row.params_summary or {})
        chat_id = _chat_id_from_summary(summary)
        if active.chat_id is not None and chat_id != int(active.chat_id):
            continue

        day_key = row.created_at.date().isoformat()
        chat_key = str(chat_id) if chat_id is not None else "unknown"
        chat_label = str(chat_id) if chat_id is not None else "未知群"

        _add_action_metrics(total_acc, row, summary)
        _add_action_metrics(day_buckets.setdefault(day_key, _StatsAccumulator(key=day_key, label=day_key)), row, summary)
        _add_action_metrics(
            chat_buckets.setdefault(chat_key, _StatsAccumulator(key=chat_key, label=chat_label)),
            row,
            summary,
        )

    _apply_ledger_total(total_acc, ledger_summary)
    _apply_ledger_buckets(day_buckets, ledger_summary.by_day)
    _apply_ledger_buckets(chat_buckets, ledger_summary.by_chat)

    return OperationalStats(
        total=_total_from_acc(total_acc),
        by_day=_buckets_from_acc(day_buckets, sort_key=lambda item: item.key),
        by_chat=_buckets_from_acc(
            chat_buckets,
            sort_key=lambda item: (item.payout_attempt_count, item.started_sessions, item.ledger_net, item.key),
            reverse=True,
        ),
        source_matrix=list(SOURCE_MATRIX),
    )


def _normalize_filters(filters: StatsFilters | None) -> StatsFilters:
    active = filters or StatsFilters()
    return StatsFilters(
        since=active.since,
        until=active.until,
        account_id=int(active.account_id) if active.account_id is not None else None,
        chat_id=int(active.chat_id) if active.chat_id is not None else None,
        plugin_key=str(active.plugin_key).strip() if active.plugin_key else None,
    )


def _ledger_filters(filters: StatsFilters) -> ledger_service.LedgerFilters:
    return ledger_service.LedgerFilters(
        since=filters.since,
        until=filters.until,
        account_id=filters.account_id,
        chat_id=filters.chat_id,
        plugin_key=filters.plugin_key,
        limit=None,
    )


async def _load_action_rows(db: AsyncSession, filters: StatsFilters) -> list[ActionEvent]:
    stmt = select(ActionEvent)
    if filters.since is not None:
        stmt = stmt.where(ActionEvent.created_at >= filters.since)
    if filters.until is not None:
        stmt = stmt.where(ActionEvent.created_at <= filters.until)
    if filters.account_id is not None:
        stmt = stmt.where(ActionEvent.account_id == int(filters.account_id))
    if filters.plugin_key:
        stmt = stmt.where(ActionEvent.plugin_key == filters.plugin_key)
    stmt = stmt.order_by(ActionEvent.created_at.asc(), ActionEvent.id.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


def _add_action_metrics(acc: _StatsAccumulator, row: ActionEvent, summary: dict[str, Any]) -> None:
    status = str(row.status or "").strip().upper()
    if _is_start_session(row, summary) and status == ACTION_EVENT_STATUS_OK:
        acc.session_start_keys.add(_session_start_key(row))
    if status == ACTION_EVENT_STATUS_OK:
        acc.participant_user_ids.update(_participant_ids_from_summary(summary))
    is_payout = _is_payout_action(row, summary)
    if is_payout and status == ACTION_EVENT_STATUS_OK:
        acc.participant_user_ids.update(_payout_participant_ids_from_summary(summary))
    if not is_payout:
        return
    if status == ACTION_EVENT_STATUS_OK:
        acc.payout_success_count += 1
    elif status == ACTION_EVENT_STATUS_FAILED:
        acc.payout_failure_count += 1


def _is_start_session(row: ActionEvent, summary: dict[str, Any]) -> bool:
    return _action_name(row, summary) == "start_session"


def _is_payout_action(row: ActionEvent, summary: dict[str, Any]) -> bool:
    action = _action_name(row, summary)
    summary_type = str(summary.get("type") or summary.get("action_type") or "").strip().lower()
    return action == "payout" or summary_type == "payout" or action.endswith("_payout") or action.startswith("payout_")


def _action_name(row: ActionEvent, summary: dict[str, Any]) -> str:
    return str(row.action_type or summary.get("type") or summary.get("action_type") or "").strip().lower()


def _session_start_key(row: ActionEvent) -> str:
    # session_key identifies the reusable Redis slot, not one game round.
    return f"event:{int(row.id)}"


def _chat_id_from_summary(summary: dict[str, Any]) -> int | None:
    return _int_or_none(
        summary.get("chat_id"),
        _nested(summary, "source", "chat_id"),
        _nested(summary, "chat", "id"),
        _nested(summary, "payment", "chat_id"),
        _nested(summary, "result", "chat_id"),
        _nested(summary, "context", "chat_id"),
        _nested(summary, "session", "chat_id"),
    )


def _participant_ids_from_summary(summary: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for value in (
        summary.get("participant_user_ids"),
        summary.get("paid_user_ids"),
        summary.get("player_user_ids"),
        _nested(summary, "result", "participant_user_ids"),
        _nested(summary, "result", "paid_user_ids"),
        _nested(summary, "result", "player_user_ids"),
    ):
        ids.update(_ints_from_value(value))
    for value in (
        summary.get("started_by_user_id"),
        summary.get("payer_user_id"),
        _nested(summary, "result", "started_by_user_id"),
    ):
        user_id = _int_or_none(value)
        if user_id is not None:
            ids.add(user_id)
    return ids


def _payout_participant_ids_from_summary(summary: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for value in (
        summary.get("reply_to_user_id"),
        summary.get("receiver_user_id"),
        _nested(summary, "result", "reply_to_user_id"),
        _nested(summary, "result", "receiver_user_id"),
    ):
        user_id = _int_or_none(value)
        if user_id is not None:
            ids.add(user_id)
    return ids


def _ints_from_value(value: Any) -> set[int]:
    out: set[int] = set()
    if isinstance(value, (list, tuple, set)):
        for item in value:
            parsed = _int_or_none(item)
            if parsed is not None:
                out.add(parsed)
        return out
    parsed = _int_or_none(value)
    if parsed is not None:
        out.add(parsed)
    return out


def _apply_ledger_total(acc: _StatsAccumulator, summary: ledger_service.LedgerSummary) -> None:
    acc.ledger_income = _decimal_from_text(summary.income)
    acc.ledger_payout = _decimal_from_text(summary.payout)
    acc.ledger_net = _decimal_from_text(summary.net)
    acc.ledger_count = int(summary.count)


def _apply_ledger_buckets(
    buckets: dict[str, _StatsAccumulator],
    ledger_buckets: list[ledger_service.LedgerSummaryBucket],
) -> None:
    for item in ledger_buckets:
        bucket = buckets.setdefault(item.key, _StatsAccumulator(key=item.key, label=item.label))
        bucket.label = item.label
        bucket.ledger_income = _decimal_from_text(item.income)
        bucket.ledger_payout = _decimal_from_text(item.payout)
        bucket.ledger_net = _decimal_from_text(item.net)
        bucket.ledger_count = int(item.count)


def _total_from_acc(acc: _StatsAccumulator) -> OperationalStatsTotal:
    attempts = _payout_attempt_count(acc)
    return OperationalStatsTotal(
        started_sessions=len(acc.session_start_keys),
        participant_count=len(acc.participant_user_ids),
        payout_success_count=acc.payout_success_count,
        payout_failure_count=acc.payout_failure_count,
        payout_attempt_count=attempts,
        payout_success_rate=_payout_success_rate(acc.payout_success_count, attempts),
        ledger_income=str(acc.ledger_income),
        ledger_payout=str(acc.ledger_payout),
        ledger_net=str(acc.ledger_net),
        ledger_count=acc.ledger_count,
    )


def _bucket_from_acc(acc: _StatsAccumulator) -> OperationalStatsBucket:
    attempts = _payout_attempt_count(acc)
    return OperationalStatsBucket(
        key=acc.key,
        label=acc.label,
        started_sessions=len(acc.session_start_keys),
        payout_success_count=acc.payout_success_count,
        payout_failure_count=acc.payout_failure_count,
        payout_attempt_count=attempts,
        payout_success_rate=_payout_success_rate(acc.payout_success_count, attempts),
        ledger_income=str(acc.ledger_income),
        ledger_payout=str(acc.ledger_payout),
        ledger_net=str(acc.ledger_net),
        ledger_count=acc.ledger_count,
    )


def _buckets_from_acc(
    buckets: dict[str, _StatsAccumulator],
    *,
    sort_key: Any,
    reverse: bool = False,
) -> list[OperationalStatsBucket]:
    out = [_bucket_from_acc(item) for item in buckets.values()]
    return sorted(out, key=sort_key, reverse=reverse)


def _payout_attempt_count(acc: _StatsAccumulator) -> int:
    return acc.payout_success_count + acc.payout_failure_count


def _payout_success_rate(success: int, attempts: int) -> str | None:
    if attempts <= 0:
        return None
    rate = (Decimal(success) * Decimal("100") / Decimal(attempts)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(rate)


def _decimal_from_text(value: str) -> Decimal:
    return Decimal(str(value or "0"))


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


__all__ = [
    "METRIC_STATUS_AVAILABLE",
    "METRIC_STATUS_NEEDS_INSTRUMENTATION",
    "MetricAvailability",
    "OperationalStats",
    "OperationalStatsBucket",
    "OperationalStatsTotal",
    "StatsFilters",
    "summarize_operational_stats",
]
