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
