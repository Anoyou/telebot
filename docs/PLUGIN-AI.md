# TelePilot 插件 AI facade

`ctx.ai` 已作为第三方插件可用的受控文本 AI facade。插件需要在 `plugin.json` 和 `manifest.py` 中声明 `permissions=["ai_text"]`，运行时才会注入 `ctx.ai`。

## Event Bus 主路径写法

```python
async def on_event(self, ctx, payload):
    message = payload["message"]
    chat = payload["chat"]
    chat_id = message.get("chat_id") or chat["id"]
    reply_to = message.get("message_id")

    if ctx.ai is None:
        return [{
            "type": "send_message",
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "text": "本插件需要 ai_text 权限",
        }]

    providers = await ctx.ai.list_providers()
    result = await ctx.ai.complete(
        system="你是一个简洁助手。",
        user=message.get("text") or "总结这段内容",
        provider_tag="chat",
        max_tokens=512,
        timeout_seconds=30,
    )
    return [{
        "type": "send_message",
        "chat_id": chat_id,
        "reply_to_message_id": reply_to,
        "text": result.text,
    }]
```

也可以在交互入口中调用 `ctx.messages.send(...)` 缓存同等标准动作。最终版插件不应把 `event.edit(...)` 作为公共群互动或高频交互的主输出路径；命令式 `event.edit(...)` 只适合管理员命令兼容示例。

## Provider 选择

- `provider_tag`：推荐写法。按用途标签选择 provider，平台会在可用 provider 中挑选成本优先的匹配项。
- `provider`：需要固定 provider 时可传 provider id 或 provider name。
- `tag` / `tags`：兼容别名，已 deprecated；新插件请使用 `provider_tag`。

## Quota 与脱敏

- `ctx.ai.complete()` 复用平台 LLM Provider 池、fallback 链、账号级预算和 usage 记录。
- 插件传入的 `max_tokens` 与超时会被平台上限收紧，不能绕过账号配额。
- `ctx.ai.list_providers()` 只返回脱敏元数据，例如 provider 名称、默认模型、标签和成本层级。
- 插件不会拿到 `api_key_enc`、明文 API Key、`base_url` 或代理 URL。
- 不要在插件日志里记录用户完整隐私输入或模型完整输出；需要排障时只记录长度、截断摘要或 request id。

## 示例

完整最小示例见 `examples/plugins/with_ai/`。CI 只导入示例并校验 manifest / plugin 元数据，不会执行真实 AI 请求。

## 配额限制

平台从 `system_setting` 的 `plugin_ai_quota` 读取插件 AI 配额配置。示例：

```json
{
  "per_minute_tokens": 20000,
  "daily_tokens": 200000,
  "plugins": {
    "sum": {
      "per_minute_tokens": 5000,
      "daily_tokens": 50000
    }
  }
}
```

- `per_minute_tokens` 是每分钟 token 软上限，`daily_tokens` 是自然日 token 软上限。
- `plugins.{key}` 可覆盖单个插件的全局配置，例如上面的 `sum`。
- 任一限制设为 `0` 表示不限制。
- 超限时，插件会收到 `AIQuotaError`；平台同时写入一条 `LLMUsage(success=False, error_type="plugin_quota_exceeded")`，可在 Usage 页排查。
- Redis 不可用时会降级为 DB 检查，但并发预扣保护会暂时关闭；生产环境建议保留 Redis 可用性监控。
- token 估算是软上限：当前按 UTF-8 字节数 `// 4` 粗估，中文场景通常会偏低 1.5-2x，并发尖峰也可能瞬时越限。
- 跨午夜的请求按 acquire 当时所属的自然日记账，软上限场景误差可接受。

## AI 玩法组件

`app.worker.plugins.ai_components` 提供三个"AI 主路径 + 确定性降级"的玩法组件，供插件
直接组合使用。它们**不持有全局状态**，AI 依赖由构造参数注入（生产传 `ctx.ai`，测试传
stub），可直接单测。

两条硬约束：

- **降级不依赖任何 AI 调用成功**：平台 LLM 运行时的重试/fallback 链目前不生效，因此每个
  组件都有一条不调用 AI 也能走通的确定性降级路径。插件不得假设 AI 一定可用。
- **只走 `ctx.ai` 正常计量路径**：组件不 import `llm_client` / `llm_invoke` /
  `llm_runtime`，所有模型调用都经 `ctx.ai.complete()`，天然继承 quota、账号预算、usage
  记录与 token 钳制。禁止在组件里新增任何绕过计量的直连调用。

### QuizMaker — 出题（AI 主，题库降级）

```python
from app.worker.plugins.ai_components import QuizMaker

maker = QuizMaker(ctx.ai)          # ctx.ai 为 None 时直接走内置题库
quiz = await maker.generate("成语")  # -> Quiz(question, answer, hints, accepted, topic, source)
```

- 优先让 AI 出题；AI 失败/超时/无 provider/返回无法解析为 JSON 时，降级到随包题库
  （`ai_components_quiz_bank.json`，≥30 题，按 `谜语 / 成语 / 常识` 分组）。
- `topic` 命中某个题库分组则从该组抽题，否则从全部题目里抽；`Quiz.source` 标注 `"ai"`
  或 `"builtin"`。
- 降级选题只依赖构造时可注入的 `rng`（`random.Random`），便于测试确定性；不碰 AI。

### AnswerJudge — 判题（规则先行，AI 兜底，失败 unsure）

```python
from app.worker.plugins.ai_components import AnswerJudge, JudgeOutcome

judge = AnswerJudge(ctx.ai)
verdict = await judge.judge(question, expected, answer, accepted=[...], regex=r"...")
if verdict.outcome is JudgeOutcome.CORRECT:
    ...
```

判定顺序（严格短路，命中即返回，**规则命中时绝不调用 AI**）：

1. 精确匹配：去首尾空白后逐字相等；
2. 归一化匹配：NFKC（全角转半角）+ 去所有空白 + casefold 后相等；
3. 可选正则：`regex`（`re.fullmatch`，忽略大小写）；
4. 以上都判不了时，**仅当 `ctx.ai` 可用**才问 AI（prompt 要求只回 `yes` / `no` /
   `unsure`）。

降级语义：AI 失败/超时/返回不可解析 → `UNSURE`；无 AI（`ctx.ai=None`）→ `UNSURE`。
规则只负责"确认正确"，从不独自宣判"错误"，因此拿不准时一律返回 `UNSURE`，交由插件走
保守分支（例如不判对、不派奖、转人工）。`Verdict` 暴露 `.correct` / `.incorrect` /
`.unsure` 便捷属性与 `source`（`exact` / `normalized` / `regex` / `ai` / `no_ai` /
`ai_failed` / `ai_unparsed`）。

### PersonaChat — 人设对话（历史裁剪，失败静默）

```python
from app.worker.plugins.ai_components import PersonaChat

chat = PersonaChat(ctx.ai)  # 内置 friendly / tsundere / sage，可用 personas= 覆盖或追加
reply = await chat.reply("friendly", history, user_text)  # -> str | None
if reply:
    await ctx.messages.send(chat_id=..., text=reply)
```

- 按 persona preset（system prompt 模板）生成一句回复；`history` 支持 `{"role","text"}`
  字典或 `(role, text)` 二元组，按 `persona.max_history` 轮裁剪，再按字符软上限二次裁剪。
- 单次输出 `max_tokens` 交给 `PluginAI` 钳制到账号上限，组件不重复实现配额。
- 降级语义：未知 persona / 空输入 / AI 不可用 / AI 失败 / 空回复 → 返回 `None`，插件静默
  （不发消息）。

### 示例

完整用法见 `examples/plugins/with_ai_components/`：用 `QuizMaker` + `AnswerJudge`
做一个最小问答局（`,quiz_new [主题]` 出题、`,quiz_answer 答案` 判题），并演示无 AI 时
的题库降级与 `unsure` 保守分支。
