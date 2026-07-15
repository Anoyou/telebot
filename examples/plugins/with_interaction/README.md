# with_interaction

历史交互入口的兼容桥示例。新插件优先参考 `examples/plugins/event_bus_demo`，本目录只演示如何把旧 `interaction_entries` / `on_interaction` 迁移到标准事件信封。

## 兼容点

- 原命令 `with_interaction` 仍可触发。
- `interaction_entries` 仅作为旧交互规则迁移声明。
- `event_subscriptions` 声明 Event Bus 投递范围。
- `on_interaction()` 返回结构化 `result` / `settlement`。
- 示例参数使用 `response_text`；历史 `message` 配置字段会与标准信封 `payload["message"]` 冲突，只保留迁移兼容。

## 结构

```text
with_interaction/
├── __init__.py
├── plugin.json
├── manifest.py
├── plugin.py
└── README.md
```
