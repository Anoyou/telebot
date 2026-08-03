# 内置 Codex Gateway

TelePilot 可以让单个 LLM Provider 选择「直接 API」或「内置 Codex Gateway」。Gateway 是 Web 进程按需管理的独立 Go 子进程，只负责 Responses 协议传输，不是第二个 Agent，也不改变 System Agent、插件 AI、预算、重试或 fallback 的决策归属。

## Provider 配置

在「AI → 模型提供商」新建或编辑 Provider：

1. 「直接 API」保持原有请求路径，继续使用 Provider 的协议档案、客户端身份、代理和兼容请求头。
2. 「内置 Codex Gateway」固定使用 Responses API 与 Codex Responses 档案，身份由 Gateway 管理。
3. direct 与 Gateway 之间切换不会清空 API Key、代理、兼容请求头或已保存的客户端身份；切回 direct 后恢复原协议、联网协议和身份配置。LLM Provider 代理仅支持 HTTP/HTTPS/SOCKS5；被 Provider 引用的代理不能改成 MTProxy。
4. Gateway Provider 必须配置 API Key；Base URL 留空时会物化当前服务类型的默认地址。未保存的凭据不会临时注入 Gateway，新建后再通过真实 Gateway 读取模型列表并测活。
5. Gateway 只支持文本/工具调用的 Responses 路径。Chat Completions、Anthropic Messages、语音转写和图片生成必须使用 direct Provider。

Gateway Provider 保存前会检查内置二进制和协议兼容性。Gateway 不可用时保存失败，不会在同一 Provider 内静默改走 direct。

Provider 与其引用代理的 Web/System Agent 写入共享进程锁和 PostgreSQL 事务 advisory lock：候选快照必须在数据库 commit 前同步；commit、取消或同步失败时会从已提交数据库快照补偿恢复。同步结果不确定时 Gateway 子进程会先停止并后台重试，避免继续使用未提交的路由或凭据。

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
- `client_rejected` / `official_account_required`：上游拒绝当前客户端或要求官方账号运行时。它们不等同于 API Key 错误。
- `quota_exhausted`、`rate_limited`、`upstream_error`、`timeout`：由 TelePilot runtime 按现有规则决定重试或切换其它 Provider。Gateway 自己不做语义重试。

若 Gateway Provider 失败，TelePilot 可以按配置切换到另一个 Provider；不会在同一个 Provider 内绕过 Gateway 偷跑 direct。流已经输出文本后不会切换，避免拼接两次回答。

## 部署、升级与回滚

生产 Web 镜像使用多阶段构建内置静态 Gateway 二进制。Gateway 与 Web 镜像原子升级和回滚，不新增 Compose service、端口、volume 或用户安装步骤。服务器只需按原方式拉取并重建或更新 TelePilot。

开发入口：

```bash
make gateway-build
make gateway-test
make gateway-run
```

`gateway-run` 默认监听仓库 `.run/gateway.sock`。本机没有 Go 时，普通 direct 开发和测试仍可继续；只有 Gateway 专项命令会明确失败并提示安装 Go 1.23 或更高版本。

执行过 `make gateway-build` 后，`make backend` 和 `make up` 会自动把本地二进制及 `.run/gateway.sock` 传给 Runtime Manager；无需把开发产物复制到 `/usr/local/bin` 或创建 `/run/telepilot`。

Gateway 当前协议版本为 `2`。后端与二进制协议版本不一致时会失败关闭，不能通过忽略未知字段继续运行。

## 安全边界

- Provider API Key 只通过权限为 `0600` 的 Unix Socket 控制面进入 Gateway 内存，不写 Gateway 配置文件。
- Gateway 不记录 Base URL、请求正文、响应正文、Authorization、API Key、兼容请求头值或代理凭据；未选择 Provider 代理时不会继承进程的 `HTTP_PROXY` / `HTTPS_PROXY`。
- TelePilot 内部 `X-TelePilot-*` 请求头在 Gateway 边界剥离，用户不能覆盖系统鉴权头、Gateway 内部头或 Codex 身份头。
- 上游重定向不会被自动跟随，避免自定义认证头跨 origin 泄露；上游错误在进入日志、API、usage 或 RunTrace 前脱敏。
- Gateway 不读取、导入、存储或同步 Codex OAuth、账号身份、设备证明或 `~/.codex/auth.json`。
- Provider 删除、变更或 Gateway 终止时，旧路由和凭据引用从内存清理；Socket 只允许同一运行用户访问。

## System Agent 与插件

System Agent、命令 AI、记忆压缩、能力探测和插件 `ctx.ai.complete()` / `run_agent()` 都复用同一 Provider runtime。Gateway 只替换所选 Provider 的传输层：

- 工具循环、Action 确认、预算、usage、取消和 fallback 仍由 TelePilot 管理。
- 成功 fallback 到其它 Provider 后，后续 Agent 工具轮次会保持该 Provider，避免每轮重新撞击已知故障 Gateway。
- RunTrace 和近期调用会记录实际 backend 与 Gateway 元数据，但不会记录密钥、原始请求或原始响应。
