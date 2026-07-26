# TelePilot 远程插件

远程插件的第一抽象是三种运行模式，而不是把 Event Bus 当成一种独立模式：

| 模式 | 触发与输入 | 默认收发通道 | 适用边界 |
| --- | --- | --- | --- |
| 裸直通 | 只接收 userbot 原始 Telethon event | 插件自行处理 userbot 能力 | 低延时、愿意跳过标准事件信封和平台动作审计的少数场景；不覆盖 interaction bot |
| userbot 命令会话 | UserBot 前缀命令触发，进入标准事件信封 | 后续收发默认都走 userbot | 管理员命令、账号身份动作、需要沿用当前账号上下文的流程 |
| interaction bot 规则会话 | 关键词、付款确认、按钮回调触发，进入标准事件信封 | 后续收发默认都走 interaction bot | 高频群内互动、按钮、题面、普通会话提示 |

能力固定路由有两个例外：`payout`、收付款、发奖永远由 userbot 执行；`send_rich_message` 默认由 interaction bot 执行，只有插件显式指定 `userbot_reply` 才使用 Layer 228 Userbot 能力。两者都不会为了迁就当前会话通道而静默降级。

Event Bus、Trace、MessageOps 是标准链路的内部契约：Event Bus 负责把标准事件投递给插件，Trace 负责记录匹配、执行和失败原因，MessageOps/action 负责把插件输出交给平台路由和审计。它们服务于 userbot 命令会话和 interaction bot 规则会话，不是第四种运行模式。新插件不再以 `interaction_entries`、旧交互规则、旧平铺 payload 或 `notice` 通道作为主路径；这些内容只用于迁移旧插件。

## 适用场景

- 把第三方插件以 zip、Git 仓库或 Registry 条目分发给 TelePilot。
- 插件需要接收 Telegram 标准事件：消息、管理员命令、按钮回调、Inline、付款确认。
- 插件需要通过 TelePilot 代发消息、ACK 按钮、回答 Inline Query 或记录结算动作。
- 插件需要通过 Interaction Bot 发送标题、任务列表、折叠详情、表格等 Telegram 原生 Rich Message。
- 插件需要声明 HTTP、AI、原生 Telegram raw 等风险能力，供安装前提示和 Trace 排障。
- 插件确有低延时 userbot 直通需求，并愿意承担无标准 action/Trace 的审计缺口。

远程插件仍按个人可信插件模式运行：安装者自行信任插件业务逻辑。标准会话里，平台负责能力声明、事件信封、MessageOps 执行、Trace、审计、限流和客观失败提示；裸直通里，平台只保留账号启用、插件授权、二次开关和急停边界，不承诺标准事件信封或 action 审计。

## 目录结构

```text
my_plugin/
├── __init__.py
├── manifest.py
├── plugin.py
└── plugin.json
```

`plugin.json` 是静态安装元数据，不执行 Python；安装后运行时仍会读取 `manifest.py` 的 `MANIFEST`。两边的 `name/key`、`version`、`category`、`event_subscriptions`、`capabilities` 必须保持一致。

标准会话链路里的互动入口，还要额外关注 4 个字段：

- `triggers.command`：把某个入口声明成可由 UserBot 前缀命令开局。
- `default_trigger_modes`：给平台注入的 `interaction_trigger_modes` 提供默认值，常见值是 `all` / `keyword_only`。
- `callback_fast_ack`：按钮入口是否在分类完成后立即空 ACK。
- `include_outgoing`：userbot 会话是否继续吃本账号自己发出的消息。

这些字段属于当前运行时契约。插件声明与当前代码版本不兼容时，安装或校验阶段应直接报错；不要为旧分支猜测降级行为。

## plugin.json 最小模板

```json
{
  "name": "event_bus_demo",
  "display_name": "Event Bus 示例",
  "description": "演示标准链路事件订阅、Trace 与 MessageOps。",
  "author": "examples",
  "version": "0.1.0",
  "entry": "plugin.py",
  "min_telepilot_version": "0.33.0",
  "category": "interactive",
  "permissions": ["send_message", "read_chat"],
  "triggers": {
    "command": "demo"
  },
  "default_trigger_modes": "all",
  "callback_fast_ack": false,
  "usage": "启用后按标准链路订阅接收 message/command/callback/inline/payment 事件，所有输出都返回标准 action。",
  "event_subscriptions": [
    {
      "events": ["message", "command"],
      "source": ["userbot", "interaction_bot"],
      "scope": "all_allowed_chats"
    },
    {
      "events": ["callback_query"],
      "source": ["interaction_bot"],
      "scope": "rule_bound"
    },
    {
      "events": ["inline_query", "chosen_inline_result"],
      "source": ["interaction_bot"],
      "scope": "inline_all"
    },
    {
      "events": ["payment_confirmed"],
      "source": ["external_payment_notice", "userbot"],
      "scope": "rule_bound"
    }
  ],
  "capabilities": {
    "telegram_native_raw": {
      "enabled": true,
      "reason": "仅用于排查 Telegram 原生字段映射差异，业务逻辑仍读取标准事件信封。",
      "sources": ["interaction_bot", "userbot"],
      "store_payload": false
    }
  }
}
```

字段要点：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` / `display_name` / `version` | 是 | `name` 必须等于 `MANIFEST.key` 和插件类 `key` |
| `usage` | 是 | 插件中心展示的使用说明；缺失会触发高级规范警告 |
| `event_subscriptions` | 事件插件必填 | Event Bus 投递声明；纯 HTTP/AI 工具可写 `[]` |
| `capabilities` | 是 | 高风险能力声明；没有高风险能力也建议写 `{}` |
| `permissions` | 按需 | 安装提示和 facade 注入依据，如 `external_http`、`ai_text`、`send_message` |
| `triggers.command` | 互动入口按需 | 声明 UserBot 命令开局名，不带前缀 |
| `default_trigger_modes` | 互动入口按需 | `all` / `keyword_only`，决定命令与关键词是否同时开放 |
| `callback_fast_ack` | callback 入口按需 | 点击按钮后先立即 ACK，再慢慢处理插件逻辑 |
| `include_outgoing` | 互动入口按需 | userbot 会话内是否继续投递本账号自己发出的消息 |
| `allowed_hosts` | HTTP 插件必填 | `ctx.http` 允许访问的域名 |
| `config_schema` | 按需 | 账号级配置；有配置时也要提供 `usage` 或 `x-usage-guide` |

`usage` 缺失不是普通文案缺口，而是规范警告：插件中心无法告诉安装者“谁能触发、监听什么事件、会发什么消息、如何排障”。远程插件、插件库维护插件和示例插件都必须写 `usage`；有配置页时还应在 `config_schema` 顶层补 `x-usage-guide`、`x-usage-instructions` 或 `x-usage-steps`，但这些只能增强说明，不能替代 `plugin.json.usage`。

## 插件生态迁移边界

当前框架按身份处理插件，不再把系统能力和可安装插件混成一类：

| 类型 | 边界 | 当前处理 |
| --- | --- | --- |
| 平台功能 | 系统运行必需或明显不是插件的能力，例如日志、账号管理、插件仓库管理、调度框架 | 不伪装成普通插件；在系统设置或平台页面展示 |
| 插件库推荐插件 | 插件库维护但不是系统必需，例如自动回复、游戏、互动玩法 | 可提示安装，可手动移除；必须完整声明 `usage`、`event_subscriptions`、`capabilities` |
| 插件库普通插件 | 插件库分发、按需安装/更新的能力，例如图片生成或玩法插件 | 从远程插件库安装/一键更新；刷新时必须保留新字段和风险提示 |
| 示例插件 | 用于开发者学习和验证，例如 `examples/plugins/event_bus_demo` | 不默认启用；必须能通过 `scripts/validate-plugin-examples.py` |
| 用户安装插件 | 用户从私有库或第三方库安装的插件 | 不强制自动迁移代码；安装、启用、更新时显示规范警告、风险提示和废弃通道错误 |

插件库维护插件不允许为了通过 lint 写空声明。至少要写清：谁能触发、订阅哪些事件、使用哪些能力、触发方式如何决定会话通道、付款/发奖是否使用 `payout`、如何在日志页按 trace 或 action 排查。

## event_subscriptions

`event_subscriptions` 描述“插件想从 Event Bus 接收什么”，不是旧规则系统的替代写法。

| 字段 | 常用值 | 说明 |
| --- | --- | --- |
| `events` | `message`、`command`、`callback_query`、`inline_query`、`chosen_inline_result`、`payment_confirmed`、`session_close`、`message_edited`、`session_expired`、`all_events` | 订阅事件类型 |
| `source` | `userbot`、`interaction_bot`、`external_payment_notice` | 事件来源 |
| `scope` | `all_allowed_chats`、`owner_only`、`known_users`、`rule_bound`、`inline_all` | 投递范围 |

`all_messages` 目前仍只表示 `message` / `command`；需要覆盖平台已登记的常见事件时，用 `all_events`。Inline 插件必须声明 `inline_all`；付款插件必须能处理 `payment_confirmed`，不要把外部转账通知文本当业务主路径。

`known_users` 只认平台 state 提供的真实集合，不会自动包含当前 sender。

`filters` 已支持常用键校验，未知 filter key 会保留并告 warning。`rule_bound` 如果带 `filters.rule_id`，必须与 `trigger.rule_id` 完全一致。

互动入口如果既声明了 `triggers.command`，又是强按钮玩法，建议让配置页暴露 `interaction_trigger_modes`，并把默认值设成 `keyword_only`。这样 userbot 命令开局会被关闭，避免按钮在 userbot 会话里只能降级成文本编号时破坏体验。

## capabilities.telegram_native_raw

默认情况下，插件只拿标准事件信封，不拿 live Telegram 对象。需要原生字段时声明：

```json
{
  "capabilities": {
    "telegram_native_raw": {
      "enabled": true,
      "reason": "排查 Bot API 与 Telethon 字段差异",
      "sources": ["interaction_bot"],
      "store_payload": false
    }
  }
}
```

插件必须先读取 `native_raw_meta`：

```python
native_raw_meta = payload.get("native_raw_meta") or {}
if not native_raw_meta.get("enabled"):
    # 降级到标准信封；不要因为拿不到原生对象而中断主流程。
    pass
```

不要使用旧 `raw_event` 字段。它代表旧运行时泄露原生对象的风险，只能出现在迁移说明或回归测试里。

## 模式 1：裸直通（userbot only）

`telegram_direct_passthrough` 对应裸直通模式，是更高风险的低延时能力。它只给 userbot 使用，插件收到的是 live Telethon event，不是标准事件信封；它不覆盖 interaction bot、Bot API callback、Inline、付款确认或规则会话。

裸直通只适合抢红包、秒杀、抢答首响等对毫秒级延迟敏感，且愿意跳过 TelePilot 标准 Event Bus / Trace / MessageOps 链路的插件。普通互动、付款确认、按钮、Inline 和需要审计回放的业务不要使用它。

插件必须同时满足两层开关才会收到直通消息：

1. `plugin.json` 与 `MANIFEST.capabilities` 显式声明 `telegram_direct_passthrough.enabled=true`，并写清 `reason`、`sources`、`directions`；`sources` 只能写 `userbot`。
2. 安装后在对应账号的插件配置里二次手动开启：

```json
{
  "direct_passthrough": {
    "enabled": true,
    "priority": 0
  }
}
```

- `enabled`（账号二次开关）：只决定插件是否加入直通调度，不决定是否消费消息。
- `priority`：数值越小越优先；账号配置页可打开「调整优先级」对已开启直通的插件排序（自上而下 = 高优先）。

仅声明能力不会启用直通；账号只启用插件本身也不会启用直通。运行时仍保留账号启用、installed 插件授权和 worker 暂停急停；通过这些外层检查后，worker 会在 userbot 标准链路、incoming 白名单、Trace、Event Bus 订阅匹配、legacy `on_message` 包装之前，按优先级顺序调用：

```python
async def on_direct_message(self, ctx, event) -> dict[str, str]:
    if not match(event):
        return {"status": "ignored"}  # 不属于本插件，继续后续链路
    try:
        await handle(event)
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}  # 失败也会回退
    return {"status": "consumed"}  # 已处理，停止后续链路
```

`event` 是 live Telethon event，不是标准事件信封。

**二次开关只启用直通，三态结果决定后续链路**：

| 返回 / 结果 | 后续行为 |
| --- | --- |
| `{"status": "consumed"}` 或兼容返回 `True` / `{"consume": true}` | 停止更低优先级直通并截断普通链路 |
| `{"status": "ignored"}`、`False`、`None`、不返回或 `{"consume": false}` | 继续其它直通，最终进入普通链路 |
| `{"status": "failed", "error": "..."}` 或抛异常 | 记录失败，继续其它直通，最终回退普通链路 |
| 二次开关关闭 | 不进入直通调度 |

平台不提供“独占消费”或“失败仍截断”开关。插件只有在确认已经承担并完成本条消息处理后才能返回 `consumed`；失败永远回退，避免消息丢失。

直通 hook 的发送、编辑、点击等行为不会自动生成标准 MessageOps 审计等价物；插件作者必须自行承担幂等、异常、限流和审计缺失的风险。

## 标准事件信封

插件入口收到的是标准事件信封：

```json
{
  "source": {"type": "message", "channel": "interaction_bot", "account_id": 1, "message_id": 41},
  "chat": {"id": -100100200300, "type": "supergroup"},
  "message": {"chat_id": -100100200300, "message_id": 41, "text": "hello"},
  "actor": {"user_id": 501, "display_name": "玩家 A"},
  "sender": {"user_id": 501, "display_name": "玩家 A"},
  "trigger": {"mode": "public_keyword"},
  "session": {"key": "chat:-100100200300:event_bus_demo", "scope": "chat", "active": true},
  "native_raw_meta": {"enabled": false, "reason": "not_requested"}
}
```

新插件读取文本用 `payload["message"]["text"]`，读取群用 `payload["chat"]["id"]` 或 `payload["message"]["chat_id"]`。不要把 `payload["text"]`、`payload["chat_id"]`、`payload.get("message")` 当主路径；顶层平铺字段仅为旧插件迁移期兼容。

## MessageOps / action 输出

插件不直接调用 Bot API、Telethon driver 或 Bot token。所有输出走 `ctx.messages` 或标准 action：

```python
return [
    {
        "type": "send_message",
        "chat_id": payload["message"]["chat_id"],
        "reply_to_message_id": payload["message"]["message_id"],
        "text": "已收到"
    }
]
```

按钮回调：

```python
return [
    {
        "type": "answer_callback",
        "callback_query_id": payload["source"]["callback_query_id"],
        "text": "按钮已收到",
        "show_alert": False
    }
]
```

Inline Query：

```python
return [
    {
        "type": "answer_inline_query",
        "inline_query_id": payload["inline_query"]["id"],
        "results": [
            {
                "type": "article",
                "id": "demo",
                "title": "示例结果",
                "input_message_content": {"message_text": "Inline 示例"}
            }
        ],
        "cache_time": 0,
        "is_personal": True
    }
]
```

付款确认与结算：

```python
return [
    {
        "type": "settlement",
        "mode": "confirm_only",
        "payer_user_id": payload["payment"]["payer"]["user_id"],
        "amount": payload["payment"]["amount"],
        "currency": payload["payment"]["currency"],
        "status": "confirmed"
    },
    {
        "type": "send_message",
        "chat_id": payload["message"]["chat_id"],
        "text": "到账已确认，等待平台结算。"
    }
]
```

普通发送类动作通常不写 `send_via`，平台会按当前 `session.channel` 路由；只有跨通道公告、管理提示或迁移桥兼容才显式使用 `interaction_bot`、`userbot_reply` 或 `auto`。`notice` / `bbot_notice` / `notice_bot` 已移除，插件请求这些通道应得到明确迁移错误。

默认 `send_message` / `send_photo` / `send_file` / `edit_caption` 等动作按 `parse_mode="plain"` 发送；只有显式声明 `parse_mode="html"` 时才启用 HTML。图片或文件题面需要原地更新时，在发送媒体动作里保存 `save_message_id_key`，后续返回 `edit_caption` 并携带 `message_id_key`；`edit_message` 只用于纯文本消息。HTML 内容要先转义，再构造标签。

在统一会话通道模型下，命令触发的会话收发全走 userbot，关键词/付款/按钮触发的会话收发全走交互 Bot；插件默认不需要感知通道。`payout` 不受会话通道影响，始终经 userbot 执行。

免费参与、按钮加入和互动游戏不要为了发奖要求玩家额外发言。插件仍然可以按自身玩法保存完整业务状态；仅从后续发奖锚点角度，在按钮回调或参与事件里保留玩家 `tgid`，结算时返回 `payout` 并携带 `reply_to_user_id` / `reply_to_search_limit` 即可。平台会用 userbot 在当前群搜索该用户近期发言作为 `+金额` 的回复锚点，插件不需要自己遍历 Telegram 消息。若找不到近期发言，动作会失败并写入日志，平台默认提示 `未找到对应用户（用户 ID）的近期消息。`；需要更贴近玩法语气时，在动作里写 `reply_anchor_missing_text`，文案可使用 `{user_id}` 占位符。

userbot 会话里的 `reply_markup` 会被平台降级成文本编号面板，而不是静默丢弃。玩家回复序号或按钮文案后，平台会合成 `callback_query` 回投插件；合成事件会在 `source.synthetic="text_button"` 标记，并跳过真正的 `answer_callback` Bot API 调用。

## manifest.py

```python
from app.worker.plugins.manifest import Manifest

EVENT_SUBSCRIPTIONS = [
    {"events": ["message", "command"], "source": ["userbot", "interaction_bot"], "scope": "all_allowed_chats"},
    {"events": ["callback_query"], "source": ["interaction_bot"], "scope": "rule_bound"},
    {"events": ["inline_query", "chosen_inline_result"], "source": ["interaction_bot"], "scope": "inline_all"},
    {"events": ["payment_confirmed"], "source": ["external_payment_notice", "userbot"], "scope": "rule_bound"},
]

CAPABILITIES = {
    "telegram_native_raw": {
        "enabled": True,
        "reason": "仅用于排查 Telegram 原生字段映射差异。",
        "sources": ["interaction_bot", "userbot"],
        "store_payload": False,
    }
}

MANIFEST = Manifest(
    key="event_bus_demo",
    display_name="Event Bus 示例",
    version="0.1.0",
    category="interactive",
    permissions=["send_message", "read_chat"],
    event_subscriptions=EVENT_SUBSCRIPTIONS,
    capabilities=CAPABILITIES,
)
```

当前 `usage` 由远程仓库读取 `plugin.json`；`Manifest` 侧仍以 `event_subscriptions` 和 `capabilities` 作为运行时声明。

## plugin.py

```python
from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class EventBusDemoPlugin(Plugin):
    key = "event_bus_demo"
    display_name = "Event Bus 示例"

    async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
        event = event_from_interaction_payload(payload)
        if event.type == "inline_query":
            return [{
                "type": "answer_inline_query",
                "inline_query_id": payload["inline_query"]["id"],
                "results": [],
                "cache_time": 0,
                "is_personal": True,
            }]
        return [{
            "type": "send_message",
            "chat_id": event.message.chat_id,
            "text": f"收到 {event.type}: {event.message.text}",
        }]
```

发送 Bot API 原生 Rich Message 时继续复用 `send_message` 权限，通过 MessageOps 构造动作：

```python
async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
    event = event_from_interaction_payload(payload)
    await ctx.messages.send_rich(
        chat_id=event.message.chat_id,
        html=(
            "<h1>任务进度</h1>"
            "<ul>"
            '<li><input type="checkbox" checked>已接收</li>'
            '<li><input type="checkbox">待处理</li>'
            "</ul>"
            "<details><summary>调试信息</summary><p>trace 已写入</p></details>"
            "<table bordered><tr><th>项目</th><th>状态</th></tr>"
            "<tr><td>Worker</td><td>正常</td></tr></table>"
        ),
    )
    return []
```

`send_rich()` 的 `html`、`markdown`、`blocks` 三选一。省略 `channel` 时仍生成 `send_via="interaction_bot"`。显式指定 `channel="userbot_reply"` 时使用 Telethon Layer 228，支持 HTML、Markdown 和可无损转换的纯文本 blocks，要求主号具备 Telegram Premium 且 `rich_message_posting` 可用；复杂/媒体 blocks、media 和按钮不会降级成普通文本。详见 [API 参考的 Rich Message 说明](./PLUGIN-API-REFERENCE.md#原生-rich-message)。

示例代码见 `examples/plugins/event_bus_demo`，fixtures 覆盖 message、command、callback、inline、chosen inline 和 payment。

## 旧 interaction_entries 迁移

旧 `interaction_entries` 只表示历史交互规则入口。迁移时按下面映射：

| 旧字段 | 新口径 |
| --- | --- |
| `interaction_entries[].events` | `event_subscriptions[].events` |
| `interaction_entries[].session_scope` | 标准信封 `session.scope` |
| `payload_contract` | 标准事件信封字段要求 |
| `result_contract.actions` | 标准 MessageOps/action |
| `result_contract.send_via` | 高级覆盖或迁移兼容的可见契约；普通互动可省略 |
| `settlement` | `settlement` action 或可审计结算元数据 |

迁移桥示例见 `examples/plugins/with_interaction`。该示例保留旧入口声明，但已经补齐 `usage`、`event_subscriptions`、`capabilities`，并修正了历史配置字段 `message` 与标准信封 `payload["message"]` 的冲突。

## 安装与验证

发布前至少运行：

```bash
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
```

示例校验会检查：

- 必要文件和 key/version/category 一致性。
- `usage` 是否存在。
- `event_subscriptions` 与 `MANIFEST.event_subscriptions` 是否一致。
- `capabilities` 与 `MANIFEST.capabilities` 是否一致。
- `capabilities.telegram_native_raw` 是否有 reason。
- 示例 fixtures 是否覆盖 message、command、callback、inline、payment。
- 是否出现旧 `notice` 发送通道、`bbot_notice`、`notice_bot`、`raw_event` 风险。

安装态校验会对已安装互动插件做一致性检查；旧插件缺少最终版字段时会输出 warning，避免在本轮无法改 installed 插件时误判为脚本故障。

## 发布前检查

- [ ] `plugin.json` 有 `usage`，并能让用户不用读旧规则也知道怎么启用。
- [ ] 事件插件声明了 `event_subscriptions`，且覆盖 message/command/callback/inline/payment 中实际使用的事件。
- [ ] `capabilities` 已声明；需要原生字段时写明 `telegram_native_raw.reason` 和 `sources`。
- [ ] 插件只读取标准事件信封，不依赖旧平铺 payload。
- [ ] 所有发送、编辑、按钮 ACK、Inline answer、结算都走 MessageOps/action。
- [ ] 原生 Rich Message 使用 `send_rich_message` 并复用 `send_message` 权限；默认确认 Interaction Bot 可用，显式 Userbot 模式还要确认 Premium 与 `rich_message_posting`，禁止静默降级。
- [ ] `answer_inline_query` 插件同时处理 `chosen_inline_result` 或明确忽略。
- [ ] 付款插件使用 `payment.status=confirmed` 与 `settlement`，普通 Bot 不执行转账。
- [ ] 旧 `interaction_entries` 只出现在迁移桥或兼容说明里。
- [ ] 没有 `notice` / `bbot_notice` / `notice_bot` 可执行通道，没有 `raw_event` 业务依赖。
