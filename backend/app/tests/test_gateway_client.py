from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.command import LLMProviderCreate
from app.services.llm_client import GatewayResponsesClient, LLMError, ResponsesClient, build_client_from_dto
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import MessageRole, ModelMessage, ModelRequest


def _dto(*, backend: str = "direct", api_format: str = "responses") -> LLMProviderDTO:
    return LLMProviderDTO(
        id=12,
        name="provider",
        provider="openai",
        execution_backend=backend,
        api_format=api_format,
        default_model="gpt-x",
        base_url="https://upstream.example/v1",
    )


def test_builder_preserves_direct_default() -> None:
    client = build_client_from_dto(_dto())
    assert type(client) is ResponsesClient


def test_builder_selects_gateway_without_decrypting_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEPILOT_GATEWAY_SOCKET", "/tmp/test-gateway.sock")
    client = build_client_from_dto(_dto(backend="codex_gateway"), request_scope="liveness")
    assert isinstance(client, GatewayResponsesClient)
    headers = client._runtime_headers(
        ModelRequest(model="gpt-x", messages=(ModelMessage.text(MessageRole.USER, "hi"),))
    )
    assert headers["X-TelePilot-Provider-ID"] == "12"
    assert headers["X-TelePilot-Request-Scope"] == "liveness"
    assert headers["X-TelePilot-Session-ID"]
    assert "Authorization" not in headers


def test_gateway_rejects_non_responses_builder() -> None:
    with pytest.raises(ValueError, match="仅支持 Responses"):
        build_client_from_dto(_dto(backend="codex_gateway", api_format="chat_completions"))


def test_create_schema_rejects_gateway_non_responses() -> None:
    with pytest.raises(ValidationError, match="仅支持 Responses"):
        LLMProviderCreate(
            name="bad",
            provider="openai",
            api_key="key",
            default_model="gpt-x",
            api_format="chat_completions",
            execution_backend="codex_gateway",
        )


def _gateway_client_with_transport(handler) -> GatewayResponsesClient:
    client = GatewayResponsesClient(
        provider_id=12,
        model="gpt-x",
        socket_path="/tmp/test-gateway.sock",
    )
    transport = httpx.MockTransport(handler)
    client._client_kwargs = lambda _timeout: {  # type: ignore[method-assign]
        "transport": transport,
        "base_url": "http://gateway",
    }
    return client


@pytest.mark.asyncio
async def test_gateway_structured_success_carries_transport_facts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-TelePilot-Provider-ID"] == "12"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-TelePilot-Gateway-Version": "0.1.0-beta.1",
                "X-TelePilot-Gateway-Request-ID": "gw-req-1",
                "X-TelePilot-Gateway-Stage": "upstream",
            },
            json={
                "model": "gpt-x",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    response = await _gateway_client_with_transport(handler).invoke(
        ModelRequest(model="gpt-x", messages=(ModelMessage.text(MessageRole.USER, "hi"),))
    )

    assert response.text == "ok"
    assert response.execution_backend == "codex_gateway"
    assert response.gateway_version == "0.1.0-beta.1"
    assert response.gateway_request_id == "gw-req-1"
    assert response.gateway_stage is None


@pytest.mark.asyncio
async def test_gateway_structured_error_preserves_gateway_fact() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={
                "Content-Type": "application/json",
                "X-TelePilot-Gateway-Version": "0.1.0-beta.1",
                "X-TelePilot-Gateway-Request-ID": "gw-req-2",
                "X-TelePilot-Gateway-Stage": "admission",
            },
            content=json.dumps(
                {
                    "error": {
                        "code": "gateway_overloaded",
                        "message": "busy",
                        "request_id": "gw-req-2",
                        "gateway_stage": "admission",
                    }
                }
            ).encode(),
        )

    with pytest.raises(LLMError) as raised:
        await _gateway_client_with_transport(handler).invoke(
            ModelRequest(model="gpt-x", messages=(ModelMessage.text(MessageRole.USER, "hi"),))
        )

    error = raised.value
    assert error.category == "gateway_overloaded"
    assert error.request_id == "gw-req-2"
    assert error.gateway_stage == "admission"
    assert error.gateway_version == "0.1.0-beta.1"
    assert error.execution_backend == "codex_gateway"


@pytest.mark.asyncio
async def test_gateway_sse_error_preserves_structured_diagnostic() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-TelePilot-Gateway-Version": "0.1.0-beta.1",
                "X-TelePilot-Gateway-Request-ID": "gw-sse-1",
                "X-TelePilot-Gateway-Stage": "upstream",
            },
            text=(
                'event: response.failed\n'
                'data: {"type":"response.failed","response":{"status":"failed",'
                '"error":{"type":"upstream_error","message":"Upstream request failed"},'
                '"upstream_status_code":400,'
                '"upstream_error_message":"Unsupported parameter: max_output_tokens",'
                '"upstream_error_detail":{"detail":"Unsupported parameter: max_output_tokens"},'
                '"upstream_request_id":"sub2api-request",'
                '"client_request_id":"sub2api-client-request"}}\n\n'
            ),
        )

    with pytest.raises(LLMError) as raised:
        async for _event in _gateway_client_with_transport(handler).stream_invoke(
            ModelRequest(model="gpt-x", messages=(ModelMessage.text(MessageRole.USER, "hi"),))
        ):
            pass

    error = raised.value
    assert error.category == "request_invalid"
    assert error.retryable is False
    assert error.upstream_status_code == 400
    assert error.upstream_error_message == "Unsupported parameter: max_output_tokens"
    assert "max_output_tokens" in (error.upstream_error_detail or "")
    assert error.upstream_request_id == "sub2api-request"
    assert error.client_request_id == "sub2api-client-request"
    assert error.request_id == "gw-sse-1"
    assert error.execution_backend == "codex_gateway"


@pytest.mark.asyncio
async def test_gateway_rejects_stt_and_image_generation() -> None:
    client = GatewayResponsesClient(
        provider_id=12,
        model="gpt-x",
        socket_path="/tmp/test-gateway.sock",
    )

    with pytest.raises(NotImplementedError, match="不支持语音转写"):
        await client.transcribe(b"audio", "whisper-1")
    with pytest.raises(NotImplementedError, match="不支持图片生成"):
        await client.generate_image("system", "draw")


@pytest.mark.asyncio
async def test_gateway_model_list_uses_bound_provider_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["X-TelePilot-Request-Scope"] == "models"
        return httpx.Response(
            200,
            headers={
                "X-TelePilot-Gateway-Version": "0.1.0-beta.1",
                "X-TelePilot-Gateway-Request-ID": "gw-models-1",
                "X-TelePilot-Gateway-Stage": "upstream",
            },
            json={"data": [{"id": "gpt-x"}, {"id": "gpt-y"}]},
        )

    client = GatewayResponsesClient(
        provider_id=12,
        model="gpt-x",
        socket_path="/tmp/test-gateway.sock",
        request_scope="models",
    )
    transport = httpx.MockTransport(handler)
    client._client_kwargs = lambda _timeout: {"transport": transport}  # type: ignore[method-assign]

    assert await client.list_models() == ["gpt-x", "gpt-y"]
