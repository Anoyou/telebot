"""Codex TUI / Desktop 身份档案兼容测试。"""

import pytest

from app.db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_RESPONSES,
    normalize_client_identity_profile,
)
from app.services.llm_identity import (
    CLIENT_IDENTITY_CLAUDE_CODE,
    CLIENT_IDENTITY_CODEX_DESKTOP,
    CLIENT_IDENTITY_CODEX_TUI,
    request_configuration_profiles,
    resolve_identity,
    selectable_identities,
)


def test_only_tui_and_desktop_codex_profiles_are_exposed() -> None:
    selectable = {item["profile"] for item in selectable_identities()}
    configured = {item["profile"] for item in request_configuration_profiles()}
    assert {"codex_tui", "codex_desktop"} <= selectable & configured
    assert "codex_cli" not in selectable | configured
    assert "codex_exec" not in selectable | configured
    assert "claude_desktop" not in selectable | configured


def test_codex_desktop_remains_distinct() -> None:
    assert normalize_client_identity_profile("codex_desktop") == CLIENT_IDENTITY_CODEX_DESKTOP
    identity = resolve_identity("codex_desktop", LLM_API_FORMAT_RESPONSES)
    assert identity.profile == CLIENT_IDENTITY_CODEX_DESKTOP
    assert identity.headers()["originator"] == "Codex Desktop"


@pytest.mark.parametrize("legacy", ("codex_cli", "codex_exec"))
def test_legacy_codex_values_map_to_tui(legacy: str) -> None:
    assert normalize_client_identity_profile(legacy) == CLIENT_IDENTITY_CODEX_TUI


def test_codex_tui_request_profile_uses_hyphenated_runtime_headers() -> None:
    profile = next(item for item in request_configuration_profiles() if item["profile"] == "codex_tui")
    names = {item["name"] for item in profile["headers"]}
    assert {"session-id", "thread-id", "x-client-request-id"} <= names
    assert "session_id" not in names
    assert "conversation_id" not in names


def test_codex_desktop_request_profile_uses_underscored_session_header() -> None:
    profile = next(
        item for item in request_configuration_profiles() if item["profile"] == "codex_desktop"
    )
    names = {item["name"] for item in profile["headers"]}
    assert {"session_id", "x-client-request-id"} <= names
    assert "session-id" not in names
    assert "thread-id" not in names


@pytest.mark.parametrize(
    ("profile_name", "expected_names"),
    [
        (
            "openai_sdk",
            {
                "User-Agent",
                "X-Stainless-Lang",
                "X-Stainless-Package-Version",
                "X-Stainless-OS",
                "X-Stainless-Arch",
                "X-Stainless-Runtime",
                "X-Stainless-Runtime-Version",
                "X-Stainless-Async",
                "Authorization",
                "Accept",
                "Content-Type",
                "Host",
                "Content-Length",
            },
        ),
        (
            "codex_tui",
            {
                "User-Agent",
                "originator",
                "session-id",
                "thread-id",
                "x-client-request-id",
                "Authorization",
                "Accept",
                "Content-Type",
                "Host",
                "Content-Length",
                "x-codex-beta-features",
                "x-codex-window-id",
                "x-codex-turn-metadata",
            },
        ),
        (
            "codex_desktop",
            {
                "User-Agent",
                "originator",
                "session_id",
                "x-client-request-id",
                "Authorization",
                "Accept",
                "Content-Type",
                "Host",
                "Content-Length",
                "x-codex-beta-features",
                "x-codex-window-id",
                "x-codex-turn-metadata",
            },
        ),
        (
            "claude_code",
            {
                "User-Agent",
                "x-app",
                "X-Stainless-Arch",
                "X-Stainless-Lang",
                "X-Stainless-Package-Version",
                "X-Stainless-Retry-Count",
                "X-Stainless-Runtime",
                "X-Stainless-Runtime-Version",
                "X-Stainless-OS",
                "X-Claude-Code-Session-Id",
                "x-api-key",
                "Accept",
                "Content-Type",
                "anthropic-version",
                "anthropic-beta",
                "Host",
                "Content-Length",
            },
        ),
        (
            "grok_cli",
            {
                "User-Agent",
                "x-grok-client-version",
                "x-grok-client-identifier",
                "x-grok-conv-id",
                "x-grok-session-id",
                "x-grok-req-id",
                "x-grok-agent-id",
                "x-grok-turn-idx",
                "x-grok-model-override",
                "Authorization",
                "Accept",
                "Content-Type",
                "Host",
                "Content-Length",
                "x-xai-token-auth",
                "x-authenticateresponse",
            },
        ),
    ],
)
def test_request_profiles_expose_complete_observed_header_inventory(
    profile_name: str,
    expected_names: set[str],
) -> None:
    profile = next(
        item for item in request_configuration_profiles() if item["profile"] == profile_name
    )
    assert {item["name"] for item in profile["headers"]} == expected_names
    assert all(item["management"] for item in profile["headers"])


def test_claude_desktop_history_value_maps_to_claude_code_cli() -> None:
    assert normalize_client_identity_profile("claude_desktop") == CLIENT_IDENTITY_CLAUDE_CODE
    identity = resolve_identity("claude_desktop", LLM_API_FORMAT_ANTHROPIC_MESSAGES)
    assert identity.profile == CLIENT_IDENTITY_CLAUDE_CODE
    assert identity.headers()["x-app"] == "cli"
