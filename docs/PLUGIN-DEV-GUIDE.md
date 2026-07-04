# TelePilot 插件开发指南（索引）

> 这是一页索引，不再承载完整正文。原来的开发指南已按主题拆分，代码层 API 仍叫 `Plugin` / `PluginContext`，产品文案统一称“插件”。

> 路线决策保留在这里：TelePilot 0.x 默认采用 **个人可信插件标准模式**。管理员安装并启用插件后，即视为信任该插件的业务逻辑；远程插件风险由管理员自行承担。平台不做公共插件市场式强沙箱，而是通过 `Manifest.permissions`、`ctx.client`、`ctx.http`、`ctx.ai`、`ctx.messages` 等 facade 收口常用能力，并保留频控、审计、急停、日志脱敏和 token/session 隔离。

> 如果未来要开放“任意第三方上传、未经人工审核”的公共市场，需要另行设计 subprocess/容器隔离、资源配额、文件系统/网络沙箱和供应链扫描。它不属于当前 0.x 默认方案，本文其余章节、示例、CI 和安全边界都按个人可信插件标准模式编写。

## 目录

- [5 分钟 Quickstart](./PLUGIN-QUICKSTART.md)
- [插件开发铁律](./PLUGIN-RULES.md)
- [完整 API 参考](./PLUGIN-API-REFERENCE.md)
- [插件概览](./PLUGIN-OVERVIEW.md)
- [HTTP facade](./PLUGIN-HTTP.md)
- [AI facade](./PLUGIN-AI.md)
- [远程插件](./PLUGIN-REMOTE.md)
- [安全边界](./PLUGIN-SAFETY.md)
- [速查表](./PLUGIN-CHEATSHEET.md)

## 0.48 收口速览

消息链路统一后，互动插件默认按这一套模型理解：

- 触发方式决定会话通道。带前缀命令开局，整段会话收发走 `userbot`；关键词、付款确认、按钮回调开局，整段会话收发走 `interaction_bot`；插件默认不感知通道，只有 `payout` 固定走 `userbot`。
- 新玩法优先写成一个 `on_interaction(ctx, entry_key, payload)` 入口，在同一个入口里按 `tp_event.type` 或 `payload["source"]["type"]` 处理 `command`、`keyword`、`payment_confirmed`、`message`、`callback_query`、`session_expired`。
- 单局状态优先写进 `session.data`，通过 `update_session` 持久化；不要再把游戏状态放进进程内全局字典、锁和自建超时任务。
- `ctx.messages.send/edit/payout(...)` 和标准 action 默认按 `parse_mode="plain"` 发送；只有显式写 `html` 时才启用 HTML，并先做 `html_escape()`。
- userbot 会话没有原生 inline 按钮能力。平台会把按钮降级成“回复序号选择”的文本面板，并把命中的回复合成为 callback 事件回投插件；强依赖按钮的入口应配合 `keyword_only` / `default_trigger_modes` 关闭命令触发。

## 读法

1. 新人先看 [5 分钟 Quickstart](./PLUGIN-QUICKSTART.md)，复制 `hello_ping` 跑通最小 Event Bus + MessageOps 插件。
2. 写真实插件前看 [插件开发铁律](./PLUGIN-RULES.md)，确认必须、禁止、推荐的边界。
3. 查字段、facade、标准事件信封、MessageOps、Trace 和生命周期时看 [完整 API 参考](./PLUGIN-API-REFERENCE.md)。
4. 需要理解个人可信插件标准模式、安装/启用/更新/卸载心智时看 [插件概览](./PLUGIN-OVERVIEW.md)。
5. 需要外部网络能力时看 [HTTP facade](./PLUGIN-HTTP.md)，需要 AI 能力时看 [AI facade](./PLUGIN-AI.md)。
6. 需要 Git 安装、`plugin.json`、Registry、发布检查时看 [远程插件](./PLUGIN-REMOTE.md)。
7. 需要权限、前缀、消息发送、并发和清理约束时看 [安全边界](./PLUGIN-SAFETY.md)。
8. 需要快速回忆字段名和常用模式时看 [速查表](./PLUGIN-CHEATSHEET.md)。

## 兼容说明

- 旧章节锚点已经不再提供。
- `docs/REMOTE-PLUGIN-GUIDE.md` 仍保留为兼容入口，但正文已指向新的远程插件文档。
- `docs/PLUGIN-AI.md` 保持独立。
- `interaction_trigger_modes`、`default_trigger_modes`、`callback_fast_ack` 属于消息链路统一阶段的入口约定；若运行环境还未合入对应 runtime/worker 分支，以当前实例实际行为为准。
