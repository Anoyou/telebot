"""with_interaction 示例模块 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="with_interaction",
    display_name="交互示例",
    version="1.0.0",
    author="TelePilot",
    description="最小交互 Bot 兼容示例",
    usage="兼容桥示例：用于把历史 interaction_entries / on_interaction 插件迁移到标准事件信封。新插件优先参考 event_bus_demo。",
    category="interactive",
    permissions=["send_message", "read_chat"],
    event_subscriptions=[
        {
            "events": ["message", "callback_query", "payment_confirmed", "session_close"],
            "source": ["interaction_bot", "external_payment_notice"],
            "scope": "rule_bound",
        }
    ],
    capabilities={
        "telegram_native_raw": {
            "enabled": False,
            "reason": "兼容示例只读取标准事件信封，不需要原生 Telegram 对象。",
        }
    },
    interaction_profile="utility_trigger",
    interaction_entries=[
        {
            "key": "start_with_interaction",
            "title": "开始示例",
            "description": "演示交互 Bot 入口与原命令双兼容。",
            "interaction_profile": "utility_trigger",
            "launch_mode": "hybrid",
            "session_scope": "chat",
            "events": ["keyword", "message", "session_close"],
            "preserve_command_trigger": True,
            "command_fallback": {
                "enabled": True,
                "command": "with_interaction",
                "mode": "hint_only",
            },
            "payload_contract": {
                "required_envelope": ["source", "actor", "trigger", "session"],
                "required_event_fields": ["type", "chat_id"],
            },
            "result_contract": {
                "actions": ["send_message", "result", "end_session"],
            },
            "settlement": {
                "mode": "announce_only",
                "winner_field": "actor.user_id",
            },
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "message": {
                        "type": "string",
                        "title": "示例文案",
                        "deprecated": True,
                        "description": "历史字段名会与标准事件信封 payload.message 冲突，请改用 response_text。",
                        "default": "你好，交互 Bot",
                    },
                    "response_text": {
                        "type": "string",
                        "title": "示例文案",
                        "default": "你好，交互 Bot",
                    },
                },
            },
        }
    ],
    preserve_command_trigger=True,
)
