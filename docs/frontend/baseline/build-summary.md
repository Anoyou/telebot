# 前端构建基线

生成命令：`ANALYZE=1 pnpm --dir frontend build`（2026-07-21）。

## 首屏与拆包

- `dist/index.html` 当前只预加载 `radix`（75.16 KiB gzip）与 `markdown-core`（47.26 KiB gzip）。
- `markdown-highlight` 独立为 177.15 kB（53.97 kB gzip），由 `Extensions` 页面动态 import，不再进入首屏 `modulepreload`。
- `echarts` 独立为 485.44 kB（162.47 kB gzip），保持路由级按需加载，不在首页预加载。
- `UpdateDialog` 独立为 19.32 kB（6.15 kB gzip），只在用户点击“检查更新”后下载；主入口由约 541 kB / 245.23 kB gzip 降到 523.18 kB / 240.91 kB gzip。
- 主入口仍略高于 500 kB，Vite 提示继续保留，作为后续 B5 结构拆分的观测项。

生产构建共转换 2870 个模块，生成的 PWA precache 为 96 项、约 3.74 MiB。构建、service worker 生成和 chunk 拆分均成功。

构建分析报告：`docs/frontend/baseline/build-report.html`。
