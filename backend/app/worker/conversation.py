"""与其他 Bot 进行多轮对话的工具类。

插件通过 ``PluginContext.conversation(peer)`` 获取 async context manager，
在 with 块内可以 send → get_response → click_button 循环交互。

实现原理：注册一次性 NewMessage handler 监听目标 peer 的回复，
收到后通过 asyncio.Event 通知等待方；超时或 close 时自动移除 handler。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.types import (
    Message,
    ReplyInlineMarkup,
)

log = logging.getLogger(__name__)


class ConversationTimeout(Exception):
    """等待 Bot 回复超时。"""


class Conversation:
    """与单个 peer 的对话会话。

    不要直接实例化——使用 ``conversation()`` 上下文管理器。
    """

    def __init__(self, client: TelegramClient, peer: Any, timeout: float = 30.0) -> None:
        self._client = client
        self._peer = peer
        self._timeout = timeout
        self._resolved_peer: Any = None
        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._handler_registered = False

    async def _setup(self) -> None:
        entity = await self._client.get_entity(self._peer)
        self._resolved_peer = entity
        self._peer_id = getattr(entity, "id", None)

        async def _on_new_message(event: events.NewMessage.Event) -> None:
            msg = event.message
            sender_id = getattr(msg, "sender_id", None) or getattr(
                getattr(msg, "from_id", None), "user_id", None
            )
            if sender_id == self._peer_id:
                await self._queue.put(msg)

        self._event_handler = _on_new_message
        self._client.add_event_handler(
            self._event_handler, events.NewMessage(incoming=True)
        )
        self._handler_registered = True

    async def send(self, message: str, **kwargs: Any) -> Message:
        """发送消息到对话 peer。"""
        return await self._client.send_message(self._resolved_peer, message, **kwargs)

    async def get_response(self, timeout: float | None = None) -> Message:
        """等待 peer 的下一条回复消息。超时抛 ConversationTimeout。"""
        t = timeout if timeout is not None else self._timeout
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=t)
        except TimeoutError:
            raise ConversationTimeout(
                f"等待 {self._peer} 回复超时（{t}s）"
            ) from None

    async def click_button(self, message: Message, row: int, col: int) -> None:
        """点击消息上的 InlineKeyboard 按钮。"""
        markup = message.reply_markup
        if not isinstance(markup, ReplyInlineMarkup):
            raise ValueError("消息没有 InlineKeyboard")
        rows = markup.rows
        if row < 0 or col < 0 or row >= len(rows) or col >= len(rows[row].buttons):
            raise IndexError(f"按钮索引越界: row={row}, col={col}")
        button = rows[row].buttons[col]
        data = getattr(button, "data", None)
        if data is None:
            raise ValueError("该按钮不是 callback 类型")
        from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

        await self._client(
            GetBotCallbackAnswerRequest(
                peer=self._resolved_peer,
                msg_id=message.id,
                data=data,
            )
        )

    async def mark_read(self) -> None:
        """标记对话为已读。"""
        await self._client.send_read_acknowledge(self._resolved_peer)

    async def close(self) -> None:
        """清理 event handler。"""
        if self._handler_registered:
            self._client.remove_event_handler(self._event_handler)
            self._handler_registered = False


@asynccontextmanager
async def conversation(client: TelegramClient, peer: Any, timeout: float = 30.0):
    """创建与 peer 的对话会话（async context manager）。

    用法::

        async with conversation(client, "@BotFather") as conv:
            await conv.send("/newbot")
            resp = await conv.get_response()
            print(resp.text)
    """
    conv = Conversation(client, peer, timeout)
    await conv._setup()
    try:
        yield conv
    finally:
        await conv.close()
