"""日志查询 API（PRD §9.6）。

涵盖：
  - ``GET /api/logs/audit``：操作日志（Web 端 Action）
  - ``GET /api/logs/runtime``：运行日志（worker 输出，由 supervisor 批量消费 stream 落库）

只读接口，鉴权后返回最近一段时间的日志列表，按 ts 倒序。前端在 Dashboard
摘要卡 + 日志页过滤都使用本路由。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, cast, desc, func, or_, select

from ..db.models.log import AuditLog, EventAction, EventSpan, EventTrace, PluginRuntimeStatus, RuntimeLog
from ..deps import CurrentUser, DBSession
from ..services.event_probe import build_event_probe_report
from ..services.redactor import redact_text, redact_value

router = APIRouter(tags=["logs"])


# ── 出参 ─────────────────────────────────────────────────────────
class AuditLogItem(BaseModel):
    """审计（操作）日志条目。"""

    id: int
    ts: datetime
    user_id: int | None
    action: str
    target: str | None
    detail: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class RuntimeLogItem(BaseModel):
    """运行日志条目（worker 上抛）。"""

    id: int
    ts: datetime
    # 兼容字段：前端 E 已使用 ``created_at``，这里同步输出，避免破坏现有页面
    created_at: datetime
    account_id: int | None
    level: str
    source: str | None
    message: str
    detail: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: RuntimeLog) -> RuntimeLogItem:
        return cls(
            id=row.id,
            ts=row.ts,
            created_at=row.ts,
            account_id=row.account_id,
            level=row.level,
            source=row.source,
            message=redact_text(row.message),
            detail=redact_value(row.detail) if row.detail is not None else None,
        )

    model_config = ConfigDict(from_attributes=True)


class EventSpanItem(BaseModel):
    id: int
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    phase: str
    component: str | None = None
    plugin_key: str | None = None
    entry_key: str | None = None
    status: str
    reason_code: str | None = None
    message: str | None = None
    detail: dict[str, Any] | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None

    @classmethod
    def from_row(cls, row: EventSpan) -> EventSpanItem:
        return cls(
            id=row.id,
            span_id=row.span_id,
            trace_id=row.trace_id,
            parent_span_id=row.parent_span_id,
            phase=row.phase,
            component=row.component,
            plugin_key=row.plugin_key,
            entry_key=row.entry_key,
            status=row.status,
            reason_code=row.reason_code,
            message=redact_text(row.message or "") or None,
            detail=redact_value(row.detail) if row.detail is not None else None,
            started_at=row.started_at,
            ended_at=row.ended_at,
            duration_ms=row.duration_ms,
        )


class EventActionItem(BaseModel):
    id: int
    action_id: str
    trace_id: str
    plugin_key: str | None = None
    action_type: str
    requested_send_via: str | None = None
    actual_send_via: str | None = None
    target_chat_id: int | None = None
    target_message_id: int | None = None
    status: str
    telegram_message_id: int | None = None
    inline_result_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    detail: dict[str, Any] | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row: EventAction) -> EventActionItem:
        return cls(
            id=row.id,
            action_id=row.action_id,
            trace_id=row.trace_id,
            plugin_key=row.plugin_key,
            action_type=row.action_type,
            requested_send_via=row.requested_send_via,
            actual_send_via=row.actual_send_via,
            target_chat_id=row.target_chat_id,
            target_message_id=row.target_message_id,
            status=row.status,
            telegram_message_id=row.telegram_message_id,
            inline_result_count=row.inline_result_count,
            error_code=row.error_code,
            error_message=redact_text(row.error_message or "") or None,
            detail=redact_value(row.detail) if row.detail is not None else None,
            created_at=row.created_at,
        )


class EventTraceSummary(BaseModel):
    id: int
    trace_id: str
    account_id: int | None = None
    source_channel: str | None = None
    event_type: str
    chat_id: int | None = None
    message_id: int | None = None
    update_id: int | None = None
    callback_query_id: str | None = None
    sender_user_id: int | None = None
    sender_name: str | None = None
    text_preview: str | None = None
    inline_query: str | None = None
    chosen_inline_result_id: str | None = None
    chosen_inline_query: str | None = None
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    native_raw_meta: dict[str, Any] | None = None
    plugin_count: int = 0
    action_count: int = 0
    error_count: int = 0

    @classmethod
    def from_row(cls, row: EventTrace) -> EventTraceSummary:
        inline_query, chosen_inline_result_id, chosen_inline_query = _inline_trace_summary(row)
        return cls(
            id=row.id,
            trace_id=row.trace_id,
            account_id=row.account_id,
            source_channel=row.source_channel,
            event_type=row.event_type,
            chat_id=row.chat_id,
            message_id=row.message_id,
            update_id=row.update_id,
            callback_query_id=row.callback_query_id,
            sender_user_id=row.sender_user_id,
            sender_name=row.sender_name,
            text_preview=redact_text(row.text_preview or "") or None,
            inline_query=inline_query,
            chosen_inline_result_id=chosen_inline_result_id,
            chosen_inline_query=chosen_inline_query,
            status=row.status,
            started_at=row.started_at,
            ended_at=row.ended_at,
            duration_ms=row.duration_ms,
            native_raw_meta=redact_value(row.native_raw_meta) if row.native_raw_meta is not None else None,
        )


def _nested_text(source: Any, *path: str) -> str | None:
    current = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    text = str(current).strip()
    return redact_text(text) or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return redact_text(text) or None
    return None


def _inline_trace_summary(row: EventTrace) -> tuple[str | None, str | None, str | None]:
    payload = row.payload_snapshot if isinstance(row.payload_snapshot, dict) else {}
    raw = row.raw_summary if isinstance(row.raw_summary, dict) else {}
    event_type = str(row.event_type or "")
    inline_query = _first_text(
        _nested_text(payload, "inline_query", "query"),
        _nested_text(raw, "inline_query", "query"),
        raw.get("query") if event_type == "inline_query" else None,
        row.text_preview if event_type == "inline_query" else None,
    )
    chosen_inline_result_id = _first_text(
        _nested_text(payload, "chosen_inline_result", "result_id"),
        _nested_text(raw, "chosen_inline_result", "result_id"),
        _nested_text(payload, "chosen_inline_result", "id"),
        _nested_text(raw, "chosen_inline_result", "id"),
    )
    chosen_inline_query = _first_text(
        _nested_text(payload, "chosen_inline_result", "query"),
        _nested_text(raw, "chosen_inline_result", "query"),
        raw.get("query") if event_type == "chosen_inline_result" else None,
        row.text_preview if event_type == "chosen_inline_result" else None,
    )
    return inline_query, chosen_inline_result_id, chosen_inline_query


class EventTraceDetail(EventTraceSummary):
    raw_summary: dict[str, Any] | None = None
    payload_snapshot: dict[str, Any] | None = None
    probe_report: dict[str, Any] | None = None
    spans: list[EventSpanItem] = []
    actions: list[EventActionItem] = []
    related_runtime_logs: list[RuntimeLogItem] = []


class PluginRuntimeStatusItem(BaseModel):
    id: int
    plugin_key: str
    account_id: int | None = None
    enabled: bool
    installed_version: str | None = None
    load_status: str
    last_load_error: str | None = None
    last_invoked_at: datetime | None = None
    last_invocation_status: str | None = None
    last_trace_id: str | None = None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: PluginRuntimeStatus) -> PluginRuntimeStatusItem:
        return cls(
            id=row.id,
            plugin_key=row.plugin_key,
            account_id=row.account_id,
            enabled=row.enabled,
            installed_version=row.installed_version,
            load_status=row.load_status,
            last_load_error=redact_text(row.last_load_error or "") or None,
            last_invoked_at=row.last_invoked_at,
            last_invocation_status=row.last_invocation_status,
            last_trace_id=row.last_trace_id,
            updated_at=row.updated_at,
        )


class TraceOverview(BaseModel):
    last_5m_total: int = 0
    last_5m_failed: int = 0
    last_5m_warning: int = 0
    source_channel_counts: dict[str, int] = Field(default_factory=dict)
    recent_errors: list[EventTraceSummary] = []
    recent_failed_actions: list[EventActionItem] = []
    recent_plugin_errors: list[PluginRuntimeStatusItem] = []


async def _trace_summaries_with_counts(db: DBSession, rows: list[EventTrace]) -> list[EventTraceSummary]:
    summaries = [EventTraceSummary.from_row(row) for row in rows]
    trace_ids = [item.trace_id for item in summaries]
    if not trace_ids:
        return summaries
    plugin_counts = {
        trace_id: int(count or 0)
        for trace_id, count in (
            await db.execute(
                select(EventSpan.trace_id, func.count(func.distinct(EventSpan.plugin_key)))
                .where(EventSpan.trace_id.in_(trace_ids), EventSpan.plugin_key.is_not(None))
                .group_by(EventSpan.trace_id)
            )
        ).all()
    }
    action_counts = {
        trace_id: int(count or 0)
        for trace_id, count in (
            await db.execute(
                select(EventAction.trace_id, func.count(EventAction.id))
                .where(EventAction.trace_id.in_(trace_ids))
                .group_by(EventAction.trace_id)
            )
        ).all()
    }
    span_error_counts = {
        trace_id: int(count or 0)
        for trace_id, count in (
            await db.execute(
                select(EventSpan.trace_id, func.count(EventSpan.id))
                .where(
                    EventSpan.trace_id.in_(trace_ids),
                    EventSpan.status.in_(("failed", "error", "warning", "warn")),
                )
                .group_by(EventSpan.trace_id)
            )
        ).all()
    }
    action_error_counts = {
        trace_id: int(count or 0)
        for trace_id, count in (
            await db.execute(
                select(EventAction.trace_id, func.count(EventAction.id))
                .where(EventAction.trace_id.in_(trace_ids), EventAction.status.in_(("failed", "error")))
                .group_by(EventAction.trace_id)
            )
        ).all()
    }
    for summary in summaries:
        summary.plugin_count = plugin_counts.get(summary.trace_id, 0)
        summary.action_count = action_counts.get(summary.trace_id, 0)
        summary.error_count = span_error_counts.get(summary.trace_id, 0) + action_error_counts.get(summary.trace_id, 0)
    return summaries


# ── /api/logs/audit ──────────────────────────────────────────────
@router.get("/api/logs/audit", response_model=list[AuditLogItem])
async def list_audit_logs(
    db: DBSession,
    _user: CurrentUser,
    user_id: int | None = Query(None, description="按 web_user 过滤"),
    action: str | None = Query(None, description="按 action 精确过滤"),
    target: str | None = Query(None, description="target 模糊匹配"),
    keyword: str | None = Query(None, description="action/target/detail 模糊匹配"),
    detail: str | None = Query(None, description="detail(JSON 字符串)模糊匹配"),
    since: datetime | None = Query(None, description="ISO 时间，仅返回此后的日志"),
    limit: int = Query(50, ge=1, le=500),
) -> list[AuditLogItem]:
    """返回最近的操作日志，按时间倒序。"""
    stmt = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target:
        stmt = stmt.where(AuditLog.target.ilike(f"%{target}%"))
    if detail:
        stmt = stmt.where(cast(AuditLog.detail, String).ilike(f"%{detail}%"))
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                AuditLog.action.ilike(like),
                AuditLog.target.ilike(like),
                cast(AuditLog.detail, String).ilike(like),
            )
        )
    if since is not None:
        stmt = stmt.where(AuditLog.ts >= since)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        AuditLogItem(
            id=r.id,
            ts=r.ts,
            user_id=r.user_id,
            action=r.action,
            target=r.target,
            detail=redact_value(r.detail) if r.detail is not None else None,
        )
        for r in rows
    ]


# ── /api/logs/runtime ────────────────────────────────────────────
# source 别名映射：
#   - 历史数据 source="worker" / "plugin" 一直存在，新代码改写成 "system" / "event"
#   - 前端只暴露 "system" / "event" 两种 tab；这里把请求转换成对应集合
_SOURCE_ALIAS: dict[str, tuple[str, ...]] = {
    "system": ("system", "worker"),
    "event": ("event",),
    "plugin": ("plugin",),
}


@router.get("/api/logs/trace/overview", response_model=TraceOverview)
async def trace_overview(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None),
) -> TraceOverview:
    since = datetime.now(UTC) - timedelta(minutes=5)
    base = [EventTrace.started_at >= since]
    if account_id is not None:
        base.append(EventTrace.account_id == account_id)
    total = int((await db.execute(select(func.count(EventTrace.id)).where(*base))).scalar_one() or 0)
    failed = int(
        (
            await db.execute(
                select(func.count(EventTrace.id)).where(*base, EventTrace.status.in_(("failed", "error")))
            )
        ).scalar_one()
        or 0
    )
    warning = int(
        (
            await db.execute(
                select(func.count(EventTrace.id)).where(*base, EventTrace.status.in_(("warning", "warn")))
            )
        ).scalar_one()
        or 0
    )
    source_channel_counts = {
        str(channel or "unknown"): int(count or 0)
        for channel, count in (
            await db.execute(
                select(EventTrace.source_channel, func.count(EventTrace.id))
                .where(*base)
                .group_by(EventTrace.source_channel)
            )
        ).all()
    }
    error_stmt = select(EventTrace).where(EventTrace.status.in_(("failed", "error", "warning", "warn")))
    if account_id is not None:
        error_stmt = error_stmt.where(EventTrace.account_id == account_id)
    error_rows = (await db.execute(error_stmt.order_by(desc(EventTrace.started_at)).limit(8))).scalars().all()

    action_stmt = select(EventAction).where(EventAction.status.in_(("failed", "error")))
    if account_id is not None:
        action_stmt = action_stmt.where(
            EventAction.trace_id.in_(select(EventTrace.trace_id).where(EventTrace.account_id == account_id))
        )
    action_rows = (await db.execute(action_stmt.order_by(desc(EventAction.created_at)).limit(8))).scalars().all()

    plugin_stmt = select(PluginRuntimeStatus).where(
        PluginRuntimeStatus.load_status.in_(("failed", "error")),
    )
    if account_id is not None:
        plugin_stmt = plugin_stmt.where(PluginRuntimeStatus.account_id == account_id)
    plugin_rows = (await db.execute(plugin_stmt.order_by(desc(PluginRuntimeStatus.updated_at)).limit(8))).scalars().all()
    return TraceOverview(
        last_5m_total=total,
        last_5m_failed=failed,
        last_5m_warning=warning,
        source_channel_counts=source_channel_counts,
        recent_errors=await _trace_summaries_with_counts(db, error_rows),
        recent_failed_actions=[EventActionItem.from_row(row) for row in action_rows],
        recent_plugin_errors=[PluginRuntimeStatusItem.from_row(row) for row in plugin_rows],
    )


@router.get("/api/logs/trace/events", response_model=list[EventTraceSummary])
async def list_event_traces(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None),
    source_channel: str | None = Query(None),
    event_type: str | None = Query(None),
    chat_id: int | None = Query(None),
    message_id: int | None = Query(None),
    update_id: int | None = Query(None),
    sender_user_id: int | None = Query(None),
    plugin_key: str | None = Query(None),
    status: str | None = Query(None),
    trace_id: str | None = Query(None),
    reason_code: str | None = Query(None),
    keyword: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[EventTraceSummary]:
    stmt = select(EventTrace).order_by(desc(EventTrace.started_at)).limit(limit)
    if account_id is not None:
        stmt = stmt.where(EventTrace.account_id == account_id)
    if source_channel:
        stmt = stmt.where(EventTrace.source_channel == source_channel)
    if event_type:
        stmt = stmt.where(EventTrace.event_type == event_type)
    if chat_id is not None:
        stmt = stmt.where(EventTrace.chat_id == chat_id)
    if message_id is not None:
        stmt = stmt.where(EventTrace.message_id == message_id)
    if update_id is not None:
        stmt = stmt.where(EventTrace.update_id == update_id)
    if sender_user_id is not None:
        stmt = stmt.where(EventTrace.sender_user_id == sender_user_id)
    if status:
        stmt = stmt.where(EventTrace.status == status)
    if trace_id:
        stmt = stmt.where(EventTrace.trace_id == trace_id)
    if reason_code:
        stmt = stmt.where(
            or_(
                EventTrace.trace_id.in_(
                    select(EventSpan.trace_id).where(EventSpan.reason_code == reason_code)
                ),
                EventTrace.trace_id.in_(
                    select(EventAction.trace_id).where(EventAction.error_code == reason_code)
                ),
            )
        )
    if since is not None:
        stmt = stmt.where(EventTrace.started_at >= since)
    if until is not None:
        stmt = stmt.where(EventTrace.started_at <= until)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                EventTrace.trace_id.ilike(like),
                EventTrace.sender_name.ilike(like),
                EventTrace.text_preview.ilike(like),
                cast(EventTrace.raw_summary, String).ilike(like),
            )
        )
    if plugin_key:
        stmt = stmt.where(
            EventTrace.trace_id.in_(
                select(EventSpan.trace_id).where(EventSpan.plugin_key == plugin_key)
            )
        )
    rows = (await db.execute(stmt)).scalars().all()
    return await _trace_summaries_with_counts(db, rows)


@router.get("/api/logs/trace/events/{trace_id}", response_model=EventTraceDetail)
async def get_event_trace(
    trace_id: str,
    db: DBSession,
    _user: CurrentUser,
) -> EventTraceDetail:
    row = (await db.execute(select(EventTrace).where(EventTrace.trace_id == trace_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="trace 不存在")
    spans = (
        await db.execute(
            select(EventSpan).where(EventSpan.trace_id == trace_id).order_by(EventSpan.started_at, EventSpan.id)
        )
    ).scalars().all()
    actions = (
        await db.execute(
            select(EventAction).where(EventAction.trace_id == trace_id).order_by(EventAction.created_at, EventAction.id)
        )
    ).scalars().all()
    logs = (
        await db.execute(
            select(RuntimeLog)
            .where(RuntimeLog.detail["trace_id"].as_string() == trace_id)
            .order_by(RuntimeLog.ts.desc())
            .limit(50)
        )
    ).scalars().all()
    summary = EventTraceSummary.from_row(row)
    summary.plugin_count = len({item.plugin_key for item in spans if item.plugin_key})
    summary.action_count = len(actions)
    summary.error_count = sum(1 for item in spans if item.status in {"failed", "error", "warning", "warn"})
    summary.error_count += sum(1 for item in actions if item.status in {"failed", "error"})
    raw_summary = redact_value(row.raw_summary) if row.raw_summary is not None else None
    payload_snapshot = redact_value(row.payload_snapshot) if row.payload_snapshot is not None else None
    native_raw_meta = redact_value(row.native_raw_meta) if row.native_raw_meta is not None else None
    span_items = [EventSpanItem.from_row(item) for item in spans]
    action_items = [EventActionItem.from_row(item) for item in actions]
    return EventTraceDetail(
        **summary.model_dump(),
        raw_summary=raw_summary,
        payload_snapshot=payload_snapshot,
        probe_report=build_event_probe_report(
            trace=summary.model_dump(),
            raw_summary=raw_summary,
            payload_snapshot=payload_snapshot,
            native_raw_meta=native_raw_meta,
            spans=span_items,
            actions=action_items,
        ),
        spans=span_items,
        actions=action_items,
        related_runtime_logs=[RuntimeLogItem.from_row(item) for item in logs],
    )


@router.get("/api/logs/trace/plugins", response_model=list[PluginRuntimeStatusItem])
async def list_plugin_runtime_status(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None),
    plugin_key: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[PluginRuntimeStatusItem]:
    stmt = select(PluginRuntimeStatus).order_by(desc(PluginRuntimeStatus.updated_at)).limit(limit)
    if account_id is not None:
        stmt = stmt.where(PluginRuntimeStatus.account_id == account_id)
    if plugin_key:
        stmt = stmt.where(PluginRuntimeStatus.plugin_key == plugin_key)
    if status:
        stmt = stmt.where(PluginRuntimeStatus.load_status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [PluginRuntimeStatusItem.from_row(row) for row in rows]


@router.get("/api/logs/trace/plugins/{plugin_key}")
async def get_plugin_runtime_detail(
    plugin_key: str,
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None),
) -> dict[str, Any]:
    status_stmt = select(PluginRuntimeStatus).where(PluginRuntimeStatus.plugin_key == plugin_key)
    if account_id is not None:
        status_stmt = status_stmt.where(PluginRuntimeStatus.account_id == account_id)
    statuses = (await db.execute(status_stmt.order_by(desc(PluginRuntimeStatus.updated_at)))).scalars().all()
    spans_stmt = select(EventSpan).where(EventSpan.plugin_key == plugin_key).order_by(desc(EventSpan.started_at)).limit(20)
    if account_id is not None:
        spans_stmt = spans_stmt.where(
            EventSpan.trace_id.in_(select(EventTrace.trace_id).where(EventTrace.account_id == account_id))
        )
    spans = (await db.execute(spans_stmt)).scalars().all()
    trace_ids = [item.trace_id for item in spans]
    traces = []
    if trace_ids:
        trace_rows = (
            await db.execute(select(EventTrace).where(EventTrace.trace_id.in_(trace_ids)).order_by(desc(EventTrace.started_at)))
        ).scalars().all()
        traces = await _trace_summaries_with_counts(db, trace_rows)
    return {
        "statuses": [PluginRuntimeStatusItem.from_row(row).model_dump(mode="json") for row in statuses],
        "recent_spans": [EventSpanItem.from_row(row).model_dump(mode="json") for row in spans],
        "recent_traces": [item.model_dump(mode="json") for item in traces],
    }


@router.get("/api/logs/trace/actions", response_model=list[EventActionItem])
async def list_event_actions(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None),
    trace_id: str | None = Query(None),
    plugin_key: str | None = Query(None),
    action_type: str | None = Query(None),
    status: str | None = Query(None),
    reason_code: str | None = Query(None),
    error_code: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[EventActionItem]:
    stmt = select(EventAction).order_by(desc(EventAction.created_at)).limit(limit)
    if trace_id:
        stmt = stmt.where(EventAction.trace_id == trace_id)
    if plugin_key:
        stmt = stmt.where(EventAction.plugin_key == plugin_key)
    if action_type:
        stmt = stmt.where(EventAction.action_type == action_type)
    if status:
        stmt = stmt.where(EventAction.status == status)
    action_reason = reason_code or error_code
    if action_reason:
        stmt = stmt.where(EventAction.error_code == action_reason)
    if since is not None:
        stmt = stmt.where(EventAction.created_at >= since)
    if until is not None:
        stmt = stmt.where(EventAction.created_at <= until)
    if account_id is not None:
        stmt = stmt.where(
            EventAction.trace_id.in_(select(EventTrace.trace_id).where(EventTrace.account_id == account_id))
        )
    rows = (await db.execute(stmt)).scalars().all()
    return [EventActionItem.from_row(row) for row in rows]


@router.get("/api/logs/trace/commands", response_model=list[EventTraceSummary])
async def list_command_traces(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None),
    keyword: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    reason_code: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[EventTraceSummary]:
    stmt = (
        select(EventTrace)
        .where(EventTrace.event_type.in_(("command", "admin_command", "sudo_command")))
        .order_by(desc(EventTrace.started_at))
        .limit(limit)
    )
    if account_id is not None:
        stmt = stmt.where(EventTrace.account_id == account_id)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(EventTrace.text_preview.ilike(like), EventTrace.trace_id.ilike(like)))
    if since is not None:
        stmt = stmt.where(EventTrace.started_at >= since)
    if until is not None:
        stmt = stmt.where(EventTrace.started_at <= until)
    if reason_code:
        stmt = stmt.where(
            or_(
                EventTrace.trace_id.in_(
                    select(EventSpan.trace_id).where(EventSpan.reason_code == reason_code)
                ),
                EventTrace.trace_id.in_(
                    select(EventAction.trace_id).where(EventAction.error_code == reason_code)
                ),
            )
        )
    rows = (await db.execute(stmt)).scalars().all()
    return await _trace_summaries_with_counts(db, rows)


@router.get("/api/logs/runtime", response_model=list[RuntimeLogItem])
async def list_runtime_logs(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None, description="按账号过滤"),
    level: str | None = Query(None, description="debug | info | warn | warning | error"),
    plugin_key: str | None = Query(None, description="按插件 key 过滤，仅 source=plugin 时常用"),
    source: str | None = Query(
        None,
        description='日志类别："event"（消息事件）/"plugin"（插件内部日志）/"system"（worker 启停/错误）',
    ),
    since: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[RuntimeLogItem]:
    """返回最近运行日志。

    兼容前端传 ``level=warning``：内部映射为 ``level >= 'warn'``（warn + error）。
    ``source`` 支持 ``"event"`` / ``"plugin"`` / ``"system"`` 三种 tab。
    """
    stmt = select(RuntimeLog).order_by(RuntimeLog.ts.desc()).limit(limit)
    if account_id is not None:
        stmt = stmt.where(RuntimeLog.account_id == account_id)
    if since is not None:
        stmt = stmt.where(RuntimeLog.ts >= since)
    if level:
        norm = level.lower()
        if norm == "warning":
            stmt = stmt.where(RuntimeLog.level.in_(("warn", "warning", "error")))
        else:
            stmt = stmt.where(RuntimeLog.level == norm)
    if source:
        aliases = _SOURCE_ALIAS.get(source.lower())
        if aliases is not None:
            stmt = stmt.where(RuntimeLog.source.in_(aliases))
        else:
            stmt = stmt.where(RuntimeLog.source == source)
    if plugin_key:
        stmt = stmt.where(RuntimeLog.detail["plugin_key"].as_string() == plugin_key)
    rows = (await db.execute(stmt)).scalars().all()
    return [RuntimeLogItem.from_row(r) for r in rows]
