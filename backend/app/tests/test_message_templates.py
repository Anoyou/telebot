from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.schemas.message_template import (
    MessageTemplateRenderRequest,
    MessageTemplateTestSendRequest,
)
from app.services import account_bot_service, message_template_service
from app.services.llm_format import DEFAULT_TEMPLATE


@pytest.mark.asyncio
async def test_catalog_discovers_feature_template_fields(monkeypatch) -> None:
    feature = SimpleNamespace(
        key="demo_plugin",
        display_name="演示模块",
        manifest={
            "config_schema": {
                "type": "object",
                "properties": {
                    "message_template": {
                        "type": "string",
                        "title": "消息模板",
                        "description": "支持 {name}",
                        "default": "默认 {name}",
                    },
                    "success_template": {
                        "type": "string",
                        "title": "成功模板",
                        "default": "完成 {status}",
                    },
                    "style_templates": {
                        "type": "string",
                        "title": "风格模板",
                        "default": "写实={prompt}",
                    },
                },
            },
        },
    )
    monkeypatch.setattr(account_bot_service, "ensure_account", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "transfer_notice_template": "转账 {amount}",
                "response_template": "收到 {payer_name}",
            }
        ),
    )
    monkeypatch.setattr(
        message_template_service.feature_service,
        "list_features",
        AsyncMock(return_value=[feature]),
    )
    monkeypatch.setattr(
        message_template_service.feature_service,
        "get_effective_plugin_config",
        AsyncMock(
            return_value={
                "message_template": "当前 {name}",
                "success_template": "已完成 {status}",
                "style_templates": "不应收录",
            }
        ),
    )
    monkeypatch.setattr(
        message_template_service.command_service,
        "list_templates",
        AsyncMock(return_value=[]),
    )

    catalog = await message_template_service.build_catalog(AsyncMock(), 7)

    field_keys = {item.field_key for item in catalog.items}
    assert "transfer_notice_template" in field_keys
    assert "response_template" in field_keys
    assert "message_template" in field_keys
    assert "success_template" in field_keys
    assert "style_templates" not in field_keys
    message_item = next(item for item in catalog.items if item.field_key == "message_template")
    assert message_item.feature_key == "demo_plugin"
    assert message_item.template == "当前 {name}"
    assert message_item.sample_data["name"] == "示例名称"


@pytest.mark.asyncio
async def test_catalog_discovers_ai_command_output_templates(monkeypatch) -> None:
    custom_ai = SimpleNamespace(
        id=1,
        name="ai",
        type="ai",
        config={
            "output_template": "<b>{answer}</b>\n<code>{model}</code>",
            "output_format": "html",
            "mode": "chat",
        },
        description="AI 问答",
    )
    default_ai = SimpleNamespace(
        id=2,
        name="sum",
        type="ai",
        config={
            "output_format": "plain",
            "mode": "chat",
        },
        description="总结",
    )
    monkeypatch.setattr(account_bot_service, "ensure_account", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        message_template_service.feature_service,
        "list_features",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        message_template_service.command_service,
        "list_templates",
        AsyncMock(return_value=[custom_ai, default_ai]),
    )

    catalog = await message_template_service.build_catalog(AsyncMock(), 7)

    ai_item = next(item for item in catalog.items if item.id == "command.ai.output_template")
    sum_item = next(item for item in catalog.items if item.id == "command.sum.output_template")
    assert ai_item.group == "AI 指令模板"
    assert ai_item.field_key == "output_template"
    assert ai_item.template == "<b>{answer}</b>\n<code>{model}</code>"
    assert ai_item.parse_mode == "HTML"
    assert ai_item.sample_data["answer"]
    assert ai_item.sample_data["command"] == "ai"
    assert sum_item.template == DEFAULT_TEMPLATE
    assert sum_item.parse_mode is None


def test_render_returns_code_language_and_blockquote_entities() -> None:
    result = message_template_service.render_template(
        MessageTemplateRenderRequest(
            template=(
                '<pre><code class="language-python">{code}</code></pre>'
                '<blockquote expandable>{quote}</blockquote>'
            ),
            sample_data={"code": "print(1)", "quote": "hello"},
            parse_mode="html",
        )
    )

    assert result.text == (
        '<pre><code class="language-python">print(1)</code></pre>'
        '<blockquote expandable>hello</blockquote>'
    )
    assert result.validation.ok is True
    pre = next(entity for entity in result.entities if entity.type == "pre")
    quote = next(entity for entity in result.entities if entity.type == "blockquote")
    assert pre.language == "python"
    assert quote.collapsed is True


def test_render_rejects_unknown_html_tags() -> None:
    result = message_template_service.render_template(
        MessageTemplateRenderRequest(
            template="<foo>{value}</foo>",
            sample_data={"value": "bad"},
            parse_mode="HTML",
        )
    )

    assert result.validation.ok is False
    assert any("不支持的 HTML 标签" in error for error in result.validation.errors)
    assert result.entities == []


@pytest.mark.asyncio
async def test_test_send_rejects_unauthorized_chat_id(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "list_bot_users",
        AsyncMock(
            return_value=[
                SimpleNamespace(enabled=True, tg_user_id=12345, last_chat_id=12345),
            ]
        ),
    )
    get_bot_config = AsyncMock()
    send_message = AsyncMock()
    monkeypatch.setattr(account_bot_service, "get_bot_config", get_bot_config)
    monkeypatch.setattr(account_bot_service, "send_message", send_message)

    with pytest.raises(HTTPException) as exc_info:
        await message_template_service.send_test_message(
            AsyncMock(),
            MessageTemplateTestSendRequest(
                account_id=1,
                target_chat_id=67890,
                text="hello",
                parse_mode="HTML",
            ),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "MESSAGE_TEMPLATE_TARGET_NOT_ALLOWED"
    get_bot_config.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_send_rejects_invalid_html(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "list_bot_users",
        AsyncMock(
            return_value=[
                SimpleNamespace(enabled=True, tg_user_id=12345, last_chat_id=12345),
            ]
        ),
    )
    get_bot_config = AsyncMock()
    send_message = AsyncMock()
    monkeypatch.setattr(account_bot_service, "get_bot_config", get_bot_config)
    monkeypatch.setattr(account_bot_service, "send_message", send_message)

    with pytest.raises(HTTPException) as exc_info:
        await message_template_service.send_test_message(
            AsyncMock(),
            MessageTemplateTestSendRequest(
                account_id=1,
                target_chat_id=12345,
                text="<foo>bad</foo>",
                parse_mode="HTML",
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "MESSAGE_TEMPLATE_TEST_SEND_INVALID"
    get_bot_config.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_send_records_trace_action(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "list_bot_users",
        AsyncMock(
            return_value=[
                SimpleNamespace(enabled=True, tg_user_id=12345, last_chat_id=12345),
            ]
        ),
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_bot_config",
        AsyncMock(return_value=SimpleNamespace(bot_token_enc="enc-token")),
    )
    monkeypatch.setattr(account_bot_service, "decrypt_bot_token", lambda _row: "bot-token")
    monkeypatch.setattr(
        account_bot_service,
        "send_message",
        AsyncMock(return_value={"message_id": 321}),
    )
    trace = SimpleNamespace(trace_id="evt_message_template_test")
    record_action = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(message_template_service, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(message_template_service, "record_action", record_action)
    monkeypatch.setattr(message_template_service, "finish_trace", finish_trace)

    result = await message_template_service.send_test_message(
        AsyncMock(),
        MessageTemplateTestSendRequest(
            account_id=1,
            target_chat_id=12345,
            text="hello",
            parse_mode="HTML",
        ),
    )

    assert result.message_id == 321
    record_action.assert_awaited_once()
    assert record_action.await_args.args[0] is trace
    assert record_action.await_args.args[1]["type"] == "send_message"
    assert record_action.await_args.kwargs["actual_send_via"] == "account_bot"
    finish_trace.assert_awaited_once_with(trace, message_template_service.TRACE_STATUS_OK)
