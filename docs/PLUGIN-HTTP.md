# TelePilot 插件 HTTP facade

本文是当前维护的 `ctx.http` facade 参考；`ctx.ai` 的完整说明仍见 [PLUGIN-AI.md](./PLUGIN-AI.md)。

## 4. PluginContext

完整字段表以 [PLUGIN-API-REFERENCE.md](./PLUGIN-API-REFERENCE.md#4-plugincontext) 为唯一来源。
HTTP 插件通常会用到 `ctx.http`、`ctx.storage`、`ctx.data_dir`、`ctx.log` 和 `ctx.account_id`；
需要安全展示群成员身份时使用 `ctx.identities`。不要在本页复制完整 dataclass，避免字段随运行时扩展后产生两份不同的参考。

核心平台兼容代码可能拿到完整运行时能力；远程、本地和插件库安装型插件拿到的是受控上下文。
`ctx.client` 是平台提供的客户端 facade；安装插件的 `ctx.redis` 是自动添加账号 + 插件命名空间、
方法受限的 `PluginRedisFacade`，不是原始 Redis client。新插件需要持久化普通状态时优先使用
`ctx.storage`，文件型数据写入 `ctx.data_dir`，不要自行拼完整 `plugin_store:` 前缀，也不要把运行数据写进插件代码目录。

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
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "text": "本插件需要 external_http 权限和 allowed_hosts",
        }]

    response = await ctx.http.get("https://api.github.com/zen")
    preview = response.text.strip().replace("\n", " ")[:120]
    return [{
        "type": "send_message",
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

DNS 检查是连接前的预解析，当前不会 pin 解析结果，也无法在连接建立后复核最终 socket peer。因此它不能完全防御恶意 DNS 服务器在预检与建连之间发生的 DNS rebinding。管理员应只放行运营方可信、DNS 控制权明确的精确 host，避免把用户可控子域或过宽的 `**.` 域名作为安全边界；对高风险出口还应在网络层限制容器访问内网和元数据地址。

## 代理与 direct mode

默认网络模式是 `account_proxy`，会使用账号代理。只有 Manifest 显式声明 `http={"allow_direct": true}`，并且账号配置请求 `network_mode="direct"` 时，插件才可以直连；否则 direct 会被拒绝。
账号显式绑定的代理若已删除、类型不受支持或配置无效，默认 `account_proxy` 下的 `ctx.http` 会在发出请求前报错，不会把该状态解释成 DIRECT。只有 Manifest 和账号配置共同明确选择 direct 时才会绕过账号代理，这属于显式授权，不是代理失败后的自动回落。Web 进程执行 `on_config_action` 与账号 worker 使用同一 fail-closed 语义；未声明 `external_http` 或没有 `allowed_hosts` 的纯本地配置动作不会查询代理，也不受账号代理状态影响。
