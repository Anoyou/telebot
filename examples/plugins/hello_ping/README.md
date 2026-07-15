# hello_ping

这是 TelePilot 插件开发的最小入门示例。

- 只订阅 `message` 事件。
- 只匹配纯文本 `ping`。
- 只返回一条 `send_message` action，内容为 `pong`。
- 不依赖外部 HTTP、AI、真实 Telegram token 或原生事件对象。

安装后还不会运行；必须回到插件中心，在目标账号上启用后才会收到 Event Bus 投递。
