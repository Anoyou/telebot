# EXECUTION-STATE（进度真相源）

> 当前计划：`agent-plans/PLAN-system-agent-hardening.md`
> 纪律：每 WP 完成即更新本文件；新会话恢复从本文件开始。
> 状态：`pending` → `in_progress` → `done`（附 commit 哈希）/ `blocked`（附原因）

## 轮次 1：修复轮

- [x] WP1 token 限额口径修复（增量计费） — done `e17bb057`
- [x] WP2 记忆上限 CJK 标定 + 条目边界裁剪 — done `55579a5b`

## 轮次 2：强化轮

- [x] WP3 滚动摘要 LLM 压缩（后台任务 + summary_rev） — done `23af70a2`
- [x] WP4 工具结果防注入框架（logs/interaction） — done `23af70a2`
- [x] WP5 路由盲区修复 + golden set（≥40 条离线样例） — done `23af70a2`

## 轮次 3：功能轮

- [x] WP6 跨会话长期记忆（表 + 工具 + API + Web 面板） — done `155f712f`

## 轮次 4：收尾

- [x] WP7 SYSTEM-AGENT.md 重写「上下文与记忆」+ CHANGELOG — done `155f712f`

## 备注

- 2026-07-23 计划完成。
- 相关 pytest 已绿；前端 typecheck 已绿。
- 部署：`alembic upgrade head` 应用 0048 `system_agent_user_memory`。
