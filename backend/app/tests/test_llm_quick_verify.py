"""快速验证模型的临时凭据、模型发现与流式结果回归测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import commands as commands_api
from app.crypto import decrypt_str
from app.llm_probe_defaults import (
    QUICK_VERIFY_MAX_TOKENS,
    QUICK_VERIFY_MESSAGE,
    QUICK_VERIFY_SYSTEM_PROMPT,
)
from app.schemas.command import FetchModelsPreviewRequest, QuickVerifyProviderRequest
from app.services import llm_quick_verify
from app.services.llm_client import LLMError, LLMResult, LLMStreamChunk


async def _collect_events(**overrides):
    params = {
        "base_url": "https://api.example.test/v1",
        "api_key": "sk-quick-secret-12345678",
        "api_format": "responses",
        "protocol_profile": "standard",
        "client_identity_profile": "auto",
        "proxy_url": None,
        "model": None,
        "reasoning_effort": None,
        "system_prompt": "请直接回复。",
        "message": "你怎么又不行了？继续。",
        "max_tokens": 400,
        "timeout_seconds": 30,
    }
    params.update(overrides)
    return [event async for event in llm_quick_verify.quick_verify_events(**params)]


@pytest.mark.asyncio
async def test_quick_verify_discovers_model_and_streams_without_leaking_key(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm_quick_verify,
        "discover_models",
        AsyncMock(return_value=["gpt-5-mini", "gpt-5"]),
    )
    captured = {}

    class FakeClient:
        async def stream_complete(self, _system, _message, **_kwargs):
            yield LLMStreamChunk(delta="可以", model="gpt-5-mini-2026")
            yield LLMStreamChunk(delta="继续。", input_tokens=7, output_tokens=3, done=True)

    def build_client(dto, **_kwargs):
        captured["dto"] = dto
        return FakeClient()

    monkeypatch.setattr(llm_quick_verify, "build_client_from_dto", build_client)
    events = await _collect_events()

    assert [event["type"] for event in events] == ["discovery", "start", "delta", "delta", "done"]
    done = events[-1]
    assert done["ok"] is True
    assert done["requested_model"] == "gpt-5-mini"
    assert done["model"] == "gpt-5-mini-2026"
    assert done["response"] == "可以继续。"
    assert done["streaming"] is True
    assert captured["dto"].default_model == "gpt-5-mini"
    assert captured["dto"].api_key_enc != "sk-quick-secret-12345678"
    assert decrypt_str(captured["dto"].api_key_enc) == "sk-quick-secret-12345678"
    assert "sk-quick-secret-12345678" not in json.dumps(events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_quick_verify_explicit_model_skips_discovery(monkeypatch) -> None:
    async def fail_discovery(**_kwargs):
        raise AssertionError("显式模型不应请求模型列表")

    class FakeClient:
        async def stream_complete(self, _system, _message, **_kwargs):
            yield LLMStreamChunk(delta="已回复", model="manual-model")
            yield LLMStreamChunk(done=True, model="manual-model")

    monkeypatch.setattr(llm_quick_verify, "discover_models", fail_discovery)
    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: FakeClient(),
    )
    events = await _collect_events(model="manual-model")

    assert events[0] == {
        "type": "discovery",
        "model": "manual-model",
        "models": [],
        "api_format": "responses",
    }
    assert events[-1]["ok"] is True


@pytest.mark.asyncio
async def test_quick_verify_passes_connection_identity_and_reasoning_to_real_request(
    monkeypatch,
) -> None:
    captured = {}

    class FakeClient:
        async def stream_complete(self, _system, _message, **kwargs):
            captured["kwargs"] = kwargs
            yield LLMStreamChunk(delta="已回复", model="grok-reasoning")
            yield LLMStreamChunk(done=True, model="grok-reasoning")

    def build_client(dto, **_kwargs):
        captured["identity"] = dto.client_identity_profile
        captured["protocol"] = dto.protocol_profile
        captured["proxy"] = dto.proxy_url
        return FakeClient()

    monkeypatch.setattr(llm_quick_verify, "build_client_from_dto", build_client)
    events = await _collect_events(
        model="grok-reasoning",
        api_format="anthropic_messages",
        protocol_profile="claude_code_proxy",
        client_identity_profile="claude_code",
        proxy_url="socks5://proxy.example:1080",
        reasoning_effort="high",
    )

    assert events[-1]["ok"] is True
    assert captured["identity"] == "claude_code"
    assert captured["protocol"] == "claude_code_proxy"
    assert captured["proxy"] == "socks5://proxy.example:1080"
    assert captured["kwargs"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_quick_verify_discovery_failure_requests_model_and_redacts_key(
    monkeypatch,
) -> None:
    secret = "sk-discovery-secret-87654321"
    monkeypatch.setattr(
        llm_quick_verify,
        "discover_models",
        AsyncMock(side_effect=LLMError(f"upstream echoed {secret}")),
    )

    events = await _collect_events(api_key=secret)

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["requires_model"] is True
    assert secret not in json.dumps(events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_quick_verify_discovery_auth_failure_does_not_request_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm_quick_verify,
        "discover_models",
        AsyncMock(side_effect=LLMError("模型列表接口返回 401", status_code=401)),
    )

    events = await _collect_events()

    assert events[0]["type"] == "error"
    assert events[0]["requires_model"] is False
    assert "401" in str(events[0]["error"])


@pytest.mark.asyncio
async def test_quick_verify_auth_failure_is_not_misreported_as_model_problem(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm_quick_verify,
        "discover_models",
        AsyncMock(return_value=["gpt-auth"]),
    )

    class FakeClient:
        async def stream_complete(self, *_args, **_kwargs):
            if False:
                yield LLMStreamChunk()
            raise LLMError("接口返回 401: invalid api key", status_code=401)

    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: FakeClient(),
    )
    events = await _collect_events()

    error = events[-1]
    assert error["type"] == "error"
    assert error["requires_model"] is False
    assert "401" in str(error["error"])


@pytest.mark.asyncio
async def test_quick_verify_auto_model_not_found_requests_manual_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm_quick_verify,
        "discover_models",
        AsyncMock(return_value=["stale-auto-model"]),
    )

    class FakeClient:
        async def stream_complete(self, *_args, **_kwargs):
            if False:
                yield LLMStreamChunk()
            raise LLMError("model not found", status_code=404)

    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: FakeClient(),
    )
    events = await _collect_events()

    assert events[-1]["type"] == "error"
    assert events[-1]["requires_model"] is True


@pytest.mark.asyncio
async def test_quick_verify_endpoint_404_does_not_request_manual_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm_quick_verify,
        "discover_models",
        AsyncMock(return_value=["gpt-endpoint-test"]),
    )

    class FakeClient:
        async def stream_complete(self, *_args, **_kwargs):
            if False:
                yield LLMStreamChunk()
            raise LLMError(
                "Responses streaming 接口返回 404: route not found",
                status_code=404,
            )

    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: FakeClient(),
    )
    events = await _collect_events()

    assert events[-1]["type"] == "error"
    assert events[-1]["requires_model"] is False


@pytest.mark.asyncio
async def test_quick_verify_falls_back_when_streaming_is_unsupported(
    monkeypatch,
) -> None:
    class FakeClient:
        async def stream_complete(self, *_args, **_kwargs):
            if False:
                yield LLMStreamChunk()
            raise NotImplementedError("stream unsupported")

        async def complete(self, *_args, **_kwargs):
            return LLMResult(
                text="完整回复",
                model="legacy-real",
                input_tokens=5,
                output_tokens=2,
            )

    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: FakeClient(),
    )
    events = await _collect_events(model="legacy-model")

    done = events[-1]
    assert done["type"] == "done"
    assert done["response"] == "完整回复"
    assert done["streaming"] is False
    assert done["stream_fallback"] is True


@pytest.mark.asyncio
async def test_quick_verify_does_not_repeat_completed_json_stream_fallback(
    monkeypatch,
) -> None:
    """同一流式请求已返回完整 JSON 时，不得再调用 complete 造成重复计费。"""

    class FakeClient:
        complete_calls = 0

        async def stream_complete(self, *_args, **_kwargs):
            yield LLMStreamChunk(
                delta="同一次请求的完整回复",
                model="json-real",
                stream_fallback=True,
            )
            yield LLMStreamChunk(
                model="json-real",
                input_tokens=5,
                output_tokens=2,
                done=True,
                stream_fallback=True,
            )

        async def complete(self, *_args, **_kwargs):
            self.complete_calls += 1
            raise AssertionError("普通 JSON 已在同一请求中消费，不应再次请求上游")

    client = FakeClient()
    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: client,
    )

    events = await _collect_events(model="json-model")

    delta = next(event for event in events if event["type"] == "delta")
    assert delta["stream_fallback"] is True
    done = events[-1]
    assert done["type"] == "done"
    assert done["response"] == "同一次请求的完整回复"
    assert done["streaming"] is False
    assert done["stream_fallback"] is True
    assert client.complete_calls == 0


@pytest.mark.asyncio
async def test_quick_verify_rejects_stream_without_done(monkeypatch) -> None:
    class FakeClient:
        async def stream_complete(self, *_args, **_kwargs):
            yield LLMStreamChunk(delta="partial", model="broken")

    monkeypatch.setattr(
        llm_quick_verify,
        "build_client_from_dto",
        lambda _dto, **_kwargs: FakeClient(),
    )

    events = await _collect_events(model="broken")

    assert events[-1]["type"] == "error"
    assert "最终状态" in str(events[-1]["error"])


def test_quick_verify_request_strips_values_and_rejects_blank_required_fields() -> None:
    payload = QuickVerifyProviderRequest(
        base_url="  https://api.example.test/v1  ",
        api_key="  sk-test  ",
        model="  gpt-5  ",
        reasoning_effort="max",
    )
    assert payload.base_url == "https://api.example.test/v1"
    assert payload.api_key == "sk-test"
    assert payload.model == "gpt-5"
    assert payload.reasoning_effort == "max"
    assert payload.system_prompt == QUICK_VERIFY_SYSTEM_PROMPT
    assert payload.message == QUICK_VERIFY_MESSAGE
    assert payload.max_tokens == QUICK_VERIFY_MAX_TOKENS

    with pytest.raises(ValueError):
        QuickVerifyProviderRequest(base_url="   ")


def test_quick_verify_model_discovery_drops_ids_that_cannot_be_imported() -> None:
    model_ids = llm_quick_verify._model_ids(
        {"data": [{"id": "gpt-ok"}, {"id": "x" * 129}]}
    )
    assert model_ids == ["gpt-ok"]


@pytest.mark.asyncio
async def test_quick_verify_discovery_uses_protocol_headers_and_ranks_chat_models(
    monkeypatch,
) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "data": [
                    {"id": "text-embedding-3-large"},
                    {"id": "utility-model"},
                    {"id": "claude-sonnet"},
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        llm_quick_verify.httpx,
        "AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    models = await llm_quick_verify.discover_models(
        base_url="https://api.anthropic.test/v1",
        api_key="anthropic-secret",
        api_format="anthropic_messages",
        timeout_seconds=30,
    )

    assert captured["url"] == "https://api.anthropic.test/v1/models"
    assert captured["headers"]["x-api-key"] == "anthropic-secret"
    assert "Authorization" not in captured["headers"]
    assert models == ["claude-sonnet", "utility-model"]


@pytest.mark.asyncio
async def test_fetch_models_preview_direct_disables_env_proxy_and_bounds_results(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "data": [
                    *({"id": f"model-{index}"} for index in range(205)),
                    {"id": "model-0"},
                    {"id": "x" * 129},
                ]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, headers):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(commands_api, "_require_ai_enabled", AsyncMock(return_value=None))
    monkeypatch.setattr(commands_api, "_resolve_proxy_url", AsyncMock(return_value=None))
    monkeypatch.setattr(commands_api.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(commands_api.audit, "write", AsyncMock(return_value=None))
    response = await commands_api.fetch_models_preview(
        payload=FetchModelsPreviewRequest(
            provider="openai",
            api_format="responses",
            base_url="https://api.example.test/v1",
            api_key="sk-preview",
        ),
        db=AsyncMock(),
        user=AsyncMock(id=1),
    )

    assert captured["trust_env"] is False
    assert "proxy" not in captured
    assert response.fetched == 200
    assert len(response.ids) == 200
    assert response.ids[0] == "model-0"
    assert response.ids[-1] == "model-199"


def test_quick_verify_rejects_credentials_embedded_in_base_url() -> None:
    with pytest.raises(ValueError, match="不能包含用户名或密码"):
        llm_quick_verify.normalize_quick_verify_base_url(
            "https://admin:secret@api.example.test/v1"
        )


@pytest.mark.asyncio
async def test_quick_verify_route_returns_ndjson_without_audit(monkeypatch) -> None:
    monkeypatch.setattr(commands_api, "_require_ai_enabled", AsyncMock(return_value=None))
    resolve_proxy = AsyncMock(return_value="socks5://proxy.example:1080")
    monkeypatch.setattr(commands_api, "_resolve_proxy_url", resolve_proxy)
    audit_write = AsyncMock(side_effect=AssertionError("临时凭据验证不应写审计"))
    monkeypatch.setattr(commands_api.audit, "write", audit_write)
    captured: dict[str, object] = {}

    async def fake_events(**kwargs):
        captured.update(kwargs)
        yield {
            "type": "done",
            "ok": True,
            "models": [],
            "api_format": "responses",
        }

    monkeypatch.setattr(llm_quick_verify, "quick_verify_events", fake_events)
    response = await commands_api.stream_quick_verify_provider(
        payload=QuickVerifyProviderRequest(
            base_url="https://api.example.test/v1",
            api_format="responses",
            protocol_profile="claude_code_proxy",
            proxy_id=7,
        ),
        db=AsyncMock(),
        _user=AsyncMock(),
    )
    body = ""
    async for chunk in response.body_iterator:
        body += chunk.decode() if isinstance(chunk, bytes) else chunk

    assert json.loads(body)["type"] == "done"
    assert response.headers["cache-control"] == "no-store"
    resolve_proxy.assert_awaited_once()
    assert captured["protocol_profile"] == "claude_code_proxy"
    assert captured["proxy_url"] == "socks5://proxy.example:1080"
    audit_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_verify_route_rejects_long_key_without_echo(monkeypatch) -> None:
    monkeypatch.setattr(commands_api, "_require_ai_enabled", AsyncMock(return_value=None))
    secret = "k" * 513
    with pytest.raises(HTTPException) as raised:
        await commands_api.stream_quick_verify_provider(
            payload=QuickVerifyProviderRequest(
                base_url="https://api.example.test/v1",
                api_key=secret,
            ),
            db=AsyncMock(),
            _user=AsyncMock(),
        )
    assert raised.value.status_code == 422
    assert secret not in str(raised.value.detail)
