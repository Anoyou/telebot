# 5 分钟写出第一个插件

这页只讲最短路径：写一个 `hello_ping` 插件，在已允许会话里收到纯文本 `ping` 后回复 `pong`。完整字段、权限和高级能力请再看 [插件开发指南](./PLUGIN-DEV-GUIDE.md) 与 [API 参考](./PLUGIN-API-REFERENCE.md)。

## 0. 脚手架 5 分钟上手（tp_plugin）

不想手写四个文件，可以用脚手架一键生成一个「已经能跑、且能通过校验」的骨架，再改成自己的玩法。三条命令：

```bash
# 1) 生成骨架（profile 可选 session_game / command / passthrough）
backend/.venv/bin/python backend/scripts/tp_plugin.py new my_game --profile session_game
# 2) 本地校验：四文件齐全 + plugin.json 结构 + 事件白名单
backend/.venv/bin/python backend/scripts/tp_plugin.py check plugins/local_imports/my_game
# 3) 登记进本地台账，让 loader 能加载（手拷目录默认会被当孤儿拒载）
backend/.venv/bin/python backend/scripts/tp_plugin.py register plugins/local_imports/my_game
```

也可以用 Makefile 简写（从仓库根目录）：

```bash
make plugin-new name=my_game profile=session_game     # 生成骨架
make plugin-new name=my_game dry_run=1                # 只看会生成哪些文件，不落盘
make plugin-check dir=plugins/local_imports/my_game    # 校验
make plugin-register dir=plugins/local_imports/my_game # 登记
```

三种 profile 各自演示一类主路径写法：

| profile | 场景 | 骨架演示的入口 |
| --- | --- | --- |
| `session_game` | 群局抢答/对战，有平台会话 | `on_interaction`：开局 `start_session → send_message → update_session`，命中 `payout + result + end_session` |
| `command` | 一次性命令动作，不建会话 | `on_command`（UserBot 原命令兼容）+ `on_interaction` 处理 `command` 事件 |
| `passthrough` | 抢红包/秒杀等低延时直通 | `capabilities.telegram_direct_passthrough` + `on_direct_message` |

一个完整流程（以 `session_game` 为例）：

1. `tp_plugin new my_game --profile session_game`，默认落到 `plugins/local_imports/my_game`。
2. 打开 `plugin.py` 把口令抢答改成你的玩法；`plugin.json` 与 `manifest.py` 的字段要一起改并保持一致。
3. `tp_plugin check plugins/local_imports/my_game` 看有没有报错或 `unknown_events` 警告。
4. `tp_plugin register plugins/local_imports/my_game` 登记；重复登记会给出「已登记，无需重复」的友好提示。
5. 回到 Web「插件中心」，选择账号启用 `my_game`；在交互 Bot 上用命令或群内关键词开局。
6. 排障去「日志中心 → 消息链路」看 Trace。

> 关于 `session_game` 开局动作序列：骨架把 `start_session` 放在 `send_message`、`update_session` 之前。这是当前所有通道（命令/关键词/付款）都安全的写法——平台会先单独处理 `start_session` 建会话，随后的 `update_session` 才不会因会话不存在而悬空。骨架注释里也写了这一点。

骨架自带一份 `test_plugin.py` pytest 样板（直调 `on_interaction` / `on_direct_message` 断言动作序列），可作为你玩法回归测试的起点。下面第 1–6 节讲的是同样四个文件的手写细节，想理解内部结构再往下读。

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
