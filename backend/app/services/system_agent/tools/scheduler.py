"""Scheduler 只读工具（Rule feature_key=scheduler）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ....db.models.rule import Rule
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit, get_timezone_name


def _compute_next_run_at(cfg: dict[str, Any], timezone_name: str) -> str | None:
    """尽力计算 next_run_at（ISO UTC）。失败返回 None。"""

    kind = str(cfg.get("kind") or "cron").lower()
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
        timezone_name = "UTC"
    now = datetime.now(UTC)

    if kind == "once":
        raw = cfg.get("run_at") or cfg.get("at") or cfg.get("once_at")
        if not raw:
            return None
        try:
            if isinstance(raw, (int, float)):
                dt = datetime.fromtimestamp(float(raw), tz=UTC)
            else:
                text = str(raw).strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                dt = datetime.fromisoformat(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz).astimezone(UTC)
                else:
                    dt = dt.astimezone(UTC)
            return dt.isoformat() if dt > now else None
        except Exception:  # noqa: BLE001
            return None

    if kind == "interval":
        try:
            seconds = int(cfg.get("interval_seconds") or cfg.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            return None
        last = cfg.get("last_run_at") or cfg.get("_last_run_at")
        try:
            if last:
                base = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if base.tzinfo is None:
                    base = base.replace(tzinfo=UTC)
            else:
                base = now
            nxt = base.astimezone(UTC) + __import__("datetime").timedelta(seconds=seconds)
            if nxt <= now:
                nxt = now + __import__("datetime").timedelta(seconds=seconds)
            return nxt.isoformat()
        except Exception:  # noqa: BLE001
            return (now + __import__("datetime").timedelta(seconds=seconds)).isoformat()

    # cron
    expr = str(cfg.get("cron") or "").strip()
    if not expr:
        return None
    try:
        from croniter import croniter

        local_now = now.astimezone(tz)
        # 支持 5 字段；6 字段秒级留给 croniter
        itr = croniter(expr, local_now)
        nxt_local = itr.get_next(datetime)
        if nxt_local.tzinfo is None:
            nxt_local = nxt_local.replace(tzinfo=tz)
        return nxt_local.astimezone(UTC).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _scheduler_view(row: Rule, timezone_name: str) -> dict[str, Any]:
    cfg = row.config if isinstance(row.config, dict) else {}
    kind = str(cfg.get("kind") or "cron").lower()
    return {
        "id": row.id,
        "account_id": row.account_id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "feature_key": "scheduler",
        "kind": kind,
        "cron": cfg.get("cron"),
        "interval_seconds": cfg.get("interval_seconds") or cfg.get("seconds"),
        "run_at": cfg.get("run_at") or cfg.get("at") or cfg.get("once_at"),
        "timezone": timezone_name,
        "next_run_at": _compute_next_run_at(cfg, timezone_name),
        "action_type": cfg.get("action") or cfg.get("action_type") or cfg.get("type"),
        "config_summary": {
            k: cfg.get(k)
            for k in (
                "kind",
                "cron",
                "interval_seconds",
                "run_at",
                "message",
                "text",
                "run_command",
                "call_llm",
                "provider_id",
                "chat_id",
                "peer",
            )
            if k in cfg
        },
    }


async def list_scheduler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    tz = await get_timezone_name(ctx.db)
    q = (
        select(Rule)
        .where(Rule.feature_key == "scheduler")
        .order_by(Rule.account_id.asc(), Rule.id.asc())
        .limit(limit)
    )
    if account_id is not None:
        q = q.where(Rule.account_id == account_id)
    elif ctx.channel == "bot":
        return {"error": "account_id_required", "message": "Bot 渠道必须有账号上下文"}
    if args.get("enabled_only"):
        q = q.where(Rule.enabled.is_(True))
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    return {
        "timezone": tz,
        "count": len(rows),
        "items": [_scheduler_view(r, tz) for r in rows],
    }


async def get_scheduler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        rule_id = int(args.get("rule_id") or args.get("id"))
    except (TypeError, ValueError):
        return {"error": "invalid_id", "message": "需要整数 rule_id"}
    row = await ctx.db.get(Rule, rule_id)
    if row is None or row.feature_key != "scheduler":
        return {"error": "not_found", "message": f"Scheduler 规则 {rule_id} 不存在"}
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        return {"error": "forbidden", "message": "无权查看其他账号的定时任务"}
    tz = await get_timezone_name(ctx.db)
    return {"item": _scheduler_view(row, tz), "timezone": tz}


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = args.get("id") or args.get("rule_id")
    tz = await get_timezone_name(ctx.db)
    if rule_id not in (None, ""):
        row = await ctx.db.get(Rule, int(rule_id))
        if row is None or row.feature_key != "scheduler":
            raise ValueError(f"Scheduler 规则 {rule_id} 不存在")
        if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
            raise PermissionError("无权修改其他账号定时任务")
        fields = {k: args[k] for k in ("name", "enabled", "priority", "config") if k in args}
        return {
            "summary": f"更新定时任务 #{row.id} {row.name}",
            "mode": "update",
            "current": _scheduler_view(row, tz),
            "target_fields": fields,
            "account_id": row.account_id,
        }
    if account_id is None:
        raise ValueError("创建定时任务需要 account_id")
    return {
        "summary": f"创建定时任务到账号 #{account_id}",
        "mode": "create",
        "account_id": account_id,
        "name": args.get("name") or "定时任务",
        "enabled": bool(args.get("enabled", True)),
        "config": args.get("config") or {},
        "timezone": tz,
    }


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    tz = await get_timezone_name(ctx.db)
    rule_id = args.get("id") or args.get("rule_id")
    if rule_id not in (None, ""):
        fields = {k: args[k] for k in ("name", "enabled", "priority", "config") if k in args}
        row = await rule_service.update_rule(ctx.db, int(rule_id), fields=fields)
        return {"mode": "update", "item": _scheduler_view(row, tz), "business_changed": True}

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("创建定时任务需要 account_id")
    row = await rule_service.create_rule(
        ctx.db,
        account_id=account_id,
        feature_key="scheduler",
        name=str(args.get("name") or "定时任务"),
        enabled=bool(args.get("enabled", True)),
        priority=int(args.get("priority") or 100),
        config=args.get("config") if isinstance(args.get("config"), dict) else {},
    )
    return {"mode": "create", "item": _scheduler_view(row, tz), "business_changed": True}


async def set_enabled_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rule_id = int(args.get("rule_id") or args.get("id"))
    enabled = bool(args.get("enabled"))
    row = await ctx.db.get(Rule, rule_id)
    if row is None or row.feature_key != "scheduler":
        raise ValueError(f"Scheduler 规则 {rule_id} 不存在")
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        raise PermissionError("无权修改其他账号定时任务")
    return {
        "summary": f"{'启用' if enabled else '禁用'}定时任务 #{rule_id} {row.name}",
        "rule_id": rule_id,
        "account_id": row.account_id,
        "current_enabled": bool(row.enabled),
        "target_enabled": enabled,
        "note": "禁用不会自动恢复。",
    }


async def set_enabled_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    tz = await get_timezone_name(ctx.db)
    rule_id = int(args.get("rule_id") or args.get("id"))
    enabled = bool(args.get("enabled"))
    row = await rule_service.set_enabled(ctx.db, rule_id, enabled)
    return {"item": _scheduler_view(row, tz), "business_changed": True}


async def delete_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rule_id = int(args.get("rule_id") or args.get("id"))
    tz = await get_timezone_name(ctx.db)
    row = await ctx.db.get(Rule, rule_id)
    if row is None or row.feature_key != "scheduler":
        raise ValueError(f"Scheduler 规则 {rule_id} 不存在")
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        raise PermissionError("无权删除其他账号定时任务")
    return {
        "summary": f"删除定时任务 #{rule_id} {row.name}",
        "item": _scheduler_view(row, tz),
        "warning": "危险操作：删除后不可恢复。",
    }


async def delete_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    rule_id = int(args.get("rule_id") or args.get("id"))
    info = await rule_service.delete_rule(ctx.db, rule_id)
    return {**info, "business_changed": True}


async def execute_now_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rule_id = int(args.get("rule_id") or args.get("id"))
    tz = await get_timezone_name(ctx.db)
    row = await ctx.db.get(Rule, rule_id)
    if row is None or row.feature_key != "scheduler":
        raise ValueError(f"Scheduler 规则 {rule_id} 不存在")
    if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
        raise PermissionError("无权执行其他账号定时任务")
    return {
        "summary": f"立即执行定时任务 #{rule_id} {row.name}",
        "account_id": row.account_id,
        "item": _scheduler_view(row, tz),
        "warning": "危险操作：将立即触发一次调度动作。",
    }


async def execute_now_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """确认规则仍存在；真实执行在 Action 提交后的 runtime effect 完成。"""

    rule_id = int(args.get("rule_id") or args.get("id"))
    row = await ctx.db.get(Rule, rule_id)
    if row is None or row.feature_key != "scheduler":
        raise ValueError(f"Scheduler 规则 {rule_id} 不存在")
    return {
        "rule_id": rule_id,
        "account_id": row.account_id,
        "requested": True,
        "business_changed": False,
        "note": "Action 提交后将通过账号 Worker 立即执行",
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="scheduler.list",
            description="列出 Scheduler 定时任务（Rule feature_key=scheduler），含 next_run_at 与 cron/once/interval 字段。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "enabled_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_scheduler,
        )
    )
    registry.register(
        ToolSpec(
            name="scheduler.get",
            description="获取单个 Scheduler 任务详情，含确定性 next_run_at。",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_scheduler,
        )
    )
    registry.register(
        ToolSpec(
            name="scheduler.save",
            description="创建或更新 Scheduler 定时任务（标准 Rule feature_key=scheduler）。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rule_id": {"type": "integer"},
                    "account_id": {"type": "integer"},
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
            name="scheduler.set_enabled",
            description="启用或禁用定时任务。禁用不会自动恢复。",
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
            name="scheduler.delete",
            description="删除定时任务（危险）。",
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
    registry.register(
        ToolSpec(
            name="scheduler.execute_now",
            description="立即执行一条定时任务（危险）。",
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
            preview_handler=execute_now_preview,
            execute_handler=execute_now_execute,
            runtime_effects=("scheduler_execute_now",),
        )
    )
