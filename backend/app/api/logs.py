"""日志查询 API（PRD §9.6）。

涵盖：
  - ``GET /api/logs/audit``：操作日志（Web 端 Action）
  - ``GET /api/logs/runtime``：运行日志（worker 输出，由 supervisor 批量消费 stream 落库）

只读接口，鉴权后返回最近一段时间的日志列表，按 ts 倒序。前端在 Dashboard
摘要卡 + 日志页过滤都使用本路由。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import String, cast, desc, func, or_, select

from ..db.models.log import AuditLog, EventAction, EventSpan, EventTrace, RuntimeLog
from ..deps import CurrentUser, DBSession
from ..services.event_probe import build_event_probe_report
from ..services.log_funel import MessageFunel, build_message_funel
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


class MessageFunelOut(BaseModel):
    received: str
    routed: str
    ran: str
    sent: str
    verdict: str
    stuck_at: str | None = None
    reason_code: str | None = None
    reason_text: str
    next_step: str

    @classmethod
    def from_funel(cls, funel: MessageFunel) -> MessageFunelOut:
        return cls(**funel.model_dump())


class MessageFunelItem(EventTraceSummary):
    funel: MessageFunelOut
    verdict: str
    stuck_at: str | None = None
    reason_code: str | None = None
    reason_text: str
    next_step: str


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


def _event_trace_stmt(
    *,
    account_id: int | None = None,
    source_channel: str | None = None,
    event_type: str | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    update_id: int | None = None,
    sender_user_id: int | None = None,
    plugin_key: str | None = None,
    status: str | None = None,
    trace_id: str | None = None,
    reason_code: str | None = None,
    keyword: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
):
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
    return stmt


async def _span_action_groups(
    db: DBSession,
    trace_ids: list[str],
) -> tuple[dict[str, list[EventSpan]], dict[str, list[EventAction]]]:
    if not trace_ids:
        return {}, {}
    span_rows = (
        await db.execute(
            select(EventSpan)
            .where(EventSpan.trace_id.in_(trace_ids))
            .order_by(EventSpan.trace_id, EventSpan.started_at, EventSpan.id)
        )
    ).scalars().all()
    action_rows = (
        await db.execute(
            select(EventAction)
            .where(EventAction.trace_id.in_(trace_ids))
            .order_by(EventAction.trace_id, EventAction.created_at, EventAction.id)
        )
    ).scalars().all()
    spans_by_trace: dict[str, list[EventSpan]] = {trace_id: [] for trace_id in trace_ids}
    actions_by_trace: dict[str, list[EventAction]] = {trace_id: [] for trace_id in trace_ids}
    for span in span_rows:
        spans_by_trace.setdefault(span.trace_id, []).append(span)
    for action in action_rows:
        actions_by_trace.setdefault(action.trace_id, []).append(action)
    return spans_by_trace, actions_by_trace


def _message_funel_item(
    row: EventTrace,
    spans: list[EventSpan],
    actions: list[EventAction],
) -> MessageFunelItem:
    summary = EventTraceSummary.from_row(row)
    summary.plugin_count = len({item.plugin_key for item in spans if item.plugin_key})
    summary.action_count = len(actions)
    summary.error_count = sum(1 for item in spans if item.status in {"failed", "error", "warning", "warn"})
    summary.error_count += sum(1 for item in actions if item.status in {"failed", "error"})
    funel = build_message_funel(row, spans, actions)
    funel_out = MessageFunelOut.from_funel(funel)
    return MessageFunelItem(
        **summary.model_dump(),
        funel=funel_out,
        verdict=funel.verdict,
        stuck_at=funel.stuck_at,
        reason_code=funel.reason_code,
        reason_text=funel.reason_text,
        next_step=funel.next_step,
    )


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
    stmt = _event_trace_stmt(
        account_id=account_id,
        source_channel=source_channel,
        event_type=event_type,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        sender_user_id=sender_user_id,
        plugin_key=plugin_key,
        status=status,
        trace_id=trace_id,
        reason_code=reason_code,
        keyword=keyword,
        since=since,
        until=until,
        limit=limit,
    )
    rows = (await db.execute(stmt)).scalars().all()
    return await _trace_summaries_with_counts(db, rows)


@router.get("/api/logs/messages", response_model=list[MessageFunelItem])
async def list_log_messages(
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
    verdict: str | None = Query(None),
    keyword: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[MessageFunelItem]:
    """返回一页式消息流。

    复用 trace 列表过滤条件，然后批量读取 span/action 计算四段漏斗。
    verdict 是派生字段，先取一小段窗口后在 Python 侧过滤。
    """
    fetch_limit = min(500, limit * 3) if verdict else limit
    stmt = _event_trace_stmt(
        account_id=account_id,
        source_channel=source_channel,
        event_type=event_type,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        sender_user_id=sender_user_id,
        plugin_key=plugin_key,
        status=status,
        trace_id=trace_id,
        reason_code=reason_code,
        keyword=keyword,
        since=since,
        until=until,
        limit=fetch_limit,
    )
    rows = (await db.execute(stmt)).scalars().all()
    trace_ids = [row.trace_id for row in rows]
    spans_by_trace, actions_by_trace = await _span_action_groups(db, trace_ids)
    items = [
        _message_funel_item(
            row,
            spans_by_trace.get(row.trace_id, []),
            actions_by_trace.get(row.trace_id, []),
        )
        for row in rows
    ]
    if verdict:
        items = [item for item in items if item.verdict == verdict]
    return items[:limit]


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


@router.get("/api/logs/runtime", response_model=list[RuntimeLogItem])
async def list_runtime_logs(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = Query(None, description="按账号过滤"),
    level: str | None = Query(None, description="debug | info | warn | warning | error"),
    plugin_key: str | None = Query(None, description="按插件 key 过滤，仅 source=plugin 时常用"),
    keyword: str | None = Query(None, description="message/source/detail 模糊匹配"),
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
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                RuntimeLog.message.ilike(like),
                RuntimeLog.level.ilike(like),
                RuntimeLog.source.ilike(like),
                cast(RuntimeLog.detail, String).ilike(like),
            )
        )
    rows = (await db.execute(stmt)).scalars().all()
    return [RuntimeLogItem.from_row(r) for r in rows]
