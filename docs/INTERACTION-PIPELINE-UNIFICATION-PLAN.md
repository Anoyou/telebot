# TelePilot 消息链路统一与会话通道计划（目标版本 0.48.0）

> 本文是 0.47.4 消息链路全量审查（含二次复核）的落地执行计划，自包含、可直接分发给多个执行 Agent 并行开发。
> 与 `docs/INTERACTION-OPEN-EVENT-FRAMEWORK-PLAN.md`（0.37 开放事件框架）一脉相承：那一版定义了标准事件信封，本计划解决"信封质量不一致、插件多次适配、通道选择负担、链路延迟与一批缺陷"。
> 冲突裁决顺序：本文 > 旧计划文档。执行时遵守 `AGENTS.md` 工作区安全与版本规则。

---

## 1. 总体目标

### 1.1 一句话目标

插件只提供功能和逻辑；**触发方式决定整个会话用哪条通道收发消息**，通道选择、状态存储、超时、发奖、按钮降级全部由平台承担。插件对同一玩法只写一份代码、一个入口、一种事件形状。

### 1.2 核心模型：触发方式 → 会话通道（已拍板）

| 触发方式 | 会话通道（收 + 发） | 例外 |
| --- | --- | --- |
| UserBot 前缀命令（`,guess 100`） | `userbot`：后续所有交互由 UserBot 发送和接收 | 无（收付款本来就是 userbot） |
| 交互规则关键词 / 付款触发 | `interaction_bot`：后续所有交互由交互 Bot 发送和接收 | **收付款动作永远走 userbot**（Bot 无转账能力） |

- 会话在创建那一刻写入 `channel` 字段，之后所有无显式通道的动作继承它。
- 插件动作不再需要写 `send_via`/`channel`（保留显式覆盖作为高级逃生门）。
- `payout` 成为一等语义动作，无论会话通道是什么都路由 userbot 并过限速引擎。
- userbot 通道**没有 inline 按钮能力** → 平台自动把按钮降级为编号文本选项，并把玩家的编号/按钮文本回复合成为 callback 事件回投插件（插件零感知）；同时插件配置页提供"仅关键词触发"开关，让强按钮玩法可以拒绝命令触发。
- 关键词规则**依赖交互 Bot 存在**：保存关键词规则时校验交互 Bot token，缺失则拒绝保存并提示，不做运行时静默 fallback。

### 1.3 不做的事

- 不新增事件版本名（没有 PluginEvent v2），继续在现有信封上改造。
- 不做新旧插件双轨兼容主路径：官方/样例/已安装插件在本计划泳道 P 内一次性迁移。
- 迁移期**不删除** payload 旧平铺字段的输出（避免未迁移插件立刻断），但文档不再描述它们；收缩平铺字段留到下一个版本单独任务。
- 不给交互 Bot 做"绕过 trace 的直通旁路"——低延迟由主链路优化与 userbot 会话进程内直调达成。
- 不把 Contract Guard 变回硬沙箱（自用可信插件标准不变）。

---

## 2. 全局约定（每个执行 Agent 必读）

- **热点文件串行原则**：`backend/app/services/account_bot_runtime.py` 与 `backend/app/worker/plugins/loader.py` 是冲突热区，各归属一条泳道独占（见 §3）；其他泳道需要改这两个文件时，只能在对应泳道的集成检查点之后 rebase 进行。
- 每条泳道每完成一个任务必须跑：
  ```bash
  cd backend && python -m pytest app/tests -q
  python scripts/validate-plugin-examples.py
  python scripts/validate-installed-interaction-plugins.py
  # 涉及前端时：
  pnpm -C frontend typecheck && pnpm -C frontend build
  ```
- 新增/修改行为必须有对应测试（现有测试目录 `backend/app/tests/`，交互链路相关：`test_account_bot.py`、`test_event_bus.py`、`test_plugin_events.py`、`test_installed_interaction_plugin_contracts.py`）。
- 所有新增 reason_code 必须登记进 `event_bus.py::EVENT_REASON_CODES`。
- commit 文案中文；版本号只在最终发布任务（泳道 D）统一 bump 为 `0.48.0`，过程提交不动版本。
- 不 revert / reset 他人未提交改动。

---

## 3. 泳道划分与并行编排

六条泳道 = 六个可并行的执行 Agent。**泳道内部任务严格按编号串行**；泳道之间按下表阶段对齐，共 3 个集成检查点。

| 泳道 | 独占文件（主权） | 阶段一（立即开工） | 阶段二（检查点 1 后） | 阶段三（检查点 2 后） |
| --- | --- | --- | --- | --- |
| **R** 交互 Runtime | `services/account_bot_runtime.py`、`services/interaction_bot_service.py`、`api/account_bots.py` | R1–R5 | R6–R11 | R12–R14（R15 可延期） |
| **W** Worker/Loader | `worker/plugins/loader.py`、`worker/runtime.py` | W1–W3 | W4–W5 | W6–W8 |
| **S** 发送层 | `services/account_bot_service.py`、`services/interaction/delivery.py`、`services/interaction/contracts.py`、`worker/plugins/message_ops.py` | S1–S2、S4–S6 | S3 | （支援 P） |
| **E** 事件契约 | `services/event_bus.py`、`worker/plugins/events.py` | E1–E4 | E5 | （支援 P） |
| **P** 插件迁移 | `plugins/installed/*`、`worker/plugins/builtin|official` | （阅读本计划+现有插件） | P1 起草 | P1–P3 |
| **D** 文档发布 | `docs/*`、`CHANGELOG.md`、版本号文件 | D1 起草提纲 | D1 持续 | D1–D3 收口 |

**集成检查点 1**（阶段一全部合并后）：全量测试绿；缺陷修复批（R1–R5、W1–W3、S1/S2、E1–E4）验收通过。
**集成检查点 2**（阶段二合并后）：单一信封源 + 会话通道字段 + payout + userbot 会话喂入联调通过（专项联调用例见 §10.3）。
**集成检查点 3**（阶段三 + P + D 合并后）：0.48.0 发布验收（§11）。

依赖关系明细：
- S3（默认通道=会话通道）依赖 R9（session.channel 落库）定义的字段名，按 §6 S3 中的契约先行实现、检查点 2 联调。
- W5（会话喂入）依赖 R6（信封基底统一，共用 builder）与 R9；W6 依赖 W5。
- E5（入口直传投影对象）依赖 E1 与 R6。
- P 依赖 R/W/S/E 阶段二完成；P3 的删除动作与 R 泳道协调（math 层代码在 R 主权文件内，由 R15 或 P3 提 PR 时经 R 泳道 review）。

---

## 4. 泳道 R：交互 Runtime（缺陷修复 → 信封统一 → 会话模型）

### R1 · offset 失败可见化（at-most-once 改为可观察的有限重试）

- **现状**：[account_bot_runtime.py:840-848](../backend/app/services/account_bot_runtime.py) 交互 loop 的 `finally` 无论 `_handle_interaction_update` 是否异常都推进 `interaction_last_update_id`，且写 `error=None` 把失败状态抹掉；管理 Bot loop（709-727）与 transfer test loop（893-900）同款。瞬时 DB/Redis 异常 = 消息永久丢失且状态显示正常。
- **改动**：处理异常时——① 同一 update 原地重试最多 2 次（间隔 1s/3s）；② 重试仍失败则推进 offset，但写 `interaction_last_error`（不再置 None）+ 写一条 `warn` 级 runtime_log（含 update_id、chat_id、异常摘要）；③ 成功路径才清 error。三个 loop 统一处理。
- **验收**：测试模拟 handler 抛错 → 重试 2 次 → offset 推进 + error 字段与 runtime_log 均可见；成功后 error 清空。

### R2 · 运行时游标搬出配置行

- **现状**：[account_bot_runtime.py:766-806](../backend/app/services/account_bot_runtime.py) `interaction_last_update_id` / `transfer_last_update_id` 存在用户配置同一个 `SystemSetting` JSON 行里，每条 update 读改写整行；与 Web UI 保存配置互相覆盖（offset 回退→消息重放；或配置丢失）。
- **改动**：游标迁移到独立存储（推荐 Redis key `account_bot:cursor:{kind}:{aid}`，或独立 `SystemSetting` key），loop 内存持有游标、批量提交（一批 updates 只落一次盘）；配置行不再被 runtime 写入。老配置行里的游标值做一次性读取迁移。管理 Bot 的 `AccountBot.last_update_id` 是独立列，不受影响，但同样改为批末提交。
- **验收**：并发场景测试——loop 推进游标期间保存配置，两边都不丢；重启后从游标续读。

### R3 · worker RPC 快速失败 + 超时收紧

- **现状**：[account_bot_runtime.py:4443-4455](../backend/app/services/account_bot_runtime.py) `_run_worker_interaction_entry` 发布到无订阅者频道后干等 `_INTERACTION_ENTRY_TIMEOUT_SECONDS=60`（[:117]）秒，期间整条交互 loop 阻塞；`_run_worker_interaction_action` 同款。
- **改动**：`redis.publish()` 返回接收者数量，为 0 时立即返回 `(False, "账号 worker 不在线", [])` 并写 `userbot_offline` reason_code 的 span；超时常量降为 15s。
- **验收**：worker 未运行时调用 <100ms 返回明确错误；trace 里可见 `userbot_offline`。

### R4 · Event Bus 路径 callback 兜底 ack

- **现状**：[account_bot_runtime.py:3223-3227](../backend/app/services/account_bot_runtime.py) callback_query 事件插件执行失败 → `terminal_handled=True` 直接返回，无人 answer callback，按钮转圈到超时；legacy 路径（3829 行）有兜底。
- **改动**：`_try_handle_event_bus_subscriptions` 结束前：若 `incoming.callback_id` 存在、事件被 terminal 处理、且已应用动作里没有 `answer_callback`，补一次空 `_answer_callback(incoming)`（失败也要 ack，文案"处理失败，请稍后再试"）。
- **验收**：测试插件抛错的 callback 场景，断言 answerCallbackQuery 被调用。

### R5 · known_users 语义修正（runtime 侧）

- **现状**：[account_bot_runtime.py:3459-3463](../backend/app/services/account_bot_runtime.py) `_event_bus_account_state` 把**当前事件 sender** 塞进 `known_user_ids`，`known_users` scope 判断的又恰是 sender ∈ known_user_ids → 恒真，等于没有过滤；userbot 侧（loader）语义正常。
- **改动**：`known_user_ids` = 账号 owner + sudo 用户 + 该账号 Bot 授权用户（`account_bot_service.list_bot_users`）+ 当前 chat 活跃会话的 `participant_user_ids`；**不含**当前 sender。查询结果按账号缓存 30s。
- **验收**：非上述集合的群友触发 `known_users` scope 订阅 → `scope_not_matched`；owner/sudo/参与者 → matched。

### R6 · 单一信封源（本计划最高价值任务）+ 配置单读下传

- **现状**：三个平行信封构造器质量不一——`_interaction_module_payload`（[:4307-4392]）的 `message` 分区 entities 恒 `[]`、media 恒 `None`、date 恒 `None`、`chat.title/username` 恒 `None`（[:4067-4089]）；而 `event_bus.normalize_bot_update`（[event_bus.py:130-224](../backend/app/services/event_bus.py)）已有完整的 entities/media/forward/via_bot/sender_chat/service 摘要。`Incoming.native_raw` 里存着完整 update。另外同一条 update 处理中 `get_transfer_notice_config` 被重复读约 5 次（入口 1037、转账通知 5428、规则命令 3589、会话消息 3698、payout mode 2444）。
- **改动**：
  1. `_interaction_module_payload` 改为以 `normalize_bot_update(incoming.native_raw)` 为基底，再叠加 `event/trigger/session/payment/player/actor/settlement/module_config` 等分区与既有平铺字段（平铺字段输出保持不变，见 §1.3）；`sender/actor/source_actor/player` 各自独立 dict 拷贝，消灭同引用 aliasing（[event_bus.py:459-462] 的 `_event()` 同步修）。
  2. `_handle_interaction_update` 读一次 cfg 后作为参数传给 `_try_handle_transfer_command/_try_handle_transfer_notice/_try_handle_interaction_rule_command_or_keyword/_try_handle_interaction_module_message` 及 payload 构造（`_resolve_payout_mode` 接受外部 cfg）。
- **验收**：payload 快照测试更新——关键词/付款/callback/会话消息四种触发下 `message.entities/media/date`、`chat.title/username` 均为真实值；单条 update 处理 `get_transfer_notice_config` 调用次数 ≤1（用 mock 计数断言）。

### R7 · 媒体/编辑消息下放 + allowed_updates 扩展

- **现状**：[account_bot_runtime.py:3693-3695](../backend/app/services/account_bot_runtime.py) 会话消息处理 `if not text: return False` → 贴纸/骰子/无 caption 图片/服务消息不投递；`allowed_updates` 只有 `message/callback_query/inline`（[:699][:830]），edited_message 源头就没订阅。
- **改动**：① 会话消息路径放行有 `media` 的空文本消息（R6 之后信封里有 `message.media` 摘要），仅当"无文本且无媒体且非服务消息"才跳过；② `allowed_updates` 增加 `edited_message`，`_extract_incoming` 识别后映射 `source.type="message_edited"`（新事件类型登记进 `VALID_EVENT_TYPES`），只投递给订阅了它的插件与活跃会话。
- **验收**：骰子消息（`message.dice`）能进入活跃会话投递，`message.media.type=="dice"`；编辑消息产生 `message_edited` 事件。

### R8 · 吃消息岔口 trace 补全

- **现状**：多处消息消失无痕：转账通知 sender 匹配但无规则时 `return True`（[:5459]）只有 log.info；约 20 处 `except → log.debug` 吞掉会话读写失败。
- **改动**：所有"消费并终止路由"的分支写 `route` span（SKIPPED/OK + reason_code）；会话/状态 Redis 读写失败从 `log.debug` 升级为 runtime_log `warn`（带 chat_id/rule_id）。`start_session` 动作在 delivery 的 trace 记录由泳道 S（S5）负责，此处不重复。
- **验收**：构造"通知无规则匹配"“会话保存失败"用例，trace/runtime_log 有对应记录。

### R9 · session.channel 字段（会话通道模型第一块）

- **现状**：会话 payload（`_save_interaction_session` [:2050-2112]、`_apply_interaction_start_session_action` [:2128-2203]）没有通道概念。
- **改动**：会话 payload 增加 `"channel": "interaction_bot" | "userbot"` 与 `"expires_at": <epoch>`：关键词/付款/callback 路径创建的会话写 `interaction_bot`；`expires_at = now + ttl`，Redis TTL 改为 `ttl + 90`（为 R11 扫描器留 grace）。`_interaction_session_envelope` 把 `channel` 加进信封 `session` 分区。worker 侧写入点由 W 泳道（W5/W6）对齐同一 schema。
- **验收**：快照测试断言新会话含 channel/expires_at；信封 `session.channel` 可读。

### R10 · payout 语义动作（主进程侧）+ 硬编码名单删除

- **现状**：发奖靠插件自己发 `+N` 文本 + worker 正则解析交互 Bot 中奖公告自动转账（文案即协议，[runtime.py:240-249](../backend/app/worker/runtime.py)）；两份硬编码模块名单 `AUTO_PAYOUT_MODULE_KEYS`（[account_bot_runtime.py:122]）/`_ACCOUNT_BOT_AUTO_AWARD_MODULE_KEYS`（[runtime.py:93]）决定"自动发放"文案。
- **改动**：
  1. 新动作 `{"type": "payout", "amount": >0, "chat_id"?, "reply_to_message_id"?, "text"?}`（text 默认 `+{amount}`）。delivery 执行器识别后**永远**经 `CMD_RUN_INTERACTION_ACTION`（`action_type="payout"`）交 worker 以 userbot 执行（worker 侧实现在 W4）；成功写 settlement trace（`actual_send_via="userbot_reply"`），失败返回 `userbot_offline`/`telegram_api_error`。
  2. 删除 `AUTO_PAYOUT_MODULE_KEYS` 与 `_resolve_payout_mode` 的名单判断：信封 `payout_mode` 迁移期恒填 `"auto"`（字段保留、语义废弃，公告文案由插件自行决定），`payout_account_label` 保留。
  3. `EVENT_REASON_CODES` 增补 `payout_failed`（如需）。
- **验收**：插件返回 payout 动作 → userbot 回复 `+N` 到指定消息；worker 离线 → 明确失败动作记录；名单常量全仓无引用。

### R11 · session_expired 扫描与事件投递（interaction_bot 通道侧）

- **现状**：会话靠 Redis TTL 被动过期，插件收不到超时通知，只能自己起 sleep 任务；插件内存状态与 Redis 会话双轨失步。
- **改动**：交互 manager 启动一个每 15s 的账号级扫描任务：`scan_iter("account_bot:interaction_session:{aid}:*")`，对 `expires_at <= now` 且 `channel=="interaction_bot"` 的会话——构造 `event_type="session_expired"` 信封（复用 R6 builder，session 分区带完整 data）→ 调插件入口 → 应用返回动作 → 删除会话。`channel=="userbot"` 的过期会话跳过（由 W7 的 worker 扫描器处理）。`VALID_EVENT_TYPES` 增加 `session_expired`。
- **验收**：会话到期后 ≤20s 插件收到 session_expired 事件，返回的公告消息发出，会话删除；未到期会话不受影响。

### R12 · 关键词规则保存校验（依赖交互 Bot）

- **现状**：关键词规则在无交互 Bot token 时仍可保存，运行期静默不工作。
- **改动**：[api/account_bots.py](../backend/app/api/account_bots.py) 保存转账/交互配置时：存在 `trigger_mode in {keyword, both}` 或含 `module_start_keywords` 的启用规则而无 interaction bot token → 返回 400 + 明确中文提示（"关键词触发依赖交互 Bot，请先配置交互 Bot Token 或改用命令触发"）。前端沿用现有错误 toast 展示，无需新组件。
- **验收**：API 测试覆盖拒绝与放行两种情况。

### R13 · 主进程并发分发 + 调试快照降频

- **现状**：交互 loop 串行处理 update（一个慢插件堵住全账号）；`_remember_interaction_debug_state`（[:4879-4906]）每条消息 2-3 次内联 Redis JSON 写。
- **改动**：① update 处理改为按 `chat_id` 分组的有序并发：同 chat 串行（`asyncio.Queue` per chat 或 keyed lock），异 chat 并发，全局并发度上限 8；offset 提交逻辑与 R1/R2 的批量语义对齐（batch 全部完成后提交最大 update_id）。② 调试快照改 `asyncio.create_task` fire-and-forget + 仅在 `payload_built`（采样 1/5）与 `plugin_error/actions_guarded`（全量）时写。
- **验收**：并发测试——A chat 阻塞的插件调用不阻塞 B chat 的消息处理；同 chat 顺序保持。

### R14 · callback fast-ack（opt-in）

- **改动**：manifest entry 新增 `"callback_fast_ack": true`；命中该入口的 callback_query 在分类完成后立即空 `answerCallbackQuery`，插件返回的 `answer_callback` 动作改记 SKIPPED（reason `already_acked`）。文档注明 fast-ack 与 `show_alert` 互斥。
- **验收**：声明 fast-ack 的入口按钮点击后 ack 延迟 < 200ms（不等插件）；未声明的行为不变。

### R15 · 路由链坍缩为分类器（收尾，可延期到 0.49，不阻塞本版验收）

- **改动**：把 `_handle_interaction_update` 的 8 层 try-consume 链重构为：内存 `RoutingIndex`（trusted_sender_ids / interaction_bot_id / 关键词与开关命令集合 / 有活跃会话的 chat 集合 / query_commands，随配置重启与会话增删维护）→ 纯内存分类产出 `source.type`（callback_confirm/self/userbot_command/transfer_command/payment_notice/keyword/message）→ R6 信封 → 统一 `dispatch_event`（legacy 规则以虚拟订阅参与）→ 并发投递。未命中任何分类的消息零 I/O 直接 SKIPPED。同时删除 math answer 层与 math10 本地 fallback（[:4531-4585]，与 P3 协调）。
- **验收**：全部既有路由测试通过；"未命中消息"路径 DB 查询次数为 0（mock 断言）。

---

## 5. 泳道 W：Worker/Loader（缺陷修复 → payout → 会话喂入）

### W1 · ctx 并发换装竞态修复

- **现状**：调插件入口前临时替换 `ctx.messages/ctx.client/ctx.log`，finally 恢复——[loader.py:688-725]（event bus 入口）、[loader.py:3514-3541]（invoke_interaction_entry）、[loader.py:2566-2631]（legacy dispatcher）、[loader.py:2925-2960]（_wrap_cmd）。`ctx` 是每插件单例，Telethon on_message 与 IPC on_interaction **可并发**：互相覆盖换装，动作写错 buffer、过期 trace 客户端泄漏为常驻。
- **改动**：不在共享 ctx 上换装。每次调用用 `dataclasses.replace(ctx, messages=..., client=..., log=...)` 构造调用级 ctx 传给插件；共享 ctx 只保留稳定字段。四个换装点全部改造。
- **验收**：并发压测用例——同一插件同时跑 on_message 与 on_interaction，各自 buffer/trace 隔离无串扰。

### W2 · worker RPC 并发化

- **现状**：[runtime.py:786-1040](../backend/app/worker/runtime.py) `_listen_cmd` 在监听循环里同步执行 `CMD_RUN_INTERACTION_ENTRY/CMD_RUN_INTERACTION_ACTION/CMD_FETCH_AVATAR` 等——一个慢插件或大图上传阻塞所有 IPC（含 pause/stop/ping）。
- **改动**：带 `reply_to` 的 RPC 型命令改 `asyncio.create_task` 执行（task 集合跟踪 + stop 时取消）；控制型命令（pause/resume/stop/reload*）保持内联保序。
- **验收**：RPC 执行期间 ping 仍即时回 pong；stop 能取消在飞 RPC。

### W3 · 交互关键词守卫缓存

- **现状**：[loader.py:2350-2375] `_interaction_bot_owns_incoming_text` 每条 incoming 消息一次 DB 查询（读交互配置），且命中即静默丢弃、无 trace（[loader.py:2502]）。
- **改动**：关键词/开关命令集合缓存进 `_AccountState`（`reload_account_config` 与周期 reconcile 时刷新）；命中时写一条 SKIPPED span（reason `interaction_rule_owned`）再丢弃。
- **验收**：普通消息路径零 DB 查询（mock 断言）；命中守卫的消息在 trace 可见。

### W4 · payout worker 执行器 + 限速收编 + 删除文案协议

- **现状**：`ctx.messages`/事件动作的 userbot 发送直接 `state.client.send_message` 裸发（[loader.py:1166-1186]），不过 `RateLimitEngine`；自动发奖靠正则解析公告文案（[runtime.py:240-249、388-568]）。
- **改动**：
  1. `_run_interaction_userbot_action`（[runtime.py:252]）新增 `action_type="payout"`：`state.engine.acquire("send_message")`（含 humanize）→ `client.send_message(chat_id, text, reply_to=...)` → 返回 message_id；loader 侧 `_apply_userbot_*` 增加同款 payout applier（进程内路径用）。
  2. 所有 userbot 通道的 `send_message/send_file/edit` applier 统一接入 `state.engine.acquire`（engine 不可用时降级直发并写 warn）。
  3. **删除** `_parse_account_bot_winner_notice`、`_try_account_bot_auto_award`、`_register_account_bot_auto_award` 及 `_ACCOUNT_BOT_AUTO_AWARD_*` 常量（与 P 泳道确认所有玩法插件已改用 payout 后再删，检查点 3 前完成）。
- **验收**：payout 动作经限速引擎（测试断言 acquire 被调）；文案协议代码删除后全量测试绿。

### W5 · userbot 会话喂入（命令触发模型的核心，进程内零 IPC）

- **现状**：userbot 收到的群消息只进 `on_message`（原始 Telethon event），无法参与标准会话；插件被迫双实现。
- **改动**：在 `_make_dispatcher`（[loader.py:2451]）的 direct passthrough 之后、event bus 之前插入会话喂入：
  1. `_AccountState` 维护 `userbot_session_chats: set[int]`（W6 建会话、W7 扫描器、reconcile 时刷新；miss 路径零 Redis）。
  2. 命中 chat → 读该 chat 的 `channel=="userbot"` 会话 → 用 `normalize_userbot_event(event)` 构信封 + `session` 分区（含 data）+ `trigger` → **进程内直调** `invoke_interaction_entry`（W1 的调用级 ctx）→ 动作经 `_apply_userbot_*` 执行，默认通道 = `userbot`（S3 契约）。
  3. 命令前缀消息跳过（命令分发器已处理）；插件消费（返回非空动作或 end_session）则本条消息不再进该插件的 legacy on_message；`include_outgoing: true` 的入口才喂 outgoing 消息。
  4. 按钮文本降级回投（见 W8）在此层合成 callback 信封。
- **验收**：联调用例——命令开局后，群友消息经 userbot 会话直达插件（与交互 Bot 路径同一信封形状快照对比，除 `source.channel` 外一致）；全程无 IPC/Bot API 调用。

### W6 · 命令触发自动注册

- **现状**：插件要自己维护 `commands` 字典 + 5 参数 handler + on_message 三件套。
- **改动**：manifest entry 支持 `"triggers": {"command": "guess"}`。`_activate` 时对声明了 command 且账号配置未设 `keyword_only`（见 W8-2）的入口自动 `register_plugin_command`：包装器 = 权限检查（owner/sudo，沿用现行命令门禁）→ 创建 `channel="userbot"` 会话（ttl 取 entry 声明或默认 600）→ 构 `source.type="command"` 信封（args 放入 `trigger.args`）→ 进程内调入口 → 应用动作。旧 `commands` 字典机制保留不动（流式/工具插件继续用）。
- **验收**：样例插件 manifest 声明 command 后，`,guess 100` 直接走单入口开局；`keyword_only` 时命令不注册。

### W7 · userbot 会话扫描器 + update_session 动作

- **改动**：
  1. worker 每 15s 扫描本账号 `channel=="userbot"` 且 `expires_at<=now` 的会话 → 投 `session_expired` 信封（进程内）→ 应用动作 → 删除（与 R11 互补，按 channel 分工）。
  2. 新动作 `{"type": "update_session", "data": {...}}`：两侧执行器（delivery + loader appliers）合并写回会话 `data` 字段并续期 TTL（不重置 expires_at，除非动作带 `extend_seconds`）。插件从此可用 `session.data` 持久化游戏状态，不再需要内存字典 + 锁。
- **验收**：update_session 后再次事件投递能读回 data；worker 重启后状态不丢（新用例）；userbot 会话超时事件可达。

### W8 · userbot 通道按钮文本降级（已拍板方案）

- **改动**：
  1. **发送侧**（userbot 会话内 send_message 带 `reply_markup`）：不再静默剥除——渲染为编号选项追加到文本（`\n\n请回复序号选择：\n1. <按钮文本>\n2. …`；url 按钮渲染为 `文本：URL`），发送纯文本；把 `{序号|按钮文本 → callback_data}` 映射存入会话 data 保留键 `_tp_button_map`（含发出的 message_id，新面板覆盖旧面板）。
  2. **接收侧**（W5 喂入层）：会话内消息精确匹配序号或按钮文本 → 合成 `source.type="callback_query"` 信封（`callback_data` 取映射值、`callback_query_id=None`、`source.synthetic="text_button"`）投给插件；插件返回的 `answer_callback` 对合成事件记 SKIPPED（reason `synthetic_callback`），不报错。
  3. **配置开关**：平台对声明了 command trigger 的入口在插件配置 schema 中注入保留字段 `interaction_trigger_modes`（`all` 默认 / `keyword_only`），配置页自动渲染下拉；manifest entry 可用 `"default_trigger_modes": "keyword_only"` 声明默认值（强按钮玩法用）。运行时 W6 读取该值决定是否注册命令。
- **验收**：userbot 会话中带按钮的题面变成编号选项；玩家回复 `1` 触发与点按钮完全相同的插件 callback 分支；`keyword_only` 入口无命令注册且配置页可切换。

---

## 6. 泳道 S：发送层（parse_mode / 连接复用 / 通道契约）

### S1 · parse_mode 全链路 + 转义工具 + 安全截断

- **现状**：Bot API 默认 `parse_mode="HTML"`（[account_bot_service.py:1456]），userbot 代发硬编码 html（[runtime.py:271/285]、[loader.py:1172]）；`ctx.messages.send()` 没有 parse_mode 参数（[message_ops.py:23-51]）；`text[:4000]`（[account_bot_service.py:1460/1515]）盲截断可把 HTML 实体拦腰切断整条消息发送失败。玩家名含 `<` 即可炸掉中奖公告。
- **改动**：
  1. 动作与 `ctx.messages.send/edit` 增加 `parse_mode` 参数：`"html" | "plain"`，**默认 `plain`**（plain = 不传 parse_mode 给 Telegram / Telethon `parse_mode=None`）。
  2. 提供 `app.worker.plugins.textutil.html_escape()`（复用 `account_bot_service.html_text`）导出给插件。
  3. 截断改为格式安全：html 模式下先转义/构造完再按实体边界截断（最简实现：截断后校验标签配平，不平则回退到更短的安全长度）；框架自有文案路径（`_render_transfer_bot_notice` 等）同步检查。
  4. 全链路透传：contracts 保留字段、delivery 与 worker appliers 按动作 parse_mode 发送。
- **验收**：含 `<>` 玩家名的公告在 plain 模式原样可达；html 模式超长文本截断后仍能成功发送；快照测试更新。

### S2 · httpx AsyncClient 复用

- **现状**：[account_bot_service.py:1430-1435] 每次 `call_bot_api` 新建 `httpx.AsyncClient`——每次 getUpdates 与每条回复都付一次 TLS 握手。
- **改动**：模块级共享 `AsyncClient`（连接池上限 20，随 app lifespan 关闭；`sendPhoto` 的 multipart 路径同改）。
- **验收**：现有 Bot API 测试全绿（mock 层适配共享 client）；无连接泄漏（lifespan 关闭断言）。

### S3 · 默认通道 = 会话通道（契约核心）

- **现状**：[contracts.py:31-39] 未指定 selector 的动作默认 `["interaction_bot"]`；插件被迫理解 send_via。
- **改动**：
  1. guard 阶段注入：`guard_interaction_actions` 增加 `session_channel` 入参（来自信封 `session.channel`，无会话时按来源通道——交互 Bot 链路 `interaction_bot`、userbot 链路 `userbot`）；无显式 selector 的发送类动作 `send_via = session_channel`。显式 selector 行为不变（逃生门）。
  2. `payout` 动作豁免：永远 userbot（R10/W4）。
  3. `reply_markup` 与 userbot 通道相遇时不再剥除，改交 W8 降级流程（contracts 中保留"非 userbot 会话下剥除"的旧逻辑仅用于显式跨通道覆盖场景，写 info 日志）。
  4. worker 侧 `_normalize_interaction_action`（[loader.py:3458]）同步默认值逻辑。
- **验收**：无 channel 动作在 interaction_bot 会话走 Bot、在 userbot 会话走 userbot；payout 恒 userbot；契约测试覆盖三态。

### S4 · contracts 死代码清理

- **改动**：删除 [contracts.py:280-291]（`send_via_options` 必非空的不可达分支）与 [contracts.py:316-325]（reply_markup 二次剥离不可达分支），随 S3 重构一并处理。

### S5 · delivery 可见性补全

- **改动**：[delivery.py:49] `actions[:10]` 截断时对被丢弃动作写 `warn` runtime_log + FAILED action 记录（reason `action_limit_exceeded`，登记 reason_code）；`start_session` 动作补 SKIPPED trace（与其他 session 控制动作一致，[delivery.py:55]）。worker 侧 [loader.py:908] 同款截断同款处理。

### S6 · save_message_id_key 命名空间隔离

- **改动**：[delivery.py:952] `action_save_message_id_key` 实际存取时自动加前缀 `tp:msgid:{account_id}:`（读写两侧一致，delivery 与 [loader.py:1487] `_save_action_message_id`），消除跨账号冲突；key 校验规则不变。

---

## 7. 泳道 E：事件契约（event_bus / 投影对象）

### E1 · MessageRef / TelePilotEvent 扩字段

- **现状**：[events.py:21-28] `MessageRef` 仅 6 字段，无 media/entities/caption/date/thread_id/forward/sender_chat——直传对象前必须扩，否则插件仍要回头挖 raw。
- **改动**：`MessageRef` 增加 `caption/date/thread_id/entities(list[dict])/media(dict|None)/forward(dict|None)/sender_chat(dict|None)/edited(bool)`；`event_from_interaction_payload` 从 R6 后的信封填充；`SessionRef` 增加 `channel`。保持 dataclass slots。
- **验收**：投影测试——R6 信封转对象后媒体/实体字段齐全。

### E2 · filter schema 严格化 + rule_bound 值比对

- **现状**：[event_bus.py:402-420] `_filters_match` 只认 keywords/contains/callback_data/commands，未知 filter key 静默忽略（trace 里看似生效实则装饰）；`rule_bound` scope（[event_bus.py:392-394]）只检查 `trigger.rule_id` **存在性**，不与 `filters["rule_id"]` 比对。
- **改动**：① 定义 `SUPPORTED_FILTER_KEYS` 常量并导出；`normalize_event_subscription` 遇未知 key 保留但标记 `unknown_filter_keys`，decision 输出带 warning reason；manifest lint（`scripts/validate-*` 与安装校验）对未知 key 给告警。② `rule_bound` 改为：`filters` 声明了 `rule_id` 时必须与 `state.trigger.rule_id` 值相等。
- **验收**：未知 filter key 触发 lint 告警与 decision warning；rule_id 不匹配的 rule_bound 订阅 skipped。

### E3 · `all_events` 订阅值

- **现状**：[event_bus.py:363-367] `all_messages` 只覆盖 `message/command`，名字误导。
- **改动**：`VALID_EVENT_TYPES` 增加 `all_events`；匹配语义 = 除需要显式声明的高危类型外全部（`message/command/callback_query/keyword/payment_confirmed/session_close/session_expired/message_edited`；inline 两类仍需显式订阅）。`all_messages` 语义不变并在文档标注"仅 message/command"。
- **验收**：`all_events` 订阅能收到 callback 与 payment_confirmed；`all_messages` 行为不变。

### E4 · known_users（event_bus 侧配合 R5）

- **改动**：`_scope_matches` 不变（语义正确），但补测试固定语义：known_users = state 提供的真实集合，防止未来回归；文档字段表同步（泳道 D）。

### E5 · 入口直传投影对象（依赖 E1 + R6）

- **改动**：`invoke_interaction_entry`（loader）与本地 fallback、`_invoke_userbot_event_bus_entry` 在调用插件前构造 `TelePilotEvent`，作为 payload 的伴生参数注入：约定 `payload["tp_event"]`（对象在进程内传递；跨 IPC 时由 worker 侧重建，不序列化对象本体）。插件签名不变（仍 `on_interaction(ctx, entry_key, payload)`），新插件读 `payload["tp_event"]` 或继续读分区字段。文档以 tp_event 为主路径示例。
- **验收**：worker 内直调与 IPC 两条路径 `tp_event` 均可用且字段一致。

---

## 8. 泳道 P：插件迁移（依赖检查点 2）

### P1 · guess_number 重写为参考实现

- 单一 `on_interaction` 入口处理 `command/keyword/payment_confirmed/message/callback_query/session_expired`；manifest 声明 `triggers.command="guess"`；状态全部走 `session.data` + `update_session`（删除 `self._games/_locks/_tasks/_auto_timeout`）；发奖用 `payout` 动作（删除 `_send_prize_reply` fallback 链）；删除 `on_message`/`commands` 双实现与 [plugin.py:394-462] 的 130 行防御读取（改读 `tp_event`/标准分区）。目标 ≤250 行。
- **验收**：命令开局（全 userbot）与关键词开局（交互 Bot + userbot payout）两条端到端用例通过；worker 重启后游戏状态存活。

### P2 · 其余玩法插件迁移

- 按 P1 模式迁移：`dice_grid_hunt`（依赖 R7 的骰子媒体下放）、`poetry_blank`、`lottery_plus`、`redpack-byRBQ`、`sum`、`pt_promote`、`bot_mute_guard`（按需）；builtin/official 的 `auto_reply/autorepeat/forward/scheduler` 属流式插件**不迁移**（继续 on_message/on_event）。每个插件补触发/会话/超时三类测试；文本输出改用 `parse_mode="plain"` 或显式转义。
- **验收**：`scripts/validate-installed-interaction-plugins.py` 全绿；每插件端到端用例至少 2 条。

### P3 · 清理旧机制

- 与 R/W 协调删除：math answer 路由层与 math10 本地 fallback（R15 未做时由本任务在 R 泳道 review 下删除）、auto-award 文案协议（W4 第 3 步的触发条件）、`_send_prize_reply` 式样板在样例中的残留。
- **验收**：全仓 `grep` 无 `_MATH_GAME\|_parse_account_bot_winner_notice\|AUTO_PAYOUT_MODULE_KEYS` 引用；全量测试绿。

---

## 9. 泳道 D：文档与发布

### D1 · 开发指南更新（与代码同 PR 或紧随其后）

| 文档 | 必改内容 |
| --- | --- |
| `docs/PLUGIN-DEV-GUIDE.md` | 新编程模型总述：触发方式→会话通道表（§1.2）、单入口生命周期（command/keyword/payment/message/callback/session_expired）、`session.data`+`update_session` 状态存储、`payout` 动作、按钮文本降级行为与 `keyword_only` 开关、`parse_mode` 与转义规范 |
| `docs/PLUGIN-API-REFERENCE.md` | 信封字段表更新（`message.media/entities/date`、`chat.title/username`、`session.channel/expires_at`、`source.synthetic`、`message_edited/session_expired/all_events`）；动作表增 `payout/update_session`、`parse_mode`；`tp_event` 投影对象字段表；filter 支持键清单（E2）；known_users 语义；删除"通道选择"作为常规步骤的描述，标注 `send_via` 为高级覆盖；`interaction_trigger_modes` 配置说明；fast-ack 声明 |
| `docs/PLUGIN-CHEATSHEET.md` | 单入口最小模板（含 payout、update_session、session_expired）替换现有双入口示例 |
| `docs/PLUGIN-REMOTE.md` / `docs/PLUGIN-RULES.md` | manifest 新字段：`triggers.command`、`default_trigger_modes`、`callback_fast_ack`、`include_outgoing`；lint 新告警（unknown filter keys） |
| `docs/PLUGIN-SAFETY.md` | userbot 通道会话的风控说明（全部经 RateLimitEngine）、payout 永远 userbot、按钮降级边界 |
| `docs/INTERACTION-BOT-OPTIMIZATION.md` | 追加 0.48 会话通道模型章节与"关键词规则依赖交互 Bot"约束 |
| `docs/INTERACTION-OPEN-EVENT-FRAMEWORK-PLAN.md` | 顶部加指引：0.48 起以本文（INTERACTION-PIPELINE-UNIFICATION-PLAN）为准 |
| `README.md` | 核心能力段一句话更新（插件单入口、会话通道） |

### D2 · CHANGELOG

- `Unreleased` 段按泳道归并：新能力（会话通道模型/payout/单入口/按钮降级/session_expired/all_events）、修复（R1-R5/W1-W3/S1/S5 等对应用户可见问题）、性能（并发分发/连接复用/单读配置）。发布时移入 `0.48.0 minor（次版本）`。

### D3 · 版本发布

- 同步 bump：`backend/app/__init__.py`（当前 0.47.4）、`backend/pyproject.toml`、`frontend/package.json`、`frontend/src/lib/version.ts` → `0.48.0`；按 AGENTS.md 只在最终发布提交执行。

---

## 10. 测试计划

### 10.1 单元/契约（各泳道自带，上文已列验收）

### 10.2 快照测试矩阵（R6/R7/R9 后统一刷新）

覆盖：关键词触发、付款触发（有/无 reply_to）、callback、会话内文本、会话内媒体（dice）、message_edited、command 触发（userbot 通道）、session_expired。每种断言：12 分区齐全、`message.entities/media/date` 真实、`session.channel` 正确、平铺字段仍在（迁移期）。

### 10.3 检查点 2 联调用例（必须全过才进阶段三）

1. 命令开局 → 群友文本作答 → userbot 提示 → 猜中 → userbot 公告 + payout `+N` → end_session（全程无 Bot API 发送）。
2. 关键词开局 → 交互 Bot 题面（带按钮）→ 玩家点按钮 → Bot 编辑消息 → 猜中 → Bot 公告 + **userbot** payout → end_session。
3. 用例 2 中 worker 离线 → payout 立即失败（<100ms）且公告仍发出，trace 有 `userbot_offline`。
4. userbot 会话发带按钮题面 → 自动降级编号选项 → 玩家回复 `2` → 插件收到合成 callback（callback_data 正确）。
5. 会话超时 → 两种通道各自收到 session_expired → 超时公告发出 → 会话清理。
6. 双 bot 同群：同一条玩家消息只被会话通道对应的一侧投递（另一侧 SKIPPED span 可见）。

### 10.4 回归重点

`test_account_bot.py`、`test_account_bot_auto_award.py`（W4 删除后改写为 payout 测试）、`test_event_bus.py`、`test_plugin_events.py`、`test_installed_interaction_plugin_contracts.py`、`test_supervisor_reliable_consumer.py`。

---

## 11. 版本建议与验收标准

**版本**：`0.48.0 minor（次版本）· 消息链路统一与会话通道`。

发布前必须全部满足：

1. 插件开发者对一个互动玩法只需实现**一个** `on_interaction` 入口，不写 `on_message`/`commands` 双实现，不写通道选择，不写发奖 fallback，不写内存状态锁（以迁移后的 guess_number 为准绳）。
2. 命令触发 = 全 userbot 收发；关键词触发 = 全交互 Bot 收发 + payout 走 userbot；两条路径插件代码零差异。
3. §10.3 六个联调用例全绿；快照矩阵（10.2）全绿；全量 pytest + 两个 validate 脚本 + 前端 typecheck/build 通过。
4. 消息不再静默消失：失败 update、守卫丢弃、通知无规则、动作截断在 trace/runtime_log 均可查。
5. worker 离线、玩家名含 HTML 字符、配置保存与运行并发、同插件并发调用四类场景不再产生本文列出的对应缺陷。
6. 已安装互动插件全部迁移并通过校验；文档清单（§9-D1）全部更新。
