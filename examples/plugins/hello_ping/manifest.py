"""hello_ping 最小 Event Bus 示例 manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest

EVENT_SUBSCRIPTIONS = [
    {
        "events": ["message"],
        "source": ["userbot", "interaction_bot"],
        "scope": "all_allowed_chats",
    }
]

MANIFEST = Manifest(
    key="hello_ping",
    display_name="Hello Ping",
    version="0.1.0",
    author="examples",
    description="最小 Event Bus + MessageOps 入门示例。",
    usage="安装并在账号上启用后，在已允许会话发送 ping，插件会回复 pong。",
    category="utility",
    permissions=["send_message"],
    event_subscriptions=EVENT_SUBSCRIPTIONS,
    capabilities={},
    interaction_profile="utility_trigger",
)

__all__ = ["MANIFEST", "EVENT_SUBSCRIPTIONS"]
