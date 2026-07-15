"""with_interaction 示例模块主类。"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class WithInteractionPlugin(Plugin):
    key = "with_interaction"
    display_name = "交互示例"

    async def on_command(
        self,
        ctx: PluginContext,
        cmd: str,
        args: list[str],
        event: Any,
    ) -> bool:
        if cmd != "with_interaction":
            return False
        if ctx.log:
            await ctx.log(
                "info",
                "with_interaction.command_compat",
                message="原命令兼容入口仍然可被命中；新插件请优先通过 Event Bus + MessageOps 返回动作。",
            )
        return True

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if entry_key != "start_with_interaction":
            return None
        event = event_from_interaction_payload(payload)
        module_config = payload.get("module_config") if isinstance(payload.get("module_config"), dict) else {}
        response_text = str(
            module_config.get("response_text")
            or payload.get("response_text")
            or module_config.get("message")
            or "你好，交互 Bot"
        ).strip() or "你好，交互 Bot"
        incoming_text = event.message.text or "空消息"
        actor_name = event.actor.display_name or event.actor.user_id or "未知"
        actions: list[dict[str, Any]] = []
        if ctx.messages is not None:
            await ctx.messages.send(
                chat_id=event.message.chat_id,
                text=f"{response_text}\n收到：{incoming_text}\n触发人：{actor_name}",
            )
        else:
            actions.append(
                {
                    "type": "send_message",
                    "text": f"{response_text}\n收到：{incoming_text}\n触发人：{actor_name}",
                }
            )
        actions.extend(
            [
                {
                    "type": "result",
                    "success": True,
                    "result": {
                        "status": "ok",
                        "actor_user_id": event.actor.user_id,
                        "entry_key": entry_key,
                    },
                    "settlement": {
                        "mode": "announce_only",
                        "winner_user_id": event.actor.user_id,
                        "winner_name": event.actor.display_name,
                    },
                },
                {"type": "end_session"},
            ]
        )
        return actions
