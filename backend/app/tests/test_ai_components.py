"""WP9 AI 玩法组件测试：确定性降级 + 规则先行 + 题库可用性。

覆盖两条 review 约束：
- 组件不假设 fallback 可用——无 provider（ai=None）或 AI 抛错/超时时，三组件的
  确定性降级路径全部走通；
- AnswerJudge 规则命中时**绝不调用 AI**（stub 记录调用次数断言）。
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest

from app.worker.plugins.ai_components import (
    DEFAULT_PERSONAS,
    AnswerJudge,
    JudgeOutcome,
    Persona,
    PersonaChat,
    QuizMaker,
    load_quiz_bank,
)
from app.worker.plugins.ai_facade import AIQuotaError, AIUnavailableError


class StubAI:
    """记录调用次数的 ctx.ai 替身，可返回文本或抛出异常。"""

    def __init__(
        self,
        *,
        text: str | None = None,
        error: BaseException | None = None,
        results: list[str] | None = None,
    ) -> None:
        self._text = text
        self._error = error
        self._results = list(results) if results is not None else None
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        provider_tag: str | None = None,
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "provider_tag": provider_tag,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._error is not None:
            raise self._error
        if self._results is not None:
            text = self._results.pop(0)
        else:
            text = self._text
        return SimpleNamespace(text=text)


# ─────────────────────────────────────────────────────
# 内置题库
# ─────────────────────────────────────────────────────
def test_builtin_bank_has_at_least_30_grouped_questions() -> None:
    bank = load_quiz_bank()
    assert {"谜语", "成语", "常识"} <= set(bank)
    total = sum(len(entries) for entries in bank.values())
    assert total >= 30
    for entries in bank.values():
        for entry in entries:
            assert entry["question"]
            assert entry["answer"]


# ─────────────────────────────────────────────────────
# QuizMaker
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_quizmaker_uses_ai_when_available() -> None:
    ai = StubAI(
        text=(
            '{"question": "1 加 1 等于几？", "answer": "2", '
            '"hints": ["很小的数"], "accepted": ["二", "两"]}'
        )
    )
    maker = QuizMaker(ai)

    quiz = await maker.generate("数学")

    assert quiz.source == "ai"
    assert quiz.question == "1 加 1 等于几？"
    assert quiz.answer == "2"
    assert quiz.hints == ("很小的数",)
    assert quiz.accepted == ("二", "两")
    assert quiz.topic == "数学"
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_quizmaker_parses_fenced_json() -> None:
    ai = StubAI(text='```json\n{"question": "首都?", "answer": "北京"}\n```')
    maker = QuizMaker(ai)

    quiz = await maker.generate("常识")

    assert quiz.source == "ai"
    assert quiz.answer == "北京"


@pytest.mark.asyncio
async def test_quizmaker_falls_back_when_no_provider() -> None:
    maker = QuizMaker(None, rng=random.Random(0))

    quiz = await maker.generate("常识")

    assert quiz.source == "builtin"
    assert quiz.question
    assert quiz.answer


@pytest.mark.asyncio
async def test_quizmaker_falls_back_on_ai_error() -> None:
    ai = StubAI(error=AIUnavailableError("no provider"))
    maker = QuizMaker(ai, rng=random.Random(1))

    quiz = await maker.generate("谜语")

    assert quiz.source == "builtin"
    assert quiz.answer
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_quizmaker_falls_back_on_quota_error() -> None:
    ai = StubAI(error=AIQuotaError("quota exceeded"))
    maker = QuizMaker(ai, rng=random.Random(2))

    quiz = await maker.generate("成语")

    assert quiz.source == "builtin"


@pytest.mark.asyncio
async def test_quizmaker_falls_back_on_unparseable_ai_output() -> None:
    ai = StubAI(text="抱歉我无法出题")
    maker = QuizMaker(ai, rng=random.Random(3))

    quiz = await maker.generate("常识")

    assert quiz.source == "builtin"


@pytest.mark.asyncio
async def test_quizmaker_falls_back_on_empty_answer() -> None:
    ai = StubAI(text='{"question": "只有题干没有答案？", "answer": ""}')
    maker = QuizMaker(ai, rng=random.Random(4))

    quiz = await maker.generate("常识")

    assert quiz.source == "builtin"


@pytest.mark.asyncio
async def test_quizmaker_topic_matches_bank_group() -> None:
    bank = {
        "动物": [{"question": "汪汪叫的是？", "answer": "狗"}],
        "植物": [{"question": "会开花的是？", "answer": "花"}],
    }
    maker = QuizMaker(None, bank=bank, rng=random.Random(0))

    quiz = await maker.generate("动物")

    assert quiz.source == "builtin"
    assert quiz.topic == "动物"
    assert quiz.answer == "狗"


@pytest.mark.asyncio
async def test_quizmaker_unknown_topic_pools_all_groups() -> None:
    bank = {"动物": [{"question": "汪汪叫的是？", "answer": "狗"}]}
    maker = QuizMaker(None, bank=bank, rng=random.Random(0))

    quiz = await maker.generate("完全不存在的主题")

    assert quiz.source == "builtin"
    assert quiz.answer == "狗"


@pytest.mark.asyncio
async def test_quizmaker_on_error_hook_receives_exception() -> None:
    seen: list[BaseException] = []
    ai = StubAI(error=AIUnavailableError("boom"))
    maker = QuizMaker(ai, rng=random.Random(0), on_error=seen.append)

    quiz = await maker.generate("常识")

    assert quiz.source == "builtin"
    assert len(seen) == 1
    assert isinstance(seen[0], AIUnavailableError)


# ─────────────────────────────────────────────────────
# AnswerJudge —— 规则先行，命中不调 AI
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_judge_exact_match_does_not_call_ai() -> None:
    ai = StubAI(text="no")  # 若被调用则会判错，用来反证规则先行
    judge = AnswerJudge(ai)

    verdict = await judge.judge("首都?", "北京", "北京")

    assert verdict.outcome is JudgeOutcome.CORRECT
    assert verdict.source == "exact"
    assert verdict.used_ai is False
    assert ai.calls == []


@pytest.mark.asyncio
async def test_judge_normalized_match_does_not_call_ai() -> None:
    ai = StubAI(text="no")
    judge = AnswerJudge(ai)

    # 全角数字 + 首尾空白，NFKC + 去空白后等于标准答案。
    verdict = await judge.judge("一年几个月?", "12", "  １２ ")

    assert verdict.outcome is JudgeOutcome.CORRECT
    assert verdict.source == "normalized"
    assert ai.calls == []


@pytest.mark.asyncio
async def test_judge_normalized_match_ignores_case_and_inner_space() -> None:
    ai = StubAI(text="no")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("greeting?", "Hello World", " hello   world ")

    assert verdict.outcome is JudgeOutcome.CORRECT
    assert verdict.source == "normalized"
    assert ai.calls == []


@pytest.mark.asyncio
async def test_judge_accepted_alias_matches_without_ai() -> None:
    ai = StubAI(text="no")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("一周几天?", "7", "七", accepted=["七", "七天"])

    assert verdict.outcome is JudgeOutcome.CORRECT
    assert ai.calls == []


@pytest.mark.asyncio
async def test_judge_regex_match_does_not_call_ai() -> None:
    ai = StubAI(text="no")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("三位数?", "任意三位数", "123", regex=r"\d{3}")

    assert verdict.outcome is JudgeOutcome.CORRECT
    assert verdict.source == "regex"
    assert ai.calls == []


@pytest.mark.asyncio
async def test_judge_invalid_regex_is_skipped_then_defers() -> None:
    judge = AnswerJudge(None)  # 无 AI，规则判不了 → unsure

    verdict = await judge.judge("q", "expected", "different", regex=r"(")

    assert verdict.outcome is JudgeOutcome.UNSURE
    assert verdict.source == "no_ai"


@pytest.mark.asyncio
async def test_judge_calls_ai_only_when_rules_miss() -> None:
    ai = StubAI(text="yes")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("同义?", "汽车", "小轿车")

    assert verdict.outcome is JudgeOutcome.CORRECT
    assert verdict.source == "ai"
    assert verdict.used_ai is True
    assert len(ai.calls) == 1
    # prompt 要求只回 yes/no/unsure
    assert "yes" in ai.calls[0]["user"]


@pytest.mark.asyncio
async def test_judge_ai_no_maps_to_incorrect() -> None:
    ai = StubAI(text="no")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("q", "北京", "上海")

    assert verdict.outcome is JudgeOutcome.INCORRECT
    assert verdict.used_ai is True


@pytest.mark.asyncio
async def test_judge_ai_unsure_maps_to_unsure() -> None:
    ai = StubAI(text="unsure")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("q", "北京", "某地")

    assert verdict.outcome is JudgeOutcome.UNSURE
    assert verdict.source == "ai"


@pytest.mark.asyncio
async def test_judge_ai_failure_returns_unsure() -> None:
    ai = StubAI(error=AIUnavailableError("model down"))
    judge = AnswerJudge(ai)

    verdict = await judge.judge("q", "北京", "上海")

    assert verdict.outcome is JudgeOutcome.UNSURE
    assert verdict.source == "ai_failed"
    assert verdict.used_ai is True


@pytest.mark.asyncio
async def test_judge_no_provider_returns_unsure() -> None:
    judge = AnswerJudge(None)

    verdict = await judge.judge("q", "北京", "上海")

    assert verdict.outcome is JudgeOutcome.UNSURE
    assert verdict.source == "no_ai"
    assert verdict.used_ai is False


@pytest.mark.asyncio
async def test_judge_ai_unparseable_returns_unsure() -> None:
    ai = StubAI(text="我觉得也许可能大概是对的吧")
    judge = AnswerJudge(ai)

    verdict = await judge.judge("q", "北京", "某地")

    assert verdict.outcome is JudgeOutcome.UNSURE
    assert verdict.source == "ai_unparsed"


# ─────────────────────────────────────────────────────
# PersonaChat
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_persona_reply_uses_ai_and_persona_prompt() -> None:
    ai = StubAI(text="你好呀，我在的。")
    chat = PersonaChat(ai)

    reply = await chat.reply("friendly", [], "在吗")

    assert reply == "你好呀，我在的。"
    assert len(ai.calls) == 1
    assert ai.calls[0]["system"] == DEFAULT_PERSONAS["friendly"].system_prompt
    assert "在吗" in ai.calls[0]["user"]


@pytest.mark.asyncio
async def test_persona_reply_none_without_provider() -> None:
    chat = PersonaChat(None)

    reply = await chat.reply("friendly", [], "在吗")

    assert reply is None


@pytest.mark.asyncio
async def test_persona_reply_none_on_ai_error() -> None:
    ai = StubAI(error=AIQuotaError("quota"))
    chat = PersonaChat(ai)

    reply = await chat.reply("friendly", [], "在吗")

    assert reply is None
    assert len(ai.calls) == 1


@pytest.mark.asyncio
async def test_persona_reply_none_for_unknown_persona() -> None:
    ai = StubAI(text="不该被调用")
    chat = PersonaChat(ai)

    reply = await chat.reply("does-not-exist", [], "在吗")

    assert reply is None
    assert ai.calls == []


@pytest.mark.asyncio
async def test_persona_reply_none_for_empty_user_text() -> None:
    ai = StubAI(text="不该被调用")
    chat = PersonaChat(ai)

    reply = await chat.reply("friendly", [], "   ")

    assert reply is None
    assert ai.calls == []


@pytest.mark.asyncio
async def test_persona_reply_none_on_empty_ai_text() -> None:
    ai = StubAI(text="   ")
    chat = PersonaChat(ai)

    reply = await chat.reply("friendly", [], "在吗")

    assert reply is None


@pytest.mark.asyncio
async def test_persona_reply_trims_history_window() -> None:
    ai = StubAI(text="收到")
    personas = {"short": Persona("short", "简短助手", max_history=2)}
    chat = PersonaChat(ai, personas=personas)

    history = [
        {"role": "user", "text": "第一句"},
        {"role": "assistant", "text": "第二句"},
        {"role": "user", "text": "第三句"},
        {"role": "assistant", "text": "第四句"},
    ]
    reply = await chat.reply("short", history, "第五句")

    assert reply == "收到"
    prompt = ai.calls[0]["user"]
    assert "第一句" not in prompt
    assert "第二句" not in prompt
    assert "第三句" in prompt
    assert "第四句" in prompt
    assert "第五句" in prompt


@pytest.mark.asyncio
async def test_persona_reply_accepts_tuple_history() -> None:
    ai = StubAI(text="嗯")
    chat = PersonaChat(ai)

    reply = await chat.reply("sage", [("user", "早"), ("assistant", "早安")], "今天如何")

    assert reply == "嗯"
    prompt = ai.calls[0]["user"]
    assert "今天如何" in prompt
