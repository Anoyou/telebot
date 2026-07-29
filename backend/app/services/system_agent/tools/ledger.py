"""资金台账查询、补付核销与重置工作流。"""

from __future__ import annotations

from typing import Any

from ....services import ledger_service
from ....services.ledger_service import LedgerFilters
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit, get_timezone_name, local_day_bounds_utc


def _entry_view(entry: Any) -> dict[str, Any]:
    return {
        "id": entry.id,
        "source": entry.source,
        "direction": entry.direction,
        "amount": entry.amount,
        "signed_amount": entry.signed_amount,
        "status": entry.status,
        "account_id": entry.account_id,
        "chat_id": entry.chat_id,
        "chat_title": entry.chat_title,
        "plugin_key": entry.plugin_key,
        "action_type": entry.action_type,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def _compensation_view(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "payout_key": item.payout_key,
        "account_id": item.account_id,
        "plugin_key": item.plugin_key,
        "chat_id": item.chat_id,
        "chat_title": item.chat_title,
        "receiver_user_id": item.receiver_user_id,
        "receiver_name": item.receiver_name,
        "amount": item.amount,
        "status": item.status,
        "ambiguous": item.ambiguous,
        "retry_count": item.retry_count,
        "error_code_last": item.error_code_last,
        "error_last": item.error_last,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


async def _require_ledger_module(ctx: ToolContext) -> dict[str, Any] | None:
    from ....services import platform_capabilities as platform_caps

    enabled = await platform_caps.is_module_enabled(ctx.db, "ledger")
    if enabled:
        return None
    return {
        "error": "platform_module_disabled",
        "module": "ledger",
        "message": "资金台账模块已暂停，查询与操作面不可用；ActionEvent 与补偿主账仍继续写入。",
    }


async def summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    blocked = await _require_ledger_module(ctx)
    if blocked is not None:
        return blocked
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    tz = await get_timezone_name(ctx.db)
    # “今天/今日”
    day = str(args.get("day") or "today").strip().lower()
    if day in {"today", "今日", "今天"}:
        since, until = local_day_bounds_utc(tz)
        day_label = "today"
    else:
        since = until = None
        day_label = day or "default_window"

    filters = LedgerFilters(
        since=since,
        until=until,
        account_id=account_id,
    )
    result = await ledger_service.summarize_ledger(ctx.db, filters)
    return {
        "timezone": tz,
        "day": day_label,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "account_id": account_id,
        "income": result.income,
        "payout": result.payout,
        "net": result.net,
        "count": result.count,
        "by_day": [
            {
                "key": b.key,
                "label": b.label,
                "income": b.income,
                "payout": b.payout,
                "net": b.net,
                "count": b.count,
            }
            for b in (result.by_day or [])
        ],
        "by_chat": [
            {
                "key": b.key,
                "label": b.label,
                "income": b.income,
                "payout": b.payout,
                "net": b.net,
                "count": b.count,
            }
            for b in (result.by_chat or [])[:20]
        ],
    }


async def list_entries(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    blocked = await _require_ledger_module(ctx)
    if blocked is not None:
        return blocked
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    limit = clamp_limit(args.get("limit"), default=20, maximum=500)
    tz = await get_timezone_name(ctx.db)
    entry_type = str(args.get("type") or args.get("entry_type") or "").strip().lower()

    since = until = None
    day = str(args.get("day") or "").strip().lower()
    if day in {"today", "今日", "今天"}:
        since, until = local_day_bounds_utc(tz)

    if entry_type in {"compensation", "compensations", "补付"}:
        comps = await ledger_service.list_compensations(
            ctx.db,
            account_id=account_id,
            limit=limit,
        )
        items = [_compensation_view(item) for item in comps]
        return {
            "timezone": tz,
            "type": "compensation",
            "count": len(items),
            "entries": items,
        }

    filters = LedgerFilters(
        since=since,
        until=until,
        account_id=account_id,
        limit=limit,
        direction=str(args.get("direction") or "").strip() or None,
    )
    entries = await ledger_service.list_ledger_entries(ctx.db, filters)
    return {
        "timezone": tz,
        "type": "ledger",
        "count": len(entries),
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "entries": [_entry_view(e) for e in entries],
    }


async def manual_paid_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    blocked = await _require_ledger_module(ctx)
    if blocked is not None:
        raise ValueError(str(blocked["message"]))
    compensation_id = int(args.get("id") or args.get("compensation_id"))
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rows = await ledger_service.list_compensations(
        ctx.db,
        account_id=account_id,
        limit=500,
    )
    row = next((item for item in rows if int(item.id) == compensation_id), None)
    if row is None:
        raise ValueError(f"待补付记录 #{compensation_id} 不存在或已关闭")
    return {
        "summary": f"将待补付记录 #{compensation_id} 标记为人工已付",
        "compensation": _compensation_view(row),
        "note": args.get("note"),
        "warning": "该操作会关闭补付队列项，确认前请核对收款人和金额。",
    }


async def manual_paid_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # 同一事务内重新校验账号范围与队列状态，不能只信任预览阶段。
    await manual_paid_preview(ctx, args)
    compensation_id = int(args.get("id") or args.get("compensation_id"))
    row = await ledger_service.mark_compensation_manual_paid(
        ctx.db,
        compensation_id,
        user_id=ctx.web_user_id,
        note=args.get("note"),
    )
    return {
        "compensation": _compensation_view(row),
        "business_changed": True,
    }


async def reset_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    blocked = await _require_ledger_module(ctx)
    if blocked is not None:
        raise ValueError(str(blocked["message"]))
    if args.get("confirmation") != "RESET_LEDGER":
        raise ValueError("重置台账必须明确传入 confirmation=RESET_LEDGER")
    current = await ledger_service.summarize_ledger(ctx.db, LedgerFilters(limit=None))
    compensations = await ledger_service.list_compensations(ctx.db, limit=500)
    return {
        "summary": "清空全部资金台账与补付队列",
        "current": {
            "income": current.income,
            "payout": current.payout,
            "net": current.net,
            "entry_count": current.count,
            "open_compensation_count": len(compensations),
        },
        "warning": "极高风险且不可恢复：将删除台账依赖的资金 ActionEvent 与全部补付记录。",
    }


async def reset_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("confirmation") != "RESET_LEDGER":
        raise ValueError("重置台账必须明确传入 confirmation=RESET_LEDGER")
    result = await ledger_service.reset_ledger_data(ctx.db, user_id=ctx.web_user_id)
    return {
        "deleted_action_events": result.deleted_action_events,
        "deleted_compensations": result.deleted_compensations,
        "business_changed": True,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="ledger.summary",
            description="资金台账汇总。day=today/今日 时按系统时区本地日界线转换 UTC，不使用默认 UTC 回溯。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "day": {
                        "type": "string",
                        "description": "today/今日 表示本地今日；省略则用服务默认窗口",
                    },
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=summary,
        )
    )
    registry.register(
        ToolSpec(
            name="ledger.manual_paid",
            description="把一条待补付记录标记为人工已付。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "compensation_id": {"type": "integer"},
                    "note": {"type": "string", "maxLength": 500},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=manual_paid_preview,
            execute_handler=manual_paid_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="ledger.reset",
            description="清空资金台账与补付队列，必须 confirmation=RESET_LEDGER。",
            input_schema={
                "type": "object",
                "properties": {
                    "confirmation": {
                        "type": "string",
                        "enum": ["RESET_LEDGER"],
                    }
                },
                "required": ["confirmation"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            channels=("web",),
            risk="dangerous",
            preview_handler=reset_preview,
            execute_handler=reset_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="ledger.list",
            description="列出资金流水或补付记录。type=compensation 时返回补付；支持 day=today。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "day": {"type": "string"},
                    "type": {"type": "string", "description": "ledger 或 compensation"},
                    "direction": {"type": "string", "description": "in 或 out"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_entries,
        )
    )
