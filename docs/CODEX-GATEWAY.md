# Codex 客户端兼容模式（Gateway）

TelePilot 可以让单个 LLM Provider 选择「标准 API 直连」或「Codex 客户端兼容模式（Gateway）」。Gateway 是最接近真实 Codex Responses 客户端公开请求契约的内置调用方式：由 Web 进程按需管理独立 Go 子进程，通过 Unix Socket 补齐动态会话身份并转发 Responses 请求。它不是第二个 Agent，也不改变 System Agent、插件 AI、预算、重试或 fallback 的决策归属。

## Provider 配置

在「AI → 模型提供商」新建或编辑 Provider：

1. 「标准 API 直连」保持原有请求路径，继续使用 Provider 的协议档案、客户端身份、代理和兼容请求头。
2. 「Codex 客户端兼容模式（Gateway）」固定使用 Responses API 与 Codex Responses 档案，身份由 Gateway 管理。
3. direct 与 Gateway 之间切换不会清空 API Key、代理、兼容请求头或已保存的客户端身份；切回 direct 后恢复原协议、联网协议和身份配置。LLM Provider 代理仅支持 HTTP/HTTPS/SOCKS5；被 Provider 引用的代理不能改成 MTProxy。
4. Gateway Provider 必须配置 API Key；Base URL 留空时会物化当前服务类型的默认地址。新建或编辑表单可把当前未保存参数临时注入 Gateway，用于读取模型列表和快速验证；临时快照会与已提交 Provider 串行同步，请求结束后立即恢复数据库中的已提交快照。
5. Gateway 只支持文本、图片输入与工具调用的 Responses 路径。Chat Completions、Anthropic Messages、语音转写和 `/images/generations` 图片生成必须使用 direct Provider。

模型测活的成功与失败结果都可展开“临时配置并再次测试”。临时选择 Gateway 时固定使用 Responses 且身份由 Gateway 管理；这些覆盖只作用于当次重试，不写回 Provider。

模型提供商页的“Gateway 管理”入口展示进程状态、模块版本、协议版本、构建与上游提交、Codex 客户端版本、Provider 数和最近错误。这里可以检测最新 Codex 版本、显式应用检测结果或恢复 TelePilot 内置版本；应用或恢复后会热同步模型列表、模型测活和 Agent 的后续调用，无需重启 Web。版本检测只读取候选版本，不会自动应用，也不代表请求契约已经完成兼容审查。

Gateway 不提供 Base URL、请求头、Socket、超时或并发等独立用户配置：Provider 接入参数仍在 Provider 表单维护，二进制路径与 Unix Socket 属于部署配置，其余传输契约由 TelePilot 随版本统一维护。

Gateway Provider 保存前会检查内置二进制和协议兼容性。Gateway 不可用时保存失败，不会在同一 Provider 内静默改走 direct。

Provider 与其引用代理的 Web/System Agent 写入共享进程锁和 PostgreSQL 事务 advisory lock：候选快照必须在数据库 commit 前同步；commit、取消或同步失败时会从已提交数据库快照补偿恢复。同步结果不确定时 Gateway 子进程会先停止并后台重试，避免继续使用未提交的路由或凭据。

### Gateway 管理的 Codex 请求身份

Gateway 不是把普通请求只换一个 UA。它依据 TelePilot 传入的 Provider 作用域伪名、会话、Run、Turn 与序号，为每个请求生成并同步以下公开传输契约：

- 稳定且 Provider 隔离的 `prompt_cache_key`、`Session_id`、session/thread/request ID；
- installation、session、thread、turn、window 对应的 `client_metadata`；
- 同一份 `X-Codex-Turn-Metadata` 与 `X-Codex-Window-Id` 兼容投影；
- 固定审查基线的 Codex `User-Agent`、`Originator` 与 `Version`。

身份字段在 Provider 兼容请求头之后由 Gateway 强制覆盖，避免自定义头拆散同一会话身份。TelePilot 原始账号、Telegram、Agent 会话 ID 不会出站；Gateway 收到的是已按 Provider 作用域 HMAC 伪名化的值，并再次转换为稳定 UUID。实现与测试基线见 `gateway/UPSTREAM.md`。

这仍不等于导入官方账号：Gateway 不生成 OAuth、ChatGPT Account ID、Cookie、设备证明、attestation 或 Agent Identity。若上游要求其中任一凭据，系统会明确报告 `official_account_required`，不会伪造或降级绕过。

## 健康状态

「系统状态」展示三种稳定状态：

| 状态 | 含义 | 处理 |
|---|---|---|
| `not_required` | 当前没有 Provider 选择 Gateway | 无需操作，direct 路径不启动 Gateway |
| `ready` | 子进程、Unix Socket、协议握手和 Provider 快照均就绪 | 可正常使用 |
| `degraded` | 二进制缺失、启动失败、协议不兼容、配置同步失败或子进程退出 | 查看卡片错误和 Web 日志；direct/Web/Worker 仍应正常 |

测活结果与「近期调用」展示实际 backend。Gateway 调用还会记录 Gateway 版本、request ID 和失败阶段，用于关联诊断；历史记录始终以当次真实调用为准，不读取 Provider 当前配置反推。

## 错误与排查

- `gateway_unavailable`：Gateway 未启动、Socket 不可达、协议不兼容或配置未就绪。先看系统状态，再确认运行镜像包含 `/usr/local/bin/telepilot-gateway`。
- `gateway_overloaded`：Gateway 全局或单 Provider 并发上限已满。等待当前请求结束后重试；Gateway 不会无限排队。
- `client_rejected`：上游拒绝当前客户端。Gateway 已补齐公开 Codex 请求契约；若仍出现该错误，先核对 Provider 是否确实支持 API-Key 形式的 Codex Responses，再检查固定上游基线是否需要升级。
- `official_account_required`：上游明确要求 OAuth / ChatGPT 账号等官方账号运行时；它不等同于 API Key 错误，也不由 Gateway 伪造。
- `quota_exhausted`、`rate_limited`、`upstream_error`、`timeout`：由 TelePilot runtime 按现有规则决定重试或切换其它 Provider。Gateway 自己不做语义重试。

错误诊断优先展示上游返回的结构化事实，包括 `upstream_status_code`、`upstream_error_code`、`upstream_error_message`、`upstream_error_detail`、`upstream_request_id` 和 `client_request_id`。即使中转站把真实上游 400 包装成外层 502，TelePilot 也应显示真实上游 400 及其错误详情；只有没有更具体上游事实且有效状态确实为 5xx 时，才提示“临时故障可重试”。

TelePilot Request ID、Gateway Request ID、上游 Request ID 与 Client Request ID 属于不同链路标识，不得互相冒充。排查多级中转时，先用 TelePilot Request ID 找到本次调用，再按界面或日志保留的上游 Request ID 查询中转站的结构化错误记录；不要根据外层包装文案反推真实上游状态。

若 Gateway Provider 失败，TelePilot 可以按配置切换到另一个 Provider；不会在同一个 Provider 内绕过 Gateway 偷跑 direct。流已经输出文本后不会切换，避免拼接两次回答。

## 部署、升级与回滚

生产 Web 镜像使用多阶段构建内置静态 Gateway 二进制。Gateway 与 Web 镜像原子升级和回滚，不新增 Compose service、端口、volume 或用户安装步骤。服务器只需按原方式拉取并重建或更新 TelePilot。

生产镜像只从 Go builder 复制 `/usr/local/bin/telepilot-gateway`、License 和第三方声明；runtime 不包含 Go 工具链、Cockpit、CLIProxyAPI 管理面或额外 Gateway 配置文件。不要在服务器另装 Gateway、另起 Compose service、发布 Socket 端口或把 Socket bind-mount 到宿主机。

开发入口：

```bash
make gateway-build
make gateway-test
make gateway-run
```

`gateway-run` 默认监听仓库 `.run/gateway.sock`。本机没有 Go 时，普通 direct 开发和测试仍可继续；只有 Gateway 专项命令会明确失败并提示安装 Go 1.23 或更高版本。

执行过 `make gateway-build` 后，`make backend` 和 `make up` 会自动把本地二进制及 `.run/gateway.sock` 传给 Runtime Manager；无需把开发产物复制到 `/usr/local/bin` 或创建 `/run/telepilot`。

Gateway 当前协议版本为 `2`。后端与二进制协议版本不一致时会失败关闭，不能通过忽略未知字段继续运行。

### 内部开发契约（不是公网 API）

Gateway 只在 Unix Socket 上提供内部 HTTP，不能加入 FastAPI OpenAPI、反向代理或第三方插件契约：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /healthz` | 只证明 Go 进程存活 |
| `GET /readyz` | 已有完整 Provider 快照并可精确路由 |
| `GET /version` | 返回 Gateway SemVer、`gateway_protocol_version`、固定上游基线和构建 commit |
| `PUT /internal/v1/config` | 接收完整内存快照；schema 1、protocol 2，revision 必须单调递增，未知字段或任一 Provider 非法时整批拒绝 |
| `GET /internal/v1/config/status` | 返回 ready、revision、Provider 数、同步时间和脱敏错误 |
| `POST /v1/responses` | Responses SSE/非流式、文本与图片输入、reasoning、工具调用与取消传输 |
| `GET /v1/models` | 按指定 Provider 的候选端点读取模型列表 |

数据面至少携带 `X-TelePilot-Provider-ID`、`X-TelePilot-Request-Scope` 和 `X-TelePilot-Request-ID`；Agent 调用还可携带伪名化 session/run/turn 元数据。Gateway 根据 Provider ID + 用户模型名精确选路，由内部路由名隔离同名模型，转发前删除所有 `X-TelePilot-*` 头并恢复上游模型名。调用方不得自行构造这些头绕过 Client Builder。

错误统一使用顶层 `error` 对象，对象字段包括 `code`、`message`、`retryable`、`request_id` 和 `gateway_stage`；响应头可回传脱敏的 Gateway version、request ID 和 stage。新增字段或错误码时，应先改 `gateway/contract`、Go 契约测试和 Python client，再同步 usage/OpenAPI 可见投影；不能只改一端。

升级和普通回滚跟随 Web 镜像完成。回滚到不认识 `execution_backend` 的旧版前，先把所有 Gateway Provider 切回 direct；若跨越新增该字段的数据库迁移，还必须使用迁移前备份恢复数据库，不能只切代码或镜像。生产人工恢复步骤见 [公网部署指南](./DEPLOY-PUBLIC.md#人工回滚)。

## 安全边界

- Provider API Key 只通过目录权限 `0700`、Socket 权限 `0600` 的 Unix Socket 控制面进入 Gateway 内存，不写 Gateway 配置文件。Socket 只供 Web 容器内同一运行用户访问，与 updater 挂载的宿主 Docker socket 无关。
- Gateway 不记录 Base URL、请求正文、响应正文、Authorization、API Key、兼容请求头值或代理凭据；未选择 Provider 代理时不会继承进程的 `HTTP_PROXY` / `HTTPS_PROXY`。
- TelePilot 内部 `X-TelePilot-*` 请求头只用于生成 Provider 隔离的 Codex 会话身份，并在 Gateway 边界剥离；用户不能覆盖系统鉴权头、Gateway 内部头或 Codex 身份头。
- 上游重定向不会被自动跟随，避免自定义认证头跨 origin 泄露；上游错误在进入日志、API、usage 或 RunTrace 前脱敏。
- Gateway 不读取、导入、存储或同步 Codex OAuth、账号身份、设备证明或 `~/.codex/auth.json`。
- Provider 删除、变更或 Gateway 终止时，旧路由和凭据引用从内存清理；Socket 只允许同一运行用户访问。

## System Agent 与插件

System Agent、命令 AI、记忆压缩、能力探测和插件 `ctx.ai.complete()` / `run_agent()` 都复用同一 Provider runtime。Gateway 只替换所选 Provider 的传输层：

- 工具循环、Action 确认、预算、usage、取消和 fallback 仍由 TelePilot 管理。
- 成功 fallback 到其它 Provider 后，后续 Agent 工具轮次会保持该 Provider，避免每轮重新撞击已知故障 Gateway。
- RunTrace 和近期调用会记录实际 backend 与 Gateway 元数据，但不会记录密钥、原始请求或原始响应。

Web Agent 输入区还提供会话级“调用客户端”选择：

- “跟随 Provider”使用每个 Provider 已保存的调用方式和身份。
- 标准 API 直连身份选项只覆盖当前会话后续请求，不修改 Provider；即使 Provider 默认走 Gateway，也可临时改用标准 API 直连。
- “Codex 客户端兼容模式”只展示并选择已经保存为 Gateway 调用方式、且可用于 Tools 的模型。没有可用 Gateway Provider 时选项会显示“未配置”；平台不会把任意 direct Provider 在 Agent 长任务中隐式改写为临时 Gateway Provider。
