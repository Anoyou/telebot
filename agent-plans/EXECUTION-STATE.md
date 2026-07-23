# EXECUTION-STATE（进度真相源）

> 当前计划：`agent-plans/PLAN-conversation-deeix.md` **v2**
> 纪律：每 WP 完成即更新本文件；新会话恢复从本文件开始。
> 状态：`pending` → `in_progress` → `done`（附 commit 哈希）/ `blocked` / `partial`（附缺口）

## 当前计划：对话体验修复与 DEEIX 吸收 v2

### 轮次 1：流式渲染修复轮（纯前端）

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

## 已完成计划（历史）

### System Agent 强化（PLAN-system-agent-hardening.md，2026-07-23）

- [x] WP1–WP7 全部完成（`e17bb057`…`1870ff05`）
- 部署：`alembic upgrade head` → 0048 `system_agent_user_memory`

## 备注

- **PLAN-conversation-deeix v2 全部 WP 已完成**（WP1–10）。
- **2026-07-23 验收通过**（commit `836f0279` 含验收 lint 修复）：ruff 清零、后端 2315 passed、前端 typecheck+test 绿、a11y 24 passed（0 critical/serious）、diff-check 干净；WP1-10 关键实现逐点抽查属实。遗留两项见下。
- 遗留 1：`provider_health._mirror_to_redis` 是空桩（检测到 async 客户端即返回），健康状态实际仅进程内；`HealthState.UNCERTAIN` 定义了但永远不会产生。跨进程共享需后续实现或删桩明示。
- 遗留 2：`test:visual` 未跑——本轮 UI 大改，基线需人工过目后用 `pnpm --dir frontend test:visual:update` 更新，不宜盲更。
- 下一批建议：整批 UI 手验（流式/fallback/审批/测活/失败重试）→ 更新视觉基线 → 合入 `main`（版本号仅发布时 bump）。
