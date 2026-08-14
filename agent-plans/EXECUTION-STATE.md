# EXECUTION-STATE（进度真相源）

> 当前计划：`agent-plans/PLAN-embedded-codex-gateway.md` **v1**
> 纪律：每 WP 完成即更新本文件；新会话恢复从本文件开始。
> 状态：`pending` → `in_progress` → `done`（附 commit 哈希）/ `blocked` / `partial`（附缺口）

## 当前计划：树形组合化改造 v1.1（agent-plans/2026-08-14-tree-composability-plan.md）

- [x] WP-T2 接线规范 — done `104ddddb`
- [x] WP-T3 钱的保险丝 — done `61c87613`
- [x] WP-T1 值守预设 — done `ee60ffed`
- [x] WP-T4 声明补齐+按叶点枝+看树视图 — done `8f426055`
- [~] WP-T5 AI 写操作工具 — 挂起（扳机：首个真实写工具需求）

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
