# TelePilot 插件速查表

先读 [插件开发铁律](./PLUGIN-RULES.md)，再用本页回忆字段名和常用模式。

- 必须理解 TelePilot 插件按个人可信插件模式运行：管理员安装并启用后，视为信任插件业务逻辑；平台负责事件信封、MessageOps 代发、Trace、风险提示、急停和审计。
- 最快命令插件可用简单模式 SDK：`from telepilot import plugin`，再写 `@plugin.command("ping")` 的单函数 `async def ping(ctx): await ctx.reply("pong")`；插件目录只需要 `__init__.py`，目录名就是插件 key。
- 简单模式不写 `PLUGIN_CLASS` / `MANIFEST` 时，loader 会从装饰器合成隐式插件类和 Manifest；当前隐式权限是 `read_event` + `send_message`，命令默认是 owner-only 的账号命令。
- 简单模式适合快速账号命令、小工具和内部玩法；需要 `plugin.json` 展示字段、`event_subscriptions`、配置 schema、HTTP/AI 权限、按钮、Inline、付款或会话时，用显式 Manifest 模式。
- 当前 `tp_plugin new` 没有 `--profile simple`；可选 profile 只有 `session_game`、`command`、`passthrough`。其中 `session_game` / `command` 仍生成 `interaction_entries + on_interaction` 兼容桥，只用于迁移或作为包结构起点，新标准插件应改成 `event_subscriptions + on_event`。
- 必须把新 Telegram 插件写成 Event Bus + Trace + MessageOps：`plugin.json` 写 `usage`、`event_subscriptions`、`capabilities`，插件只读标准事件信封，动作只通过 `ctx.messages` 或标准 action 返回。
- 禁止把 `interaction_entries`、旧交互规则、旧平铺 payload、`notice` / `bbot_notice` / `notice_bot` 作为新插件模板；这些只用于迁移说明。
- 显式 Manifest 模式必须保留最小目录：`plugin.json`、`manifest.py`、`plugin.py`、`__init__.py`。`plugin.json.name`、`MANIFEST.key`、插件类 `key` 必须一致。
- 必须把 `usage` 写成插件中心可展示的使用说明；有 `config_schema` 时也可以补 `x-usage-guide` / `x-usage-steps`，但不要只靠口头说明。
- 禁止用空 `usage` 绕过检查；缺失会触发高级规范警告，插件库维护插件和示例插件都必须完整声明。
- `event_subscriptions[].events` 常用值：`message`、`command`、`callback_query`、`inline_query`、`chosen_inline_result`、`payment_confirmed`、`webhook`、`session_close`、`message_edited`、`session_expired`、`all_events`；`all_messages` 仍只等于 `message` / `command`。
- `event_subscriptions[].source` 常用值：`userbot`、`interaction_bot`、`external_payment_notice`、`webhook`。
- `event_subscriptions[].scope` 常用值：`all_allowed_chats`、`owner_only`、`known_users`、`rule_bound`、`inline_all`；Inline 插件必须显式用 `inline_all`。
- 账号级“允许会话”留空表示全部会话；列表非空时才只允许名单内会话。插件自定义的 `allowed_chat_ids=[]` 不自动继承这条语义。
- `known_users` 只认平台 state 提供的真实集合，不会自动把当前 sender 算进去。
- `filters` 常见键：`keywords`、`contains`、`callback_data`、`commands`、`rule_id`、`hook_key`、`hook_keys`；未知 key 会保留但会告 warning。
- 严格来说只有 `telegram_direct_passthrough` 叫裸直通；Event Bus、会话入口、legacy hook 都是消息分发/会话路由。
- `capabilities.telegram_native_raw` 是高风险能力声明；需要原生 Telegram 字段时写 `enabled=true`、`reason`、`sources`，并处理 `native_raw_meta.enabled=false` 的降级。
- 标准事件信封优先读：`source`、`message`、`chat`、`sender`、`actor`、`source_actor`、`player`、`payment`、`reply_to`、`trigger`、`session`、`native_raw_meta`。
- 新插件读取文本优先用 `payload["tp_event"]` 或 `event_from_interaction_payload(payload)`；不要用 `payload["text"]` / `payload["chat_id"]` / `payload.get("message")` 当主路径。
- 互动玩法优先写成一个 `on_event(ctx, payload)`，在同一个入口里处理 `command`、`keyword`、`payment_confirmed`、`message`、`callback_query`、`session_expired`。
- 会话状态放进 `session.data`，状态变更返回 `update_session`；不要再靠进程内 dict/lock 才能续局。
- `source` 描述事件类型和来源通道；`actor` 是当前行为主体；`sender` 是发出消息的人或 Bot；`source_actor` 可表示可信外部通知 Bot；`player` 是付款绑定玩家；`payment.status=confirmed` 才能作为到账依据。
- `session.channel` 表示当前整段会话默认收发通道；普通发送动作不用手写 `send_via`，平台会继承会话通道。
- 普通消息回复使用 `ctx.messages.send(...)` 或返回 `{"type": "send_message", ...}`；标题、任务列表、折叠详情、表格等原生格式使用 `ctx.messages.send_rich(html=...)` 或 `send_rich_message` action；图片题面可用 `ctx.messages.send_photo(..., save_message_id_key="round")`，后续用 `ctx.messages.edit_caption(message_id_key="round", caption="...")` 原地更新 caption。
- `send_rich` 的 `html` / `markdown` / `blocks` 必须三选一，继续声明 `send_message` 权限；默认走 Interaction Bot。显式 `userbot_reply` 使用 Layer 228，支持 HTML、Markdown 和纯文本 blocks，并要求 Premium 与 `rich_message_posting`；复杂/媒体 blocks 失败时不会静默退化。
- 当前 Interaction Bot 发按钮、用户点击：订阅 `source=["interaction_bot"]`、`events=["callback_query"]`、`scope="all_allowed_chats"`，用 `answer_callback` ACK；不依赖旧 Interaction Bot 规则或活跃会话。
- 第三方 Bot 发按钮、TelePilot UserBot 主动点击：插件声明 `permissions=["click_bot_button"]`，在 UserBot 执行链路调用 `ctx.messages.click_callback_button(chat_id=..., message_id=..., row=..., column=..., expected_bot_id=..., expected_button_text=...)`；Interaction Bot 插件入口不支持。这不是 `callback_query`，也不能用 `answer_callback`。
- 平台只允许 callback 类型，重新读取 Telegram 消息和 callback data，并统一执行 Bot/文字校验、限流、Trace、ActionEvent、dry-run 和账号级物理点击锁；明确成功后保护 20 秒，超时/未知结果保护 5 分钟，Redis 不可用时拒绝点击。`message.buttons[row][column].click()` 已被安全阻断。
- userbot 会话没有原生按钮，平台会把按钮降级成文本编号，并把玩家回复合成为 callback；此时 `source.synthetic="text_button"`。
- 免费参与、按钮加入、互动游戏可按自身玩法保存完整业务状态；如果只是为了后续发奖锚点，记录玩家 `tgid` 即可。发奖动作优先用 `payout`，也可走 `userbot_reply` 并携带 `reply_to_user_id`，平台会搜索该玩家近期发言作为回复锚点，插件不要自己遍历群消息。已有 `reply_to_message_id` 时优先用消息 id；找不到锚点时动作失败并写入日志，默认提示 `未找到对应用户（用户 ID）的近期消息。`，可用 `reply_anchor_missing_text` 自定义提示。
- Inline 插件返回 `answer_inline_query`；选择结果进入 `chosen_inline_result`，用于记录选择、结算或后续状态。
- 付款/发奖插件返回 `settlement` 或 userbot 受控动作；普通 Bot 只公告结果，不直接执行转账、催付或发奖。
- 常见 action：`send_message`、`send_rich_message`、`send_photo`、`send_file`、`edit_message`、`edit_caption`、`delete_message`、`pin_message`、`click_callback_button`、`answer_callback`、`answer_inline_query`、`payout`、`update_session`、`settlement`、`result`、`end_session`。
- `send_via` 只在高级覆盖时使用 `interaction_bot`、`userbot_reply` 或 `auto`；旧 `notice` 值应返回迁移错误，不能静默执行。
- 默认 `send_message` / `send_photo` / `send_file` 等动作按 `parse_mode="plain"` 发送；只有显式声明 `parse_mode="html"` 时才启用普通消息 HTML。`send_rich_message` 使用独立的 Rich HTML/Markdown/blocks 语法，动态 HTML 文案同样先转义再拼标签。
- 强按钮玩法可通过 `interaction_trigger_modes=keyword_only` 关闭命令触发；manifest 可用 `default_trigger_modes` 提供默认值。
- `callback_fast_ack=true` 适合耗时按钮入口；启用后晚到的 `answer_callback` 不再真正调用 Telegram ACK。
- `ctx.http` 需要 `permissions=["external_http"]` 和 `allowed_hosts`。
- `ctx.ai` 需要 `permissions=["ai_text"]`，复用平台 LLM Provider、fallback、预算和 usage 记录。
- `ctx.client` 只保留给管理员命令和高级兼容场景；远程插件仍不能直接拿 token、session、Bot API client 或 live event。
- `ctx.conversation()` 目前只适合获得真实 `TelegramClient` 的 builtin/内部兼容代码；普通安装插件拿到 `SandboxClient`，无法可靠注册会话 handler 或发送 raw MTProto，不应依赖它。
- 安装插件的 `ctx.redis` 是带 `plugin_store:{account_id}:{plugin_key}:` 前缀的 `PluginRedisFacade`，不是原始 Redis client；`keys`、`scan`、`eval`、`pipeline`、`pubsub` 等操作不可用，普通状态仍优先用 `ctx.storage`。
- `command` 只保存裸指令名，不保存前缀；schema/usage 模板用 `{prefix}`，运行时帮助与错误示例必须调用 `from app.worker.command import current_command_prefix`，使用 `current_command_prefix(fallback=",")`。不要从 `ctx.account_config` 读取系统前缀，也不要硬编码逗号。
- `on_command(ctx, cmd, args, event) -> bool` 保留给账号主人/授权管理员命令；群友公开触发走 Event Bus 订阅。
- `on_message` 是旧消息监听兼容 hook；新增 Telegram 交互优先写 `on_event` 标准事件入口，`on_interaction` 只作为迁移桥。
- 已有 `interaction_entries` 插件迁移时，要把入口事件映射到 `event_subscriptions`，把 `payload_contract/result_contract/settlement` 转成标准信封和标准 action。
- `interaction_entries[].session_scope` 的迁移含义：群局映射为 `session.scope=chat`，个人流程映射为 `session.scope=user`，一次性动作映射为无持久 session。
- 规则 `concurrency=user` 只是触发频控粒度，不等于插件会话 key。
- 抢答、竞猜、抽奖要加锁和二次检查；禁用、热重载、超时和卸载都要清理状态。
- 外部请求必须有 timeout；日志里不要写 token、session、完整原生 payload、隐私消息或完整文件路径。
- 维护示例：新主模板看 `examples/plugins/event_bus_demo`；Webhook 看 `webhook_receiver`；HTTP 看 `with_http`；AI 看 `with_ai`；旧交互迁移看 `with_interaction`。
- 迁移边界：平台功能不伪装成插件；插件库维护插件必须完整声明；示例插件只用于学习和 CI；用户安装插件可保留代码但启用/更新时要提示规范警告。
- 验证示例：`backend/.venv/bin/python scripts/validate-plugin-examples.py`；检查已安装互动插件：`backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py`。

常见 `reason_code` 快查：

| reason_code | 含义 |
| --- | --- |
| `matched` | 订阅命中 |
| `subscription_not_matched` / `filter_not_matched` | 没有订阅命中 / 过滤条件未命中 |
| `plugin_disabled` / `plugin_load_failed` / `plugin_runtime_error` | 插件未启用 / 加载失败 / 运行异常 |
| `command_matched` / `command_not_matched` / `command_unauthorized` | 命令命中 / 未命中 / 权限不足 |
| `event_bus_delivery_disabled` / `inline_disabled` | 运维开关关闭 Event Bus 投递 / Inline |
| `native_raw_not_allowed` / `native_raw_skipped` | 未声明原生数据能力 / 本次未下发 |
| `send_channel_deprecated` / `unsupported_send_via` | 使用旧 notice 通道 / 未支持通道 |
| `premium_required` / `rich_message_posting_disabled` / `rich_message_blocks_unsupported` / `rich_message_media_unsupported` / `invalid_rich_message` | Userbot Rich Message 能力不足、输入超出当前适配范围或结构非法 |
| `already_acked` / `synthetic_callback` | fast-ack 已提前确认 / 文本按钮降级合成的 callback |
| `action_limit_exceeded` | 动作数量超出平台限制，后续动作会被截断并写入可见告警 |
| `bot_not_configured` / `bot_token_missing` / `userbot_offline` | Bot 未配置 / token 缺失 / UserBot 离线 |
| `payout_failed` / `telegram_api_error` / `trace_write_failed` | 发奖动作失败 / Telegram API 失败 / Trace 写入降级 |

最小单入口模板：

```python
from telepilot import plugin


@plugin.command("ping")
async def ping(ctx):
    await ctx.reply("pong")
```

显式 Event Bus 单入口模板：

```python
async def on_event(self, ctx, payload):
    event = payload["tp_event"]

    if event.type == "command":
        return [{"type": "update_session", "data": {"answer": "42"}}]

    if event.type == "message" and event.message.text == "42":
        return [
            {"type": "payout", "amount": 88, "reply_to_user_id": event.sender.user_id},
            {"type": "end_session"},
        ]

    if event.type == "session_expired":
        return [{"type": "send_message", "chat_id": event.message.chat_id, "text": "本局已超时"}]

    return []
```

Rich Message 快速模板：

```python
await ctx.messages.send_rich(
    html=(
        "<h1>任务状态</h1>"
        '<ul><li><input type="checkbox" checked>已完成</li></ul>'
        "<details><summary>详情</summary><p>trace 已写入</p></details>"
        "<table bordered><tr><th>项目</th><th>状态</th></tr>"
        "<tr><td>Worker</td><td>正常</td></tr></table>"
    )
)
```
