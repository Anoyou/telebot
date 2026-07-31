from __future__ import annotations

from app.services.llm_call_context import ClientRuntimeContext, runtime_metadata


def test_runtime_context_is_stable_per_provider_but_request_id_is_unique() -> None:
    metadata = runtime_metadata(session_id=42, run_id="run-1", turn_id="turn-1")

    first = ClientRuntimeContext.from_metadata(metadata, provider_scope="provider-a|standard")
    second = ClientRuntimeContext.from_metadata(metadata, provider_scope="provider-a|standard")
    fallback = ClientRuntimeContext.from_metadata(metadata, provider_scope="provider-b|standard")

    assert first.session_id == second.session_id
    assert first.run_id == second.run_id
    assert first.request_id != second.request_id
    assert first.session_id != fallback.session_id
    assert first.session_id != "42"
    assert first.run_id != "run-1"
    assert len(first.session_id) == 32


def test_runtime_headers_omit_raw_and_agent_identity_fields() -> None:
    metadata = runtime_metadata(session_id="private-session", run_id="private-run")
    context = ClientRuntimeContext.from_metadata(
        metadata,
        provider_scope="provider|codex_responses",
    )

    codex = context.headers_for_identity("codex_tui", model="gpt")
    grok = context.headers_for_identity("grok_cli", model="grok")

    assert codex["session-id"] == codex["thread-id"]
    assert codex["x-client-request-id"] == context.request_id
    assert "private-session" not in str(codex)
    assert "x-grok-agent-id" not in grok
    assert "x-grok-model-override" not in context.headers_for_identity(
        "grok_cli",
        model="model\r\nX-Injected: yes",
    )
