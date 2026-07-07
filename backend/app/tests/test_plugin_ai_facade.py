from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.llm_client import LLMCallFailed, LLMResult, LLMStreamChunk
from app.services.llm_dto import LLMProviderDTO
from app.worker.plugins import ai_facade
from app.worker.plugins.ai_facade import AIQuotaError, AIUnavailableError, PluginAI


def _provider(
    provider_id: int,
    *,
    name: str = "primary",
    api_key_enc: str | None = "encrypted-secret",
    tags: list[str] | None = None,
    cost_tier: int = 2,
    api_format: str = "chat_completions",
) -> LLMProviderDTO:
    return LLMProviderDTO(
        id=provider_id,
        name=name,
        provider="openai",
        api_format=api_format,
        base_url="https://secret-base.example/v1",
        default_model="gpt-test",
        api_key_enc=api_key_enc,
        proxy_url="socks5://user:pass@127.0.0.1:1080",
        modality="text",
        tags=tags or ["chat"],
        cost_tier=cost_tier,
        models=[
            {
                "id": "gpt-test",
                "label": "Test",
                "base_url": "https://model-secret.example",
                "api_key_enc": "model-secret",
            }
        ],
    )


@pytest.fixture(autouse=True)
def _enable_ai_feature(monkeypatch) -> None:
    async def _enabled(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(ai_facade, "is_ai_enabled", _enabled)


@pytest.mark.asyncio
async def test_list_providers_redacts_sensitive_metadata() -> None:
    async def _loader():
        return {1: _provider(1)}

    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    providers = await facade.list_providers()

    assert len(providers) == 1
    payload = providers[0].__dict__
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["has_api_key"] is True
    assert "api_key_enc" not in payload
    assert "base_url" not in payload
    assert "proxy_url" not in payload
    assert providers[0].models == [{"id": "gpt-test", "label": "Test"}]
    assert "encrypted-secret" not in encoded
    assert "secret-base" not in encoded
    assert "user:pass" not in encoded
    assert "model-secret" not in encoded


@pytest.mark.asyncio
async def test_ai_disabled_short_circuits_provider_loader(monkeypatch) -> None:
    invoked = False

    async def _disabled(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def _loader():
        nonlocal invoked
        invoked = True
        return {1: _provider(1)}

    monkeypatch.setattr(ai_facade, "is_ai_enabled", _disabled)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIUnavailableError, match="AI 能力已在系统设置中关闭"):
        await facade.list_providers()

    assert invoked is False


@pytest.mark.asyncio
async def test_complete_clamps_max_tokens_and_timeout(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    quota_calls: dict[str, Any] = {}

    async def _loader():
        return {1: _provider(1)}

    async def _invoke(primary, providers, system, user, **kwargs):
        captured.update(kwargs)
        return (
            LLMResult(text="ok", model="gpt-test", input_tokens=3, output_tokens=5),
            primary,
            False,
        )

    async def _acquire(plugin_key, account_id, estimated_tokens):
        quota_calls["acquire"] = (plugin_key, account_id, estimated_tokens)
        return object()

    async def _release(ticket, actual_tokens):
        quota_calls["release"] = (ticket, actual_tokens)

    monkeypatch.setattr(ai_facade, "invoke_ai_runtime", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", _acquire)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", _release)
    facade = PluginAI(
        account_id=7,
        plugin_key="demo",
        provider_loader=_loader,
        max_tokens_limit=64,
        timeout_limit_seconds=9,
    )

    result = await facade.complete("sys", "hello", max_tokens=9999, timeout=99)

    assert result.text == "ok"
    assert captured["max_tokens"] == 64
    assert captured["timeout_seconds"] == 9
    assert captured["source"] == "plugin:demo"
    assert captured["account_id"] == 7
    assert quota_calls["acquire"] == ("demo", 7, 66)
    assert quota_calls["release"][1] == 8


@pytest.mark.asyncio
async def test_complete_selects_provider_tag_without_exposing_api_key(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _loader():
        return {
            1: _provider(1, name="premium", tags=["code"], cost_tier=3),
            2: _provider(2, name="cheap", tags=["code"], cost_tier=1),
        }

    async def _invoke(primary, providers, system, user, **kwargs):
        captured["primary"] = primary
        captured["providers"] = providers
        captured.update(kwargs)
        return (
            LLMResult(text="selected", model="gpt-test", input_tokens=1, output_tokens=1),
            primary,
            False,
        )

    monkeypatch.setattr(ai_facade, "invoke_ai_runtime", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    result = await facade.complete("sys", "write code", provider_tag="code")

    assert result.provider_id == 2
    assert captured["primary"].name == "cheap"
    assert captured["matched_tag"] == "code"
    # The facade may pass internal DTOs to the runtime, but never returns them.
    assert not hasattr(result, "api_key_enc")


@pytest.mark.asyncio
async def test_complete_accepts_plugin_compat_aliases(monkeypatch) -> None:
    """兼容已迁移插件传入的 timeout_seconds / override_model / tags 形态。"""

    captured: dict[str, Any] = {}

    async def _loader():
        return {
            1: _provider(1, name="chat", tags=["chat"], cost_tier=1),
            2: _provider(2, name="long", tags=["long_context"], cost_tier=2),
        }

    async def _invoke(primary, providers, system, user, **kwargs):
        captured["primary"] = primary
        captured.update(kwargs)
        return (
            LLMResult(text="summary", model="gpt-override", input_tokens=1, output_tokens=2),
            primary,
            False,
        )

    monkeypatch.setattr(ai_facade, "invoke_ai_runtime", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    facade = PluginAI(
        account_id=7,
        plugin_key="sum",
        provider_loader=_loader,
        timeout_limit_seconds=30,
    )

    with pytest.warns(DeprecationWarning, match="ctx.ai.complete tag/tags"):
        result = await facade.complete(
            "sys",
            "messages",
            tags=["long_context"],
            override_model="gpt-override",
            timeout_seconds=12,
            source="plugin:sum",
        )

    assert result.provider_id == 2
    assert captured["matched_tag"] == "long_context"
    assert captured["override_model"] == "gpt-override"
    assert captured["timeout_seconds"] == 12


@pytest.mark.parametrize("error_type", ["budget_exceeded", "rate_limit"])
@pytest.mark.asyncio
async def test_quota_failures_are_mapped_to_plugin_error(monkeypatch, error_type: str) -> None:
    async def _loader():
        return {1: _provider(1)}

    async def _invoke(*_args, **_kwargs):
        raise LLMCallFailed(error_type, error_type=error_type)

    monkeypatch.setattr(ai_facade, "invoke_ai_runtime", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIQuotaError):
        await facade.complete("sys", "hello")


@pytest.mark.asyncio
async def test_stream_complete_yields_text_deltas_and_settles_quota(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    quota_calls: dict[str, Any] = {}
    budget_calls: dict[str, Any] = {}
    usage_records: list[Any] = []

    async def _loader():
        return {1: _provider(1, api_format="responses")}

    class _Client:
        async def stream_complete(self, system, user, **kwargs):
            captured["system"] = system
            captured["user"] = user
            captured.update(kwargs)
            yield LLMStreamChunk(delta="hel", model="gpt-stream")
            yield LLMStreamChunk(delta="lo", model="gpt-stream")
            yield LLMStreamChunk(
                done=True,
                model="gpt-stream",
                input_tokens=3,
                output_tokens=2,
            )

    def _build_client(provider, **kwargs):
        captured["provider"] = provider
        captured["build_kwargs"] = kwargs
        return _Client()

    async def _acquire(plugin_key, account_id, estimated_tokens):
        quota_calls["acquire"] = (plugin_key, account_id, estimated_tokens)
        return object()

    async def _release(ticket, actual_tokens):
        quota_calls["release"] = (ticket, actual_tokens)

    async def _budget_acquire(account_id, provider, estimated_tokens):
        budget_calls["acquire"] = (account_id, provider.id, estimated_tokens)
        return ai_facade.llm_account_budget.LLMAccountBudgetTicket(
            account_id,
            provider.id,
            estimated_tokens,
            backend="test",
        )

    async def _budget_settle(ticket, *, actual_tokens, actual_provider, success):
        budget_calls["settle"] = (ticket, actual_tokens, actual_provider.id if actual_provider else None, success)

    async def _emit_usage(record):
        usage_records.append(record)

    monkeypatch.setattr(ai_facade, "build_llm_client", _build_client)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", _acquire)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", _release)
    monkeypatch.setattr(ai_facade.llm_account_budget, "acquire", _budget_acquire)
    monkeypatch.setattr(ai_facade.llm_account_budget, "settle", _budget_settle)
    monkeypatch.setattr(ai_facade.llm_runtime, "_emit_usage", _emit_usage)
    facade = PluginAI(
        account_id=7,
        plugin_key="demo",
        provider_loader=_loader,
        max_tokens_limit=64,
        timeout_limit_seconds=9,
    )

    deltas = [
        delta
        async for delta in facade.stream_complete("sys", "hello", max_tokens=9999, timeout=99)
    ]

    assert deltas == ["hel", "lo"]
    assert captured["system"] == "sys"
    assert captured["user"] == "hello"
    assert captured["max_tokens"] == 64
    assert captured["timeout_seconds"] == 9
    assert captured["build_kwargs"]["api_format_override"] is None
    assert quota_calls["acquire"] == ("demo", 7, 66)
    assert quota_calls["release"][1] == 5
    assert budget_calls["acquire"] == (7, 1, 66)
    assert budget_calls["settle"][1:] == (5, 1, True)
    assert len(usage_records) == 1
    usage = usage_records[0]
    assert usage.source == "plugin:demo"
    assert usage.account_id == 7
    assert usage.provider_id == 1
    assert usage.provider_name == "primary"
    assert usage.model == "gpt-stream"
    assert usage.input_tokens == 3
    assert usage.output_tokens == 2
    assert usage.success is True
    assert usage.fallback_chain == ["primary"]


@pytest.mark.asyncio
async def test_stream_complete_account_budget_precheck_is_mapped_to_quota_error(monkeypatch) -> None:
    async def _loader():
        return {1: _provider(1, api_format="responses")}

    async def _quota_acquire(*_args, **_kwargs):
        return object()

    quota_release = AsyncNoop()
    usage_records: list[Any] = []
    budget_settle = AsyncNoop()
    invoked = False

    async def _budget_acquire(*_args, **_kwargs):
        raise ai_facade.llm_account_budget.LLMAccountBudgetExceeded("account budget exceeded")

    def _build_client(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("provider should not be built when account budget fails")

    async def _emit_usage(record):
        usage_records.append(record)

    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", _quota_acquire)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", quota_release)
    monkeypatch.setattr(ai_facade.llm_account_budget, "acquire", _budget_acquire)
    monkeypatch.setattr(ai_facade.llm_account_budget, "settle", budget_settle)
    monkeypatch.setattr(ai_facade.llm_runtime, "_emit_usage", _emit_usage)
    monkeypatch.setattr(ai_facade, "build_llm_client", _build_client)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIQuotaError, match="account budget exceeded"):
        async for _delta in facade.stream_complete("sys", "hello"):
            pass

    assert invoked is False
    assert quota_release.calls[0][0][1] == 0
    assert budget_settle.calls == []
    assert len(usage_records) == 1
    assert usage_records[0].source == "plugin:demo"
    assert usage_records[0].success is False
    assert usage_records[0].error_type == "budget_exceeded"


@pytest.mark.asyncio
async def test_stream_complete_rejects_non_streaming_provider(monkeypatch) -> None:
    async def _loader():
        return {1: _provider(1, api_format="chat_completions")}

    invoked = False

    def _build_client(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("chat_completions provider should not be built for streaming")

    monkeypatch.setattr(ai_facade, "build_llm_client", _build_client)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIUnavailableError, match="暂不支持 streaming"):
        async for _delta in facade.stream_complete("sys", "hello"):
            pass

    assert invoked is False


@pytest.mark.asyncio
async def test_stream_complete_ai_disabled_short_circuits_provider_loader(monkeypatch) -> None:
    invoked = False

    async def _disabled(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def _loader():
        nonlocal invoked
        invoked = True
        return {1: _provider(1, api_format="responses")}

    monkeypatch.setattr(ai_facade, "is_ai_enabled", _disabled)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIUnavailableError, match="AI 能力已在系统设置中关闭"):
        async for _delta in facade.stream_complete("sys", "hello"):
            pass

    assert invoked is False


@pytest.mark.asyncio
async def test_stream_complete_plugin_quota_precheck_is_mapped_to_quota_error(monkeypatch) -> None:
    async def _loader():
        return {1: _provider(1, api_format="responses")}

    async def _acquire(*_args, **_kwargs):
        raise ai_facade.plugin_ai_quota.PluginAIQuotaExceeded("quota exceeded")

    invoked = False

    def _build_client(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("provider should not be built when precheck fails")

    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", _acquire)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    monkeypatch.setattr(ai_facade, "build_llm_client", _build_client)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIQuotaError):
        async for _delta in facade.stream_complete("sys", "hello"):
            pass

    assert invoked is False


class AsyncNoop:
    def __init__(self, return_value=None) -> None:
        self.return_value = return_value
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any):
        self.calls.append((args, kwargs))
        return self.return_value


@pytest.mark.asyncio
async def test_plugin_quota_precheck_is_mapped_to_quota_error(monkeypatch) -> None:
    async def _loader():
        return {1: _provider(1)}

    async def _acquire(*_args, **_kwargs):
        raise ai_facade.plugin_ai_quota.PluginAIQuotaExceeded("quota exceeded")

    invoked = False

    async def _invoke(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("runtime should not be called when precheck fails")

    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", _acquire)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    monkeypatch.setattr(ai_facade, "invoke_ai_runtime", _invoke)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIQuotaError):
        await facade.complete("sys", "hello")

    assert invoked is False
