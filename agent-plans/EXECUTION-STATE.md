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

- [ ] WP4 usage v2 与统计口径修正 — **partial** `efa12878`
  - 已有：`run_id`、`elapsed_ms`、既有 provider/model/tokens、`used_fallback`/`stream_fallback`/`tool_count`（旧语义）
  - **缺口**：`schema_version: 2`；`requested_*` / `selection_mode`；`api_format`；**`tool_calls`（实际调用数）与 `available_tools` 分拆**；前端 meta 仍读 `tool_count` 暴露数
- [ ] WP5 本轮模型选择（不改全局） — **pending**
  - 现存缺陷仍在：Composer / 配置里选模型仍 `patchSystemAgentConfig` 写全局
  - **缺口**：请求体 `model_selection`；pinned 校验；三入口透传；localStorage 会话级选择；「设为默认」与本轮选择分离
- [ ] WP6 无气泡 + RunTrace + ModelRunMeta — **partial** `efa12878` + `7b500b2e`
  - 已有：助手无气泡正文；`ResponseMeta.tsx`；`RunTrace.tsx` 懒加载 events
  - **缺口**：`ModelRunMeta.tsx`（共享组件名/三行 fallback 规格）；`runTraceState.ts` 事件 reducer；运行中默认展开/完成收起/失败保持展开；meta 用实际 `tool_calls`；轨迹在正文上方的布局顺序
- [ ] WP7 ModelPicker 能力徽标 — **partial** `efa12878`
  - 已有：capabilities `model_matrix` 透传声明/实测/health；Composer option 文案徽标
  - **缺口**：独立 `components/ai/ModelPicker.tsx`（Provider 分组、灰显不可用、健康槽位）；测活筛选 lite
- [ ] WP8 测活复用（可选） — **deferred**（不阻塞轮次 2 验收）

### 轮次 3：运行时健康与冷却

- [ ] WP9 健康状态记录 — **partial** `efa12878`
  - 已有：`provider_health.py` 两态+冷却；`llm_runtime._emit_usage` 写入；测活 source 跳过；单测
  - **缺口**：能力不支持/参数错误不计故障的完整分类；Redis 镜像/`uncertain` 呈现（当前异步 Redis 镜像基本跳过）
- [ ] WP10 健康状态消费 — **partial** `efa12878`
  - 已有：`sort_provider_candidates`；matrix 冷却徽标
  - **缺口**：Agent runtime/fallback **尚未调用**排序；ModelPicker 完整 degraded UI；测活页「运行时健康」只读栏；集成测

## 已完成计划（历史）

### System Agent 强化（PLAN-system-agent-hardening.md，2026-07-23）

- [x] WP1–WP7 全部完成（`e17bb057`…`1870ff05`）
- 部署：`alembic upgrade head` → 0048 `system_agent_user_memory`

## 备注

- 2026-07-23 PLAN 升 v2：轮次 2 重编号为 WP4–8，轮次 3 为 WP9–10；本文件已按 v2 重写清单。
- **v1 实施（`7b500b2e`/`efa12878`）不等于 v2 完成**——轮次 1 对齐；轮次 2/3 需按 v2 补齐缺口后再标 done。
- 下一批建议：先 WP4 口径 + WP5 本轮选择（后端契约），再 WP6/7 前端工作台形态，最后 WP9/10 把排序接进 runtime。
