"""最小 Event Bus + MessageOps 插件：收到 ping 回复 pong。"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class HelloPingPlugin(Plugin):
    key = "hello_ping"
    display_name = "Hello Ping"

    async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
        event = event_from_interaction_payload(payload)
        if event.type != "message":
            return []
        if event.message.text.strip().lower() != "ping":
            return []
        return [
            {
                "type": "send_message",
                "chat_id": event.message.chat_id,
                "reply_to_message_id": event.message.message_id,
                "text": "pong",
            }
        ]
