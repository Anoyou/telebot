# TelePilot 远程插件

远程插件最终版契约是 **Event Bus + Trace + MessageOps**。新插件不再以 `interaction_entries`、旧交互规则、旧平铺 payload 或 `notice` 通道作为主路径；这些内容只用于迁移旧插件。

## 适用场景

- 把第三方插件以 zip、Git 仓库或 Registry 条目分发给 TelePilot。
- 插件需要接收 Telegram 标准事件：消息、管理员命令、按钮回调、Inline、付款确认。
- 插件需要通过 TelePilot 代发消息、ACK 按钮、回答 Inline Query 或记录结算动作。
- 插件需要声明 HTTP、AI、原生 Telegram raw 等风险能力，供安装前提示和 Trace 排障。

远程插件仍按个人可信插件模式运行：安装者自行信任插件业务逻辑；平台负责能力声明、事件信封、MessageOps 执行、Trace、审计、限流和客观失败提示。

## 目录结构

```text
my_plugin/
├── __init__.py
├── manifest.py
├── plugin.py
└── plugin.json
```

`plugin.json` 是静态安装元数据，不执行 Python；安装后运行时仍会读取 `manifest.py` 的 `MANIFEST`。两边的 `name/key`、`version`、`category`、`event_subscriptions`、`capabilities` 必须保持一致。

消息链路统一阶段的互动入口，还要额外关注 4 个字段：

- `triggers.command`：把某个入口声明成可由 UserBot 前缀命令开局。
- `default_trigger_modes`：给平台注入的 `interaction_trigger_modes` 提供默认值，常见值是 `all` / `keyword_only`。
- `callback_fast_ack`：按钮入口是否在分类完成后立即空 ACK。
- `include_outgoing`：userbot 会话是否继续吃本账号自己发出的消息。

如果当前实例还没合入对应 runtime/worker 分支，上述字段只作为仓库契约说明，不保证旧环境自动生效。

## plugin.json 最小模板

```json
{
  "name": "event_bus_demo",
  "display_name": "Event Bus 示例",
  "description": "演示最终版事件订阅、Trace 与 MessageOps。",
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
  "usage": "启用后按 Event Bus 订阅接收 message/command/callback/inline/payment 事件，所有输出都返回标准 action。",
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

`usage` 缺失不是普通文案缺口，而是最终版规范警告：插件中心无法告诉安装者“谁能触发、监听什么事件、会发什么消息、如何排障”。远程插件、插件库维护插件和示例插件都必须写 `usage`；有配置页时还应在 `config_schema` 顶层补 `x-usage-guide`、`x-usage-instructions` 或 `x-usage-steps`，但这些只能增强说明，不能替代 `plugin.json.usage`。

## 插件生态迁移边界

最终版按身份处理插件，不再把系统能力和可安装插件混成一类：

| 类型 | 边界 | 最终版处理 |
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

## capabilities.telegram_direct_passthrough

`telegram_direct_passthrough` 是更高风险的低延时直通能力，只适合抢红包、秒杀、抢答首响等对毫秒级延迟敏感、且愿意跳过 TelePilot 标准 Event Bus / Trace / MessageOps 链路的插件。普通互动、付款确认、按钮、Inline 和需要审计回放的业务不要使用它。

插件必须同时满足两层开关才会收到直通消息：

1. `plugin.json` 与 `MANIFEST.capabilities` 显式声明 `telegram_direct_passthrough.enabled=true`，并写清 `reason`、`sources`、`directions`。
2. 安装后在对应账号的插件配置里二次手动开启：

```json
{
  "direct_passthrough": {
    "enabled": true
  }
}
```

仅声明能力不会启用直通；账号只启用插件本身也不会启用直通。运行时仍保留账号启用、installed 插件授权和 worker 暂停急停；通过这些外层检查后，worker 会在 incoming 白名单、交互 Bot 关键词接管、Trace、Event Bus 订阅匹配、legacy `on_message` 包装之前广播调用：

```python
async def on_direct_message(self, ctx, event):
    ...
```

`event` 是 live Telethon event，不是标准事件信封。所有开启直通模式且匹配 source/direction 的插件都会收到同一条原始事件；只要至少一个直通插件被调用，本条消息就会被直通链路消费，不再进入 incoming 白名单、交互 Bot guard、Event Bus 或 legacy `on_message`，避免低延时插件和普通链路重复处理同一条消息。直通 hook 的发送、编辑、点击等行为不会自动生成标准 action/Trace；插件作者必须自行承担幂等、异常、限流和审计缺失的风险。

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

默认 `send_message` / `send_photo` / `send_file` 等动作按 `parse_mode="plain"` 发送；只有显式声明 `parse_mode="html"` 时才启用 HTML。HTML 内容要先转义，再构造标签。

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
- [ ] `answer_inline_query` 插件同时处理 `chosen_inline_result` 或明确忽略。
- [ ] 付款插件使用 `payment.status=confirmed` 与 `settlement`，普通 Bot 不执行转账。
- [ ] 旧 `interaction_entries` 只出现在迁移桥或兼容说明里。
- [ ] 没有 `notice` / `bbot_notice` / `notice_bot` 可执行通道，没有 `raw_event` 业务依赖。
