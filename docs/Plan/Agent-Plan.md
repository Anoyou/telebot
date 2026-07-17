# TelePilot System Agent 轻量实施计划

> 状态：阶段 1 已落地、阶段 2 实施中（分支 `Agent-beta`）。
>
> 版本：v3，按“个人自用、维护最简、隐私可放宽”收敛。
>
> 唯一执行真相源：本文档。
>
> 历史对照稿：`Agent-Plan-claude.md`、`Agent-Plan-codex.md`。两份历史稿只用于追溯取舍，不再指导实现。
>
> 计划基线：2026-07-18，`main` 分支，TelePilot `0.63.0`。实现时以当前分支源码和项目级 `AGENTS.md` 为准；源码发生实质漂移时先更新本文档。

## 1. 最终决策

TelePilot 建设一个平台级 `System Agent`，让个人管理员通过 Web 工作台和绑定的管理 Bot，用自然语言查询、解释和操作系统已有能力。

最终原则：

- AI 负责理解意图、选择工具、补齐参数和解释结构化结果。
- 工具只调用现有业务 service，不给模型任意 SQL、Shell、文件或 HTTP 权限。
- Web 与 Bot 共用同一个 Agent Runtime、工具注册表、Action 执行器和审计语义。
- 会话和消息持久化到数据库，方便回看、排错和跨端继续。
- 写操作生成预览并确认；删除和资金类操作使用醒目的危险确认。
- 数据库事务由 Action 执行器统一管理，工具不得自行创建会话或提交。
- Action ID、数据库行锁和状态机负责防双击及 Telegram 重复 Update；不再增加 `idempotency_key`、fingerprint、`needs_attention` 和刷新预览。
- Web 使用 `/assistant`，管理 Bot 使用 `/agent`，后端使用 `/api/system-agent`。
- 流式响应统一使用 `StreamingResponse + NDJSON`。
- Web 和 Bot 聊天都允许直接粘贴 API Key；用户明确接受内容经过 Telegram（Bot 渠道）并发送给当前上游模型。
- 工具首发控制在约 40 个高频能力；代理、Webhook、配置包、插件安装、系统重启等长尾能力进入 backlog。
- 分四阶段交付；每个可独立使用的阶段单独发布，不等待全部功能一次性完成。

## 2. 目标与完成标准

### 2.1 高频用户场景

- “交互里有哪些规则？”
- “这条规则是怎么触发和停止的？”
- “把这条规则禁用。”
- “创建一个每天上午 9 点发送消息的定时任务。”
- “最近 20 条运行日志里有什么错误？”
- “我今天收入多少？”
- “添加一个模型提供商，Key 是……”
- “创建一个 `/总结` 自定义指令并给账号 A 启用。”
- “账号 A 启用了哪些功能和插件？”
- “暂停或恢复账号 A。”

### 2.2 工程目标

- 新增普通能力时，只需提供或复用一个稳定 service，再注册一个工具。
- Web、Bot 和现有页面不复制业务校验。
- 写入、审计和 Action 状态具备统一提交和回滚语义。
- Agent 创建的 Scheduler、Provider、指令和 Rule 都是标准业务对象，可继续在现有页面维护。
- 关闭 System Agent 后，普通 TelePilot 功能和 Agent 已创建对象继续工作。

### 2.3 完成标准

- Web 与 Bot 都能完成只读查询。
- 核心写操作从自然语言到落库只需一次确认。
- 重复点击、请求重试和 Telegram 重复 Update 不重复执行。
- 新增普通工具不修改 Agent 主循环、流协议或通用确认组件。
- 失败回答明确说明“业务未变化”“数据库已保存但 reload 失败”或“外部结果未知”。

## 3. 明确不建设的内容

首发不建设：

- 任意 SQL、Shell、文件操作和任意 HTTP 请求工具。
- Agent 自行修改 Prompt、工具注册表或权限规则。
- 无确认的自主写操作。
- 跨用户记忆、自动学习凭据或个人画像。
- Agent 配置启动自己的第一组 Provider；首次 Provider 继续走现有页面。
- 企业级策略引擎、审批链、凭据保险库和 verification grant。
- fingerprint、`needs_attention`、`refresh-preview` 和状态漂移框架。
- 独立 `idempotency_key` 和 request nonce；同一 Action 的重复确认由 Action ID、行锁和状态机处理。
- 临时禁用自动恢复 Runner；首版“暂时禁用”就是普通禁用，Agent 明确提示不会自动恢复。
- Bot 本地密钥截获、`awaiting_secret`、`deleteMessage` 和 Web 密钥深链。
- Provider `auto` 路由；首版固定选择一个支持 tools 的 Provider 和模型。
- 事务 outbox、分布式工作流和自动补偿平台。
- 低频长尾工具，详见第 17 节 backlog。

## 4. 已确认的复用点

| 能力 | 当前实现 | Agent 接入方式 |
| --- | --- | --- |
| 有界 LLM 循环 | `backend/app/services/llm_agent.py` | 复用轮数、工具数、重复调用、Token 和超时限制 |
| tools 模型判断 | `ai_facade.py` 的 `_tools_model_for_dto` | 抽出最小共享 helper；首版不重构完整 auto 路由 |
| Provider 与指令 | `backend/app/services/command_service.py` | 复用 Schema、冲突校验、加密和 DTO |
| Provider 验证 | `backend/app/api/commands.py`、前端 `streamQuickVerifyProvider` | 复用真实验证和 NDJSON 消费模式 |
| Scheduler | `Rule(feature_key="scheduler")`、`api/rules.py`、`scheduler_runtime.py` | 抽取实际复用的规范化和 CRUD service |
| 交互规则 | `account_bot_service.py` 的账号级配置 JSON | 单独工具族，不混入通用 Rule 表 |
| 交互会话 | `account_bot_runtime.py` | 查询活跃会话和规则影响 |
| 日志 | 审计、运行日志、ActionEvent | 只返回有限条数和摘要 |
| 台账 | `ledger_service.py` | 复用列表、汇总和补付查询 |
| 时区 | `SystemSetting("timezone")` | “今日”按本地日界线转换 UTC |
| 管理 Bot | `_handle_command`、`_request_confirm` | 增加 `/agent`、普通聊天密钥处理和 Inline 确认 |
| 前端流式 | `frontend/src/api/commands.ts` | 复用 `fetch + getReader` 的 NDJSON 解析方式 |

## 5. 总体架构

```text
Web /assistant                         管理 Bot /agent
      │                                      │
      ├── NDJSON 对话                        ├── 斜杠命令/助手模式
      ├── 内联 Action 卡片                   ├── Inline 确认
      └── 可选密钥补填                       └── 普通聊天可包含密钥
                     │                │
                     ▼                ▼
                SystemAgentService
          ┌──────────┼────────────┐
          │          │            │
          ▼          ▼            ▼
   FixedProvider   AgentLoop   ToolRegistry
                                  │
                    ┌─────────────┴────────────┐
                    ▼                          ▼
               Read Handlers              Action Preview
                                                │
                                                ▼
                                        ActionExecutor
                                                │
                                      单一数据库事务
                                                │
                                       现有业务 Services
                                                │
                                      commit 后 reload/同步
```

禁止反向依赖：

- 业务 service 不依赖 System Agent。
- Tool handler 不调用本项目 Web API。
- 管理 Bot 不实现独立工具和事务逻辑。
- Prompt 不复制业务校验来替代 service。

## 6. 后端模块结构

```text
backend/app/services/system_agent/
├── __init__.py
├── service.py              # 会话、消息和主流程编排
├── runtime.py              # 有界工具调用循环与事件发射
├── registry.py             # ToolSpec 注册与角色/渠道过滤
├── context.py              # ToolContext
├── actions.py              # Action 创建、确认、拒绝、过期
├── executor.py             # 统一事务、行锁和幂等
├── secrets.py              # 密钥抽取、加密暂存、清理
├── redactor.py             # 消息、工具结果和审计基础打码
├── events.py               # NDJSON 事件
├── prompts.py              # 稳定系统概念和行为边界
└── tools/
    ├── __init__.py
    ├── system.py
    ├── accounts.py
    ├── interaction.py
    ├── rules.py
    ├── scheduler.py
    ├── providers.py
    ├── commands.py
    ├── features.py
    ├── logs.py
    └── ledger.py
```

同时新增：

- `backend/app/api/system_agent.py`
- `backend/app/schemas/system_agent.py`
- `backend/app/db/models/system_agent.py`
- 两份阶段迁移：阶段 1 创建会话与消息表，阶段 2 创建 Action 表。
- `backend/app/services/interaction_rule_service.py`
- `backend/app/services/rule_service.py`
- 一个最小 tools 模型选择 helper；不提前抽象完整 Provider 路由平台。

## 7. 数据模型

### 7.1 `SystemAgentSession`

表名：`system_agent_session`。

- `id`：UUID v4 字符串主键。
- `web_user_id`：Web 用户 ID，可空。
- `bot_tg_user_id`：Bot Telegram 用户 ID，可空。
- `account_id`：账号上下文，可空；Bot 会话必须绑定账号。
- `channel`：`web` 或 `bot`。
- `title`：首条用户消息去除换行后截取前 30 个字符，不额外调用 LLM。
- `status`：`active`、`archived`。
- `created_at`、`updated_at`。

### 7.2 `SystemAgentMessage`

表名：`system_agent_message`。

- `id`：自增主键。
- `session_id`：外键；删除会话时级联删除消息。
- `role`：`user`、`assistant`、`tool`、`system_event`。
- `content`：JSON，保存打码后的文本、工具参数摘要和结果摘要。
- `usage`：JSON，保存 Provider、模型和 Token 摘要。
- `created_at`。

持久化规则：

- 默认长期保存，直到用户删除会话或清空历史。
- Web 原始消息先在内存中用于当次模型请求，再执行打码后落库。
- 已从工具参数提取出的 Key 做精确值替换；常见 `api_key/token/authorization/password` 字段做基础打码。
- Bot 原始消息与 Web 一样先用于当次模型请求，再打码后创建 `SystemAgentMessage`。
- 工具结果只保存 UI 与排障需要的有限摘要。

### 7.3 `SystemAgentAction`

表名：`system_agent_action`。

- `id`：UUID v4 字符串主键。
- `session_id`：可空；删除会话时 `ON DELETE SET NULL`，Action 和审计继续保留。
- `account_id`：目标账号，可空。
- `actor_user_id`、`actor_bot_user_id`：执行身份快照。
- `channel`：`web` 或 `bot`。
- `tool_name`：注册工具名。
- `arguments`：规范化后的非敏感参数 JSON。
- `secret_fields`：只保存敏感字段名，例如 `api_key`，不保存值。
- `secret_payload_enc`：Fernet 加密的临时敏感参数 JSON，可空。
- `summary`：用户可读操作摘要。
- `preview`：当前值、目标值、影响对象和警告。
- `risk`：`normal` 或 `dangerous`。
- `status`：`pending`、`executing`、`executed`、`rejected`、`expired`、`failed`。
- `result`：打码后的结果。
- `error_code`、`error_message`。
- `runtime_sync_status`：`not_required`、`pending`、`succeeded`、`failed`。
- `runtime_sync_error`：打码错误摘要。
- `expires_at`：默认 10 分钟。
- `created_at`、`updated_at`、`executed_at`。

清理规则：

- Action 执行、拒绝或确认时发现过期后，立即清空 `secret_payload_enc`。
- 每次新 Agent 请求顺带清理已过期 Action 的密文；应用启动时再清理一次。
- 不新增专门清理 Runner；长期无人使用时最多保留已加密密文，不保留明文。
- `secret_payload_enc` 必须加入 `app.scripts.rekey` 覆盖。

## 8. 会话与上下文

### 8.1 Web

- 支持创建、切换、归档、删除会话和清空历史。
- 页面重开后恢复最后一个 active 会话。
- 从数据库读取最近消息构造模型上下文。
- 按现有 Token 预算滑窗截断；被截断历史仍可在 UI 查看。

### 8.2 Bot

- `/agent`：进入助手模式或显示当前状态。
- `/agent <自然语言>`：直接发起任务。
- `/agent new`：新建会话。
- `/agent exit`：退出助手模式，不删除历史。
- `/agent clear`：确认后删除当前会话。
- 助手模式标记使用 Redis，TTL 30 分钟滚动刷新。
- 既有 `/status`、`/rules` 等斜杠命令始终优先。

## 9. API Key 与敏感输入

### 9.1 Web 与 Bot 普通聊天

用户明确把 Web、Telegram、管理 Bot 和当前上游模型视为可信链路。Web 与 Bot 都允许在普通 Agent 消息中直接粘贴 API Key，不建设 Bot 本地截获或 Web 深链限制。

处理顺序：

1. 原始消息参与当次模型请求；Bot 消息会先经过 Telegram 服务器。
2. 模型可以生成包含 Key 的 Provider 工具调用。
3. 工具层把敏感参数从普通 arguments 移出，Fernet 加密写入 `secret_payload_enc`。
4. Action 的普通 `arguments`、preview 和模型工具结果只显示 `has_api_key=true`。
5. 用户消息、Assistant 回复和工具消息在落库前替换已提取的 Key，并做基础字段打码。
6. 执行、拒绝或过期后清除 Action 中的密文。

用户接受 Key 被 Telegram 和上游模型处理；TelePilot 仍不得把明文写进数据库普通字段、审计或本地日志，也不得在回复中复述 Key。

### 9.2 当轮未消费的 Key

落库消息只有打码版本。如果用户粘贴 Key 后，模型当轮没有产生带该 Key 的工具调用，例如先追问 Provider 名称或 Base URL，则后续上下文只能看到掩码，原 Key 不会被 TelePilot 恢复。

处理规则：

- 不把原 Key 额外保存到会话、Redis 或临时缓存。
- 后续需要 Key 且上下文里只有掩码时，Agent 明确要求用户重新发送。
- Web 用户也可以在内联 Action 卡片的可选密码输入框补填。
- Bot 用户直接在普通私聊中重新发送，不切换特殊输入模式。
- 不得把缺少 Key 静默解释为“验证失败”或尝试使用掩码调用 Provider。

### 9.3 其他敏感字段

相同聊天和 Action 加密机制可用于代理密码与 Bot Token，但这些长尾工具不进入前三阶段。

所有新增凭据字段继续遵守项目 `MASTER_KEY`、`*_enc`、`app.scripts.rekey` 和敏感日志过滤规则。Bot 日志不得输出 update 文本、Telegram Bot API URL 或异常请求体。

## 10. Provider 与 System Prompt

### 10.1 固定 Provider

`SystemSetting` 新增 `system_agent_config` JSON：

- `enabled`：总开关，默认 `false`。
- `provider_id`：必选固定 Provider。
- `model`：必选或使用该 Provider 的默认 tools 模型。
- `max_steps`：沿用 `llm_agent.py` 默认值。
- `max_tool_calls`：沿用 `llm_agent.py` 默认值。
- `session_token_limit`：单次上下文预算。

规则：

- 只允许选择真实声明支持 tools 的模型。
- 固定 Provider 不可用时直接报错，不自动切换其他 Provider。
- 没有可用 Provider 时，Web 和 Bot 都给出现有 Provider 配置入口。
- `auto` 路由等真实使用证明有必要后再加入 backlog。

### 10.2 System Prompt

Prompt 只保存稳定知识：

- 账号、功能、Rule、交互规则、Scheduler、Provider、指令、插件、日志和台账的关系。
- 交互规则在账号级配置 JSON，不属于通用 Rule 表。
- 当前系统时区、本地时间、账号上下文和用户角色。
- 查询必须使用工具，不根据聊天记忆编造系统状态。
- 写工具只产生待确认 Action，不能提前声称完成。
- 配置数据由模型直接解释，不注册独立 `explain_*` 工具。
- `get_*` 工具必须返回解释所需的确定性数据，例如 `next_run_at`、触发字段和启用状态。
- 用户通过 Web 发送密钥是允许行为；模型不得在回答中复述密钥。
- Web 和 Bot 都允许普通聊天携带密钥；模型不得在回答或工具结果中复述密钥。
- 工具失败必须说明业务是否发生变化。

## 11. 工具注册协议

每个 `ToolSpec` 包含：

- `name`
- `description`
- `input_schema`
- `read_only`
- `min_role`
- `risk`
- `channels`
- `read_handler`，读工具必填
- `preview_handler`，写工具必填
- `execute_handler`，写工具必填
- `secret_argument_names`
- `runtime_effects`

每个 handler 接收统一 `ToolContext`：

- 数据库会话 `db`
- Web/Bot 身份和角色
- 渠道
- 会话
- 账号上下文
- 当前 Action，可空

硬规则：

- handler 禁止创建 `AsyncSessionLocal`。
- handler 禁止 `commit()` 或 `rollback()`；允许 `flush()`。
- handler 禁止调用本项目 HTTP API，必须直接调用 service。
- handler 不返回 ORM、明文密钥或无界数据。
- `secret_argument_names` 中的值在 Action 建立前移入加密字段。
- 新工具至少覆盖 Schema、权限、事务和结果上限测试。

## 12. Action 与数据库事务

### 12.1 预览

写工具只运行 `preview_handler`：

1. Schema 校验并规范化参数。
2. 把敏感参数移入加密 payload。
3. 读取当前对象并计算变更摘要。
4. 创建 `pending` Action。
5. 返回内联确认卡片，不修改业务数据。

不保存 fingerprint。确认时对象不存在、字段不合法或状态已变化，由最新 service 校验直接返回失败；更新工具只修改用户明确指定的字段，避免整对象覆盖。

### 12.2 确认执行

`ActionExecutor`：

1. 创建一个 `AsyncSession` 并开启事务。
2. 对 Action 执行 `SELECT ... FOR UPDATE`。
3. 校验 Action 为 `pending` 且未过期。
4. 把 Action 改为 `executing`。
5. 解密当次所需敏感参数，仅保留在请求内存中。
6. 在同一数据库会话调用 `execute_handler`。
7. 写业务审计。
8. 把 Action 改为 `executed`，保存打码结果并清空密文。
9. 一次性 commit。

数据库步骤失败时整体 rollback，再用短事务把 Action 记录为 `failed` 并清除密文。不能出现业务对象已创建、Action 却仍为 `pending` 的正常路径。

### 12.3 重复确认保护

- 确认接口只接收 Action ID，不再传递独立 nonce。
- 对 Action 行加锁后检查状态；只有 `pending` 可以进入 `executing`。
- 双击、前端重试和 Telegram 重复 Update 对同一 Action 返回现有状态，不再次调用 handler。
- 同一用户消息偶尔生成两个 `pending` 草稿是可接受的；未确认草稿不产生业务变化。
- `executing` 超时不自动重放，记录为 `failed`、`error_code=RESULT_UNKNOWN`，提示人工检查目标对象。

### 12.4 事务外动作

首发涉及的事务外动作：

- Provider quick verify
- Worker reload
- Bot 消息发送或编辑
- Scheduler 立即执行

规则：

- Provider 验证在数据库事务前完成；失败时 Action 保持 `pending`，清除无效密钥，允许重新输入。
- 数据库配置 commit 后再 reload。
- reload 失败时 `runtime_sync_status=failed`，显示“配置已保存，运行时同步失败”。
- 提供“重新同步”按钮；不建设通用 outbox。
- 结果无法判断的外部动作标为 `failed/RESULT_UNKNOWN`，不自动重试。

### 12.5 现有 service 改造边界

- 只调整前三阶段实际复用的 service，不做全仓事务整风。
- `command_service.py` 已使用外部 `db` 和 `flush()`，保持该模式。
- 从 `api/rules.py` 只抽出 Agent 需要的 Rule/Scheduler 规范化和 CRUD service。
- 每调整一个现有 service，立即运行该模块全部既有测试和新增 Agent 测试。
- 未接入 Agent 的 Provider、模板、台账或其他 API 不为“统一风格”顺手重构。

## 13. 确认、权限与渠道

### 13.1 风险等级

`normal`：

- 创建、修改、启停 Rule 和 Scheduler。
- 启停功能或账号插件。
- 创建和修改 Provider、指令。
- 账号暂停和恢复。

`dangerous`：

- 删除 Rule、Scheduler、Provider 或指令。
- Worker 重启。
- Scheduler 立即执行。
- `manual-paid`，若后续加入。

所有写操作确认一次；危险操作使用红色卡片并写明影响，不增加输入固定文字等企业级步骤。

### 13.2 角色

| 角色 | 能力 |
| --- | --- |
| `viewer` | 当前账号范围内查询 |
| `operator` | 当前账号范围内普通写操作 |
| `admin` | 系统级查询、全部普通写操作和危险操作 |

不新增权限引擎。Web 使用当前登录身份；Bot 复用 `AccountBotUser` 角色和账号范围。

### 13.3 渠道差异

- Web 允许系统级会话和账号级会话。
- Bot 只允许绑定账号范围内操作。
- Web 和 Bot 密钥都可以进入普通 Agent 聊天及当前上游模型。
- Bot 可以确认不需要文件上传的普通和危险 Action。

## 14. NDJSON 与 Web API

### 14.1 NDJSON 事件

消息接口返回 `application/x-ndjson`，每行一个完整 JSON 对象：

- `run_started`
- `assistant_delta`
- `tool_started`
- `tool_finished`
- `action_proposed`
- `action_updated`
- `assistant_message`
- `error`
- `done`

所有事件带 `run_id`、`session_id`、时间戳和递增序号。事件中的敏感参数只显示 `has_secret=true`，不得回显明文。

前端解析器必须处理任意分块边界；连接中断后通过会话和 Action API 读取最终状态，不依赖重放旧流。

### 14.2 API

配置：

- `GET /api/system-agent/config`
- `PATCH /api/system-agent/config`
- `GET /api/system-agent/capabilities`

会话：

- `POST /api/system-agent/sessions`
- `GET /api/system-agent/sessions`
- `GET /api/system-agent/sessions/{session_id}`
- `PATCH /api/system-agent/sessions/{session_id}`
- `DELETE /api/system-agent/sessions/{session_id}`
- `DELETE /api/system-agent/sessions`

消息：

- `POST /api/system-agent/sessions/{session_id}/messages/stream`
- `GET /api/system-agent/sessions/{session_id}/messages`

Action：

以下接口和 `system_agent_action` 表从阶段 2 开始注册；阶段 1 不创建空 Action 表。

- `GET /api/system-agent/actions`
- `GET /api/system-agent/actions/{action_id}`
- `POST /api/system-agent/actions/{action_id}/confirm`
- `POST /api/system-agent/actions/{action_id}/reject`
- `POST /api/system-agent/actions/{action_id}/retry-runtime-sync`
- `POST /api/system-agent/actions/{action_id}/secret-input`：Web 内联卡片可选补填。

`secret-input` 只接受注册表声明的字段，立即加密，响应只返回 `has_secret=true`。

## 15. Web 工作台

新增一级入口 `/assistant`，侧边栏名称“系统助手”。

首版采用对话优先布局：

```text
┌──────────────┬────────────────────────────────────┐
│ 会话列表抽屉  │ 系统助手 / 当前账号 / 模型状态      │
│              ├────────────────────────────────────┤
│ 新建          │ 用户消息                           │
│ 历史会话      │ 工具运行摘要                       │
│              │ Agent 回答                         │
│              │ [内联 Action 预览/密钥补填/确认]    │
│              ├────────────────────────────────────┤
│              │ 输入框                             │
└──────────────┴────────────────────────────────────┘
```

- 不建设右侧独立 Action 面板。
- 不建设 `RecentActions` 首版组件；历史 Action 跟随会话消息展示。
- 移动端将会话列表放入抽屉，对话和 Action 保持单列。

新增：

- `frontend/src/pages/Assistant/Index.tsx`
- `frontend/src/api/systemAgent.ts`
- `frontend/src/components/assistant/Conversation.tsx`
- `frontend/src/components/assistant/Composer.tsx`
- `frontend/src/components/assistant/ActionCard.tsx`
- `frontend/src/components/assistant/SessionDrawer.tsx`
- `frontend/src/components/assistant/SecretInput.tsx`

修改：

- `frontend/src/App.tsx` 注册 `/assistant`。
- `frontend/src/components/layout/Sidebar.tsx` 新增一级入口。
- AI 中心只提供“配置系统助手模型”链接，不把助手放入 `/ai`。

## 16. 管理 Bot `/agent`

Bot 从第一阶段接入，不再放到开发末尾。

命令：

- `/agent`
- `/agent <自然语言>`
- `/agent new`
- `/agent exit`
- `/agent clear`

行为：

- 私聊自由文本进入与 Web 相同的 `SystemAgentService`。
- 默认绑定当前管理 Bot 对应账号。
- 读结果尽量编辑“处理中”原消息，减少消息噪音。
- 写工具返回 Inline 确认/取消按钮。
- 回调 nonce 绑定 Action、账号、Bot 用户和过期时间。
- 点击确认调用同一个 `ActionExecutor`。
- 长日志和列表给摘要及 Web 查看入口。
- Bot 普通私聊允许直接发送 Key，与 Web 使用同一密钥抽取、加密暂存和落库打码逻辑。

## 17. 首发工具矩阵

首发目标约 40 个工具。`list/get/save` 使用筛选参数和可选 ID，避免为同一业务对象拆出大量重复工具。

### 17.1 系统与账号

- `system.get_context`：时区、指令前缀、Agent/AI 开关和当前版本。
- `system.get_health`：真实组件就绪状态。
- `accounts.list`
- `accounts.get`
- `accounts.set_paused`
- `accounts.restart_worker`

### 17.2 交互规则

- `interaction.list_rules`
- `interaction.get_rule`
- `interaction.list_active_sessions`
- `interaction.save_rule`：ID 为空时创建，有 ID 时更新明确字段。
- `interaction.set_enabled`
- `interaction.delete_rule`

约束：

- 工具直接返回触发、暂停、结束、启用状态和会话影响等结构化字段，由模型解释。
- `set_enabled(false)` 只做普通禁用，不安排自动恢复。
- 如果用户说“停两小时”，Agent 必须提示当前版本不会自动恢复，并询问是否仍要禁用。

### 17.3 通用 Rule

- `rules.list`
- `rules.get`
- `rules.save`
- `rules.set_enabled`
- `rules.delete`

通用工具按 feature schema 校验；交互规则仍禁止走这组工具。

### 17.4 Scheduler

- `scheduler.list`
- `scheduler.get`
- `scheduler.save`
- `scheduler.set_enabled`
- `scheduler.delete`
- `scheduler.execute_now`

约束：

- `get/list` 返回 `cron/once/interval` 原始字段、系统时区、自然语言所需字段和确定性的 `next_run_at`。
- `run_command` 不绕过现有白名单。
- `call_llm` 必须引用真实 Provider。
- 保存结果仍是标准 `Rule(feature_key="scheduler")`。

### 17.5 Provider

- `providers.list`：支持按 ID/名称筛选，返回脱敏配置、模型清单和 `has_api_key`。
- `providers.save`：ID 为空时创建，有 ID 时更新；支持聊天 Key 或可选补填。
- `providers.delete`
- `providers.verify`

首版固定 Provider 路由。删除时复用现有 service 校验，预览列出可查询到的引用。

### 17.6 自定义指令

- `commands.list`：支持按 ID、名称、类型筛选。
- `commands.save`
- `commands.delete`
- `commands.set_enabled_for_accounts`

约束：

- 复用 `CommandTemplateBase`。
- 校验内置命令保留字、名称和跨模板别名冲突。
- 新建并启用账号由一次 `save` Action 完成，在同一事务写入。

### 17.7 功能与账号级插件启停

- `features.get_account_status`
- `features.set_enabled`

源码核对确认：内置功能和第三方插件的账号级启停都落在 `AccountFeature`，现有插件 API 也调用 `feature_service.bulk_set_enabled()`。因此统一使用 `features.*`，通过 feature 元数据区分内置功能与插件，不做两套工具壳。

`InstalledPlugin.enabled` 是安装包全局状态，属于插件文件管理；首发不接入安装包全局启停、安装、更新、卸载和仓库管理。

### 17.8 日志

- `logs.recent`
- `logs.search_errors`
- `logs.get_event_detail`

约束：

- 默认 20 条，单次最大 500 条。
- 必须带 limit；大范围搜索必须带时间窗。
- 返回打码摘要，不把整份日志长期复制到会话。

### 17.9 资金台账

- `ledger.summary`
- `ledger.list`

`ledger.list` 通过类型筛选返回普通流水或补付记录。首发只读，不包含 `manual-paid` 和重置。

“今天/今日”必须读取 `SystemSetting("timezone")`，按本地零点到下一本地零点转换 UTC，不使用默认 UTC 回溯窗口代替。

## 18. Backlog

以下能力不进入前三阶段，也不为其提前创建工具文件和测试：

- 临时禁用自动恢复和恢复 Runner。
- Provider `auto` 路由与 fallback。
- `ledger.mark_compensation_manual_paid`、`ledger.reset`。
- 插件安装包全局启停、安装、更新、卸载、reload 和远程仓库管理。
- 消息模板编辑与测试发送。
- 代理、通知 Bot、Webhook、配置包导入导出。
- 风控写操作、别名、忽略对象、设备档案和 Sudo 用户。
- 系统重启、更新、配置导入和文件上传。
- AI liveness 主动探测和网络路径诊断。

Backlog 规则：

- 真实使用出现明确高频需求后，才把对应能力加入下一阶段。
- 必须先有稳定 service，再注册工具。
- `GET /api/system-agent/capabilities` 对未接入能力返回 `available=false` 和简短原因。
- Agent 遇到未接入请求时说明限制并给出现有页面入口，不假装执行。
- 不承诺把项目每一个低频按钮都 Agent 化。

## 19. 四阶段交付

每个阶段独立可合并、可使用、可回滚。阶段内部的多个提交只积累到 `CHANGELOG.md` 的 `Unreleased`；阶段验收完成时才统一发布版本。

### 阶段 1：`0.64.0` Web + Bot 只读助手

交付：

- `system_agent_session`、`system_agent_message` 两张表及阶段 1 迁移。
- 固定 Provider 配置、Runtime、注册表和消息持久化。
- `/api/system-agent` 会话与 NDJSON API。
- `/assistant` 对话页面。
- 管理 Bot `/agent`、助手模式和只读结果编辑。
- 第 17 节全部只读工具。

验收：

- Web 和 Bot 均能查询系统、账号、交互规则、Scheduler、日志和今日收入。
- 配置数据由模型解释，不依赖 `explain_*` 工具。
- 会话历史可恢复、删除。
- 无 Provider 或 Provider 不支持 tools 时给出确定性入口。

### 阶段 2：`0.65.0` 核心写操作

交付：

- `system_agent_action` 表及阶段 2 迁移。
- Action 状态机、统一事务执行器、行锁和重复确认保护。
- 内联 Action 卡片和 Bot Inline 确认。
- Rule、交互规则、Scheduler、账号暂停/恢复、功能和插件启停。
- commit 后 reload 与同步失败重试。

验收：

- 双击确认、网络重试和重复 Telegram Update 不重复执行。
- service 校验失败整体 rollback。
- 目标已删除或状态变化时返回明确失败，不覆盖整对象。
- reload 失败显示“已保存但同步失败”。
- “停两小时”明确提示不自动恢复。

### 阶段 3：`0.66.0` Provider、指令和密钥输入

交付：

- Provider 与自定义指令写工具。
- Web 聊天 Key 抽取、Action Fernet 临时加密和消息落库打码。
- Web Action 卡片可选密钥补填。
- Bot 普通聊天 Key 抽取、Action Fernet 临时加密和消息落库打码。
- Provider quick verify 与保存。

验收：

- Web 聊天 Key 会进入上游模型，但不会明文写入消息、Action 普通参数、审计和本地日志。
- Bot Key 与 Web 一样进入上游模型；TelePilot 本地会话只保存打码版本。
- Action 执行、拒绝和过期后清除密文。
- Provider 验证失败不落库，并允许重新输入。
- 指令名称和别名冲突返回可读错误。

### 阶段 4：真实使用驱动的扩展

- 不预先指定版本号和工具清单。
- 根据前三阶段实际使用频率，从第 18 节一次选择一个小批次。
- 每批必须独立有价值、独立测试、按项目 SemVer 判断版本。
- 没有高频需求时，阶段 4 可以永久不做。

## 20. 文件改动清单

### 20.1 新增

- `backend/app/api/system_agent.py`
- `backend/app/schemas/system_agent.py`
- `backend/app/db/models/system_agent.py`
- `backend/app/services/system_agent/` 全目录
- `backend/app/services/interaction_rule_service.py`
- `backend/app/services/rule_service.py`
- 两份 Alembic 迁移：阶段 1 创建会话和消息表，阶段 2 创建 Action 表。
- `frontend/src/api/systemAgent.ts`
- `frontend/src/pages/Assistant/Index.tsx`
- `frontend/src/components/assistant/Conversation.tsx`
- `frontend/src/components/assistant/Composer.tsx`
- `frontend/src/components/assistant/ActionCard.tsx`
- `frontend/src/components/assistant/SessionDrawer.tsx`
- `frontend/src/components/assistant/SecretInput.tsx`
- `backend/app/tests/test_system_agent_registry.py`
- `backend/app/tests/test_system_agent_service.py`
- `backend/app/tests/test_system_agent_actions.py`
- `backend/app/tests/test_system_agent_api.py`
- `backend/app/tests/test_system_agent_tools.py`
- `backend/app/tests/test_system_agent_bot.py`
- `backend/app/tests/test_interaction_rule_service.py`
- `docs/SYSTEM-AGENT.md`

### 20.2 修改

- `backend/app/main.py`
- `backend/app/db/models/__init__.py`
- `backend/app/api/rules.py`
- `backend/app/api/commands.py`
- `backend/app/services/command_service.py`
- `backend/app/services/account_bot_runtime.py`
- `backend/app/scripts/rekey.py`
- tools 模型判断 helper 的现有归属文件。
- `frontend/src/App.tsx`
- `frontend/src/components/layout/Sidebar.tsx`
- `frontend/src/pages/AI/Index.tsx`
- `CHANGELOG.md`
- 每阶段发布时的四处版本文件。

实现前重新 `rg` 函数和文件位置，不把历史行号当接口。

## 21. 测试计划

### 21.1 核心单元测试

事务与重复确认：

- handler 不能自行 commit。
- 业务写入、审计和 Action 状态同事务提交。
- 中途异常整体 rollback。
- Action ID、行锁、状态迁移和重复确认。
- `RESULT_UNKNOWN` 不自动重放。

密钥与打码：

- Web/Bot Key 从普通 arguments 移入 `secret_payload_enc`。
- Web/Bot 用户消息和工具消息落库前替换明文 Key。
- Bot Key 进入上游模型后，本地会话、Action 普通参数、审计和日志只保留打码内容。
- 当轮未被工具消费的 Key 不保存；后续明确要求重新发送。
- 执行、拒绝、过期和启动清理都会清除密文。
- rekey 覆盖 `secret_payload_enc`。

领域：

- “今日”时区边界，包含夏令时地区。
- Scheduler cron/once/interval 与 `next_run_at`。
- 交互规则不写入通用 Rule 表。
- Provider 验证失败不落库。
- 指令名称、别名和启用关系回滚。
- 日志默认 20、最大 500。

### 21.2 API 与 Bot 测试

- 会话 CRUD 和所有权。
- NDJSON 分块边界、正常流、工具流、Action 流和错误流。
- Action 确认、拒绝、过期和同步重试。
- `secret-input` 只接受注册字段且不回显。
- `/agent` 模式进出、斜杠命令优先、角色限制和 Inline 回调。
- Bot 普通聊天 Key 的提取、打码、确认和重发分支。

### 21.3 前端与门禁

- `pnpm --dir frontend typecheck`
- `pnpm --dir frontend build`
- `/assistant` 做一次桌面和一次移动端冒烟检查。
- 检查对话、内联 Action、会话抽屉和长错误不重叠。
- 不建设庞大的 UI 状态组合测试矩阵。

后端门禁：

```bash
cd backend && . .venv/bin/activate && ruff check app
cd backend && . .venv/bin/activate && pytest
backend/.venv/bin/python scripts/validate-plugin-examples.py
backend/.venv/bin/python scripts/validate-installed-interaction-plugins.py
git diff --check
```

每改造一个现有 service，先运行该 service 既有测试，再运行全量门禁。

### 21.4 分阶段真实 E2E

阶段 1：

- Web 和 Bot 查询交互规则、最近日志和今日收入。
- 页面重开恢复会话。

阶段 2：

- Web 创建 Scheduler，在原页面核对。
- Bot 禁用规则并 Inline 确认。
- 重复确认不产生重复对象。
- reload 失败后手动重新同步。

阶段 3：

- Web 聊天直接粘 Key 创建 Provider。
- Web 不提供 Key 时在 Action 卡片补填。
- Bot 普通私聊直接粘 Key，确认后创建 Provider。
- 检查数据库、审计和日志无 Key 明文。
- 创建自定义指令并给账号启用。

## 22. 故障语义

| 故障 | 用户可见结果 | 业务变化 |
| --- | --- | --- |
| 固定 Provider 不可用 | 本轮失败，提示检查固定模型 | 未确认 Action 不变 |
| 数据库不可用 | 查询或写入失败 | 写操作不发生 |
| Redis 不可用 | Web 可用；Bot 助手模式和 Inline nonce 不可用 | 普通 TelePilot 不受影响 |
| service 校验失败 | 显示可读业务错误 | 事务整体回滚 |
| Provider Key 验证失败 | 要求重新输入，清除无效密文 | Provider 不落库 |
| Worker reload 失败 | 配置已保存、运行时同步失败 | 数据库已变更 |
| NDJSON 中断 | 页面重读会话和 Action | 已开始的数据库事务继续 |
| 外部结果未知 | `failed/RESULT_UNKNOWN`，要求人工检查 | 可能发生，不自动重试 |

## 23. 扩展与维护规则

新增工具固定流程：

1. 确认已有稳定 service；没有则只抽当前工具真正需要的 service。
2. 定义输入 Schema、角色、渠道和风险。
3. 读工具返回有限结构化事实，让模型解释。
4. 写工具实现 preview 和 execute，使用传入 `db`。
5. 敏感参数声明在 `secret_argument_names`。
6. 注册工具，不修改 Agent 主循环。
7. 增加最小 Schema、事务、幂等和领域测试。
8. 更新 `docs/SYSTEM-AGENT.md` 能力矩阵。

禁止：

- 为配置解释新建 `explain_*` 工具。
- Tool handler 自建数据库会话或 commit。
- Agent 调用现有 Web API 间接完成业务操作。
- 返回 ORM、密钥或无界日志。
- 建立可调用任意 URL、SQL、文件或方法名的万能工具。
- 为尚未进入阶段的 backlog 预先搭空架子。
- 因为接入一个工具而顺手重构同领域全部 API。

## 24. 发布与回滚

### 24.1 分阶段发布

- 阶段 1 发布 `0.64.0`：Web + Bot 只读助手。
- 阶段 2 发布 `0.65.0`：核心写操作。
- 阶段 3 发布 `0.66.0`：Provider、指令和密钥输入。
- 阶段 4 在范围确定时按项目 SemVer 判断，不提前占版本号。

每阶段发布时：

- 将本阶段 `CHANGELOG.md` 的 `Unreleased` 内容移动到中文正式版本段落。
- 同步更新：
  - `backend/app/__init__.py`
  - `backend/pyproject.toml`
  - `frontend/package.json`
  - `frontend/src/lib/version.ts`
- commit、PR 和 release 文案使用中文。
- 只写本阶段已经实现和验证的能力，不写后续愿景。

### 24.2 功能回滚

- 关闭 `system_agent_config.enabled`，隐藏入口并停止新请求。
- 管理员仍可读取历史和 Action，但不能发起新执行。
- Agent 创建的标准业务对象继续运行。
- 没有自动恢复 Runner 和后台 Agent 任务需要额外收尾。

### 24.3 代码与数据回滚

- 新表不改变现有业务表语义，代码回滚时可保留三张 Agent 表。
- downgrade 只删除 Agent 表，不删除 Agent 已创建的 Rule、Provider 和指令。
- 已公开使用后若要删除会话表，先由用户明确确认。
- 加密暂存字段无法解密时，Action 失败并要求重新输入，不影响现有 Provider 密文。

## 25. 最脆弱假设

计划假设至少有一个稳定、真实支持 tools 的 Provider，并且用户愿意把 Web 聊天内容发送给该 Provider。

如果 tools Provider 不可用：

- `/assistant` 和 `/agent` 不使用自由文本猜测业务参数。
- 系统保留现有确定性页面和 Bot 命令。
- 普通 Worker、Scheduler 和交互 Bot 不依赖 System Agent。

如果用户将来不再接受 Key 进入 Telegram 或上游模型：

- 关闭 Web 普通消息中的敏感参数支持。
- Web 保留 Action 卡片补填；Bot 改为返回 Web 深链。
- 数据模型和业务工具无需重做。

## 26. 已确认的取舍

### 26.1 采用统一事务，不采用 handler 自建会话

原因：统一事务减少半完成状态，也让新增工具不必重新决定 commit/rollback。

### 26.2 用 Action 状态防重，不建设额外幂等和漂移框架

原因：个人单用户环境下，预览后并发修改概率低；最新 service 校验和明确字段 PATCH 足够。重复确认由 Action ID、行锁和状态机解决，不再增加 nonce 或唯一幂等键。

### 26.3 允许 Web 聊天 Key 进入模型

原因：这是用户明确选择的便捷路径。接受上游模型看到 Key 的代价，同时确保本地数据库普通字段、会话、审计和日志不保存明文。

### 26.4 Bot 普通聊天允许 Key

原因：用户把 Telegram 和上游模型都视为可信，优先保持不离开 Bot 的完整操作体验。TelePilot 本地持久化仍在落库前打码。

### 26.5 不强制 SecureFields

原因：Web 密钥输入框只是未在聊天中提供 Key 时的可选补填，不建设独立凭据工作流、verification grant 和长期密钥暂存。

### 26.6 删除 `explain_*`

原因：结构化 `get/list` 数据已经足够让模型解释，单独解释工具会复制文案和测试。

### 26.7 长尾能力进入 backlog

原因：插件安装、Webhook、配置导入、系统重启等低频能力的维护成本高于自然语言操作收益。

### 26.8 Bot 前置、分阶段发布

原因：Bot 是原始核心入口；个人自用应尽早获得真实反馈，不等待全部功能完成。

## 27. 实施完成定义

前三阶段完成时，System Agent 基线能力完成：

- `/assistant` 和 `/agent` 均可用。
- 会话历史可恢复、删除。
- 固定 tools Provider 可配置并有明确错误状态。
- 约 40 个高频工具覆盖系统、账号、交互规则、Rule、Scheduler、日志、台账、Provider、指令、功能和插件启停。
- 配置解释由 LLM 基于结构化结果完成，无 `explain_*` 工具。
- 写操作通过内联 Action 预览和一次确认执行。
- 数据库事务、Action 行锁和重复确认保护通过测试。
- Web 聊天 Key 按用户决定进入上游模型，本地持久化前打码。
- Bot Key 进入上游模型，本地会话、Action 普通参数、审计和日志不保存明文。
- 密钥密文在执行、拒绝或过期清理时删除，并覆盖 rekey。
- Agent 创建的对象可在现有页面继续管理。
- 每阶段静态门禁、后端测试、前端构建和真实 Web/Bot E2E 完成。
- `docs/SYSTEM-AGENT.md` 写清工具矩阵、Key 边界和故障排查。

阶段 4 backlog 不属于基线完成条件。

## 28. 分支与合并策略

- 从干净且最新的 `main` 创建 `codex/system-agent` 分支；并行时使用独立 worktree。
- 每个阶段形成一批中文提交，开发中只积累 `CHANGELOG.md` 的 `Unreleased`。
- 阶段验收完成后合入最新 `origin/main`、运行全量门禁，再统一迭代该阶段版本。
- Git 按内容上下文合并，不依赖固定行号；双方修改同一文件不同区域通常可自动合并。
- 冲突逐块理解双方意图，禁止用整文件 `ours/theirs` 覆盖 Provider、指令、Rule、Bot 或前端路由。
- 其他用户或 Agent 的未提交改动只观察、不回滚、不格式化。
- 每个阶段有独立总开关或能力判断；未完成的后续阶段不阻止已发布阶段使用。
- tag 必须指向合并后的发布提交。
