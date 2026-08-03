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
from app.llm_probe_defaults import (
    QUICK_VERIFY_MAX_TOKENS,
    QUICK_VERIFY_MESSAGE,
    QUICK_VERIFY_SYSTEM_PROMPT,
    QUICK_VERIFY_TIMEOUT_SECONDS,
)
from app.services.system_agent.actions import decrypt_secret_payload, encrypt_secret_payload
from app.services.system_agent.context import ToolContext
from app.services.system_agent.executor import ActionExecutor
from app.services.system_agent.registry import (
    ActionKeepPendingError,
    PreparedAction,
    ToolRegistry,
    ToolSpec,
)
from app.services.system_agent.runtime import SystemAgentRuntime


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
        raise ActionKeepPendingError(
            "Provider 验证失败：401",
            code="API_KEY_REJECTED",
            clear_secret_names=("api_key",),
        )

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
async def test_upstream_precheck_failure_keeps_encrypted_secret(action_db, monkeypatch) -> None:
    async def precheck(_ctx, _args):  # noqa: ANN001
        raise ActionKeepPendingError(
            "Responses streaming 接口返回 503: model_not_found",
            code="PROVIDER_VERIFY_FAILED",
        )

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="providers.save",
            description="save",
            input_schema={"type": "object"},
            read_only=False,
            min_role="admin",
            secret_argument_names=("api_key", "request_headers"),
            precheck_clear_secret_argument_names=("api_key",),
            preview_handler=AsyncMock(return_value={"summary": "save"}),
            precheck_handler=precheck,
            execute_handler=AsyncMock(),
        )
    )
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)

    async with action_db() as db:
        db.add(
            SystemAgentAction(
                id="act-upstream-fail",
                channel=CHANNEL_WEB,
                tool_name="providers.save",
                arguments={"name": "p1", "has_api_key": True},
                secret_fields=["api_key", "request_headers"],
                secret_payload_enc=encrypt_secret_payload(
                    {
                        "api_key": "sk-still-valid",
                        "request_headers": [{"name": "x-client", "value": "desktop"}],
                    }
                ),
                summary="创建 Provider",
                preview={},
                status=ACTION_STATUS_PENDING,
                actor_user_id=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.commit()

    result = await ActionExecutor().confirm(
        action_id="act-upstream-fail", role="admin", web_user_id=1
    )

    assert result["error_code"] == "PROVIDER_VERIFY_FAILED"
    assert result["action"]["has_secret"] is True
    async with action_db() as db:
        row = await db.get(SystemAgentAction, "act-upstream-fail")
        assert row is not None
        assert decrypt_secret_payload(row.secret_payload_enc)["api_key"] == "sk-still-valid"


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
async def test_provider_action_syncs_gateway_candidate_before_commit(
    action_db, monkeypatch
) -> None:
    import asyncio

    from app.services import gateway_runtime
    from app.services.gateway_runtime import GatewayRuntimeStatus

    async def execute(ctx, _args):  # noqa: ANN001
        ctx.gateway_candidate_sync = True
        return {"saved": True, "business_changed": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="providers.save",
            description="save",
            input_schema={"type": "object"},
            read_only=False,
            min_role="admin",
            preview_handler=AsyncMock(return_value={"summary": "save"}),
            execute_handler=execute,
        )
    )
    sync = AsyncMock(
        return_value=GatewayRuntimeStatus("ready", True, 1, 1, version="test")
    )
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)
    monkeypatch.setattr("app.services.system_agent.executor.audit.write", AsyncMock())
    monkeypatch.setattr(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock())
    monkeypatch.setattr(gateway_runtime, "reconcile_gateway_runtime_from_session", sync)

    async with action_db() as db:
        db.add(
            SystemAgentAction(
                id="act-gateway-sync",
                channel=CHANNEL_WEB,
                tool_name="providers.save",
                arguments={"name": "gateway"},
                summary="save",
                preview={},
                status=ACTION_STATUS_PENDING,
                actor_user_id=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.commit()

    result = await ActionExecutor().confirm(
        action_id="act-gateway-sync", role="admin", web_user_id=1
    )

    assert result["ok"] is True
    sync.assert_awaited_once()
    assert gateway_runtime.gateway_provider_transaction_lock.locked() is False


@pytest.mark.asyncio
async def test_provider_action_commit_failure_restores_gateway_snapshot(
    action_db, monkeypatch
) -> None:
    import asyncio

    from app.services import gateway_runtime
    from app.services.gateway_runtime import GatewayRuntimeStatus

    async def execute(ctx, _args):  # noqa: ANN001
        ctx.gateway_candidate_sync = True

        async def fail_commit() -> None:
            raise RuntimeError("commit failed")

        ctx.db.commit = fail_commit
        return {"saved": True, "business_changed": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="providers.save",
            description="save",
            input_schema={"type": "object"},
            read_only=False,
            min_role="admin",
            preview_handler=AsyncMock(return_value={"summary": "save"}),
            execute_handler=execute,
        )
    )
    sync = AsyncMock(
        return_value=GatewayRuntimeStatus("ready", True, 1, 1, version="test")
    )

    async def restore(db):  # noqa: ANN001
        await db.rollback()
        return True

    restore_mock = AsyncMock(side_effect=restore)
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)
    monkeypatch.setattr("app.services.system_agent.executor.audit.write", AsyncMock())
    monkeypatch.setattr(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock())
    monkeypatch.setattr(gateway_runtime, "reconcile_gateway_runtime_from_session", sync)
    monkeypatch.setattr(gateway_runtime, "rollback_and_restore_gateway", restore_mock)

    async with action_db() as db:
        db.add(
            SystemAgentAction(
                id="act-gateway-commit-fail",
                channel=CHANNEL_WEB,
                tool_name="providers.save",
                arguments={"name": "gateway"},
                summary="save",
                preview={},
                status=ACTION_STATUS_PENDING,
                actor_user_id=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.commit()

    result = await ActionExecutor().confirm(
        action_id="act-gateway-commit-fail", role="admin", web_user_id=1
    )

    assert result["ok"] is False
    assert result["error_code"] == "COMMIT_FAILED"
    sync.assert_awaited_once()
    restore_mock.assert_awaited_once()
    assert gateway_runtime.gateway_provider_transaction_lock.locked() is False


@pytest.mark.asyncio
async def test_probe_action_pipeline_replaces_mask_and_encrypts_chat_secret(action_db) -> None:
    async def preview(_ctx, args):  # noqa: ANN001
        assert args["api_key"] == "sk-real-secret-value-from-chat"
        return PreparedAction(
            arguments={
                **args,
                "name": "api.example",
                "provider": "openai",
                "default_model": "chat-model",
                "api_format": "chat_completions",
            },
            preview={
                "summary": "测活成功，是否添加 Provider「api.example」？",
                "mode": "verified_create",
            },
        )

    async def execute(_ctx, _args):  # noqa: ANN001
        return {"ok": True}

    spec = ToolSpec(
        name="providers.probe_and_add",
        description="probe",
        input_schema={"type": "object"},
        read_only=False,
        min_role="admin",
        secret_argument_names=("api_key",),
        preview_handler=preview,
        execute_handler=execute,
    )

    async with action_db() as db:
        ctx = ToolContext(
            db=db,
            channel=CHANNEL_WEB,
            role="admin",
            web_user_id=1,
            chat_secrets=["sk-real-secret-value-from-chat"],
        )
        events: list[dict] = []
        handler = SystemAgentRuntime()._bind_write_handler(  # noqa: SLF001
            spec,
            ctx,
            events,
            lambda event_type, **payload: {"type": event_type, **payload},
        )
        result = await handler(
            {
                "base_url": "https://api.example/v1",
                "api_key": "[REDACTED]",
            }
        )

        action = await db.get(SystemAgentAction, result["action_id"])
        assert action is not None
        assert action.arguments["has_api_key"] is True
        assert "api_key" not in action.arguments
        assert "sk-real-secret-value-from-chat" not in str(action.arguments)
        assert decrypt_secret_payload(action.secret_payload_enc) == {
            "api_key": "sk-real-secret-value-from-chat"
        }
        assert events[0]["type"] == "action_proposed"
        assert events[0]["action"]["has_secret"] is True


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
    assert captured["system_prompt"] == QUICK_VERIFY_SYSTEM_PROMPT
    assert captured["message"] == QUICK_VERIFY_MESSAGE
    assert captured["max_tokens"] == QUICK_VERIFY_MAX_TOKENS
    assert captured["timeout_seconds"] == QUICK_VERIFY_TIMEOUT_SECONDS


def test_agent_client_selection_can_temporarily_force_direct_identity() -> None:
    from app.services.llm_dto import LLMProviderDTO
    from app.services.system_agent.config import ResolvedAgentProviders
    from app.services.system_agent.runtime import _apply_client_selection

    provider = LLMProviderDTO(
        id=7,
        name="Gateway provider",
        provider="openai",
        execution_backend="codex_gateway",
        api_format="responses",
        protocol_profile="codex_responses",
        client_identity_profile="auto",
        base_url="https://api.example.test/v1",
        api_key_enc="encrypted",
        default_model="gpt-5",
        models=[{"id": "gpt-5", "enabled": True, "supports_tools": True}],
    )
    resolved = ResolvedAgentProviders(
        primary=provider,
        model="gpt-5",
        providers={provider.id: provider},
    )

    selected = _apply_client_selection(
        resolved,
        {
            "mode": "pinned",
            "execution_backend": "direct",
            "client_identity_profile": "grok_cli",
        },
    )

    assert not isinstance(selected, str)
    assert selected.primary.execution_backend == "direct"
    assert selected.primary.client_identity_profile == "grok_cli"
    assert provider.execution_backend == "codex_gateway"


def test_agent_gateway_client_rejects_direct_pinned_provider() -> None:
    from app.services.llm_dto import LLMProviderDTO
    from app.services.system_agent.config import ResolvedAgentProviders
    from app.services.system_agent.runtime import _apply_client_selection

    provider = LLMProviderDTO(
        id=8,
        name="Direct provider",
        provider="openai",
        execution_backend="direct",
        api_format="responses",
        base_url="https://api.example.test/v1",
        api_key_enc="encrypted",
        default_model="gpt-5",
        models=[{"id": "gpt-5", "enabled": True, "supports_tools": True}],
    )
    resolved = ResolvedAgentProviders(
        primary=provider,
        model="gpt-5",
        providers={provider.id: provider},
    )

    selected = _apply_client_selection(
        resolved,
        {"mode": "pinned", "execution_backend": "codex_gateway"},
    )

    assert selected == "该 Provider 未配置为内置 Codex Gateway，不能在 Agent 中临时转入 Gateway"
