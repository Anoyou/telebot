# TelePilot System Agent 实施计划（历史对照稿）

状态：历史对照稿。最终方向已合并到 `Agent-Plan.md`；后续实施以 `Agent-Plan.md` 为唯一执行真相源。

目标分支：`codex/system-agent`

计划性质：平台级新能力，按阶段独立合并；本文件不代表功能已经实现。

## 1. 结论

TelePilot 应建设一个平台级 `System Agent`，让用户通过 Web/PWA 和绑定的管理 Bot 使用自然语言查询、解释和调配项目已有能力。

“可以调配整个系统”不等于把数据库、Shell 或所有 HTTP 接口直接交给模型。正确边界是：每个业务域通过显式注册的系统工具接入，AI 负责理解意图、读取上下文和生成操作草稿，业务服务负责校验，用户负责确认，确定性执行器负责最终写入。

首批完整覆盖以下高频场景：

- 查询、解释和启停交互规则。
- 查询、创建、修改和启停定时任务。
- 查询最近日志、错误链路和消息命中情况。
- 查询资金台账的今日收入、支出、净额和流水。
- 添加并验证模型提供商。
- 创建自定义指令并在指定账号启用。
- 查询账号、Worker、功能、插件、Provider 和指令的当前状态。

后续业务域沿用同一工具契约逐项接入，不另建第二套 Agent 框架。

## 2. 当前仓库基础

现有代码已经提供大部分底座：

- `backend/app/services/llm_agent.py`：Provider 无关的有界 Agent 循环，已有轮数、工具数、重复调用、Token 和超时限制。
- `backend/app/services/llm_agent_observability.py`：Agent 工具调用的 ActionEvent 和 span 观测适配器。
- `backend/app/worker/scheduler_runtime.py`：平台常驻 Scheduler，支持 `cron`、`once`、`interval` 以及发送消息、执行白名单指令和调用 LLM。
- `backend/app/services/account_bot_runtime.py`：管理 Bot 的授权用户、角色、命令、按钮和二次确认入口。
- `backend/app/services/account_bot_service.py`：交互规则归一化、敏感 Bot Token 加密和交互配置保存。
- `backend/app/services/command_service.py`：自定义指令和 Provider 的业务层、名称冲突校验、加密与 Worker reload。
- `backend/app/api/logs.py`：审计日志、运行日志、事件 Trace、消息漏斗和系统控制台日志查询。
- `backend/app/services/ledger_service.py`：资金流水、收入、支出、净额、群组和接收者汇总。

System Agent 必须调用这些业务语义或从现有 API 中抽出的共享 service，不能复制表单逻辑，也不能从模型工具中直接提交整份数据库对象。

## 3. 产品体验

### 3.1 Web/PWA

新增一级入口 `/assistant`，作为实际工作台，而不是悬浮聊天气泡。

页面结构：

- 顶部：当前作用域，包含全局或指定账号。
- 主区：自然语言会话和执行状态。
- 操作预览：以内联结构展示目标、当前值、变更值、影响范围、风险和回滚方式。
- 确认区：确认、拒绝、修改参数；高风险操作使用更强确认。
- 最近操作：展示当前会话产生的待确认、成功、失败和自动恢复任务。

桌面采用会话区加操作预览区；移动端改为单列，不嵌套卡片，不允许按钮和长文本重叠。

### 3.2 管理 Bot

在现有 Account Bot runtime 中增加：

- `/agent <自然语言需求>`：发起或继续 Agent 会话。
- `/agent cancel`：清除当前会话。
- 主菜单增加“系统助手”按钮。
- 私聊中进入 Agent 会话后，30 分钟内的普通文本继续交给 Agent；现有 `/status`、`/rules` 等斜杠命令始终优先。
- 写操作通过 Telegram Inline 按钮确认，按钮必须绑定账号、授权用户、Action ID 和过期时间。

管理 Bot 只处理账号作用域能力。Provider、系统更新、全局配置和任何需要输入密钥的操作只能生成草稿，并跳转到登录后的 Web 页面完成。

## 4. 核心架构

```text
Web / PWA                         Account Management Bot
    |                                      |
    +-------------- Channel Adapters ------+
                           |
                  SystemAgentRuntime
                           |
                 SystemToolRegistry
                           |
         +-----------------+-----------------+
         |                                   |
   Read Tool Handlers                Mutation Planners
         |                                   |
   Domain Query Services            AgentActionRequest
                                             |
                                  用户确认 / 权限复核
                                             |
                                   Deterministic Executors
                                             |
     Interaction / Scheduler / Commands / Providers / Features / Logs / Ledger
```

依赖方向必须保持单向：渠道适配器依赖 Agent Runtime，Agent Runtime 依赖工具注册表，工具依赖业务服务；业务服务不得反向依赖 Web 或 Bot。

## 5. Agent Runtime

新增 `SystemAgentRuntime`，复用现有结构化 LLM Runtime 和 Provider fallback。

默认限制：

- 最大 Agent 轮数：8。
- 最大工具调用：24。
- 单轮最大工具调用：8。
- 相同工具和参数最多重复：3 次。
- 单次总 Token：16384。
- 总超时：180 秒。
- 同一操作者在同一作用域同时只允许一个运行中的 Agent Run。

系统设置新增 `system_agent`：

- `enabled`：默认 `false`，与现有 `ai_enabled` 同时为真才可运行。
- `provider_id`：运行 System Agent 的固定 Provider。
- `model`：可选固定模型；留空时使用 Provider 的默认且支持 tools 的模型。

如果没有已启用且真实可用的 tools 模型，入口只显示配置要求，不得降级为从自然语言正则猜参数后直接写系统。

## 6. 系统工具契约

每个工具必须声明：

- `name`：稳定、唯一的工具名。
- `description`：只描述事实和边界，不诱导模型扩大权限。
- `input_schema`：严格 object JSON Schema。
- `scope`：`global` 或 `account`。
- `effect`：`read`、`reversible_write`、`high_risk_write`、`forbidden`。
- `required_role`：Web 当前用户或管理 Bot 的 `viewer/operator/admin`。
- `requires_confirmation`：所有写操作固定为真。
- `sensitive_fields`：不得进入模型、会话、Action JSON 和日志的字段。
- `prepare`：读取当前状态、校验参数、生成差异预览和配置指纹。
- `execute`：确认后重新读取、重新鉴权、校验指纹并调用业务服务。
- `rollback`：可逆操作的恢复方法；没有可靠恢复方法时必须归类为高风险。

模型永远拿不到任意 URL、任意 SQL、任意 Python 导入或任意 Shell 工具。

## 7. 风险和确认模型

### 7.1 只读

自动执行，返回脱敏结果。典型操作包括规则查询、日志查询、台账汇总、状态读取和配置解释。

### 7.2 可逆写

必须展示差异并单次确认。包括启停规则、创建定时任务、创建模板、启用账号功能等。

### 7.3 高风险写

首版默认不向 Agent 开放。后续开放时必须满足 Web-only、Admin、二次确认和完整审计。包括删除、插件安装或更新、代理和 Bot Token 修改、系统重启、配置导入、台账核销或清空。

### 7.4 永久禁止

- 读取或回显任何 API Key、Bot Token、Authorization、代理凭据或 Session。
- 任意 Shell、SQL、文件写入和宿主机命令。
- 绕过业务 service 直接修改 ORM 对象。
- 在凭据校验前创建持久化配置。
- 由 Agent 自主确认自己提出的写操作。
- 让普通管理 Bot 用户执行全局配置或跨账号提权。

## 8. 数据模型

新增 `AgentActionRequest` 表，使用 UUID Action ID：

- `thread_id`：Redis 会话标识。
- `channel`：`web` 或 `account_bot`。
- `actor_type`、`actor_id`：Web 用户或 Telegram 授权用户。
- `account_id`：账号作用域操作必填，全局操作为空。
- `tool_name`、`effect`。
- `input_json`：已规范化且不含敏感字段的参数。
- `preview_json`：当前值、目标值、影响范围和提示。
- `config_fingerprint`：生成草稿时的业务状态指纹。
- `idempotency_key`：唯一，阻止双击、网络重试和重复 Telegram Update 重复执行。
- `status`：`pending`、`executing`、`scheduled`、`succeeded`、`failed`、`rejected`、`expired`、`cancelled`、`needs_attention`。
- `execute_at`：延迟执行或自动恢复时间。
- `restore_tool_name`、`restore_input_json`：临时修改的恢复动作。
- `result_json`、`error_code`、`error_message`。
- `expires_at`、`created_at`、`updated_at`、`executed_at`。

会话消息只存 Redis，TTL 30 分钟。首版不保存长期聊天记录，避免用户误发密钥后形成永久副本。Action 记录和业务审计持久化。

## 9. 并发、幂等和状态漂移

- 确认时使用数据库行锁，把 `pending` 原子变为 `executing`；其它请求返回原执行结果。
- 执行器必须重新读取目标对象并比较 `config_fingerprint`。
- 目标在草稿后被 Web、Bot 或其它 Agent 修改时，不自动覆盖，Action 标记 `needs_attention` 并要求重新生成预览。
- 业务写入和 Action 成功状态尽量处于同一数据库事务；Worker reload、Bot restart 等事务外动作失败时返回“配置已保存但运行时未确认”，不能伪报完全成功。
- Redis 不可用时停止新会话和确认操作；数据库不可用时所有写操作 fail-closed。

## 10. 能力矩阵

### 10.1 系统和账号

首批只读：

- `system.get_context`：时区、指令前缀、AI 和 Agent 开关。
- `system.get_health`：就绪状态和关键组件状态。
- `account.list`、`account.get_status`：账号、Worker、启用功能和最近错误。
- `account.list_recent_peers`：最近会话及 Worker 是否在线。

后续可逆写：

- `account.pause`、`account.resume`。
- `feature.list`、`feature.set_enabled`。

首版禁止账号删除、登录验证码、2FA、Session、设备指纹和全局 kill switch 写入。

### 10.2 交互规则

首批工具：

- `interaction.list_rules(account_id)`：列出规则 ID、名称、启用状态、群范围、动作和模块入口。
- `interaction.get_rule(account_id, rule_id)`：读取一条规则的完整脱敏配置。
- `interaction.explain_triggers(account_id, rule_id)`：规范化展示 `trigger_mode`、`trigger_texts`、`module_start_keywords`、`open_commands`、`close_commands`、`status_commands`、金额和收款人条件。
- `interaction.set_rule_enabled(account_id, rule_id, enabled)`：只修改目标规则的 `enabled`，不提交模型提供的整份交互配置。
- `interaction.get_runtime_status(account_id)`：查看 Bot runtime、最近错误、DLQ 数和群成员状态。

规则实际存储在账号级 `SystemSetting` 交互配置中，不是通用 `Rule` 表。实现时新增 `interaction_rule_service`：加载现有配置、按稳定规则 ID 定位、仅 patch 目标字段、复用 `AccountBotInteractionConfig` 和现有归一化逻辑、保存、通知 Worker reload 并重启 Interaction Bot runtime。

禁用语义必须在预览和回执中明确：`enabled=false` 会阻止该规则的新触发，但现有活跃会话仍可能继续处理和完成过期清理；禁用不等于强制结束当前会话。

“暂时禁用”处理规则：

- 未说明时长时，Agent 必须追问“直到手动恢复”还是给出恢复时间。
- “直到手动恢复”只创建一次禁用 Action。
- 指定时长或恢复时间时，同时创建持久化 `scheduled` 恢复 Action。
- 恢复动作只把同一稳定规则 ID 的 `enabled` 改回 `true`，不覆盖期间发生的其它规则字段修改。
- 规则已删除时，恢复 Action 标记 `cancelled`；配置发生无法安全判断的结构变化时标记 `needs_attention` 并通知用户。

### 10.3 定时任务

首批工具：

- `scheduler.list_rules`、`scheduler.get_rule`。
- `scheduler.create_rule`、`scheduler.update_rule`、`scheduler.set_enabled`。
- `scheduler.explain_schedule`：按系统时区解释 Cron、单次和间隔任务，并返回下次执行时间。
- `scheduler.execute_now`：高风险级别的即时执行，必须单独确认。

创建和修改继续生成标准 `Rule(feature_key="scheduler")`。Agent 不能绕过现有 `run_command` 白名单，`call_llm` 必须引用真实 Provider。

### 10.4 日志和诊断

首批工具：

- `logs.query_runtime`：按账号、级别、来源、插件、关键字、起始时间和数量查询，数量限制 1 到 500。
- `logs.query_audit`：查询 Web 操作日志。
- `logs.query_messages`：查询一页式消息漏斗和卡住阶段。
- `logs.query_traces`、`logs.get_trace_detail`：查询完整事件、span、action 和关联运行日志。
- `logs.query_system_console`：Web-only，只读，使用现有 updater sidecar 或本地日志回退。
- `logs.summarize`：只在已查询且脱敏的数据上做归纳，不把未受限的日志文件直接塞入模型。

“帮我看最近多少条日志”缺少数量或类型时，默认读取最近 20 条运行日志，并在结果中标明账号范围、日志类型和实际返回数；用户明确数量时按 1 到 500 限制。

任何日志工具都必须继续经过 `redact_text` 和 `redact_value`。关键字查询不得进入 access log；系统控制台关键字继续使用请求体。

### 10.5 资金台账

首批只读工具：

- `ledger.get_summary`：收入、支出、净额、笔数。
- `ledger.list_entries`：按账号、群、插件、方向、金额、状态和时间查询。
- `ledger.get_stats`：开局数、参与人数、派奖成功率和台账指标。
- `ledger.list_compensations`：查询待补付和异常补偿，不执行核销。

“今日”必须按系统设置的时区计算本地零点和下一日零点，再转换为 UTC `since/until`，不能直接使用数据库 UTC 日期。回答必须同时给出统计区间、收入、支出、净额和笔数，避免把入账误解为利润。

首版禁止 `manual-paid` 和 `reset ledger`。这两类操作即使后续开放，也必须 Web-only、Admin、强确认和单独权限开关。

### 10.6 模型提供商

首批工具：

- `provider.list_safe`、`provider.get_safe`：只返回脱敏元数据和 `has_api_key`。
- `provider.prepare_create`：生成名称、协议、Base URL、模型、路由标签和代理选择草稿。
- `provider.discover_models`、`provider.verify_model`：复用当前预览发现与真实验证路径。
- `provider.create_verified`：仅 Web 确认后执行。

API Key 使用专用 password input 直接提交后端，绝不进入 Agent 消息、Action JSON、Telegram 或日志。验证结果产生短期 `verification_grant`，绑定 Provider 配置指纹和 API Key 哈希；保存时重新比较，成功后立即使用现有 Fernet 加密落入 `api_key_enc`。

管理 Bot 只能生成 Provider 草稿和登录后 Web 深链。无现有可用 Provider 时，首次 Provider 必须通过确定性配置向导完成，Agent 不能配置启动自己的第一组模型。

### 10.7 自定义指令

首批工具：

- `command.list_templates`、`command.get_template`、`command.list_builtin`。
- `command.prepare_create`：支持 `reply_text`、`forward_to`、`ai`。
- `command.create_and_enable`：创建全局模板，并在明确选择的账号建立启用关系。
- `command.set_enabled_for_accounts`：启停已有模板的账号关系。
- `command.explain`：解释触发名、别名、Provider、模式、输出格式和影响账号。

执行前必须复用 `CommandTemplateBase`、内置命令保留字和跨模板别名冲突校验。创建模板和账号启用关系放在一个事务中，成功后通知相关 Worker reload。

当前 `run_plugin` 仍属于未完整实现的占位类型，System Agent 不提供创建入口。

### 10.8 插件、功能和其它业务域

第二批只读覆盖：

- 已安装插件、版本、启用状态、签名状态、最后错误和可更新状态。
- Feature Matrix、账号功能配置摘要和配置 Schema。
- Webhook 状态、速率限制使用、LLM 用量、通知 Bot 摘要、代理摘要、消息模板目录和渲染预览。

第二批可逆写覆盖：

- 启停已安装且可信的插件或账号功能。
- 基于插件 `config_schema` 生成配置草稿，并调用现有 schema 校验。
- 创建或修改速率限制、自动回复和转发规则。
- 渲染和测试发送消息模板；测试发送必须再次确认目标会话。

插件安装、更新、卸载，Webhook Token 重置，代理凭据，通知 Bot Token，系统更新、重启、配置导入和导出不进入默认 Agent 工具集。

## 11. 权限

Web 当前用户沿用现有 Web 认证模型；所有写操作仍记录 `AuditLog.user_id`。

管理 Bot 角色：

- `viewer`：只读查询和解释。
- `operator`：账号作用域的可逆写操作，例如启停规则、创建 Scheduler、启停功能。
- `admin`：账号作用域的高风险候选操作；仍受独立工具开关和强确认约束。

管理 Bot 的角色不能提升为 Web 全局管理员。一个账号的 Bot 用户不能查询或修改其它账号。

## 12. API 契约

新增：

- `POST /api/system-agent/sessions`：创建 Web 会话。
- `DELETE /api/system-agent/sessions/{session_id}`：清除 Redis 会话。
- `POST /api/system-agent/sessions/{session_id}/messages/stream`：SSE 返回运行、工具、草稿、回答和错误事件。
- `GET /api/system-agent/actions`：查询当前用户可见的 Agent Actions。
- `GET /api/system-agent/actions/{action_id}`：查看草稿、状态和结果。
- `POST /api/system-agent/actions/{action_id}/confirm`：确认并执行。
- `POST /api/system-agent/actions/{action_id}/reject`：拒绝。
- `POST /api/system-agent/actions/{action_id}/cancel-scheduled`：取消尚未执行的自动恢复或延迟 Action。

SSE 事件固定为：`run_started`、`tool_started`、`tool_finished`、`action_proposed`、`assistant_message`、`error`、`done`。事件只包含脱敏摘要。

## 13. 后端文件规划

新增：

- `backend/app/api/system_agent.py`
- `backend/app/schemas/system_agent.py`
- `backend/app/db/models/system_agent.py`
- `backend/app/services/system_agent.py`
- `backend/app/services/system_agent_registry.py`
- `backend/app/services/system_agent_approval.py`
- `backend/app/services/system_agent_deferred.py`
- `backend/app/services/system_agent_tools/`，按 `system`、`accounts`、`interaction`、`scheduler`、`logs`、`ledger`、`providers`、`commands` 分模块。
- `backend/app/services/interaction_rule_service.py`
- 一条仅新增 Agent Action 表的 Alembic 迁移。

修改：

- `backend/app/main.py`：注册 API 和持久化延迟 Action runner；runner 启停进入组件状态。
- `backend/app/services/account_bot_runtime.py`：增加管理 Bot Agent 渠道适配器，不新增 polling 消费者。
- `backend/app/api/rules.py`：把 Scheduler 规范化和写入逻辑下沉为可复用 service。
- `backend/app/services/command_service.py`：增加原子创建模板并启用账号的编排入口。
- `backend/app/api/rate_limit.py`：读写 `system_agent` 设置。
- 日志、审计、重加密和系统健康相关文件按新增数据模型补覆盖。

## 14. 前端文件规划

新增：

- `frontend/src/pages/Assistant/Index.tsx`
- `frontend/src/api/systemAgent.ts`
- `frontend/src/components/assistant/Conversation.tsx`
- `frontend/src/components/assistant/ActionPreview.tsx`
- `frontend/src/components/assistant/SecureFields.tsx`
- `frontend/src/components/assistant/RecentActions.tsx`

修改：

- `frontend/src/App.tsx`
- `frontend/src/components/layout/Sidebar.tsx`
- `frontend/src/components/layout/AppShell.tsx`
- `frontend/src/api/types.ts`
- 系统设置页增加 Agent 开关和执行模型选择。

所有操作预览必须使用稳定布局，显示 loading、empty、error、pending、executing、succeeded、failed、expired 和 needs-attention 状态。敏感输入不得回填、预览或进入浏览器缓存数据结构之外的持久状态。

## 15. 分阶段交付

### 阶段 1：核心 Runtime、Web、只读工作台

交付：System Agent Runtime、注册表、Redis 会话、Action 表、Web 页面，以及系统/账号/交互/日志/台账的只读工具。

可独立价值：可以询问交互有哪些规则、规则触发方式、最近日志、今日收入、账号状态和错误原因。

预计：7 到 10 个工程日。

### 阶段 2：交互与 Scheduler 可逆写

交付：交互规则单字段启停、临时禁用和自动恢复、Scheduler 创建/修改/启停、审批、幂等、状态漂移保护、管理 Bot `/agent`。

可独立价值：Web 和管理 Bot 均可安全完成最常见的日常运维。

预计：7 到 10 个工程日。

### 阶段 3：Provider 和自定义指令

交付：Provider 安全草稿、密钥隔离、发现和验证、指令创建、预览和账号启用。

可独立价值：自然语言可以完成 AI 能力配置，但敏感凭据仍保持确定性输入链路。

预计：7 到 10 个工程日。

### 阶段 4：插件、功能和规则生态扩展

交付：Feature、可信插件、自动回复、转发、速率限制、模板等工具；高风险工具仍保持默认关闭。

可独立价值：System Agent 从固定功能助手扩展为 TelePilot 的统一操作平面。

预计：按业务域每组 3 到 5 个工程日，不阻塞前三阶段发布。

阶段 1 到 3 预计涉及 35 到 50 个文件、多个新 service 和一条增量迁移。每个阶段结束时系统都处于可用状态，不依赖下一阶段才能工作。

## 16. 测试计划

### 单元测试

- 工具注册表只暴露显式工具，角色和作用域过滤正确。
- Read 工具不能返回敏感字段。
- Mutation 工具只能创建草稿，不能直接写业务表。
- Action 状态机、过期、拒绝、双击确认和幂等。
- 配置指纹变化后阻止覆盖。
- 系统时区下“今日、本周、明天上午九点”的边界，包括 DST 时区。
- 交互规则 ID 定位、触发解释、单字段启停和不覆盖其它规则。
- 禁用规则不再接受新触发，但已有 session 仍可按现有语义处理和清理。
- 自动恢复只修改 `enabled`，规则删除和结构变化可安全降级。
- 日志 limit 1 到 500、脱敏、关键字和来源过滤。
- 台账收入、支出、净额和本地日界线计算。
- Provider API Key 不进入消息、Action、Audit、RuntimeLog 和异常文本。
- 自定义指令名称、别名、Provider 和账号启用事务回滚。

### 集成测试

- Web 会话到只读回答、操作草稿、确认和回执完整链路。
- SSE 中断后重连能读取 Action 最终状态，不重复执行。
- 管理 Bot viewer/operator/admin 权限矩阵。
- Telegram 重复 Update 和重复按钮只产生一次业务动作。
- Redis、Provider、数据库、Worker reload 和 Bot restart 分别失败时的 fail-closed 或部分成功语义。
- Agent Provider fallback 仍满足 tools 支持条件。

### 手工和真实环境验收

- Web：查询交互规则，解释开启、关闭、状态和玩法触发词。
- Web：禁用规则并确认现有会话语义，随后恢复。
- Bot：创建定时消息并在现有 Scheduler 页面查看、修改和手动执行。
- Web：查询最近 20 条错误日志并打开一条 Trace。
- Web 和 Bot：查询上海时区今日资金收入、支出、净额和笔数，结果与资金台账页面一致。
- Web：添加 Provider，确认 API Key 不出现在浏览器响应、后端日志和 Agent 记录中。
- Web：创建 AI 自定义指令，指定账号启用，在真实 Telegram 中触发。
- 桌面和移动视口截图检查无重叠、无横向滚动、长中文和错误消息不撑破布局。

推荐验证命令：

```bash
backend/.venv/bin/ruff check backend/app
backend/.venv/bin/pytest backend/app/tests
pnpm --dir frontend typecheck
pnpm --dir frontend build
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
git diff --check
```

静态和本地测试不能替代真实管理 Bot、Interaction Bot、Telegram 消息和 Provider E2E。

## 17. 依赖和启动前提

- PostgreSQL：Agent Action、审批和延迟恢复持久化。
- Redis：短期会话、并发锁和渠道上下文；不可用时 Agent 写操作停止。
- 一个已验证、支持 tools 的现有 LLM Provider：负责运行 System Agent。
- 管理 Bot Token：只在需要 Bot 渠道时使用，继续由现有加密字段保存。
- 新 Provider 的 API Key：仅在 Web 安全输入中提供。

不新增外部 Agent 服务、MCP Server、CLI、消息队列或第二种编程语言。

## 18. 失败、扩容和回滚

外部 Provider 不可用时：保留已生成 Action 状态，不执行未确认写入；回答明确说明没有业务变更。

Redis 不可用时：禁止新 Agent 会话、确认和管理 Bot Agent 路由，普通 TelePilot 功能继续运行。

10 倍会话量下：先受单用户并发锁、全局 Agent 并发上限、LLM 预算和 Provider 限速保护；日志和台账工具必须限制返回数量，禁止无界查询。

回滚：关闭 `system_agent.enabled` 即停止入口和新执行；Agent Action 表保留审计，已创建的 Scheduler、Provider、指令和规则仍是标准业务对象，不依赖 Agent 才能运行。自动恢复 runner 在总开关关闭后仍只完成已经确认的恢复 Action；系统设置提供单独的 `cancel_scheduled_actions` 管理入口。

数据库迁移只新增表，不重写现有数据。代码回滚时可保留新表，不要求立即 downgrade。

## 19. 分支和发布策略

当前 `main` 工作区存在未提交的 Provider、Quick Verify 和指令编辑改动，与阶段 3 重叠。开始实现前应先把这批改动整理成明确提交，或者确认它们将在阶段 3 前以 commit 形式合入。

从干净且最新的 `main` 创建独立 worktree 和 `codex/system-agent` 分支。每完成一个阶段合入一次最新 `origin/main`，解决冲突并运行该阶段全量验证；禁止用整文件 ours/theirs 覆盖 Provider、命令或 Interaction 配置文件。

开发期间写入 `CHANGELOG.md` 的 `Unreleased`。准备 PR 或稳定发布时按用户可感知的新能力使用 `MINOR`，同步更新：

- `backend/app/__init__.py`
- `backend/pyproject.toml`
- `frontend/package.json`
- `frontend/src/lib/version.ts`

更新日志、commit、PR 和 release 文案全部使用中文。

## 20. 成功标准

- 用户不需要知道 Rule JSON、Cron、Provider Schema 或日志过滤参数，也能完成高频查询和配置。
- 所有写操作都有可理解的差异预览、明确确认、幂等执行和结果回执。
- Agent 不能看到或泄露密钥，不能绕过现有权限和业务校验。
- Web 和管理 Bot 对同一请求使用相同业务语义和审计格式。
- Agent 创建或修改的对象能继续在现有 TelePilot 页面查看、编辑、执行和停用。
- 交互规则、日志和台账回答与各自权威 service 的结果一致，不由模型自行计算或猜测。

## 21. 明确不做

- 首版不做长期会话记忆、跨用户共享记忆和自主学习用户密钥。
- 不让 Interaction Bot 在公共群里接受系统管理自然语言；只使用授权管理 Bot 私聊。
- 不开放任意 HTTP、Shell、SQL、文件系统和源码修改工具。
- 不允许 Agent 自主安装插件、更新系统、删除账号、清空台账或执行资金动作。
- 不用 Agent 替代现有页面；现有页面仍是最终人工编辑、核对和恢复入口。

## 22. 最脆弱前提

本计划假设至少存在一个稳定、真实支持 tools 的 Provider 来运行 System Agent。如果这个前提不成立，System Agent 无法可靠启动；系统必须保持现有手动 Provider 引导作为 bootstrap 路径，而不是引入不受控的文本解析写入作为降级方案。

## 23. 待确认决策

本计划没有阻塞实施的技术未知项。需要产品确认的是整体方向：接受“全系统逐域工具化、所有写操作先预览再确认、敏感和高风险能力默认不开放”作为 System Agent 的长期边界。
