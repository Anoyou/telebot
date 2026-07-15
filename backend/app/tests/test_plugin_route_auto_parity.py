"""阶段 F 复审修复 #4：插件显式 route="auto" 必须复用共享 Router。

契约：对同一段内容（如代码提示），模板路由（``llm_router.pick_provider``）与插件
``ctx.ai`` 的显式 ``route="auto"`` 应选出**同一个** Provider——即插件 auto 不再只挑
最便宜的 chat，而是走内容路由（code/math/vision/reason 规则）。

同时验证：
- 插件 auto **禁用**分类器与全局 fallback（宿主级能力，插件不可用）。
- 旧参数全缺省的"推断 auto"保持向后兼容（chat 优先 / cost_tier 升序），不引入内容路由。
"""

from __future__ import annotations

import pytest

from app.services.llm_dto import LLMProviderDTO
from app.services.llm_router import pick_provider
from app.worker.plugins import ai_facade

_CODE_PROMPT = "帮我写一个 python 函数 def foo(): 返回斐波那契数列"


def _dto(
    provider_id: int,
    *,
    name: str,
    tags: list[str],
    cost_tier: int,
) -> LLMProviderDTO:
    return LLMProviderDTO(
        id=provider_id,
        name=name,
        provider="openai",
        api_format="chat_completions",
        base_url="https://example/v1",
        default_model=f"{name}-model",
        api_key_enc="enc",
        modality="text",
        tags=tags,
        cost_tier=cost_tier,
        models=[{"id": f"{name}-model", "enabled": True}],
    )


def _router_pool(dtos: list[LLMProviderDTO]) -> dict[int, dict]:
    pool: dict[int, dict] = {}
    for dto in dtos:
        d = dto.to_dict()
        d["api_key_enc"] = dto.api_key_enc
        pool[int(dto.id)] = d
    return pool


@pytest.mark.asyncio
async def test_template_and_plugin_auto_pick_same_provider_for_code() -> None:
    """代码内容：模板 Router 与插件显式 auto 应选同一个 code Provider。"""
    chat = _dto(1, name="chat", tags=["chat"], cost_tier=1)
    coder = _dto(2, name="coder", tags=["code"], cost_tier=3)
    dtos = [chat, coder]

    # 模板路由（共享 Router）。
    template_decision = await pick_provider(_CODE_PROMPT, None, False, _router_pool(dtos))

    # 插件显式 route="auto"。
    providers = {int(d.id): d for d in dtos}
    plugin_dto, plugin_tag, plugin_mode = await ai_facade._resolve_route(
        providers,
        provider=None,
        provider_tag=None,
        route="auto",
        user_content=_CODE_PROMPT,
    )

    assert plugin_mode == "auto"
    # 关键断言：二者选出同一个 Provider（都命中 code Provider，而不是便宜的 chat）。
    assert plugin_dto.id == template_decision.provider_id == 2
    assert plugin_tag == "code"


@pytest.mark.asyncio
async def test_plugin_inferred_auto_stays_cheap_chat_backward_compatible() -> None:
    """旧行为：不传 route（推断 auto）仍走 chat 优先 / cost_tier 升序，不做内容路由。"""
    chat = _dto(1, name="chat", tags=["chat"], cost_tier=1)
    coder = _dto(2, name="coder", tags=["code"], cost_tier=3)
    providers = {1: chat, 2: coder}

    dto, _tag, mode = await ai_facade._resolve_route(
        providers,
        provider=None,
        provider_tag=None,
        route=None,  # 推断 auto
        user_content=_CODE_PROMPT,  # 即使给了代码内容，推断 auto 也不做内容路由
    )
    assert mode == "auto"
    # 推断 auto 保持便宜 chat（向后兼容），不因内容是代码而切到 coder。
    assert dto.id == 1


@pytest.mark.asyncio
async def test_plugin_auto_does_not_use_classifier_or_fallback() -> None:
    """插件 auto 只在候选内做内容路由；无 code 命中时回落到 chat 兜底，不调用分类器。"""
    # 只有 chat / general，无 code 标签：代码内容也无处可选 → 回落 chat 兜底。
    chat = _dto(1, name="chat", tags=["chat"], cost_tier=1)
    general = _dto(2, name="general", tags=["general"], cost_tier=2)
    providers = {1: chat, 2: general}

    dto, _tag, mode = await ai_facade._resolve_route(
        providers,
        provider=None,
        provider_tag=None,
        route="auto",
        user_content=_CODE_PROMPT,
    )
    assert mode == "auto"
    # 无 code Provider 时共享 Router 回落到便宜候选；chat 优先。
    assert dto.id == 1
