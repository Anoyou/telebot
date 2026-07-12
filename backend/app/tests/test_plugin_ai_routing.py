"""阶段 E：插件统一路由测试。

覆盖：
- `_resolve_route` 的 fixed / tag / auto 三种模式与旧参数推断兼容。
- `route=fixed` 缺 provider、`route=tag` 缺 provider_tag 时报错。
- `require_tools=True`（run_agent）预先排除无已启用模型的 Provider。
- 脱敏路由摘要不含 api_key / base_url / 代理。
- `_enabled_model_for_dto` 严格取已启用模型。
"""

from __future__ import annotations

import pytest

from app.services.llm_dto import LLMProviderDTO
from app.worker.plugins import ai_facade
from app.worker.plugins.ai_facade import AIUnavailableError


def _provider(
    provider_id: int,
    *,
    name: str = "p",
    api_key_enc: str | None = "enc",
    tags: list[str] | None = None,
    cost_tier: int = 2,
    api_format: str = "chat_completions",
    client_identity_profile: str = "auto",
    models: list[dict] | None = None,
    default_model: str = "gpt-test",
) -> LLMProviderDTO:
    return LLMProviderDTO(
        id=provider_id,
        name=name,
        provider="openai",
        api_format=api_format,
        base_url="https://secret-base.example/v1",
        default_model=default_model,
        api_key_enc=api_key_enc,
        proxy_url="socks5://user:pass@127.0.0.1:1080",
        modality="text",
        tags=tags if tags is not None else ["chat"],
        cost_tier=cost_tier,
        client_identity_profile=client_identity_profile,
        models=models if models is not None else [{"id": default_model, "enabled": True}],
    )


# ── _resolve_route: 三种模式 ────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_route_fixed_by_explicit_mode() -> None:
    pool = {1: _provider(1, name="a"), 2: _provider(2, name="b")}
    dto, tag, mode = await ai_facade._resolve_route(pool, provider=2, provider_tag=None, route="fixed")
    assert dto.id == 2
    assert mode == "fixed"
    assert tag is None


@pytest.mark.asyncio
async def test_resolve_route_fixed_missing_provider_raises() -> None:
    pool = {1: _provider(1)}
    with pytest.raises(AIUnavailableError):
        await ai_facade._resolve_route(pool, provider=None, provider_tag=None, route="fixed")


@pytest.mark.asyncio
async def test_resolve_route_tag_picks_cheapest() -> None:
    pool = {
        1: _provider(1, name="expensive", tags=["code"], cost_tier=3),
        2: _provider(2, name="cheap", tags=["code"], cost_tier=1),
    }
    dto, tag, mode = await ai_facade._resolve_route(pool, provider=None, provider_tag="code", route="tag")
    assert dto.id == 2
    assert tag == "code"
    assert mode == "tag"


@pytest.mark.asyncio
async def test_resolve_route_tag_missing_tag_raises() -> None:
    pool = {1: _provider(1)}
    with pytest.raises(AIUnavailableError):
        await ai_facade._resolve_route(pool, provider=None, provider_tag=None, route="tag")


@pytest.mark.asyncio
async def test_resolve_route_tag_no_match_raises() -> None:
    pool = {1: _provider(1, tags=["chat"])}
    with pytest.raises(AIUnavailableError):
        await ai_facade._resolve_route(pool, provider=None, provider_tag="vision", route="tag")


@pytest.mark.asyncio
async def test_resolve_route_auto_prefers_chat_cheapest() -> None:
    pool = {
        1: _provider(1, name="chat-hi", tags=["chat"], cost_tier=3),
        2: _provider(2, name="chat-lo", tags=["chat"], cost_tier=1),
        3: _provider(3, name="misc", tags=["math"], cost_tier=1),
    }
    dto, tag, mode = await ai_facade._resolve_route(pool, provider=None, provider_tag=None, route="auto")
    assert dto.id == 2
    assert tag == "chat"
    assert mode == "auto"


# ── 旧参数推断兼容 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_route_infers_fixed_when_provider_given() -> None:
    pool = {1: _provider(1, name="a"), 2: _provider(2, name="b")}
    dto, _tag, mode = await ai_facade._resolve_route(pool, provider="b", provider_tag=None, route=None)
    assert dto.id == 2
    assert mode == "fixed"


@pytest.mark.asyncio
async def test_resolve_route_infers_tag_when_only_tag_given() -> None:
    pool = {1: _provider(1, tags=["code"])}
    _dto, tag, mode = await ai_facade._resolve_route(pool, provider=None, provider_tag="code", route=None)
    assert tag == "code"
    assert mode == "tag"


@pytest.mark.asyncio
async def test_resolve_route_infers_auto_when_nothing_given() -> None:
    pool = {1: _provider(1, tags=["chat"])}
    _dto, _tag, mode = await ai_facade._resolve_route(pool, provider=None, provider_tag=None, route=None)
    assert mode == "auto"


# ── require_tools 预排除无启用模型的 Provider ────────────────


@pytest.mark.asyncio
async def test_resolve_route_require_tools_excludes_providers_without_enabled_models() -> None:
    pool = {
        1: _provider(1, name="no-models", models=[], default_model=""),
        2: _provider(2, name="disabled", models=[{"id": "m", "enabled": False}], default_model=""),
        3: _provider(3, name="usable", models=[{"id": "m3", "enabled": True}]),
    }
    dto, _tag, _mode = await ai_facade._resolve_route(
        pool, provider=None, provider_tag=None, route="auto", require_tools=True
    )
    assert dto.id == 3


@pytest.mark.asyncio
async def test_resolve_route_require_tools_all_excluded_raises() -> None:
    pool = {1: _provider(1, models=[{"id": "m", "enabled": False}], default_model="")}
    with pytest.raises(AIUnavailableError):
        await ai_facade._resolve_route(
            pool, provider=None, provider_tag=None, route="auto", require_tools=True
        )


@pytest.mark.asyncio
async def test_resolve_route_require_tools_excludes_models_without_tools() -> None:
    pool = {
        1: _provider(1, models=[{"id": "plain", "enabled": True, "supports_tools": False}]),
        2: _provider(2, models=[{"id": "agent", "enabled": True, "supports_tools": True}]),
    }
    dto, _tag, _mode = await ai_facade._resolve_route(
        pool, provider=None, provider_tag=None, route=None, require_tools=True
    )
    assert dto.id == 2


def test_tools_model_skips_unsupported_default() -> None:
    dto = _provider(
        1,
        default_model="plain",
        models=[
            {"id": "plain", "enabled": True, "supports_tools": False},
            {"id": "agent", "enabled": True, "supports_tools": True},
        ],
    )
    assert ai_facade._tools_model_for_dto(dto) == "agent"


def test_tools_model_rejects_explicit_unsupported_or_disabled_model() -> None:
    dto = _provider(
        1,
        models=[
            {"id": "plain", "enabled": True, "supports_tools": False},
            {"id": "disabled-agent", "enabled": False, "supports_tools": True},
        ],
    )
    assert ai_facade._tools_model_for_dto(dto, "plain") is None
    assert ai_facade._tools_model_for_dto(dto, "disabled-agent") is None


# ── _enabled_model_for_dto ─────────────────────────────────


def test_enabled_model_prefers_explicit() -> None:
    dto = _provider(1, models=[{"id": "a", "enabled": True}])
    assert ai_facade._enabled_model_for_dto(dto, "custom") == "custom"


def test_enabled_model_prefers_default_when_enabled() -> None:
    dto = _provider(
        1,
        default_model="d",
        models=[{"id": "a", "enabled": True}, {"id": "d", "enabled": True}],
    )
    assert ai_facade._enabled_model_for_dto(dto, None) == "d"


def test_enabled_model_falls_back_to_first_enabled() -> None:
    dto = _provider(
        1,
        default_model="not-enabled",
        models=[{"id": "a", "enabled": False}, {"id": "b", "enabled": True}],
    )
    assert ai_facade._enabled_model_for_dto(dto, None) == "b"


def test_enabled_model_no_enabled_uses_default_model() -> None:
    dto = _provider(1, default_model="d", models=[{"id": "a", "enabled": False}])
    assert ai_facade._enabled_model_for_dto(dto, None) == "d"


# ── 脱敏路由摘要 ────────────────────────────────────────────


def test_routing_summary_is_sanitized() -> None:
    dto = _provider(1, name="prov", api_format="responses", client_identity_profile="codex_cli")
    summary = ai_facade._routing_summary(
        dto, mode="auto", matched_tag="chat", selected_model="gpt-test", used_fallback=False
    )
    assert summary["provider_id"] == 1
    assert summary["provider_name"] == "prov"
    assert summary["mode"] == "auto"
    assert summary["matched_tag"] == "chat"
    assert summary["model"] == "gpt-test"
    assert summary["api_format"] == "responses"
    assert summary["client_identity_profile"] == "codex_cli"
    assert summary["used_fallback"] is False
    # 绝不泄露敏感字段。
    blob = repr(summary)
    assert "api_key" not in blob
    assert "secret-base.example" not in blob
    assert "socks5" not in blob
