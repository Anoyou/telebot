"""阶段 3：Provider 预检失败保持 pending、密钥清除。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.system_agent import (
    ACTION_STATUS_PENDING,
    CHANNEL_WEB,
    SystemAgentAction,
    SystemAgentMessage,
    SystemAgentSession,
)
from app.services.system_agent.actions import encrypt_secret_payload
from app.services.system_agent.executor import ActionExecutor
from app.services.system_agent.registry import (
    ActionKeepPendingError,
    ToolRegistry,
    ToolSpec,
)


@pytest.fixture
async def action_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE web_user (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY)"))
        await conn.run_sync(SystemAgentSession.__table__.create)
        await conn.run_sync(SystemAgentMessage.__table__.create)
        await conn.run_sync(SystemAgentAction.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_precheck_failure_keeps_pending_and_clears_secret(action_db, monkeypatch) -> None:
    executed = {"n": 0}

    async def preview(ctx, args):  # noqa: ANN001
        return {"summary": "save provider"}

    async def precheck(ctx, args):  # noqa: ANN001
        assert args.get("api_key") == "sk-bad-key-value-here"
        raise ActionKeepPendingError("Provider 验证失败：401", code="PROVIDER_VERIFY_FAILED")

    async def execute(ctx, args):  # noqa: ANN001
        executed["n"] += 1
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="providers.save",
            description="save",
            input_schema={"type": "object"},
            read_only=False,
            min_role="admin",
            secret_argument_names=("api_key",),
            preview_handler=preview,
            precheck_handler=precheck,
            execute_handler=execute,
        )
    )
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)
    monkeypatch.setattr("app.services.system_agent.executor.audit.write", AsyncMock())

    async with action_db() as db:
        action = SystemAgentAction(
            id="act-verify-fail",
            channel=CHANNEL_WEB,
            tool_name="providers.save",
            arguments={"name": "p1", "has_api_key": True},
            secret_fields=["api_key"],
            secret_payload_enc=encrypt_secret_payload({"api_key": "sk-bad-key-value-here"}),
            summary="创建 Provider",
            preview={},
            status=ACTION_STATUS_PENDING,
            actor_user_id=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.add(action)
        await db.commit()

    result = await ActionExecutor().confirm(
        action_id="act-verify-fail",
        role="admin",
        web_user_id=1,
    )
    assert result["ok"] is False
    assert result.get("keep_pending") is True
    assert result["action"]["status"] == ACTION_STATUS_PENDING
    assert result["action"]["has_secret"] is False
    assert executed["n"] == 0

    async with action_db() as db:
        row = await db.get(SystemAgentAction, "act-verify-fail")
        assert row is not None
        assert row.status == ACTION_STATUS_PENDING
        assert row.secret_payload_enc is None


@pytest.mark.asyncio
async def test_precheck_success_then_execute(action_db, monkeypatch) -> None:
    calls = {"pre": 0, "exec": 0}

    async def preview(ctx, args):  # noqa: ANN001
        return {"summary": "ok"}

    async def precheck(ctx, args):  # noqa: ANN001
        calls["pre"] += 1
        return {"ok": True}

    async def execute(ctx, args):  # noqa: ANN001
        calls["exec"] += 1
        return {"saved": True, "business_changed": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="providers.save",
            description="save",
            input_schema={"type": "object"},
            read_only=False,
            min_role="admin",
            preview_handler=preview,
            precheck_handler=precheck,
            execute_handler=execute,
        )
    )
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)
    monkeypatch.setattr("app.services.system_agent.executor.audit.write", AsyncMock())

    async with action_db() as db:
        db.add(
            SystemAgentAction(
                id="act-ok",
                channel=CHANNEL_WEB,
                tool_name="providers.save",
                arguments={"name": "p1"},
                summary="create",
                preview={},
                status=ACTION_STATUS_PENDING,
                actor_user_id=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.commit()

    result = await ActionExecutor().confirm(action_id="act-ok", role="admin", web_user_id=1)
    assert result["ok"] is True
    assert calls == {"pre": 1, "exec": 1}


@pytest.mark.asyncio
async def test_run_quick_verify_maps_error(monkeypatch) -> None:
    from app.services.system_agent import provider_verify
    from app.services.system_agent.registry import ActionKeepPendingError

    async def fake_events(**kwargs):  # noqa: ANN003
        yield {"type": "error", "ok": False, "error": "invalid key sk-secret"}

    monkeypatch.setattr(provider_verify.llm_quick_verify, "quick_verify_events", fake_events)
    monkeypatch.setattr(
        provider_verify.llm_quick_verify,
        "normalize_quick_verify_base_url",
        lambda x: x or "https://api.openai.com/v1",
    )

    with pytest.raises(ActionKeepPendingError) as ei:
        await provider_verify.run_quick_verify(
            base_url="https://api.openai.com/v1",
            api_key="sk-secret",
            api_format="chat_completions",
            default_model="gpt-4o-mini",
            provider="openai",
        )
    assert "验证失败" in ei.value.message
    assert "sk-secret" not in ei.value.message


@pytest.mark.asyncio
async def test_existing_provider_verify_uses_encrypted_compatibility_headers(monkeypatch) -> None:
    from app.services.system_agent import provider_verify

    row = SimpleNamespace(
        provider="openai",
        base_url="https://api.example/v1",
        default_model="model",
        api_format="responses",
        api_key_enc="encrypted-key",
        request_headers_enc="encrypted-headers",
        protocol_profile="standard",
        client_identity_profile="codex_cli",
    )

    class _Db:
        async def get(self, _model, _provider_id):  # noqa: ANN001
            return row

    monkeypatch.setattr(provider_verify, "decrypt_str", lambda _token: "sk-secret")
    monkeypatch.setattr(
        provider_verify,
        "decrypt_request_headers",
        lambda _token: [{"name": "X-Tenant-ID", "value": "tenant", "scopes": ["liveness"]}],
    )

    resolved = await provider_verify.resolve_provider_verify_args(_Db(), {"id": 7})

    assert resolved["api_key"] == "sk-secret"
    assert resolved["request_headers"] == [{"name": "X-Tenant-ID", "value": "tenant", "scopes": ["liveness"]}]


@pytest.mark.asyncio
async def test_run_quick_verify_forwards_compatibility_headers(monkeypatch) -> None:
    from app.services.system_agent import provider_verify

    captured: dict[str, object] = {}

    async def fake_events(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        yield {"type": "done", "ok": True, "model": "model", "response": "ok"}

    monkeypatch.setattr(provider_verify.llm_quick_verify, "quick_verify_events", fake_events)
    monkeypatch.setattr(
        provider_verify.llm_quick_verify,
        "normalize_quick_verify_base_url",
        lambda value: value,
    )
    headers = [{"name": "X-Tenant-ID", "value": "tenant", "scopes": ["liveness"]}]

    result = await provider_verify.run_quick_verify(
        base_url="https://api.example/v1",
        api_key="sk-secret",
        api_format="responses",
        default_model="model",
        provider="openai",
        request_headers=headers,
    )

    assert result["ok"] is True
    assert captured["request_headers"] == headers
