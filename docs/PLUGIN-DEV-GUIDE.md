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

先按当前代码区分三条消息链路：

1. **裸直通**：`telegram_direct_passthrough` 把 UserBot 实时事件交给 `on_direct_message`；安装插件收到 `SandboxEvent` 权限包装，不拿真实嵌套按钮客户端。
2. **UserBot 标准消息链路**：包括 Event Bus、前缀命令会话和 legacy `on_message`，可以接收 `incoming` / `outgoing`；只有实际建立会话时才由 `session.channel=userbot` 决定后续普通收发。
3. **Interaction Bot 标准消息链路**：包括 Event Bus 自主订阅和旧规则会话；自主订阅不要求先配置旧 Interaction Bot 规则或建立活跃会话。

Event Bus、Trace 和 MessageOps 是两条标准消息链路共用的内部契约，不是第四种模式。不要把整个 UserBot 标准链路简称为“命令会话”，也不要把整个 Interaction Bot 标准链路简称为“规则会话”。

消息链路统一后，互动插件还要记住：

- 触发方式决定普通会话通道。带前缀命令开局，整段普通收发走 `userbot`；关键词、付款确认、按钮回调开局，整段普通收发走 `interaction_bot`。固定能力例外：`payout` 走 UserBot，且已安装插件必须在 `permissions` 中显式声明 `payout`，否则运行时拒绝执行；`send_rich_message` 默认走 Interaction Bot，显式 `userbot_reply` 才使用 Layer 228。
- 账号级“允许会话”列表留空表示全部会话放行；列表非空时才只允许名单内会话。插件自己的 `allowed_chat_ids=[]` 是插件自定义配置，语义由插件自己定义，不能套用平台规则。
- 新玩法优先实现一个 `on_event(ctx, payload)` 入口，在同一个入口里按 `tp_event.type` 或 `payload["source"]["type"]` 处理 `command`、`keyword`、`payment_confirmed`、`message`、`callback_query`、`session_expired`。
- 单局状态优先写进 `session.data`，通过 `update_session` 持久化；不要再把游戏状态放进进程内全局字典、锁和自建超时任务。
- 普通 JSON 状态优先使用 `ctx.storage`；确需 SQLite、缓存文件或索引文件时写入 `ctx.data_dir`。禁止把运行数据写到插件代码目录或 `Path(__file__).parent`，因为安装和更新会整体替换该目录。
- 免费参与、按钮加入、互动游戏可按自身玩法保存完整业务状态；仅从后续发奖锚点角度，保存玩家 `tgid` 并通过 `payout.reply_to_user_id` 交给平台即可。平台优先读取 UserBot 按账号、群、真实消息发送者保存的近期锚点，缓存缺失时再精确搜索并最多扫描 2000 条历史消息。发奖同时传公开名时，`payout.reply_to_display_name` 必须来自安全身份 facade，匿名管理员不得传 `reply_to_username`。匿名管理员和频道身份不会建立真实用户锚点；找不到锚点时平台默认提示，并允许插件用 `reply_anchor_missing_text` 自定义失败提示。
- 按钮回调的 `payload.sender` 是实际点击账号，不能直接把其中的姓名写回群消息；群内公开姓名必须调用 `resolve_public_sender_identity()`。返回的 `display_name` 已由平台过滤 Unicode 控制符、零宽字符和不可见空白，并限制为 10 个字符；`is_admin` 表示平台已确认的本群管理员状态，插件不能通过是否存在 `tag` 猜测管理员身份。插件已有独立公开标签时可调用 `sanitize_public_display_name()` 使用同一规则。身份只通过 UserBot 核验；匿名管理员只显示管理员标签，普通成员标签不会覆盖姓名，查询失败时平台会隐藏姓名。
- `resolve_public_sender_identity()` / `resolve_public_sender_identities()` 的身份结果不做应用层缓存，每次调用都会实时读取当前群权限。UserBot 实体恢复只读取本地实体缓存或 Redis 中可重新校验的近期发言 `message_id`，不会在按钮 callback 内扫描群历史；锚点不保存姓名、username、管理员状态或标签。插件不要另行缓存这些身份字段。
- 明确需要“账号 UserBot 在所有已加入群里看到的姓名”时，调用 `ctx.identities.resolve_userbot(chat_id=..., user_id=..., fallback_display_name=...)`。该入口不会调用 Interaction Bot，并会保留 UserBot 联系人实体中的姓名；仍会通过 UserBot 群权限隐藏匿名管理员。当前 loader 的默认身份注入同样只使用 UserBot。
- `ctx.messages` 的 helper 随上下文不同。Event Bus / `on_interaction` 的缓冲 facade 提供 `send_photo/send_file/edit_rich/edit_caption/update_session`；常驻、插件命令、legacy 消息、裸直通和 scheduler/后台 callback 注入 live facade，这些 helper 不存在，应使用文档化标准 action + `ctx.messages.apply([...])`。完整矩阵见 [API 参考：`ctx.messages` 上下文 × 方法矩阵](./PLUGIN-API-REFERENCE.md#43-ctxmessages-上下文--方法矩阵)。普通文本和 caption 默认 `parse_mode="plain"`；媒体 caption 用 `edit_caption`，原生 Rich Message 从 `html` / `markdown` / `blocks` 三选一。
- userbot 会话没有原生 inline 按钮能力。平台会把按钮降级成“回复序号选择”的文本面板，并把命中的回复合成为 callback 事件回投插件；强依赖按钮的入口应配合 `keyword_only` / `default_trigger_modes` 关闭命令触发。
- Inline 按钮有两种完全不同的行为：当前 Interaction Bot 发按钮、用户点击，是 `callback_query`，插件用 `answer_callback` ACK；第三方 Bot 发按钮、TelePilot UserBot 主动点击，是 MTProto 客户端操作，**不是** callback ACK。后者必须声明 `click_bot_button` 权限，并在 UserBot 执行链路调用 `ctx.messages.click_callback_button(...)`；Interaction Bot 插件入口不支持。旧的 `message.buttons[row][column].click()` 已被安全阻断。完整边界和示例见 [API 参考：Inline 按钮的两种场景](./PLUGIN-API-REFERENCE.md#inline-按钮的两种完全不同场景)。

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

1. 新标准插件优先复制 `examples/plugins/hello_ping` 或 `examples/plugins/event_bus_demo`。`tp_plugin new <name> --profile session_game|command` 当前生成的是 `interaction_entries + on_interaction` 兼容桥骨架，适合迁移和理解旧会话入口；`passthrough` 只用于明确需要原始 UserBot 事件的高级场景。
2. 写 `plugin.py`、`plugin.json` / `manifest.py` 的入口、权限、事件订阅和配置。
3. 用 `tp_plugin check <dir>` 做 manifest、事件订阅和权限推导审计；它只报告问题，不自动改文件。
4. 用 `tp_plugin register <dir>` 登记本地插件目录；外部目录与 `plugins/local_imports` 旧副本冲突时，确认后再加 `--force`。
5. 在账号配置里打开 `{"dev_mode": {"dry_run": true}}`，先让发送和 payout 出口只记录、不真实投递。
6. 优先打开侧栏一级「命中调试」页面，选择账号并粘贴消息文本；需要自动化或对照 OpenAPI 时再直接调用 `POST /api/dispatch/simulate`。
7. 需要完整链路 trace 时，优先用 manifest 的 `strict_trace` 常驻追踪资金/高风险插件；临时排查用 `POST /api/dispatch/router-debug-trace` 打开短 TTL router trace。
8. 需要回归样本时，打开 `{"dev_mode": {"recording": true}}` 录入站信封 JSONL，再用 `tp_replay run <recording.jsonl>` 离线 dry-run 回放。

## 兼容说明

- 旧章节锚点已经不再提供。
- `docs/PLUGIN-AI.md` 保持独立。
- `interaction_trigger_modes`、`default_trigger_modes`、`callback_fast_ack` 是当前运行时入口契约；插件发布前应使用当前版本的示例校验脚本验证，不要按旧分支行为兼容。

## 文档维护要求

插件开发文档必须按当前代码维护，不能根据历史聊天、旧文档或“设计上应该如此”推断：

1. 事件投递以 `backend/app/worker/plugins/loader.py`、`backend/app/services/event_bus.py` 和对应测试为准；标准事件字段以 `events.py` 及运行中的 schema 为准。
2. 沙箱与客户端能力以 `sandbox.py`、`message_ops.py`、`redis_facade.py` 的实际白名单/包装层级为准。Facade 上存在方法、底层库存在方法，不等于安装插件能调用。
3. 涉及 Telethon 对象时必须同时检查平台包装代码和当前锁定版本的 Telethon 实现。嵌套对象也必须持续净化；当前 `message.buttons` 与 `message.reply_markup.rows[].buttons[]` 都只暴露只读 `text` / `kind`，不允许插件取得 callback data、真实客户端或 raw button。
4. 协议名要按实际方向写清：Interaction Bot **收到** callback 后执行 ACK，与 UserBot **发起** MTProto 点击请求不能都简称“按钮回调”。
5. 文档新增可复制示例前，至少做一次最小运行验证；若能力只是当前兼容行为而非稳定 Contract，必须在示例旁明确写出限制、审计缺口和未来可能失效。
6. 修改插件路由、权限、沙箱、facade、事件订阅、配置语义或底层 SDK 版本时，要全局搜索并同步维护索引、铁律、速查、API 参考、安全边界、远程插件和 Quickstart，不能只改一页。
