"""AI 玩法组件示例：用 QuizMaker + AnswerJudge 跑一个最小问答局。

演示要点：
- 通过 ``ctx.ai``（``PluginAI`` facade）把 AI 依赖注入组件，调用统一计量、统一降级；
- 无可用 AI 时 ``QuizMaker`` 自动出内置题库题、``AnswerJudge`` 走规则匹配或 ``unsure``，
  整个问答局仍可完整进行（确定性降级，不依赖任何 AI 调用成功）；
- ``unsure`` 演示保守分支：不判对、不泄题，交回人来定夺。
"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.ai_components import AnswerJudge, JudgeOutcome, Quiz, QuizMaker
from app.worker.plugins.base import Plugin, PluginContext, register

DEFAULT_TOPIC = "常识"


def _source_label(quiz: Quiz) -> str:
    return "AI 出题" if quiz.source == "ai" else "内置题库"


@register
class QuizGamePlugin(Plugin):
    """最小问答局：``,quiz_new [主题]`` 出题，``,quiz_answer 答案`` 判题。"""

    key = "with_ai_components"
    display_name = "AI 玩法组件示例"
    owner_only = True

    def __init__(self) -> None:
        # 每个 [账号 × feature] 一份实例；进行中的题目按 chat 隔离存实例属性，
        # 不使用类属性，避免跨账号污染（见 base.Plugin 文档）。
        self._current: dict[int, Quiz] = {}
        self.commands = {
            "quiz_new": self._cmd_quiz_new,
            "quiz_answer": self._cmd_quiz_answer,
        }

    def _provider_tag(self, ctx: PluginContext) -> str | None:
        return str(ctx.config.get("provider_tag") or "chat").strip() or None

    async def _cmd_quiz_new(
        self, client: Any, event: Any, args: list[str], account_id: int, ctx: PluginContext
    ) -> None:
        topic = " ".join(args).strip() or str(ctx.config.get("default_topic") or DEFAULT_TOPIC)
        maker = QuizMaker(ctx.ai, provider_tag=self._provider_tag(ctx))
        quiz = await maker.generate(topic)
        self._current[event.chat_id] = quiz

        lines = [f"【出题 · {_source_label(quiz)}】主题：{quiz.topic or topic}", quiz.question]
        if quiz.hints:
            lines.append("提示：" + "；".join(quiz.hints))
        lines.append("用 ,quiz_answer 你的答案 作答。")
        await event.edit("\n".join(lines))

    async def _cmd_quiz_answer(
        self, client: Any, event: Any, args: list[str], account_id: int, ctx: PluginContext
    ) -> None:
        quiz = self._current.get(event.chat_id)
        if quiz is None:
            await event.edit("还没有进行中的题目，先用 ,quiz_new [主题] 出一题。")
            return
        answer = " ".join(args).strip()
        if not answer:
            await event.edit("请在命令后写上你的答案，例如 ,quiz_answer 北京")
            return

        judge = AnswerJudge(ctx.ai, provider_tag=self._provider_tag(ctx))
        verdict = await judge.judge(quiz.question, quiz.answer, answer, accepted=quiz.accepted)

        if verdict.outcome is JudgeOutcome.CORRECT:
            self._current.pop(event.chat_id, None)
            await event.edit(f"答对了！标准答案：{quiz.answer}（判定来源：{verdict.source}）")
        elif verdict.outcome is JudgeOutcome.INCORRECT:
            await event.edit("不对哦，再想想，或用 ,quiz_new 换一题。")
        else:
            # 保守分支：AI 不可用或拿不准时不判对、不泄题，交回人来定夺。
            await event.edit("暂时无法自动判定（保守处理为未答对），可再试或让管理员定夺。")


__all__ = ["QuizGamePlugin"]
