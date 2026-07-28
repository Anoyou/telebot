"""通用 Rule 只读工具（不含交互规则）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.rule import Rule
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit, mark_external_text


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


def _require_bot_rule_scope(ctx: ToolContext, row: Rule) -> None:
    if ctx.channel == "bot" and (
        ctx.account_id is None or int(row.account_id) != int(ctx.account_id)
    ):
        raise PermissionError("无权操作其他账号规则")


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


async def dry_run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services.rule_dry_run_service import dry_run_rule

    rule_id = int(args.get("rule_id") or args.get("id"))
    row = await ctx.db.get(Rule, rule_id)
    if row is None or row.feature_key == "interaction":
        return {"error": "not_found", "message": f"通用规则 {rule_id} 不存在"}
    if ctx.channel == "bot" and (
        ctx.account_id is None or int(row.account_id) != int(ctx.account_id)
    ):
        return {"error": "forbidden", "message": "无权试运行其他账号规则"}
    result = await dry_run_rule(
        ctx.db,
        row,
        sample_message=str(args.get("sample_message") or ""),
        sample_chat_type=str(args.get("sample_chat_type") or "private"),
        sample_chat_id=(
            int(args["sample_chat_id"])
            if args.get("sample_chat_id") not in (None, "")
            else None
        ),
    )
    if isinstance(result.get("output"), str):
        result["output"] = mark_external_text(result["output"])
    return result


async def copy_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    source_account_id = int(args.get("source_account_id") or args.get("account_id"))
    rule_ids = sorted({int(value) for value in (args.get("rule_ids") or [])})
    targets = sorted(
        {
            int(value)
            for value in (args.get("target_account_ids") or [])
            if int(value) != source_account_id
        }
    )
    rows = list(
        (
            await ctx.db.execute(
                select(Rule)
                .where(Rule.account_id == source_account_id, Rule.id.in_(rule_ids))
                .order_by(Rule.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != len(rule_ids):
        raise ValueError("部分源规则不存在或不属于源账号")
    if any(row.feature_key == "interaction" for row in rows):
        raise ValueError("交互规则不能通过通用 Rule 复制工具复制")
    return {
        "summary": f"从账号 #{source_account_id} 复制 {len(rows)} 条规则到 {len(targets)} 个账号",
        "source_account_id": source_account_id,
        "target_account_ids": targets,
        "rules": [_rule_view(row) for row in rows],
        "warning": "目标账号会新增独立规则；同名规则不会自动覆盖。",
    }


async def copy_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    source_account_id = int(args.get("source_account_id") or args.get("account_id"))
    result = await rule_service.copy_rules(
        ctx.db,
        source_account_id=source_account_id,
        rule_ids=[int(value) for value in (args.get("rule_ids") or [])],
        target_account_ids=[
            int(value) for value in (args.get("target_account_ids") or [])
        ],
        web_user_id=ctx.web_user_id,
    )
    if ctx.action is not None:
        stored = dict(ctx.action.arguments or {})
        stored["reload_account_ids"] = result["targets"]
        ctx.action.arguments = stored
    return {**result, "business_changed": bool(result["copied"])}


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
        if row.feature_key == "scheduler":
            raise PermissionError("修改 Scheduler 规则必须使用 scheduler 工具")
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
    if str(args.get("feature_key") or "") == "scheduler":
        raise PermissionError("创建 Scheduler 规则必须使用 scheduler 工具")
    if not feature_key or feature_key == "interaction":
        raise ValueError("创建通用规则需要 feature_key，且不能为 interaction")
    if feature_key == "scheduler":
        raise PermissionError("创建 Scheduler 规则必须使用 scheduler 工具")
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
        current = await ctx.db.get(Rule, int(rule_id))
        if current is None:
            raise ValueError(f"规则 {rule_id} 不存在")
        _require_bot_rule_scope(ctx, current)
        if current is not None and current.feature_key == "scheduler":
            raise PermissionError("修改 Scheduler 规则必须使用 scheduler 工具")
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
    if ctx.channel == "bot" and row.feature_key == "scheduler" and enabled:
        raise PermissionError("Bot 渠道启用 Scheduler 规则必须使用 scheduler 工具")
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
    current = await ctx.db.get(Rule, rule_id)
    if current is None:
        raise ValueError(f"规则 {rule_id} 不存在")
    _require_bot_rule_scope(ctx, current)
    if (
        ctx.channel == "bot"
        and enabled
        and current is not None
        and current.feature_key == "scheduler"
    ):
        raise PermissionError("Bot 渠道启用 Scheduler 规则必须使用 scheduler 工具")
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
    current = await ctx.db.get(Rule, rule_id)
    if current is None:
        raise ValueError(f"规则 {rule_id} 不存在")
    _require_bot_rule_scope(ctx, current)
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
            name="rules.dry_run",
            description="用模拟消息试运行一条通用 Rule，不发送消息、不修改业务数据。",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "sample_message": {"type": "string"},
                    "sample_chat_type": {"type": "string"},
                    "sample_chat_id": {"type": "integer"},
                },
                "required": ["sample_message"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=dry_run,
        )
    )
    registry.register(
        ToolSpec(
            name="rules.copy",
            channels=("web",),
            description="把明确的通用 Rule 复制到一个或多个目标账号。",
            input_schema={
                "type": "object",
                "properties": {
                    "source_account_id": {"type": "integer"},
                    "account_id": {"type": "integer"},
                    "rule_ids": {"type": "array", "items": {"type": "integer"}},
                    "target_account_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["rule_ids", "target_account_ids"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=copy_preview,
            execute_handler=copy_execute,
            runtime_effects=("reload_config",),
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
