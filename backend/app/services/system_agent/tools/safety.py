"""账号风控、拟人化和系统总闸工作流。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from ....db.models.account import Account, HumanizeConfig
from ....db.models.rate_limit import (
    ACTION_KEYS,
    POLICY_BACKOFF,
    POLICY_DROP,
    POLICY_NOTIFY,
    POLICY_PAUSE,
    POLICY_QUEUE,
    SCOPE_ACCOUNT,
    SCOPE_TEMPLATE,
    RateLimitEvent,
    RateLimitOverride,
    RateLimitRule,
    RateLimitTemplate,
)
from ....db.models.system import SystemSetting
from ....services import rate_limit_service
from ....worker.ratelimit.buckets import TokenBuckets
from ....worker.ratelimit.overrides import list_active
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit, mark_external_fields

_POLICIES = {POLICY_DROP, POLICY_QUEUE, POLICY_BACKOFF, POLICY_PAUSE, POLICY_NOTIFY}


def _account_id(ctx: ToolContext, args: dict[str, Any]) -> int:
    raw = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if raw is None:
        raise ValueError("需要 account_id")
    return int(raw)


def _effective_view(value: Any) -> dict[str, Any]:
    return asdict(value)


def _rule_view(row: RateLimitRule) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope": row.scope,
        "scope_id": row.scope_id,
        "action": row.action,
        "per_second": row.per_second,
        "per_minute": row.per_minute,
        "per_hour": row.per_hour,
        "per_day": row.per_day,
        "same_peer_per_minute": row.same_peer_per_minute,
        "policy": row.policy,
        "backoff_base_seconds": row.backoff_base_seconds,
        "backoff_max_seconds": row.backoff_max_seconds,
        "enabled": row.enabled,
    }


async def get_limits(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    action = str(args.get("action") or "").strip()
    actions = [action] if action else list(ACTION_KEYS)
    if any(item not in ACTION_KEYS for item in actions):
        raise ValueError(f"未知风控动作：{action}")
    effective = {
        item: _effective_view(
            await rate_limit_service.get_effective(ctx.db, account_id, item)
        )
        for item in actions
    }
    explicit = await rate_limit_service.list_rules(
        ctx.db, SCOPE_ACCOUNT, account_id
    )
    return {
        "account_id": account_id,
        "effective": effective,
        "account_overrides": [_rule_view(row) for row in explicit],
    }


def _validate_rule_args(args: dict[str, Any]) -> None:
    action = str(args.get("action") or "").strip()
    if action not in ACTION_KEYS:
        raise ValueError(f"未知风控动作：{action}")
    policy = args.get("policy")
    if policy is not None and policy not in _POLICIES:
        raise ValueError(f"未知风控策略：{policy}")
    for key in (
        "per_second",
        "per_minute",
        "per_hour",
        "per_day",
        "same_peer_per_minute",
        "backoff_base_seconds",
        "backoff_max_seconds",
    ):
        if args.get(key) is not None and int(args[key]) < 0:
            raise ValueError(f"{key} 不能为负数")


async def save_limit_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _validate_rule_args(args)
    account_id = _account_id(ctx, args)
    current = await get_limits(
        ctx, {"account_id": account_id, "action": args["action"]}
    )
    return {
        "summary": f"更新账号 #{account_id} 的 {args['action']} 风控覆盖",
        "current": current,
        "target_fields": {
            key: value
            for key, value in args.items()
            if key not in {"account_id"}
        },
        "warning": "过严限制可能导致消息排队、丢弃、暂停账号或触发告警。",
    }


async def save_limit_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _validate_rule_args(args)
    account_id = _account_id(ctx, args)
    action = str(args["action"])
    row = (
        await ctx.db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_ACCOUNT,
                RateLimitRule.scope_id == account_id,
                RateLimitRule.action == action,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = RateLimitRule(
            scope=SCOPE_ACCOUNT,
            scope_id=account_id,
            action=action,
            policy=str(args.get("policy") or POLICY_QUEUE),
        )
        ctx.db.add(row)
    for key in (
        "per_second",
        "per_minute",
        "per_hour",
        "per_day",
        "same_peer_per_minute",
        "policy",
        "backoff_base_seconds",
        "backoff_max_seconds",
        "enabled",
    ):
        if key in args and args[key] is not None:
            setattr(row, key, args[key])
    await ctx.db.flush()
    return {"rule": _rule_view(row), "business_changed": True}


async def delete_limit_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    action = str(args.get("action") or "").strip()
    row = (
        await ctx.db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_ACCOUNT,
                RateLimitRule.scope_id == account_id,
                RateLimitRule.action == action,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"账号 #{account_id} 没有 {action} 风控覆盖")
    return {
        "summary": f"删除账号 #{account_id} 的 {action} 风控覆盖",
        "current": _rule_view(row),
        "note": "删除后恢复继承模板或系统默认值。",
    }


async def delete_limit_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    action = str(args.get("action") or "").strip()
    row = (
        await ctx.db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_ACCOUNT,
                RateLimitRule.scope_id == account_id,
                RateLimitRule.action == action,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"账号 #{account_id} 没有 {action} 风控覆盖")
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "action": action, "business_changed": True}


async def get_humanize(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    opts = await rate_limit_service.get_humanize_opts(ctx.db, account_id)
    value = asdict(opts)
    for key in ("active_window_start", "active_window_end"):
        if value.get(key) is not None:
            value[key] = value[key].isoformat()
    if value.get("cold_start_until") is not None:
        value["cold_start_until"] = value["cold_start_until"].isoformat()
    return {"account_id": account_id, "humanize": value}


def _parse_time(value: Any) -> time | None:
    if value in (None, ""):
        return None
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"无效时间：{value}") from exc


async def save_humanize_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    current = await get_humanize(ctx, {"account_id": account_id})
    return {
        "summary": f"更新账号 #{account_id} 的拟人化参数",
        "current": current,
        "target_fields": {
            key: value for key, value in args.items() if key != "account_id"
        },
    }


async def save_humanize_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    row = await ctx.db.get(HumanizeConfig, account_id)
    if row is None:
        row = HumanizeConfig(account_id=account_id)
        ctx.db.add(row)
    for key in (
        "jitter_pct",
        "typing_simulate",
        "typing_min_ms",
        "typing_max_ms",
        "typing_probability",
        "read_before_reply",
        "cold_start_days",
    ):
        if key in args and args[key] is not None:
            setattr(row, key, args[key])
    for key in ("active_window_start", "active_window_end"):
        if key in args:
            setattr(row, key, _parse_time(args[key]))
    await ctx.db.flush()
    return {
        "account_id": account_id,
        "humanize": (await get_humanize(ctx, {"account_id": account_id}))["humanize"],
        "business_changed": True,
    }


def _template_view(row: RateLimitTemplate) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "is_default": bool(row.is_default),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_templates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = (
        await ctx.db.execute(select(RateLimitTemplate).order_by(RateLimitTemplate.id.asc()))
    ).scalars().all()
    items: list[dict[str, Any]] = []
    for row in rows:
        rules = await rate_limit_service.list_rules(ctx.db, SCOPE_TEMPLATE, row.id)
        account_count = int(
            await ctx.db.scalar(
                select(func.count(Account.id)).where(Account.template_id == row.id)
            )
            or 0
        )
        items.append(
            {
                **_template_view(row),
                "account_count": account_count,
                "rules": [_rule_view(rule) for rule in rules],
            }
        )
    return {"count": len(items), "templates": items}


async def save_template_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    template_id = args.get("template_id") or args.get("id")
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    if len(name) > 128:
        raise ValueError("模板名称不能超过 128 个字符")
    current = None
    if template_id not in (None, ""):
        row = await ctx.db.get(RateLimitTemplate, int(template_id))
        if row is None:
            raise ValueError(f"风控模板 #{template_id} 不存在")
        current = _template_view(row)
    return {
        "summary": (
            f"更新风控模板 #{template_id}" if current else f"创建风控模板 {name}"
        ),
        "mode": "update" if current else "create",
        "current": current,
        "target": {"name": name, "is_default": bool(args.get("is_default", False))},
        "warning": "设为默认会取消其它模板的默认状态。",
    }


async def save_template_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await save_template_preview(ctx, args)
    template_id = args.get("template_id") or args.get("id")
    name = str(args["name"]).strip()
    is_default = bool(args.get("is_default", False))
    if is_default:
        existing_defaults = (
            await ctx.db.execute(
                select(RateLimitTemplate).where(RateLimitTemplate.is_default.is_(True))
            )
        ).scalars().all()
        for item in existing_defaults:
            if template_id in (None, "") or item.id != int(template_id):
                item.is_default = False
    if template_id in (None, ""):
        row = RateLimitTemplate(name=name, is_default=is_default)
        ctx.db.add(row)
        mode = "create"
    else:
        row = await ctx.db.get(RateLimitTemplate, int(template_id))
        if row is None:
            raise ValueError(f"风控模板 #{template_id} 不存在")
        row.name = name
        row.is_default = is_default
        mode = "update"
    await ctx.db.flush()
    return {"mode": mode, "template": _template_view(row), "business_changed": True}


async def delete_template_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    template_id = int(args.get("template_id") or args.get("id") or 0)
    row = await ctx.db.get(RateLimitTemplate, template_id)
    if row is None:
        raise ValueError(f"风控模板 #{template_id} 不存在")
    account_count = int(
        await ctx.db.scalar(
            select(func.count(Account.id)).where(Account.template_id == template_id)
        )
        or 0
    )
    if account_count:
        raise ValueError(f"仍有 {account_count} 个账号引用该模板，请先调整账号模板")
    rules = await rate_limit_service.list_rules(ctx.db, SCOPE_TEMPLATE, template_id)
    return {
        "summary": f"删除风控模板 #{template_id}",
        "template": _template_view(row),
        "rule_count": len(rules),
        "warning": "模板及其全部规则会永久删除。",
    }


async def delete_template_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    preview = await delete_template_preview(ctx, args)
    template_id = int(args.get("template_id") or args.get("id"))
    await ctx.db.execute(
        delete(RateLimitRule).where(
            RateLimitRule.scope == SCOPE_TEMPLATE,
            RateLimitRule.scope_id == template_id,
        )
    )
    row = await ctx.db.get(RateLimitTemplate, template_id)
    if row is not None:
        await ctx.db.delete(row)
    await ctx.db.flush()
    return {
        "deleted": True,
        "template_id": template_id,
        "rule_count": preview["rule_count"],
        "business_changed": True,
    }


async def save_template_rule_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _validate_rule_args(args)
    template_id = int(args.get("template_id") or args.get("id") or 0)
    template = await ctx.db.get(RateLimitTemplate, template_id)
    if template is None:
        raise ValueError(f"风控模板 #{template_id} 不存在")
    current = (
        await ctx.db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_TEMPLATE,
                RateLimitRule.scope_id == template_id,
                RateLimitRule.action == str(args["action"]),
            )
        )
    ).scalar_one_or_none()
    return {
        "summary": f"更新风控模板 #{template_id} 的 {args['action']} 规则",
        "template": _template_view(template),
        "current": _rule_view(current) if current is not None else None,
        "target_fields": {
            key: value
            for key, value in args.items()
            if key not in {"template_id", "id"}
        },
    }


async def save_template_rule_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await save_template_rule_preview(ctx, args)
    template_id = int(args.get("template_id") or args.get("id"))
    action = str(args["action"])
    row = (
        await ctx.db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_TEMPLATE,
                RateLimitRule.scope_id == template_id,
                RateLimitRule.action == action,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = RateLimitRule(
            scope=SCOPE_TEMPLATE,
            scope_id=template_id,
            action=action,
            policy=str(args.get("policy") or POLICY_QUEUE),
        )
        ctx.db.add(row)
    for key in (
        "per_second",
        "per_minute",
        "per_hour",
        "per_day",
        "same_peer_per_minute",
        "policy",
        "backoff_base_seconds",
        "backoff_max_seconds",
        "enabled",
    ):
        if key in args and args[key] is not None:
            setattr(row, key, args[key])
    await ctx.db.flush()
    return {"template_id": template_id, "rule": _rule_view(row), "business_changed": True}


async def get_usage(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    window = str(args.get("window") or "1m")
    window_map = {"1s": ("second", "per_second"), "1m": ("minute", "per_minute"), "1h": ("hour", "per_hour"), "24h": ("day", "per_day")}
    if window not in window_map:
        raise ValueError("window 仅支持 1s、1m、1h、24h")
    from ....redis_client import get_redis

    buckets = TokenBuckets(get_redis())
    win_key, limit_field = window_map[window]
    values: list[dict[str, Any]] = []
    for action in ACTION_KEYS:
        effective = await rate_limit_service.get_effective(ctx.db, account_id, action)
        limit = getattr(effective, limit_field, None)
        used = float(await buckets.usage(account_id, action, win_key))
        pct = used / limit * 100 if limit else 0.0
        values.append(
            {
                "action": action,
                "used": used,
                "limit": limit,
                "pct": round(pct, 2),
                "warn": pct >= 80,
            }
        )
    overrides = await list_active(ctx.db, account_id)
    return {
        "account_id": account_id,
        "window": window,
        "buckets": values,
        "active_overrides": [
            {
                "action": row.action,
                "multiplier": float(row.multiplier),
                "reason": row.reason,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }
            for row in overrides
        ],
    }


async def get_events(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    q = select(RateLimitEvent).where(RateLimitEvent.account_id == account_id)
    action = str(args.get("action") or "").strip()
    outcome = str(args.get("outcome") or "").strip()
    since = args.get("since")
    if action:
        if action not in ACTION_KEYS:
            raise ValueError(f"未知风控动作：{action}")
        q = q.where(RateLimitEvent.action == action)
    if outcome:
        q = q.where(RateLimitEvent.outcome == outcome)
    if since:
        try:
            since_dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("since 必须是 ISO 8601 时间") from exc
        q = q.where(RateLimitEvent.ts >= since_dt)
    limit = clamp_limit(args.get("limit"), default=100, maximum=1000)
    rows = (
        await ctx.db.execute(q.order_by(RateLimitEvent.ts.desc()).limit(limit))
    ).scalars().all()
    events = [
        {
            "id": row.id,
            "ts": row.ts.isoformat() if row.ts else None,
            "action": row.action,
            "outcome": row.outcome,
            "detail": row.detail,
        }
        for row in rows
    ]
    return {
        "account_id": account_id,
        "count": len(events),
        "events": mark_external_fields(events, {"detail", "message", "error", "text"}),
    }


async def estimate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    action = str(args.get("action") or "")
    if action not in ACTION_KEYS:
        raise ValueError(f"未知风控动作：{action}")
    target_count = int(args.get("target_count") or 0)
    total_count = int(args.get("total_count") or 0)
    if target_count < 0 or total_count < 0:
        raise ValueError("target_count 与 total_count 不能为负数")
    effective = await rate_limit_service.get_effective(ctx.db, account_id, action)
    candidates: list[float] = []
    for value, seconds in (
        (effective.per_second, 1),
        (effective.per_minute, 60),
        (effective.per_hour, 3600),
        (effective.per_day, 86400),
    ):
        if value:
            candidates.append(total_count / float(value) * seconds)
    return {
        "account_id": account_id,
        "action": action,
        "target_count": target_count,
        "total_count": total_count,
        "eta_seconds": int(max(candidates) if candidates else 0),
        "exceeds_limit": bool(effective.per_day and total_count > effective.per_day),
    }


async def set_strict_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    multiplier = float(args.get("multiplier", 0.5))
    ttl_seconds = int(args.get("ttl_seconds", 7200))
    if not 0 <= multiplier <= 1:
        raise ValueError("multiplier 必须在 0 到 1 之间")
    if not 60 <= ttl_seconds <= 604800:
        raise ValueError("ttl_seconds 必须在 60 秒到 7 天之间")
    return {
        "summary": f"临时调严账号 #{account_id} 的全部风控动作",
        "account_id": account_id,
        "multiplier": multiplier,
        "ttl_seconds": ttl_seconds,
        "affected_actions": len(ACTION_KEYS),
        "warning": "临时阈值只会更严格；到期自动恢复。multiplier=0 等同临时禁用全部动作。",
    }


async def set_strict_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    preview = await set_strict_preview(ctx, args)
    account_id = int(preview["account_id"])
    multiplier = float(preview["multiplier"])
    expires_at = datetime.now(UTC) + timedelta(seconds=int(preview["ttl_seconds"]))
    for action in ACTION_KEYS:
        current = (
            await ctx.db.execute(
                select(RateLimitOverride).where(
                    RateLimitOverride.account_id == account_id,
                    RateLimitOverride.action == action,
                    RateLimitOverride.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one_or_none()
        if current is None:
            ctx.db.add(
                RateLimitOverride(
                    account_id=account_id,
                    action=action,
                    multiplier=multiplier,
                    expires_at=expires_at,
                    reason="system_agent_manual_strict",
                )
            )
        else:
            current.multiplier = min(float(current.multiplier), multiplier)
            current_expires = current.expires_at
            if current_expires.tzinfo is None:
                current_expires = current_expires.replace(tzinfo=UTC)
            current.expires_at = max(current_expires, expires_at)
            current.reason = "system_agent_manual_strict"
    await ctx.db.flush()
    return {
        "account_id": account_id,
        "applied": len(ACTION_KEYS),
        "expires_at": expires_at.isoformat(),
        "business_changed": True,
    }


async def drop_override_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    action = str(args.get("action") or "")
    if action not in ACTION_KEYS:
        raise ValueError(f"未知风控动作：{action}")
    rows = (
        await ctx.db.execute(
            select(RateLimitOverride).where(
                RateLimitOverride.account_id == account_id,
                RateLimitOverride.action == action,
                RateLimitOverride.expires_at > datetime.now(UTC),
            )
        )
    ).scalars().all()
    if not rows:
        raise ValueError(f"账号 #{account_id} 没有 {action} 的有效临时覆盖")
    return {
        "summary": f"撤销账号 #{account_id} 的 {action} 临时风控覆盖",
        "account_id": account_id,
        "action": action,
        "count": len(rows),
        "warning": "撤销后立即恢复模板/账号规则的常规阈值。",
    }


async def drop_override_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    preview = await drop_override_preview(ctx, args)
    result = await ctx.db.execute(
        delete(RateLimitOverride).where(
            RateLimitOverride.account_id == int(preview["account_id"]),
            RateLimitOverride.action == str(preview["action"]),
        )
    )
    await ctx.db.flush()
    return {
        "account_id": preview["account_id"],
        "action": preview["action"],
        "deleted": int(result.rowcount or 0),
        "business_changed": True,
    }


async def get_kill_switch(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    row = await ctx.db.get(SystemSetting, "kill_switch")
    value = row.value if row is not None else {"enabled": False}
    enabled = bool(value.get("enabled", False)) if isinstance(value, dict) else bool(value)
    return {"enabled": enabled}


async def set_kill_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    current = await get_kill_switch(ctx, {})
    enabled = bool(args.get("enabled"))
    return {
        "summary": f"{'开启' if enabled else '关闭'}系统全局总闸",
        "current_enabled": current["enabled"],
        "target_enabled": enabled,
        "warning": (
            "开启总闸会停止 UserBot Worker、管理 Bot 与交互 Bot；关闭时会按平台能力恢复。"
        ),
    }


async def set_kill_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import kill_switch_service

    enabled = bool(args.get("enabled"))
    await kill_switch_service.set_enabled(ctx.db, enabled)
    return {
        "target_enabled": enabled,
        "runtime_sync_required": True,
        "business_changed": True,
    }


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def register(registry: ToolRegistry) -> None:
    account_action = {
        "account_id": {"type": "integer"},
        "action": {"type": "string", "enum": list(ACTION_KEYS)},
    }
    registry.register(ToolSpec(
        name="rate_limits.get", description="读取账号所有或指定动作的最终风控阈值与显式覆盖。",
        input_schema=_obj(account_action, required=["account_id"]), read_handler=get_limits,
    ))
    registry.register(ToolSpec(
        name="rate_limits.get_usage", description="读取账号实时 Token Bucket 用量与有效临时覆盖。",
        input_schema=_obj({
            "account_id": {"type": "integer"},
            "window": {"type": "string", "enum": ["1s", "1m", "1h", "24h"]},
        }, required=["account_id"]), read_handler=get_usage,
    ))
    registry.register(ToolSpec(
        name="rate_limits.get_events", description="读取账号最近的限流、排队、FloodWait 等风控事件。",
        input_schema=_obj({
            "account_id": {"type": "integer"},
            "since": {"type": "string", "description": "ISO 8601 时间"},
            "action": {"type": "string", "enum": list(ACTION_KEYS)},
            "outcome": {"type": "string"},
            "limit": {"type": "integer"},
        }, required=["account_id"]), read_handler=get_events,
    ))
    registry.register(ToolSpec(
        name="rate_limits.estimate", description="按最终风控阈值估算一批动作所需时间及是否超过日限额。",
        input_schema=_obj({
            "account_id": {"type": "integer"},
            "action": {"type": "string", "enum": list(ACTION_KEYS)},
            "target_count": {"type": "integer"},
            "total_count": {"type": "integer"},
        }, required=["account_id", "action", "target_count", "total_count"]),
        read_handler=estimate,
    ))
    registry.register(ToolSpec(
        name="rate_limits.list_templates", description="列出风控模板、模板规则及引用账号数。",
        channels=("web",),
        input_schema=_obj({}), read_handler=list_templates,
    ))
    limit_fields = {
        **account_action,
        "per_second": {"type": "integer"}, "per_minute": {"type": "integer"},
        "per_hour": {"type": "integer"}, "per_day": {"type": "integer"},
        "same_peer_per_minute": {"type": "integer"},
        "policy": {"type": "string", "enum": sorted(_POLICIES)},
        "backoff_base_seconds": {"type": "integer"},
        "backoff_max_seconds": {"type": "integer"}, "enabled": {"type": "boolean"},
    }
    registry.register(ToolSpec(
        name="rate_limits.save_template", description="创建或更新风控模板，可设置为系统默认模板。",
        channels=("web",),
        input_schema=_obj({
            "template_id": {"type": "integer"},
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "is_default": {"type": "boolean"},
        }, required=["name"]), read_only=False, min_role="admin", risk="dangerous",
        preview_handler=save_template_preview, execute_handler=save_template_execute,
        runtime_effects=("reload_rate_limits_all",),
    ))
    registry.register(ToolSpec(
        name="rate_limits.delete_template", description="删除未被账号引用的风控模板及其规则。",
        channels=("web",),
        input_schema=_obj({
            "template_id": {"type": "integer"},
            "id": {"type": "integer"},
        }), read_only=False, min_role="admin", risk="dangerous",
        preview_handler=delete_template_preview, execute_handler=delete_template_execute,
        runtime_effects=("reload_rate_limits_all",),
    ))
    template_rule_fields = dict(limit_fields)
    template_rule_fields.pop("account_id", None)
    template_rule_fields["template_id"] = {"type": "integer"}
    template_rule_fields["id"] = {"type": "integer"}
    registry.register(ToolSpec(
        name="rate_limits.save_template_rule", description="创建或更新风控模板中的单动作规则。",
        channels=("web",),
        input_schema=_obj(template_rule_fields, required=["template_id", "action"]),
        read_only=False, min_role="admin", risk="dangerous",
        preview_handler=save_template_rule_preview, execute_handler=save_template_rule_execute,
        runtime_effects=("reload_rate_limits_all",),
    ))
    registry.register(ToolSpec(
        name="rate_limits.save", description="创建或更新账号级单动作风控覆盖。",
        input_schema=_obj(limit_fields, required=["account_id", "action"]),
        read_only=False, min_role="admin", risk="dangerous",
        preview_handler=save_limit_preview, execute_handler=save_limit_execute,
        runtime_effects=("reload_config",),
    ))
    registry.register(ToolSpec(
        name="rate_limits.delete", description="删除账号级动作覆盖并恢复继承值。",
        input_schema=_obj(account_action, required=["account_id", "action"]),
        read_only=False, min_role="admin", preview_handler=delete_limit_preview,
        execute_handler=delete_limit_execute, runtime_effects=("reload_config",),
    ))
    registry.register(ToolSpec(
        name="rate_limits.set_strict", description="在指定 TTL 内按倍率临时调严账号的全部风控动作。",
        input_schema=_obj({
            "account_id": {"type": "integer"},
            "multiplier": {"type": "number", "minimum": 0, "maximum": 1},
            "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 604800},
        }, required=["account_id"]), read_only=False, min_role="admin", risk="dangerous",
        preview_handler=set_strict_preview, execute_handler=set_strict_execute,
        runtime_effects=("sync_rate_limit_overrides",),
    ))
    registry.register(ToolSpec(
        name="rate_limits.drop_override", description="提前撤销指定动作的临时风控覆盖。",
        input_schema=_obj(account_action, required=["account_id", "action"]),
        read_only=False, min_role="admin", risk="dangerous",
        preview_handler=drop_override_preview, execute_handler=drop_override_execute,
        runtime_effects=("sync_rate_limit_overrides",),
    ))
    registry.register(ToolSpec(
        name="humanize.get", description="读取账号拟人化、活跃时段和冷启动参数。",
        input_schema=_obj({"account_id": {"type": "integer"}}, required=["account_id"]),
        read_handler=get_humanize,
    ))
    humanize_fields = {
        "account_id": {"type": "integer"}, "jitter_pct": {"type": "integer"},
        "typing_simulate": {"type": "boolean"}, "typing_min_ms": {"type": "integer"},
        "typing_max_ms": {"type": "integer"}, "typing_probability": {"type": "integer"},
        "read_before_reply": {"type": "boolean"}, "active_window_start": {"type": ["string", "null"]},
        "active_window_end": {"type": ["string", "null"]}, "cold_start_days": {"type": "integer"},
    }
    registry.register(ToolSpec(
        name="humanize.save", description="更新账号拟人化、活跃时段和冷启动参数。",
        input_schema=_obj(humanize_fields, required=["account_id"]), read_only=False,
        min_role="admin", preview_handler=save_humanize_preview,
        execute_handler=save_humanize_execute, runtime_effects=("reload_config",),
    ))
    registry.register(ToolSpec(
        name="safety.get_kill_switch", description="读取系统全局总闸状态。",
        channels=("web",),
        input_schema=_obj({}), read_handler=get_kill_switch,
    ))
    registry.register(ToolSpec(
        name="safety.set_kill_switch", description="开启或关闭系统全局总闸并收敛所有 Worker/Bot runtime。",
        channels=("web",),
        input_schema=_obj({"enabled": {"type": "boolean"}}, required=["enabled"]),
        read_only=False, min_role="admin", risk="dangerous",
        preview_handler=set_kill_preview, execute_handler=set_kill_execute,
        runtime_effects=("kill_switch",),
    ))


__all__ = ["register"]
