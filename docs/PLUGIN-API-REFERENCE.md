# TelePilot 插件 API 参考

本文是当前维护的插件 API 参考，覆盖配置、派发、日志、前端集成、调试和示例。用户界面与开发文档统一使用“插件”指代可安装、可启停、可配置的扩展能力；历史代码字段名仍按兼容要求保留。

## 1. 三链路心智模型

新插件先判断消息从哪条链路进入，再看 Event Bus、Trace、MessageOps 这些内部契约：

| 链路 | 触发与输入 | 会话/发送语义 | 典型用法 |
| --- | --- | --- | --- |
| 裸直通 | userbot Telethon 实时事件；安装插件收到 `SandboxEvent` 权限包装 | 插件自行处理 userbot 能力 | 抢首响、极低延时 userbot 监听；不覆盖 interaction bot |
| UserBot 标准消息链路 | Event Bus、前缀命令、legacy `on_message`；可含 incoming/outgoing | 只有建立会话时才由 `session.channel=userbot` 路由后续普通收发 | 管理员命令、账号身份动作、第三方 Bot 消息监听 |
| Interaction Bot 标准消息链路 | Event Bus 自主订阅或旧规则会话 | 只有建立会话时才由 `session.channel=interaction_bot` 路由后续普通收发 | 高频群内互动、按钮、题面、普通会话提示 |

唯一例外：`payout`、收付款、发奖永远由 userbot 执行，不随会话通道切到 interaction bot。

必填顶层字段 `requires_platform_capabilities` 必须同时写在 `plugin.json` 与 `Manifest`，声明插件对 `ai` / `interaction_bot` / `webhooks` / `ledger` / `dispatch_debug` 平台模块的依赖；无需求时显式写 `[]`。它不能写在 interaction entry 或 event subscription 上。存量插件缺失时只挂 warning，新装或升级缺失时拒绝；启用叶只会自动点亮允许开启的枝，不会顶开值守预设或管理员强制关闭。完整规则见[远程插件：`requires_platform_capabilities` 声明](./PLUGIN-REMOTE.md#requires_platform_capabilities-声明)和[平台能力热插拔](./PLATFORM-CAPABILITIES.md)。

Event Bus、Trace、MessageOps 是两条标准消息链路的内部契约，不是第四种模式。两条标准消息链路都可使用标准事件信封，并通过 `ctx.messages` / 标准 action 输出；legacy hook 仍保留兼容语义：

```python
from app.worker.plugins.events import event_from_interaction_payload


async def on_event(self, ctx, payload):
    event = payload["tp_event"] if "tp_event" in payload else event_from_interaction_payload(payload)
    text = event.message.text or ""
    if "ping" not in text:
        return []
    return [{
        "type": "send_message",
        "chat_id": event.message.chat_id,
        "reply_to_message_id": event.message.message_id,
        "text": "pong",
    }]
```

旧 `on_message`、`on_command`、`interaction_entries`、旧平铺 payload 只作为迁移兼容说明出现，不再是公共群玩法或新插件的推荐主路径。

裸直通入口只给 userbot 使用：

```python
async def on_direct_message(self, ctx, event):
    if not match(event):
        return {"status": "ignored"}
    try:
        await handle(event)
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    return {"status": "consumed"}
```

这里的 `event` 不是 `payload`，也不会自动生成标准事件信封或解释 handler 返回的标准 action。安装插件收到的是保留实时字段读取能力的 `SandboxEvent`，嵌套按钮已净化；builtin 内部插件才保留真实 Telethon event。平台会记录直通路由与插件调用 Trace；显式 `ctx.messages` 动作和受支持的 `ctx.client` 操作仍进入统一执行或动作审计。它不接 interaction bot 事件；需要 Interaction Bot 按钮回调、Inline、付款确认、标准会话或完整事件回放时，改用标准消息链路。

账号二次开关只决定插件是否加入直通调度，优先级只决定尝试顺序。只有 `consumed` 会停止后续直通与普通链路；`ignored`、`failed`、异常和未返回结果都会继续调度并最终回退。兼容返回值为 `True → consumed`、`False/None → ignored`、`{"consume": true/false} → consumed/ignored`。

## 2. 标准会话链路与单入口模型

进入标准链路后，互动插件按“触发方式决定整段会话通道”理解；插件全程不需要感知或选择收发通道：

| 开局方式 | 会话通道（收 + 发） | 说明 |
| --- | --- | --- |
| UserBot 前缀命令 | `userbot` | 后续消息、继续追问、普通回复默认都走 userbot，不覆盖 interaction bot |
| 关键词 / 付款确认 / 按钮回调 | `interaction_bot` | 题面、按钮、编辑消息默认都走 interaction bot |
| `payout` | 固定 `userbot` | 不受会话通道影响，始终经 userbot 执行 |

推荐写法：

1. 用一个 `on_event(ctx, payload)` 覆盖 `command`、`keyword`、`payment_confirmed`、`message`、`callback_query`、`session_expired`。
2. 读取 `payload["tp_event"]` 或 `event_from_interaction_payload(payload)`，不要再围绕旧平铺字段写分支。
3. 单局状态保存在 `session.data`，变更后返回 `update_session`；Event Bus 订阅入口首次建局必须先返回 `start_session`，只有当前事件已带有效会话时才可直接 `update_session`。不要再依赖进程内全局状态才能继续游戏。
4. 普通发送类动作不要写 `send_via`，平台会继承 `session.channel`；只有跨通道公告、特殊管理消息或迁移桥兼容这类高级场景才显式覆盖。
5. userbot 会话里的按钮会降级为文本编号面板；玩家回复编号后，平台会把它合成为 `callback_query`，并在 `source.synthetic="text_button"` 标记来源。

`interaction_trigger_modes`、`default_trigger_modes`、`callback_fast_ack` 是当前运行时契约。插件发布前应在目标 TelePilot 版本上运行示例校验和真实账号 smoke test，不要为旧分支猜测兼容行为。

### 2.1 动作 × 执行体分工表

标准 action 会按入口进入三类执行体：E1 是 Event Bus 的 UserBot 直执行（[loader.py](../backend/app/worker/plugins/loader.py)），E2 是 Interaction Bot 的投递编排（[delivery.py](../backend/app/services/interaction/delivery.py)），E3 是 E2 调用的 UserBot RPC 动作面（[runtime.py](../backend/app/worker/runtime.py)）。下表是插件作者可依赖的边界；不是三个执行体都必须具备同一种能力。回归覆盖见 [三方 parity 矩阵](../backend/app/tests/test_interaction_executor_parity.py)。

| 动作/情形 | E1 Event Bus UserBot | E2 Interaction Bot delivery | E3 Worker RPC | 插件作者应知的契约 |
| --- | --- | --- | --- | --- |
| `send_message`、媒体、编辑、删除、置顶（`userbot_reply`） | 直接以账号身份执行 | 编排、审计后委派 | 实际 UserBot RPC | UserBot 会话中 inline 按钮降级为文本编号；不要依赖原始 `reply_markup`。 |
| `send_message`、媒体、编辑（`interaction_bot`） | 仅在该入口已具备 Bot token 时按 Bot API 发送 | Bot API 实际发送 | 不进入 | `reply_markup` 是 Interaction Bot 富消息能力；不要把它当 E3/UserBot 富消息能力。 |
| `send_rich_message` | 可走 UserBot 富消息路径，但不承诺 Bot API inline markup 等能力 | `interaction_bot` 时走 Bot API | `userbot_reply` 时实际执行 | 选择通道时以富消息能力边界为准，不能假定三端渲染相同。 |
| `click_callback_button` | 支持，以 UserBot 点击第三方 Bot 按钮 | 有意不代点，记录 `skipped/unsupported_send_via` | 不支持 | 这是 E1 专属能力，不是待修的不一致。 |
| `answer_callback` / `answer_inline_query` | 调用 Interaction Bot API | 调用 Interaction Bot API | 不进入 | 回答回调/Inline 必须视为 Bot API 动作，不能期待账号 UserBot RPC 代答。 |
| `payout` | 固定 UserBot 路径 | 编排、审计、补偿后委派 | 实际 UserBot RPC | 始终经过 T3 资金闸与补偿/幂等语义；`message_id=None` 不能安全关联时仍按既有边界放行。 |
| `start_session` | 内联创建/续用会话 | `apply` 仅记录控制动作，外层入口负责建会话 | 不进入 | Event Bus 需要先显式 `start_session`；关键词、付款等既有入口会按自己的已修流程预建会话。 |
| `update_session` | 更新已有会话 | 更新已有会话 | 不进入 | Event Bus 不会隐式建会话：裸 `update_session` 失败为 `session_not_found`，必须先 `start_session`。 |
| `result`、`end_session`、`close_session`、`no_session` | 控制类动作 | 控制类动作 | 不进入 | 会话结束/结果的外层编排归入口负责；这些动作不是普通 UserBot RPC。 |
| 未知 action type | `failed/unsupported_action` | `failed/unsupported_action` | RPC 拒绝为 `unsupported_action` | 未知动作会计入批处理 `failed`，不得把它当作可忽略的向前兼容。 |

限流拒绝统一记录为 `failed/rate_limited`；插件可据错误码和可选等待信息决定是否延迟重试，平台不会隐式重放。按 `rule_id` 隔离去重键的既有边界保持不变。

## 3. Plugin 基类（兼容层）

```python
class Plugin:
    # === 必须设置 ===
    key: str                          # 唯一标识，也是插件 key
    display_name: str                 # 显示名

    # === 可选配置 ===
    message_channels: set[str]        # 监听方向: {"incoming"} / {"outgoing"} / 二者都监听
    owner_only: bool = True           # 只影响 on_message；False 表示允许普通成员消息进入 on_message
    commands: dict = {}               # TG 内指令；只由本账号 outgoing 指令触发
    command_config_keys: set[str] = set()  # 这些配置变化后需要重载并重新注册指令
    description: str = ""             # 描述（用于帮助系统）

    # === 生命周期钩子 ===
    async def on_startup(self, ctx: PluginContext) -> None:
        """插件激活时调用一次。"""

    async def on_shutdown(self, ctx: PluginContext) -> None:
        """插件关停前调用一次。必须幂等。"""

    # === 事件处理 ===
    async def on_event(self, ctx: PluginContext, payload: dict) -> list[dict] | None:
        """Event Bus 主入口；新插件优先实现。"""

    async def on_interaction(
        self, ctx: PluginContext, entry_key: str, payload: dict
    ) -> list[dict] | None:
        """历史交互入口兼容桥。"""

    async def on_message(self, ctx: PluginContext, event) -> None:
        """历史消息事件回调。"""

    async def on_message_edited(self, ctx: PluginContext, event) -> None:
        """历史消息编辑回调；只有显式重写该方法的插件才会收到。"""

    async def on_command(self, ctx: PluginContext, cmd: str, args: list[str], event) -> bool:
        """指令派发回调。返回 True 表示已处理。"""
        return False
```

`commands` 是基类上的空字典占位。命令名来自配置时，请像完整示例那样在 `__init__` 或 `on_startup` 中赋值 `self.commands = {...}`；不要修改 `Plugin.commands` / `type(self).commands`，否则同一进程里其它账号实例可能共享到错误命令。

### 注册

```python
@register
class MyPlugin(Plugin):
    key = "my_plugin"
    ...
```

`@register` 装饰器把插件类注册到全局表，loader 通过 key 查找。

---

## 4. PluginContext

```python
@dataclass
class PluginContext:
    account_id: int
    feature_key: str
    config: dict           # 当前账号的插件配置
    account_config: dict   # 原始账号级配置（不含 schema/global 合并值）
    rules: list            # 规则列表
    client: Any | None     # 受控客户端 facade；新插件不要作为主动发送主路径
    messages: Any | None   # MessageOps facade；发送/编辑/删除/按钮/Inline 主路径
    identities: Any | None # 群内安全公开身份 facade
    http: Any | None       # HTTP facade；需要 external_http + allowed_hosts
    ai: Any | None         # AI facade；需要 ai_text
    engine: Any | None     # RateLimitEngine；安装型插件通常为 None
    redis: Any | None      # builtin 可为原始 Redis；安装插件为 PluginRedisFacade
    storage: Any | None    # PluginStorage；按账号和插件隔离的持久化 KV facade
    data_dir: Path | None  # 插件独享的持久化文件目录；更新插件代码时不会被覆盖
    log: Callable          # 日志函数
    scheduler: Any         # 平台调度器 facade
    generation: int        # generation guard 计数
    account_proxy_url: str | None  # 账号代理 URL；只供平台 facade 组装，不应写入日志

    # 简单模式命令运行字段
    event: Any | None
    args: list[str]
    command: str

    # 工具方法
    async def conversation(self, peer, timeout=30) -> Conversation:
        """内部/真实 TelegramClient 兼容入口；普通安装插件不可依赖。"""
```

注意：核心平台兼容代码可能拿到完整运行时能力；远程/本地/插件库安装型插件拿到的是受控上下文：`ctx.client` 是 `SandboxClient` facade，指令 handler 中传入的 `client` 参数与 `ctx.client` 同源。安装型插件的 `ctx.redis` 是 `PluginRedisFacade`，会自动添加 `plugin_store:{account_id}:{plugin_key}:` 前缀，并拒绝 `keys`、`scan`、`eval`、`pipeline`、`pubsub` 等操作；builtin 兼容代码才可能拿到原始 Redis client。新插件的普通状态仍应优先使用 `ctx.storage`。`ctx.engine` 只供核心 builtin 兼容代码直接依赖，安装型插件通常为 `None`。受控上下文用于收口常用操作和审计，不是公共插件市场式强沙箱。

插件需要 SQLite、索引文件或其他文件型持久化时，必须写入 `ctx.data_dir`，不要写进 `Path(__file__).parent`、插件安装目录或仓库源码目录。TelePilot 更新插件时会整体替换代码目录，而 `ctx.data_dir` 位于持久化插件卷的 `_data/<plugin_key>/` 下，不随代码更新删除。插件仍需使用 `ctx.account_id` 隔离账号数据；若一个数据库文件服务多个账号，表结构必须包含账号隔离键。

为兼容旧插件，更新器会在替换代码目录前迁移插件根目录的 `*.sqlite3` 和顶层运行态 `*.json`（排除 `plugin.json`），并在新目录保留兼容链接；已有 `ctx.data_dir` 文件不会被旧文件覆盖。这个迁移只负责旧数据过渡，不扫描嵌套目录，也不能替代新代码直接使用 `ctx.data_dir`。

### 4.0 受控 facade：ctx.http 与 ctx.ai

第三方插件可以使用两个受控 facade，但必须在 Manifest 中显式声明权限；未声明或策略不完整时字段会是 `None`：

- `ctx.http`：声明 `permissions=["external_http"]` 且填写 `allowed_hosts` 后注入。它限制协议、域名、超时、响应大小，并在发起请求前阻断 localhost/内网/链路本地地址。默认走账号代理；账号代理若已删除、类型不受支持或配置无效，Web 配置动作和 worker 都会在网络请求前失败。只有 Manifest 的 `http={"allow_direct": true}` 与账号配置共同请求 direct 时才允许直连，这属于显式授权而非失败回落；未注入 `ctx.http` 的纯本地配置动作不会查询代理。
- `ctx.ai`：声明 `permissions=["ai_text"]` 后注入。它复用 TelePilot 的 LLM Provider 池、fallback 链、账号级预算和 usage 记录；插件只能拿到脱敏 provider 元数据，不能读取 `api_key_enc`、`base_url` 或代理 URL。显式绑定缺失、已停用或无效代理的 Provider 会从插件路由排除，不会自动改成 DIRECT。
- `ctx.ai.complete()` 推荐用 `provider_tag` 按用途选择 provider；`tag` / `tags` 是兼容别名且已 deprecated，新插件不要依赖它们作为主要入口。可选 `route="fixed"|"tag"|"auto"` 显式声明路由模式（留空时按旧参数推断，向后兼容）；返回结果的 `routing` 字段是脱敏路由摘要（模式 / provider / 生效模型 / 命中 tag / 协议 / 身份 / 是否 fallback），不含 key、base_url、代理或内部分类器细节。插件不能指定 UA、身份、密钥、代理、内部分类器或全局 fallback。
- `ctx.ai.stream_complete()` 返回 Provider 原生文本 delta 的异步迭代器，支持 Chat Completions、Responses 与 Anthropic Messages；不拆分完整响应、不在已输出部分文本后切 Provider。上游忽略 `stream=true` 而返回普通 JSON 时，同一次请求的完整文本作为一个块产出，不会再次调用模型。调用方必须消费到迭代器自然结束；取消、提前关闭、超时或异常按已发起调用保守结算。需要跨 Provider 的完整响应 fallback 时使用 `complete()`。
- `ctx.ai.run_agent()` 需要独立 `ai_agent` 权限，并同时要求 `capabilities.agent_tools.enabled=true`、manifest `agent_tools[]` 声明和调用方传入同名 handler。平台限制轮数、工具数、重复调用、token 与总超时；只读工具可并行，副作用工具串行。同样支持 `route="fixed"|"tag"|"auto"`，且 Agent 路由会预先排除没有已启用模型的 provider（无法支撑 tools 调用）。
- `ctx.ai.list_providers()` 可用于展示当前账号可见的脱敏 provider 摘要；更完整的 AI facade 说明见 `docs/PLUGIN-AI.md`。

标准链路示例：

```python
async def on_event(self, ctx, payload):
    message = payload["message"]
    chat_id = message.get("chat_id") or (payload.get("chat") or {}).get("id")
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

管理员命令兼容示例可以继续用 `event.edit(...)` 更新命令消息；标准公共互动插件应优先返回标准 action 或通过 `ctx.messages` 缓存标准 action。

### 4.1 持久化 facade：ctx.storage

`ctx.storage` 是 `PluginStorage` facade，用于插件自己的持久化 key-value 状态。loader 使用原始 Redis 后端以及 `PluginContext.account_id`、`PluginContext.feature_key` 单独构造它，不会再套一层安装插件可见的 `ctx.redis` facade，因此不会产生双重前缀。它与 `PluginRedisFacade` 使用相同的 `plugin_store:{account_id}:{plugin_key}:` 命名空间格式，所以同一插件在不同账号之间、同一账号的不同插件之间不会串数据。

API 签名：

| 方法 | 签名 | 返回 | 说明 |
|------|------|------|------|
| `available` | `ctx.storage.available` | `bool` | 是否挂载了 Redis 后端 |
| `get` | `await ctx.storage.get(key, default=None)` | `Any` | 读取 JSON 值；缺失、Redis 不可用或 JSON 不可读时返回 `default` |
| `set` | `await ctx.storage.set(key, value, *, ttl=None)` | `bool` | 写入 JSON 值；`ttl` 为可选秒数，必须大于 0；Redis 不可用时返回 `False` |
| `delete` | `await ctx.storage.delete(*keys)` | `int` | 删除一个或多个用户 key，返回删除数量 |
| `incr` | `await ctx.storage.incr(key, amount=1, *, ttl=None)` | `int | None` | 整数递增；Redis 不可用时返回 `None` |
| `get_all` | `await ctx.storage.get_all()` | `dict[str, Any]` | 返回当前账号和插件命名空间下全部可解析 JSON 值 |
| `items` | `await ctx.storage.items()` | `dict[str, Any]` | `get_all()` 别名 |

`key` 会先转成去首尾空白的字符串，空 key 会抛 `ValueError`。`value` 通过 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 序列化，不能 JSON 序列化的值会在 `set` 时抛出原始序列化错误。

`ttl` 只支持 `set` 和 `incr`，单位为秒，可传 `int` / `float` / 可转整数的值；`None` 表示不设置过期，`<= 0` 或不可转整数会抛 `ValueError`。`get_all()` / `items()` 只扫描当前命名空间，过期或不可解析 JSON 的值会被跳过。

`incr(ttl=...)` 的窗口语义要特别注意：Redis 后端优先使用 `INCRBY` / `INCR` 做递增，然后单独调用 `EXPIRE` 刷新 TTL，所以 TTL 是“每次递增都续期”的滑动窗口，不是固定窗口。递增和设置过期不是一个事务；如果进程在两条命令之间崩溃，可能留下已递增但未设置过期的 key。若 Redis facade 没有 `incrby` / `incr`，实现会退回 `get` + `set`，该退回路径本身也不是原子递增。需要 NX、CAS、锁或严格原子计数时，不要把 `ctx.storage` 当分布式锁使用，应交给平台会话/结算链路或官方受控低层实现。

不使用时没有额外开销：运行时只是在上下文里挂载 facade；插件不调用 `ctx.storage`，就不会触发 Redis 读写或扫描。

推荐写法：

```python
round_key = f"round:{chat_id}"

state = await ctx.storage.get(round_key, default={})
await ctx.storage.set(round_key, {"answer": 24, "active": True}, ttl=3600)

attempts = await ctx.storage.incr(f"attempts:{chat_id}:{user_id}", ttl=60)
if attempts is not None and attempts > 5:
    return [{"type": "send_message", "text": "尝试太频繁，请稍后再试。"}]
```

### 4.2 可用上下文与访问方式（PluginContext Contract）

插件请只从 `PluginContext` 读取运行时信息，不要跨层 import worker 私有实现。

| 字段 | 访问方式 | 说明 |
|------|----------|------|
| `ctx.account_id` | `ctx.account_id` | 当前账号 ID（账号级隔离边界） |
| `ctx.feature_key` | `ctx.feature_key` | 当前插件 feature key |
| `ctx.config` | `ctx.config.get("k")` | 插件配置（账号/全局已合并后的可见配置） |
| `ctx.account_config` | `ctx.account_config.get("k")` | 原始账号级配置；通常优先读已合并的 `ctx.config` |
| `ctx.rules` | 遍历 `ctx.rules` | 当前账号 + 当前插件已启用规则 |
| `ctx.client` | 高级兼容场景只读或受控调用 | UserBot 客户端 facade；新插件不要用它作为主动发送主路径，消息输出优先使用 `ctx.messages` 或标准 action |
| `ctx.engine` | `await ctx.engine.acquire(...)` | 仅核心 builtin 兼容代码可直接依赖；安装型插件通常为 `None` |
| `ctx.redis` | `await ctx.redis.get("key")` | 安装插件拿 `PluginRedisFacade`，自动添加账号 + 插件前缀；不支持 `keys/scan/eval/pipeline/pubsub` 等操作。builtin 才可能拿原始 Redis；普通状态优先用 `ctx.storage` |
| `ctx.storage` | `await ctx.storage.get("k")` / `await ctx.storage.set("k", value, ttl=3600)` | 推荐的插件持久化 facade；按账号 + 插件自动命名空间隔离 |
| `ctx.log` | `await ctx.log("info", "...", **detail)` | 运行日志写入器 |
| `ctx.scheduler` | `ctx.scheduler.register(job_id, schedule, callback, *, replace=True)` / `ctx.scheduler.unregister(job_id)` | 调度 facade（按权限/能力边界开放） |
| `ctx.http` | `await ctx.http.get(url, params={...})` / `await ctx.http.post(url, json={...})` | 安全 HTTP facade；第三方插件需声明 `external_http` + `allowed_hosts` |
| `ctx.ai` | `await ctx.ai.complete(system="...", user="...")` | 文本 LLM facade；第三方插件需声明 `ai_text` |
| `ctx.ai` | `await ctx.ai.run_agent(..., handlers={...})` | 有界工具调用；第三方插件需声明独立 `ai_agent` 与工具双白名单 |
| `ctx.messages` | `await ctx.messages.send(...)` / `apply([...])` | MessageOps facade；具体 helper 取决于交互缓冲或常驻/命令/调度上下文，见下方矩阵 |
| `ctx.identities` | 通常通过 `resolve_public_sender_identity(ctx, ...)` 间接使用；UserBot 专用场景可调用 `resolve_userbot(...)` | 平台注入的群内安全身份 facade；只返回标签、姓名、管理员状态和解析状态，不向插件开放成员目录 |
| `ctx.conversation(...)` | `async with ctx.conversation(peer)` | 仅真实 `TelegramClient` 的 builtin/内部兼容场景可靠；普通安装插件的 `SandboxClient` 不支持其 handler/raw MTProto 要求 |

### 4.3 `ctx.messages` 上下文 × 方法矩阵

上表的 `send_photo/edit_caption` 示例只适用于交互缓冲 facade，不能据此推断所有入口都有同名 helper。`ctx.messages` 会按入口注入不同的 Python 类：

| 上下文 | facade | 可直接调用的方法 |
| --- | --- | --- |
| Event Bus / `on_interaction` 的当前交互入口 | 缓冲 facade | `apply`、`send`、`send_rich`、`send_photo`、`send_file`、`edit`、`edit_rich`、`edit_caption`、`delete`、`pin`、`answer_callback`、`answer_inline_query`、`payout`、`update_session`、`read_saved_message_id`、`delete_saved_message_id`；UserBot 执行链路才允许 `click_callback_button` |
| 常驻上下文、插件命令、legacy `on_message` / `on_message_edited`、裸直通、scheduler/后台 callback | live facade | `apply`、`send`、`send_rich`、`edit`、`delete`、`pin`、`answer_callback`、`click_callback_button`、`answer_inline_query`、`payout`、`read_saved_message_id`、`delete_saved_message_id` |

live facade 当前没有 `send_photo`、`send_file`、`edit_rich`、`edit_caption`、`update_session` helper；直接调用会得到属性不存在错误。后台需要发送媒体或编辑 caption 时，构造文档化的标准 action 后调用 `await ctx.messages.apply([action], entry_key="...")`。`update_session` 只在拥有当前交互会话的入口中使用，不把后台任务伪装成会话更新。

`apply(actions, entry_key=None)` 是公共 live 执行入口：它会规范化 action、补 trace context 并立即交给 UserBot/Event Bus 动作执行器；返回 `None`，部分动作失败会写插件日志，不会把失败 action 列表作为返回值。交互缓冲 facade 也提供 `apply` 供延迟或即时提交，但 Interaction Bot 入口仍拒绝 `click_callback_button`。

`read_saved_message_id(key)` 与 `delete_saved_message_id(key)` 只访问平台拥有、按账号和插件隔离的 message-id 命名空间，不开放原始 Redis。普通纯 `BufferedMessageOps` 没有 live 存储时读取返回 `None`、删除返回 `False`；平台注入的交互 adapter 和 live facade 会转发到真实存储。

### 4.4 权限边界与禁止事项

1. 第三方插件必须遵循 `manifest.permissions` 最小授权，未声明的客户端能力不可调用。
2. 第三方插件不得假设 `ctx.engine` 恒可用；需要状态持久化时优先使用 `ctx.storage`。确需 Redis 的 NX、hash/list/set 等已开放语义时才访问 `ctx.redis`，并接受自动命名空间和方法白名单；不要假设它是原始 Redis client。
3. 禁止通过插件绕过账号边界：不要读写其他账号配置、规则、会话状态。
4. 禁止在插件中执行系统级/运维级动作（如重启进程、安装/卸载插件、修改权限模型）。
5. 禁止依赖 worker 私有实现或 monkey patch 运行时对象来“扩权”。
6. 禁止把敏感凭据直接打到日志；`ctx.log` 只记录最小必要信息。

### 4.5 配置/账号/运行时数据访问建议

1. 配置：通过 `ctx.config` 读取；按 `config_schema` 的 `level` 设计字段，不自行拼接跨账号配置。
2. 账号：通过 `ctx.account_id` 做所有业务隔离键；插件持久化状态优先放进 `ctx.storage`，由平台按账号 + 插件隔离。
3. 运行时：管理员命令兼容场景可使用 `ctx.client` 已开放的方法和 `ctx.scheduler`；普通安装插件不要依赖 `ctx.conversation`。公共群互动、Interaction Bot callback、Inline、付款确认和后台通知优先使用 `ctx.messages` 或标准 action。
4. 日志：统一用 `ctx.log`，并在 `detail` 里带结构化字段（如 `chat_id`、`action`）。
5. 兜底：对可选能力（`engine`/`storage`/`redis`）做 feature-detection，保证第三方插件在受限上下文也能安全降级。

最小示例见：[docs/examples/plugin_context_minimal.py](./examples/plugin_context_minimal.py)。

---

## 5. Manifest 元数据

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | str | 唯一标识，与 Plugin.key 一致 |
| `display_name` | str | 显示名称 |
| `version` | str | 语义化版本（如 `1.0.0`） |
| `author` | str | 作者 |
| `description` | str | 功能描述，用于帮助系统 |

### 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `permissions` | list | 权限声明，默认 `[]`；第三方插件必须显式声明需要的能力 |
| `config_schema` | dict | JSON Schema，有配置的插件必须写 |
| `requires_features` | list | 依赖的其他插件 key |
| `min_telepilot_version` | str | 最低 TelePilot 版本要求，远程插件建议填写 |
| `min_telebot_version` | str | 旧字段名，仅作为兼容别名保留，新插件不要再新增 |
| `category` | str | `interactive` / `automation` / `utility`，只决定展示分组 |
| `event_subscriptions` | list | 标准链路订阅声明，描述插件想从 Event Bus 接收哪些事件 |
| `capabilities` | dict | 高风险能力声明，例如 `telegram_native_raw` |
| `agent_tools` | list | Agent 工具名、说明、object JSON Schema、`read_only` 与 `strict`；必须配合 `ai_agent` 权限和 `capabilities.agent_tools` |
| `agent_keywords` | list | 可选；暴露给系统助手时的路由关键词（最多 6 个） |
| `strict_trace` | bool | 是否要求路由投递层常驻全链路 trace；默认 `false`，资金类 / `payout` 插件建议开启 |

### 暴露工具给系统助手（System Agent）

插件自用的 `ctx.ai.run_agent()` 与系统助手工具注册表是两套路径。若希望**系统助手**也能调用插件工具，在对应 `agent_tools[]` 条目上增加：

```json
"expose": ["system_agent"]
```

约束（第一期）：

1. **只读**：`read_only` 必须为 `true`（或缺省）。写语义条目会被拒绝暴露，并在安装/刷新时写警告日志。
2. **命名**：系统助手侧工具名为 `plugin_{plugin_key}.{tool_name}`，例如 `plugin_lottery_plus.list_recent_rounds`。
3. **数量**：每个插件最多暴露 5 个工具。
4. **执行**：主进程经 worker IPC 调用；worker 侧需提供 handler：
   - 插件实例方法 `system_agent_{tool_name}(arguments, ctx)`，或
   - `system_agent_tool(name, arguments, ctx)`，或
   - `register_system_agent_tool_handler(plugin_key, name, handler)` 显式注册。
5. **安全**：调用前校验账号已启用该插件（`AccountFeature`）；结果文本经 `mark_external_text` 防注入；超时 10s 返回结构化错误且 `business_changed=false`。
6. **权限**：仍需 `permissions` 含 `ai_agent`，且 `capabilities.agent_tools.enabled=true`。

参考实现：`lottery_plus` 的只读工具 `list_recent_rounds`（近期开奖轮次摘要）。

### 完整示例

```python
from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="my_plugin",
    display_name="我的插件",
    version="1.0.0",
    author="your_name",
    description="插件功能描述",
    category="interactive",
    permissions=["send_message", "edit_message", "read_chat"],
    event_subscriptions=[
        {
            "events": ["message", "command", "callback_query"],
            "source": ["userbot", "interaction_bot"],
            "scope": "all_allowed_chats",
        }
    ],
    capabilities={
        "telegram_native_raw": {
            "enabled": False,
            "reason": "默认只读取标准事件信封。",
        }
    },
    strict_trace=False,
    config_schema={
        "type": "object",
        "properties": {
            "api_key": {
                "type": "string",
                "title": "API Key",
                "level": "global",
            },
            "target_chat": {
                "type": "string",
                "title": "目标聊天 ID",
                "level": "account",
            },
        },
    },
    requires_features=[],
)
```

### config_schema 配置规范

`config_schema` 遵循 JSON Schema 规范，额外支持 `level` 字段控制配置的作用域：

| level | 作用域 | 存储位置 | 说明 |
|-------|--------|---------|------|
| `global` | 全局（所有账号共享） | `plugin_config` | API Key、通用参数等 |
| `account` | 单个账号 | 普通/`single` 模式写入 `account_feature.config`；`rules` 模式的每条规则写入 `rule.config` | 聊天 ID、行为开关等 |
| （不填） | 默认 account | 与 `account` 相同 | 向后兼容 |

**优先级：** 账号级配置 > 插件全局配置 > config_schema 中的 default

**前端渲染：** `config_schema["x-ui-mode"]` 决定插件配置入口：
- `rules` → 规则配置独立页，适合多条规则 CRUD 和 dry-run；不表示旧运行时规则驱动主路径
- `single` → 单配置对象独立配置页；没有专属页面的轻量插件也应按通用独立配置页处理
- `platform` → 平台基础能力页，不混在普通插件列表里
- `schema` → 兼容旧插件的别名；不再代表“Schema 弹窗”类，按通用单配置独立页读取字段
- `level: global` 的字段 → 全局配置区（所有账号共享）
- `level: account` 的字段 → 账号配置区（按账号隔离）
- 无 level 的字段 → 默认按账号隔离

#### 通用配置控件

通用独立配置页支持声明式控件，插件不要为了某个字段新增 TelePilot 前端特例。常用扩展字段：

| 声明 | 适用字段 | 效果 |
|------|----------|------|
| `x-ui-widget: "textarea"` | `string` | 多行文本 |
| `x-ui-widget: "llm-provider-select"` | `string` | 选择当前 TelePilot AI Provider |
| `x-ui-widget: "llm-model-select"` | `string` | 选择 Provider 下的模型；用 `x-ui-provider-field` 指向 Provider 字段 |
| `x-ui-widget: "dynamic-select"` | `string` | 从同一配置对象的动态选项数组中单选；用 `x-ui-options-field` 指向 `[{value,label}]` 字段 |
| `x-ui-widget: "multi-select"` | `array` + `items.enum` | 多选列表 |
| `x-ui-widget: "list-select"` | `string` + `enum` | 列表式单选 |
| `x-ui-widget: "config-list"` | `array` + `items.type="object"` | 多组配置行，支持添加、编辑、复制、删除、启停、排序 |
| `x-ui-widget: "allowed-peer-multi-select"` | `array` + `items.type="integer"` | 从当前账号“允许会话”中选择群聊/频道，保存为 Chat ID 数组；适合插件群聊白名单 |
| `x-ui-placeholder` | `string` / `textarea` | 输入框浅色占位内容；与实际 `default` 分离，适合展示内置提示词但让留空继续使用内置值 |
| `x-ui-hidden: true` | 任意字段 | 不在 UI 渲染，但仍保留在表单值和保存链路中 |

`config-list` 适合“多组配置，每组一行”的常见体验。支持这些元数据：

| 字段 | 说明 |
|------|------|
| `x-ui-summary` | 行摘要模板，支持 `{field}` 和 `{items.length}` 这类简单路径 |
| `x-ui-title-field` | 行标题字段，例如 `remark` / `title` / `name` |
| `x-ui-description-field` | 行描述字段，例如 `url` / `description` |
| `x-ui-enabled-field` | 启停开关字段，通常为 `enabled` |
| `x-ui-reorderable` | 是否允许拖拽和上下移动，默认允许 |
| `x-ui-add-label` | 添加按钮文案 |

示例：

```python
"knowledge_bases": {
    "type": "array",
    "title": "题库",
    "x-ui-widget": "config-list",
    "x-ui-summary": "{questions.length} 题 · {summary}",
    "x-ui-title-field": "title",
    "x-ui-description-field": "url",
    "x-ui-enabled-field": "enabled",
    "items": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "title": "启用", "default": True},
            "title": {"type": "string", "title": "标题"},
            "url": {"type": "string", "title": "URL"},
            "summary": {"type": "string", "title": "摘要", "x-ui-widget": "textarea"},
            "questions": {"type": "array", "title": "题目 JSON", "items": {"type": "object"}},
        },
    },
}
```

#### 配置页动作

插件可以在 `Manifest.config_actions` 或 `config_schema["x-config-actions"]` 声明配置页按钮。前端按 `placement` 放置按钮，当前推荐 `field:<字段名>`，例如 `field:knowledge_bases`。

```python
config_actions=[
    {
        "key": "generate_knowledge_base",
        "title": "获取并整理为题库",
        "placement": "field:knowledge_bases",
        "submit_label": "生成题库",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "title": "来源 URL"},
                "title": {"type": "string", "title": "标题提示（可选）"},
            },
            "required": ["url"],
        },
    }
]
```

后端会调用插件的 `on_config_action(ctx, action_key, payload)`。`ctx` 不带 Telegram client，但会按 manifest 权限注入受控 `ctx.http` 与 `ctx.ai`；`payload["input"]` 是按钮弹窗输入，`payload["config"]` 是当前表单配置。插件返回：

```python
return {
    "message": "已生成题库，请保存配置后生效。",
    "config_patch": {"knowledge_bases": next_items},
    "result": {},
}
```

后台配置动作完成后，`config_patch` 会自动写入配置并通知 worker 热加载；前端当前草稿若已有未保存修改会继续保留并提示远端配置已变化。通用 API 路径是：

```text
POST /api/accounts/{aid}/features/{key}/config/actions/{action_key}/jobs
GET  /api/plugin-config-action-jobs/{job_id}
POST /api/plugin-config-action-jobs/{job_id}/control  {"action":"pause"|"cancel"}
```

长时间动作支持真实中断和终止。`pause` 将任务标记为“已中断”，适合插件已经分批持久化阶段性结果后调整配置再重新执行；`cancel` 将任务标记为“已终止”。平台会取消当前进程中的在途异步调用，但“继续”的业务进度仍需插件自行持久化，不能只保存在插件实例内存中。

**字段验证清单（平台能力与仓库插件）：**

| 插件 | config_schema | UI 模式 | 状态 |
|------|--------------|---------|------|
| auto_reply | 规则通过 Rules API 管理 | `rules` | 仓库插件，按需安装 |
| autorepeat | ✅ trigger / repeat / chat 配置 | `rules` | 仓库插件，按需安装 |
| game24 | ✅ command, timeout | `single` | 仓库插件，按需安装 |
| math10 | ✅ Event Bus / prize；历史 `interaction_entries.start_math_game` 兼容 | `single` | 仓库插件，交互 Bot 可启动 |
| codex_image | ✅ command, access_token, model, message_template, image_size/aspect_ratio/image_format, timeout/status/output/instructions | `single` | 仓库图片插件，按需安装 |
| scheduler | ✅ default_notify, max_tasks | `platform` | 平台基础能力 |

新增第三方 Telegram 事件插件优先参考 `examples/plugins/event_bus_demo`；需要 HTTP 时参考 `examples/plugins/with_http`，需要 AI 文本能力时参考 `examples/plugins/with_ai`，需要把旧交互入口迁移到标准信封时参考 `examples/plugins/with_interaction`。曾经存在的 `translate` 示例依赖后端私有 LLM 链路，已从当前示例集移除；如需考古只查看 Git 历史，不要把它复原成可安装模板。

### Manifest 验证

远程插件安装阶段验证的是 `plugin.json`，不会执行 Python：

```python
required = ["name 或 key", "version"]
name_pattern = r"^[A-Za-z0-9_][A-Za-z0-9_-]*$"
version_pattern = r"^\d+\.\d+\.\d+"
```

ZIP 上传还会在解压和加载 Python 前执行供应链门禁：

- 配置 `PLUGIN_PUBKEY` 后，签名缺失或验签失败都会拒绝安装。
- 未配置公钥时，新 ZIP 默认仍拒绝；只有管理员显式设置 `PLUGIN_ALLOW_NEW_UNSIGNED_PLUGINS=true` 才允许以 `trust_tier=community` 安装。
- `PLUGIN_ALLOW_LEGACY_UNSIGNED_PLUGINS` 只控制历史 `signature_ok=NULL` 插件能否继续加载，不影响新 ZIP 安装。

这两个未签名开关不能互相替代。兼容历史插件时可以保留 legacy 开关，但新安装入口仍应保持关闭；临时允许未签名安装后，应在完成安装后恢复为 `false`。

### 标准链路内部契约：Event Bus + Trace + MessageOps

UserBot 与 Interaction Bot 两条标准消息链路的内部链路是：

```text
Telegram 来源
  -> Source Adapter
  -> TelePilotEvent 标准事件信封
  -> Trace / Event Bus matcher
  -> 插件标准入口
  -> MessageOps/action
  -> Delivery Executor
```

开发新插件时先写 `plugin.json`：

```json
{
  "name": "event_bus_demo",
  "display_name": "Event Bus 示例",
  "version": "0.1.0",
  "category": "interactive",
  "permissions": ["send_message", "read_chat"],
  "usage": "启用后按 Event Bus 订阅处理 message/command/callback/inline/payment。",
  "event_subscriptions": [
    {"events": ["message", "command"], "source": ["userbot", "interaction_bot"], "scope": "all_allowed_chats"},
    {"events": ["callback_query"], "source": ["interaction_bot"], "scope": "all_allowed_chats"},
    {"events": ["inline_query", "chosen_inline_result"], "source": ["interaction_bot"], "scope": "inline_all"},
    {"events": ["payment_confirmed"], "source": ["external_payment_notice", "userbot"], "scope": "rule_bound"}
  ],
  "capabilities": {
    "telegram_native_raw": {
      "enabled": false,
      "reason": "默认只读取标准事件信封。"
    }
  }
}
```

`usage` 必须让开发者和安装者不用理解旧规则也能知道插件怎么启用。`event_subscriptions` 描述 Event Bus 投递范围；`capabilities` 描述高风险能力，没有高风险能力也建议显式写 `{}`。这组契约服务于标准消息链路，不是裸直通，也不是独立于三条链路之外的第四种模式。

自主发送的 Inline Keyboard 按钮不依赖交互 Bot 触发规则或活跃会话。插件只需订阅
`source=["interaction_bot"]`、`events=["callback_query"]`、`scope="all_allowed_chats"`，
并在 `on_event(ctx, payload)` 中处理回调；允许会话留空时，`all_allowed_chats`
表示全部会话。只有确实依附某条旧交互规则的按钮才使用 `rule_bound`。
新标准 `on_event` 订阅不要求 `entry_key`；`entry_key` 仅在需要回落到旧
`on_interaction(ctx, entry_key, payload)` 入口时必填。按钮必须由当前 Interaction
Bot 发送，Telegram 才会把点击更新投递给该 Bot。

### Inline 按钮的两种完全不同场景

“收到按钮回调”和“主动点击别的 Bot 的按钮”在 Telegram 协议里是两件事：

| 场景 | 谁发按钮 | 谁执行点击 | TelePilot 收到/发出什么 | 插件入口 |
| --- | --- | --- | --- | --- |
| Interaction Bot 按钮回调 | 当前 TelePilot Interaction Bot | Telegram 用户 | Interaction Bot 收到 `callback_query` update | `on_event` 处理 `callback_query`，用 `answer_callback` ACK |
| UserBot 主动点击第三方 Bot 按钮 | 第三方 Bot | TelePilot UserBot | UserBot 经 MTProto 发送 `GetBotCallbackAnswerRequest` | UserBot 执行链路调用 `ctx.messages.click_callback_button(...)` |

第一种场景的标准订阅如下：

```json
{
  "events": ["callback_query"],
  "source": ["interaction_bot"],
  "scope": "all_allowed_chats"
}
```

这不依赖旧 Interaction Bot 规则或活跃会话。插件用
`ctx.messages.answer_callback(...)` 或 `{"type": "answer_callback", ...}` 确认 Telegram
已经投递给当前 Interaction Bot 的 callback。`answer_callback` 不会、也不能让 UserBot
点击第三方 Bot 的按钮。

第二种场景使用平台正式 MessageOps 接口。安装插件的 manifest 必须声明：

```python
permissions = ["click_bot_button"]
```

legacy `on_message` 示例：

```python
from app.worker.plugins.base import Plugin, PluginContext, register


@register
class ThirdPartyBotButtonPlugin(Plugin):
    key = "third_party_bot_button"
    display_name = "第三方 Bot 按钮示例"

    # legacy on_message 默认 owner_only=True；要接收第三方 Bot 消息必须显式关闭。
    message_channels = {"incoming"}
    owner_only = False

    async def on_message(self, ctx: PluginContext, event) -> None:
        message = getattr(event, "message", event)
        sender_id = int(
            getattr(message, "sender_id", None)
            or getattr(event, "sender_id", 0)
            or 0
        )
        chat_id = int(
            getattr(message, "chat_id", None)
            or getattr(event, "chat_id", 0)
            or 0
        )

        # 必须同时限制发送 Bot 和会话；这里的 ID 由插件配置提供。
        if sender_id != int(ctx.config["target_bot_id"]):
            return
        if chat_id != int(ctx.config["target_chat_id"]):
            return

        await ctx.messages.click_callback_button(
            chat_id=chat_id,
            message_id=int(getattr(message, "id", 0) or 0),
            row=0,
            column=0,
            expected_bot_id=int(ctx.config["target_bot_id"]),
            expected_button_text="确认",
        )
```

要让这段逻辑收到消息，还必须满足：

- 插件已经在目标 UserBot 账号启用。
- 账号级“允许会话”留空，表示全部会话；列表非空时，目标会话必须在名单内。
- 不需要配置旧 Interaction Bot 触发规则。
- `owner_only=False` 只开放 legacy `on_message`，不会把第三方消息变成管理命令；UserBot outgoing 指令仍是独立安全边界。

`click_callback_button(...)` 属于 UserBot 执行链路能力：UserBot Event Bus、legacy
`on_message`、插件命令、裸直通和后台/调度任务的 `ctx.messages` 都可调用；Interaction
Bot 插件入口不支持，收到当前 Interaction Bot 的用户点击时必须使用 `answer_callback`。
实践中仍推荐在收到第三方 Bot 消息的处理函数内立即调用，并同时校验目标会话、Bot ID
和按钮文字，避免依赖已经变化的旧消息。

平台会重新读取指定 Telegram 消息，确认发送者确实为 Bot，定位对应行列，并且只接受
`KeyboardButtonCallback`。callback data 不向插件暴露，也不接受插件传入；推荐同时传
`expected_bot_id` 和 `expected_button_text`，防止消息或按钮在读取前被替换。平台统一负责：

- installed 插件的 `click_bot_button` 权限检查；
- `callback_query` 限流；如果限流要求等待，等待结束后重新读取并复核目标消息；
- Trace、ActionEvent 和 dev-mode dry-run；
- 15 秒消息读取/点击超时；
- 同账号、同消息、同行列的物理点击锁：明确成功后保护 20 秒，超时或结果未知时保守保护 5 分钟；
- Redis 不可用时 fail-closed，拒绝失去幂等保护的点击。

普通安装插件看到的 `message.buttons[row][column]` 和
`message.reply_markup.rows[row].buttons[column]` 都是平台只读视图，只暴露 `text`
与 `kind`，不会暴露原始按钮对象、client 或 callback data。旧
`message.buttons[row][column].click()`、`message.click()`、`get_buttons()` 等穿透调用
会被沙箱拒绝。

标准事件信封中的 `payload["message"]["reply_markup"]["buttons"]` 是扁平按钮数组，
每项只包含 `row`、兼容字段 `col`、正式字段 `column`、`text`、`kind`；转换成
`tp_event.message.reply_markup` 后得到 `ReplyMarkupRef`，其中每个 `ButtonRef` 只公开
`row`、`column`、`text`、`kind`。这两种标准投影都不公开 callback data 或 URL。

普通键盘、URL、Switch Inline、手机号、地理位置和其他非 callback 按钮都会被拒绝。

`strict_trace` 是布尔字段，默认 `false`。开启后，插件声明的路由投递层会更积极保留全链路 trace，便于复盘订阅匹配、插件执行、动作投递和失败原因；动作层 `record_action` 不受这个开关影响。涉及资金、发奖、`payout`、补偿重放或对账的插件建议设为 `true`，普通查询/娱乐插件可保持默认。

当前标准事件：

| event.type | 说明 |
| --- | --- |
| `message` | 普通消息，读取 `payload["message"]["text"]` |
| `command` | 管理员/授权用户命令，仍受 UserBot command 权限约束 |
| `callback_query` | 当前 Interaction Bot 收到的 Inline keyboard 按钮回调，用 `answer_callback` ACK；不是 UserBot 主动点击第三方 Bot 按钮 |
| `inline_query` | Inline 查询，用 `answer_inline_query` 返回结果 |
| `chosen_inline_result` | 用户选择了 Inline 结果，用于记录选择或后续结算 |
| `payment_confirmed` | 可信外部通知或平台解析确认到账后生成 |
| `message_edited` | 平台已登记的编辑后消息事件，需显式订阅 |
| `session_expired` | 会话 TTL 到期后由平台投递，供插件清理状态 |
| `webhook` | 外部 HTTP 入站 webhook，经账号 token 鉴权后投递 |
| `all_messages` | 兼容聚合订阅，当前仍只覆盖 `message` / `command` |
| `all_events` | 平台聚合订阅，覆盖当前已登记的常见事件类型 |
| `session_close` | 会话关闭或规则关闭，插件可清理状态 |

`all_messages` 目前仍只等于 `message` / `command`；需要覆盖更多已登记事件时，使用 `all_events`。Inline 相关事件、付款确认、`message_edited` 与 `session_expired` 仍可按需显式订阅。

`known_users` 只认平台 state 提供的真实集合，不会自动把当前 sender 算进去。

### 入站 webhook 事件

第一次接入请先看 [入站 Webhook Quickstart](./PLUGIN-WEBHOOK-QUICKSTART.md)，可运行示例位于 `examples/plugins/webhook_receiver`。

插件可以通过 Event Bus 订阅外部 HTTP webhook。Manifest / `plugin.json` 写法如下：

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

`filters.hook_key` 可写单个字符串，`filters.hook_keys` 可写多个 key。也可以使用 trigger 简写：

```json
{
  "event_subscriptions": [
    {
      "source": ["webhook"],
      "events": ["webhook"],
      "scope": "all_allowed_chats",
      "triggers": {"webhook": "default"}
    }
  ]
}
```

`triggers.webhook` 会被归一化为 `filters.hook_key`；对象形式也支持 `{"hook_key": "orders"}`、`{"hook_keys": ["orders", "billing"]}`、`{"key": "orders"}` 或 `{"keys": [...]}`。Webhook 事件的 `scope="all_allowed_chats"` 会直接通过 scope 检查，因为外部 HTTP 请求没有 Telegram `chat_id`。

插件收到的标准 payload 包含：

```json
{
  "event_type": "webhook",
  "source": {
    "type": "webhook",
    "channel": "webhook",
    "driver": "http_webhook",
    "hook_key": "default"
  },
  "trigger": {"hook_key": "default"},
  "message": {"text": "{\"order_id\":\"A-1\"}"},
  "webhook": {
    "hook_key": "default",
    "body": {"order_id": "A-1"},
    "headers": {"content-type": "application/json"},
    "body_size": 19,
    "content_type": "application/json",
    "received_at": "2026-07-10T00:00:00+00:00"
  },
  "hook_key": "default",
  "body": {"order_id": "A-1"},
  "headers": {"content-type": "application/json"}
}
```

`body` 会按 `Content-Type` 尝试解析 JSON；JSON 解析失败或非 JSON 请求会保留为文本。`message.text` 是 body 的文本摘要，最长 2000 字符。headers 只保留白名单字段，并统一小写，单个值最多 512 字符；当前白名单为 `content-type`、`user-agent`、`x-request-id`、`x-correlation-id`、`x-github-event`、`x-gitlab-event`、`x-hub-signature`、`x-hub-signature-256`、`x-signature`、`x-telegram-bot-api-secret-token`。账号 token 头不会进入插件 payload。

外部触发方式：

```bash
curl -X POST "https://<telepilot-host>/api/webhooks/{account_id}/{hook_key}" \
  -H "X-TelePilot-Webhook-Token: <account_webhook_token>" \
  -H "Content-Type: application/json" \
  --data '{"order_id":"A-1","status":"paid"}'
```

默认只接受 `X-TelePilot-Webhook-Token` 请求头。旧调用方只有在服务端显式设置 `WEBHOOK_ALLOW_QUERY_TOKEN=true` 时才能继续使用 `?token=<account_webhook_token>`；查询参数可能进入 URL、反代和访问日志，生产环境不建议开启。`hook_key` 必须是 1-64 位的字母、数字、下划线、点或短横线组合，且必须存在于账号 webhook 配置并处于启用状态；当前页面保证提供 `default`，插件声明不会自动创建其他 Hook key。请求体上限为 64 KiB；投递接口返回 `202` 只表示 worker 已确认接收，插件是否命中和执行结果请在日志/trace 中查看。Webhook 投递受 `webhook_deliver` 风控动作限流，Worker 暂停或全局总闸开启时拒绝投递。

标准事件信封优先读这些字段：`source`、`message`、`chat`、`sender`、`actor`、`source_actor`、`player`、`payment`、`reply_to`、`trigger`、`session`、`native_raw_meta`。新插件不要依赖 `payload["text"]`、`payload["chat_id"]`、`payload.get("message")` 这类旧平铺字段；`payload["message"]` 是消息对象，不是配置字符串。

重点补充字段：

- `payload["tp_event"]`：进程内调用时已挂好的 `TelePilotEvent` 投影对象；跨 IPC 路径平台会重建同形对象。
- `message.entities` / `message.media` / `message.date`：统一消息摘要，编辑消息和媒体消息也走同一套字段。
- `message.rich_message` / `message.text_source`：Userbot 收到 Layer 228 Rich Message 时保留规范化 blocks；普通文本为空时，`message.text` 使用纯文本 fallback，并标记 `text_source="rich_message_fallback"`。
- `chat.title` / `chat.username`：群标题、username 等可直接读取，不必回头翻 `native_raw`。
- `session.channel` / `session.expires_at` / `session.data`：当前会话通道、超时点和持久化状态。
- `source.synthetic`：平台合成事件来源，例如 userbot 文本按钮降级后的 `text_button`。

`tp_event` 对象字段主看：

| 字段 | 说明 |
| --- | --- |
| `tp_event.type` | 事件类型，等价于 `payload["source"]["type"]` |
| `tp_event.source_channel` | 当前事件来源通道 |
| `tp_event.message` | `MessageRef`，含 `text/caption/date/entities/media/forward/sender_chat/edited` |
| `tp_event.callback` | `CallbackRef`，按钮事件时可读 `id/data` |
| `tp_event.payment` | `PaymentRef`，付款确认场景可读金额、币种、payer/receiver |
| `tp_event.session` | `SessionRef`，可读 `key/scope/channel/data` |
| `tp_event.trigger` | 命中的规则、入口和触发参数 |

`capabilities.telegram_native_raw` 只用于排障。声明 `enabled=true` 时必须写 `reason` 和 `sources`，插件仍要先检查：

```python
native_raw_meta = payload.get("native_raw_meta") or {}
if not native_raw_meta.get("enabled"):
    # 降级到标准事件信封。
    pass
```

不要读取旧 `raw_event`。它只能作为迁移风险或回归测试名出现。

MessageOps/action 示例：

```python
event = event_from_interaction_payload(payload)
return [
    {
        "type": "send_message",
        "chat_id": event.message.chat_id,
        "reply_to_message_id": event.message.message_id,
        "text": f"收到：{event.message.text}",
    }
]
```

默认 `send_message` 类动作按 `parse_mode="plain"` 发送；只有显式写 `parse_mode="html"` 时才启用 HTML。HTML 内容应先用 `app.worker.plugins.textutil.html_escape()` 或等价工具转义，再把标签手动拼好。

#### 原生 Rich Message

需要标题、任务列表、折叠详情、表格等 Telegram 原生结构时，使用 Bot API Rich Message，而不是把普通 `send_message` 的 HTML 当成同一种格式：

```python
await ctx.messages.send_rich(
    chat_id=event.message.chat_id,
    html=(
        "<h1>巡检结果</h1>"
        "<ul>"
        '<li><input type="checkbox" checked>数据库正常</li>'
        '<li><input type="checkbox">等待人工复核</li>'
        "</ul>"
        "<details><summary>失败详情</summary><p>上游返回 429</p></details>"
        "<table bordered striped>"
        "<tr><th>模型</th><th>状态</th></tr>"
        "<tr><td>grok-4</td><td>正常</td></tr>"
        "</table>"
    ),
    reply_to_message_id=event.message.message_id,
    save_message_id_key="latest_health_report",
)
```

`ctx.messages.send_rich()` 必须且只能提供 `html`、`markdown`、`blocks` 其中一个；也可附带 `media`、`is_rtl`、`skip_entity_detection`、`reply_markup`、`save_message_id_key` 和 `pin`。动态文本嵌入 `html` 前仍要转义。对应标准 action 为：

```python
return [{
    "type": "send_rich_message",
    "chat_id": event.message.chat_id,
    "rich_message": {
        "markdown": "# 巡检结果\n\n- [x] 数据库正常\n- [ ] 等待人工复核",
    },
}]
```

Rich Message 默认通过 `interaction_bot` 执行：即使当前是 UserBot 命令会话，省略通道也会切到 Interaction Bot。显式指定 `userbot_reply` 时使用 Telethon Layer 228；主号必须具备 Telegram Premium 且 Telegram app config 的 `rich_message_posting` 可用，当前接受 HTML、Markdown 和可无损转换的纯文本 blocks，不支持媒体型/复杂 blocks、media 或 Bot `reply_markup`。两条路径都不做静默降级。Telegram 原生限制包括 32768 文本单位、500 个结构块、16 层嵌套、50 个媒体附件和表格最多 20 列；TelePilot 的 Userbot 适配按 32768 UTF-8 bytes 检查，Bot 通道保持 32768 字符计量。最终格式解析仍以 [Telegram Rich Message 文档](https://core.telegram.org/bots/api#rich-message-formatting-options) 为准。

插件 action 的失败语义保持严格，不会自动改成普通消息。只有 TelePilot 自己拥有的账号 Bot 移动控制页和系统告警采用“同一个 Bot 先发 Rich Message，Bot API 拒绝时回退原有 HTML”的兼容策略；这个内部回退不会切换到 UserBot，也不会改变插件 action 的契约。

会话内状态更新示例：

```python
return [{
    "type": "update_session",
    "data": {
        "answer": "42",
        "attempts": 3,
    },
}]
```

`update_session` 会回写当前 `session.data`，并按平台规则续租 Redis TTL；除非额外声明 `extend_seconds`，它不会偷偷改掉原来的 `expires_at`。

免费答题、抽奖、按钮加入、互动游戏这类“不想让玩家为了发奖额外刷消息”的玩法，推荐让玩家点击 inline 按钮加入或确认参与。按钮回调里的点击者在 `payload["sender"]["user_id"]`；插件仍可按自身玩法保存完整业务状态，仅从后续发奖锚点角度，至少保留这个 `tgid` 即可。发奖时交给平台用 `reply_to_user_id` 在当前群查找该玩家最近一次发言作为回复锚点，不需要插件自己遍历群消息。

如果还在用显式 `userbot_reply` 发奖消息，可以这样写：

```python
event = event_from_interaction_payload(payload)
winner_user_id = event.sender.user_id

return [
    {
        "type": "send_message",
        "send_via": "userbot_reply",
        "chat_id": event.message.chat_id,
        "reply_to_user_id": winner_user_id,
        "reply_to_search_limit": 2000,
        "reply_anchor_missing_text": "未找到对应用户（{user_id}）的近期消息，本次发奖需要人工补发。",
        "text": "+88",
        "settlement": {
            "mode": "auto",
            "amount": 88,
            "winner_user_id": winner_user_id,
            "winner_name": event.sender.display_name,
            "status": "announced",
        },
    }
]
```

平台会先读取账号 UserBot 在当前群为该真实用户保存的近期消息锚点，缓存未命中时再使用 Telegram `from_user` 精确搜索，最后最多扫描 2000 条近期消息；找到后把 `+88` 回复到那条消息。缓存键按账号、群和用户隔离，只接受 Telegram 消息自身的 `PeerUser`，匿名管理员、频道身份和按钮 callback 不会反向生成真实用户锚点。若插件已经有明确的 `reply_to_message_id`，平台优先使用它；若只给了 `settlement.winner_user_id` 而没写 `reply_to_user_id`，平台也会尝试用赢家 user id 作为回复锚点。找不到近期消息时，本次 `userbot_reply` 会失败并记录 action 错误，避免把发奖消息误发成普通群消息；平台默认会在群里提示 `未找到对应用户（用户 ID）的近期消息。`，插件可通过 `reply_anchor_missing_text` 自定义失败提示，提示文案支持 `{user_id}` 占位符，并沿用当前 action 的 `parse_mode`。例如在 HTML 模式中使用 `<code>/command</code>`，Telegram 会将命令显示为可点击复制的代码文本。

更推荐的新写法是直接返回 `payout`：

```python
event = event_from_interaction_payload(payload)
winner_user_id = event.sender.user_id
identity = await resolve_public_sender_identity(
    ctx,
    chat_id=event.message.chat_id,
    user_id=winner_user_id,
    fallback_display_name=event.sender.display_name,
)

return [{
    "type": "payout",
    "chat_id": event.message.chat_id,
    "amount": 88,
    "reply_to_user_id": winner_user_id,
    "reply_to_display_name": identity.display_name,
    "reply_to_username": None if identity.is_anonymous_admin else event.sender.username,
    "reply_to_search_limit": 2000,
    "reply_anchor_missing_text": "未找到对应用户（{user_id}）的近期消息，本次发奖需要人工补发。",
}]
```

`reply_to_display_name` 必须来自上文的 `resolve_public_sender_identity()` / `resolve_public_sender_identities()`，不能直接复制按钮回调中的姓名；匿名管理员应同时把 `reply_to_username` 设为 `None`。平台会在 UserBot 发奖消息发送后先保存这组安全公开身份，再完成 payout 账本，供后续转账通知复用。历史动作未提供公开名、映射尚未写入或 Redis 暂时不可用时，平台会再用 Interaction Bot 的官方 `getChatMember` 核验；无法确认时隐藏为“匿名用户”，不会回退回复消息里的真实姓名。

`payout` 永远经 userbot 执行，并进入限速与 trace。已安装插件必须在 Manifest 的 `permissions` 中显式声明 `payout`，否则运行时以 `error_code=permission_denied` 拒绝动作；涉及资金的插件同时建议开启 `strict_trace=true`。它适合“发奖文案本身就是协议”的玩法，普通 Bot 不会代替它执行转账样动作。找不到 `reply_to_user_id` 对应的近期发言时，`payout` 同样会失败、写入日志，并发送默认或自定义的 `reply_anchor_missing_text` 提示。

`payout` 的额度校验与失败补偿由平台负责，插件不用自己实现，但要理解它对 trace 的影响：

- **超限直接拒**：账号可在「系统设置 → 风控与预算」配置 `payout` 的单笔上限与日累计上限（默认 `0` = 不限，按业务币种 / 积分的整数口径填写）。本笔金额超单笔上限、或“今日已累计 + 本笔”超日累计上限时，`payout` 被拒绝执行，action 记 `FAILED`、`error_code=payout_limit_exceeded`。超限属配置拒绝，**不进补偿队列、也不自动重试**；需要人工调额度，日累计上限跨 UTC 零点自动重置。
- **瞬时失败自动补偿**：userbot 离线（`userbot_offline`）、Telegram API 报错（`telegram_api_error`）、命中限速（`rate_limited`）这类可恢复失败，平台会把这一笔 `payout` 写进补偿队列并按退避自动重放。插件侧看到的仍是本次的 `FAILED` action（detail 带 `compensation_queued=true` 和 `payout_key`）；补发成功后会**以一条新的 `OK` action 出现**（同 `payout_key`、带 `replay` 标记）。**插件不要自己重试 `payout`**——补发交给平台，重复返回只会靠幂等去重。
- **补发幂等**：平台以数据库中的 `payout_key` intent / claim / sent 状态作为持久化幂等边界，发送完成状态与 ActionEvent 在同一事务提交。只有 Telegram 明确拒绝发送时才释放 claim；超时、断连或未知异常会进入 ambiguous，不会假定“异常等于未发送”。
- **暧昧送达核对**：只有 payload 带稳定 `payout_probe_fingerprint` 时，worker 才会回查账号最近自己发言并自动确认送达。旧记录或缺少 fingerprint 的记录需要人工核对，平台不会只凭相同文本和回复锚点认定已发送。插件若要把多次调用视为同一笔，应提供稳定 `payout_key`，不要自行重试。
- **彻底放弃会告警**：重放次数耗尽或遇到不可补偿错误时，补偿单置为 `abandoned` 并写一条 error 级运维日志，供“收款成功但发奖失败”场景人工介入。运维侧配置与巡检见 [SECURITY-OPS](./SECURITY-OPS.md) §7。

按钮回调：

```python
return [{
    "type": "answer_callback",
    "callback_query_id": payload["source"]["callback_query_id"],
    "text": "按钮已收到",
    "show_alert": False,
}]
```

Inline：

```python
return [{
    "type": "answer_inline_query",
    "inline_query_id": payload["inline_query"]["id"],
    "results": [{
        "type": "article",
        "id": "demo",
        "title": "示例结果",
        "input_message_content": {"message_text": "Inline 示例"},
    }],
    "cache_time": 0,
    "is_personal": True,
}]
```

付款确认：

```python
return [{
    "type": "settlement",
    "mode": "confirm_only",
    "payer_user_id": payload["payment"]["payer"]["user_id"],
    "amount": payload["payment"]["amount"],
    "currency": payload["payment"]["currency"],
    "status": "confirmed",
}]
```

`notice` / `bbot_notice` / `notice_bot` 不再是可执行发送通道，只能出现在迁移说明或故意回归测试里。普通 Bot 不执行转账、催付或发奖；钱相关动作应交给 `settlement`、`userbot_reply` 或平台受控结算链路。

userbot 会话里的 `reply_markup` 不会直接丢掉：

- 平台会把按钮渲染成文本编号列表。
- 玩家回复序号或按钮文案后，平台会合成 `callback_query` 回投插件。
- 这类事件的 `callback_query_id` 可能为空，同时 `source.synthetic="text_button"`。
- 对合成 callback 返回 `answer_callback` 时，平台会按“已合成事件”跳过真正的 Bot API ACK。

入口控制字段：

| 字段 | 位置 | 作用 |
| --- | --- | --- |
| `interaction_trigger_modes` | 平台注入的配置字段 | `all` / `keyword_only`，控制声明了 command trigger 的入口是否仍注册命令 |
| `default_trigger_modes` | manifest entry | 为 `interaction_trigger_modes` 提供默认值，强按钮玩法建议设为 `keyword_only` |
| `callback_fast_ack` | manifest entry | callback 分类后立即空 ACK，插件晚到的 `answer_callback` 会记为 `already_acked` |

`callback_fast_ack` 只适合“先消除按钮转圈，再慢慢算结果”的入口；启用后不要再依赖 `show_alert=True` 的晚返回提示。

常见 `reason_code` 排障表：

| reason_code | 说明 |
| --- | --- |
| `matched` | Event Bus 订阅命中，准备投递 |
| `subscription_not_matched` / `event_type_not_subscribed` / `source_not_subscribed` | 没有订阅命中、事件类型未订阅或来源未订阅 |
| `scope_not_matched` / `filter_not_matched` | 允许会话、owner_only、inline_all 等范围不匹配，或关键词、金额、callback data 等过滤不匹配 |
| `plugin_disabled` / `plugin_load_failed` / `plugin_runtime_error` | 插件未启用、加载失败或运行异常 |
| `entry_key_missing` | 订阅未声明 `entry_key`，且插件也没有实现标准 `on_event` 入口 |
| `command_matched` / `command_not_matched` / `command_unauthorized` | 管理员命令命中、普通文本未命中命令、权限不足 |
| `event_bus_delivery_disabled` / `inline_disabled` | 运维回滚开关关闭 Event Bus 新投递路径或 Inline updates |
| `native_raw_not_allowed` / `native_raw_skipped` | 插件未声明 `telegram_native_raw` 或本次因来源、大小、设置未下发 |
| `contract_warning` / `contract_failed` | 插件越声明调用被告警放行，或请求客观不可执行能力 |
| `action_limit_exceeded` | 插件返回的动作超出平台允许数量，后续动作被截断并写入可见告警 |
| `send_channel_deprecated` / `unsupported_send_via` | 请求旧 `notice` 通道或未知通道 |
| `premium_required` / `rich_message_posting_disabled` / `rich_message_capability_unknown` / `rich_message_blocks_unsupported` / `rich_message_media_unsupported` / `invalid_rich_message` | Userbot Rich Message 能力不足或无法确认、输入超出当前适配范围、内容结构未通过校验 |
| `bot_not_configured` / `bot_token_missing` / `userbot_offline` | 交互 Bot 未配置、Bot token 缺失或 UserBot 离线 |
| `settlement_requires_userbot` / `telegram_api_error` | 普通 Bot 请求钱相关能力，或 Telegram API 返回失败 |
| `trace_write_failed` | Trace 写库失败，平台已降级写入旧 runtime log |

运行阶段 loader 会 import `__init__.py`，并检查：

- `PLUGIN_CLASS` 是 `Plugin` 子类
- `MANIFEST` 是 `Manifest` 实例
- `MANIFEST.key` 与插件 key / 目录名保持一致

### 旧交互 Bot 兼容声明（interaction entries，仅迁移）

本节只用于迁移历史 `interaction_entries` / `on_interaction` 插件。新插件不要把旧交互规则、旧平铺 payload 或旧入口声明当主路径；请优先使用上一节的 `usage`、`event_subscriptions`、`capabilities` 和标准 action。

迁移旧插件时，可以暂时保留 `manifest.py` 顶层的 `interaction_entries`，但必须同时补齐 `event_subscriptions`。旧 `config_schema["x-category"]` 与 `config_schema["x-interaction-entries"]` 仅作为兼容入口，新插件不要再新增。

### 双通道与外部转账证据

当前标准模式只有两个主动发送通道：交互 Bot 负责承接高频互动、按钮和会话提示，`UserBot` 负责管理员命令、账号身份动作、收款确认和发奖。群里已有的转账结果通知 Bot 只属于外部付款证据来源；TelePilot 监听它的到账消息并生成 `payment_confirmed`，但不会把它作为插件主动发送通道。

`outgoing` 的频率控制，指的是当前 `UserBot` 账号自己发出的消息要保持低频、可解释、可回溯，避免把大量游戏交互都压回账号本身。`incoming` 订阅则只表示这个账号愿意看见哪些外部消息，用来做公告监听、状态同步和必要的自动回复，不表示这些消息都能直接触发指令，更不表示可以绕过风控做批量互动。

原则上，交互 Bot 不碰钱，只负责题面、答复、结果提示、按钮和规则事件；真正的奖金发放仍由 `UserBot` 或平台受控结算链路完成。插件如果要做发奖、结算或红包类动作，也应把钱相关动作留在 `UserBot` 侧，不要把转账、发奖、催付这些动作混进交互 Bot 的高频入口里。

插件分类只保留三类，前端会按中文分组展示：

| category | 中文分组 | 适用插件 |
| --- | --- | --- |
| `interactive` | 互动娱乐 | 游戏、群内娱乐、需要交互 Bot 承接高频消息的插件 |
| `automation` | 自动化 | 自动回复、转发、定时任务等账号自动化能力 |
| `utility` | 工具能力 | AI、媒体生成、查询、辅助工具等能力 |

`category` 只决定展示分组；标准会话事件投递看 `event_subscriptions`。`interaction_entries` 只用于旧交互中心规则迁移和入口参数兼容。

注意：`interaction_entries` 只负责“让前端知道这个插件有哪些交互入口可选”。真正运行时，worker 会调用插件实例的 `on_interaction(ctx, entry_key, payload)`。如果插件只声明入口但没有实现这个 hook，交互 Bot 会提示“插件尚未实现交互入口”。

Interaction Bot 运行时采用事件路由模型：普通 Bot 负责接收群消息、按钮回调和规则指令；UserBot/回复上下文与外部转账通知来源负责补充付款证据。新标准插件可以通过 `event_subscriptions` 自主订阅，并以 `all_allowed_chats` 接收账号允许范围内的事件，不要求先命中旧规则或存在活跃会话；平台仍会按 source、event、scope、filters 做匹配，不会无条件广播给所有插件。旧 `interaction_entries` / `on_interaction` 兼容路径才依赖规则匹配及相应会话上下文。

交互入口是新增触发面，不是命令系统的替代品。插件原有 `commands`、`on_command`、`message_channels` 和 `on_message` 语义必须保持不变；任何新入口都不得让普通 incoming 消息绕过 UserBot outgoing 指令边界。需要复用能力时，把业务逻辑抽成共享函数，由 UserBot 命令和交互入口分别调用。

#### 平台、规则、插件的职责边界

交互 Bot 的核心设计是“触发器和业务分离”。开发插件时先按下面的边界判断代码应该放在哪里：

| 层级 | 负责 | 不负责 |
| --- | --- | --- |
| 自动回复 | 轻量关键词/变量触发，把消息转换成普通回复或白名单命令 | 不承载复杂业务状态，不直接实现插件业务 |
| 交互 Bot 规则 | 匹配群、关键词、转账通知、金额/收款人过滤、每用户冷却、每日次数、开关命令、会话路由 | 不生成题目、不查询 PT、不校验答案、不发奖 |
| 插件 `on_interaction` | 真正业务逻辑：开局、查询、校验答案、渲染结果、维护插件内部状态 | 不解析 Bot Token、不解析转账通知原文、不自己做规则级冷却/每日次数 |
| UserBot 命令 | 管理员手动触发同一业务能力，例如 `{prefix}pt 12345` 或 `{prefix}24d 100` | 不承接群友高频互动 |

因此，同一个能力推荐有多个触发器，但只有一份业务实现：

```text
群友关键词 / 转账通知
  -> 交互 Bot 规则过滤、限流、路由
  -> 插件 on_interaction 执行业务

管理员 UserBot 命令
  -> commands 入口
  -> 调用同一份插件业务函数
```

不要把自动回复做成“业务实现”。自动回复可以作为轻量触发器，但 PT 促销、抽奖、游戏、查询这类能力必须沉到插件本体里，再由交互 Bot 或 UserBot 命令调用。

当前标准事件类型写在 `payload["source"]["type"]`：

| event.type | 触发时机 | 说明 |
| --- | --- | --- |
| `payment_confirmed` | 转账通知命中规则 | 常用于付费开局 |
| `keyword` | 插件启动关键词命中且无付费门槛 | 常用于免费开局 |
| `message` | 自主订阅命中，或旧规则已有活跃会话后的普通群消息 | 常用于答题、猜测、继续流程 |
| `callback_query` | 当前 Interaction Bot 收到的 inline keyboard 点击；可自主订阅，也可来自旧规则会话 | 常用于按钮选择、翻页、确认操作；不是 UserBot 主动点击第三方 Bot 按钮 |
| `session_close` | 规则被关闭或会话被强制结束 | 插件可清理状态，第一版可按需实现 |

#### interaction_entries 迁移字段

旧交互入口迁移时必须把启动方式、事件、会话和输出边界写清楚。推荐同时映射到 `event_subscriptions` 和标准 action：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | 是 | 传给插件 `on_interaction(ctx, entry_key, payload)` 的入口名 |
| `title` / `description` | 推荐 | 前端选择器、实验室和日志里展示给人的说明 |
| `launch_mode` | 兼容 | `bridge` / `direct` / `hybrid`，旧字段；新插件建议同时声明 `dispatch_modes` |
| `dispatch_modes` | 推荐 | `admin_command` / `public_keyword`，分别表示管理员带前缀命令触发、群友关键词/转账规则触发 |
| `message_channels` | 兼容 | 旧 `Plugin` hook 的 incoming/outgoing 监听方向；不要把它当发送通道偏好 |
| `money_channel` | 兼容 | 旧文档提示字段；钱相关动作现在由 `payout` / 结算链路固定路由 userbot |
| `events` | 是 | 入口接受的事件白名单，例如 `keyword`、`payment_confirmed`、`message`、`callback_query`、`session_close` |
| `session_scope` | 是 | `chat` / `user` / `none`，决定平台如何保存会话和路由后续消息 |
| `session_policy` | 推荐 | 会话 TTL、重复触发、关闭策略、并发策略的声明 |
| `payload_contract` | 推荐 | 插件要求平台提供的输入信封与必填字段 |
| `input_schema` | 推荐 | 当前规则可覆盖的入口参数，默认值用于前端预填 |
| `result_contract` | 推荐 | 插件会返回的标准动作类型、结算字段和结束语义 |
| `settlement` | 按需 | 涉及奖金、补发、对账时声明结算责任和字段 |
| `command_fallback` | 按需 | 是否允许平台在无法走交互入口时提示或回退到 UserBot 命令 |
| `preserve_command_trigger` | 是 | 必须为 `true`，表示保留原有 UserBot 命令触发，不被交互入口覆盖 |
| `interaction_profile` | 推荐 | 玩法类型声明，供前端展示和后续插件接入分型使用 |

`interaction_profile` 当前建议值：

| 值 | 说明 |
| --- | --- |
| `session_game` | 群局抢答、竞猜、填空、算题、24 点等单局互动玩法 |
| `challenge_game` | 双人/多人对战、轮流操作的互动玩法 |
| `reward_pool` | 红包、奖池、下注开奖这类多人结算玩法 |
| `utility_trigger` | 只借交互 Bot 做入口，但主体不是群局玩法的工具插件 |

`launch_mode` 的含义：

| launch_mode | 启动路径 | 适用场景 |
| --- | --- | --- |
| `bridge` | 交互 Bot 收到事件，平台组装信封后调用插件 `on_interaction` | 群局、抢答、抽奖、转账命中开局等高频群内流程 |
| `direct` | UserBot 原有命令或插件内部调用直接执行业务，不经过交互 Bot | 管理员命令、私有工具、无需交互 Bot 规则的能力 |
| `hybrid` | 同一能力同时支持 `bridge` 和 `direct`，但两边仍是独立触发边界 | 既允许管理员 `{prefix}24d 100` 开局，也允许群友关键词/转账由交互 Bot 开局 |

`direct` 和 `hybrid` 都不表示普通群友 incoming 消息可以直接触发 `commands`。`command_fallback` 只用于平台提示或受控内部派发，不能把群友文本原样送入 `on_command`。如果启用回退，必须同时声明 `preserve_command_trigger: true`，并保证原命令名、参数格式、权限和 outgoing 限制保持兼容。

```python
MANIFEST = Manifest(
    key="game24",
    display_name="24点游戏",
    version="1.1.0",
    category="interactive",
    interaction_entries=[
        {
            "key": "start_paid_game",
            "title": "付费开局",
            "description": "转账命中或插件关键词命中后，由交互 Bot 开启一局游戏。",
            "launch_mode": "hybrid",
            "dispatch_modes": ["admin_command", "public_keyword"],
            "session_scope": "chat",
            "events": ["payment_confirmed", "keyword", "message", "callback_query", "session_close"],
            "preserve_command_trigger": True,
            "command_fallback": {
                "enabled": True,
                "command": "24d",
                "mode": "hint_only",
            },
            "session_policy": {
                "ttl_seconds": 3600,
                "duplicate_start": "reject",
                "close_on": ["winner", "timeout", "session_close"],
            },
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "prize": {
                        "type": "integer",
                        "title": "奖金",
                        "default": 123,
                        "minimum": 1,
                    },
                    "timeout": {
                        "type": "integer",
                        "title": "答题限时（秒）",
                        "default": 500,
                        "minimum": 30,
                        "maximum": 3600,
                    },
                },
                "required": ["prize"],
            },
            "payload_contract": {
                "required_envelope": ["source", "actor", "trigger", "session"],
                "required_event_fields": ["type", "chat_id"],
            },
            "result_contract": {
                "actions": ["send_message", "send_photo", "end_session"],
            },
            "settlement": {
                "mode": "announce_only",
                "winner_field": "actor.user_id",
                "amount_field": "prize",
            },
        }
    ],
    config_schema={
        "type": "object",
        "x-ui-mode": "single",
        "properties": {
            "command": {"type": "string", "title": "触发指令名", "default": "24d"},
            "timeout": {"type": "integer", "title": "答题限时（秒）", "default": 500},
        },
    },
)
```

`input_schema` 描述的是某个交互入口允许接收的参数形态和默认值，不是插件的全局配置。Web 端在交互规则里保存的是 `module_config`：它只属于当前规则，只保存这条规则对入口参数的覆盖值，并会随规则 payload 一起提交给后端。

例如某条规则可以绑定 `game24 / start_paid_game`，并保存：

```json
{
  "module_key": "game24",
  "module_action": "start_paid_game",
  "module_config": {
    "prize": 200,
    "timeout": 600
  }
}
```

运行时入口收到的 payload 会包含当前规则的 `module_config` 字段。Web 端会在选择入口时用 `input_schema.properties.*.default` 辅助生成初始 JSON。新插件应从 `payload["module_config"]` 读取本次规则参数；插件自身的账号级配置仍通过 `ctx.config` 读取。历史兼容层可能继续在 payload 顶层附带平铺字段，但那只用于旧插件迁移，不作为新插件示例或主路径。

### on_interaction 迁移实现

```python
from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class GuessNumberPlugin(Plugin):
    key = "guess_number"
    display_name = "猜数字"

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if entry_key != "start_guess_number":
            return None

        event = event_from_interaction_payload(payload)
        event_type = event.type

        if event_type == "message":
            answer = event.message.text.strip()
            if answer != "42":
                return []
            if ctx.messages:
                await ctx.messages.send(
                    text=f"答对了：{event.actor.display_name or '玩家'}\n奖金：{payload.get('prize') or 123}",
                    reply_to_message_id=event.message.message_id,
                )
                return []
            return [{"type": "send_message", "text": "答对了"}]

        module_config = payload.get("module_config") if isinstance(payload.get("module_config"), dict) else {}
        prize = int(module_config.get("prize") or 123)
        if ctx.messages:
            await ctx.messages.send(
                text=f"猜数字开始，奖金：{prize}",
                reply_markup={
                    "inline_keyboard": [[{"text": "查看状态", "callback_data": "guess:status"}]]
                },
            )
            return []
        return [{"type": "send_message", "text": f"猜数字开始，奖金：{prize}"}]
```

当前平台已支持的标准动作：

| type | 字段 | 说明 |
| --- | --- | --- |
| `send_message` | `text` | 在命中的群或动作指定的 `chat_id` 发送消息 |
| `send_message` | `reply_to_message_id` | 可选，指定回复哪条消息 |
| `send_message` | `reply_to_user_id` / `reply_to_search_limit` | 可选；`send_via=userbot_reply` 时，平台按用户 ID 搜索近期发言作为回复锚点 |
| `send_message` | `reply_anchor_missing_text` | 可选；找不到 `reply_to_user_id` 的近期发言时发送的提示，支持 `{user_id}` |
| `send_message` | `chat_id` | 可选；不填时发送到触发会话，填写时由平台按通道能力发送到指定会话 |
| `send_message` | `send_via` / `channel` / `channel_selector` / `send_via_options` | 高级可选；普通互动省略，平台继承 `session.channel`，只在跨通道公告、管理提示或迁移桥兼容时显式覆盖 |
| `send_message` | `reply_markup` | 可选，Bot API inline keyboard；只会透传给 `interaction_bot`，`userbot_reply` 不承接按钮 |
| `send_message` | `save_message_id_key` | 可选；发送成功后把本次 Telegram `message_id` 按 key 保存 2 小时，供后续编辑、删除或替换使用 |
| `send_message` | `replace_saved_message_id_key` | 可选；发送新消息并保存新 `message_id` 后，读取该 key 原来的消息 ID 并删除旧消息，适合“只保留最新一条”的滚动通知 |
| `send_rich_message` | `rich_message` | Telegram 原生 Rich Message；对象中必须且只能提供 `html`、`markdown`、`blocks` 之一，可表达标题、任务列表、表格、折叠详情、公式和媒体块 |
| `send_rich_message` | `reply_to_message_id`、`reply_markup`、`save_message_id_key`、`pin` | 可选；Userbot 路径支持回复，但 `reply_markup` 仅由 Interaction Bot 执行 |
| `send_rich_message` | `send_via` | 可省略，平台默认选择 `interaction_bot`；显式 `userbot_reply` 使用 Layer 228，并受 Premium/能力与格式限制 |
| `send_photo` / `send_file` | `photo_base64` / `file_base64` | 按动作通道发送图片/文件字节；交互 Bot 下 `send_photo` 走 `sendPhoto`，`send_file` 走 `sendDocument` |
| `send_photo` / `send_file` | `filename`、`caption`、`reply_to_message_id` | 可选，文件名、说明文字、回复目标 |
| `send_photo` / `send_file` | `save_message_id_key` | 可选；媒体发送成功后把 Telegram `message_id` 按 key 保存 2 小时，供后续 `edit_caption`、删除或替换使用 |
| `edit_message` | `message_id`、`text` | 编辑纯文本消息；不用于编辑媒体 caption |
| `edit_message` | `message_id`、`rich_message` | 编辑 Rich Message；默认由 Interaction Bot 执行，显式 Userbot 支持 HTML、Markdown 和纯文本 blocks，并受 Premium 与 `rich_message_posting` 门禁 |
| `edit_caption` | `message_id` / `message_id_key`、`caption` | 编辑图片或文件消息的 caption；`message_id_key` 会读取同账号命名空间下由 `save_message_id_key` 保存的消息 ID |
| `edit_caption` | `parse_mode`、`reply_markup` | 可选；`parse_mode="html"` 时按 HTML 发送，`reply_markup` 只由 `interaction_bot` 原生承接 |
| `delete_message` | `message_id` | 删除对应 Bot 通道可操作的消息 |
| `pin_message` | `message_id` | 置顶对应 Bot 通道可操作的消息 |
| `click_callback_button` | `chat_id`、`message_id`、`row`、`column`、可选 `expected_bot_id` / `expected_button_text` | 仅 UserBot 执行链路；Interaction Bot 插件入口（包括 `ctx.messages.apply`）不支持。installed 插件需声明 `click_bot_button`。平台重新读取 callback data，只允许 callback 类型，并执行限流、审计、dry-run；明确成功后保护 20 秒，超时或结果未知时保护 5 分钟 |
| `answer_callback` | `callback_query_id`、`text`、`show_alert` | ACK 当前 Interaction Bot 收到的 inline keyboard callback；不能点击第三方 Bot 按钮 |
| `payout` | `amount`、`text`、`reply_to_message_id`、`reply_to_user_id`、`reply_to_display_name`、`reply_to_username`、`reply_to_search_limit`、`reply_anchor_missing_text` | UserBot 发奖动作；有消息 ID 时直接回复，否则可按用户 ID 查找近期发言作为锚点。公开名必须来自安全身份 facade，匿名管理员不传 username。超限拒（`error_code=payout_limit_exceeded`），瞬时失败自动进补偿队列重发，插件无需自己重试（见上文 payout 语义） |
| `end_session` | 无 | 本次入口处理完成后不保留交互会话，适合彩票、红包等长期轮回插件 |

通道原则是：**触发方式决定会话通道，插件默认不感知通道，框架负责路由和执行**。命令触发的普通会话收发走 userbot，关键词/付款/按钮触发的普通会话收发走交互 Bot。能力固定路由有两个例外：`payout`、收款确认和发奖等钱相关动作永远走 userbot；`send_rich_message` 默认走 Interaction Bot，只有明确指定时才走 Layer 228 Userbot。其他动作只有在跨通道公告、特殊管理提示或迁移桥兼容时才显式写 `send_via`：

| send_via | 含义 | 约束 |
| --- | --- | --- |
| `interaction_bot` | 由交互 Bot 发送群内题面、答复、图片、会话提示 | 高级覆盖值；别名 `bot` |
| `userbot_reply` | 由当前账号 worker 的 userbot 代发指定消息 | 高级覆盖值；适合确有账号身份需要的动作 |
| `auto` | 按平台默认候选顺序尝试 | 迁移兼容值；新互动插件通常不需要使用 |

入口未声明 `result_contract.send_via` 时，平台按会话通道和动作类型决定实际发送方。入口声明了 `result_contract.actions` 或 `result_contract.send_via` 时，运行时把它作为可见契约和调试依据：插件调用未声明动作或未声明通道会写入 runtime log、交互中心调试面板和插件 lint 告警，但不会因为“未声明”本身静默丢弃动作。`reply_markup` 在 interaction bot 会话中由 Bot API 承接；在 userbot 会话中由平台降级成文本编号并把玩家回复合成为 callback 事件。`bbot_notice` / `notice` / `notice_bot` 已移除且不兼容，不再作为插件主动发送通道；插件显式请求这些旧通道会返回明确失败并提示迁移到会话通道模型。群里已有的转账结果通知 Bot 只作为外部到账证据来源，TelePilot 监听它来确认付款，不用它发送插件结果。涉及奖金、补发、转账、催付的插件必须在 `settlement` 或 `payout` 中写清职责：普通 Bot 只能公告和给出可对账结果，真正收款确认和发奖仍由账号 worker 的 userbot 代发或由平台受控结算流程处理。

普通互动推荐不写通道：

```python
await ctx.messages.send(
    text="题面或回复内容",
    reply_to_message_id=event_from_interaction_payload(payload).message.message_id,
)

await ctx.messages.send_rich(
    html="<h1>任务状态</h1><ul><li><input type=\"checkbox\" checked>已完成</li></ul>",
)

await ctx.messages.send(
    chat_id=-1001234567890,
    text="指定会话发送，仍由平台按会话通道执行",
)

await ctx.messages.send_photo(
    photo=image_bytes,
    filename="round.png",
    caption="题面",
    save_message_id_key="round",
)

await ctx.messages.edit_caption(
    message_id_key="round",
    caption="题面\n\n答对结果",
)
```

推荐迁移路径：旧插件继续返回 `list[dict]` 标准动作可以兼容；新插件或重构插件优先调用 `ctx.messages.send/send_rich/send_photo/send_file/edit/edit_caption/delete/pin/click_callback_button/answer_callback`。`ctx.messages` 只生成或提交标准动作，不会暴露 Bot Token、UserBot session 或 callback data。

框架层源码位于 `backend/app/services/interaction/`：`contracts.py` 负责记录 `result_contract` 告警与旧通道失败，`delivery.py` 负责受控发送、编辑、删除、置顶、按钮 ACK、媒体发送和 message_id 保存。

#### Contract Guard 行为

Contract Guard 不是公共插件市场式硬沙箱，而是个人可信插件标准下的契约提示器：

| 场景 | 运行时行为 |
| --- | --- |
| 调用未声明 `result_contract.actions` 的动作 | 记录 `guard_level=warning`，动作继续进入执行链路 |
| 显式请求未声明 `result_contract.send_via` 的高级覆盖通道 | 记录 `guard_level=warning`，按可执行能力尝试并保留审计 |
| `send_via` 同时包含受控通道和旧 `notice` / `bbot_notice` / `notice_bot` | 整个动作记录 `guard_level=failed`，返回 `send_channel_deprecated`，不做自动改写 |
| `send_via` 只包含未知值 | 记录 `guard_level=failed`，返回不可执行失败和迁移提示 |
| `send_via` 同时包含受控通道和非旧未知值 | 记录 `guard_level=warning`，保留可执行受控通道并继续执行 |
| `send_rich_message` 未指定通道或使用 `auto` | 默认选择 `interaction_bot`，保持既有行为 |
| `send_rich_message` 只指定 `userbot_reply` | 使用 Layer 228 Userbot；能力或格式不满足时返回稳定错误码，不降级 |
| 交互 Bot token 缺失、UserBot worker 离线、Telegram API 失败 | 返回客观能力失败，不伪装成功 |

#### 标准事件信封

`payload` 本身就是标准事件信封。新插件不要把旧平铺字段当主路径，也不要依赖 `payload["event"]`；如果想少写字段判断，优先使用 `event_from_interaction_payload(payload)`。

```json
{
  "source": {
    "type": "payment_confirmed",
    "channel": "interaction_bot",
    "driver": "telegram_bot_api",
    "account_id": 1,
    "chat_id": -100123,
    "chat_type": "supergroup",
    "update_id": 10,
    "message_id": 81,
    "callback_query_id": null,
    "callback_data": null
  },
  "message": {
    "chat_id": -100123,
    "message_id": 81,
    "text": "转账成功...",
    "entities": [],
    "media": null,
    "date": null,
    "reply_to_message_id": 80
  },
  "chat": {
    "id": -100123,
    "type": "supergroup",
    "title": null,
    "username": null
  },
  "sender": {
    "user_id": 8980553289,
    "display_name": "转账通知 Bot",
    "username": null
  },
  "actor": {
    "user_id": 111,
    "display_name": "AAA",
    "username": "aaa"
  },
  "source_actor": {
    "user_id": 8980553289,
    "display_name": "转账通知 Bot"
  },
  "payment": {
    "status": "confirmed",
    "amount": 100,
    "payer_user_id": 111,
    "payer_display_name": "AAA",
    "receiver_display_name": "BBB",
    "notice_sender_user_id": 8980553289,
    "notice_message_id": 81,
    "source_message_id": 81,
    "reply_to_message_id": 80
  },
  "player": {
    "user_id": 111,
    "display_name": "AAA",
    "username": "aaa",
    "identity_key": "tg:111",
    "identity_confidence": "reply_context"
  },
  "reply_to": {
    "message_id": 99,
    "user_id": 111,
    "display_name": "AAA",
    "text": "+100"
  },
  "trigger": {
    "type": "payment_confirmed",
    "rule_id": "game24-ticket",
    "rule_name": "24 点门票",
    "module_key": "game24",
    "entry_key": "start_paid_game"
  },
  "session": {
    "key": "account_bot:interaction_session:...",
    "scope": "chat",
    "ttl_seconds": 3600,
    "active": true,
    "data": {}
  },
  "raw": {
    "update_id": 10,
    "message_id": 81,
    "event_type": "payment_confirmed",
    "rule_id": "game24-ticket",
    "module_key": "game24",
    "entry_key": "start_paid_game",
    "parsed": {
      "payer_name": "AAA",
      "receiver_name": "BBB",
      "amount": 100
    }
  },
  "module_config": {
    "prize": 200
  }
}
```

信封字段说明：

| 字段 | 说明 |
| --- | --- |
| `source` | 事件来源、事件类型、update/message/callback 基础索引；`source.type` 是插件分流主字段 |
| `message` | 当前消息文本、消息 ID、回复目标、实体、媒体和可选 Rich Message 摘要；`text_source` 标记文本来源 |
| `chat` | 当前会话 ID、类型、标题和 username；标题可能为空 |
| `sender` | Telegram 实际发送者；转账触发时通常是外部转账通知 Bot |
| `source_actor` | 实际发来本条 Telegram 消息的 Bot/用户。转账触发时通常是可信转账通知 Bot，不应当作玩家 |
| `actor` | 当前事件的业务行为主体。答题、按钮点击、关键词触发时通常就是发送者；付费开局时平台会尽量映射到付款玩家 |
| `payment` | 可信转账通知 Bot 已确认到账后的结构化凭证，包含金额、付款人、收款人和通知消息信息 |
| `player` | 付费开局绑定的玩家身份。独玩/按钮玩法应优先读取它，并检查 `player.user_id` 是否存在 |
| `reply_to` | 本动作应引用的原消息或被回复对象，中奖公告必须尽量带上 |
| `trigger` | 命中的规则、入口、消息和触发类型；用于排障和幂等 |
| `session` | 平台会话标识、作用域、TTL 和数据；插件内部状态 key 应与它一致 |
| `raw` | 脱敏后的 Telegram 更新摘要，只用于排障，不作为常规业务字段 |

`payload_contract` 用来声明插件对上述信封的要求。平台和前端可以据此校验规则是否能保存，排障时也能判断是“事件没到”还是“字段不满足”。不要把敏感原文、Bot Token、完整付款通知文本写进信封；只传插件业务需要的结构化字段。

#### 群内安全公开身份

Telegram 按钮回调的 `payload.sender` 必须保留真实点击者，供权限校验、幂等、每日限制和发奖使用；但匿名管理员点击按钮时，这个对象同样包含真实姓名，因此不得直接用于群内文案。平台提供统一身份解析器和姓名清洗函数：

```python
from app.worker.plugins.base import resolve_public_sender_identity, sanitize_public_display_name

identity = await resolve_public_sender_identity(
    ctx,
    chat_id=payload["chat"]["id"],
    user_id=payload["sender"]["user_id"],
    fallback_display_name=payload["sender"].get("display_name") or "",
)

# 只把 identity.display_name 写入公开消息。
# user_id 仍使用 payload.sender.user_id 做业务校验。

# 已有一个不经过身份解析的公开标签时，也必须先清洗。
safe_label = sanitize_public_display_name(raw_label, limit=10)
```

结算、排行榜等多人名单使用 `resolve_public_sender_identities(ctx, chat_id=..., senders={user_id: name})` 批量解析；平台通过 `ctx.identities` 使用不受插件沙箱影响的内部 UserBot 读取管理员目录和成员权限，不依赖 Interaction Bot 的 `getChatMember`。身份无法由 UserBot 确认时平台会隐藏姓名，而不会回退按钮回调中的真实姓名。平台不会把 Bot Token、原始客户端或成员列表交给插件。

身份解析结果不做应用层缓存：每次调用都会重新读取当前群管理员目录和成员权限。UserBot 实体恢复只使用本地实体缓存或 Redis 中可重新校验的近期消息 `message_id`；缓存未命中时不会在按钮 callback 内扫描群历史。近期消息锚点不会缓存姓名、username、管理员状态或标签。

身份解析返回的名称已统一调用 `sanitize_public_display_name()`：移除 Unicode 控制符、零宽格式符、各类空白与不可见填充字符，并限制为最多 10 个字符；清洗后为空时使用“匿名用户”。这只解决公开姓名安全，不是 HTML/Markdown 转义，插件仍须按实际 `parse_mode` 转义后再发送。匿名管理员若无法由 UserBot 安全确认，会按 fail-closed 结果隐藏姓名。

明确要求锁定账号 UserBot 自身视角时，可直接调用专用 facade：

```python
identity = await ctx.identities.resolve_userbot(
    chat_id=chat_id,
    user_id=user_id,
    fallback_display_name=name_from_userbot_message,
)
```

`resolve_userbot()` 不调用 Interaction Bot；姓名以 UserBot 实体和传入的 UserBot 消息姓名为准，因此会保留该账号保存的联系人姓名。它仍会读取 UserBot 群权限并隐藏匿名管理员。

返回对象字段：

| 字段 | 说明 |
| --- | --- |
| `user_id` | 真实 Telegram User ID，只用于业务校验，不等于可公开姓名 |
| `display_name` | 可安全写入群消息的名称；已过滤不可见字符并限制为 10 个字符，匿名管理员为清洗后的标签，无标签时为“匿名管理员” |
| `is_anonymous_admin` | 当前成员是否开启匿名管理员身份 |
| `is_admin` | 当前成员是否为本群管理员；由 UserBot 成员权限或管理员目录确认，不能通过是否存在标签推断 |
| `tag` | Telegram 成员标签或管理员自定义头衔；普通成员存在标签时也不会覆盖 `display_name` |
| `resolved` | 是否成功读取群成员权限；为 `false` 时 `display_name` 固定使用隐藏身份的回退值 |

普通消息事件会同步投影 `sender.is_anonymous_admin` 与 `sender.tag`。匿名管理员消息的 `sender.user_id` 为 `null`，`sender.display_name` 已使用 `sender_tag` / `author_signature`；普通成员即使带 `sender_tag`，`sender.display_name` 仍是其姓名。插件不要用“是否存在标签”判断匿名状态，只能使用 `is_anonymous_admin`。

付费触发有两个证据源：UserBot/回复上下文负责补充付款玩家身份，可信转账通知 Bot 负责证明到账成功。插件不得把普通 `+金额` 文本当作到账依据；只有 `source.type=payment_confirmed` 且 `payment.status=confirmed` 才表示平台已经通过转账通知完成金额、收款人和规则校验。如果转账通知只提供付款人名称，平台会把 `player.identity_confidence` 标为 `name_only`；`participant_policy=solo_owner` 或 `paid_pool` 的入口会先要求付款人点击确认来获得真实 `player.user_id`。

入口可声明 `participant_policy` 来描述参与边界：

| 值 | 说明 |
| --- | --- |
| `open_race` | 一人付款/关键词开局，全群可参与抢答或竞猜 |
| `solo_owner` | 只有开局付款人/触发人可继续操作，适合 21 点、个人按钮流程 |
| `paid_pool` | 只有已确认付费的玩家池可参与，适合多人付费入场 |
| `notify_only` | 只做通知或一次性动作，不建立玩家操作边界 |

`solo_owner` / `paid_pool` 的按钮回调明确指向牌桌操作，平台会在调用插件前完成参与者校验。普通群消息具有歧义，平台会先让插件判断是否属于当前会话；插件返回业务动作后才应用参与者门禁，返回零动作或仅返回 `no_session` / `end_session` 时静默放行。插件仍必须在修改内部状态前校验消息发送者是否属于自己的玩家集合，不能把平台门禁当成业务状态校验的唯一依据。

`interaction_entries` 中的 `session_scope` 是插件会话作用域，必须按插件业务形态声明。它和交互规则里的 `concurrency` 不是一回事：

| 字段 | 归属 | 含义 | 示例 |
| --- | --- | --- | --- |
| `interaction_entries[].session_scope` | 插件入口声明 | 插件会话怎么保存和路由后续 `message` 事件 | 九宫格、24 点、猜数字填 `chat` |
| 交互规则 `concurrency` | 规则层 | 规则的触发/限流对象，用于每用户 CD、每日次数、触发去重 | 群友每天最多置顶 2 次可填 `user` |

可选值：

- `chat`：同一个群内同一时间只开一局，适合 24 点、九宫格、猜数字、诗词填空、红包这类公共抢答或公共流程。
- `user`：同一个用户一条会话，适合个人查询、个人表单、每个人互不影响的私有流程，例如 `pt_promote.promote_torrent`。
- `none`：入口本身不需要平台保存会话，适合只执行一次就结束的动作；插件仍可在内部维护自己的长期状态。

后端保存规则时会优先读取 `plugin.json` / `manifest.py` 中声明的 `session_scope`，并写入规则的 `module_session_scope`。这样即使规则为了“每个群友 6 小时 CD、每日 2 次”设置了 `concurrency=user`，九宫格这类 `session_scope=chat` 的群局也仍然会按群保存会话，其他群友回复 `1-9` 才能进入同一局。

如果插件没有声明 `session_scope`，平台只能回退到规则 `concurrency`，这很容易让群局被误判成用户私有会话。所有声明了 `interaction_entries` 的插件都必须显式填写 `session_scope`。

### 事件过滤与 rule_bound

当前事件订阅支持 `filters` 过滤。已知过滤键按平台约定校验，常见值包括 `keywords`、`contains`、`callback_data`、`commands` 和 `rule_id`；未知 filter key 会被保留用于兼容，但会触发 warning，便于插件作者尽快修正。

`rule_bound` 订阅如果声明了 `filters.rule_id`，它必须与当前触发上下文里的 `trigger.rule_id` 完全相等，否则本次订阅会被判定为 `filter_not_matched`。这条规则用于把“规则绑定”从松散匹配收紧到明确的一对一绑定，避免跨规则误投递。

#### 入口参数来源

交互入口 payload 由平台运行时组装，当前不会在后端再次读取 manifest 默认值或插件账号级配置做自动合并。有效来源如下，越靠后越容易覆盖同名字段：

```text
交互规则 module_config
< 转账事件动态参数（payer / receiver / amount / chat_id 等）
```

`input_schema` 的默认值主要给前端表单预填使用；旧规则、API 直接写入或第三方客户端不一定会带上这些默认值，所以插件仍应在代码里为关键参数提供兜底。`module_config` 只保存当前交互规则的覆盖项，例如“这条门票规则奖金为 200”。插件自身的通用配置仍放在插件配置页中，运行时从 `ctx.config` 读取，不能混进规则的 `module_config`。

`session_policy` 用来告诉平台和维护者会话如何结束、重复触发如何处理、TTL 多久。常见写法：

```json
{
  "ttl_seconds": 3600,
  "duplicate_start": "reject",
  "close_on": ["winner", "timeout", "session_close"],
  "max_active_per_scope": 1
}
```

`payload_contract` 描述输入，`result_contract` 描述输出。它们是文档化契约，也会成为 Contract Guard 告警依据。`result_contract.actions` 只能列标准动作；`result_contract.send_via` 只用于高级覆盖或迁移兼容的可见契约，不是普通互动必填项，也不是硬沙箱白名单；`settlement` 只说明结算/公告语义，不能让普通 Bot 直接拥有发奖权限。

#### 标准事件输入

平台调用交互入口时，会提供标准信封；历史适配层或旧规则还可能同时提供平铺字段。新插件不要依赖转账通知原文或旧平铺字段，优先读取 `source` / `message` / `chat` / `sender` / `actor` / `source_actor` / `player` / `payment` / `reply_to` / `trigger` / `session` 信封。

```json
{
  "source": {
    "type": "payment_confirmed",
    "channel": "interaction_bot",
    "driver": "telegram_bot_api",
    "account_id": 1,
    "chat_id": -100123,
    "message_id": 81
  },
  "message": {
    "chat_id": -100123,
    "message_id": 81,
    "text": "转账成功...",
    "reply_to_message_id": 80
  },
  "sender": {
    "user_id": 8980553289,
    "display_name": "转账通知 Bot"
  },
  "actor": {
    "user_id": 111,
    "display_name": "AAA"
  },
  "payment": {
    "status": "confirmed",
    "amount": 100,
    "payer_user_id": 111,
    "payer_display_name": "AAA",
    "receiver_display_name": "BBB",
    "source_message_id": 81,
    "reply_to_message_id": 80
  },
  "player": {
    "user_id": 111,
    "display_name": "AAA",
    "identity_key": "tg:111",
    "identity_confidence": "reply_context"
  },
  "trigger": {
    "type": "payment_confirmed",
    "rule_id": "game24-ticket",
    "module_key": "game24",
    "entry_key": "start_paid_game"
  },
  "session": {
    "key": "account_bot:interaction_session:...",
    "scope": "chat",
    "ttl_seconds": 3600,
    "active": true,
    "data": {}
  }
}
```

为了兼容历史插件，payload 顶层还可能同时带 `account_id`、`chat_id`、`payer_user_id`、`notice_message_id` 等平铺字段。它们只用于旧插件迁移期，不要在新插件里当作标准输入主路径。

#### 标准动作输出

交互入口或适配器应返回平台可执行的标准动作，或通过 `ctx.messages` 缓存这些动作，而不是直接调用 Telegram API。交互 Bot runtime 统一负责发送、回复、删除、置顶、按钮 ACK 与基础动作执行。需要跨消息保存业务状态时，插件应优先使用 `ctx.storage`；它会按账号和插件自动隔离命名空间。安装插件的 `ctx.redis` 是 `PluginRedisFacade`，会自动添加同一命名空间并限制可调用方法，不要自行拼接完整 `plugin_store:` 前缀。需要 NX 时可使用 facade 已开放的 `set(..., nx=True, ex=...)`；CAS、Lua、pipeline、分布式锁或严格原子抢占不在公开能力内，应交给平台会话/结算链路或官方受控实现。

```json
[
  {
    "type": "send_message",
    "text": "24 点开始..."
  },
  {
    "type": "send_message",
    "text": "答对了：AAA\n题目：24 点 [1 5 5 5]\n奖金：123",
    "reply_to_message_id": 99,
    "settlement": {
      "status": "winner_confirmed",
      "winner_user_id": 111,
      "amount": 123,
      "currency": "points"
    }
  },
  {
    "type": "send_photo",
    "photo_base64": "...",
    "filename": "puzzle.png",
    "caption": "题面"
  }
]
```

#### 端到端示例：24 点交互入口

下面是 `payment_confirmed` / `keyword` 开局、`message` 答题、`session_close` 清理的最小形态。真实插件可以把 `generate_24_puzzle()`、`check_answer()`、`render_start()` 拆成纯函数复用。示例用 `ctx.storage` 做持久化状态；它适合普通会话状态，不适合充当原子抢答锁。

```python
import secrets
import time
from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class Game24Plugin(Plugin):
    key = "game24"
    display_name = "24点游戏"

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if entry_key != "start_paid_game":
            return None
        event = event_from_interaction_payload(payload)
        event_type = event.type
        chat_id = int(event.message.chat_id or 0)
        if not chat_id:
            return []

        state_key = f"game24:{chat_id}"
        if ctx.storage is None or not ctx.storage.available:
            if event_type in ("payment_confirmed", "keyword"):
                return [
                    {
                        "type": "send_message",
                        "text": "当前运行上下文没有可用状态存储，无法启动需要持续会话的游戏。",
                    }
                ]
            return []

        if event_type in ("payment_confirmed", "keyword"):
            numbers = generate_24_puzzle()
            module_config = payload.get("module_config") if isinstance(payload.get("module_config"), dict) else {}
            prize = int(module_config.get("prize") or 123)
            state = {
                "account_id": ctx.account_id,
                "chat_id": chat_id,
                "numbers": numbers,
                "prize": prize,
                "active": True,
                "game_id": secrets.token_hex(8),
                "created_at": time.time(),
            }
            await ctx.storage.set(state_key, state, ttl=3600)
            return [{"type": "send_message", "text": render_start(numbers, prize)}]

        state = await ctx.storage.get(state_key, default={})
        if event_type == "message":
            if not state.get("active") or not check_answer(event.message.text, state["numbers"]):
                return []
            claim_key = f"game24_claim:{chat_id}:{state['game_id']}"
            if await ctx.storage.get(claim_key):
                return []
            await ctx.storage.set(claim_key, event.message.message_id or "", ttl=3600)
            state["active"] = False
            await ctx.storage.set(state_key, state, ttl=3600)
            return [
                {
                    "type": "send_message",
                    "text": f"答对了：{event.actor.display_name or '玩家'}\n奖金：{state['prize']}",
                    "reply_to_message_id": event.message.message_id,
                }
            ]

        if event_type == "session_close":
            if state.get("active"):
                state["active"] = False
                await ctx.storage.set(state_key, state, ttl=3600)
            return []

        return []
```

#### 兼容边界

1. 原插件本体不得为了交互 Bot 直接改写 `commands` / `on_message` 语义；UserBot 入口和交互 Bot 入口是两套边界。
2. 可以把纯业务逻辑抽到共享函数，例如题目生成、答案校验、渲染模板；UserBot 插件和交互 Bot 适配器共同调用这些纯函数。
3. 插件不处理 Bot Token、外部转账通知原文格式、转账过滤、发奖账号；这些都属于平台层职责，钱相关动作也不该放进交互 Bot 的高频入口。
4. 交互 Bot 中奖公告必须引用赢家的答案消息，方便 `UserBot` 账号按结构化公告自动回复发奖或补发奖金。
5. 若插件未声明 `interaction_entries`，前端不应把它展示为可由交互 Bot 启动的插件。旧 `config_schema["x-interaction-entries"]` 仅作为兼容入口，新插件不要再用旧字段。
6. `interaction_entries[].session_scope` 必须和插件内部状态 key 一致：群局状态 key 应包含 `chat_id`，用户私有流程状态 key 应同时包含 `chat_id` 和 `user_id`。
7. 返回 `end_session` / `close_session` / `no_session` 时，平台会清理规则会话；插件自己的 Redis 状态仍由插件负责清理。
8. `preserve_command_trigger` 必须保持为 `true`。交互入口新增后，原本能用的 UserBot 指令仍要按原指令名、原参数和原权限工作。
9. 新插件建议声明 `dispatch_modes`，让前端明确入口来源；不要用 `message_channels` / `money_channel` 表达发送通道，普通动作继承 `session.channel`，`payout` 永远 userbot。
10. 使用 inline keyboard 时，入口必须声明 `callback_query` 事件；按钮动作只通过 `send_message.reply_markup` 交给交互 Bot 发送，`userbot_reply` 不承接按钮。
11. `settlement` / `result_contract` 只描述可对账结果和平台动作，不得把发奖、转账、催付等钱相关动作塞进交互 Bot 高频入口。

---

## 6. 指令系统（command API）

**安全底线：普通指令只能由当前 UserBot 账号自己发出的 outgoing 消息触发。** 群成员、普通用户、频道消息等 incoming 消息不能直接触发插件 `commands`。`owner_only=False` 只表示插件的 `on_message` 可以监听普通成员消息，不表示开放指令执行权限。

**前缀底线：插件不能在用户可见文案、帮助、错误提示、配置默认值、预览或示例里硬编码英文逗号 `,` 作为指令前缀。** 指令名配置只保存裸命令名，例如 `game`、`help`、`cancel`；真正展示给用户时必须使用 `{prefix}` 占位符或运行时当前前缀拼接。

必须使用当前命令前缀的场景：

- 帮助/用法模板：写 `{prefix}{command}`，不要写 `,{command}` 或 `,game`。
- 错误提示里的示例：运行时用 `current_command_prefix()` 拼接，例如 `f"{current_command_prefix()}{command} 100"`。
- 配置页预览：从 `getSystemSettings().command_prefix` 注入 `{prefix}`，接口未返回时才用 `,` 兜底。
- `plugin.json` / `manifest.py` / `config_schema` 的默认模板：默认值应包含 `{prefix}`，不要包含固定 `,` 前缀。
- 交互 Bot、通知 Bot、定时任务或自动回复里展示“如何发送命令”的文字：同样使用 `{prefix}` 渲染。

允许保存为配置项的是“裸命令名”，不是完整命令文本：

```python
# 推荐：command 只保存裸指令名，模板使用 {prefix}
"command": {"type": "string", "default": "game"}
"help_message_template": {"type": "string", "default": "{prefix}{command} 100 - 开始一局"}

# 不推荐：默认值、帮助或预览写死英文逗号
"help_message_template": {"type": "string", "default": ",game 100 - 开始一局"}
```

红包、抢答、24 点、猜数字这类“公共参与 + 私有管理”的新插件，标准会话链路应先声明 `event_subscriptions`，由 Event Bus 接收玩家关键词、答案、callback、inline 和付款确认，再通过 `ctx.messages` 或标准 action 输出结果。下面的 `commands` / `on_message` 模型仅用于管理员命令兼容和仍未迁移的旧 hook 插件，不应作为公开玩法的新模板：

- 开局、发红包、撤销、强制结束、查看管理状态等管理动作优先声明为 `command` 事件；保留旧 hook 时才写成 `commands`，且只能由本账号 outgoing 指令触发。
- 领取口令、答题、参与投票等普通成员行为优先订阅 `message` / `callback_query` / `inline_query`；保留旧 hook 时才写在 `on_message`，通过普通文本判断，不要求用户发送系统指令前缀。
- 如果自动回复、定时任务等平台内部动作需要“代替本账号执行指令”，使用平台内部派发能力，不让普通 incoming 消息直接进 `commands`。
- 自动回复需要把群友输入的参数传给指令时，可以用变量模式：例如模式 `置顶 id=数字` 会匹配群友消息 `置顶 id=12345`，回复内容 `{prefix}pt {id}` 会使用 `12345`；游戏金额建议写 `num=数字`，可选参数写 `num=数字?`，`?` 表示这个 `num=...` 参数整体可以不填，默认值写 `{num|1000}`。熟悉正则时也可用模式 `^置顶\s+(\d+)$`、回复内容 `{prefix}pt {1}`。这些自动命令仍必须通过自动指令白名单，并受规则冷却、冷却对象和每人每日上限限制；冷却时间支持 `2s`、`2m`、`2h`、`2d`，纯数字按秒处理。自动命令成功后会按规则名称或 `usage_label` 把“今日已成功置顶促销 1/2 次”追加到结果底部；冷却中也会提示剩余 CD 和今日次数，达到每日上限时提示当日不可再用；管理员可回复群友消息发送 `{prefix}arcd`，或发送 `{prefix}arcd 用户ID` 重置当前会话相关的自动回复会话/用户冷却与该用户今日次数。

### 指令派发流程

1. 当前账号 outgoing 消息到达 → 检查前缀匹配
2. 提取指令名和参数
3. 检查别名（贪心最长匹配）
4. 遍历已注册插件，调用 `on_command(ctx, cmd, args, event)`
5. 第一个返回 True 的插件接管，后续不再传递

### on_command 签名

```python
async def on_command(
    self,
    ctx: PluginContext,       # 上下文
    cmd: str,                 # 指令名（如 "weather"）
    args: list[str],          # 参数列表
    event: NewMessage.Event,  # 原始事件
) -> bool:
    """返回 True 表示已处理。"""
```

### 别名支持

指令别名支持多词贪心匹配和参数透传：

```
用户: ,fy zh hello
→ 别名 "fy zh" → "translate"
→ 参数透传: translate hello
```

---

## 7. 消息监听

```python
class MyPlugin(Plugin):
    message_channels = {"incoming"}

    async def on_message(self, ctx: PluginContext, event) -> None:
        """监听所有匹配方向的消息。"""
        # 兼容 NewMessage.Event 与裸 Message；Telethon 1.44 的方向字段是 message.out。
        msg = getattr(event, "message", event)
        if bool(getattr(msg, "out", False)):
            return  # 忽略自己发的
        # 处理逻辑
```

### channels 类型

| 值 | 说明 |
|---|------|
| `incoming` | 别人发给本账号、群、频道的消息 |
| `outgoing` | 当前 UserBot 账号自己发出的消息 |

> 注意：当前 loader 的方向过滤只有 `incoming/outgoing` 两类。群组、私聊、频道请在 hook 内用 `event.is_group` / `event.is_private` / `event.is_channel` 或 `chat_id` 判断。

### 事件对象兼容写法

插件收到的对象通常表现为 Telegram 消息事件，但在测试、热重载、代理属性等场景里，也可能表现得更像裸 `Message`。Telethon 1.44 的 `NewMessage.Event` 不提供 `event.outgoing`；方向应从事件的 `message.out`（或裸 `Message` 的 `out`）读取。因此建议用 `getattr` 做兼容，不要直接假设 `event.message.id` 一定存在：

```python
def event_message(event):
    return getattr(event, "message", event)

def event_text(event) -> str:
    msg = event_message(event)
    return str(getattr(event, "raw_text", None) or getattr(msg, "raw_text", None) or "").strip()

def is_outgoing(event) -> bool:
    msg = event_message(event)
    return bool(getattr(msg, "out", False))
```

这样可以避免类似 `'Message' object has no attribute 'outgoing'` 的运行时错误。

---

## 8. Conversation 工具（仅内部/真实客户端兼容）

`Conversation` 的实现会在进入上下文时调用客户端的 `add_event_handler`，发送和点击时还会
直接使用 TelegramClient/raw MTProto。它只适合 builtin 或其他明确持有真实
`TelegramClient` 的内部兼容代码。

普通安装插件的 `ctx.client` 是 `SandboxClient`：它不开放 `add_event_handler`，也会拒绝
`GetBotCallbackAnswerRequest` 等 raw MTProto，因此不能把 `ctx.conversation()` 当作可靠的
公开 API。`event.message.click(...)` 和 `message.buttons[row][column].click()` 都会被安全
包装拒绝。普通安装插件点击第三方 Bot callback 必须改用
`ctx.messages.click_callback_button(...)`；具体限制见
[Inline 按钮的两种完全不同场景](#inline-按钮的两种完全不同场景)。

以下示例仅用于真实客户端的内部兼容代码（如与 @BotFather 交互）：

```python
async with ctx.conversation("@BotFather") as conv:
    await conv.send("/newbot")
    resp = await conv.get_response(timeout=30)
    print(resp.text)

    # 点击内联按钮
    await conv.click_button(msg, row=0, col=0)
```

### API

| 方法 | 说明 |
|------|------|
| `send(text, **kwargs)` | 发送文本/文件/图片 |
| `get_response(timeout)` | 等对方回复 |
| `click_button(msg, row, col)` | 点击 inline keyboard |
| `mark_read()` | 标记已读 |
| `close()` | 清理 handler |

> 不要把本节的 `click_button()` 复制到普通远程/本地安装插件；它不是安装插件 Contract。

### 超时处理

```python
from app.worker.conversation import ConversationTimeout

try:
    resp = await conv.get_response(timeout=10)
except ConversationTimeout:
    await conv.send("超时了，请重试")
```

---

## 9. 插件日志

插件日志会进入后台的“日志中心 → Runtime → 插件日志”分页，和“消息日志”“系统日志”分开显示；涉及 sudo、Config Bundle confirm、userbot_reply confirm 等安全决策的记录则在“日志中心 → Audit”查看。

### 如何写日志

插件运行时通过 `ctx.log(level, message, **detail)` 输出日志：

```python
await ctx.log(
    "info",
    "自动回复命中：关键词 hello，准备发送回复。",
    chat_id=event.chat_id,
    rule_id=rule.id,
    keyword="hello",
)
```

日志会自动带上：

- `source="plugin"`
- `plugin_key`
- `account_id`
- `level`
- `message`
- `detail`

### 日志写法规范

- `message` 写给人看：用一句通俗的话说明发生了什么。
- `detail` 写给排障看：放 `chat_id`、`rule_id`、`sender_id`、`message_preview`、`elapsed_ms` 等结构化字段。
- 不要在日志中写 API Key、Bot Token、session、完整文件路径、完整群聊长文本。
- 错误日志要说明“哪一步失败 + 失败原因 + 是否已跳过/重试/继续运行”。

推荐：

```python
await ctx.log(
    "error",
    f"图片生成失败：上游返回限额错误，本次任务已停止。原因：{err_type}",
    chat_id=chat_id,
    elapsed_ms=elapsed_ms,
)
```

不推荐：

```python
await ctx.log("error", f"failed: {raw_exception_with_token}")
```

### loader 自动记录的插件异常

如果插件 `on_message` 抛异常，loader 会自动写一条插件日志，并附带：

- `plugin_key`
- `direction`
- `chat_id`
- `sender_id`
- `message_preview`
- `traceback`

这类异常不会让 worker 崩溃，当前消息会被跳过，其它插件继续运行。

---

## 11. 清理生命周期（cleanup）

参考 TeleBox 的三种风格：

| 风格 | 适用场景 | cleanup 行为 |
|------|---------|-------------|
| `resource` | 持有定时器/子进程/网络连接 | 真正释放资源 |
| `reset` | 持有 db/缓存/配置引用 | 引用置空 |
| `no-op` | 流程型插件，无长期资源 | 空方法 + 注释说明 |

### 统一约束

- **必须幂等**：重复调用不报错
- **不应依赖用户输入**
- **不应误伤系统级资源**：systemd 服务、iptables 等不要在 reload 时停掉

### 实现

```python
class MyPlugin(Plugin):
    _timer = None
    _db = None

    async def on_startup(self, ctx):
        self._timer = create_timer(...)
        self._db = get_db()

    async def on_shutdown(self, ctx):
        """resource 风格：释放资源"""
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._db:
            self._db = None
```

---

## 13. 前端集成

插件前端配置推荐分为两种配置形态，另有一类平台内置基础能力。历史 `schema` 只作为兼容别名保留，不再新增“Schema 弹窗”类插件。后续新增插件时，优先通过 `manifest.py` 的 `config_schema["x-ui-mode"]` 声明分类，前端会自动归类展示。

### 配置形态概览

| 分类 | 适用场景 | 大白话 | 典型功能 | 配置入口 |
|------|---------|--------|---------|---------|
| **规则配置页** | 多条规则独立配置，需 CRUD + 试运行 | 像自动化流水线：先建规则，规则只保存配置和 dry-run 输入 | 插件库 auto_reply / autorepeat、远程规则插件 | 专属配置页 |
| **单配置对象 / 通用独立配置页** | 每个账号只保存一份插件配置，或轻量插件只需要字段表单 | 像一个工具面板：配置好触发指令和参数，直接运行；普通字段由 schema 驱动渲染 | 插件库 game24 / math10 / codex_image / chatgpt_image、简单远程插件 / 小工具插件 | 专属或通用独立配置页 |
| **基础能力 — 平台内置** | 系统运行时常驻能力，不作为普通插件展示 | 像底座服务：给插件或平台调用，不强调启停 | scheduler | 平台功能页 |

**关键判断**：需要维护多条规则 → `rules`；只有一份账号配置或普通字段表单足够 → `single`；旧插件已经写了 `schema` → 按 `single` 通用独立页兼容；像调度器这种系统服务 → `platform`。这里的 `rules` 只表示配置页/CRUD/dry-run 形态，不是旧运行时规则驱动主路径；标准会话事件投递仍以 Event Bus + `event_subscriptions` + 标准 action 为主。通用 schema 独立页只读写单个配置对象，不会自动提供 Rules API 的列表、新建、编辑、删除或 dry-run，因此不能用来替代真正的规则 CRUD 页。

#### 自动分类规则

新增插件应在 `config_schema` 顶层声明 `x-ui-mode`：

```python
config_schema={
    "type": "object",
    "x-ui-mode": "single",  # 推荐：rules / single / platform；schema 仅作兼容别名
    "properties": {
        "command": {"type": "string", "title": "触发指令名", "default": "demo"},
    },
}
```

| `x-ui-mode` | 展示位置 | 说明 |
|-------------|----------|------|
| `rules` | 规则配置页 | 多条规则配置插件，通常有规则列表、创建/编辑、dry-run；不改变标准会话链路的投递方式 |
| `single` | 单配置对象 / 通用独立配置页 | 单配置对象或通用独立配置页，字段可由 `config_schema` 驱动 |
| `schema` | legacy alias | 旧别名；不要在新插件中使用，不再表示弹窗类 |
| `platform` | 基础能力 | 平台内置能力，不混在普通插件列表里 |

前端统一从 `frontend/src/lib/plugin-modes.ts` 读取分类。旧内置插件仍保留 key fallback，但新插件不要依赖 fallback。

---

### 统一配置页样式规范

所有账号级插件配置入口都使用独立页面，不再新增 Schema 弹窗或内部分类的用户可见分组。账号详情的“插件启停”页只展示“基础能力 · 平台内置”和“插件”两组，插件列表按 `feature.key` 首字母排序；用户界面和文档统一称“插件”，代码、API、数据库字段和 Manifest 仍保留 `plugin` / `feature` 命名。

配置页从上到下固定为：

1. 返回按钮 + 插件标题
2. 使用说明
3. 功能总开关
4. 插件配置（规则列表或字段表单）
5. 插件预览（建议项；没有预览时显示轻量提示）

#### 配置操作条

长表单页必须把保存操作放在“插件配置”卡片底部的 sticky 工具条中，参考 `ChatGPTImageConfig.tsx`、`CodexImageConfig.tsx`、`Game24Config.tsx` 和 `GenericPluginConfig.tsx`：

```tsx
<div className="sticky bottom-0 z-20 mt-4 rounded-b-lg border-t bg-background/95 px-6 py-3 shadow-[0_-8px_20px_rgba(15,23,42,0.06)] backdrop-blur supports-[backdrop-filter]:bg-background/85">
  <div className="flex flex-wrap items-center justify-between gap-3">
    <div className="text-sm">
      <div className="font-medium">配置操作</div>
      <div className="text-xs text-muted-foreground">
        {dirty ? "有未保存修改，保存后 worker 会热加载。" : "当前配置已同步。"}
      </div>
    </div>
    <div className="flex items-center gap-4">
      <Button disabled={!dirty || saveMut.isPending} onClick={handleSave}>
        保存配置
      </Button>
      <Button type="button" variant="ghost" className="px-0" disabled={!dirty || saveMut.isPending} onClick={resetForm}>
        撤销
      </Button>
    </div>
  </div>
</div>
```

规则配置页如果单条规则在 Dialog 内保存，主页面可以不放 sticky 保存条；但 Dialog 外的说明、总开关和规则列表顺序仍必须一致。

#### 使用说明卡片

“使用说明”必须是独立 `Card`，放在“功能总开关”之前。说明内容用一层 `rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground` 包住，再用短 bullet 写真实用法、指令示例、触发条件和排障入口。不要把使用说明写成页面顶部散落的提示块，也不要把总开关塞进说明卡。

规则配置页复用 `RuleInfoBox`，单配置和通用 schema 页面直接使用同样结构。指令示例必须读取当前系统前缀和当前配置中的指令名，不要写死 `,draw`、`,24d`、`,cximg`。

通用 schema 配置页不再提供默认兜底说明。插件只要声明 `config_schema` 并进入配置页，就必须自带详细使用说明。推荐在 schema 顶层写 `x-usage-guide`、`x-usage-instructions` 或 `x-usage-steps`；也可以继续提供只读字段 `usage_preview`、`usage_guide`、`usage_instructions`、`ai_usage_guide`。缺少这些内容时，插件中心会显示红色“高级规范警告”，配置页也会用红色警告替代说明内容。

#### 功能总开关卡片

“功能总开关”也必须是独立 `Card`，放在“使用说明”之后、“配置”之前。卡片右侧放 `Switch`，左侧展示说明、启用 Badge、`state` 和 `last_error`。关闭总开关表示当前账号不运行该插件，但仍允许进入配置页提前填写配置。

规则配置页复用 `RuleFeatureToggleCard`；单配置和通用页面按同样布局实现。不要再使用旧的“运行状态”卡片替代总开关。

#### 插件配置与宽度

配置主体必须独立成“插件配置”或“规则”卡片，宽度跟随页面容器自适应，不要给表单区域加 `max-w-lg`、`max-w-3xl` 这类窄宽限制。字段多时用响应式网格：

- 普通字段：`grid gap-4 md:grid-cols-2 xl:grid-cols-3`
- 小型配置：`grid gap-6 md:grid-cols-2`
- 复杂分组：外层 `CardContent className="space-y-6"`，内部再分组

字段控件统一使用项目内 `Input`、`Select`、`Switch`、`Textarea`、`Label`、`Button`、`Card`、`Badge`、`Table`。指令字段只填指令名，不填系统前缀；密码、Token 和只读预览字段要遵守现有脱敏和只读规则。

通用 schema 页允许插件在平台容器内声明更自由的布局，但不能注入任意 HTML、外链样式或脚本。可用声明：

- `x-ui-section`：把字段放进同名分组。
- `x-ui-order`：控制字段排序，数值越小越靠前。
- `x-ui-columns`：控制分组列数，允许 1 到 3。
- `x-ui-widget: "config-list"`：把 `array<object>` 渲染为多组配置列表，内置添加、编辑、复制、删除、启停和排序。
- `x-ui-widget: "multi-select"`：把枚举数组渲染为多选列表。
- `x-ui-widget: "list-select"`：把枚举字符串渲染为列表式单选。
- `x-ui-widget: "allowed-peer-multi-select"`：把 `array<integer>` 渲染为当前账号“允许会话”选择器，保存 Chat ID 数组；留空语义由插件自行定义，常用于群聊白名单。
- `x-ui-hidden: true`：隐藏兼容字段或内部字段，但仍保留保存链路。
- `config_actions` / `x-config-actions`：把插件后端动作渲染为字段旁按钮，动作只能调用插件的 `on_config_action`，不能执行任意前端脚本。

#### 插件预览

“插件预览”是独立 `Card`，位于“插件配置”之后。预览不是强制项，但强烈建议所有会发送消息的插件声明 `template_preview` 或 `*_preview`，让用户能用模拟上下文看到最终 Telegram 消息效果。没有预览字段时，通用配置页只显示建议提示，不阻断保存或运行。

#### 禁止回退

- 不新增 Schema 配置弹窗；当前入口是 `GenericPluginConfigPage` 独立页。`GenericPluginConfig.tsx` 可以复用 `ConfigDialog.tsx` 导出的 schema 解析/表单 helper 与类型，但 `ConfigDialog` 弹窗本身不是当前配置入口，也不是一种插件分类。
- 不在账号详情页展示内部分类名或 legacy schema 分组。
- 不把“使用说明”“功能总开关”“插件配置”“插件预览”合并到同一张卡片。
- 不把保存按钮放到页面顶部，或只放在滚动到底才能看到的位置。
- 不在用户界面继续使用“模块”指代可启停能力；面向用户统一称“插件”。

---

### 规则配置页（AutoReply / Autorepeat / 远程规则插件）

规则配置页每条 rule 存储独立的 `config` JSON，通过 CRUD API 管理。前端专属页面提供：规则列表 + 创建/编辑对话框 + 试运行（dry-run）。这只定义配置数据和页面形态；真正的 Telegram 消息投递仍应通过 Event Bus 的 `event_subscriptions`、标准事件信封和标准 action 完成。

`forward` 是核心兼容能力，当前插件中心不提供它的专属管理 UI，不应把历史 `Forward.tsx` 文件当成可访问入口或新规则页模板。通用 `GenericPluginConfigPage` 也不会把 `config_schema` 自动升级成规则 CRUD；需要多规则管理的插件必须有对应 Rules API 和专属页面。

#### 专属规则页适配清单

| # | 文件 | 修改内容 |
|---|------|---------|
| 1 | `frontend/src/api/types.ts` | 添加 `XxxRuleConfig` 接口（描述单条规则的 config 字段） |
| 2 | `frontend/src/pages/Plugins/configs/XxxConfig.tsx` | **新建**：规则列表页（参考当前可访问的 `AutoReply.tsx`） |
| 3 | 插件包内 `manifest.py` | `config_schema["x-ui-mode"] = "rules"`；新插件应放在远程仓库或 `plugins/local_imports/xxx/` 后由 Web 安装 |
| 4 | `frontend/src/App.tsx` | lazy import 新页面组件，并在通用 `:featureKey` 路由之前添加 `:aid/features/xxx` 显式路由 |
| 5 | `frontend/src/pages/Plugins/_shared/featureConfig.ts` | 在共享的 `FEATURE_CONFIG_PAGE_KEYS` Set 中添加 key |
| 6 | `backend/app/db/models/feature.py` | 添加 `FEATURE_XXX = "xxx"` 常量（如已有可跳过） |

#### 1. types.ts — RuleConfig 接口

```typescript
// frontend/src/api/types.ts
export interface AutorepeatRuleConfig {
  target_chat_id: number;   // 必填
  time_window?: number;     // 可选，默认 300
  min_users?: number;       // 可选，默认 5
}
```

接口字段应与 `manifest.py` 中 `config_schema.properties` 一一对应，必填字段不加 `?`。

#### 2. 新建配置页面

创建 `frontend/src/pages/Plugins/configs/XxxConfig.tsx`，核心结构：

```tsx
// 标准页面骨架（以 AutoReply 为模板）
import { useParams } from "react-router-dom";
// ... UI 组件导入

export function XxxConfig() {
  const { aid } = useParams<{ aid: string }>();
  const queryClient = useQueryClient();

  // ① 规则列表查询
  const { data: rules } = useQuery({
    queryKey: ["rules", Number(aid), "xxx"],
    queryFn: () => api.getRules(Number(aid), "xxx"),
  });

  // ② 创建/编辑对话框状态
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<RuleOut | null>(null);

  // ③ CRUD mutations（create / update / delete）
  // ④ 试运行 mutation（dry-run）
  // ⑤ 表单渲染 + 规则表格
}
```

**页面要素**：
- 顶部：返回按钮 + 插件标题
- 使用说明：独立 `Card`，复用 `RuleInfoBox`，写清触发方向、指令/规则用法和排障入口
- 功能总开关：独立 `Card`，复用 `RuleFeatureToggleCard`，展示启用 Badge、运行状态和最近错误
- 规则卡片：标题为“规则”，右侧放新建按钮，主体为规则表格（序号 / 关键字段 / 启用状态 / 操作按钮）
- 对话框：创建/编辑表单，字段来自 RuleConfig；保存按钮留在 Dialog 内
- 试运行：选规则 → 填样本消息 → 显示命中结果

#### 3. manifest.py — UI 分类

```python
config_schema={
    "type": "object",
    "x-ui-mode": "rules",
    "properties": {
        "target_chat_id": {"type": "integer", "title": "目标聊天"},
        "enabled": {"type": "boolean", "title": "启用", "default": True},
    },
}
```

#### 4. App.tsx — 专属路由

```tsx
// ① lazy import（与现有配置页一致）
const XxxConfig = lazy(() => import("@/pages/Plugins/configs/XxxConfig"));

// ② 放在 :aid/features/:featureKey 通用路由之前
<Route path=":aid/features/xxx" element={<XxxConfig />} />
```

路由路径格式固定为 `:aid/features/{plugin_key}`，`plugin_key` 必须与 `MANIFEST.key` 一致。当前代码没有 `FEATURE_CONFIG_PAGES`；不要按旧文档重新创建这份重复注册表。没有专属 React 页面时，`App.tsx` 已有的 `:aid/features/:featureKey` 会承接通用 schema 配置页，无需新增路由。

#### 5. FEATURE_CONFIG_PAGE_KEYS — 共享入口点

账号详情与插件中心统一复用同一个 helper，不维护两份 Set。新增专属配置页时只改这一处：

```tsx
// frontend/src/pages/Plugins/_shared/featureConfig.ts
const FEATURE_CONFIG_PAGE_KEYS = new Set([
  "auto_reply", "autorepeat", "chatgpt_image", "codex_image", "scheduler", "game24",
  "xxx",  // ← 新增
]);
```

**作用**：Set 中的 key 会让账号详情和插件中心的“配置”按钮跳转到专属页面路由 `/accounts/:aid/features/xxx`；不在 Set 中的 key 应进入 `GenericPluginConfigPage` 通用独立配置页。该页可复用 `ConfigDialog.tsx` 中的 schema helper，但不会打开 `ConfigDialog` 弹窗；`ConfigDialog` 也不再代表一类插件形态。

#### 6. feature.py — 后端常量

```python
# backend/app/db/models/feature.py
FEATURE_XXX = "xxx"
```

此常量只在 TelePilot 主仓库为插件新增专属后端分支时需要。普通远程/本地插件不需要改 `feature.py`，安装流程会根据 `plugin.json` / `manifest.py` 自动登记 `Feature`。

---

### 规则配置页补充：后端 Dry-Run 适配

规则配置页通常需要试运行功能，后端需同步适配 `rules.py`：

#### 插件侧导出 _dry_run_match

```python
# plugins/local_imports/xxx/plugin.py
# 或远程插件仓库中的 xxx/plugin.py

def _dry_run_match(cfg: dict, text: str, chat_id: int | None = None) -> tuple[bool, str | None]:
    """纯函数：给定规则 config + 样本消息，返回 (matched, output)。
    不访问 DB / Redis / 网络，仅做模式匹配逻辑判断。
    """
    # 匹配逻辑（与 on_message 中使用的判断一致）
    if cfg.get("target_chat_id") and chat_id == cfg["target_chat_id"]:
        return True, "命中目标群组"
    return False, None
```

```python
# 插件包 __init__.py
from .plugin import _dry_run_match  # noqa: F401 — 供 API dry-run 导入
```

#### rules.py — 添加 dry-run 分支

```python
# backend/app/api/rules.py

# ① import
from ..db.models.feature import FEATURE_XXX
# 普通插件不从 TelePilot 本体 import。需要接入平台 rules.py 时，应通过
# 已安装插件目录动态加载其 `_dry_run_match`，并在插件自身测试中覆盖该纯函数。

# ② 在 dry_run_rule() 函数中，在 fallback return 之前添加分支
#    ⚠️ 必须放在最后的 `return RuleDryRunResponse(matched=False, ...)` 之前！

if key == FEATURE_XXX:
    cfg = rule.config or {}
    matched, output = _xxx_dry_run_match(cfg, payload.sample_message, payload.sample_chat_id)
    logs = [
        {"step": "config", "msg": f"关键字段：{cfg.get('xxx_field', '(未设置)')}"},
        # ... 更多诊断步骤
    ]
    if not matched:
        logs.append({"step": "result", "msg": "未命中"})
    else:
        logs.append({"step": "result", "msg": "命中"})
    return RuleDryRunResponse(
        matched=matched,
        output=output,
        detail={"feature": key, "rule_id": rid, "logs": logs},
    )
```

**常见错误**：dry-run 分支放在 `return RuleDryRunResponse(matched=False, ...)` 之后 → 永远不可达。
**正确位置**：在所有已实现的 dry-run 分支之后、fallback return 之前。

---

### 单配置对象页（Game24 / Codex Image）

只有一份配置、无规则列表的插件，使用专属页面但不需要 CRUD 和 dry-run：

- 创建 `frontend/src/pages/Plugins/configs/XxxConfig.tsx`，直接展示/编辑单个 config 对象
- `manifest.py` 中声明 `config_schema["x-ui-mode"] = "single"`
- 只有确实新增专属 React 页面时，才添加 App.tsx 显式路由和 `FEATURE_CONFIG_PAGE_KEYS`；使用通用 `GenericPluginConfigPage` 的插件只需正确声明 schema，不改前端注册表
- 后端不需要 dry-run 分支

#### 页面布局约定

单配置对象页参考 `Game24Config.tsx`、`CodexImageConfig.tsx` 与 `ChatGPTImageConfig.tsx`，并遵守“统一配置页样式规范”。页面从上到下固定为：

1. 返回按钮 + 插件标题
2. 使用说明（真实触发指令示例、参数示例、注意事项）
3. 功能总开关（当前账号是否启用、关键运行状态、最近错误）
4. 插件配置（账号级配置为主，必要时展示全局配置；保存条固定在卡片底部）
5. 插件预览（模板预览是建议项，没有预览时给出提示）

“使用说明 → 功能总开关 → 插件配置 → 插件预览”要作为独立卡片，不要把总开关塞进说明或配置里。单配置插件通常靠指令触发，用户最关心的是“怎么叫它”“现在能不能用”“要改哪些参数”“最终发出去是什么样”，所以顺序保持稳定。配置字段要按可用屏幕宽度展开，避免窄表单造成长配置反复滚动。

#### 指令型插件配置

如果插件支持自定义触发指令，应同时做三件事：

```python
class XxxPlugin(Plugin):
    key = "xxx"
    command_config_keys = {"command"}
```

```python
config_schema={
    "type": "object",
    "x-ui-mode": "single",
    "properties": {
        "command": {
            "type": "string",
            "title": "触发指令名",
            "default": "xxx",
            "description": "跟在系统指令前缀后使用，支持中文；不要包含空格。",
        },
    },
}
```

- `command_config_keys` 用于告诉 loader：指令字段变化后要重启该插件并重新注册指令。
- 指令名支持中文，例如 `,画图 一只猫`；但不能包含空格，因为指令解析以第一个空白分隔指令和参数。
- 说明文案必须用当前配置中的指令动态生成，不要把 `,cximg`、`,24d` 写死。

#### 已有单配置插件字段参考

| 插件 | 推荐字段 | 说明 |
|------|---------|------|
| `game24` | `command`, `timeout` | 触发指令名、答题限时 |
| `codex_image` | `command`, `access_token`, `model`, `message_template`, `image_size`, `aspect_ratio`, `image_format`, `max_wait_seconds`, `status_interval_seconds`, `delete_command_message`, `show_revised_prompt`, `reasoning_effort`, `custom_instructions` | 触发指令、鉴权、模型、消息模板、图片尺寸/比例/格式、等待与状态提示、输出行为、自定义生成指令 |

专属页面字段应与运行时实际读取的配置保持一致；`manifest.config_schema` 也要同步，避免通用配置页、接口校验和文档出现三套口径。

`codex_image` 现在是插件库图片插件，源码由插件库分发，用户需在“安装插件”页安装后才会复制到 `plugins/installed/codex_image/` 并加载。旧数据库中已经启用或保存配置的 `codex_image` 会在 seed 阶段尝试从插件库登记为 repo installed 插件，保留账号配置和规则引用；未使用过的旧 builtin feature 行会被清理，避免误展示。

---

### 通用 Schema 驱动独立页（legacy schema 兼容）

不再新增“Schema 弹窗”类插件。历史上无专属页面的插件可能声明 `x-ui-mode: "schema"`，现在应把它理解为“由 `config_schema` 提供字段的通用单配置独立页”：

- `level: "global"` 的字段 → 全局配置区
- `level: "account"` 或无 level → 账号配置区
- **不需要**添加到 `FEATURE_CONFIG_PAGE_KEYS`，不需要创建插件专属页面文件
- 新插件请优先写 `config_schema["x-ui-mode"] = "single"`；`schema` 只保留为旧插件兼容别名
- 页面同样使用“使用说明 → 功能总开关 → 插件配置 → 插件预览”的独立卡片顺序，并在有可保存字段时把“配置操作”条固定在插件配置卡片底部
- 页面宽度、滚动高度、字段间距和控件风格应与 ChatGPT2API / 自定义指令 / LLM 等系统配置页保持一致：使用统一的 `Input`、`Select`、`Switch`、`Textarea`、`Label` 视觉语言，不在字段标题里放 emoji 或临时说明块
- 普通配置字段展示在配置区顶部；`message_template` / `*_message_template` / `*_template` 等消息模板字段进入“消息模板”折叠组；`template_preview` / `*_preview` 进入独立“插件预览”卡片。
- `message_template`、`*_message_template`、`prompt`、`content`、`text` 等长文案字段会按多行文本体验展示；字段描述里应写清占位符和示例值。
- `field.readOnly === true`、`template_preview`、`*_preview`、`template_placeholders` 会自动按只读块渲染，不会保存回配置；其中预览字段使用 `TelegramHtmlPreview` 展示最终 HTML 消息效果。
- 多个预览字段应在同一个 Telegram 风格预览场景里按字段顺序展示为多条气泡，方便同时检查开局、进行中、答对、超时、取消和错误提示等模板。
- `usage_preview` / `usage_guide` / `usage_instructions` / `ai_usage_guide` 只用于“使用说明”卡片，不会再出现在插件配置字段区；`template_placeholders` 只作为只读占位符说明，不算详细使用说明。
- 配置布局可使用 `x-ui-section`、`x-ui-order`、`x-ui-columns` 在平台容器内做分组、排序和列数控制。
- 这个页面只编辑一份账号/全局配置对象，不提供 Rules API 的多条列表、创建、编辑、删除或 dry-run；规则插件不能用通用 schema 页代替专属 CRUD。

```python
# config_schema 示例（适用于通用独立配置页自动渲染）
config_schema={
    "type": "object",
    "x-ui-mode": "single",
    "properties": {
        "api_key": {
            "type": "string",
            "title": "API Key",
            "level": "global",
        },
        "threshold": {
            "type": "number",
            "title": "阈值",
            "default": 5,
        },
    },
}
```

---

### 基础能力：平台内置功能（Scheduler）

基础能力不是普通插件卡片，而是系统运行时一起初始化的服务。比如 `scheduler` 现在属于平台内置调度能力：页面仍可配置定时任务，但不再强调“作为插件启停”。

适配规则：

- `manifest.py` 声明 `config_schema["x-ui-mode"] = "platform"`
- 前端会在账号详情和插件中心里放到“基础能力 / 平台内置”分组
- 如果有专属页面，仍需 `App.tsx` 路由和共享 `FEATURE_CONFIG_PAGE_KEYS`
- 后端运行时由 `PlatformScheduler` 常驻初始化；调度算法与 action 执行在平台层，`scheduler` 插件壳只保留兼容入口或配置入口
- 普通插件需要定时执行时，不要自己 `create_task` 写永久循环，优先使用 `ctx.scheduler`

#### 插件调用平台调度器

`ctx.scheduler` 是绑定到当前插件的最小 capability facade。插件只能注册 / 注销自己名下的任务，热重载、禁用、worker 退出时 loader 会统一清理，避免旧 callback 继续触发。

```python
from app.worker.scheduler_runtime import ScheduledJob


class DemoPlugin(Plugin):
    key = "demo"

    async def on_startup(self, ctx: PluginContext) -> None:
        if ctx.scheduler is None:
            return
        ctx.scheduler.register(
            "daily_digest",
            {"kind": "cron", "cron": "0 9 * * *"},
            self._send_daily_digest,
        )

    async def on_shutdown(self, ctx: PluginContext) -> None:
        if ctx.scheduler is not None:
            ctx.scheduler.unregister_all()

    async def _send_daily_digest(self, job: ScheduledJob) -> None:
        # callback 可闭包引用插件自己的状态，也可以在 config 中保存轻量参数
        ...
```

支持的 `schedule` 字段与定时任务页面一致：

| 类型 | 示例 | 说明 |
|------|------|------|
| `cron` | `{"kind": "cron", "cron": "*/10 * * * *"}` | 按系统时区解析 cron |
| `interval` | `{"kind": "interval", "interval_sec": 300}` | 首次 tick 会立即执行一次，之后按间隔推进 |
| `once` | `{"kind": "once", "fire_at": "2026-05-11T10:00:00+00:00"}` | 执行后自动置为 disabled |

注意：

- callback 异常会写入插件日志，并保留任务等待下次 tick；不要把异常吞掉后静默失败
- `ctx.scheduler` 注册的是运行期任务；worker 重启后会由插件 `on_startup` 重新注册，若需要精确保存 `last_fire` / `next_fire`，插件应把状态写回自己的配置或规则表
- 如果任务依赖插件配置，配置变更后建议触发插件热重载，或在 callback 中读取最新 `ctx.config`
- 第三方插件拿到的是 scheduler facade，不会直接获得 DB 或 userbot session；安装插件的 `ctx.redis` 是自动命名空间、方法受限的 `PluginRedisFacade`，不是原始 Redis client
- GUI 定时任务页仍走 `Rule(feature_key="scheduler")`，由同一个 `PlatformScheduler` 调度；后续新增插件不要依赖 `SchedulerPlugin`，只依赖 `ctx.scheduler`

---

### 风格要求

- 与 TelePilot 现有页面风格一致
- React + TypeScript + TailwindCSS
- 新页面参考 `AutoReply.tsx`（规则配置页）、`Game24Config.tsx` / `CodexImageConfig.tsx` / `ChatGPTImageConfig.tsx`（单配置）或 `GenericPluginConfig.tsx`（通用 schema）的代码结构
- 使用说明、功能总开关、插件配置、插件预览必须是独立卡片，顺序固定为“使用说明 → 功能总开关 → 插件配置 → 插件预览”
- 有可保存字段的长表单必须在插件配置卡片底部使用 sticky“配置操作”条，按钮文案统一为“保存配置”“撤销”
- 配置区域宽度随页面自适应，不使用窄 `max-w-*` 限制；字段多时使用响应式 grid
- 表格列宽要稳定，账号详情页和插件中心的同类列表要纵向对齐
- 配置按钮不依赖启用状态；即使插件当前关闭，也应允许先配置
- 用户界面和文档统一称“插件”，开发文档、API、代码标识可以继续使用 plugin / feature

### 适配自检清单

新增插件前端配置页后，逐项检查：

- [ ] `manifest.py` 中 `config_schema["x-ui-mode"]` 已声明：推荐 `rules` / `single` / `platform`；仅旧插件保留 `schema`
- [ ] `config_schema` 已声明详细使用说明：优先使用 `x-usage-guide` / `x-usage-steps`，或只读 `usage_preview`
- [ ] `types.ts` 中 `XxxRuleConfig` 接口与 `manifest.py` config_schema 字段一致
- [ ] 如果有专属页面：`App.tsx` 中路由路径 `:aid/features/{key}` 与插件 key 一致
- [ ] 如果有专属页面：`frontend/src/pages/Plugins/_shared/featureConfig.ts` 的 `FEATURE_CONFIG_PAGE_KEYS` 包含该 key
- [ ] 如果是指令型插件：`command` 字段可配置，`Plugin.command_config_keys = {"command"}`，说明文案动态读取当前指令
- [ ] 指令型插件的帮助、取消/结束、撤销、自动删除、冷却/超时、消息模板等用户常调行为已尽量配置化；帮助模板支持 `{prefix}`，不硬编码 `,命令`
- [ ] `owner_only=False` 仅用于开放 `on_message`，没有把普通 incoming 消息当成管理指令入口
- [ ] 页面按“使用说明 → 功能总开关 → 插件配置 → 插件预览”的独立卡片顺序排布；不要把说明、总开关、配置和预览混在一张卡片
- [ ] 有可保存字段的页面在插件配置卡片底部使用 sticky“配置操作”条；长配置不只在滚动到底才能保存
- [ ] 如会发送消息，建议提供 `template_preview` 或 `*_preview`；没有预览不会阻断运行，但会降低配置体验
- [ ] 配置主体宽度自适应屏幕宽度，字段用响应式 grid 或分组，不使用窄 `max-w-*` 限制
- [ ] 用户可见文案使用“插件”，不展示内部分类名或“Schema 弹窗”
- [ ] 如需 dry-run：`plugin.py` 导出 `_dry_run_match`，`__init__.py` re-export；平台专属配置页再按需接入 `rules.py`
- [ ] 如需接入平台 `rules.py` 专属 dry-run：`feature.py` 中有 `FEATURE_XXX` 常量，后端通过已安装插件目录动态调用 `_dry_run_match`；普通远程/本地插件可先用插件自身测试覆盖 dry-run 纯函数
- [ ] 前端 `pnpm --dir frontend typecheck` 和 `pnpm --dir frontend build` 通过

---

## 15. 调试建议

### 快速自检

- [ ] `__init__.py` 是否导出 `PLUGIN_CLASS` 和 `MANIFEST`
- [ ] `MANIFEST.key` 是否和插件 class key 一致
- [ ] 新 Telegram 交互插件是否声明了 `usage`、`event_subscriptions`、`capabilities`
- [ ] 插件主入口是否读取标准事件信封，例如 `payload["message"]`、`payload["chat"]`、`payload["sender"]`、`payload["payment"]`
- [ ] 发送、编辑、删除、置顶、按钮 ACK、Inline answer、结算是否通过 `ctx.messages` 或标准 action，而不是直接调用 live client
- [ ] 是否区分 Interaction Bot callback ACK 和 UserBot 主动点击第三方 Bot 按钮；后者使用 `ctx.messages.click_callback_button(...)`，没有混用 `answer_callback`
- [ ] legacy `on_message` 若需接收普通成员/第三方 Bot，是否同时设置 `message_channels={"incoming"}` 与 `owner_only=False`
- [ ] 账号级“允许会话”留空是否按“全部会话”理解；有没有误套到插件自定义 `allowed_chat_ids=[]`
- [ ] 自动点击第三方 Bot callback 是否声明 `click_bot_button`，并传入 `expected_bot_id`、`expected_button_text`；没有读取/传入 callback data，也没有尝试点击 URL 等非 callback 按钮
- [ ] 日志页是否能用 `trace_id`、`plugin_key`、`reason_code` 查到订阅匹配、插件执行和动作结果
- [ ] `permissions` 是否覆盖实际调用的方法
- [ ] 如果保留旧管理员命令 hook，`on_command` 签名是否是 5 参数；不要把旧 hook 当作公共玩法的新入口
- [ ] 错误是否都被捕获并反馈给用户

### 为什么我的 Event Bus / on_interaction 没被调用

按这条顺序排查，基本能定位 90% 的插件启动问题：

- `InstalledPlugin.enabled`：远程插件是否已安装并启用（旧 `RemotePlugin` 表仅作只读兼容）。
- `AccountFeature.enabled`：当前账号是否启用了这个插件。
- `plugin.json` / `manifest.py` 是否声明了 `event_subscriptions`，事件来源、事件类型、scope 和 filters 是否覆盖当前输入。
- 日志页按 `trace_id` 或消息 ID 搜索，查看 `subscription_match` 的 `reason_code`：`source_not_subscribed`、`event_type_not_subscribed`、`scope_not_matched`、`filter_not_matched` 通常能直接说明未触发原因。
- 需要原生字段的插件是否声明 `capabilities.telegram_native_raw.enabled=true`；未声明时读取不到 `native_raw` 是正常边界。
- 返回 action 后，日志页是否有 `event_action`；如果出现 `send_channel_deprecated`，说明插件还在请求旧 `notice` / `bbot_notice` 通道。
- 如果仍使用旧 `interaction_entries` / `on_interaction` 兼容层，再检查规则动作是否是 `action == "module"`，`module_key` 是否和 `MANIFEST.key` 完全一致，`module_action` 是否等于 `interaction_entries[].key`。
- 当前群 `chat_id` 是否在规则 `chat_ids` 内；未配置时才表示所有群。
- 触发模式是否匹配：付费通知走 `payment_confirmed`，免费关键词走 `keyword`，已有会话后的群消息才走 `message`。
- 群局兼容入口是否声明了 `interaction_entries[].session_scope = "chat"`；如果漏写，规则设置 `concurrency=user` 后，后续群友消息可能找不到会话。
- 用户私有流程是否声明了 `session_scope = "user"`，并在插件内部状态 key 中包含用户 ID。
- worker 是否在线；离线时交互 Bot 会返回“插件启动失败：worker 调用超时”。
- 日志页搜索 `plugin_runtime_status`、`run_interaction_entry`、`interaction module`、`unsupported type`、`result_contract`，未知 action type 或越权 `send_via` 会写入 trace/runtime log，便于发现返回了平台尚不支持或未声明的动作。

### 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 插件被跳过 | MANIFEST 类型不对或导出缺失 | 检查 `__init__.py` |
| 指令没反应 | feature 未启用或前缀不匹配 | 检查 rule 配置和前缀 |
| 热重载后旧 handler 还在触发 | generation guard 未生效 | 检查 loader.py 版本 |
| 远程插件安装失败 | plugin.json 缺必填字段或格式不合法 | 检查 name/description/version/entry |
| 群友回复数字/答案没反应 | 群局入口漏写 `session_scope=chat`，或规则没有保存活跃会话 | 补齐 `plugin.json` / `manifest.py` 的 `interaction_entries[].session_scope`，检查规则有效期 |
| cleanup 后插件状态异常 | cleanup 未幂等 | 重复调用测试 |

---

## 17. 完整示例

标准 Telegram 交互插件请优先参考 `examples/plugins/event_bus_demo`，它覆盖 message、command、callback、inline、payment、`native_raw` 和旧 `notice` 迁移错误。下面的天气查询插件是 **管理员命令型兼容示例**，用于说明旧 `on_command` API 和前缀处理；它不应作为公共群玩法或高频交互插件的新模板。

### 天气查询插件（管理员命令兼容）

```python
# manifest.py
from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="weather",
    display_name="天气查询",
    version="1.0.0",
    author="community",
    description="查询天气信息，支持城市名",
    permissions=["send_message"],
    config_schema={
        "type": "object",
        "x-ui-mode": "single",
        "properties": {
            "command": {
                "type": "string",
                "title": "触发指令名",
                "default": "weather",
                "minLength": 1,
                "maxLength": 32,
                "pattern": "^\\S+$",
            },
            "default_city": {
                "type": "string",
                "title": "默认城市",
                "default": "Beijing",
            },
            "api_key": {"type": "string", "description": "可选的 API Key"},
        },
    },
)
```

```python
# plugin.py
import httpx
from app.worker.plugins.base import Plugin, register

@register
class WeatherPlugin(Plugin):
    key = "weather"
    display_name = "天气查询"
    command_config_keys = {"command"}

    def _command(self, ctx) -> str:
        return str(ctx.config.get("command") or "weather").strip()

    async def on_command(self, ctx, cmd, args, event) -> bool:
        if cmd != self._command(ctx):
            return False

        city = " ".join(args) if args else str(ctx.config.get("default_city") or "Beijing")
        try:
            # 第三方插件发布时应声明 external_http + allowed_hosts，并优先使用 ctx.http。
            # 这里保留直接 httpx 调用只是为了展示旧管理员命令兼容写法。
            async with httpx.AsyncClient(timeout=10.0) as client:
                geo = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1},
                )
                if not geo.json().get("results"):
                    await event.edit(f"未找到: {city}")
                    return True
                lat = geo.json()["results"][0]["latitude"]
                lon = geo.json()["results"][0]["longitude"]

                weather = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={"latitude": lat, "longitude": lon, "current_weather": True},
                )
                data = weather.json()["current_weather"]
                temp = data["temperature"]
                wmo = data["weathercode"]

                await event.edit(f"{city}: {temp}°C (天气代码: {wmo})")
        except Exception as e:
            await event.edit(f"天气查询失败: {e}")

        return True
```

```python
# __init__.py
from .manifest import MANIFEST
from .plugin import WeatherPlugin

PLUGIN_CLASS = WeatherPlugin

__all__ = ["PLUGIN_CLASS", "MANIFEST"]
```

---

## 版本与兼容

- `0.x`：开发阶段，允许快速迭代
- `1.x`：接口稳定后
- 不要依赖私有内部插件路径
- 尽量只依赖 `Plugin` / `Manifest` / `PluginContext` 公开契约
- 新增行为优先通过 `config` 可选项实现
