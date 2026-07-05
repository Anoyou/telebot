# 5 分钟写出第一个插件

这页只讲最短路径：写一个 `hello_ping` 插件，在已允许会话里收到纯文本 `ping` 后回复 `pong`。完整字段、权限和高级能力请再看 [插件开发指南](./PLUGIN-DEV-GUIDE.md) 与 [API 参考](./PLUGIN-API-REFERENCE.md)。

## 1. 目录结构

最小插件目录只需要四个文件：

```text
hello_ping/
├── __init__.py
├── manifest.py
├── plugin.json
└── plugin.py
```

远程仓库里可以放多个插件目录；TelePilot 安装后会复制到本地插件库。安装只代表代码进入本地，必须回到插件中心按账号启用后才会运行。

## 2. plugin.json

`plugin.json` 是安装和展示阶段读取的静态声明。最小插件也必须写清 `usage`、`event_subscriptions`、`capabilities` 和 `permissions`。

```json
{
  "name": "hello_ping",
  "display_name": "Hello Ping",
  "description": "最小 Event Bus + MessageOps 入门示例。",
  "author": "examples",
  "version": "0.1.0",
  "entry": "plugin.py",
  "min_telepilot_version": "0.41.0",
  "category": "utility",
  "permissions": ["send_message"],
  "interaction_profile": "utility_trigger",
  "usage": "安装并在账号上启用后，在已允许会话发送 ping，插件会回复 pong。",
  "event_subscriptions": [
    {
      "events": ["message"],
      "source": ["userbot", "interaction_bot"],
      "scope": "all_allowed_chats"
    }
  ],
  "capabilities": {}
}
```

关键点：

- `name`、`MANIFEST.key`、插件类 `key` 必须一致。
- `event_subscriptions` 决定插件会收到哪些标准事件信封。
- `permissions` 是给安装者和平台审计看的能力声明。
- 没有高风险能力时，`capabilities` 也要写成 `{}`，不要省略。

## 3. manifest.py

`manifest.py` 是运行阶段读取的真实 Manifest，字段应和 `plugin.json` 保持一致。

```python
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
```

## 4. plugin.py

新 Telegram 插件优先实现 `on_event`。插件读取标准事件信封，然后返回标准 action；发送动作由平台执行并写入 Trace。

```python
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
```

普通回复不要写 `send_via`；平台会按当前 `session.channel` 选择 UserBot 或交互 Bot。你也可以用 `ctx.messages` 生成等价消息操作；两种方式都会走平台 MessageOps。图片题面先 `send_photo(save_message_id_key=...)`，后续用 `edit_caption(message_id_key=...)` 原地更新 caption，不要用 `edit_message` 编辑媒体消息。最小示例直接返回 action，方便复制和测试。

## 5. __init__.py

```python
from .manifest import MANIFEST
from .plugin import HelloPingPlugin

PLUGIN_CLASS = HelloPingPlugin

__all__ = ["MANIFEST", "PLUGIN_CLASS"]
```

## 6. 安装、启用、验证

1. 把插件目录放进远程插件仓库，或先放到本地示例目录验证。
2. 在 Web 面板的“插件中心 → 安装插件”里添加仓库并安装。
3. 安装后插件不会自动运行；回到插件中心，选择账号，启用 `Hello Ping`。
4. 在该账号已允许会话里发送 `ping`。
5. 正常结果是一条 `pong` 回复；排障时去“日志中心 → 消息链路”查 Trace。

本仓库已提供完整可运行示例：[examples/plugins/hello_ping](../examples/plugins/hello_ping)。维护示例时运行：

```bash
backend/.venv/bin/python scripts/validate-plugin-examples.py
```

## 7. 下一步

- 想看 message、command、callback、inline、payment 的完整写法：读 [event_bus_demo](../examples/plugins/event_bus_demo)。
- 想调用外部 HTTP：读 [PLUGIN-HTTP.md](./PLUGIN-HTTP.md) 和 `examples/plugins/with_http`。
- 想调用平台 LLM：读 [PLUGIN-AI.md](./PLUGIN-AI.md) 和 `examples/plugins/with_ai`。
- 写任何真实插件前，先读 [插件开发铁律](./PLUGIN-RULES.md)。
