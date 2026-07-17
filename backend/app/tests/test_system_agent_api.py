"""System Agent HTTP API 层：会话所有权与 NDJSON 错误路径。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import system_agent as api


class _FakeSvc:
    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}
        self.messages: dict[str, list[Any]] = {}
        self.config = {
            "enabled": False,
            "provider_id": None,
            "model": None,
            "max_steps": 8,
            "max_tool_calls": 24,
            "session_token_limit": 16384,
        }

    async def get_config(self, _db):
        return dict(self.config)

    async def update_config(self, _db, patch):
        self.config.update({k: v for k, v in patch.items() if v is not None or k in patch})
        return dict(self.config)

    async def get_capabilities(self, _db, *, channel, role):
        return {
            "enabled": self.config["enabled"],
            "provider_id": self.config["provider_id"],
            "model": self.config["model"],
            "ai_enabled": True,
            "timezone": "UTC",
            "tools": [{"name": "system.get_context", "available": True, "read_only": True}],
            "stage": 1,
            "write_tools_available": False,
        }

    async def create_session(self, _db, **kwargs):
        session = SimpleNamespace(
            id="s1",
            web_user_id=kwargs.get("web_user_id"),
            bot_tg_user_id=None,
            account_id=kwargs.get("account_id"),
            channel=kwargs["channel"],
            title=kwargs.get("title"),
            status="active",
            created_at=None,
            updated_at=None,
        )
        self.sessions[session.id] = session
        return session

    async def list_sessions(self, _db, **kwargs):
        uid = kwargs.get("web_user_id")
        return [s for s in self.sessions.values() if s.web_user_id == uid]

    async def get_session(self, _db, session_id, **kwargs):
        s = self.sessions.get(session_id)
        if s is None:
            return None
        if kwargs.get("web_user_id") is not None and s.web_user_id != kwargs["web_user_id"]:
            return None
        return s

    async def update_session(self, _db, session, **kwargs):
        for k, v in kwargs.items():
            if v is not ... and hasattr(session, k):
                setattr(session, k, v)
        return session

    async def delete_session(self, _db, session):
        self.sessions.pop(session.id, None)

    async def delete_all_sessions(self, _db, *, web_user_id):
        ids = [sid for sid, s in self.sessions.items() if s.web_user_id == web_user_id]
        for sid in ids:
            self.sessions.pop(sid, None)
        return len(ids)

    async def list_messages(self, _db, session_id, **kwargs):
        return self.messages.get(session_id, [])

    async def stream_message(self, _db, **kwargs):
        yield {
            "type": "run_started",
            "run_id": "r1",
            "session_id": kwargs["session"].id,
            "seq": 1,
        }
        yield {
            "type": "assistant_message",
            "content": "ok",
            "run_id": "r1",
            "session_id": kwargs["session"].id,
            "seq": 2,
        }
        yield {"type": "done", "ok": True, "run_id": "r1", "session_id": kwargs["session"].id, "seq": 3}


@pytest.fixture
def fake_svc(monkeypatch):
    svc = _FakeSvc()
    monkeypatch.setattr(api, "get_system_agent_service", lambda: svc)
    return svc


@pytest.mark.asyncio
async def test_create_and_list_sessions(fake_svc) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=7)
    created = await api.create_session(
        api.SystemAgentSessionCreate(account_id=3, title="hi"),
        db,
        user,
    )
    assert created.id == "s1"
    assert created.account_id == 3
    rows = await api.list_sessions(db, user)
    assert len(rows) == 1
    assert rows[0].web_user_id == 7


@pytest.mark.asyncio
async def test_get_session_not_found(fake_svc) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as ei:
        await api.get_session("missing", db, user)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_config(fake_svc) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=1)
    out = await api.patch_config(
        api.SystemAgentConfigPatch(enabled=True, provider_id=2, model="tools-m"),
        db,
        user,
    )
    assert out.enabled is True
    assert out.provider_id == 2
    assert out.model == "tools-m"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_capabilities_stage1(fake_svc) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=1)
    caps = await api.get_capabilities(db, user)
    assert caps.stage == 1
    assert caps.write_tools_available is False
    assert caps.tools


@pytest.mark.asyncio
async def test_stream_message_ndjson(fake_svc) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=7)
    await api.create_session(api.SystemAgentSessionCreate(), db, user)
    response = await api.stream_message(
        "s1",
        api.SystemAgentMessageCreate(content="你好"),
        db,
        user,
    )
    assert response.media_type == "application/x-ndjson"
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    body = "".join(chunks)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert len(lines) >= 3
    events = [json.loads(ln) for ln in lines]
    types = [e["type"] for e in events]
    assert "run_started" in types
    assert "assistant_message" in types
    assert "done" in types
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_secret_input_locks_action_before_writing(monkeypatch) -> None:
    row = SimpleNamespace(
        id="act-secret",
        actor_user_id=7,
        tool_name="providers.save",
        status="pending",
        expires_at=None,
        secret_payload_enc=None,
        secret_fields=None,
        arguments={},
        error_code="API_KEY_REQUIRED",
        error_message="缺少 Key",
    )
    lock_mock = AsyncMock(return_value=row)
    monkeypatch.setattr(api, "lock_action", lock_mock)
    monkeypatch.setattr(
        api,
        "get_registry",
        lambda: SimpleNamespace(
            get=lambda _name: SimpleNamespace(secret_argument_names=("api_key",))
        ),
    )
    monkeypatch.setattr(api, "encrypt_secret_payload", lambda _data: "encrypted")
    db = AsyncMock()

    out = await api.secret_input_action(
        "act-secret",
        api.SystemAgentSecretInput(fields={"api_key": "sk-new-secret-value"}),
        db,
        SimpleNamespace(id=7),
    )

    lock_mock.assert_awaited_once_with(db, "act-secret")
    assert out.has_secret is True
    assert row.secret_payload_enc == "encrypted"
    assert row.error_code is None
    db.commit.assert_awaited_once()
