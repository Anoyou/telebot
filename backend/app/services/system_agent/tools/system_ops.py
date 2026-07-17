"""系统级运维：检查更新、应用更新、重启（危险）。

直接复用 system_health 内部实现，不走 HTTP。
"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec


def _actor_user(ctx: ToolContext) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=ctx.web_user_id or 0)


async def check_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....api import system_health as sh
    from ....api.system_health import UpdateRequest

    payload = UpdateRequest(
        remote=args.get("remote"),
        branch=args.get("branch"),
        full=bool(args.get("force_full") or args.get("full") or False),
    )
    result = await sh.check_update(_user=_actor_user(ctx), payload=payload)  # type: ignore[arg-type]
    if hasattr(result, "model_dump"):
        data = result.model_dump()
    else:
        data = dict(result)  # type: ignore[arg-type]
    return {
        "update": data,
        "business_changed": False,
        "note": "仅检查，不拉取代码。应用更新请用 system.apply_update。",
    }


async def apply_update_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "应用系统更新（git pull / updater job）",
        "remote": args.get("remote"),
        "branch": args.get("branch"),
        "force_full": bool(args.get("force_full", False)),
        "warning": "危险：将更新运行中的 TelePilot，服务可能短暂中断。",
    }


async def apply_update_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "requested": True,
        "remote": args.get("remote"),
        "branch": args.get("branch"),
        "force_full": bool(args.get("force_full") or args.get("full") or False),
        "business_changed": False,
        "note": "Action 提交后才会启动更新，避免重启中断确认事务。",
    }


async def restart_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "重启 TelePilot 应用",
        "warning": "危险：将触发应用重启，Web/Worker 会短暂不可用。生产容器环境可能只返回手工命令提示。",
    }


async def restart_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "requested": True,
        "business_changed": False,
        "note": "Action 提交后才会下发重启；进程退出后结果可能保持 pending。",
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="system.check_update",
            description="检查 TelePilot 是否有代码更新（不拉取）。",
            input_schema={
                "type": "object",
                "properties": {
                    "remote": {"type": "string"},
                    "branch": {"type": "string"},
                    "full": {"type": "boolean"},
                    "force_full": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="admin",
            read_handler=check_update,
        )
    )
    registry.register(
        ToolSpec(
            name="system.apply_update",
            description="应用系统更新（危险）。生产环境走 updater job；开发环境 git pull + restart。",
            input_schema={
                "type": "object",
                "properties": {
                    "remote": {"type": "string"},
                    "branch": {"type": "string"},
                    "full": {"type": "boolean"},
                    "force_full": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=apply_update_preview,
            execute_handler=apply_update_execute,
            runtime_effects=("system_apply_update",),
        )
    )
    registry.register(
        ToolSpec(
            name="system.restart",
            description="重启 TelePilot 应用（危险）。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=restart_preview,
            execute_handler=restart_execute,
            runtime_effects=("system_restart",),
        )
    )
