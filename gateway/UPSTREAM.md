# Upstream baseline

- 审查基线：`github.com/router-for-me/CLIProxyAPI/v7`
- 固定提交：`ffdb9c9fbc78a6235d59c9ccbdc4243ba35ecdcd`
- 核对日期：2026-08-03
- 使用范围：只参考 Codex Responses 的请求、SSE、工具调用与错误行为；TelePilot 不复制管理面、账号池、OAuth 或桌面能力。

Gateway 的首版适配层保持小型且可审计。任何吸收的行为必须有本地契约测试；不得读取 `~/.codex/auth.json`，不得伪造 ChatGPT OAuth、Account ID、设备证明或 Agent Identity。
