# TelePilot 内部 API 契约

TelePilot 当前只为自身 Web/PWA 提供内部 HTTP API，不承诺第三方公共平台兼容性，也不增加 `/api/v1`、OAuth、PAT 或 MCP 入口。接口仍统一位于 `/api/*`。

## 认证与写请求保护

- 登录态：HttpOnly Cookie `auth_token`。
- 写请求来源头：`X-Requested-With: telepilot-ui`。
- CSRF double-submit：`csrf_token` Cookie 与 `X-CSRF-Token` Header 必须相同。
- 外部 Webhook 投递：只使用 `X-TelePilot-Webhook-Token`，不使用登录 Cookie；查询参数 Token 兼容默认关闭。
- 完全公开的读取入口只有 CSRF 签发、liveness、readiness 和版本元数据；注册、登录、注销虽不要求登录 Cookie，仍受 CSRF 保护。

具体安全矩阵以生成的 [`openapi/telepilot.openapi.json`](../openapi/telepilot.openapi.json) 为准。新增未鉴权路由必须同时更新固定白名单测试，不能依赖“开发者记得加鉴权”。

## 错误响应

业务与 HTTP 错误保持现有结构：

```json
{
  "error": {
    "code": "AUTH_REQUIRED",
    "message": "未登录"
  }
}
```

请求参数校验错误继续使用 FastAPI 的 `HTTPValidationError`（422 `detail` 数组）。前端错误解析必须同时兼容这两种既有格式；本阶段不为统一外观而破坏运行时兼容性。

## 修改接口后的固定动作

从仓库根目录执行：

```bash
make codegen
pnpm --dir frontend typecheck
```

`make codegen` 会离线执行以下动作，不需要先启动后端：

1. 调用 `scripts/export-openapi.py` 生成 `openapi/telepilot.openapi.json`。
2. 使用 `openapi-typescript` 生成 `frontend/src/api/schema.ts`。

两个生成文件必须与 API 代码一同提交。CI 会重新生成并执行 `git diff --exit-code`；契约未更新、operationId 重复、安全矩阵漂移、公开路由增加或成功响应缺少 schema 都会阻止合并。

新增或修改的前端 API 类型优先引用 `frontend/src/api/schema.ts`；现有 `frontend/src/api/types.ts` 按模块逐步迁移，不做一次性重写。
