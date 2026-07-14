"""入站 Webhook 完整示例 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

EVENT_SUBSCRIPTIONS = [
    {
        "source": ["webhook"],
        "events": ["webhook"],
        "scope": "all_allowed_chats",
        "filters": {"hook_key": "default"},
    }
]

CONFIG_SCHEMA = {
    "type": "object",
    "x-ui-mode": "single",
    "x-usage-guide": (
        "先填写接收通知的 Telegram Chat ID，再到入站 Webhook 页面复制 default 地址与 Token。"
        "外部系统只需 POST JSON；请求返回 202 后，在日志或 Trace 中确认插件执行结果。"
    ),
    "additionalProperties": False,
    "required": ["target_chat_id"],
    "properties": {
        "target_chat_id": {
            "type": "integer",
            "title": "目标 Chat ID",
            "description": "接收订单通知的私聊、群聊、超级群或频道 ID。",
        },
        "title": {
            "type": "string",
            "title": "通知标题",
            "default": "订单事件",
            "maxLength": 64,
        },
    },
}

MANIFEST = Manifest(
    key="webhook_receiver",
    display_name="Webhook 订单通知示例",
    version="0.1.0",
    author="examples",
    description="接收 default Webhook，并把订单事件发送到配置的 Telegram 会话。",
    usage=(
        "安装并为账号启用后，设置目标 Chat ID；外部系统向入站 Webhook 页提供的 default "
        "地址 POST JSON，插件会通过该账号把事件摘要发送到目标会话。"
    ),
    category="utility",
    permissions=["send_message"],
    event_subscriptions=EVENT_SUBSCRIPTIONS,
    capabilities={},
    config_schema=CONFIG_SCHEMA,
)

__all__ = ["MANIFEST", "EVENT_SUBSCRIPTIONS", "CONFIG_SCHEMA"]
