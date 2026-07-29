from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app import crypto
from app.services import llm_client
from app.services.llm_client import AnthropicClient, ResponsesClient
from app.services.llm_identity import resolve_identity
from app.services.llm_request_headers import (
    RequestHeaderConfigError,
    decrypt_request_headers,
    encrypt_request_headers,
    normalize_request_headers,
    plan_request_headers,
    request_header_summaries,
    request_headers_for_scope,
)


@pytest.fixture(autouse=True)
def _isolated_fernet(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", Fernet(Fernet.generate_key()))


def test_request_headers_encrypt_values_and_only_expose_summaries() -> None:
    token = encrypt_request_headers(
        [{"name": "X-Tenant-ID", "value": "tenant-secret", "scopes": ["inference", "models"]}]
    )
    assert token is not None
    assert "tenant-secret" not in token
    assert decrypt_request_headers(token)[0]["value"] == "tenant-secret"
    assert request_header_summaries(token) == [
        {"name": "X-Tenant-ID", "scopes": ["inference", "models"], "has_value": True}
    ]


def test_compatibility_header_value_is_redacted_from_upstream_error() -> None:
    secret = "opaque-header-secret"
    llm_client._llm_headers(compatibility_headers={"X-Tenant-ID": secret})

    safe = llm_client._safe_error_message(
        f"OpenAI 接口返回 401: received X-Tenant-ID: {secret}",
        "different-api-key",
    )

    assert secret not in safe
    assert "<redacted>" in safe


def test_request_headers_preserve_existing_value_by_case_insensitive_name() -> None:
    token = encrypt_request_headers([{"name": "X-Tenant-ID", "value": "kept", "scopes": ["inference"]}])
    updated = encrypt_request_headers(
        [{"name": "x-tenant-id", "value": None, "scopes": ["models"]}],
        existing_token=token,
    )
    assert decrypt_request_headers(updated) == [
        {"name": "x-tenant-id", "value": "kept", "scopes": ["models"]}
    ]


@pytest.mark.parametrize(
    "item",
    [
        {"name": "Authorization", "value": "secret", "scopes": ["inference"]},
        {"name": "X-Forwarded-For", "value": "127.0.0.1", "scopes": ["inference"]},
        {"name": "X-Stainless-Lang", "value": "js", "scopes": ["inference"]},
        {"name": "X-Grok-Client-Version", "value": "0.1.0", "scopes": ["inference"]},
        {"name": "X-XAI-Token-Auth", "value": "fake", "scopes": ["inference"]},
        {"name": "Bad Header", "value": "value", "scopes": ["inference"]},
        {"name": "X-Test", "value": "line\nbreak", "scopes": ["inference"]},
        {"name": "X-Test", "value": "value", "scopes": []},
    ],
)
def test_request_headers_reject_unsafe_inputs_without_echoing_values(item) -> None:
    with pytest.raises(RequestHeaderConfigError) as caught:
        normalize_request_headers([item])
    assert str(item["value"]) not in str(caught.value)


def test_scope_filter_and_planner_keep_system_headers_authoritative() -> None:
    items = [
        {"name": "X-Tenant-ID", "value": "a", "scopes": ["inference"]},
        {"name": "HTTP-Referer", "value": "https://example.com", "scopes": ["models"]},
    ]
    assert request_headers_for_scope(items, "inference") == {"X-Tenant-ID": "a"}
    assert plan_request_headers(
        system_headers={"Content-Type": "application/json"},
        compatibility_headers={"X-Tenant-ID": "a"},
    ) == {"Content-Type": "application/json", "X-Tenant-ID": "a"}


def test_duplicate_names_are_case_insensitive() -> None:
    with pytest.raises(RequestHeaderConfigError, match="重复"):
        normalize_request_headers(
            [
                {"name": "X-Test", "value": "a", "scopes": ["inference"]},
                {"name": "x-test", "value": "b", "scopes": ["models"]},
            ]
        )


def test_encrypted_payload_is_valid_json_after_decryption() -> None:
    token = encrypt_request_headers([{"name": "X-Test", "value": "value", "scopes": ["liveness"]}])
    assert token is not None
    assert isinstance(json.loads(crypto.decrypt_str(token)), list)


def test_codex_responses_uses_random_non_user_runtime_ids() -> None:
    client = ResponsesClient(
        "secret",
        "https://api.example/v1",
        "model",
        identity=resolve_identity("codex_cli", "responses"),
    )
    session_id = client._runtime_headers["session-id"]
    assert uuid.UUID(session_id)
    assert client._runtime_headers["thread-id"] == session_id
    assert client._runtime_headers["x-client-request-id"] == session_id

    minimal = ResponsesClient(
        "secret",
        "https://api.example/v1",
        "model",
        identity=resolve_identity("minimal", "responses"),
    )
    assert minimal._runtime_headers == {}


def test_claude_code_uses_random_non_user_session_id() -> None:
    client = AnthropicClient(
        "secret",
        "https://api.example/v1",
        "model",
        identity=resolve_identity("claude_code", "anthropic_messages"),
    )
    assert uuid.UUID(client._runtime_headers["X-Claude-Code-Session-Id"])


def test_grok_cli_uses_random_non_user_runtime_ids() -> None:
    client = ResponsesClient(
        "secret",
        "https://api.example/v1",
        "grok-4.5",
        identity=resolve_identity("grok_cli", "responses"),
    )
    session_id = client._runtime_headers["x-grok-session-id"]
    assert uuid.UUID(session_id)
    assert client._runtime_headers["x-grok-conv-id"] == session_id
    assert uuid.UUID(client._runtime_headers["x-grok-req-id"])
    assert uuid.UUID(client._runtime_headers["x-grok-agent-id"])
    assert client._runtime_headers["x-grok-turn-idx"] == "1"
    assert client._runtime_headers["x-grok-model-override"] == "grok-4.5"
