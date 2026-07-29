"""消息命中模拟、路由统计与临时 Debug Trace 工作流。"""

from __future__ import annotations

from typing import Any

from ....services import (
    account_bot_runtime,
    dispatch_debug_service,
    platform_capabilities,
)
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter


def _account_id(ctx: ToolContext, args: dict[str, Any]) -> int:
    value = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if value is None:
        raise ValueError("需要 account_id")
    return value


async def _require_enabled(ctx: ToolContext) -> None:
    if not await platform_capabilities.is_module_enabled(ctx.db, "dispatch_debug"):
        raise ValueError("命中调试模块已暂停，请先启用平台能力 dispatch_debug")


async def simulate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_enabled(ctx)
    account_id = _account_id(ctx, args)
    trace = await dispatch_debug_service.simulate_dispatch(
        account_id=account_id,
        chat_type=str(args.get("chat_type") or "group"),
        chat_id=args.get("chat_id"),
        sender_id=args.get("sender_id"),
        text=str(args.get("text") or ""),
        via=str(args.get("via") or "userbot"),
    )
    if trace is None:
        return {
            "error": "worker_offline",
            "message": f"账号 #{account_id} 的 Worker 未在线或未返回命中结果",
        }
    return {"account_id": account_id, "trace": trace}


async def stats(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_enabled(ctx)
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if ctx.channel == "bot" and account_id is None:
        raise ValueError("Bot 渠道缺少绑定账号")
    return account_bot_runtime.get_router_delivery_stats_summary(
        account_id=account_id,
        channel=args.get("channel"),
        plugin_key=args.get("plugin_key"),
        chat_id=args.get("chat_id"),
        limit=max(1, min(int(args.get("limit") or 50), 200)),
    )


async def trace_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_enabled(ctx)
    account_id = _account_id(ctx, args)
    ttl = max(1, min(int(args.get("ttl_seconds") or 300), 3600))
    return {
        "summary": f"为账号 #{account_id} 开启 {ttl} 秒 Router Debug Trace",
        "account_id": account_id,
        "plugin_key": args.get("plugin_key"),
        "chat_id": args.get("chat_id"),
        "ttl_seconds": ttl,
        "note": "Trace 到期自动关闭，只影响匹配范围内的调试记录。",
    }


async def trace_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    return {
        "account_id": account_id,
        "runtime_sync_required": True,
        "business_changed": True,
    }


def register(registry: ToolRegistry) -> None:
    common = {
        "account_id": {"type": "integer"},
        "chat_id": {"type": "integer"},
        "plugin_key": {"type": "string"},
    }
    registry.register(
        ToolSpec(
            name="dispatch.simulate",
            description="向在线 Worker 发送虚拟消息，读取完整命中与路由 Trace，不真实发送消息。",
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "chat_type": {"type": "string"},
                    "sender_id": {"type": "integer"},
                    "text": {"type": "string", "maxLength": 20000},
                    "via": {"type": "string"},
                },
                "required": ["account_id"],
                "additionalProperties": False,
            },
            min_role="operator",
            diagnostic_safe=True,
            read_handler=simulate,
        )
    )
    registry.register(
        ToolSpec(
            name="dispatch.stats",
            description="读取 Router 投递统计，可按账号、通道、插件或 Chat 筛选。",
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "channel": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            diagnostic_safe=True,
            read_handler=stats,
        )
    )
    registry.register(
        ToolSpec(
            name="dispatch.enable_trace",
            description="按账号、插件或 Chat 临时开启 Router Debug Trace，最长 1 小时。",
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                },
                "required": ["account_id"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            preview_handler=trace_preview,
            execute_handler=trace_execute,
            runtime_effects=("dispatch_enable_trace",),
        )
    )


__all__ = ["register"]
