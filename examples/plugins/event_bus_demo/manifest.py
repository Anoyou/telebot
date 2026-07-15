"""Event Bus + Trace + MessageOps 示例 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

EVENT_SUBSCRIPTIONS = [
    {
        "events": ["message", "command"],
        "source": ["userbot", "interaction_bot"],
        "scope": "all_allowed_chats",
    },
    {
        "events": ["callback_query"],
        "source": ["interaction_bot"],
        "scope": "rule_bound",
    },
    {
        "events": ["inline_query", "chosen_inline_result"],
        "source": ["interaction_bot"],
        "scope": "inline_all",
    },
    {
        "events": ["payment_confirmed"],
        "source": ["external_payment_notice", "userbot"],
        "scope": "rule_bound",
    },
]

CAPABILITIES = {
    "telegram_native_raw": {
        "enabled": True,
        "reason": "只在排查 Telegram 原生字段映射差异时读取 native_raw_meta；业务逻辑仍以标准事件信封为主。",
        "sources": ["interaction_bot", "userbot"],
        "store_payload": False,
    },
    "telegram_direct_passthrough": {
        "enabled": False,
        "reason": "示例插件不启用低延时直通；只有抢红包、秒杀等需要跳过标准链路的插件才应声明并让账号二次手动开启。",
        "sources": ["userbot"],
        "directions": ["incoming"],
    }
}

MANIFEST = Manifest(
    key="event_bus_demo",
    display_name="Event Bus 示例",
    version="0.1.0",
    author="examples",
    description="演示最终版 Event Bus、Trace 与 MessageOps 插件契约。",
    usage=(
        "安装后在插件中心启用；Event Bus 会按 event_subscriptions 投递 message、command、"
        "callback_query、inline_query、chosen_inline_result 与 payment_confirmed。插件只读取标准事件信封，"
        "发送、按钮 ACK、inline answer 与 settlement 都返回 MessageOps/action。"
    ),
    category="interactive",
    permissions=["send_message", "read_chat"],
    event_subscriptions=EVENT_SUBSCRIPTIONS,
    capabilities=CAPABILITIES,
    interaction_profile="utility_trigger",
    preserve_command_trigger=True,
)

__all__ = ["MANIFEST", "EVENT_SUBSCRIPTIONS", "CAPABILITIES"]
