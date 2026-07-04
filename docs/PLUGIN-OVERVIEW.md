# TelePilot 插件概览

本文是当前维护的插件开发入口，统一说明插件路线、快速开始和基础目录结构。项目对外统一使用“插件”指代可安装、可启停、可配置的扩展能力；“模块化”只描述 TelePilot 的架构特色。

## 插件标准模式

TelePilot 0.x 阶段只保留一个默认插件模式：**个人可信插件标准模式**。

- 管理员安装并启用插件后，即视为信任该插件的业务逻辑；远程插件风险由管理员自行承担。
- 平台不做公共插件市场式强沙箱，但会通过 `Manifest.permissions`、`ctx.client`、`ctx.http`、`ctx.ai`、`ctx.messages` 等 facade 收口常用能力，并保留频控、审计、急停、日志脱敏和 token/session 隔离。
- 插件可以通过两类调度方式接入：管理员带前缀命令、群友关键词/付款开局。入口只描述触发来源和默认通道偏好，不绑死后续回复账号；插件可通过 `ctx.messages` 选择交互 Bot / UserBot 的单通道或候选顺序。涉及收款确认、发奖、补发等钱相关动作仍由 UserBot 或平台受控结算链路处理。群里已有的转账结果通知 Bot 只作为外部付款证据来源，不是插件主动发送通道。

如果未来要开放“任意第三方上传、未经人工审核”的公共市场，需要另行设计 subprocess/容器隔离、资源配额、文件系统/网络沙箱和供应链扫描。它不属于当前 0.x 默认方案；本文当前所有示例、CI 和安全边界都按个人可信插件标准模式编写。

---

## 1. 快速开始

### 文件结构

```
plugins/installed/{插件名}/
├── __init__.py        # 导出 PLUGIN_CLASS 和 MANIFEST
├── manifest.py        # Manifest 元数据
├── plugin.py          # 插件主类
└── (其他插件)
```

### 最小 Event Bus 插件

**plugin.py：**
```python
from typing import Any

from app.worker.plugins.base import Plugin, register
from app.worker.plugins.events import event_from_interaction_payload

@register
class EventPingPlugin(Plugin):
    key = "event_ping"
    display_name = "Event Ping"

    async def on_event(self, ctx, payload: dict[str, Any]) -> list[dict[str, Any]]:
        event = event_from_interaction_payload(payload)
        text = event.message.text or ""
        if "ping" not in text.lower():
            return []
        return [
            {
                "type": "send_message",
                "send_via": ["interaction_bot", "userbot_reply"],
                "chat_id": event.message.chat_id,
                "reply_to_message_id": event.message.message_id,
                "text": "pong",
            }
        ]
```

**manifest.py：**
```python
from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="event_ping",
    display_name="Event Ping",
    version="0.1.0",
    author="example",
    description="演示 Event Bus + MessageOps 的最小插件",
    usage="在允许会话内发送 ping，插件会通过交互 Bot 或 UserBot 回复 pong。",
    permissions=["send_message"],
    event_subscriptions=[
        {
            "source": ["userbot", "interaction_bot"],
            "events": ["message"],
            "scope": "all_allowed_chats",
        }
    ],
    capabilities={},
)
```

**__init__.py：**
```python
from .manifest import MANIFEST
from .plugin import EventPingPlugin

PLUGIN_CLASS = EventPingPlugin
__all__ = ["PLUGIN_CLASS", "MANIFEST"]
```

通过安装接口安装并在账号上启用后，Event Bus 会按 `event_subscriptions` 投递事件。插件只读取标准事件信封，并通过 `ctx.messages` 或标准 action 请求发送、编辑、删除、按钮 ACK、Inline answer 和 settlement。

可直接参考最终版主模板：`examples/plugins/event_bus_demo`。它覆盖 message、command、callback、inline、chosen inline 和 payment fixtures。

`on_command` / `on_message` 仍保留给管理员命令和历史插件迁移，不再作为新 Telegram 交互插件的快速开始路径。

---

## 2. 插件结构（Plugin 包）

### 目录约定

```
backend/app/worker/plugins/
├── base.py              # Plugin 基类 + register 装饰器
├── manifest.py          # Manifest 数据类
├── loader.py            # 插件加载器 + 热重载 + generation guard
├── builtin/             # 平台能力/兼容壳，普通插件不要放这里
│   ├── scheduler/        # 平台调度兼容壳，实际由 PlatformScheduler 执行
│   └── forward/

plugins/installed/       # 远程/本地/插件库安装后的运行目录
├── guess_number/
└── (更多插件...)
```

`backend/app/worker/plugins/builtin/` 只保留平台能力和兼容壳，扫描器只把平台能力纳入 builtin registry。`auto_reply`、`autorepeat`、`chatgpt_image`、`codex_image`、`game24`、`math10` 都不再由 TelePilot 本体分发；Web 推荐入口只从 `OFFICIAL_PLUGIN_REPO_URL` 指向的插件库读取，安装后复制到 `plugins/installed/{key}/`，再按安装型插件加载。

### 生命周期

```
loader._load_all()
  → scan 核心 builtin/ + plugins/installed/
  → import plugin.py + manifest.py
  → 验证 Manifest 合法性
  → 实例化 Plugin 子类
  → 调用 on_startup(ctx)

热重载 (reload_plugin):
  → state.generation += 1          # generation guard
  → 旧插件: on_shutdown(ctx)
  → 重新 import + 实例化
  → 新插件: on_startup(ctx)

事件派发:
  → Source Adapter 生成标准事件信封
  → Event Bus 按 event_subscriptions 匹配插件
  → 检查 ctx.generation == state.generation
  → 跳过过期 handler（竞态保护）
  → 调用 on_event / on_interaction 迁移桥
  → 插件返回标准 action，经 MessageOps / Delivery Executor 执行

兼容 hook:
  → on_command / on_message 仅用于管理员命令、历史插件和高级兼容场景
```

生命周期心智：

- 安装：把插件代码下载到本地插件库，不会自动运行，也不会收到事件。
- 启用：某个账号允许该插件运行，Event Bus 开始按 `event_subscriptions` 投递事件。
- 禁用：该账号停止投递事件，并清理插件注册的任务、会话和运行态。
- 更新：替换本地插件代码；已启用账号会由 worker 热重载，失败会进入日志、Trace 或加载错误。
- 卸载：移除安装记录和本地插件文件；如果插件曾经启用，先按禁用路径清理。

---
