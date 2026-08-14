# WP-B 插件文档族真实性审计

> 审计日期：2026-08-15。范围、方法和修改权限遵循 [2026-08-15-consistency-audit-plan.md](./2026-08-15-consistency-audit-plan.md) §4；本轮只改文档，不改代码。

## 结论摘要

本轮逐篇核验范围内 14 篇文档，重点覆盖 API 签名、ctx.messages 上下文矩阵、权限/能力名、错误码与数值限制、示例和交叉引用。发现并修复 2 项误导性事实错误、补充 1 项缺失能力说明；未发现过时路径、权限名或失效 Markdown 链接。PLUGIN-REMOTE.md 的 requires_platform_capabilities 声明章节按要求跳过内容复核，仅核查其交叉引用。

### 四档统计（本轮发现项）

“正确”表示抽查断言与当前代码相符；其余三列只计发现的问题项。

| 文档 | 正确 | 过时 | 误导 | 缺失 | 处理 |
| --- | --- | ---: | ---: | ---: | --- |
| PLUGIN-OVERVIEW.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| PLUGIN-QUICKSTART.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| PLUGIN-DEV-GUIDE.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| PLUGIN-API-REFERENCE.md | 通过 | 0 | 0 | 0 | 未发现事实错误；ctx.messages 的 apply 展示列入语义待裁定 |
| PLUGIN-CHEATSHEET.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| PLUGIN-RULES.md | — | 0 | 1 | 0 | 已修 CLI 调用形式 |
| PLUGIN-SAFETY.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| PLUGIN-AI.md | — | 0 | 1 | 0 | 已修 token 估算公式 |
| PLUGIN-HTTP.md | 通过 | 0 | 0 | 0 | 未发现事实错误；direct 权限措辞列入语义待裁定 |
| PLUGIN-DEVTOOLS.md | 通过 | 0 | 0 | 0 | CLI/replay 帮助与示例核验通过 |
| PLUGIN-REMOTE.md | 通过 | 0 | 0 | 0 | 只查声明章节交叉引用，未重审内容 |
| PLUGIN-WEBHOOK-QUICKSTART.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| PLATFORM-CAPABILITIES.md | 通过 | 0 | 0 | 0 | 未发现问题 |
| SYSTEM-AGENT.md | — | 0 | 0 | 1 | 已补动态插件只读工具 |
| **合计** | 11 篇通过 | **0** | **2** | **1** | **3 项事实性变更** |

## 逐篇问题清单与证据

### 1. PLUGIN-OVERVIEW.md

- 结论：正确；未发现过时、误导或缺失断言。Event Bus、on_event、能力依赖和入口链接与当前插件基类/manifest 约束一致。
- 回归测试：有，插件加载与能力声明由 backend/app/tests/test_plugin_loader.py、backend/app/tests/test_plugin_capability_requirements.py 覆盖；示例校验通过。

### 2. PLUGIN-QUICKSTART.md

- 结论：正确；简单模式、显式 manifest、账号启用步骤及 tp_plugin 入口均与当前 loader/CLI 一致。
- 证据：backend/scripts/tp_plugin.py:1311-1319 的 parser 以 new 为脚手架生成子命令；backend/app/tests/test_tp_plugin_cli.py 覆盖 CLI。
- 回归测试：有；validate-plugin-examples.py 通过。

### 3. PLUGIN-DEV-GUIDE.md

- 结论：正确；作为索引的内部链接和开发链路入口均可达，未发现独立事实错误。
- 回归测试：文档索引无专用测试；目标章节对应的 loader/示例测试通过。

### 4. PLUGIN-API-REFERENCE.md

- 结论：正确；ctx.messages 矩阵、facade 方法、HTTP/AI facade、Manifest agent_tools 约束和 Rich Message 限制与代码一致。
- 证据：backend/app/worker/plugins/loader.py:6091,6245 提供 apply；backend/app/worker/plugins/http_facade.py:214-263,383-384 实施 direct 双门禁、host 白名单和策略拒绝；backend/app/services/rich_message.py:19-22,144-147,216-219 定义 32768 字节、500 块、16 层、50 媒体、20 列限制；backend/app/services/remote_plugin_service.py:320-337 校验 ai_agent + capabilities.agent_tools。
- 回归测试：有，test_plugin_http_facade.py、插件安装安全测试、loader 测试覆盖；矩阵中的 apply 展示取舍见语义待裁定。

### 5. PLUGIN-CHEATSHEET.md

- 结论：正确；命令、权限速查和安全提示与 API 参考及当前 manifest schema 对齐。
- 回归测试：由 test_plugin_loader.py、test_tp_plugin_cli.py 和示例校验间接锁定。

### 6. PLUGIN-RULES.md

- 结论：误导 1 项，已修。
- 原文位置：原第 39 行把脚手架写成 tp_plugin session_game|command，读者可能照抄成不存在的 CLI 形式。
- 修正：改为 tp_plugin new <name> --profile session_game|command。
- 代码证据：backend/scripts/tp_plugin.py:6 的用法说明及 :1311-1319 的 new parser/--profile 参数；真实运行 tp_plugin new --help 验证。
- 回归测试：backend/app/tests/test_tp_plugin_cli.py 存在；本轮未改代码。

### 7. PLUGIN-SAFETY.md

- 结论：正确；权限边界、direct 双门禁、敏感日志和 Rich Message/消息限制与当前实现一致。
- 证据：backend/app/worker/plugins/http_facade.py:214-238；backend/app/services/rich_message.py:19-22；backend/app/tests/test_plugin_http_facade.py。
- 回归测试：有；external_http_bypass_proxy 的预留权限语义另列待裁定，不在本轮改动。

### 8. PLUGIN-AI.md

- 结论：误导 1 项，已修。
- 原文位置：原第 159 行把 token 估算写成 UTF-8 字节数 // 4，遗漏向上取整及 max_output_tokens 预留，可能导致插件作者低估配额预扣。
- 修正：明确 system/user 字节合计后按 (bytes + 3) // 4、至少 1，再加 max_output_tokens。
- 代码证据：backend/app/worker/plugins/ai_facade.py:1016-1021 的 _estimate_total_tokens()；调用处 :246-249、:572-578 将估算值交给 quota acquire。
- 回归测试：test_plugin_ai_facade.py、test_plugin_ai_quota.py 锁定 quota acquire/release 与超限语义，但没有对 (bytes + 3)//4 + max_output_tokens 做专门断言，建议后续补一条纯函数测试。

### 9. PLUGIN-HTTP.md

- 结论：正确；host 通配、SSRF/响应限长、默认代理和 direct 双门禁均与代码一致。
- 证据：backend/app/worker/plugins/http_facade.py:112-145,214-238,249-263,383-384；backend/app/tests/test_plugin_http_facade.py。
- 回归测试：有。

### 10. PLUGIN-DEVTOOLS.md

- 结论：正确；tp_plugin、tp_replay.py、check/register 流程和示例路径均可执行或可达。
- 证据：backend/scripts/tp_plugin.py:1311-1320；本轮真实运行 tp_plugin --help、各子命令 --help 与 tp_replay.py --help。
- 回归测试：有，test_tp_plugin_cli.py；示例校验通过。

### 11. PLUGIN-REMOTE.md

- 结论：正确；本轮只检查 requires_platform_capabilities 章节的内部/外部交叉引用，未重复复核声明章节事实；其它引用未发现断链。
- 回归测试：声明内容已有前轮核验；本轮无新增代码测试。

### 12. PLUGIN-WEBHOOK-QUICKSTART.md

- 结论：正确；路径、头白名单、64 KiB 限制、202 接受语义、限流和错误码与实现一致。
- 证据：backend/app/api/webhooks.py:38-41,323-355,417；对应 webhook 测试覆盖请求体/响应和限流。
- 回归测试：有。

### 13. PLATFORM-CAPABILITIES.md

- 结论：正确；能力开关状态、fail-closed、插件声明入口和相关代码链接均有效。
- 回归测试：由平台能力与插件 manifest 测试覆盖；交叉引用检查无失效链接。

### 14. SYSTEM-AGENT.md

- 结论：缺失 1 项，已修。代表性工具表此前没有动态插件只读工具，无法让插件作者发现已经落地的 plugin_{plugin_key}.{tool_name} 入口。
- 修正：新增动态工具行，载明声明条件、ai_agent 权限、账号启用门禁、Worker IPC、10 秒超时、只读拒绝和结果脱敏。
- 代码证据：backend/app/services/system_agent/plugin_tools.py:29-37,45-121,126-170,218-280；backend/app/worker/plugins/system_agent_tools.py；安装元数据约束见 backend/app/services/remote_plugin_service.py:320-337。
- 回归测试：backend/app/tests/test_system_agent_plugin_tools.py、test_plugin_install_security.py 覆盖声明解析、动态注册、脱敏、Worker IPC 和权限/能力门禁。

## 语义/取舍待裁定（本轮不改）

1. **ctx.messages 矩阵是否单列缓冲 facade 的 apply**：当前 PLUGIN-API-REFERENCE.md:274-287 的缓冲行列出各 helper，正文随后明确两种 facade 都提供 apply，并说明 apply 的返回与入口限制。是否把它移入矩阵属于信息架构/表达取舍，不是事实错误。
2. **external_http_bypass_proxy 与 direct 的权限契约措辞**：PLUGIN-SAFETY.md:44 称其为预留高危权限，而运行时 direct 实际检查 Manifest http.allow_direct 与账号 network_mode=direct（backend/app/worker/plugins/http_facade.py:214-238），权限名本身未参与 direct 门禁。是否将文档改成“历史/预留字段”或调整代码契约，属于设计裁定；本轮按权限不改代码也不改该措辞。

## 真实校验记录

- backend/.venv/bin/python scripts/validate-plugin-examples.py：通过。
- backend/.venv/bin/python backend/scripts/tp_plugin.py check examples/plugins/webhook_receiver：通过（仅有 send_message 权限草案警告）。
- tp_plugin 各子命令 --help 与 tp_replay.py --help：通过。
- Markdown 链接与 anchor：14 篇文档交叉引用检查未发现失效项。

## 修改边界

本轮只修改文档：本审计报告、PLUGIN-AI.md、PLUGIN-RULES.md、SYSTEM-AGENT.md 和 CHANGELOG.md；未修改后端、前端或示例代码。
