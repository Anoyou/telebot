# System Agent（系统助手）

平台级自然语言助手：通过 Web 悬浮助手与管理 Bot `/agent` 查询 TelePilot 已有能力。

本文只描述**当前已落地**的能力、数据流与运维边界；历史实施计划已清理，代码和本文共同作为维护依据。

## 当前能力

系统助手已覆盖只读查询、核心写操作与 Action 确认、Provider/指令与密钥、远程插件与仓库、系统更新/重启、AI 指令 auto 路由，并通过三种 Provider 原生 SSE 向 Web 与管理 Bot 提供真实增量。版本历史和阶段归属只记录在 `CHANGELOG.md`，本文不再绑定过期版本目标。

## 入口

| 渠道 | 路径/命令 |
| --- | --- |
| Web | 任意工作台页面右下角「系统助手」悬浮球 |
| 管理 Bot | `/agent`、`/agent <问题>`、助手模式下的自由文本 |
| 配置 | 悬浮助手面板中的「配置」；AI 中心提供「配置系统助手」快捷入口 |
| API | `/api/system-agent/*` |

## 配置

`SystemSetting` 键：`system_agent_config`

```json
{
  "enabled": false,
  "provider_id": null,
  "model": null,
  "fallback_provider_ids": [],
  "require_tool_approval": false,
  "max_steps": 8,
  "max_tool_calls": 24,
  "session_token_limit": 16384
}
```

规则：

- 默认关闭；需管理员显式启用。
- 必须选择支持 tools 的固定 Provider/模型。声明只用于候选预筛选；模型首次进入 Agent 前还会接受一次真实的强制工具调用探测，只有返回指定工具及正确参数才会放行。
- 工具能力探测结果保存在 `SystemSetting.system_agent_model_capability_cache`：支持结果缓存 7 天，明确不支持缓存 1 天，429/5xx/网络故障只按暂时不可用缓存 5 分钟。明确不支持的 fallback 会被排除；暂时不可用的 fallback 仍保留为待确认候选，避免一次网络故障把它永久从切换链路中移除。
- 固定 Provider 作为首选；同一 Provider 内会先按首选模型、默认模型、其它已启用 Tools 模型静默 fallback。
- 同一模型遇到超时、网络错误、429 或 5xx 后会固定间隔 3 秒重试 5 次；5 次均失败后才尝试同 Provider 的下一 Tools 模型。Web 可用输入框右侧的停止按钮中断当前请求和重试等待。
- 跨 Provider 只使用 `fallback_provider_ids` 白名单中拥有 API Key 和 Tools 模型的候选；当前 Provider 内模型均失败时，Web 会先询问是否改用下一个候选，确认后才重试。
- `require_tool_approval=true` 时，Web 会先让模型理解需求；只有模型实际产出工具调用时，才用中文能力名称列出本批待执行工具。批准门禁位于整批 handler 执行之前，普通回答无需批准。该开关当前不阻断管理 Bot，Bot 写操作仍由 Action 二次确认保护。
- 上游把自身故障包装成 `400 Upstream request failed` 时按 Provider 故障处理；普通参数错误 400 仍直接失败，不会把错误请求扩散到其它 Provider。
- 某个备用 Provider 在本轮成功后，后续 Agent 步骤优先沿用它，避免反复撞击已知不稳定的主 Provider。
- 无可用 tools 模型时，Web/Bot 均提示到 AI 中心配置。

## 架构要点

```text
Web 悬浮助手 → Durable Run / 可续接事件 ──┐
                                            ├→ Turn Resolver → Skill Router
管理 Bot /agent → SystemAgentService ───────┘                 │
                                                   Runtime（最多 8 个工具）
                                                              │
                          原生 LLM SSE → assistant_delta ─────┤
                                                   ToolRegistry → 现有业务 service
```

硬规则：

- 工具只调用允许列表内的现有 service；禁止万能 SQL/Shell/HTTP/文件工具。插件与系统运维只能走明确注册的危险工具并二次确认。
- 工具注册表使用可读的内部名称（如 `interaction.list_rules`）；Provider 适配器在协议边界映射为只含字母、数字、下划线或连字符的稳定别名，并在模型返回后回译。Action、审批、日志和权限判断从不使用协议别名。
- handler 禁止自建 `AsyncSessionLocal`、禁止 `commit/rollback`。
- 业务 service 不依赖 System Agent。
- 查询必须走工具，禁止根据聊天记忆编造状态。
- 确认、拒绝和密钥补填共享 Action 行锁；预检期间密钥变化时保持 pending，必须再次确认。
- 会触发文件、Worker 或系统进程的操作在 Action 提交后执行，失败记录为 `runtime_sync_status=failed` 并允许重试。

### 上下文与记忆

System Agent 使用三层上下文，避免每轮回放全部原始消息：

1. **短期上下文**：仅携带最近 8 条成功/已完成消息，并继续受会话 token 预算约束。
2. **滚动摘要**：`memory_summary` 只吸收移出短期窗口的旧成功轮次，避免与最近原始消息重复发送，并控制在有限长度内。
3. **结构化工作记忆**：`memory_state` 保存最近工具领域、用户目标、结果、工具摘要与账号上下文，用于“把它停掉”“继续刚才那个”等指代请求。

失败、超时和中断轮次不会写入摘要，也不会进入下一轮模型历史。服务端只在 `memory_state.failed_turn` 保存打码后的失败目标、消息 ID 与错误码，使“重试 / 再试一次 / 继续刚才的”能确定性复用原失败消息；失败的模型输出和工具输出仍不进入上下文。清空会话消息时同步清空摘要和结构化记忆。

`system_agent_run_event` 是一次运行的持久事实记录；滚动摘要、短期历史和结构化工作记忆只是给下一次推理使用的上下文投影。裁剪或压缩上下文不会改写已经落库的模型重试、工具调用、Action 和终止事件，刷新后仍按事件游标恢复 UI。

### 工具渐进披露

- 工具按前缀划分为账号、交互规则、Scheduler、日志、台账、Provider、插件、系统运维等领域。
- 常见中文/英文请求先本地关键词路由；普通问答、帮助和闲聊发送 **0 个工具定义**。
- 本地无法判断且存在操作意图时，使用轻量模型路由器，最多选择 3 个领域。
- 模型路由失败时优先复用结构化记忆中的最近领域，否则安全降级为不带工具的直接回答。
- 主 Agent 只接收所选领域的工具 Schema，不再每轮固定携带全部已注册工具。
- 内置领域技能为 `interaction`、`scheduler`、`ai-config`、`plugins`、`diagnostics`。每轮最多加载 2 个技能、暴露 8 个工具；技能只补充处理流程和澄清规则，不能扩大 ToolRoute 权限，也不复制工具 Schema 或业务校验。

## 数据表

- `system_agent_session`：会话（web/bot、账号上下文、标题、状态、滚动摘要、结构化工作记忆）
- `system_agent_message`：消息（user/assistant/tool/system_event，落库为打码内容；包含运行状态、错误码、错误信息与重试次数）
- `system_agent_run`：Web 后台运行句柄、幂等请求标识、终态与取消状态
- `system_agent_run_event`：按 Run 单调序号持久化的 NDJSON 事件，可从游标续接
- `system_agent_action`：写操作预览与确认（pending → executing → executed/failed/rejected/expired）

## 已注册只读工具

| 工具 | 说明 |
| --- | --- |
| `system.get_context` | 时区、前缀、开关、版本、会话上下文 |
| `system.get_health` | DB/Redis/账号/Provider 就绪 |
| `accounts.list` / `accounts.get` | 账号列表与详情 |
| `interaction.list_rules` / `get_rule` / `list_active_sessions` | 交互规则与活跃会话（账号 JSON，非 Rule 表） |
| `rules.list` / `rules.get` | 通用 Rule（拒绝 `feature_key=interaction`） |
| `scheduler.list` / `scheduler.get` | 定时任务与 `next_run_at` |
| `providers.list` | 脱敏 Provider 与 tools 模型 |
| `commands.list` | 自定义指令与启用账号 |
| `features.get_account_status` | 账号功能/插件启停矩阵 |
| `logs.recent` / `search_errors` / `get_event_detail` | 运行日志（默认 20，最大 500） |
| `ledger.summary` / `ledger.list` | 台账汇总与明细；「今日」按系统时区日界线 |
| `accounts.set_paused` / `restart_worker` | 暂停恢复 / 重启 Worker（危险） |
| `rules.save` / `set_enabled` / `delete` | 通用 Rule 写操作 |
| `interaction.save_rule` / `set_enabled` / `delete_rule` | 交互规则写操作 |
| `scheduler.save` / `set_enabled` / `delete` / `execute_now` | 定时任务写操作 |
| `features.set_enabled` | 账号功能/插件启停 |
| `providers.save` / `delete` / `verify` | Provider 创建更新删除与本地可调用性检查 |
| `commands.save` / `delete` / `set_enabled_for_accounts` | 自定义指令与账号启用 |
| `plugins.list_installed` / `get` / `check_updates` | 远程插件包查询 |
| `plugins.install` / `update` / `uninstall` / `set_package_enabled` | 安装包装卸更新与全局启停（危险项需确认） |
| `plugin_repos.list` / `list_plugins` / `list_official` | 远程/官方仓库浏览 |
| `plugin_repos.create` / `delete` / `install_plugin` | 仓库维护与从仓库安装 |
| `system.check_update` / `apply_update` / `restart` | 系统更新检查/应用/重启 |
| `routing.list_ai_commands` / `preview` / `set_command_mode` | AI 指令 fixed/auto 路由 |

写工具只产生 `pending` Action，用户确认后由 `ActionExecutor` 统一事务执行。
Web 用内联卡片确认；Bot 用 Inline 按钮（`ab:{aid}:confirm|cancel:agent:{nonce}`）。

## NDJSON 事件

`POST /api/system-agent/sessions/{id}/messages/stream` 返回 `application/x-ndjson`：

- `run_started`
- `provider_selected`（当前 Provider 名称、模型与 configured/model_fallback/provider_fallback 原因）
- `model_capability_check`（首次使用或缓存过期时验证模型的真实工具调用能力）
- `model_attempt` / `retry_scheduled` / `model_exhausted`（当前模型尝试、固定间隔重试和模型耗尽）
- `heartbeat`（等待上游期间每 10 秒保持 NDJSON 连接并报告当前 Provider/模型）
- `route_selected`（所选领域、来源、工具数量）
- `skill_selected`（本轮加载的领域技能、理解摘要与工具数量）
- `tool_started` / `tool_finished`
- `action_proposed`
- `assistant_delta`：上游模型真实返回的文本增量；不会把完整响应拆字模拟流式
- `assistant_delta_reset`：模型确认工具调用后，撤销工具前的临时自然语言草稿
- `assistant_message`
- `error`
- `done`

Web 首先通过 `POST /api/system-agent/sessions/{session_id}/runs` 创建持久 Run，再通过 `GET /api/system-agent/runs/{run_id}/stream?after_seq=N` 订阅事件；重试使用 `POST /api/system-agent/sessions/{session_id}/messages/{message_id}/retry/runs`。刷新、切换会话或 PWA 暂时离线不会取消后台执行，前端会保存 Run ID 和最后游标并自动补收缺失事件；`POST /api/system-agent/runs/{run_id}/cancel` 才会明确终止。

旧的 `messages/stream` 与 `retry/stream` 接口继续兼容，但内部同样创建持久 Run。Web 会实时展示理解到的领域技能、当前 Provider/模型、重试进度和“正在调用某工具”，不再只显示笼统的“思考中”。

`assistant_message` 始终是最终权威全文，用于持久化和重连对账。OpenAI Chat Completions、OpenAI Responses 与 Anthropic Messages 均直接消费上游 SSE；工具参数允许跨多个 SSE 事件拼接。若兼容上游忽略 `stream=true` 并返回普通 JSON，只发送最终 `assistant_message`，同时在 usage 和 UI 标记“完整响应”，绝不伪造 delta。已经向客户端发送任何文本后若上游中断，本轮立即失败，不自动换模型或 Provider，避免把两次回答拼接成一条。

预算门禁在结构化调用与流式调用中使用同一 scope 语义：高价 Provider 的 `premium_daily` 到限时允许在尚未输出文本前继续尝试更便宜 Provider；账号级请求数、每日 token 或预算后端不可用属于整条请求的终止条件。上游没有返回 usage 时按请求预估值保守结算；已输出部分文本、取消或异常终止同样不会按“未调用”释放费用。

每个 `assistant_delta` 在进入 Durable Run 前先经过跨分块缓冲脱敏，覆盖已知聊天密钥、Authorization、Telegram Bot Token 及常见 `sk-`、`xai-`、`gsk_`、`AIza` Provider Key。最终 `assistant_message`、工具摘要和错误事件仍会再次做完整对象脱敏，防止增量路径和历史路径语义分叉。

前端使用共享 NDJSON 增量解析器处理任意网络分块和 UTF-8 边界，并以 `requestAnimationFrame` 合并已经抵达的 delta，降低渲染频率；这只是批量提交 React 状态，不是打字机效果。Run 事件按 `seq` 去重，重连不会重复追加文本。

失败用户消息会显示错误原因与「重试本轮」按钮；需要跨 Provider 或工具批准时，会显示对应确认按钮。重试接口为：

`POST /api/system-agent/sessions/{session_id}/messages/{message_id}/retry/stream`

重试复用原用户消息，不新增重复历史；同一消息的 `retry_count` 递增。若原消息含 API Key，落库内容已打码，重试时模型会要求重新发送或在 Action 卡片补填，不能恢复旧明文。

System Agent 在自己的运行入口注册 LLM usage 持久化回调，路由调用、失败模型、同 Provider fallback 与最终成功调用都会进入 AI 页面「近期调用」，来源分别为 `system_agent_router` 或 `system_agent`。

## Bot 命令

| 命令 | 行为 |
| --- | --- |
| `/agent` | 进入助手模式并显示帮助 |
| `/agent <自然语言>` | 直接提问 |
| `/agent new` | 新建会话（归档旧 active） |
| `/agent clear` | 删除当前会话 |
| `/agent exit` | 退出助手模式（保留历史） |

助手模式标记在 Redis，TTL 30 分钟滚动刷新。既有斜杠命令（`/status`、`/rules` 等）始终优先。

## 密钥与隐私（阶段 3）

- Web / Bot 普通聊天允许粘贴 API Key；原始文本进入当轮实际调用的上游模型请求，首选 Provider 故障时也可能发送给 fallback Provider。
- 落库前对用户消息、助手回复和工具摘要做密钥替换 + 基础打码。
- 工具敏感参数移入 `secret_payload_enc`（Fernet），普通 `arguments` 仅 `has_api_key=true`。
- 执行 / 拒绝 / 过期后清除密文；`rekey` 覆盖该字段。
- Provider 保存/验证：真实 quick verify 失败时 Action **保持 pending**、清除无效密钥，允许重新输入。
- Provider 预检期间若用户更新密钥，本次确认不会继续执行，需使用新密钥再次确认。
- Bot 存在待确认密钥 Action 时，单独发送的纯 Token 会优先加密绑定到最近匹配的 Action。
- 当轮未消费的 Key 不额外缓存；后续上下文只有掩码时必须要求用户重发。

## 故障语义（摘录）

| 情况 | 用户可见 |
| --- | --- |
| 未启用 / 无 tools Provider | 明确错误 + AI 中心入口 |
| 首选 Provider 返回 429/5xx/超时 | 当前模型每 3 秒重试一次，共 5 次；随后尝试同 Provider 的其它 Tools 模型，跨 Provider 前要求确认 |
| Web 开启工具前置批准 | 模型先理解需求并选择具体工具；有真实工具调用时才显示中文批准项，批准前不执行任何 handler |
| 当前 Provider 内所有 Tools 模型失败 | 返回候选 Provider；Web 用户确认后用原消息重试，不重复写入历史 |
| 工具异常 | 说明业务是否变化 |
| Provider 验证失败 | 保持待确认，清除无效密钥，要求重输 |
| Redis 不可用 | Web 仍可用；Bot 助手模式/Inline 确认可能不可用 |
| NDJSON 中断 | 后台 Run 继续执行；Web 按最后事件游标自动重连并补收 |
| 上游返回普通 JSON | 完整结果照常展示并标记“完整响应”，不模拟增量 |
| 已显示部分文本后上游中断 | 立即失败且保守结算预算，不重试或切 Provider，避免重复内容 |
| Agent 本轮失败 | 消息标记 failed，不进入后续上下文；Web 可直接重试原轮 |
| 用户发送“重试 / 继续刚才的” | 有失败锚点时原子复用最近失败用户消息，不新增重复消息；没有锚点时按普通消息处理 |
| PWA 切后台或网络断开 | Run 与订阅连接解耦，恢复页面后自动续接；只有服务进程重启才会把未完成 Run 标记为可重试失败 |
| 系统更新/重启中断连接 | Action 已先提交；刷新后以 Action 与运行时同步状态为准 |

## 扩展新工具

1. 确认已有稳定 service。
2. 定义 Schema、角色、渠道、风险。
3. 读工具返回有限结构化事实；写工具实现 preview + execute（阶段 2+）。
4. 注册到 `tools/`，不改主循环。
5. 补 Schema/事务/领域测试。
6. 更新本文能力矩阵。

禁止：`explain_*` 工具、handler 自建会话/commit、调用本项目 Web API、返回 ORM/密钥/无界日志、万能 SQL/Shell 工具。

## 关闭与回滚

- 关闭 `system_agent_config.enabled` 即可停止新请求；历史会话仍可读。
- Agent 未改动业务表语义；阶段 1 仅会话/消息表。
- 关闭后普通 TelePilot 功能与已有业务对象不受影响。
