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
        self.last_stream_kwargs: dict[str, Any] = {}
        self.config = {
            "enabled": False,
            "provider_id": None,
            "model": None,
            "fallback_provider_ids": [],
            "require_tool_approval": False,
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

    async def reconcile_stale_messages(self, _db, _session_id):
        return 0

    async def get_message(self, _db, message_id, *, session_id):
        return next(
            (
                message
                for message in self.messages.get(session_id, [])
                if message.id == message_id
            ),
            None,
        )

    async def stream_message(self, _db, **kwargs):
        self.last_stream_kwargs = dict(kwargs)
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


class _FakeRunManager:
    def __init__(self) -> None:
        self.last_start_kwargs: dict[str, Any] = {}
        self.broken_stream = False

    async def start_run(self, **kwargs):
        self.last_start_kwargs = dict(kwargs)
        return SimpleNamespace(
            id="durable-r1",
            session_id=kwargs["session_id"],
            web_user_id=kwargs["web_user_id"],
            user_message_id=kwargs.get("retry_message_id") or 101,
            client_request_id=kwargs["client_request_id"],
            kind="retry" if kwargs.get("retry_message_id") else "message",
            status="running",
            last_seq=0,
            cancel_requested=False,
            error_code=None,
            error_message=None,
            started_at=None,
            finished_at=None,
            created_at=None,
            updated_at=None,
        )

    async def stream_events(self, run_id, *, after_seq=0):
        if self.broken_stream:
            raise RuntimeError("database failed with sk-sensitive-secret-value")
        yield {"type": "run_started", "run_id": run_id, "session_id": "s1", "seq": after_seq + 1}
        yield {"type": "assistant_message", "content": "ok", "run_id": run_id, "session_id": "s1", "seq": after_seq + 2}
        yield {"type": "done", "ok": True, "run_id": run_id, "session_id": "s1", "seq": after_seq + 3}

    async def list_runs(self, **kwargs):
        self.last_list_kwargs = dict(kwargs)
        return [
            await self.start_run(
                session_id="s1",
                web_user_id=kwargs["web_user_id"],
                client_request_id="request-listed-1",
            )
        ]


@pytest.fixture
def fake_svc(monkeypatch):
    svc = _FakeSvc()
    monkeypatch.setattr(api, "get_system_agent_service", lambda: svc)
    return svc


@pytest.fixture
def fake_run_manager(monkeypatch):
    manager = _FakeRunManager()
    monkeypatch.setattr(api, "get_system_agent_run_manager", lambda: manager)
    return manager


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
async def test_stream_message_ndjson(fake_svc, fake_run_manager) -> None:
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
async def test_start_durable_run_returns_run_and_message_identity(
    fake_svc,
    fake_run_manager,
) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=7)
    await api.create_session(api.SystemAgentSessionCreate(), db, user)

    out = await api.start_system_agent_run(
        "s1",
        api.SystemAgentRunCreate(
            content="交互里有哪些规则？",
            client_request_id="request-api-1",
        ),
        db,
        user,
    )

    assert out.id == "durable-r1"
    assert out.run_id == "durable-r1"
    assert out.user_message_id == 101
    assert fake_run_manager.last_start_kwargs["text"] == "交互里有哪些规则？"


@pytest.mark.asyncio
async def test_list_durable_runs_is_scoped_to_current_user(fake_run_manager) -> None:
    user = SimpleNamespace(id=7)

    rows = await api.list_system_agent_runs(
        user,
        status="failed",
        since=None,
        until=None,
        limit=25,
    )

    assert [row.run_id for row in rows] == ["durable-r1"]
    assert fake_run_manager.last_list_kwargs == {
        "web_user_id": 7,
        "status": "failed",
        "since": None,
        "until": None,
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_stream_exception_does_not_expose_raw_error(fake_svc, fake_run_manager) -> None:
    fake_run_manager.broken_stream = True
    db = AsyncMock()
    user = SimpleNamespace(id=7)
    await api.create_session(api.SystemAgentSessionCreate(), db, user)

    response = await api.stream_message(
        "s1",
        api.SystemAgentMessageCreate(content="你好"),
        db,
        user,
    )
    chunks = [
        chunk if isinstance(chunk, str) else chunk.decode()
        async for chunk in response.body_iterator
    ]
    body = "".join(chunks)

    assert "sk-sensitive-secret-value" not in body
    assert "RUN_STREAM_FAILED" in body
    assert "RuntimeError" in body


@pytest.mark.asyncio
async def test_retry_failed_message_stream(fake_svc, fake_run_manager) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=7)
    await api.create_session(api.SystemAgentSessionCreate(), db, user)
    fake_svc.messages["s1"] = [
        SimpleNamespace(id=9, session_id="s1", role="user", run_status="failed")
    ]

    response = await api.retry_message(
        "s1",
        9,
        api.SystemAgentMessageRetry(
            fallback_provider_id=12,
            approved_tools=["scheduler.list"],
        ),
        db,
        user,
    )
    chunks = [
        chunk if isinstance(chunk, str) else chunk.decode()
        async for chunk in response.body_iterator
    ]
    events = [json.loads(line) for line in "".join(chunks).splitlines() if line]

    assert events[-1]["type"] == "done"
    assert events[-1]["ok"] is True
    assert fake_run_manager.last_start_kwargs["fallback_provider_id"] == 12
    assert fake_run_manager.last_start_kwargs["approved_tools"] == ["scheduler.list"]


@pytest.mark.asyncio
async def test_retry_rejects_non_failed_message(fake_svc) -> None:
    db = AsyncMock()
    user = SimpleNamespace(id=7)
    await api.create_session(api.SystemAgentSessionCreate(), db, user)
    fake_svc.messages["s1"] = [
        SimpleNamespace(id=10, session_id="s1", role="user", run_status="succeeded")
    ]

    with pytest.raises(HTTPException) as exc_info:
        await api.retry_message(
            "s1",
            10,
            api.SystemAgentMessageRetry(),
            db,
            user,
        )

    assert exc_info.value.status_code == 409


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
