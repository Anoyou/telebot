# TelePilot 全量 Telegram 消息事件总线与链路日志重构计划

> 当前执行入口：如果代码已经处于 `0.40.x` 封口阶段，后续不要再从 0.38/0.39/0.40 的历史施工阶段重新解释方向，而是直接按第 24 节《最终版执行版协议》、第 25 节《最终版落地总则》、第 26 节《最终版执行锁定补丁》、第 27 节《最终版可实现性锁定》、第 28 节《最终版执行冻结清单》、第 29 节《最终版执行封条》、第 30 节《最终版签收执行补丁》和第 31 节《最终版收束补丁》执行。前文所有章节作为设计依据、任务来源和验收细则；若前文与第 24/25/26/27/28/29/30/31 节冲突，以第 24/25/26/27/28/29/30/31 节更严格的封版口径为准。

## 0. 审查结论

这份计划方向正确，尤其是“先做 Trace / 日志，再做全量 Event Bus”。当前日志系统是 `runtime_log` / `audit_log` 的平面文本流，无法天然回答“消息走到哪一步、哪个插件为什么没执行、动作最后由谁发出”。继续美化旧日志页意义不大，应该以 `trace_id` 为主线重构。

但原计划还缺三块必须补齐的设计：

1. **原生数据免检通道**：需要给指定可信插件提供 `payload["native_raw"]`，否则做严格数字 ID 关系链风控时只能依赖平台投影字段，确实不够。
2. **Inline 模式闭环**：需要支持 `inline_query` 进入 Event Bus，并支持 `answer_inline_query` 动作；只支持群消息和按钮回调，交互能力是不完整的。
3. **Trace 与旧日志的关联边界**：旧 `runtime_log` 可以保留，但插件 `ctx.log`、Contract Guard、Delivery Executor、loader 错误都必须带上 `trace_id` / `plugin_key` / `entry_key`，否则新日志页还是会断链。

### 0.1 最终版口径

这份计划的“最终版”不是 0.38、0.39、0.40 三个互相独立的愿景版本，而是一个完整施工目标：

- 当前基线版本是 `0.37.0`。
- `0.38.0`、`0.39.0`、`0.40.0` 是可独立验收的发布检查点，便于并行、review、回滚和部署观察。
- 如果一次性在当前分支完成全部计划，最终发布版本应直接按实际落地范围定为下一个阶段性 minor（次版本），建议为 `0.40.0`，并在 `CHANGELOG.md` 中完整说明 0.37.0 之后的新增能力；若封口过程中又产生 patch（补丁版本），最终证据、部署和报告必须以当前实际发布版本为准，例如 `0.40.1`。
- 分支只有通过第 17 节最终验收矩阵后，才算“最终版完成”。只完成 Trace 或只完成 Event Bus 都不算最终版。

### 0.2 唯一产品模式

后续不再设计“标准模式 / 个人模式”双模式。TelePilot 的统一口径是：

- **个人可信插件标准**：插件和插件仓库由账号主人主动安装、更新和启用，插件风险由账号主人在清楚风险提示后自行承担。
- 平台不做公共插件市场式强沙箱，不把 Contract Guard 设计成业务拦截器。
- 平台仍保留最小客观边界：不下发凭据，不暴露 live client，不把普通 Bot 伪装成可转账主体，不让旧 `notice` / `bbot_notice` 继续作为发送通道。
- 平台必须把插件声明、实际调用、越声明调用、发送通道选择、失败原因和高风险能力写入 Trace，让账号主人能看见、能追责、能停用。

### 0.3 当前分支已半落地内容与缺口

本计划执行前必须承认当前分支已经有部分代码改动，后续不能从零重写，也不能假设它们已经完整：

已半落地：

- `event_trace` / `event_span` / `event_action` / `plugin_runtime_status` 模型和迁移已有雏形。
- `event_trace.py` Trace Service 已有雏形。
- 交互 Bot `message` / `callback_query` / `inline_query` / `chosen_inline_result` 解析已有雏形。
- Delivery Executor 已开始记录 action，并新增 `answer_inline_query` 雏形。
- `ctx.log` 已开始补 `trace_id`。

必须补齐后才能算完成：

- `native_raw` 当前不能默认下发给所有插件，必须按 `capabilities.telegram_native_raw.enabled=true` gate。
- `event_subscriptions` 和 `capabilities` 还没有完整进入 manifest、远程插件解析、feature matrix、WebUI 和 lint。
- Event Bus 服务和订阅匹配还没成为真实调度入口。
- UserBot/Telethon 消息和命令链路还没有完整 Trace/Event Bus 接入。
- Inline Query 当前只做到解析/动作雏形，还缺插件订阅投递、scope、rate limit、Trace 闭环和前端排障展示。
- 新日志中心 UI 还没重构，旧 `runtime_log` 表格不能作为最终日志页。
- Trace 保留策略、清理任务和 native_raw 持久化设置还没落地。
- 插件开发指南还没有按最终 Event Bus + Trace + MessageOps 口径重写。

### 0.4 最终版不可缩水清单

以下内容任何一项缺失，都只能称为“阶段性可测版本”，不能称为最终版：

- 所有 Telegram 来源先标准化为 `TelePilotEvent`，再进入 Event Bus。
- UserBot 消息、管理员命令、交互 Bot 消息、按钮回调、Inline Query、Inline 选择结果、外部转账通知消息全部有 Trace。
- 插件通过 `event_subscriptions` 接收事件，平台记录 matched / skipped / delivered 的稳定 reason_code。
- 插件收到统一事件信封，业务主路径不再依赖旧平铺 payload。
- `native_raw` 只给显式声明能力的插件，并在日志页可见是否下发、大小和是否持久化。
- 插件用 `ctx.messages` 或标准 action 请求发送、编辑、删除、置顶、按钮 ACK、Inline Answer。
- 插件选择 `interaction_bot` / `userbot_reply` / `auto`，平台执行并记录实际通道；涉及转账/发奖仍由 userbot 或受控结算流程处理。
- 旧 `notice` / `bbot_notice` / `notice_bot` 主动发送通道不恢复，只能明确失败并提示迁移。
- 日志页默认以 Trace 视角排障，不再以旧 runtime log 表格作为主入口。
- 插件开发文档能让开发者直接写出 message、command、callback、inline、payment 插件，并知道如何从日志页排查。

### 0.5 最终版稳定不变量

后续实现必须保护以下不变量。只要某条被打破，即使功能表面可用，也不能称为最终版：

- **唯一事件入口**：Telegram 来源只允许通过 Source Adapter 标准化后进入 Event Bus；业务代码不得在运行期绕过 Event Bus 直接调用插件。
- **唯一插件消息协议**：新插件主路径只读标准事件信封；旧平铺 payload 只允许作为迁移层输出，不允许出现在新版开发指南的最小示例里。
- **唯一消息操作出口**：插件只能通过 `ctx.messages` 或标准 action 请求发送、编辑、删除、置顶、按钮 ACK、Inline Answer；插件不能直接拿 Bot token、UserBot client 或 Telegram driver。
- **可见的自由**：插件可以自由选择发送通道和读取声明内能力，但平台必须在 Trace 中记录声明、实际调用、越声明调用、失败原因和最终通道。
- **可解释的未触发**：任意消息未触发插件时，日志页必须能给出稳定 reason_code，而不是只显示“没有执行”。
- **可解释的失败**：任意插件加载失败、运行异常、Contract Guard 告警、Telegram API 失败、UserBot 离线、Bot token 缺失，都必须能从日志页定位到插件、入口、动作和 trace。
- **可回滚的数据层**：新增 Trace 表和设置不能破坏旧 `runtime_log` / `audit_log`；紧急回滚时不要求删新表。
- **可迁移的旧规则**：旧交互规则 UI 可以保留，但内部语义必须收敛为 Event Bus 订阅条件，不能形成第二套并行调度真相。
- **可审计的高风险能力**：`telegram_native_raw`、`inline_all`、跨通道发送、转账/发奖动作必须有 WebUI 风险提示和 Trace 留痕。
- **可照文开发**：插件开发指南必须和真实代码字段一致；文档里的最小插件复制后能通过验证脚本和本地运行。

### 0.6 最终版硬门禁

实现完成后必须逐项打勾，不能用“已基本完成”替代：

- 代码中所有新 Telegram update 处理入口都能找到 `trace_id` 创建或继承逻辑。
- 所有插件调用路径都能找到 Event Bus decision 或旧规则映射后的 decision。
- 所有 action 执行路径都能产生 `event_action`，失败动作不得被记录为成功。
- 所有 manifest 新字段都贯穿：解析、远程仓库写回、数据库 feature 信息、前端类型、WebUI 展示、lint、文档。
- 所有旧发送通道值 `notice` / `bbot_notice` / `notice_bot` 都不再被当作可执行通道；manifest lint、规则保存或运行时必须给出迁移提示。
- 文档 grep 不得再把旧平铺 payload、旧规则驱动、旧 notice 通道写成推荐主路径。
- 至少有一组 fixture 或测试插件覆盖 message、command、callback、inline、payment 五类事件。
- 前端日志页必须用真实 API 或固定 fixture 验收空状态、成功链路、未命中链路、插件失败链路、动作失败链路和窄屏/PWA。

### 0.7 最终版执行合同

后续执行时，“完成”只能按证据判断，不能按主观进度判断。每个任务卡必须在合并前给出以下证据：

- **代码证据**：列出实际改动文件、公共接口变化、兼容层位置和禁区是否被触碰。
- **测试证据**：列出自动化测试命令、关键测试用例、失败命令的第一条可行动错误和是否阻塞。
- **链路证据**：对消息、命令、callback、inline、payment、action 至少给出对应 Trace/span/action 的产生路径。
- **UI 证据**：涉及页面时必须说明桌面和窄屏/PWA 验收入口、空状态、错误态、长文本是否可用。
- **文档证据**：涉及插件契约时必须同步开发文档、示例和验证脚本，不能只改代码。
- **发布证据**：准备推送或部署时必须给出版本文件、中文 CHANGELOG、commit、远端 commit、部署健康检查。

任务状态统一使用：

- `未开始`：没有可复用实现。
- `半落地`：已有代码或 UI 雏形，但缺最终验收矩阵中的任一必备证据。
- `可测`：实现已贯通，定向测试通过，但尚未通过最终验收矩阵。
- `已完成`：自动验证、人工验收、文档审计和发布/回滚要求全部满足。

当前分支已有大量 `半落地` 代码。执行者必须先复核它们是否满足本合同，不能因为文件存在、定向测试通过或页面能打开，就把对应任务记为 `已完成`。

### 0.8 半落地代码处置规则

本计划不是要求从零重写，而是要求把当前分支已有实现收敛到最终版。处置规则如下：

- 已存在的 `event_trace`、`event_bus`、`Logs.tsx`、Delivery Executor、Contract Guard 改动，先按最终验收矩阵逐项补缺，不轻易推翻重写。
- 任何已经跑通的旧规则路径都视为回归底线；Event Bus 接入必须先旁路/映射，再逐步成为主路径。
- 半落地实现如果暴露公共契约不一致，例如字段名、reason_code、send_via、native_raw 边界，以第 11 节公共接口契约为准修正。
- 半落地实现如果只覆盖交互 Bot，不能据此宣称“所有消息进入 Event Bus”；UserBot、命令、外部转账通知、inline 都必须补齐。
- 半落地前端如果只展示 API 数据，不能据此宣称“日志系统可排障”；必须能回答第 1 节列出的排障问题。
- 半落地文档如果仍把旧规则、旧平铺 payload、旧 `notice` 当主路径，必须在最终版发布前重写或降级为迁移说明。
- 半落地测试如果只验证纯函数，必须补集成链路测试；如果只验证后端，必须补前端类型/构建和关键页面验收。

### 0.9 一票否决项

以下任一情况存在时，即使主要功能可用，也不得发布为最终版：

- 新插件仍需要直接理解旧交互规则才能接收普通消息。
- 插件可以绕过 `capabilities.telegram_native_raw` 拿到完整原生 Telegram 数据。
- 旧 `notice` / `bbot_notice` / `notice_bot` 被自动改写成可执行发送通道。
- 插件动作发送失败被记录为成功。
- 任一 Telegram 入口没有 Trace，或 Trace 中看不到插件匹配/跳过/投递原因。
- 日志页无法定位“消息为什么没触发插件”或“插件为什么没启动”。
- 远程插件安装/升级会丢失 `usage`、`event_subscriptions`、`capabilities`。
- 插件开发指南复制出来的最小插件不能通过验证脚本。
- 生产部署没有备份、没有迁移验证、没有远端版本和健康检查证据。

## 1. 目标

TelePilot 最终形态改为：所有 UserBot、交互 Bot、按钮回调、外部转账通知来源收到的 Telegram 事件，先进入统一 Event Bus，再按插件订阅投递给插件。平台不再替插件过度判断业务是否应该启动，而是负责标准化消息、记录完整链路、提供双通道消息操作、执行客观能力边界和风险提示。

新版日志页必须能回答：

- 系统当前是否健康。
- 某条消息进入系统后走到了哪一步。
- 消息为什么没有进入插件。
- 插件为什么没有启动。
- 插件被什么调用。
- 命令启动后调用了什么。
- 插件执行到哪一步卡住。
- 插件返回了什么动作。
- 平台实际发送、删除、置顶、编辑了什么。
- Contract Guard、频控、Telegram API、UserBot 离线等失败原因。

## 2. 非目标

- 不恢复旧 `notice` / `bbot_notice` 主动发送通道。
- 不把外部转账通知 Bot 当成 TelePilot 的发送通道。
- 不下发 Bot Token、UserBot session、API key、私钥等敏感凭据。
- 不要求平台替插件判断所有业务规则。
- 不把新版日志页做成旧 `runtime_log` 的美化版。
- 不为旧插件保留旧平铺字段作为主路径。
- 不把完整 Telegram driver 对象直接塞给插件；`native_raw` 必须是可序列化 dict，而不是 Telethon event / Bot API client / token 之类的 live object。
- 不默认把 `native_raw` 持久化进日志库；日志默认只保存摘要、体积、来源和是否下发。

## 3. 总体架构

```text
Telegram Update
  -> Source Adapter
  -> TelePilotEvent 标准化
  -> Trace 开始
  -> Event Bus
  -> Plugin Subscription Matcher
  -> Plugin Invocation
  -> Plugin Actions
  -> Contract Guard 软告警
  -> Delivery Executor / MessageOps
  -> Trace 完成
  -> 新日志中心展示
```

## 4. 关键决策

### 4.1 日志先于全量广播

先做 Trace / 日志系统，再做全量消息开放。原因是全量开放后消息量、插件候选、跳过原因、动作执行路径都会变多，没有 Trace 会比现在更难排查。

### 4.2 所有消息进入 Event Bus

所有 Telegram 消息都先进入统一 Event Bus。插件是否收到，由插件声明决定。支持插件声明：

```json
{
  "event_subscriptions": [
    {
      "source": ["userbot", "interaction_bot"],
      "events": ["message", "callback_query", "payment_confirmed"],
      "scope": "all_allowed_chats"
    }
  ]
}
```

### 4.3 插件可以自由判断

平台只做来源标准化、敏感字段剥离、频控、审计、客观能力边界。插件拿到标准事件后自行判断是否处理。

### 4.4 两种调度方式是正式主路径

新版不是“普通 Bot 完全独立跑游戏”，也不是“所有交互都必须由 userbot 回复”。主路径固定为两类：

1. **管理员命令调度**
   - 触发：账号主人或授权管理员用系统命令前缀触发。
   - 来源：UserBot 监听到命令消息。
   - 后续交互：默认由 userbot 继续回复或编辑，适合管理、配置、补发、查询、低频人工指令。
   - Trace：事件类型为 `command` 或 `message`，`source.channel=userbot`，`dispatch_mode=admin_command`。

2. **玩家关键词调度**
   - 触发：群内普通玩家发送插件声明的关键词、按钮、答案或 Inline Query。
   - 来源：UserBot 或交互 Bot 监听到消息后进入 Event Bus。
   - 后续交互：默认由 `interaction_bot` 承接高频玩法消息、按钮、开奖公告和普通结果通知。
   - 钱相关动作：收款确认、发奖、补发、转账等仍由 userbot 或平台受控结算流程处理；普通 Bot 只能公告和参与交互，不能被视为有转账能力。
   - Trace：事件类型为 `message` / `callback_query` / `inline_query` / `payment_confirmed`，`dispatch_mode=public_keyword` 或订阅声明中的显式模式。

插件可以在 action 中选择 `interaction_bot`、`userbot_reply` 或 `auto`。平台尊重插件选择，但必须记录实际发送通道、失败原因和回退路径。

### 4.5 日志以 Trace 为中心

旧 `runtime_log` 只能回答“某时刻写了一行什么”。新版必须围绕 `trace_id` 展示一条消息完整生命周期。

### 4.6 旧日志保留为原始日志

当前 `runtime_log` 和 `/api/logs/runtime` 可保留为底层兼容和原始文本流，但新版日志页默认不再以它为主。

### 4.7 `native_raw` 是可信插件显式能力

`payload["native_raw"]` 支持，但必须是插件显式声明、账号主人确认后的能力，不默认下发给所有插件。

设计理由：

- TelePilot 是个人可信插件系统，插件和插件库由账号主人主动安装，平台不应该把“平台投影字段不完整”变成插件开发者绕不开的限制。
- 防改名诈骗、付款关系链、回复链、forward 来源、via bot、sender_chat 等风控场景，确实需要完整数字 ID 关系链。
- 风险不应通过隐藏数据来伪安全，而应该通过插件声明、WebUI 风险提示、审计、Trace 留痕、保留策略和急停来承担。

边界：

- UserBot / Telethon 来源：`native_raw` 放 `event.message.to_dict()` 的 JSON 兼容版本，尽量完整保留 Telegram 原生字段。
- 交互 Bot / Bot API 来源：`native_raw` 放 Telegram Bot API 原始 update 中对应对象，例如 `message`、`callback_query`、`inline_query` 或完整 `update` 的 JSON 兼容版本。
- 外部转账通知来源：如果来自 UserBot 监听群消息，则按 Telethon message 处理；如果来自 Bot API，则按 Bot API update 处理。
- 不加入 Bot Token、UserBot session、平台 API key、数据库连接串、插件仓库凭据等传输/平台凭据，因为这些本来不属于 Telegram 原始消息对象。
- `payload_snapshot` 默认不保存 `native_raw`，只保存 `native_raw_meta`；需要保存完整原生数据时必须有单独的全局设置和短保留期。

推荐插件声明：

```json
{
  "capabilities": {
    "telegram_native_raw": {
      "enabled": true,
      "sources": ["userbot", "interaction_bot"],
      "reason": "需要完整数字 ID 关系链做防改名诈骗风控"
    }
  }
}
```

标准事件中增加：

```json
{
  "native_raw": {},
  "native_raw_meta": {
    "enabled": true,
    "source": "userbot",
    "driver": "telethon",
    "object": "message",
    "stored_in_trace": false,
    "size_bytes": 12345
  }
}
```

日志页必须能看到“该插件请求了原生数据免检通道、本次事件是否下发、大小是多少、是否被持久化”，但默认不展示完整内容。

### 4.8 Inline 模式是一等事件

Inline 模式不是按钮回调的变体。它应作为独立事件进入 Event Bus：

- `inline_query`：用户在任意聊天框输入 `@botname 关键词`。
- `chosen_inline_result`：用户选择了某个 inline 结果，建议同步纳入 Event Bus 以便统计和后续状态回写。

标准事件增加：

```json
{
  "source": {
    "type": "inline_query",
    "channel": "interaction_bot",
    "driver": "telegram_bot_api",
    "inline_query_id": "AA...",
    "chat_id": null
  },
  "inline_query": {
    "id": "AA...",
    "query": "关键词",
    "offset": "",
    "chat_type": "sender",
    "from": {
      "user_id": 123,
      "display_name": "Alice",
      "username": "alice"
    }
  }
}
```

Delivery Executor 增加动作：

```json
{
  "type": "answer_inline_query",
  "inline_query_id": "AA...",
  "results": [
    {
      "type": "article",
      "id": "result-1",
      "title": "标题",
      "input_message_content": {
        "message_text": "发送到聊天里的内容",
        "parse_mode": "HTML"
      }
    }
  ],
  "cache_time": 0,
  "is_personal": true
}
```

Inline 事件没有稳定群 `chat_id`，不能套用“允许会话”判断。订阅声明必须单独支持 `scope`：

- `owner_only`：仅账号主人/管理员可触发。
- `known_users`：仅平台见过的用户。
- `inline_all`：允许所有 Telegram 用户触发，需要 WebUI 高级风险提示。

交互 Bot polling / webhook 的 `allowed_updates` 必须加入：

```json
["message", "callback_query", "inline_query", "chosen_inline_result"]
```

### 4.9 `raw` / `raw_event` / `native_raw` 边界

新版只保留三个清晰概念：

- `raw`：平台生成的脱敏摘要，只用于日志和排障，不承诺包含完整 Telegram 原始结构。
- `native_raw`：插件显式声明 `capabilities.telegram_native_raw.enabled=true` 后才下发的原生 Telegram dict，用于严格风控、关系链校验、reply/forward/via bot/source chat 等高级场景。
- `native_raw_meta`：不论是否下发 `native_raw` 都要提供，记录本次是否允许、来源、driver、对象类型、大小、是否持久化、失败原因。

不再把 `raw_event` 作为新版公开协议。若历史代码里仍有 `raw_event`，最终版必须处理为以下两种之一：

- 内部变量：只存在于 Source Adapter 内部，不进入插件 payload 和文档。
- 迁移失败提示：插件读取旧 `raw_event` 时得到空值或兼容摘要，并在 Trace / 插件规范警告中提示改用 `native_raw` 声明。

禁止把 `raw_event` 作为绕过 `telegram_native_raw` 声明的后门。

### 4.10 Contract Guard 的最终定位

Contract Guard 不再是“平台替个人插件做强安全沙箱”，而是可信插件系统里的契约记录器和客观失败保护层：

- 插件调用声明外能力时，平台原则上不替账号主人阻断业务，但必须产生规范警告、Trace span 和日志页提示。
- 插件请求平台根本不支持的动作或通道，例如旧 `notice`、缺失 inline_query_id 的 `answer_inline_query`、普通 Bot 执行转账，必须明确失败并返回可读错误。
- 插件请求高风险但可执行能力，例如 `native_raw`、`inline_all`、跨通道发送，必须在安装/启用/配置页显示风险提示，并在 Trace 中记录实际使用。
- Contract Guard 的输出必须同时包含机器可筛选的 `reason_code` 和中文可读说明。

第一版 severity 分级：

- `info`：声明内调用，记录审计。
- `warning`：越声明调用但平台可执行，放行并告警。
- `blocked`：客观不可执行或明确废弃能力，拒绝执行。
- `failed`：平台尝试执行但 Telegram API、UserBot、Bot token、网络或权限失败。

### 4.11 `notice` 通道收口

用户所说的“转账通知 Bot”是群里第三方已有 Bot，它只作为外部消息来源，用于确认转账结果；它不是 TelePilot 的发送通道。

最终版必须统一为：

- 外部转账通知 Bot 的消息进入 Event Bus 时，事件来源为 `external_payment_notice` 或 `source_actor.type=external_bot`。
- 插件普通交互内容、玩法结果、开奖公告、按钮反馈，如果选择普通 Bot 执行，必须走 `interaction_bot`。
- 涉及转账、发奖、补发、余额等动作，必须走 userbot 或平台受控结算动作，不能走 `interaction_bot`。
- 旧 `notice` / `bbot_notice` / `notice_bot` 只作为迁移错误值处理，不能再恢复为任何发送通道别名。

运行时遇到旧值时必须：

- `event_action.status = "failed"` 或 manifest lint 失败/警告。
- `reason_code = "send_channel_deprecated"`。
- 中文提示：“notice/bbot_notice 已不是系统发送通道，请改用 interaction_bot、userbot_reply 或 auto；外部转账通知 Bot 仅作为消息来源。”

### 4.12 两种调度流程硬定义

最终版只保留两条产品主流程，避免继续出现“规则、命令、Bot 回调、插件自跑”多套解释。

管理员命令调度：

```text
管理员/账号主人发送命令
  -> UserBot 收到消息
  -> Source Adapter 标准化为 command/message
  -> Event Bus 匹配 owner_only / command filters
  -> 插件入口执行
  -> 插件通过 ctx.messages/action 请求 userbot_reply 或 auto
  -> Delivery Executor 执行
  -> Trace 展示命令解析、插件调用、动作发送
```

玩家关键词调度：

```text
玩家在群里发送关键词/答案/点击按钮/Inline Query
  -> UserBot 或 interaction_bot 收到事件
  -> Source Adapter 标准化为 message/callback_query/inline_query
  -> Event Bus 匹配 all_allowed_chats/known_users/inline_all 与 filters
  -> 插件入口执行并维护会话状态
  -> 普通交互动作走 interaction_bot
  -> 转账/发奖/结算动作走 userbot 或 settlement
  -> Trace 展示玩家、会话、付款归属、动作结果
```

任何插件都可以在这两条流程中自由决定是否处理事件；平台只负责让事件完整、动作可执行、风险可见、失败可查。

## 5. 新数据模型

### 5.1 event_trace

记录一条 Telegram 事件的主链路。

字段：

- `trace_id`
- `account_id`
- `source_channel`
- `event_type`
- `chat_id`
- `message_id`
- `update_id`
- `callback_query_id`
- `sender_user_id`
- `sender_name`
- `text_preview`
- `status`
- `started_at`
- `ended_at`
- `duration_ms`
- `raw_summary`
- `payload_snapshot`
- `native_raw_meta`

### 5.2 event_span

记录链路中的每一步。

典型阶段：

- `receive`
- `normalize`
- `route`
- `rule_match`
- `session_match`
- `subscription_match`
- `plugin_load`
- `plugin_invoke`
- `plugin_return`
- `contract_guard`
- `delivery`
- `settlement`
- `finish`
- `inline_answer`

字段：

- `span_id`
- `trace_id`
- `parent_span_id`
- `phase`
- `component`
- `plugin_key`
- `entry_key`
- `status`
- `reason_code`
- `message`
- `detail`
- `started_at`
- `ended_at`
- `duration_ms`

`reason_code` 必须使用稳定枚举，避免日志页变成不可搜索的自由文本。第一批枚举：

- `plugin_disabled`
- `plugin_not_installed`
- `plugin_load_failed`
- `subscription_not_matched`
- `rule_not_matched`
- `session_not_found`
- `rate_limited`
- `contract_warning`
- `contract_failed`
- `telegram_api_error`
- `userbot_offline`
- `bot_token_missing`
- `native_raw_not_allowed`
- `inline_query_answer_failed`

### 5.3 event_action

记录插件返回动作与平台实际执行动作。

字段：

- `action_id`
- `trace_id`
- `plugin_key`
- `action_type`
- `requested_send_via`
- `actual_send_via`
- `target_chat_id`
- `target_message_id`
- `status`
- `telegram_message_id`
- `inline_result_count`
- `error_code`
- `error_message`
- `detail`

### 5.4 plugin_runtime_status

记录插件加载和运行状态。

字段：

- `plugin_key`
- `account_id`
- `enabled`
- `installed_version`
- `load_status`
- `last_load_error`
- `last_invoked_at`
- `last_invocation_status`
- `last_trace_id`

### 5.5 索引与保留策略

Trace 表会明显比旧日志更大，必须随迁移一起建索引和清理策略：

- `event_trace(account_id, started_at desc)`
- `event_trace(account_id, chat_id, message_id)`
- `event_trace(account_id, update_id)`
- `event_trace(status, started_at desc)`
- `event_span(trace_id, started_at)`
- `event_span(plugin_key, started_at desc)`
- `event_action(trace_id)`
- `event_action(plugin_key, status, created_at desc)`

默认保留：

- `event_trace` / `event_span` / `event_action`：30 天。
- `payload_snapshot`：7 天或按全局设置关闭。
- 完整 `native_raw`：默认不保存；开启后默认只保留 1 天，并在日志页显式标记高风险。

### 5.6 设置与回退开关

为了让最终版可以安全上线、实测和回滚，必须新增或复用以下系统设置。最终版 WebUI、API 文档和开发文档统一使用下列设置名；如历史代码已有不同命名，兼容别名只能留在迁移层，并必须在最终证据台账中登记映射关系。

- `trace_enabled`：是否写入 Trace。默认开启；关闭后不得影响旧 `runtime_log` / `audit_log` 和插件主流程。
- `trace_payload_snapshot_enabled`：是否保存脱敏 payload snapshot。默认开启。
- `trace_payload_snapshot_retention_days`：payload snapshot 保留天数。默认 7 天。
- `trace_retention_days`：trace/span/action 主记录保留天数。默认 30 天。
- `native_raw_persist_enabled`：是否持久化完整 `native_raw`。默认关闭。
- `native_raw_retention_days`：完整 `native_raw` 保留天数。默认 1 天，且仅在持久化开关开启时生效。
- `event_bus_delivery_enabled`：是否启用 Event Bus 新投递路径。最终版默认开启；紧急回滚时可关闭并回退到旧规则驱动路径。
- `inline_updates_enabled`：是否允许交互 Bot 拉取/接收 `inline_query` 和 `chosen_inline_result`。默认按账号 Bot 配置开启。

这些开关不是产品上长期保留的多模式入口，而是部署和回滚护栏。最终版 WebUI 不应把它们包装成“标准模式/个人模式”，只在系统高级设置或运维配置中暴露。

`event_bus_delivery_enabled=false` 只能代表紧急降级状态，不能作为最终版常态运行证据。关闭后系统必须在设置页、日志页或证据台账中标记 `degraded_event_bus_disabled`；此时可以证明“系统可回滚”，但不能证明“入口唯一”。最终签收第 2 项时，该开关必须处于开启状态，且 legacy 入口只能由 Event Bus decision 驱动。

### 5.7 Trace 数据体积上限

为了避免日志系统本身拖垮主流程，必须定义数据体积上限：

- `text_preview` 默认截断到 500 字符以内。
- `payload_snapshot` 默认脱敏并限制大小，超过上限时写入 `truncated=true`、`size_bytes` 和截断原因。
- `native_raw` 即使允许下发给插件，也不能默认写入数据库；若下发对象过大，必须记录 `native_raw_meta.size_bytes` 和 `native_raw_meta.truncated_for_trace=true`。
- 单条 trace 的 span/action 数量应有软上限；超过上限时继续执行业务，但日志页展示聚合摘要并记录 `trace_span_limit_reached`。
- Trace 写入失败不得阻断插件业务，但必须写旧 runtime error 以便发现日志系统故障。

## 6. 新日志页

### 6.1 总览

展示：

- worker 状态。
- Bot 状态。
- Redis / DB / 队列状态。
- 最近 5 分钟事件数。
- 最近错误插件。
- 最近失败发送动作。
- 最近 Contract Guard 告警。

### 6.2 消息链路

支持按以下条件搜索：

- chat_id
- message_id
- trace_id
- 用户 ID
- 插件 key
- 关键词
- 时间范围
- 状态

点开一条消息后展示完整时间线：

```text
收到消息
-> 标准化成功
-> 命中 3 个插件订阅
-> 插件 A 跳过：关键词不匹配
-> 插件 B 执行成功
-> 插件 B 返回 send_message
-> Contract Guard warning
-> interaction_bot 发送成功
```

### 6.3 插件诊断

展示每个插件：

- 是否安装。
- 是否启用。
- 是否加载成功。
- 最近被什么调用。
- 最近失败原因。
- 最近 20 条 invocation。
- 最近返回 actions。
- 最近 Contract Guard 告警。

### 6.4 命令链路

管理员命令触发后展示：

- 命令来源。
- 命令解析。
- 命中的插件/系统处理器。
- 调用的服务。
- 产生的动作。
- 最终发送结果。

### 6.5 动作发送

展示：

- 插件请求动作。
- 请求通道。
- 平台实际通道。
- Telegram API 返回。
- UserBot 是否离线。
- Bot token 是否缺失。
- 删除/置顶/编辑失败原因。

### 6.6 原始日志

保留旧 runtime log / audit log 作为高级排障页，不再作为默认入口。

### 6.7 原生数据与 Inline 调试

新增高级折叠区：

- 标准事件信封。
- `raw_summary`。
- `native_raw_meta`。
- `native_raw` 是否下发给插件。
- Inline Query 请求参数。
- Inline Answer 返回结果数量、Telegram API 错误、cache_time / is_personal。

默认不展开完整 JSON；点击展开时提醒“这是插件免检通道数据，可能包含完整消息内容和关系链”。

## 7. 实施阶段

这些阶段是可独立合并、可独立部署观察的施工波次，不是最终目标的降级版。每个波次完成后系统都必须保持可用；如果任一波次失败，应能回滚到上一波次而不破坏旧 `runtime_log`、旧交互规则和现有插件调用。

最终版执行时采用以下口径：

- 需要快速交付给服务器实测时，可以先发布 `0.38.0` 和 `0.39.0` 作为稳定检查点。
- 用户要求“一次做到最终版”时，三个波次仍按顺序施工和 review，但最终只在全部验收通过后统一 bump 到 `0.40.0`。
- 版本号只能在发布检查点或最终合并前统一修改，不能每个小任务单独 bump。

### 0.38.0 minor（次版本）：链路日志与日志页重构

- 新增 `event_trace`、`event_span`、`event_action`、`plugin_runtime_status`。
- 接入现有交互 Bot、UserBot 命令、插件 loader、Delivery Executor。
- 新增 Trace 写入服务。
- 新增日志中心 API。
- 重构前端日志页为“总览 / 消息链路 / 插件诊断 / 命令链路 / 动作发送 / 原始日志”。
- 所有现有 `ctx.log`、Contract Guard、Delivery Executor 日志补 `trace_id` / `plugin_key` / `entry_key`。
- `payload_snapshot` 默认脱敏，不保存完整 `native_raw`。
- 当前规则驱动调度保持不变。
- 交付后必须仍能按旧规则启动插件；新日志页已经可用，但还不宣称 Event Bus 全量开放。

### 0.39.0 minor（次版本）：统一 Event Bus 与插件订阅

- 所有 Telegram 消息进入 Event Bus。
- 新增插件 `event_subscriptions` 声明。
- UserBot 消息、交互 Bot 消息、callback、inline_query、chosen_inline_result、付款确认统一生成 TelePilotEvent。
- 插件可声明接收所有允许会话消息。
- 插件可声明 `telegram_native_raw` 能力，WebUI 展示高风险提示并在 Trace 留痕。
- 交互 Bot polling / webhook 支持 `inline_query` / `chosen_inline_result`。
- Delivery Executor 支持 `answer_inline_query`。
- 每次投递和跳过都写入 Trace。
- 旧规则命中逻辑降级为订阅条件的一种来源。
- 交付后新插件可不依赖旧规则接收事件；旧规则仍能作为筛选条件继续工作。

### 0.40.0 minor（次版本）：最终开放插件运行模型

- 插件可在同一入口处理命令、关键词、按钮、付款、普通消息。
- 插件可自由选择 `interaction_bot` / `userbot_reply` / `auto`。
- 平台只做风险提示、频控、审计、敏感字段剥离和客观失败返回。
- 插件开发指南全面切换到 Event Bus + Trace 模型。
- 插件开发指南新增 `native_raw` 风控示例、Inline Query 插件示例、Trace 排障清单。
- 清理 WebUI 和文档中的旧主路径描述；旧字段只作为迁移说明出现，不再作为新插件推荐路径。

## 8. 验证标准

必须能在日志页完成以下排查：

- 输入一条群消息 ID，看到它是否进入系统。
- 看到它为什么没有触发任何插件。
- 看到它命中了哪些插件订阅。
- 看到插件为什么跳过。
- 看到插件为什么加载失败。
- 看到插件执行耗时。
- 看到插件返回 actions。
- 看到平台最终用哪个账号/Bot 发送。
- 看到 Telegram API 失败原因。
- 看到 Contract Guard 是 warning 还是 failed。
- 看到命令触发后完整调用链。
- 指定插件声明 `telegram_native_raw` 后，能在插件 payload 中拿到 `native_raw`，并在日志页看到 `native_raw_meta`。
- 未声明 `telegram_native_raw` 的插件拿不到 `native_raw`，日志页记录 `native_raw_not_allowed` 或 `native_raw_skipped`。
- 发送 `@botname 关键词` 后能产生 `inline_query` trace，插件能返回 `answer_inline_query`，日志页能看到 Telegram API 成功或失败。
- Inline 结果被选择时能产生 `chosen_inline_result` trace。

## 9. 风险与处理

- 数据量变大：Trace 表必须有保留策略、索引和按时间清理。
- 文本敏感：默认保存 `text_preview`，完整 payload 需脱敏并受设置控制。
- 插件过多：订阅匹配必须先过滤账号、会话、事件类型，再调用插件。
- UI 复杂：默认展示时间线，JSON 细节折叠。
- 回滚：保留旧 `runtime_log`，新 Trace 可独立关闭写入。
- `native_raw` 过大：默认只投递不持久化；如开启保存，必须有短保留期、大小记录和清理任务。
- `native_raw` 字段类型复杂：进入插件前统一转换成 JSON 兼容 dict，保留字段名和值，不暴露 live driver object。
- Inline Query 没有 chat_id：不能用群白名单硬套，必须使用独立 scope 和风险提示。
- Inline Answer 结果格式复杂：第一版支持 Bot API 常用 `article` / `photo` / `gif` / `document` 结果透传，并把 Telegram API 错误完整写入 `event_action`。

## 10. 最终版完成定义

“最终版”不是指把所有 Telegram API 都封装一遍，而是指 TelePilot 对插件提供稳定、可排障、可扩展的事件与消息操作框架。完成后必须满足：

- 所有 UserBot、交互 Bot、按钮回调、Inline Query、Inline 结果选择、外部转账通知来源都进入统一 Event Bus。
- 插件通过 `event_subscriptions` 声明自己要接收哪些事件，平台记录每一次匹配、跳过和投递原因。
- 插件收到统一标准事件信封，业务优先读标准字段，需要严格风控时可声明 `telegram_native_raw` 获取 `payload["native_raw"]`。
- 插件通过 `ctx.messages` 或标准 action 请求发送、编辑、删除、置顶、回应按钮、回应 Inline Query；插件选择通道，平台负责执行、回退、记录和客观失败返回。
- 日志中心默认围绕 `trace_id` 展示消息生命周期，而不是围绕旧文本日志；旧 `runtime_log` 只作为原始日志高级入口。
- 插件加载失败、未启用、未订阅、会话不匹配、Contract Guard 告警、频控、Telegram API 失败、UserBot 离线等原因都能在日志页直接看到。
- 插件开发指南以 Event Bus + Trace + MessageOps 为主路径，旧规则驱动和平铺 payload 不再作为新插件开发主路径。

最终版验收口径：

- 任意一条群消息：能查到是否进入系统、标准化结果、匹配了哪些插件、哪些插件跳过、哪些插件执行、执行耗时、返回动作和最终发送结果。
- 任意一个插件：能查到安装/启用/加载状态、最近被什么事件调用、最近为什么失败、最近返回了什么动作。
- 任意一个动作：能查到插件请求内容、Contract Guard 结果、实际通道、Telegram API 响应或 UserBot 失败原因。
- 任意一个 Inline Query：能查到 query 来源、订阅匹配、插件返回结果数量、`answerInlineQuery` 成败和 `chosen_inline_result`。
- 任意一个声明 `telegram_native_raw` 的插件：能拿到 `native_raw`，日志页能看到 `native_raw_meta`，默认不会把完整 `native_raw` 长期持久化。

## 11. 公共接口契约

### 11.1 标准事件信封

所有插件收到的 payload 统一包含以下顶层字段：

```json
{
  "trace_id": "evt_...",
  "source": {},
  "message": {},
  "chat": {},
  "sender": {},
  "actor": {},
  "source_actor": {},
  "player": {},
  "payment": null,
  "reply_to": null,
  "session": null,
  "trigger": {},
  "inline_query": null,
  "chosen_inline_result": null,
  "raw": {},
  "native_raw_meta": {},
  "native_raw": null
}
```

字段边界：

- `trace_id`：贯穿 Trace、插件日志、action、Delivery Executor。
- `source`：事件来源和 driver 信息，例如 `type`、`channel`、`driver`、`account_id`、`update_id`、`message_id`、`callback_query_id`、`inline_query_id`。
- `message` / `chat` / `sender`：Telegram 消息、会话、实际发送者的稳定投影。
- `actor`：业务动作主体；可等于 sender，也可由付款/回复链推断。
- `source_actor`：实际产生事件的一方，例如外部转账通知 Bot。
- `player`：游戏/玩法中的玩家身份，付款事件中通常为付款人。
- `payment`：只有付款确认类事件有有效内容。
- `reply_to`：被回复消息摘要，用于付款原消息归属等场景。
- `inline_query`：只有 `source.type == "inline_query"` 时有值。
- `chosen_inline_result`：只有 `source.type == "chosen_inline_result"` 时有值。
- `raw`：脱敏摘要，排障用，不作为长期业务协议。
- `native_raw`：可信插件显式声明后才下发的原生 Telegram dict。

### 11.2 插件 manifest 新增字段

远程插件和内置/官方插件 manifest 均支持：

```json
{
  "event_subscriptions": [
    {
      "source": ["userbot", "interaction_bot", "external_payment_notice"],
      "events": ["message", "command", "callback_query", "inline_query", "chosen_inline_result", "payment_confirmed"],
      "scope": "all_allowed_chats",
      "entry_key": "main",
      "filters": {
        "keywords": ["开始"],
        "command_prefix_required": false
      }
    }
  ],
  "capabilities": {
    "telegram_native_raw": {
      "enabled": true,
      "sources": ["userbot"],
      "reason": "需要完整数字 ID 关系链做防改名诈骗风控"
    }
  }
}
```

订阅 scope 第一版支持：

- `all_allowed_chats`：账号允许会话内事件。
- `owner_only`：仅账号主人/管理员。
- `known_users`：平台已见过用户。
- `inline_all`：Inline 对所有 Telegram 用户开放，WebUI 必须高风险提示。
- `rule_bound`：旧交互规则迁移过渡，按现有规则范围触发。

### 11.3 MessageOps / Action 契约

标准 action 类型：

- `send_message`
- `send_photo`
- `send_file`
- `edit_message`
- `delete_message`
- `pin_message`
- `answer_callback`
- `answer_inline_query`
- `settlement`
- `end_session`

`send_via` / `channel_selector` 支持：

- `interaction_bot`
- `userbot_reply`
- `auto`

旧 `notice` / `bbot_notice` / `notice_bot` 不恢复；这些值只能产生明确失败和迁移提示。

`answer_inline_query` 第一版字段：

```json
{
  "type": "answer_inline_query",
  "inline_query_id": "AA...",
  "results": [],
  "cache_time": 0,
  "is_personal": true,
  "next_offset": "",
  "button": null
}
```

### 11.4 Event Bus Service 接口

新增服务放在 `backend/app/services/event_bus.py`，第一版必须提供以下接口：

- `normalize_bot_update(account_id, update, *, channel) -> TelePilotEvent`
- `normalize_userbot_event(account_id, event, *, command_meta=None) -> TelePilotEvent`
- `normalize_payment_notice(account_id, event, parsed) -> TelePilotEvent`
- `normalize_event_subscription(raw, *, plugin_key, entry_key=None) -> EventSubscription`
- `match_subscriptions(event, subscriptions, account_state) -> list[SubscriptionDecision]`
- `dispatch_event(event) -> DispatchResult`

`SubscriptionDecision` 必须包含：

- `plugin_key`
- `entry_key`
- `matched`
- `reason_code`
- `reason_message`
- `dispatch_mode`
- `scope`
- `filters`

匹配顺序固定：

1. account 是否一致。
2. 插件是否安装和启用。
3. 事件来源 `source.channel` 是否匹配。
4. 事件类型是否匹配。
5. scope 是否匹配，例如 `all_allowed_chats`、`owner_only`、`known_users`、`inline_all`、`rule_bound`。
6. filters 是否匹配，例如关键词、命令、callback data、付款金额、reply_to、chat_id。
7. session 是否匹配或是否需要新建。
8. 投递给插件。

跳过记录采用“候选明细 + 聚合摘要”：

- 对已进入候选集的插件，必须写 `event_span`，记录 matched / skipped / delivered 和稳定 `reason_code`。
- 对明显不相关的插件，例如未订阅该 source/event_type 的插件，可以写聚合摘要 span：`subscription_not_matched_count`、`source_not_subscribed_count`、`event_type_not_subscribed_count`。
- 如果用户在日志页按某个插件 key 深挖，API 再按 manifest 现场解释“该插件为什么不是候选”，避免每条消息为每个插件落库。
- 不能只在最终“没触发插件”时写一条笼统日志。

`reason_code` 第一版至少包含：

- `account_not_matched`
- `plugin_disabled`
- `plugin_not_installed`
- `event_type_not_subscribed`
- `source_not_subscribed`
- `scope_not_matched`
- `filter_not_matched`
- `session_not_found`
- `rate_limited`
- `native_raw_not_allowed`
- `plugin_load_failed`
- `plugin_runtime_error`
- `action_failed`

### 11.5 Trace Service 接口

新增服务建议放在 `backend/app/services/event_trace.py`，提供以下稳定接口：

- `start_trace(event) -> TraceContext`
- `record_span(trace, phase, status, **detail) -> EventSpan`
- `record_action(trace, action, status, **detail) -> EventAction`
- `finish_trace(trace, status, **summary) -> None`
- `trace_log_context(trace, plugin_key=None, entry_key=None) -> dict`
- `redact_payload_snapshot(payload) -> dict`

所有接口必须吞掉非关键日志写入异常，不能因为 Trace 写库失败阻断插件主流程；但 Trace 服务故障要写入 `runtime_log` 的 system/error。

### 11.6 日志 API 契约

新增 API 放在 `backend/app/api/logs.py` 或拆分 `backend/app/api/event_traces.py` 后在 `backend/app/main.py` 注册：

- `GET /api/logs/trace/overview`
- `GET /api/logs/trace/events`
- `GET /api/logs/trace/events/{trace_id}`
- `GET /api/logs/trace/plugins`
- `GET /api/logs/trace/plugins/{plugin_key}`
- `GET /api/logs/trace/actions`
- `GET /api/logs/trace/commands`

必须保留：

- `GET /api/logs/runtime`
- `GET /api/logs/audit`

新 API 统一支持 `account_id`、`since`、`until`、`status`、`plugin_key`、`event_type`、`chat_id`、`message_id`、`trace_id`、`keyword`、`limit`。

### 11.7 插件运行时 API 契约

开发者最终只需要记住三类入口，不应再理解平台内部旧规则细节：

事件读取：

- `payload["trace_id"]`
- `payload["source"]`
- `payload["message"]`
- `payload["chat"]`
- `payload["sender"]`
- `payload["actor"]`
- `payload["player"]`
- `payload["payment"]`
- `payload["reply_to"]`
- `payload["session"]`
- `payload["trigger"]`
- `payload["inline_query"]`
- `payload["chosen_inline_result"]`
- `payload["native_raw_meta"]`
- `payload["native_raw"]`

消息操作：

- `ctx.messages.send_text(...)`
- `ctx.messages.send_photo(...)`
- `ctx.messages.send_file(...)`
- `ctx.messages.edit_message(...)`
- `ctx.messages.delete_message(...)`
- `ctx.messages.pin_message(...)`
- `ctx.messages.answer_callback(...)`
- `ctx.messages.answer_inline_query(...)`
- `ctx.messages.settlement(...)`
- `ctx.messages.end_session(...)`

排障记录：

- `ctx.log.info(...)`
- `ctx.log.warning(...)`
- `ctx.log.error(...)`

所有 `ctx.log` 必须自动补上 `trace_id`、`plugin_key`、`entry_key`。插件开发者不需要手动拼 trace 字段，但可以读取 `payload["trace_id"]` 在业务日志或外部记录里关联。

### 11.8 插件 manifest 最小最终版

新插件 manifest 至少应能表达以下内容：

```json
{
  "key": "example_game",
  "name": "示例玩法",
  "version": "1.0.0",
  "description": "演示 Event Bus + MessageOps 的最小插件",
  "usage": "在允许群内发送“开始示例”启动玩法；点击按钮或回复答案继续。",
  "event_subscriptions": [
    {
      "source": ["userbot", "interaction_bot"],
      "events": ["message", "callback_query"],
      "scope": "all_allowed_chats",
      "entry_key": "main",
      "filters": {
        "keywords": ["开始示例"]
      }
    }
  ],
  "capabilities": {
    "telegram_native_raw": {
      "enabled": false
    }
  }
}
```

规范要求：

- `usage` 必须由插件声明；没有详细使用说明时，插件配置页和规范警告必须显示红色高级警告。
- `event_subscriptions` 为空时，插件只能被手动命令或系统内部显式调用；WebUI 必须提示“该插件没有声明可自动接收的事件”。
- `capabilities.telegram_native_raw.enabled=true` 时必须提供 `reason`，否则 lint 报警。
- `send_via` 只允许 `interaction_bot`、`userbot_reply`、`auto`；旧 `notice` / `bbot_notice` / `notice_bot` 必须报迁移警告或保存失败。
- 远程插件仓库同步、私有 GitHub 仓库、`tree/<branch>` 分支 URL、安装记录和本地插件 manifest 必须保留这些字段，不得在任一层丢失。

### 11.9 文档漂移审计

最终版发布前必须对开发文档做一次反向审计：

- 从代码 schema 反查文档字段，确认文档没有漏掉 `event_subscriptions`、`capabilities`、`native_raw_meta`、`answer_inline_query`、`settlement`。
- 从文档示例反跑验证脚本，确认示例 manifest 和示例 payload 可被当前代码接受。
- 搜索旧概念：`notice`、`bbot_notice`、`raw_event`、旧平铺 `payload["text"]` 主路径、旧规则驱动主路径。保留处必须明确标注“迁移说明”或“废弃值”，不能作为推荐写法。
- 插件开发指南必须包含“插件为什么没启动”的排查顺序：安装状态、启用状态、manifest lint、event_subscriptions、scope、filters、session、rate limit、plugin load、plugin runtime、action delivery。

## 12. 实施依赖图

```text
0.38 Trace 数据层
  -> Trace Service
  -> 现有交互链路埋点
  -> 新日志 API
  -> 新日志 UI
  -> 文档/测试

0.39 Source Adapter / Event Bus
  -> 插件订阅匹配
  -> native_raw 能力
  -> Inline Query 入口
  -> answer_inline_query Delivery
  -> Trace 覆盖新链路
  -> 文档/测试

0.40 插件运行模型收敛
  -> 旧规则降级为订阅条件
  -> 插件迁移示例
  -> 开发指南最终版
  -> 清理旧概念和 UI 文案
```

并行原则：

- 数据模型和 Trace Service 先于 UI 与链路埋点。
- Event Bus 先于全量消息投递。
- Inline Delivery 可和 Source Adapter 并行，但必须最后用 Trace 串起来。
- 插件文档可以先写草案，但最终必须以代码实际字段为准复核。

## 13. 0.38.0 施工任务卡：链路日志与日志页重构

### T38-1 数据模型与迁移

写入范围：

- `backend/app/db/models/log.py`
- `backend/app/db/models/__init__.py`
- `backend/alembic/versions/0031_event_trace.py`

具体改动：

- 新增 `EventTrace`、`EventSpan`、`EventAction`、`PluginRuntimeStatus` ORM。
- 建立第 5.5 节索引。
- `payload_snapshot`、`raw_summary`、`native_raw_meta`、`detail` 使用 JSON。
- 所有外键删除策略不得影响原始 `runtime_log`。

禁止改动：

- 不删除 `RuntimeLog` 和 `AuditLog`。
- 不改旧日志 API 返回字段。

测试：

- 新增迁移结构测试或模型导入测试。
- 执行 `cd backend && .venv/bin/alembic upgrade head`。

验收：

- 空库可迁移成功。
- 旧库可迁移成功。
- 新表存在且索引存在。

### T38-2 Trace Service

写入范围：

- `backend/app/services/event_trace.py`
- `backend/app/services/redactor.py`
- `backend/app/tests/test_event_trace_service.py`

具体改动：

- 实现第 11.5 节接口。
- 实现 payload 脱敏和 `native_raw` 默认剥离。
- `trace_id` 使用稳定可搜索字符串，例如 `evt_` 前缀加 UUID/ULID。
- Trace 写入失败不阻断主流程。

禁止改动：

- 不在业务代码里直接拼 SQL 写 Trace。

测试：

- start/span/action/finish happy path。
- payload_snapshot 不含完整 `native_raw`。
- Trace 写库异常不向外抛。

验收：

- 单元测试能证明 Trace 服务可独立使用。

### T38-3 现有交互链路埋点

写入范围：

- `backend/app/services/account_bot_runtime.py`
- `backend/app/services/interaction/delivery.py`
- `backend/app/services/interaction/contracts.py`
- `backend/app/worker/plugins/loader.py`
- `backend/app/worker/plugins/message_ops.py`

具体改动：

- 在交互 Bot 收到 message/callback/payment 时创建 trace。
- 规则匹配、会话匹配、插件加载、插件调用、插件返回、Contract Guard、Delivery Executor 全部写 span。
- 插件 `ctx.log` 自动带 `trace_id`、`plugin_key`、`entry_key`。
- Delivery Executor 每个 action 写 `event_action`，记录请求通道、实际通道、结果、错误。

禁止改动：

- 不改变现有规则驱动调度语义。
- 不因 Trace 写入失败中断消息处理。

测试：

- 扩展 `backend/app/tests/test_account_bot.py`。
- 扩展 `backend/app/tests/test_plugin_security_regression.py`。

验收：

- 现有交互规则仍能启动插件。
- 成功和失败动作都有 trace/action 记录。
- Contract Guard warning/failed 能关联 trace。

### T38-4 UserBot 命令链路埋点

写入范围：

- `backend/app/worker/runtime.py`
- `backend/app/worker/command.py`
- `backend/app/worker/plugins/loader.py`

具体改动：

- 管理员命令进入时创建 trace。
- 命令解析、命中系统命令/插件命令、插件调用、返回动作、发送结果写 span。
- sudo / alias / plugin command 保留原行为。

禁止改动：

- 不重写命令系统。

测试：

- 扩展 `backend/app/tests/test_worker_command.py`。
- 扩展 `backend/app/tests/test_plugin_loader.py`。

验收：

- 日志页能按命令文本或 trace_id 看到完整命令链路。

### T38-5 日志 Trace API

写入范围：

- `backend/app/api/logs.py` 或 `backend/app/api/event_traces.py`
- `backend/app/schemas/logs.py` 或现有 schema 文件
- `backend/app/main.py`
- `backend/app/tests/test_logs_trace_api.py`

具体改动：

- 实现第 11.6 节 API。
- 列表接口默认不返回大 JSON；详情接口才返回 payload 摘要和 span/action。
- 支持按 chat_id/message_id/trace_id/plugin_key/status/event_type/keyword 搜索。

禁止改动：

- 不破坏 `/api/logs/runtime` 和 `/api/logs/audit`。

测试：

- API 鉴权、过滤、详情、空状态。

验收：

- 前端可以只靠新 API 画出日志中心。

### T38-6 前端日志中心重构

写入范围：

- `frontend/src/pages/Logs.tsx`
- `frontend/src/api/system.ts`
- `frontend/src/api/types.ts`
- 必要时新增 `frontend/src/components/logs/*`

具体改动：

- 重构为“总览 / 消息链路 / 插件诊断 / 命令链路 / 动作发送 / 原始日志”。
- 默认页展示系统健康、最近错误插件、最近失败动作、最近事件量。
- 消息链路详情使用时间线，不直接铺 JSON。
- JSON、payload、native_raw_meta 放高级折叠区。
- 原始日志 tab 继续调用旧 `/api/logs/runtime` / `/api/logs/audit`。

禁止改动：

- 不把新日志页做成旧日志表格换皮。
- 不在首屏展示大段 JSON。

测试：

- `cd frontend && ./node_modules/.bin/tsc -b --pretty false`
- `cd frontend && ./node_modules/.bin/vite build`
- 桌面和窄屏手动验收日志页。

验收：

- 用户打开日志页能按 trace/message/plugin/action 四种路径排查问题。

### T38-7 保留策略和设置

写入范围：

- `backend/app/api/rate_limit.py`
- `backend/app/worker/supervisor.py`
- `backend/app/services/event_trace.py`
- `frontend/src/pages/Settings/Index.tsx`
- `frontend/src/api/types.ts`

具体改动：

- 复用现有 `/api/system/settings` 的 `log_retention` 设置，新增 Trace 保留天数、payload_snapshot 保留天数、是否保存完整 native_raw、native_raw 保留天数。
- 定时清理过期 Trace 和 payload_snapshot。
- 默认不保存完整 native_raw。
- Trace 清理与现有 runtime_log 清理同属 supervisor 后台维护职责，但要独立开关和独立保留天数。
- `payload_snapshot` 到期时只清空大字段，不删除 `event_trace` 主记录，保证历史链路统计仍可用。

禁止改动：

- 不改变旧 runtime_log 默认保留策略。
- 不把 `native_raw` 完整内容默认写入数据库。

测试：

- 保留策略归一化测试。
- 清理任务测试。
- payload_snapshot 到期清空但 trace 主记录保留。

验收：

- 大量 Trace 不会无限增长。

### T38-8 文档和发布检查

写入范围：

- `CHANGELOG.md`
- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-CHEATSHEET.md`
- `docs/INTERACTION-BOT-OPTIMIZATION.md`
- `docs/TELEGRAM-FULL-EVENT-BUS-TRACE-PLAN.md`

具体改动：

- 写入 0.38.0 实际落地内容。
- 文档说明 Trace 日志页用法。
- 插件开发文档暂不宣称 Event Bus 已全量开放。

验收命令：

- `cd backend && .venv/bin/ruff check app`
- `cd backend && .venv/bin/pytest -q`
- `cd frontend && ./node_modules/.bin/tsc -b --pretty false`
- `cd frontend && ./node_modules/.bin/vite build`
- `git diff --check`

## 14. 0.39.0 施工任务卡：Event Bus、native_raw 与 Inline

### T39-1 Source Adapter 与标准事件生成

写入范围：

- `backend/app/services/event_bus.py`
- `backend/app/worker/plugins/events.py`
- `backend/app/services/account_bot_runtime.py`
- `backend/app/worker/runtime.py`

具体改动：

- 新增 Source Adapter，把 UserBot/Telethon、Bot API message、callback_query、inline_query、chosen_inline_result、payment_confirmed 转为同一标准事件信封。
- 保留现有 `event_from_interaction_payload(payload)`，但让它读取新标准信封。
- 所有新事件必须带 `trace_id`。

禁止改动：

- 不把 live Telethon event/client 直接放进 payload。

测试：

- `backend/app/tests/test_plugin_events.py`
- 新增 `backend/app/tests/test_event_bus_source_adapters.py`

验收：

- 同一插件能用同一字段读取 UserBot 消息、交互 Bot 消息和 Inline Query。

### T39-2 Event Bus 与订阅匹配

写入范围：

- `backend/app/services/event_bus.py`
- `backend/app/services/account_bot_service.py`
- `backend/app/services/remote_plugin_service.py`
- `backend/app/worker/plugins/loader.py`
- `backend/app/schemas/account_bot.py`

具体改动：

- 解析 manifest `event_subscriptions`。
- 按 account、source、event_type、scope、filters 先过滤，再调用插件。
- 每次匹配、跳过、投递都写 Trace span，跳过必须有 `reason_code`。
- 旧 `interaction_entries.events` 映射为 `event_subscriptions` 的兼容输入。

禁止改动：

- 不删除现有交互规则 UI，0.39 只让它降级为订阅条件来源。

测试：

- manifest 规范化测试。
- 订阅匹配 happy path / skip reason / disabled plugin。

验收：

- 插件没启动时，日志页能说明是未订阅、未启用、未安装、scope 不匹配还是过滤条件不匹配。

### T39-3 `native_raw` 能力

写入范围：

- `backend/app/services/event_bus.py`
- `backend/app/services/account_bot_service.py`
- `backend/app/services/remote_plugin_service.py`
- `backend/app/api/plugins.py`
- `frontend/src/pages/Plugins/*`
- `frontend/src/pages/Interaction/*` 如存在相关提示

具体改动：

- 插件声明 `capabilities.telegram_native_raw.enabled=true` 后才下发 `payload["native_raw"]`。
- WebUI 插件详情、安装/启用、规范警告处展示高风险提示。
- Trace 记录 `native_raw_meta`。
- 默认不在 `payload_snapshot` 中保存完整 `native_raw`。

禁止改动：

- 不把 token/session/API key 当 native_raw 的一部分下发。
- 不给未声明插件下发 native_raw。

测试：

- 声明插件能拿到 native_raw。
- 未声明插件拿不到 native_raw。
- payload_snapshot 不含 native_raw。
- native_raw_meta 包含 source/driver/object/size_bytes。

验收：

- 插件可以基于 Telegram 数字 ID 关系链做严格风控。

### T39-4 Inline Query 入口

写入范围：

- `backend/app/services/account_bot_runtime.py`
- `backend/app/services/account_bot_service.py`
- `backend/app/db/models/rate_limit.py`
- `backend/app/services/rate_limit_service.py`
- `frontend/src/api/types.ts`

具体改动：

- getUpdates / webhook `allowed_updates` 加入 `inline_query`、`chosen_inline_result`。
- `_extract_incoming` 或新的 Source Adapter 支持 Inline Query。
- Inline Query 事件没有 chat_id，必须按 inline scope 判断。
- rate limit 增加 `inline_query`。

禁止改动：

- 不把 Inline Query 当普通 message 伪造 chat_id。

测试：

- Bot API inline_query update 生成标准事件。
- chosen_inline_result update 生成标准事件。
- inline_all / owner_only / known_users scope 匹配。

验收：

- 发送 `@botname keyword` 能在 Trace 中看到 inline_query。

### T39-5 Delivery Executor 支持 `answer_inline_query`

写入范围：

- `backend/app/services/account_bot_service.py`
- `backend/app/services/interaction/delivery.py`
- `backend/app/services/interaction/contracts.py`
- `backend/app/worker/plugins/message_ops.py`
- `backend/app/tests/test_account_bot.py`

具体改动：

- 新增 `account_bot_service.answer_inline_query()` 调 Bot API `answerInlineQuery`。
- `BufferedMessageOps` 新增 `answer_inline_query()`。
- Contract Guard 支持 `answer_inline_query` 动作。
- Delivery Executor 记录 action 结果和错误。

禁止改动：

- 不让 userbot_reply 承接 Inline Answer。

测试：

- answer_inline_query happy path。
- 缺 inline_query_id 返回 failed action。
- Telegram API 失败写 event_action。

验收：

- 插件可以返回 Inline 结果，日志页能看到结果数量和 API 响应。

### T39-6 Event Bus 前端与交互中心提示

写入范围：

- `frontend/src/pages/Interaction/*`
- `frontend/src/pages/Plugins/*`
- `frontend/src/api/types.ts`

具体改动：

- 插件详情展示 event_subscriptions。
- 对 `telegram_native_raw` 和 `inline_all` 展示高风险提示。
- 交互中心规则详情中标出该规则已映射到哪些 Event Bus 订阅。

禁止改动：

- 不把风险提示做成阻断安装；按个人可信插件标准只提醒和留痕。

测试：

- 前端类型检查和构建。
- 手动检查长插件名、长订阅列表、窄屏。

验收：

- 管理员能看懂插件会收到哪些事件、是否请求原生数据、是否开放 Inline。

### T39-7 文档与示例插件

写入范围：

- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-REMOTE.md`
- `docs/PLUGIN-CHEATSHEET.md`
- `docs/PLUGIN-SAFETY.md`
- 示例插件或测试 fixture

具体改动：

- 新增 Event Bus 插件最小示例。
- 新增 native_raw 防改名诈骗风控示例。
- 新增 Inline Query 插件示例。
- 明确旧平铺字段不作为新插件主路径。

验收命令：

- `backend/.venv/bin/python scripts/validate-plugin-examples.py`
- `backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py`

### T39-8 废弃通道和旧字段收口

写入范围：

- `backend/app/worker/plugins/manifest.py`
- `backend/app/services/remote_plugin_service.py`
- `backend/app/services/interaction/contracts.py`
- `backend/app/services/interaction/delivery.py`
- `frontend/src/pages/Plugins/*`
- `frontend/src/pages/Interaction/*`
- `docs/PLUGIN-API-REFERENCE.md`

具体改动：

- manifest lint 对 `notice` / `bbot_notice` / `notice_bot` 输出迁移警告或阻止保存，提示改用 `interaction_bot`、`userbot_reply`、`auto`。
- Delivery Executor 遇到旧通道值时不尝试发送，直接记录 failed action 和 `send_channel_deprecated`。
- 插件 payload 不公开 `raw_event`；如存在兼容字段，只能是脱敏摘要，并在规范警告中提示改用 `native_raw` 声明。
- WebUI 规范警告将旧通道和旧字段标成红色高级警告。

禁止改动：

- 不把 `notice` 重命名成 `interaction_bot` 自动执行，避免误发到错误通道。
- 不给未声明 `telegram_native_raw` 的插件提供旧 `raw_event` 后门。

测试：

- 旧通道 manifest lint。
- 运行时旧通道 action failed。
- 未声明 native_raw 时 payload 不含 raw_event/native_raw。

验收：

- 旧插件必须看到明确迁移提示，新插件不会再从文档学到旧通道。

### T39-9 插件仓库字段贯通

写入范围：

- `backend/app/services/remote_plugin_service.py`
- `backend/app/services/feature_service.py`
- `backend/app/schemas/feature.py`
- `frontend/src/api/types.ts`
- 插件仓库刷新/更新相关 API 和页面

具体改动：

- 远程插件列表、私有 GitHub 仓库、`tree/<branch>` URL、仓库一键更新、已安装插件升级，都必须保留 `event_subscriptions`、`capabilities`、`usage`、`interaction_entries`。
- 仓库刷新后 WebUI 能立即看到新增/变更的 Event Bus 声明和风险提示。
- 仓库一键更新时，先展示将升级的插件、版本变化、风险能力变化；执行后写 audit/trace 或 runtime log。

禁止改动：

- 不把远程插件字段裁剪成旧 feature matrix 子集。

测试：

- 远程 manifest 解析保留新字段。
- feature matrix API 返回新字段。
- 前端类型覆盖新字段。

验收：

- 从远程插件库安装/升级的插件，与本地插件 manifest 在 Event Bus 字段上表现一致。

## 15. 0.40.0 施工任务卡：最终开放插件运行模型

### T40-1 旧规则模型收敛

写入范围：

- `backend/app/services/account_bot_runtime.py`
- `backend/app/services/account_bot_service.py`
- `frontend/src/pages/Interaction/*`
- `docs/INTERACTION-BOT-OPTIMIZATION.md`

具体改动：

- 旧交互规则仍可配置，但内部作为 Event Bus 订阅条件。
- 文案从“规则驱动插件”调整为“事件订阅 + 规则过滤”。
- 日志页显示旧规则 ID 和新 trace 之间的映射。

验收：

- 老规则配置路径仍可用。
- 新插件可完全不依赖旧规则，通过 event_subscriptions 工作。

### T40-2 插件运行入口统一

写入范围：

- `backend/app/worker/plugins/loader.py`
- `backend/app/worker/plugins/events.py`
- `backend/app/worker/plugins/message_ops.py`
- 官方/内置插件目录

具体改动：

- 插件统一在 `on_event` 或新版 `on_interaction` 中处理 message/command/callback/inline/payment。
- 保留一个清晰兼容层，但文档主推新入口。
- 官方插件迁移到新事件字段。

验收：

- 官方插件不再依赖旧平铺 payload 作为主路径。

### T40-3 最终日志页验收与 UI 打磨

写入范围：

- `frontend/src/pages/Logs.tsx`
- `frontend/src/components/logs/*`

具体改动：

- 日志页默认入口能直接回答第 1 节的问题。
- PWA/窄屏下底部导航、筛选、时间线、详情抽屉不重叠。
- 错误原因使用中文可读文案，同时保留 reason_code。

验收：

- 桌面、平板、窄屏截图验收。
- 真实或 fixture trace 展示完整链路。

### T40-4 开发指南最终版

写入范围：

- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-REMOTE.md`
- `docs/PLUGIN-CHEATSHEET.md`
- `README.md`
- `CHANGELOG.md`

具体改动：

- 插件开发指南全面切换到 Event Bus + Trace + MessageOps。
- 移除或降级旧系统描述，避免开发者照旧机制写新插件。
- 写清“如何排查插件为什么没启动”“如何查某条消息走到哪一步”。

验收：

- 开发者只看插件开发指南，就能写出 message/callback/inline/payment 四类插件。

### T40-5 发布检查

验收命令：

- `cd backend && .venv/bin/ruff check app`
- `cd backend && .venv/bin/pytest -q`
- `backend/.venv/bin/python scripts/validate-plugin-examples.py`
- `backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py`
- `cd frontend && ./node_modules/.bin/tsc -b --pretty false`
- `cd frontend && ./node_modules/.bin/vite build`
- `git diff --check`

发布要求：

- 按 SemVer 更新到对应版本。
- 四处版本号同步。
- 中文 CHANGELOG。
- 中文 commit / PR 文案。
- 不覆盖 main，继续推到新分支或当前 0.33+ 工作分支。

### T40-6 插件迁移最终验收

写入范围：

- 官方/内置/可选插件目录
- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-CHEATSHEET.md`
- 插件验证脚本

具体改动：

- 所有随 TelePilot 一起维护的插件 manifest 补齐 `usage`、`event_subscriptions`、`capabilities`。
- 交互型插件迁移到标准事件信封和 `ctx.messages`。
- 游戏/玩法类插件必须明确两类动作：普通互动走 `interaction_bot`，收款/发奖/结算走 userbot/settlement。
- 图片/AI/工具类插件必须明确是管理员命令调度还是玩家关键词调度。
- 插件示例必须覆盖 message、command、callback、inline、payment 五类事件中的至少四类；剩余类别必须在文档中说明如何扩展。

禁止改动：

- 不为了通过验证而把插件写成空声明。
- 不保留依赖旧平铺 payload 的官方主路径。

测试：

- `backend/.venv/bin/python scripts/validate-plugin-examples.py`
- `backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py`
- 定向插件单元测试或 dry-run 测试。

验收：

- 开发者照官方插件或示例插件写新插件，不需要再“缝补”旧规则、旧 notice、旧 raw_event。

### T40-7 最终版文档发布审计

写入范围：

- `README.md`
- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-REMOTE.md`
- `docs/PLUGIN-CHEATSHEET.md`
- `docs/PLUGIN-SAFETY.md`
- `docs/INTERACTION-BOT-OPTIMIZATION.md`
- `CHANGELOG.md`

具体改动：

- README 只保留面向用户的一键部署、插件仓库、交互中心、日志中心入口，不展开旧机制。
- 插件开发指南按“准备 manifest -> 声明事件订阅 -> 读取事件 -> 返回动作 -> 看日志排障 -> 发布远程插件”的顺序重写。
- `PLUGIN-SAFETY` 从“平台强沙箱”改为“个人可信插件风险提示 + 可审计能力边界”。
- `INTERACTION-BOT-OPTIMIZATION` 归档为历史设计或更新为最终框架说明，避免和新开发指南冲突。
- CHANGELOG 用中文把 0.32.0 之后已落地的重要改动分组写清楚，UI 小修简写，架构改动详细写。

文档审计命令：

- `rg -n "notice|bbot_notice|notice_bot|raw_event|平铺 payload|旧规则驱动|Contract Guard|Event Bus|native_raw|answer_inline_query" README.md docs`

验收：

- 搜索命中的旧概念只能出现在迁移说明、废弃说明或历史说明里。
- 新开发者只看插件开发指南，可以写出符合最终版框架的插件。

## 16. 多 Agent 并行分工建议

Wave 0：冻结契约和现状复核

- 主 Agent：确认当前分支、版本文件、未提交改动、已半落地 Trace 代码。
- 主 Agent：先修明显的语法/导入错误，跑最小静态检查，保证后续 Agent 不是在坏基线上并行。
- 只读 Reviewer：对照第 0.4 节确认本轮没有遗漏最终版不可缩水项。

Wave 0 通过门禁：

- `git status --short --branch` 已记录。
- 半落地代码的风险已列入执行清单，特别是 `native_raw` 默认下发问题。
- 没有未解释的语法错误阻塞并行。

Wave 1：0.38 数据和 Trace 底座

- Agent A：T38-1 + T38-2，负责模型、迁移、Trace Service。
- Agent B：T38-5，等 A 的 schema 稳定后做 API。
- Agent C：T38-6，先按 API mock 写 UI，后接真实 API。
- 主 Agent：T38-3 + T38-4 集成埋点，避免多人同时改 runtime 主链路。

Wave 1 通过门禁：

- 数据库迁移可从空库和现有库升级。
- 交互 Bot 现有 message/callback/payment 规则行为不回归。
- 新日志 API 能返回 trace、span、action、plugin status。
- 新日志 UI 可用，旧 runtime/audit 仍在原始日志入口。

Wave 2：0.39 Event Bus 与 Inline

- Agent A：T39-1 + T39-2，负责 Event Bus 和订阅匹配。
- Agent B：T39-4 + T39-5，负责 Inline 入口和 Delivery Executor。
- Agent C：T39-3 + T39-6，负责 native_raw 能力和 WebUI 风险提示。
- Agent D：T39-7 + T39-8，只写文档、示例、废弃通道/旧字段 lint，不改 runtime 主链路。
- Agent E：T39-9，负责远程插件仓库字段贯通、仓库刷新、一键更新相关字段保真。
- 主 Agent：合并冲突、补 Trace 串联、跑全量验证。

Wave 2 通过门禁：

- `event_subscriptions` 和 `capabilities` 已贯穿 manifest、远程插件解析、feature manifest、前端类型和 WebUI 提示。
- 未声明 `telegram_native_raw` 的插件拿不到 `native_raw`；声明插件可以拿到，并有 Trace 留痕。
- Inline Query 能被订阅插件处理并返回 `answer_inline_query`。
- 所有匹配、跳过、投递都有稳定 `reason_code`。
- 旧 `notice` / `bbot_notice` / `notice_bot` 不能被执行，只能给出迁移提示。
- 远程插件库安装和升级不会丢失 Event Bus 新字段。

Wave 3：0.40 收敛最终版

- Agent A：T40-1 + T40-2，负责旧规则收敛和官方插件迁移。
- Agent B：T40-3，负责日志页最终 UI 验收。
- Agent C：T40-4 + T40-7，负责开发指南最终版和文档发布审计。
- Agent D：T40-6，负责插件迁移最终验收和示例 dry-run。
- 主 Agent：T40-5 发布检查、版本号、CHANGELOG、最终 review。

Wave 3 通过门禁：

- 新插件开发文档不再把旧规则驱动和平铺 payload 写成主路径。
- 官方/可选插件示例已按新事件信封和 MessageOps 校准。
- 日志页能完成第 17 节人工验收。
- 版本号、中文 CHANGELOG、中文 commit/PR 文案准备完毕。
- 文档审计确认旧概念只出现在迁移/废弃/历史说明里。

并行禁区：

- `backend/app/services/account_bot_runtime.py`、`backend/app/worker/runtime.py`、`backend/app/worker/plugins/loader.py` 同一时间只能由主 Agent 或一个指定 Agent 写入。
- Alembic revision 只能由一个 Agent 创建。
- 版本号和 CHANGELOG 正式发布段只由主 Agent 在 release check 时写。
- 前端日志页组件可以拆，但 `frontend/src/pages/Logs.tsx` 的路由和数据流由一个 Agent 统一收口。

任何 Agent 发现以下情况必须停下来交给主 Agent：

- 需要修改同一个 runtime 主链路文件但当前不在自己的写入范围。
- 需要新增或重写数据库迁移。
- 需要改变 `send_via`、`event_subscriptions`、`native_raw` 这三个公共契约的语义。
- 测试失败显示现有插件行为回归。
- 需要部署、推送、bump 版本号或写正式 CHANGELOG 发布段。

### 16.1 Agent 交付格式

每个执行 Agent 的最终报告必须按以下格式交付，方便主 Agent 做最终版收口：

```text
任务卡：
状态：未开始 / 半落地 / 可测 / 已完成
改动文件：
公共契约变化：
未触碰禁区：
自动验证：
人工验收：
文档同步：
剩余风险：
需要主 Agent 复核：
```

状态只能由证据支撑：

- 只改了代码，没有测试：最多 `半落地`。
- 定向测试通过，但未跑相关集成/构建：最多 `可测`。
- 前端页面未做桌面和窄屏验收：前端相关任务最多 `可测`。
- 文档未同步：插件契约相关任务最多 `可测`。
- 有失败验证命令且未解释是否阻塞：不能标 `已完成`。

主 Agent 合并任何子任务前必须核验：

- diff 是否只在写入范围内。
- 是否引入了第二套事件入口、第二套发送通道或第二套插件 payload 主协议。
- 是否有未解释的测试失败。
- 是否破坏旧命令和旧交互规则回归底线。
- 是否需要补充 CHANGELOG `Unreleased`，但不得在非发布节点随手 bump 版本。

### 16.2 最终版收口顺序

主 Agent 收口时必须按以下顺序执行，不能先写发布材料再补实现：

1. 固定当前分支和工作树状态，确认没有未解释的外部改动。
2. 对照第 0.4 节，把最终版不可缩水清单逐项标为 `未开始`、`半落地`、`可测`、`已完成`。
3. 先修 `未开始` 和阻塞性的 `半落地`，再处理 UI 打磨和文档措辞。
4. 运行第 18.2 节部署前检查中的全部自动验证。
5. 完成第 17 节人工验收，至少覆盖普通消息、未触发消息、插件动作、Contract Guard、插件加载失败、inline、native_raw、旧 notice、远程插件安装/升级、窄屏日志页。
6. 通过文档审计后，才 bump 版本、整理中文 CHANGELOG、commit、push。
7. 部署到服务器前备份，部署后按第 18.3 节确认远端 commit、版本、迁移、健康检查和关键 Trace。

如果第 4-5 步任一项失败，版本号和 CHANGELOG 正式版本段不得提前落定；失败项必须回到对应任务卡修复。

## 17. 最终验收矩阵

### 17.0 能力闭环登记表

最终版验收时必须先填写这张表。任一能力没有达到 `已完成`，最终版不得发布。

| 能力 | 对应任务卡 | 必须证据 | 完成标准 |
| --- | --- | --- | --- |
| Trace 数据层 | T38-1、T38-2、T38-7 | 迁移、模型导入、Trace Service 测试、清理任务测试 | 新表可迁移，Trace 写入失败不阻断业务，保留策略生效 |
| 旧链路 Trace | T38-3、T38-4 | 交互 Bot、UserBot 命令、loader、Delivery、Contract Guard 定向测试 | 现有规则和命令行为不回归，成功/失败都能查 trace |
| 新日志页 | T38-5、T38-6、T40-3 | Trace API 测试、前端类型/构建、桌面/窄屏截图验收 | 日志页能查消息、插件、命令、动作和原始日志 |
| Event Bus 主路径 | T39-1、T39-2、T40-1 | Source Adapter 测试、订阅匹配测试、旧规则映射测试 | 所有 Telegram 来源先标准化，再匹配订阅并写 reason_code |
| `native_raw` 边界 | T39-3、T39-8 | 声明/未声明插件测试、payload_snapshot 测试、WebUI 风险提示 | 只有声明插件拿到 native_raw，日志默认不持久化完整原生数据 |
| Inline 闭环 | T39-4、T39-5 | inline_query/chosen_inline_result 标准化、answer_inline_query 成功/失败测试 | Inline 事件可订阅、可回应、可在日志页排障 |
| MessageOps / Delivery | T38-3、T39-5、T40-2 | action 执行测试、失败动作测试、实际通道记录 | 插件请求动作和平台实际执行都写 event_action |
| Contract Guard 新定位 | T38-3、T39-8 | warning/blocked/failed 测试、旧通道迁移提示 | 越声明调用有告警，不支持/废弃能力明确失败 |
| 远程插件字段贯通 | T39-9 | 远程解析、安装、升级、feature matrix、前端类型测试 | `usage`、`event_subscriptions`、`capabilities` 任一层不丢失 |
| 交互中心和插件 UI | T39-6、T40-1、T40-3 | 风险提示、订阅展示、旧规则映射、窄屏验收 | 管理员能看懂插件收什么事件、用什么能力、由谁发送 |
| 官方/示例插件迁移 | T40-2、T40-6 | 验证脚本、dry-run、定向插件测试 | 示例覆盖 message、command、callback、inline、payment 的核心写法 |
| 开发指南最终版 | T39-7、T40-4、T40-7 | 文档 grep、示例验证脚本、README/CHANGELOG 审计 | 新开发者不读旧机制也能写出可运行插件 |
| 发布与部署 | T40-5、18.2、18.3 | 版本四处同步、中文 CHANGELOG、push、远端健康检查 | 服务器运行新版本，Trace API 和日志页可用，有回滚点 |

后端自动验证：

- Trace 表迁移成功。
- Trace Service 单元测试通过。
- 交互 Bot message/callback/payment 产生 trace。
- UserBot command 产生 trace。
- Event Bus 订阅匹配和跳过 reason_code 正确。
- `native_raw` 只给声明插件。
- `payload_snapshot` 默认不含完整 `native_raw`。
- `inline_query` / `chosen_inline_result` 可标准化。
- `answer_inline_query` 成功和失败都记录 event_action。
- Contract Guard warning/failed 都能关联 trace。
- 旧 `notice` / `bbot_notice` / `notice_bot` 不能执行，且返回 `send_channel_deprecated`。
- 插件读取旧 `raw_event` 不能绕过 `telegram_native_raw` 声明。
- 远程插件仓库解析、安装、升级保留 `event_subscriptions`、`capabilities`、`usage`。
- Trace 清理任务能清理过期 payload/native_raw，同时保留主记录。

前端自动验证：

- 日志 API 类型完整。
- 日志页构建通过。
- 空状态、加载态、错误态可用。
- 插件详情、规范警告、交互中心能展示 event subscriptions、native_raw 风险、inline_all 风险、废弃通道警告。
- 插件仓库刷新/一键更新后，新字段和风险提示实时更新。

人工验收：

- 在群里发一条普通消息，日志页能查到完整链路。
- 触发一个不会启动插件的消息，日志页能显示未启动原因。
- 启动一个插件并让它返回 `send_message`，动作发送页能看到实际通道和 Telegram message_id。
- 触发一个 Contract Guard warning，日志页能定位到插件和 action。
- 让插件加载失败，插件诊断页能显示失败原因和最近 trace。
- 发送 `@botname 关键词`，日志页能看到 inline_query 和 answer_inline_query。
- 声明 `telegram_native_raw` 的测试插件能读取 native_raw；未声明插件读取不到。
- 窄屏/PWA 打开日志页，筛选、时间线、详情区域不重叠。
- 用一个旧 `notice` send_via 测试插件触发动作，页面能看到明确迁移错误，不会误发消息。
- 从远程插件库安装或升级一个声明 Event Bus 的插件后，插件详情页能看到完整订阅和能力提示。
- 只看新版插件开发指南，新建一个最小 message/callback 插件并通过验证脚本。

回滚验收：

- 关闭 Trace 写入后，旧 `runtime_log` / `audit_log` 仍可用。
- Event Bus 新链路异常时，可临时回退到旧规则驱动路径。
- Inline 支持异常时，不影响普通 message/callback 处理。
- 关闭 `native_raw_persist_enabled` 后，不影响已声明插件运行，只是不再持久化完整原生数据。

## 18. 发布、部署与回滚要求

### 18.1 发布要求

最终版准备推送时必须：

- 按 SemVer 选择版本；完整最终版建议为 `0.40.0 minor（次版本）`。
- 同步修改 `backend/app/__init__.py`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts`。
- `CHANGELOG.md` 使用中文写入正式版本段，只记录实际落地内容。
- commit、PR、部署说明使用中文。
- 不覆盖 main，继续推送到当前 0.33+ 工作分支或新建 `codex/0.40-event-bus-trace-final` 分支。

### 18.2 部署前检查

部署到服务器前必须完成：

- `git diff --check`
- `cd backend && .venv/bin/ruff check app`
- `cd backend && .venv/bin/pytest -q`
- `backend/.venv/bin/python scripts/validate-plugin-examples.py`
- `backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py`
- `cd frontend && ./node_modules/.bin/tsc -b --pretty false`
- `cd frontend && ./node_modules/.bin/vite build`

如果某条命令因环境失败，必须写清原因、替代验证和剩余风险；不能把环境失败当通过。

### 18.3 服务器部署验收

部署到 `144.24.5.159` 后必须确认：

- 远端当前 commit 与本地推送 commit 一致。
- Docker 服务启动成功，数据库迁移已执行。
- Web 首页显示新版本号。
- `/api/logs/trace/overview` 可返回数据或空状态。
- 日志页可打开，原始日志 tab 仍可用。
- 至少触发一次普通消息 trace、一次插件调用 trace、一次 action trace。
- 如果有可测交互 Bot，触发一次 callback 或 Inline Query 并确认 Trace。
- `docker compose logs --tail=100 web` 没有迁移、导入、API 路由或前端资源错误。

### 18.4 回滚要求

回滚必须优先保护数据：

- 部署前备份 `.env`、compose 文件和数据库，记录备份路径。
- 代码回滚到上一稳定 commit 后，旧 `runtime_log` / `audit_log` 仍可排障。
- 新 Trace 表可以保留不用，不在紧急回滚中删表。
- 如果 Event Bus 新链路出问题，先通过配置关闭新投递或回退旧规则路径，再考虑代码回滚。
- 如果 Inline Query 出问题，先移除 `allowed_updates` 中的 inline 事件或停用相关订阅，不影响普通消息和 callback。

## 19. 最终版执行总控

本节用于把前面的架构、任务卡和验收矩阵收束为一套可以直接执行的总控流程。执行者不能跳过本节直接按单个任务卡挑着做；否则很容易出现“某个局部功能可用，但最终版链路仍然断开”的半落地状态。

### 19.1 最终版前置锁定

开始实现前必须完成以下锁定：

- **契约锁定**：第 11 节中的标准事件信封、manifest 字段、MessageOps/action、Event Bus Service、Trace Service、日志 API 不再随意改名。确需改名时，必须同时改代码、前端类型、测试 fixture、插件示例和开发文档。
- **入口锁定**：所有 Telegram 来源只能经 Source Adapter 进入 `TelePilotEvent`。新代码不得新增绕过 Event Bus 的插件调用入口。
- **出口锁定**：所有插件动作只能经 MessageOps/action 到 Delivery Executor。新代码不得让插件直接拿 Bot token、UserBot client、Telegram driver 或 live event。
- **风险边界锁定**：平台按个人可信插件标准放宽业务自由度，但不放宽凭据、live client、旧 notice 通道、普通 Bot 转账这四条客观边界。
- **版本锁定**：最终版未通过第 17 节和第 19.9 节之前，不提前 bump 到正式版本，不提前写正式 CHANGELOG 版本段。

### 19.2 五条真实链路必须闭环

最终版不是“有 Event Bus 类”和“有日志页”就算完成。以下五条真实链路必须全部闭环，每条链路都要能在日志页查到 trace、span、action 和失败原因。

1. **普通群消息链路**
   - 输入：UserBot 或交互 Bot 收到群消息。
   - 必经：Source Adapter -> `TelePilotEvent` -> Trace -> Event Bus matcher -> 插件投递或 skipped reason。
   - 产出：日志页能看到消息标准化、订阅匹配、插件跳过/执行、动作发送。

2. **管理员命令链路**
   - 输入：账号主人或授权管理员发送带命令前缀的命令。
   - 必经：UserBot command parser -> Source Adapter -> Trace -> Event Bus matcher 或旧命令兼容映射 -> 插件/系统处理器。
   - 产出：日志页能看到命令解析、权限、命中处理器、插件调用、消息操作。

3. **按钮回调链路**
   - 输入：交互 Bot 收到 `callback_query`。
   - 必经：Source Adapter -> Trace -> session/rule/subscription match -> 插件执行 -> `answer_callback` 或后续消息动作。
   - 产出：日志页能看到 callback data、会话命中、插件入口、按钮 ACK 成败。

4. **Inline 链路**
   - 输入：交互 Bot 收到 `inline_query` 或 `chosen_inline_result`。
   - 必经：Source Adapter -> Trace -> inline scope -> 插件执行 -> `answer_inline_query`。
   - 产出：日志页能看到 query、scope、结果数量、Telegram API 成败和选择结果。

5. **转账/付款确认链路**
   - 输入：UserBot 监听到第三方转账通知 Bot 的群消息，或平台解析到付款确认。
   - 必经：Source Adapter -> `source_actor.type=external_bot` 或 `payment_confirmed` -> Trace -> 插件投递 -> settlement/userbot 动作。
   - 产出：日志页能看到外部通知来源、付款人/玩家归属、插件处理、发奖/结算动作；普通 Bot 不得执行转账。

任一链路只能做到“收到消息”或“插件能运行”，但缺 Trace、reason_code、action 记录、失败展示中的任一项，状态都只能记为 `半落地` 或 `可测`，不能记为 `已完成`。

### 19.3 验收夹具和测试插件包

最终版必须准备一组稳定 fixture 和测试插件，用来防止后续继续靠线上手测猜问题。

固定 fixture 覆盖项如下。文件名可以按现有测试目录调整，但最终证据台账必须逐项映射到这些事件类型，不能只写“已有相关测试”：

- `backend/app/tests/fixtures/event_bus/userbot_message.json`
- `backend/app/tests/fixtures/event_bus/interaction_bot_message.json`
- `backend/app/tests/fixtures/event_bus/callback_query.json`
- `backend/app/tests/fixtures/event_bus/inline_query.json`
- `backend/app/tests/fixtures/event_bus/chosen_inline_result.json`
- `backend/app/tests/fixtures/event_bus/external_payment_notice.json`
- `backend/app/tests/fixtures/event_bus/native_raw_telethon_message.json`
- `backend/app/tests/fixtures/event_bus/deprecated_notice_action.json`

固定测试插件或示例插件覆盖项如下。插件名可以按实际目录调整，但最终证据台账必须证明每类行为都被可运行示例或自动化测试覆盖：

- `event_echo`：订阅 message/callback，回显标准字段，验证标准事件信封和 `ctx.messages.send_text`。
- `event_inline_demo`：订阅 inline_query/chosen_inline_result，返回 `answer_inline_query`。
- `event_payment_guard`：订阅 payment_confirmed/external_payment_notice，验证付款人、玩家、reply_to 和 settlement/userbot 动作。
- `event_native_raw_audit`：声明 `telegram_native_raw`，读取 `native_raw` 并记录 `native_raw_meta`。
- `event_deprecated_notice_probe`：故意返回旧 `notice`/`bbot_notice` 通道，验证 `send_channel_deprecated` 和不会误发。

这些插件不一定都作为用户可见内置插件发布，但必须能被验证脚本或测试用例调用。最终版文档中的示例代码应优先从这组可运行示例中抽取，避免文档和真实运行时漂移。

### 19.4 代码落地的唯一顺序

最终版必须按以下顺序收口。可以并行做局部任务，但合并和验收顺序不能反过来。

1. **Trace 数据层和服务稳定**
   - 先保证迁移、模型、Trace Service、清理策略稳定。
   - 任何业务链路接 Trace 前，Trace 写入失败必须已被证明不会阻断主流程。

2. **现有链路先埋点，不改变行为**
   - 交互 Bot、UserBot 命令、loader、Contract Guard、Delivery Executor 先全部记录 trace/span/action。
   - 这一阶段旧规则语义不变，作为回归底线。

3. **manifest 新字段端到端贯通**
   - `usage`、`event_subscriptions`、`capabilities` 必须贯穿 loader、远程仓库、安装记录、feature matrix、前端类型、插件 UI、规范警告。
   - 字段未端到端贯通前，不开始大规模迁移插件。

4. **Source Adapter 和 Event Bus 成为主路径**
   - UserBot、交互 Bot、callback、inline、payment 都标准化为 `TelePilotEvent`。
   - 旧规则只作为订阅条件来源，不再作为第二套插件调度真相。

5. **MessageOps/action 和 Contract Guard 收口**
   - 所有发送、编辑、删除、置顶、callback ACK、inline answer、settlement 都写 `event_action`。
   - 越声明调用放行但告警；不支持/废弃能力明确失败。

6. **日志页按 Trace 重构**
   - UI 使用真实 Trace API 和 fixture 验收，不再围绕旧文本日志组织默认入口。
   - 错误态必须暴露 API 错误，不能静默伪装为空状态。

7. **插件和文档最终迁移**
   - 维护内插件、示例插件、开发指南、远程插件说明统一切到 Event Bus + MessageOps。
   - 旧机制只作为迁移说明，不作为推荐路径。

8. **发布和部署**
   - 全部自动验证、人工验收、文档审计通过后，才 bump 版本、写中文 CHANGELOG、commit、push、部署。

### 19.5 当前分支必须关闭的已知阻塞项

执行最终版时，以下项必须被逐一关闭。它们不是“后续优化”，而是最终版门禁的一部分：

- 插件开发文档仍推荐旧 `interaction_entries`、旧规则、旧平铺 payload 或旧 `event.reply/respond` 作为主路径时，必须重写。
- `PLUGIN-SAFETY` 仍把平台描述成强沙箱主导时，必须改为“个人可信插件风险提示 + 可审计能力边界”。
- 官方/示例/维护插件 manifest 缺 `usage`、`event_subscriptions`、`capabilities` 时，不能通过最终版插件验收。
- 日志页如果 Trace API 出错却显示空状态，必须修复为显式错误态。
- 日志总览如果看不到 DB、Redis、Worker、账号/Bot 状态，不能作为最终日志中心。
- 插件中心、远程仓库、交互中心如果不展示 `event_subscriptions`、`telegram_native_raw`、`inline_all`、废弃通道风险，不能通过最终 UI 验收。
- 远程仓库一键更新如果不能在更新前展示版本变化和风险能力变化，不能称为最终版插件仓库体验。
- 交互中心如果只能展示旧规则列表，而看不到规则映射到哪些 Event Bus 订阅，不能通过最终交互中心验收。
- `event_bus_delivery_enabled`、`inline_updates_enabled`、Trace 保留策略如果只是设置字段但未接入运行时，不得记为完成。
- `raw_event` 如果仍可让插件绕过 `telegram_native_raw` 声明拿到原生对象，必须移除或改成迁移警告。

### 19.6 Review 必跑审计命令

最终 review 必须执行以下审计。命中结果不能简单删除；每条命中都要归类为“合法迁移说明、合法历史说明、测试故意覆盖、需要修复”。

旧机制审计：

```bash
rg -n "notice|bbot_notice|notice_bot|raw_event|平铺 payload|旧规则驱动|event\\.reply|event\\.respond|ctx\\.client\\.send_message" README.md docs backend frontend examples scripts
```

新契约覆盖审计：

```bash
rg -n "event_subscriptions|capabilities|telegram_native_raw|native_raw_meta|answer_inline_query|chosen_inline_result|settlement|send_channel_deprecated" README.md docs backend frontend examples scripts
```

插件 payload 主路径审计：

```bash
rg -n "payload\\[\"text\"\\]|payload\\.get\\(\"text\"\\)|payload\\[\"chat_id\"\\]|payload\\.get\\(\"chat_id\"\\)" docs backend examples
```

发送通道审计：

```bash
rg -n "send_via|channel_selector|interaction_bot|userbot_reply|auto|notice" backend frontend docs examples
```

Trace 覆盖审计：

```bash
rg -n "start_trace|record_span|record_action|finish_trace|trace_id|reason_code" backend/app
```

审计通过标准：

- 旧机制命中只能出现在迁移、废弃、历史或故意回归测试里。
- 新契约命中必须覆盖后端 schema、服务、前端类型、UI、文档、验证脚本。
- 插件 payload 示例必须优先读取标准事件信封字段，例如 `payload["message"]["text"]`、`payload["chat"]["id"]`。
- `notice` 只允许作为废弃值、测试值或迁移提示出现，不允许作为可执行通道出现。

### 19.7 最终版开发者验收剧本

最终版必须让开发者按以下顺序完成一个新插件，而不是靠多次试错缝补：

1. 阅读插件开发指南，理解 TelePilot 是个人可信插件系统，平台提供事件、消息操作、Trace 和风险提示。
2. 新建 `plugin.json`，填写 `key`、`name`、`version`、`usage`、`event_subscriptions`、`capabilities`。
3. 写一个 `on_event` 或新版主入口，只读取标准事件信封字段。
4. 用 `ctx.messages.send_text` 或 action 返回普通互动消息。
5. 需要按钮时使用 `answer_callback`，需要 Inline 时使用 `answer_inline_query`。
6. 需要严格风控时声明 `telegram_native_raw`，并在代码里处理 `native_raw_meta.enabled=false` 的降级情况。
7. 需要付款/发奖时返回 settlement/userbot 动作，不让 `interaction_bot` 承担转账。
8. 运行验证脚本，通过 manifest lint 和示例 dry-run。
9. 在 WebUI 安装/启用插件，能看到使用说明、订阅、能力、风险提示。
10. 触发插件后在日志页按 trace、message、plugin、action 任一路径排障。

只要开发者仍需要先理解旧交互规则、旧 notice 通道、旧平铺 payload、旧 runtime log 才能写出可用插件，就说明最终版没有达标。

### 19.8 最终版用户验收剧本

给账号主人验收时，必须按真实使用场景跑完：

1. 打开系统设置，确认 Trace 保留、payload snapshot、native_raw 持久化默认值合理。
2. 打开插件中心，确认每个插件都有使用说明、订阅事件、能力声明和风险提示。
3. 刷新远程插件仓库，确认新字段不丢失；执行一键更新前看到版本变化和风险变化。
4. 打开交互中心，选择账号和交互 Bot，确认规则列表、规则详情、Event Bus 映射、最近错误可见。
5. 在允许群发一条普通消息，日志页能按 chat_id/message_id 查到 trace。
6. 发一条不会触发插件的消息，日志页能看到 skipped reason。
7. 用管理员命令启动插件，日志页能看到 command 链路和 userbot_reply 动作。
8. 用玩家关键词启动插件，日志页能看到 interaction_bot 动作。
9. 点击按钮，日志页能看到 callback_query 和 answer_callback。
10. 发送 Inline Query，日志页能看到 inline_query 和 answer_inline_query。
11. 触发转账通知解析，日志页能看到 external payment notice、付款归属和 settlement/userbot 动作。
12. 故意触发一个失败动作，日志页能看到失败原因、reason_code 和中文说明。

这些场景全部通过后，才可以向用户称为“最终版框架落地”。

### 19.9 Go / No-Go 清单

发布前必须逐项回答 `是`。任一项为 `否`，不得发布最终版。

| 检查项 | Go 标准 |
| --- | --- |
| 数据迁移 | 空库和旧库都能升级，回滚不需要删 Trace 表 |
| Trace | 五条真实链路都有 trace/span/action/reason_code |
| Event Bus | 所有 Telegram 来源先标准化，再通过订阅匹配 |
| 插件协议 | 新插件主路径只读标准事件信封，不依赖旧平铺 payload |
| MessageOps | 插件所有动作经 Delivery Executor，成功/失败都记录 |
| Contract Guard | 越声明调用告警，不支持/废弃能力失败 |
| native_raw | 只有声明插件拿到，默认不持久化完整原生数据 |
| Inline | inline_query/chosen_inline_result/answer_inline_query 可测可查 |
| notice 收口 | 旧 notice/bbot_notice 不执行，只给迁移错误 |
| 远程插件 | 安装、刷新、升级、一键更新都不丢失新字段 |
| 日志页 | 默认入口能查消息、插件、命令、动作、原始日志 |
| 交互中心 | 能配置规则，也能看规则映射到 Event Bus 的结果 |
| 插件 UI | 使用说明缺失、native_raw、inline_all、废弃通道都有显式提示 |
| 文档 | 新指南能直接开发 message/callback/inline/payment 插件 |
| 自动验证 | 第 18.2 节命令全部通过，或环境失败有可接受替代证据 |
| 人工验收 | 第 19.8 节场景全部跑完 |
| 发布材料 | 四处版本号同步，中文 CHANGELOG，中文 commit/PR |
| 部署 | 远端 commit、版本、迁移、健康检查、关键 trace 均确认 |

### 19.10 为什么按本计划可以到“最终版”

按本计划执行后，TelePilot 的插件系统会从“旧规则 + 局部交互 Bot + 难排查文本日志”收敛成一套稳定框架：

- 插件不再需要关心消息来自 UserBot、交互 Bot、按钮、Inline 还是付款通知，统一读取事件信封。
- 插件可以自由选择普通互动由交互 Bot 发，管理/结算/转账由 userbot 或 settlement 发，但平台记录实际通道和失败原因。
- 平台不再用强沙箱假装替个人用户承担插件风险，而是把风险声明、越声明调用、高风险能力和失败动作可视化。
- 日志页不再只是文本流，而是围绕 trace 回答“消息到哪了、插件为什么没启动、动作为什么失败”。
- 文档和示例不再教旧机制，新开发者可以直接按 Event Bus + MessageOps + Trace 写插件。

因此，“最终版”的核心不是封装 Telegram 所有能力，而是完成统一事件入口、统一插件协议、统一消息操作出口、统一排障视角和统一开发文档这五个闭环。

### 19.11 当前分支审查补充 blocker 对照表

后端只读审查确认：当前分支已经具备 Trace、Event Bus、native_raw、Inline、远程字段贯通的一部分可测实现，但仍不能称为最终版。以下问题必须在执行中逐项关闭，不能被归类为 UI 小修或后续优化。

| blocker | 当前风险 | 必须关闭到什么程度 | 对应任务卡 |
| --- | --- | --- | --- |
| Source Adapter 未成为运行时唯一入口 | `normalize_bot_update` / `normalize_userbot_event` / `normalize_payment_notice` 仍像 helper，实际运行时还有 `_extract_incoming()`、`_incoming_trace_payload()` 等旧入口 | UserBot、交互 Bot、callback、inline、payment 都必须先生成 `TelePilotEvent`，旧 helper 只能作为兼容适配层 | T39-1、T39-2、T40-1 |
| UserBot command 没有 Trace/Event Bus decision | 管理员命令仍直接执行 builtin/template/plugin handler，日志页无法回答命令调用了什么 | 命令解析、权限、命中处理器、插件调用、消息动作都要写 trace/span/action/reason_code | T38-4、T39-2 |
| 管理 Account Bot 入口缺 Trace，allowed_updates 缺 inline | 管理 Bot 的 message/callback 入口和交互 Bot 的入口不一致，inline 能力覆盖不完整 | 管理入口也要有 receive/normalize/route trace；需要 inline 时 allowed_updates 与交互 Bot 口径一致 | T38-3、T39-4 |
| 外部转账通知和旧 rule/session fallback 绕过 Event Bus | `_try_handle_transfer_notice()`、旧 rule/session/math fallback 仍可能在 Event Bus 前截走事件 | 外部付款通知和旧规则都要转成 Event Bus decision；旧规则只能是订阅条件来源 | T39-1、T39-2、T40-1 |
| 回滚开关未完整接入运行时 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled` 若只是设置字段，部署回滚不可控 | 开关必须实际控制 Trace 写入、新投递路径和 inline updates；关闭后旧路径仍可用 | T38-7、T39-4、T40-5 |
| 并非所有 action 都写 `event_action` | 空文本、非法 media 等直接 return 时没有失败 action，日志页会断链 | 插件返回的每个 action 都要记录成功、失败或跳过；失败不得静默 | T38-3、T39-5 |
| 插件加载失败未进入 `PluginRuntimeStatus` | loader startup 失败只写旧状态和 runtime log，新日志插件诊断页读不到 | 插件加载、热更新、启动失败必须更新 `PluginRuntimeStatus` 并关联最近 trace 或 reason_code | T38-3、T38-5、T40-2 |
| `native_raw_persist_enabled` 未实际持久化完整 raw | 设置存在但 `_native_raw_meta.stored_in_trace` 始终为 false，设置语义不完整 | 默认不持久化；开启后按短保留期保存、脱敏/大小记录、清理任务可验证 | T38-7、T39-3 |
| Event Bus 字段枚举漂移 | lint 允许的事件值与 Event Bus `VALID_EVENT_TYPES` 不一致时，插件可通过规范检查却永远不匹配 | manifest lint、Event Bus matcher、文档、前端提示必须共用同一事件类型枚举 | T39-2、T39-7、T40-4 |
| Trace 写入失败不可见 | Trace best-effort 失败只 debug，线上会表现为日志缺失但无排障入口 | Trace 服务故障要写旧 runtime system/error，同时不阻断插件主流程 | T38-2、T38-7 |

针对这些 blocker，最终版执行必须新增或补齐以下验证：

- UserBot command creates trace。
- 管理 Bot `_handle_update` creates trace。
- `event_bus_delivery_enabled=false` 时新投递路径停用，系统进入 `degraded_event_bus_disabled` 降级兼容状态；旧规则可作为回滚兜底处理已有交互，但该状态不能用于签收入口唯一。
- `inline_updates_enabled=false` 时 inline updates 不进入处理，但 message/callback 不受影响。
- 空文本、非法 media、缺 inline_query_id 等动作都会产生 failed `event_action`。
- 插件启动/加载失败会更新 `PluginRuntimeStatus`，日志页插件诊断可见。
- manifest lint 允许的事件类型与 Event Bus matcher 的事件类型完全一致。
- Trace 写库失败会落旧 runtime system/error。

### 19.12 最终版证据台账

最终版发布前必须新增或更新当前发布版本对应的证据台账，例如 `docs/release/0.40.0-final-evidence.md` 或 `docs/release/0.40.1-final-evidence.md`。这不是普通总结文档，而是 Go / No-Go 的证据台账；没有这份台账，不能称为最终版完成。

证据台账必须包含：

| 区块 | 必填内容 | 不合格表现 |
| --- | --- | --- |
| 分支和版本 | 本地分支、远端分支、commit、四处版本号、CHANGELOG 版本段 | 只写“已更新版本” |
| 变更范围 | 本次实际改动文件分组、公共契约变化、兼容层位置 | 按计划愿景写，和 diff 不对应 |
| 五条链路 | 普通消息、管理员命令、callback、inline、payment 的 trace/span/action 证据 | 只说“测试通过”，没有 trace 路径 |
| blocker 关闭 | 第 19.11 节每个 blocker 的关闭方式、测试名和剩余风险 | 把 blocker 降级成后续优化 |
| 自动验证 | 第 18.2 节每条命令、退出码、失败原因或替代证据 | 环境失败但写成通过 |
| 人工验收 | 第 19.8 节每个场景的页面 URL、账号/插件、观察结果 | 只验收桌面，不验收窄屏/PWA |
| 文档审计 | 第 19.6 节 grep 结果分类：合法迁移、历史说明、测试覆盖、已修复 | 删除命中但没有解释 |
| 部署证据 | 远端 commit、版本号、迁移、健康检查、关键 trace、docker 日志 | 只写“已部署” |
| 回滚证据 | 备份路径、关闭开关演练结果、旧日志可用性 | 没有备份或没有验证旧路径 |

台账中的每条“通过”都必须能回到具体命令、测试、页面或 trace。若某项只能人工观察，必须写清观察入口和通过标准。若某项暂时无法验证，最终版状态只能是 `可测`，不能发布。

### 19.13 稳定 reason_code 与状态字典

日志页要能排障，必须先让后端输出稳定 reason_code。最终版禁止用临时英文句子当唯一失败原因；中文说明可以改，reason_code 不能随意改名。

事件和投递状态第一版固定为：

| 状态 | 含义 | 日志页展示要求 |
| --- | --- | --- |
| `received` | 已收到 Telegram update 或 UserBot event | 展示来源、账号、update/message/callback/inline ID |
| `normalized` | 已转为 `TelePilotEvent` | 展示事件类型和标准字段摘要 |
| `matched` | 至少一个插件订阅命中 | 展示插件、entry、dispatch_mode |
| `skipped` | 候选插件跳过 | 展示 reason_code 和可读原因 |
| `delivered` | 已投递给插件运行时 | 展示插件入口和耗时 |
| `plugin_succeeded` | 插件运行成功 | 展示返回 action 数量 |
| `plugin_failed` | 插件运行失败 | 展示异常摘要和插件 runtime 状态 |
| `action_succeeded` | 动作执行成功 | 展示请求通道、实际通道、Telegram 返回 ID |
| `action_failed` | 动作执行失败 | 展示失败原因、是否可重试 |
| `trace_degraded` | Trace 写入降级 | 展示旧 runtime_log 的 fallback 记录 |

reason_code 第一版必须至少覆盖：

| reason_code | 类别 | 触发场景 |
| --- | --- | --- |
| `account_not_matched` | 订阅匹配 | 事件账号与插件/规则账号不一致 |
| `account_bot_user_unauthorized` | 权限 | 账号 Bot 收到未授权用户消息 |
| `action_failed` | 动作 | 插件动作执行失败的通用状态 |
| `plugin_not_installed` | 插件状态 | 插件不存在或安装记录缺失 |
| `plugin_disabled` | 插件状态 | 插件未启用 |
| `manifest_invalid` | 插件状态 | manifest 解析失败或缺必要字段 |
| `plugin_load_failed` | 插件状态 | loader 加载失败 |
| `matched` | 订阅匹配 | 订阅命中并准备投递 |
| `event_type_not_subscribed` | 订阅匹配 | 插件未订阅该事件类型 |
| `source_not_subscribed` | 订阅匹配 | 插件未订阅该来源 |
| `scope_not_matched` | 订阅匹配 | 允许会话、owner_only、known_users、inline_all 不匹配 |
| `filter_not_matched` | 订阅匹配 | 关键词、命令、callback data、金额等不匹配 |
| `session_not_found` | 会话 | 需要已有会话但未找到 |
| `session_expired` | 会话 | 会话过期或已结束 |
| `rate_limited` | 频控 | 命中账号、插件、用户或 inline 频控 |
| `callback_query` | 路由 | 收到并路由按钮回调 |
| `command_matched` | 命令 | 管理员命令已命中处理器 |
| `command_not_matched` | 命令 | 普通文本未命中命令处理 |
| `command_unauthorized` | 权限 | 非管理员触发管理员命令 |
| `contract_warning` | 契约 | 插件越声明调用但可审计放行 |
| `contract_failed` | 契约 | 插件请求客观不可执行能力 |
| `entry_key_missing` | 订阅匹配 | 订阅缺少可投递插件入口 |
| `event_bus_delivery_disabled` | 回滚 | 运维开关关闭 Event Bus 新投递路径 |
| `handler_error` | 运行时 | 系统 handler 捕获异常 |
| `inline_disabled` | Inline | 系统或账号关闭 inline updates |
| `inline_query_answer_failed` | Inline | answerInlineQuery 执行失败 |
| `native_raw_not_allowed` | 能力 | 插件未声明 `telegram_native_raw` |
| `native_raw_skipped` | 能力 | 已声明但来源、大小或设置导致未下发 |
| `permission_denied` | 权限 | 当前操作者权限不足 |
| `send_channel_deprecated` | 动作 | 请求 `notice` / `bbot_notice` / `notice_bot` |
| `bot_not_configured` | 动作 | 需要交互 Bot 但未配置 token 或未启用 |
| `bot_self_message` | 路由 | 交互 Bot 自身消息被忽略 |
| `bot_token_missing` | 动作 | 需要 Bot API 但缺少 token |
| `userbot_offline` | 动作 | 需要 userbot 但账号离线 |
| `settlement_requires_userbot` | 动作 | 普通 Bot 请求转账/发奖能力 |
| `subscription_load_failed` | 订阅匹配 | 加载插件订阅失败并回退旧链路 |
| `subscription_not_matched` | 订阅匹配 | 没有订阅命中当前事件 |
| `telegram_api_error` | 动作 | Telegram API 返回失败 |
| `plugin_runtime_error` | 插件运行 | 插件执行抛错 |
| `trace_write_failed` | 日志 | Trace 写库失败，已 fallback 到 runtime_log |
| `unsupported_send_via` | 动作 | 请求未知或不支持的发送通道 |

新增 reason_code 必须同时更新：后端常量/测试、前端中文映射、开发文档排障表、最终证据台账。否则视为日志契约漂移。

### 19.14 全量消息开放的真实边界

“传递所有消息给插件”在最终版里的含义必须精确定义，避免重新落回强沙箱或完全无边界两端：

| 层级 | 默认是否给插件 | 内容 | 边界 |
| --- | --- | --- | --- |
| 标准事件信封 | 给订阅命中的插件 | `source`、`message`、`chat`、`sender`、`actor`、`reply_to`、`payment`、`inline_query`、`session`、`trigger` | 不含凭据、不含 live client、不含 Bot token |
| `raw_summary` | 默认给 | 脱敏摘要、实体摘要、原始来源类型、大小 | 只用于排障，不作为推荐业务主协议 |
| `native_raw_meta` | 默认给 | 是否可用、是否下发、大小、来源、是否持久化 | 不包含完整原文对象 |
| `native_raw` | 仅声明能力后给 | Telegram 原生 dict 兼容结构 | 必须声明 `capabilities.telegram_native_raw.enabled=true`，且不得包含凭据或 live object |
| Trace payload snapshot | 默认按设置保存脱敏快照 | 标准事件和动作摘要 | 默认不保存完整 `native_raw` |

最终版平台不再替个人用户判断“某个插件业务上应不应该看这条消息”，但仍必须执行四条客观边界：

- 不下发账号 session、Bot token、API key、私钥、数据库连接串等凭据。
- 不下发 Telethon event/client、Bot API client、HTTP session 等 live object。
- 不把普通 Bot 包装成有转账能力的主体；转账/发奖必须走 userbot 或 settlement。
- 不恢复旧 `notice` / `bbot_notice` / `notice_bot` 主动发送通道。

这四条是最终版的底线，不属于“强沙箱”残留，也不能由插件声明绕过。

### 19.15 插件生态迁移边界

最终版必须重新登记插件身份，避免“平台功能、官方插件、远程插件、示例插件”混在一起：

| 类型 | 定义 | 最终版处理 |
| --- | --- | --- |
| 平台功能 | 系统运行所需或明显不是插件的能力，例如定时任务框架、日志、账号管理、插件仓库管理 | 不再伪装成普通插件；在系统或平台设置中展示 |
| 插件库推荐插件 | TelePilot 维护推荐，但不随本体分发，例如自动回复、自动复读 | 首次部署或升级后可提示从插件库安装；安装后可手动移除；manifest 必须完整声明 `usage`、`event_subscriptions`、`capabilities` |
| 插件库维护插件 | 由插件库维护、按需安装的能力，例如图片生成、游戏玩法、算数题等 | 从插件库安装/更新；仓库刷新和一键更新必须保留新字段和风险提示 |
| 示例插件 | 用于开发者学习和验证的插件，例如 event bus demo | 不默认启用；必须能通过验证脚本；文档示例从这里抽取 |
| 用户安装插件 | 用户从私有库或第三方库安装的插件 | 不强制迁移代码，但安装/启用/更新时必须显示规范警告、风险提示和废弃通道错误 |

插件库推荐插件和插件库维护插件不允许为了通过 lint 写空声明。每个插件必须说明：

- 谁能触发：管理员命令、玩家关键词、callback、inline、payment。
- 收到什么事件：`event_subscriptions`。
- 用什么能力：`capabilities`。
- 普通互动默认由谁发送：`interaction_bot`、`userbot_reply` 或 `auto`。
- 付款、发奖、结算是否需要 userbot。
- 开发者如何在日志页排查它为什么没启动。

如果某个历史内置插件暂时不能迁移到新模型，它必须被降级为“待迁移插件库插件”，不能继续作为最终版内置主路径发布。

### 19.16 前端最终版页面合同

最终版不是后端链路能跑就结束。以下页面必须能用真实 API 或固定 fixture 验收，且桌面和窄屏/PWA 都要可用：

| 页面 | 必须回答的问题 | 验收重点 |
| --- | --- | --- |
| 日志中心 | 系统是否健康、消息走到哪、插件卡在哪、动作为什么失败 | 总览、时间线、详情、错误态、原始日志、reason_code 中文说明 |
| 交互中心 | 当前账号/交互 Bot 有哪些规则，规则如何映射 Event Bus | 顶部账号/Bot 选择、规则列表、规则详情、订阅映射、最近错误 |
| 插件中心 | 插件有什么使用说明、订阅事件、能力风险、规范警告 | 缺 usage 红色高级警告、native_raw/inline_all 风险、一键更新风险预览 |
| 插件配置页 | 开发者自定义配置是否在容器框架内可用 | 使用说明、总开关、配置区、预览区顺序固定；无默认说明兜底 |
| 系统设置 | 运维开关是否清楚，是否能支持回滚 | Trace/Event Bus/Inline/payload/native_raw 设置和默认值 |

前端验收不能只跑 typecheck/build。凡是改到 UI，最终证据台账必须写明：

- 验收 URL。
- 桌面视口结果。
- 窄屏/PWA 结果。
- 长中文、长插件名、长错误消息是否撑破布局。
- loading、empty、error、disabled、success 状态是否可见。

### 19.17 部署和回滚演练最低要求

部署到 `144.24.5.159` 前，最终版必须先证明“出问题时能退”：

1. 备份 `.env`、compose 文件和数据库，记录绝对路径和时间。
2. 记录部署前远端 commit、版本号和容器状态。
3. 部署后执行迁移，确认新 Trace 表存在且旧 `runtime_log` / `audit_log` 仍可读。
4. 触发至少一条普通消息 trace 和一条插件 action trace。
5. 将 `event_bus_delivery_enabled=false` 演练一次，确认旧规则路径仍能处理已有交互规则。
6. 将 `inline_updates_enabled=false` 演练一次，确认普通 message/callback 不受影响。
7. 将 `trace_enabled=false` 演练一次，确认旧 runtime/audit 仍有排障记录。
8. 恢复最终默认开关，再确认日志页和插件运行正常。

如果第 5-7 步因为线上不适合直接演练，必须在同版本本地或临时环境完成，并在证据台账中写明为什么线上跳过、替代环境是什么、剩余风险是什么。

### 19.18 最终版执行启动条件

当用户要求“按计划执行”时，主 Agent 不再重新讨论方向，直接按以下顺序启动：

1. 读取 `AGENTS.md`、全局经验文档、`docs/AGENT-PLAYBOOKS.md` 和本计划。
2. 固定分支、版本、工作树和当前 diff。
3. 建立第 19.12 节证据台账骨架。
4. 按 Wave 0-3 分配子 Agent；每个子 Agent 必须带写入范围、禁区、验证命令和交付格式。
5. 主 Agent 只在契约漂移、数据迁移、部署发布、跨 Agent 冲突和 blocker 无法关闭时重新决策。
6. 全部 Go / No-Go 为 `是` 后，才更新版本、写中文 CHANGELOG、commit、push、部署。

按这套启动条件执行后，计划不再依赖“记得检查一下”的口头约束，而是每一步都有证据、门禁、回滚和最终判定。

### 19.19 最终版状态控制板

最终版执行期间，主 Agent 必须维护一个状态控制板，并同步写入当前封口版本对应的 `docs/release/<version>-final-evidence.md`。控制板不是进度汇报，而是 Go / No-Go 的事实来源。

控制板固定包含以下行，状态只允许使用第 0.7 节的四种状态：

| 控制项 | 对应证据 | 不得标为已完成的情况 |
| --- | --- | --- |
| 数据层和迁移 | Alembic、模型导入、索引、保留策略测试 | 只在开发库迁移过，没有空库/旧库验证 |
| 五条事件链路 | 普通消息、命令、callback、inline、payment 的 trace/span/action | 只有单元测试，没有日志页或 API 详情证据 |
| 插件契约 | manifest、Event Bus、MessageOps、Contract Guard、reason_code | 后端可用但前端类型、文档或验证脚本未同步 |
| 官方和示例插件 | 官方插件 manifest、示例插件、验证脚本、dry-run | 只是补了字段，但插件主路径仍读旧 payload |
| 远程插件仓库 | 私有库、`tree/<branch>`、刷新、一键更新、字段保真 | 安装可用但升级前不能看到风险变化 |
| 日志中心 | Trace API、时间线、插件诊断、动作详情、原始日志 | typecheck/build 通过但未做桌面和窄屏验收 |
| 交互中心 | 账号/Bot 选择、规则列表、规则详情、Event Bus 映射 | 仍要求钻到账号详情页才能理解或配置规则 |
| 插件 UI | usage、配置容器、自定义样式、预览建议、规范警告 | 缺 usage 没有红色高级警告 |
| 设置和回滚 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled`、保留期 | 设置字段存在但没有影响运行时 |
| 开发文档 | API Reference、Cheatsheet、Remote、Safety、README | 旧机制仍作为推荐路径出现 |
| 发布材料 | 版本四处同步、中文 CHANGELOG、中文 commit/PR | 根据计划写发布说明，未按实际 diff 写 |
| 服务器部署 | 备份、远端 commit、迁移、健康检查、关键 trace、docker logs | 只写“已部署”，没有可复核命令和结果 |

主 Agent 每次合并子 Agent 工作后必须更新控制板。任何一行仍为 `未开始`、`半落地` 或 `可测` 时，最终版状态必须保持为 `可测`，不得向用户表达“最终版已完成”。

### 19.20 端到端验收数据包

最终版必须有一套可以重复运行的端到端验收数据包，用来证明框架不是只在真实 Telegram 环境里靠手感可用。

建议固定为以下结构；如实际目录不同，证据台账必须写清映射：

```text
backend/app/tests/fixtures/event_bus/
  userbot_message.json
  interaction_bot_message.json
  callback_query.json
  inline_query.json
  chosen_inline_result.json
  external_payment_notice.json
  native_raw_telethon_message.json
  deprecated_notice_action.json

examples/plugins/event_bus_demo/
  plugin.json
  manifest.py
  plugin.py
  fixtures/
```

这些 fixture 必须支持以下自动化断言：

- 标准化后 `TelePilotEvent` 字段完整，且不包含 token、session、live client。
- `event_subscriptions` 匹配结果包含 matched、skipped、delivered 和稳定 reason_code。
- 未声明 `telegram_native_raw` 的插件没有 `native_raw`；声明插件拿到 JSON 兼容 dict。
- `answer_callback`、`answer_inline_query`、`send_message`、`settlement` 都能进入 `event_action`。
- 旧 `notice` / `bbot_notice` / `notice_bot` 只能产生 `send_channel_deprecated`，不能被自动改写为可执行通道。
- 付款确认事件能区分 `source_actor`、`player`、`payment`、`reply_to`，普通 Bot 不执行转账。

最终验收命令至少包括：

```bash
cd backend && .venv/bin/pytest app/tests/test_event_bus.py app/tests/test_event_trace.py app/tests/test_account_bot.py app/tests/test_worker_command.py -q
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
```

如果全量 `pytest -q` 已覆盖这些用例，证据台账仍必须列出具体测试名，避免未来测试被删除后只剩一个笼统命令。

### 19.21 数据库、设置和迁移闭环

最终版涉及新增表、设置项和运行时开关，必须把迁移和升级体验当作正式交付内容。

数据库闭环必须证明：

- `alembic upgrade head` 可在空库执行。
- `alembic upgrade head` 可在现有数据目录执行。
- 新表存在，索引存在，旧 `runtime_log` / `audit_log` 仍可读。
- Trace 写入失败不会回滚业务事务。
- 清理任务能处理过期 `payload_snapshot` 和持久化 `native_raw`，并保留 `event_trace` 主记录。
- 紧急回滚时不要求执行删表 downgrade；新表可留存不用。

设置闭环必须证明：

- `trace_enabled=false`：不写新 Trace，但旧 runtime/audit 仍记录关键错误。
- `event_bus_delivery_enabled=false`：新 Event Bus 投递停用，系统进入 `degraded_event_bus_disabled` 降级兼容状态；旧规则路径只能作为回滚兜底，不能作为最终版常态。
- `inline_updates_enabled=false`：inline updates 不处理，message/callback 不受影响。
- `native_raw_persist_enabled=false`：插件仍可在声明后收到 `native_raw`，但日志库不持久化完整原生数据。
- `native_raw_retention_days=1`：开启持久化后，一天外完整原生数据被清理或标记过期。

这些开关是运维护栏，不是产品双模式。前端系统设置可以展示它们，但文案必须避免让用户以为 TelePilot 有“标准模式 / 个人模式”两套框架。

### 19.22 运行时稳态和性能边界

全量消息开放会放大消息量，最终版必须定义稳态边界，避免日志系统和插件调度拖垮主流程。

运行时必须满足：

- Trace 写库使用 best-effort；失败写旧 runtime system/error，不阻断 Telegram 消息处理。
- Event Bus 订阅匹配先按 account、source、event_type、scope 做粗过滤，再进入 filters 和插件调用。
- 对明显不相关插件写聚合 skipped span，不为每条消息给所有插件逐一落库。
- `payload_snapshot`、`raw_summary`、`native_raw_meta` 都有大小上限和截断标记。
- 单 trace 的 span/action 数量超过软上限时，继续执行业务并记录 `trace_span_limit_reached`。
- Inline Query、callback、公共关键词触发必须经过频控；频控命中写 `rate_limited`。
- 插件运行异常只影响当前插件调用，不应中断同一事件下其他已匹配插件的处理，除非插件入口显式声明独占会话。

最终 review 必须抽查代码里是否存在绕过这些边界的路径，尤其是直接循环所有插件、直接保存完整 raw、Trace 异常向外抛、动作失败不落库这四类问题。

### 19.23 前端实测脚本和验收入口

最终版前端验收必须绑定具体 URL，不允许只说“页面看过”。本地或远端至少检查：

| 页面 | URL 示例 | 必测状态 |
| --- | --- | --- |
| 首页/版本 | `/` | 版本号、导航、移动端底部栏 |
| 日志中心 | `/logs` | 总览、筛选、详情时间线、错误态、原始日志 |
| 插件中心 | `/plugins` | usage 缺失警告、订阅/能力展示、长插件名 |
| 插件仓库 | `/plugins/manage?tab=plugins` | 刷新、私有库、`tree/<branch>`、一键更新预览 |
| 插件配置 | `/accounts/1/features/<plugin_key>?from=plugins` | 使用说明、总开关、配置容器、预览建议 |
| 交互中心 | `/interaction?aid=1` | 账号/Bot 选择、规则列表、规则详情、Event Bus 映射 |
| 系统设置 | `/settings` | Trace、Event Bus、Inline、payload、native_raw 设置 |

每个页面至少验收两种视口：

- 桌面宽屏：`1440x900` 或当前浏览器宽屏。
- 窄屏/PWA：`390x844` 或接近手机宽度。

验收重点：

- 底部导航不能换行、重叠或遮挡主按钮。
- 固定按钮必须固定在网页显示区域内，而不是跟随内容滚动到不可见位置。
- 长中文、长英文、长插件名、长错误消息不能撑破容器。
- loading、empty、error、disabled、success 状态都有可读文案。
- 高级 JSON、native_raw、风险提示默认折叠，展开前有明确提示。

### 19.24 文档最终同步包

最终版文档必须按读者路径重组，而不是把所有历史设计堆在一起。

发布前每份文档的定位如下：

| 文档 | 最终定位 | 必须包含 | 不应包含 |
| --- | --- | --- | --- |
| `README.md` | 用户安装、部署、入口导航 | 一键部署、docker compose、WebUI 配置、插件仓库、交互中心、日志中心 | 旧交互 Bot 内部机制长篇说明 |
| `docs/PLUGIN-API-REFERENCE.md` | 插件开发主文档 | 标准事件信封、manifest、MessageOps、Trace 排障、示例 | 旧平铺 payload 推荐写法 |
| `docs/PLUGIN-CHEATSHEET.md` | 快速抄写手册 | 最小插件、常用 action、reason_code 快查 | 过时 hook 或 `event.reply` 主路径 |
| `docs/PLUGIN-REMOTE.md` | 远程插件发布和仓库维护 | 私有 GitHub、`tree/<branch>`、usage、订阅、能力、一键更新 | 旧字段裁剪说明 |
| `docs/PLUGIN-SAFETY.md` | 个人可信插件风险说明 | 风险自担、平台提醒、客观边界、审计、回滚 | 平台强沙箱会替用户兜底的暗示 |
| `docs/INTERACTION-BOT-OPTIMIZATION.md` | 历史/架构说明 | 若保留，必须标注历史背景或更新到最终框架 | 与最终 Event Bus 文档冲突的主路径 |

发布前必须运行文档审计命令，并把旧概念命中逐条归类。允许保留旧概念，但只允许出现在三种上下文：

- 废弃值说明，例如 `notice` / `bbot_notice` / `notice_bot`。
- 迁移说明，例如旧平铺 payload 如何迁移到标准事件信封。
- 历史设计说明，并明确不再是当前开发主路径。

### 19.25 残余风险允许范围

最终版可以带少量残余风险上线，但这些风险必须被明确归类，不能伪装成完成。

允许作为残余风险的情况：

- 用户安装的第三方插件仍缺 `usage` 或仍使用旧 `interaction_entries`，但 WebUI 和验证脚本已给出规范警告，平台维护插件已迁移。
- 线上不适合直接演练某个回滚开关，但已在同版本本地或临时环境演练，并记录原因和剩余风险。
- 某些历史设计文档保留旧概念，但已标注历史背景，不会被开发指南引用为主路径。
- 某个 Telegram 罕见 update 类型暂不支持，但不影响 message、command、callback、inline、payment 五条主链路。

不允许作为残余风险的情况：

- 官方/示例插件仍依赖旧平铺 payload 主路径。
- 旧 `notice` / `bbot_notice` / `notice_bot` 还能发送消息。
- 未声明插件能拿到完整 `native_raw`。
- 插件动作失败没有 `event_action`。
- 日志页无法解释插件为什么没启动。
- 远程插件安装或升级会丢失 `usage`、`event_subscriptions`、`capabilities`。
- 版本、CHANGELOG、远端部署和实际 diff 不一致。

最终报告必须单独列出残余风险。若残余风险属于“不允许”类别，发布结论只能是 No-Go。

### 19.26 最终版宣称边界

计划全部执行后，可以向用户和插件开发者宣称：

- TelePilot 已形成统一 Telegram Event Bus、Trace 日志、MessageOps 和个人可信插件风险提示框架。
- 插件可以用统一事件信封处理 UserBot 消息、交互 Bot 消息、命令、callback、inline、payment。
- 插件可以自由选择普通互动发送通道，转账/发奖等能力仍由 userbot 或 settlement 承接。
- 日志中心可以从消息、插件、命令、动作四个入口排查链路。
- 新插件开发应以 `event_subscriptions`、标准 payload、`ctx.messages`、Trace 排障为主路径。

不能宣称：

- TelePilot 封装了 Telegram 的所有 update 类型和所有 Bot API 方法。
- 平台会替用户审查远程插件是否安全可信。
- 普通 Bot 具备转账能力。
- 旧插件无需修改即可自动符合最终版最佳实践。
- 关闭 Trace 后仍能获得同等详细的链路日志。

这条宣称边界必须同步到最终 CHANGELOG 和插件开发指南，避免外部读者把“最终版框架落地”理解成“所有 Telegram 能力和所有旧插件都已完美覆盖”。

## 20. 最终版封口补强计划

本节用于解决最后一个容易误判的问题：代码已经大体实现、测试也通过时，仍然不能自动等同于“最终版”。最终版必须同时满足可开发、可运行、可排障、可部署、可回滚、可解释六个条件。

如果当前分支已经完成到 `0.40.0` 或更高的代码实现检查点，后续执行应从本节开始，不再回头重做 0.38/0.39/0.40 的架构讨论。执行目标是把状态从 `可测` 收口到 `已完成`，并把所有证据写入当前封口版本对应的 `docs/release/<version>-final-evidence.md`。

### 20.1 最终版六个闭环

最终版必须同时完成以下六个闭环。任一闭环缺证据，都只能称为“可测版本”。

| 闭环 | 必须成立的事实 | 证据 |
| --- | --- | --- |
| 事件闭环 | UserBot、交互 Bot、callback、inline、payment 都先标准化为事件，再进入 Event Bus | 五条链路 trace、Source Adapter 测试、日志页详情 |
| 插件闭环 | 插件只需理解标准事件信封、`event_subscriptions`、`capabilities` 和 `ctx.messages` | 示例插件、验证脚本、开发指南 |
| 动作闭环 | 发送、编辑、删除、置顶、callback ACK、inline answer、settlement 都经 Delivery Executor 并落 `event_action` | action 测试、失败动作 trace、旧 notice 失败证据 |
| 排障闭环 | 日志页能解释收到、未触发、已触发、插件失败、动作失败、旧日志 fallback | Trace API、日志页桌面和窄屏验收 |
| 发布闭环 | 版本、CHANGELOG、commit、远端分支、部署版本和实际 diff 一致 | 四处版本号、中文 CHANGELOG、远端 commit |
| 回滚闭环 | 出问题时能关闭新链路或退回旧路径，且不要求删 Trace 表 | 备份路径、开关演练、旧 runtime/audit 可用 |

这六个闭环不是新增范围，而是对前文任务卡的最终判定方式。实现代码、文档和 UI 只有进入这些闭环，才算真正服务于最终版。

### 20.2 当前分支封口顺序

若代码已经处于 `0.40.0` 或更高的实现检查点，按以下顺序封口：

1. **证据基线**
   - 记录本地分支、远端分支、commit、工作树、未跟踪文件。
   - 更新当前封口版本对应的 `docs/release/<version>-final-evidence.md` 控制板，不允许用“应该已完成”填充。
   - 明确哪些条目是 `未开始`、`半落地`、`可测`、`已完成`；缺远端、浏览器或真实 trace 证据时只能写 `可测` 并列剩余风险，不能写计划外状态。

2. **自动验证**
   - 跑完第 18.2 节命令。
   - 单独列出覆盖普通消息、管理员命令、callback、inline、payment、native_raw、旧 notice、action failed 的测试名。
   - 如果本地数据库或 Docker 不可用，必须用 offline SQL、远端迁移或临时环境补证据。

3. **文档和契约反查**
   - 从代码枚举反查文档：事件类型、reason_code、send_via、capabilities、MessageOps。
   - 从文档示例反跑验证脚本。
   - 所有旧 `notice`、旧平铺 payload、旧 `raw_event`、旧规则主路径命中必须归类。

4. **前端实测**
   - 用真实 API 或固定 fixture 验收 `/logs`、`/interaction?aid=1`、`/plugins`、`/plugins/manage?tab=plugins`、插件配置页、`/settings`。
   - 每个页面至少验收桌面和窄屏/PWA。
   - 截图或记录页面 URL、视口、通过标准和失败项。

5. **远端部署**
   - 部署前备份 `.env`、compose 文件和数据库。
   - 拉取目标分支和目标 commit，执行迁移和服务更新。
   - 确认远端版本、远端 commit、健康检查、Docker 日志和 Trace API。

6. **真实链路验收**
   - 至少触发普通消息、未触发消息、插件 action、callback 或 inline、插件错误、旧 notice 失败中的核心场景。
   - 每个场景记录 trace_id、页面入口、预期结果、实际结果。

7. **回滚演练**
   - 验证 `trace_enabled=false`、`event_bus_delivery_enabled=false`、`inline_updates_enabled=false` 的实际效果。
   - 如果线上不能演练，必须在同版本临时环境演练，并在证据台账记录原因和剩余风险。

8. **最终报告**
   - 按实际 diff 和证据写中文报告。
   - 给用户和朋友看的报告必须区分“大架构落地”“UI 小修”“残余风险”“后续非阻塞优化”。

任何一步失败，都不回退到重新讨论架构，而是回到对应任务卡修复。只有第 20.1 节六个闭环全部有证据，才能把最终状态写成 `已完成`。

### 20.3 最小真实验收数据集

最终版至少要拿到以下真实或 fixture trace。真实环境优先，fixture 只能补线上不适合触发的危险或稀有场景。

| 场景 | 输入 | 必须看到 |
| --- | --- | --- |
| 普通群消息 | 允许群内任意文本 | receive、normalize、subscription skipped 或 delivered |
| 未触发消息 | 不匹配任何插件的文本 | `filter_not_matched`、`event_type_not_subscribed` 或聚合 skipped reason |
| 管理员命令 | 账号主人发送命令 | command parse、权限、插件/系统处理器、userbot_reply 或 action |
| 玩家关键词 | 群友发送玩法关键词 | public keyword、session、插件执行、interaction_bot action |
| 按钮回调 | 点击交互 Bot 按钮 | callback data、session/rule/subscription、answer_callback |
| Inline Query | `@botname keyword` | inline scope、answer_inline_query、结果数量或 API 错误 |
| 付款确认 | 外部转账通知 Bot 消息 | source_actor、payment、player、reply_to、settlement/userbot action |
| native_raw | 声明能力的测试插件 | `native_raw_meta.enabled=true`、插件可读 native_raw、默认不持久化完整 raw |
| 未声明 native_raw | 未声明能力的插件读取原生数据 | `native_raw_not_allowed` 或 `native_raw_meta.enabled=false` |
| 旧 notice | 插件返回 `notice` / `bbot_notice` | `send_channel_deprecated`，不发送消息 |
| 插件加载失败 | 故意损坏插件 manifest 或入口 | `PluginRuntimeStatus`、`plugin_load_failed`、最近 trace |
| 动作失败 | 缺 token、非法 message_id 或缺 inline_query_id | failed `event_action`、中文错误、可重试判断 |

证据台账里不能只写“测试覆盖”。每一行至少需要测试名或 trace_id；前端相关场景还需要页面入口。

### 20.4 最终版 No-Go 缺口处理规则

最终封口时遇到缺口，按以下规则处理，避免把 blocker 降级成“后续优化”。

| 缺口类型 | 处理 |
| --- | --- |
| 自动测试失败 | 必须修复或证明是环境问题；没有替代证据时 No-Go |
| 远端无法部署 | 最终状态保持 `可测`；可以 push 分支，但不能宣称服务器最终版已落地 |
| 浏览器/PWA 未验收 | 前端相关控制项最多 `可测` |
| 回滚开关未演练 | 发布控制项最多 `可测`；生产部署必须明确剩余风险 |
| 第三方插件仍旧 | 可作为允许残余风险，但 WebUI 必须有规范警告 |
| 官方/示例插件仍旧 | No-Go，必须迁移或从官方/内置主路径移出 |
| 文档仍教旧主路径 | No-Go，开发指南必须先修 |
| 旧 notice 还能发消息 | No-Go，必须改为明确失败 |
| 未声明插件能拿 native_raw | No-Go，必须修复能力边界 |
| 日志页解释不了未触发原因 | No-Go，必须补 reason_code 或 UI 展示 |

### 20.5 子 Agent 可并行执行的最终封口任务

如果需要并行推进，按下面拆分，避免互相覆盖。每个 Agent 都必须知道自己不是唯一编辑者，不得 revert、checkout 或 reset 其他人的改动。

| Agent | 类型 | 写入范围 | 任务 | 禁区 |
| --- | --- | --- | --- | --- |
| Evidence Agent | 只读或文档 | `docs/release/<version>-final-evidence.md` | 汇总验证命令、测试名、trace 证据、残余风险 | 不改 runtime、迁移、版本 |
| Runtime Agent | 后端 | `backend/app/services/event_bus.py`、`backend/app/services/event_trace.py`、运行时相关测试 | 关闭五条链路、action、reason_code、开关缺口 | 不改前端页面、不写发布版本段 |
| UI Agent | 前端 | `/logs`、`/interaction`、`/plugins`、`/settings` 相关文件 | 修复日志页和交互/插件 UI，完成桌面和窄屏验收 | 不改后端契约语义 |
| Docs Agent | 文档和示例 | README、插件开发文档、示例插件、验证脚本 | 删除旧主路径、补最终版开发指南、跑示例验证 | 不改生产 runtime |
| Deploy Agent | 运维 | 部署脚本、远端部署记录、证据台账 | 备份、部署、迁移、健康检查、回滚演练 | 没有 SSH/备份时不得硬改服务器 |

主 Agent 只负责契约冲突、跨任务合并、版本发布、最终 review 和部署确认。任何 Agent 需要修改公共契约字段名、数据库迁移、版本号、CHANGELOG 正式段或远端环境时，必须停下来交给主 Agent。

### 20.6 最终版报告结构

最终执行完成后，对用户输出的报告必须按以下结构写，避免把计划愿景当完成事实：

1. **版本与分支**
   - 本地分支、远端分支、commit、版本号、CHANGELOG 段。

2. **朋友建议落地情况**
   - 全量消息进入 Event Bus。
   - 个人可信插件标准。
   - UserBot 主控、交互 Bot 承接高频互动。
   - 插件自由选择通道。
   - 转账/发奖仍由 userbot 或 settlement。
   - 日志中心可追踪消息、插件、动作。

3. **重要架构改动**
   - Event Bus、Trace、MessageOps、Contract Guard 新定位、native_raw、Inline、远程插件字段贯通。

4. **UI 和体验改动**
   - 日志中心、交互中心、插件中心、插件配置、仓库刷新/一键更新、一键部署。

5. **验证证据**
   - 自动验证命令。
   - 人工验收页面。
   - 远端部署和健康检查。
   - 回滚演练。

6. **残余风险**
   - 只列允许残余风险。
   - 若存在 No-Go 风险，不能写“最终版已完成”。

### 20.7 最终执行口径

后续如果用户说“继续执行最终版计划”，默认含义是：

- 不重新争论是否需要 Event Bus、Trace、MessageOps、个人可信插件标准。
- 不再兼容旧插件作为目标；旧插件应迁移到新规则。
- 不把远端部署、浏览器验收、回滚演练视为可选项。
- 不提前宣布最终完成；只有证据台账全部通过才宣布。
- 不覆盖 main；继续使用当前 0.33+ 工作分支或明确的新分支。

这就是“按计划后能实现最终版”的边界：计划本身不靠口头信任，而是靠任务卡、闭环、No-Go 规则、真实 trace、部署证据和回滚证据共同判定。

## 21. 0.40.x 最终版执行蓝图

本节把前面的计划压缩成真正执行时的作战图。它解决三个问题：当前 0.40.x 封口时先修什么、哪些任务可以并行、什么顺序合并才不会让“最终版”变成多个半成品叠在一起。

### 21.1 当前 0.40.x 封口起点

执行者进入当前分支时，必须先把以下事实写入证据台账：

- 当前版本文件中的实际版本号。
- 当前分支和远端跟踪分支。
- 当前 HEAD、本地未提交改动和未跟踪文件。
- 最近一次自动验证对应的 commit；如果工作区之后又有改动，旧验证只能作为历史基线，不能作为当前通过证据。
- 证据台账文件名是否与当前版本一致；不一致时必须创建或更新当前版本台账，旧台账只能作为历史记录。

截至 `0.40.x` 封口补丁，必须特别防止三类误判：

- 版本号已经从 `0.40.0` 进入后续 patch（补丁版本），所有最终证据、部署报告和 CHANGELOG 都必须写当前版本。
- 已跑过的测试如果早于当前工作区改动，不能继续标为当前通过。
- 任何半成品 runtime patch、未跟踪文件、远端未部署或浏览器未验收，都必须在控制板里保持 `半落地` 或 `可测`，不能写成 `已完成`。

### 21.2 五个并行执行包

最终版可以并行推进，但必须按包隔离写入范围。每个包完成后只提交证据和自己负责的文件，不跨包“顺手修”。

| 执行包 | 目标 | 写入范围 | 完成标准 |
| --- | --- | --- | --- |
| A. Runtime 封口 | 关闭 Event Bus、Trace、MessageOps、旧 notice、native_raw、reason_code 的运行时 blocker | `backend/app/services/*`、`backend/app/worker/*`、后端测试 | 五条链路自动测试通过；所有 action 成功/失败都落 `event_action`；旧通道明确失败 |
| B. Contract 与示例 | 保证 manifest、示例插件、验证脚本、已安装插件警告按最终契约工作 | `backend/app/worker/plugins/*`、`examples/plugins/*`、`scripts/validate-*`、插件测试 | 示例插件覆盖 message/command/callback/inline/payment/native_raw/旧 notice；验证脚本能失败在正确原因 |
| C. Frontend 验收 | 日志中心、交互中心、插件中心、设置页能按最终版排障和配置 | `frontend/src/pages/*`、`frontend/src/components/*`、前端类型 | typecheck/build 通过；桌面和窄屏/PWA 验收记录写入证据台账 |
| D. Docs 与证据 | 开发文档、README、远程插件文档、安全说明和证据台账同步当前实现 | `README.md`、`docs/*`、`docs/release/*` | 文档不再推荐旧主路径；grep 审计分类完成；证据台账不含旧版本伪通过 |
| E. Release 与 Deploy | 版本、CHANGELOG、commit、push、远端部署、迁移、回滚演练闭环 | 版本文件、`CHANGELOG.md`、部署记录 | 远端 commit/版本/健康检查/Trace API/回滚开关均有证据；没有证据则只能保持 `可测` |

### 21.3 合并顺序

并行包可以同时工作，但合并和最终验收必须按以下顺序：

1. 合并 A，因为后端契约和运行时行为是所有 UI、文档和部署的事实来源。
2. 合并 B，因为示例和验证脚本是文档可信度的基础。
3. 合并 C，因为前端必须读取 A/B 的真实字段，不能先按想象写 UI。
4. 合并 D，因为文档和证据必须以最终代码、最终 UI、最终验证命令为准。
5. 执行 E，因为发布和部署只能发生在代码、文档、验证都稳定之后。

如果 C 或 D 发现 A/B 的公共字段不够用，必须回到 A/B 修契约，再重新跑 C/D 验收。不得在前端或文档里创造后端不存在的字段。

### 21.4 最终版实现的最小可证明集合

最终版不是把所有理想功能做满，而是至少证明以下集合完整闭环：

- 一条普通群消息能进入 Trace，匹配订阅或给出 skipped reason。
- 一条管理员命令能产生 command trace，记录权限、处理器和回复动作。
- 一个 callback 能产生 callback trace，并记录 `answer_callback`。
- 一个 inline query 能产生 inline trace，并记录 `answer_inline_query` 成功或失败。
- 一条外部转账通知消息能作为消息来源进入 Event Bus，并产生 payment/settlement/userbot 动作记录。
- 一个声明 `telegram_native_raw` 的插件能拿到 `native_raw`，未声明插件拿不到。
- 一个返回旧 `notice` / `bbot_notice` 的插件不会发送消息，只产生 `send_channel_deprecated`。
- 一个插件加载失败能在 `PluginRuntimeStatus` 和日志页插件诊断看到。
- 一个动作失败能在 `event_action` 看到失败原因和中文说明。
- 一个开发者照着文档复制最小插件，可以通过验证脚本并在 WebUI 看见使用说明、订阅和能力提示。

只要这十项齐备，并且部署和回滚证据齐备，就可以称为“最终版框架落地”。不需要声称 Telegram 所有 update 类型、所有 Bot API 方法或所有第三方插件都已经完全覆盖。

### 21.5 当前封口必须先清理的证据漂移

进入 0.40.x 最终封口前，主 Agent 必须先处理以下证据漂移：

- 证据台账标题和版本号要与当前版本一致，或者明确写明旧台账只是 `0.40.0` 历史基线。
- `CHANGELOG.md` 中当前 patch（补丁版本）的内容必须只写实际封口修复，不能继续复述 `0.40.0` 的大架构能力。
- README 当前版本必须与四处版本文件一致。
- 任何“自动验证通过”的记录必须标注对应 commit；当前工作区改动后，需要重新跑验证或降级为历史记录。
- 未跟踪文件必须明确处理：纳入提交、加入忽略，或记录为不属于本次交付；不得静默混进发布。

### 21.6 最终版执行完成后的唯一结论格式

最终报告只能有三种结论：

- **Go / 已完成**：第 20.1 节六个闭环全部有证据，远端部署和回滚演练完成，可以向用户宣称最终版框架落地。
- **可测 / 待服务器实测**：自动验证、文档和本地/浏览器验收通过，但远端部署、真实 Telegram trace 或回滚演练缺证据；可以 push 分支，不可宣称服务器最终版完成。
- **No-Go / 阻塞**：存在旧 notice 仍可执行、未声明插件拿到 native_raw、动作失败不落库、日志页无法解释未触发、文档仍教旧主路径、自动验证失败且无替代证据等任一硬 blocker。

最终报告不得使用“基本完成”“应该没问题”“后面再看”作为结论。状态必须落到以上三类之一，并写清下一步是修 blocker、部署实测，还是可以合并发布。

## 22. 最终版补强清单

本节用于把当前计划从“方向正确、任务完整”推进到“执行后可以证明是最终版”。它不是新增愿景，而是封口时必须逐条关掉的具体缺口。后续执行如果发现本节与前文有冲突，以本节更严格的口径为准。

### 22.1 当前必须先修的硬 blocker

以下 blocker 任何一项存在时，最终结论只能是 `No-Go / 阻塞`；若只是证据缺失且实现已经可运行，对应控制项最多只能是 `半落地` 或 `可测`，不能宣称最终版完成。

| blocker | 风险 | 必须做到 | 验收证据 |
| --- | --- | --- | --- |
| UserBot Event Bus 只记录匹配、不真实投递插件 | 日志显示 matched，但插件 `on_event` / 入口没有执行，形成假闭环 | UserBot 来源消息命中 `event_subscriptions` 后必须调用插件入口；无旧 `on_message` 的新插件也能被触发 | 后端测试：仅声明 `event_subscriptions` + `on_event` 的插件收到 UserBot 消息并产生 `plugin_invoke`、`plugin_return`、`event_action` |
| 管理员命令没有动作级 Trace | 命令链路只能看到 parse，看不到最终回复/编辑/失败 | 命令处理器产生的回复、编辑、删除、插件 action 都必须写 `event_action` | 后端测试：命令成功、命令未匹配、命令回复失败都有 trace/action/reason_code |
| action 失败分支不落 `event_action` | 空文本、非法 media、缺 inline_query_id、Bot/UserBot 不可用时日志断链 | 插件返回的每个 action 最终必须是 `success`、`failed` 或 `skipped`，不能静默 return | 后端测试覆盖 `empty_message_text`、`media_payload_invalid`、`inline_query_id_missing`、`telegram_api_error` |
| reason_code 多处手写漂移 | 前端无法稳定筛选，文档和测试对不上 | 后端常量、前端中文映射、开发文档和测试共享同一批稳定 reason_code | grep/AST 测试证明关键 reason_code 未散落成临时字符串；新增 reason_code 必须同步文档 |
| 旧 `notice` 混合通道被部分放行 | 插件写 `["bot", "notice"]` 时可能误以为已迁移 | 任意 action selector 中含 `notice` / `bbot_notice` / `notice_bot`，整个 action 明确失败 | 测试证明 mixed selector 失败，`reason_code=send_channel_deprecated`，没有实际发送 |
| `native_raw` 边界不完整 | 未声明插件可能拿到原生数据，或日志长期持久化敏感 raw | 只有声明 `capabilities.telegram_native_raw.enabled=true` 且来源允许的插件能拿 `native_raw`；默认只写 `native_raw_meta` | fixture 测试：声明/未声明两类插件；日志页看到是否下发、大小、是否持久化 |
| 日志页只展示数据但不能排障 | 用户仍然不知道消息卡在哪一步 | 日志页必须按消息、插件、命令、动作四个入口定位链路，并展示 reason_code 中文说明 | 桌面和窄屏/PWA 验收：成功链路、未命中链路、插件失败、动作失败、原始日志 fallback |
| 证据台账写历史通过 | 已修代码后旧测试记录仍被当作当前证据 | 每次关键代码改动后，自动验证、浏览器验收、部署验收都必须重新标注 commit 或降级为历史基线 | `docs/release/<version>-final-evidence.md` 中每条通过都有命令、commit、页面或 trace_id |

### 22.2 UserBot 事件总线真实投递口径

UserBot 侧最终不能停在“收到消息并记录 Event Bus decision”。完整链路必须是：

```text
UserBot 收到 message/command/payment-notice
  -> Source Adapter 生成 TelePilotEvent
  -> Trace receive/normalize
  -> Event Bus 匹配插件 event_subscriptions
  -> 对 matched decision 调用插件入口
  -> 插件读取统一 payload
  -> 插件返回 ctx.messages/action
  -> Delivery Executor 或 worker 内受控执行器执行
  -> event_action 记录实际通道和结果
  -> Trace finish
```

执行规则：

- 新插件优先实现 `on_event(ctx, payload)`；如插件只有 `on_interaction(ctx, entry_key, payload)`，平台可以按 `entry_key` 兼容调用。
- 已声明 `event_subscriptions` 的插件，应以 Event Bus decision 为准；旧 `on_message` 只能作为迁移层或无订阅插件的兼容入口，不能和新订阅形成两套触发真相。
- UserBot 侧执行普通交互动作时，也必须遵守 `interaction_bot` / `userbot_reply` / `auto`；无法执行的通道必须写 failed action，而不是静默丢弃。
- 任何插件异常都必须更新 `PluginRuntimeStatus.last_invocation_status`、`last_trace_id` 和 trace span。

最低测试：

- 仅有 `on_event`、无旧 `on_message` 的插件被 UserBot 消息触发。
- 订阅未命中时写 skipped reason，不调用插件入口。
- 插件抛异常时 trace 为 failed，日志页插件诊断可见。
- 插件返回 `send_message`、`answer_callback`、`settlement` 中至少一种动作时产生 `event_action`。

### 22.3 日志系统最终版问题清单

日志页不是“能显示表格”就算完成。它必须让用户打开页面后按下面问题排查，不需要读源码：

- 系统是否在线：worker、UserBot、交互 Bot、DB、Redis、队列是否健康。
- 这条消息有没有进系统：按 chat_id、message_id、trace_id、文本关键词能查到 receive/normalize。
- 为什么没触发插件：展示 source/event/scope/filter/session/rate limit 的 skipped reason。
- 插件为什么没启动：安装、启用、manifest lint、加载异常、入口缺失、运行异常。
- 插件被什么调用：最近 trace、来源、事件类型、触发词、调用入口。
- 命令启动后调用了什么：命令解析、权限、系统处理器或插件入口、产生动作。
- 插件卡在哪一步：plugin_invoke、plugin_return、contract_guard、delivery 的耗时和状态。
- 最终由谁发消息：requested_send_via、actual_send_via、Telegram message_id、失败原因。
- 外部转账通知如何参与：第三方转账通知 Bot 只作为消息来源，进入 Event Bus 后生成 payment/settlement 相关 trace。

前端验收必须覆盖：

- `/logs` 桌面视口：总览、筛选、详情时间线、动作详情、插件诊断。
- `/logs` 窄屏/PWA：底部导航不换行、不遮挡主内容，详情可滚动。
- `/interaction?aid=1`：规则列表高度与详情协同，规则能映射为 Event Bus 订阅说明。
- `/plugins` 和插件配置页：usage 缺失红色高级警告、能力声明、事件订阅、规范警告。
- `/settings`：Trace/Event Bus/Inline/native_raw/payload 保留期设置能读到当前值，并说明回滚用途。

### 22.4 文档最终版写法

插件开发文档必须按“开发者要做什么”重写，而不是按 TelePilot 内部历史解释：

1. 创建插件：manifest 必填 `usage`、`event_subscriptions`、版本、入口。
2. 读取消息：统一读取 `payload["source"]`、`message`、`chat`、`sender`、`actor`、`payment`、`inline_query`。
3. 高级原生数据：需要严格风控时声明 `telegram_native_raw`，解释风险和日志可见性。
4. 发送动作：只用 `ctx.messages` 或标准 action，通道只写 `interaction_bot`、`userbot_reply`、`auto`。
5. 两条主流程：管理员命令调度和玩家关键词调度，各给一份最小示例。
6. Inline 插件：声明 `inline_query`，返回 `answer_inline_query`，解释 `inline_all` 风险。
7. 付款/发奖：外部转账通知 Bot 是消息来源；转账/发奖由 userbot 或 settlement，不由普通 Bot 执行。
8. 排障：照日志页顺序检查安装、启用、manifest、订阅、scope、filters、session、插件异常、action 失败。
9. 迁移：旧 `notice`、`bbot_notice`、`raw_event`、旧平铺 payload、旧规则驱动只作为废弃/迁移说明。

最终文档审计命令必须至少搜索：

```bash
rg -n "notice|bbot_notice|notice_bot|raw_event|payload\\[\"text\"\\]|event\\.reply|event\\.respond|ctx\\.client|旧规则驱动" README.md docs examples scripts backend frontend
```

保留命中必须逐条分类为：废弃说明、迁移说明、测试 fixture、内部实现、或需要修复。任何推荐路径命中旧写法都是 No-Go。

### 22.5 可并行执行的最终版任务卡

最终版可以并行推进，但每张任务卡必须独立可 review、可测试、可回滚。

| 任务卡 | 负责人类型 | 写入范围 | 完成定义 |
| --- | --- | --- | --- |
| R1 UserBot/Event Bus 投递闭环 | Runtime | `backend/app/worker/plugins/loader.py`、`backend/app/worker/runtime.py`、Event Bus 测试 | UserBot matched decision 真实调用插件入口并产生 trace/action |
| R2 命令与 action Trace 闭环 | Runtime | `backend/app/worker/command.py`、Delivery/Trace 相关测试 | 命令 parse、handler、reply/edit/delete/action 成功失败均可查 |
| R3 reason_code 与 Contract Guard 收口 | Runtime + Contract | `backend/app/services/interaction/*`、前端 reason 映射、文档 | 旧通道、越声明、客观失败都有稳定 code 和中文说明 |
| R4 日志页排障体验 | UI | `frontend/src/pages/Logs.tsx`、相关类型/API 调用 | 五类状态桌面/窄屏可用，reason_code 中文显示，原始日志 fallback |
| R5 交互/插件/设置 UI 校准 | UI | `/interaction`、`/plugins`、`/settings` 相关文件 | 规则、usage、能力、回滚设置都按最终框架展示 |
| R6 示例、验证脚本、文档 | Docs | `examples/plugins/*`、`scripts/validate-*`、`docs/*`、`README.md` | 开发者照文档能写 message/command/callback/inline/payment 插件 |
| R7 证据台账与发布 | Release | 版本文件、`CHANGELOG.md`、`docs/release/*` | 当前版本、当前 commit、当前验证结果一致，中文 changelog 完整 |
| R8 服务器部署与回滚演练 | Deploy | 远端部署记录、证据台账 | `144.24.5.159` 远端 commit/版本/健康检查/Trace API/开关回滚均有证据 |

合并顺序固定为 R1-R3、R4-R5、R6、R7、R8。R4/R5 可以并行开发，但必须在 R1-R3 的字段稳定后最终验收；R7/R8 只能在自动验证和浏览器验收通过后执行。

### 22.6 最终验证命令

每次准备宣称最终版完成前，必须从干净工作区或明确的提交点重新执行：

```bash
git diff --check
cd backend && .venv/bin/ruff check app ../scripts/validate-plugin-examples.py
cd backend && .venv/bin/pytest -q
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
cd frontend && ./node_modules/.bin/tsc -b --pretty false
cd frontend && ./node_modules/.bin/vite build
cd backend && .venv/bin/alembic heads
cd backend && .venv/bin/alembic upgrade head --sql >/tmp/telepilot-alembic-final.sql
```

浏览器验收必须使用本地或远端真实页面，至少记录：

- `/logs`
- `/interaction?aid=1`
- `/plugins`
- `/plugins/manage?tab=plugins`
- 一个真实插件配置页
- `/settings`

部署验收必须记录：

- 远端备份路径。
- 远端分支和 commit。
- 远端四处版本文件或 API 版本。
- 数据库迁移状态。
- `docker compose ps` 或等价服务状态。
- `/healthz` 或等价健康检查。
- 至少一条真实或可复现 trace。
- `trace_enabled=false`、`event_bus_delivery_enabled=false`、`inline_updates_enabled=false` 的回滚演练结果。

### 22.7 最终版宣称模板

只有全部证据满足时，最终报告才能写：

```text
结论：Go / 已完成。
版本：填写四处版本文件一致的当前版本号。
分支：填写当前推送分支，例如 codex/0.33-interaction-framework。
commit：填写最终本地和远端一致的提交 SHA。
服务器：144.24.5.159 已部署到同一个提交 SHA。
证据：自动验证通过、浏览器/PWA 验收通过、真实 trace 已记录、回滚开关演练通过。
边界：这表示最终版框架落地，不表示所有第三方插件已自动迁移，也不表示关闭 Trace 后仍有同等详细链路。
```

如果缺服务器部署、真实 Telegram trace 或回滚演练，必须写：

```text
结论：可测 / 待服务器实测。
原因：本地自动验证和文档/UI 验收已通过，但缺少远端部署、真实 Telegram trace 或回滚演练中的一项或多项。
下一步：补齐缺失的远端证据后再改为 Go。
```

如果存在本节任何硬 blocker，必须写：

```text
结论：No-Go / 阻塞。
阻塞项：写明命中的硬门禁、失败命令或不可安全继续的外部条件。
下一步：回到对应 F1-F9 任务卡修复并重新跑最终验证。
```

## 23. 最终版封版协议

本节用于把前文全部设计收束成“可以一次执行到底”的封版协议。它不是新增愿景，也不是降低验收标准；它的作用是防止执行过程中继续出现“又补一套旧入口”“前端先按想象展示”“文档先写以后会有”的漂移。

后续如果继续执行本计划，默认进入封版状态：

- 不再新增第三种产品模式。
- 不再恢复旧 `notice` / `bbot_notice` / `notice_bot` 发送通道。
- 不再把旧平铺 payload 写成新插件主路径。
- 不再把旧 runtime log 表格当作日志中心主入口。
- 不再把部署、浏览器验收、真实 trace、回滚演练当作可选项。

### 23.1 封版前置锁

开始执行代码收尾前，主 Agent 必须先锁定以下事实，并写入当前版本证据台账：

| 锁定项 | 锁定内容 | 漂移时处理 |
| --- | --- | --- |
| 分支 | 当前工作分支和远端跟踪分支 | 与用户要求不一致时先停下说明 |
| 版本 | 四处版本文件中的实际版本 | 证据台账和 CHANGELOG 必须跟随实际版本 |
| 工作树 | 已修改文件和未跟踪文件 | 未跟踪文件必须归类为纳入、不纳入或需忽略 |
| 数据库 | 当前 Alembic head 和线上/本地迁移状态 | 迁移证据过期时重新跑，不沿用旧记录 |
| 公共契约 | event type、payload、send_via、capabilities、reason_code、settings | 契约变更必须同步后端、前端、文档、测试 |
| 远端环境 | `144.24.5.159` 当前 commit、版本、容器状态、备份位置 | 无法访问远端时只能保持 `可测`，不能宣称 Go |

版本号只在准备发布、推送稳定检查点、创建 PR/release 或用户明确要求时统一迭代。若当前已经是封口补丁版本，后续每次发布前仍按实际 diff 判断 patch（补丁版本）或 minor（次版本），不得把第三位当流水号。

### 23.2 公共契约冻结

最终版公共契约冻结为以下集合。任何新增或修改都必须同时更新代码、类型、文档、示例、验证脚本和证据台账。

| 契约 | 冻结集合 |
| --- | --- |
| 主事件类型 | `message`、`command`、`callback_query`、`inline_query`、`chosen_inline_result`、`payment_confirmed` |
| 会话生命周期事件 | `session_close`，用于记录会话关闭、超时或插件主动结束；它不是新的 Telegram 来源，但必须能进入 Trace 和日志页 |
| 主来源通道 | `userbot`、`interaction_bot`、`external_payment_notice` |
| payload 顶层字段 | `trace_id`、`source`、`message`、`chat`、`sender`、`actor`、`source_actor`、`player`、`payment`、`reply_to`、`session`、`trigger`、`inline_query`、`chosen_inline_result`、`raw`、`native_raw_meta`、`native_raw` |
| action 类型 | `send_message`、`send_photo`、`send_file`、`edit_message`、`delete_message`、`pin_message`、`answer_callback`、`answer_inline_query`、`settlement`、`end_session` |
| 可执行发送通道 | `interaction_bot`、`userbot_reply`、`auto` |
| 废弃发送通道 | `notice`、`bbot_notice`、`notice_bot`，只能产生 `send_channel_deprecated` |
| 高风险能力 | `telegram_native_raw`、`inline_all`、跨通道发送、`settlement` / 转账发奖 |
| 回滚设置 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled`、`native_raw_persist_enabled`、payload/native_raw 保留期 |

封版后不允许前端或文档先行创造后端不存在的字段。确实需要新增字段时，先改公共契约和后端测试，再改前端展示和开发文档。

### 23.3 入口闭环

最终版只允许一种 Telegram 入口主路径：

```text
Telegram 来源
  -> Source Adapter
  -> TelePilotEvent
  -> Trace receive/normalize
  -> Event Bus decision
  -> Plugin dispatch
  -> Plugin action
  -> Delivery Executor / MessageOps
  -> event_action
  -> Trace finish
```

每类入口的封版要求：

| 入口 | 必须进入 Event Bus | 必须有 Trace | 插件调用规则 | No-Go 条件 |
| --- | --- | --- | --- | --- |
| UserBot 普通消息 | 是 | receive、normalize、subscription、invoke/skip | 命中 `event_subscriptions` 后调用 `on_event` 或兼容入口 | 只记录 matched 不执行插件 |
| 管理员命令 | 是 | parse、permission、handler、action | 命令也要形成可排障 trace | 只能看到命令解析，看不到回复/编辑动作 |
| 交互 Bot 消息 | 是 | receive、normalize、subscription、delivery | 高频互动默认可用 `interaction_bot` | 仍只依赖旧规则直接触发插件 |
| Callback | 是 | callback、answer、plugin action | 必须记录 `answer_callback` 成败 | 缺 query id 时静默失败 |
| Inline Query | 是 | inline_query、answer_inline_query | `inline_all` 需风险提示和 trace | 没有 `inline_query_id` 仍显示成功 |
| Chosen Inline Result | 是 | chosen_inline_result | 至少记录来源和选择结果 | 选择结果完全无 trace |
| 外部转账通知 | 是 | source_actor、payment、settlement | 第三方通知 Bot 只是消息来源，发奖走 userbot/settlement | 把第三方通知 Bot 当发送通道 |

旧规则、旧 session、旧玩法 fallback 如果仍存在，只能作为 Event Bus 的订阅条件或迁移适配层，不能形成第二套绕过 trace/action 的插件调度真相。

管理员命令、旧 `on_message`、旧 `interaction_entries`、付款确认和 `session_close` 都必须由 Event Bus decision 产生命中/跳过/投递记录后再进入兼容调用层。只在外层补 `trace_id`、再由旧路径直接调用插件，不满足最终版入口闭环。

`event_bus_delivery_enabled=false` 是回滚开关，不是产品模式。该开关关闭时可以继续保住旧规则可用性，但最终验收必须降级为 `可测` 或 `No-Go`，不能签收“入口唯一”。

### 23.4 动作闭环

插件产生用户可见 Telegram 操作时，最终版必须满足：

- 成功动作写 `event_action.status=success`。
- 客观不可执行动作写 `event_action.status=failed` 和稳定 `error_code` / `reason_code`。
- 被策略跳过动作写 `event_action.status=skipped`。
- 任何 action 不允许静默 return。
- 任何直接调用 Telegram driver 的代码都必须位于 Delivery Executor、MessageOps 或命令包装器等受控出口内，并记录 action。
- 插件上下文中不再暴露可直接发送用户可见消息的 live Telegram client；历史 `ctx.client`、命令包装传入的 `client`、交互入口传入的 `client` 必须替换为 trace-aware facade，或者只能提供只读/非发送能力。
- 内置插件、官方示例和迁移层如果仍调用 `send_message`、`edit_message`、`delete_messages`、`pin_message`、`answer_inline_query` 等 Telegram 方法，必须证明这些方法最终落入 `record_action + Delivery/MessageOps`，否则就是动作门禁 No-Go。

需要重点审计的动作失败分支：

| 场景 | 必须 reason_code |
| --- | --- |
| 空消息文本 | `empty_message_text` |
| media payload 缺失 | `media_payload_missing` |
| media payload 非法 | `media_payload_invalid` |
| callback query id 缺失 | `callback_query_id_missing` |
| inline query id 缺失 | `inline_query_id_missing` |
| 编辑/删除/置顶目标消息缺失 | `target_message_id_missing` |
| 旧发送通道 | `send_channel_deprecated` |
| 普通 Bot 请求转账 | `settlement_channel_not_supported` 或等价稳定 code |
| Telegram API 失败 | `telegram_api_error` |
| UserBot 不在线 | `userbot_offline` |
| Bot token 缺失 | `bot_token_missing` |

### 23.5 日志最终信息架构

日志中心必须围绕 trace，而不是围绕旧日志表。最终版页面至少包含以下排障入口：

| 入口 | 用户要解决的问题 | 必须展示 |
| --- | --- | --- |
| 总览 | 系统是否健康 | worker、UserBot、交互 Bot、DB、Redis、最近错误、最近失败动作 |
| 消息链路 | 某条消息走到哪一步 | receive、normalize、matched/skipped、plugin invoke、delivery、finish |
| 插件诊断 | 插件为什么没启动或卡住 | 安装、启用、加载、manifest、最近调用、最近异常、last_trace_id |
| 命令链路 | 命令调用了什么 | 权限、处理器、插件入口、回复/编辑/删除动作 |
| 动作发送 | 谁最终发了什么 | requested_send_via、actual_send_via、Telegram message id、失败 reason |
| 原始日志 | 新 Trace 降级时如何补查 | runtime/audit fallback，并明确这是高级排障 |

日志页验收不得只看页面能打开。必须覆盖成功、未命中、插件失败、动作失败、旧 notice 失败、native_raw 下发/未下发、窄屏/PWA 六类状态。

### 23.6 最终版可并行任务合同

如果使用多个子 Agent，并行只能按以下任务合同拆分。每张卡都必须独立可 review，不得跨范围重构。

| 卡 | 可并行范围 | 必须交付 | 禁区 |
| --- | --- | --- | --- |
| F1 契约注册表 | reason_code、event type、send_via、capabilities、settings | 后端常量、前端映射、文档表、测试同步 | 不改业务分发语义 |
| F2 Source Adapter 与 Event Bus | UserBot、管理员命令、交互 Bot、callback、inline、payment、session_close 标准化和投递 | message/command/callback/inline/payment/session_close 测试、matched/skipped/delivered decision trace | 不改前端样式，不把回滚开关当常态路径 |
| F3 MessageOps 与 Delivery Executor | action 执行、失败落库、旧通道失败、live client 收口 | action 成功/失败测试、旧 notice fixture、trace-aware facade 证据 | 不恢复旧 notice，不向插件暴露可直发 live client |
| F4 Contract Guard 与插件状态 | 越声明告警、客观失败、PluginRuntimeStatus | 规范警告、trace span、运行状态测试 | 不把 Guard 改回强沙箱 |
| F5 日志中心 UI | `/logs`、Trace API 展示、reason 中文 | 桌面和窄屏验收、失败态、原始日志 fallback | 不创造后端不存在字段 |
| F6 交互/插件/设置 UI | `/interaction`、`/plugins`、配置页、`/settings` | usage 警告、能力风险、订阅展示、回滚设置 | 不把旧账号详情页作为唯一配置入口 |
| F7 示例和开发文档 | README、插件文档、示例、验证脚本 | 开发者照文档可写五类插件 | 不把旧 payload 写成推荐路径 |
| F8 发布证据 | 版本、CHANGELOG、证据台账、最终报告 | 当前 commit 的验证命令和页面验收记录 | 不沿用历史通过记录 |
| F9 部署和回滚 | 服务器、备份、迁移、健康检查、真实 trace | 远端 commit、版本、docker 状态、回滚开关演练 | 没备份不改远端 |

合并顺序固定为：F1 -> F2/F3/F4 -> F5/F6 -> F7 -> F8 -> F9。
F5/F6 可以先做视觉和结构，但最终验收必须等 F1-F4 字段稳定后重跑。

### 23.7 最终 review 程序

进入最终 review 时，主 Agent 必须按以下顺序审查，而不是只看测试通过：

1. **契约审查**：事件类型、payload 字段、action、send_via、reason_code、capabilities、settings 是否在后端、前端、文档一致。
2. **入口审查**：所有 Telegram 来源是否经 Source Adapter 和 Event Bus；旧 helper 是否只作为适配层；测试是否断言 decision，而不是只断言 trace/span。
3. **动作审查**：所有用户可见操作是否落 `event_action`；失败分支是否有稳定 code；`ctx.client` / 命令 `client` 是否已收口到 trace-aware facade。
4. **风险审查**：`native_raw`、inline_all、跨通道发送、settlement 是否有声明、风险提示和 trace。
5. **文档审查**：README 和插件开发文档是否仍推荐旧平铺 payload、旧 notice、旧 runtime log 主路径。
6. **UI 审查**：日志中心和交互中心是否能排障，不只是展示数据。
7. **部署审查**：版本、CHANGELOG、commit、远端版本、迁移、健康检查和回滚证据是否一致。

最低审计命令：

```bash
rg -n "notice|bbot_notice|notice_bot|raw_event|payload\\[\"text\"\\]|event\\.reply|event\\.respond|ctx\\.client|旧规则驱动" README.md docs examples scripts backend frontend
rg -n "send_via|channel_selector|interaction_bot|userbot_reply|auto|settlement|answer_inline_query|answer_callback" backend frontend docs examples
rg -n "native_raw|telegram_native_raw|inline_all|reason_code|send_channel_deprecated" backend frontend docs examples scripts
rg -n "send_message|send_file|send_photo|edit_message|delete_message|pin_message|answer_inline" backend/app
```

这些命令的命中不是自动失败，但必须分类。任何命中属于“新插件推荐旧写法”“旧通道可执行”“绕过能力声明拿 raw”“直接发送但不落 action”，就是 No-Go。

### 23.8 证据台账字段

`docs/release/<version>-final-evidence.md` 必须成为最终版唯一事实来源。每条证据至少包含：

| 字段 | 要求 |
| --- | --- |
| 控制项 | 对应六个闭环或 F1-F9 任务卡 |
| 状态 | `未开始`、`半落地`、`可测`、`已完成` |
| commit | 证据对应的本地或远端 commit |
| 命令/页面 | 自动命令、API、页面 URL 或真实 trace_id |
| 结果 | 通过、失败、跳过原因或替代证据 |
| 剩余风险 | 只允许写第 19.25 节允许残余风险 |
| 复验时间 | 记录本次证据采集时间 |

没有 commit、命令/页面、结果的条目，不能标为 `已完成`。
工作区发生相关代码改动后，旧证据必须降级为历史基线，直到重新验证。

### 23.9 服务器实测与回滚底线

最终版部署到 `144.24.5.159` 时必须按以下底线执行：

1. 部署前备份 `.env`、compose 文件、数据库，记录路径。
2. 部署前记录远端 commit、版本、容器状态。
3. 部署当前分支，不覆盖 `main`。
4. 执行迁移并验证 Alembic head。
5. 验证 `/healthz`、首页版本、日志中心、交互中心、插件中心、设置页。
6. 触发或导入至少一条普通消息 trace、一条插件 action trace、一条失败 action trace。
7. 演练 `trace_enabled=false`、`event_bus_delivery_enabled=false`、`inline_updates_enabled=false`，再恢复默认。
8. 保存最终远端 commit、版本、健康检查、docker 日志摘要和回滚演练结果。

如果 SSH、服务器、数据库或真实 Telegram 场景不可用，最终结论只能是 `可测 / 待服务器实测`。可以 push 分支给用户测试，但不得写 `Go / 已完成`。

### 23.10 最终版含义

按本计划完成后，可以宣称：

- TelePilot 已完成个人可信插件标准下的统一 Telegram Event Bus。
- 插件可以用统一事件信封处理 message、command、callback、inline、payment。
- 插件可以通过 MessageOps / action 选择 `interaction_bot`、`userbot_reply` 或 `auto`。
- 转账、发奖、结算仍由 userbot 或 settlement 承接，普通 Bot 不被伪装成转账主体。
- 日志中心能从消息、插件、命令、动作四个角度排查链路。
- 开发者可以按新版插件开发指南直接写出符合新框架的插件。

不能宣称：

- 所有第三方插件都已自动迁移。
- 所有 Telegram update 类型和 Bot API 方法都已封装。
- 平台会替用户承担远程插件风险。
- 关闭 Trace 后仍有同等详细的排障能力。
- 旧 `notice` / `bbot_notice` / `notice_bot` 仍可作为发送通道使用。

这就是“最终版”的封口定义：核心框架、主路径、日志排障、开发文档、部署回滚全部闭环；非主链路能力可以后续扩展，但不能影响当前最终版框架成立。

### 23.11 最终版签收清单

执行完成前，主 Agent 必须逐条签收下表。任何一项没有证据，都不得写 `Go / 已完成`。

| 签收项 | 必须证明 | 允许的证据 |
| --- | --- | --- |
| 1. 契约一致 | event type、payload、action、send_via、reason_code、capabilities、settings 在后端、前端、文档一致 | 枚举 grep、类型检查、文档审计记录 |
| 2. 入口唯一 | UserBot、交互 Bot、callback、inline、chosen inline、payment、session_close 都经 Source Adapter / Event Bus / Trace | 单测、trace_id、日志页详情 |
| 3. 插件可开发 | 新插件只读标准事件信封、`event_subscriptions`、`capabilities`、`ctx.messages` 即可完成五类插件 | 示例插件验证、开发指南复制运行 |
| 4. 动作可追踪 | 发送、编辑、删除、置顶、callback ACK、inline answer、settlement、end_session 成功失败都落 `event_action` | action 测试、失败 fixture、日志页动作详情 |
| 5. 风险可见 | `native_raw`、`inline_all`、跨通道发送、转账/发奖、越声明调用都有风险提示和 Trace | WebUI、Contract Guard span、规范警告 |
| 6. 旧通道收口 | `notice` / `bbot_notice` / `notice_bot` 不能发送，只能产生 `send_channel_deprecated` | manifest lint、运行时测试、旧 notice fixture |
| 7. 日志能排障 | 能从消息、插件、命令、动作四个入口解释未触发、加载失败、插件异常、动作失败 | `/logs` 桌面和窄屏/PWA 验收记录 |
| 8. UI 能操作 | `/interaction`、`/plugins`、插件配置、仓库管理、`/settings` 不再要求用户回到旧账号详情页完成主配置 | 浏览器验收 URL、视口、通过标准 |
| 9. 发布材料一致 | 版本文件、中文 CHANGELOG、证据台账、commit、push 分支和实际 diff 一致 | git commit、远端分支、版本 grep |
| 10. 服务器可退 | `144.24.5.159` 已备份、部署、迁移、健康检查、真实 trace、回滚开关演练 | 备份路径、远端 commit、健康检查、docker 日志、回滚记录 |

签收顺序固定为 1 -> 4 -> 7 -> 8 -> 9 -> 10。第 2、3、5、6 项可与第 1-4 项并行验证，但必须在第 9 项发布材料定稿前完成。

### 23.12 最终版执行出口

本计划执行到末尾时，只允许三种出口：

| 出口 | 使用条件 | 允许动作 |
| --- | --- | --- |
| `Go / 已完成` | 第 23.11 节十项全部有当前 commit 的证据，远端部署和回滚演练完成 | 向用户宣称最终版框架落地，推送分支，保留证据台账 |
| `可测 / 待服务器实测` | 自动验证、文档审计、本地/浏览器验收已过，但缺远端部署、真实 Telegram trace 或线上回滚证据 | 推送分支给用户实测，但不能宣称服务器最终版已完成 |
| `No-Go / 阻塞` | 任一硬门禁失败，或自动验证失败且没有替代证据 | 停止发布，回到对应 F/R 任务卡修复，再重新跑最终验证 |

最终报告必须引用 `docs/release/<version>-final-evidence.md` 中的证据，不允许只引用聊天记录、子 Agent 自述或历史测试结果。

### 23.13 当前分支最终版剩余缺口清单

本节是从当前 `0.40.x` 工作分支继续执行时的最后任务面。它不新增愿景，只把已经暴露的半落地风险变成必须关掉的工程缺口。执行者可以并行处理，但每一项都必须回填到第 23.11 节签收清单和 `docs/release/<version>-final-evidence.md`。

| 缺口 | 关联签收项 | 必须补齐 | 必须验收 |
| --- | --- | --- | --- |
| UserBot/管理员命令仍可绕过 Event Bus | 2、3、7 | UserBot 普通消息、管理员命令、插件命令都先标准化为 `TelePilotEvent`，由 `dispatch_event` 产生 decision 后再调用 `on_event` 或兼容入口；`event_bus_delivery_enabled=false` 只能作为降级状态 | `test_worker_command.py`、`test_plugin_loader.py` 断言命令和普通消息存在 Event Bus decision；关闭开关时日志页/证据台账显示 degraded，不能签收入口唯一 |
| 交互 Bot 旧规则、付款确认、`session_close` 仍可直调插件 | 2、4、7 | 旧规则迁移为虚拟 subscription 或 rule-bound subscription；`payment_confirmed`、确认回调、`session_close` 都生成标准事件并经过 matcher；禁止手工伪造 `matched` span | `test_account_bot.py` 覆盖关键词、callback、payment confirm、session_close 的 `dispatch_event` decision、plugin invoke、action/skip |
| 插件仍能拿 live client 直发消息 | 1、4、7 | `ctx.client`、命令参数 `client`、交互入口 client 全部变为 trace-aware facade 或只读对象；内置插件迁到 `ctx.messages` 或标准 action；直接 Telegram 调用点必须分类并收口 | `rg` 直接发送点分类；`test_plugin_loader.py` / `test_worker_command.py` 断言直发也落 `event_action`；内置插件目标测试通过 |
| `session_close` 不是独立 trace | 2、7 | 关闭会话、规则停用、超时清理必须产生独立 `event_type=session_close` trace，并记录 subscription、plugin invoke、action/skip | 后端测试断言 `start_trace(event_type=session_close)`、日志页可搜索该 trace |
| 动作出口仍可能绕过 `event_action` | 1、4、7 | 所有 `send_message`、`edit_message`、`delete_message`、`pin_message`、`answer_callback`、`answer_inline_query`、`settlement` 都只能通过受控出口记录成功/失败/跳过 | `rg` 直接发送点分类；动作成功和失败测试；日志页动作详情 |
| 删除/置顶等动作通道语义不稳定 | 1、4、7 | `delete_message`、`pin_message`、`edit_message` 对 `interaction_bot`、`userbot_reply`、`auto` 的支持范围必须明确；不支持组合在 Guard 或 Delivery 阶段写稳定 `unsupported_send_via` / `bot_token_missing` / `userbot_offline` / `telegram_api_error` | Delivery/Contract 单测覆盖支持和不支持组合；失败动作不只写模糊 `no supported send_via` |
| 测试只证明“有 trace”，没有证明“经过 Event Bus” | 2、4、7、9 | 命令、普通消息、旧规则、payment、session_close、inline 的测试必须断言 `dispatch_event`、decision reason_code、plugin invoke 和 event_action；旧旁路仍存在时测试必须失败 | 新增/更新目标测试；证据台账不得把仅有 trace/span 的旧测试当入口闭环证据 |
| 编辑消息与占位消息语义混淆 | 1、4 | `edit_message` 必须是独立 action，不再伪装成 `send_message + edit_message_id`；占位消息更新仍可作为兼容策略，但 trace 要写清实际动作 | MessageOps 单测、Delivery 单测、UserBot runtime 单测 |
| 旧 `notice` 概念残留 | 5、6 | `notice` / `bbot_notice` / `notice_bot` 只保留为废弃迁移错误；外部转账通知 Bot 只能作为 `external_payment_notice` 消息来源 | manifest lint、旧 notice fixture、插件开发文档 grep 分类 |
| `native_raw` 边界证据不足 | 1、5、7 | 默认只下发标准信封和 `native_raw_meta`；只有声明 `capabilities.telegram_native_raw.enabled=true` 的插件能拿到 `native_raw` | 声明/未声明两组 fixture，日志页展示下发状态、大小和持久化状态 |
| 日志页不能一眼排障 | 7 | `/logs` 必须显示系统健康、消息链路、插件状态、命令链路、动作失败、reason_code 中文、原始日志 fallback | 桌面和窄屏/PWA 浏览器验收：成功、未触发、插件失败、动作失败、旧 notice 失败 |
| 交互中心仍像旧规则页 | 8 | `/interaction` 以账号 / 交互 Bot / Event Bus 订阅为主入口，旧规则只是订阅配置表达；保留账号详情跳转但不要求钻详情页 | `/interaction?aid=1` 桌面和窄屏验收，新增/编辑/保存/日志跳转可用 |
| 插件页缺日志跳转和风险闭环 | 5、7、8 | 插件中心、插件配置页、规范警告、最近错误都能跳到日志页，并带 `account_id`、`plugin_key`、`status` 等筛选参数 | `/plugins`、插件配置页浏览器验收；失败插件可一键定位日志 |
| 开发文档仍混入旧主路径 | 3、6、9 | README、插件开发指南、API 参考、AI/HTTP 示例、examples README 不再把 `event.edit`、旧平铺 payload、旧 `notice` 写成推荐路径 | 文档 grep 分类；示例验证脚本通过；旧写法只能标为管理员命令兼容或迁移说明 |
| 发布证据缺第 23.11 节映射 | 9、10 | 证据台账必须同时有 F1-F9 控制板和 10 项签收清单；每项写 commit、命令/页面、结果、剩余风险、复验时间 | 当前版本证据台账可直接支撑 Go / 可测 / No-Go 结论 |
| 远端部署和回滚未实测 | 10 | 部署到 `144.24.5.159` 前备份，部署后验证版本、健康、迁移、docker logs、真实 trace、回滚开关 | 远端备份路径、commit、版本、健康检查、trace_id、回滚演练记录 |

这些缺口处理完之前，当前分支最多只能被描述为“可测候选”。如果某个缺口已经通过代码实现解决，也必须补齐当前 commit 上的测试或页面证据后才能标为 `已完成`。

### 23.14 最终版执行波次

为了保证“按计划后可以实现最终版”，后续执行固定使用以下波次。每个波次结束后系统都必须可运行，不能依赖后续波次才能恢复基本功能。

| 波次 | 目标 | 可并行任务 | 出口条件 |
| --- | --- | --- | --- |
| W0 冻结事实 | 锁定分支、版本、工作树、未跟踪文件、当前证据状态 | Evidence Agent 可只读整理台账 | 证据台账写明所有控制项当前状态，没有假 `已完成` |
| W1 运行时封口 | 补齐 Event Bus、`session_close`、MessageOps、Delivery、Contract Guard、reason_code | Runtime Agent、Contract Agent | 后端 ruff、目标测试、动作失败测试、旧 notice fixture 通过 |
| W2 前端和文档封口 | 日志中心、交互中心、插件中心、设置页、开发文档与示例同步最终契约 | UI Agent、Docs Agent | Typecheck/build 通过，文档 grep 已分类，示例验证通过 |
| W3 全量验证 | 在同一工作区和当前 commit 上重跑自动验证、浏览器验收、迁移 SQL | 主 Agent 统筹，子 Agent 只读复核 | F1-F8 至少达到 `可测`，无 No-Go 命中 |
| W4 发布检查点 | 版本、CHANGELOG、commit、push 当前分支，不覆盖 main | 主 Agent | 中文 commit，远端分支存在，证据台账写入 commit |
| W5 服务器实测 | 备份、部署、迁移、健康检查、真实 trace、回滚开关 | Deploy Agent 或主 Agent | F9 和回滚演练有证据；否则结论保持 `可测 / 待服务器实测` |
| W6 最终报告 | 给用户和朋友看的落地报告 | 主 Agent | 报告只引用证据台账，不引用聊天记忆 |

W1 和 W2 可以部分并行，但 W2 的最终验收必须等待 W1 公共字段稳定。W4 之前不得把历史测试写成当前通过；W5 之前不得宣称服务器已经落地。

### 23.15 最终版 Definition of Done

最终版不是“所有想象中的能力都做完”，而是以下 Definition of Done 全部成立：

- **代码层**：所有 Telegram 入口进入标准事件信封；所有插件可见动作进入 `event_action`；所有客观失败有稳定 reason_code。
- **插件层**：开发者只看新版开发指南和 `examples/plugins/event_bus_demo`，即可写出 message、command、callback、inline、payment 插件；旧插件迁移路径清楚。
- **风险层**：平台不替用户承担远程插件风险，但必须把 `native_raw`、inline、跨通道发送、转账/发奖、越声明调用以告警、trace 或规范警告形式显示出来。
- **UI 层**：用户打开日志页能判断系统是否健康、消息到哪一步、插件为何未启动、动作为何失败；打开交互页能直接配置订阅和入口，不必钻账号详情。
- **文档层**：README、插件开发文档、API 参考、示例 README 与当前代码一致，旧 `notice`、旧平铺 payload、旧 `raw_event`、旧规则驱动没有被写成推荐主路径。
- **发布层**：版本文件、中文 CHANGELOG、commit、远端分支、证据台账一致。
- **线上层**：`144.24.5.159` 有备份、部署、迁移、健康检查、真实 trace 和回滚开关演练证据。

只要以上任一层缺失，最终结论就不能写 `Go / 已完成`。对应层如果代码已实现但缺证据，应写 `可测`；如果实现本身不满足硬门禁，应写 `No-Go / 阻塞`。

### 23.16 最终版施工地图

为了让后续执行不再重新解释“最终版要改哪里”，封版执行时按下表施工。表中的文件是本计划允许直接修改的主要落点；如果执行中发现必须修改表外文件，先在证据台账写明原因和影响，再继续。

| 施工面 | 主要文件 | 必须完成的行为 | 验收证据 |
| --- | --- | --- | --- |
| Event Bus 契约 | `backend/app/services/event_bus.py`、`backend/app/worker/plugins/base.py`、`frontend/src/api/types.ts`、插件开发文档 | 事件信封、来源、动作、reason_code、capabilities 字段前后端一致 | 枚举 grep、后端契约测试、前端类型检查 |
| UserBot 投递 | `backend/app/worker/runtime.py`、`backend/app/worker/plugins/loader.py`、`backend/app/worker/command.py` | UserBot 普通消息、管理员命令、付款确认都进入 Event Bus；legacy `on_message` / plugin command 只由 decision 驱动 | UserBot Event Bus 测试、命令 decision 测试、关闭回滚开关的 degraded 证据 |
| 交互 Bot 投递 | `backend/app/services/account_bot_runtime.py`、`backend/app/services/interaction/contracts.py` | 消息、callback、payment_confirmed、session_close、旧规则适配都进入 Event Bus / Trace，不再形成第二套主路径 | 交互 Bot 测试、payment/session_close decision 测试 |
| MessageOps / Delivery | `backend/app/worker/plugins/message_ops.py`、`backend/app/services/interaction/delivery.py` | 所有用户可见动作都有 `event_action`，失败动作有稳定 code；历史 live client 替换为 trace-aware facade | action 成功/失败测试、旧 notice fixture、直接发送点分类 |
| Contract Guard / 插件状态 | `backend/app/worker/plugins/loader.py`、`backend/app/worker/runtime.py`、`backend/app/services/interaction/contracts.py` | 越声明调用写 warning，废弃或不可执行能力写 failed，PluginRuntimeStatus 可查 | loader 测试、规范警告、日志页插件诊断 |
| 日志 API | `backend/app/api/logs.py`、Trace 相关模型和测试 | API 能按 trace、plugin、command、action、reason_code 聚合排障信息 | `test_logs_api.py`、真实/fixture trace 查询 |
| 日志中心 UI | `frontend/src/pages/Logs.tsx`、`frontend/src/api/types.ts` | 总览、消息链路、插件诊断、命令链路、动作发送、原始日志 fallback 可用 | 桌面和窄屏/PWA 浏览器验收 |
| 交互中心 UI | `frontend/src/pages/Interaction/Index.tsx`、账号详情交互入口 | 账号/交互 Bot 选择、订阅/规则配置、状态、最近触发、最近错误、日志跳转可用 | `/interaction?aid=1` 桌面和窄屏验收 |
| 插件中心和配置 UI | `frontend/src/pages/Plugins/*`、`frontend/src/pages/Settings/Index.tsx` | usage 必填警告、风险提示、仓库刷新/更新、日志跳转、回滚开关可用 | `/plugins`、插件配置页、`/settings` 浏览器验收 |
| 示例和文档 | `examples/plugins/event_bus_demo`、`docs/PLUGIN-*.md`、`README.md`、`scripts/validate-plugin-examples.py` | 开发者只看新版文档即可写 message、command、callback、inline、payment 插件 | 示例验证、文档 grep 分类 |
| 发布证据 | `CHANGELOG.md`、四处版本文件、`docs/release/<version>-final-evidence.md` | 中文更新日志、版本同步、证据台账、commit/push 一致 | 版本 grep、git commit、远端分支 |
| 服务器 | 部署脚本、远端 compose、数据库、日志 | `144.24.5.159` 备份、部署、迁移、健康检查、真实 trace、回滚演练 | 备份路径、远端 commit、health、docker logs、trace_id |

执行时每个施工面都必须满足“实现、测试、文档、证据”四件套。只做实现不补证据，状态只能是 `半落地` 或 `可测`。

### 23.17 最终版硬门禁

下面这些门禁是封版时的停止线。命中任意一条，当前波次必须停下修复，不能靠说明绕过。

| 门禁 | No-Go 条件 | 修复后必须重跑 |
| --- | --- | --- |
| 契约门禁 | 后端、前端、文档对 event type、action、send_via、reason_code 的集合不一致 | 契约 grep、后端目标测试、前端类型检查、文档审计 |
| 入口门禁 | 任一 Telegram 来源绕过 Source Adapter / Event Bus / Trace 直接调用插件 | 对应入口单测、trace 查询、日志页详情 |
| 动作门禁 | 用户可见发送、编辑、删除、置顶、callback ACK、inline answer、settlement 任一分支不落 `event_action` | action 成功/失败测试、直接发送点 `rg` 分类 |
| Client 门禁 | 插件可通过 `ctx.client`、命令参数 `client` 或交互入口 client 直接调用 Telegram 发送/编辑/删除/置顶且不落 `event_action` | trace-aware facade 测试、内置插件迁移测试、直接发送点分类 |
| 测试门禁 | 目标测试只断言 trace/span 存在，没有断言 Event Bus decision、plugin invoke 和 action | 更新测试断言并重跑目标测试 |
| 旧通道门禁 | `notice` / `bbot_notice` / `notice_bot` 被作为发送通道执行，或文档推荐继续使用 | 旧 notice fixture、manifest lint、文档 grep |
| 原生数据门禁 | 未声明 `telegram_native_raw` 的插件拿到完整 `native_raw` | native_raw 声明/未声明两组 fixture |
| 日志门禁 | 日志页不能解释“未触发、插件未启动、插件异常、动作失败”至少一种核心场景 | `/logs` 桌面和窄屏验收、Trace API 查询 |
| UI 门禁 | 交互中心仍要求用户回账号详情才能完成主配置，或关键按钮/列表窄屏不可用 | `/interaction`、`/plugins`、配置页、`/settings` 浏览器验收 |
| 文档门禁 | 新版开发指南仍把旧平铺 payload、旧 `event.reply/respond`、旧规则驱动写成公共玩法推荐路径 | 文档 grep 分类、示例验证 |
| 发布门禁 | 版本、CHANGELOG、证据台账、commit 实际 diff 不一致 | 版本 grep、`git diff --stat`、中文 CHANGELOG 审查 |
| 线上门禁 | 远端没有备份、部署后健康检查失败、无真实 trace、回滚开关不可恢复 | 远端备份、health、docker logs、回滚演练 |

这些门禁不用于扩大范围；它们只判断本计划定义的最终版是否成立。

### 23.18 子 Agent 接单模板

任务多时可以并行，但每个子 Agent 必须按下面的固定任务卡接单。主 Agent 负责最终集成和证据台账，不把子 Agent 自述当作完成事实。

| 任务卡 | 写入范围 | 禁止范围 | 验收命令或页面 | 交付物 |
| --- | --- | --- | --- | --- |
| F1 契约注册表 | `backend/app/services/event_bus.py`、`backend/app/worker/plugins/base.py`、`frontend/src/api/types.ts`、`docs/PLUGIN-API-REFERENCE.md`、`docs/PLUGIN-DEV-GUIDE.md` | 不改业务分发语义，不改部署文件 | `rg -n "reason_code|send_via|capabilities|telegram_native_raw|inline_all" backend frontend docs examples scripts`；`cd frontend && ./node_modules/.bin/tsc -b --pretty false`；`cd backend && .venv/bin/pytest -q app/tests/test_event_bus.py app/tests/test_logs_api.py` | 字段一致性结论、变更文件、命令结果、No-Go 命中情况 |
| F2 Source Adapter 与 Event Bus | `backend/app/worker/runtime.py`、`backend/app/worker/plugins/loader.py`、`backend/app/worker/command.py`、`backend/app/services/account_bot_runtime.py`、相关后端测试 | 不改前端样式，不改版本号；不把 `event_bus_delivery_enabled=false` 当常态路径 | `cd backend && .venv/bin/pytest -q app/tests/test_event_bus.py app/tests/test_worker_command.py app/tests/test_account_bot.py` | message/command/callback/inline/payment/session_close 投递证据、decision reason_code、剩余入口风险 |
| F3 MessageOps 与 Delivery Executor | `backend/app/worker/plugins/message_ops.py`、`backend/app/services/interaction/delivery.py`、`backend/app/services/interaction/contracts.py`、`backend/app/worker/plugins/base.py`、动作相关测试 | 不恢复旧 `notice` 发送，不把失败动作吞掉，不向插件暴露可直发的 live client | `cd backend && .venv/bin/pytest -q app/tests/test_account_bot.py app/tests/test_worker_command.py app/tests/test_plugin_loader.py`；`rg -n "ctx\\.client|send_message|send_file|send_photo|edit_message|delete_message|pin_message|answer_inline" backend/app` | 动作成功/失败覆盖、trace-aware facade 证据、直接发送点分类、旧通道失败证据 |
| F4 Contract Guard 与插件状态 | `backend/app/worker/plugins/loader.py`、`backend/app/worker/runtime.py`、`backend/app/services/interaction/contracts.py`、`scripts/validate-installed-interaction-plugins.py` | 不改成强沙箱，不删除用户插件 | `backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py`；`cd backend && .venv/bin/pytest -q app/tests/test_plugin_loader.py app/tests/test_logs_api.py` | warning/failed 行为说明、第三方插件残余 warning 分类 |
| F5 日志中心 UI | `frontend/src/pages/Logs.tsx`、`frontend/src/api/types.ts`、日志相关组件 | 不创造后端不存在字段，不改 runtime 语义 | `cd frontend && ./node_modules/.bin/tsc -b --pretty false`；`cd frontend && ./node_modules/.bin/vite build`；浏览器验收 `/logs` 桌面和窄屏/PWA | 页面 URL、视口、成功/未触发/插件失败/动作失败/旧 notice 失败验收记录 |
| F6 交互/插件/设置 UI | `frontend/src/pages/Interaction/Index.tsx`、`frontend/src/pages/Plugins/*`、`frontend/src/pages/Settings/Index.tsx`、账号详情交互入口 | 不把账号详情页恢复成唯一主入口，不绕过 API 类型 | `cd frontend && ./node_modules/.bin/tsc -b --pretty false`；`cd frontend && ./node_modules/.bin/vite build`；浏览器验收 `/interaction?aid=1`、`/plugins`、插件配置页、`/settings` | 主配置可用性、风险提示、日志跳转、窄屏/PWA 记录 |
| F7 示例和开发文档 | `README.md`、`docs/PLUGIN-*.md`、`examples/plugins/*`、`scripts/validate-plugin-examples.py` | 不把旧 payload、旧 `event.reply/respond`、旧 `notice` 写成推荐路径 | `backend/.venv/bin/python scripts/validate-plugin-examples.py`；`rg -n "notice|bbot_notice|notice_bot|raw_event|payload\\[\"text\"\\]|event\\.reply|event\\.respond|ctx\\.client|旧规则驱动" README.md docs examples scripts backend frontend` | grep 分类表、示例验证结果、开发者五场景覆盖说明 |
| F8 发布证据 | `CHANGELOG.md`、四处版本文件、`docs/release/<version>-final-evidence.md` | 不部署服务器，不覆盖 main | `rg -n "__version__|version" backend/app/__init__.py backend/pyproject.toml frontend/package.json frontend/src/lib/version.ts`；`git diff --check`；`git diff --stat` | 中文 CHANGELOG、版本一致性、证据台账、commit/push 准备结论 |
| F9 部署和回滚 | 远端部署记录、证据台账、部署脚本中确需调整的文件 | 没有备份不改远端，不在 main 上部署，不隐藏失败健康检查 | 远端 `docker compose ps`、`docker compose logs --tail=100`、`curl -fsS http://127.0.0.1:8000/healthz`、Alembic head、回滚开关演练 | 备份路径、远端 commit、版本、健康检查、docker 摘要、trace_id、回滚记录 |

每张任务卡还必须遵守四条共同规则：重新读取当前文件，不按旧行号改；不 revert、checkout、reset 未负责改动；不创造计划外公共契约；最终交付必须说明命中或未命中 No-Go。

可并行规则：

- F1 契约注册表必须先完成或至少冻结字段清单，F2-F6 才能最终验收。
- F2、F3、F4 可以并行，但任何一方改动公共字段后必须通知 F1 重新审计。
- F5、F6 可以并行做 UI，但不得发明 API 字段；缺字段时回到 F1/F2/F4 修后再验收。
- F7 可以并行准备文档，但只能在 F1-F4 稳定后做最终 grep 和示例验证。
- F8、F9 只能由主 Agent 或明确指定的发布/部署 Agent 执行。

### 23.19 开发者最终体验验收

最终版不仅要系统能跑，还要让插件开发者不用靠旧文档和反复试错。开发者验收按下面五个最小插件场景判断：

| 场景 | 开发者只需要依赖 | 必须能完成 |
| --- | --- | --- |
| 管理员命令插件 | `event_subscriptions` 的 `command`、标准事件信封、`ctx.messages` | 管理员带前缀命令触发，后续可由 userbot 回复/编辑，Trace 能查到命令和动作 |
| 群内关键词玩法 | `message` 订阅、关键词 filter、`interaction_bot` action | 玩家关键词启动，普通 Bot 高频互动，userbot 只承接必要结算 |
| 按钮互动玩法 | `callback_query` 订阅、`answer_callback`、`edit_message` | 点击按钮后 ACK、更新消息或发送下一步，失败时日志页能看到 reason_code |
| Inline 插件 | `inline_query` / `chosen_inline_result`、`inline_all` 风险声明、`answer_inline_query` | Inline 查询和选择结果可追踪，可在 UI 看到风险提示 |
| 付款/发奖插件 | `payment_confirmed`、`external_payment_notice`、`settlement` | 第三方转账通知 Bot 只作为到账证据来源，普通 Bot 公告，userbot 或 settlement 执行发奖 |

插件开发文档必须围绕这五个场景组织。旧 `on_message`、旧 `on_command`、`interaction_entries`、旧平铺 payload 可以作为迁移说明出现，但不能作为公共群玩法的新模板。

### 23.20 回滚地图

最终版改造允许用户实测，但必须随时能退回可用状态。回滚按层级处理：

| 层级 | 回滚方式 | 必须保留的证据 |
| --- | --- | --- |
| 功能开关 | 关闭 `event_bus_delivery_enabled`、`inline_updates_enabled`、`native_raw_persist_enabled`，必要时关闭 `trace_enabled` | 开关修改前后值、恢复默认记录 |
| 前端发布 | 回退到上一个镜像或上一个构建产物 | 当前镜像 tag、上一个镜像 tag、页面健康检查 |
| 后端发布 | 回退到部署前 commit 或镜像 | 部署前 commit、部署后 commit、服务日志 |
| 数据库迁移 | 优先用向前兼容字段和保留旧表，不依赖破坏性 downgrade | Alembic head、迁移 SQL、备份文件 |
| 插件生态 | 对未迁移插件显示规范警告，不自动删除用户插件 | 插件 key、版本、warning、用户可见提示 |
| 远端配置 | 部署前备份 `.env`、compose、数据库 | 带时间戳的备份路径、恢复命令或恢复步骤 |

如果某个改动无法通过功能开关回滚，必须在证据台账标为“需要代码回滚”，并在部署前明确给用户。

### 23.21 最终版状态语义

为了避免“完成了一部分就说完成”，后续所有报告统一使用以下状态语义：

| 状态 | 含义 | 可否对用户宣称最终版 |
| --- | --- | --- |
| `未开始` | 没有实现或没有当前证据 | 否 |
| `半落地` | 代码或文档有改动，但测试、页面或证据不完整 | 否 |
| `可测` | 当前 commit 自动验证和本地/浏览器验收已过，但缺远端、真实 trace 或回滚证据 | 可以邀请实测，不能宣称 Go |
| `已完成` | 当前 commit、远端部署、真实 trace、回滚演练和证据台账全部闭环 | 可以宣称最终版完成 |
| `阻塞` | 命中硬门禁，或缺权限/缺环境导致无法安全继续 | 不能发布；必须列 blocker |

最终报告只能在 `Go / 已完成`、`可测 / 待服务器实测`、`No-Go / 阻塞` 三种出口中选择一个。若证据与结论冲突，以证据台账为准。

## 24. 最终版执行版协议

本节是当前 `0.40.x` 封口的唯一执行入口。它不新增愿景，只把前文压缩成“按此执行后可以证明最终版成立”的最短闭环。执行者进入本计划后，应先读本节，再按需要回查前文细节。

### 24.1 最终版定义

本计划所说的“最终版”只表示 TelePilot 插件框架的主路径闭环完成，不表示 Telegram 全量 API 都已封装，也不表示所有第三方插件无需迁移。

最终版必须同时成立：

- **统一入口**：UserBot 消息、管理员命令、交互 Bot 消息、callback、inline、付款通知都先变成标准事件，再进入 Event Bus decision。
- **统一协议**：插件主路径只依赖标准事件信封、`event_subscriptions`、`capabilities` 和 `ctx.messages` / 标准 action。
- **统一出口**：发送、编辑、删除、置顶、callback ACK、inline answer、settlement 都落 `event_action`，失败也必须可查。
- **统一排障**：日志中心能解释消息有没有进入系统、为什么没触发插件、插件为何失败、动作为何失败、最终由谁发送。
- **统一开发体验**：开发者只看新版插件开发指南和示例插件，即可写出 message、command、callback、inline、payment 插件。
- **统一发布证据**：当前 commit、版本号、中文 CHANGELOG、证据台账、远端部署和回滚演练一致。

### 24.2 当前封口目标

当前封口版本按实际版本文件和证据台账执行，例如 `0.40.x` 的当前 patch（补丁版本）。所有报告、验证、部署和 CHANGELOG 必须绑定当前版本，不能继续引用旧的 `0.40.0` 作为完成证据。

封口目标不是“继续讨论架构”，而是把当前状态从半落地收口为以下三种出口之一：

| 出口 | 条件 | 允许动作 |
| --- | --- | --- |
| `Go / 已完成` | 第 24.6 节十项签收全部有当前 commit 证据，远端部署和回滚演练完成 | 可以宣称最终版框架落地 |
| `可测 / 待服务器实测` | 自动验证、文档审计、本地/浏览器验收通过，但缺远端真实 trace 或回滚演练 | 可以 push 分支给用户实测 |
| `No-Go / 阻塞` | 任一硬门禁失败，或验证失败且没有替代证据 | 停止发布，回到对应任务卡修复 |

不得使用“基本完成”“应该可以”“后续再补”作为最终状态。

### 24.3 执行顺序

后续按以下顺序执行。可以并行写代码，但合并、签收和发布不能跳序。

1. **冻结事实**
   - 记录分支、HEAD、远端跟踪分支、工作树、未跟踪文件。
   - 更新 `docs/release/<当前版本>-final-evidence.md`，所有控制项先按证据标为 `未开始`、`半落地`、`可测` 或 `已完成`。
   - 历史测试只能写“历史基线”，不能写成当前通过。

2. **冻结公共契约**
   - 确认事件类型、`send_via`、action、capabilities、reason_code、日志 API、前端类型、开发文档一致。
   - 新增字段必须同步后端常量、前端中文映射、验证脚本、示例和文档。

3. **关闭运行时硬缺口**
   - UserBot 普通消息、管理员命令、交互 Bot 消息、callback、inline、payment、`session_close` 必须有 Event Bus decision。
   - 旧 `on_message`、旧 `interaction_entries`、命令兼容层只能由 decision 驱动，不能形成第二套主路径。
   - `ctx.client`、命令参数 client、交互入口 client 的发送/编辑/删除/置顶必须被 trace-aware facade 捕获并落 `event_action`。

4. **关闭动作和风险缺口**
   - 所有 action 成功、失败、跳过都记录 `event_action`。
   - 旧 `notice` / `bbot_notice` / `notice_bot` 一律 `send_channel_deprecated`，不得自动改写为可执行通道。
   - 未声明 `telegram_native_raw` 的插件不得拿到完整 `native_raw`。
   - 普通 Bot 不得执行转账/发奖，必须返回 `settlement_requires_userbot` 或走 userbot/settlement。

5. **关闭 UI 和文档缺口**
   - `/logs` 能按 trace、message、plugin、command、action 排障，并展示 reason_code 中文说明和原始日志 fallback。
   - `/interaction` 是真正交互中心，不要求用户钻账号详情完成主配置。
   - `/plugins`、插件配置页、仓库管理和 `/settings` 展示 usage、订阅、能力、风险、回滚设置。
   - 插件开发指南不再把旧平铺 payload、旧 `event.reply/respond`、旧规则驱动、旧 notice 写成推荐主路径。

6. **全量验证**
   - 在同一工作区和当前 commit 上跑自动验证、文档审计、前端构建、浏览器桌面和窄屏/PWA 验收。
   - 失败命令必须写第一条可行动错误；未解释失败不能进入发布。

7. **发布检查点**
   - 四处版本号同步，中文 CHANGELOG 只写实际 diff。
   - 中文 commit，push 当前分支，不覆盖 main。
   - 证据台账写入最终 commit、命令、页面和剩余风险。

8. **服务器实测**
   - 部署到 `144.24.5.159` 前备份 `.env`、compose 文件和数据库。
   - 部署后确认远端 commit、版本、迁移、健康检查、docker logs、Trace API。
   - 触发真实或可复现 trace，并演练 `trace_enabled=false`、`event_bus_delivery_enabled=false`、`inline_updates_enabled=false` 后恢复。

### 24.4 可并行任务包

并行只能按包推进，每包有明确写入范围和禁区。

| 包 | 目标 | 主要写入范围 | 出口条件 |
| --- | --- | --- | --- |
| A 契约和后端主链路 | Event Bus、Trace、MessageOps、reason_code、开关真实生效 | `backend/app/services/*`、`backend/app/worker/*`、后端测试 | 五条链路测试通过，所有可见动作落 `event_action` |
| B Contract 和示例 | manifest、示例插件、验证脚本、旧 notice、native_raw 边界 | `backend/app/worker/plugins/*`、`examples/plugins/*`、`scripts/validate-*` | 示例覆盖 message/command/callback/inline/payment/native_raw/旧 notice |
| C 前端验收 | 日志中心、交互中心、插件中心、配置页、设置页 | `frontend/src/pages/*`、前端类型 | typecheck/build 通过，桌面和窄屏验收写入台账 |
| D 文档和证据 | README、插件开发文档、远程插件文档、安全说明、证据台账 | `README.md`、`docs/*`、`docs/release/*` | 文档 grep 已分类，旧主路径不再推荐 |
| E 发布和部署 | 版本、CHANGELOG、commit、push、远端部署、回滚演练 | 版本文件、`CHANGELOG.md`、远端部署记录 | 远端证据齐备；否则只能保持 `可测` |

合并顺序固定为 A -> B -> C -> D -> E。C/D 发现字段不够用时，必须回到 A/B 修契约，不能在前端或文档里发明后端不存在的字段。

### 24.5 硬门禁

以下任一命中，最终状态只能是 `No-Go / 阻塞`：

- 任一 Telegram 主入口绕过 Source Adapter / Event Bus / Trace 直接调用插件。
- 插件可通过 live client 直接发送、编辑、删除、置顶且不落 `event_action`。
- action 失败分支静默返回，没有 `event_action.status=failed` 和稳定 `error_code`。
- 旧 `notice` / `bbot_notice` / `notice_bot` 仍能发送消息，或文档推荐使用。
- 未声明 `telegram_native_raw` 的插件拿到完整 `native_raw`。
- 日志页不能解释“未触发插件”“插件加载失败”“插件运行异常”“动作发送失败”中的任一核心问题。
- 官方/示例插件仍把旧平铺 payload 作为主路径。
- 远程插件安装、刷新、升级会丢失 `usage`、`event_subscriptions` 或 `capabilities`。
- 版本、CHANGELOG、证据台账、commit 和实际 diff 不一致。
- 部署没有备份、健康检查失败、无真实 trace、回滚开关无法恢复。

### 24.6 十项签收清单

最终证据台账必须同时映射以下十项。任一项没有当前 commit 证据，不能写 `Go / 已完成`。

| 签收项 | 必须证据 |
| --- | --- |
| 1. 契约一致 | 后端常量、前端类型、文档、示例、验证脚本的 event type/action/send_via/reason_code 一致 |
| 2. 入口唯一 | message、command、callback、inline、payment、session_close 都有 Event Bus decision |
| 3. 插件可开发 | 示例插件和开发指南覆盖 message、command、callback、inline、payment |
| 4. 动作可追踪 | 发送、编辑、删除、置顶、callback ACK、inline answer、settlement 成功/失败都落 `event_action` |
| 5. 风险可见 | native_raw、inline_all、跨通道、转账/发奖、越声明调用都有 WebUI 或 Trace 提示 |
| 6. 旧通道收口 | `notice` / `bbot_notice` / `notice_bot` 只产生废弃错误，不发送 |
| 7. 日志能排障 | `/logs` 能查成功、未命中、插件失败、动作失败、旧日志 fallback |
| 8. UI 能操作 | `/interaction`、`/plugins`、插件配置页、仓库管理、`/settings` 桌面和窄屏可用 |
| 9. 发布材料一致 | 四处版本号、中文 CHANGELOG、commit、push、证据台账一致 |
| 10. 服务器可退 | 远端备份、部署、迁移、健康检查、真实 trace、回滚开关演练齐备 |

### 24.7 必跑验证

准备宣称最终版前，至少重跑：

```bash
cd backend && .venv/bin/ruff check app ../scripts/validate-plugin-examples.py
cd backend && .venv/bin/pytest -q
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
cd frontend && ./node_modules/.bin/tsc -b --pretty false
cd frontend && ./node_modules/.bin/vite build
cd backend && .venv/bin/alembic heads
cd backend && .venv/bin/alembic upgrade head --sql >/tmp/telepilot-alembic-final.sql
git diff --check
```

同时必须运行文档和旧通道审计：

```bash
rg -n "notice|bbot_notice|notice_bot|raw_event|payload\\[\"text\"\\]|event\\.reply|event\\.respond|ctx\\.client|旧规则驱动" README.md docs examples scripts backend frontend
rg -n "event_subscriptions|capabilities|telegram_native_raw|native_raw_meta|answer_inline_query|chosen_inline_result|settlement|send_channel_deprecated" README.md docs backend frontend examples scripts
```

审计命中不能只删除。必须分类为：废弃说明、迁移说明、历史说明、测试 fixture、内部兼容实现、需要修复。

### 24.8 浏览器验收入口

前端验收必须写入证据台账，不能只说“看过页面”。

| 页面 | URL | 必测内容 |
| --- | --- | --- |
| 首页 | `/` | 版本号、导航、移动端底部栏 |
| 日志中心 | `/logs` | 总览、筛选、trace 详情、插件诊断、动作详情、错误态、原始日志 |
| 交互中心 | `/interaction?aid=1` | 账号/Bot 选择、规则列表、规则详情、Event Bus 映射、最近错误 |
| 插件中心 | `/plugins` | usage 警告、订阅/能力展示、长插件名、风险提示 |
| 插件仓库 | `/plugins/manage?tab=plugins` | 刷新、私有库、`tree/<branch>`、一键更新风险预览 |
| 插件配置页 | `/accounts/1/features/<plugin_key>?from=plugins` | 使用说明、总开关、配置容器、自定义样式、预览建议 |
| 系统设置 | `/settings` | Trace/Event Bus/Inline/payload/native_raw 设置和回滚说明 |

每个页面至少验收桌面宽屏和窄屏/PWA。特别检查底部导航不换行、固定按钮不随内容滚走、长中文/长英文/长错误消息不撑破容器。

### 24.9 最终报告格式

最终报告只允许引用证据台账、命令输出、页面验收、远端检查和 trace_id，不允许用聊天记忆或子 Agent 自述代替证据。

报告必须包含：

- 结论：`Go / 已完成`、`可测 / 待服务器实测` 或 `No-Go / 阻塞`。
- 版本与分支：本地分支、远端分支、commit、版本号。
- 朋友建议落地：全量消息、个人可信插件标准、UserBot 主控、交互 Bot 高频互动、转账/发奖由 userbot/settlement、日志排障。
- 重要架构改动：Event Bus、Trace、MessageOps、Contract Guard 新定位、native_raw、Inline、远程插件字段贯通。
- UI 和体验改动：日志中心、交互中心、插件中心、配置页、仓库刷新/一键更新、一键部署。
- 验证证据：自动验证、浏览器验收、远端部署、真实 trace、回滚演练。
- 残余风险：只列允许残余风险；如存在硬门禁风险，结论必须是 `No-Go / 阻塞`。

### 24.10 当前分支封口缺口清单

本节把当前 `0.40.x` 分支最容易误判为“已经完成”的缺口固定下来。执行时先逐项复核；如果代码已经修好，也必须补当前 commit 的测试或页面证据后才能签收。

| 缺口 | 为什么会破坏最终版 | 必须做到 | 证据 |
| --- | --- | --- | --- |
| 命令链路只补 trace，没有真实 Event Bus decision | 日志看似有链路，但命令仍由旧 handler 直达插件或系统处理器 | 管理员命令、插件命令、未命中命令都必须生成 `TelePilotEvent`，经过 `dispatch_event` 或等价 decision 记录后再进入兼容 handler | `test_worker_command.py` 断言 decision、reason_code、plugin/system handler、event_action |
| UserBot 普通消息只走旧 `on_message` | 新插件只写 `event_subscriptions` + `on_event` 时无法被触发 | UserBot 普通消息匹配订阅后真实调用 `on_event` 或新版兼容入口；旧 `on_message` 只能作为迁移层 | `test_plugin_loader.py` 覆盖无旧入口的新插件被 UserBot 消息触发 |
| 交互 Bot 旧规则、付款确认、`session_close` 仍可直调插件 | 旧规则和新 Event Bus 形成两套调度真相，日志无法解释真实路径 | 旧规则映射为 `rule_bound` 订阅；付款确认和 `session_close` 生成标准事件并经过 matcher；禁止手工伪造 matched span 代替 decision | `test_account_bot.py` 覆盖 message、callback、payment、session_close 的 decision、invoke、action/skip |
| `send(pin=True)` 或复合动作只记录主动作 | 发送成功但置顶失败时，动作页会显示假成功 | 复合动作拆成可追踪子动作；发送、编辑、删除、置顶、ACK、inline answer、settlement 任一失败都写 failed `event_action` | Delivery 测试覆盖 pin 失败、edit/delete 失败、callback/inline 缺 ID |
| 插件仍可通过 live client 直发 | 插件绕过 MessageOps，Trace 和 Contract Guard 都失效 | `ctx.client`、命令参数 `client`、交互入口 client 必须是 trace-aware facade 或只读对象；直接 Telegram 调用点要分类并收口 | `rg` 直接发送点分类；facade 测试证明发送会落 `event_action` |
| reason_code 和前端中文映射漂移 | 日志筛选和文档排障会对不上 | 后端 reason 常量、日志 API、前端映射、开发文档、测试 fixture 使用同一批稳定 code | grep/测试列出新增 code 并证明文档和前端已同步 |
| 日志 API 过滤不完整 | 用户按账号、trace、reason 查不到真实问题 | `trace_id`、`reason_code`、`error_code`、`account_id`、时间范围在 events/actions/commands/overview 中一致生效 | `test_logs_api.py` 覆盖 SQL 过滤和空状态 |
| 回滚开关只是配置字段 | 生产出问题时不能安全降级 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled`、`native_raw_persist_enabled` 必须实际影响运行时，并在关闭时写 degraded 证据 | 设置测试、运行时测试、部署或本地回滚演练记录 |
| 远程插件字段或一键更新丢契约 | 插件从仓库安装后和本地 manifest 表现不同 | 私有库、`tree/<branch>`、刷新、一键更新、安装记录、feature matrix 都保留 `usage`、`event_subscriptions`、`capabilities` | 远程插件服务测试、前端页面验收、验证脚本 |
| 开发文档仍教旧主路径 | 开发者会继续写旧 payload、旧 notice、旧 rule 绑定插件 | 新指南只主推 Event Bus + 标准 payload + `ctx.messages`；旧写法只放迁移/废弃说明 | 文档 grep 分类、示例插件验证通过 |
| 服务器证据缺失 | 本地可测不能证明线上最终版落地 | 部署到 `144.24.5.159` 后要有备份、远端 commit、版本、迁移、健康、真实 trace、回滚开关演练 | 证据台账中的远端命令、trace_id、备份路径 |

以上任一缺口没有证据，最终结论最多是 `可测 / 待服务器实测`。如果缺口本身命中第 24.5 节硬门禁，例如旧通道仍可发送、未声明插件拿到 `native_raw`、动作失败不落库，则结论必须是 `No-Go / 阻塞`。

### 24.11 最终版执行任务卡

后续实现不要再按聊天记忆拆任务，直接按下面任务卡并行。每张卡都必须独立可 review、可测试、可回滚。

| 任务卡 | 负责人类型 | 写入范围 | 必须交付 | 禁区 |
| --- | --- | --- | --- | --- |
| F0 事实冻结 | 主 Agent / Evidence | `docs/release/<version>-final-evidence.md` | 分支、HEAD、工作树、未跟踪文件、版本、历史验证降级记录 | 不改 runtime，不写正式完成结论 |
| F1 契约注册表 | 后端 + 前端类型 | `backend/app/services/event_bus.py`、插件 base/manifest、`frontend/src/api/types.ts`、reason 映射、文档表 | event type、payload、action、send_via、capabilities、settings、reason_code 一致 | 不改业务分发语义 |
| F2 入口唯一 | Runtime | `backend/app/worker/runtime.py`、`backend/app/worker/command.py`、`backend/app/worker/plugins/loader.py`、`backend/app/services/account_bot_runtime.py` | message、command、callback、inline、payment、session_close 的 decision、invoke/skip、reason_code | 不用手工 matched span 冒充 `dispatch_event` |
| F3 动作出口 | Runtime | `backend/app/worker/plugins/message_ops.py`、`backend/app/services/interaction/delivery.py`、`backend/app/services/interaction/contracts.py` | 所有成功/失败/跳过动作写 `event_action`，复合动作拆分，旧 notice failed | 不恢复旧通道，不吞失败 |
| F4 插件状态与风险 | Runtime + Contract | loader、PluginRuntimeStatus、规范警告、验证脚本 | 插件加载失败、越声明调用、native_raw、inline_all、settlement 风险可见 | 不把 Contract Guard 改回强沙箱 |
| F5 日志中心 UI | Frontend | `frontend/src/pages/Logs.tsx`、日志类型/API 调用 | 成功、未命中、插件失败、动作失败、旧日志 fallback 的桌面和窄屏验收 | 不创造后端不存在字段 |
| F6 交互/插件/设置 UI | Frontend | `frontend/src/pages/Interaction/*`、`frontend/src/pages/Plugins/*`、`frontend/src/pages/Settings/*` | 交互中心主配置、插件 usage/能力/订阅、仓库更新、回滚设置可用 | 不把账号详情恢复成唯一主入口 |
| F7 示例和开发文档 | Docs | `README.md`、`docs/PLUGIN-*.md`、`examples/plugins/*`、`scripts/validate-*` | message、command、callback、inline、payment、native_raw、旧 notice 示例/迁移说明 | 不把旧 payload、旧 notice、旧 `event.reply/respond` 写成推荐路径 |
| F8 发布检查点 | Release | 四处版本文件、`CHANGELOG.md`、证据台账 | 当前 diff 对应的中文 CHANGELOG、版本同步、中文 commit、push 分支 | 不覆盖 main，不沿用旧测试证据 |
| F9 服务器和回滚 | Deploy | 远端部署记录、证据台账；必要时部署脚本 | 备份、部署、迁移、健康、真实 trace、回滚开关演练 | 没备份不改远端，健康失败不报 Go |

合并顺序固定为：F0 -> F1 -> F2/F3/F4 -> F5/F6 -> F7 -> F8 -> F9。F5/F6 可以先做视觉结构，但最终验收必须等 F1-F4 字段稳定后重跑。F8/F9 只能由主 Agent 或明确指定的发布/部署 Agent 执行。

### 24.12 每张任务卡的最低验收

每张任务卡交付时必须按这个格式写入证据台账，不能只在聊天里说明：

```text
任务卡：
状态：未开始 / 半落地 / 可测 / 已完成
commit：
改动文件：
公共契约变化：
禁区未触碰：
自动验证：
浏览器或人工验收：
文档同步：
剩余风险：
复验时间：
```

状态判定规则：

- 只改代码但没有测试，最多 `半落地`。
- 后端测试通过但前端类型、文档或验证脚本未同步，插件契约相关任务最多 `可测`。
- UI 只跑构建、没有桌面和窄屏/PWA 验收，前端相关任务最多 `可测`。
- 部署未到 `144.24.5.159`，或没有真实 trace/回滚演练，最终出口最多 `可测 / 待服务器实测`。
- 任一硬门禁命中，任务和最终出口都必须写 `No-Go / 阻塞`。

### 24.13 最终版端到端必测剧本

最终版必须用真实 Telegram 场景或固定 fixture 覆盖以下剧本。真实环境优先；线上不适合触发的场景可以用同版本本地 fixture 代替，但必须写清原因。

| 剧本 | 输入 | 必须看到 |
| --- | --- | --- |
| 普通消息未命中 | 允许群内发送不匹配文本 | receive、normalize、subscription skipped、稳定 reason_code |
| 玩家关键词启动 | 玩家发送插件关键词 | public keyword decision、plugin invoke、interaction_bot action |
| 管理员命令 | 账号主人发送命令 | command parse、permission、handler/plugin、userbot_reply 或 action |
| 按钮回调 | 点击交互 Bot 按钮 | callback_query、session/rule/subscription、answer_callback action |
| Inline 查询 | `@botname keyword` | inline_query、scope、answer_inline_query 成功或失败 |
| 外部转账通知 | 第三方转账通知 Bot 消息 | `source_actor.type=external_bot`、payment、player、settlement/userbot action |
| native_raw 声明 | 声明能力的测试插件 | 插件拿到 JSON 兼容 `native_raw`，日志只默认保存 `native_raw_meta` |
| native_raw 未声明 | 未声明插件尝试读取 | 无完整 `native_raw`，Trace 显示 `native_raw_not_allowed` 或 skipped meta |
| 旧 notice | 插件返回 `notice` / `bbot_notice` | `send_channel_deprecated`，没有实际发送 |
| 动作失败 | 缺 token、缺目标 message_id、pin 失败或 inline 缺 ID | failed `event_action`、稳定 error_code、中文说明 |
| 插件加载失败 | 损坏 manifest 或入口 | `PluginRuntimeStatus`、`plugin_load_failed`、日志页可定位 |

这些剧本至少要覆盖 `event_trace`、`event_span`、`event_action` 三类记录。只验证“插件函数被调用”不足以签收最终版。

### 24.14 允许残余风险与不允许残余风险

允许带到最终版之后继续优化的风险：

- 用户第三方插件仍使用旧字段，但 WebUI、验证脚本和文档已给出迁移警告。
- 某个罕见 Telegram update 类型暂未封装，但 message、command、callback、inline、payment、session_close 主链路完整。
- 线上不适合演练某个破坏性回滚动作，但同版本本地或临时环境已演练，并记录原因。
- 历史设计文档保留旧概念，但已明确标注历史背景，不被开发指南作为主路径引用。

不允许作为残余风险：

- 官方/示例插件仍依赖旧平铺 payload 主路径。
- 旧 `notice` / `bbot_notice` / `notice_bot` 还能发送消息。
- 未声明插件能拿完整 `native_raw`。
- 任一用户可见动作失败没有 `event_action`。
- 日志页无法解释未触发、插件加载失败、插件运行异常或动作失败。
- 远程插件安装或升级丢失 `usage`、`event_subscriptions`、`capabilities`。
- 发布材料、版本、commit、部署结果和实际 diff 不一致。

### 24.15 最终版执行完成的判定算法

主 Agent 最终判定时按以下算法执行：

1. 第 24.5 节硬门禁逐条检查；命中任一条，输出 `No-Go / 阻塞`。
2. 第 24.6 节十项签收逐条查证据台账；任一项缺当前 commit 证据，不能输出 `Go / 已完成`。
3. 第 24.7 节自动验证全部重跑；失败且没有替代证据，输出 `No-Go / 阻塞`。
4. 第 24.8 节页面完成桌面和窄屏/PWA 验收；缺前端证据时最多输出 `可测 / 待服务器实测`。
5. 第 24.13 节端到端剧本至少覆盖主链路；缺真实 trace 但 fixture 通过，最多输出 `可测 / 待服务器实测`。
6. 部署到 `144.24.5.159` 并完成备份、迁移、健康、真实 trace、回滚开关演练；缺任一项，最多输出 `可测 / 待服务器实测`。
7. 只有以上全部通过，才输出 `Go / 已完成`，并在最终报告中明确“这是最终版框架落地，不等于所有第三方插件无需迁移，也不等于 Telegram 全 API 已封装”。

这套判定算法是第 24 节的最终出口。任何聊天记忆、子 Agent 自述、历史测试、页面能打开、局部功能可用，都不能覆盖它。

## 25. 最终版落地总则

本节回答“按计划后是否真的能实现最终版”。答案是：可以，但最终版必须按本节的落地总则来做，不能把前文中的某个阶段性能力当作最终版，也不能把所有想象中的 Telegram API 全覆盖误解成最终版。

最终版的定义固定为一句话：

> TelePilot 对个人可信插件提供稳定的统一 Telegram 事件入口、统一插件协议、统一消息操作出口、统一日志排障视角和统一开发文档；插件可以自由处理声明范围内的消息并选择发送通道，平台保留凭据、live client、旧 notice 通道和普通 Bot 转账四条客观边界。

### 25.1 最终版不是继续加功能，而是关闭五个断点

当前计划已经足够大，后续不再通过“再加一批功能”来证明最终版。真正需要关闭的是五个断点：

| 断点 | 最终版必须怎样关闭 | 失败时的结果 |
| --- | --- | --- |
| 入口断点 | 所有 Telegram 主入口都生成 `TelePilotEvent`，并由 Event Bus decision 进入插件或跳过 | 日志只能看到局部 trace，不能解释插件为什么没启动 |
| 协议断点 | 新插件只读标准事件信封，不需要理解旧规则、旧平铺 payload 或旧 `raw_event` | 开发者仍要缝补旧系统，最终版不成立 |
| 动作断点 | 所有用户可见动作都经 `ctx.messages` / action / Delivery Executor，并落 `event_action` | 发送失败、置顶失败、按钮 ACK 失败会被误判为成功 |
| 排障断点 | `/logs` 以 trace 为主线解释 receive、matched/skipped、invoke、action、finish | 日志页只是旧日志美化，无法排查真实卡点 |
| 发布断点 | 版本、CHANGELOG、commit、部署、真实 trace、回滚演练都绑定同一提交 | 本地可测和线上状态分裂，不能对外称完成 |

这五个断点全部关闭后，即使仍有罕见 Telegram update 类型未封装，也可以称为最终版框架落地；任一断点未关闭，只能称为阶段性可测版本。

### 25.2 最终版模块边界

最终实现必须按下列模块边界落地。边界清楚，后续并行才不会互相覆盖。

| 模块 | 责任 | 不负责 |
| --- | --- | --- |
| Source Adapter | 把 UserBot、交互 Bot、callback、inline、payment、session_close 标准化为 `TelePilotEvent` | 判断插件业务是否应该处理 |
| Event Bus | 读取插件订阅，输出 matched/skipped/delivered decision 和 reason_code | 直接发送 Telegram 消息 |
| Plugin Runtime | 构造插件上下文，调用 `on_event` / 兼容入口，收集 action 或 `ctx.messages` 调用 | 暴露 Bot token、UserBot session、live Telegram client |
| Contract Guard | 对越声明调用、高风险能力、废弃值给出告警、失败或 trace 证据 | 重新做公共插件市场式强沙箱 |
| MessageOps / Delivery Executor | 执行发送、编辑、删除、置顶、callback ACK、inline answer、settlement，并记录 `event_action` | 判断玩法业务逻辑 |
| Trace Service | 记录 trace、span、action、runtime status、payload 摘要和失败原因 | 持久化默认完整 `native_raw` |
| Logs UI | 用 trace 视角展示系统健康、消息链路、插件诊断、命令链路、动作发送和原始日志 fallback | 用前端字段猜测后端不存在的状态 |
| Interaction UI | 作为交互中心管理账号/Bot、规则、插件入口、触发词、参数和最近状态 | 继续要求用户必须钻到账号详情页配置 |
| Plugin Docs / Examples | 让开发者按文档写出 message、command、callback、inline、payment 插件 | 把旧 payload、旧 notice、旧规则驱动写成推荐主路径 |

任何实现如果让一个模块承担了“不负责”列里的事情，都要回到本表修正，而不是在文档里解释例外。

### 25.3 插件开发者最终体验

最终版必须让开发者按以下路径一次写出可用插件，而不是先写一个小框架再不断补洞：

1. 在 `plugin.json` 中声明 `usage`、`event_subscriptions`、`capabilities` 和入口。
2. 在插件代码中接收标准事件信封，通过 `event.type`、`event.source.channel`、`event.message`、`event.sender`、`event.chat`、`event.payment`、`event.inline_query` 判断业务。
3. 用 `ctx.messages.send_text`、`edit_message`、`delete_message`、`pin_message`、`answer_callback`、`answer_inline_query` 或 `settlement` 发起动作。
4. 在 action 中选择 `send_via=interaction_bot`、`userbot_reply` 或 `auto`；平台记录 requested 和 actual 通道。
5. 如需完整 Telegram 原生字段，在 manifest 中声明 `capabilities.telegram_native_raw.enabled=true`，并接受 WebUI 风险提示和 Trace 留痕。
6. 调试时打开 `/logs`，按 trace_id、插件、reason_code、action 状态定位问题。
7. 发布前跑示例验证脚本，确保 usage、订阅、能力、旧通道和 native_raw 边界都通过检查。

开发指南必须围绕这七步重写。旧 `event.reply/respond`、旧 `ctx.client` 直发、旧平铺 `payload["text"]`、旧 `raw_event`、旧 `notice` / `bbot_notice` 只能放在迁移或废弃说明里。

### 25.4 两种调度方式的最终执行定义

最终版仍保留两种调度方式，但它们都属于同一个 Event Bus，不是两套系统。

| 调度方式 | 触发 | 默认发送通道 | 钱相关动作 | 必须有的 trace |
| --- | --- | --- | --- | --- |
| 管理员命令调度 | 账号主人或授权管理员发送带命令前缀的命令 | `userbot_reply` 或 `auto` | userbot / settlement | command parse、permission、handler/plugin、action |
| 玩家关键词调度 | 群内玩家发送插件声明的关键词、按钮、答案或 inline query | `interaction_bot` 或 `auto` | userbot / settlement，普通 Bot 只公告 | message/callback/inline、subscription decision、plugin invoke、action |

关键原则：

- 插件不能被硬绑死必须由某个账号回复；插件可以声明或在 action 中选择通道。
- 平台不能把普通 Bot 伪装成有转账能力的主体；转账、发奖、补发、结算必须走 userbot 或 settlement。
- UserBot 仍是主控和信息源；普通 Bot 是低风险高频交互出口。
- 玩家玩法强交互由统一会话和双通道消息操作承接，不让普通 Bot 独立跑出第二套状态。

### 25.5 最终版实现顺序

后续执行时，固定使用下面顺序。它比“按文件分工”更重要，因为前面的契约没稳定，后面的 UI 和文档就会漂。

1. **冻结契约**
   - 冻结 event type、source channel、payload 字段、action type、send_via、capabilities、reason_code、settings。
   - 同步后端常量、前端类型、文档表、示例 fixture。

2. **关闭入口**
   - UserBot message、管理员 command、交互 Bot message、callback、inline、chosen inline、payment、session_close 都必须有 Event Bus decision。
   - 旧规则和旧 session 只能映射成订阅条件，不允许绕过 decision 直调插件。

3. **关闭动作**
   - 所有用户可见 Telegram 操作都必须落 `event_action`。
   - 复合动作要拆分，例如 `send(pin=true)` 必须记录 send 和 pin 两个结果。
   - 旧 `notice` / `bbot_notice` / `notice_bot` 一律失败并给 `send_channel_deprecated`。

4. **关闭风险可见性**
   - `native_raw`、inline_all、settlement、跨通道发送、越声明调用必须在 Trace、规范警告或 WebUI 中可见。
   - Contract Guard 保持软告警和客观失败定位，不回到强沙箱。

5. **关闭日志和 UI**
   - `/logs` 先能排障，再谈视觉细节。
   - `/interaction` 作为真正交互中心，不再只是介绍页。
   - `/plugins` 和插件配置页必须展示 usage、订阅、能力、风险、预览建议和规范警告。
   - `/settings` 只把回滚开关作为高级运维护栏展示，不包装成多模式产品。

6. **关闭文档和示例**
   - 开发指南、API 参考、速查、AI/HTTP 文档、examples README 全部改成同一套最终版主路径。
   - 示例插件覆盖 message、command、callback、inline、payment、native_raw、旧 notice 失败。

7. **关闭发布和部署**
   - 四处版本号、中文 CHANGELOG、证据台账、commit、push、服务器部署和回滚演练一致。
   - 没有服务器证据时，状态只能是 `可测 / 待服务器实测`。

### 25.6 可并行但不能互相等待的任务包

为了快速推进，可以拆成以下任务包。每个任务包完成后系统都必须仍可运行，不能依赖后续任务包才能恢复基本可用状态。

| 包 | 可并行内容 | 独立完成标准 | 不能做 |
| --- | --- | --- | --- |
| P0 Evidence | 事实冻结、证据台账、旧证据降级 | 当前分支、版本、未跟踪文件、验证状态清楚 | 不改 runtime |
| P1 Contract | 契约注册表、reason_code、前端类型、文档表 | 后端/前端/文档字段一致，相关测试通过 | 不改业务调度 |
| P2 Runtime | Source Adapter、Event Bus、Plugin Runtime、MessageOps | 五条主链路测试通过，action 成功失败都有记录 | 不改 UI 文案结构 |
| P3 Risk | Contract Guard、native_raw、inline_all、settlement、旧通道失败 | 风险能力和废弃值都有告警、失败或 trace | 不恢复强沙箱 |
| P4 UI | 日志中心、交互中心、插件中心、设置页 | typecheck/build 通过，桌面和窄屏验收写入台账 | 不创造后端不存在字段 |
| P5 Docs | 插件开发指南、API、示例、验证脚本 | 开发者七步路径可复制，示例验证通过 | 不推荐旧主路径 |
| P6 Release | 版本、CHANGELOG、commit、push、部署、回滚 | 当前 commit 的本地、远端、服务器证据一致 | 不覆盖 main，不无备份改远端 |

合并顺序固定为 P0 -> P1 -> P2/P3 -> P4/P5 -> P6。P4 可以先做结构，但最终验收必须等 P1-P3 字段稳定后重跑。

### 25.7 最终版验收的最小证明集

最终版不要求证明所有理论场景，但必须至少证明以下集合：

| 证明项 | 必须证据 |
| --- | --- |
| 普通消息未命中可解释 | trace 中有 receive、normalize、subscription skipped 和 reason_code |
| 玩家关键词可启动插件 | trace 中有 public keyword 或订阅命中、plugin invoke、interaction_bot action |
| 管理员命令可排障 | trace 中有 command parse、权限、handler/plugin、userbot_reply/action |
| Callback 可闭环 | trace 中有 callback_query、answer_callback action 成功或失败 |
| Inline 可闭环 | trace 中有 inline_query、answer_inline_query action 成功或失败 |
| 付款通知可作为消息来源 | trace 中区分 external payment notice、payer/player/reply_to、settlement/userbot |
| 旧通道不可执行 | `notice` / `bbot_notice` 产生 `send_channel_deprecated` 且没有实际发送 |
| native_raw 边界正确 | 声明插件可读 JSON 兼容 `native_raw`，未声明插件拿不到完整原生数据 |
| 动作失败不静默 | 缺 token、缺 message_id、pin 失败、inline 缺 ID 等都落 failed `event_action` |
| 插件加载失败可定位 | `PluginRuntimeStatus` 和日志页能看到插件、入口、错误和最近 trace |
| UI 可用 | `/logs`、`/interaction`、`/plugins`、仓库管理、插件配置页、`/settings` 桌面和窄屏均通过 |
| 部署可退 | 远端备份、迁移、健康检查、真实 trace、回滚开关演练齐备 |

这张表是最终版最小证明集。缺一项时，不能用“其他测试很多”抵消。

### 25.8 最终版报告必须能给朋友看的落地映射

最终报告必须把朋友的建议逐条映射到实际落地，而不是只写技术名词。

| 朋友建议 | 最终落地说法 |
| --- | --- |
| 插件自由拿两边消息 | 插件通过统一 Event Bus 看到声明范围内的 UserBot / 交互 Bot / callback / inline / payment 事件 |
| 普通 Bot 负责高频游戏 | 玩家关键词和强交互默认走 `interaction_bot`，UserBot 仍负责主控、监听和转账/发奖 |
| 不要复杂强沙箱 | Contract Guard 改为个人可信插件标准下的风险提示、越声明留痕和客观失败 |
| 基于 UserBot 搞 | UserBot 是信息源和结算主体，普通 Bot 是低风险交互出口 |
| 游戏容易状态分裂 | 统一会话、Event Bus decision、Trace 和 MessageOps 让状态、按钮、付款归属在同一条链路里 |
| 插件维护太难 | 新开发指南只教标准事件信封、`event_subscriptions`、`capabilities`、`ctx.messages` 和日志排障 |

如果某条建议没有对应代码、UI、文档和证据，就不能在报告里写成“已落地”。

### 25.9 最终版文档发布门禁

最终版发布前，插件开发文档必须满足以下门禁：

- 新开发者只读 `docs/PLUGIN-DEV-GUIDE.md` 和 `docs/PLUGIN-API-REFERENCE.md`，能写出 message、command、callback、inline、payment 插件。
- `README.md` 只描述当前安装、部署、插件仓库、日志和交互中心入口，不再把旧复杂流程当主路径。
- `docs/PLUGIN-CHEATSHEET.md` 给出最小 manifest、最小 `on_event`、最小 `ctx.messages` 示例。
- `docs/PLUGIN-SAFETY.md` 解释个人可信插件标准、风险自担、平台四条客观边界。
- `docs/PLUGIN-AI.md` 和 `docs/PLUGIN-HTTP.md` 与最终版 `capabilities` / Contract Guard 口径一致。
- `examples/plugins/README.md` 只推荐最终版示例；旧写法必须标为迁移或废弃。
- 文档中所有 `notice`、`bbot_notice`、`notice_bot`、`raw_event`、`ctx.client`、`event.reply/respond` 命中都已分类，不能作为推荐写法。

文档门禁失败时，即使代码通过，也只能写 `可测`；因为开发者仍会按旧逻辑写插件。

### 25.10 最终版可宣称范围

最终版完成后可以宣称：

- TelePilot 已把插件主路径统一到 Event Bus、Trace 和 MessageOps。
- 插件可以基于 UserBot 和交互 Bot 的统一消息流开发强交互玩法。
- 插件可以自由选择 `interaction_bot`、`userbot_reply` 或 `auto` 作为发送通道。
- 转账、发奖、结算仍由 userbot 或 settlement 承接，普通 Bot 不被赋予转账能力。
- 日志中心能排查消息走到哪一步、插件为什么没启动、动作为什么失败。
- 开发者可按新版插件开发指南开发和迁移插件。

最终版完成后不能宣称：

- TelePilot 已封装 Telegram 的所有 update 类型和所有 Bot API 方法。
- 所有第三方插件无需修改即可自动兼容。
- 平台会替账号主人承担远程插件风险。
- 关闭 Trace 后仍有同等详细的排障能力。
- 普通 Bot 可以执行转账或发奖。
- 旧 `notice` / `bbot_notice` / `notice_bot` 仍是可用发送通道。

### 25.11 最终版执行出口

主 Agent 最终只能输出三种结论之一：

| 结论 | 条件 | 允许做什么 |
| --- | --- | --- |
| `Go / 已完成` | 第 25.7 节最小证明集全部有当前 commit 证据，且远端部署、真实 trace、回滚演练齐备 | 宣称最终版框架落地，提交、推送、部署报告 |
| `可测 / 待服务器实测` | 本地自动验证、文档审计、浏览器验收通过，但缺远端部署、真实 Telegram trace 或线上回滚证据 | 推送分支给用户实测，不宣称服务器最终版完成 |
| `No-Go / 阻塞` | 命中硬门禁、验证失败无替代证据、文档仍推荐旧主路径、旧通道仍可执行、native_raw 越界、动作失败不落库 | 回到对应任务包修复，不能发布 |

只要按第 25.1 到第 25.11 执行并完成证据，所谓“最终版”就是可实现、可验收、可部署、可解释的；否则再多局部功能也只能是阶段性版本。

## 26. 最终版执行锁定补丁

本节用于把“计划已经足够完整”进一步锁定为“当前分支照此修完就能进入最终版验收”。它不替代第 24/25 节，而是把当前代码审查发现的剩余断点固定成必须修复的 No-Go 清单。后续执行如果发现本节条目已经被修复，也必须补当前 commit 的目标测试和证据台账，不能只凭文件里已有实现宣称完成。

### 26.1 最终版的四条执行红线

最终版不允许再出现以下四类“看起来有 trace，实际仍旧绕路”的实现：

| 红线 | 禁止状态 | 正确状态 |
| --- | --- | --- |
| 手写 matched span 冒充 Event Bus decision | 代码直接 `record_span(reason_code="matched")` 后调用插件 | 先构造标准事件和订阅，调用 `dispatch_event` 或等价 matcher，按 decision 记录 matched/skipped/delivered |
| 插件 helper 直连 live Telegram 方法 | `event.reply/respond/edit/delete` 直接调用真实 event 或真实 client，绕过 trace-aware facade | 通过 `ctx.messages`、标准 action、Delivery Executor 或 trace-aware client 记录 `event_action`；不能追踪时明确阻断并提示迁移 |
| 系统调度直发消息不落 action | scheduler、自动发奖、兼容入口直接 `client.send_message/delete_messages`，只写 runtime log | 每个用户可见发送、删除、置顶、编辑、ACK、inline answer、settlement 都写成功或失败 `event_action` |
| 证据台账先写完成结论 | 自动验证通过但目标链路没有断言 decision/action，或缺浏览器/远端证据 | 台账只按当前证据写 `未开始`、`半落地`、`可测`、`已完成`；命中硬门禁时写 `No-Go / 阻塞` |

这四条红线是最终版签收的底线。只要命中其中一条，最终状态必须是 `No-Go / 阻塞`。

### 26.2 当前分支必须先关闭的 No-Go 清单

以下清单来自当前分支的代码级复核。每项都必须有代码修复、目标测试和证据台账三件套。

| 编号 | 位置 | 当前问题 | 必须修成 | 最低测试证据 |
| --- | --- | --- | --- | --- |
| N1 | `backend/app/services/account_bot_runtime.py::_try_handle_interaction_module_message` | 旧交互会话 `message/callback_query` 仍用手写 `subscription_match` span，然后直接 `_run_worker_interaction_entry` | 用真实 `dispatch_event` 或 `_legacy_rule_event_bus_decision` 生成 `rule_bound` decision；只有 matched decision 才能调用插件；skipped 要写稳定 reason_code | `test_interaction_plain_message_routes_to_worker_entry_as_message`、`test_interaction_callback_routes_to_worker_entry_and_answers_callback` 断言 `dispatch_event` 被调用并写 decision |
| N2 | `backend/app/worker/runtime.py::_try_account_bot_auto_award` | 自动发奖有 trace/action，但没有 Event Bus decision，仍是独立 `NewMessage` 入口 | 转账通知/自动发奖标准化为 `payment_confirmed` 或内部 settlement 事件，经过虚拟订阅 decision 后再执行 userbot 发奖回复 | `test_account_bot_auto_award_records_trace_action` 增加 decision 断言；失败分支有 failed `event_action` |
| N3 | `backend/app/worker/plugins/sandbox.py::SandboxEvent` | `event.reply/respond/edit/delete` 非读 helper 仍直接调真实 event 方法，可能绕过 trace-aware action | 非读 helper 改走 trace-aware client / MessageOps；若无法保证 action 落库，则阻断并提示使用 `ctx.messages`；读 helper 仍可包装返回值 | `test_plugin_security_regression.py` 覆盖允许 helper 时会落 `event_action`，不允许 helper 时明确 PermissionError/迁移提示 |
| N4 | `backend/app/worker/scheduler_runtime.py` | 定时任务发送、命令发送、LLM 结果发送和延迟删除通过 `ctx.client` 直发/直删，没有统一 `record_action` | `action_send_message`、`action_run_command`、`action_call_llm`、`delete_message_after` 统一记录 scheduler action，成功/失败/限流丢弃都可见 | `test_scheduler_runtime.py` 覆盖 send success、send failure、rate limited drop、delete failure 的 action 记录 |

这四项关闭前，证据台账不能写“入口唯一”“动作可追踪”已完成，也不能进入发布部署步骤。若自动化全量测试通过但 N1-N4 没有目标断言，结论仍然是 `No-Go / 阻塞`。

### 26.3 No-Go 修复后的最小重跑命令

关闭 N1-N4 后先跑目标测试，再跑全量验证。

目标测试：

```bash
cd backend && .venv/bin/pytest -q \
  app/tests/test_account_bot.py::test_interaction_plain_message_routes_to_worker_entry_as_message \
  app/tests/test_account_bot.py::test_interaction_callback_routes_to_worker_entry_and_answers_callback \
  app/tests/test_account_bot_auto_award.py::test_account_bot_auto_award_records_trace_action \
  app/tests/test_plugin_security_regression.py \
  app/tests/test_scheduler_runtime.py
```

如果测试名因重构改变，必须先用 `rg` 找到等价测试，并在证据台账写清新的测试名。不得用“全量 pytest 通过”替代这组目标测试，因为这组测试专门证明当前 No-Go 是否关闭。

全量验证仍按第 24.7 节执行：

```bash
cd backend && .venv/bin/ruff check app ../scripts/validate-plugin-examples.py
cd backend && .venv/bin/pytest -q
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
cd frontend && ./node_modules/.bin/tsc -b --pretty false
cd frontend && ./node_modules/.bin/vite build
cd backend && .venv/bin/alembic heads
cd backend && .venv/bin/alembic upgrade head --sql >/tmp/telepilot-alembic-final.sql
git diff --check
```

### 26.4 证据台账必须同步降级或升级

证据台账是 Go / No-Go 的事实来源，不是发布报告草稿。执行 N1-N4 时按下面规则维护：

- 修复前：`docs/release/<version>-final-evidence.md` 结论必须是 `No-Go / 阻塞`，并列出 N1-N4。
- 修复中：对应控制项只能写 `半落地`，除非目标测试和全量验证都已经重跑。
- 修复后但未浏览器验收：后端链路可以写 `可测`，前端/日志项仍保持 `半落地`。
- 浏览器验收后但未部署：总出口最多 `可测 / 待服务器实测`。
- 部署、真实 trace 和回滚演练后：才允许按第 24.15 / 25.11 算法判断是否 `Go / 已完成`。

任何台账条目如果使用历史 commit、历史测试或子 Agent 自述，必须显式标注“历史基线”，不能写成当前签收证据。

### 26.5 最终版封口后的真实验收闭环

N1-N4 关闭后，还必须补齐以下最终闭环，否则只能是本地可测版本：

1. **浏览器验收**
   - `/logs`：成功链路、未命中链路、插件失败、动作失败、旧 notice 失败、原始日志 fallback。
   - `/interaction?aid=1`：规则列表、规则详情、插件入口、触发词、会话状态、最近错误，桌面和窄屏/PWA。
   - `/plugins`、`/plugins/manage?tab=plugins`、插件配置页、`/settings`：usage、订阅、能力、风险、仓库刷新/一键更新、回滚设置。

2. **真实或可复现 trace**
   - 普通消息未命中。
   - 玩家关键词启动插件。
   - 管理员命令。
   - Callback。
   - Inline。
   - 付款通知 / 自动发奖。
   - 旧 notice 失败。
   - 动作失败。

3. **服务器部署和回滚**
   - 部署到 `144.24.5.159` 前备份 `.env`、compose 文件和数据库。
   - 部署后确认远端 commit、版本、迁移、健康检查、docker logs、Trace API。
   - 演练并恢复 `trace_enabled=false`、`event_bus_delivery_enabled=false`、`inline_updates_enabled=false`。

这三组证据缺任一组，最终出口最多 `可测 / 待服务器实测`。

### 26.6 为什么补完本节后可以实现“最终版”

第 24/25 节已经定义了最终版的产品形态和验收算法，本节补的是最后一层执行可证性：它把当前剩余的绕路点全部落到文件、函数、测试和台账状态上。补完 N1-N4 后：

- 入口断点关闭：旧交互会话和自动发奖不再绕过 Event Bus decision。
- 动作断点关闭：插件 helper、scheduler、自动发奖和 Delivery Executor 的用户可见动作都有 `event_action`。
- 协议断点关闭：插件开发者被引导到 `ctx.messages` / 标准 action，而不是旧 `event.reply/respond` 或 live client。
- 排障断点关闭：日志页能看到 decision、invoke、action、failure，而不是只看到手写 span 或 runtime log。
- 发布断点关闭：台账会先降级到真实状态，再随测试、浏览器、部署和回滚证据逐步升级。

因此，本计划不是“再讨论一个理想架构”，而是已经收敛成可以直接施工、直接 review、直接部署验收的最终版执行清单。

## 27. 最终版可实现性锁定

本节回答当前问题：“继续完善计划后，是否真的能按计划实现所谓的最终版”。结论是可以，但前提是后续执行只做收口和验收，不再把最终版扩展成新的愿景版本。本节是第 24/25/26 节之后的最后执行封套，用于防止计划越写越大、实现越做越散。

### 27.1 最终版只剩一条主线

从本节开始，最终版不再按“新增能力”理解，而按“关闭断点”理解。后续工作只围绕一条主线推进：

```text
Telegram 事件
  -> Source Adapter
  -> 标准事件信封
  -> Event Bus decision
  -> 插件 on_event / 兼容入口
  -> ctx.messages / 标准 action
  -> Delivery Executor / settlement
  -> Trace / 日志中心 / 证据台账
```

所有修改都必须能回答：它修的是入口、协议、动作、日志、文档、部署中的哪一个断点。回答不上来，就不纳入最终版。

### 27.2 不再扩大范围

最终版封口期间禁止把以下内容加入当前发布范围：

- 封装 Telegram 全量 API。
- 新增第三套调度方式或“标准模式 / 个人模式”开关。
- 恢复 `notice` / `bbot_notice` / `notice_bot` 发送通道。
- 为所有第三方插件做无修改兼容。
- 重写全部插件 UI 设计体系。
- 把日志中心继续扩展成通用 APM 平台。
- 因为发现新想法而新增 0.40.x 之外的产品入口。

如果执行中发现新需求，按下面规则处理：

| 类型 | 处理 |
| --- | --- |
| 命中第 24.5 节硬门禁 | 立刻修复，仍属于最终版范围 |
| 影响插件开发主路径 | 修复或补文档，仍属于最终版范围 |
| 只改善体验但不影响最终版成立 | 记录到后续版本，不阻塞最终版 |
| 需要新数据库模型或新产品模式 | 移出最终版，另开计划 |

### 27.3 当前剩余执行线

当 N1-N4 已经完成代码修复、目标测试和全量自动验证后，最终版剩余执行线固定为以下六步。不得跳过其中任一步直接宣称完成。

| 顺序 | 执行线 | 目标 | 完成证据 | 失败出口 |
| --- | --- | --- | --- | --- |
| R1 | 最终 diff review | 复核入口、动作、旧通道、native_raw、文档主路径没有硬门禁 | reviewer 记录、关键 diff 分类、审计命令分类 | `No-Go / 阻塞` |
| R2 | 浏览器验收 | 桌面和窄屏/PWA 验收 `/logs`、`/interaction`、`/plugins`、仓库、插件配置、`/settings` | 页面 URL、视口、通过/失败现象写入台账 | 最多 `可测` |
| R3 | 发布检查点 | 当前版本、中文 CHANGELOG、证据台账、commit、push 一致 | commit SHA、远端分支、版本 grep | `No-Go / 阻塞` |
| R4 | 服务器部署 | 部署到 `144.24.5.159`，确认迁移、健康、版本、日志 | 备份路径、远端 commit、健康检查、docker logs | `No-Go / 阻塞` |
| R5 | 真实链路 | 至少证明普通消息、管理员命令、插件 action、失败 action 可查 | trace_id、reason_code、event_action 记录 | 最多 `可测` |
| R6 | 回滚演练 | 证明出问题时可关闭 Trace/Event Bus/Inline 并恢复 | 开关修改前后值、降级日志、恢复记录 | `No-Go / 阻塞` |

R1-R6 全部通过，才允许把 `docs/release/<version>-final-evidence.md` 从 `No-Go / 阻塞` 或 `可测 / 待服务器实测` 改为 `Go / 已完成`。

### 27.4 最终执行算法

执行者每次准备收尾时按这个算法判断状态：

1. 先检查第 24.5 节硬门禁。命中任一条，结论是 `No-Go / 阻塞`。
2. 再检查第 24.6 节十项签收。没有当前 commit 证据的项不能标 `已完成`。
3. 再检查 R1-R6。R1-R3 通过但 R4-R6 缺失时，只能输出 `可测 / 待服务器实测`。
4. 如果自动验证、浏览器、远端、真实 trace、回滚演练全部通过，输出 `Go / 已完成`。
5. 如果证据台账和最终报告冲突，以证据台账为准；如果台账和实际命令冲突，以实际命令为准。

这个算法优先保护真实性，而不是保护“完成感”。最终版宁愿晚一点，也不能把半落地写成完成。

### 27.5 可并行执行但必须串行签收

为了快，可以同时派出子任务；为了稳，签收必须串行。

| 子任务 | 可并行内容 | 写入范围 | 主 Agent 签收点 |
| --- | --- | --- | --- |
| Review Agent | 只读审查入口、动作、旧通道、native_raw、文档旧主路径 | 不写文件 | 把命中归类为硬门禁、合法迁移、历史说明、测试覆盖 |
| UI Agent | 浏览器验收和必要小修 | `frontend/src/pages/*`、相关类型 | `tsc`、`vite build`、桌面/窄屏记录 |
| Docs Agent | 文档 grep、插件开发指南一致性、示例路径 | `README.md`、`docs/*`、`examples/*` | 不推荐旧 payload / 旧 notice / 旧 rule 主路径 |
| Release Agent | 版本、CHANGELOG、证据台账、commit/push 准备 | 版本文件、`CHANGELOG.md`、`docs/release/*` | 中文发布材料和实际 diff 一致 |
| Deploy Agent | 远端备份、部署、健康、真实 trace、回滚开关 | 远端环境和台账记录 | 没有备份不部署，健康失败不报 Go |

子任务可以并行产出证据，但只有主 Agent 能把状态升级为 `Go / 已完成`。子 Agent 自述不能直接作为完成证据。

### 27.6 最终版必须交付的四份材料

最终版完成时必须同时交付四份材料：

| 材料 | 作用 | 最低要求 |
| --- | --- | --- |
| 证据台账 | 判断 Go / No-Go | commit、命令、页面、trace、部署、回滚都有当前证据 |
| 中文 CHANGELOG | 给用户看实际更新 | 只写真实落地，不写愿景 |
| 朋友建议落地报告 | 解释讨论建议哪些已实现 | 按建议逐条映射 Event Bus、个人可信插件标准、UserBot 主控、交互 Bot 高频互动、日志排障 |
| 插件开发文档 | 让开发者按新版一次写对插件 | 最小 manifest、标准事件信封、`ctx.messages`、Trace 排障、迁移禁区完整 |

缺任一份材料，最终版最多是技术可测，不能称为“安全落地”。

### 27.7 最终版完成后的允许残余风险

最终版允许保留以下风险，但必须写入最终报告：

- 第三方插件仍需按新版文档迁移，平台不承诺旧插件零修改运行。
- `native_raw` 是可信插件能力，账号主人启用后需要自行承担业务风险。
- 关闭 Trace 后只能保留旧日志级别的排障能力，不等同于完整链路追踪。
- 少见 Telegram update 类型可能仍未封装，但不能影响 message、command、callback、inline、payment 主路径。
- 线上真实付款通知场景如果不方便实测，可以使用同版本 fixture 证明解析链路，并在报告中标注未做真实转账。

不允许保留以下风险：

- 旧 `notice` / `bbot_notice` 实际还能发送。
- 未声明插件能拿到完整 `native_raw`。
- 用户可见动作失败不落 `event_action`。
- 日志页无法解释未触发、插件失败或动作失败。
- 远端部署无备份、无健康检查或无法回滚。

### 27.8 为什么按本计划可以实现最终版

补完第 27 节后，计划已经具备最终版所需的五个条件：

- **方向封闭**：最终版只关闭入口、协议、动作、日志、文档、部署断点，不再无限新增功能。
- **范围封闭**：哪些需求纳入 0.40.x，哪些移到后续版本，已有明确判断规则。
- **证据封闭**：每个签收项都必须绑定当前 commit、命令、页面、trace 或远端记录。
- **执行封闭**：子任务可以并行，但状态升级只能按 R1-R6 串行完成。
- **发布封闭**：没有浏览器、远端、真实 trace 和回滚证据，就不能写 `Go / 已完成`。

因此，后续只要按第 24/25/26/27/28 节执行，就不是在追一个不断变化的“最终版”概念，而是在按可验证清单关闭已知断点。清单全部通过时，最终版成立；清单缺项时，结论自动降级。

## 28. 最终版执行冻结清单

本节是封口期的最后一页执行清单。它不新增产品范围，只把第 24/25/26/27 节压缩成执行者每天可以直接照做的 DoD、失败分流和证据格式。后续如果再次问“按计划是否能实现最终版”，答案只看本节和证据台账，不再重新解释架构方向。

### 28.1 唯一可推进队列

最终版只允许沿三条队列推进。任何新发现都必须先放入其中一条；放不进去的事项不属于本次最终版。

| 队列 | 允许处理 | 必须产出 | 不允许 |
| --- | --- | --- | --- |
| 代码封口 | 硬门禁 bug、Trace/Event Bus/MessageOps/Contract Guard/设置开关真实断链 | 最小测试、全量验证、证据台账更新 | 新产品模式、新 Telegram API 大扩展 |
| 页面封口 | `/logs`、`/interaction`、`/plugins`、仓库、插件配置、`/settings` 的最终版展示和窄屏/PWA 问题 | typecheck、build、桌面/窄屏验收记录 | 创造后端没有的字段、恢复账号详情为唯一入口 |
| 发布封口 | 版本、CHANGELOG、commit、push、服务器部署、真实 trace、回滚演练 | 当前 commit、远端 commit、备份、健康、trace_id、回滚记录 | 无备份改远端、覆盖 main、用本地可测冒充线上完成 |

推进顺序固定为：代码封口先达到 `可测`，页面封口再达到 `可测`，最后发布封口把整体从 `可测 / 待服务器实测` 推到 `Go / 已完成`。页面可以提前并行验证布局，但最终签收必须等代码契约稳定后重跑。

### 28.2 最终版 Definition of Done

以下 12 条全部满足，才允许称为最终版。任一条缺当前 commit 证据，结论自动降级。

| 编号 | 完成条件 | 证据 |
| --- | --- | --- |
| D1 | 所有主入口有 Trace：UserBot 消息、管理员命令、交互 Bot message/callback、inline、payment | 目标测试、真实或 fixture trace_id |
| D2 | 所有插件调度经过 Event Bus decision 或旧规则映射后的 decision | `event_span` / `event_action` / 测试断言 |
| D3 | 插件开发主路径只依赖标准事件信封、`event_subscriptions`、`capabilities`、`ctx.messages` | 开发指南、示例插件、验证脚本 |
| D4 | `native_raw` 只对显式声明插件下发，未声明插件拿不到完整原生数据 | 能力测试、日志页风险展示 |
| D5 | 旧 `notice` / `bbot_notice` / `notice_bot` 只会失败并提示迁移，不会发送 | `send_channel_deprecated` 测试和页面证据 |
| D6 | 所有用户可见动作成功或失败都会落 `event_action` | Delivery、scheduler、helper、callback、inline、settlement 测试 |
| D7 | 日志页能解释收到、未触发、插件失败、动作失败、旧日志 fallback | `/logs` 桌面和窄屏/PWA 验收 |
| D8 | 交互中心、插件中心、仓库、插件配置页能展示订阅、能力、风险、更新和警告 | 页面验收、长文本/空态/错误态记录 |
| D9 | 开发文档不再把旧规则、旧平铺 payload、旧 `raw_event`、旧 notice 当主路径 | grep 审计分类、文档 diff |
| D10 | 四处版本、中文 CHANGELOG、证据台账、commit、远端分支一致 | 版本 grep、commit SHA、push 记录 |
| D11 | 服务器 `144.24.5.159` 完成备份、迁移、健康检查、真实 trace 验收 | 备份路径、远端 commit、docker 摘要、trace_id |
| D12 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled` 完成关闭和恢复演练 | 开关前后值、降级日志、恢复记录 |

D1-D10 全部满足但 D11-D12 缺失时，最多只能输出 `可测 / 待服务器实测`。D1-D12 全部满足，且没有第 24.5 节硬门禁命中，才能输出 `Go / 已完成`。

### 28.3 失败分流

执行中失败时，不重新讨论方向，直接按失败类型分流：

| 失败 | 处理 | 允许结论 |
| --- | --- | --- |
| 测试失败且不是环境问题 | 回到代码封口修复，补目标测试 | `No-Go / 阻塞` |
| 测试因环境失败但有等价替代证据 | 在台账写明环境、替代命令和剩余风险 | 视证据可到 `可测` |
| 浏览器或 PWA 验收失败 | 修 UI 或记录为前端 blocker | 前端项最多 `半落地` |
| 文档 grep 命中旧主路径 | 改文档或降级为迁移说明 | 未分类前 `No-Go / 阻塞` |
| 远端部署失败 | 保留备份，停止签收，按部署日志修复 | 最多 `可测 / 待服务器实测` |
| 真实 trace 无法触发 | 用同版本 fixture 补证据并写明原因 | 最多 `可测 / 待服务器实测` |
| 回滚开关无效 | 修运行时开关或部署配置 | `No-Go / 阻塞` |

### 28.4 证据台账写入格式

所有最终版证据都写入当前版本对应的 `docs/release/<version>-final-evidence.md`。每条证据至少包含：

```text
控制项：
状态：未开始 / 半落地 / 可测 / 已完成
commit：
命令或页面：
结果：
trace_id 或截图/视口：
剩余风险：
复验时间：
```

不允许写“已验证”“应该没问题”“页面正常”这类无法复核的结论。命令证据必须写命令；页面证据必须写 URL 和视口；远端证据必须写远端 commit、版本和健康检查；真实链路证据必须写 trace_id 或说明为什么只能用 fixture。

### 28.5 最终签收口径

最终报告只允许三种出口：

| 出口 | 条件 | 对用户的说法 |
| --- | --- | --- |
| `Go / 已完成` | D1-D12 全部有当前 commit 证据，远端部署和回滚演练完成 | 最终版框架已落地，可给朋友看完整报告 |
| `可测 / 待服务器实测` | 本地自动验证、文档审计、浏览器验收通过，但缺远端真实 trace 或回滚证据 | 分支可推送给用户实测，但不宣称服务器最终版完成 |
| `No-Go / 阻塞` | 命中硬门禁、关键验证失败、文档仍推荐旧主路径、旧通道可发送、native_raw 越界、动作失败不落库 | 先回到对应队列修复，不发布 |

这就是最终版的冻结口径：最终版不是“所有想法都做完”，而是 D1-D12 证明入口、协议、动作、日志、页面、文档、发布和回滚全部闭环。按这张清单执行，完成时可以稳定称为最终版；没完成时，结论会自动且诚实地降级。

## 29. 最终版执行封条

本节是对第 24-28 节的最后压缩版，用于防止执行者在封口阶段重新发散。后续只要有人问“按这个计划能不能实现最终版”，答案固定为：能，但只能在本节前提成立、D1-D12 证据齐全、证据台账为 `Go / 已完成` 时才能这样宣称。

### 29.1 执行前提

最终版执行依赖以下外部条件。它们不是功能需求，但会决定本轮能否从 `可测 / 待服务器实测` 升级到 `Go / 已完成`：

| 前提 | 必须成立 | 不成立时的结论 |
| --- | --- | --- |
| 代码分支 | 当前工作在 `codex/0.33-interaction-framework` 或明确的新封口分支，且不覆盖 `main` | 停止发布，先切回正确分支 |
| 发布版本 | 四处版本文件、中文 `CHANGELOG.md`、证据台账指向同一版本 | `No-Go / 阻塞` |
| GitHub 远端 | 当前封口 commit 已推送到远端分支，远端 SHA 可核对 | 最多 `可测`，不能部署为最终版 |
| 服务器通道 | 能访问 `144.24.5.159` 的部署用户、密钥或等价部署通道 | 最多 `可测 / 待服务器实测` |
| 线上登录态 | 能用浏览器验收 `https://telebot.260505.xyz/` 的业务页 | UI 项最多 `半落地` |
| Telegram 测试入口 | 至少能触发普通消息、管理员命令、插件动作和失败动作；付款/Inline 不适合实测时必须有同版本 fixture | 缺真实 trace 时最多 `可测` |
| 回滚权限 | 能修改并恢复 `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled` | `No-Go / 阻塞` |

上述前提不成立时，不说明计划方向失败，只说明当前执行环境无法完成最终版签收。此时必须在 `docs/release/<version>-final-evidence.md` 写清缺口，不能把缺口藏进最终报告。

### 29.2 唯一执行顺序

最终版封口只按以下顺序推进。并行任务可以提前产出修复或证据，但主 Agent 签收必须按顺序升级状态。

```text
F0 事实冻结
  -> F1 契约与代码硬门禁
  -> F2 自动验证
  -> F3 文档与示例审计
  -> F4 浏览器业务页验收
  -> F5 发布检查点
  -> F6 服务器部署
  -> F7 真实 trace 与回滚演练
  -> F8 最终 Go / No-Go
```

每一步完成后必须更新证据台账。没有台账更新，状态不允许升级。

### 29.3 可并行任务包

为了快，可以同时派子 Agent，但每个任务包必须只处理自己的范围。

| 包 | 可并行执行内容 | 写入范围 | 必跑验证 | 交付证据 |
| --- | --- | --- | --- | --- |
| P1 代码硬门禁 | Trace 继承、Event Bus decision、MessageOps/action、旧通道失败、`native_raw` gate、回滚开关 | `backend/app/services/*`、`backend/app/worker/*`、相关测试 | `ruff`、目标 pytest、全量 pytest | D1、D2、D4、D5、D6、D12 的代码证据 |
| P2 前端封口 | 日志页、交互中心、插件页、仓库、插件配置、设置页桌面和窄屏/PWA | `frontend/src/pages/*`、`frontend/src/components/*`、前端类型 | `tsc -b`、`vite build`、浏览器验收 | D7、D8 页面证据 |
| P3 文档示例 | 插件开发指南、README、AI/HTTP 文档、示例插件、验证脚本 | `docs/*`、`README.md`、`examples/*`、`scripts/validate-*` | 示例验证、文档 grep 审计 | D3、D9 文档证据 |
| P4 发布材料 | 版本号、中文 CHANGELOG、证据台账、commit、push | 四处版本文件、`CHANGELOG.md`、`docs/release/*` | 版本 grep、`git diff --check`、远端 SHA 核对 | D10 发布证据 |
| P5 部署验收 | 服务器备份、部署、迁移、健康、真实 trace、回滚开关 | 远端环境、证据台账 | `make prod-update` 或等价部署命令、健康检查、docker logs | D11、D12 线上证据 |

任务包之间的禁区固定：

- P2 不创造后端没有的字段；缺字段时回报 P1。
- P3 不把旧规则、旧平铺 payload、旧 `notice` 写成推荐主路径。
- P4 不把未验证的未来能力写入 CHANGELOG。
- P5 不在无备份时改远端，不在健康失败时写 Go。
- 所有包都不得恢复 `notice` / `bbot_notice` / `notice_bot` 为发送通道。

### 29.4 当前版本的最短签收路线

如果当前代码已经处于 `0.40.x` 封口状态，并且 N1-N4 与追加 P0 修复已通过自动验证，则后续最短路线固定为：

1. 复核工作树，只允许保留明确不纳入发布的未跟踪文件，并在台账登记。
2. 重跑最终自动验证组：后端 `ruff`、目标 pytest、全量 pytest、示例验证、已安装插件验证、前端 `tsc`、前端 build、Alembic head/offline SQL、`git diff --check`。
3. 做文档 grep 审计，把旧概念命中逐条分类为迁移说明、废弃说明、测试 fixture、兼容层或必须修复。
4. 用浏览器验收 `/logs`、`/interaction?aid=1`、`/plugins`、`/plugins/manage?tab=plugins`、真实插件配置页、`/settings`，桌面和窄屏/PWA 都要记录 URL、视口和结论。
5. 若有代码、文档或证据台账改动，按版本规则决定是否需要 patch（补丁版本） bump；发布检查点必须中文 CHANGELOG、中文 commit。
6. 推送远端分支后，核对远端 SHA 与本地 SHA 一致。
7. 部署到 `144.24.5.159`：部署前备份，部署后核对远端 commit、版本 API、迁移、容器状态、健康检查和关键日志。
8. 触发真实或 fixture trace，至少覆盖普通消息、管理员命令、插件 action、失败 action；Inline 和付款如不能真实触发，必须用同版本 fixture 并写原因。
9. 分别关闭并恢复 `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled`，记录关闭前值、关闭后表现、恢复后值。
10. 对照 D1-D12 改证据台账结论；全部有证据才写 `Go / 已完成`。

这十步不能跳过。若执行中发现新 bug，只能回到对应任务包修复，再从受影响步骤重跑。

### 29.5 最终版成立证明

按本计划执行完后，最终版成立的理由必须能用下面五句话证明：

1. 入口闭环：所有主 Telegram 来源都先产生 Trace，再通过 Event Bus decision 或旧规则映射 decision 进入插件。
2. 协议闭环：新插件只需要标准事件信封、`event_subscriptions`、`capabilities` 和 `ctx.messages`，不需要理解旧规则驱动。
3. 动作闭环：插件的发送、编辑、删除、置顶、按钮 ACK、Inline Answer、settlement 成功或失败都会落 `event_action`。
4. 排障闭环：日志页能解释收到、未触发、插件失败、动作失败、旧通道废弃、`native_raw` 下发与否。
5. 发布闭环：版本、CHANGELOG、文档、证据台账、远端部署、真实 trace、回滚演练全部绑定当前 commit。

任何一句无法用证据台账证明，最终版不成立。

### 29.6 最终版报告模板

最终报告必须按下面结构写，且结论只能来自证据台账：

```text
结论：
Go / 已完成 或 可测 / 待服务器实测 或 No-Go / 阻塞

版本与分支：
版本：
本地 commit：
远端分支：
远端 commit：

D1-D12：
D1 ...
...
D12 ...

已验证：
自动验证：
浏览器页面：
远端部署：
真实 trace：
回滚演练：

朋友建议落地：
个人可信插件标准：
UserBot 主控 + interaction_bot 高频交互：
统一事件信封：
MessageOps / Delivery Executor：
日志排障：
开发文档：

允许残余风险：
...

不允许残余风险：
确认不存在旧 notice 可发送、native_raw 越界、动作失败不落库、无备份部署、文档推荐旧主路径。
```

若实际结论不是 `Go / 已完成`，报告必须把缺口写在开头，不能把缺口放在末尾弱化。

### 29.7 最终判定

本计划已经足以实现所谓的“最终版”，但最终版不是靠计划文本本身实现，而是靠第 29.4 节十步和 D1-D12 证据实现。执行完成后：

- D1-D10 有证据，D11-D12 缺失：只能说“本地和分支可测，待服务器实测”。
- D1-D12 全部有当前 commit 证据，且第 24.5 节硬门禁没有命中：可以说“最终版框架落地”。
- 任一硬门禁命中：必须说 `No-Go / 阻塞`，先修复再谈发布。

这就是最终封条：不再新增愿景，不再放宽门槛，不再把半落地当完成。按封条执行到底，最终版可以实现；没有证据时，计划会自动阻止错误宣称。

## 30. 最终版签收执行补丁

本节用于回答最后一个执行问题：如何保证“按计划做完”之后真的能成为所谓的最终版，而不是又变成一份更长的愿景清单。结论是：后续执行不再新增产品范围，只按本节的状态机、闸门、复验点和失败回退来推进。

### 30.1 最终版是证据状态，不是主观状态

最终版只能由证据台账推导出来。执行过程中所有口头状态必须落到下面状态机之一：

```text
No-Go / 阻塞
  -> 本地可测
  -> 业务页可测
  -> 分支发布可测
  -> 服务器已部署
  -> 真实链路可追踪
  -> 回滚已演练
  -> Go / 已完成
```

状态升级规则：

- `本地可测`：D1-D6、D3/D9 的自动验证和文档审计通过。
- `业务页可测`：D7-D8 的桌面和窄屏/PWA 业务页验收通过。
- `分支发布可测`：D10 的版本、CHANGELOG、commit、远端分支一致。
- `服务器已部署`：D11 的备份、迁移、健康检查和远端版本一致。
- `真实链路可追踪`：至少一组真实或同版本 fixture trace 覆盖普通消息、管理员命令、插件 action、失败 action。
- `回滚已演练`：D12 三个开关完成关闭和恢复，并记录降级表现。
- `Go / 已完成`：D1-D12 全部有当前 commit 证据，且没有命中第 24.5 节硬门禁。

任何阶段只要发现硬门禁，立即降回 `No-Go / 阻塞`。不能用“前面很多项都通过了”抵消硬门禁。

### 30.2 执行者现场事实冻结

每次继续执行前，必须先现场采集事实，不能沿用历史聊天、历史台账或子 Agent 自述。

最低事实冻结命令：

```bash
git rev-parse --show-toplevel
git status --short --branch -uall
git rev-parse HEAD
git ls-remote origin refs/heads/codex/0.33-interaction-framework
rg -n "__version__|version =|\"version\"|APP_VERSION" backend/app/__init__.py backend/pyproject.toml frontend/package.json frontend/src/lib/version.ts
curl -fsS https://telebot.260505.xyz/api/system/version
```

事实冻结必须写入 `docs/release/<version>-final-evidence.md`：

- 当前本地 commit。
- 当前远端分支 commit。
- 当前服务器版本。
- 工作树未提交和未跟踪文件的处理结论。
- 本轮证据是否仍绑定当前 commit。

如果工作树有未跟踪文件，例如临时 `frontend/pnpm-workspace.yaml`，必须明确“不纳入发布、不 stage、不删除”。如果该文件影响构建或部署，先处理影响；如果不影响，只登记，不为了干净工作树而擅自删除。

### 30.3 七个签收闸门

最终版签收只允许按七个闸门推进。每个闸门完成后更新证据台账；如果失败，按“重跑边界”从对应闸门重新开始。

| 闸门 | 目标 | 必须证据 | 失败时回到 |
| --- | --- | --- | --- |
| G0 事实冻结 | 当前分支、版本、工作树、远端和服务器状态明确 | 命令输出摘要、commit、服务器版本 | G0 |
| G1 契约硬门禁 | 入口、协议、动作、旧通道、native_raw、回滚开关没有硬断链 | ruff、目标 pytest、全量 pytest、关键 diff review | G1 |
| G2 文档示例 | 开发者只按新版主路径即可写插件 | 文档 grep 分类、示例验证、插件验证脚本 | G2，必要时回 G1 |
| G3 业务页验收 | 日志、交互、插件、仓库、配置、设置页面可排障 | URL、视口、状态、失败截图或描述 | G3，字段不一致回 G1 |
| G4 发布检查点 | 版本、中文 CHANGELOG、证据台账、commit、push 一致 | 版本 grep、git diff/check、远端 SHA | G4，发现代码变更回 G1 |
| G5 服务器部署 | `144.24.5.159` 运行当前分支 | 备份路径、远端 commit、版本 API、迁移、docker/health | G5，部署后代码变更回 G1 |
| G6 链路和回滚 | 真实或 fixture trace 可查，三个回滚开关可关闭恢复 | trace_id、reason_code、event_action、开关前后值 | G6，开关无效回 G1 |

G0-G6 全部通过后，才能执行最终报告。最终报告不是新的闸门，只是把台账结论压缩给用户和朋友看。

### 30.4 自动验证组的当前口径

最终版的自动验证组固定如下。命令失败时必须记录第一条可行动错误；环境失败必须说明解释器、依赖或服务状态，不能写成代码失败。

```bash
cd backend && .venv/bin/ruff check app ../scripts/validate-plugin-examples.py
cd backend && .venv/bin/pytest -q
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
cd backend && .venv/bin/alembic heads
cd backend && .venv/bin/alembic upgrade head --sql >/tmp/telepilot-alembic-final.sql
cd frontend && ./node_modules/.bin/tsc -b --pretty false
cd frontend && ./node_modules/.bin/vite build
git diff --check
```

针对最终版硬门禁，还必须保留目标测试组。测试名如因重构变化，先用 `rg` 找等价测试，并在台账中写清替代关系。

```bash
cd backend && .venv/bin/pytest -q \
  app/tests/test_account_bot.py::test_interaction_plain_message_routes_to_worker_entry_as_message \
  app/tests/test_account_bot.py::test_interaction_callback_routes_to_worker_entry_and_answers_callback \
  app/tests/test_account_bot_auto_award.py::test_account_bot_auto_award_records_trace_action \
  app/tests/test_worker_command.py \
  app/tests/test_plugin_security_regression.py \
  app/tests/test_scheduler_runtime.py
```

全量 pytest 通过不能替代目标测试组，因为目标测试组证明的是最终版硬门禁是否关闭。

### 30.5 业务页验收剧本

业务页验收不能只写“页面正常”。每个页面必须记录 URL、视口、账号/Bot 上下文、通过状态和失败原因。

| 页面 | 桌面必须看 | 窄屏/PWA 必须看 |
| --- | --- | --- |
| `/logs` | 总览、消息链路、插件诊断、命令链路、动作发送、原始日志 fallback；成功、未命中、插件失败、动作失败、旧 notice 失败 | 底部导航不重叠，筛选和详情可操作，长 reason_code/错误文本不撑破 |
| `/interaction?aid=1` | 顶部账号/Bot 选择、规则列表、规则详情、插件入口、触发词、会话状态、最近错误 | 列表压缩后仍能开关规则和进入详情，保存按钮不遮挡内容 |
| `/plugins` | usage、订阅、能力、风险、规范警告、安装/移除状态 | 插件名长文本、警告、按钮不换行错乱 |
| `/plugins/manage?tab=plugins` | 仓库刷新、单仓库一键更新、私有库和 `tree/<branch>` URL 提示、更新失败脱敏 | 操作按钮可触达，错误信息不横向溢出 |
| 真实插件配置页 | 使用说明、总开关、插件配置、插件预览；缺 usage 时红色高级警告 | 底部导航、保存操作条、长配置字段不重叠 |
| `/settings` | Trace/Event Bus/Inline 回滚开关、高级风险提示、保存反馈 | 开关说明和状态不被底部导航遮挡 |

如果没有线上登录态，G3 不能签收，只能写 `半落地` 或 `可测 / 待服务器实测`。可以用本地已登录环境或固定 fixture 做补充，但不能把未登录页面当业务页验收。

### 30.6 部署和回滚执行底线

部署到 `144.24.5.159` 前必须满足：

- 已有正确 SSH 用户、密钥或等价部署通道。
- 已备份 `.env`、compose 文件和数据库，并记录路径。
- 当前分支已推送，远端 SHA 可核对。
- 当前证据台账至少达到 `分支发布可测`。

推荐部署命令：

```bash
cd /opt/telepilot
./deploy/backup.sh
TELEPILOT_UPDATE_BRANCH=codex/0.33-interaction-framework make prod-update
```

部署后必须记录：

```bash
git rev-parse HEAD
docker compose ps
docker compose logs --tail=100 web
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS https://telebot.260505.xyz/api/system/version
```

回滚演练必须逐个进行，不能一次性全关：

1. 记录 `trace_enabled` 当前值，关闭，触发或导入一条降级场景，确认降级提示或旧日志 fallback，再恢复原值。
2. 记录 `event_bus_delivery_enabled` 当前值，关闭，确认旧规则兜底或 degraded 标记，再恢复原值。
3. 记录 `inline_updates_enabled` 当前值，关闭，确认 inline update 不进入新投递或有明确跳过 reason，再恢复原值。

如果没有权限读取或修改这些开关，最终状态不能是 `Go / 已完成`。

### 30.7 真实链路最小证明集

最终版不要求线上证明所有理论场景，但至少要证明下面四类链路；Inline 和付款如果不适合真实触发，可以用同版本 fixture 补充，但必须写明原因。

| 链路 | 最低输入 | 必须看到 |
| --- | --- | --- |
| 普通消息未命中 | 允许群内一条无关键词文本 | `event_trace` receive/normalize，`event_span` skipped，稳定 reason_code |
| 管理员命令 | 账号主人或管理员命令 | command parse、permission、handler/plugin、userbot_reply 或 action |
| 插件正常 action | 玩家关键词、按钮或测试插件 | matched/delivered、plugin invoke、`event_action.status=success`、actual_send_via |
| 插件失败 action | 缺目标 message_id、旧 notice、缺 token 或测试失败动作 | `event_action.status=failed/skipped`、reason_code/error_code、中文说明 |

可选但强烈建议证明：

- callback_query 的 `answer_callback` 成功或失败。
- inline_query 的 `answer_inline_query` 成功或失败。
- payment_confirmed / external_payment_notice 的 payer/player/settlement 归属。
- `telegram_native_raw` 声明插件和未声明插件的对照。

### 30.8 文档最终审计边界

开发文档最终审计的目标不是删除所有旧词，而是确保旧词不会作为推荐主路径出现。

必须逐条分类的 grep：

```bash
rg -n "notice|bbot_notice|notice_bot|raw_event|payload\\[\"text\"\\]|event\\.reply|event\\.respond|ctx\\.client|旧规则驱动" README.md docs examples scripts backend frontend
rg -n "event_subscriptions|capabilities|ctx\\.messages|MessageOps|Trace|reason_code|send_channel_deprecated|native_raw" README.md docs examples scripts backend frontend
```

允许保留的命中：

- 历史背景。
- 迁移说明。
- 废弃值失败测试。
- 兼容层实现。
- 风险说明。

不允许保留的命中：

- 新插件最小示例仍使用旧平铺 payload。
- 文档建议插件使用 `notice` / `bbot_notice` / `notice_bot` 发送。
- 文档建议通过 `ctx.client` 或 `event.reply/respond` 直发可见消息。
- 文档把旧交互规则写成唯一运行主路径。

### 30.9 版本和发布材料规则

如果本轮只修改计划文档、证据台账或未推送的草稿，不强制 bump 版本。只要准备提交、推送稳定检查点、部署、PR 或 release，就必须按项目规则判断版本：

- 破坏兼容：`MAJOR（主版本）`，0.x 阶段一般仍需单独确认。
- 新能力或入口重组：`MINOR（次版本）`。
- 封口修复、文档、验收补强、UI 小修：`PATCH（补丁版本）`。

发布检查点必须同步：

- `backend/app/__init__.py`
- `backend/pyproject.toml`
- `frontend/package.json`
- `frontend/src/lib/version.ts`
- `CHANGELOG.md`
- `docs/release/<version>-final-evidence.md`

中文 CHANGELOG 只能写已经落地和已经验证的内容，不写“计划将支持”。commit、PR、release 文案必须使用中文。

### 30.10 最终执行可以并行，但最终签收只能串行

允许并行：

- 只读 review agent 审查硬门禁和文档旧主路径。
- UI agent 验收或修复业务页。
- Docs agent 审计插件开发指南和示例。
- Deploy agent 在主 Agent 授权和备份后执行远端部署。

不允许并行：

- 多个 agent 同时改同一运行时入口。
- UI 先创造后端不存在字段。
- Docs 先写未落地能力。
- Release agent 在自动验证或浏览器验收前 bump/commit/push。
- Deploy agent 在没有备份和正确分支证据时改远端。

主 Agent 是唯一签收 owner。子 Agent 的报告只能作为证据线索，不能直接把 D 项标为 `已完成`。

### 30.11 最终报告的硬格式

最终报告开头必须先写结论，不能先讲进展。

如果是 `Go / 已完成`，第一段必须包含：

- 版本。
- 本地 commit。
- 远端 commit。
- 服务器版本。
- 证据台账文件。
- D1-D12 全部通过。

如果不是 `Go / 已完成`，第一段必须直接列出缺口，例如：

```text
结论：可测 / 待服务器实测。
缺口：服务器仍是 0.37.0；SSH 无法进入 144.24.5.159；缺业务页登录态；缺真实 trace 和回滚演练。
```

不允许把缺口放在末尾，也不允许用“基本完成”“应该没问题”“只差实测”替代正式状态。

### 30.12 为什么这版计划足以实现最终版

本计划已经具备最终版落地所需的四个闭环：

1. **产品闭环**：个人可信插件标准、UserBot 主控、交互 Bot 高频出口、转账/发奖仍走 userbot 或 settlement 的边界已经固定。
2. **技术闭环**：Source Adapter、Event Bus、标准事件信封、MessageOps、Delivery Executor、Trace 和日志页的职责已经固定。
3. **开发者闭环**：插件开发指南、示例、验证脚本和文档审计都围绕 `event_subscriptions`、`capabilities`、`ctx.messages` 和 Trace 排障。
4. **发布闭环**：D1-D12、G0-G6、证据台账、服务器部署、真实 trace 和回滚演练组成了可复核签收链。

因此，后续不需要再继续扩写愿景。只要按本节执行，完成 D1-D12 并把证据台账升级为 `Go / 已完成`，就可以稳定称为最终版；如果缺服务器、登录态、真实 trace 或回滚证据，计划会自动把结论降级为 `可测 / 待服务器实测` 或 `No-Go / 阻塞`，不会误报完成。

## 31. 最终版收束补丁

本节用于把前面的执行协议收束成最后一张施工图。它不新增产品能力，也不扩大 0.40.x 范围；它只回答一个问题：后续如果严格按计划执行，怎样确保结果不是“又一个阶段性版本”，而是真正可以签收的最终版框架落地。

结论固定为：

> 按第 31 节执行时，最终版只由 D1-D12 的当前 commit 证据决定。代码、页面、文档、发布、部署、真实 trace 和回滚演练全部闭环时，输出 `Go / 已完成`；缺服务器或真实链路证据时，输出 `可测 / 待服务器实测`；命中硬门禁时，输出 `No-Go / 阻塞`。

### 31.1 最终版唯一收束路径

后续执行不再新增章节、不再重新讨论“个人模式 / 标准模式”、不再扩展 Telegram 全 API。唯一收束路径如下：

```text
G0 事实冻结
  -> G1 契约和运行时硬门禁
  -> G2 文档、示例和验证脚本
  -> G3 业务页桌面和窄屏验收
  -> G4 版本、CHANGELOG、commit、push
  -> G5 服务器备份、部署、迁移、健康检查
  -> G6 真实 trace、失败 trace、回滚开关演练
  -> Go / 已完成
```

每个闸门只能向前升级一次。后续任何代码、schema、前端类型、文档主路径或部署脚本变更，都必须回到最早受影响的闸门重新跑。例子：

- 改后端事件字段：回到 G1，并重跑 G2/G3/G4。
- 改插件开发指南示例：回到 G2，并重跑示例验证和文档 grep。
- 改页面展示字段：回到 G3，并重跑前端 typecheck/build 和桌面/窄屏验收。
- 部署后发现服务端代码变更：回到 G1，不允许只热修远端后直接报 Go。

### 31.2 D1-D12 最终施工映射

执行者只按下表推进。某项没有达到“最低签收证据”，就不能把该项写成 `已完成`。

| D 项 | 必须关闭的断点 | 主要落点 | 最低自动证据 | 最低人工/远端证据 | 失败出口 |
| --- | --- | --- | --- | --- | --- |
| D1 主入口 Trace | 任一消息入口没有 trace | `account_bot_runtime.py`、`runtime.py`、`command.py`、`event_bus.py` | message、command、callback、inline、payment 目标测试 | trace 详情能看到 receive/normalize/start | `No-Go` |
| D2 Event Bus decision | 插件被旧路径直调 | `event_bus.py`、loader、旧规则映射层 | decision matched/skipped/delivered 断言 | 日志页能解释未触发 reason_code | `No-Go` |
| D3 新插件主路径 | 开发者仍需旧 payload 或旧规则 | `docs/PLUGIN-*.md`、`examples/plugins/*`、验证脚本 | 示例插件验证通过 | 文档复制最小示例可说明 message/command/callback/inline/payment | `可测`，若文档推荐旧主路径则 `No-Go` |
| D4 native_raw 边界 | 未声明插件拿完整原生数据 | manifest parser、Contract Guard、Trace redactor | 声明/未声明对照测试 | 插件页和日志页显示能力、风险、是否下发 | `No-Go` |
| D5 旧 notice 收口 | `notice` / `bbot_notice` 仍能发送 | Delivery、Contract Guard、验证脚本 | `send_channel_deprecated` 测试 | 日志页动作失败有中文迁移提示 | `No-Go` |
| D6 action 全落库 | 发送失败或复合动作假成功 | `message_ops.py`、`delivery.py`、scheduler、settlement | send/edit/delete/pin/callback/inline/settlement 成功失败测试 | 动作页能按 trace/action 查到 requested/actual channel | `No-Go` |
| D7 日志可排障 | 日志页只是旧 runtime log | Logs API、`Logs.tsx` | logs API 过滤测试、前端 typecheck/build | `/logs` 桌面和窄屏覆盖成功、未命中、插件失败、动作失败 | `可测`，核心问题不可解释则 `No-Go` |
| D8 业务 UI 可操作 | 交互和插件配置仍靠旧入口猜状态 | `Interaction`、`Extensions`、插件配置、`Settings` | 前端 typecheck/build | `/interaction`、`/plugins`、仓库、配置页、`/settings` 桌面和窄屏验收 | `可测` |
| D9 文档不漂移 | 文档仍推荐旧系统 | README、docs、examples、scripts | 旧词 grep 分类、新词 grep 覆盖 | 开发指南按最终七步可读 | `可测`，推荐旧主路径则 `No-Go` |
| D10 发布材料一致 | 版本、CHANGELOG、commit、远端分支不一致 | 四处版本文件、`CHANGELOG.md`、证据台账 | version grep、`git diff --check` | 中文 commit、push 后远端 SHA 对齐 | `No-Go` |
| D11 服务器上线 | 本地可测冒充线上完成 | 远端 `/opt/telepilot`、部署脚本、证据台账 | 远端迁移/health/version 命令 | 备份路径、远端 commit、docker 状态、线上 trace_id | 最多 `可测 / 待服务器实测` |
| D12 回滚演练 | 开关只是字段、不能降级恢复 | system settings、runtime 开关、证据台账 | 设置 API roundtrip、运行时开关测试 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled` 逐个关闭恢复 | `No-Go` |

这张表是最终版施工总图。任何更细的任务卡、子 Agent 报告或临时修复，都必须能落回这 12 项之一；落不回去的事项不进入最终版范围。

### 31.3 子 Agent 并行合同

为了快，可以继续分配子 Agent，但每个子 Agent 必须拿到下面四件事：写入范围、禁区、验证命令、交付格式。主 Agent 保留唯一签收权。

| 子任务 | 适合并行内容 | 写入范围 | 禁区 | 必须回报 |
| --- | --- | --- | --- | --- |
| Runtime Agent | D1、D2、D4、D5、D6、D12 的后端断点 | `backend/app/services/*`、`backend/app/worker/*`、后端测试 | 不改前端字段幻想，不恢复旧 notice，不绕过 Event Bus | 改动文件、目标测试、全量 pytest/ruff、reason_code/action 证据 |
| UI Agent | D7、D8 的页面和窄屏/PWA | `frontend/src/pages/*`、相关组件和类型 | 不创造后端没有的字段，不把账号详情恢复成唯一入口 | URL、视口、截图或现象、typecheck/build |
| Docs Agent | D3、D9 的开发文档和示例 | `README.md`、`docs/*`、`examples/*`、`scripts/validate-*` | 不推荐旧 payload、旧 `ctx.client`、旧 notice、旧规则主路径 | grep 分类、示例验证、文档入口清单 |
| Release Agent | D10 的版本和发布材料 | 版本文件、`CHANGELOG.md`、证据台账 | 自动验证或浏览器验收前不 commit/push，不覆盖 main | 版本 grep、中文 CHANGELOG 摘要、待 stage 文件 |
| Deploy Agent | D11、D12 的服务器部署和回滚 | 远端部署环境、证据台账 | 没备份不改远端，健康失败不报 Go | 备份路径、远端 commit、version/health、trace_id、开关前后值 |

子 Agent 报告不能直接升级 D 项状态。主 Agent 必须复核命令、diff、页面或远端证据后，才能把证据台账从 `半落地` 升到 `可测` 或 `已完成`。

### 31.4 最终版禁止借用的证据

以下证据一律不能用于最终签收：

- 历史聊天里说过“已经修了”。
- 子 Agent 自述“测试通过”，但没有命令和当前 commit。
- 旧版本、旧 commit、旧部署环境的测试输出。
- 只打开未登录页面或空白壳页面，就宣称业务页验收通过。
- 只看到 runtime log 有文本，就宣称 Trace 可排障。
- 只跑全量 pytest，通过后替代旧 notice、native_raw、action failed、Event Bus decision 的目标测试。
- 只部署成功但没有备份、远端版本、健康检查、真实 trace 或回滚演练。

如果只能拿到上述证据，证据台账必须写“历史基线”或“不可签收”，不能写 `已完成`。

### 31.5 最终版必须重跑的最小命令包

进入 G4 发布检查点前，最低命令包固定如下。除非环境不可用且有等价替代证据，否则不能删减。

```bash
git status --short --branch -uall
rg -n "__version__|version =|\"version\"|APP_VERSION" backend/app/__init__.py backend/pyproject.toml frontend/package.json frontend/src/lib/version.ts
(
  cd backend
  .venv/bin/ruff check app ../scripts/validate-plugin-examples.py
  .venv/bin/pytest -q
)
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
(
  cd backend
  .venv/bin/alembic heads
  .venv/bin/alembic upgrade head --sql >/tmp/telepilot-alembic-final.sql
)
(
  cd frontend
  ./node_modules/.bin/tsc -b --pretty false
  ./node_modules/.bin/vite build
)
rg -n "notice|bbot_notice|notice_bot|raw_event|payload\\[\"text\"\\]|event\\.reply|event\\.respond|ctx\\.client|旧规则驱动" README.md docs examples scripts backend frontend
rg -n "event_subscriptions|capabilities|telegram_native_raw|native_raw_meta|answer_inline_query|chosen_inline_result|settlement|send_channel_deprecated" README.md docs backend frontend examples scripts
git diff --check
```

目标测试组必须和全量测试一起保留：

```bash
(
  cd backend
  .venv/bin/pytest -q \
  app/tests/test_account_bot.py::test_interaction_plain_message_routes_to_worker_entry_as_message \
  app/tests/test_account_bot.py::test_interaction_callback_routes_to_worker_entry_and_answers_callback \
  app/tests/test_account_bot.py::test_interaction_inline_query_routes_through_event_bus_and_records_trace \
  app/tests/test_account_bot.py::test_interaction_chosen_inline_result_routes_through_event_bus \
  app/tests/test_account_bot.py::test_interaction_delivery_executor_answer_inline_query_records_success_and_failure \
  app/tests/test_account_bot_auto_award.py::test_account_bot_auto_award_records_trace_action \
  app/tests/test_worker_command.py \
  app/tests/test_plugin_security_regression.py \
  app/tests/test_scheduler_runtime.py \
  app/tests/test_plugin_loader.py \
  app/tests/test_system_settings.py
)
```

如果某个测试名已经改动，执行者必须先用 `rg` 找等价测试，把替代关系写入证据台账，再运行。不能静默跳过。

### 31.6 最终版页面验收最低样本

G3 至少验收以下样本。没有登录态时，不能把 G3 写成 `已完成`。

| 页面 | 桌面视口 | 窄屏/PWA 视口 | 必须记录 |
| --- | --- | --- | --- |
| `/logs` | 1365px 以上 | 390px-480px | 成功链路、未命中、插件失败、动作失败、原始日志 fallback |
| `/interaction?aid=1` | 1365px 以上 | 390px-480px | 账号/Bot 选择、规则列表、详情编辑、保存按钮、最近状态 |
| `/plugins` | 1365px 以上 | 390px-480px | usage、订阅、能力、风险、规范警告、安装/移除状态 |
| `/plugins/manage?tab=plugins` | 1365px 以上 | 390px-480px | 刷新、私有库、`tree/<branch>`、单仓库一键更新、错误脱敏 |
| `/accounts/1/features/<plugin_key>?from=plugins` | 1365px 以上 | 390px-480px | 使用说明、总开关、配置容器、自定义样式、预览建议、缺 usage 红色警告 |
| `/settings` | 1365px 以上 | 390px-480px | Trace/Event Bus/Inline/native_raw 开关、保存反馈、降级提示 |

每页都必须检查长中文、长英文、长插件名、长 reason_code、长错误消息、底部导航、固定操作按钮。任何重叠、横向溢出或按钮不可触达，都不能把对应 UI 项写成 `已完成`。

### 31.7 服务器最终版签收最低样本

G5/G6 的最低远端证据固定为：

```bash
ssh root@144.24.5.159 '
  DEPLOY_DIR=$(docker inspect telepilot-web-1 --format "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}")
  test -n "$DEPLOY_DIR"
  cd "$DEPLOY_DIR"
  git rev-parse HEAD
'
ssh root@144.24.5.159 '
  DEPLOY_DIR=$(docker inspect telepilot-web-1 --format "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}")
  test -n "$DEPLOY_DIR"
  cd "$DEPLOY_DIR"
  docker compose ps
  docker compose logs --tail=100 web
'
curl -fsS https://telebot.260505.xyz/api/system/version
curl -fsS https://telebot.260505.xyz/healthz
```

如果 compose label 没有 working_dir，先执行第 31.11 节探测流程，不允许假设 `/opt/telepilot`。如果实际健康检查路径不是 `/healthz`，执行者必须用代码或部署配置确认真实路径，并把替代路径写入证据台账。

服务器 trace 最少保留四条：

- 普通消息未命中 trace。
- 管理员命令 trace。
- 插件成功 action trace。
- 插件失败 action trace。

Inline 和付款可以用同版本 fixture 补充，但必须在台账写清为什么不在线上真实触发。不能用“线上不方便”直接跳过。

### 31.8 最终版完成判定伪代码

最终收口时按下面伪代码判定，不再凭感觉判断：

```text
if hit_hard_gate:
    status = "No-Go / 阻塞"
elif any(D1..D10 missing current_commit_evidence):
    status = "No-Go / 阻塞"
elif D1..D10 passed and (D11 missing or D12 missing):
    status = "可测 / 待服务器实测"
elif D1..D12 passed and evidence_ledger.status == "Go / 已完成":
    status = "Go / 已完成"
else:
    status = "No-Go / 阻塞"
```

其中 `current_commit_evidence` 的含义是：证据中的命令、页面、trace、远端版本或回滚记录，都能对应当前准备发布的 commit。历史 commit 证据不能自动继承。

### 31.9 为什么按本节后可以实现最终版

本节让“最终版”变成可执行结果，而不是口号，原因是：

- 范围被收束到 D1-D12，不再继续发散。
- 每个 D 项都有代码、测试、页面或远端证据要求，不能空口签收。
- 子 Agent 可以并行，但状态升级由主 Agent 串行复核，避免互相覆盖。
- 本地、浏览器、发布、服务器、真实 trace、回滚演练被拆成 G0-G6，任何缺口都会自动降级。
- 允许残余风险和一票否决项已经明确，不会把旧通道、native_raw 越界、动作失败不落库这类问题带进最终版。

所以，后续不需要再继续完善愿景；需要的是按第 31 节把证据跑全。证据跑全，最终版成立；证据缺失，结论自动降级。这就是 TelePilot 插件框架“最终版”的可实现边界。

### 31.10 最终版交付包

最终版不是单个 commit，也不是“页面能打开”。最终版交付包必须同时包含以下材料，缺任一项都不能报 `Go / 已完成`：

| 交付物 | 必须包含 | 记录位置 |
| --- | --- | --- |
| 代码版本 | 本地分支、远端分支、本地 SHA、远端 SHA、四处版本号 | 证据台账 G0/G4 |
| 发布说明 | 中文 CHANGELOG、中文 commit、0.x SemVer 级别说明 | `CHANGELOG.md`、git log、证据台账 |
| 自动验证 | ruff、目标 pytest、全量 pytest、插件示例验证、已安装插件验证、alembic、前端 typecheck/build、diff check | 证据台账 G1/G2/G4 |
| 文档审计 | 旧词 grep 分类、新契约 grep 覆盖、示例插件可通过验证、开发指南按新主路径可读 | 证据台账 G2 |
| 业务页验收 | `/logs`、`/interaction`、`/plugins`、仓库、真实插件配置页、`/settings` 的桌面和窄屏/PWA 结果 | 证据台账 G3 |
| 远端部署 | 备份路径、部署目录、远端 SHA、容器状态、迁移状态、版本 API、健康检查、关键日志 | 证据台账 G5 |
| 真实链路 | 普通未命中、管理员命令、插件成功动作、插件失败动作的 trace_id 或同版本 fixture 说明 | 证据台账 G6 |
| 回滚演练 | `trace_enabled`、`event_bus_delivery_enabled`、`inline_updates_enabled` 的关闭前值、关闭后表现、恢复后值 | 证据台账 G6 |
| 残余风险 | 只允许列非硬门禁风险；如果是硬门禁，结论必须降级 | 最终报告 |

交付包只认当前 commit 的证据。只要发布后又改代码、文档、前端类型、部署脚本或设置默认值，就必须回到受影响的 G 闸门重新生成交付包。

### 31.11 服务器部署目录不确定时的探测和接管路径

G5 不允许假设服务器一定在 `/opt/telepilot`。如果目标服务器已经有运行中的 TelePilot 容器，但找不到源码目录，按下面顺序探测，探测结果写入证据台账：

```bash
docker compose ls
docker inspect telepilot-web-1 telepilot-frontend-1 \
  --format '{{.Name}} labels={{json .Config.Labels}} mounts={{json .Mounts}} image={{.Config.Image}}'
docker inspect telepilot-web-1 \
  --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}} {{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker inspect telepilot-web-1 \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n '1,120p'
find /opt /srv /root /home -maxdepth 4 \( -name docker-compose.yml -o -name compose.yml -o -name '.env' -o -name '.git' \) 2>/dev/null
```

探测到 compose working_dir 时：

- 先备份该目录的 `.env`、compose 文件、部署脚本和当前 git SHA。
- 再运行项目提供的备份脚本；如果脚本不存在，至少执行数据库 dump，并备份插件目录、session 目录和上传/数据 volume。
- 确认 compose project name、volume name、数据库服务名和当前镜像来源，再更新代码。

探测不到源码目录但容器仍正常运行时：

- 不删除任何容器、volume、网络或镜像。
- 先用 `docker inspect` 记录现有 compose labels、mounts、env 和 image。
- 在新的固定目录重新 clone 当前分支，仅接管 compose 配置，不改 volume 名称。
- 把旧 `.env` 和 volume 映射迁移到新目录后，先 `docker compose config`，再执行备份、构建和启动。
- 只有新栈健康、版本 API 正确、真实 trace 可产生后，才允许把旧目录标记为历史备份；不得直接清理。

如果无法确定 volume 或数据库映射，G5 立即停止，最终状态只能是 `No-Go / 阻塞`。线上数据不允许靠猜测接管。

### 31.12 线上真实链路触发剧本

G6 必须尽量使用真实线上链路。为了避免误伤群聊或转账流程，触发顺序固定为从低风险到高风险：

1. **普通未命中消息**
   - 在已允许会话发送一条不会命中插件关键词的短文本。
   - 必须在 `/logs` 或 Trace API 看到 receive/normalize、订阅跳过或聚合跳过、稳定 reason_code。

2. **管理员命令**
   - 只使用当前命令注册表中已存在的只读或低风险命令；执行前必须用代码或 API 确认命令名称和权限。
   - 必须看到 command parse、权限判断、handler/plugin decision、回复或 action。

3. **插件成功动作**
   - 优先使用已安装的低风险测试插件或示例插件。
   - 如果线上没有适合插件，可以临时安装并启用示例插件到测试群；测试后恢复原启用状态。
   - 必须看到 matched/delivered、plugin invoke、`event_action.status=success`、requested_send_via 和 actual_send_via。

4. **插件失败动作**
   - 优先触发废弃 `notice` / `bbot_notice` 发送通道、缺 message_id、缺 inline_query_id 或测试插件提供的失败动作。
   - 不为了制造失败而破坏 Bot token、UserBot session、数据库或线上配置。
   - 必须看到 `event_action.status=failed` 或 `skipped`、稳定 `reason_code` / `error_code` 和中文迁移或修复提示。

Inline 和付款通知如不适合在线上真实触发，允许使用同版本 fixture 或后台测试接口补充，但必须满足两点：

- fixture 运行在已部署的同一版本代码上，不是本地旧代码。
- 台账写清不真实触发的原因、替代命令、替代输出和覆盖的字段。

### 31.13 线上失败与回滚剧本

最终版部署不是“更新失败再说”，必须预先写清失败出口。

| 失败点 | 立即动作 | 回滚或降级 | 状态 |
| --- | --- | --- | --- |
| git 更新失败 | 停止部署，保留旧容器 | 不改 compose、不重启服务 | `No-Go / 阻塞` |
| 构建失败 | 停止新镜像发布，保留旧镜像运行 | 记录构建日志第一条可行动错误 | `No-Go / 阻塞` |
| 迁移失败 | 停止启动新后端 | 用部署前数据库备份恢复，或保持旧服务和旧库不动 | `No-Go / 阻塞` |
| 后端健康检查失败 | 不签收 D11 | 回到旧镜像/旧 commit；保留错误日志 | `No-Go / 阻塞` |
| 前端版本错误 | 不签收 D7/D8/D11 | 回滚 frontend image 或重新构建静态资源 | `可测` 或 `No-Go` |
| Event Bus 线上异常 | 关闭 `event_bus_delivery_enabled`，观察旧规则兜底 | 修复前不报 Go，恢复开关后复验 | 最多 `可测 / 待服务器实测` |
| Trace 写入异常 | 关闭或降级 `trace_enabled`，确认 runtime log 有错误 | 修复 Trace 后重新打开并复验 | 最多 `可测 / 待服务器实测` |
| Inline 异常 | 关闭 `inline_updates_enabled`，确认跳过 reason | 修复后恢复并复验 | 最多 `可测 / 待服务器实测` |

回滚不得删除新增表作为默认动作。Trace 表和新增设置必须设计为可留存；紧急回滚优先切回旧代码/旧镜像和关闭运行时开关。只有数据库结构本身阻塞旧服务启动时，才允许按备份恢复数据库。

### 31.14 插件开发指南最终审计清单

G2/D9 的目标是让开发者照文档一次写出贴近新版本的插件。最终审计必须确认插件开发指南至少包含以下结构：

1. **新版本插件心智模型**
   - 插件接收统一事件信封。
   - 插件通过 `event_subscriptions` 声明想接收什么事件。
   - 插件通过 `capabilities` 声明高风险能力。
   - 插件通过 `ctx.messages` 或标准 action 请求消息操作。
   - Trace 是排障主入口。

2. **最小可运行插件**
   - `plugin.json` 最小字段。
   - `usage` 必填说明。
   - message 事件入口。
   - `ctx.messages.send_text` 示例。
   - 验证脚本命令。

3. **五类主场景**
   - 管理员命令。
   - 玩家关键词。
   - callback 按钮。
   - inline query / chosen inline result。
   - payment_confirmed / settlement。

4. **标准 payload 字段**
   - `trace_id`、`source`、`message`、`chat`、`sender`、`actor`、`player`、`reply_to`、`trigger`、`session`、`payment`、`inline_query`、`native_raw_meta`。
   - `native_raw` 必须放在能力声明章节，不放进最小示例。

5. **MessageOps / action 说明**
   - send、edit、delete、pin、answer_callback、answer_inline_query、settlement、end_session。
   - `send_via` 只允许 `interaction_bot`、`userbot_reply`、`auto`。
   - 明确 `notice`、`bbot_notice`、`notice_bot` 是废弃值，只会失败并写 Trace。

6. **调试路径**
   - 插件没启动：安装状态、启用状态、usage/manifest warning、订阅 source/event/scope/filter、session、rate limit、load error、runtime error。
   - 消息没发出：action 详情、requested/actual channel、Telegram API 错误、UserBot/Bot 状态。
   - 数据不够：先看标准字段，再按需声明 `telegram_native_raw` 并说明原因。

7. **迁移说明**
   - 旧平铺 payload 只作为迁移历史，不作为新示例。
   - 旧 `event.reply/respond`、`ctx.client` 直发只作为废弃或兼容说明，不作为推荐。
   - 旧交互规则只说明如何映射为订阅条件，不再作为开发主路径。

文档审计如果发现“新插件最小示例仍依赖旧 payload / 旧规则 / 旧 notice / live client 直发”，D9 必须降级为 `No-Go`。

### 31.15 最终版执行顺序锁

后续执行严格按下面顺序，不再调整：

```text
1. 复核当前 HEAD、工作树和证据台账。
2. 如果计划或证据有改动，先不 bump；等准备提交/推送时再按规则统一 bump。
3. 补 D1-D12 中仍是半落地或未开始的项。
4. 重跑第 31.5 节自动验证和目标测试。
5. 审计插件开发指南和示例。
6. 验收业务页桌面和窄屏/PWA。
7. 如有代码或文档改动，按项目规则更新版本和中文 CHANGELOG，中文 commit，push 当前分支。
8. 部署到 144.24.5.159，先备份再更新。
9. 做真实 trace 和三个回滚开关演练。
10. 更新证据台账和最终报告。
```

第 8 步之前最多只能输出 `可测 / 待服务器实测`。第 9 步失败时，即使服务器版本正确，也不能输出 `Go / 已完成`。

### 31.16 最终版子 Agent 任务模板

分配子 Agent 时必须使用可复核任务模板，不能只说“帮我看看”。模板如下：

```text
任务：
目标 D 项：
写入范围：
只读范围：
禁区：
当前分支和 commit：
必须执行的命令：
必须人工验收的页面或链路：
交付格式：
- 改动文件
- 关键 diff 摘要
- 命令结果
- 证据如何映射到 D 项
- 剩余风险

规则：
- 不 reset、checkout、clean 或 revert 非自己改动。
- 不改范围外文件。
- 不创造后端不存在的字段。
- 不恢复 notice / bbot_notice / notice_bot。
- 不把旧 payload 或 live client 直发写成推荐路径。
- 未跑验证必须说明原因，不能写已完成。
```

主 Agent 收到子 Agent 结果后必须自己复核 `git diff`、测试输出、页面或远端证据。只有复核后，证据台账状态才能升级。
