"""Message operation facade exposed to plugins.

The facade does not expose Telegram clients or bot tokens. In interaction
entry calls it buffers platform-standard actions; the main process later
executes those actions through the existing controlled delivery path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ...services.interaction.contracts import action_send_via_options, apply_action_send_via_options

MessageChannel = Literal["interaction_bot", "userbot_reply", "auto", "bot", "userbot"]
MessageChannelSelector = MessageChannel | list[MessageChannel] | tuple[MessageChannel, ...] | dict[str, Any]


@dataclass
class BufferedMessageOps:
    actions: list[dict[str, Any]] = field(default_factory=list)

    async def send(
        self,
        *,
        channel: MessageChannelSelector | None = None,
        chat_id: int | None = None,
        text: str,
        parse_mode: Literal["html", "plain"] = "plain",
        reply_to_message_id: int | None = None,
        reply_to_user_id: int | None = None,
        reply_to_search_limit: int | None = None,
        reply_anchor_missing_text: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        save_message_id_key: str | None = None,
        replace_saved_message_id_key: str | None = None,
        pin: bool = False,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": "send_message",
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
        }
        if reply_to_user_id is not None:
            action["reply_to_user_id"] = reply_to_user_id
        if reply_to_search_limit is not None:
            action["reply_to_search_limit"] = reply_to_search_limit
        if reply_anchor_missing_text:
            action["reply_anchor_missing_text"] = reply_anchor_missing_text
        if channel is not None:
            _apply_channel(action, channel)
        if reply_markup is not None:
            action["reply_markup"] = dict(reply_markup)
        if save_message_id_key:
            action["save_message_id_key"] = save_message_id_key
        if replace_saved_message_id_key:
            action["replace_saved_message_id_key"] = replace_saved_message_id_key
        if pin:
            action["pin"] = True
        self.actions.append(action)
        return action

    async def edit(
        self,
        *,
        channel: MessageChannelSelector | None = None,
        chat_id: int | None = None,
        message_id: int,
        text: str,
        parse_mode: Literal["html", "plain"] = "plain",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": "edit_message",
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if channel is not None:
            _apply_channel(action, channel)
        if reply_markup is not None:
            action["reply_markup"] = dict(reply_markup)
        self.actions.append(action)
        return action

    async def delete(
        self,
        *,
        channel: MessageChannelSelector | None = None,
        chat_id: int | None = None,
        message_id: int,
    ) -> dict[str, Any]:
        action = {
            "type": "delete_message",
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if channel is not None:
            _apply_channel(action, channel)
        self.actions.append(action)
        return action

    async def pin(
        self,
        *,
        channel: MessageChannelSelector | None = None,
        chat_id: int | None = None,
        message_id: int,
    ) -> dict[str, Any]:
        action = {
            "type": "pin_message",
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if channel is not None:
            _apply_channel(action, channel)
        self.actions.append(action)
        return action

    async def answer_callback(
        self,
        *,
        callback_query_id: str | None,
        text: str = "",
        show_alert: bool = False,
    ) -> dict[str, Any]:
        action = {
            "type": "answer_callback",
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        self.actions.append(action)
        return action

    async def answer_inline_query(
        self,
        *,
        inline_query_id: str | None,
        results: list[dict[str, Any]],
        cache_time: int = 0,
        is_personal: bool = True,
        next_offset: str | None = None,
        button: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": "answer_inline_query",
            "inline_query_id": inline_query_id,
            "results": list(results or []),
            "cache_time": cache_time,
            "is_personal": is_personal,
            "next_offset": next_offset,
        }
        if button is not None:
            action["button"] = dict(button)
        self.actions.append(action)
        return action

    async def payout(
        self,
        *,
        chat_id: int | None = None,
        amount: int,
        text: str | None = None,
        parse_mode: Literal["html", "plain"] = "plain",
        reply_to_message_id: int | None = None,
        reply_to_user_id: int | None = None,
        reply_to_search_limit: int | None = None,
        reply_anchor_missing_text: str | None = None,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": "payout",
            "chat_id": chat_id,
            "amount": amount,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
        }
        if text is not None:
            action["text"] = text
        if reply_to_user_id is not None:
            action["reply_to_user_id"] = reply_to_user_id
        if reply_to_search_limit is not None:
            action["reply_to_search_limit"] = reply_to_search_limit
        if reply_anchor_missing_text:
            action["reply_anchor_missing_text"] = reply_anchor_missing_text
        self.actions.append(action)
        return action

    async def update_session(
        self,
        *,
        data: dict[str, Any] | None = None,
        extend_seconds: int | None = None,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": "update_session",
            "data": dict(data or {}),
        }
        if extend_seconds is not None:
            action["extend_seconds"] = extend_seconds
        self.actions.append(action)
        return action


def _apply_channel(action: dict[str, Any], channel: MessageChannelSelector) -> None:
    action["channel_selector"] = channel
    apply_action_send_via_options(action, action_send_via_options(action))
    if isinstance(channel, (dict, list, tuple)) or str(channel or "").strip() == "auto":
        action["channel_selector"] = channel


__all__ = ["BufferedMessageOps", "MessageChannel", "MessageChannelSelector"]
