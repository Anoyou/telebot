"""长期记忆工具（写操作走 Action 确认）。"""

from __future__ import annotations

from typing import Any

from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ..user_memory import (
    create_memory,
    delete_memory,
    list_memories,
    memory_to_dict,
)


def _scope(ctx: ToolContext) -> tuple[str, int]:
    if ctx.channel == "bot":
        if ctx.bot_tg_user_id is None:
            raise ValueError("Bot 渠道缺少触发者 ID，无法操作长期记忆")
        return "bot_user", int(ctx.bot_tg_user_id)
    if ctx.web_user_id is None:
        raise ValueError("Web 渠道缺少用户 ID，无法操作长期记忆")
    return "web_user", int(ctx.web_user_id)


async def list_items(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    scope_type, scope_id = _scope(ctx)
    rows = await list_memories(ctx.db, scope_type=scope_type, scope_id=scope_id)
    return {
        "count": len(rows),
        "items": [memory_to_dict(r) for r in rows],
        "note": "长期偏好跨会话保留；可在 Web 助手配置中管理。",
    }


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content") or "").strip()
    return {
        "summary": f"将保存偏好：{content[:80]}",
        "content": content,
        "source": str(args.get("source") or "agent_learned"),
    }


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    scope_type, scope_id = _scope(ctx)
    row = await create_memory(
        ctx.db,
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_id=scope_id,
        content=str(args.get("content") or ""),
        source=str(args.get("source") or "agent_learned"),
        enabled=True,
    )
    return {"item": memory_to_dict(row), "business_changed": True}


async def delete_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    memory_id = int(args.get("memory_id") or args.get("id") or 0)
    scope_type, scope_id = _scope(ctx)
    rows = await list_memories(ctx.db, scope_type=scope_type, scope_id=scope_id)
    target = next((r for r in rows if int(r.id) == memory_id), None)
    content = target.content if target else f"#{memory_id}"
    return {"summary": f"将删除偏好：{content[:80]}", "memory_id": memory_id}


async def delete_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    scope_type, scope_id = _scope(ctx)
    memory_id = int(args.get("memory_id") or args.get("id") or 0)
    await delete_memory(
        ctx.db,
        memory_id=memory_id,
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_id=scope_id,
    )
    return {"memory_id": memory_id, "deleted": True, "business_changed": True}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="memory.list",
            description="列出当前用户的长期偏好记忆。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            min_role="viewer",
            read_handler=list_items,
        )
    )
    registry.register(
        ToolSpec(
            name="memory.save",
            description="保存一条长期偏好（需用户确认）。内容不得包含密钥。",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=save_preview,
            execute_handler=save_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="memory.delete",
            description="删除一条长期偏好（需用户确认）。",
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer"},
                    "id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=delete_preview,
            execute_handler=delete_execute,
        )
    )
