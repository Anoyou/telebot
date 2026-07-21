# 无障碍基线

使用 `pnpm --dir frontend test:a11y` 扫描 Dashboard、Accounts、Ledger、Logs、Extensions、AI、LLMProviders 与 BotTab。Critical 与 serious 级问题会直接令测试失败；moderate 与 minor 作为后续优化清单保留。

2026-07-21 使用本机 Google Chrome 完成 390px、834px、1440px 三档视口扫描，共 24 项：

- critical：0
- serious：0
- moderate：26 个节点（保留为后续非阻断优化清单）
- minor：0
- 通过：24 / 24

本轮同时补齐日志筛选下拉、插件更新开关、AI 折叠按钮、Bot 用户角色下拉和可滚动代码块的可访问名称或键盘焦点；亮色主题的 muted、warning、destructive 状态色通过实际 axe 对比度检查。

测试日志按 `视口/页面` 输出四级节点计数。CI 在固定的 `macos-15` runner 上安装 `@playwright/test` 锁定版本对应的 Chromium，并同时执行视觉与 axe 门禁。
