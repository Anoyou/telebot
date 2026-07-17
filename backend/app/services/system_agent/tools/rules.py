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


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = args.get("id") or args.get("rule_id")
    feature_key = str(args.get("feature_key") or "").strip()
    if rule_id not in (None, ""):
        row = await ctx.db.get(Rule, int(rule_id))
        if row is None:
            raise ValueError(f"规则 {rule_id} 不存在")
        if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
            raise PermissionError("无权修改其他账号规则")
        fields = {k: args[k] for k in ("name", "enabled", "priority", "config") if k in args}
        return {
            "summary": f"更新规则 #{row.id} {row.name}",
            "mode": "update",
            "current": _rule_view(row),
            "target_fields": fields,
            "account_id": row.account_id,
        }
    if account_id is None:
        raise ValueError("创建规则需要 account_id")
    if not feature_key or feature_key == "interaction":
        raise ValueError("创建通用规则需要 feature_key，且不能为 interaction")
    await rule_service.ensure_account(ctx.db, account_id)
    await rule_service.ensure_feature(ctx.db, feature_key)
    return {
        "summary": f"创建 {feature_key} 规则到账号 #{account_id}",
        "mode": "create",
        "account_id": account_id,
        "feature_key": feature_key,
        "name": args.get("name") or "未命名规则",
        "enabled": bool(args.get("enabled", True)),
        "priority": int(args.get("priority") or 100),
        "config": args.get("config") or {},
    }


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    rule_id = args.get("id") or args.get("rule_id")
    if rule_id not in (None, ""):
        fields = {k: args[k] for k in ("name", "enabled", "priority", "config") if k in args}
        row = await rule_service.update_rule(ctx.db, int(rule_id), fields=fields)
        return {"mode": "update", "rule": _rule_view(row), "business_changed": True}

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("创建规则需要 account_id")
    row = await rule_service.create_rule(
        ctx.db,
        account_id=account_id,
        feature_key=str(args.get("feature_key") or ""),
        name=str(args.get("name") or "未命名规则"),
        enabled=bool(args.get("enabled", True)),
        priority=int(args.get("priority") or 100),
        config=args.get("config") if isinstance(args.get("config"), dict) else {},
    )
    return {"mode": "create", "rule": _rule_view(row), "business_changed": True}


async def set_enabled_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rule_id = int(args.get("rule_id") or args.get("id"))
    enabled = bool(args.get("enabled"))
    row = await ctx.db.get(Rule, rule_id)
    if row is None:
        raise ValueError(f"规则 {rule_id} 不存在")
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        raise PermissionError("无权修改其他账号规则")
    return {
        "summary": f"{'启用' if enabled else '禁用'}规则 #{rule_id} {row.name}",
        "rule_id": rule_id,
        "account_id": row.account_id,
        "current_enabled": bool(row.enabled),
        "target_enabled": enabled,
        "note": "暂时禁用不会自动恢复。",
    }


async def set_enabled_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    rule_id = int(args.get("rule_id") or args.get("id"))
    enabled = bool(args.get("enabled"))
    row = await rule_service.set_enabled(ctx.db, rule_id, enabled)
    return {"rule": _rule_view(row), "business_changed": True}


async def delete_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rule_id = int(args.get("rule_id") or args.get("id"))
    row = await ctx.db.get(Rule, rule_id)
    if row is None:
        raise ValueError(f"规则 {rule_id} 不存在")
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        raise PermissionError("无权删除其他账号规则")
    return {
        "summary": f"删除规则 #{rule_id} {row.name}",
        "rule": _rule_view(row),
        "warning": "危险操作：删除后不可恢复。",
    }


async def delete_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    rule_id = int(args.get("rule_id") or args.get("id"))
    info = await rule_service.delete_rule(ctx.db, rule_id)
    return {**info, "business_changed": True}


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
    registry.register(
        ToolSpec(
            name="rules.save",
            description="创建或更新通用 Rule。有 id/rule_id 时更新明确字段；否则创建（需 account_id + feature_key）。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rule_id": {"type": "integer"},
                    "account_id": {"type": "integer"},
                    "feature_key": {"type": "string"},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "priority": {"type": "integer"},
                    "config": {"type": "object"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=save_preview,
            execute_handler=save_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="rules.set_enabled",
            description="启用或禁用通用 Rule。禁用不会自动恢复。",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["enabled"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=set_enabled_preview,
            execute_handler=set_enabled_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="rules.delete",
            description="删除通用 Rule（危险）。",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_preview,
            execute_handler=delete_execute,
            runtime_effects=("reload_config",),
        )
    )
