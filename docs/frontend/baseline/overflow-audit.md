# 390px 横向溢出审计

基线页面：Dashboard、Accounts、BotTab、Ledger、Logs、Extensions、AI、LLMProviders。

验收规则：页面根节点 `scrollWidth <= innerWidth`；Ledger 的固定宽表格在窄屏改用可展开卡片，所有字段仍可达。

`tests/visual/baseline.spec.ts` 会在 mobile 项目中逐页校验根节点宽度；失败信息包含前 12 个越界元素的标签、类名与左右边界，便于直接定位责任组件。

2026-07-21 使用 390px 视口完成亮暗主题各一轮检查：

| 页面 | 亮色 | 暗色 | 说明 |
| --- | --- | --- | --- |
| Dashboard | 通过 | 通过 | 根节点无横向滚动 |
| Accounts | 通过 | 通过 | 根节点无横向滚动 |
| Ledger | 通过 | 通过 | 流水与补付记录使用可展开卡片，扩展字段可达 |
| Logs | 通过 | 通过 | 筛选区随窄屏降列 |
| Extensions | 通过 | 通过 | 更新与插件卡片无越界 |
| AI | 通过 | 通过 | 三个快捷入口保持单行，KPI 标题完整显示 |
| LLMProviders | 通过 | 通过 | 创建向导在窄屏纵向排列 |
| BotTab | 通过 | 通过 | 表单、状态信号和操作区无页面级横滚 |

结果：16 / 16 主题页面组合通过，根节点 `scrollWidth` 均未超过视口宽度 1px 容差。
