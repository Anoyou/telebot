"""``@<provider>`` inline override 解析 + 在 ``_run_ai`` 中的端到端行为。

不出网，用 SimpleNamespace + AsyncMock 模拟 telethon Message。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import llm_client as _lc
from app.services.llm_client import LLMResult
from app.worker import command as wcmd
from app.worker import inline_override as _io


@pytest.fixture(autouse=True)
def _disable_ai_refresh(monkeypatch):
    from app.services import llm_account_budget
    from app.worker import runtime as worker_runtime

    monkeypatch.setattr(worker_runtime, "_refresh_command_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        llm_account_budget,
        "acquire",
        AsyncMock(return_value=llm_account_budget.LLMAccountBudgetTicket(1, 1, 512)),
    )
    monkeypatch.setattr(llm_account_budget, "settle", AsyncMock(return_value=None))

# ── 静态 fixtures ──────────────────────────────────────────────


def _provider(
    pid: int,
    name: str,
    *,
    provider: str = "openai",
    modality: str = "text",
    default_model: str = "m",
    enabled_models: list[str] | None = None,
    has_key: bool = True,
) -> dict[str, object]:
    """构造一个 worker 用的 provider dict（与 runtime._build_provider_dict 同形）。"""
    return {
        "id": pid,
        "name": name,
        "provider": provider,
        "api_key_enc": b"x" if has_key else None,
        "base_url": None,
        "default_model": default_model,
        "modality": modality,
        "tags": [],
        "cost_tier": 2,
        "notes": None,
        "proxy_url": None,
        "models": [
            {"id": m, "enabled": True, "custom": False, "label": None}
            for m in (enabled_models or [])
        ],
    }


# ════════════════════════════════════════════════════════════
# 1) parse_inline_override 纯解析
# ════════════════════════════════════════════════════════════


def test_parse_no_at_returns_none() -> None:
    """args[0] 不以 @ 开头：返回 (None, args)，原样不动。"""
    out, rest = _io.parse_inline_override(["你好", "世界"], {})
    assert out is None
    assert rest == ["你好", "世界"]


def test_parse_empty_args() -> None:
    out, rest = _io.parse_inline_override([], {})
    assert out is None
    assert rest == []


def test_parse_at_list() -> None:
    out, rest = _io.parse_inline_override(["@list"], {})
    assert out is not None and out.kind == "list"
    assert rest == []


def test_parse_at_list_case_insensitive() -> None:
    out, _ = _io.parse_inline_override(["@LIST"], {})
    assert out is not None and out.kind == "list"


def test_parse_at_auto() -> None:
    out, rest = _io.parse_inline_override(["@auto", "复杂", "问题"], {})
    assert out is not None and out.kind == "auto"
    assert rest == ["复杂", "问题"]


def test_parse_at_provider_only() -> None:
    providers = {3: _provider(3, "Opus")}
    out, rest = _io.parse_inline_override(["@opus", "问题"], providers)
    assert out is not None and out.kind == "provider"
    assert out.provider_id == 3
    assert out.model is None
    assert rest == ["问题"]


def test_parse_at_provider_with_model() -> None:
    providers = {3: _provider(3, "Opus", default_model="x", enabled_models=["claude-opus-4"])}
    out, _ = _io.parse_inline_override(["@opus:claude-opus-4", "问题"], providers)
    assert out is not None and out.kind == "provider"
    assert out.provider_id == 3
    assert out.model == "claude-opus-4"


def test_parse_normalization_loose_matching() -> None:
    """大小写、连字符、下划线、空格都视为等价。"""
    providers = {3: _provider(3, "Mimo-CN")}
    for token in ["@mimocn", "@MIMO-CN", "@Mimo_cn", "@mimo cn"]:
        # 注意 "mimo cn" 在真 TG args 下其实会被空格切成两段；
        # 这里测的是同一段 token 内的归一化
        clean = token.replace(" ", "")
        out, _ = _io.parse_inline_override([clean], providers)
        assert out is not None and out.provider_id == 3, f"token={clean!r} 没匹中"


def test_parse_unknown_provider_raises_with_list() -> None:
    """找不到时报错，error message 含可用列表（友好引导）。"""
    providers = {3: _provider(3, "Opus")}
    with pytest.raises(_io.InlineOverrideError) as ei:
        _io.parse_inline_override(["@nonexistent", "问"], providers)
    msg = str(ei.value)
    assert "未找到" in msg
    assert "@Opus" in msg or "Opus" in msg  # 列表里有的 provider


def test_parse_model_not_in_enabled_raises() -> None:
    """指定的 model 不在 provider.models[].enabled → 拒绝（含可用列表）。"""
    providers = {3: _provider(3, "P", enabled_models=["a", "b"])}
    with pytest.raises(_io.InlineOverrideError) as ei:
        _io.parse_inline_override(["@p:nonexistent"], providers)
    assert "未启用或不存在" in str(ei.value)
    assert "可用：" in str(ei.value)


def test_parse_default_model_treated_as_enabled() -> None:
    """provider.models 没列出但 ==default_model：放行（允许新 provider 没填 enabled list 的兼容场景）。"""
    providers = {3: _provider(3, "P", default_model="dm", enabled_models=[])}
    out, _ = _io.parse_inline_override(["@p:dm"], providers)
    assert out is not None and out.model == "dm"


def test_parse_empty_model_after_colon_errors() -> None:
    providers = {3: _provider(3, "P")}
    with pytest.raises(_io.InlineOverrideError, match="冒号后不能为空"):
        _io.parse_inline_override(["@p:"], providers)


def test_parse_ambiguous_match_errors() -> None:
    """两条 provider normalize 后同名 → 拒绝（让用户去重命名）。"""
    providers = {1: _provider(1, "Opus"), 2: _provider(2, "OPUS")}
    with pytest.raises(_io.InlineOverrideError, match="多条"):
        _io.parse_inline_override(["@opus"], providers)


# ════════════════════════════════════════════════════════════
# 2) format_provider_list 输出
# ════════════════════════════════════════════════════════════


def test_format_provider_list_empty() -> None:
    out = _io.format_provider_list({})
    assert "尚未配置" in out


def test_format_provider_list_includes_marks() -> None:
    """vision/audio modality 与未配 key 都该在末尾标出来。"""
    providers = {
        1: _provider(1, "TextOnly"),
        2: _provider(2, "Mimo", modality="vision"),
        3: _provider(3, "NoKey", has_key=False),
    }
    out = _io.format_provider_list(providers)
    assert "@TextOnly" in out
    assert "@Mimo" in out and "vision" in out
    assert "@NoKey" in out and "未配 key" in out


def test_format_provider_list_ollama_no_keymark() -> None:
    """ollama 本地不需要 key——不应给"未配 key"标注。"""
    providers = {1: _provider(1, "Local", provider="ollama", has_key=False)}
    out = _io.format_provider_list(providers)
    assert "@Local" in out
    assert "未配 key" not in out


# ════════════════════════════════════════════════════════════
# 3) _run_ai 端到端：inline override 行为
# ════════════════════════════════════════════════════════════


def _bare_event() -> object:
    """构造一个无回复、自身无媒体的 event mock。"""
    self_msg = SimpleNamespace(
        id=20, photo=None, document=None, sticker=None, voice=None, audio=None,
        video=None, video_note=None, gif=None, geo=None, contact=None, poll=None,
        file=None, grouped_id=None,
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    return event


@pytest.mark.asyncio
async def test_run_ai_inline_at_list_returns_list_no_llm(monkeypatch) -> None:
    """``,ai @list`` 应直接 edit 列表，不调 LLM、不消耗 token。"""
    called = {"complete": False}

    class _Spy:
        async def complete(self, *a, **k):
            called["complete"] = True
            return LLMResult(text="x", model="m", input_tokens=0, output_tokens=0)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _Spy())

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={3: _provider(3, "Opus", modality="vision")},
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "fixed"}}
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@list"], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "可用 provider" in msg or "@Opus" in msg
    assert not called["complete"], "@list 不应调 LLM"


@pytest.mark.asyncio
async def test_run_ai_inline_at_provider_overrides_template_default(monkeypatch) -> None:
    """模板默认是 provider_id=3，`,ai @glm 你好` 应改走 5。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        def __init__(self, *, provider_id):
            self._pid = provider_id

        async def complete(self, system, user, max_tokens=512, images=None):
            captured["pid_used"] = self._pid
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    def _build(row, override_model=None, proxy_url=None):
        captured["override_model"] = override_model
        return _FakeLLM(provider_id=row.id)

    monkeypatch.setattr(_lc, "build_client", _build)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                3: _provider(3, "Opus"),
                5: _provider(5, "GLM", default_model="glm-4"),
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "fixed"}}
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@glm", "你好"], tpl, account_id=1)
    assert captured["pid_used"] == 5, "inline override 应胜过模板里的 provider_id"


@pytest.mark.asyncio
async def test_run_ai_inline_at_provider_with_model_overrides_model(monkeypatch) -> None:
    """``@p:specific-model`` 应把 build_client 的 override_model 改成具体值。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    def _build(row, override_model=None, proxy_url=None):
        captured["override_model"] = override_model
        return _FakeLLM()

    monkeypatch.setattr(_lc, "build_client", _build)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                3: _provider(3, "P", default_model="dm", enabled_models=["alpha", "beta"]),
            },
        )
    )
    tpl = {
        "name": "ai", "type": "ai",
        "config": {"provider_id": 3, "routing_mode": "fixed", "model": "dm"},  # 模板里写 dm
    }
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@p:beta", "q"], tpl, account_id=1)
    assert captured["override_model"] == "beta", "inline @name:model 应胜过 cfg.model"


@pytest.mark.asyncio
async def test_run_ai_inline_at_auto_forces_auto_routing(monkeypatch) -> None:
    """``,ai @auto`` 在 fixed 模板上仍能强制走 auto 路由。"""
    captured: dict[str, object] = {}

    async def _fake_pick(user_q, replied_text, has_photo, providers, **kw):
        captured["picked"] = True

        class _D:
            provider_id = 3
            reason = "rule"

        return _D()

    from app.services import llm_router as _lr
    monkeypatch.setattr(_lr, "pick_provider", _fake_pick)

    class _FakeLLM:
        async def complete(self, *a, **k):
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={3: _provider(3, "P")},
        )
    )
    # 模板配的是 fixed
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "fixed"}}
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@auto", "你好"], tpl, account_id=1)
    assert captured.get("picked") is True, "@auto 应强制走 pick_provider 即使模板是 fixed"


@pytest.mark.asyncio
async def test_run_ai_inline_provider_beats_auto_routing(monkeypatch) -> None:
    """``,ai @specific 你好`` 在 auto 模板上应**强制 fixed**——
    避免路由器把请求换到别的 provider。"""
    pick_called = {"n": 0}

    async def _fake_pick(*a, **k):
        pick_called["n"] += 1

        class _D:
            provider_id = 99  # 给个明显错的，证明 inline override 没让它走到这里
            reason = "should-not-be-used"

        return _D()

    from app.services import llm_router as _lr
    monkeypatch.setattr(_lr, "pick_provider", _fake_pick)

    class _FakeLLM:
        def __init__(self, pid):
            self.pid = pid

        async def complete(self, *a, **k):
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    used: dict[str, object] = {}

    def _build(row, override_model=None, proxy_url=None):
        used["pid"] = row.id
        return _FakeLLM(row.id)

    monkeypatch.setattr(_lc, "build_client", _build)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={3: _provider(3, "Opus"), 5: _provider(5, "GLM")},
        )
    )
    # 模板配 auto；inline 指定 @glm 应强制 fixed→5
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "auto"}}
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@glm", "你好"], tpl, account_id=1)
    assert used["pid"] == 5
    assert pick_called["n"] == 0, "inline @<name> 时不应再调路由器"


@pytest.mark.asyncio
async def test_run_ai_inline_unknown_provider_friendly_error(monkeypatch) -> None:
    """未知 provider name 应给可读的错误 + 可用列表，不调 LLM。"""

    class _Spy:
        async def complete(self, *a, **k):
            raise AssertionError("不该被调")

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _Spy())

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={3: _provider(3, "Opus")},
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "fixed"}}
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@nonexistent", "问"], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "未找到" in msg
    assert "@Opus" in msg or "Opus" in msg


@pytest.mark.asyncio
async def test_run_ai_inline_strips_override_from_user_msg(monkeypatch) -> None:
    """``@xxx`` 不应进 user prompt——否则模型可能把 "@xxx" 当问题正文。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, system, user, max_tokens=512, images=None):
            captured["user"] = user
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _FakeLLM())
    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={3: _provider(3, "P")},
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "fixed"}}
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@p", "解释一下"], tpl, account_id=1)
    assert "@p" not in captured["user"]
    assert "解释一下" in captured["user"]


@pytest.mark.asyncio
async def test_run_ai_inline_override_still_subject_to_vision_guard(monkeypatch) -> None:
    """inline @text-only-provider + 含图 → 反幻觉守卫仍生效（拒答）。

    inline override 让用户**故意**选了纯文本模型；这时含图必须拒答，
    不能因为是用户主动选的就放行——否则反幻觉规则等同没用。"""

    class _Spy:
        async def complete(self, *a, **k):
            raise AssertionError("含图但 modality=text 不该调 LLM")

    monkeypatch.setattr(_lc, "build_client", lambda *a, **k: _Spy())

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                3: _provider(3, "Vision", modality="vision"),
                5: _provider(5, "TextOnly", modality="text"),
            },
        )
    )
    tpl = {"name": "ai", "type": "ai", "config": {"provider_id": 3, "routing_mode": "fixed"}}
    self_msg = SimpleNamespace(
        id=20, photo=object(), document=None, sticker=None, voice=None, audio=None,
        video=None, video_note=None, gif=None, geo=None, contact=None, poll=None,
        file=None, grouped_id=None,
        download_media=AsyncMock(return_value=b"\xff\xd8\xff\xe0"),
    )
    event = AsyncMock()
    event.get_reply_message = AsyncMock(return_value=None)
    event.message = self_msg
    await wcmd._run_ai(AsyncMock(), event, ["@textonly", "这是啥"], tpl, account_id=1)
    msg = event.edit.call_args[0][0]
    assert "✗" in msg
    assert "vision" in msg or "识图" in msg


@pytest.mark.asyncio
async def test_run_ai_inline_provider_without_model_clears_template_model(monkeypatch) -> None:
    """``,ai @AnyGPT 问题`` 应清空模板里配的 model，让 build_client 用 AnyGPT.default_model。
    
    Bug 场景：
    - 模板配了 provider_id=1 (Mimo), model="mimo-v2.5"
    - 用户用 inline override: @AnyGPT (provider_id=2)
    - 旧逻辑：override_model 还是 "mimo-v2.5" → AnyGPT 收到错误的 model
    - 新逻辑：override_model 应为 None → build_client 用 AnyGPT.default_model
    """
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, *a, **k):
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    def _fake_build(provider_row, override_model=None, **kw):
        captured["provider_id"] = provider_row.id
        captured["override_model"] = override_model
        return _FakeLLM()

    monkeypatch.setattr(_lc, "build_client", _fake_build)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                1: _provider(1, "Mimo", default_model="mimo-v2.5"),
                2: _provider(2, "AnyGPT", default_model="gpt-4o"),
            },
        )
    )
    # 模板配的是 Mimo + mimo-v2.5
    tpl = {
        "name": "ai",
        "type": "ai",
        "config": {
            "provider_id": 1,
            "routing_mode": "fixed",
            "model": "mimo-v2.5",  # ← 模板里显式配了 model
        },
    }
    event = _bare_event()
    # 用户用 inline override 切到 AnyGPT，但没指定 :model
    await wcmd._run_ai(AsyncMock(), event, ["@AnyGPT", "测试"], tpl, account_id=1)
    
    assert captured["provider_id"] == 2, "应该用 AnyGPT (id=2)"
    assert captured["override_model"] is None, (
        "inline @name（未指定 :model）应清空 override_model，"
        "让 build_client 使用 AnyGPT.default_model，而不是用模板里的 mimo-v2.5"
    )


@pytest.mark.asyncio
async def test_run_ai_inline_provider_with_model_overrides_everything(monkeypatch) -> None:
    """``,ai @AnyGPT:claude-3-5-sonnet 问题`` 应同时覆盖 provider 和 model。"""
    captured: dict[str, object] = {}

    class _FakeLLM:
        async def complete(self, *a, **k):
            return LLMResult(text="ok", model="m", input_tokens=1, output_tokens=1)

    def _fake_build(provider_row, override_model=None, **kw):
        captured["provider_id"] = provider_row.id
        captured["override_model"] = override_model
        return _FakeLLM()

    monkeypatch.setattr(_lc, "build_client", _fake_build)

    wcmd.set_command_context(
        wcmd.CommandContext(
            account_id=1,
            templates={},
            providers={
                1: _provider(1, "Mimo", default_model="mimo-v2.5"),
                2: _provider(2, "AnyGPT", default_model="gpt-4o", enabled_models=[
                    "gpt-4o",
                    "claude-3-5-sonnet",
                ]),
            },
        )
    )
    tpl = {
        "name": "ai",
        "type": "ai",
        "config": {
            "provider_id": 1,
            "routing_mode": "fixed",
            "model": "mimo-v2.5",
        },
    }
    event = _bare_event()
    await wcmd._run_ai(AsyncMock(), event, ["@AnyGPT:claude-3-5-sonnet", "测试"], tpl, account_id=1)
    
    assert captured["provider_id"] == 2, "应该用 AnyGPT (id=2)"
    assert captured["override_model"] == "claude-3-5-sonnet", (
        "inline @name:model 应完全覆盖模板配置"
    )
