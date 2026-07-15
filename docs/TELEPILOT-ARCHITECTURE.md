# TelePilot 架构图说明

本文是 README 架构图的文字解读，目标是帮助维护者快速定位组件边界与数据流。

## 1. 组件职责

- `Web / PWA GUI`：运维入口，负责账号配置、插件配置、日志查看与系统操作。
- `FastAPI`：统一 API 网关，处理认证、配置读写、审计落库与 worker 调度。
- `PostgreSQL`：持久化账号、规则、模板、插件配置、日志与审计记录。
- `Redis`：进程间通信（IPC）、限速令牌与部分短生命周期数据。
- `Worker Supervisor`：按账号生命周期拉起/停止 worker 子进程并监控存活状态。
- `Account Worker`：每个账号一个独立执行单元，处理 Telegram 消息、插件分发、定时任务。
- `Plugin Runtime / Plugin API`：插件执行容器，按 manifest/config_schema 和运行时上下文执行插件逻辑；代码层 API 仍叫 `Plugin`。
- `LLM Providers`：由插件或 AI 指令模板调用的外部大模型服务。
- `Account Bot Manager`：管理每账号可选的控制 Bot polling runtime，用于授权用户的远程运维入口。
- `Interaction Bot Manager`：管理群消息、关键词、按钮、付款确认和交互会话使用的 Bot polling runtime。

## 2. 关键数据流

- 用户通过 GUI 发起配置操作，写入由 FastAPI 校验后进入 PostgreSQL。
- FastAPI 将必要变更通过 Redis 通知 worker，worker 拉取最新配置并在本账号作用域生效。
- worker 接收 Telegram 事件后执行指令派发与插件逻辑；需要 AI 能力时经 provider 路由访问外部 LLM。
- 账号 Bot runtime 通过 Telegram Bot API 接收授权用户指令，再调用 FastAPI/worker 完成账号级操作。
- 交互 Bot runtime 接收群消息、callback 和外部付款证据，完成规则匹配与会话路由；插件业务通过 Event Bus、标准事件信封和 MessageOps 执行。
- `payout` 不随会话通道切换，固定交给账号 Worker 的 userbot 执行，并进入限额、持久化幂等、补偿和 ActionEvent 链路。

## 3. 生命周期与就绪状态

- FastAPI 启动时分别拉起 Worker Supervisor、Account Bot Manager 和 Interaction Bot Manager。任一组件启动失败都会记录组件错误并指数退避重试，不会只在启动日志中失败一次后永久缺席。
- `/healthz` 只表示 FastAPI 进程仍在运行；`/readyz` 同时检查 PostgreSQL、Redis 和三个关键运行组件。生产反代、Compose 与更新流程应使用 `/readyz` 判断是否可以接流量。
- 全局总闸会保存目标状态，并并行停止或恢复 Worker、账号 Bot manager 和交互 Bot manager。任一运行时操作或 Redis 广播失败时，接口返回 `503 KILL_SWITCH_PARTIAL_FAILURE`，不能把目标状态写入当成运行时已经完全收敛。
- Worker 暂停期间拒绝规则执行、交互入口、交互 action、Webhook 投递和 payout 补偿扫描等副作用。Redis 或数据库无法确认关键状态时，资金、预算、风控和交互 claim 采用 fail-closed。

## 4. 隔离与边界

- 账号隔离：每账号独立 worker 进程，默认不共享运行态内存与会话。
- 权限边界：管理权限、账号权限与插件权限都以账号作用域为主，不跨账号隐式升级。
- 插件边界：插件应只依赖公开 PluginContext 与稳定 API，不直接耦合内部私有实现；第三方插件的 `ctx.client`（以及指令 handler 的 `client` 参数）均为 sandbox client。

PluginContext 的可用字段、禁止事项与最小示例见 `docs/PLUGIN-API-REFERENCE.md` 和 `docs/PLUGIN-SAFETY.md`。

## 5. 非目标说明

本文仅解释现有架构图，不引入新的运行时模型，不修改权限模型、schema、
workflow、artifact、template renderer 或 marketplace 设计。
