"""消息模板目录、渲染验证与测试发送工作流。"""

from __future__ import annotations

from typing import Any

from ....schemas.message_template import (
    MessageTemplateRenderRequest,
    MessageTemplateTestSendRequest,
)
from ....services import message_template_service
from ..context import ToolContext
from ..registry import PreparedAction, ToolRegistry, ToolSpec
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


async def catalog(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    result = await message_template_service.build_catalog(ctx.db, account_id)
    return result.model_dump(mode="json")


async def render(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    result = message_template_service.render_template(
        MessageTemplateRenderRequest(
            template=str(args.get("template") or ""),
            sample_data=args.get("sample_data") or {},
            parse_mode=args.get("parse_mode", "HTML"),
        )
    )
    return result.model_dump(mode="json")


async def test_send_preview(ctx: ToolContext, args: dict[str, Any]) -> PreparedAction:
    account_id = _account_id(ctx, args)
    target_chat_id = int(args["target_chat_id"])
    if args.get("template") is not None:
        rendered = await render(ctx, args)
        if not bool(rendered.get("validation", {}).get("ok")):
            raise ValueError("模板渲染验证失败，请先修正后再测试发送")
        text = str(rendered.get("text") or "")
        parse_mode = rendered.get("parse_mode")
        validation = rendered.get("validation")
    else:
        text = str(args.get("text") or "")
        parse_mode = args.get("parse_mode", "HTML")
        rendered_direct = await render(
            ctx,
            {"template": text, "sample_data": {}, "parse_mode": parse_mode},
        )
        if not bool(rendered_direct.get("validation", {}).get("ok")):
            raise ValueError("消息内容验证失败，请先修正后再测试发送")
        validation = rendered_direct.get("validation")
    if not text:
        raise ValueError("测试发送需要 template 或 text")
    canonical = {
        "account_id": account_id,
        "target_chat_id": target_chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    return PreparedAction(
        arguments=canonical,
        preview={
            "summary": f"向授权私聊 {target_chat_id} 测试发送消息模板",
            "account_id": account_id,
            "target_chat_id": target_chat_id,
            "parse_mode": parse_mode,
            "rendered_text": text,
            "validation": validation,
            "warning": "确认后会真实发送一条 Telegram 私聊消息。",
        },
    )


async def test_send_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # 真实 Telegram 投递在 Action 提交后执行，避免事务失败但消息已发出。
    MessageTemplateTestSendRequest(
        account_id=int(args["account_id"]),
        target_chat_id=int(args["target_chat_id"]),
        text=str(args["text"]),
        parse_mode=args.get("parse_mode", "HTML"),
    )
    return {
        "account_id": int(args["account_id"]),
        "runtime_sync_required": True,
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
    registry.register(
        ToolSpec(
            name="message_templates.catalog",
            description="读取账号可用的系统、AI 指令和插件消息模板目录与示例变量。",
            input_schema=_obj({"account_id": {"type": "integer"}}, required=["account_id"]),
            read_handler=catalog,
        )
    )
    render_fields = {
        "template": {"type": "string", "maxLength": 10000},
        "sample_data": {"type": "object"},
        "parse_mode": {"type": ["string", "null"]},
    }
    registry.register(
        ToolSpec(
            name="message_templates.render",
            description="用模拟数据渲染消息模板，并验证 Telegram HTML/实体。",
            input_schema=_obj(render_fields, required=["template"]),
            read_handler=render,
        )
    )
    registry.register(
        ToolSpec(
            name="message_templates.test_send",
            description="渲染验证后，向当前账号已授权且启动过 Bot 的私聊真实测试发送。",
            input_schema=_obj(
                {
                    "account_id": {"type": "integer"},
                    "target_chat_id": {"type": "integer"},
                    "template": {"type": "string", "maxLength": 10000},
                    "text": {"type": "string", "maxLength": 4000},
                    "sample_data": {"type": "object"},
                    "parse_mode": {"type": ["string", "null"]},
                },
                required=["account_id", "target_chat_id"],
            ),
            read_only=False,
            min_role="operator",
            preview_handler=test_send_preview,
            execute_handler=test_send_execute,
            runtime_effects=("message_template_test_send",),
            runtime_retryable=False,
        )
    )


__all__ = ["register"]
