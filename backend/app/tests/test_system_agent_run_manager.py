"""System Agent durable run：断线恢复、幂等、取消与重启对账。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.system_agent import (
    AGENT_RUN_CANCELLED,
    AGENT_RUN_FAILED,
    AGENT_RUN_QUEUED,
    AGENT_RUN_SUCCEEDED,
    CHANNEL_WEB,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MESSAGE_RUN_COMPLETED,
    MESSAGE_RUN_PENDING,
    MESSAGE_RUN_SUCCEEDED,
    SystemAgentMessage,
    SystemAgentRun,
    SystemAgentRunEvent,
    SystemAgentSession,
)
from app.services.system_agent.run_manager import RunConflictError, SystemAgentRunManager


class _ControlledService:
    def __init__(self, response: str = "完成") -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.response = response

    async def stream_message(self, db, **kwargs):
        self.calls += 1
        session = kwargs["session"]
        message = kwargs.get("retry_message")
        if message is None:
            message = SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_USER,
                content={"text": kwargs["text"]},
                run_status=MESSAGE_RUN_PENDING,
            )
            db.add(message)
            await db.flush()
            await db.commit()
        yield {"type": "run_started", "session_id": session.id}
        await self.release.wait()
        message.run_status = MESSAGE_RUN_SUCCEEDED
        db.add(
            SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_ASSISTANT,
                content={"text": self.response},
                run_status=MESSAGE_RUN_COMPLETED,
            )
        )
        await db.commit()
        yield {"type": "assistant_message", "content": self.response}
        yield {"type": "done", "ok": True}


@pytest.fixture
async def run_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE web_user (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY)"))
        await conn.run_sync(SystemAgentSession.__table__.create)
        await conn.run_sync(SystemAgentMessage.__table__.create)
        await conn.run_sync(SystemAgentRun.__table__.create)
        await conn.run_sync(SystemAgentRunEvent.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(text("INSERT INTO web_user (id) VALUES (7)"))
        db.add(
            SystemAgentSession(
                id="session-1",
                web_user_id=7,
                channel=CHANNEL_WEB,
                status="active",
            )
        )
        await db.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _wait_for_status(manager: SystemAgentRunManager, run_id: str, status: str) -> Any:
    for _ in range(200):
        row = await manager.get_run(run_id)
        if row.status == status:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {status}")


@pytest.mark.asyncio
async def test_subscriber_disconnect_does_not_cancel_run_and_events_resume(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-disconnect",
        text="交互里有哪些规则？",
    )
    assert run.user_message_id is not None

    subscriber = manager.stream_events(run.id)
    first = await anext(subscriber)
    assert first["type"] == "run_started"
    first_seq = first["seq"]
    await subscriber.aclose()
    assert (await manager.get_run(run.id)).status == "running"

    service.release.set()
    terminal = await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)
    resumed = [event async for event in manager.stream_events(run.id, after_seq=first_seq)]

    assert terminal.last_seq == 3
    assert [event["type"] for event in resumed] == ["assistant_message", "done"]
    assert [event["seq"] for event in resumed] == [first_seq + 1, first_seq + 2]


@pytest.mark.asyncio
async def test_start_is_idempotent_and_cancel_is_idempotent(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    first = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-idempotent",
        text="继续刚才的",
    )
    second = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-idempotent",
        text="继续刚才的",
    )
    assert second.id == first.id
    assert service.calls == 1

    await manager.cancel_run(first.id)
    cancelled = await _wait_for_status(manager, first.id, AGENT_RUN_CANCELLED)
    again = await manager.cancel_run(first.id)
    events = await manager.list_events(first.id)

    assert again.status == AGENT_RUN_CANCELLED
    assert again.finished_at == cancelled.finished_at
    assert [row.event["type"] for row in events][-2:] == ["error", "done"]
    assert events[-1].event["ok"] is False


@pytest.mark.asyncio
async def test_different_request_is_rejected_while_session_run_is_active(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    first = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-active-first",
        text="查看交互规则",
    )

    with pytest.raises(RunConflictError, match="当前会话已有一轮助手请求正在执行"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=7,
            client_request_id="request-active-second",
            text="查看最近日志",
        )

    await manager.cancel_run(first.id)
    await _wait_for_status(manager, first.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_persisted_events_redact_turn_secrets(run_db) -> None:
    secret = "xai-abcdefghijklmnop"
    service = _ControlledService(response=f"已保存 {secret}")
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-redaction",
        text=f"token: {secret}",
    )
    service.release.set()
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)
    events = await manager.list_events(run.id)
    persisted = json.dumps([row.event for row in events], ensure_ascii=False)

    assert secret not in persisted
    assert "[REDACTED]" in persisted


@pytest.mark.asyncio
async def test_persisted_delta_events_redact_unknown_provider_secret(run_db) -> None:
    secret = "gsk_abcdefghijklmnopqrstuvwxyz123456"

    class _DeltaService(_ControlledService):
        async def stream_message(self, db, **kwargs):
            async for event in super().stream_message(db, **kwargs):
                if event["type"] == "assistant_message":
                    yield {"type": "assistant_delta", "delta": f"模型输出 {secret}"}
                yield event

    service = _DeltaService(response="已完成")
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-delta-redaction",
        text="检查输出",
    )
    service.release.set()
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)
    events = await manager.list_events(run.id)
    persisted = json.dumps([row.event for row in events], ensure_ascii=False)

    assert secret not in persisted
    assert "[REDACTED]" in persisted


@pytest.mark.asyncio
async def test_lazy_reconcile_marks_previous_process_runs_retryable(run_db) -> None:
    async with run_db() as db:
        db.add(
            SystemAgentRun(
                id="orphan-run",
                session_id="session-1",
                web_user_id=7,
                client_request_id="request-orphan",
                request_hash="0" * 64,
                kind="message",
                status=AGENT_RUN_QUEUED,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

    manager = SystemAgentRunManager(session_factory=run_db, poll_interval=0.01)
    await manager.ensure_ready()
    run = await manager.get_run("orphan-run")
    events = await manager.list_events("orphan-run")

    assert run.status == AGENT_RUN_FAILED
    assert run.error_code == "AGENT_RUN_INTERRUPTED"
    assert [row.event["type"] for row in events] == ["error", "done"]
    assert events[-1].seq == run.last_seq == 2


@pytest.mark.asyncio
async def test_reconcile_preserves_success_when_done_event_was_already_persisted(run_db) -> None:
    async with run_db() as db:
        db.add(
            SystemAgentRun(
                id="completed-orphan-run",
                session_id="session-1",
                web_user_id=7,
                client_request_id="request-completed-orphan",
                request_hash="1" * 64,
                kind="message",
                status=AGENT_RUN_QUEUED,
                last_seq=1,
                created_at=datetime.now(UTC),
            )
        )
        db.add(
            SystemAgentRunEvent(
                run_id="completed-orphan-run",
                seq=1,
                event={"type": "done", "ok": True, "seq": 1},
            )
        )
        await db.commit()

    manager = SystemAgentRunManager(session_factory=run_db, poll_interval=0.01)
    await manager.ensure_ready()
    run = await manager.get_run("completed-orphan-run")
    events = await manager.list_events("completed-orphan-run")

    assert run.status == AGENT_RUN_SUCCEEDED
    assert run.error_code is None
    assert len(events) == 1


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0046_system_agent_runs.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0046", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0046_upgrade_and_downgrade(monkeypatch) -> None:
    migration = _load_migration_module()
    created_tables: list[str] = []
    created_indexes: list[str] = []
    dropped_tables: list[str] = []
    dropped_indexes: list[str] = []
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *_columns, **_kwargs: created_tables.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *_args, **_kwargs: created_indexes.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda name, **_kwargs: dropped_tables.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, **_kwargs: dropped_indexes.append(name),
    )

    migration.upgrade()
    migration.downgrade()

    assert migration.revision == "0046"
    assert migration.down_revision == "0045"
    assert created_tables == ["system_agent_run", "system_agent_run_event"]
    assert created_indexes == [
        "ix_system_agent_run_session_created",
        "ix_system_agent_run_user_status",
        "ix_system_agent_run_status_updated",
        "ix_system_agent_run_event_run_seq",
    ]
    assert dropped_tables == ["system_agent_run_event", "system_agent_run"]
    assert dropped_indexes == list(reversed(created_indexes))
