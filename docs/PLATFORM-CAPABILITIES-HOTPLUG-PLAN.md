# 平台能力热插拔改造计划

状态：已实施（主链路 + 运行时裁剪/冻结边界/关键自动化测试已补齐；人工验收与独立功能分支提交仍待）
制定日期：2026-07-22
实施日期：2026-07-22

## 1. 目标与范围

TelePilot 的核心定位是提供稳定的平台级 API 和通道。AI、Interaction Bot、入站 Webhook、资金台账、命中调试属于可选的平台能力模块，可以热关闭和热启动；通道契约固定存在，普通插件根据声明自动计算当前可用入口。

本计划覆盖：

- AI（沿用现有 `ai_enabled`，接入统一状态机）
- Interaction Bot 通道
- 入站 Webhook
- 资金台账查询与操作模块
- 命中调试模块

`userbot`、插件加载器、事件信封、会话协议、Action 执行、审计、结算和失败补偿属于平台内核，不纳入普通模块关闭范围。

## 2. 目标架构

```text
TelePilot 平台内核
├── Userbot 通道：核心常驻
├── 插件加载器、事件信封、会话协议
├── Action、审计、结算、补偿
│
├── 可热插拔平台模块
│   ├── AI
│   ├── Interaction Bot
│   ├── 入站 Webhook
│   ├── 资金台账界面/查询
│   └── 命中调试
│
└── 普通插件
    ├── 支持 userbot
    ├── 支持 interaction_bot
    └── 按入口声明 AI 等平台能力依赖
```

固定通道契约：

- `userbot`：核心通道，不受这些模块开关影响。
- `interaction_bot`：由 Interaction 模块管理，可以暂停。
- `webhook`：固定的入站通道，由 Webhook 模块管理。

“关闭”表示通道或模块当前不可用，不表示删除通道定义、插件配置或数据库数据。

### 2.1 现有可复用基础

实施不得重复建设当前已经存在的链路：

- `backend/app/worker/runtime.py` 的 `_periodic_config_reconcile` 已约每 180 秒调用 `reload_account_config(source="periodic_reconcile")`，并会重新读取 `ai_enabled`。
- `CMD_WEBHOOK_DELIVER`、`CMD_DISPATCH_SIMULATE` 和 `publish_cmd_with_ack()` 已存在。
- AI API 的 `_require_ai_enabled`、worker `CommandContext.ai_enabled` 和插件 `ctx.ai` 门禁已经覆盖主要路径。
- `Sidebar.tsx` 已有 `navForAIState`、`mobilePrimaryNavForAIState` 等桌面与移动导航过滤原型。

本次改造应扩展这些基础，不新增平行的 IPC、reconcile 或导航体系。

## 3. 统一模块状态

新增 `backend/app/services/platform_capabilities.py`，负责模块定义、状态读取、启停编排、并发切换和运行时状态汇总。

模块设置键：

- 保留 `ai_enabled`
- 新增 `interaction_bot_enabled`
- 新增 `webhooks_enabled`
- 新增 `ledger_enabled`
- 新增 `dispatch_debug_enabled`

设置继续存放在现有 `SystemSetting` KV 表中，不新增数据库迁移。设置缺失时默认 `true`，保证旧版本升级后行为不变。每个设置值保存 `enabled` 和 `generation`；现有 `ai_enabled={"enabled": ...}` 读取时把缺失的 generation 规范化为 `0`。

持久化状态：

- `desired_enabled`
- `generation`

运行时状态不写入 KV，由主进程内存结合 worker ACK/心跳聚合：

- `runtime_state`：`starting`、`ready`、`quiescing`、`stopped`、`failed`
- `last_error`
- `last_transition_at`

服务进程重启后，runtime state 从 `starting` 重新收敛，不能把上次进程的 `ready` 当作当前事实。`GET /api/system/capabilities` 同时返回 desired state、主进程状态和 worker 收敛摘要。

平台能力服务维护进程内只读快照。主进程在启动时从 DB 预加载，开关写入后立即替换缓存；worker 在启动、`reload_account_config` 和周期 reconcile 时刷新自己的快照。Webhook 等公开入口只读取缓存，缓存未初始化或刷新失败时按 fail-closed 处理。

状态转换：

```text
stopped -> starting -> ready
ready -> quiescing -> stopped
starting/ready -> failed
failed -> starting
```

关闭必须先进入 `quiescing`，阻止新任务，再处理或取消可取消的后台任务，最后进入 `stopped`。

## 4. 热启动与热关闭链路

```text
PATCH /api/system/capabilities/{module_key}
  -> 持久化目标状态
  -> 更新主进程能力缓存
  -> 主进程平台能力服务执行本地启停
  -> 向各账号 worker 发送现有 CMD_RELOAD_CONFIG
  -> worker ACK 或由周期 reconcile 收敛
  -> 插件入口重新计算
  -> 前端刷新模块与插件状态
```

不新增 `CMD_RELOAD_CAPABILITIES`。平台能力切换复用现有 `CMD_RELOAD_CONFIG`、`publish_cmd_with_ack()` 和 `reload_account_config`，payload 增加 `source="platform_capabilities"` 与 generation。worker 在同一次重载中刷新能力快照并重新计算插件入口。

worker 离线或 ACK 超时不能伪装成完全成功；数据库目标状态保留，现有约 180 秒周期 reconcile 和 worker 启动加载负责最终收敛。

新增能力 API：

- `GET /api/system/capabilities`：返回模块状态、固定通道状态、generation、worker 收敛摘要和不含敏感信息的资源摘要。
- `PATCH /api/system/capabilities/{module_key}`：写入单个模块目标状态并执行热切换与审计。

`rate_limit.py` 只保留现有 `/api/system/settings` 与 `ai_enabled` 的兼容读写，并委托平台能力服务处理；新增四个开关不继续堆入该文件。Token、Provider 密钥、代理 URL 不得由能力接口返回。

## 5. 各模块冻结边界

### AI

- 关闭后不加载 Provider、模型客户端和插件 `ctx.ai`。
- AI 命令、AI 模板、系统助手的 AI 调用被拒绝。
- 仅依赖 AI 的插件入口暂停；同一插件中的 userbot 和非 AI 入口继续运行。
- 已在执行的调用进入有限 drain；超时结果不得继续触发外部动作。

### Interaction Bot

- `interaction_bot_enabled` 只管理 `start_interaction_bot_manager` / `stop_interaction_bot_manager`、交互 Bot、测试 Bot 和相关会话任务。
- `start_account_bot_manager` 管理的管理 Bot 不属于该模块，关闭交互时必须继续运行。
- 停止所有交互 Bot、测试 Bot polling task。
- 停止交互会话过期任务和交互来源的规则投递。
- `interaction_bot` 来源的 Event Bus 事件不再投递，userbot 来源继续工作。
- 账号级交互开关、Token 和规则配置不修改。
- 已有会话按原始过期时间处理，重新开启时不能让已过期会话复活。
- 正在进行的派奖、补偿和资金结算不被强行取消。冻结 drain 只根据内存任务、队列项或持久化任务是否实际存在判断，不依赖“派奖状态字段一定正确”的假设。

### 入站 Webhook

- 关闭后外部投递接口最前层返回 `404`。
- `deliver_webhook` 必须先读取平台能力服务的进程内缓存，再进入 ingress 限流、账号查询、配置读取和 Token 校验。
- 关闭时不访问 DB、不读取 body、不查账号配置、不校验 Token、不执行限流、不向 worker 投递。
- 缓存未初始化或刷新失败时按关闭处理，不能为了判断开关临时查 DB 后再继续公开入口。
- 已保存的 Token 和 hook 配置保留。
- worker 内部的 `CMD_WEBHOOK_DELIVER` 同样拒绝，防止关闭瞬间的竞态请求执行。

### 资金台账

- 关闭页面、查询、统计、导出、重置、人工标记和 System Agent 台账工具。
- `ActionEvent`、支付确认、派奖失败补偿和审计记录继续写入。
- 重新开启后可以看到关闭期间产生的历史数据。
- 这是“台账操作模块冻结”，不是停止资金主账。

### 命中调试

- 禁止 dispatch simulation、router debug trace 临时开关和调试探针接口。
- router debug trace 当前存放在 Redis，key 前缀为 `account_bot:router_debug_trace:`。关闭时按该前缀 SCAN 并批量删除临时 key，复用 `_router_debug_trace_keys` 的命名规则。
- 不停止普通错误日志、基础 Event Trace、插件运行状态和审计链路。
- `log_retention.trace_enabled` 保持独立，不与命中调试开关混用。

## 6. 插件契约改造

保留现有 `event_subscriptions.source`、`interaction_entries`、`result_contract.send_via`、`interaction_send_via`、`permissions` 和 `capabilities`。

新增：

- Manifest 级 `requires_platform_capabilities`
- 入口级 `requires_platform_capabilities`
- Event subscription 级 `requires_platform_capabilities`

兼容规则：

- 旧插件没有新字段时继续加载。
- 旧插件调用 `ctx.ai` 时由 AI facade 返回结构化的能力关闭错误。
- 新插件可提前声明依赖，由平台在路由层裁剪不可用入口。
- 只有显式声明“全插件依赖 AI/交互”时才整体暂停。
- Interaction Bot 专属按钮、复杂富媒体或 Bot API 不得静默降级成错误的 userbot 行为。

`FeatureInfo` 当前没有 runtime/channel 字段。本次纯新增 `runtime_availability`、`available_channels`、`blocked_entries`、`blocked_reason_code`，并同步后端 schema、feature matrix 和前端 `types.ts`，不能只改前端推断状态。

统一原因码：

- `platform_module_disabled`
- `channel_disabled`
- `channel_not_configured`
- `capability_unavailable`
- `platform_module_transitioning`

插件有效入口计算：

```text
插件已启用
× 入口支持该通道
× 平台能力已开启
× 当前账号通道已就绪
= 入口实际可运行
```

## 7. 后端改造范围

核心目标文件：

- `backend/app/services/platform_capabilities.py`（新增统一模块服务）
- `backend/app/schemas/platform_capabilities.py`（新增状态模型）
- `backend/app/api/platform_capabilities.py`（新增能力读写、状态和审计接口）
- `backend/app/api/rate_limit.py`（仅保留现有设置接口和 `ai_enabled` 兼容委托）
- `backend/app/worker/runtime.py`（刷新 worker 能力快照）
- `backend/app/worker/command.py`（应用 AI 和通道门禁）
- `backend/app/worker/plugins/loader.py`（按能力和通道裁剪入口）
- `backend/app/worker/plugins/manifest.py`（扩展插件声明）
- `backend/app/schemas/feature.py`
- `backend/app/services/feature_service.py`
- `backend/app/services/account_bot_runtime.py`
- `backend/app/api/webhooks.py`
- `backend/app/api/ledger.py`
- `backend/app/api/dispatch_debug.py`
- `backend/app/services/event_trace.py`
- `backend/app/services/system_agent/tools/interaction.py`
- `backend/app/services/system_agent/tools/ledger.py`
- `backend/app/main.py`

## 8. 前端改造范围

- `frontend/src/api/types.ts`
- `frontend/src/api/system.ts`
- `frontend/src/components/layout/Sidebar.tsx`
- `frontend/src/lib/navigation.ts`
- `frontend/src/App.tsx`
- `frontend/src/components/layout/AppShell.tsx`
- `frontend/src/pages/Settings/Index.tsx`
- `frontend/src/pages/AI/Index.tsx`
- `frontend/src/pages/Interaction/Index.tsx`
- `frontend/src/pages/Plugins/Home.tsx`

交互要求：

- 系统设置新增“平台能力”区域，显示目标状态和运行时状态。
- 把现有 `navForAIState`、`mobilePrimaryNavForAIState` 和相关辅助函数泛化成基于 capabilities 集合的统一过滤，不新建第二套导航清单。
- 侧边栏与移动端底部导航使用同一个能力过滤结果。
- 直达已关闭页面时显示“模块已暂停”，而不是白屏或错误页。
- 页面 URL 保留，确保深链接和历史书签稳定。
- 插件中心显示“正常、部分可用、已暂停、等待热加载”等状态。
- 关闭交互但插件支持 userbot 时，明确显示“userbot 入口仍可用”。

## 9. 分阶段实施

### 阶段一：统一能力基础和 AI 迁移

- 新增模块服务、能力 API、进程内缓存和基于现有 `CMD_RELOAD_CONFIG` 的 worker reload。
- 将现有 `ai_enabled` 接入统一状态机。
- 复用已有 `_require_ai_enabled`、`CommandContext.ai_enabled` 和 `ctx.ai` 门禁，不重复实现端点级判断。
- 保证 AI 关闭后 Provider、`ctx.ai` 和 AI 入口行为不回归。
- 把现有单 AI 导航过滤泛化为统一 capabilities 过滤，并完成 AI 文档更新。

### 阶段二：Interaction Bot 通道

- 接入交互 Bot manager 启停。
- 明确只停止交互 Bot manager 与测试 Bot，保留管理 Bot、userbot 和核心 Event Bus。
- 增加入口级通道裁剪和插件降级状态。
- 覆盖会话、按钮、测试 Bot、派奖和补偿边界；冻结 drain 按实际任务存在性判断，不依赖可能错误的派奖状态字段。
- 不在本阶段顺带修复既有交互业务或 payout 计算问题；冻结层把业务状态视为不可信输入。若发现会阻塞资金安全验收的问题，单独记录并用独立提交处理。

### 阶段三：入站 Webhook

- 增加 Webhook 模块热开关。
- 实现进程内缓存驱动的最前层 fail-closed，关闭路径不访问 DB。
- 覆盖外部请求、worker 内部命令、配置保留和重新开启。

### 阶段四：资金台账与命中调试

- 台账只冻结查询和操作面，保持主账与补偿链路。
- 命中调试停止模拟、debug trace 和调试任务，保留基础诊断。
- 更新 System Agent 工具和前端状态。

每个阶段都能独立运行、独立测试和独立回滚。实施应在独立功能分支完成，不与现有前端优化未提交改动混合。

## 10. 开发文档同步要求

新增：

- `docs/PLATFORM-CAPABILITIES.md`

更新：

- `docs/TELEPILOT-ARCHITECTURE.md`
- `docs/PLUGIN-OVERVIEW.md`
- `docs/PLUGIN-RULES.md`
- `docs/PLUGIN-SAFETY.md`
- `docs/PLUGIN-API-REFERENCE.md`
- `docs/PLUGIN-DEV-GUIDE.md`
- `docs/PLUGIN-AI.md`
- `docs/PLUGIN-WEBHOOK-QUICKSTART.md`
- `examples/plugins/with_ai/README.md`
- `examples/plugins/with_interaction/README.md`
- `examples/plugins/event_bus_demo/README.md`
- `examples/plugins/webhook_receiver/README.md`
- `README.md`
- `CHANGELOG.md` 的 `Unreleased`

文档必须写清：

- 平台内核、模块、通道、插件的边界。
- desired state 与 generation 的持久化位置，以及 runtime state、ACK 和心跳聚合的内存边界。
- 平台能力进程内缓存、启动预加载、刷新和 fail-closed 语义。
- 能力热加载复用 `CMD_RELOAD_CONFIG` 与现有周期 reconcile，不存在独立能力 IPC。
- AI 与 Interaction Bot 的关闭语义。
- 管理 Bot、userbot 不受 Interaction Bot 模块关闭影响。
- 台账关闭不停止 ActionEvent 和补偿。
- 旧插件兼容规则。
- 模块状态机、热加载失败和周期收敛机制。
- 插件入口如何声明通道和平台能力依赖。

本计划阶段不 bump 正式版本；实现并准备发布时，按项目规则同步四处版本文件和中文更新日志。

## 11. 验证标准

后端：

```bash
cd backend && . .venv/bin/activate && pytest \
  app/tests/test_platform_capabilities.py \
  app/tests/test_system_settings.py \
  app/tests/test_account_bot.py \
  app/tests/test_plugin_loader.py \
  app/tests/test_webhooks.py \
  app/tests/test_ledger_service.py \
  app/tests/test_dispatch_debug.py

cd backend && . .venv/bin/activate && ruff check app
```

前端与插件：

```bash
pnpm --dir frontend typecheck
pnpm --dir frontend build
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
git diff --check
```

人工验收：

1. AI 关闭后，AI Provider 不加载，纯 userbot 插件仍可工作。
2. Interaction Bot 关闭后，polling task 数量归零，userbot 入口继续工作，管理 Bot polling 保持运行。
3. Webhook 关闭后，公开 URL 立即返回 404，配置仍保留。
4. 台账关闭后 API 和页面不可用，但新 ActionEvent 与补偿仍产生。
5. 命中调试关闭后模拟接口不可用，普通日志和基础 trace 正常。
6. 连续关闭、开启 10 次不产生重复 polling task。
7. `CMD_RELOAD_CONFIG` ACK 丢失后，worker 重启或约 180 秒周期 reconcile 能恢复正确状态。
8. 关闭期间的进行中派奖不会丢失，补偿状态可追踪。
9. 桌面端、移动端导航和直达 URL 状态一致。

需要新增或扩展的自动化测试：

- 平台能力服务的默认值、进程内缓存、幂等切换、并发切换、失败状态和 generation 测试。
- 新能力 API 的状态聚合、审计记录，以及系统设置 `ai_enabled` 兼容委托测试。
- 现有 `CMD_RELOAD_CONFIG` 的平台能力 payload、ACK、启动加载和周期 reconcile 测试。
- AI 关闭时 Provider/`ctx.ai` 卸载及混合插件降级测试。
- Interaction Bot manager 停止后不影响管理 Bot 与 userbot 的回归测试。
- 交互关闭期间会话过期、进行中结算和补偿继续运行的测试；drain 必须根据真实任务存在性工作，即使业务状态字段不可靠也不能误杀。
- Webhook 关闭时仅访问进程内缓存，并在 Token、限流、账号查询、配置查询和 body 读取之前返回 404 的测试。
- 台账关闭期间 ActionEvent 和补偿仍写入、重开后可查询的测试。
- 命中调试关闭时普通日志与基础 trace 不受影响的测试。
- 前端导航过滤、直达页面和插件降级状态的组件测试。

## 12. 风险、前提与回滚

关键前提：资金主账、审计、结算补偿和基础 userbot 是 TelePilot 核心内核，不能被可选模块关闭。若要求关闭台账时连 ActionEvent 写入也停止，需要另行设计数据安全方案，不能直接套用普通模块开关。

主要风险：

- 多进程切换存在 ACK 超时，必须保留 generation 和周期 reconcile。
- runtime state 只能代表当前进程观测结果，重启后必须重新从 `starting` 收敛，不能持久化伪造的 `ready`。
- Webhook 能力缓存初始化失败必须 fail-closed，否则会重新暴露公开入口。
- 交互关闭时不能误杀派奖、结算和补偿任务。
- 冻结逻辑不得依赖现有派奖状态一定正确，只能依据实际任务、队列和补偿记录进行 drain。
- Interaction Bot 关闭不能停止 userbot 使用的公共 Event Bus、会话协议和 Action 层。
- 旧插件未声明新字段时需要维持兼容，并通过运行时错误和插件状态说明不可用原因。
- 关闭后到达的旧 generation 任务必须在副作用执行前再次检查模块状态。

回滚规则：

- 回滚不删除配置、Token、规则、会话记录或资金数据。
- 若回滚到不认识新开关的旧版本，旧代码会忽略这些设置并恢复旧行为。
- 正式回滚前先把新增模块开关恢复为 `true`，确认 worker 收敛后再切回旧版本。
- 每个实施阶段独立提交，出现问题时只回滚对应阶段。

## 13. 完成定义

只有同时满足以下条件才视为计划完成：

- 五个模块都能在不重启服务的情况下关闭和重新启动。
- 插件按入口和通道降级，不把多通道插件整体误停。
- userbot、资金审计、结算和补偿链路没有行为回归。
- 前端导航、页面门禁、插件状态和后端状态一致。
- 开发文档、示例插件、API 参考和 `CHANGELOG.md` 已与真实实现同步。
- 类型检查、构建、后端测试、插件示例校验和桌面/移动端人工验收全部通过。
