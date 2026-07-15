"""AI 玩法组件库（``ctx.ai`` 之上的确定性降级封装）。

设计约束（对应 WP9 两条 review 结论）：

1. **不假设 fallback 一定成功**：降级是刻意的第二道保险，因此每个组件都
   必须有一条**不依赖任何 AI 调用成功**的确定性降级路径。
   ``QuizMaker`` 降级到随包题库，``AnswerJudge`` 降级到 ``unsure``（交由插件走
   保守分支），``PersonaChat`` 降级到静默（返回 ``None``）。
2. **只走 PluginAI 正常计量路径**：组件不 import 平台私有 LLM 运行时/客户端模块，所有
   模型调用都通过构造时注入的 ``ctx.ai``（``PluginAI`` facade）完成，天然继承 quota /
   账号预算 / usage 记录与 token 钳制，禁止新增任何绕过计量的直连调用。

这些组件**不持有全局状态**：AI 依赖由构造参数注入（生产传 ``ctx.ai``，测试传
stub），可直接单测；题库读取有一层模块级缓存，但可被 ``bank=`` 覆盖。
"""

from __future__ import annotations

import json
import random
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

# 组件对 AI 依赖只做鸭子类型使用（``.complete(...)``），因此不在此 import
# ``ai_facade``，既让 stub 注入更轻，也避免把后端服务依赖面带进纯组件库。
# 任何 AI 调用异常（AIUnavailableError / AIQuotaError / 超时 / 意外）都统一降级。

_QUIZ_BANK_PATH = Path(__file__).with_name("ai_components_quiz_bank.json")

# 题库文件缺失时的兜底，保证降级路径永不落空（确定性、无需 AI）。
_EMBEDDED_QUIZ_BANK: dict[str, list[dict[str, Any]]] = {
    "常识": [
        {"question": "中国的首都是哪座城市？", "answer": "北京", "hints": ["华北地区"], "accepted": ["北京市"]},
        {"question": "一个星期有几天？", "answer": "7", "hints": ["个位数"], "accepted": ["七", "七天"]},
    ],
    "成语": [
        {"question": "补全成语：守株待＿", "answer": "兔", "hints": ["一种小动物"], "accepted": ["兔子"]},
    ],
}

ErrorHook = Callable[[BaseException], None]


# ─────────────────────────────────────────────────────
# 题库加载
# ─────────────────────────────────────────────────────
_QUIZ_BANK_CACHE: dict[str, list[dict[str, Any]]] | None = None


def load_quiz_bank() -> dict[str, list[dict[str, Any]]]:
    """加载随包题库（按主题分组），带模块级缓存。

    读取失败或结构非法时回退到内嵌兜底题库，保证调用方永远拿到非空题库。
    返回的是缓存对象，调用方只读不改。
    """

    global _QUIZ_BANK_CACHE
    if _QUIZ_BANK_CACHE is None:
        _QUIZ_BANK_CACHE = _read_quiz_bank()
    return _QUIZ_BANK_CACHE


def _read_quiz_bank() -> dict[str, list[dict[str, Any]]]:
    try:
        raw = json.loads(_QUIZ_BANK_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 任何读取/解析失败都回退兜底
        return _clone_bank(_EMBEDDED_QUIZ_BANK)
    if not isinstance(raw, Mapping):
        return _clone_bank(_EMBEDDED_QUIZ_BANK)

    bank: dict[str, list[dict[str, Any]]] = {}
    for topic, entries in raw.items():
        if not isinstance(entries, Sequence):
            continue
        normalized = [_normalize_bank_entry(item) for item in entries]
        normalized = [item for item in normalized if item is not None]
        if normalized:
            bank[str(topic)] = normalized  # type: ignore[assignment]
    return bank if any(bank.values()) else _clone_bank(_EMBEDDED_QUIZ_BANK)


def _normalize_bank_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    question = str(item.get("question") or "").strip()
    answer = str(item.get("answer") or "").strip()
    if not question or not answer:
        return None
    return {
        "question": question,
        "answer": answer,
        "hints": _string_list(item.get("hints")),
        "accepted": _string_list(item.get("accepted")),
    }


def _clone_bank(bank: Mapping[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {str(topic): [dict(entry) for entry in entries] for topic, entries in bank.items()}


# ─────────────────────────────────────────────────────
# QuizMaker：出题（AI 主，题库降级）
# ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Quiz:
    """一道题目。``source`` 标注来自 ``ai`` 还是内置 ``builtin`` 题库。"""

    question: str
    answer: str
    hints: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()
    topic: str = ""
    source: str = "builtin"


class QuizMaker:
    """按主题出题：优先问 AI，AI 失败/超时/无 provider/返回不可解析时降级到内置题库。

    - ``ai``：``ctx.ai``（``PluginAI``）或 stub；``None`` 表示无 AI，直接走题库。
    - ``bank``：可注入自定义题库（主题 -> 题目列表）；默认使用随包题库。
    - ``rng``：可注入 ``random.Random`` 以便测试确定性；降级选题只依赖它，不碰 AI。
    """

    def __init__(
        self,
        ai: Any = None,
        *,
        bank: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        provider_tag: str | None = "chat",
        max_tokens: int = 512,
        timeout_seconds: int = 30,
        rng: random.Random | None = None,
        on_error: ErrorHook | None = None,
    ) -> None:
        self._ai = ai
        self._bank = _coerce_bank(bank) if bank is not None else load_quiz_bank()
        self._provider_tag = provider_tag
        self._max_tokens = max(1, int(max_tokens))
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._rng = rng or random.Random()
        self._on_error = on_error

    async def generate(self, topic: str) -> Quiz:
        """出一道题；AI 不可用或产出非法时返回内置题库中的题目。"""

        topic = str(topic or "").strip()
        quiz = await self._generate_via_ai(topic)
        if quiz is not None:
            return quiz
        return self._fallback(topic)

    async def _generate_via_ai(self, topic: str) -> Quiz | None:
        if self._ai is None:
            return None
        subject = topic or "常识"
        system = (
            "你是一个中文出题助手，负责根据主题出一道简短问答题。"
            "只输出一个 JSON 对象，不要输出任何解释或多余文字。"
        )
        user = (
            f"主题：{subject}\n"
            "请出一道题，返回 JSON，字段如下：\n"
            '{"question": "题干", "answer": "标准答案", '
            '"hints": ["提示1", "提示2"], "accepted": ["可接受的其它答案"]}\n'
            "hints 与 accepted 可以为空数组。只返回这个 JSON。"
        )
        text = await _safe_complete(
            self._ai,
            system,
            user,
            provider_tag=self._provider_tag,
            max_tokens=self._max_tokens,
            timeout_seconds=self._timeout_seconds,
            on_error=self._on_error,
        )
        if not text:
            return None
        payload = _extract_json_object(text)
        if payload is None:
            return None
        question = str(payload.get("question") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        if not question or not answer:
            return None
        return Quiz(
            question=question,
            answer=answer,
            hints=tuple(_string_list(payload.get("hints"))),
            accepted=tuple(_string_list(payload.get("accepted"))),
            topic=topic,
            source="ai",
        )

    def _fallback(self, topic: str) -> Quiz:
        entries, resolved_topic = self._pick_bank_pool(topic)
        entry = self._rng.choice(entries)
        return Quiz(
            question=str(entry.get("question") or ""),
            answer=str(entry.get("answer") or ""),
            hints=tuple(_string_list(entry.get("hints"))),
            accepted=tuple(_string_list(entry.get("accepted"))),
            topic=resolved_topic,
            source="builtin",
        )

    def _pick_bank_pool(self, topic: str) -> tuple[list[dict[str, Any]], str]:
        matched = _match_topic(self._bank, topic)
        if matched is not None:
            entries = [entry for entry in self._bank[matched] if entry.get("question")]
            if entries:
                return entries, matched
        pooled = [entry for entries in self._bank.values() for entry in entries if entry.get("question")]
        if pooled:
            return pooled, topic
        # 理论上不可达：题库加载已保证非空，此处再兜一层。
        embedded = _clone_bank(_EMBEDDED_QUIZ_BANK)
        pooled = [entry for entries in embedded.values() for entry in entries]
        return pooled, topic


# ─────────────────────────────────────────────────────
# AnswerJudge：判题（规则先行，AI 兜底，失败 unsure）
# ─────────────────────────────────────────────────────
class JudgeOutcome(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNSURE = "unsure"


@dataclass(frozen=True)
class Verdict:
    """判题结论。``source`` 记录由哪一层得出，``used_ai`` 标注是否真的问了 AI。"""

    outcome: JudgeOutcome
    source: str
    used_ai: bool = False
    detail: str = ""

    @property
    def correct(self) -> bool:
        return self.outcome is JudgeOutcome.CORRECT

    @property
    def incorrect(self) -> bool:
        return self.outcome is JudgeOutcome.INCORRECT

    @property
    def unsure(self) -> bool:
        return self.outcome is JudgeOutcome.UNSURE


class AnswerJudge:
    """判定玩家答案是否正确。

    判定顺序（严格短路，命中即返回，绝不多问 AI）：
      1. 精确匹配：去首尾空白后逐字相等；
      2. 归一化匹配：NFKC（全角转半角）+ 去所有空白 + casefold 后相等；
      3. 可选正则：``regex`` 命中（``re.fullmatch``，忽略大小写）；
      4. 以上都判不了时，**仅当 AI 可用**才问 AI（prompt 要求只回 yes/no/unsure）；
         AI 失败/超时/返回不可解析 → ``UNSURE``；无 AI → ``UNSURE``。

    ``UNSURE`` 让插件走保守分支（例如不判对、不派奖、转人工），
    因为规则只负责"确认正确"，从不独自宣判"错误"。
    """

    def __init__(
        self,
        ai: Any = None,
        *,
        provider_tag: str | None = "chat",
        max_tokens: int = 16,
        timeout_seconds: int = 20,
        on_error: ErrorHook | None = None,
    ) -> None:
        self._ai = ai
        self._provider_tag = provider_tag
        self._max_tokens = max(1, int(max_tokens))
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._on_error = on_error

    async def judge(
        self,
        question: str,
        expected: str | Iterable[str],
        answer: str,
        *,
        accepted: str | Iterable[str] | None = None,
        regex: str | Iterable[str] | None = None,
    ) -> Verdict:
        answer_raw = str(answer or "")
        expected_values = _as_list(expected) + _as_list(accepted)

        answer_stripped = answer_raw.strip()
        for exp in expected_values:
            if answer_stripped == exp.strip():
                return Verdict(JudgeOutcome.CORRECT, "exact")

        answer_norm = _normalize_text(answer_raw)
        if answer_norm:
            for exp in expected_values:
                if answer_norm == _normalize_text(exp):
                    return Verdict(JudgeOutcome.CORRECT, "normalized")

        for pattern in _as_list(regex):
            try:
                if re.fullmatch(pattern, answer_stripped, flags=re.IGNORECASE):
                    return Verdict(JudgeOutcome.CORRECT, "regex")
            except re.error:
                continue

        # 规则判不了：仅当 AI 可用才问；否则 unsure（确定性降级，不碰 AI）。
        if self._ai is None:
            return Verdict(JudgeOutcome.UNSURE, "no_ai")
        return await self._judge_via_ai(question, expected_values, answer_raw)

    async def _judge_via_ai(
        self, question: str, expected_values: list[str], answer_raw: str
    ) -> Verdict:
        expected_display = "、".join(expected_values) or "（无）"
        system = (
            "你是问答判题助手。判断玩家答案与标准答案是否表达同一含义"
            "（允许同义词、别称、简写、数字与中文写法互通）。"
            "只输出一个词：yes 表示正确，no 表示错误，unsure 表示无法确定。"
            "不要输出其它任何字符。"
        )
        user = (
            f"题目：{question}\n"
            f"标准答案：{expected_display}\n"
            f"玩家答案：{answer_raw}\n"
            "只回 yes、no 或 unsure："
        )
        text = await _safe_complete(
            self._ai,
            system,
            user,
            provider_tag=self._provider_tag,
            max_tokens=self._max_tokens,
            timeout_seconds=self._timeout_seconds,
            on_error=self._on_error,
        )
        if text is None:
            return Verdict(JudgeOutcome.UNSURE, "ai_failed", used_ai=True)
        word = _leading_word(text)
        if word == "yes":
            return Verdict(JudgeOutcome.CORRECT, "ai", used_ai=True)
        if word == "no":
            return Verdict(JudgeOutcome.INCORRECT, "ai", used_ai=True)
        if word == "unsure":
            return Verdict(JudgeOutcome.UNSURE, "ai", used_ai=True)
        # AI 未按约定回词 → 保守 unsure。
        return Verdict(JudgeOutcome.UNSURE, "ai_unparsed", used_ai=True, detail=text.strip()[:40])


# ─────────────────────────────────────────────────────
# PersonaChat：人设对话（历史裁剪，失败静默）
# ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Persona:
    """人设预设：system prompt 模板 + 历史窗口大小 + 单次输出 token 上限。"""

    key: str
    system_prompt: str
    max_history: int = 6
    max_tokens: int = 512


DEFAULT_PERSONAS: dict[str, Persona] = {
    "friendly": Persona(
        "friendly",
        "你是群里一个友好、简洁的中文助手。回答控制在两三句话内，语气自然亲切。",
    ),
    "tsundere": Persona(
        "tsundere",
        "你在群里扮演一个嘴硬心软（傲娇）的角色，语气俏皮但不刻薄，回答简短。",
    ),
    "sage": Persona(
        "sage",
        "你扮演一位沉稳的智者，用简练、有分寸的中文回答，避免长篇大论。",
    ),
}


class PersonaChat:
    """按人设预设生成一句回复，失败静默（返回 ``None``，插件不发消息）。

    - 未知 ``persona_key`` / 空 ``user_text`` / AI 不可用 / AI 失败 / 空回复 → ``None``。
    - 历史按 ``persona.max_history`` 轮裁剪，再按字符软上限二次裁剪，避免 prompt 过长；
      单次输出 ``max_tokens`` 交给 ``PluginAI`` 钳制到账号上限（不在此重复实现配额）。
    """

    def __init__(
        self,
        ai: Any = None,
        *,
        personas: Mapping[str, Persona | Mapping[str, Any] | str] | None = None,
        provider_tag: str | None = "chat",
        max_tokens: int = 512,
        timeout_seconds: int = 30,
        max_history: int = 6,
        max_history_chars: int = 4000,
        on_error: ErrorHook | None = None,
    ) -> None:
        self._ai = ai
        self._personas: dict[str, Persona] = dict(DEFAULT_PERSONAS)
        if personas:
            self._personas.update(_coerce_personas(personas))
        self._provider_tag = provider_tag
        self._max_tokens = max(1, int(max_tokens))
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._max_history = max(0, int(max_history))
        self._max_history_chars = max(0, int(max_history_chars))
        self._on_error = on_error

    def persona_keys(self) -> list[str]:
        return sorted(self._personas)

    async def reply(
        self,
        persona_key: str,
        history: Iterable[Any] | None,
        user_text: str,
    ) -> str | None:
        persona = self._personas.get(str(persona_key or "").strip())
        if persona is None:
            return None
        user_text = str(user_text or "").strip()
        if not user_text:
            return None

        window_turns = persona.max_history if persona.max_history else self._max_history
        window = _trim_history(history, window_turns, self._max_history_chars)
        user_prompt = _render_history(window, user_text)

        text = await _safe_complete(
            self._ai,
            persona.system_prompt,
            user_prompt,
            provider_tag=self._provider_tag,
            max_tokens=persona.max_tokens or self._max_tokens,
            timeout_seconds=self._timeout_seconds,
            on_error=self._on_error,
        )
        if not text:
            return None
        reply = text.strip()
        return reply or None


# ─────────────────────────────────────────────────────
# 共享工具
# ─────────────────────────────────────────────────────
async def _safe_complete(
    ai: Any,
    system: str,
    user: str,
    *,
    provider_tag: str | None,
    max_tokens: int,
    timeout_seconds: int,
    on_error: ErrorHook | None,
) -> str | None:
    """调用注入的 ``ctx.ai.complete`` 并把任何失败折叠为 ``None``。

    捕获 ``Exception`` 是刻意为之：AIUnavailableError / AIQuotaError / 超时 / provider
    未配置 / 意外异常都必须触发降级；``BaseException``（如 ``CancelledError``）不吞。
    """

    if ai is None:
        return None
    try:
        result = await ai.complete(
            system=system,
            user=user,
            provider_tag=provider_tag,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 所有 AI 失败统一降级
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:  # noqa: BLE001 - 观测回调不得影响降级
                pass
        return None
    text = getattr(result, "text", None)
    if not text:
        return None
    return str(text)


def _normalize_text(value: Any) -> str:
    """归一化：NFKC（全角转半角等）+ 去所有空白 + casefold。"""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(text.split())
    return text.casefold()


def _leading_word(text: str) -> str:
    match = re.match(r"[a-zA-Z]+", str(text or "").strip())
    return match.group(0).lower() if match else ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里尽力提取第一个 JSON 对象；失败返回 ``None``。"""

    body = str(text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001
        pass
    start = body.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(body)):
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                snippet = body[start : index + 1]
                try:
                    parsed = json.loads(snippet)
                except Exception:  # noqa: BLE001
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, Iterable):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


def _as_list(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable):
        return [str(item) for item in value if str(item) != ""]
    return [str(value)]


def _match_topic(bank: Mapping[str, Any], topic: str) -> str | None:
    topic = str(topic or "").strip()
    if not topic:
        return None
    if topic in bank:
        return topic
    lowered = topic.casefold()
    for key in bank:
        key_low = str(key).casefold()
        if key_low == lowered or key_low in lowered or lowered in key_low:
            return key
    return None


def _coerce_bank(
    bank: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for topic, entries in bank.items():
        normalized = [_normalize_bank_entry(item) for item in entries]
        normalized = [item for item in normalized if item is not None]
        if normalized:
            out[str(topic)] = normalized
    return out or _clone_bank(_EMBEDDED_QUIZ_BANK)


def _coerce_personas(
    personas: Mapping[str, Persona | Mapping[str, Any] | str],
) -> dict[str, Persona]:
    out: dict[str, Persona] = {}
    for key, value in personas.items():
        key = str(key)
        if isinstance(value, Persona):
            out[key] = value
        elif isinstance(value, str):
            out[key] = Persona(key, value)
        elif isinstance(value, Mapping):
            prompt = str(value.get("system_prompt") or value.get("prompt") or "").strip()
            if not prompt:
                continue
            out[key] = Persona(
                key,
                prompt,
                max_history=int(value.get("max_history", 6) or 6),
                max_tokens=int(value.get("max_tokens", 512) or 512),
            )
    return out


def _trim_history(
    history: Iterable[Any] | None,
    max_turns: int,
    max_chars: int,
) -> list[tuple[str, str]]:
    if not history or max_turns <= 0:
        return []
    turns: list[tuple[str, str]] = []
    for item in history:
        role, text = _coerce_turn(item)
        if text:
            turns.append((role, text))
    window = turns[-max_turns:]
    if max_chars > 0:
        while len(window) > 1 and sum(len(role) + len(text) for role, text in window) > max_chars:
            window = window[1:]
    return window


def _coerce_turn(item: Any) -> tuple[str, str]:
    if isinstance(item, Mapping):
        role = str(item.get("role") or item.get("from") or "user")
        text = str(item.get("text") or item.get("content") or "").strip()
        return _role_label(role), text
    if isinstance(item, (tuple, list)) and len(item) == 2:
        role, text = item
        return _role_label(str(role)), str(text or "").strip()
    return "用户", str(item or "").strip()


def _role_label(role: str) -> str:
    lowered = str(role or "").strip().lower()
    if lowered in {"assistant", "bot", "ai", "model", "助手"}:
        return "助手"
    return "用户"


def _render_history(window: Sequence[tuple[str, str]], user_text: str) -> str:
    lines = [f"{role}：{text}" for role, text in window]
    lines.append(f"用户：{user_text}")
    lines.append("助手：")
    return "\n".join(lines)


__all__ = [
    "AnswerJudge",
    "DEFAULT_PERSONAS",
    "JudgeOutcome",
    "Persona",
    "PersonaChat",
    "Quiz",
    "QuizMaker",
    "Verdict",
    "load_quiz_bank",
]
