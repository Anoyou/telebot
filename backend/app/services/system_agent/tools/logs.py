"""日志只读工具。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select

from ....db.models.log import RuntimeLog
from ....services.redactor import redact_text, redact_value
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit, mark_external_fields, mark_external_text


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _runtime_view(row: RuntimeLog) -> dict[str, Any]:
    message = redact_text(row.message or "")
    detail = redact_value(row.detail) if row.detail else None
    if isinstance(detail, str):
        detail = mark_external_text(detail)
    elif isinstance(detail, dict):
        detail = mark_external_fields(detail, {"message", "detail", "text", "body", "error"})
    return {
        "id": row.id,
        "ts": row.ts.isoformat() if row.ts else None,
        "account_id": row.account_id,
        "level": row.level,
        "source": row.source,
        "message": mark_external_text(message),
        "detail": detail,
    }


async def recent_logs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=20, maximum=500)
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    level = str(args.get("level") or "").strip().upper() or None
    q = select(RuntimeLog).order_by(desc(RuntimeLog.ts)).limit(limit)
    if account_id is not None:
        q = q.where(RuntimeLog.account_id == account_id)
    if level:
        q = q.where(RuntimeLog.level == level)
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    return {"count": len(rows), "limit": limit, "logs": [_runtime_view(r) for r in rows]}


async def search_errors(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=20, maximum=500)
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    since = _parse_dt(args.get("since"))
    until = _parse_dt(args.get("until"))
    # 大范围搜索必须带时间窗
    if since is None and until is None:
        since = datetime.now(UTC) - timedelta(hours=24)
        time_window_note = "未提供时间窗，默认最近 24 小时"
    else:
        time_window_note = None
    keyword = str(args.get("keyword") or "").strip() or None

    q = (
        select(RuntimeLog)
        .where(RuntimeLog.level.in_(["ERROR", "error", "WARN", "warn", "WARNING", "warning"]))
        .order_by(desc(RuntimeLog.ts))
        .limit(limit)
    )
    if account_id is not None:
        q = q.where(RuntimeLog.account_id == account_id)
    if since is not None:
        q = q.where(RuntimeLog.ts >= since)
    if until is not None:
        q = q.where(RuntimeLog.ts < until)
    if keyword:
        q = q.where(RuntimeLog.message.ilike(f"%{keyword}%"))
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    out: dict[str, Any] = {
        "count": len(rows),
        "limit": limit,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "logs": [_runtime_view(r) for r in rows],
    }
    if time_window_note:
        out["note"] = time_window_note
    return out


async def get_event_detail(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """优先查 RuntimeLog；若提供 action_event_id 则查 ActionEvent。"""

    runtime_id = args.get("runtime_log_id") or args.get("id")
    action_event_id = args.get("action_event_id")

    if action_event_id not in (None, ""):
        try:
            from ....db.models.action_event import ActionEvent as AE

            ae_id = int(action_event_id)
            row = await ctx.db.get(AE, ae_id)
            if row is None:
                return {"error": "not_found", "message": f"ActionEvent {ae_id} 不存在"}
            if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
                return {"error": "forbidden", "message": "无权查看其他账号事件"}
            return {
                "type": "action_event",
                "event": {
                    "id": row.id,
                    "account_id": row.account_id,
                    "channel": row.channel,
                    "plugin_key": row.plugin_key,
                    "action_type": row.action_type,
                    "status": row.status,
                    "error_code": row.error_code,
                    "error_summary": (
                        mark_external_text(redact_text(row.error_summary or ""))
                        if row.error_summary
                        else None
                    ),
                    "params_summary": redact_value(row.params_summary or {}),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": "lookup_failed", "message": str(exc)[:300]}

    if runtime_id in (None, ""):
        return {"error": "id_required", "message": "需要 runtime_log_id 或 action_event_id"}
    try:
        rid = int(runtime_id)
    except (TypeError, ValueError):
        return {"error": "invalid_id", "message": "id 必须是整数"}
    row = await ctx.db.get(RuntimeLog, rid)
    if row is None:
        return {"error": "not_found", "message": f"RuntimeLog {rid} 不存在"}
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        return {"error": "forbidden", "message": "无权查看其他账号日志"}
    return {"type": "runtime_log", "log": _runtime_view(row)}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="logs.recent",
            description="获取最近运行日志。默认 20 条，最大 500 条；返回打码摘要。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "level": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=recent_logs,
        )
    )
    registry.register(
        ToolSpec(
            name="logs.search_errors",
            description="搜索错误/警告日志。大范围搜索应提供 since/until 时间窗；默认最近 24 小时。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "keyword": {"type": "string"},
                    "since": {"type": "string", "description": "ISO 时间"},
                    "until": {"type": "string", "description": "ISO 时间"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=search_errors,
        )
    )
    registry.register(
        ToolSpec(
            name="logs.get_event_detail",
            description="获取单条运行日志或 ActionEvent 详情（打码）。",
            input_schema={
                "type": "object",
                "properties": {
                    "runtime_log_id": {"type": "integer"},
                    "action_event_id": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_event_detail,
        )
    )
