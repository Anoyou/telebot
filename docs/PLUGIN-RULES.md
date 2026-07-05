# 插件开发铁律

这页是短契约，不是完整教程。完整字段查 [API 参考](./PLUGIN-API-REFERENCE.md)，最小模板看 [5 分钟 Quickstart](./PLUGIN-QUICKSTART.md)。

## 必须

1. 新 Telegram 插件必须走 Event Bus + MessageOps：读取标准事件信封，输出 `ctx.messages` 操作或标准 action。
2. 新互动玩法优先实现一个 `on_interaction(ctx, entry_key, payload)`，在同一个入口里处理 `command`、`keyword`、`payment_confirmed`、`message`、`callback_query`、`session_expired`。
3. 会话状态优先写进 `session.data`，并通过 `update_session` 持久化；不要再把单局状态只放在进程内 dict/lock 里。
4. 插件必须声明 `usage`、`event_subscriptions`、`capabilities`；没有高风险能力也要写 `capabilities={}`。
5. `plugin.json.name`、`MANIFEST.key`、插件类 `key` 必须一致。
6. 发送、编辑、媒体 caption 编辑、删除、置顶、按钮 ACK、Inline answer、`payout`、`update_session`、`settlement` 必须通过 `ctx.messages` 或标准 action 交给平台执行。
7. 远程插件安装后默认不运行，必须在插件中心按账号启用后才会收到事件。
8. 钱相关能力必须走 UserBot 或平台受控结算链路；普通 Bot 只能做交互和公告，不能执行转账。
9. 群里已有的外部转账结果通知 Bot 只作为付款证据来源，不是 TelePilot 主动发送通道。
10. 需要原生 Telegram 字段时，必须声明 `capabilities.telegram_native_raw`，写清 reason、sources 和降级路径。
11. 外部 HTTP 必须声明 `permissions=["external_http"]` 和 `allowed_hosts`，请求必须有 timeout。
12. AI 能力必须声明 `permissions=["ai_text"]`，并通过 `ctx.ai` 使用平台 Provider、fallback 和预算。
13. 插件启停、禁用、热重载、超时和卸载时，必须清理 handler、session、scheduler job、asyncio task、临时消息、临时文件和游戏状态。
14. 日志必须脱敏，不得写入 token、session、完整原生 payload、隐私消息或完整敏感文件路径。

## 禁止

1. 禁止把旧 `notice` / `bbot_notice` / `notice_bot` 当主动发送通道。
2. 禁止依赖旧 `raw_event` 或旧平铺 payload 作为新插件主路径。
3. 禁止直接拼 Bot API、直接拿 Bot Token、直接操作 UserBot session 或 live Telegram event。
4. 禁止绕过 MessageOps 自行发送、编辑、删除、置顶或 ACK 按钮回调。
5. 禁止把用户输入直接拼进 SQL、shell、路径、HTML 或正则执行点。
6. 禁止在 `on_startup` / `on_shutdown` 无条件群发消息；确需通知必须有显式配置开关。
7. 禁止让抢答、抽奖、付款确认在无锁状态下结算；必须有原子判定和二次检查。
8. 禁止用空 `usage`、空权限或模糊能力声明绕过规范警告。
9. 禁止在普通会话消息里把 `send_via` 当必填样板；大多数动作应该继承 `session.channel`。
10. 禁止在需要 `show_alert` 晚提示的按钮入口上开启 `callback_fast_ack`。

## 推荐

1. 从 `examples/plugins/hello_ping` 开始复制最小结构，再参考 `event_bus_demo` 扩展事件类型。
2. 把单局金额、题目范围、奖励等动态参数放在触发参数或会话里，不要写死到全局配置。
3. 帮助、开局、成功、失败、超时、取消和冷却文案做成模板，并支持 `{prefix}`。
4. 游戏和高频交互按 chat/user 设计锁、冷却、超时和取消入口。
5. 默认让平台继承 `session.channel`；只有跨通道公告、迁移桥兼容或高级兜底时才显式给候选 `send_via`。
6. 强按钮玩法把 `default_trigger_modes` 设为 `keyword_only`，避免 userbot 会话里的文本按钮降级影响体验。
7. 免费参与、按钮加入和互动游戏可按玩法需要保存完整业务状态；仅从发奖锚点角度，保留玩家 `tgid` 并用 `payout.reply_to_user_id` 交给平台查找近期发言即可。不要要求玩家为了领奖再刷一条消息，也不要在插件里自行遍历群消息。
8. 文本输出默认走 `parse_mode="plain"`；需要 HTML 时先做 `html_escape()` 再拼标签。
9. 更新版本时同步 `plugin.json.version`、`MANIFEST.version` 和插件仓库索引里的版本。
10. 发布前运行 `backend/.venv/bin/python scripts/validate-plugin-examples.py`，并在真实账号上至少验证一次启用、触发、禁用和更新。
