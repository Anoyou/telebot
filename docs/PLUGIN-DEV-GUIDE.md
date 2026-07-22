# TelePilot 插件开发指南（索引）

> 这是一页索引，不再承载完整正文。原来的开发指南已按主题拆分，代码层 API 仍叫 `Plugin` / `PluginContext`，产品文案统一称“插件”。

> 路线决策保留在这里：TelePilot 0.x 默认采用 **个人可信插件标准模式**。管理员安装并启用插件后，即视为信任该插件的业务逻辑；远程插件风险由管理员自行承担。平台不做公共插件市场式强沙箱，而是通过 `Manifest.permissions`、`ctx.client`、`ctx.http`、`ctx.ai`、`ctx.messages`、`ctx.identities` 等 facade 收口常用能力，并保留频控、审计、急停、日志脱敏和 token/session 隔离。

> 如果未来要开放“任意第三方上传、未经人工审核”的公共市场，需要另行设计 subprocess/容器隔离、资源配额、文件系统/网络沙箱和供应链扫描。它不属于当前 0.x 默认方案，本文其余章节、示例、CI 和安全边界都按个人可信插件标准模式编写。

## 目录

- [5 分钟 Quickstart](./PLUGIN-QUICKSTART.md)
- [入站 Webhook Quickstart](./PLUGIN-WEBHOOK-QUICKSTART.md)
- [插件开发铁律](./PLUGIN-RULES.md)
- [完整 API 参考](./PLUGIN-API-REFERENCE.md)
- [插件概览](./PLUGIN-OVERVIEW.md)
- [HTTP facade](./PLUGIN-HTTP.md)
- [AI facade](./PLUGIN-AI.md)
- [平台能力热插拔](./PLATFORM-CAPABILITIES.md)
- [远程插件](./PLUGIN-REMOTE.md)
- [安全边界](./PLUGIN-SAFETY.md)
- [开发者工具链](./PLUGIN-DEVTOOLS.md)
- [速查表](./PLUGIN-CHEATSHEET.md)

## 当前插件链路速览

消息链路统一后，互动插件默认按这一套模型理解：

- 触发方式决定普通会话通道。带前缀命令开局，整段普通收发走 `userbot`；关键词、付款确认、按钮回调开局，整段普通收发走 `interaction_bot`。固定能力例外：`payout` 走 UserBot，`send_rich_message` 默认走 Interaction Bot，显式 `userbot_reply` 才使用 Layer 228。
- 新玩法优先写成一个 `on_interaction(ctx, entry_key, payload)` 入口，在同一个入口里按 `tp_event.type` 或 `payload["source"]["type"]` 处理 `command`、`keyword`、`payment_confirmed`、`message`、`callback_query`、`session_expired`。
- 单局状态优先写进 `session.data`，通过 `update_session` 持久化；不要再把游戏状态放进进程内全局字典、锁和自建超时任务。
- 普通 JSON 状态优先使用 `ctx.storage`；确需 SQLite、缓存文件或索引文件时写入 `ctx.data_dir`。禁止把运行数据写到插件代码目录或 `Path(__file__).parent`，因为安装和更新会整体替换该目录。
- 免费参与、按钮加入、互动游戏可按自身玩法保存完整业务状态；仅从后续发奖锚点角度，保存玩家 `tgid` 并通过 `payout.reply_to_user_id` 交给平台即可。平台优先读取 UserBot 按账号、群、真实消息发送者保存的近期锚点，缓存缺失时再精确搜索并最多扫描 2000 条历史消息。发奖同时传公开名时，`payout.reply_to_display_name` 必须来自安全身份 facade，匿名管理员不得传 `reply_to_username`。匿名管理员和频道身份不会建立真实用户锚点；找不到锚点时平台默认提示，并允许插件用 `reply_anchor_missing_text` 自定义失败提示。
- 按钮回调的 `payload.sender` 是实际点击账号，不能直接把其中的姓名写回群消息；群内公开姓名必须调用 `resolve_public_sender_identity()`。返回的 `display_name` 已由平台过滤 Unicode 控制符、零宽字符和不可见空白，并限制为 10 个字符；`is_admin` 表示平台已确认的本群管理员状态，插件不能通过是否存在 `tag` 猜测管理员身份。插件已有独立公开标签时可调用 `sanitize_public_display_name()` 使用同一规则。身份只通过 UserBot 核验；匿名管理员只显示管理员标签，普通成员标签不会覆盖姓名，查询失败时平台会隐藏姓名。
- `resolve_public_sender_identity()` / `resolve_public_sender_identities()` 的身份结果不做应用层缓存，每次调用都会实时读取当前群权限。UserBot 实体恢复只读取本地实体缓存或 Redis 中可重新校验的近期发言 `message_id`，不会在按钮 callback 内扫描群历史；锚点不保存姓名、username、管理员状态或标签。插件不要另行缓存这些身份字段。
- 明确需要“账号 UserBot 在所有已加入群里看到的姓名”时，调用 `ctx.identities.resolve_userbot(chat_id=..., user_id=..., fallback_display_name=...)`。该入口不会调用 Interaction Bot，并会保留 UserBot 联系人实体中的姓名；仍会通过 UserBot 群权限隐藏匿名管理员。当前 loader 的默认身份注入同样只使用 UserBot。
- `ctx.messages.send/send_photo/edit/edit_rich/edit_caption/payout(...)` 和普通标准 action 默认按 `parse_mode="plain"` 发送；图片/文件 caption 更新用 `edit_caption`，不要把媒体消息交给 `edit_message` 猜类型。标题、任务列表、折叠详情、表格等 Telegram 原生结构改用 `ctx.messages.send_rich()` 或 `ctx.messages.edit_rich()`，并从 `html` / `markdown` / `blocks` 三选一。Rich Message 默认由 Interaction Bot 发送/编辑；显式 Userbot 支持 HTML、Markdown 和可无损转换的纯文本 blocks，并要求 Premium 与能力开关。
- userbot 会话没有原生 inline 按钮能力。平台会把按钮降级成“回复序号选择”的文本面板，并把命中的回复合成为 callback 事件回投插件；强依赖按钮的入口应配合 `keyword_only` / `default_trigger_modes` 关闭命令触发。

## 读法

1. 新人先看 [5 分钟 Quickstart](./PLUGIN-QUICKSTART.md)，复制 `hello_ping` 跑通最小 Event Bus + MessageOps 插件。
2. 写真实插件前看 [插件开发铁律](./PLUGIN-RULES.md)，确认必须、禁止、推荐的边界。
3. 查字段、facade、标准事件信封、MessageOps、Trace 和生命周期时看 [完整 API 参考](./PLUGIN-API-REFERENCE.md)。
4. 需要理解个人可信插件标准模式、安装/启用/更新/卸载心智时看 [插件概览](./PLUGIN-OVERVIEW.md)。
5. 需要外部网络能力时看 [HTTP facade](./PLUGIN-HTTP.md)，需要 AI 能力时看 [AI facade](./PLUGIN-AI.md)。
6. 需要让 n8n、GitHub、监控或业务系统触发插件时看 [入站 Webhook Quickstart](./PLUGIN-WEBHOOK-QUICKSTART.md)。
7. 需要 Git 安装、`plugin.json`、Registry、发布检查时看 [远程插件](./PLUGIN-REMOTE.md)。
8. 需要权限、前缀、消息发送、并发和清理约束时看 [安全边界](./PLUGIN-SAFETY.md)。
9. 从零开发、校验、登记、命中调试、dry-run 安全测试、录制回放回归时看 [开发者工具链](./PLUGIN-DEVTOOLS.md)。
10. 需要快速回忆字段名和常用模式时看 [速查表](./PLUGIN-CHEATSHEET.md)。

## 开发者工具链速览

工具链说明统一收口到 [开发者工具链](./PLUGIN-DEVTOOLS.md)。推荐顺序是：

1. 用 `tp_plugin new <name> --profile session_game|command|passthrough` 生成骨架。
2. 写 `plugin.py`、`plugin.json` / `manifest.py` 的入口、权限、事件订阅和配置。
3. 用 `tp_plugin check <dir>` 做 manifest、事件订阅和权限推导审计；它只报告问题，不自动改文件。
4. 用 `tp_plugin register <dir>` 登记本地插件目录；外部目录与 `plugins/local_imports` 旧副本冲突时，确认后再加 `--force`。
5. 在账号配置里打开 `{"dev_mode": {"dry_run": true}}`，先让发送和 payout 出口只记录、不真实投递。
6. 用 `POST /api/dispatch/simulate` 贴账号和消息文本，看命中哪条规则、插件、入口及未命中原因。
7. 需要完整链路 trace 时，优先用 manifest 的 `strict_trace` 常驻追踪资金/高风险插件；临时排查用 `POST /api/dispatch/router-debug-trace` 打开短 TTL router trace。
8. 需要回归样本时，打开 `{"dev_mode": {"recording": true}}` 录入站信封 JSONL，再用 `tp_replay run <recording.jsonl>` 离线 dry-run 回放。

## 兼容说明

- 旧章节锚点已经不再提供。
- `docs/PLUGIN-AI.md` 保持独立。
- `interaction_trigger_modes`、`default_trigger_modes`、`callback_fast_ack` 是当前运行时入口契约；插件发布前应使用当前版本的示例校验脚本验证，不要按旧分支行为兼容。
