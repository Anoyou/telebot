# with_ai

最小 AI facade 示例，展示第三方插件如何声明并使用平台统一的文本 LLM 能力。

## 重点

- `plugin.json` 是安装阶段静态元数据。
- `manifest.py` 是运行阶段真实 Manifest。
- `permissions` 必须包含 `ai_text`。
- 示例用 `edit_message` 把结果编辑回命令消息；如果要主动发新消息，可改为声明 `send_message`。
- 运行时只通过 `ctx.ai` 调用文本 AI，不直接 import 后端私有 LLM runtime。
- `ctx.ai.list_providers()` 只返回脱敏 provider 元数据，不包含 API Key、base URL 或代理 URL。
- `ctx.ai.complete()` 推荐优先使用 `provider_tag` 选择用途标签；`tag` / `tags` 仅作为旧写法兼容别名保留且已 deprecated。
- 系统 AI 平台能力关闭时不会注入 `ctx.ai`；可选声明 `requires_platform_capabilities=["ai"]`。

## 使用

安装到 `plugins/installed/with_ai/` 后启用插件，可发送：

```text
,ai_providers
```

查看当前账号可见的脱敏 provider 摘要。

```text
,ai_complete 用一句话解释什么是 TelePilot 插件
```

通过 `ctx.ai.complete()` 调用平台统一 LLM 池，并把回答编辑回命令消息。

CI 只会导入 manifest 和实例化插件类，不会执行命令，也不会访问真实网络或数据库。
