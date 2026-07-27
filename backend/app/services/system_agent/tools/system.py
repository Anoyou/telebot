"""系统级只读工具。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text

from .... import __version__
from ....db.models.account import Account
from ....db.models.command import LLMProvider
from ....redis_client import get_redis
from ..config import load_config, load_system_context_flags
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import get_timezone_name


async def get_context(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    flags = await load_system_context_flags(ctx.db)
    cfg = flags["agent_config"]
    return {
        "version": __version__,
        "timezone": flags["timezone"],
        "command_prefix": flags["command_prefix"],
        "ai_enabled": flags["ai_enabled"],
        "system_agent": {
            "enabled": cfg.get("enabled"),
            "provider_id": cfg.get("provider_id"),
            "model": cfg.get("model"),
            "max_steps": cfg.get("max_steps"),
            "max_tool_calls": cfg.get("max_tool_calls"),
            "session_token_limit": cfg.get("session_token_limit"),
        },
        "channel": ctx.channel,
        "role": ctx.role,
        "account_id": ctx.account_id,
    }


async def get_health(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    # DB
    try:
        await ctx.db.execute(text("SELECT 1"))
        db_check: dict[str, Any] = {"ok": True, "migration_revision": None}
        try:
            # SAVEPOINT 隔离缺表等错误，避免诊断查询把当前只读事务整体置为 failed。
            async with ctx.db.begin_nested():
                result = await ctx.db.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
                db_check["migration_revision"] = result.scalar_one_or_none()
            if db_check["migration_revision"] is None:
                db_check["migration_warning"] = "未找到数据库迁移版本记录"
        except Exception as exc:  # noqa: BLE001
            db_check["migration_warning"] = f"无法读取数据库迁移版本：{str(exc)[:160]}"
        checks["db"] = db_check
    except Exception as exc:  # noqa: BLE001
        checks["db"] = {"ok": False, "migration_revision": None, "error": str(exc)[:200]}
    # Redis
    try:
        r = get_redis()
        pong = await r.ping()
        checks["redis"] = {"ok": bool(pong)}
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = {"ok": False, "error": str(exc)[:200]}
    # 账号统计
    try:
        result = await ctx.db.execute(select(Account.id, Account.status))
        rows = result.all()
        by_status: dict[str, int] = {}
        for _id, status in rows:
            key = str(status or "unknown")
            by_status[key] = by_status.get(key, 0) + 1
        checks["accounts"] = {"ok": True, "total": len(rows), "by_status": by_status}
    except Exception as exc:  # noqa: BLE001
        checks["accounts"] = {"ok": False, "error": str(exc)[:200]}
    # Provider 数量
    try:
        result = await ctx.db.execute(select(LLMProvider.id))
        checks["llm_providers"] = {"ok": True, "count": len(result.scalars().all())}
    except Exception as exc:  # noqa: BLE001
        checks["llm_providers"] = {"ok": False, "error": str(exc)[:200]}

    overall = all(bool(v.get("ok")) for v in checks.values() if isinstance(v, dict))
    tz = await get_timezone_name(ctx.db)
    agent_cfg = await load_config(ctx.db)
    return {
        "ok": overall,
        "timezone": tz,
        "system_agent_enabled": bool(agent_cfg.get("enabled")),
        "checks": checks,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="system.get_context",
            description="获取系统上下文：时区、指令前缀、AI/Agent 开关、版本与当前会话上下文。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            min_role="viewer",
            read_handler=get_context,
        )
    )
    registry.register(
        ToolSpec(
            name="system.get_health",
            description="获取系统健康状态：数据库及迁移版本、Redis、账号分布、Provider 数量等组件就绪信息。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            min_role="viewer",
            read_handler=get_health,
        )
    )
