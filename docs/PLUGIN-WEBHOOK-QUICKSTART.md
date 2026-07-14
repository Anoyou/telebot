# 入站 Webhook Quickstart

入站 Webhook 把外部 HTTP 事件交给指定账号的插件。外部系统向 TelePilot 发送 JSON，账号 Worker 完成鉴权和事件归一化，再按插件的 `event_subscriptions` 匹配 `hook_key`。

如果你想直接运行一个完整示例，复制 [webhook_receiver](../examples/plugins/webhook_receiver/README.md)。它订阅页面默认提供的 `default`，读取订单 JSON，并把摘要发送到配置的 Telegram Chat ID。

## 1. 声明 Hook

在 `plugin.json` 和 `manifest.py` 中保持同一份订阅：

```json
{
  "event_subscriptions": [
    {
      "source": ["webhook"],
      "events": ["webhook"],
      "scope": "all_allowed_chats",
      "filters": {"hook_key": "default"}
    }
  ]
}
```

`filters.hook_key` 匹配单个入口，`filters.hook_keys` 可以匹配多个入口。也可以写成 `"triggers": {"webhook": "default"}`，运行时会归一化为相同的过滤条件。当前页面保证提供 `default`；使用其他名称前，必须先确认它已经出现在该账号的 Hook keys 列表中。

Webhook 没有 Telegram `chat_id`，因此使用 `scope="all_allowed_chats"`。插件如果需要向 Telegram 发送消息，应在自己的 `config_schema` 中要求管理员填写目标 Chat ID。

## 2. 处理事件

插件在 `on_event()` 中读取 `payload["webhook"]`：

```python
from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register


@register
class OrdersPlugin(Plugin):
    key = "orders_plugin"
    display_name = "订单通知"

    async def on_event(
        self,
        ctx: PluginContext,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        webhook = payload.get("webhook")
        if not isinstance(webhook, dict):
            return []

        body = webhook.get("body")
        if not isinstance(body, dict):
            return []

        target_chat_id = int(ctx.config["target_chat_id"])
        return [
            {
                "type": "send_message",
                "chat_id": target_chat_id,
                "text": f"订单 {body.get('order_id')}：{body.get('status')}",
                "parse_mode": "plain",
            }
        ]
```

常用字段：

| 字段 | 含义 |
| --- | --- |
| `payload["webhook"]["hook_key"]` | 当前入口名称，例如 `default` |
| `payload["webhook"]["body"]` | 解析后的 JSON 对象、数组或文本 |
| `payload["webhook"]["headers"]` | 平台保留的白名单请求头 |
| `payload["webhook"]["received_at"]` | TelePilot 接收时间 |
| `payload["trace_id"]` | 本次事件 Trace ID，可用于日志关联 |

不要从 `payload["message"]` 读取业务 JSON。它只是正文文本摘要，最长 2000 字符；业务字段应读取 `payload["webhook"]["body"]`。

## 3. 安装并启用

从仓库根目录执行：

```bash
backend/.venv/bin/python backend/scripts/tp_plugin.py check examples/plugins/webhook_receiver
backend/.venv/bin/python backend/scripts/tp_plugin.py register examples/plugins/webhook_receiver
```

然后在 Web 控制台中：

1. 为目标账号启用插件。
2. 打开插件配置，填写目标 Chat ID。
3. 打开「入站 Webhook」，选择同一个账号。
4. 确认 Hook keys 中出现 `default`，复制地址和 Token。

插件声明只负责匹配事件，不会自动创建新的 Hook key。当前页面默认提供 `default`，所以本示例直接复用它；如果 URL 中使用其他名称，该名称必须已经存在并启用，否则接口返回 `404`。

## 4. 从外部调用

```bash
curl -X POST 'https://<telepilot-host>/api/webhooks/<account_id>/default' \
  -H 'X-TelePilot-Webhook-Token: <account_webhook_token>' \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"A-1001","status":"paid","amount":88}'
```

不需要登录 Cookie、`X-CSRF-Token` 或 `X-Requested-With`。入站接口使用账号 Webhook Token 鉴权，默认只接受 `X-TelePilot-Webhook-Token` 请求头。

成功响应示例：

```json
{
  "account_id": 1,
  "hook_key": "default",
  "delivered": true,
  "body_size": 54
}
```

HTTP `202` 表示账号 Worker 已确认接收，不表示插件动作已经成功。插件命中、执行和 Telegram 发送结果应在「日志」、Trace 或消息链路动作记录中确认。

## 5. 常见错误

| 状态 | 错误码 | 处理方式 |
| --- | --- | --- |
| `401` | `WEBHOOK_TOKEN_INVALID` | 检查账号是否选对，Token 是否放在请求头；重置后旧 Token 会立即失效 |
| `404` | `WEBHOOK_HOOK_NOT_FOUND` | 确认插件已为该账号启用，订阅的 `hook_key` 与 URL 一致 |
| `413` | `WEBHOOK_BODY_TOO_LARGE` | 请求正文不得超过 64 KiB |
| `429` | `WEBHOOK_RATE_LIMITED` | 按 `Retry-After` 退避，不要立即并发重试 |
| `503` | `WEBHOOK_WORKER_OFFLINE` | 确认账号 Worker 在线且未暂停 |
| `503` | `WEBHOOK_DELIVERY_FAILED` | 查看 Redis、Worker 和后端日志 |

请求进入后没有消息时，依次检查：账号是否选对、插件是否启用、Hook key 是否匹配、目标 Chat ID 是否配置、账号是否能向目标会话发消息。命中调试页面目前面向 Telegram 消息模拟，Webhook 的真实执行结果以 Trace 和日志为准。

## 6. 生产安全

- 只通过请求头传递账号 Webhook Token，避免 Token 进入 URL、反向代理和访问日志。
- 公网入口使用 HTTPS。怀疑 Token 泄漏时，在页面重置 Token，并同步更新所有调用方。
- Webhook Token 只鉴权到 TelePilot 账号。GitHub、支付平台等来源还应在插件中验证各自的签名、时间戳和重放保护。
- 会产生订单、发货、资金或权限副作用的插件需要业务幂等键。不要把一次 HTTP `202` 当成业务已经完成。
- 请求正文上限为 64 KiB；默认限流为每秒 2 次、每分钟 60 次、每小时 1000 次、每天 5000 次。

完整字段、请求头白名单和事件信封见 [插件 API 参考](./PLUGIN-API-REFERENCE.md#入站-webhook-事件)。
