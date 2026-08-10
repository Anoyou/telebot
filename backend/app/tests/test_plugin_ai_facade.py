from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.llm_agent import AgentResult
from app.services.llm_client import LLMCallFailed, LLMError, LLMResult, LLMStreamChunk
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import ModelResponse, ModelUsage, StopReason, TextContent
from app.util.proxy import ProxyConfigError
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
    default_model: str = "gpt-test",
    models: list[dict[str, Any]] | None = None,
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
        tags=tags or ["chat"],
        cost_tier=cost_tier,
        models=models
        if models is not None
        else [
            {
                "id": "gpt-test",
                "label": "Test",
                "enabled": True,
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
async def test_legacy_proxy_provider_is_excluded_before_plugin_ai_client_build(
    monkeypatch,
) -> None:  # noqa: ANN001
    provider_row = SimpleNamespace(proxy_id=8)
    legacy_proxy = SimpleNamespace(
        id=8,
        type="mtproxy",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )

    class _Result:
        def __init__(self, rows) -> None:  # noqa: ANN001
            self._rows = rows

        def scalars(self):  # noqa: ANN201
            return self

        def all(self):  # noqa: ANN201
            return self._rows

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN002
            return None

        async def execute(self, _query):  # noqa: ANN001, ANN201
            self.calls += 1
            return _Result([provider_row] if self.calls == 1 else [legacy_proxy])

    monkeypatch.setattr(ai_facade, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(
        ai_facade.LLMProviderDTO,
        "from_orm_row",
        staticmethod(lambda _row: _provider(1)),
    )
    client_built = False

    def _build_client(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal client_built
        client_built = True
        raise AssertionError("旧代理 Provider 不得进入 LLM 客户端")

    monkeypatch.setattr(ai_facade, "build_llm_client", _build_client)
    facade = PluginAI(account_id=7, plugin_key="demo")

    with pytest.raises(AIUnavailableError, match="没有可用的 LLM provider"):
        await facade.complete("sys", "hello")

    assert client_built is False


def test_proxy_url_projection_rejects_missing_proxy() -> None:
    with pytest.raises(ProxyConfigError, match="代理不存在"):
        ai_facade._proxy_url_from_row(None)


def test_proxy_url_projection_rejects_broken_proxy_credentials(monkeypatch) -> None:
    proxy = SimpleNamespace(
        type="socks5",
        host="proxy.example",
        port=1080,
        username="alice",
        password_enc=b"broken",
    )
    monkeypatch.setattr(
        ai_facade,
        "decrypt_str",
        lambda _value: (_ for _ in ()).throw(ValueError("bad master key")),
    )

    with pytest.raises(ProxyConfigError, match="凭据无法解密"):
        ai_facade._proxy_url_from_row(proxy)


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
async def test_complete_auto_routes_to_enabled_non_default_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _loader():
        return {
            1: _provider(
                1,
                default_model="gpt-disabled-default",
                models=[
                    {"id": "gpt-disabled-default", "enabled": False},
                    {"id": "gpt-enabled", "enabled": True},
                ],
            )
        }

    async def _invoke(primary, providers, system, user, **kwargs):
        captured.update(kwargs)
        return (
            LLMResult(text="ok", model="gpt-enabled", input_tokens=1, output_tokens=1),
            primary,
            False,
        )

    monkeypatch.setattr(ai_facade, "invoke_ai_runtime", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    result = await facade.complete("sys", "hello", route="auto")

    assert captured["override_model"] is None
    assert captured["routed_model"] == "gpt-enabled"
    assert result.routing["model"] == "gpt-enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_model", [None, "gpt-disabled"])
async def test_complete_cannot_bypass_disabled_model_list(monkeypatch, explicit_model) -> None:
    async def _loader():
        return {
            1: _provider(
                1,
                default_model="gpt-disabled",
                models=[{"id": "gpt-disabled", "enabled": False}],
            )
        }

    acquire = AsyncNoop(return_value=object())
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", acquire)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIUnavailableError, match="未在 .* 中启用|没有已启用模型"):
        await facade.complete("sys", "hello", model=explicit_model)

    assert acquire.calls == []


@pytest.mark.asyncio
async def test_complete_accepts_plugin_compat_aliases(monkeypatch) -> None:
    """兼容已迁移插件传入的 timeout_seconds / override_model / tags 形态。"""

    captured: dict[str, Any] = {}

    async def _loader():
        return {
            1: _provider(1, name="chat", tags=["chat"], cost_tier=1),
            2: _provider(
                2,
                name="long",
                tags=["long_context"],
                cost_tier=2,
                models=[{"id": "gpt-override", "enabled": True}],
            ),
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


@pytest.mark.asyncio
async def test_agent_requires_separate_permission() -> None:
    async def _loader():
        return {1: _provider(1, api_format="responses")}

    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIUnavailableError, match="ai_agent"):
        await facade.run_agent("sys", "user", handlers={})


@pytest.mark.asyncio
async def test_agent_uses_manifest_allowlist_and_shared_runtime(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _loader():
        return {1: _provider(1, api_format="responses")}

    async def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments.get("id")}

    async def _run_agent(model_call, request, tools, **kwargs):
        captured["request"] = request
        captured["tools"] = tools
        captured["callbacks"] = kwargs["callbacks"]
        response = await model_call(request)
        return AgentResult(
            text=response.text,
            model=response.model,
            messages=request.messages,
            usage=response.usage,
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    async def _invoke(primary, providers, request, **kwargs):
        captured["source"] = kwargs["source"]
        return (
            ModelResponse(
                model=request.model,
                content=(TextContent("done"),),
                usage=ModelUsage(input_tokens=2, output_tokens=1),
            ),
            primary,
            False,
        )

    monkeypatch.setattr(ai_facade, "run_agent", _run_agent)
    monkeypatch.setattr(ai_facade, "invoke_structured", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    facade = PluginAI(
        account_id=7,
        plugin_key="demo",
        provider_loader=_loader,
        allow_agent=True,
        manifest={
            "capabilities": {"agent_tools": {"enabled": True}},
            "agent_tools": [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    result = await facade.run_agent("sys", "user", handlers={"lookup": lookup})

    assert result.text == "done"
    assert list(captured["tools"]) == ["lookup"]
    assert captured["source"] == "plugin:demo:agent"
    assert captured["callbacks"] is not None
    assert result.routing["mode"] == "auto"
    assert result.routing["provider_id"] == 1
    assert result.routing["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_agent_sticks_to_provider_after_fallback(monkeypatch) -> None:
    providers = {
        1: _provider(1, name="gateway", api_format="responses"),
        2: _provider(2, name="direct", api_format="responses"),
    }
    starts: list[int] = []

    async def _loader():
        return providers

    async def lookup(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def _run_agent(model_call, request, _tools, **_kwargs):
        await model_call(request)
        response = await model_call(request)
        return AgentResult(
            text=response.text,
            model=response.model,
            messages=request.messages,
            usage=response.usage,
            steps=2,
            tool_calls=1,
            stop_reason=StopReason.COMPLETED,
        )

    async def _invoke(primary, _providers, request, **kwargs):
        starts.append(primary.id)
        actual = providers[2] if len(starts) == 1 else primary
        progress = kwargs.get("progress_callback")
        if progress is not None:
            await progress({"type": "model_attempt", "provider_id": actual.id, "model": request.model})
        return (
            ModelResponse(
                model=request.model,
                content=(TextContent("done"),),
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            actual,
            actual.id != primary.id,
        )

    monkeypatch.setattr(ai_facade, "run_agent", _run_agent)
    monkeypatch.setattr(ai_facade, "invoke_structured", _invoke)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", AsyncNoop())
    facade = PluginAI(
        account_id=7,
        plugin_key="demo",
        provider_loader=_loader,
        allow_agent=True,
        manifest={
            "capabilities": {"agent_tools": {"enabled": True}},
            "agent_tools": [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    result = await facade.run_agent("sys", "user", handlers={"lookup": lookup})

    assert result.text == "done"
    assert starts == [1, 2]
    assert result.provider_id == 2


@pytest.mark.asyncio
async def test_agent_failure_settles_plugin_quota_with_consumed_tokens(monkeypatch) -> None:
    async def _loader():
        return {1: _provider(1, api_format="responses")}

    async def lookup(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def _run_agent(_model_call, _request, _tools, **kwargs):
        await kwargs["callbacks"].on_usage(ModelUsage(input_tokens=7, output_tokens=5))
        raise RuntimeError("agent failed after model usage")

    release = AsyncNoop()
    monkeypatch.setattr(ai_facade, "run_agent", _run_agent)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", AsyncNoop(return_value=object()))
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", release)
    facade = PluginAI(
        account_id=7,
        plugin_key="demo",
        provider_loader=_loader,
        allow_agent=True,
        manifest={
            "capabilities": {"agent_tools": {"enabled": True}},
            "agent_tools": [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    with pytest.raises(RuntimeError, match="failed after model usage"):
        await facade.run_agent("sys", "user", handlers={"lookup": lookup})

    assert release.calls[0][0][1] == 12


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
        budget_calls["settle"] = (
            ticket,
            actual_tokens,
            actual_provider.id if actual_provider else None,
            success,
        )

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

    deltas = [delta async for delta in facade.stream_complete("sys", "hello", max_tokens=9999, timeout=99)]

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


def _install_stream_test_doubles(monkeypatch, client, *, api_format: str = "responses"):
    usage_records: list[Any] = []
    quota_release = AsyncNoop()
    budget_settle = AsyncNoop()

    async def _loader():
        return {1: _provider(1, api_format=api_format)}

    async def _quota_acquire(plugin_key, account_id, estimated_tokens):
        return ai_facade.plugin_ai_quota.PluginAIQuotaTicket(
            plugin_key,
            account_id,
            estimated_tokens,
        )

    async def _budget_acquire(account_id, provider, estimated_tokens):
        return ai_facade.llm_account_budget.LLMAccountBudgetTicket(
            account_id,
            provider.id,
            estimated_tokens,
            backend="test",
        )

    async def _emit_usage(record):
        usage_records.append(record)

    monkeypatch.setattr(ai_facade, "build_llm_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", _quota_acquire)
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "release", quota_release)
    monkeypatch.setattr(ai_facade.llm_account_budget, "acquire", _budget_acquire)
    monkeypatch.setattr(ai_facade.llm_account_budget, "settle", budget_settle)
    monkeypatch.setattr(ai_facade.llm_runtime, "_emit_usage", _emit_usage)
    facade = PluginAI(
        account_id=7,
        plugin_key="demo",
        provider_loader=_loader,
        max_tokens_limit=8,
        timeout_limit_seconds=9,
    )
    return facade, quota_release, budget_settle, usage_records


@pytest.mark.asyncio
async def test_stream_complete_aclose_after_first_delta_keeps_conservative_charge(monkeypatch) -> None:
    class _Client:
        async def stream_complete(self, *_args, **_kwargs):
            yield LLMStreamChunk(delta="partial", model="gpt-stream")
            await asyncio.sleep(10)

    facade, quota_release, budget_settle, usage_records = _install_stream_test_doubles(
        monkeypatch,
        _Client(),
    )
    stream = facade.stream_complete("sys", "hello", max_tokens=8)

    assert await anext(stream) == "partial"
    await stream.aclose()

    assert quota_release.calls[-1][0][1] == 10
    settle_kwargs = budget_settle.calls[-1][1]
    assert settle_kwargs["actual_tokens"] == 10
    assert settle_kwargs["success"] is False
    assert settle_kwargs["charge"] is True
    assert usage_records[-1].error_type == "consumer_closed"
    assert usage_records[-1].input_tokens == 10


@pytest.mark.asyncio
async def test_stream_complete_task_cancel_keeps_conservative_charge(monkeypatch) -> None:
    entered = asyncio.Event()

    class _Client:
        async def stream_complete(self, *_args, **_kwargs):
            entered.set()
            await asyncio.sleep(10)
            yield LLMStreamChunk(delta="never")

    facade, quota_release, budget_settle, usage_records = _install_stream_test_doubles(
        monkeypatch,
        _Client(),
    )

    async def consume() -> None:
        async for _ in facade.stream_complete("sys", "hello", max_tokens=8):
            pass

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert quota_release.calls[-1][0][1] == 10
    assert budget_settle.calls[-1][1]["charge"] is True
    assert usage_records[-1].error_type == "cancelled"


@pytest.mark.asyncio
async def test_stream_complete_partial_output_then_error_keeps_conservative_charge(monkeypatch) -> None:
    class _Client:
        async def stream_complete(self, *_args, **_kwargs):
            yield LLMStreamChunk(delta="partial", model="gpt-stream")
            raise LLMError("upstream disconnected")

    facade, quota_release, budget_settle, usage_records = _install_stream_test_doubles(
        monkeypatch,
        _Client(),
    )

    with pytest.raises(AIUnavailableError, match="upstream disconnected"):
        async for _ in facade.stream_complete("sys", "hello", max_tokens=8):
            pass

    assert quota_release.calls[-1][0][1] == 10
    assert budget_settle.calls[-1][1]["charge"] is True
    assert usage_records[-1].error_type == "LLMError"


@pytest.mark.asyncio
async def test_stream_complete_rejects_natural_eof_without_done(monkeypatch) -> None:
    class _Client:
        async def stream_complete(self, *_args, **_kwargs):
            yield LLMStreamChunk(delta="partial", model="gpt-stream")

    facade, quota_release, budget_settle, usage_records = _install_stream_test_doubles(
        monkeypatch,
        _Client(),
    )

    with pytest.raises(AIUnavailableError, match="最终状态"):
        async for _ in facade.stream_complete("sys", "hello", max_tokens=8):
            pass

    assert quota_release.calls[-1][0][1] == 10
    assert budget_settle.calls[-1][1]["charge"] is True
    assert usage_records[-1].success is False


@pytest.mark.asyncio
async def test_stream_complete_timeout_has_actionable_message(monkeypatch) -> None:
    class _Client:
        async def stream_complete(self, *_args, **_kwargs):
            await asyncio.sleep(10)
            yield LLMStreamChunk(delta="never")

    facade, _quota_release, _budget_settle, usage_records = _install_stream_test_doubles(
        monkeypatch,
        _Client(),
    )
    monkeypatch.setattr(facade, "_clamp_timeout", lambda _value: 0.01)

    with pytest.raises(AIUnavailableError, match="超过 0.01 秒总超时时间"):
        async for _ in facade.stream_complete("sys", "hello", max_tokens=8):
            pass

    assert usage_records[-1].error_type == "timeout"


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
async def test_stream_complete_chat_completions_success(monkeypatch) -> None:
    class _Client:
        async def stream_complete(self, *_args, **_kwargs):
            yield LLMStreamChunk(delta="chat")
            yield LLMStreamChunk(done=True, input_tokens=2, output_tokens=1)

    facade, quota_release, _budget_settle, usage_records = _install_stream_test_doubles(
        monkeypatch,
        _Client(),
        api_format="chat_completions",
    )

    deltas = [delta async for delta in facade.stream_complete("sys", "hello", max_tokens=8)]

    assert deltas == ["chat"]
    assert quota_release.calls[-1][0][1] == 3
    assert usage_records[-1].success is True


@pytest.mark.asyncio
async def test_stream_complete_rejects_explicit_disabled_model_before_quota(monkeypatch) -> None:
    async def _loader():
        return {
            1: _provider(
                1,
                api_format="responses",
                default_model="gpt-disabled",
                models=[{"id": "gpt-disabled", "enabled": False}],
            )
        }

    acquire = AsyncNoop(return_value=object())
    monkeypatch.setattr(ai_facade.plugin_ai_quota, "acquire", acquire)
    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(AIUnavailableError, match="未在 provider primary 中启用"):
        async for _ in facade.stream_complete("sys", "hello", model="gpt-disabled"):
            pass

    assert acquire.calls == []


@pytest.mark.asyncio
async def test_stream_complete_protocol_error_lists_all_supported_formats() -> None:
    async def _loader():
        return {1: _provider(1, api_format="unsupported")}

    facade = PluginAI(account_id=7, plugin_key="demo", provider_loader=_loader)

    with pytest.raises(
        AIUnavailableError,
        match="chat_completions、responses 或 anthropic_messages",
    ):
        async for _ in facade.stream_complete("sys", "hello"):
            pass


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
