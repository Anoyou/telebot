"""自定义指令只读工具。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.command import AccountCommandLink, CommandTemplate
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import clamp_limit


def _template_view(row: CommandTemplate, account_ids: list[int] | None = None) -> dict[str, Any]:
    cfg = row.config if isinstance(row.config, dict) else {}
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "description": row.description,
        "aliases": list(row.aliases or []),
        "provider_id": cfg.get("provider_id"),
        "config_keys": sorted(cfg.keys())[:30],
        "enabled_account_ids": account_ids or [],
    }


async def list_commands(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    q = select(CommandTemplate).order_by(CommandTemplate.id.asc()).limit(limit)
    if args.get("id") not in (None, ""):
        try:
            q = q.where(CommandTemplate.id == int(args["id"]))
        except (TypeError, ValueError):
            pass
    name = str(args.get("name") or "").strip()
    if name and hasattr(CommandTemplate, "name"):
        q = q.where(CommandTemplate.name.ilike(f"%{name}%"))
    type_filter = str(args.get("type") or "").strip()
    if type_filter and hasattr(CommandTemplate, "type"):
        q = q.where(CommandTemplate.type == type_filter)

    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    link_map: dict[int, list[int]] = {}
    if rows:
        ids = [r.id for r in rows]
        lr = await ctx.db.execute(
            select(AccountCommandLink).where(AccountCommandLink.template_id.in_(ids))
        )
        for link in lr.scalars().all():
            if getattr(link, "enabled", True):
                link_map.setdefault(int(link.template_id), []).append(int(link.account_id))
    return {
        "count": len(rows),
        "commands": [_template_view(r, link_map.get(r.id, [])) for r in rows],
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="commands.list",
            description="列出自定义指令模板，可按 ID/名称/类型筛选，并返回已启用账号。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_commands,
        )
    )
