from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import functions, types

from app.services.rich_message import (
    RICH_MESSAGE_JSON_BYTE_LIMIT,
    InputRichMessage,
    RichMessageFormat,
    RichMessageValidationError,
    build_input_rich_message,
)
from app.services.userbot_rich_message import (
    ERROR_INVALID_RICH_MESSAGE,
    ERROR_PREMIUM_REQUIRED,
    ERROR_RICH_MESSAGE_BLOCKS_UNSUPPORTED,
    ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN,
    ERROR_RICH_MESSAGE_DRAFT_DISABLED,
    ERROR_RICH_MESSAGE_MEDIA_UNSUPPORTED,
    ERROR_RICH_MESSAGE_NOT_SUPPORTED,
    ERROR_RICH_MESSAGE_POSTING_DISABLED,
    ERROR_TELEGRAM_API,
    ERROR_TELETHON_LAYER_TOO_OLD,
    RichMessageCapability,
    UserbotRichMessageError,
    build_telethon_input_rich_message,
    clear_rich_message_capability_cache,
    detect_rich_message_capability,
    edit_rich_message,
    evaluate_rich_message_capability,
    local_telethon_rich_message_capability,
    send_rich_message,
    send_rich_message_draft,
)


def _available_capability() -> RichMessageCapability:
    return evaluate_rich_message_capability(
        layer=228,
        raw_types_available=True,
        is_premium=True,
        rich_message_posting=True,
    )


class _MockClient:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.get_me = AsyncMock(return_value={"premium": True})
        self.get_input_entity = AsyncMock(return_value=types.InputPeerChat(chat_id=99))
        self.requests: list[object] = []
        self._responses = list(responses or [])

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if self._responses:
            response = self._responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return SimpleNamespace(id=101)


def test_provider_neutral_builder_serializes_and_defensively_copies() -> None:
    raw = {
        "html": "<h1>状态</h1>",
        "media": [],
        "is_rtl": True,
        "skip_entity_detection": True,
    }

    built = build_input_rich_message(raw)
    raw["html"] = "mutated"
    normalized = built.to_dict()

    assert built == InputRichMessage(
        format=RichMessageFormat.HTML,
        content="<h1>状态</h1>",
        media=[],
        is_rtl=True,
        skip_entity_detection=True,
    )
    assert normalized == {
        "html": "<h1>状态</h1>",
        "media": [],
        "is_rtl": True,
        "skip_entity_detection": True,
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "必须且只能"),
        ({"html": "a", "markdown": "b"}, "必须且只能"),
        ({"html": "中" * 10_923}, "UTF-8"),
        ({"blocks": [{"type": "divider"} for _ in range(501)]}, "最多 500"),
        ({"html": "x", "media": [{} for _ in range(51)]}, "最多 50"),
        (
            {"blocks": [{"type": "table", "cells": [[{"text": "x", "colspan": 21}]]}]},
            "最多 20",
        ),
        ({"html": "x", "is_rtl": "yes"}, "必须是布尔值"),
    ],
)
def test_provider_neutral_builder_rejects_protocol_limit_violations(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(RichMessageValidationError, match=message) as exc_info:
        build_input_rich_message(payload)

    assert exc_info.value.code == ERROR_INVALID_RICH_MESSAGE


def test_provider_neutral_builder_uses_utf8_bytes_not_characters() -> None:
    accepted = build_input_rich_message({"html": "中" * 10_922})
    assert accepted.format is RichMessageFormat.HTML

    with pytest.raises(RichMessageValidationError, match="32768 bytes"):
        build_input_rich_message({"html": "中" * 10_923})


def test_provider_neutral_builder_preserves_empty_optional_format_semantics() -> None:
    built = build_input_rich_message(
        {
            "html": "",
            "markdown": "# 唯一有效输入",
            "blocks": [],
        }
    )

    assert built.format is RichMessageFormat.MARKDOWN
    assert built.to_dict() == {"markdown": "# 唯一有效输入"}


def test_provider_neutral_builder_rejects_nesting_and_json_size() -> None:
    nested: object = "value"
    for _ in range(17):
        nested = [nested]
    with pytest.raises(RichMessageValidationError, match="嵌套 16 层"):
        build_input_rich_message({"blocks": [{"type": "paragraph", "value": nested}]})

    oversized = "x" * RICH_MESSAGE_JSON_BYTE_LIMIT
    with pytest.raises(RichMessageValidationError, match="1 MiB"):
        build_input_rich_message({"blocks": [{"type": "custom", "data": oversized}]})


def test_telethon_builder_supports_html_and_markdown_flags() -> None:
    html = build_telethon_input_rich_message(
        {"html": "<h1>状态</h1>", "is_rtl": True, "skip_entity_detection": True}
    )
    markdown = build_telethon_input_rich_message({"markdown": "# 状态"})

    assert isinstance(html, types.InputRichMessageHTML)
    assert html.html == "<h1>状态</h1>"
    assert html.rtl is True
    assert html.noautolink is True
    assert html.files is None
    assert isinstance(markdown, types.InputRichMessageMarkdown)
    assert markdown.markdown == "# 状态"


def test_telethon_builder_converts_text_only_blocks_to_rich_html() -> None:
    built = build_telethon_input_rich_message(
        {
            "blocks": [
                {"type": "heading", "text": "巡检", "size": 2},
                {"type": "paragraph", "text": {"type": "bold", "text": "正常"}},
            ]
        }
    )

    assert isinstance(built, types.InputRichMessageHTML)
    assert built.html == "<h2>巡检</h2>\n<p><b>正常</b></p>"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"blocks": [{"type": "table", "cells": []}]}, ERROR_RICH_MESSAGE_BLOCKS_UNSUPPORTED),
        ({"html": "<b>媒体</b>", "media": [{"id": "photo"}]}, ERROR_RICH_MESSAGE_MEDIA_UNSUPPORTED),
        ({"html": "", "markdown": ""}, ERROR_INVALID_RICH_MESSAGE),
    ],
)
def test_telethon_builder_rejects_unsupported_input_without_text_fallback(
    payload: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(UserbotRichMessageError) as exc_info:
        build_telethon_input_rich_message(payload)

    assert exc_info.value.code == code


def test_local_capability_matches_pinned_layer_228_schema() -> None:
    layer, raw_types_available = local_telethon_rich_message_capability()

    assert layer == 228
    assert raw_types_available is True


@pytest.mark.parametrize(
    ("kwargs", "error_code"),
    [
        ({"layer": 227, "raw_types_available": True}, ERROR_TELETHON_LAYER_TOO_OLD),
        (
            {"layer": 228, "raw_types_available": True, "is_premium": False},
            ERROR_PREMIUM_REQUIRED,
        ),
        (
            {"layer": 228, "raw_types_available": False},
            ERROR_RICH_MESSAGE_NOT_SUPPORTED,
        ),
        (
            {
                "layer": 228,
                "raw_types_available": True,
                "is_premium": True,
                "rich_message_posting": False,
            },
            ERROR_RICH_MESSAGE_POSTING_DISABLED,
        ),
    ],
)
def test_pure_capability_gate_has_stable_error_codes(
    kwargs: dict[str, object],
    error_code: str,
) -> None:
    capability = evaluate_rich_message_capability(**kwargs)  # type: ignore[arg-type]

    assert capability.available is False
    assert capability.error_code == error_code
    with pytest.raises(UserbotRichMessageError) as exc_info:
        capability.require()
    assert exc_info.value.code == error_code


@pytest.mark.asyncio
async def test_async_capability_probe_checks_premium_app_config_and_caches() -> None:
    clear_rich_message_capability_cache()
    client = _MockClient(
        responses=[
            types.help.AppConfig(
                hash=1,
                config=types.JsonObject(
                    value=[
                        types.JsonObjectValue(
                            key="rich_message_posting",
                            value=types.JsonBool(value=True),
                        )
                    ]
                ),
            )
        ]
    )

    first = await detect_rich_message_capability(client)
    second = await detect_rich_message_capability(client)

    assert first.available is True
    assert first.is_premium is True
    assert first.rich_message_posting is True
    assert second is first
    client.get_me.assert_awaited_once_with()
    assert len(client.requests) == 1
    assert isinstance(client.requests[0], functions.help.GetAppConfigRequest)


@pytest.mark.asyncio
async def test_partial_capability_probe_does_not_poison_full_probe_cache() -> None:
    clear_rich_message_capability_cache()
    client = _MockClient(
        responses=[
            types.help.AppConfig(
                hash=1,
                config=types.JsonObject(
                    value=[
                        types.JsonObjectValue(
                            key="rich_message_posting",
                            value=types.JsonBool(value=True),
                        )
                    ]
                ),
            )
        ]
    )

    local_only = await detect_rich_message_capability(
        client,
        query_me=False,
        query_app_config=False,
    )
    fully_probed = await detect_rich_message_capability(client)

    assert local_only.is_premium is None
    assert fully_probed.is_premium is True
    assert fully_probed.rich_message_posting is True
    client.get_me.assert_awaited_once_with()
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_async_capability_probe_rejects_non_premium_and_disabled_config() -> None:
    non_premium = await detect_rich_message_capability(
        _MockClient(),
        me={"premium": False},
        app_config={"rich_message_posting": True},
    )
    disabled = await detect_rich_message_capability(
        _MockClient(),
        me={"premium": True},
        app_config={"rich_message_posting": False},
    )

    assert non_premium.error_code == ERROR_PREMIUM_REQUIRED
    assert disabled.error_code == ERROR_RICH_MESSAGE_POSTING_DISABLED


@pytest.mark.asyncio
async def test_async_capability_probe_fails_closed_when_remote_state_is_unknown() -> None:
    get_me_failed = _MockClient(responses=[types.help.AppConfig(hash=1, config=types.JsonObject(value=[]))])
    get_me_failed.get_me.side_effect = RuntimeError("timeout")
    missing_config = await detect_rich_message_capability(
        _MockClient(),
        me={"premium": True},
        app_config={},
    )
    probe_failed = await detect_rich_message_capability(get_me_failed, force_refresh=True)

    assert probe_failed.available is False
    assert probe_failed.error_code == ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN
    assert probe_failed.probe_errors == ("get_me:RuntimeError",)
    assert missing_config.error_code == ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN


@pytest.mark.asyncio
async def test_transient_capability_probe_failures_are_not_cached() -> None:
    clear_rich_message_capability_cache()
    client = _MockClient(responses=[RuntimeError("timeout"), RuntimeError("timeout")])

    first = await detect_rich_message_capability(client)
    second = await detect_rich_message_capability(client)

    assert first.error_code == ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN
    assert second.error_code == ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN
    assert first.probe_errors == ("get_app_config:RuntimeError",)
    assert second.probe_errors == ("get_app_config:RuntimeError",)
    assert client.get_me.await_count == 2
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_async_capability_probe_treats_unset_telethon_premium_flag_as_false() -> None:
    capability = await detect_rich_message_capability(
        _MockClient(),
        me=SimpleNamespace(premium=None),
        app_config={"rich_message_posting": True},
    )

    assert capability.is_premium is False
    assert capability.error_code == ERROR_PREMIUM_REQUIRED


@pytest.mark.asyncio
async def test_send_raw_rich_message_preserves_reply_and_returns_action_shape() -> None:
    response = SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=321))])
    client = _MockClient(responses=[response])

    result = await send_rich_message(
        client,
        -100123,
        {"html": "<h1>巡检</h1>"},
        reply_to_message_id=41,
        capability=_available_capability(),
    )

    request = client.requests[0]
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.message == ""
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert request.rich_message.html == "<h1>巡检</h1>"
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.reply_to_msg_id == 41
    assert result == {
        "message_id": 321,
        "chat_id": -100123,
        "reply_to_message_id": 41,
        "rich_message_format": "html",
        "actual_send_via": "userbot_reply",
    }


@pytest.mark.asyncio
async def test_edit_raw_rich_message_returns_target_id_when_updates_omit_it() -> None:
    client = _MockClient(responses=[SimpleNamespace(updates=[])])

    result = await edit_rich_message(
        client,
        -100123,
        77,
        {"markdown": "# 更新"},
        capability=_available_capability(),
    )

    request = client.requests[0]
    assert isinstance(request, functions.messages.EditMessageRequest)
    assert request.id == 77
    assert request.message is None
    assert isinstance(request.rich_message, types.InputRichMessageMarkdown)
    assert result == {
        "message_id": 77,
        "chat_id": -100123,
        "rich_message_format": "markdown",
        "actual_send_via": "userbot_reply",
    }


@pytest.mark.asyncio
async def test_userbot_rich_draft_is_disabled_by_default_and_uses_set_typing_when_enabled() -> None:
    client = _MockClient(responses=[True])
    with pytest.raises(UserbotRichMessageError) as exc_info:
        await send_rich_message_draft(client, -100123, 91, {"html": "<tg-thinking>思考</tg-thinking>"})
    assert exc_info.value.code == ERROR_RICH_MESSAGE_DRAFT_DISABLED

    result = await send_rich_message_draft(
        client,
        -100123,
        91,
        {"html": "<tg-thinking>思考</tg-thinking>"},
        message_thread_id=7,
        enabled=True,
        capability=_available_capability(),
    )
    request = client.requests[0]
    assert isinstance(request, functions.messages.SetTypingRequest)
    assert request.top_msg_id == 7
    assert isinstance(request.action, types.InputSendMessageRichMessageDraftAction)
    assert request.action.random_id == 91
    assert result["ephemeral"] is True


@pytest.mark.asyncio
async def test_send_maps_telethon_failure_to_stable_error_code() -> None:
    client = _MockClient(responses=[RuntimeError("RPC failed")])

    with pytest.raises(UserbotRichMessageError) as exc_info:
        await send_rich_message(
            client,
            -100123,
            {"html": "<b>状态</b>"},
            capability=_available_capability(),
        )

    assert exc_info.value.code == ERROR_TELEGRAM_API
    assert exc_info.value.to_result(chat_id=-100123)["error_code"] == ERROR_TELEGRAM_API


@pytest.mark.asyncio
async def test_send_capability_gate_does_not_call_telegram_or_downgrade() -> None:
    client = _MockClient()
    blocked = evaluate_rich_message_capability(
        layer=228,
        raw_types_available=True,
        is_premium=False,
    )

    with pytest.raises(UserbotRichMessageError) as exc_info:
        await send_rich_message(
            client,
            -100123,
            {"html": "<b>状态</b>"},
            capability=blocked,
        )

    assert exc_info.value.code == ERROR_PREMIUM_REQUIRED
    assert client.requests == []
    client.get_input_entity.assert_not_awaited()
