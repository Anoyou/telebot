"""项目通知 Bot 配置与测试发送工作流。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....crypto import encrypt_str
from ....db.models.account_bot import AccountBot
from ....db.models.notify import NotifyBot
from ..context import ToolContext
from ..registry import PreparedAction, ToolRegistry, ToolSpec


async def _view(ctx: ToolContext, row: NotifyBot) -> dict[str, Any]:
    source = None
    if row.source_account_id is not None:
        source = (
            await ctx.db.execute(
                select(AccountBot).where(AccountBot.account_id == int(row.source_account_id))
            )
        ).scalar_one_or_none()
    return {
        "id": row.id,
        "name": row.name,
        "default_chat_id": row.default_chat_id,
        "enabled": row.enabled,
        "has_token": bool(source.bot_token_enc) if source else bool(row.bot_token_enc),
        "credential_source": "account_bot" if row.source_account_id else "direct",
        "source_account_id": row.source_account_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def list_bots(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = list((await ctx.db.execute(select(NotifyBot).order_by(NotifyBot.id.asc()))).scalars().all())
    return {"count": len(rows), "notify_bots": [await _view(ctx, row) for row in rows]}


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot_id = args.get("id") or args.get("bot_id")
    if args.get("bot_token") and args.get("source_account_id") is not None:
        raise ValueError("独立 Bot Token 与引用管理 Bot 不能同时设置")
    if bot_id not in (None, ""):
        row = await ctx.db.get(NotifyBot, int(bot_id))
        if row is None:
            raise ValueError(f"通知 Bot #{bot_id} 不存在")
        return {
            "summary": f"更新通知 Bot #{bot_id} {row.name}",
            "mode": "update",
            "current": await _view(ctx, row),
            "target_fields": {
                key: ("***" if key == "bot_token" and value else value)
                for key, value in args.items()
                if key not in {"id", "bot_id"}
            },
            "has_bot_token_input": bool(args.get("bot_token")),
        }
    if not str(args.get("name") or "").strip() or args.get("default_chat_id") is None:
        raise ValueError("创建通知 Bot 需要 name 与 default_chat_id")
    if not args.get("bot_token") and args.get("source_account_id") is None:
        raise ValueError("必须提供独立 Bot Token 或 source_account_id")
    return {
        "summary": f"创建通知 Bot「{args['name']}」",
        "mode": "create",
        "name": args["name"],
        "default_chat_id": args["default_chat_id"],
        "enabled": bool(args.get("enabled", True)),
        "credential_source": ("account_bot" if args.get("source_account_id") is not None else "direct"),
        "source_account_id": args.get("source_account_id"),
        "has_bot_token_input": bool(args.get("bot_token")),
    }


async def _validate_source(ctx: ToolContext, source_account_id: int | None) -> None:
    if source_account_id is None:
        return
    source = (
        await ctx.db.execute(select(AccountBot).where(AccountBot.account_id == source_account_id))
    ).scalar_one_or_none()
    if source is None or not source.bot_token_enc:
        raise ValueError(f"账号 #{source_account_id} 尚未配置管理 Bot Token")


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot_id = args.get("id") or args.get("bot_id")
    source_touched = "source_account_id" in args
    token_touched = args.get("bot_token") not in (None, "")
    if source_touched and token_touched:
        raise ValueError("独立 Bot Token 与引用管理 Bot 不能同时设置")
    if source_touched:
        await _validate_source(ctx, args.get("source_account_id"))
    if bot_id not in (None, ""):
        row = await ctx.db.get(NotifyBot, int(bot_id))
        if row is None:
            raise ValueError(f"通知 Bot #{bot_id} 不存在")
        for key in ("name", "default_chat_id", "enabled"):
            if key in args and args[key] is not None:
                setattr(row, key, args[key])
        if bool(args.get("clear_token")):
            row.bot_token_enc = None
            row.source_account_id = None
        if source_touched:
            row.source_account_id = args.get("source_account_id")
            if row.source_account_id is not None:
                row.bot_token_enc = None
        if token_touched:
            row.bot_token_enc = encrypt_str(str(args["bot_token"]))
            row.source_account_id = None
        mode = "update"
    else:
        source_account_id = args.get("source_account_id")
        await _validate_source(ctx, source_account_id)
        row = NotifyBot(
            name=str(args["name"]).strip(),
            default_chat_id=int(args["default_chat_id"]),
            enabled=bool(args.get("enabled", True)),
            source_account_id=source_account_id,
            bot_token_enc=(encrypt_str(str(args["bot_token"])) if args.get("bot_token") else None),
        )
        ctx.db.add(row)
        mode = "create"
    await ctx.db.flush()
    return {
        "mode": mode,
        "notify_bot": await _view(ctx, row),
        "business_changed": True,
    }


async def delete_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot_id = int(args.get("id") or args.get("bot_id"))
    row = await ctx.db.get(NotifyBot, bot_id)
    if row is None:
        raise ValueError(f"通知 Bot #{bot_id} 不存在")
    return {
        "summary": f"删除通知 Bot #{bot_id} {row.name}",
        "notify_bot": await _view(ctx, row),
        "warning": "依赖该路由名的告警或定时报告将无法发送。",
    }


async def delete_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot_id = int(args.get("id") or args.get("bot_id"))
    row = await ctx.db.get(NotifyBot, bot_id)
    if row is None:
        raise ValueError(f"通知 Bot #{bot_id} 不存在")
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "bot_id": bot_id, "business_changed": True}


async def test_preview(ctx: ToolContext, args: dict[str, Any]) -> PreparedAction:
    bot_id = int(args.get("id") or args.get("bot_id"))
    row = await ctx.db.get(NotifyBot, bot_id)
    if row is None or not row.enabled:
        raise ValueError(f"通知 Bot #{bot_id} 不存在或未启用")
    text_value = str(args.get("text") or "TelePilot 通知通道测试")
    return PreparedAction(
        arguments={
            "bot_id": bot_id,
            "target_chat_id": int(row.default_chat_id),
            "text": text_value,
        },
        preview={
            "summary": f"通过通知 Bot「{row.name}」测试发送",
            "notify_bot": await _view(ctx, row),
            "target_chat_id": int(row.default_chat_id),
            "text": text_value,
            "warning": "确认后只会向本卡片列出的 Chat ID 真实发送消息。",
        },
    )


async def test_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    bot_id = int(args.get("id") or args.get("bot_id"))
    row = await ctx.db.get(NotifyBot, bot_id)
    if row is None or not row.enabled:
        raise ValueError(f"通知 Bot #{bot_id} 不存在或未启用")
    if args.get("target_chat_id") is None:
        raise ValueError("缺少已确认的 target_chat_id，请重新发起测试发送")
    return {
        "bot_id": bot_id,
        "target_chat_id": int(args["target_chat_id"]),
        "runtime_sync_required": True,
        "business_changed": True,
    }


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


def register(registry: ToolRegistry) -> None:
    ids = {"id": {"type": "integer"}, "bot_id": {"type": "integer"}}
    registry.register(
        ToolSpec(
            name="notifications.list",
            channels=("web",),
            description="列出项目通知 Bot 路由、默认 Chat 与凭据来源，不返回 Token。",
            input_schema=_obj({}),
            read_handler=list_bots,
        )
    )
    registry.register(
        ToolSpec(
            name="notifications.save",
            channels=("web",),
            description="创建或更新通知 Bot，可使用独立 Token 或引用账号管理 Bot。",
            input_schema=_obj(
                {
                    **ids,
                    "name": {"type": "string"},
                    "default_chat_id": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                    "bot_token": {"type": "string"},
                    "source_account_id": {"type": ["integer", "null"]},
                    "clear_token": {"type": "boolean"},
                }
            ),
            read_only=False,
            min_role="admin",
            preview_handler=save_preview,
            execute_handler=save_execute,
            secret_argument_names=("bot_token",),
        )
    )
    registry.register(
        ToolSpec(
            name="notifications.test",
            channels=("web",),
            description="通过已启用的通知 Bot 向默认 Chat ID 真实测试发送。",
            input_schema=_obj({**ids, "text": {"type": "string", "maxLength": 4096}}),
            read_only=False,
            min_role="operator",
            preview_handler=test_preview,
            execute_handler=test_execute,
            runtime_effects=("notification_test_send",),
            runtime_retryable=False,
        )
    )
    registry.register(
        ToolSpec(
            name="notifications.delete",
            channels=("web",),
            description="删除项目通知 Bot 路由。",
            input_schema=_obj(ids),
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_preview,
            execute_handler=delete_execute,
        )
    )


__all__ = ["register"]
