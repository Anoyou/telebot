"""命令别名、Sudo 用户与忽略名单工作流。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.account import Account
from ....schemas.alias import CommandAliasCreate, CommandAliasResponse, CommandAliasUpdate
from ....schemas.ignored_peer import IgnoredPeerCreate, IgnoredPeerOut
from ....schemas.sudo import SudoUserCreate, SudoUserResponse, SudoUserUpdate
from ....services import alias_service, ignored_peer_service, sudo_service
from ..context import ToolContext
from ..registry import PreparedAction, ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, mark_external_fields


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


def _optional_account_id(ctx: ToolContext, args: dict[str, Any]) -> int | None:
    return account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )


def _require_bot_row_scope(ctx: ToolContext, row: Any) -> None:
    if ctx.channel != "bot":
        return
    if ctx.account_id is None or int(getattr(row, "account_id", 0) or 0) != int(
        ctx.account_id
    ):
        raise ValueError("Bot 渠道只能管理当前绑定账号的数据")


def _bot_canonical_action(
    ctx: ToolContext,
    args: dict[str, Any],
    preview: dict[str, Any],
    account_id: int | None,
) -> dict[str, Any] | PreparedAction:
    if ctx.channel != "bot":
        return preview
    return PreparedAction(
        arguments={**args, "account_id": account_id},
        preview=preview,
    )


async def _store_alias_reload_scope(
    ctx: ToolContext,
    *account_scopes: int | None,
) -> None:
    """固化别名变更影响的 Worker；全局别名覆盖全部账号。"""

    if ctx.action is None:
        return
    if any(value is None for value in account_scopes):
        account_ids = list(
            (await ctx.db.execute(select(Account.id).order_by(Account.id.asc())))
            .scalars()
            .all()
        )
    else:
        account_ids = [int(value) for value in account_scopes if value is not None]
    stored = dict(ctx.action.arguments or {})
    stored["reload_account_ids"] = sorted(set(account_ids))
    ctx.action.arguments = stored


async def list_aliases(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = await alias_service.get_aliases(ctx.db, _optional_account_id(ctx, args))
    items = [_dump(CommandAliasResponse.model_validate(row)) for row in rows]
    return {"count": len(items), "aliases": items}


async def save_alias_preview(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any] | PreparedAction:
    alias_id = args.get("id") or args.get("alias_id")
    if alias_id not in (None, ""):
        row = await alias_service.get_alias(ctx.db, int(alias_id))
        if row is None:
            raise ValueError(f"命令别名 #{alias_id} 不存在")
        _require_bot_row_scope(ctx, row)
        preview = {
            "summary": f"更新命令别名 #{row.id} {row.alias}",
            "mode": "update",
            "account_id": row.account_id,
            "current": _dump(CommandAliasResponse.model_validate(row)),
            "target": {
                "target": args.get("target"),
                "account_id": _optional_account_id(ctx, args),
            },
        }
        return _bot_canonical_action(ctx, args, preview, row.account_id)
    if not str(args.get("alias") or "").strip() or not str(args.get("target") or "").strip():
        raise ValueError("创建命令别名需要 alias 与 target")
    account_id = _optional_account_id(ctx, args)
    preview = {
        "summary": f"创建命令别名 {args['alias']} → {args['target']}",
        "mode": "create",
        "alias": args["alias"],
        "target": args["target"],
        "account_id": account_id,
    }
    return _bot_canonical_action(ctx, args, preview, account_id)


async def save_alias_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    alias_id = args.get("id") or args.get("alias_id")
    if alias_id not in (None, ""):
        target = str(args.get("target") or "").strip()
        if not target:
            raise ValueError("更新命令别名需要 target")
        current = await alias_service.get_alias(ctx.db, int(alias_id))
        if current is None:
            raise ValueError(f"命令别名 #{alias_id} 不存在")
        _require_bot_row_scope(ctx, current)
        update_fields: dict[str, Any] = {"target": target}
        if "account_id" in args or ctx.channel == "bot":
            update_fields["account_id"] = _optional_account_id(ctx, args)
        previous_account_id = current.account_id
        row = await alias_service.update_alias(
            ctx.db,
            int(alias_id),
            CommandAliasUpdate(**update_fields),
            commit=False,
        )
        if row is None:
            raise ValueError(f"命令别名 #{alias_id} 不存在")
        mode = "update"
    else:
        row = await alias_service.create_alias(
            ctx.db,
            CommandAliasCreate(
                alias=str(args.get("alias") or "").strip(),
                target=str(args.get("target") or "").strip(),
                account_id=_optional_account_id(ctx, args),
            ),
            commit=False,
        )
        mode = "create"
        previous_account_id = row.account_id
    await _store_alias_reload_scope(ctx, previous_account_id, row.account_id)
    return {
        "mode": mode,
        "alias": _dump(CommandAliasResponse.model_validate(row)),
        "business_changed": True,
    }


async def delete_alias_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    alias_id = int(args.get("id") or args.get("alias_id"))
    row = await alias_service.get_alias(ctx.db, alias_id)
    if row is None:
        raise ValueError(f"命令别名 #{alias_id} 不存在")
    _require_bot_row_scope(ctx, row)
    return {
        "summary": f"删除命令别名 #{row.id} {row.alias}",
        "account_id": row.account_id,
        "alias": _dump(CommandAliasResponse.model_validate(row)),
    }


async def delete_alias_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    alias_id = int(args.get("id") or args.get("alias_id"))
    row = await alias_service.get_alias(ctx.db, alias_id)
    if row is None:
        raise ValueError(f"命令别名 #{alias_id} 不存在")
    _require_bot_row_scope(ctx, row)
    await _store_alias_reload_scope(ctx, row.account_id)
    if not await alias_service.delete_alias(ctx.db, alias_id, commit=False):
        raise ValueError(f"命令别名 #{alias_id} 不存在")
    return {"deleted": True, "alias_id": alias_id, "business_changed": True}


async def list_sudo(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = await sudo_service.get_sudo_users(ctx.db, _optional_account_id(ctx, args))
    items = [_dump(SudoUserResponse.model_validate(row)) for row in rows]
    return {"count": len(items), "sudo_users": items}


async def save_sudo_preview(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any] | PreparedAction:
    sudo_id = args.get("id") or args.get("sudo_id")
    if sudo_id not in (None, ""):
        row = await sudo_service.get_sudo_user(ctx.db, int(sudo_id))
        if row is None:
            raise ValueError(f"Sudo 用户 #{sudo_id} 不存在")
        _require_bot_row_scope(ctx, row)
        preview = {
            "summary": f"更新 Sudo 用户 #{row.id}",
            "mode": "update",
            "account_id": row.account_id,
            "current": _dump(SudoUserResponse.model_validate(row)),
            "target_fields": {key: value for key, value in args.items() if key not in {"id", "sudo_id"}},
        }
        return _bot_canonical_action(ctx, args, preview, row.account_id)
    account_id = _optional_account_id(ctx, args)
    if account_id is None or args.get("tg_user_id") is None:
        raise ValueError("创建 Sudo 用户需要 account_id 与 tg_user_id")
    preview = {
        "summary": f"给账号 #{account_id} 添加 Sudo 用户 {args['tg_user_id']}",
        "mode": "create",
        "account_id": account_id,
        "target_fields": {**args, "account_id": account_id},
        "warning": "Sudo 用户会获得指定聊天与指令范围内的管理权限。",
    }
    return _bot_canonical_action(ctx, args, preview, account_id)


async def save_sudo_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sudo_id = args.get("id") or args.get("sudo_id")
    if sudo_id not in (None, ""):
        current = await sudo_service.get_sudo_user(ctx.db, int(sudo_id))
        if current is None:
            raise ValueError(f"Sudo 用户 #{sudo_id} 不存在")
        _require_bot_row_scope(ctx, current)
        fields = {
            key: args[key]
            for key in (
                "display_name",
                "allowed_chat_ids",
                "allowed_commands",
                "allow_all_chats",
                "allow_all_commands",
            )
            if key in args
        }
        row = await sudo_service.update_sudo_user(
            ctx.db,
            int(sudo_id),
            SudoUserUpdate(**fields),
            commit=False,
        )
        if row is None:
            raise ValueError(f"Sudo 用户 #{sudo_id} 不存在")
        mode = "update"
    else:
        account_id = _optional_account_id(ctx, args)
        if account_id is None:
            raise ValueError("创建 Sudo 用户需要 account_id")
        row = await sudo_service.create_sudo_user(
            ctx.db,
            SudoUserCreate(
                account_id=account_id,
                tg_user_id=int(args["tg_user_id"]),
                display_name=args.get("display_name"),
                allowed_chat_ids=args.get("allowed_chat_ids"),
                allowed_commands=args.get("allowed_commands"),
                allow_all_chats=bool(args.get("allow_all_chats")),
                allow_all_commands=bool(args.get("allow_all_commands")),
            ),
            commit=False,
        )
        mode = "create"
    return {
        "mode": mode,
        "sudo_user": _dump(SudoUserResponse.model_validate(row)),
        "business_changed": True,
    }


async def delete_sudo_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sudo_id = int(args.get("id") or args.get("sudo_id"))
    row = await sudo_service.get_sudo_user(ctx.db, sudo_id)
    if row is None:
        raise ValueError(f"Sudo 用户 #{sudo_id} 不存在")
    _require_bot_row_scope(ctx, row)
    return {
        "summary": f"删除 Sudo 用户 #{sudo_id}",
        "account_id": row.account_id,
        "sudo_user": _dump(SudoUserResponse.model_validate(row)),
        "warning": "删除后该用户会失去对应账号的 Sudo 权限。",
    }


async def delete_sudo_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sudo_id = int(args.get("id") or args.get("sudo_id"))
    row = await sudo_service.get_sudo_user(ctx.db, sudo_id)
    if row is not None:
        _require_bot_row_scope(ctx, row)
    if row is None or not await sudo_service.delete_sudo_user(
        ctx.db, sudo_id, commit=False
    ):
        raise ValueError(f"Sudo 用户 #{sudo_id} 不存在")
    if ctx.action is not None:
        ctx.action.account_id = row.account_id
    return {"deleted": True, "sudo_id": sudo_id, "business_changed": True}


def _account_id(ctx: ToolContext, args: dict[str, Any]) -> int:
    raw = _optional_account_id(ctx, args)
    if raw is None:
        raise ValueError("需要 account_id")
    return int(raw)


async def list_ignored(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    rows = await ignored_peer_service.list_ignored(ctx.db, account_id)
    items = [_dump(IgnoredPeerOut.model_validate(row)) for row in rows]
    return {"account_id": account_id, "count": len(items), "ignored_peers": items}


async def recent_peers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    alive, items = await ignored_peer_service.fetch_recent(account_id)
    return {
        "account_id": account_id,
        "worker_alive": alive,
        "items": mark_external_fields(
            items[:50], {"title", "name", "username", "label", "text"}
        ),
    }


async def add_ignored_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    peer_id = int(args["peer_id"])
    return {
        "summary": f"账号 #{account_id} 忽略 Peer {peer_id}",
        "account_id": account_id,
        "peer_id": peer_id,
        "peer_kind": args.get("peer_kind") or "private",
        "peer_label": args.get("peer_label"),
    }


async def add_ignored_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    row = await ignored_peer_service.add_ignored(
        ctx.db,
        account_id,
        IgnoredPeerCreate(
            peer_id=int(args["peer_id"]),
            peer_kind=args.get("peer_kind") or "private",
            peer_label=args.get("peer_label"),
        ),
    )
    return {
        "ignored_peer": _dump(IgnoredPeerOut.model_validate(row)),
        "business_changed": True,
    }


async def delete_ignored_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    ignored_id = int(args.get("id") or args.get("ignored_id"))
    rows = await ignored_peer_service.list_ignored(ctx.db, account_id)
    row = next((item for item in rows if int(item.id) == ignored_id), None)
    if row is None:
        raise ValueError(f"账号 #{account_id} 的忽略项 #{ignored_id} 不存在")
    return {
        "summary": f"移除账号 #{account_id} 的忽略项 #{ignored_id}",
        "ignored_peer": _dump(IgnoredPeerOut.model_validate(row)),
    }


async def delete_ignored_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    ignored_id = int(args.get("id") or args.get("ignored_id"))
    await ignored_peer_service.remove_ignored(ctx.db, account_id, ignored_id)
    return {
        "deleted": True,
        "account_id": account_id,
        "ignored_id": ignored_id,
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
    account_property = {"account_id": {"type": "integer"}}
    registry.register(
        ToolSpec(
            name="aliases.list",
            description="列出命令别名，可按账号筛选。",
            input_schema=_obj(account_property),
            read_handler=list_aliases,
        )
    )
    registry.register(
        ToolSpec(
            name="aliases.save",
            description="创建或更新命令别名。",
            input_schema=_obj(
                {
                    "id": {"type": "integer"},
                    "alias_id": {"type": "integer"},
                    "alias": {"type": "string"},
                    "target": {"type": "string"},
                    **account_property,
                }
            ),
            read_only=False,
            min_role="admin",
            preview_handler=save_alias_preview,
            execute_handler=save_alias_execute,
            runtime_effects=("reload_config", "reload_commands"),
        )
    )
    registry.register(
        ToolSpec(
            name="aliases.delete",
            description="删除命令别名。",
            input_schema=_obj({"id": {"type": "integer"}, "alias_id": {"type": "integer"}}),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_alias_preview,
            execute_handler=delete_alias_execute,
            runtime_effects=("reload_config", "reload_commands"),
        )
    )
    registry.register(
        ToolSpec(
            name="sudo.list",
            description="列出 Sudo 用户及聊天/指令权限范围。",
            input_schema=_obj(account_property),
            read_handler=list_sudo,
        )
    )
    sudo_properties = {
        "id": {"type": "integer"},
        "sudo_id": {"type": "integer"},
        "account_id": {"type": "integer"},
        "tg_user_id": {"type": "integer"},
        "display_name": {"type": "string"},
        "allowed_chat_ids": {"type": "array", "items": {"type": "integer"}},
        "allowed_commands": {"type": "array", "items": {"type": "string"}},
        "allow_all_chats": {"type": "boolean"},
        "allow_all_commands": {"type": "boolean"},
    }
    registry.register(
        ToolSpec(
            name="sudo.save",
            description="创建或更新 Sudo 用户权限。",
            input_schema=_obj(sudo_properties),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=save_sudo_preview,
            execute_handler=save_sudo_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="sudo.delete",
            description="删除 Sudo 用户。",
            input_schema=_obj({"id": {"type": "integer"}, "sudo_id": {"type": "integer"}}),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_sudo_preview,
            execute_handler=delete_sudo_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="ignored.list",
            description="列出账号忽略的 Peer。",
            input_schema=_obj(account_property, required=["account_id"]),
            read_handler=list_ignored,
        )
    )
    registry.register(
        ToolSpec(
            name="ignored.recent",
            description="从账号 Worker 读取最近活跃 Peer，便于选择忽略对象。",
            input_schema=_obj(account_property, required=["account_id"]),
            read_handler=recent_peers,
        )
    )
    ignored_properties = {
        "account_id": {"type": "integer"},
        "peer_id": {"type": "integer"},
        "peer_kind": {
            "type": "string",
            "enum": ["private", "group", "channel"],
        },
        "peer_label": {"type": "string"},
    }
    registry.register(
        ToolSpec(
            name="ignored.add",
            description="把一个 Peer 加入账号忽略名单。",
            input_schema=_obj(ignored_properties, required=["account_id", "peer_id"]),
            read_only=False,
            min_role="operator",
            preview_handler=add_ignored_preview,
            execute_handler=add_ignored_execute,
            runtime_effects=("reload_ignored",),
        )
    )
    registry.register(
        ToolSpec(
            name="ignored.delete",
            description="从账号忽略名单移除一项。",
            input_schema=_obj(
                {
                    "account_id": {"type": "integer"},
                    "id": {"type": "integer"},
                    "ignored_id": {"type": "integer"},
                },
                required=["account_id"],
            ),
            read_only=False,
            min_role="operator",
            preview_handler=delete_ignored_preview,
            execute_handler=delete_ignored_execute,
            runtime_effects=("reload_ignored",),
        )
    )


__all__ = ["register"]
