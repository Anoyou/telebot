"""System Agent Action 状态机与执行器。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from app.services.system_agent.actions import (
    bot_owns_action,
    create_pending_action,
    mark_expired_if_needed,
    reject_action,
    split_secret_arguments,
    web_owns_action,
)
from app.services.system_agent.context import ToolContext
from app.services.system_agent.executor import ActionExecutor
from app.services.system_agent.registry import ToolRegistry, ToolSpec, reset_registry_for_tests


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
