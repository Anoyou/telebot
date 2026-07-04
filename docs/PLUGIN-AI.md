# TelePilot 插件 AI facade

`ctx.ai` 已作为第三方插件可用的受控文本 AI facade。插件需要在 `plugin.json` 和 `manifest.py` 中声明 `permissions=["ai_text"]`，运行时才会注入 `ctx.ai`。

## Event Bus 主路径写法

```python
async def on_event(self, ctx, payload):
    message = payload["message"]
    chat = payload["chat"]
    chat_id = message.get("chat_id") or chat["id"]
    reply_to = message.get("message_id")

    if ctx.ai is None:
        return [{
            "type": "send_message",
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "text": "本插件需要 ai_text 权限",
        }]

    providers = await ctx.ai.list_providers()
    result = await ctx.ai.complete(
        system="你是一个简洁助手。",
        user=message.get("text") or "总结这段内容",
        provider_tag="chat",
        max_tokens=512,
        timeout_seconds=30,
    )
    return [{
        "type": "send_message",
        "chat_id": chat_id,
        "reply_to_message_id": reply_to,
        "text": result.text,
    }]
```

也可以在交互入口中调用 `ctx.messages.send(...)` 缓存同等标准动作。最终版插件不应把 `event.edit(...)` 作为公共群互动或高频交互的主输出路径；命令式 `event.edit(...)` 只适合管理员命令兼容示例。

## Provider 选择

- `provider_tag`：推荐写法。按用途标签选择 provider，平台会在可用 provider 中挑选成本优先的匹配项。
- `provider`：需要固定 provider 时可传 provider id 或 provider name。
- `tag` / `tags`：兼容别名，已 deprecated；新插件请使用 `provider_tag`。

## Quota 与脱敏

- `ctx.ai.complete()` 复用平台 LLM Provider 池、fallback 链、账号级预算和 usage 记录。
- 插件传入的 `max_tokens` 与超时会被平台上限收紧，不能绕过账号配额。
- `ctx.ai.list_providers()` 只返回脱敏元数据，例如 provider 名称、默认模型、标签和成本层级。
- 插件不会拿到 `api_key_enc`、明文 API Key、`base_url` 或代理 URL。
- 不要在插件日志里记录用户完整隐私输入或模型完整输出；需要排障时只记录长度、截断摘要或 request id。

## 示例

完整最小示例见 `examples/plugins/with_ai/`。CI 只导入示例并校验 manifest / plugin 元数据，不会执行真实 AI 请求。

## 配额限制

平台从 `system_setting` 的 `plugin_ai_quota` 读取插件 AI 配额配置。示例：

```json
{
  "per_minute_tokens": 20000,
  "daily_tokens": 200000,
  "plugins": {
    "sum": {
      "per_minute_tokens": 5000,
      "daily_tokens": 50000
    }
  }
}
```

- `per_minute_tokens` 是每分钟 token 软上限，`daily_tokens` 是自然日 token 软上限。
- `plugins.{key}` 可覆盖单个插件的全局配置，例如上面的 `sum`。
- 任一限制设为 `0` 表示不限制。
- 超限时，插件会收到 `AIQuotaError`；平台同时写入一条 `LLMUsage(success=False, error_type="plugin_quota_exceeded")`，可在 Usage 页排查。
- Redis 不可用时会降级为 DB 检查，但并发预扣保护会暂时关闭；生产环境建议保留 Redis 可用性监控。
- token 估算是软上限：当前按 UTF-8 字节数 `// 4` 粗估，中文场景通常会偏低 1.5-2x，并发尖峰也可能瞬时越限。
- 跨午夜的请求按 acquire 当时所属的自然日记账，软上限场景误差可接受。
