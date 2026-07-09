"""LLM 路由器单元测试（Sprint2 #2 路由扩展）。

不连真 LLM；分类器路径用 monkeypatch 替换 ``_ask_classifier``。
覆盖：
- 规则层 7 条主路径都各自正常命中
- 视觉路径在没有 vision provider 时正确退化到文本规则
- 兜底链：classifier → fallback_provider_id → 第一个可用候选
- 候选池为空 / 全无 api_key → 抛 ValueError
- 选择策略：cheap-first / premium-first 在同 tag 多 provider 时排序正确
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services import llm_router


def _p(
    pid: int,
    *,
    tags: list[str] | None = None,
    modality: str = "text",
    cost_tier: int = 2,
    has_key: bool = True,
    provider_kind: str = "openai",
) -> dict[str, Any]:
    """造一个 provider dict（与 worker.runtime._refresh_command_context 输出格式一致）。"""
    return {
        "id": pid,
        "name": f"p{pid}",
        "provider": provider_kind,
        # ollama provider 视为不需要 key；其余 has_key=False 时 _has_api_key 返 False
        "api_key_enc": "fernet-token-fake" if has_key else None,
        "base_url": None,
        "default_model": "model",
        "modality": modality,
        "tags": list(tags or []),
        "cost_tier": cost_tier,
        "notes": None,
    }


# ════════════════════════════════════════════════════════════
# 1) 候选池为空 / 全无 key → ValueError
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pick_empty_pool_raises() -> None:
    with pytest.raises(ValueError):
        await llm_router.pick_provider(
            "你好", None, False, providers={}
        )


@pytest.mark.asyncio
async def test_pick_all_missing_key_raises() -> None:
    """所有 openai/anthropic provider 都没 key → 没有可用候选。"""
    pool = {
        1: _p(1, tags=["chat"], has_key=False),
        2: _p(2, tags=["chat"], has_key=False),
    }
    with pytest.raises(ValueError):
        await llm_router.pick_provider("hi", None, False, providers=pool)


@pytest.mark.asyncio
async def test_pick_ollama_no_key_is_still_candidate() -> None:
    """ollama 本地部署可不要 api_key，仍应作为候选。"""
    pool = {
        1: _p(1, tags=["chat"], has_key=False, provider_kind="ollama"),
    }
    d = await llm_router.pick_provider("hi", None, False, providers=pool)
    assert d.provider_id == 1


# ════════════════════════════════════════════════════════════
# 2) 规则层各分支
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_route_vision_via_replied_photo() -> None:
    """被回复消息含图 → 选 modality=vision。"""
    pool = {
        1: _p(1, tags=["chat"], modality="text"),
        2: _p(2, tags=["vision"], modality="vision", cost_tier=3),
    }
    d = await llm_router.pick_provider("这是什么", None, has_replied_photo=True, providers=pool)
    assert d.provider_id == 2
    assert d.matched_tag == "vision"


@pytest.mark.asyncio
async def test_route_vision_via_keyword() -> None:
    """消息里有"识别图"等关键词，且有 multimodal provider → 走视觉。"""
    pool = {
        1: _p(1, tags=["chat"]),
        9: _p(9, tags=["smart"], modality="multimodal", cost_tier=2),
    }
    d = await llm_router.pick_provider(
        "帮我识别图里的文字", None, has_replied_photo=False, providers=pool
    )
    assert d.provider_id == 9


@pytest.mark.asyncio
async def test_route_vision_no_vision_provider_falls_back_to_text() -> None:
    """有视觉关键词但池里没视觉 provider → 不能强行选，走后续文本规则。"""
    pool = {
        1: _p(1, tags=["chat"], modality="text"),
    }
    d = await llm_router.pick_provider(
        "帮我识别图里的文字", None, False, providers=pool
    )
    assert d.provider_id == 1
    assert d.matched_tag == "chat"


@pytest.mark.asyncio
async def test_route_code_via_fence() -> None:
    """消息含 ``` 围栏代码块 → tag=code。"""
    pool = {
        1: _p(1, tags=["chat"], cost_tier=1),
        2: _p(2, tags=["code", "smart"], cost_tier=3),
        3: _p(3, tags=["code", "cheap"], cost_tier=1),
    }
    d = await llm_router.pick_provider(
        "帮我看这段代码\n```python\nprint(1)\n```", None, False, providers=pool
    )
    # cheap-first：tier=1 的 #3 优先
    assert d.provider_id == 3
    assert d.matched_tag == "code"


@pytest.mark.asyncio
async def test_route_code_via_def_keyword() -> None:
    pool = {
        1: _p(1, tags=["code"], cost_tier=2),
    }
    d = await llm_router.pick_provider(
        "def fib(n):  # 怎么改成迭代版", None, False, providers=pool
    )
    assert d.matched_tag == "code"


@pytest.mark.asyncio
async def test_route_math() -> None:
    pool = {
        1: _p(1, tags=["chat"]),
        2: _p(2, tags=["math"], cost_tier=2),
    }
    d = await llm_router.pick_provider(
        "请算 12*34=? 再算 56+78=?", None, False, providers=pool
    )
    assert d.provider_id == 2
    assert d.matched_tag == "math"


@pytest.mark.asyncio
async def test_route_translate() -> None:
    pool = {
        1: _p(1, tags=["chat"]),
        7: _p(7, tags=["translate", "cheap"], cost_tier=1),
    }
    d = await llm_router.pick_provider(
        "请翻译为英文：你好世界", None, False, providers=pool
    )
    assert d.provider_id == 7


@pytest.mark.asyncio
async def test_route_long_context() -> None:
    long = "测试" * 1000  # 2000 chars
    pool = {
        1: _p(1, tags=["chat"]),
        2: _p(2, tags=["long_context", "cheap"], cost_tier=1),
    }
    d = await llm_router.pick_provider(
        "总结一下", long, False, providers=pool
    )
    assert d.provider_id == 2
    assert d.matched_tag == "long_context"


@pytest.mark.asyncio
async def test_route_reason_picks_premium() -> None:
    """reason 关键词命中：同 tag 时旗舰优先（cost_tier 降序）。"""
    pool = {
        1: _p(1, tags=["chat"]),
        2: _p(2, tags=["reason"], cost_tier=2),
        3: _p(3, tags=["reason", "smart"], cost_tier=3),
    }
    d = await llm_router.pick_provider(
        "为什么 React 18 引入并发模式？分析一下", None, False, providers=pool
    )
    assert d.provider_id == 3  # tier=3 旗舰优先
    assert d.matched_tag == "reason"


@pytest.mark.asyncio
async def test_route_chat_default_picks_cheapest() -> None:
    """什么都不匹配时走 chat，cost_tier 最低优先。"""
    pool = {
        1: _p(1, tags=["chat"], cost_tier=3),
        2: _p(2, tags=["chat"], cost_tier=1),
    }
    d = await llm_router.pick_provider("你今天怎么样", None, False, providers=pool)
    assert d.provider_id == 2  # 便宜的优先


# ════════════════════════════════════════════════════════════
# 3) 兜底链：classifier → fallback → 第一个可用
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_classifier_redirects_to_matching_tag(monkeypatch) -> None:
    """规则全失败时，classifier 返回 'code' → 路由到 tag=code provider。"""
    pool = {
        99: _p(99, tags=[], cost_tier=1),  # classifier 自身（无 chat tag 不会被规则命中）
        2: _p(2, tags=["code"], cost_tier=2),
    }

    # 让规则层无候选：消息既不含 code/math 等，也无 chat tag 候选
    async def fake_classifier(*_args, **_kw):
        return "code"

    monkeypatch.setattr(llm_router, "_ask_classifier", fake_classifier)
    d = await llm_router.pick_provider(
        "随便聊聊",
        None,
        False,
        providers=pool,
        classifier_provider_id=99,
    )
    assert d.provider_id == 2
    assert d.matched_tag == "code"


@pytest.mark.asyncio
async def test_classifier_call_uses_shared_invoke_with_budget_context(monkeypatch) -> None:
    """分类器兜底本身也是 LLM 调用，必须带账号预算上下文并写 router usage。"""
    from app.services import llm_invoke
    from app.services.llm_client import LLMResult

    captured: dict[str, Any] = {}

    async def fake_invoke(primary, providers, system, user, **kwargs):
        captured["primary"] = primary
        captured["providers"] = providers
        captured["system"] = system
        captured["user"] = user
        captured["kwargs"] = kwargs
        return LLMResult(text="code", model="router-small", input_tokens=3, output_tokens=1), primary, False

    monkeypatch.setattr(llm_invoke, "invoke", fake_invoke)

    label = await llm_router._ask_classifier(
        _p(99, tags=[], cost_tier=1),
        "随便聊聊",
        None,
        account_id=7,
        triggered_by_account_id=12,
    )

    assert label == "code"
    assert captured["kwargs"]["account_id"] == 7
    assert captured["kwargs"]["triggered_by_account_id"] == 12
    assert captured["kwargs"]["source"] == "router"
    assert captured["kwargs"]["max_tokens"] == 8
    assert captured["kwargs"]["matched_tag"] == "router"


@pytest.mark.asyncio
async def test_classifier_unknown_label_then_fallback(monkeypatch) -> None:
    """classifier 返回奇怪 label → 不命中；走 fallback_provider_id。"""
    pool = {
        99: _p(99, tags=[]),
        77: _p(77, tags=[]),  # fallback
    }

    async def fake_classifier(*_args, **_kw):
        return "weird-token"  # 不在白名单

    # 让分类器调用层直接返回奇怪 token；但 _ask_classifier 内部白名单会过滤掉，返 None
    # 这里直接 patch 上层使其返 None
    async def fake_none(*_args, **_kw):
        return None

    monkeypatch.setattr(llm_router, "_ask_classifier", fake_none)
    d = await llm_router.pick_provider(
        "??",
        None,
        False,
        providers=pool,
        classifier_provider_id=99,
        fallback_provider_id=77,
    )
    assert d.provider_id == 77
    assert "fallback" in d.reason


@pytest.mark.asyncio
async def test_fallback_when_no_classifier(monkeypatch) -> None:
    """没配 classifier 时也能走 fallback。"""
    pool = {
        77: _p(77, tags=[]),
        88: _p(88, tags=[], cost_tier=3),
    }
    d = await llm_router.pick_provider(
        "??",
        None,
        False,
        providers=pool,
        fallback_provider_id=88,
    )
    assert d.provider_id == 88


@pytest.mark.asyncio
async def test_last_resort_picks_cheapest_when_nothing_configured() -> None:
    """既无 classifier 又无 fallback → 候选池里 cost_tier 最低的兜底。"""
    pool = {
        1: _p(1, tags=[], cost_tier=3),
        2: _p(2, tags=[], cost_tier=1),
    }
    d = await llm_router.pick_provider("??", None, False, providers=pool)
    assert d.provider_id == 2


@pytest.mark.asyncio
async def test_fallback_id_no_key_skipped() -> None:
    """fallback_provider_id 指向一个无 key 的 provider → 跳过它走最后兜底。"""
    pool = {
        77: _p(77, tags=[], has_key=False),
        2: _p(2, tags=[], cost_tier=1),
    }
    d = await llm_router.pick_provider(
        "??", None, False, providers=pool, fallback_provider_id=77
    )
    assert d.provider_id == 2
