from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from app import crypto
from app.api import commands as commands_api


@pytest.mark.asyncio
async def test_reveal_provider_api_key_is_no_store_and_audited(monkeypatch) -> None:
    row = SimpleNamespace(
        id=7,
        name="self-hosted",
        api_key_enc="encrypted-value",
    )
    monkeypatch.setattr(commands_api, "_require_ai_enabled", AsyncMock(return_value=None))
    monkeypatch.setattr(
        commands_api.command_service,
        "get_provider_row",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(crypto, "decrypt_str", lambda value: "sk-saved-secret" if value else "")
    audit_write = AsyncMock(return_value=None)
    monkeypatch.setattr(commands_api.audit, "write", audit_write)
    db = AsyncMock()
    response = Response()

    result = await commands_api.reveal_provider_api_key(
        pid=7,
        response=response,
        db=db,
        user=SimpleNamespace(id=3),
    )

    assert result.api_key == "sk-saved-secret"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    audit_write.assert_awaited_once_with(
        db,
        3,
        "llm_provider.api_key_reveal",
        target="llm_provider:7",
        detail={"name": "self-hosted"},
    )
    assert "sk-saved-secret" not in str(audit_write.await_args)
    db.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reveal_provider_api_key_rejects_unconfigured_provider(monkeypatch) -> None:
    monkeypatch.setattr(commands_api, "_require_ai_enabled", AsyncMock(return_value=None))
    monkeypatch.setattr(
        commands_api.command_service,
        "get_provider_row",
        AsyncMock(return_value=SimpleNamespace(id=8, name="ollama", api_key_enc=None)),
    )
    audit_write = AsyncMock(return_value=None)
    monkeypatch.setattr(commands_api.audit, "write", audit_write)

    with pytest.raises(HTTPException) as raised:
        await commands_api.reveal_provider_api_key(
            pid=8,
            response=Response(),
            db=AsyncMock(),
            user=SimpleNamespace(id=3),
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "LLM_PROVIDER_API_KEY_NOT_CONFIGURED"
    audit_write.assert_not_awaited()
