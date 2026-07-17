# System Agent（系统助手）

平台级自然语言助手：通过 Web `/assistant` 与管理 Bot `/agent` 查询 TelePilot 已有能力。

计划与阶段定义见 `docs/Plan/Agent-Plan.md`。本文描述**当前已落地**的能力与运维边界。

## 阶段状态

| 阶段 | 版本目标 | 状态 |
| --- | --- | --- |
| 1 | `0.64.0` Web + Bot 只读助手 | 已实现（本分支，待发版） |
| 2 | `0.65.0` 核心写操作 + Action | 已实现（本分支，待发版） |
| 3 | `0.66.0` Provider/指令写 + 密钥 | 开发中（本分支） |
| 4 | 真实使用驱动扩展 | backlog |

当前能力：只读查询、核心写操作（Web 卡片 + Bot Inline 确认）、Provider/指令写工具骨架，以及聊天密钥抽取与 Action 密文暂存。

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
  "max_steps": 8,
  "max_tool_calls": 24,
  "session_token_limit": 16384
}
```

规则：

- 默认关闭；需管理员显式启用。
- 必须选择**真实声明支持 tools** 的固定 Provider/模型。
- 固定 Provider 不可用时直接失败，不自动切换其他 Provider。
- 无可用 tools 模型时，Web/Bot 均提示到 AI 中心配置。

## 架构要点

```text
Web /assistant  ──NDJSON──┐
                          ├── SystemAgentService → Runtime → ToolRegistry
管理 Bot /agent ──────────┘                              │
                                                    只读 handlers
                                                         │
                                                   现有业务 service
```

硬规则：

- 工具只调用现有 service，禁止任意 SQL/Shell/HTTP/文件。
- handler 禁止自建 `AsyncSessionLocal`、禁止 `commit/rollback`。
- 业务 service 不依赖 System Agent。
- 查询必须走工具，禁止根据聊天记忆编造状态。

## 数据表

- `system_agent_session`：会话（web/bot、账号上下文、标题、状态）
- `system_agent_message`：消息（user/assistant/tool/system_event，落库为打码内容）
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

写工具只产生 `pending` Action，用户确认后由 `ActionExecutor` 统一事务执行。
Web 用内联卡片确认；Bot 用 Inline 按钮（`ab:{aid}:confirm|cancel:agent:{nonce}`）。

## NDJSON 事件

`POST /api/system-agent/sessions/{id}/messages/stream` 返回 `application/x-ndjson`：

- `run_started`
- `tool_started` / `tool_finished`
- `action_proposed`
- `assistant_message`
- `error`
- `done`

连接中断后通过会话消息 API 读取最终状态，不依赖重放旧流。

## Bot 命令

| 命令 | 行为 |
| --- | --- |
| `/agent` | 进入助手模式并显示帮助 |
| `/agent <自然语言>` | 直接提问 |
| `/agent new` | 新建会话（归档旧 active） |
| `/agent clear` | 删除当前会话 |
| `/agent exit` | 退出助手模式（保留历史） |

助手模式标记在 Redis，TTL 30 分钟滚动刷新。既有斜杠命令（`/status`、`/rules` 等）始终优先。

## 密钥与隐私（阶段 1）

阶段 1 只读，不主动收集 API Key。落库消息仍走基础打码（`redactor`）。阶段 3 才会支持聊天粘贴 Key、Action 密文暂存与 rekey 覆盖。

## 故障语义（摘录）

| 情况 | 用户可见 |
| --- | --- |
| 未启用 / 无 tools Provider | 明确错误 + AI 中心入口 |
| 工具异常 | 说明业务未变化（只读） |
| Redis 不可用 | Web 仍可用；Bot 助手模式可能不可用 |
| NDJSON 中断 | 刷新后重读会话消息 |

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
