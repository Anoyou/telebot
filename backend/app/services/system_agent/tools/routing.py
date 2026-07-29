"""Provider auto 路由：预览路由决策 + 设置指令 routing_mode。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.command import CommandTemplate
from ....schemas.command import CommandTemplateUpdate
from ....services import command_service
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec


async def preview_route(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """对一段用户消息预览 auto 路由会选哪个 Provider。"""

    from ....db.models.command import LLMProvider
    from ....services import llm_router
    from ....services.llm_dto import LLMProviderDTO

    text = str(args.get("text") or args.get("message") or "").strip()
    if not text:
        return {"error": "text_required", "message": "需要 text"}

    result = await ctx.db.execute(select(LLMProvider).order_by(LLMProvider.id.asc()))
    rows = list(result.scalars().all())
    providers: dict[int, dict[str, Any]] = {}
    for row in rows:
        dto = LLMProviderDTO.from_orm_row(row)
        providers[int(row.id)] = dto.to_dict()
    if not providers:
        return {"error": "no_providers", "message": "没有可用的 Provider"}

    try:
        payload = await llm_router.preview_route(
            text,
            str(args.get("replied_text") or "") or None,
            bool(args.get("has_replied_photo", False)),
            providers,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "message": str(exc)[:400]}
    return {"text": text, "decision": payload, "business_changed": False}


async def set_routing_mode_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    template_id = int(args.get("template_id") or args.get("id") or args.get("command_id"))
    mode = str(args.get("routing_mode") or args.get("mode") or "").strip().lower()
    if mode not in {"fixed", "auto"}:
        raise ValueError("routing_mode 只能是 fixed 或 auto")
    row = await ctx.db.get(CommandTemplate, template_id)
    if row is None:
        raise ValueError(f"指令 #{template_id} 不存在")
    if str(row.type or "") != "ai":
        raise ValueError("仅 type=ai 的指令支持 routing_mode")
    cfg = dict(row.config or {})
    return {
        "summary": f"将指令 #{template_id} {row.name} 的 routing_mode 设为 {mode}",
        "template_id": template_id,
        "name": row.name,
        "current_routing_mode": cfg.get("routing_mode", "fixed"),
        "target_routing_mode": mode,
        "provider_id": cfg.get("provider_id"),
        "note": "auto 会按消息内容自动选 Provider；fixed 使用指令绑定的 provider_id。",
    }


async def set_routing_mode_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException
    from pydantic import ValidationError

    template_id = int(args.get("template_id") or args.get("id") or args.get("command_id"))
    mode = str(args.get("routing_mode") or args.get("mode") or "").strip().lower()
    if mode not in {"fixed", "auto"}:
        raise ValueError("routing_mode 只能是 fixed 或 auto")
    row = await ctx.db.get(CommandTemplate, template_id)
    if row is None:
        raise ValueError(f"指令 #{template_id} 不存在")
    cfg = dict(row.config or {})
    cfg["routing_mode"] = mode
    # 可选 fallback
    if args.get("routing_fallback_provider_id") is not None:
        cfg["routing_fallback_provider_id"] = int(args["routing_fallback_provider_id"])
    try:
        updated = await command_service.update_template(
            ctx.db,
            template_id,
            CommandTemplateUpdate(config=cfg),
        )
    except (HTTPException, ValidationError) as exc:
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict):
            raise ValueError(str(detail.get("message") or detail.get("code") or exc)) from None
        raise ValueError(str(detail or exc)) from None
    out_cfg = dict(updated.config or {})
    if ctx.action is not None:
        stored = dict(ctx.action.arguments or {})
        stored["reload_ai_command_accounts"] = True
        ctx.action.arguments = stored
    return {
        "template_id": updated.id,
        "name": updated.name,
        "routing_mode": out_cfg.get("routing_mode"),
        "provider_id": out_cfg.get("provider_id"),
        "business_changed": True,
    }


async def list_ai_routing(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """列出 AI 指令的路由模式。"""

    q = select(CommandTemplate).where(CommandTemplate.type == "ai").order_by(CommandTemplate.id.asc())
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    items = []
    for row in rows:
        cfg = dict(row.config or {})
        items.append(
            {
                "id": row.id,
                "name": row.name,
                "routing_mode": cfg.get("routing_mode", "fixed"),
                "provider_id": cfg.get("provider_id"),
                "routing_fallback_provider_id": cfg.get("routing_fallback_provider_id"),
                "model": cfg.get("model"),
            }
        )
    return {"count": len(items), "commands": items}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="routing.list_ai_commands",
            channels=("web",),
            description="列出 AI 指令及其 routing_mode（fixed/auto）。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            min_role="viewer",
            read_handler=list_ai_routing,
        )
    )
    registry.register(
        ToolSpec(
            name="routing.preview",
            channels=("web",),
            description="预览一段文本在 auto 路由下会选哪个 Provider/模型（不改配置）。",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "message": {"type": "string"},
                    "replied_text": {"type": "string"},
                    "has_replied_photo": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=preview_route,
        )
    )
    registry.register(
        ToolSpec(
            name="routing.set_command_mode",
            channels=("web",),
            description="设置某条 AI 指令的 routing_mode 为 fixed 或 auto。",
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "command_id": {"type": "integer"},
                    "routing_mode": {"type": "string"},
                    "mode": {"type": "string"},
                    "routing_fallback_provider_id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            preview_handler=set_routing_mode_preview,
            execute_handler=set_routing_mode_execute,
            runtime_effects=("reload_commands",),
        )
    )
