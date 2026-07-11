# Telebotmini 优化提案 vs TelePilot 实际代码 — 对比报告

> 对象：`mini_optimization_direction_for_telebot_devs-1.md`（Telebotmini 框架收敛提案）
> 基线：TelePilot `codex/0.33-interaction-framework` 分支实际代码（2026-07-09 核对）
> 结论方式：逐条对照提案主张与当前实现，判定 TelePilot 是【已实现/更强】/【已实现但更重】/【真缺口·值得采纳】/【不适用】。所有判定附代码证据。

---

## 一句话总览

**这份提案描述的"插件宿主框架"，TelePilot 已经实现了其中 80%，而且多数地方做得更完整、更硬。** 提案的价值不在"给 TelePilot 指方向"，而在于它是一面镜子：照出 TelePilot 三个真实可取的收敛点（简单模式 SDK、能力账单式权限、限频 key 加身份维度），以及提醒 TelePilot 不要在已经证明有价值的地方（trace 链路）盲目做减法。

提案的底层假设是"从零做一个更轻的框架"，而 TelePilot 是"一个已经长出业务、需要向框架收敛"的成熟系统——**方向高度一致，但 TelePilot 已经走得更远**。提案里大量"建议做"的事，TelePilot 是"已经做了且有测试锁定"。

---

## 逐条对照

### 1. 框架职责边界：core 不理解业务（提案 §1-2）

**判定：✅ 已实现且理念完全一致。**

提案主张"框架只理解账号/身份/事件/路由/动作/发送/限频/日志/插件/配置，不理解红包/游戏/发奖"。TelePilot 正是如此：
- core 侧 `loader.py`/`account_bot_runtime.py`/`delivery.py` 全文无任何"红包/lottery/dice"业务词，业务全在 `plugins/installed/`（9 个插件）。
- 框架给的是通用契约：`interaction_entries` + `events` + `actions`（send/edit/payout/update_session…），插件只按 `event.type` 分发。
- 唯一"半业务"概念是 `payout`（发 "+金额" 文本），但它被抽象成一个通用动作、不含任何记账逻辑——记账由群内第三方 bot 完成。这恰好符合提案"框架管通道"的边界。

**TelePilot 更强的地方**：提案只到"理念"，TelePilot 有 `test_installed_interaction_plugin_contracts.py` 把"插件零通道感知"这条边界用测试锁死了。

---

### 2. 全量消息下放 + 来源标记（提案 §3）

**判定：✅ 已实现，且比提案的结构更细。**

提案建议事件打 `source: {type, identity_id, account_id, bot_id, display_name}`。TelePilot 的 `normalize_userbot_event`（event_bus.py:285）产出的 payload 里 `source` 有 `type`/`channel`/`observed_channel`/`hook_key` 等字段，且区分了"逻辑通道"与"实际观测通道"（userbot 替 interaction_bot 观测群消息的场景，提案完全没覆盖到）。

**TelePilot 更强**：提案的 source 是静态标记；TelePilot 的 source 还携带"跨身份观测"语义（`observed_channel`），这是提案的模型表达不了的真实复杂度。

**提案略胜一点的细节**：提案显式建议 `display_name`（人话身份名）进 source。TelePilot 的 source 里没有直接带 display_name，前端要另查——这是个**可取的小改进**（见末尾"值得采纳"3）。

---

### 3. 消息路由极短 + 日志只留两类（提案 §4, §11, §14）

**判定：⚠️ 理念一致，但提案在这里的"做减法"建议对 TelePilot 是退步，不能采纳。**

提案主张"路由只三步、投递状态只有'收到/没收到'两种、因此不需要默认开启复杂 trace 链路"。这对一个**纯 ping-pong 插件框架**成立，但 TelePilot 的核心场景是**收钱→游戏→发奖的资金链路**——恰恰需要"这条消息在哪一步断了、payout 发出去没有"的完整可追溯性。

证据：TelePilot 的 `event_trace`（source_channel/span/record_action）是本轮 review 中被反复依赖的排障基石；payout 补偿闭环、跨管道去重 bug 的定位全靠它。如果按提案"默认关 trace"，这些资金 bug 根本无法定位。

**结论**：提案的日志观（系统底层日志 + 插件日志 + 默认关的轻量路由诊断）适合无资金语义的轻框架；TelePilot 因为管钱，trace 是特性不是负担。**这一条提案不适用**，但它的"系统终端日志要捕获 stdout/stderr/asyncio 异常并做 secret redaction"（§12）是好的——而 TelePilot 已经有（`docs/LOG-CENTER-REBUILD-PLAN.md` 对应的日志中心重构已落地，verdict 后端化、secret 脱敏在 `redactor.py`）。

---

### 4. ctx.client 是代理而非真实 client（提案 §8）

**判定：✅ 已实现，且比提案更严。**

提案："插件不应拿到真实 Telethon client，应是 SendProxyClient，防止绕过限频和日志。"

TelePilot 的 `SandboxClient`（sandbox.py:248）：installed 插件拿到的是方法白名单代理（`_require_allowed_method`），按 manifest permissions 放行 9 类方法，且**封禁了 `session`/`__class__`/`__dict__` 等反射逃逸**（sandbox.py:7 注释明确"移除 session 防第三方插件访问真实 session"）。

**一个诚实的差距**：TelePilot 里 **builtin 插件**和**直通模式**拿到的是真实 Telethon client（不过 sandbox），限频确实可被绕过——这正是本轮 review C 议题"三条限流旁路"的发现之一，波次 0/3 已补了命令回复/AI 回复/SandboxClient 三路限流。所以 TelePilot 现状与提案理想的差距**已经在收敛中**。提案在这一点上和 TelePilot 的修复方向完全同频。

---

### 5. 默认回复身份 via=source（提案 §9）

**判定：✅ 已实现，且 TelePilot 的约束更贴合真实业务。**

提案建议 `via` 语义：source/userbot/default_bot/bot:id/auto。TelePilot 的 `guard_interaction_actions`（contracts.py）按 `session_channel` 注入默认 send_via，插件不指定就走来源通道——这就是提案的 `via=source`。TelePilot 还额外有一条提案没有的硬约束：**payout/收款永远强制 userbot**（交互 bot 无收付款能力），这是真实业务倒逼出的规则，提案的纯技术模型想不到。

**提案略胜**：提案的 `via="auto"`（发 inline button 时若 source 是 userbot 则自动选可用 bot）是个优雅的能力自适应。TelePilot 现在是"payout 强制切 userbot"这种硬编码切换，没有通用的 `auto` 能力路由——但 TelePilot 单账号的 bot 拓扑比提案设想的简单（见下），所以 `auto` 的收益有限。

---

### 6. 限频在代理层 + key 带身份维度（提案 §10）

**判定：⚠️ 已实现代理层限频，但 key 结构上提案指出了一个真实的小缺口。**

TelePilot 的限频（`buckets.py`）是 Redis Lua 五窗口令牌桶，key 结构：
```
rl:{account_id}:{action}:s|m|h|d
rl:{account_id}:{action}:peer:{pid}:m   ← 已有同会话维度
```
提案建议 key 至少 `rate:send:{identity_id}:{chat_id}`，并加身份级全局 `rate:identity:{identity_id}`。

对比：TelePilot 的 key 是 `{account_id}:{action}` 维度 + peer 维度，**没有 identity(bot vs userbot) 维度**——因为限频引擎主要服务 userbot 动作。这在 TelePilot 是合理的：交互 bot 走 Bot API、封号风险不在主账号，所以没给 bot 单独限频。**但如果未来一个账号挂多个 bot 且都高频发消息，提案的 `identity_id` 维度就有价值**。当前单账号 bot 拓扑简单（`account_bot.role` 主要是 viewer/管理角色，非"多个发消息 bot"），所以这是**低优先的可取点**，不是当前缺口。

---

### 7. 统一能力层 MessageOps/InteractionOps/…（提案 §7）

**判定：✅ 已实现，命名不同但结构对应。**

提案建议抽象 MessageOps/InteractionOps/IdentityOps/AdminOps/EventOps。TelePilot 的 `ctx.messages`（`message_ops.py`：send/edit/delete/pin/answer_callback/payout/update_session…）= MessageOps + InteractionOps 合体；`ctx.http`/`ctx.ai`/`ctx.scheduler` 是提案没列的额外能力面。

**TelePilot 缺的**：IdentityOps（改名/改头像/改简介）和 AdminOps（群管理/ban）——提案 §7.1/§15 P4 列的 userbot 高级能力，TelePilot 目前确实没有把它们抽进 ctx。这些是**真实的能力覆盖缺口**，但属于"要不要做这类功能"的产品决策，不是框架结构问题。

---

### 8. 插件开发复杂度：简单模式 SDK + 能力账单（提案 §5-6）★核心可取点★

**判定：🎯 这是提案对 TelePilot 最有价值的一条——真缺口，值得采纳。**

提案主张两级 SDK：
```python
# 简单模式
@plugin.command("ping")
async def ping(ctx):
    await ctx.reply("pong")
# 框架自动推导：监听 command=ping、需要 read_event + send_message
```
以及"能力账单"——开发者只写功能，框架通过 SDK 使用/lint/运行时捕获**推导**权限需求，而非手写 manifest permissions。

**TelePilot 现状**：写插件必须手写完整 `manifest.py` + `plugin.json`（双文件）、显式声明 `permissions`/`interaction_entries`/`events`/`config_schema`。本轮 WP1 脚手架（`tp_plugin.py`）用"一条命令生成骨架"缓解了这个负担，但**没有做到提案的"先写功能→框架推导权限草案"**——权限仍是手写。

这正好击中用户的核心痛点（"写一个玩法插件要适配好几套"）的**下一层**：脚手架解决了"起步慢"，但没解决"声明繁琐"。提案的 `@plugin.command` 装饰器式简单模式 + 权限自动推导，是 WP1 之后插件体验的自然演进方向。**强烈建议纳入后续路线**。

---

### 9. 插件安装全局 / 启用配置账号级（提案 §15.2）

**判定：✅ 已实现，且是 TelePilot 做得最好的部分。**

提案："插件安装是全局的、启用是账号级的、配置是账号级的。" TelePilot 完全就是这样：`installed_plugin` 台账全局、`account_feature(account_id, feature_key)` 按账号启停、三层配置合并（schema<全局<账号）。这条提案对 TelePilot 是"已完成的确认"，不是建议。

---

### 10. 动态配置 schema 渲染 + 控件白名单（提案 §15.3）

**判定：✅ 已实现，控件覆盖度相近。**

提案建议控件白名单 text/password/number/switch/select/multi-select/json/bot-picker/chat-picker/secret/list。TelePilot 的 config_schema 已支持 `x-ui-mode`、readOnly 预览、Draft7 校验，前端 GenericPluginConfig 动态渲染。bot-picker/chat-picker 这类领域控件 TelePilot 是否全有需前端逐一核对，但机制完全就位。提案"不建议插件直接注入前端代码"——TelePilot 本就是纯 schema 驱动，符合。

---

### 11. 账号工作台顶部账号选择器（提案 §15.1）

**判定：✅ 已实现。** TelePilot 前端账号切换后各页面跟随，是现有信息架构。这条是确认。

---

## 三、真正值得从提案吸收的（按性价比排序）

| # | 采纳点 | 来源 | 理由 | 成本 |
|---|---|---|---|---|
| 1 | **简单模式 SDK（`@plugin.command` 装饰器）** | §6.1 | 直击用户核心痛点的下一层：脚手架解决起步、简单模式解决"每次都要声明一堆"。最小插件从"双文件+manifest"降到"一个函数" | 中（新增 SDK 门面 + 装饰器注册，兼容现有 manifest 模式） |
| 2 | **能力账单 / 权限自动推导** | §5.2 | 开发者写 `ctx.messages.delete()` → lint 自动推导需要 delete 权限，不用手写 permissions 数组。配合 WP1 的 `check` 命令天然契合 | 中（静态分析 SDK 调用 → 生成 permissions 草案） |
| 3 | **source 里带 display_name（人话身份名）** | §3.2 | 前端展示身份不用再查一次；日志/trace 可读性提升 | 低（normalize 时补一个字段） |
| 4 | **限频 key 加 identity 维度** | §10.2 | 为"单账号挂多个高频 bot"预留；现在单 bot 拓扑收益低，但改动小可顺手 | 低（key 前缀加 identity 段），低优先 |
| 5 | **IdentityOps/AdminOps 能力面** | §7.1 | 改名/改头像/群管理，若产品要做这类功能则需要；当前非刚需 | 高，按需 |

## 四、提案不适用于 TelePilot 的地方（勿盲从）

1. **"默认关 trace 链路"（§4/§11/§14）**：提案面向无资金语义的轻框架成立；TelePilot 管钱，trace 是资金可追溯性的基石，**不能砍**。
2. **"投递状态只有收到/没收到两种"**：TelePilot 的动作有 OK/FAILED/SKIPPED/PENDING/COMPENSATED（payout 补偿状态机），远比提案的二值模型丰富，这是业务复杂度决定的，不是过度设计。
3. **整体"从零收敛成 mini"的框架重构**：TelePilot 是已长出业务、已有 1410 测试锁定的成熟系统，提案的"推倒重来做更纯的 core"对 TelePilot 是负收益；正确姿势是**在现有框架上做提案 §5-6 的局部收敛**（简单 SDK + 权限推导），而非重写。

## 五、总结

提案和 TelePilot 是"同一张地图的两个位置"：提案站在起点规划路线，TelePilot 已经走到路线的 80%。**提案确认了 TelePilot 的架构方向是对的**（框架管通道、插件管业务、代理 client、账号级启用、schema 配置全部命中），同时精准地指出了 TelePilot 插件开发体验还差的最后一公里——**简单模式 SDK + 权限自动推导**。这一条建议纳入 WP1 脚手架之后的插件体验演进；其余多为"已实现"的确认，个别（关 trace）需明确拒绝。
