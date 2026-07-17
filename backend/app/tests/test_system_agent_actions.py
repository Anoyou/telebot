"""System Agent Action 状态机与执行器。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.system_agent import (
    ACTION_STATUS_EXECUTED,
    ACTION_STATUS_FAILED,
    ACTION_STATUS_PENDING,
    ACTION_STATUS_REJECTED,
    CHANNEL_WEB,
    SystemAgentAction,
    SystemAgentMessage,
    SystemAgentSession,
)
from app.services import rule_service
from app.services.system_agent.actions import (
    bot_owns_action,
    create_pending_action,
    encrypt_secret_payload,
    mark_expired_if_needed,
    reject_action,
    split_secret_arguments,
    web_owns_action,
)
from app.services.system_agent.context import ToolContext
from app.services.system_agent.executor import ActionExecutor
from app.services.system_agent.registry import ToolRegistry, ToolSpec, reset_registry_for_tests
from app.worker.ipc import IPCMessage


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


def test_split_secret_arguments() -> None:
    public, secrets, fields = split_secret_arguments(
        {"name": "p", "api_key": "sk-secret", "token": ""},
        ("api_key", "token"),
    )
    assert public["name"] == "p"
    assert public["has_api_key"] is True
    assert "api_key" not in public
    assert secrets == {"api_key": "sk-secret"}
    assert fields == ["api_key"]


def test_ownership_strict() -> None:
    bot_action = SystemAgentAction(
        id="a",
        channel=CHANNEL_WEB,
        tool_name="x",
        arguments={},
        summary="s",
        preview={},
        status=ACTION_STATUS_PENDING,
        actor_user_id=None,
        actor_bot_user_id=42,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert not web_owns_action(bot_action, 1)
    assert bot_owns_action(bot_action, 42)
    assert not bot_owns_action(bot_action, 99)

    web_action = SystemAgentAction(
        id="b",
        channel=CHANNEL_WEB,
        tool_name="x",
        arguments={},
        summary="s",
        preview={},
        status=ACTION_STATUS_PENDING,
        actor_user_id=7,
        actor_bot_user_id=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert web_owns_action(web_action, 7)
    assert not web_owns_action(web_action, 8)


@pytest.mark.asyncio
async def test_create_and_reject_action(action_db) -> None:
    async def preview(ctx, args):  # noqa: ANN001
        return {"summary": "暂停账号", "account_id": 1}

    async def execute(ctx, args):  # noqa: ANN001
        return {"ok": True}

    spec = ToolSpec(
        name="accounts.set_paused",
        description="pause",
        input_schema={"type": "object"},
        read_only=False,
        min_role="operator",
        preview_handler=preview,
        execute_handler=execute,
    )
    async with action_db() as db:
        session = SystemAgentSession(
            id="sess-1",
            channel=CHANNEL_WEB,
            web_user_id=1,
            status="active",
        )
        db.add(session)
        await db.flush()
        ctx = ToolContext(
            db=db,
            channel=CHANNEL_WEB,
            role="admin",
            session=session,
            web_user_id=1,
            account_id=1,
        )
        action = await create_pending_action(
            db,
            ctx=ctx,
            spec=spec,
            arguments={"account_id": 1, "paused": True},
            preview={"summary": "暂停账号 #1", "account_id": 1},
            summary="暂停账号 #1",
        )
        await db.commit()
        assert action.status == ACTION_STATUS_PENDING
        assert action.account_id == 1

        action = await reject_action(db, action)
        await db.commit()
        assert action.status == ACTION_STATUS_REJECTED
        assert action.secret_payload_enc is None


@pytest.mark.asyncio
async def test_expire_pending_action(action_db) -> None:
    async with action_db() as db:
        action = SystemAgentAction(
            id="a1",
            channel=CHANNEL_WEB,
            tool_name="rules.delete",
            arguments={},
            summary="del",
            preview={},
            status=ACTION_STATUS_PENDING,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.add(action)
        await db.commit()
        action = await mark_expired_if_needed(db, action)
        assert action.status == "expired"
        assert action.error_code == "EXPIRED"


@pytest.mark.asyncio
async def test_executor_confirm_and_idempotent(action_db, monkeypatch) -> None:
    calls = {"n": 0}

    async def preview(ctx, args):  # noqa: ANN001
        return {"summary": "x"}

    async def execute(ctx, args):  # noqa: ANN001
        calls["n"] += 1
        return {"done": True, "business_changed": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="features.set_enabled",
            description="set",
            input_schema={"type": "object"},
            read_only=False,
            min_role="operator",
            preview_handler=preview,
            execute_handler=execute,
            runtime_effects=(),
        )
    )
    reset_registry_for_tests()
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)
    monkeypatch.setattr(
        "app.services.system_agent.executor.audit.write",
        AsyncMock(),
    )

    async with action_db() as db:
        action = SystemAgentAction(
            id="act-1",
            channel=CHANNEL_WEB,
            tool_name="features.set_enabled",
            arguments={"account_id": 1, "feature_key": "x", "enabled": True},
            summary="enable",
            preview={"summary": "enable"},
            status=ACTION_STATUS_PENDING,
            actor_user_id=9,
            account_id=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.add(action)
        await db.commit()

    executor = ActionExecutor()
    first = await executor.confirm(action_id="act-1", role="admin", web_user_id=9)
    assert first["ok"] is True
    assert first["action"]["status"] == ACTION_STATUS_EXECUTED
    assert calls["n"] == 1

    second = await executor.confirm(action_id="act-1", role="admin", web_user_id=9)
    assert second["ok"] is True
    assert second.get("already_final") is True
    assert calls["n"] == 1  # 不重复执行


@pytest.mark.asyncio
async def test_executor_rollback_on_handler_error(action_db, monkeypatch) -> None:
    async def preview(ctx, args):  # noqa: ANN001
        return {"summary": "x"}

    async def execute(ctx, args):  # noqa: ANN001
        raise ValueError("业务校验失败")

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="rules.save",
            description="save",
            input_schema={"type": "object"},
            read_only=False,
            min_role="operator",
            preview_handler=preview,
            execute_handler=execute,
        )
    )
    monkeypatch.setattr("app.services.system_agent.executor.get_registry", lambda: reg)
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", action_db)
    monkeypatch.setattr("app.services.system_agent.executor.audit.write", AsyncMock())

    async with action_db() as db:
        action = SystemAgentAction(
            id="act-fail",
            channel=CHANNEL_WEB,
            tool_name="rules.save",
            arguments={"account_id": 1},
            summary="save",
            preview={},
            status=ACTION_STATUS_PENDING,
            actor_user_id=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.add(action)
        await db.commit()

    executor = ActionExecutor()
    result = await executor.confirm(action_id="act-fail", role="admin", web_user_id=1)
    assert result["ok"] is False
    assert result.get("business_changed") is False
    assert result["action"]["status"] == ACTION_STATUS_FAILED


@pytest.mark.asyncio
async def test_scheduler_execute_now_runtime_effect_uses_worker_rpc(monkeypatch) -> None:
    execute_now = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "app.services.rule_service.execute_scheduler_rule_now",
        execute_now,
    )

    await ActionExecutor()._apply_effect(
        "scheduler_execute_now",
        account_id=7,
        action_id="act-scheduler",
        arguments={"rule_id": 9},
    )

    execute_now.assert_awaited_once_with(7, 9)


@pytest.mark.asyncio
async def test_scheduler_execute_now_service_waits_for_worker_result(monkeypatch) -> None:
    pubsub = SimpleNamespace(
        subscribe=AsyncMock(),
        unsubscribe=AsyncMock(),
        aclose=AsyncMock(),
        get_message=AsyncMock(
            return_value={
                "type": "message",
                "data": IPCMessage(type="result", payload={"ok": True}).encode(),
            }
        ),
    )
    redis = SimpleNamespace(
        pubsub=lambda: pubsub,
        publish=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(rule_service, "get_redis", lambda: redis)

    result = await rule_service.execute_scheduler_rule_now(7, 9)

    assert result == {"ok": True, "account_id": 7, "rule_id": 9}
    redis.publish.assert_awaited_once()
    pubsub.unsubscribe.assert_awaited_once()
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_system_restart_runtime_effect_rejects_failed_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system_health.restart_app",
        AsyncMock(
            return_value=SimpleNamespace(
                model_dump=lambda: {"success": False, "error": "当前环境不支持自动重启"}
            )
        ),
    )

    with pytest.raises(RuntimeError, match="不支持自动重启"):
        await ActionExecutor()._apply_effect(
            "system_restart",
            account_id=None,
            action_id="act-restart",
            arguments={},
        )


@pytest.mark.asyncio
async def test_plugin_update_runtime_effect_commits_before_reload(monkeypatch) -> None:
    db_update = SimpleNamespace(commit=AsyncMock())
    db_reload = SimpleNamespace()

    class _Session:
        calls = 0

        def __init__(self) -> None:
            type(self).calls += 1
            self.db = db_update if type(self).calls == 1 else db_reload

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            return False

    update = AsyncMock()
    reload_ = AsyncMock()
    monkeypatch.setattr("app.services.system_agent.executor.AsyncSessionLocal", _Session)
    monkeypatch.setattr("app.services.remote_plugin_service.update", update)
    monkeypatch.setattr("app.services.remote_plugin_service.trigger_reload", reload_)

    await ActionExecutor()._apply_effect(
        "plugin_update",
        account_id=None,
        action_id="act-plugin-update",
        arguments={"plugin_name": "demo"},
    )

    update.assert_awaited_once_with(db_update, "demo")
    db_update.commit.assert_awaited_once()
    reload_.assert_awaited_once_with(db_reload, "demo")


@pytest.mark.asyncio
async def test_precheck_does_not_execute_secret_changed_during_verification(
    action_db, monkeypatch
) -> None:
    execute = AsyncMock(return_value={"business_changed": True})

    async def precheck(_ctx, _args):  # noqa: ANN001
        async with action_db() as other:
            row = await other.get(SystemAgentAction, "act-secret-race")
            assert row is not None
            row.secret_payload_enc = encrypt_secret_payload({"api_key": "sk-key-b-changed"})
            await other.commit()
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="providers.save",
            description="save provider",
            input_schema={"type": "object"},
            read_only=False,
            min_role="admin",
            secret_argument_names=("api_key",),
            preview_handler=AsyncMock(return_value={"summary": "save"}),
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
                id="act-secret-race",
                channel=CHANNEL_WEB,
                tool_name="providers.save",
                arguments={"name": "demo", "has_api_key": True},
                secret_fields=["api_key"],
                secret_payload_enc=encrypt_secret_payload({"api_key": "sk-key-a-original"}),
                summary="save",
                preview={},
                status=ACTION_STATUS_PENDING,
                actor_user_id=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.commit()

    result = await ActionExecutor().confirm(
        action_id="act-secret-race",
        role="admin",
        web_user_id=1,
    )

    assert result["ok"] is False
    assert result["keep_pending"] is True
    assert result["error_code"] == "PRECHECK_STALE"
    assert result["action"]["status"] == ACTION_STATUS_PENDING
    assert result["action"]["has_secret"] is True
    execute.assert_not_awaited()
