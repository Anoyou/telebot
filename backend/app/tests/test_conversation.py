"""Conversation 工具类单元测试。

不连真 Telethon；只验证：
- ConversationTimeout 异常
- get_response 超时行为（asyncio.wait_for）
- context manager 正常 close
- click_button 在没有 InlineKeyboard 时抛 ValueError
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.worker.conversation import Conversation, ConversationTimeout, conversation


def test_conversation_timeout_is_exception():
    """ConversationTimeout 应该是 Exception 子类。"""
    assert issubclass(ConversationTimeout, Exception)
    e = ConversationTimeout("超时了")
    assert "超时了" in str(e)


@pytest.mark.asyncio
async def test_get_response_raises_on_timeout():
    """get_response 在 queue 为空时应该抛 ConversationTimeout。"""
    client = AsyncMock()
    conv = Conversation(client, "@SomeBot", timeout=0.05)
    # 不调用 _setup，直接测 get_response 的超时路径
    with pytest.raises(ConversationTimeout, match="@SomeBot"):
        await conv.get_response()


@pytest.mark.asyncio
async def test_get_response_returns_from_queue():
    """put 一条消息进 queue 后，get_response 应该立刻返回。"""
    client = AsyncMock()
    conv = Conversation(client, "@SomeBot", timeout=5.0)

    fake_msg = MagicMock()
    await conv._queue.put(fake_msg)

    result = await conv.get_response()
    assert result is fake_msg


@pytest.mark.asyncio
async def test_get_response_respects_custom_timeout():
    """get_response(timeout=...) 应该覆盖实例默认 timeout。"""
    client = AsyncMock()
    conv = Conversation(client, "@SomeBot", timeout=10.0)
    # 用很短的 timeout 参数
    with pytest.raises(ConversationTimeout):
        await conv.get_response(timeout=0.01)


@pytest.mark.asyncio
async def test_context_manager_calls_close():
    """conversation() context manager 退出时应调用 close()。"""
    client = AsyncMock()
    client.add_event_handler = MagicMock()
    client.remove_event_handler = MagicMock()
    # mock get_entity 返回一个有 id 的对象
    entity = MagicMock()
    entity.id = 12345
    client.get_entity.return_value = entity

    async with conversation(client, "@SomeBot", timeout=1.0) as conv:
        assert isinstance(conv, Conversation)
        assert conv._handler_registered is True

    # 退出后 handler 应该被移除
    assert conv._handler_registered is False
    client.remove_event_handler.assert_called_once()


@pytest.mark.asyncio
async def test_close_idempotent():
    """多次调用 close() 不应该报错。"""
    client = AsyncMock()
    conv = Conversation(client, "@SomeBot")
    # 没注册 handler 的情况下 close 应该是 no-op
    await conv.close()
    await conv.close()
    client.remove_event_handler.assert_not_called()


@pytest.mark.asyncio
async def test_click_button_no_markup():
    """click_button 在消息没有 reply_markup 时应该抛 ValueError。"""
    client = AsyncMock()
    conv = Conversation(client, "@SomeBot")

    msg = MagicMock()
    msg.reply_markup = None

    with pytest.raises(ValueError, match="InlineKeyboard"):
        await conv.click_button(msg, 0, 0)


@pytest.mark.asyncio
async def test_click_button_wrong_markup_type():
    """click_button 在 reply_markup 不是 ReplyInlineMarkup 时应该抛 ValueError。"""
    from telethon.tl.types import ReplyKeyboardMarkup

    client = AsyncMock()
    conv = Conversation(client, "@SomeBot")

    msg = MagicMock()
    # 用 ReplyKeyboardMarkup（非 Inline）模拟
    msg.reply_markup = MagicMock(spec=ReplyKeyboardMarkup)

    with pytest.raises(ValueError, match="InlineKeyboard"):
        await conv.click_button(msg, 0, 0)


@pytest.mark.asyncio
async def test_click_button_index_out_of_range():
    """click_button 在 row/col 越界时应该抛 IndexError。"""
    from telethon.tl.types import ReplyInlineMarkup

    client = AsyncMock()
    conv = Conversation(client, "@SomeBot")

    msg = MagicMock()
    markup = MagicMock(spec=ReplyInlineMarkup)
    markup.rows = []  # 空 rows
    msg.reply_markup = markup

    with pytest.raises(IndexError, match="越界"):
        await conv.click_button(msg, 0, 0)


@pytest.mark.asyncio
async def test_setup_registers_handler():
    """_setup 应该注册 NewMessage handler。"""
    client = AsyncMock()
    client.add_event_handler = MagicMock()
    client.remove_event_handler = MagicMock()
    entity = MagicMock()
    entity.id = 999
    client.get_entity.return_value = entity

    conv = Conversation(client, "@SomeBot")
    await conv._setup()

    assert conv._handler_registered is True
    assert conv._peer_id == 999
    client.add_event_handler.assert_called_once()


@pytest.mark.asyncio
async def test_send_delegates_to_client():
    """send 应该调用 client.send_message。"""
    client = AsyncMock()
    conv = Conversation(client, "@SomeBot")
    conv._resolved_peer = MagicMock()

    await conv.send("hello")
    client.send_message.assert_called_once_with(conv._resolved_peer, "hello")
