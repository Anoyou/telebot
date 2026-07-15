"""把 default Webhook 中的订单事件转成 Telegram 通知。"""

from __future__ import annotations

import json
from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register


def _target_chat_id(config: dict[str, Any]) -> int | None:
    raw = config.get("target_chat_id")
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value or None


def _body_preview(body: Any) -> str:
    if isinstance(body, (dict, list)):
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(body or "")
    return text[:1200] or "（空正文）"


@register
class WebhookReceiverPlugin(Plugin):
    key = "webhook_receiver"
    display_name = "Webhook 订单通知示例"

    async def on_event(
        self,
        ctx: PluginContext,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        webhook = payload.get("webhook")
        if not isinstance(webhook, dict):
            return []

        target_chat_id = _target_chat_id(ctx.config)
        if target_chat_id is None:
            if ctx.log is not None:
                await ctx.log(
                    "warn",
                    "Webhook 示例未配置 target_chat_id，事件已跳过。",
                    hook_key=webhook.get("hook_key"),
                )
            return []

        title = str(ctx.config.get("title") or "订单事件").strip()[:64] or "订单事件"
        hook_key = str(webhook.get("hook_key") or "default")
        received_at = str(webhook.get("received_at") or "-")
        body = webhook.get("body")
        text = (
            f"{title}\n"
            f"Hook：{hook_key}\n"
            f"接收时间：{received_at}\n"
            f"内容：{_body_preview(body)}"
        )
        return [
            {
                "type": "send_message",
                "chat_id": target_chat_id,
                "text": text,
                "parse_mode": "plain",
            }
        ]


__all__ = ["WebhookReceiverPlugin"]
