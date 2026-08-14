# 一致性核销与文档真实性审计方案 v1

> 2026-08-15 定稿。两个独立审计 WP：WP-A **零代码修改**，WP-B **仅文档修改**。
> ⚠️ 项目铁律：**计划文档 ≠ 代码现状**。本文与所引旧设计文档中的行号、结论都是历史快照（部分来自 0.53.x 时代），一切以当前 Beta 分支代码复核为准。
> 进度真相源：`agent-plans/EXECUTION-STATE.md`（开工标 in_progress，完成标 done + commit 哈希）。

## §1 背景与目的

0.97.0 树形组合化轮已收官（值守、资金闸、声明与按叶点枝、接线协议全部落地并验收）。满分收尾剩两条主线：

1. **三通道一致性**（WP-A）：2026-07-09 全面 review 确认过一批交互框架 bug（当时基线 0.53.x）。此后多个版本修掉了其中一部分（0.94/0.95 的互动奖励与重复发奖修复、补偿系统、T3 资金闸等），但**从未逐条销账**。"同一片叶在不同嫁接通道行为不一致"是插件生态最大的隐性成本：插件作者会把框架的坑当成自己的 bug。
2. **文档真实性**（WP-B）：插件文档族是生态的宪法。本轮仅核对声明章节就抓出一处会让派奖游戏判错声明的措辞（`ledger` 判据，已修 `5519be29`）。需按同一标准把全族文档过一遍。

## §2 全局纪律（两单通用）

1. **不混轮**：WP-A 只核销、只记录，**禁止顺手修代码**；WP-B 只修文档，**禁止改代码**。发现代码 bug 一律进清单。
2. **证据格式**：每条判定必须附`文件:行号`或 commit 哈希。"已修"必须给修复 commit 或现行代码证据；"未修"必须给当前代码的最小复现路径。
3. **行号复核**：所引旧设计文档（`agent-plans/2026-07-09-*.md`）里的行号全部过期，逐条重定位。
4. **测试盘点**：每条结论同时回答"是否已有回归测试锁定"。
5. 完成即更新 EXECUTION-STATE 对应条目；WP-B 的文档修正在 CHANGELOG 记一条。

## §3 WP-A 三通道一致性核销 —— 预估 2~3 天（纯审读）

**目的**：把历史确认的交互框架 bug 清单对当前代码逐条销账；产出核销表与（若有未修项）修复轮立项草案。

**必读输入**：
- `agent-plans/2026-07-09-session-timing-design.md`（会话时序修复设计：方案 A1 + 两伴随修）
- `agent-plans/2026-07-09-dedup-sessiongrab-executor-audit.md`（执行体三体 10 项漂移清单 + parity 测试设计）
- `agent-plans/2026-07-09-payout-compensation-design.md`（补偿闭环设计）

**待核销清单（八项，逐条判定 已修 / 未修 / 部分修）**：

1. 关键词 / 付款 / 事件订阅通道**开局 `update_session` 悬空**：动作应用先于建会话导致开局丢状态；userbot 命令通道因框架先建会话而正常 → 同插件跨通道行为不一致。
2. **信封嵌套不一致**：交互 bot 侧把整条会话记录塞进信封 `data`，loader 侧正确解包 `session["data"]` → 续会话/过期路径插件读到记录外壳。
3. **`_save_interaction_session` 重建抹 data**：只继承 created_at / started_by / paid 集合 → 第二笔付款重触发抹掉已攒状态。
4. **payout 失败补偿闭环**：当年"收款成功→结算失败只能人工翻日志"；大概率已被补偿系统 + T3 资金闸解决，销账需给完整证据链。
5. **跨管道双分发竞态**：同一群消息经 userbot 观测与 bot 直收两路喂会话、无 message 级去重 → bot group privacy off 时可能双结算；0.95.0"修复重复发奖"可能相关。
6. **双通道抢会话**：bot 侧保存会话时无条件覆写 `channel=interaction_bot`，userbot 会话可被翻转且 data 丢失。
7. **过期扫描声明检查漂移**：worker 侧过期扫描不检查入口是否声明 `session_expired`，bot 侧检查 → 行为漂移。
8. **执行体三体漂移**（E1 loader 直执行 / E2 delivery / E3 worker RPC，按 10 子项展开核销）：历史最重两项为 E3 不读 reply_markup（userbot 按钮经 bot 路静默丢失）、错误码族与限流拒绝行为跨执行体分叉。注意 T3 资金闸已统一了 payout 拒绝面，核销时区分"T3 已覆盖"与"仍分叉"。

**产出**：
1. **核销表**：八项（第 8 项按子项展开）×（判定 / 证据 / 是否有回归测试锁定）。
2. 未修项的**修复轮 PLAN 草案**：只立项、排优先级、估工作量，不动工。

**卡点**：核销表先交人审，确认后再写修复轮草案。

## §4 WP-B 插件文档族真实性审计 —— 预估 2~4 天

**范围**：`docs/` 下 PLUGIN-OVERVIEW / PLUGIN-QUICKSTART / PLUGIN-DEV-GUIDE / PLUGIN-API-REFERENCE / PLUGIN-CHEATSHEET / PLUGIN-RULES / PLUGIN-SAFETY / PLUGIN-AI / PLUGIN-HTTP / PLUGIN-DEVTOOLS / PLUGIN-REMOTE / PLUGIN-WEBHOOK-QUICKSTART，以及 PLATFORM-CAPABILITIES 与 SYSTEM-AGENT 中涉插件章节。

**方法**：逐篇提取**可验证断言**（API 签名、facade 方法与上下文矩阵、权限名、错误码、行为描述、示例代码），逐条对当前代码核对。分类四档：**正确 / 过时 / 误导 / 缺失**（代码有能力而文档未载）。

- 重点盯"**误导类**"——`ledger` 判据那种会让插件作者做出错误决定的措辞，是最高优先级。
- `ctx.messages` 上下文 × 方法矩阵（PLUGIN-API-REFERENCE §4.3 一带）逐格验证。
- 示例插件真跑 `scripts/validate-plugin-examples.py`；文档内代码片段与当前契约比对。
- 声明章节（PLUGIN-REMOTE 的 requires_platform_capabilities 节）本轮已核过，可跳过复核、只查交叉引用。

**修改权限**：
- **事实性错误**（签名、字段名、路径、数值、失效引用）→ 直接修，提交信息逐条附代码证据。
- **语义/取舍类措辞**（涉设计判断）→ 不修，列清单卡点送裁定。

**产出**：逐篇问题清单（含四档统计）+ 事实性修正提交 + 语义类待裁定清单。

**卡点**：语义类清单交人裁定；事实性修正随做随交。

## §5 顺序与派单

两单零交集，可并行（分 worktree）或串行任选。每单提示词 = 本文件 §1+§2 + 对应 WP 小节。

## §6 EXECUTION-STATE 粘贴模板

```markdown
## 当前计划：一致性核销与文档真实性审计 v1（agent-plans/2026-08-15-consistency-audit-plan.md）

- [ ] WP-A 三通道一致性核销 — pending
- [ ] WP-B 插件文档族真实性审计 — pending
```
