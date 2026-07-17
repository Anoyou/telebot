"""通用 Rule 只读工具（不含交互规则）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.rule import Rule
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit


def _rule_view(row: Rule) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "feature_key": row.feature_key,
        "name": row.name,
        "enabled": bool(row.enabled),
        "priority": row.priority,
        "config": row.config if isinstance(row.config, dict) else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
    }


async def list_rules(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    feature_key = str(args.get("feature_key") or "").strip() or None
    if feature_key == "interaction":
        return {
            "error": "wrong_tool",
            "message": "交互规则请使用 interaction.list_rules，不要使用通用 rules 工具。",
        }
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    q = select(Rule).order_by(Rule.account_id.asc(), Rule.priority.asc(), Rule.id.asc()).limit(limit)
    if account_id is not None:
        q = q.where(Rule.account_id == account_id)
    elif ctx.channel == "bot":
        return {"error": "account_id_required", "message": "Bot 渠道必须有账号上下文"}
    if feature_key:
        q = q.where(Rule.feature_key == feature_key)
    if args.get("enabled_only"):
        q = q.where(Rule.enabled.is_(True))
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    return {
        "count": len(rows),
        "note": "通用 Rule 表；交互规则不在此表。",
        "rules": [_rule_view(r) for r in rows],
    }


async def get_rule(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        rule_id = int(args.get("rule_id"))
    except (TypeError, ValueError):
        return {"error": "invalid_rule_id", "message": "rule_id 必须是整数"}
    row = await ctx.db.get(Rule, rule_id)
    if row is None:
        return {"error": "not_found", "message": f"规则 {rule_id} 不存在"}
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        return {"error": "forbidden", "message": "无权查看其他账号的规则"}
    return {"rule": _rule_view(row)}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="rules.list",
            description="列出通用 Rule（可按 account_id / feature_key 过滤）。交互规则请用 interaction.*。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "feature_key": {"type": "string"},
                    "enabled_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_rules,
        )
    )
    registry.register(
        ToolSpec(
            name="rules.get",
            description="获取单条通用 Rule 详情。",
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "integer"}},
                "required": ["rule_id"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_rule,
        )
    )
