# EXECUTION-STATE（进度真相源）

> 当前计划：`agent-plans/PLAN-embedded-codex-gateway.md` **v1**
> 纪律：每 WP 完成即更新本文件；新会话恢复从本文件开始。
> 状态：`pending` → `in_progress` → `done`（附 commit 哈希）/ `blocked` / `partial`（附缺口）

## 当前计划：树形组合化改造 v1.1（agent-plans/2026-08-14-tree-composability-plan.md）

- [x] WP-T2 接线规范 — done `104ddddb`
- [x] WP-T3 钱的保险丝 — done `61c87613`
- [x] WP-T1 值守预设 — done `ee60ffed`
- [x] WP-T4 声明补齐+按叶点枝+看树视图 — done `8f426055`

### 挂起窗口（扳机制：故意不做，条件到了才开工，非遗漏）

- [~] WP-T5 AI 写操作工具 — 扳机：首个真实写工具需求；实现必须走 PreparedAction 确认流
- [~] AI Provider 插件化 seam — 扳机：llm_router 契约硬化完成 + 出现真实第三方接入需求
- [~] 新枝脚手架（能力模块单点定义 + codegen 波及全楼）— 扳机：首次真实"加盖"需求；首根新枝与脚手架同批施工
- [~] 可选能力声明 optional_platform_capabilities（叶的"营养不良"降级模式）— 扳机：首个"枝暗仍可降级运行"的真实插件需求
- [~] 更多运行预设（轻量 / 开发沙箱）— 扳机：真实部署场景出现

备注：下面把前三条每条的扳机从抽象翻译成你能一眼认出的具体场景。

T5 · AI 写操作工具——当你想"对 AI 说一句话，让插件替你做一件事"

现在插件给 System Agent 的工具全是只读的：查数据、看状态、汇总报表。扳机响起的样子是：

你哪天想对 AI 管家说"把今晚的抽奖开起来""给这个用户补发 100 游戏币""把 XX 群的自动回复关了"——而这些动作要经插件去执行；
或者你写新插件时，想暴露一个"创建活动 / 改配置 / 发奖"类的工具给 Agent 用。

判据一句话：第一次希望 AI 经插件"改变世界"而不只是"报告世界"时，T5 开工。做法已定死：全部走 PreparedAction 确认流（AI 摆出"我要做这个"，你点确认才执行），约一周。

AI Provider seam——当你第二次为接新模型源而改核心

这条的扳机是双条件，性质不同：

需求侧（被动等）：你想接一个核心不认识的模型供应商或私有部署协议，而且预感这类事以后还会有。实用判据：第一次为接新 Provider 改核心，正常，改就是了；第二次再遇到，说明这是一类持续需求，seam 该开了。
前置侧（主动做）：llm_router 契约硬化不是"等来的事件"，是一项工程。如果需求先到而硬化没做，就把硬化轮和 seam 一起立项，需求驱动前置——不会出现"干等前置"的死锁。

现实判断：你的模型接入目前核心 + Codex Gateway 全覆盖，这条大概率是三条里最晚响的。

新枝脚手架——当你真的要"加盖一层"

最典型的加盖场景：接入第二个消息平台（Discord、Slack、微信 Bot——新根系+新枝），或者需要一个重资源的独立子系统（本地语音转写池、浏览器自动化池这类）。

判据三连问，全部为"是"才算加盖：需要独立开关吗？需要能整层断电吗？有自己的生命周期（启动/静默/停止）吗？——有一个"否"，它就只是给现有楼层拉管线（扩 facade，小时到天级，不劳驾脚手架）。加盖那天的做法也定死了：首根新枝与脚手架同批施工，第一根慢、往后每根一天级。

## 当前计划：一致性核销与文档真实性审计 v1（agent-plans/2026-08-15-consistency-audit-plan.md）

- [x] WP-A 三通道一致性核销 — done `9669daf7`
- [x] WP-B 插件文档族真实性审计 — done `edce2d94`

## 当前计划：三通道一致性修复轮（agent-plans/2026-08-15-three-channel-consistency-fix-plan.md，已完成）

- [x] P1 结果/错误语义收口 — done `3e121d8d`
- [x] P2 纯空白 payout 文案统一回退 — done `3e121d8d`
- [x] P3 三方独立 parity 与可观测字段补强 — done `3e121d8d`
- [x] P4 插件 API 执行体分工表 — done `3e121d8d`

## 当前计划：内置 Codex Gateway / 全链路 Agent 接入 / LLM 错误语义收口 v1

### 阶段 A：错误语义收口

- [x] WP-A1 统一诊断事实 — done `cd305088`
- [x] WP-A2 全调用面接入 — done `546277d3`
- [x] WP-A3 前端与用量展示 — done `e6e8b430`

### 阶段 B：独立 Gateway 模块

- [x] WP-B1 进程骨架与契约 — done `b63267fd`
- [x] WP-B2 Provider 快照与精确路由 — done `726d9495`
- [x] WP-B3 Codex Responses 数据面 — done `cb3192f1`

### 阶段 C：TelePilot 全链路接入

- [x] WP-C1 运行时管理与配置同步 — done `88374f4e`
- [x] WP-C2 Provider schema 与 Client Builder — done `03e8aac6`
- [x] WP-C3 Agent、插件、测活与 usage 验收 — done `1892beee`
- [x] WP-C4 Provider UI、健康与文档 — done `6dbeeaeb`

开发工作包已完成；发布审查补齐了 Codex session/thread/turn/window/installation、prompt cache 与出站身份契约。2026-08-03 对原受限 Provider #32 的对照请求未复现原 `official clients` 403，但 Provider 反代当前返回 `502 Lucky Bad Gateway`；该结果既不能证明身份已被接受，也不能标记为真实模型成功。amd64/arm64 Gateway 二进制与完整 Web runtime 镜像已完成本地构建检查，线上 Deploy Check 仍属于发布后外部验收。

---

## 当前计划：Bot 交互修复 / 插件工具插槽 / OpenWorker 三点 v4

### 轮次 F：Bot 修复轮（先行，独立可发布）

- [x] WP-F1 draft 重复消息修复（final 不进 draft + clear_draft 专用路径） — done `162f74b7`
- [x] WP-F2 审批可见性（角色过滤有声提示 + /agent 状态 + 日志留痕） — done（待 commit）

### 轮次 O：OpenWorker 三点

- [x] WP-O1 定时运行落完整轨迹（origin=scheduled 会话 + Run + Web 深链） — done `12158aa0`
- [x] WP-O2 Agent 自建自动化（scheduler.save 暴露 agent_prompt + 防套娃） — done `354114f3`
- [x] WP-O3 待确认收件箱（侧栏角标 + 列表页 + /agent pending） — done `2fdae86a`

### 轮次 X：插件工具插槽（第一期只读）

- [x] WP-X1 manifest expose → 动态注册 + IPC 执行 + 安全四件套 — done `0c9e728f`
- [x] WP-X2 前端来源徽标 + 文档 + 示例插件 + golden set — done `df907abc`

## 已完成计划（历史）

### Agent 延迟优化 / 定时任务 / 更新提速（PLAN-agent-latency-and-ops v3，2026-07 完成）

#### 轮次 L：延迟与词表

- [x] WP-L1 阶段计时 stage_timings（先测量） — done `57303157`
- [x] WP-L2 路由调用提速（1 次重试 + 8s 预算 + 与探测并行） — done `5c0d367b`
- [x] WP-L3 词表扩容 + golden set ≥20 条（项目自身问法/英文） — done `5c0d367b`
- [x] WP-L4 探测不阻塞（按已知状态放行 + 后台刷新） — done `5c0d367b`

### 轮次 T：定时 Agent 任务

- [x] WP-T1 scheduler `agent_prompt` 类型（只读工具集 + 写操作永远 pending） — done `4002a221`
- [x] WP-T2 异常日志巡检预设模板 — done `4002a221`

### 轮次 U：更新提速

- [x] WP-U1 更新作业分步计时 — done `f5a70bb5`
- [x] WP-U2 按测量结果优化 top 1-2 项 — done `f5a70bb5`

### 对话体验修复与 DEEIX 吸收（PLAN-conversation-deeix v2，2026-07-23 完成并验收）

#### 轮次 1：流式渲染修复轮（纯前端）

- [x] WP1 统一流式/最终 Markdown 渲染器（光标 CSS 伪元素 + 围栏补闭合） — done `7b500b2e`
- [x] WP2 气泡复用去闪动（`live-assistant-stream` 同 id 翻转） — done `7b500b2e`
- [x] WP3 delta_reset 降级为状态行 — done `7b500b2e`

### 轮次 2：模型执行工作台（一个功能批次审查）

- [x] WP4 usage v2 与统计口径修正 — done `97594050`
- [x] WP5 本轮模型选择（不改全局） — done `97594050` + `c2677986`
- [x] WP6 无气泡 + RunTrace + ModelRunMeta — done `c2677986`
- [x] WP7 ModelPicker 能力徽标 — done `c2677986`
- [x] WP8 测活复用与快速单测 — done `dd9b25a7`
  - 统一九态词表 `livenessStatus.ts`（正常/降级/失败/超时/限流/协议不匹配/缺少能力/跳过/未知）
  - 对话测活与全量测活结果行复用 `ModelRunMeta`；不接 Durable Run、不写 Agent 会话
  - 测活页能力筛选 lite（Tools / Vision）；`?provider=&model=` 快速深链
  - Provider 模型列表「对话」入口跳转测活并预选模型；保留原「测试」连通性

### 轮次 3：运行时健康与冷却

- [x] WP9 健康状态记录 — done `97594050`
- [x] WP10 健康状态消费 — done `97594050` + `c2677986` + `dd9b25a7`
  - Agent fallback 排序；ModelPicker 冷却徽标
  - 测活页 `RuntimeHealthBar` + `GET /api/commands/llm-providers/runtime-health` 只读

### System Agent 强化（PLAN-system-agent-hardening.md，2026-07-23 完成）

- [x] WP1–WP7 全部完成（`e17bb057`…`1870ff05`）
- 部署：`alembic upgrade head` → 0048 `system_agent_user_memory`

## 备注

- **2026-07-25 v4 计划创建**：Bot draft 重复/按钮可见性根因、审批两层语义、插件插槽与 OpenWorker 三点设计已核实写入 PLAN 头部，worker 勿重复调查。

- **PLAN-conversation-deeix v2 全部 WP 已完成**（WP1–10）。
- **2026-07-23 验收通过**（commit `836f0279` 含验收 lint 修复）：ruff 清零、后端 2315 passed、前端 typecheck+test 绿、a11y 24 passed（0 critical/serious）、diff-check 干净；WP1-10 关键实现逐点抽查属实。遗留两项见下。
- 遗留 1：`provider_health._mirror_to_redis` 是空桩（检测到 async 客户端即返回），健康状态实际仅进程内；`HealthState.UNCERTAIN` 定义了但永远不会产生。跨进程共享需后续实现或删桩明示。
- 遗留 2：`test:visual` 未跑——本轮 UI 大改，基线需人工过目后用 `pnpm --dir frontend test:visual:update` 更新，不宜盲更。
- 下一批建议：整批 UI 手验（流式/fallback/审批/测活/失败重试）→ 更新视觉基线 → 合入 `main`（版本号仅发布时 bump）。
