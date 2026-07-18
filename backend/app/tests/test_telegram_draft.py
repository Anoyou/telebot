from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import account_bot_service


@pytest.mark.asyncio
async def test_send_message_draft_uses_ephemeral_bot_api_method(monkeypatch: pytest.MonkeyPatch) -> None:
    call_bot_api = AsyncMock(return_value={"result": True})
    monkeypatch.setattr(account_bot_service, "call_bot_api", call_bot_api)

    result = await account_bot_service.send_message_draft(
        "123:token",
        42,
        9001,
        "<b>生成中</b>",
        message_thread_id=7,
    )

    assert result == {"result": True}
    call_bot_api.assert_awaited_once_with(
        "123:token",
        "sendMessageDraft",
        {
            "chat_id": 42,
            "message_thread_id": 7,
            "draft_id": 9001,
            "text": "<b>生成中</b>",
            "parse_mode": "HTML",
        },
    )


@pytest.mark.asyncio
async def test_send_rich_message_draft_validates_and_calls_bot_api(monkeypatch: pytest.MonkeyPatch) -> None:
    call_bot_api = AsyncMock(return_value={"result": True})
    monkeypatch.setattr(account_bot_service, "call_bot_api", call_bot_api)

    await account_bot_service.send_rich_message_draft(
        "123:token",
        42,
        -99,
        {"html": "<tg-thinking>处理中</tg-thinking>"},
    )

    call_bot_api.assert_awaited_once_with(
        "123:token",
        "sendRichMessageDraft",
        {
            "chat_id": 42,
            "draft_id": -99,
            "rich_message": {"html": "<tg-thinking>处理中</tg-thinking>"},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_id", [0, True, "1"])
async def test_draft_id_must_be_nonzero_integer(draft_id: object) -> None:
    with pytest.raises(ValueError, match="draft_id"):
        await account_bot_service.send_message_draft("123:token", 42, draft_id)  # type: ignore[arg-type]
