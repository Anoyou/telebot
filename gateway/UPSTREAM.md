# Upstream baseline

- 审查基线：`github.com/router-for-me/CLIProxyAPI/v7`
- 固定提交：`ffdb9c9fbc78a6235d59c9ccbdc4243ba35ecdcd`
- 核对日期：2026-08-03
- 使用范围：只吸收 Codex Responses 的请求身份、SSE、工具调用与错误行为；TelePilot 不复制管理面、账号池、OAuth 或桌面能力。

Gateway 当前使用 Go 标准库重新实现经审查的必要执行契约，不直接 vendoring CLIProxyAPI 管理面或账号运行时。固定基线中的 `codex_executor_request.go` 是身份适配事实源：Gateway 生成稳定 `prompt_cache_key` / `Session_id`，同步 `session/thread/turn/window/installation` client metadata，并固定 Codex UA、`Originator`、`Version` 与对应兼容头。所有字段都有本地出站契约测试，Provider 自定义头不能覆盖它们。

这些公开传输字段不是 OAuth 或设备证明。Gateway 不读取 `~/.codex/auth.json`，不生成 ChatGPT Account ID、Cookie、attestation 或 Agent Identity；若上游实际绑定官方账号凭据，仍必须返回 `official_account_required`。
