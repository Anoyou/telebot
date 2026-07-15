# TelePilot 插件示例

本目录用于放置可维护的第三方插件示例。CI 只校验已经迁移到稳定公开 API 的示例，避免把历史写法或未合并接口误判为推荐模板。

## 最终版主模板

- `hello_ping`：入门最小示例，只订阅 `message`，只匹配纯文本 `ping`，只返回 `send_message` / `pong`。新开发者优先从它复制目录结构。
- `event_bus_demo`：最终版主模板，演示 `usage` / `event_subscriptions` / `capabilities.telegram_native_raw`、message/command/callback/inline/payment fixtures、`answer_inline_query` 与 `settlement`。
- `webhook_receiver`：入站 Webhook 完整示例，订阅 `default` Hook、读取标准 Webhook payload，并把订单摘要发送到配置的 Telegram Chat ID。

## Facade 兼容示例

- `with_http`：最小 HTTP facade 示例，演示 `manifest.py` 如何声明 `external_http`、`allowed_hosts`，以及管理员命令兼容入口如何通过 `ctx.http` 发起受控请求。它不是公共群互动插件的新模板。
- `with_ai`：最小 AI facade 示例，演示 `manifest.py` 如何声明 `ai_text`，以及管理员命令兼容入口如何通过 `ctx.ai.complete` / `ctx.ai.list_providers` 使用平台统一 LLM 池。它不是公共群互动插件的新模板。
- `with_ai_components`：AI 玩法组件示例，演示用 `QuizMaker` + `AnswerJudge` 组件跑一个最小问答局，并展示无可用 AI 时的题库降级与 `unsure` 保守分支。组件 API 见 `docs/PLUGIN-AI.md`。
- `with_interaction`：旧交互入口迁移桥，演示原命令与交互 Bot 入口并存时如何补齐 Event Bus 字段，并修正历史 `payload["message"]` 字段冲突。

## 暂不纳入 CI 的示例

- `translate`：历史示例，仍直接复用后端私有 LLM 链路。它保留作迁移参考，但不是新的第三方插件模板。
