# TelePilot 系统 AI Agent（自然语言运维助手）实施方案（历史对照稿）

> 状态：历史对照稿。最终方向已合并到 `Agent-Plan.md`；后续实施以 `Agent-Plan.md` 为唯一执行真相源。本文档仅保留 Claude v2 的设计过程和取舍记录。

## 1. Context

目标：赋予一个 AI Agent 操作整个 TelePilot 系统的能力，管理员用自然语言完成原本要手动填表/翻页面的运维操作与查询诊断。两个交互入口：**前端聊天窗口** 与 **绑定的管理 Bot（账号 Bot 私聊）**，本轮一起交付。

典型用例（用户原话级）：
- 「帮我创建一个每天 9 点发消息的定时任务」
- 「添加一个模型提供商，base_url 是 …，key 是 …」
- 「添加一条自定义指令 ,fanyi，AI 类型，用 XX 模型」
- 「交互里有哪些规则？帮我暂时禁用交互里的某条规则」
- 「这条规则的开启和暂停是用什么触发的？」（解读规则 config）
- 「帮我看下最近 50 条日志 / 为什么这条规则没触发」
- 「资金台账我今日收入多少？」

设计立场：**全系统能力面都可被 Agent 查询与调配**——不是给每个页面单独做 AI 表单，而是一个统一的工具层覆盖系统所有域；查询直接执行，写操作一律确认制。

已确认的三个决策：
1. **写操作确认制**：查询直接执行；所有写操作先生成「操作预览卡片」，用户确认后才真正执行。
2. **全域能力面**：读能力覆盖系统全部模块；写能力覆盖定时任务、自定义指令、LLM Provider、插件启停、全域规则（含交互规则）启停与 CRUD、账号暂停/恢复。
3. **双入口同版本交付**（前端 + 管理 Bot）。

## 2. 现有可复用资产（探索结论，全部已验证）

| 资产 | 位置 | 说明 |
|---|---|---|
| Agent 工具循环 | `backend/app/services/llm_agent.py` | `run_agent()` 有界循环：AgentTool(spec+handler+read_only)、AgentLimits、防重复调用、超限总结轮、AgentCallbacks(on_step/on_usage/on_tool_start/on_tool_finish) |
| 协议层 | `backend/app/services/llm_protocol.py` | ModelRequest/ModelResponse 带 tools/tool_choice；三种 api_format（chat_completions/responses/anthropic_messages）均 `tools=True` |
| 结构化调用 | `backend/app/services/llm_invoke.py` `invoke_structured()` | 带 fallback 链、预算门、用量记录 |
| Provider 路由 | `backend/app/worker/plugins/ai_facade.py` `_resolve_route(require_tools=True)`、`_tools_model_for_dto` | 需抽到共享位置供系统 Agent 复用 |
| Agent 观测 | `backend/app/services/llm_agent_observability.py` | 步骤/用量观测回调 |
| 审计 | `backend/app/services/audit.py` `write(db, user_id, action, target, detail)` | 已有 `redactor` 脱敏 |
| 定时任务 | `Rule(feature_key="scheduler")` + `api/rules.py` `_normalize_scheduler_config_for_save` | once/interval/cron + send_message/run_command/call_llm 动作 |
| 通用规则 | `Rule` 表按 feature_key 区分（内置 5 个 + 第三方插件 feature 同步登记） | `api/rules.py` 全套 CRUD + dry-run |
| 交互规则 | **不在 Rule 表**：存于账号级交互配置 JSON 的 `rules` 数组（`account_bot_service.normalize_interaction_rules`，SystemSetting 持久化）——已对照代码核实，v1 的「交互规则挂 Rule 表」表述是错的 | 需新增 `interaction_rule_service`：按稳定规则 ID 定位、只 patch 目标字段、复用既有归一化、保存后通知 reload/重启交互 runtime |
| 自定义指令 | `CommandTemplate` + `services/command_service.py` | reply_text/forward_to/ai 类型 |
| Provider | `LLMProvider` + `api/commands.py`（0.62.0 已有 quick-verify 流式验证） | 创建前可先验证连通 |
| 资金台账 | `services/ledger_service.py`：`list_ledger_entries` / `summarize_ledger`（LedgerFilters 带时间窗）/ `list_compensations` | 「今日」必须按 `SystemSetting("timezone")` 算本地零点→UTC 窗口（service 默认窗口是纯 UTC 回溯，不能直接当「今日」用）；scheduler_runtime.py:135 已有同款时区读取可参照 |
| 日志 | `RuntimeLog` / `AuditLog`（`api/logs.py`）、EventTrace 链路 | 查询与诊断素材 |
| 管理 Bot | `services/account_bot_runtime.py` `_handle_command`（if/elif；私聊全部文本进入）+ `_request_confirm`（Redis nonce 确认模式）| `AccountBotUser` 三角色 viewer/operator/admin |
| 前端流式 | `frontend/src/api/commands.ts` `streamQuickVerifyProvider`（fetch+getReader 消费 StreamingResponse） | 直接复用该模式 |
| 前端栈 | React18 + Radix/shadcn + TanStack Query + react-markdown + sonner | 聊天 UI 渲染无新依赖 |

## 3. 架构总览

```
前端聊天窗口 ──NDJSON流──> api/agent.py ─┐
                                         ├─> services/system_agent/（核心，入口无关）
管理Bot私聊 ──polling──> account_bot_runtime ─┘      │
                                                     ├─ tools/     系统工具注册表（按域分模块，直调 service 层）
                                                     ├─ service.py 会话编排（run_agent + 流事件）
                                                     ├─ confirm.py 写操作确认流（pending_action）
                                                     └─ store.py   会话/消息持久化（新表）
```

核心原则：**工具白名单硬编码 + JSON Schema 参数校验 + service 层直调（不走 HTTP 自环）+ 写操作全确认 + 全链路脱敏 + 全审计**。

## 4. 阶段 A：后端 Agent 核心（`backend/app/services/system_agent/`，新模块）

### A1. 数据模型（新表，alembic 迁移）

- `agent_session`：id、web_user_id（可空）、bot_tg_user_id（可空）、account_id（可空=全局会话）、channel(web/bot)、title、status、created_at/updated_at
- `agent_message`：id、session_id、role(user/assistant/tool)、content(JSON：文本+工具调用+工具结果，落库前脱敏)、usage(JSON)、created_at
- `agent_pending_action`：id、session_id、tool_name、arguments(JSON，脱敏；凭据类参数 Fernet 加密暂存、执行/过期后清除)、summary(人类可读预览文本)、preview(JSON：当前值/目标值 diff)、fingerprint(生成预览时目标对象的状态指纹)、idempotency_key(唯一)、status(pending/executing/executed/rejected/expired/failed/needs_attention)、result(JSON)、execute_at(可空，定时自动恢复用)、expires_at、created_at
  - 待确认动作 TTL 默认 10 分钟，过期自动作废
  - 确认时用行锁把 pending 原子迁移为 executing；双击、网络重试、Telegram 重复 Update 一律返回原执行结果（幂等）
  - execute 前重读目标对象并比对 fingerprint：预览后目标被 Web/Bot/其它会话改过 → 置 needs_attention 要求重新生成预览，不盲目覆盖

### A2. 工具注册表（`system_agent/tools/`，按域分模块）

每个工具 = `ToolSpec`(JSON Schema) + async handler + `read_only` + `min_role`(viewer/operator/admin)。handler 自建 `AsyncSessionLocal`、直调现有 service 函数；返回给 LLM 的数据一律经 `redactor` 脱敏（api_key/token 只返 `has_api_key`）。

工具设计原则：**宁用少量泛化工具，不做一页面一工具**。Rule 表内的规则（定时任务、内置功能、第三方插件 feature）统一走 `feature_key` 参数化的泛化工具；**交互规则是例外**——它存在交互配置 JSON 里而非 Rule 表（对照 codex 计划修正），单独走 `interaction_*` 工具族。

**查询类（read_only=True，viewer 起）**：

| 域 | 工具 | 复用 |
|---|---|---|
| 账号 | `list_accounts` / `get_account_status`（含 worker 运行态） | account_service |
| 功能/规则 | `list_features(account_id)`；`list_rules(account_id, feature_key)` / `get_rule(rule_id)`（返回完整 config JSON，LLM 据此解读触发条件；仅覆盖 Rule 表域） | api/rules.py 背后逻辑 |
| 交互 | `interaction_list_rules(account_id)` / `interaction_get_rule(account_id, rule_id)`（返回归一化脱敏配置，LLM 解读 trigger_mode/开闭命令/金额条件——覆盖「交互里有哪些规则」「这条规则用什么触发」）；`get_interaction_bot_status(account_id)` | 新 `interaction_rule_service` |
| 定时任务 | `list_scheduled_tasks` / `get_scheduled_task`（= rules 的 scheduler 特化视图，附下次触发时间） | scheduler 校验函数 |
| 指令 | `list_command_templates` / `get_command_template`；`list_builtin_commands` | command_service |
| Provider | `list_llm_providers`（脱敏）/ `list_provider_models` | commands API service 层 |
| 插件 | `list_plugins`（已安装+启用态+签名态） | plugin services |
| 日志 | `search_runtime_logs(account_id?, level?, keyword?, limit≤500，缺省 20，回答标明实际范围与条数)`；`search_audit_logs(...)`；`get_trace_detail(trace_id)` | api/logs.py 查询逻辑；归纳只基于已查回的脱敏数据，不把日志文件整体喂给模型 |
| 台账 | `ledger_summary(date_from, date_to, ...)` / `list_ledger_entries(filters, limit)` / `list_payout_compensations`；「今日」由工具层按系统时区换算 UTC 窗口，回答须带统计区间与收入/支出/净额/笔数（避免把入账当利润） | ledger_service + SystemSetting("timezone") |
| AI 用量 | `get_llm_usage_summary` | llm_usage_service |
| 系统 | `get_system_health`（组件就绪态/资源快照）；`get_system_context`（时区/指令前缀/AI 开关——system prompt 注入也用它） | system_health、SystemSetting |
| 模板 | `list_message_templates` / `get_message_template` | message_template_service |
| Webhook | `list_webhooks` | webhooks API service 层 |
| 风控 | `list_rate_limit_rules` / `get_rate_limit_status` | rate_limit_service |

**写操作类（read_only=False，operator 起；标注 admin 的除外）**：

| 域 | 工具 | 说明 |
|---|---|---|
| 规则（Rule 表域） | `toggle_rule(rule_id, enabled)`；`create_rule` / `update_rule` / `delete_rule`（feature_key 必须在 feature 表已登记） | 复用 rules API 的校验与 worker reload 通知 |
| 交互规则 | `interaction_set_rule_enabled(account_id, rule_id, enabled)`：只 patch 目标规则的 `enabled`，绝不让模型回写整份交互配置。预览与回执必须说明「禁用只挡新触发，已活跃会话按既有语义走完」。「暂时禁用」未给时长要追问（直到手动恢复 / 给恢复时间）；给了时长则同时落一条 `execute_at` 定时恢复动作——只把同一规则 ID 的 enabled 改回 true，不覆盖期间其它字段改动；规则已删则恢复动作自动作废 | 新 interaction_rule_service，保存后通知 reload/重启交互 runtime |
| 定时任务 | `create_scheduled_task` / `update_scheduled_task` / `delete_scheduled_task`；`run_scheduled_task_now`（**admin**，单独强确认） | 把 `api/rules.py` 的 `_normalize_scheduler_config_for_save` 抽为共享函数复用；预览含下次触发时间；不绕过 run_command 白名单，call_llm 必须引用真实 Provider |
| 指令 | `create_command_template` / `update_command_template` / `delete_command_template` | command_service 校验 |
| Provider | `create_llm_provider` / `update_llm_provider` / `delete_llm_provider`（**仅 Web 渠道**；Bot 端只生成草稿并返回 Web 深链，API Key 永不经 Telegram） | create 前先跑 quick-verify 探测（复用 `llm_quick_verify`），失败把上游错误带回给 Agent 调整参数。Key 处理：聊天中粘贴 → 消息落库前打码、参数 Fernet 加密暂存于 pending_action、执行/过期后清除；未提供 → 确认卡片以 password 输入补填。Key 全程不进模型上下文、不回显 |
| 插件 | `enable_plugin` / `disable_plugin` | 复用 plugins API 背后 service；插件配置 JSON 生成不做（出错率高，留二期） |
| 账号 | `pause_account` / `resume_account`（**admin**） | account_service.pause/resume |
| 台账 | 补偿 `manual-paid` 标记（**admin**）；`reset_ledger` **不提供**（危险） | ledger_service |

写工具 handler 分两段：`preview(args) -> summary`（人类可读预览，含 dry-run 信息）与 `execute(args) -> result`。Agent 循环内写工具只跑 preview 并产出 pending_action；execute 只由确认接口触发。

### A3. 会话编排（`system_agent/service.py`）

- 复用 `run_agent()`；`model_call` 用 `invoke_structured`（source=`system_agent`，进用量/预算链路）
- Provider 选择：把 ai_facade 的 `_resolve_route` / `_tools_model_for_dto` 抽到共享模块；`SystemSetting` 新增 `system_agent_config`（固定 provider_id+model，或 auto 路由 require_tools；会话 token 上限；总开关）
- 多轮：从 `agent_message` 重建 ModelMessage 历史（滑窗截断）；system prompt 描述系统概念图（账号/功能/规则/指令/Provider/插件/台账 的关系、feature_key 语义、**交互规则在配置 JSON 而非 Rule 表**）+ 系统时区与当前本地时间 + 当前账号上下文 + 确认制规则
- 写工具触发时：pending_action 落库，给 LLM 的 tool result 为「已提交待确认」，模型自然收尾本轮
- 事件流回调（SSE 与 Bot 共用）：`step / tool_start / tool_result / text / pending_action / done / error`
- 每次工具执行（含确认后的 execute）写 `audit.write(action="agent.tool.<name>", detail={session_id, args脱敏})`

### A4. Web API（`backend/app/api/agent.py`，新）

- `POST /api/agent/sessions`、`GET /api/agent/sessions`、`GET /api/agent/sessions/{id}`、`DELETE /api/agent/sessions/{id}`
- `POST /api/agent/sessions/{id}/messages/stream` → StreamingResponse（NDJSON 事件，模式同 quick-verify stream）
- `POST /api/agent/pending-actions/{id}/confirm`、`/reject` → confirm 触发 execute 段并返回结果；状态机保证幂等
- 权限：CurrentUser（Web 登录即管理员，映射 admin 等级）；`_require_ai_enabled` 同款闸门 + system_agent 总开关

## 5. 阶段 B：前端聊天窗口

- 新页面 `frontend/src/pages/AI/Assistant.tsx`，挂到 AI 中心（`pages/AI/Index.tsx` 加卡片入口 + `App.tsx` 路由 `/ai/assistant`）；侧边栏不新增一级项
- `frontend/src/api/agent.ts`：会话 CRUD + `streamAgentMessage()`（照抄 `streamQuickVerifyProvider` 的 fetch+getReader 模式）
- 聊天 UI：
  - 消息气泡（react-markdown）、流式文本增量、工具调用折叠行（「正在查询交互规则…✓」）
  - **确认卡片**：pending_action 渲染为参数摘要表 + 「确认执行 / 取消」按钮；执行结果回填卡片
  - 会话列表侧栏（切换/删除历史会话）
- 设置卡片：AI 中心新增「系统助手」设置（启用开关、模型选择——复用 LLMProviders 页 provider/model 选择组件、会话 token 上限）

## 6. 阶段 C：管理 Bot 入口

- `_handle_command`（`account_bot_runtime.py:8306` 附近）新增 `/ai` 分支：进入/退出助手模式（模式标记存 Redis，key 仿 `interaction/session_index.py` 前缀风格，TTL 30 分钟滚动）
- 助手模式中：私聊自由文本 → system_agent 会话（channel=bot，绑定该账号 account_id 上下文）；未进入模式时 else 分支提示可用 `/ai`
- Bot 无流式：done 事件后整段发送；执行中先发「思考中…」再编辑为结果（复用 `_send(edit=True)`）
- 确认流：pending_action → inline 按钮（复用 `_request_confirm` 的 Redis nonce 模式，nonce 绑 pending_action id），「确认」走与 Web 相同的 confirm 执行路径
- 角色映射：`AccountBotUser.role` → 工具 min_role 过滤（viewer 只装载查询工具；operator 常规写；admin 全部）；Bot 角色不提升为 Web 全局管理员，一个账号的 Bot 用户不可查改其它账号
- Bot 端能力裁剪：Provider 等需要密钥的操作只出草稿 + Web 深链，任何凭据不经 Telegram（Telegram 服务端会留存聊天记录）；`/status`、`/rules` 等既有斜杠命令始终优先于助手模式的自由文本

## 7. 阶段 D：测试、文档、发版

- 后端测试（仿 `tests/test_llm_agent.py` / `test_plugin_ai_facade.py` 的 FakeDB / fake model_call 惯例）：
  - 工具注册表：schema 校验、角色过滤、脱敏（api_key 绝不出现在 tool result / 落库消息 / 审计 detail）
  - 确认流状态机：pending→confirmed→executed、过期、重复确认幂等、reject
  - 会话编排：写工具不直接执行、审计落库、用量 source=system_agent
  - 泛化规则工具：Rule 表域可列出/禁用；未登记 feature_key 被拒
  - 交互规则工具：按规则 ID 单字段启停、不覆盖其它字段；定时恢复只改 enabled、规则已删则作废
  - 时区：「今日/明天上午九点」在系统时区（含 DST）下窗口与 cron 计算正确
  - 漂移与幂等：fingerprint 变化置 needs_attention；双击/重复 Telegram Update 只执行一次
  - Bot 入口：/ai 模式进出、角色限制、inline 确认回调
- 文档：`docs/SYSTEM-AGENT.md`（能力面清单、权限矩阵、确认制、审计位置、扩展新工具的步骤）
- 门禁命令：`ruff check backend/app`、`pytest backend/app/tests`、`pnpm --dir frontend typecheck`、`pnpm --dir frontend build`、两个插件校验脚本、`git diff --check`；静态与本地测试不替代真实管理 Bot / Telegram / Provider 的 E2E
- 交付切分：可按「只读问答版 → 写操作确认版 → Provider/指令版 → Bot 入口」分批发版，每批独立可用，避免一次大爆炸合并
- CHANGELOG `Unreleased` 积累；发布时按 AGENTS.md 四处同步版本（minor；0.63.0 已被 Provider 验证工作流占用，本功能从 **0.64.0** 起）

## 8. 安全设计（贯穿）

1. 工具白名单硬编码，LLM 无法触达任意 API/SQL；参数过 JSON Schema + service 层既有校验双保险
2. 所有写操作确认制 + TTL 过期；确认接口校验会话归属与角色
3. 敏感字段全链路脱敏（tool result、agent_message 落库、审计 detail 均过 redactor + 高熵 token 启发式打码）；用户对话中粘贴的 api_key 只进当次工具参数（Fernet 加密暂存、用后即清），消息落库前打码；会话支持整体删除
4. Fail-closed 全清单：全局总闸关 → 拒绝写；Redis 不可用 → 停新会话与确认；DB 不可用 → 写全停；LLM Provider 不可用 → 未确认动作保持 pending，回复明确说明「没有发生任何业务变更」
5. 资金相关：台账只读（reset 不暴露）；补偿标记 admin 且确认制；payout 链路不加任何 Agent 直接触发入口
6. 预算：AgentLimits.max_total_tokens 会话级 + llm 预算门（invoke_structured 自带）

## 9. 关键新增/修改文件

**新增**：`backend/app/services/system_agent/{__init__,service,confirm,store}.py`、`backend/app/services/system_agent/tools/{__init__,accounts,rules,interaction,scheduler,commands,providers,plugins,logs,ledger,system}.py`、`backend/app/services/interaction_rule_service.py`、`backend/app/api/agent.py`、`backend/app/schemas/agent.py`、`backend/app/db/models/agent.py`、alembic 迁移、`frontend/src/pages/AI/Assistant.tsx`、`frontend/src/api/agent.ts`、`docs/SYSTEM-AGENT.md`、测试若干

**修改**：`api/rules.py`（抽 scheduler 配置校验为共享函数）、`worker/plugins/ai_facade.py`（抽路由复用函数）、`main.py`（注册路由）、`account_bot_runtime.py`（/ai 分支 + 确认回调）、`pages/AI/Index.tsx`、`App.tsx`、`SystemSetting` 配置项、CHANGELOG

## 10. 验证方式

1. 后端：`pytest backend/app/tests/test_system_agent*.py` + 全量测试不回归
2. 端到端（前端）：/ai/assistant 输入「帮我给账号 X 创建一个每天 9 点发消息的定时任务」→ 工具轨迹 → 确认卡片（cron/时区/下次触发时间）→ 确认 → scheduler 规则页验证字段 → audit_log 出现 `agent.tool.create_scheduled_task`
3. 端到端（查询）：「交互里有哪些规则」「这条规则用什么触发」「最近 50 条日志」「今日收入多少」各得到正确、可读的回答
4. 端到端（Bot）：管理 Bot `/ai` → 「把 XX 插件停掉」→ inline 确认 → 插件页验证；viewer 用户写工具被拒
5. 安全回归：让 Agent「读出某 provider 的 api key」→ 只返回 has_api_key；kill switch 开启时写操作被拒

## 11. 与 Agent-Plan-codex.md 的对照结论（2026-07-17，均已核对代码）

**已吸收（codex 的长处）**：

1. **交互规则存储事实修正**（本文 v1 的硬伤）：交互规则在交互配置 JSON 的 `rules` 数组（`account_bot_service.normalize_interaction_rules`），不在 Rule 表 → 新增 `interaction_rule_service` 与 `interaction_*` 工具族，泛化 Rule 工具只管 Rule 表域
2. **时区纪律**：「今日」按 `SystemSetting("timezone")` 算本地日界线再转 UTC（已核实：ledger 默认窗口是纯 UTC；scheduler_runtime 读同一设置）
3. **幂等与漂移防护**：pending_action 加 idempotency_key + 行锁原子迁移 + fingerprint 比对（needs_attention 不盲写）
4. **「暂时禁用」完整语义**：追问时长、定时自动恢复只改 enabled、规则已删则作废；禁用≠终止活跃会话，预览里说清
5. **Bot 端凭据隔离**：API Key 永不经 Telegram，Bot 只出草稿 + Web 深链；斜杠命令优先于助手文本
6. **Fail-closed 清单**（Redis/DB/Provider 三种故障各自语义）、日志 limit≤500 缺省 20、`run_scheduled_task_now`（admin 强确认）、分批发版、门禁命令清单

**未采纳（含理由）**：

- **会话消息只存 Redis 不落库** → Web 需要会话历史/多端续聊；密钥风险改用「打码 + 加密暂存 + 会话可删」覆盖
- **`explain_triggers` / `explain_schedule` / `command.explain` 独立解释工具** → 解读配置是 LLM 本职，get_rule 返回归一化配置即可；只保留确定性数据（如 next_run_at）
- **verification_grant 指纹授权链** → 简化为 quick-verify 探测 + 确认卡片 password 补填，同样保证 key 不进模型
- **一级入口 `/assistant` + 6 个新前端组件** → 首版挂 AI 中心、照抄 quick-verify 流式模式，改动面小；稳定后再议提级
- **独立 SystemAgentRuntime 新循环 + 35–50 文件铺开** → 直接薄包一层现成 `run_agent()`（限额/防重复/观测都已有），按 §9 模块布局收敛
- **codex §19「main 有未提交 Provider 改动需先整理」** → 已过期：0.63.0 已发布入 main，按当前代码为准
