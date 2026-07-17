"""自定义指令工具：列表 + 创建/更新/删除/账号启用。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.command import AccountCommandLink, CommandTemplate
from ....schemas.command import CommandTemplateCreate, CommandTemplateUpdate
from ....services import command_service
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


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cmd_id = args.get("id") or args.get("template_id")
    enable_accounts = args.get("enable_account_ids") or args.get("account_ids") or []
    if not isinstance(enable_accounts, list):
        enable_accounts = []
    if cmd_id not in (None, ""):
        row = await ctx.db.get(CommandTemplate, int(cmd_id))
        if row is None:
            raise ValueError(f"指令 #{cmd_id} 不存在")
        fields = {k: args[k] for k in ("name", "type", "description", "aliases", "config") if k in args}
        return {
            "summary": f"更新指令 #{row.id} {row.name}",
            "mode": "update",
            "current": _template_view(row),
            "target_fields": fields,
            "enable_account_ids": enable_accounts,
        }
    name = str(args.get("name") or "").strip()
    cmd_type = str(args.get("type") or "ai").strip()
    if not name:
        raise ValueError("创建指令需要 name")
    return {
        "summary": f"创建指令 /{name}（type={cmd_type}）",
        "mode": "create",
        "name": name,
        "type": cmd_type,
        "description": args.get("description"),
        "aliases": args.get("aliases") or [],
        "config": args.get("config") or {},
        "enable_account_ids": enable_accounts,
        "note": "可在同一次 Action 中为账号启用。",
    }


def _http_err_message(exc: Exception) -> str:
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code")
            msg = detail.get("message") or detail.get("detail")
            if code and msg:
                return f"{code}: {msg}"
            return str(msg or code or detail)
        return str(detail or exc)
    return str(exc)


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException
    from pydantic import ValidationError

    enable_accounts = args.get("enable_account_ids") or args.get("account_ids") or []
    if not isinstance(enable_accounts, list):
        enable_accounts = []
    account_ids = []
    for item in enable_accounts:
        try:
            account_ids.append(int(item))
        except (TypeError, ValueError):
            continue

    try:
        cmd_id = args.get("id") or args.get("template_id")
        if cmd_id not in (None, ""):
            data = {k: args[k] for k in ("name", "type", "description", "aliases", "config") if k in args}
            payload = CommandTemplateUpdate(**data)
            row = await command_service.update_template(ctx.db, int(cmd_id), payload)
            for aid in account_ids:
                await command_service.enable_for_account(ctx.db, aid, row.id)
            return {
                "mode": "update",
                "command": _template_view(row, account_ids),
                "enabled_account_ids": account_ids,
                "business_changed": True,
            }

        create = CommandTemplateCreate(
            name=str(args.get("name") or "").strip(),
            type=str(args.get("type") or "ai"),  # type: ignore[arg-type]
            description=args.get("description"),
            aliases=list(args.get("aliases") or []),
            config=args.get("config") if isinstance(args.get("config"), dict) else {},
        )
        row = await command_service.create_template(ctx.db, create)
        for aid in account_ids:
            await command_service.enable_for_account(ctx.db, aid, row.id)
        return {
            "mode": "create",
            "command": _template_view(row, account_ids),
            "enabled_account_ids": account_ids,
            "business_changed": True,
        }
    except (HTTPException, ValidationError) as exc:
        raise ValueError(_http_err_message(exc)) from None


async def delete_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cmd_id = int(args.get("id") or args.get("template_id"))
    row = await ctx.db.get(CommandTemplate, cmd_id)
    if row is None:
        raise ValueError(f"指令 #{cmd_id} 不存在")
    return {
        "summary": f"删除指令 #{cmd_id} {row.name}",
        "command": _template_view(row),
        "warning": "危险操作：删除后账号启用关系一并移除。",
    }


async def delete_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cmd_id = int(args.get("id") or args.get("template_id"))
    affected = await command_service.delete_template(ctx.db, cmd_id)
    return {
        "id": cmd_id,
        "deleted": True,
        "affected_account_ids": sorted(affected) if affected else [],
        "business_changed": True,
    }


async def set_enabled_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cmd_id = int(args.get("id") or args.get("template_id"))
    account_ids = args.get("account_ids") or args.get("enable_account_ids") or []
    enabled = bool(args.get("enabled", True))
    if not isinstance(account_ids, list) or not account_ids:
        raise ValueError("需要 account_ids")
    row = await ctx.db.get(CommandTemplate, cmd_id)
    if row is None:
        raise ValueError(f"指令 #{cmd_id} 不存在")
    return {
        "summary": f"{'启用' if enabled else '停用'}指令 #{cmd_id} 于账号 {account_ids}",
        "command": _template_view(row),
        "account_ids": account_ids,
        "enabled": enabled,
    }


async def set_enabled_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cmd_id = int(args.get("id") or args.get("template_id"))
    account_ids = args.get("account_ids") or args.get("enable_account_ids") or []
    enabled = bool(args.get("enabled", True))
    if not isinstance(account_ids, list) or not account_ids:
        raise ValueError("需要 account_ids")
    aids: list[int] = []
    for item in account_ids:
        try:
            aids.append(int(item))
        except (TypeError, ValueError):
            continue
    if enabled:
        for aid in aids:
            await command_service.enable_for_account(ctx.db, aid, cmd_id)
    else:
        for aid in aids:
            await command_service.disable_for_account(ctx.db, aid, cmd_id)
    return {
        "template_id": cmd_id,
        "account_ids": aids,
        "enabled": enabled,
        "business_changed": True,
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
    registry.register(
        ToolSpec(
            name="commands.save",
            description="创建或更新自定义指令；可选 enable_account_ids 同事务启用到账号。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "template_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "config": {"type": "object"},
                    "enable_account_ids": {"type": "array", "items": {"type": "integer"}},
                    "account_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            preview_handler=save_preview,
            execute_handler=save_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="commands.delete",
            description="删除自定义指令（危险）。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "template_id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_preview,
            execute_handler=delete_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="commands.set_enabled_for_accounts",
            description="为若干账号启用或停用某条自定义指令。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "template_id": {"type": "integer"},
                    "account_ids": {"type": "array", "items": {"type": "integer"}},
                    "enable_account_ids": {"type": "array", "items": {"type": "integer"}},
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
        )
    )
