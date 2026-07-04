# TelePilot 插件安全边界

本文是当前维护的插件安全边界与工程规范，覆盖权限声明、运行时限制、日志脱敏和发布前检查。

## 12. 安全边界

### 指令前缀（command_prefix）

- 所有 Telegram 指令必须有明确前缀（如 `,` 或自定义）
- 前缀由系统设置里的 `command_prefix` 控制；插件配置中不要再单独硬编码 `prefix`

### 权限声明

Manifest 中的 `permissions` 字段声明插件需要的能力：

| 权限 | 典型方法 | 说明 |
|------|----------|------|
| `send_message` | `send_message` / `respond` / `reply` | 发送文本消息 |
| `edit_message` | `edit` / `edit_message` | 编辑消息 |
| `read_chat` | `get_messages` / `get_chat` / `iter_messages` | 读取聊天历史 |
| `resolve_entity` | `get_entity` | 解析用户名、频道、群等实体 |
| `send_file` | `send_file` | 发送图片或文件 |
| `join_chat` | `join_chat` | 加入聊天 |
| `delete_message` | `delete_messages` | 删除消息；高风险，必须有明确用户开关 |
| `moderate_chat` | `ban_user` / `kick_user` / `mute_user` / `unban_user` | 受控成员管理；高危权限，不开放 raw MTProto |
| `external_http` | `ctx.http.get` / `ctx.http.post` | 安全 HTTP facade；必须同时声明 `allowed_hosts` |
| `external_http_bypass_proxy` | direct 网络出口 | 预留高危权限；当前直连还必须通过 Manifest `http.allow_direct` 和账号配置共同开启 |
| `ai_text` | `ctx.ai.complete` / `ctx.ai.list_providers` | 平台文本 LLM facade；返回脱敏 provider 元数据 |

`permissions` 默认是空列表。远程/本地/插件库安装型插件漏写权限时不会注入对应 facade，也不能调用未声明的 `ctx.client` / `event` helper 能力；核心平台兼容代码也建议显式写全，方便审计和后续迁移。

TelePilot 按个人可信插件模式运行：管理员安装并启用插件后，远程插件的业务风险由管理员自行承担；平台不做公共插件市场式强沙箱，但仍保留频控、审计、急停、Trace 和 token/session 隔离。新 Telegram 插件必须走 Event Bus + MessageOps：在 `plugin.json` 声明 `usage`、`event_subscriptions`、`capabilities`，运行时只读取标准事件信封，所有发送、编辑、删除、置顶、按钮 ACK、Inline answer 和结算都返回标准 action 或通过 `ctx.messages` 生成。`ctx.client` 保留给管理员命令和高级兼容场景，不作为普通 Bot 按钮回调的主入口。群里已有的转账结果通知 Bot 只作为外部付款证据来源，不是插件主动发送通道。

### 会话通道边界

消息链路统一后，插件要额外记住三个安全边界：

- 会话通道由触发方式决定。命令开局默认走 userbot，关键词/付款/按钮开局默认走 interaction bot；普通发送动作应继承 `session.channel`，不要每条消息手动改通道。
- `payout` 永远经 userbot 执行，并进入 `RateLimitEngine`、trace 和失败回写。插件不要把 Bot 公告伪装成转账，也不要把发奖逻辑拆成“先发普通消息，再等旧文案协议补发”。
- userbot 会话没有原生 inline 按钮能力。平台会把按钮降级成文本编号，并把玩家回复合成为 callback 事件；这意味着强按钮玩法最好默认 `keyword_only`，避免命令开局落到不适合的通道。

### 配置页动作边界

通用配置页支持 `config_actions` / `x-config-actions`，但它不是任意 HTML、CSS 或 JavaScript 注入能力。插件只能声明按钮、输入 schema 和放置位置；点击按钮后，平台在后端调用插件的 `on_config_action(ctx, action_key, payload)`。

配置动作的安全边界：

- `ctx` 不注入 Telegram live client，不允许借配置页按钮直接发消息、转账或改群。
- `ctx.http` 仍要求 `external_http` + `allowed_hosts`，并继续阻断 localhost、内网和链路本地地址。
- `ctx.ai` 仍要求 `ai_text`，复用 TelePilot Provider、预算和用量记录，不暴露明文 API Key。
- 前端只合并插件返回的 `config_patch` 到当前表单；管理员仍需点击“保存配置”才会写入数据库并触发 worker 热加载。
- 动作输入、URL、AI 输出都必须由插件二次校验，不要把 AI 输出当成可信配置直接执行。

安全顾虑主要来自内部插件代码被授予能力后的扩权面，而不是来自外部 URL 字符串本身。外部内容必须通过受控 HTTP/AI facade 进入插件；平台不允许插件用配置页承载自定义脚本来绕过这些边界。

### 禁止行为

- 不允许 `os.system` / `subprocess` 执行系统命令（除非显式声明）
- 不允许把明文 key 写入日志
- 不允许持久化完整隐私消息到外部系统
- 对外部请求必须做超时和异常处理
- 不允许把旧 `notice` / `bbot_notice` / `notice_bot` 当主动发送通道
- 不允许依赖旧 `raw_event` 或旧平铺 payload；需要原生字段必须声明 `capabilities.telegram_native_raw`

### Event Bus 能力声明

最终版插件安全检查先看三个字段：

| 字段 | 必要性 | 安全意义 |
| --- | --- | --- |
| `usage` | 必填 | 让安装者知道插件会监听什么、发什么、如何启用 |
| `event_subscriptions` | Telegram 事件插件必填 | 明确 message/command/callback/inline/payment 的来源和范围 |
| `capabilities` | 必填，空能力写 `{}` | 暴露高风险能力，例如 `telegram_native_raw` |

`capabilities.telegram_native_raw.enabled=true` 时必须写 `reason` 和 `sources`。插件只能把 `native_raw` 当排障补充，业务判断仍以标准事件信封为准；当 `native_raw_meta.enabled=false` 时必须降级运行。

---

## 14. 插件工程规范（Plugin 实现）

这一章是给插件作者看的“不要踩坑”规范。只要插件涉及发消息、抢答、后台任务、奖励或远程发布，都建议先按这里的模板走。

### 发布与交互体验检查清单

这部分是发布前的产品质量门槛。插件能跑起来只是第一步；能被用户确认版本、理解状态、稳定退出、少刷屏，才算适合放进插件市场。

#### 版本与发布

- 远程插件发布时必须同步更新所有元数据入口的版本号：`plugin.json.version`、`manifest.py` 里的 `MANIFEST.version`、Registry 索引中的 `version`。
- `plugin.json` 是安装/更新阶段的静态来源，`manifest.py` 是运行阶段的真实 Manifest。两者版本不一致时，市场展示、配置缓存和运行日志会很难排查。
- 需要热更新验证的插件，建议在 `on_startup` 日志和主要业务消息中暴露版本，例如 `"[quiz] 已启动 v1.2.3，指令：quiz"`。
- 发布说明里要写清最低 TelePilot 版本、权限、依赖库、是否需要 `send_file` / `delete_message` 等敏感能力；版本字段优先写 `min_telepilot_version`。

#### 消息与交互

- Event Bus 新插件优先通过 `ctx.messages` 或标准 action 复用/编辑已有业务消息；只有管理员命令兼容 hook 才直接用 `event.edit(...)` 表示指令状态。题面进度、答对奖励和按钮反馈都应让平台记录 trace/action。
- 插件进行中时，重复触发指令必须给出明确提示，并说明下一步：继续当前流程、等待超时、或使用 `stop` / `cancel` / `结束`。
- 指令型插件必须提供帮助入口或帮助子命令，例如 `help` / `status` / 空参数展示帮助；帮助内容要显示当前配置的指令名和当前系统指令前缀。
- 视觉题、图片题、文件题不要在文本说明、alt 文案、日志或 preview 中泄露答案；文本只说明规则和限时。
- 避免连续发送多条含义重复的消息。降级发送时也要保证“同一事件只产生一个用户可见结果”。

#### 状态管理

- 明确区分 `idle` / `running` / `completed` / `cancelled` / `timeout` / `failed` 等状态，并为每种状态设计用户可见反馈。
- 同一聊天只能有一个活跃任务时，启动前必须检查已有状态；不要直接覆盖进行中的局。
- 每个流程都要有取消或强制结束入口，默认可用 `stop`、`cancel`、`结束` 等别名，并在结束时清理状态和后台任务。
- 状态对象里要冻结单局动态参数，例如奖励、题目、答案、超时时间；一局进行中不要反复读取可变配置。

#### 防滥用与公平性

- 抢答、竞猜、投票、抽奖等高频交互插件，应设计用户级或聊天级冷却，避免刷屏、暴力枚举和误触抢占。
- 冷却、限流、超时等策略尽量配置化，并在开局文案中说明关键规则，例如“每人每 3 秒可答一次，限时 60 秒”。
- 游戏类/互动类插件建议使用 `plugin.json.tags` 标记为 `game`、`quiz`、`interactive`，后续平台可按标签做统一启停、限流或分组展示。当前不要自己发明全局开关协议，先复用插件全局开关、账号级开关和配置项。
- 媒体型题面必须保证可观测、可辨认、不遮挡；如果图片或文件发送失败，应降级为明确错误提示，而不是泄露答案。

#### 资源清理与降级

- 插件产生的临时消息、图片、文件、后台任务、定时器，应在完成、取消、超时、禁用、热重载和卸载时清理。
- 群聊类插件建议提供消息清理策略，例如 `cleanup_mode` / `cleanup_delay_seconds` / `delete_command_message`，并允许用户选择保留记录；其中 `cleanup_mode` 目前只是插件 manifest/config 的约定字段，不是平台自动执行的运行时协议。
- 平台不支持编辑、删除、发媒体时，应降级为回复文本或普通发送；降级路径要写日志，并避免发送多条重复消息。
- 可配置指令发生变化时，建议保留常用历史别名一段时间，或在重复触发/未知指令提示里告诉用户新指令。

#### 插件清理检查表

禁用、热重载、卸载、超时和异常退出都要按同一张清单检查：

- handler：注销命令、事件回调、内部订阅和自定义路由，避免旧实例继续响应。
- session：结束或迁移未完成会话，给用户一个可见的取消、超时或失败反馈。
- scheduler job：注销 `ctx.scheduler` 注册的 job，避免旧 generation 继续触发。
- asyncio task：取消后台任务并处理 `CancelledError`，不要留下悬挂协程。
- 临时消息：按配置删除或编辑题面、占位消息、按钮消息和中间状态消息。
- 临时文件：删除生成图片、缓存文件、下载文件和一次性导出包。
- 游戏状态：清理 chat/user 维度锁、题目、答案、赢家、付款等待态和发奖状态。
- 外部资源：关闭 HTTP stream、文件句柄、数据库游标和第三方 SDK client。
- 日志：只记录必要摘要和 reason_code，不记录 token、session、完整原生 payload 或隐私消息。

### 指令权限底线

`owner_only` 不是“公开指令开关”。框架约定如下：

- `commands` 只处理当前账号 outgoing 指令；普通群成员直接发送 `{prefix}{command}` 不会触发插件命令。
- `owner_only=False` 只开放 `on_message`，用于答题、口令、领取码、关键词参与等普通消息监听。
- 平台内部动作（自动回复、scheduler）如果需要触发指令，应通过内部命令派发能力执行，并把返回结果转成回复/普通发送；不要要求用户直接发送管理指令。
- 指令 handler 内可以假设事件来自当前账号 outgoing 消息，因此可以优先 `event.edit(...)`；`on_message` 处理 incoming 消息时不要 `event.edit(...)`。

新版等价流程应优先写成：

```text
用户: 我想玩 24 点
插件 event_subscriptions: 命中关键词 / 付款确认 / callback
插件 on_event: 读取标准事件信封，创建会话
插件 ctx.messages/action: 由 interaction_bot 发开局和按钮；转账/发奖走 userbot 或 settlement
```

下面是旧自动回复/命令兼容模型，仅用于迁移历史配置，不作为新插件推荐主路径：

```text
用户: 我想玩 24 点
自动回复规则: 命中关键词后由 TelePilot 内部执行 "{prefix}24d 100"
插件 commands: 本账号开局
插件 on_message: 普通成员提交答案，答对后反馈奖励
```

自动回复需要带参数时，使用正则捕获组渲染到回复内容里，并继续依赖自动指令白名单和冷却：

```text
用户: 置顶 id=12345
自动回复规则:
  匹配类型: 正则
  模式: ^置顶\s+(\d+)$
  回复内容: {prefix}pt {1}
  冷却: 30 秒
插件 commands: 本账号执行 pt_promote 置顶
```

可选参数可以写默认值，例如 `回复内容: {prefix}ct {1|1000}`，群友只发“我要猜骰”时会使用 `1000`。

如果不想写正则，自动回复也支持变量模式：

```text
用户: 置顶 id=12345
自动回复规则:
  匹配类型: 变量模式
  模式: 置顶 id=数字
  回复内容: {prefix}pt {id}
  提示名称: 置顶
  冷却对象: 每个用户
  冷却时间: 6h
  每人每日上限: 2
```

变量模式里的 `id=数字` 会匹配群友消息里的 `id=12345`，并把 `=` 后面的数字提取到 `{id}`；游戏金额建议写 `num=数字`。可选参数写 `num=数字?`，`?` 表示这个 `num=...` 参数整体可以不填，回复里用 `{num|1000}` 设置默认值。
冷却时间支持单位：`2s`、`2m`、`2h`、`2d`；不带单位时按秒计算。

自动命令成功后会把今日成功次数追加到结果底部；冷却中不再静默跳过，也会提示该用户今日已成功次数、每日上限和剩余 CD，例如“今日已成功置顶促销 1/2 次，距离下次可用 CD 还剩 6小时”。达到每日上限时，会提示当日无法再次使用该功能，如需使用请联系管理员或明日再用。管理员可回复某个群友的消息发送 `{prefix}arcd`，也可发送 `{prefix}arcd 123456789` 按 Telegram 用户 ID 重置当前会话相关的会话/用户冷却和该用户今日次数；如果只想重置某条规则，追加规则 ID。

不要这样做：

```text
用户: {prefix}24d 100
插件: 直接开局
```

普通成员要参与流程，应发答案、口令或关键词，而不是发送系统指令。

### 消息发送能力边界

新 Telegram 插件的默认路径是 Event Bus + MessageOps + Trace：插件读取标准事件信封，返回标准 action，或通过 `ctx.messages` 生成同等 action；平台按当前会话通道和动作类型路由并记录 trace/action/reason_code。默认消息格式按 `parse_mode="plain"` 发送，只有显式声明 `parse_mode="html"` 时才会启用 HTML；HTML 文案先转义再拼标签。旧 hook 仍可作为内置插件和迁移桥兼容，但不能作为远程插件的新模板。

| 场景 | 最终版主路径 | 旧 hook 兼容边界 |
|------|--------------|------------------|
| 普通消息/关键词 | 返回 `send_message`，默认省略 `send_via` 并继承会话通道 | `on_message` 的 `event.reply(...)` / `event.respond(...)` 仅用于历史内置或迁移桥 |
| 管理员命令 | `command` 事件进入 Event Bus 后返回 action；需要编辑原指令时声明 `edit_message` | `on_command` 的 `event.edit(...)` 可保留；另发消息时不要绕过 MessageOps 记录 |
| 按钮回调 | 返回 `answer_callback`，再按需返回 `send_message` / `edit_message` | 不直接拼 Bot API，不假设 incoming message 可编辑 |
| Inline Query | 返回 `answer_inline_query`；选择结果用 `chosen_inline_result` 记录 | 旧 hook 没有统一 trace，不作为新插件入口 |
| 付款/发奖 | 返回 `settlement` 或 `userbot_reply` 受控动作；普通 Bot 只公告结果 | 不把外部转账通知 Bot 当主动发送通道 |
| 定时任务/后台任务 | 保存目标 chat/session 后通过 `ctx.messages` 或标准 action 输出 | 直接使用受控 client 发消息只作为旧调度代码兼容，不作为新模板 |
| 启停阶段 | 默认不发；确需通知时必须有显式配置开关并记录日志 | 不在 `on_startup` / `on_shutdown` 里无条件群发 |
| 成员管理 | 声明 `moderate_chat`，由受控 facade 执行并记录审计 | 不开放 raw MTProto 或 live client 给远程插件 |

#### 旧 hook 安全回复模板（仅迁移兼容）

下面的模板只用于尚未迁移的内置插件或历史 hook；新 Event Bus 插件应改为返回 `send_message` action，由 Delivery Executor 记录成功/失败。

```python
async def safe_reply_action(ctx, text: str, *, chat_id: int, reply_to_id: int | None = None) -> dict:
    if ctx.messages is not None:
        return await ctx.messages.send(
            channel="auto",
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_id,
        )
    return {
        "type": "send_message",
        "channel_selector": "auto",
        "chat_id": chat_id,
        "text": text,
        "reply_to_message_id": reply_to_id,
    }
```

注意：

- `event.edit(...)` 只适合编辑当前账号自己发出的指令/状态消息；不要用它编辑别人发来的 incoming 消息。
- 远程插件安装阶段只读 `plugin.json`；运行时由 TelePilot facade 代发、记录和限流，插件不直接接触 Bot Token 或 userbot session。
- 第三方插件不要把 `event.reply/respond/edit` 当作绕过审计的路径；新插件发送、编辑、删除、置顶、按钮 ACK、Inline answer 都应通过 `ctx.messages` 或标准 action 交给平台，方便 Trace、限流和问题追踪。
- 若启用 `callback_fast_ack`，按钮会先被平台立即 ACK；插件后续返回的 `answer_callback` 只会记 trace，不会再真正调用 Telegram。需要 `show_alert` 的入口不要开启它。
- 需要发送图片、文件时，为 `BytesIO` 设置 `name`，例如 `image_file.name = "result.png"`，否则 Telegram 客户端可能显示无后缀文件。
- 长消息要按 Telegram 4096 字符限制分段；HTML 模式下切分前要保证标签闭合，失败时应降级为纯文本。

### 并发与抢答标准模板

抢答类、竞猜类、抽奖类插件都要处理并发：多个人几乎同时答对时，只能有一个人获胜。推荐使用 `chat_id -> asyncio.Lock`，并在加锁后再次检查状态。

```python
import asyncio
from collections import defaultdict


class QuizPlugin(Plugin):
    key = "quiz"
    display_name = "抢答示例"
    message_channels = {"incoming"}
    owner_only = False

    def __init__(self) -> None:
        super().__init__()
        self._games: dict[int, dict] = {}
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def on_message(self, ctx: PluginContext, event) -> None:
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        text = str(getattr(event, "raw_text", "") or "").strip()
        if not chat_id or not text:
            return

        lock = self._locks[chat_id]
        async with lock:
            # 第一次检查：是否有进行中的局
            game = self._games.get(chat_id)
            if not game or game.get("answered"):
                return

            # 判断可能比较耗 CPU 时，尽量先做轻量过滤，再进入重计算
            if not self._is_correct(text, game):
                return

            # 第二次状态变更必须在锁内完成，防止两个答对者同时发奖
            game["answered"] = True
            self._games.pop(chat_id, None)

        # 发消息可以放到锁外，避免网络慢时阻塞同群后续消息
        await safe_reply(ctx, event, f"答对了，奖励 {game['reward']} 分！", chat_id=chat_id)

    def _is_correct(self, text: str, game: dict) -> bool:
        ...
```

常见竞态：

- 只在锁外检查 `answered`：两个协程都看到未答对，最后发两次奖励。
- 在锁内等待网络请求：一个群的消息会被长时间阻塞。
- 超时任务和答题消息同时结束一局：超时回调也要拿同一把锁，再二次检查。
- 热重载没有清理状态：旧任务继续执行，和新插件实例抢状态。

### 配置项完整性原则

插件作者要尽量把“用户可能合理想改”的行为做成配置项，而不是写死在代码里。尤其是互动类、游戏类、生成类和通知类插件，至少检查这些能力是否需要外露：

- 指令名：主指令、帮助子命令、取消/结束命令、撤销命令、管理子命令。
- 自动删除：是否删除触发指令、是否删除中间状态、完成后多久清理、失败时是否保留排障信息。
- 消息模板：帮助、开局、进行中、成功、失败、超时、取消、撤销、冷却、权限拒绝。
- 交互策略：冷却时间、超时时间、是否引用原消息、是否保留历史别名、是否允许指定群启用。
- 输出行为：HTML/纯文本、是否展示详细错误、是否展示内部 ID、是否发送预览或来源说明。

帮助模板必须支持 `{prefix}` 占位符。不要在帮助列表里硬编码 `,命令`，否则用户把系统指令前缀改成 `。`、`/` 或其它字符后，帮助会误导。推荐默认帮助模板写成：

```text
{prefix}{command} 100 - 开始一局
{prefix}{command} status - 查看状态
{prefix}{command} {help_command} - 查看帮助
{prefix}{command} {cancel_command} - 取消当前流程
```

运行时渲染帮助时，`{prefix}` 应来自 `current_command_prefix()` 或平台注入的当前前缀；配置页预览时，`{prefix}` 应来自系统设置中的 `command_prefix`，拿不到时才用 `,` 兜底。

### 统一配置项命名与校验

新增插件尽量复用以下字段名，减少前端、文档、Bot 指令和用户认知的分裂。

| 字段 | 类型 | 推荐默认值 | 推荐范围/校验 | 说明 |
|------|------|------------|---------------|------|
| `command` | string | 插件短名 | 1-32 字符，不含空白，支持中文 | 触发指令名，配合 `command_config_keys = {"command"}` |
| `help_command` | string | `help` | 1-32 字符，不含空白 | 帮助子命令或独立帮助指令名 |
| `help_message_template` | string | 内置模板 | 建议限制最大长度 | 帮助文本模板，必须支持 `{prefix}` 和 `{command}` |
| `default_reward` | integer | `0` | `0` 到业务允许上限 | 可选默认奖励；抢答/下注类插件的单局奖励优先由指令参数传入 |
| `timeout` | integer | `60` | 10-86400 秒 | 用户可理解的超时秒数；已有插件沿用该字段 |
| `auto_next` | boolean | `false` | 布尔 | 游戏/任务结束后是否自动开下一轮 |
| `message_template` | string | 内置模板 | 建议限制最大长度 | 用户可编辑输出消息模板 |
| `template_preview` | string | 只读示例 | 由前端/后端生成 | 展示模板渲染后的示例文本，不参与运行时配置 |
| `status_interval_seconds` | integer | `30` | 10-300 秒 | 状态编辑频率，避免频繁编辑触发风控 |
| `cooldown_seconds` | integer/string | `0` | 0、纯数字秒数，或 `2s` / `2m` / `2h` / `2d` | 聊天级或用户级冷却时间 |
| `usage_label` | string | 规则名称 | 1-32 字符 | 自动回复冷却和每日上限提示里的动作名称，例如 `置顶` |
| `cooldown_notice_enabled` | boolean | `true` | 布尔 | 自动回复冷却中是否提示剩余 CD |
| `daily_limit_notice_enabled` | boolean | `true` | 布尔 | 自动回复达到每日上限时是否提示当日不可再用 |
| `daily_limit_success_message_template` | string | 内置模板 | 建议限制最大长度 | 自动命令成功后追加的今日成功次数提示 |
| `cleanup_delay_seconds` | integer | `0` | 0-86400 秒 | 流程结束后延迟清理临时消息 |
| `cancel_commands` | array[string] | `["stop", "cancel", "结束"]` | 每项 1-32 字符，不含空白 | 取消/强制结束指令别名 |
| `undo_command` | string | `undo` | 1-32 字符，不含空白 | 撤销上一步、撤回本轮或回滚最近动作的指令名 |
| `allowed_chat_ids` | array[int] | `[]` | 留空表示不限制 | 限制插件只在指定聊天生效 |
| `delete_command_message` | boolean | `false` | 布尔 | 指令完成后是否删除原指令 |
| `auto_delete_enabled` | boolean | `false` | 布尔 | 是否自动删除插件产生的临时消息 |
| `auto_delete_delay_seconds` | integer | `0` | 0-86400 秒 | 自动删除延迟；0 表示立即或不启用，按插件语义说明 |

示例：

```python
class GamePlugin(Plugin):
    key = "game"
    command_config_keys = {"command"}


config_schema={
    "type": "object",
    "x-ui-mode": "single",
    "properties": {
        "command": {
            "type": "string",
            "title": "触发指令名",
            "default": "game",
            "minLength": 1,
            "maxLength": 32,
            "pattern": r"^\S+$",
            "description": "跟在系统指令前缀后使用，支持中文；不要包含空格。",
        },
        "default_reward": {
            "type": "integer",
            "title": "默认奖励",
            "default": 0,
            "minimum": 0,
            "description": "仅作为指令未传奖励时的兜底值；本轮奖励建议通过指令参数传入。",
        },
        "timeout": {
            "type": "integer",
            "title": "超时时间（秒）",
            "default": 60,
            "minimum": 10,
            "maximum": 86400,
        },
        "auto_next": {
            "type": "boolean",
            "title": "结束后自动下一轮",
            "default": False,
        },
    },
}
```

配置页只适合放长期稳定配置，例如 `command`、`help_command`、`cancel_commands`、`undo_command`、`timeout`、`auto_next`、`message_template`、`delete_command_message`、`auto_delete_enabled`。像奖励金额、题目范围、下注金额这类单局动态参数，优先从指令参数读取，例如 `{prefix}game 100`，并在开局时冻结到本轮状态里。

### 模板配置与占位符

凡是会向 Telegram 用户发送、编辑或回复文案的插件，都必须把用户可见文案模板化，尤其是开局文案、进行中文案、答对文案、超时文案、取消文案和错误提示。模板配置要参考“通用模板 → 自定义指令模板”的输出模板编辑体验，告诉用户可用占位符、含义和示例。

不要把面向用户的句子硬编码在 `plugin.py` 里。代码里只能保留模板默认值、不可恢复的兜底错误、内部日志和开发者调试信息；只要这段文字可能在群聊、私聊、指令回复、状态编辑或媒体 caption 中出现，就应该有对应的 `*_message_template` 配置项，或至少复用一个通用 `message_template`。

推荐字段拆分：

| 字段 | 说明 |
|------|------|
| `help_message_template` | 帮助/用法文案；必须支持 `{prefix}` 和 `{command}` |
| `start_message_template` | 开局/题面文案 |
| `progress_message_template` | 进行中状态文案 |
| `success_message_template` | 答对/成功文案 |
| `timeout_message_template` | 超时文案 |
| `cancel_message_template` | 取消/结束文案 |
| `undo_message_template` | 撤销/回滚成功文案 |
| `error_message_template` | 可恢复错误文案 |

占位符说明建议写进 `description`，并保持稳定：

```python
"start_message_template": {
    "type": "string",
    "title": "开局文案模板",
    "default": "第 {round} 轮开始，奖励 +{reward}，限时 {timeout}s。",
    "description": (
        "可用占位符："
        "{round}=轮次，例如 1；"
        "{reward}=本轮奖励，例如 100；"
        "{timeout}=限时秒数，例如 60；"
        "{command}=当前触发指令，例如 game；"
        "{prefix}=系统指令前缀，例如 ,。"
    ),
}
```

`{prefix}` 是平台约定的系统级占位符，表示“系统设置 → 指令前缀”的当前值。运行时需要展示指令示例、帮助列表、错误提示里的用法示例时，优先从 worker 的当前指令前缀读取；前端配置预览中应通过 `getSystemSettings().command_prefix` 注入示例上下文，接口未返回时才兜底使用 `,`。不要把逗号硬编码成固定前缀。

只要插件声明 `config_schema` 并进入配置页，就必须声明详细使用说明。优先在 schema 顶层写 `x-usage-guide`、`x-usage-instructions` 或 `x-usage-steps`；也可以提供只读字段 `usage_preview`、`usage_guide`、`usage_instructions`、`ai_usage_guide`。平台不再给“全局配置共享、命令不写前缀、模板可预览”这类默认兜底说明；缺少说明会在插件中心和配置页显示红色高级规范警告。

如果插件会发送消息，建议提供只读预览：用户修改模板后，用示例上下文渲染一段 `template_preview` 或 `*_preview`。预览应展示“模板 + 示例上下文”替换后的最终消息效果，而不是简单重复默认值或字段说明。预览不是强制项，没有预览不会阻断保存和运行，但至少应在字段描述里给出一条完整示例，避免用户猜最终效果。

配置页里的模板预览体验应对齐自定义指令模板：模板输入和占位符说明放在“插件配置”里，最终消息预览放在独立“插件预览”卡片里。预览只使用模拟数据，不读取真实群消息，也不触发实际发送；如果模板支持 Telegram HTML，应复用 `frontend/src/components/TelegramHtmlPreview.tsx`。

#### Telegram 消息预览规范

模板预览用于回答一个问题：“这段模板最终发到 Telegram 里大概长什么样？”它不是字段说明，也不是原始模板文本回显。

- 预览必须使用示例上下文渲染最终消息，例如把 `{answer}`、`{question}`、`{sources}` 等占位符替换成模拟值。
- 如果模板支持 `{prefix}`，预览必须使用系统设置里的 `command_prefix` 渲染，不要硬编码为 `,`。
- 预览必须使用 Telegram 风格聊天场景：浅色聊天背景、左侧示例用户消息、右侧 TelePilot 蓝色气泡、时间和已读状态。不要只用普通灰色文本框展示。
- HTML 模式允许展示 Telegram 常用标签效果：`<b>`、`<i>`、`<code>`、`<pre>`、`<blockquote expandable>`；不支持的标签应被转义为普通文本。
- Markdown / plain 模式在同一气泡里按纯文本预览，不尝试模拟完整 Telegram Markdown 解析。
- 预览只使用模拟数据，不读取真实聊天、账号、用户资料，也不触发发送或编辑消息。
- 如果插件或页面需要做消息模板预览，优先直接使用 `TelegramHtmlPreview`；只有需要嵌入极小空间时，才使用更轻量的纯内容预览。

通用独立配置页兼容已有 schema 约定：`message_template` / `*_template` 是可编辑多行模板；`template_placeholders` 是只读占位符说明；`usage_preview` / `usage_guide` / `usage_instructions` / `ai_usage_guide` 只进入“使用说明”；`template_preview` / `*_preview` 进入独立“插件预览”。多个 `*_preview` 字段会合并到同一个预览区域，以多条 Telegram 气泡展示。

### 定时任务与后台任务生命周期

优先使用平台调度器：

- 重复执行、cron、一次性延迟任务：用 `ctx.scheduler.register(...)`。
- 插件禁用、热重载、worker 退出时，loader 会清理该插件名下的调度任务。
- 调度回调没有 `event`，必须从 `job.config` 或插件状态里拿 `chat_id`、模板、目标参数。

只有短期后台动作才建议自己 `asyncio.create_task`，例如“本轮游戏 60 秒后超时”。自建 task 必须集中管理并在 `on_shutdown` 取消：

```python
class RoundPlugin(Plugin):
    key = "round"

    def __init__(self) -> None:
        super().__init__()
        self._tasks: set[asyncio.Task] = set()

    def _track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_command(self, ctx, cmd, args, event) -> bool:
        if cmd != "round":
            return False
        task = asyncio.create_task(self._round_timeout(ctx, event.chat_id, timeout=60))
        self._track_task(task)
        await event.edit("已开局")
        return True

    async def on_shutdown(self, ctx) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _round_timeout(self, ctx, chat_id: int, timeout: int) -> None:
        try:
            await asyncio.sleep(timeout)
            # 超时前必须再次检查该局是否仍存在
        except asyncio.CancelledError:
            raise
```

不要这样做：

- `while True: await asyncio.sleep(...)` 的永久循环。
- task 不保存引用，导致卸载时无法取消。
- `on_shutdown` 里只 `cancel()` 不 `await gather()`。
- 超时任务不做二次状态检查，导致已答对后仍发超时消息。

### 奖惩系统接入约定

当前 TelePilot 没有统一积分服务时，插件奖励分三类：

| 模式 | 适用场景 | 推荐做法 |
|------|----------|----------|
| 文案奖励 | 游戏娱乐、无真实积分 | 只发送“奖励 X 分/金币”的消息，并写插件日志 |
| 插件内记分 | 插件自己维护排行榜 | 仅写自己插件的配置/状态文件或专属表，不直接改其它插件数据 |
| 统一积分接口 | 后续平台提供积分服务后 | 通过平台 service/event 接口发放，插件不直接操作 DB |

奖励日志建议统一字段：

```python
await ctx.log(
    "info",
    "抢答成功，准备发放奖励。",
    chat_id=chat_id,
    winner_id=sender_id,
    reward=reward,
    reward_mode="text_only",
    round_id=round_id,
)
```

约束：

- 不要在日志里记录完整昵称、完整消息正文或隐私文本。
- 真正记分前必须保证“首个答对”已经在锁内原子判定。
- 奖励金额不建议作为抢答类插件的固定配置项；优先由触发指令携带，例如 `{prefix}game 100`。
- 开局时把本轮奖励写入局状态，例如 `RoundState.reward`；一局进行中不要再读取运行时可变配置，避免配置变更导致结算金额漂移。
- 答对后建议两步反馈：先回复答对者消息发送纯文本奖励（如 `+100`），再编辑原题目消息追加答对者、正确答案、奖励金额、耗时等结算信息。
- 图片题面插件必须在 `plugin.json` 和 `manifest.py` 的 `permissions` 中声明 `send_file`，并给发送的文件设置明确后缀名。
- 图片题面插件不要隐式依赖未声明系统库；如果不用 Pillow，可以说明使用标准库生成 PNG；如果必须使用 Pillow、numpy 等第三方库，要在 README 或插件说明中写清安装约束。
- 奖励发送失败要写 `warn/error` 日志，并说明是否已经兜底发送普通消息。

### 插件最小测试清单

发布前至少覆盖这些路径：

- [ ] 指令能触发，指令名改配置后能热重载生效。
- [ ] 指令冲突时只由一个插件处理，`on_command` 正确返回 `True/False`。
- [ ] 群聊、私聊、频道/匿名频道场景下不崩溃。
- [ ] `event` 兼容裸 `Message`：不直接假设 `event.outgoing`、`event.message.id` 存在。
- [ ] 重复开局/重复创建规则会给出明确提示。
- [ ] 抢答并发：两个答对消息同时到达只奖励一次。
- [ ] 超时任务和答题消息同时发生时，只结束一次。
- [ ] 插件禁用、热重载、worker shutdown 后没有幽灵 task。
- [ ] 远程插件 `plugin.json`、`manifest.py`、`__init__.py`、`plugin.py` 均可被加载。
- [ ] 远程插件 `plugin.json.version`、`MANIFEST.version`、Registry `version` 一致。
- [ ] 远程插件不是单文件旧结构；缺少 `manifest.py` 或 `__init__.py` 会被安装阶段拒绝。
- [ ] 缺权限时远程插件会收到可理解的 `PermissionError`，不会访问 session/Redis/engine。
- [ ] 主要交互消息或启动日志能显示当前插件版本，便于确认热更新是否生效。
- [ ] 模板类配置包含占位符说明和示例预览。
- [ ] 高频交互插件有冷却/限流/超时策略，并在用户文案里说明关键规则。
- [ ] 取消、完成、超时、禁用、热重载路径都会清理状态和后台任务。
- [ ] 动作数量超出平台限制时会触发 `action_limit_exceeded`，并在日志里可见截断结果。
- [ ] 所有外部 HTTP 请求有 timeout，错误提示不泄露 token、路径、session。
- [ ] 插件日志能说明“收到什么、判断了什么、为什么跳过/为什么执行、失败在哪一步”。

### 旧 hook 游戏插件骨架（仅内置/迁移兼容）

下面骨架保留给现有 `on_command` / `on_message` 插件迁移时对照状态、并发锁、超时、日志和清理。新远程插件不要从这里直接复制入口和发送方式；请以 `examples/plugins/event_bus_demo` 为主模板，并把群友触发、按钮、Inline、付款确认都写成 `event_subscriptions` + 标准 action。

**plugin.py：**

```python
from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register


@dataclass
class RoundState:
    chat_id: int
    answer: str
    reward: int
    timeout: int
    answered: bool = False


def _event_text(event: Any) -> str:
    msg = getattr(event, "message", event)
    return str(getattr(event, "raw_text", None) or getattr(msg, "raw_text", None) or "").strip()


@register
class GuessNumberPlugin(Plugin):
    key = "guess_number"
    display_name = "猜数字"
    message_channels = {"incoming"}
    owner_only = False
    command_config_keys = {"command"}

    def __init__(self) -> None:
        super().__init__()
        self._rounds: dict[int, RoundState] = {}
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()

    async def on_startup(self, ctx: PluginContext) -> None:
        self.commands = {self._command(ctx): self._cmd_start}
        if ctx.log:
            await ctx.log("info", "猜数字插件已启动。", command=self._command(ctx))

    async def on_shutdown(self, ctx: PluginContext) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._rounds.clear()
        self._locks.clear()

    async def _cmd_start(self, client, event, args: list[str], account_id: int, ctx: PluginContext) -> None:
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        # 单局奖励优先由指令参数传入，如：,guess 100。
        # 没传时才使用 default_reward 兜底，并在开局时冻结到 RoundState。
        reward = int(args[0]) if args else int(ctx.config.get("default_reward", 0) or 0)
        timeout = int(ctx.config.get("timeout", 60) or 60)

        async with self._locks[chat_id]:
            if chat_id in self._rounds:
                await event.edit("当前聊天已有进行中的游戏。")
                return
            answer = str(random.randint(1, 9))
            self._rounds[chat_id] = RoundState(chat_id, answer, reward, timeout)

        task = asyncio.create_task(self._timeout_round(ctx, chat_id, timeout))
        self._track_task(task)
        await event.edit(f"猜一个 1-9 的数字，限时 {timeout} 秒，奖励 +{reward}。")

    async def on_message(self, ctx: PluginContext, event) -> None:
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        text = _event_text(event)
        if not chat_id or not text:
            return

        async with self._locks[chat_id]:
            state = self._rounds.get(chat_id)
            if not state or state.answered:
                return
            if text != state.answer:
                return
            state.answered = True
            self._rounds.pop(chat_id, None)

        if ctx.log:
            await ctx.log("info", "猜数字答对，准备发送奖励文案。", chat_id=chat_id, reward=state.reward)
        prize_text = f"+{state.reward}"
        if ctx.messages is not None:
            await ctx.messages.payout(chat_id=chat_id, amount=state.reward, text=prize_text)
        else:
            return [{"type": "payout", "chat_id": chat_id, "amount": state.reward, "text": prize_text}]

    async def _timeout_round(self, ctx: PluginContext, chat_id: int, timeout: int) -> None:
        try:
            await asyncio.sleep(timeout)
            async with self._locks[chat_id]:
                state = self._rounds.get(chat_id)
                if not state or state.answered:
                    return
                self._rounds.pop(chat_id, None)
            if ctx.messages is not None:
                await ctx.messages.send(
                    channel="interaction_bot",
                    chat_id=chat_id,
                    text=f"本轮超时，答案是 {state.answer}。",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if ctx.log:
                await ctx.log("error", "猜数字超时任务异常，本轮已跳过。", chat_id=chat_id, error=type(exc).__name__)

    def _track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _command(self, ctx: PluginContext) -> str:
        return str(ctx.config.get("command") or "guess")
```

**manifest.py：**

```python
from app.worker.plugins.manifest import Manifest

MANIFEST = Manifest(
    key="guess_number",
    display_name="猜数字",
    version="0.1.0",
    author="example",
    description="一个可作为游戏插件模板的猜数字抢答插件。",
    category="interactive",
    permissions=["send_message", "edit_message", "read_chat"],
    event_subscriptions=[
        {
            "events": ["message", "command", "session_close"],
            "source": ["userbot", "interaction_bot"],
            "scope": "all_allowed_chats",
        }
    ],
    capabilities={},
    config_schema={
        "type": "object",
        "x-ui-mode": "single",
        "properties": {
            "command": {
                "type": "string",
                "title": "触发指令名",
                "default": "guess",
                "minLength": 1,
                "maxLength": 32,
                "pattern": r"^\S+$",
            },
            "default_reward": {
                "type": "integer",
                "title": "默认奖励",
                "default": 0,
                "minimum": 0,
                "description": "仅作为指令未传奖励时的兜底值；本轮奖励建议通过指令参数传入。",
            },
            "timeout": {
                "type": "integer",
                "title": "答题限时（秒）",
                "default": 60,
                "minimum": 10,
                "maximum": 86400,
            },
        },
    },
)
```

**plugin.json（远程安装元数据）：**

```json
{
  "name": "guess_number",
  "display_name": "猜数字",
  "description": "一个可作为游戏插件模板的猜数字抢答插件。",
  "author": "example",
  "version": "0.1.0",
  "entry": "plugin.py",
  "category": "interactive",
  "permissions": ["send_message", "edit_message", "read_chat"],
  "usage": "迁移兼容示例：管理员命令开局，群友消息参与；新插件应优先改写为 Event Bus 标准事件入口。",
  "event_subscriptions": [
    {
      "events": ["message", "command", "session_close"],
      "source": ["userbot", "interaction_bot"],
      "scope": "all_allowed_chats"
    }
  ],
  "capabilities": {}
}
```

**__init__.py：**

```python
from .manifest import MANIFEST
from .plugin import GuessNumberPlugin

PLUGIN_CLASS = GuessNumberPlugin
__all__ = ["PLUGIN_CLASS", "MANIFEST"]
```

---

## 16. 安全与合规

- 不要把明文 key 写入日志
- 不要把完整隐私消息持久化到外部系统
- 对外部请求做超时和异常处理
- 对高风险操作（删消息、批量发送）加显式开关

---
