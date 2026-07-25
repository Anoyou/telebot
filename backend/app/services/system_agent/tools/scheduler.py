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
        "action": (
            {
                "type": (cfg.get("action") or {}).get("type")
                if isinstance(cfg.get("action"), dict)
                else None,
                "prompt": (
                    (cfg.get("action") or {}).get("prompt")
                    if isinstance(cfg.get("action"), dict)
                    else None
                ),
                "target_chat_id": (
                    (cfg.get("action") or {}).get("target_chat_id")
                    if isinstance(cfg.get("action"), dict)
                    else None
                ),
            }
            if isinstance(cfg.get("action"), dict)
            else None
        ),
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


def _normalize_scheduler_save_args(args: dict[str, Any]) -> dict[str, Any]:
    """把 agent_prompt 便捷字段折叠进 config.action，兼容裸 config 写入。"""

    out = dict(args)
    config = dict(out.get("config") or {}) if isinstance(out.get("config"), dict) else {}
    action = dict(config.get("action") or {}) if isinstance(config.get("action"), dict) else {}

    action_type = str(
        out.get("action_type") or action.get("type") or config.get("action_type") or ""
    ).strip().lower()
    prompt = out.get("prompt")
    if prompt is None and isinstance(action.get("prompt"), str):
        prompt = action.get("prompt")
    cron = out.get("cron")
    if cron is None:
        cron = config.get("cron")
    report_channel = out.get("report_channel")
    if report_channel is None:
        report_channel = out.get("target_chat_id")
    if report_channel is None:
        report_channel = action.get("target_chat_id")

    if action_type == "agent_prompt" or prompt not in (None, ""):
        action_type = "agent_prompt"
        action["type"] = "agent_prompt"
        if prompt not in (None, ""):
            action["prompt"] = str(prompt).strip()
        if report_channel not in (None, ""):
            action["target_chat_id"] = report_channel
        config["action"] = action
        if cron not in (None, ""):
            config["cron"] = str(cron).strip()
            config.setdefault("kind", "cron")
        out["config"] = config
        out["action_type"] = "agent_prompt"
    elif action_type:
        action["type"] = action_type
        config["action"] = action
        if cron not in (None, ""):
            config["cron"] = str(cron).strip()
            config.setdefault("kind", "cron")
        out["config"] = config
        out["action_type"] = action_type
    elif config:
        out["config"] = config

    return out


def _agent_prompt_schedule_label(config: dict[str, Any]) -> str:
    kind = str(config.get("kind") or "cron").lower()
    if kind == "interval":
        seconds = config.get("interval_seconds") or config.get("seconds") or config.get("interval_sec")
        try:
            sec = int(seconds or 0)
        except (TypeError, ValueError):
            sec = 0
        if sec > 0:
            if sec % 3600 == 0:
                return f"每 {sec // 3600} 小时"
            if sec % 60 == 0:
                return f"每 {sec // 60} 分钟"
            return f"每 {sec} 秒"
        return "按间隔"
    if kind == "once":
        return "单次"
    cron = str(config.get("cron") or "").strip()
    parts = cron.split()
    # 5 字段：min hour dom mon dow；6 字段：sec min hour dom mon dow
    if len(parts) == 5:
        minute, hour, dom, mon, dow = parts
        if dom == mon == dow == "*" and hour.isdigit() and minute.isdigit():
            return f"每天 {int(hour):02d}:{int(minute):02d}"
    elif len(parts) == 6:
        _sec, minute, hour, dom, mon, dow = parts
        if dom == mon == dow == "*" and hour.isdigit() and minute.isdigit():
            return f"每天 {int(hour):02d}:{int(minute):02d}"
    if cron:
        return f"cron {cron}"
    return "按计划"


def _reject_nested_agent_prompt(ctx: ToolContext, config: dict[str, Any]) -> None:
    """防套娃：定时会话内禁止再创建 agent_prompt 定时任务。"""

    from ....db.models.system_agent import SESSION_ORIGIN_SCHEDULED

    action = config.get("action") if isinstance(config.get("action"), dict) else {}
    action_type = str(action.get("type") or "").strip().lower()
    if action_type != "agent_prompt":
        return
    origin = getattr(ctx.session, "origin", None) if ctx.session is not None else None
    if origin == SESSION_ORIGIN_SCHEDULED:
        raise ValueError(
            "定时任务会话内不能再创建 agent_prompt 定时任务（防套娃）。"
            "请在 Web 对话或 Bot 助手中创建。"
        )


def _agent_prompt_preview_summary(*, name: str, config: dict[str, Any], mode: str) -> str:
    action = config.get("action") if isinstance(config.get("action"), dict) else {}
    prompt = str(action.get("prompt") or "").strip()
    prompt_snip = prompt[:40] + ("…" if len(prompt) > 40 else "")
    schedule = _agent_prompt_schedule_label(config)
    target = action.get("target_chat_id")
    target_text = f" → 汇报会话 {target}" if target not in (None, "") else ""
    verb = "更新" if mode == "update" else "创建"
    base = f"将{verb}定时 Agent 任务：{schedule}"
    if prompt_snip:
        base += f" {prompt_snip}"
    elif name:
        base += f" {name}"
    return base + target_text


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    args = _normalize_scheduler_save_args(args)
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = args.get("id") or args.get("rule_id")
    tz = await get_timezone_name(ctx.db)
    config = args.get("config") if isinstance(args.get("config"), dict) else {}
    _reject_nested_agent_prompt(ctx, config)
    action = config.get("action") if isinstance(config.get("action"), dict) else {}
    is_agent_prompt = str(action.get("type") or "").lower() == "agent_prompt"

    if rule_id not in (None, ""):
        row = await ctx.db.get(Rule, int(rule_id))
        if row is None or row.feature_key != "scheduler":
            raise ValueError(f"Scheduler 规则 {rule_id} 不存在")
        if ctx.channel == "bot" and ctx.account_id is not None and row.account_id != ctx.account_id:
            raise PermissionError("无权修改其他账号定时任务")
        fields = {k: args[k] for k in ("name", "enabled", "priority", "config") if k in args}
        if is_agent_prompt:
            summary = _agent_prompt_preview_summary(
                name=str(args.get("name") or row.name or ""),
                config=config or (row.config if isinstance(row.config, dict) else {}),
                mode="update",
            )
        else:
            summary = f"更新定时任务 #{row.id} {row.name}"
        return {
            "summary": summary,
            "mode": "update",
            "current": _scheduler_view(row, tz),
            "target_fields": fields,
            "account_id": row.account_id,
        }
    if account_id is None:
        raise ValueError("创建定时任务需要 account_id")
    name = str(args.get("name") or ("定时 Agent 巡检" if is_agent_prompt else "定时任务"))
    if is_agent_prompt:
        if not str(action.get("prompt") or "").strip():
            raise ValueError("agent_prompt 任务需要 prompt")
        if action.get("target_chat_id") in (None, ""):
            raise ValueError("agent_prompt 任务需要 report_channel / target_chat_id（汇报会话）")
        summary = _agent_prompt_preview_summary(name=name, config=config, mode="create")
    else:
        summary = f"创建定时任务到账号 #{account_id}"
    return {
        "summary": summary,
        "mode": "create",
        "account_id": account_id,
        "name": name,
        "enabled": bool(args.get("enabled", True)),
        "config": config,
        "timezone": tz,
    }


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import rule_service

    args = _normalize_scheduler_save_args(args)
    config = args.get("config") if isinstance(args.get("config"), dict) else {}
    _reject_nested_agent_prompt(ctx, config)

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
    action = config.get("action") if isinstance(config.get("action"), dict) else {}
    is_agent_prompt = str(action.get("type") or "").lower() == "agent_prompt"
    default_name = "定时 Agent 巡检" if is_agent_prompt else "定时任务"
    row = await rule_service.create_rule(
        ctx.db,
        account_id=account_id,
        feature_key="scheduler",
        name=str(args.get("name") or default_name),
        enabled=bool(args.get("enabled", True)),
        priority=int(args.get("priority") or 100),
        config=config,
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
            description=(
                "创建或更新 Scheduler 定时任务（Rule feature_key=scheduler）。"
                "可创建 agent_prompt 类型：按 cron 无人值守跑只读系统助手并把报告推到汇报会话。"
                "便捷字段：action_type=agent_prompt、prompt、cron、report_channel。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rule_id": {"type": "integer"},
                    "account_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "priority": {"type": "integer"},
                    "config": {
                        "type": "object",
                        "description": "完整 scheduler 配置；也可只用下方便捷字段生成",
                    },
                    "action_type": {
                        "type": "string",
                        "description": "动作类型，如 agent_prompt / send_message / run_command / call_llm",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "agent_prompt 专用：定时交给系统助手的提示词",
                    },
                    "cron": {
                        "type": "string",
                        "description": "cron 表达式，如 0 9 * * *（每天 09:00）",
                    },
                    "report_channel": {
                        "description": "agent_prompt 汇报目标（chat_id 或 @username），同 target_chat_id",
                    },
                    "target_chat_id": {
                        "description": "汇报/发送目标会话，兼容 report_channel",
                    },
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
