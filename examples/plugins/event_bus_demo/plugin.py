"""Event Bus + Trace + MessageOps 示例插件。"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class EventBusDemoPlugin(Plugin):
    key = "event_bus_demo"
    display_name = "Event Bus 示例"

    async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """处理标准事件信封。

        当前示例只读取稳定字段。`native_raw` 只能在 manifest 声明
        capabilities.telegram_native_raw 后用于排障，不能成为业务主路径。
        """

        event = event_from_interaction_payload(payload)
        event_type = event.type
        actions: list[dict[str, Any]] = []

        if event_type == "callback_query":
            actions.append(
                {
                    "type": "answer_callback",
                    "callback_query_id": event.callback.id if event.callback else None,
                    "text": "按钮已收到",
                    "show_alert": False,
                }
            )
            return actions

        if event_type == "inline_query":
            inline_query = payload.get("inline_query") if isinstance(payload.get("inline_query"), dict) else {}
            query_id = str(inline_query.get("id") or "")
            actions.append(
                {
                    "type": "answer_inline_query",
                    "inline_query_id": query_id,
                    "results": [
                        {
                            "type": "article",
                            "id": "event-bus-demo",
                            "title": "Event Bus 示例",
                            "input_message_content": {"message_text": "来自 Event Bus 的 inline 结果"},
                        }
                    ],
                    "cache_time": 0,
                    "is_personal": True,
                }
            )
            return actions

        if event_type == "chosen_inline_result":
            return [{"type": "result", "success": True, "result": {"chosen": True}}]

        if event_type == "payment_confirmed":
            payer = event.payment.payer if event.payment else None
            actions.append(
                {
                    "type": "settlement",
                    "mode": "confirm_only",
                    "payer_user_id": payer.user_id if payer else None,
                    "amount": event.payment.amount if event.payment else None,
                    "currency": event.payment.currency if event.payment else None,
                    "status": "confirmed",
                }
            )

        command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        text = event.message.text or str(command.get("name") or "event")
        actions.append(
            {
                "type": "send_message",
                "chat_id": event.message.chat_id,
                "reply_to_message_id": event.message.message_id,
                "text": f"Event Bus 收到 {event_type}: {text}",
            }
        )
        return actions
