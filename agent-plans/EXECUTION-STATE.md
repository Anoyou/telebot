# EXECUTION-STATE（进度真相源）

> 当前计划：`agent-plans/PLAN-conversation-deeix.md`
> 纪律：每 WP 完成即更新本文件；新会话恢复从本文件开始。
> 状态：`pending` → `in_progress` → `done`（附 commit 哈希）/ `blocked`（附原因）

## 当前计划：对话体验修复与 DEEIX 吸收

### 轮次 1：流式渲染修复轮（纯前端）

- [x] WP1 统一流式/最终 Markdown 渲染器（光标改 CSS 伪元素） — done `7b500b2e`
- [x] WP2 气泡复用去闪动（assistant_message 同 id 翻转） — done `7b500b2e`
- [x] WP3 delta_reset 降级为状态行（过渡语不再消失） — done `7b500b2e`

### 轮次 2：统一对话面 + 能力矩阵

- [x] WP4 meta 数据补齐：usage 增写 run_id + elapsed_ms（零迁移） — done（本批）
- [x] WP5 无气泡正文 + meta 行 + 执行轨迹面板（RunTrace/ResponseMeta） — done（本批）
- [x] WP6 模型选择器能力徽标（声明/实测分开展示） — done（本批）
- [ ] WP7 测活页组件复用（最小化，可推迟不阻塞） — deferred（按计划可不阻塞）

### 轮次 3：运行时健康与冷却

- [x] WP8 健康状态记录（两态+cooldown，真实调用才写入） — done（本批）
- [x] WP9 健康状态消费（Agent 排序钩子 + 前端徽标） — done（本批，排序工具 `sort_provider_candidates` 已就绪；前端冷却徽标在 model_matrix）

## 已完成计划（历史）

### System Agent 强化（PLAN-system-agent-hardening.md，2026-07-23 完成）

- [x] WP1–WP7 全部完成（见 git log `e17bb057`…`1870ff05`）
- 部署备注：`alembic upgrade head` 应用 0048 `system_agent_user_memory`。

## 备注

- 2026-07-23 完成 PLAN-conversation-deeix 主体；WP7 按计划可推迟。
- 门禁：相关前端 typecheck/test 绿；provider_health 单测绿。
