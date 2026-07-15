# event_bus_demo

最终版插件主模板：`usage`、`event_subscriptions`、`capabilities` 写在 `plugin.json`，`event_subscriptions` 与 `capabilities` 同步到 `MANIFEST`，运行时只读取标准事件信封。

覆盖事件：

- `message` / `command`：返回 `send_message`。
- `callback_query`：返回 `answer_callback`。
- `inline_query`：返回 `answer_inline_query`。
- `chosen_inline_result`：返回结构化 `result`。
- `payment_confirmed`：返回 `settlement`，普通 Bot 不执行转账。

`capabilities.telegram_native_raw` 只用于排障。插件必须先处理 `native_raw_meta.enabled=false` 的降级场景，不能把原生对象当业务主路径。
