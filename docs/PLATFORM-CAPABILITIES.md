# 平台能力热插拔

TelePilot 的核心定位是稳定的平台级 API 与通道。以下能力属于**可选平台模块**，可在不重启服务的情况下热关闭与热启动：

| 模块 key | 设置键 | 作用 |
|---|---|---|
| `ai` | `ai_enabled` | 模型 Provider、AI 指令、插件 `ctx.ai` |
| `interaction_bot` | `interaction_bot_enabled` | 交互 Bot / 测试 Bot 与 `interaction_bot` 通道 |
| `webhooks` | `webhooks_enabled` | 公开入站 Webhook 投递 |
| `ledger` | `ledger_enabled` | 台账查询、统计、导出与人工操作面 |
| `dispatch_debug` | `dispatch_debug_enabled` | 命中模拟与 router debug trace |

## 边界

### 平台内核（不可被这些开关关闭）

- `userbot` 通道
- 插件加载器、事件信封、会话协议
- Action 执行、审计、结算与失败补偿
- 管理 Bot（`start_account_bot_manager`）

### 固定通道契约

- `userbot`：核心通道，始终可用。
- `interaction_bot`：由 Interaction 模块管理，可暂停。
- `webhook`：固定入站通道，由 Webhook 模块管理。

“关闭”表示通道或模块当前不可用，**不删除**通道定义、插件配置、Token 或数据库数据。

## 状态模型

设置存放在现有 `SystemSetting` KV 表，**无新迁移**。缺失时默认 `enabled=true`，保证旧版本升级行为不变。

持久化（DB）：

```json
{ "enabled": true, "generation": 0 }
```

现有 `ai_enabled={"enabled": ...}` 读取时会把缺失的 `generation` 规范化为 `0`。

运行时状态（进程内存，不写 KV）：

- `runtime_state`：`starting` / `ready` / `quiescing` / `stopped` / `failed`
- `last_error` / `last_transition_at`

服务进程重启后，runtime 从 `starting` 重新收敛，不能把上次进程的 `ready` 当作当前事实。

状态转换：

```text
stopped -> starting -> ready
ready -> quiescing -> stopped
starting/ready -> failed
failed -> starting
```

关闭必须先进入 `quiescing`，阻止新任务，再处理或取消可取消的后台任务，最后进入 `stopped`。

## 进程内缓存与 fail-closed

主进程在启动时从 DB 预加载能力缓存；开关写入后立即替换缓存。Worker 在启动、`reload_account_config` 与约 180 秒周期 reconcile 时刷新自己的快照。

公开入口（如入站 Webhook）**只读缓存**：

- 缓存未初始化或刷新失败时按 **fail-closed**（视为关闭）
- 关闭路径不访问 DB、不读 body、不查账号、不校验 Token、不限流、不向 worker 投递

## 热切换链路

```text
PATCH /api/system/capabilities/{module_key}
  -> 持久化 desired + generation
  -> 更新主进程能力缓存
  -> 主进程本地启停（如 interaction bot manager）
  -> 向各账号 worker 发送现有 CMD_RELOAD_CONFIG
     payload: source=platform_capabilities, generation, ...
  -> worker ACK 或由周期 reconcile 收敛
  -> 插件入口重新计算
  -> 前端刷新模块与插件状态
```

**不新增** `CMD_RELOAD_CAPABILITIES`。复用 `CMD_RELOAD_CONFIG`、`publish_cmd_with_ack()` 与 `reload_account_config`。

Worker 离线或 ACK 超时不能伪装成完全成功；ACK 必须回传实际加载的模块 generation 与开关值，只有达到本次请求 generation 才计为已收敛。DB 目标状态保留，周期 reconcile 与 worker 启动加载负责最终收敛。

Worker 在注册插件、命令 handler 或 scheduler 前必须先从 DB 初始化能力快照；主进程 Interaction Bot manager 与遗留 polling handler 也只在缓存就绪且模块开启时工作。读取或刷新失败一律 fail-closed，由 supervisor/主进程重试器等待 DB 恢复后重新收敛，不使用默认开启值抢跑。

## API

- `GET /api/system/capabilities`：模块状态、固定通道、generation、worker 收敛摘要（不含 Token/密钥）
- `PATCH /api/system/capabilities/{module_key}`：写入单个模块目标状态并热切换 + 审计

兼容：

- `GET/PATCH /api/system/settings` 的 `ai_enabled` 仍可用，委托平台能力服务处理
- 新增四个开关不继续堆入 `rate_limit.py` 设置接口

## 各模块关闭语义

### AI

- 不加载 Provider、模型客户端与插件 `ctx.ai`
- AI 命令、模板与系统助手 AI 调用被拒绝
- 仅依赖 AI 的入口暂停；同一插件的 userbot / 非 AI 入口继续运行

### Interaction Bot

- 只管理 `start_interaction_bot_manager` / `stop_interaction_bot_manager`、交互 Bot、测试 Bot 与相关会话任务
- **管理 Bot 不属于该模块**，关闭交互时必须继续运行
- 关闭时**停止**交互 polling 与测试 Bot；**保留**会话过期任务，使已有会话按原始过期时间 drain
- `interaction_bot` 来源 Event Bus 事件在路由层被裁剪，不再投递；userbot 来源继续
- 遗留 polling 任务若仍在，处理更新前会再次检查模块状态并丢弃
- 账号级交互开关、Token 与规则配置不修改
- 已有会话按原始过期时间处理，重新开启时不复活已过期会话
- 进行中的派奖、补偿与资金结算不被强行取消（不触碰 userbot payout 链路）

### 入站 Webhook

- 外部投递最前层返回 `404`
- worker 内部 `CMD_WEBHOOK_DELIVER` 同样拒绝，防止关闭瞬间竞态
- 已保存的 Token 与 hook 配置保留

### 资金台账

- 关闭页面、查询、统计、导出、重置、人工标记与 System Agent 台账工具
- **ActionEvent、支付确认、派奖失败补偿与审计继续写入**
- 重新开启后可见关闭期间历史数据
- 这是“台账操作模块冻结”，不是停止资金主账

### 命中调试

- 禁止 dispatch simulation、router debug trace 临时开关
- 关闭时按 Redis 前缀 `account_bot:router_debug_trace:` SCAN 删除临时 key
- 不停止普通错误日志、基础 Event Trace、插件运行状态与审计
- `log_retention.trace_enabled` 保持独立

## 插件契约

保留现有 `event_subscriptions.source`、`interaction_entries`、`result_contract.send_via`、`permissions` 与 `capabilities`。

新增可选字段：

- Manifest 级 `requires_platform_capabilities`
- 入口级 / event subscription 级 `requires_platform_capabilities`

兼容：

- 旧插件没有新字段时继续加载
- 旧插件调用 `ctx.ai` 时由 AI facade 返回结构化关闭错误
- 只有显式声明全插件依赖时才整体暂停
- Interaction Bot 专属能力不得静默降级成错误的 userbot 行为

`FeatureInfo` 新增：

- `runtime_availability`：`ready` / `partial` / `paused` / `transitioning`
- `available_channels`
- `blocked_entries`
- `blocked_reason_code`

统一原因码：

- `platform_module_disabled`
- `channel_disabled`
- `channel_not_configured`
- `capability_unavailable`
- `platform_module_transitioning`

有效入口计算：

```text
插件已启用
× 入口支持该通道
× 平台能力已开启
× 当前账号通道已就绪
= 入口实际可运行
```

运行时投递：

- `_event_bus_subscriptions_from_state` 会过滤插件级 `requires_platform_capabilities` 与通道源（interaction_bot / webhook）。
- `dispatch_webhook_event` 与 worker `CMD_WEBHOOK_DELIVER` 在副作用前再次检查模块缓存。
- `reload_account_config` 刷新能力快照，并在 AI 关闭时卸载已有 `ctx.ai`。

## 前端

- 系统设置 → 平台能力：展示目标状态、runtime 状态与 worker 收敛摘要
- 侧边栏与移动端导航统一按 capabilities 过滤（泛化原 `navForAIState`）
- 直达已关闭页面时显示“模块已暂停”，URL 保留
- 插件中心可展示“正常 / 部分可用 / 已暂停 / 等待热加载”

## 回滚

- 回滚不删除配置、Token、规则、会话或资金数据
- 旧版本不认识新开关时会忽略并恢复旧行为
- 正式回滚前先把新增模块开关恢复为 `true`，确认 worker 收敛后再切旧版本

## 相关代码

- `backend/app/services/platform_capabilities.py`
- `backend/app/api/platform_capabilities.py`
- `backend/app/schemas/platform_capabilities.py`
