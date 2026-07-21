# 前端视觉基线

测试通过 `page.route()` 固定 GET API 响应，`/api/auth/me` 使用固定测试用户，不写入真实 Cookie 或 token。所有非 GET `/api/**` 请求都会被阻断并让测试失败。

运行：

```bash
pnpm --dir frontend test:visual
pnpm --dir frontend test:a11y
```

首次建立或人工确认页面改动后，用 `pnpm --dir frontend test:visual:update` 显式更新 48 张基线。普通 `test:visual` 只读已提交截图并按 0.1% 阈值比较，不会覆盖基线。

脚本默认由 Playwright 启动 Vite。已经手动启动服务时，可运行 `VISUAL_BASE_URL=http://127.0.0.1:5173 pnpm --dir frontend exec playwright test tests/visual`。截图输出到 `docs/frontend/baseline/screenshots/`，构建分析使用 `ANALYZE=1 pnpm --dir frontend build`。
