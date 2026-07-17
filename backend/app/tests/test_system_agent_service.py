"""System Agent 会话/配置/消息编排。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.system import SystemSetting
from app.db.models.system_agent import (
    CHANNEL_BOT,
    CHANNEL_WEB,
    MESSAGE_ROLE_USER,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    SystemAgentMessage,
    SystemAgentSession,
)
from app.services.system_agent.config import (
    normalize_config,
    resolve_fixed_provider,
    save_config,
    tools_model_for_dto,
)
from app.services.system_agent.prompts import session_title_from_message
from app.services.system_agent.service import SystemAgentService


@pytest.fixture
async def agent_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # 最小 FK 目标表，避免创建完整 WebUser / Account 约束
        await conn.execute(text("CREATE TABLE web_user (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY)"))
        await conn.run_sync(SystemSetting.__table__.create)
        await conn.run_sync(SystemAgentSession.__table__.create)
        await conn.run_sync(SystemAgentMessage.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_normalize_config_defaults_and_clamps() -> None:
    cfg = normalize_config(None)
    assert cfg["enabled"] is False
    assert cfg["provider_id"] is None
    assert cfg["max_steps"] >= 1
    over = normalize_config(
        {
            "enabled": 1,
            "provider_id": "12",
            "model": "  gpt-x  ",
            "max_steps": 999,
            "max_tool_calls": 999,
            "session_token_limit": 10,
        }
    )
    assert over["enabled"] is True
    assert over["provider_id"] == 12
    assert over["model"] == "gpt-x"
    assert over["max_steps"] == 16
    assert over["max_tool_calls"] == 64
    assert over["session_token_limit"] == 1024


def test_session_title_from_message() -> None:
    assert session_title_from_message("  交互里\n有哪些规则？  ") == "交互里 有哪些规则？"
    assert len(session_title_from_message("a" * 100)) == 30
    assert session_title_from_message("") == "新对话"


def test_tools_model_for_dto_requires_tools_support() -> None:
    dto = SimpleNamespace(
        enabled_model_ids=lambda: ["m1", "m2"],
        has_model_list=lambda: True,
        pick_enabled_model=lambda: "m1",
        capabilities_for_model=lambda mid: SimpleNamespace(tools=(mid == "m2")),
    )
    assert tools_model_for_dto(dto) == "m2"
    assert tools_model_for_dto(dto, "m1") is None
    assert tools_model_for_dto(dto, "m2") == "m2"
    assert tools_model_for_dto(dto, "missing") is None


@pytest.mark.asyncio
async def test_resolve_fixed_provider_disabled(agent_db) -> None:
    async with agent_db() as db:
        db.add(SystemSetting(key="system_agent_config", value={"enabled": False}))
        await db.commit()
        dto, err = await resolve_fixed_provider(db)
        assert dto is None
        assert "未启用" in err


@pytest.mark.asyncio
async def test_session_crud_and_ownership(agent_db) -> None:
    svc = SystemAgentService()
    async with agent_db() as db:
        s1 = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1, title="t1")
        s2 = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=2, title="t2")
        await db.commit()

        rows = await svc.list_sessions(db, web_user_id=1)
        assert [r.id for r in rows] == [s1.id]

        owned = await svc.get_session(db, s1.id, web_user_id=1)
        assert owned is not None
        foreign = await svc.get_session(db, s2.id, web_user_id=1)
        assert foreign is None

        await svc.update_session(db, s1, status=SESSION_STATUS_ARCHIVED, title="archived")
        await db.commit()
        assert s1.status == SESSION_STATUS_ARCHIVED
        assert s1.title == "archived"

        active = await svc.list_sessions(db, web_user_id=1, status=SESSION_STATUS_ACTIVE)
        assert active == []


@pytest.mark.asyncio
async def test_bot_session_requires_account(agent_db) -> None:
    svc = SystemAgentService()
    async with agent_db() as db:
        with pytest.raises(ValueError, match="account_id"):
            await svc.create_session(db, channel=CHANNEL_BOT, bot_tg_user_id=99)


@pytest.mark.asyncio
async def test_get_or_create_and_clear_messages(agent_db) -> None:
    svc = SystemAgentService()
    async with agent_db() as db:
        session = await svc.get_or_create_active_session(
            db, channel=CHANNEL_WEB, web_user_id=7
        )
        again = await svc.get_or_create_active_session(
            db, channel=CHANNEL_WEB, web_user_id=7
        )
        assert session.id == again.id

        db.add(
            SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_USER,
                content={"text": "hello"},
            )
        )
        await db.flush()
        msgs = await svc.list_messages(db, session.id)
        assert len(msgs) == 1
        deleted = await svc.clear_messages(db, session)
        assert deleted == 1
        assert await svc.list_messages(db, session.id) == []


@pytest.mark.asyncio
async def test_stream_message_persists_redacted_user_and_assistant(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()

    async def fake_stream(*_a, **_k):
        yield {
            "type": "assistant_message",
            "content": "查询完成",
            "usage": {"total_tokens": 3},
        }
        yield {"type": "tool_finished", "tool_name": "system.get_context", "call_id": "c1", "is_error": False, "result_summary": {"ok": True}}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        events: list[dict[str, Any]] = []
        async for ev in svc.stream_message(
            db,
            session=session,
            text="系统版本是多少？",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            events.append(ev)
        await db.commit()

        assert any(e.get("type") == "assistant_message" for e in events)
        msgs = await svc.list_messages(db, session.id)
        roles = [m.role for m in msgs]
        assert MESSAGE_ROLE_USER in roles
        assert "assistant" in roles
        assert "tool" in roles
        assert session.title == "系统版本是多少？"


@pytest.mark.asyncio
async def test_stream_message_commits_history_before_terminal_events(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()

    async def fake_stream(*_a, **_k):
        yield {"type": "run_started", "run_id": "r1"}
        yield {"type": "assistant_message", "content": "已完成", "usage": {}}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        await db.commit()
        stream = svc.stream_message(
            db,
            session=session,
            text="断线后也要保留",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        )

        first = await anext(stream)
        assert first["type"] == "run_started"
        async with agent_db() as observer:
            roles = [m.role for m in await svc.list_messages(observer, session.id)]
            assert roles == [MESSAGE_ROLE_USER]

        second = await anext(stream)
        assert second["type"] == "assistant_message"
        async with agent_db() as observer:
            roles = [m.role for m in await svc.list_messages(observer, session.id)]
            assert roles == [MESSAGE_ROLE_USER, "assistant"]

        await stream.aclose()


@pytest.mark.asyncio
async def test_save_config_roundtrip(agent_db) -> None:
    async with agent_db() as db:
        saved = await save_config(
            db,
            {"enabled": True, "provider_id": 3, "model": "gpt-tools", "max_steps": 6},
        )
        await db.commit()
        assert saved["enabled"] is True
        assert saved["provider_id"] == 3
        assert saved["model"] == "gpt-tools"
        assert saved["max_steps"] == 6
        row = await db.get(SystemSetting, "system_agent_config")
        assert row is not None
        assert row.value["provider_id"] == 3
