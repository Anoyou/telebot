# System Agent（系统助手）

平台级自然语言助手：通过 Web `/assistant` 与管理 Bot `/agent` 查询 TelePilot 已有能力。

计划与阶段定义见 `docs/Plan/Agent-Plan.md`。本文描述**当前已落地**的能力与运维边界。

## 阶段状态

| 阶段 | 版本目标 | 状态 |
| --- | --- | --- |
| 1 | `0.64.0` Web + Bot 只读助手 | 已实现 |
| 2 | `0.64.0` 核心写操作 + Action | 已实现 |
| 3 | `0.64.0` Provider/指令写 + 密钥 | 已实现 |
| 4 | `0.64.0` 使用驱动扩展 | 已实现首批：插件包/仓库、系统更新重启、auto 路由 |

当前能力：只读查询、核心写操作、Provider/指令与密钥、远程插件与仓库、系统更新/重启、AI 指令 auto 路由。

## 入口

| 渠道 | 路径/命令 |
| --- | --- |
| Web | 侧边栏「系统助手」→ `/assistant` |
| 管理 Bot | `/agent`、`/agent <问题>`、助手模式下的自由文本 |
| 配置 | `/assistant` 右上角「配置」；AI 中心提供「配置系统助手」链接 |
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
- 必须选择**真实声明支持 tools** 的固定 Provider/模型。
- 固定 Provider 作为首选；同一 Provider 内会先按首选模型、默认模型、其它已启用 Tools 模型静默 fallback。
- 同一模型遇到超时、网络错误、429 或 5xx 后会固定间隔 3 秒重试 5 次；5 次均失败后才尝试同 Provider 的下一 Tools 模型。Web 可用输入框右侧的停止按钮中断当前请求和重试等待。
- 跨 Provider 只使用 `fallback_provider_ids` 白名单中拥有 API Key 和 Tools 模型的候选；当前 Provider 内模型均失败时，Web 会先询问是否改用下一个候选，确认后才重试。
- `require_tool_approval=true` 时，Web 会在正式 Agent 调用前列出本轮路由到的工具；只有批准完整工具清单后才继续。该开关当前不阻断管理 Bot，Bot 写操作仍由 Action 二次确认保护。
- 上游把自身故障包装成 `400 Upstream request failed` 时按 Provider 故障处理；普通参数错误 400 仍直接失败，不会把错误请求扩散到其它 Provider。
- 某个备用 Provider 在本轮成功后，后续 Agent 步骤优先沿用它，避免反复撞击已知不稳定的主 Provider。
- 无可用 tools 模型时，Web/Bot 均提示到 AI 中心配置。

## 架构要点

```text
Web /assistant  ──NDJSON──┐
                          ├── SystemAgentService → Runtime → ToolRegistry
管理 Bot /agent ──────────┘                              │
                                              按领域渐进披露工具
                                                         │
                                                   现有业务 service
```

硬规则：

- 工具只调用允许列表内的现有 service；禁止万能 SQL/Shell/HTTP/文件工具。插件与系统运维只能走明确注册的危险工具并二次确认。
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

失败、超时和中断轮次不会写入摘要，也不会进入下一轮模型历史。清空会话消息时同步清空摘要和结构化记忆。

### 工具渐进披露

- 工具按前缀划分为账号、交互规则、Scheduler、日志、台账、Provider、插件、系统运维等领域。
- 常见中文/英文请求先本地关键词路由；普通问答、帮助和闲聊发送 **0 个工具定义**。
- 本地无法判断且存在操作意图时，使用轻量模型路由器，最多选择 3 个领域。
- 模型路由失败时优先复用结构化记忆中的最近领域，否则安全降级为不带工具的直接回答。
- 主 Agent 只接收所选领域的工具 Schema，不再每轮固定携带全部已注册工具。

## 数据表

- `system_agent_session`：会话（web/bot、账号上下文、标题、状态、滚动摘要、结构化工作记忆）
- `system_agent_message`：消息（user/assistant/tool/system_event，落库为打码内容；包含运行状态、错误码、错误信息与重试次数）
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
- `model_attempt` / `retry_scheduled` / `model_exhausted`（当前模型尝试、固定间隔重试和模型耗尽）
- `heartbeat`（等待上游期间每 10 秒保持 NDJSON 连接并报告当前 Provider/模型）
- `route_selected`（所选领域、来源、工具数量）
- `tool_started` / `tool_finished`
- `action_proposed`
- `assistant_message`
- `error`
- `done`

连接中断后通过会话消息 API 读取最终状态，不依赖重放旧流。Web 会实时展示当前 Provider/模型、重试进度和“正在调用某工具”，不再只显示笼统的“思考中”。

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
| Web 开启工具前置批准 | 保存本轮工具清单并显示批准按钮；未批准前不把工具定义交给正式 Agent |
| 当前 Provider 内所有 Tools 模型失败 | 返回候选 Provider；Web 用户确认后用原消息重试，不重复写入历史 |
| 工具异常 | 说明业务是否变化 |
| Provider 验证失败 | 保持待确认，清除无效密钥，要求重输 |
| Redis 不可用 | Web 仍可用；Bot 助手模式/Inline 确认可能不可用 |
| NDJSON 中断 | 刷新后重读会话消息与 Action |
| Agent 本轮失败 | 消息标记 failed，不进入后续上下文；Web 可直接重试原轮 |
| PWA 切后台或网络断开 | 服务端把本轮标记为可重试失败；超过 15 分钟的历史 pending 会自动修复，避免误回收仍在 10 分钟运行窗口内的请求 |
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
