"""资金台账只读工具。"""

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
            {"key": b.key, "label": b.label, "income": b.income, "payout": b.payout, "net": b.net, "count": b.count}
            for b in (result.by_day or [])
        ],
        "by_chat": [
            {"key": b.key, "label": b.label, "income": b.income, "payout": b.payout, "net": b.net, "count": b.count}
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
        items = []
        for c in comps:
            items.append(
                {
                    "id": getattr(c, "id", None),
                    "type": "compensation",
                    "status": getattr(c, "status", None),
                    "account_id": getattr(c, "account_id", None),
                    "amount": str(getattr(c, "amount", "")),
                    "created_at": getattr(c, "created_at", None).isoformat()
                    if getattr(c, "created_at", None)
                    else None,
                }
            )
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
