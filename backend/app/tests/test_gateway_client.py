from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.command import LLMProviderCreate
from app.services.llm_client import GatewayResponsesClient, ResponsesClient, build_client_from_dto
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
