# Webhook 订单通知示例

这是一个可以直接安装和修改的完整入站 Webhook 插件。它订阅页面默认提供的 `default` Hook，读取外部 JSON，并通过当前账号的 UserBot 把事件摘要发送到配置的 Telegram 会话。

## 使用步骤

1. 安装本目录并在目标账号上启用 `webhook_receiver`。
2. 在插件配置中填写 `target_chat_id`。私聊通常是正数，群聊是负数，超级群或频道通常以 `-100` 开头。
3. 打开「入站 Webhook」，选择同一个账号，复制 `default` 地址和 Token。
4. 从外部系统发送：

```bash
curl -X POST 'https://<telepilot-host>/api/webhooks/<account_id>/default' \
  -H 'X-TelePilot-Webhook-Token: <account_webhook_token>' \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"A-1001","status":"paid","amount":88}'
```

接口返回 `202` 表示账号 Worker 已确认接收。插件是否命中、消息是否成功发送，请到「日志」或 Trace 中查看。

若系统关闭了入站 Webhook 平台能力，公开 URL 会直接 `404`，Hook Token 与配置仍保留，重新开启后无需重建。

## 目录

```text
webhook_receiver/
├── fixtures/orders_paid.json
├── __init__.py
├── manifest.py
├── plugin.json
├── plugin.py
└── README.md
```

生产插件通常还应增加业务字段校验、幂等键、签名验证或来源校验。账号 Webhook Token 只负责保护 TelePilot 的公共入口，不等于第三方业务系统自身的签名验证。
