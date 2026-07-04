# TelePilot 插件 HTTP facade

本文是当前维护的 `ctx.http` facade 参考；`ctx.ai` 的完整说明仍见 [PLUGIN-AI.md](./PLUGIN-AI.md)。

## 4. PluginContext

```python
@dataclass
class PluginContext:
    account_id: int
    feature_key: str
    config: dict           # 当前账号的插件配置
    rules: list            # 规则列表
    client: Any | None     # 受控客户端 facade；新插件不要作为主动发送主路径
    messages: Any | None   # MessageOps facade；发送/编辑/删除/按钮/Inline 主路径
    http: Any | None       # HTTP facade；需要 external_http + allowed_hosts
    ai: Any | None         # AI facade；需要 ai_text
    engine: Any | None     # RateLimitEngine；安装型插件通常为 None
    redis: Any | None      # redis.asyncio.Redis；安装型插件通常为 None
    log: Callable          # 日志函数
    scheduler: Any         # 平台调度器 facade
    generation: int        # generation guard 计数

    # 工具方法
    async def conversation(self, peer, timeout=30) -> Conversation:
        """创建与 bot 的对话会话。"""
```

注意：核心平台兼容代码可能拿到完整运行时能力；远程/本地/插件库安装型插件拿到的是受控上下文：`ctx.client` 是平台提供的客户端 facade，指令 handler 中传入的 `client` 参数与 `ctx.client` 同源，`ctx.engine` 和 `ctx.redis` 通常为 `None`，只能通过声明过的权限以及 `ctx.scheduler`、`ctx.http`、`ctx.ai`、`ctx.messages` 等 facade 使用平台能力。它用于收口常用操作和审计，不是公共插件市场式强沙箱。

### 4.0 受控 facade：ctx.http 与 ctx.ai

第三方插件可以使用两个受控 facade，但必须在 Manifest 中显式声明权限；未声明或策略不完整时字段会是 `None`：

- `ctx.http`：声明 `permissions=["external_http"]` 且填写 `allowed_hosts` 后注入。它限制协议、域名、超时、响应大小，并在发起请求前阻断 localhost/内网/链路本地地址。默认走账号代理；只有 Manifest 的 `http={"allow_direct": true}` 且账号配置请求 direct 时才允许直连。
- `ctx.ai`：声明 `permissions=["ai_text"]` 后注入。它复用 TelePilot 的 LLM Provider 池、fallback 链、账号级预算和 usage 记录；插件只能拿到脱敏 provider 元数据，不能读取 `api_key_enc`、`base_url` 或代理 URL。
- `ctx.ai.complete()` 推荐用 `provider_tag` 按用途选择 provider；`tag` / `tags` 是兼容别名且已 deprecated，新插件不要依赖它们作为主要入口。
- `ctx.ai.list_providers()` 可用于展示当前账号可见的脱敏 provider 摘要；更完整的 AI facade 说明见 `docs/PLUGIN-AI.md`。

Event Bus 主路径示例：

```python
async def on_event(self, ctx, payload):
    message = payload["message"]
    chat = payload["chat"]
    chat_id = message.get("chat_id") or chat["id"]
    reply_to = message.get("message_id")

    if ctx.http is None:
        return [{
            "type": "send_message",
            "send_via": ["interaction_bot", "userbot_reply"],
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "text": "本插件需要 external_http 权限和 allowed_hosts",
        }]

    response = await ctx.http.get("https://api.github.com/zen")
    preview = response.text.strip().replace("\n", " ")[:120]
    return [{
        "type": "send_message",
        "send_via": ["interaction_bot", "userbot_reply"],
        "chat_id": chat_id,
        "reply_to_message_id": reply_to,
        "text": f"HTTP {response.status_code}: {preview}",
    }]
```

管理员命令兼容示例仍可以 `event.edit(...)` 更新命令消息，但公共群互动、按钮回调、Inline 或付款确认插件应返回标准 action，或通过 `ctx.messages` 生成标准 action。

## allowed_hosts 匹配规则

`ctx.http` 只允许访问 Manifest 声明的 `allowed_hosts`。匹配语义与运行时 `PluginHTTP` 保持一致：

- `example.com` 只匹配 `example.com`。
- `*.example.com` 匹配一层子域名，例如 `api.example.com`，不匹配 `example.com` 或 `x.api.example.com`。
- `**.example.com` 匹配 `example.com` 以及任意层级子域名。

## SSRF 与响应限制

运行时只允许 `http` / `https` URL，并在连接前阻断这些目标：

- `localhost` 和 `*.localhost`。
- loopback、私网、链路本地、保留地址、组播地址、非 global IP。
- DNS 解析结果落到上述地址的 host。

响应体会流式计数，超过 `max_response_bytes` 会抛出 `PluginHTTPResponseTooLarge`，不会等完整 body 读完后才拒绝。

## 代理与 direct mode

默认网络模式是 `account_proxy`，会使用账号代理。只有 Manifest 显式声明 `http={"allow_direct": true}`，并且账号配置请求 `network_mode="direct"` 时，插件才可以直连；否则 direct 会被拒绝。
