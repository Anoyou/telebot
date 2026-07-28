"""平台能力模块状态与热切换工作流。"""

from __future__ import annotations

from typing import Any

from ....services import platform_capabilities as caps
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec


async def list_capabilities(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if not caps.get_snapshot().cache_ready:
        try:
            await caps.bootstrap_from_db(ctx.db)
        except Exception:  # noqa: BLE001
            await caps.refresh_cache_from_db(ctx.db)
    return caps.build_status_payload()


async def set_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    module_key = str(args.get("module_key") or "").strip()
    if module_key not in caps.MODULE_DEFS:
        raise ValueError(f"未知平台模块：{module_key}")
    current, generation = await caps.read_module_desired(ctx.db, module_key)  # type: ignore[arg-type]
    enabled = bool(args.get("enabled"))
    meta = caps.MODULE_DEFS[module_key]  # type: ignore[index]
    return {
        "summary": f"{'启用' if enabled else '暂停'}平台模块「{meta['label']}」",
        "module_key": module_key,
        "description": meta["description"],
        "current_enabled": current,
        "target_enabled": enabled,
        "current_generation": generation,
        "warning": ("暂停只关闭入口与运行时资源，不删除配置或数据；部分离线 Worker 会在重连后收敛。"),
    }


async def set_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "module_key": str(args["module_key"]),
        "target_enabled": bool(args["enabled"]),
        "runtime_sync_required": True,
        "business_changed": True,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="capabilities.list",
            description="读取 AI、Interaction Bot、Webhook、台账和命中调试模块的目标/运行状态。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            channels=("web",),
            read_handler=list_capabilities,
        )
    )
    registry.register(
        ToolSpec(
            name="capabilities.set",
            description="启用或暂停可选平台模块；暂停不删除配置、Token、规则或资金数据。",
            input_schema={
                "type": "object",
                "properties": {
                    "module_key": {
                        "type": "string",
                        "enum": list(caps.ALL_MODULE_KEYS),
                    },
                    "enabled": {"type": "boolean"},
                },
                "required": ["module_key", "enabled"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            channels=("web",),
            risk="dangerous",
            preview_handler=set_preview,
            execute_handler=set_execute,
            runtime_effects=("platform_capability",),
        )
    )


__all__ = ["register"]
