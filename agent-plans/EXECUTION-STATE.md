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
  - `schema_version: 2`；`tool_calls` / `available_tools` 分拆；`requested_*` / `selection_mode` / `api_format`
  - 旧键 `tool_count` 仍写入=暴露数；前端 `ModelRunMeta` 读实际 `tool_calls`
  - `run_id` / `elapsed_ms` / `retry_count` 由 service 落库时 enrich
- [x] WP5 本轮模型选择（不改全局） — done `97594050` + `c2677986`
  - 后端：三入口透传 `model_selection`；pinned 校验；`model_pinned` 元数据；失败不静默换模型
  - 前端：`sessionModelSelection` localStorage；Composer `ModelPicker` 默认自动路由；「设为默认」才 patch 全局
- [x] WP6 无气泡 + RunTrace + ModelRunMeta — done `c2677986`
  - `ModelRunMeta.tsx`；`runTraceState.ts` 摘要 reducer；轨迹在正文上方、meta 在正文下方
  - 运行中默认展开 / 完成收起 / 失败保持展开；历史按 `run_id` 懒加载
- [x] WP7 ModelPicker 能力徽标 — done `c2677986`
  - `components/ai/ModelPicker.tsx`：Provider 分组、声明/实测分标、灰显不可用、健康槽位
  - 接 `capabilities.model_matrix`
- [ ] WP8 测活复用（可选） — **deferred**（不阻塞轮次 2 验收）

### 轮次 3：运行时健康与冷却

- [x] WP9 健康状态记录 — done `97594050`
  - 能力不支持/参数错误 → `capability` 类不计故障不冷却
  - 401/403 凭据标记不冷却；429 短冷却；超时/5xx 指数退避封顶 10min
  - 测活 source 跳过；单测覆盖
- [x] WP10 健康状态消费 — done `97594050` + `c2677986`
  - `build_fallback_chain` 与 Agent runtime 对 fallback 候选 `sort_provider_candidates`
  - ModelPicker 冷却徽标；matrix health 槽位已用
  - 测活页「运行时健康」只读栏：仍可后续补强（不阻塞）

## 已完成计划（历史）

### System Agent 强化（PLAN-system-agent-hardening.md，2026-07-23）

- [x] WP1–WP7 全部完成（`e17bb057`…`1870ff05`）
- 部署：`alembic upgrade head` → 0048 `system_agent_user_memory`

## 备注

- 2026-07-23 PLAN 升 v2：轮次 2 重编号为 WP4–8，轮次 3 为 WP9–10；本文件已按 v2 重写清单。
- **v1 实施（`7b500b2e`/`efa12878`）不等于 v2 完成**——轮次 1 对齐；轮次 2/3 已在本轮补齐（WP8 可选延后）。
- 下一批建议：可选 WP8 测活复用 ModelRunMeta；测活页运行时健康只读栏；整批审查后合入 main。
