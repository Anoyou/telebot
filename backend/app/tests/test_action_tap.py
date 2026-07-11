from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.account import Account  # noqa: F401 - registers FK target table metadata
from app.db.models.action_event import (
    ACTION_EVENT_STATUS_DRY_RUN,
    ACTION_EVENT_STATUS_FAILED,
    ACTION_EVENT_STATUS_OK,
    ActionEvent,
)
from app.services import action_tap
from app.services.interaction import delivery as delivery_mod
from app.services.interaction.delivery import InteractionDeliveryExecutor
from app.worker.plugins import loader as loader_mod
from app.worker.plugins.base import Plugin, PluginContext


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


@pytest.fixture
async def action_event_session_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE account (id BIGINT PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO account (id) VALUES (7)"))
        await conn.run_sync(ActionEvent.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(action_tap, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(action_tap, "_DB_DISABLED_UNTIL", 0.0)
    monkeypatch.setattr(action_tap, "_REDIS_DISABLED_UNTIL", 0.0)
    monkeypatch.setattr(action_tap, "_DB_WRITE_FAILURES", 0)
    monkeypatch.setattr(action_tap, "_DB_DROPPED_EVENTS", 0)
    monkeypatch.setattr(action_tap, "_DB_LAST_ERROR", None)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_action_event_persistence_failure_is_observable(monkeypatch, caplog) -> None:
    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(action_tap, "AsyncSessionLocal", _BrokenSession)
    monkeypatch.setattr(action_tap, "_DB_DISABLED_UNTIL", 0.0)
    monkeypatch.setattr(action_tap, "_DB_WRITE_FAILURES", 0)
    monkeypatch.setattr(action_tap, "_DB_DROPPED_EVENTS", 0)
    monkeypatch.setattr(action_tap, "_DB_LAST_ERROR", None)

    with caplog.at_level("ERROR"):
        persisted = await action_tap.emit_action_event(
            account_id=7,
            action={"type": "payout", "amount": 10},
            status=ACTION_EVENT_STATUS_OK,
            redis=_FakeRedis(),
        )

    assert persisted is None
    health = action_tap.action_tap_health()
    assert health["db_available"] is False
    assert health["db_write_failures"] == 1
    assert health["db_dropped_events"] == 1
    assert "database unavailable" in str(health["db_last_error"])
    assert "结构化资金视图已降级" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (ACTION_EVENT_STATUS_OK, None),
        (ACTION_EVENT_STATUS_FAILED, "telegram_api_error"),
        (ACTION_EVENT_STATUS_DRY_RUN, None),
    ],
)
async def test_emit_action_event_persists_status_and_publishes(
    action_event_session_factory,
    status: str,
    error_code: str | None,
) -> None:
    redis = _FakeRedis()
    action = {
        "type": "payout",
        "send_via": "userbot_reply",
        "chat_id": -100123,
        "chat_title": "测试群",
        "amount": Decimal("12.50"),
        "receiver_user_id": 9001,
        "receiver_name": "收款人",
        "receiver_username": "receiver",
        "text": "+12.50",
        "context": {"plugin_key": "game24", "entry_key": "start", "session_key": "sess_1"},
    }

    await action_tap.emit_action_event(
        account_id=7,
        action=action,
        status=status,
        channel="userbot_reply",
        error_code=error_code,
        error="boom" if error_code else None,
        result={"message_id": 99, "chat_id": -100123},
        redis=redis,
    )

    async with action_event_session_factory() as db:
        rows = (await db.execute(select(ActionEvent))).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.account_id == 7
    assert row.channel == "userbot_reply"
    assert row.session_key == "sess_1"
    assert row.plugin_key == "game24"
    assert row.entry_key == "start"
    assert row.action_type == "payout"
    assert row.status == status
    assert row.error_code == error_code
    assert row.params_summary["amount"] == "12.50"
    assert row.params_summary["chat_title"] == "测试群"
    assert row.params_summary["receiver_user_id"] == 9001
    assert row.params_summary["receiver_name"] == "收款人"
    assert row.params_summary["receiver_username"] == "receiver"
    assert redis.published
    channel, payload = redis.published[-1]
    assert channel == "worker_event:7"
    event = json.loads(payload)
    assert event["type"] == action_tap.ACTION_TAP_EVENT_TYPE
    assert event["payload"]["status"] == status


@pytest.mark.asyncio
async def test_delivery_payout_dry_run_does_not_publish_worker_ipc(monkeypatch) -> None:
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 1}))
    record_action = AsyncMock()
    emit_action_event = AsyncMock()
    monkeypatch.setattr(delivery_mod, "record_action", record_action)
    monkeypatch.setattr(delivery_mod, "emit_action_event", emit_action_event)
    incoming = SimpleNamespace(
        account_id=7,
        chat_id=-100123,
        callback_id=None,
        callback_already_acked=False,
        token="token",
        trace_id="evt_dry",
        user_id=42,
        display_name="中奖用户",
        username="winner",
        reply_to_user_id=None,
        reply_to_display_name=None,
        reply_to_username=None,
        native_raw={"message": {"chat": {"id": -100123, "title": "测试群"}}},
    )
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=lambda _incoming: {},
        trace_context=lambda _context: {},
    )

    await executor.apply(
        [
            {
                "type": "payout",
                "amount": 88,
                "chat_id": -100123,
                "context": {
                    "trace_id": "evt_dry",
                    "plugin_key": "game24",
                    "entry_key": "start",
                    "dev_mode": {"dry_run": True},
                },
            }
        ]
    )

    run_worker_action.assert_not_awaited()
    record_action.assert_awaited_once()
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"
    assert record_action.await_args.kwargs["result"]["dry_run"] is True
    emit_action_event.assert_awaited_once()
    assert emit_action_event.await_args.kwargs["status"] == ACTION_EVENT_STATUS_DRY_RUN
    tapped_action = emit_action_event.await_args.kwargs["action"]
    assert tapped_action["chat_title"] == "测试群"
    assert tapped_action["receiver_user_id"] == 42
    assert tapped_action["receiver_name"] == "中奖用户"


def _stage(trace: dict[str, Any], name: str) -> dict[str, Any]:
    for item in trace["stages"]:
        if item["stage"] == name:
            return item
    raise AssertionError(f"stage missing: {name}")


def _state_with_plugin(plugin_key: str, inst: Plugin, *, account_config: dict[str, Any] | None = None) -> Any:
    state = loader_mod._AccountState(account_id=7)
    state.instances[plugin_key] = inst
    state.contexts[plugin_key] = PluginContext(
        account_id=7,
        feature_key=plugin_key,
        config={},
        account_config=dict(account_config or {}),
        generation=state.generation,
    )
    return state


def test_evaluate_dispatch_prefix_command_match(monkeypatch) -> None:
    async def _handler(*_args, **_kwargs):
        return None

    class _CommandPlugin(Plugin):
        key = "_eval_cmd"
        display_name = "eval command"
        owner_only = False
        commands = {"go": _handler}

    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")
    state = _state_with_plugin("_eval_cmd", _CommandPlugin())

    trace = loader_mod.evaluate_dispatch(
        state=state,
        chat={"chat_id": -100123, "sender_id": 42},
        text=",go 100",
        via="userbot",
        direction="outgoing",
    )

    command_stage = _stage(trace, "prefix_command")
    assert command_stage["matched"] is True
    assert command_stage["reason_code"] == "command_matched"
    assert command_stage["matches"][0]["plugin_key"] == "_eval_cmd"


def test_evaluate_dispatch_keyword_match() -> None:
    state = loader_mod._AccountState(account_id=7)
    state.interaction_text_guard_rules = (
        loader_mod._InteractionTextGuardRule(chat_ids=frozenset({-100123}), texts=frozenset({"开局"})),
    )

    trace = loader_mod.evaluate_dispatch(
        state=state,
        chat={"chat_id": -100123, "sender_id": 42},
        text="开局",
        via="userbot",
        direction="incoming",
    )

    keyword_stage = _stage(trace, "keyword")
    assert keyword_stage["matched"] is True
    assert keyword_stage["reason_code"] == "matched"
    assert keyword_stage["matches"][0]["reason_code"] == "interaction_rule_owned"


def test_evaluate_dispatch_no_match(monkeypatch) -> None:
    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")
    state = loader_mod._AccountState(account_id=7)

    trace = loader_mod.evaluate_dispatch(
        state=state,
        chat={"chat_id": -100123, "sender_id": 42},
        text="nothing",
        via="userbot",
        direction="incoming",
    )

    assert _stage(trace, "direct_passthrough")["matched"] is False
    assert _stage(trace, "prefix_command")["matched"] is False
    assert _stage(trace, "keyword")["matched"] is False
    assert _stage(trace, "event_subscription")["matched"] is False
