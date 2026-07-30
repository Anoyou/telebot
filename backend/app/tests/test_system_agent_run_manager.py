"""System Agent durable run：断线恢复、幂等、取消与重启对账。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crypto import decrypt_str, encrypt_str
from app.db.models.system_agent import (
    AGENT_RUN_CANCELLED,
    AGENT_RUN_FAILED,
    AGENT_RUN_QUEUED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_SUCCEEDED,
    AGENT_RUN_WAITING_APPROVAL,
    AGENT_RUN_WAITING_INPUT,
    CHANNEL_BOT,
    CHANNEL_WEB,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MESSAGE_RUN_COMPLETED,
    MESSAGE_RUN_PENDING,
    MESSAGE_RUN_SUCCEEDED,
    PENDING_TURN_CANCELLED,
    PENDING_TURN_PAUSED,
    PENDING_TURN_PENDING,
    RUN_INPUT_APPLIED,
    RUN_INPUT_APPROVAL,
    RUN_INPUT_STEER,
    RUN_INPUT_USER,
    SystemAgentMessage,
    SystemAgentPendingTurn,
    SystemAgentRun,
    SystemAgentRunEvent,
    SystemAgentRunInput,
    SystemAgentSession,
)
from app.services.system_agent.run_manager import (
    RunConflictError,
    RunNotFoundError,
    SystemAgentRunManager,
    _WorkerLeaseLost,
)


class _ControlledService:
    def __init__(self, response: str = "完成") -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.response = response
        self.kwargs: list[dict[str, Any]] = []

    async def stream_message(self, db, **kwargs):
        self.calls += 1
        self.kwargs.append(dict(kwargs))
        session = kwargs["session"]
        message = kwargs.get("retry_message") or kwargs.get("regenerate_message")
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
        assistant = kwargs.get("regenerate_assistant_message")
        if assistant is not None:
            assistant.content = {"text": self.response}
        else:
            db.add(SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_ASSISTANT,
                content={"text": self.response},
                run_status=MESSAGE_RUN_COMPLETED,
            ))
        await db.commit()
        yield {"type": "assistant_message", "content": self.response}
        yield {"type": "done", "ok": True}


class _WaitingService:
    def __init__(self, *, approval: bool = False) -> None:
        self.approval = approval
        self.calls = 0
        self.kwargs: list[dict[str, Any]] = []

    async def stream_message(self, db, **kwargs):
        self.calls += 1
        self.kwargs.append(dict(kwargs))
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
        if self.calls == 1:
            yield {
                "type": "error",
                "code": (
                    "AGENT_TOOL_APPROVAL_REQUIRED"
                    if self.approval
                    else "AGENT_PROVIDER_SWITCH_REQUIRED"
                ),
                "message": "需要审批" if self.approval else "需要补充输入",
            }
            yield {"type": "done", "ok": False}
            return
        message.run_status = MESSAGE_RUN_SUCCEEDED
        db.add(
            SystemAgentMessage(
                session_id=session.id,
                role=MESSAGE_ROLE_ASSISTANT,
                content={"text": "恢复完成"},
                run_status=MESSAGE_RUN_COMPLETED,
            )
        )
        await db.commit()
        yield {"type": "assistant_message", "content": "恢复完成"}
        yield {"type": "done", "ok": True}


@pytest.fixture
async def run_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE web_user (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY)"))
        await conn.run_sync(SystemAgentSession.__table__.create)
        await conn.run_sync(SystemAgentMessage.__table__.create)
        await conn.run_sync(SystemAgentPendingTurn.__table__.create)
        await conn.run_sync(SystemAgentRun.__table__.create)
        await conn.run_sync(SystemAgentRunInput.__table__.create)
        await conn.run_sync(SystemAgentRunEvent.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(text("INSERT INTO web_user (id) VALUES (7)"))
        await db.execute(text("INSERT INTO account (id) VALUES (11)"))
        db.add(
            SystemAgentSession(
                id="session-1",
                web_user_id=7,
                channel=CHANNEL_WEB,
                status="active",
            )
        )
        db.add(
            SystemAgentSession(
                id="bot-session-1",
                bot_tg_user_id=42,
                account_id=11,
                channel=CHANNEL_BOT,
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
    run = await _wait_for_status(manager, run.id, "running")
    for _ in range(100):
        run = await manager.get_run(run.id)
        if run.user_message_id is not None:
            break
        await asyncio.sleep(0.01)
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
    await _wait_for_status(manager, first.id, "running")
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
async def test_list_runs_filters_by_owner_and_status(run_db) -> None:
    async with run_db() as db:
        await db.execute(text("INSERT INTO web_user (id) VALUES (8)"))
        db.add_all(
            [
                SystemAgentRun(
                    id="listed-failed",
                    session_id="session-1",
                    web_user_id=7,
                    client_request_id="request-listed-failed",
                    request_hash="2" * 64,
                    kind="message",
                    status=AGENT_RUN_FAILED,
                    created_at=datetime.now(UTC),
                ),
                SystemAgentRun(
                    id="listed-other-user",
                    session_id="session-1",
                    web_user_id=8,
                    client_request_id="request-listed-other-user",
                    request_hash="3" * 64,
                    kind="message",
                    status=AGENT_RUN_FAILED,
                    created_at=datetime.now(UTC),
                ),
                SystemAgentRun(
                    id="listed-bot",
                    session_id="bot-session-1",
                    bot_tg_user_id=42,
                    channel=CHANNEL_BOT,
                    client_request_id="request-listed-bot",
                    request_hash="4" * 64,
                    kind="message",
                    status=AGENT_RUN_FAILED,
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await db.commit()

    manager = SystemAgentRunManager(session_factory=run_db, poll_interval=0.01)
    rows = await manager.list_runs(web_user_id=7, status=AGENT_RUN_FAILED)
    rows_with_bot = await manager.list_runs(
        web_user_id=7,
        status=AGENT_RUN_FAILED,
        include_bot=True,
    )

    assert [row.id for row in rows] == ["listed-failed"]
    assert {row.id for row in rows_with_bot} == {"listed-failed", "listed-bot"}


@pytest.mark.asyncio
async def test_list_queue_can_include_bot_items_read_only(run_db) -> None:
    async with run_db() as db:
        pending = SystemAgentPendingTurn(
            id="listed-bot-turn",
            session_id="bot-session-1",
            bot_tg_user_id=42,
            account_id=11,
            channel=CHANNEL_BOT,
            kind="message",
            position=1,
            status=PENDING_TURN_PAUSED,
            client_request_id="request-listed-bot-turn",
            request_hash="5" * 64,
            content_enc=encrypt_str("Bot 排队内容"),
            request_payload={"role": "viewer"},
            dispatch_run_id="listed-bot-queued-run",
        )
        db.add(pending)
        await db.flush()
        db.add(
            SystemAgentRun(
                id="listed-bot-queued-run",
                session_id="bot-session-1",
                bot_tg_user_id=42,
                channel=CHANNEL_BOT,
                pending_turn_id=pending.id,
                client_request_id="request-listed-bot-turn",
                request_hash="5" * 64,
                kind="message",
                status=AGENT_RUN_QUEUED,
                phase="paused",
            )
        )
        await db.commit()

    manager = SystemAgentRunManager(session_factory=run_db, poll_interval=0.01)
    web_only = await manager.list_queue(web_user_id=7)
    with_bot = await manager.list_queue(web_user_id=7, include_bot=True)

    assert web_only == []
    assert [(item["channel"], item["content"]) for item in with_bot] == [
        (CHANNEL_BOT, "Bot 排队内容")
    ]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_different_request_is_queued_while_session_run_is_active(run_db) -> None:
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
    await _wait_for_status(manager, first.id, "running")

    second = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-active-second",
        text="查看最近日志",
    )
    queue = await manager.list_queue(web_user_id=7, session_id="session-1")

    assert second.status == AGENT_RUN_QUEUED
    assert [item["run_id"] for item in queue if item["status"] == "pending"] == [
        second.id
    ]

    await manager.cancel_run(first.id)
    await _wait_for_status(manager, first.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_stop_and_replace_is_atomic_and_idempotent(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    current = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-stop-replace-current",
        text="旧任务",
    )
    await _wait_for_status(manager, current.id, AGENT_RUN_RUNNING)

    replacement = await manager.stop_and_replace(
        current.id,
        web_user_id=7,
        client_request_id="request-stop-replace-new",
        text="新任务",
        model_selection={"provider_id": 3},
    )
    duplicate = await manager.stop_and_replace(
        current.id,
        web_user_id=7,
        client_request_id="request-stop-replace-new",
        text="新任务",
        model_selection={"provider_id": 3},
    )

    assert duplicate.id == replacement.id
    async with run_db() as db:
        stored_current = await db.get(SystemAgentRun, current.id)
        stored_replacement = await db.get(SystemAgentRun, replacement.id)
        pending = await db.get(
            SystemAgentPendingTurn,
            stored_replacement.pending_turn_id if stored_replacement else "missing",
        )
        assert stored_current is not None and stored_current.cancel_requested is True
        assert stored_current.paused_reason == "stop_replace"
        assert stored_replacement is not None
        assert pending is not None and decrypt_str(pending.content_enc) == "新任务"
        assert pending.request_payload["model_selection"] == {"provider_id": 3}

    with pytest.raises(RunConflictError, match="同一个请求标识"):
        await manager.stop_and_replace(
            current.id,
            web_user_id=7,
            client_request_id="request-stop-replace-new",
            text="不同的新任务",
            model_selection={"provider_id": 3},
        )

    await _wait_for_status(manager, current.id, AGENT_RUN_CANCELLED)
    await _wait_for_status(manager, replacement.id, AGENT_RUN_RUNNING)
    await manager.cancel_run(replacement.id)
    await _wait_for_status(manager, replacement.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_stop_and_replace_rejects_non_owner_and_terminal_run(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    current = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-stop-replace-guard",
        text="旧任务",
    )
    await _wait_for_status(manager, current.id, AGENT_RUN_RUNNING)

    with pytest.raises(RunNotFoundError):
        await manager.stop_and_replace(
            current.id,
            web_user_id=8,
            client_request_id="request-stop-replace-wrong-owner",
            text="越权替换",
        )

    await manager.cancel_run(current.id)
    await _wait_for_status(manager, current.id, AGENT_RUN_CANCELLED)
    with pytest.raises(RunConflictError, match="当前任务已结束"):
        await manager.stop_and_replace(
            current.id,
            web_user_id=7,
            client_request_id="request-stop-replace-terminal",
            text="终态替换",
        )


@pytest.mark.asyncio
async def test_message_and_stop_replace_reject_blank_content(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    with pytest.raises(RunConflictError, match="消息不能为空"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=7,
            client_request_id="request-blank-message",
            text="   ",
        )

    current = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-blank-replacement-current",
        text="正常任务",
    )
    await _wait_for_status(manager, current.id, AGENT_RUN_RUNNING)
    with pytest.raises(RunConflictError, match="替代消息不能为空"):
        await manager.stop_and_replace(
            current.id,
            web_user_id=7,
            client_request_id="request-blank-replacement",
            text=" \n ",
        )
    await manager.cancel_run(current.id)
    await _wait_for_status(manager, current.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_run_input_validation_is_enforced_by_manager(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    current = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-input-validation-current",
        text="正常任务",
    )
    await _wait_for_status(manager, current.id, AGENT_RUN_RUNNING)

    with pytest.raises(RunConflictError, match="Steer 内容不能为空"):
        await manager.add_run_input(
            current.id,
            kind=RUN_INPUT_STEER,
            client_request_id="request-blank-steer",
            payload={"content": "   "},
        )
    with pytest.raises(RunConflictError, match="请选择要批准的工具"):
        await manager.add_run_input(
            current.id,
            kind=RUN_INPUT_APPROVAL,
            client_request_id="request-blank-approval",
            payload={"approved": True, "approved_tools": []},
        )

    await manager.cancel_run(current.id)
    await _wait_for_status(manager, current.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "waiting_status",
    [AGENT_RUN_WAITING_INPUT, AGENT_RUN_WAITING_APPROVAL],
)
async def test_stop_and_replace_cancels_waiting_run_and_dispatches_replacement(
    run_db,
    waiting_status: str,
) -> None:
    async with run_db() as db:
        pending = SystemAgentPendingTurn(
            id=f"waiting-turn-{waiting_status}",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=1,
            status="dispatched",
            client_request_id=f"request-waiting-{waiting_status}",
            request_hash="7" * 64,
            content_enc=encrypt_str("等待中的任务"),
            request_payload={"role": "admin"},
            dispatch_run_id=f"waiting-run-{waiting_status}",
        )
        following_pending = SystemAgentPendingTurn(
            id=f"waiting-following-turn-{waiting_status}",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=2,
            status=PENDING_TURN_PAUSED,
            blocked_reason=waiting_status,
            client_request_id=f"request-waiting-following-{waiting_status}",
            request_hash="8" * 64,
            content_enc=encrypt_str("原队列后续任务"),
            request_payload={"role": "admin"},
            dispatch_run_id=f"waiting-following-run-{waiting_status}",
        )
        db.add_all([pending, following_pending])
        await db.flush()
        db.add_all(
            [
                SystemAgentRun(
                    id=f"waiting-run-{waiting_status}",
                    session_id="session-1",
                    web_user_id=7,
                    channel=CHANNEL_WEB,
                    pending_turn_id=pending.id,
                    client_request_id=f"request-waiting-{waiting_status}",
                    request_hash="7" * 64,
                    kind="message",
                    status=waiting_status,
                    phase="waiting",
                ),
                SystemAgentRun(
                    id=f"waiting-following-run-{waiting_status}",
                    session_id="session-1",
                    web_user_id=7,
                    channel=CHANNEL_WEB,
                    pending_turn_id=following_pending.id,
                    client_request_id=f"request-waiting-following-{waiting_status}",
                    request_hash="8" * 64,
                    kind="message",
                    status=AGENT_RUN_QUEUED,
                    phase="paused",
                    paused_reason=waiting_status,
                ),
            ]
        )
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    replacement = await manager.stop_and_replace(
        f"waiting-run-{waiting_status}",
        web_user_id=7,
        client_request_id=f"request-replace-{waiting_status}",
        text="替代任务",
    )

    old = await manager.get_run(f"waiting-run-{waiting_status}")
    assert old.status == AGENT_RUN_CANCELLED
    assert old.paused_reason == "stop_replace"
    async with run_db() as db:
        old_pending = await db.get(SystemAgentPendingTurn, pending.id)
        new_pending = await db.get(SystemAgentPendingTurn, replacement.pending_turn_id)
        following = await db.get(SystemAgentPendingTurn, following_pending.id)
        assert old_pending is not None and old_pending.status == PENDING_TURN_CANCELLED
        assert new_pending is not None and new_pending.status in {
            PENDING_TURN_PENDING,
            "dispatching",
        }
        assert following is not None and following.status == PENDING_TURN_PENDING

    await _wait_for_status(manager, replacement.id, AGENT_RUN_RUNNING)
    service.release.set()
    await _wait_for_status(manager, replacement.id, AGENT_RUN_SUCCEEDED)
    await _wait_for_status(
        manager,
        f"waiting-following-run-{waiting_status}",
        AGENT_RUN_SUCCEEDED,
    )
    assert [item["text"] for item in service.kwargs] == [
        "替代任务",
        "原队列后续任务",
    ]


@pytest.mark.asyncio
async def test_manager_enforces_web_and_bot_session_ownership(run_db) -> None:
    manager = SystemAgentRunManager(session_factory=run_db, poll_interval=0.01)

    with pytest.raises(RunNotFoundError, match="session:session-1"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=8,
            client_request_id="request-wrong-web-owner",
            text="不应访问",
        )
    with pytest.raises(RunNotFoundError, match="session:bot-session-1"):
        await manager.start_run(
            session_id="bot-session-1",
            web_user_id=None,
            bot_tg_user_id=43,
            account_id=11,
            channel=CHANNEL_BOT,
            role="viewer",
            client_request_id="request-wrong-bot-user",
            text="不应访问",
        )
    with pytest.raises(RunNotFoundError, match="session:bot-session-1"):
        await manager.start_run(
            session_id="bot-session-1",
            web_user_id=None,
            bot_tg_user_id=42,
            account_id=12,
            channel=CHANNEL_BOT,
            role="viewer",
            client_request_id="request-wrong-bot-account",
            text="不应访问",
        )
    with pytest.raises(RunNotFoundError, match="session:bot-session-1"):
        await manager.start_run(
            session_id="bot-session-1",
            web_user_id=7,
            client_request_id="request-wrong-channel",
            text="不应访问",
        )


@pytest.mark.asyncio
async def test_bot_role_and_identity_are_restored_for_execution(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="bot-session-1",
        web_user_id=None,
        bot_tg_user_id=42,
        account_id=11,
        channel=CHANNEL_BOT,
        role="viewer",
        client_request_id="request-bot-viewer",
        text="只读查看状态",
        read_only_only=True,
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)
    service.release.set()
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)

    assert service.calls == 1
    assert service.kwargs[0]["role"] == "viewer"
    assert service.kwargs[0]["channel"] == CHANNEL_BOT
    assert service.kwargs[0]["bot_tg_user_id"] == 42
    assert service.kwargs[0]["web_user_id"] is None
    assert service.kwargs[0]["read_only_only"] is True


@pytest.mark.asyncio
async def test_idempotency_hash_includes_role_and_read_only_mode(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-identity-hash",
        text="查看状态",
        role="viewer",
        read_only_only=True,
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)

    with pytest.raises(RunConflictError, match="同一个请求标识"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=7,
            client_request_id="request-identity-hash",
            text="查看状态",
            role="admin",
            read_only_only=True,
        )
    with pytest.raises(RunConflictError, match="同一个请求标识"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=7,
            client_request_id="request-identity-hash",
            text="查看状态",
            role="viewer",
            read_only_only=False,
        )

    await manager.cancel_run(run.id)
    await _wait_for_status(manager, run.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_queue_limit_position_edit_reorder_delete_and_clear(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
        queue_limit=2,
    )
    active = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-queue-active",
        text="先执行",
    )
    await _wait_for_status(manager, active.id, AGENT_RUN_RUNNING)
    second = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-queue-second",
        text="第二条",
    )
    third = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-queue-third",
        text="第三条",
    )

    assert await manager.get_queue_position(second.id) == 1
    assert await manager.get_queue_position(third.id) == 2
    with pytest.raises(RunConflictError, match="最多排队 2 条"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=7,
            client_request_id="request-queue-overflow",
            text="超出队列",
        )

    queue = await manager.list_queue(web_user_id=7, session_id="session-1")
    by_run = {item["run_id"]: item for item in queue}
    updated = await manager.update_queue_item(
        by_run[third.id]["id"],
        web_user_id=7,
        content="第三条（已编辑）",
        pinned=True,
    )
    assert updated["content"] == "第三条（已编辑）"
    reordered = await manager.reorder_queue(
        session_id="session-1",
        web_user_id=7,
        turn_ids=[by_run[second.id]["id"], by_run[third.id]["id"]],
    )
    assert [item["run_id"] for item in reordered] == [second.id, third.id]

    deleted = await manager.delete_queue_item(
        by_run[second.id]["id"],
        web_user_id=7,
    )
    assert deleted.status == AGENT_RUN_CANCELLED
    assert await manager.clear_queue(session_id="session-1", web_user_id=7) == 1
    remaining = await manager.list_queue(web_user_id=7, session_id="session-1")
    assert [item["run_id"] for item in remaining] == [active.id]

    await manager.cancel_run(active.id)
    await _wait_for_status(manager, active.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_pending_turn_and_run_inputs_are_encrypted_and_steer_is_consumed_once(
    run_db,
) -> None:
    secret_text = "队列正文 secret-queue-value"
    steer_text = "调整方向 secret-steer-value"
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-encrypted-turn",
        text=secret_text,
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)
    first_input = await manager.add_run_input(
        run.id,
        kind=RUN_INPUT_STEER,
        client_request_id="request-steer-once",
        payload={"content": steer_text},
    )
    duplicate = await manager.add_run_input(
        run.id,
        kind=RUN_INPUT_STEER,
        client_request_id="request-steer-once",
        payload={"content": steer_text},
    )
    assert duplicate.id == first_input.id
    with pytest.raises(RunConflictError, match="不能提交不同"):
        await manager.add_run_input(
            run.id,
            kind=RUN_INPUT_STEER,
            client_request_id="request-steer-once",
            payload={"content": "不同输入"},
        )

    async with run_db() as db:
        pending = await db.get(SystemAgentPendingTurn, run.pending_turn_id)
        stored_input = await db.get(SystemAgentRunInput, first_input.id)
        assert pending is not None and stored_input is not None
        assert secret_text not in pending.content_enc
        assert steer_text not in stored_input.payload_enc
        assert decrypt_str(pending.content_enc) == secret_text
        assert json.loads(decrypt_str(stored_input.payload_enc)) == {
            "content": steer_text
        }

    assert await manager._consume_steers(run.id) == [steer_text]
    assert await manager._consume_steers(run.id) == []
    async with run_db() as db:
        pending = await db.get(SystemAgentPendingTurn, run.pending_turn_id)
        stored_input = await db.get(SystemAgentRunInput, first_input.id)
        assert pending is not None and stored_input is not None
        assert steer_text not in pending.content_enc
        assert decrypt_str(pending.content_enc) == (
            f"{secret_text}\n\n运行中调整：{steer_text}"
        )
        assert stored_input.status == RUN_INPUT_APPLIED
        assert stored_input.applied_at is not None

    await manager.cancel_run(run.id)
    await _wait_for_status(manager, run.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_waiting_input_can_resume_same_run_with_supplement(run_db) -> None:
    service = _WaitingService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-waiting-input",
        text="执行需要模型的任务",
        role="viewer",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_WAITING_INPUT)
    item = await manager.add_run_input(
        run.id,
        kind=RUN_INPUT_USER,
        client_request_id="request-supplement",
        payload={"content": "改用备用模型", "fallback_provider_id": 9},
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)

    assert item.status == RUN_INPUT_APPLIED
    assert service.calls == 2
    assert service.kwargs[1]["role"] == "viewer"
    assert service.kwargs[1]["fallback_provider_id"] == 9
    async with run_db() as db:
        pending = await db.get(SystemAgentPendingTurn, run.pending_turn_id)
        assert pending is not None
        assert "改用备用模型" in decrypt_str(pending.content_enc)


@pytest.mark.asyncio
async def test_waiting_input_resume_is_idempotent_after_run_requeues(run_db) -> None:
    service = _WaitingService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-waiting-idempotent",
        text="执行需要模型的任务",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_WAITING_INPUT)

    first = await manager.add_run_input(
        run.id,
        kind=RUN_INPUT_USER,
        client_request_id="request-waiting-idempotent-resume",
        payload={"content": "改用备用模型", "fallback_provider_id": 9},
    )
    duplicate = await manager.add_run_input(
        run.id,
        kind=RUN_INPUT_USER,
        client_request_id="request-waiting-idempotent-resume",
        payload={"content": "改用备用模型", "fallback_provider_id": 9},
    )

    assert duplicate.id == first.id
    assert duplicate.status == RUN_INPUT_APPLIED
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)


@pytest.mark.asyncio
async def test_waiting_run_rejects_input_kind_for_the_other_waiting_state(
    run_db,
) -> None:
    service = _WaitingService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-waiting-kind-guard",
        text="执行需要模型的任务",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_WAITING_INPUT)

    with pytest.raises(RunConflictError, match="没有等待补充输入或审批"):
        await manager.add_run_input(
            run.id,
            kind=RUN_INPUT_APPROVAL,
            client_request_id="request-wrong-waiting-kind",
            payload={"approved": False},
        )

    async with run_db() as db:
        count = len(
            list(
                (
                    await db.execute(
                        select(SystemAgentRunInput).where(
                            SystemAgentRunInput.run_id == run.id
                        )
                    )
                ).scalars()
            )
        )
        assert count == 0

    await manager.cancel_run(run.id)


@pytest.mark.asyncio
async def test_stale_worker_cannot_append_events_or_finish_after_claim_changes(
    run_db,
) -> None:
    manager = SystemAgentRunManager(
        session_factory=run_db,
        poll_interval=0.01,
        worker_id="stale-worker",
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-worker-fencing",
        text="验证 worker fencing",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)
    async with run_db() as db:
        stored = await db.get(SystemAgentRun, run.id, with_for_update=True)
        assert stored is not None
        stored.claimed_by = "new-worker"
        stored.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        steer = SystemAgentRunInput(
            run_id=run.id,
            kind=RUN_INPUT_STEER,
            payload_enc=encrypt_str(json.dumps({"content": "只给新 worker"})),
            client_request_id="request-steer-after-claim-transfer",
        )
        db.add(steer)
        await db.commit()

    with pytest.raises(_WorkerLeaseLost, match=run.id):
        await manager._append_event(run.id, {"type": "assistant_message", "content": "旧结果"})
    with pytest.raises(_WorkerLeaseLost, match=run.id):
        await manager._finish_run(
            run.id,
            status=AGENT_RUN_SUCCEEDED,
            error_code=None,
            error_message=None,
        )
    assert await manager._consume_steers(run.id) == []
    new_manager = SystemAgentRunManager(
        session_factory=run_db,
        poll_interval=0.01,
        worker_id="new-worker",
    )
    assert await new_manager._consume_steers(run.id) == ["只给新 worker"]
    async with run_db() as db:
        stored = await db.get(SystemAgentRun, run.id)
        events = list(
            (
                await db.execute(
                    select(SystemAgentRunEvent).where(
                        SystemAgentRunEvent.run_id == run.id
                    )
                )
            ).scalars()
        )
        assert stored is not None and stored.claimed_by == "new-worker"
        assert stored.status == AGENT_RUN_RUNNING
        assert all((event.event or {}).get("content") != "旧结果" for event in events)

    await manager.shutdown()


@pytest.mark.asyncio
async def test_recovery_scan_preserves_explicitly_paused_queue(run_db) -> None:
    pending = SystemAgentPendingTurn(
        id="paused-recovery-turn",
        session_id="session-1",
        web_user_id=7,
        channel=CHANNEL_WEB,
        kind="message",
        position=1,
        status=PENDING_TURN_PAUSED,
        blocked_reason=AGENT_RUN_FAILED,
        client_request_id="request-paused-recovery",
        request_hash="a" * 64,
        content_enc=encrypt_str("等待显式恢复"),
        request_payload={"role": "admin"},
        dispatch_run_id="paused-recovery-run",
    )
    async with run_db() as db:
        db.add(pending)
        await db.flush()
        db.add(
            SystemAgentRun(
                id="paused-recovery-run",
                session_id="session-1",
                web_user_id=7,
                channel=CHANNEL_WEB,
                pending_turn_id=pending.id,
                client_request_id="request-paused-recovery",
                request_hash="a" * 64,
                kind="message",
                status=AGENT_RUN_QUEUED,
                phase="paused",
                paused_reason=AGENT_RUN_FAILED,
            )
        )
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    await manager.ensure_ready()
    await manager._recover_expired_and_queued_runs()
    await asyncio.sleep(0.05)

    stored = await manager.get_run("paused-recovery-run")
    queue = await manager.list_queue(web_user_id=7, session_id="session-1")
    assert stored.status == AGENT_RUN_QUEUED
    assert stored.phase == "paused"
    assert queue[0]["status"] == PENDING_TURN_PAUSED
    assert service.calls == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_rejected_approval_cancels_run_and_keeps_following_queue_paused(
    run_db,
) -> None:
    service = _WaitingService(approval=True)
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-waiting-approval",
        text="执行写操作",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_WAITING_APPROVAL)
    following = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-after-approval",
        text="后续任务",
    )
    queue = await manager.list_queue(web_user_id=7, session_id="session-1")
    assert queue[0]["status"] == PENDING_TURN_PAUSED

    await manager.add_run_input(
        run.id,
        kind=RUN_INPUT_APPROVAL,
        client_request_id="request-reject-approval",
        payload={"approved": False},
    )
    cancelled = await _wait_for_status(manager, run.id, AGENT_RUN_CANCELLED)
    assert cancelled.error_code == "AGENT_TOOL_APPROVAL_REJECTED"
    assert (await manager.get_run(following.id)).status == AGENT_RUN_QUEUED
    queue = await manager.list_queue(web_user_id=7, session_id="session-1")
    assert queue[0]["status"] == PENDING_TURN_PAUSED

    assert await manager.resume_queue(session_id="session-1", web_user_id=7) == 1
    await _wait_for_status(manager, following.id, AGENT_RUN_SUCCEEDED)


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_and_preserves_viewer_role(run_db) -> None:
    now = datetime.now(UTC)
    pending = SystemAgentPendingTurn(
        id="expired-turn",
        session_id="session-1",
        web_user_id=7,
        channel=CHANNEL_WEB,
        kind="message",
        position=1,
        status="dispatching",
        client_request_id="request-expired-lease",
        request_hash="4" * 64,
        content_enc=encrypt_str("恢复任务"),
        request_payload={"role": "viewer", "read_only_only": True},
        dispatch_run_id="expired-run",
    )
    run = SystemAgentRun(
        id="expired-run",
        session_id="session-1",
        web_user_id=7,
        channel=CHANNEL_WEB,
        pending_turn_id="expired-turn",
        client_request_id="request-expired-lease",
        request_hash="4" * 64,
        kind="message",
        status=AGENT_RUN_RUNNING,
        phase="thinking",
        claimed_by="dead-worker",
        lease_expires_at=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(seconds=10),
        started_at=now - timedelta(seconds=20),
        created_at=now - timedelta(seconds=20),
    )
    async with run_db() as db:
        db.add(pending)
        await db.flush()
        db.add(run)
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
        worker_id="recovery-worker",
    )
    await manager.ensure_ready()
    recovered = await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)
    assert recovered.claimed_by == "recovery-worker"
    service.release.set()
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)
    assert service.kwargs[0]["role"] == "viewer"
    assert service.kwargs[0]["read_only_only"] is True


@pytest.mark.asyncio
async def test_runtime_recovery_loop_claims_lease_that_expires_after_ready(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
        recovery_seconds=0.02,
        worker_id="runtime-recovery-worker",
    )
    await manager.ensure_ready()
    now = datetime.now(UTC)
    pending = SystemAgentPendingTurn(
        id="runtime-expired-turn",
        session_id="session-1",
        web_user_id=7,
        channel=CHANNEL_WEB,
        kind="message",
        position=1,
        status="dispatching",
        client_request_id="request-runtime-expired-lease",
        request_hash="5" * 64,
        content_enc=encrypt_str("运行期恢复任务"),
        request_payload={"role": "viewer", "read_only_only": True},
        dispatch_run_id="runtime-expired-run",
    )
    run = SystemAgentRun(
        id="runtime-expired-run",
        session_id="session-1",
        web_user_id=7,
        channel=CHANNEL_WEB,
        pending_turn_id="runtime-expired-turn",
        client_request_id="request-runtime-expired-lease",
        request_hash="5" * 64,
        kind="message",
        status=AGENT_RUN_RUNNING,
        phase="thinking",
        claimed_by="worker-that-died-later",
        lease_expires_at=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(seconds=10),
        started_at=now - timedelta(seconds=20),
        created_at=now - timedelta(seconds=20),
    )
    async with run_db() as db:
        db.add(pending)
        await db.flush()
        db.add(run)
        await db.commit()

    try:
        recovered = None
        for _ in range(200):
            snapshot = await manager.get_run(run.id)
            if (
                snapshot.status == AGENT_RUN_RUNNING
                and snapshot.claimed_by == "runtime-recovery-worker"
            ):
                recovered = snapshot
                break
            await asyncio.sleep(0.01)
        assert recovered is not None
        service.release.set()
        await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_expired_stop_replace_cancels_old_run_and_only_dispatches_replacement(
    run_db,
) -> None:
    now = datetime.now(UTC)
    async with run_db() as db:
        old_turn = SystemAgentPendingTurn(
            id="expired-stop-old-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=1,
            status="dispatching",
            client_request_id="request-expired-stop-old",
            request_hash="6" * 64,
            content_enc=encrypt_str("不得再次执行的旧任务"),
            request_payload={"role": "admin"},
            dispatch_run_id="expired-stop-old-run",
        )
        replacement_turn = SystemAgentPendingTurn(
            id="expired-stop-replacement-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=0,
            status=PENDING_TURN_PENDING,
            client_request_id="request-expired-stop-replacement",
            request_hash="7" * 64,
            content_enc=encrypt_str("只执行替代任务"),
            request_payload={"role": "admin"},
            dispatch_run_id="expired-stop-replacement-run",
        )
        db.add_all([old_turn, replacement_turn])
        await db.flush()
        db.add_all(
            [
                SystemAgentRun(
                    id="expired-stop-old-run",
                    session_id="session-1",
                    web_user_id=7,
                    channel=CHANNEL_WEB,
                    pending_turn_id=old_turn.id,
                    client_request_id="request-expired-stop-old",
                    request_hash="6" * 64,
                    kind="message",
                    status=AGENT_RUN_RUNNING,
                    phase="thinking",
                    cancel_requested=True,
                    paused_reason="stop_replace",
                    claimed_by="dead-worker",
                    lease_expires_at=now - timedelta(seconds=1),
                    heartbeat_at=now - timedelta(seconds=10),
                ),
                SystemAgentRun(
                    id="expired-stop-replacement-run",
                    session_id="session-1",
                    web_user_id=7,
                    channel=CHANNEL_WEB,
                    pending_turn_id=replacement_turn.id,
                    client_request_id="request-expired-stop-replacement",
                    request_hash="7" * 64,
                    kind="message",
                    status=AGENT_RUN_QUEUED,
                    phase="queued",
                ),
            ]
        )
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
        worker_id="replacement-recovery-worker",
    )
    await manager.ensure_ready()

    old = await manager.get_run("expired-stop-old-run")
    replacement = await _wait_for_status(
        manager,
        "expired-stop-replacement-run",
        AGENT_RUN_RUNNING,
    )
    assert old.status == AGENT_RUN_CANCELLED
    assert old.paused_reason == "stop_replace"
    assert service.calls == 1
    assert service.kwargs[0]["text"] == "只执行替代任务"

    await manager.cancel_run(replacement.id)
    await _wait_for_status(manager, replacement.id, AGENT_RUN_CANCELLED)
    assert service.calls == 1


@pytest.mark.asyncio
async def test_expired_plain_cancel_pauses_following_queue(run_db) -> None:
    now = datetime.now(UTC)
    async with run_db() as db:
        old_turn = SystemAgentPendingTurn(
            id="expired-cancel-old-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=1,
            status="dispatching",
            client_request_id="request-expired-cancel-old",
            request_hash="8" * 64,
            content_enc=encrypt_str("已取消任务"),
            request_payload={"role": "admin"},
            dispatch_run_id="expired-cancel-old-run",
        )
        following_turn = SystemAgentPendingTurn(
            id="expired-cancel-following-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=2,
            status=PENDING_TURN_PENDING,
            client_request_id="request-expired-cancel-following",
            request_hash="9" * 64,
            content_enc=encrypt_str("等待人工恢复"),
            request_payload={"role": "admin"},
            dispatch_run_id="expired-cancel-following-run",
        )
        db.add_all([old_turn, following_turn])
        await db.flush()
        db.add_all(
            [
                SystemAgentRun(
                    id="expired-cancel-old-run",
                    session_id="session-1",
                    web_user_id=7,
                    channel=CHANNEL_WEB,
                    pending_turn_id=old_turn.id,
                    client_request_id="request-expired-cancel-old",
                    request_hash="8" * 64,
                    kind="message",
                    status=AGENT_RUN_RUNNING,
                    phase="thinking",
                    cancel_requested=True,
                    claimed_by="dead-worker",
                    lease_expires_at=now - timedelta(seconds=1),
                ),
                SystemAgentRun(
                    id="expired-cancel-following-run",
                    session_id="session-1",
                    web_user_id=7,
                    channel=CHANNEL_WEB,
                    pending_turn_id=following_turn.id,
                    client_request_id="request-expired-cancel-following",
                    request_hash="9" * 64,
                    kind="message",
                    status=AGENT_RUN_QUEUED,
                    phase="queued",
                ),
            ]
        )
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    await manager.ensure_ready()
    await asyncio.sleep(0.05)

    old = await manager.get_run("expired-cancel-old-run")
    following = await manager.get_run("expired-cancel-following-run")
    queue = await manager.list_queue(web_user_id=7, session_id="session-1")
    assert old.status == AGENT_RUN_CANCELLED
    assert following.status == AGENT_RUN_QUEUED
    assert following.phase == "paused"
    assert queue[0]["status"] == PENDING_TURN_PAUSED
    assert service.calls == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_requeues_owned_run_for_next_process(run_db) -> None:
    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
        worker_id="shutdown-worker",
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-shutdown-recovery",
        text="退出后继续",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)
    for _ in range(100):
        run = await manager.get_run(run.id)
        if run.user_message_id is not None:
            break
        await asyncio.sleep(0.01)
    assert run.user_message_id is not None

    await manager.shutdown()

    async with run_db() as db:
        stored = await db.get(SystemAgentRun, run.id)
        pending = await db.get(SystemAgentPendingTurn, run.pending_turn_id)
        assert stored is not None and stored.status == AGENT_RUN_QUEUED
        assert stored.claimed_by is None
        assert stored.lease_expires_at is None
        assert pending is not None and pending.status == PENDING_TURN_PENDING

    recovery_service = _ControlledService()
    recovery = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: recovery_service,
        poll_interval=0.01,
        worker_id="next-worker",
    )
    await recovery.ensure_ready()
    recovered = await _wait_for_status(recovery, run.id, AGENT_RUN_RUNNING)
    assert recovered.claimed_by == "next-worker"
    for _ in range(100):
        if recovery_service.kwargs:
            break
        await asyncio.sleep(0.01)
    assert recovery_service.kwargs
    assert recovery_service.kwargs[0]["retry_message"].id == run.user_message_id
    async with run_db() as db:
        messages = list(
            (
                await db.execute(
                    select(SystemAgentMessage).where(
                        SystemAgentMessage.session_id == run.session_id,
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                    )
                )
            ).scalars()
        )
        assert len(messages) == 1
    await recovery.cancel_run(run.id)
    await _wait_for_status(recovery, run.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_regenerate_run_reuses_latest_message_pair(run_db) -> None:
    async with run_db() as db:
        user_message = SystemAgentMessage(
            session_id="session-1",
            role=MESSAGE_ROLE_USER,
            content={"text": "原问题"},
            run_status=MESSAGE_RUN_SUCCEEDED,
        )
        assistant_message = SystemAgentMessage(
            session_id="session-1",
            role=MESSAGE_ROLE_ASSISTANT,
            content={"text": "原回答"},
            run_status=MESSAGE_RUN_COMPLETED,
        )
        db.add_all([user_message, assistant_message])
        await db.commit()
        await db.refresh(user_message)
        await db.refresh(assistant_message)
        user_message_id = user_message.id
        assistant_message_id = assistant_message.id

    service = _ControlledService(response="新回答")
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-regenerate",
        text="编辑后的问题",
        regenerate_message_id=user_message_id,
        regenerate_assistant_message_id=assistant_message_id,
    )
    assert run.kind == "regenerate"
    assert run.user_message_id == user_message_id

    service.release.set()
    await _wait_for_status(manager, run.id, AGENT_RUN_SUCCEEDED)
    async with run_db() as db:
        rows = list(
            (
                await db.execute(
                    select(SystemAgentMessage).order_by(SystemAgentMessage.id)
                )
            )
            .scalars()
            .all()
        )

    assert [message.id for message in rows] == [user_message_id, assistant_message_id]
    assert rows[1].content["text"] == "新回答"


@pytest.mark.asyncio
async def test_regenerate_rejects_an_older_pair(run_db) -> None:
    async with run_db() as db:
        messages = [
            SystemAgentMessage(
                session_id="session-1",
                role=role,
                content={"text": text_value},
                run_status=status,
            )
            for role, text_value, status in [
                (MESSAGE_ROLE_USER, "旧问题", MESSAGE_RUN_SUCCEEDED),
                (MESSAGE_ROLE_ASSISTANT, "旧回答", MESSAGE_RUN_COMPLETED),
                (MESSAGE_ROLE_USER, "新问题", MESSAGE_RUN_SUCCEEDED),
                (MESSAGE_ROLE_ASSISTANT, "新回答", MESSAGE_RUN_COMPLETED),
            ]
        ]
        db.add_all(messages)
        await db.commit()
        for message in messages:
            await db.refresh(message)

    manager = SystemAgentRunManager(session_factory=run_db, poll_interval=0.01)
    with pytest.raises(RunConflictError, match="最新完成的一轮"):
        await manager.start_run(
            session_id="session-1",
            web_user_id=7,
            client_request_id="request-regenerate-old",
            text="",
            account_id=3,
            regenerate_message_id=messages[0].id,
            regenerate_assistant_message_id=messages[1].id,
        )

    async with run_db() as db:
        session = await db.get(SystemAgentSession, "session-1")
        assert session is not None
        assert session.account_id is None


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
async def test_persisted_run_usage_redacts_nested_credentials(run_db) -> None:
    manager = SystemAgentRunManager(
        session_factory=run_db,
        poll_interval=0.01,
        worker_id="usage-redaction-worker",
    )
    run = await manager.start_run(
        session_id="session-1",
        web_user_id=7,
        client_request_id="request-usage-redaction",
        text="检查用量脱敏",
    )
    await _wait_for_status(manager, run.id, AGENT_RUN_RUNNING)
    await manager._update_usage(
        run.id,
        {
            "request": {
                "authorization": "Bearer abcdefghijklmnop",
                "proxy": "socks5://user:pass@127.0.0.1:1080",
            },
            "provider": {
                "url": "https://api.telegram.org/bot123456789:abcdefghijklmnopqrstuvwxyz/sendMessage",
                "has_api_key": True,
            },
        },
    )

    stored = await manager.get_run(run.id)
    serialized = json.dumps(stored.usage, ensure_ascii=False)
    for secret in (
        "abcdefghijklmnop",
        "user",
        "pass",
        "123456789:abcdefghijklmnopqrstuvwxyz",
    ):
        assert secret not in serialized
    assert stored.usage["provider"]["has_api_key"] is True
    assert "***" in serialized

    await manager.cancel_run(run.id)
    await _wait_for_status(manager, run.id, AGENT_RUN_CANCELLED)


@pytest.mark.asyncio
async def test_lazy_reconcile_keeps_queued_runs_recoverable(run_db) -> None:
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

    assert run.status == AGENT_RUN_QUEUED
    assert run.error_code is None
    assert events == []


@pytest.mark.asyncio
async def test_reconcile_converges_run_with_persisted_success_event(run_db) -> None:
    now = datetime.now(UTC)
    async with run_db() as db:
        pending = SystemAgentPendingTurn(
            id="completed-orphan-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=1,
            status="dispatching",
            client_request_id="request-completed-orphan",
            request_hash="1" * 64,
            content_enc=encrypt_str("已经完成"),
            request_payload={"role": "viewer"},
            dispatch_run_id="completed-orphan-run",
        )
        db.add(pending)
        db.add(
            SystemAgentRun(
                id="completed-orphan-run",
                session_id="session-1",
                web_user_id=7,
                pending_turn_id=pending.id,
                client_request_id="request-completed-orphan",
                request_hash="1" * 64,
                kind="message",
                status=AGENT_RUN_RUNNING,
                last_seq=1,
                claimed_by="dead-worker",
                lease_expires_at=now - timedelta(seconds=1),
                created_at=now,
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

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    await manager.ensure_ready()
    run = await manager.get_run("completed-orphan-run")
    events = await manager.list_events("completed-orphan-run")

    assert run.status == AGENT_RUN_SUCCEEDED
    assert run.error_code is None
    assert run.finished_at is not None
    assert len(events) == 1
    assert service.calls == 0
    async with run_db() as db:
        pending = await db.get(SystemAgentPendingTurn, "completed-orphan-turn")
        assert pending is not None
        assert pending.status == "dispatched"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_reconcile_converges_run_with_committed_message_result(run_db) -> None:
    now = datetime.now(UTC)
    async with run_db() as db:
        pending = SystemAgentPendingTurn(
            id="committed-result-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=1,
            status="dispatching",
            client_request_id="request-committed-result",
            request_hash="2" * 64,
            content_enc=encrypt_str("已经提交业务结果"),
            request_payload={"role": "viewer", "after_message_id": 0},
            dispatch_run_id="committed-result-run",
        )
        db.add(pending)
        user_message = SystemAgentMessage(
            session_id="session-1",
            role=MESSAGE_ROLE_USER,
            content={"text": "已经提交业务结果"},
            run_status=MESSAGE_RUN_SUCCEEDED,
        )
        db.add(user_message)
        await db.flush()
        db.add(
            SystemAgentMessage(
                session_id="session-1",
                role=MESSAGE_ROLE_ASSISTANT,
                content={"text": "已完成"},
                usage={"run_id": "committed-result-run"},
                run_status=MESSAGE_RUN_COMPLETED,
            )
        )
        db.add(
            SystemAgentRun(
                id="committed-result-run",
                session_id="session-1",
                web_user_id=7,
                pending_turn_id=pending.id,
                user_message_id=user_message.id,
                client_request_id="request-committed-result",
                request_hash="2" * 64,
                kind="message",
                status=AGENT_RUN_RUNNING,
                claimed_by="dead-worker",
                lease_expires_at=now - timedelta(seconds=1),
                created_at=now,
            )
        )
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
    )
    await manager.ensure_ready()
    run = await manager.get_run("committed-result-run")

    assert run.status == AGENT_RUN_SUCCEEDED
    assert service.calls == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_reconcile_links_untracked_user_message_and_retries_in_place(run_db) -> None:
    now = datetime.now(UTC)
    async with run_db() as db:
        pending = SystemAgentPendingTurn(
            id="unlinked-message-turn",
            session_id="session-1",
            web_user_id=7,
            channel=CHANNEL_WEB,
            kind="message",
            position=1,
            status="dispatching",
            client_request_id="request-unlinked-message",
            request_hash="3" * 64,
            content_enc=encrypt_str("已落库但未发事件"),
            request_payload={"role": "viewer", "after_message_id": 0},
            dispatch_run_id="unlinked-message-run",
        )
        db.add(pending)
        user_message = SystemAgentMessage(
            session_id="session-1",
            role=MESSAGE_ROLE_USER,
            content={"text": "已落库但未发事件"},
            run_status=MESSAGE_RUN_PENDING,
        )
        db.add(user_message)
        await db.flush()
        user_message_id = user_message.id
        db.add(
            SystemAgentRun(
                id="unlinked-message-run",
                session_id="session-1",
                web_user_id=7,
                pending_turn_id=pending.id,
                client_request_id="request-unlinked-message",
                request_hash="3" * 64,
                kind="message",
                status=AGENT_RUN_RUNNING,
                claimed_by="dead-worker",
                lease_expires_at=now - timedelta(seconds=1),
                created_at=now,
            )
        )
        await db.commit()

    service = _ControlledService()
    manager = SystemAgentRunManager(
        session_factory=run_db,
        service_factory=lambda: service,
        poll_interval=0.01,
        worker_id="recovery-worker",
    )
    await manager.ensure_ready()
    recovered = await _wait_for_status(
        manager,
        "unlinked-message-run",
        AGENT_RUN_RUNNING,
    )

    assert recovered.user_message_id == user_message_id
    assert service.kwargs[0]["retry_message"].id == user_message_id
    async with run_db() as db:
        messages = list(
            (
                await db.execute(
                    select(SystemAgentMessage).where(
                        SystemAgentMessage.session_id == "session-1",
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                    )
                )
            ).scalars()
        )
        assert len(messages) == 1
    await manager.cancel_run(recovered.id)
    await _wait_for_status(manager, recovered.id, AGENT_RUN_CANCELLED)


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
