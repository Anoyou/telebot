"""System Agent 会话/配置/消息编排。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
    MESSAGE_RUN_FAILED,
    MESSAGE_RUN_PENDING,
    MESSAGE_RUN_SUCCEEDED,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    SystemAgentMessage,
    SystemAgentSession,
)
from app.services.system_agent.config import (
    normalize_config,
    resolve_agent_providers,
    resolve_fixed_provider,
    save_config,
    tools_model_for_dto,
    tools_models_for_dto,
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
    assert cfg["require_tool_approval"] is False
    assert cfg["max_steps"] >= 1
    over = normalize_config(
        {
            "enabled": 1,
            "provider_id": "12",
            "model": "  gpt-x  ",
            "fallback_provider_ids": [12, "13", 13, 0, "bad"],
            "require_tool_approval": True,
            "max_steps": 999,
            "max_tool_calls": 999,
            "session_token_limit": 10,
        }
    )
    assert over["enabled"] is True
    assert over["provider_id"] == 12
    assert over["model"] == "gpt-x"
    assert over["fallback_provider_ids"] == [13]
    assert over["require_tool_approval"] is True
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
    assert tools_models_for_dto(dto, "m2") == ["m2"]


@pytest.mark.asyncio
async def test_resolve_fixed_provider_disabled(agent_db) -> None:
    async with agent_db() as db:
        db.add(SystemSetting(key="system_agent_config", value={"enabled": False}))
        await db.commit()
        dto, err = await resolve_fixed_provider(db)
        assert dto is None
        assert "未启用" in err


@pytest.mark.asyncio
async def test_resolve_agent_providers_uses_fallback_allowlist() -> None:
    def row(provider_id: int, name: str, *, supports_tools: bool = True):
        return SimpleNamespace(
            id=provider_id,
            name=name,
            provider="openai",
            api_format="responses",
            base_url="https://example.invalid/v1",
            default_model=f"model-{provider_id}",
            api_key_enc="encrypted",
            models=[
                {
                    "id": f"model-{provider_id}",
                    "enabled": True,
                    "supports_tools": supports_tools,
                }
            ],
        )

    rows = [
        row(1, "primary"),
        row(2, "allowed"),
        row(3, "not-selected"),
        row(4, "no-tools", supports_tools=False),
    ]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return rows

    class _DB:
        async def execute(self, _query):
            return _Result()

    resolved = await resolve_agent_providers(
        _DB(),  # type: ignore[arg-type]
        {
            "enabled": True,
            "provider_id": 1,
            "model": "model-1",
            "fallback_provider_ids": [2, 4],
        },
    )

    assert not isinstance(resolved, str)
    assert list(resolved.providers) == [1, 2]


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
        session.memory_summary = "old summary"
        session.memory_state = {"last_domains": ["logs"]}
        await db.flush()
        msgs = await svc.list_messages(db, session.id)
        assert len(msgs) == 1
        deleted = await svc.clear_messages(db, session)
        assert deleted == 1
        assert await svc.list_messages(db, session.id) == []
        assert session.memory_summary == ""
        assert session.memory_state == {}


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
async def test_successful_turn_redacts_secret_from_persistent_memory(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

    async def fake_stream(*_args, **_kwargs):
        yield {"type": "route_selected", "domains": ["providers"]}
        yield {
            "type": "tool_finished",
            "tool_name": "providers.save",
            "call_id": "c-secret",
            "is_error": False,
            "result_summary": {"echo": secret},
        }
        yield {
            "type": "assistant_message",
            "content": f"已处理 {secret}",
            "usage": {},
        }
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        async for _event in svc.stream_message(
            db,
            session=session,
            text=f"添加 Provider，key={secret}",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass

        assert secret not in session.memory_summary
        assert secret not in str(session.memory_state)
        assert "REDACTED" in str(session.memory_state)
        assert secret not in str(session.title)


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
async def test_failed_turn_is_marked_and_excluded_from_next_history(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()
    histories: list[list[SystemAgentMessage]] = []
    call_count = 0

    async def fake_stream(*_args, **kwargs):
        nonlocal call_count
        histories.append(list(kwargs.get("history_messages") or []))
        call_count += 1
        if call_count == 1:
            yield {"type": "error", "code": "UPSTREAM_503", "message": "上游暂时不可用"}
            yield {"type": "done", "ok": False}
            return
        yield {"type": "assistant_message", "content": "第二轮成功", "usage": {}}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        async for _event in svc.stream_message(
            db,
            session=session,
            text="第一轮失败",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass

        first_messages = await svc.list_messages(db, session.id)
        assert len(first_messages) == 1
        assert first_messages[0].run_status == MESSAGE_RUN_FAILED
        assert first_messages[0].error_code == "UPSTREAM_503"

        async for _event in svc.stream_message(
            db,
            session=session,
            text="第二轮",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass

        assert histories == [[], []]
        rows = await svc.list_messages(db, session.id)
        assert rows[-2].run_status == MESSAGE_RUN_SUCCEEDED
        assert session.memory_state["last_result"] == "第二轮成功"


@pytest.mark.asyncio
async def test_provider_switch_failure_is_persisted_for_confirmation(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()

    async def fake_stream(*_args, **_kwargs):
        yield {
            "type": "error",
            "code": "AGENT_PROVIDER_SWITCH_REQUIRED",
            "message": "是否切换 Provider？",
            "provider_switch": {
                "from_provider_name": "primary",
                "candidates": [
                    {"provider_id": 2, "provider_name": "fallback", "model": "model-b"}
                ],
            },
        }
        yield {"type": "done", "ok": False}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        async for _event in svc.stream_message(
            db,
            session=session,
            text="查询定时任务",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass

        message = (await svc.list_messages(db, session.id))[0]
        assert message.run_status == MESSAGE_RUN_FAILED
        assert message.error_code == "AGENT_PROVIDER_SWITCH_REQUIRED"
        assert message.usage == {
            "provider_switch": {
                "from_provider_name": "primary",
                "candidates": [
                    {"provider_id": 2, "provider_name": "fallback", "model": "model-b"}
                ],
            }
        }


@pytest.mark.asyncio
async def test_tool_approval_failure_is_persisted_for_retry(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()

    async def fake_stream(*_args, **_kwargs):
        yield {
            "type": "error",
            "code": "AGENT_TOOL_APPROVAL_REQUIRED",
            "message": "本轮需要调用系统工具，请批准后继续。",
            "tool_approval": {
                "domains": ["scheduler"],
                "tools": [
                    {
                        "name": "scheduler.list",
                        "description": "查看定时任务",
                        "read_only": True,
                        "risk": "normal",
                    }
                ],
            },
        }
        yield {"type": "done", "ok": False}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        async for _event in svc.stream_message(
            db,
            session=session,
            text="查询定时任务",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass

        message = (await svc.list_messages(db, session.id))[0]
        assert message.error_code == "AGENT_TOOL_APPROVAL_REQUIRED"
        assert message.usage is not None
        assert message.usage["tool_approval"]["tools"][0]["name"] == "scheduler.list"


@pytest.mark.asyncio
async def test_provider_switch_and_tool_approval_context_are_persisted_together(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()

    async def fake_stream(*_args, **_kwargs):
        yield {
            "type": "error",
            "code": "AGENT_PROVIDER_SWITCH_REQUIRED",
            "message": "是否切换 Provider？",
            "provider_switch": {
                "from_provider_name": "primary",
                "candidates": [
                    {"provider_id": 2, "provider_name": "fallback", "model": "model-b"}
                ],
            },
            "tool_approval": {
                "domains": ["scheduler"],
                "tools": [
                    {
                        "name": "scheduler.list",
                        "description": "查看定时任务",
                        "read_only": True,
                        "risk": "normal",
                    }
                ],
            },
        }
        yield {"type": "done", "ok": False}

    monkeypatch.setattr(svc.runtime, "stream_turn", fake_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        async for _event in svc.stream_message(
            db,
            session=session,
            text="查询定时任务",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass

        message = (await svc.list_messages(db, session.id))[0]
        assert message.usage is not None
        assert message.usage["provider_switch"]["candidates"][0]["provider_id"] == 2
        assert message.usage["tool_approval"]["tools"][0]["name"] == "scheduler.list"


@pytest.mark.asyncio
async def test_cancelled_stream_marks_message_retryable(agent_db, monkeypatch) -> None:
    svc = SystemAgentService()

    async def cancelled_stream(*_args, **_kwargs):
        yield {"type": "run_started", "run_id": "cancelled"}
        raise asyncio.CancelledError

    monkeypatch.setattr(svc.runtime, "stream_turn", cancelled_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        stream = svc.stream_message(
            db,
            session=session,
            text="会被断开的请求",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        )
        assert (await anext(stream))["type"] == "run_started"
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)

    async with agent_db() as observer:
        message = (await svc.list_messages(observer, session.id))[0]
        assert message.run_status == MESSAGE_RUN_FAILED
        assert message.error_code == "AGENT_STREAM_CANCELLED"


@pytest.mark.asyncio
async def test_reconcile_stale_pending_message(agent_db) -> None:
    svc = SystemAgentService()
    old = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        db.add(
            SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_USER,
                content={"text": "旧请求"},
                usage={"run_started_at": old},
                run_status="pending",
            )
        )
        await db.commit()
        assert await svc.reconcile_stale_messages(db, session.id) == 1
        await db.commit()
        message = (await svc.list_messages(db, session.id))[0]
        assert message.run_status == MESSAGE_RUN_FAILED
        assert message.error_code == "AGENT_STREAM_INTERRUPTED"


@pytest.mark.asyncio
async def test_reconcile_keeps_pending_message_within_runtime_window(agent_db) -> None:
    svc = SystemAgentService()
    still_running = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        db.add(
            SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_USER,
                content={"text": "仍在运行的长请求"},
                usage={"run_started_at": still_running},
                run_status="pending",
            )
        )
        await db.commit()

        assert await svc.reconcile_stale_messages(db, session.id) == 0
        message = (await svc.list_messages(db, session.id))[0]
        assert message.run_status == MESSAGE_RUN_PENDING


@pytest.mark.asyncio
async def test_retry_reuses_failed_user_message(agent_db, monkeypatch) -> None:
    svc = SystemAgentService()

    async def fail_stream(*_args, **_kwargs):
        yield {"type": "error", "code": "TIMEOUT", "message": "超时"}
        yield {"type": "done", "ok": False}

    monkeypatch.setattr(svc.runtime, "stream_turn", fail_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        async for _event in svc.stream_message(
            db,
            session=session,
            text="查询日志",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
        ):
            pass
        failed = (await svc.list_messages(db, session.id))[0]
        original_id = failed.id

        async def success_stream(*_args, **_kwargs):
            yield {"type": "route_selected", "domains": ["logs"]}
            yield {"type": "assistant_message", "content": "已恢复", "usage": {}}
            yield {"type": "done", "ok": True}

        monkeypatch.setattr(svc.runtime, "stream_turn", success_stream)
        async for _event in svc.stream_message(
            db,
            session=session,
            text="",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
            retry_message=failed,
        ):
            pass

        rows = await svc.list_messages(db, session.id)
        users = [row for row in rows if row.role == MESSAGE_ROLE_USER]
        assert len(users) == 1
        assert users[0].id == original_id
        assert users[0].run_status == MESSAGE_RUN_SUCCEEDED
        assert users[0].retry_count == 1
        assert users[0].error_message is None


@pytest.mark.asyncio
async def test_stale_concurrent_retry_cannot_execute_twice(agent_db, monkeypatch) -> None:
    svc = SystemAgentService()

    async def success_stream(*_args, **_kwargs):
        yield {"type": "assistant_message", "content": "完成", "usage": {}}
        yield {"type": "done", "ok": True}

    monkeypatch.setattr(svc.runtime, "stream_turn", success_stream)

    async with agent_db() as setup_db:
        session = await svc.create_session(
            setup_db,
            channel=CHANNEL_WEB,
            web_user_id=1,
        )
        message = SystemAgentMessage(
            session_id=session.id,
            role=MESSAGE_ROLE_USER,
            content={"text": "只允许重试一次"},
            run_status=MESSAGE_RUN_FAILED,
        )
        setup_db.add(message)
        await setup_db.commit()
        session_id = session.id
        message_id = message.id

    async with agent_db() as first_db, agent_db() as stale_db:
        first_session = await first_db.get(SystemAgentSession, session_id)
        first_message = await first_db.get(SystemAgentMessage, message_id)
        stale_session = await stale_db.get(SystemAgentSession, session_id)
        stale_message = await stale_db.get(SystemAgentMessage, message_id)
        assert first_session is not None and first_message is not None
        assert stale_session is not None and stale_message is not None

        async for _event in svc.stream_message(
            first_db,
            session=first_session,
            text="",
            role="admin",
            channel=CHANNEL_WEB,
            web_user_id=1,
            retry_message=first_message,
        ):
            pass

        with pytest.raises(ValueError, match="已被重试|状态已变化"):
            async for _event in svc.stream_message(
                stale_db,
                session=stale_session,
                text="",
                role="admin",
                channel=CHANNEL_WEB,
                web_user_id=1,
                retry_message=stale_message,
            ):
                pass


@pytest.mark.asyncio
async def test_unexpected_stream_crash_becomes_retryable_failed_turn(
    agent_db, monkeypatch
) -> None:
    svc = SystemAgentService()

    async def crash_stream(*_args, **_kwargs):
        yield {"type": "run_started", "run_id": "r-crash"}
        yield {"type": "assistant_message", "content": "未提交的答案", "usage": {}}
        raise RuntimeError("boom")

    monkeypatch.setattr(svc.runtime, "stream_turn", crash_stream)

    async with agent_db() as db:
        session = await svc.create_session(db, channel=CHANNEL_WEB, web_user_id=1)
        events = [
            event
            async for event in svc.stream_message(
                db,
                session=session,
                text="会崩溃的一轮",
                role="admin",
                channel=CHANNEL_WEB,
                web_user_id=1,
            )
        ]

        assert events[0]["type"] == "run_started"
        assert all(event.get("type") != "assistant_message" for event in events)
        assert any(event.get("code") == "AGENT_STREAM_FAILED" for event in events)
        assert events[-1] == {"type": "done", "ok": False, "session_id": session.id}
        row = (await svc.list_messages(db, session.id))[0]
        assert row.run_status == MESSAGE_RUN_FAILED
        assert row.error_code == "AGENT_STREAM_FAILED"


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
