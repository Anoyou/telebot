from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.gateway_runtime import GatewayRuntimeManager
from app.services.llm_dto import LLMProviderDTO


def _provider(*, backend: str = "codex_gateway", api_format: str = "responses") -> LLMProviderDTO:
    return LLMProviderDTO(
        id=7,
        name="gateway",
        provider="openai",
        execution_backend=backend,
        api_format=api_format,
        base_url="https://upstream.example/v1",
        api_key_enc="encrypted",
        default_model="gpt-x",
    )


@pytest.mark.asyncio
async def test_direct_only_does_not_start_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = GatewayRuntimeManager(binary="/missing")
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_start_locked", start)
    status = await manager.reconcile([_provider(backend="direct")])
    assert status.state == "not_required"
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_binary_is_degraded_not_global_failure() -> None:
    manager = GatewayRuntimeManager(binary="/definitely/missing")
    status = await manager.reconcile([_provider()])
    assert status.state == "degraded"
    assert status.required is True
    assert "不存在" in (status.error or "")


@pytest.mark.asyncio
async def test_snapshot_sync_is_revisioned_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = GatewayRuntimeManager(binary="/fake")
    manager._process = type("Process", (), {"returncode": None})()  # type: ignore[assignment]
    monkeypatch.setattr("app.services.gateway_runtime.decrypt_str", lambda _: "plain-key")
    request = AsyncMock(side_effect=[{"revision": 1}])
    monkeypatch.setattr(manager, "_request_json", request)
    first = await manager.reconcile([_provider()])
    second = await manager.reconcile([_provider()])
    assert first.state == second.state == "ready"
    assert first.revision == second.revision == 1
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_non_responses_provider_is_rejected_during_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = GatewayRuntimeManager(binary="/fake")
    manager._process = type("Process", (), {"returncode": None})()  # type: ignore[assignment]
    status = await manager.reconcile([_provider(api_format="chat_completions")])
    assert status.state == "degraded"
    assert "仅支持 Responses" in (status.error or "")
