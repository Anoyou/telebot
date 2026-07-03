# TelePilot 插件开放事件框架优化计划

> 0.48 消息链路统一补充：涉及“触发方式 -> 会话通道”、“单入口生命周期”、“`session.data` / `update_session`”、“`payout`”、“按钮文本降级”、“`all_events` / `session_expired` / `message_edited`”、“`tp_event` 投影对象”等内容时，当前执行与开发文档以 [docs/INTERACTION-PIPELINE-UNIFICATION-PLAN.md](./INTERACTION-PIPELINE-UNIFICATION-PLAN.md) 为准。本文保留开放事件框架的阶段性背景和早期约束。

本文整理当前讨论后的下一阶段改造计划。目标不是新增一套新概念，而是在现有 `PluginEvent` / `on_interaction(ctx, entry_key, payload)` 基础上直接改造，把传给插件的 `payload` 明确定义为“当前标准事件信封字段”，并把框架从“替插件做过多判断”调整为“完整下发消息、提供双通道操作能力、记录告警与审计”。

> 后续执行入口：本文件记录 0.37.0 阶段的开放事件框架决策和落地记录。当前 `0.40.x` 最终版封口已经并入 `docs/TELEGRAM-FULL-EVENT-BUS-TRACE-PLAN.md`，执行时以该文档第 24 节《最终版执行版协议》、第 25 节《最终版落地总则》、第 26 节《最终版执行锁定补丁》、第 27 节《最终版可实现性锁定》和第 28 节《最终版执行冻结清单》为准。若本文件的阶段性版本建议、验收状态或旧入口描述与最终版总控冲突，以最终版总控为准。

## 1. 总体目标

### 1.1 一句话目标

TelePilot 负责把 Telegram 消息和平台解析结果稳定、完整地交给插件，并提供交互 Bot / UserBot 的受控操作方法；插件自己决定业务逻辑、参与规则和使用哪个发送通道。

### 1.2 不做的事

- 不新增 `PluginEvent v2`、`schema` 或其它新事件版本名。
- 不做新旧事件双轨，不为旧平铺字段继续设计兼容主路径。
- 不把外部转账通知 Bot 当成 TelePilot 的主动发送通道。
- 不用 display name 当作稳定用户身份。
- 不让普通 Bot 执行转账、发奖等客观做不到的动作。
- 不再把 Contract Guard 设计成公共插件市场式硬沙箱。

### 1.3 保留的框架职责

- 监听并区分消息来源：UserBot、交互 Bot、按钮回调、外部转账通知来源。
- 生成稳定的标准事件信封。
- 提供 `ctx.messages` 双通道操作方法。
- 对插件动作做可观察的告警、审计和失败返回。
- 处理 Telegram 客观能力边界，例如普通 Bot 无法转账、UserBot worker 离线、Bot 无法删除非自己可操作消息。

## 2. 标准事件信封

### 2.1 命名原则

继续使用现有：

```python
async def on_interaction(ctx, entry_key, payload):
    ...
```

文档中直接称 `payload` 为“当前标准事件信封字段”。不加 `PluginEvent v2`，不加 `schema` 字段，避免多一个迁移名词。

### 2.2 顶层字段

标准事件信封固定包含以下顶层分区。字段值可以为空，但字段名应稳定：

| 字段 | 说明 |
| --- | --- |
| `source` | 平台事件来源、事件类型、update/message/callback 基础索引 |
| `message` | 当前消息正文、实体、媒体摘要、消息 ID、时间等 |
| `chat` | 当前会话 ID、类型、标题、username 等 |
| `sender` | Telegram 实际发送者信息 |
| `actor` | 本次事件的业务行为主体，由平台按上下文推断，但不覆盖 `sender` |
| `source_actor` | 实际产生本条事件的用户或 Bot，例如外部转账通知 Bot |
| `reply_to` | 被回复消息摘要；转账通知回复付款原消息时用于确认付款玩家 |
| `payment` | 到账证据，只有付款确认类事件才有有效内容 |
| `player` | 玩法中的付款玩家或触发玩家身份 |
| `session` | 平台会话 key、作用域、TTL、是否新建 |
| `trigger` | 命中的规则、入口、触发词、触发类型 |
| `raw` | 脱敏后的原始更新摘要，用于高级插件排障 |

### 2.3 字段完整性要求

每种触发方式都必须尽量填满同一组字段，而不是给不同插件下发不同形状：

- 关键词触发：必须有 `source`、`message`、`chat`、`sender`、`actor`、`trigger`、`session`。
- 付款触发：必须有 `source`、`message`、`chat`、`source_actor`、`reply_to`、`payment`、`player`、`trigger`、`session`。
- 按钮回调：必须有 `source.callback_query_id`、`source.callback_data`、`message`、`chat`、`sender`、`actor`、`session`。
- 管理员命令触发：必须有 `source`、`message`、`chat`、`sender`、`actor`、`trigger`，并标记来源是 UserBot 管理命令。

### 2.4 raw 摘要原则

`raw` 用于排障，不用于插件常规业务逻辑：

- 保留 Telegram 原始结构中对插件有用的摘要。
- 不放 Bot Token、session、敏感密文。
- 大字段、媒体二进制、完整私密文本按摘要或脱敏处理。
- 文档明确：优先读标准字段，只有排障或特殊兼容才读 `raw`。

## 3. 外部转账通知来源

### 3.1 概念边界

外部转账通知 Bot 是群里已有的第三方/官方通知 Bot，TelePilot 只监听它发出的到账消息。它不是 TelePilot 的发送通道。

它只负责提供到账证据：

- 金额。
- 付款人的 Telegram display name。
- 收款人的 Telegram display name。
- 通知消息 ID。
- 通知回复的原消息上下文。

它不负责：

- 发送插件消息。
- 发送按钮。
- 发送公告。
- 删除或置顶消息。
- 执行发奖、退款、催付。

### 3.2 付款身份确认逻辑

外部转账通知一般只有金额和收付款人的 Telegram 姓名，不包含 `@username` 或 `user_id`。因此真实玩家身份不能从通知文本里直接得出。

推荐确认逻辑：

```text
转账通知金额匹配
+ 收款人 display name 匹配配置
+ 转账通知回复了付款人的原消息
+ 通知里的付款人 display name 与被回复用户 display name 可匹配
= payment.status confirmed，player.user_id 可用
```

如果转账通知没有回复付款人的原消息，或付款姓名与被回复用户不匹配：

- `payment.status` 可以表示到账文本本身被识别。
- `player.user_id` 不应强行填写。
- `player.identity_confidence` 应降级为 `name_only` 或 `unknown`。
- 独玩/按钮类玩法不能直接绑定该玩家。

### 3.3 payment 字段

`payment` 表示到账证据，而不是玩家身份：

```json
{
  "status": "confirmed",
  "amount": 88,
  "payer_display_name": "Alice",
  "receiver_display_name": "Bob",
  "source_message_id": 123,
  "reply_to_message_id": 120
}
```

### 3.4 player 字段

`player` 表示平台能确认到的付款玩家：

```json
{
  "user_id": 123456,
  "display_name": "Alice",
  "username": null,
  "identity_confidence": "reply_context"
}
```

允许值建议：

| 值 | 含义 |
| --- | --- |
| `reply_context` | 转账通知回复了付款人的原消息，且姓名可核对 |
| `callback_confirmed` | 付款人点击确认按钮后绑定 |
| `name_only` | 只有 display name，不足以作为稳定身份 |
| `unknown` | 无法确认付款玩家 |

## 4. 框架拦截改造

### 4.1 从硬拦截变成软告警

框架不再把插件声明视作强沙箱。默认策略调整为：

| 场景 | 新行为 |
| --- | --- |
| 插件调用未声明能力 | 记录高级告警，继续执行或返回可见失败 |
| 插件请求未声明 `send_via` | 记录 Contract Guard 告警，按请求执行可用通道 |
| 插件请求已移除通道 | 记录告警并返回失败，不静默转为其它通道 |
| 插件返回未知动作 | 写 runtime log，并返回插件错误 |
| Telegram 物理能力不可用 | 返回失败原因 |

### 4.2 保留硬失败的客观边界

以下不是风险控制，而是能力事实，必须失败：

- 交互 Bot token 不存在或不可用。
- UserBot worker 不在线。
- 普通 Bot 无法转账或发奖。
- 普通 Bot 无法删除或置顶它无权操作的消息。
- `callback_query_id` 缺失时无法回应按钮。
- Telegram API 返回明确失败。

### 4.3 告警可见性

所有软告警必须可见：

- 写 runtime log。
- 在交互中心的规则详情 / 调试面板显示最近告警。
- 插件仓库 lint 显示高级规范警告。
- 不把告警藏在后端日志里。

## 5. 通道与操作方法

### 5.1 主动发送通道

插件主动发送只保留：

| 通道 | 说明 |
| --- | --- |
| `interaction_bot` | 普通 Bot，适合高频互动、按钮、题面、结果公告 |
| `userbot_reply` | UserBot 代发，适合账号身份、低频回复、发奖/收款相关动作 |
| `auto` | 默认候选顺序，建议为 `interaction_bot -> userbot_reply` |

`notice` / `bbot_notice` 不再作为发送通道。

### 5.2 ctx.messages 能力

插件统一通过 `ctx.messages` 返回平台动作：

```python
await ctx.messages.send(channel="interaction_bot", text="题面")
await ctx.messages.send(channel="userbot_reply", text="账号本人回复")
await ctx.messages.send(channel=["interaction_bot", "userbot_reply"], text="可回退消息")
await ctx.messages.edit(channel="interaction_bot", message_id=mid, text="更新题面")
await ctx.messages.delete(channel="interaction_bot", message_id=mid)
await ctx.messages.pin(channel="interaction_bot", message_id=mid)
await ctx.messages.answer_callback(callback_query_id=cid, text="收到")
```

### 5.3 不绑定入口与发送通道

入口的 `dispatch_modes` / `message_channels` 只表示默认偏好，不是能力范围：

- 管理员命令触发后，插件也可以选择交互 Bot 发消息。
- 群友关键词触发后，插件也可以选择 UserBot 代发。
- 是否合理由插件和管理员负责；框架只记录、执行和返回失败原因。

## 6. 后端实施计划

### 6.1 事件构造统一

任务：

- 梳理当前交互 payload 构造点。
- 直接改造现有 `PluginEvent` / 交互 payload 构造链路，不新增 `PluginEvent v2`。
- 提取统一 builder，例如 `build_standard_interaction_payload(...)`。
- 所有关键词、付款、按钮、管理员命令触发都走同一 builder。
- 删除旧平铺字段主路径；如运行时仍能读到旧字段，只作为历史数据清理对象，不写入新版插件文档。

交付物：

- 标准事件信封构造函数。
- 每种触发方式的 payload 快照测试。
- 旧平铺字段不再作为断言目标。

### 6.2 字段补全

任务：

- 增加 `message` 分区，补齐 text、message_id、entities、media 摘要。
- 增加 `chat` 分区，补齐 chat_id、chat_type、title、username。
- 增加 `sender` 分区，明确实际发送者。
- 明确 `actor` 与 `source_actor` 的差异。
- 增加 `raw` 脱敏摘要。

交付物：

- 字段表和测试快照一致。
- 插件不再需要猜旧字段名。

### 6.3 转账通知解析收束

任务：

- 转账通知解析只依赖金额、付款 display name、收款 display name。
- 从转账通知的 reply_to 原消息确认真实付款人。
- 姓名核对失败时降低 `player.identity_confidence`。
- 缺少 reply_to 时不强填 `player.user_id`。

交付物：

- 付款通知 payload 快照。
- 姓名不匹配、无 reply_to、金额不匹配、收款人不匹配的测试。

### 6.4 Contract Guard 软化

任务：

- 将未声明能力 / 未声明通道从硬阻断改成告警。
- 保留客观能力失败。
- 返回动作执行结果和失败原因。
- 在 runtime log 中标出 `guard_level=warning` / `guard_level=failed`。

交付物：

- Contract Guard 行为表。
- 告警可见性测试。

### 6.5 Delivery Executor 调整

任务：

- 继续只支持 `interaction_bot` / `userbot_reply` 主动发送。
- `notice` / `bbot_notice` 请求返回明确失败。
- `reply_markup` 只给 `interaction_bot`。
- UserBot 代发失败时返回 worker 错误。

交付物：

- 双通道发送测试。
- 已移除通道失败测试。

## 7. 前端实施计划

### 7.1 交互中心调试面板

新增“事件与动作调试”区域，展示：

- 最近下发给插件的标准事件信封摘要。
- 最近插件返回的 actions。
- 最终使用的发送通道。
- 最近 Contract Guard 告警。
- 最近 Telegram API / worker 失败原因。

### 7.2 Payload 预览

规则编辑页增加预览入口：

- 模拟关键词触发 payload。
- 模拟付款通知触发 payload。
- 模拟按钮回调 payload。
- 标注哪些字段一定有、哪些可能为空、哪些是平台推断。

### 7.3 文案调整

- 不再把 `message_channels` 描述成能力范围。
- 明确“外部转账通知 Bot 只做付款证据来源”。
- 旧通道显示“已移除通道”，并提供迁移提示。

## 8. 插件迁移计划

当前旧插件不要求兼容，直接按新规则修改。

### 8.1 迁移要求

- 插件统一读取标准事件信封。
- 不再以旧平铺字段作为主路径。
- 不要求兼容旧插件；所有官方、样本、当前使用中的插件都按新版信封迁移。
- 不再使用 `notice` / `bbot_notice`。
- 游戏插件优先读 `message`、`actor`、`player`、`session`。
- 付款插件只用 `payment.status == "confirmed"` 判断到账。
- 需要真实玩家身份时检查 `player.user_id` 和 `player.identity_confidence`。

### 8.2 官方/样本插件

优先迁移：

- `game24`
- `math10`
- 已安装互动样本：猜数字、诗词填空、九宫格猜骰、彩票、红包、PT 促销

每个插件都要补：

- 标准信封读取测试。
- 关键词触发测试。
- 付款触发测试。
- 按钮回调测试，如适用。

## 9. 测试计划

### 9.1 Payload 快照测试

必须覆盖：

1. UserBot 管理员命令触发。
2. 群友关键词触发。
3. 外部转账通知触发，且 reply_to 可确认付款人。
4. 外部转账通知触发，但无 reply_to。
5. 外部转账通知触发，付款 display name 不匹配 reply_to 用户。
6. 按钮 callback 回到已有会话。
7. 已有会话中的普通消息。

### 9.2 通道测试

必须覆盖：

1. 插件选择 `interaction_bot` 发送。
2. 插件选择 `userbot_reply` 发送。
3. 插件选择 `auto` 回退。
4. 插件选择 `notice` / `bbot_notice` 返回明确失败。
5. `reply_markup` 只通过交互 Bot。
6. UserBot worker 离线时返回可见失败。

### 9.3 Contract Guard 测试

必须覆盖：

1. 未声明能力产生告警但不静默丢动作。
2. 未声明通道产生告警。
3. 混合候选里含已移除通道产生告警。
4. 未知动作写 runtime log。
5. 告警能被前端查询到。

## 10. 文档计划

最终必须更新：

- `docs/PLUGIN-DEV-GUIDE.md`
- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-REMOTE.md`
- `docs/PLUGIN-CHEATSHEET.md`
- `docs/PLUGIN-SAFETY.md`
- `docs/INTERACTION-BOT-OPTIMIZATION.md`

文档重点：

- 当前标准事件信封字段表。
- 每个字段何时必有、何时可空。
- 付款通知只提供金额和双方 display name。
- 真实付款人身份来自通知 reply_to 的原消息。
- 插件如何选择交互 Bot / UserBot。
- Contract Guard 告警不等于业务失败，但客观能力失败会返回错误。
- 完整示例：关键词游戏、付款游戏、按钮游戏、管理员命令插件。

## 11. 并行任务拆分

### A. 后端事件信封

范围：

- payload builder
- 字段补全
- payload 快照测试

禁止：

- 改前端 UI
- 改插件业务逻辑

### B. 转账通知身份确认

范围：

- 转账通知解析
- reply_to 付款人确认
- payment / player 字段
- 边界测试

禁止：

- 把 display name 当 user_id
- 引入外部通知发送通道

### C. Contract Guard 与 Delivery Executor

范围：

- 软告警
- 双通道发送
- 已移除通道失败返回
- runtime log

禁止：

- 恢复 `notice` / `bbot_notice` 主动发送

### D. 前端交互中心调试能力

范围：

- payload 预览
- actions 展示
- 告警展示
- 最近失败原因

禁止：

- 大幅重做规则编辑主流程

### E. 插件迁移

范围：

- 官方/样本插件读取标准信封
- 去旧平铺字段主路径
- 补测试

禁止：

- 修改框架通道规则

### F. 文档与发布

范围：

- 全部插件开发文档
- README 摘要
- CHANGELOG
- 版本号

禁止：

- 写未落地能力

## 12. 版本建议

建议版本：

```text
0.37.0 minor（次版本） · 插件开放事件框架
```

理由：

- 这是插件框架语义变化。
- 标准事件信封成为开发主路径。
- Contract Guard 从硬阻断变为软告警。
- 交互中心增加调试与预览能力。
- 当前插件需要迁移。

## 13. 验收标准

完成后必须满足：

- 插件开发者只看文档即可知道 `payload` 有哪些字段。
- 任意触发方式下，插件收到的顶层字段稳定。
- 付款通知只以金额和双方 display name 做到账核验，真实玩家来自 reply_to 原消息。
- 外部转账通知 Bot 不再出现在任何主动发送通道中。
- 插件可以自由选择交互 Bot / UserBot，框架只做执行、告警和失败返回。
- 前端能看到最近 payload、actions、告警和失败原因。
- 官方/样本互动插件全部迁移。
- 后端全量测试、插件校验、前端 typecheck/build 通过。

## 14. 0.37.0 落地记录

本计划按“个人可信插件标准”落地到 0.37.0：

- 后端交互 payload 已补齐 `source`、`message`、`chat`、`sender`、`actor`、`source_actor`、`reply_to`、`payment`、`player`、`session`、`trigger`、`raw` 标准信封字段。
- `event_from_interaction_payload(payload)` 已把 `sender`、`actor`、`source_actor`、`player`、`payment`、`session` 投影成稳定事件对象，插件不需要直接猜旧平铺字段。
- Contract Guard 已从硬阻断改为软告警：未声明动作或未声明受控通道会记录 `guard_level=warning` 并继续执行；旧 `notice` / `bbot_notice` / `notice_bot` 主动发送通道已移除且不兼容，会返回 `guard_level=failed` 和迁移提示。
- 交互中心新增“事件与动作调试”面板，展示最近 payload、插件 actions、平台处理后 actions、Contract Guard 告警和插件失败原因。
- 插件开发指南、API 参考、远程插件规范、速查表、安全边界、交互优化说明和 README 已同步为标准事件信封、可信插件风险自担、旧通道迁移和 `ctx.messages` 双通道开发口径。

后续插件迁移要求：

- 新插件和当前远程插件都应以标准事件信封为主路径，不再依赖 `payload["event"]` 或旧平铺字段。
- 主动消息只使用 `interaction_bot`、`userbot_reply` 或 `auto`；旧 `notice` / `bbot_notice` 一律迁移。
- 付费玩法只以 `source.type == "payment_confirmed"` 且 `payment.status == "confirmed"` 作为到账依据；真实玩家身份优先读 `player.user_id` 和 `player.identity_confidence`。
