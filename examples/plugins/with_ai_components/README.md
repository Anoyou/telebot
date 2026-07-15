# with_ai_components

AI 玩法组件示例：用平台内置的 `QuizMaker` + `AnswerJudge` 组件跑一个最小问答局，
演示"AI 主路径 + 确定性降级"的写法。

## 重点

- `plugin.json` 是安装阶段静态元数据，`manifest.py` 是运行阶段真实 Manifest。
- `permissions` 必须包含 `ai_text`，运行时才会注入 `ctx.ai`；示例用 `edit_message`
  把结果编辑回命令消息。
- 组件通过 `QuizMaker(ctx.ai)` / `AnswerJudge(ctx.ai)` 注入 AI 依赖，所有模型调用都走
  `ctx.ai`（`PluginAI` facade），统一计量、脱敏、token 钳制。插件**不直接** import
  后端 LLM runtime。
- **降级不依赖任何 AI 调用成功**：没有可用 provider 时 `QuizMaker` 自动出内置题库题，
  `AnswerJudge` 用精确/归一化/正则规则判定；规则判不了且无 AI 时返回 `unsure`，
  示例把 `unsure` 当作保守分支（不判对、不泄题）。

## 使用

安装到 `plugins/installed/with_ai_components/` 后启用插件，可发送：

```text
,quiz_new 成语
```

出一道"成语"主题的题（有 AI 时由模型出题，否则取内置题库）。

```text
,quiz_answer 兔
```

提交答案，由 `AnswerJudge` 判定：规则命中直接判对，命中不了且 AI 可用才问 AI，
AI 失败或无 AI 时按保守分支处理。

## 组件说明

三个组件（`QuizMaker` / `AnswerJudge` / `PersonaChat`）的完整 API、降级语义与更多示例见
`docs/PLUGIN-AI.md` 的「AI 玩法组件」章节。本示例只演示前两个组件；`PersonaChat`
用法参见文档。

CI 只会导入 manifest 和实例化插件类，不会执行命令，也不会访问真实网络或数据库。
