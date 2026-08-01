# 5 分钟写出第一个插件

这页先讲最快路径：用简单模式 SDK 写一个 `hello_ping` 插件，账号主人或授权管理员发送命令 `ping` 后回复 `pong`。需要公开群互动、Event Bus、Trace、按钮、会话、付款或完整前端展示时，再使用后面的显式 Manifest 模式。完整字段、权限和高级能力请再看 [插件开发指南](./PLUGIN-DEV-GUIDE.md) 与 [API 参考](./PLUGIN-API-REFERENCE.md)。

## 0. 最快路径：简单模式 SDK

只写一个 `__init__.py` 就能被 loader 加载。目录名就是插件 key；下面例子目录名必须是 `hello_ping`。

```text
hello_ping/
└── __init__.py
```

```python
from telepilot import plugin


@plugin.command("ping")
async def ping(ctx):
    await ctx.reply("pong")
```

加载逻辑：

- `from telepilot import plugin` 暴露的是 SDK 装饰器命名空间。
- `@plugin.command("ping")` 会登记一个简单模式命令函数。
- loader 导入插件目录后，如果模块没有显式 `PLUGIN_CLASS` 和 `MANIFEST`，会尝试从简单模式装饰器合成隐式插件类和 `Manifest`。
- 隐式 Manifest 当前会自动声明 `permissions=["read_event", "send_message"]`，插件类会带上 `commands["ping"]`，运行期再注册进命令分发表。

触发方式：

1. 把 `hello_ping/` 放到 loader 能扫描的插件目录，并在插件中心给目标账号启用。
2. 用该账号的命令前缀触发，例如默认前缀是逗号时发送 `,ping`。
3. 正常结果是当前命令消息收到 `pong` 回复。

### 命令前缀必须读取系统实时值

`command` 配置只保存裸指令名。插件在运行时展示帮助、示例、启动日志或错误用法时，必须使用平台 API 读取“系统设置 → 指令前缀”，不要读取 `ctx.account_config`，也不要硬编码逗号：

```python
from app.worker.command import current_command_prefix

prefix = current_command_prefix(fallback=",")
usage = f"用法：{prefix}{command} 100"
```

配置 schema、`usage` 和消息模板中的静态说明使用 `{prefix}`，由插件中心按系统设置渲染：

```python
usage = "发送 {prefix}guess 100 开始游戏。"
```

`ctx.account_config` 是当前插件的账号级原始配置，不包含系统 `command_prefix`。把它当成系统设置读取会长期回退成 `,`，这是插件帮助文案前缀错误的常见根因。

简单模式和显式 Manifest 模式可以共存：同一个系统里可以同时加载只有 `@plugin.command` 的简单插件，也可以加载带 `PLUGIN_CLASS` / `MANIFEST` 的完整插件。简单模式适合快速玩法、账号命令、小工具和内部自动化；显式 Manifest 适合需要 `plugin.json` 展示字段、`event_subscriptions`、配置 schema、HTTP/AI 权限、按钮回调、Inline、付款、会话状态或完整 Trace 的插件。

> 当前 `tp_plugin new` 只提供 `session_game` / `command` / `passthrough` 三种 profile，代码里没有 `--profile simple`。所以简单模式先按上面的单文件方式手写；不要在文档或脚本里使用不存在的 `tp_plugin new --profile simple`。

## 1. 显式 Manifest 兼容桥脚手架（tp_plugin）

`tp_plugin new` 可以生成一套已经能跑、且能通过校验的四文件骨架。当前 `session_game` 和
`command` profile 仍保留 `interaction_entries + on_interaction` 兼容桥，适合迁移旧玩法或先生成包结构；
新标准插件应继续阅读第 2 至 5 节，改成 `event_subscriptions + on_event`，也可以直接复制
`examples/plugins/hello_ping` 或 `examples/plugins/event_bus_demo`。三条命令：

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

三种 profile 当前演示的入口：

| profile | 场景 | 骨架演示的入口 |
| --- | --- | --- |
| `session_game` | 旧群局抢答/对战迁移，有平台会话 | `on_interaction` 兼容桥：开局 `start_session → send_message → update_session`，命中 `payout + result + end_session` |
| `command` | 旧一次性命令动作迁移，不建会话 | `on_command`（UserBot 原命令兼容）+ `on_interaction` 兼容桥处理 `command` 事件 |
| `passthrough` | 抢红包/秒杀等低延时直通 | `capabilities.telegram_direct_passthrough` + `on_direct_message` |

一个完整流程（以 `session_game` 为例）：

1. `tp_plugin new my_game --profile session_game`，默认落到 `plugins/local_imports/my_game`。
2. 打开 `plugin.py` 把口令抢答改成你的玩法，并把兼容入口迁成 `on_event`；在 `plugin.json` 与 `manifest.py` 同步声明 `event_subscriptions`。
3. `tp_plugin check plugins/local_imports/my_game` 看有没有报错或 `unknown_events` 警告。
4. `tp_plugin register plugins/local_imports/my_game` 登记；重复登记会给出「已登记，无需重复」的友好提示。
5. 回到 Web「插件中心」，选择账号启用 `my_game`；在交互 Bot 上用命令或群内关键词开局。
6. 排障去「日志中心 → 消息链路」看 Trace。

> 关于 `session_game` 开局动作序列：骨架把 `start_session` 放在 `send_message`、`update_session` 之前。这是当前所有通道（命令/关键词/付款）都安全的写法——平台会先单独处理 `start_session` 建会话，随后的 `update_session` 才不会因会话不存在而悬空。骨架注释里也写了这一点。

骨架自带一份 `test_plugin.py` pytest 样板（直调兼容 `on_interaction` / `on_direct_message` 断言动作序列），可作为迁移测试起点。迁到 `on_event` 后应同步修改测试入口。下面第 2 至 7 节讲的是标准四文件写法。

## 2. 显式 Manifest 目录结构

显式 Manifest 插件的最小目录需要四个运行期文件：

```text
hello_ping/
├── __init__.py
├── manifest.py
├── plugin.json
└── plugin.py
```

远程仓库里可以放多个插件目录；TelePilot 安装后会复制到本地插件库。安装只代表代码进入本地，必须回到插件中心按账号启用后才会运行。

## 3. plugin.json

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
  "usage": "安装并在账号上启用后，在已允许会话发送 ping，插件会回复 pong。账号允许会话列表留空时表示全部会话。",
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

## 4. manifest.py

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
    usage="安装并在账号上启用后，在已允许会话发送 ping，插件会回复 pong。账号允许会话列表留空时表示全部会话。",
    category="utility",
    permissions=["send_message"],
    event_subscriptions=EVENT_SUBSCRIPTIONS,
    capabilities={},
    interaction_profile="utility_trigger",
)
```

## 5. plugin.py

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

## 6. __init__.py

```python
from .manifest import MANIFEST
from .plugin import HelloPingPlugin

PLUGIN_CLASS = HelloPingPlugin

__all__ = ["MANIFEST", "PLUGIN_CLASS"]
```

## 7. 安装、启用、验证

1. 把插件目录放进远程插件仓库，或先放到本地示例目录验证。
2. 在 Web 面板的“插件中心 → 安装插件”里添加仓库并安装。
3. 安装后插件不会自动运行；回到插件中心，选择账号，启用 `Hello Ping`。
4. 在该账号已允许会话里发送 `ping`。账号级“允许会话”列表留空时表示全部会话；列表非空时才只允许名单内会话。
5. 正常结果是一条 `pong` 回复；排障时去“日志中心 → 消息链路”查 Trace。

本仓库已提供完整可运行的显式 Manifest 示例：[examples/plugins/hello_ping](../examples/plugins/hello_ping)。维护示例时运行：

```bash
backend/.venv/bin/python scripts/validate-plugin-examples.py
```

## 8. 下一步

- 想看 message、command、callback、inline、payment 的完整写法：读 [event_bus_demo](../examples/plugins/event_bus_demo)。
- 想处理按钮：先读 [Inline 按钮的两种完全不同场景](./PLUGIN-API-REFERENCE.md#inline-按钮的两种完全不同场景)，不要把 Interaction Bot 的 callback ACK 和 UserBot 主动点击第三方 Bot 按钮混为一谈。
- 想调用外部 HTTP：读 [PLUGIN-HTTP.md](./PLUGIN-HTTP.md) 和 `examples/plugins/with_http`。
- 想调用平台 LLM：读 [PLUGIN-AI.md](./PLUGIN-AI.md) 和 `examples/plugins/with_ai`。
- 写任何真实插件前，先读 [插件开发铁律](./PLUGIN-RULES.md)。
