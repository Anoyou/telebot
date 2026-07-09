"""payout 失败补偿阶段 1 测试：只验证入队，不触发重放。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.account import Account  # noqa: F401 - registers FK target table metadata
from app.db.models.payout_compensation import PayoutCompensation
from app.services import account_bot_runtime
from app.services import payout_compensation as payout_compensation_service
from app.services.interaction.delivery import InteractionDeliveryExecutor
from app.worker.plugins import loader as loader_mod


class _FakeRedis:
    def __init__(self) -> None:
        self.list_pushes: list[tuple[str, str]] = []
        self.values: dict[str, str] = {}

    async def rpush(self, key: str, value: str) -> int:
        self.list_pushes.append((key, value))
        return len(self.list_pushes)

    async def get(self, key: str, *_args, **_kwargs):
        return self.values.get(str(key))

    async def set(self, key: str, value: str, **_kwargs):
        self.values[str(key)] = value
        return True


@pytest.mark.parametrize(
    ("error_code", "error", "should_enqueue", "normalized"),
    [
        ("userbot_offline", "userbot client unavailable", True, "userbot_offline"),
        ("telegram_api_error", "ConnectionError: network down", True, "telegram_api_error"),
        ("rate_limited", "bucket full", True, "rate_limited"),
        ("telegram_api_error", "RuntimeError: rate_limited: bucket full", True, "rate_limited"),
        ("invalid_payout_amount", "bad amount", False, "invalid_payout_amount"),
        ("empty_message_text", "empty", False, "empty_message_text"),
        ("scope_not_matched", "missing chat_id", False, "scope_not_matched"),
        ("action_limit_exceeded", "too many actions", False, "action_limit_exceeded"),
        ("reply_anchor_missing", "找不到用户近期消息", False, "reply_anchor_missing"),
        ("payout_limit_exceeded", "payout 单笔上限超限", False, "payout_limit_exceeded"),
    ],
)
def test_payout_error_classification_matrix(error_code, error, should_enqueue, normalized) -> None:
    classification = payout_compensation_service.classify_payout_error(error_code, error)

    assert classification.error_code == normalized
    assert classification.should_enqueue is should_enqueue
    assert classification.retryable is should_enqueue


def test_payout_error_classification_marks_ambiguous_only_for_timeout_or_network() -> None:
    timeout = payout_compensation_service.classify_payout_error("telegram_api_error", "worker 调用超时")
    flood_wait = payout_compensation_service.classify_payout_error("telegram_api_error", "FloodWaitError: wait 42")

    assert timeout.ambiguous is True
    assert flood_wait.ambiguous is False


@pytest.fixture
async def payout_session_factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE account (id BIGINT PRIMARY KEY)"))
        await conn.run_sync(PayoutCompensation.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(payout_compensation_service, "AsyncSessionLocal", session_factory)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_payout_compensation_is_idempotent_by_payout_key(payout_session_factory) -> None:
    payload = {
        "action_type": "payout",
        "payout_key": "pay_same_key",
        "chat_id": -100123,
        "amount": 88,
        "text": "+88",
        "context": {"trace_id": "evt_same", "plugin_key": "game", "entry_key": "main"},
    }

    first = await payout_compensation_service.enqueue_payout_compensation(
        account_id=7,
        origin="delivery",
        payload=payload,
        error_code="userbot_offline",
        error="userbot client unavailable",
    )
    second = await payout_compensation_service.enqueue_payout_compensation(
        account_id=7,
        origin="delivery",
        payload=payload,
        error_code="telegram_api_error",
        error="ConnectionError: network down",
    )

    assert first is not None
    assert second is not None
    assert first.id == second.id
    async with payout_session_factory() as db:
        rows = (await db.execute(select(PayoutCompensation))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.payout_key == "pay_same_key"
    assert row.trace_id == "evt_same"
    assert row.plugin_key == "game"
    assert row.entry_key == "main"
    assert row.error_code_first == "userbot_offline"
    assert row.error_code_last == "telegram_api_error"
    assert row.ambiguous is True
    assert row.payload["payout_key"] == "pay_same_key"


@pytest.mark.asyncio
async def test_delivery_failed_payout_enqueues_and_records_detail(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_payout_fail",
    )
    run_worker_action = AsyncMock(return_value=(False, "账号 worker 不在线", {}))
    record_action = AsyncMock()
    enqueue = AsyncMock(return_value=SimpleNamespace(id=1))
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    monkeypatch.setattr(payout_compensation_service, "enqueue_payout_compensation", enqueue)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply(
        [
            {
                "type": "payout",
                "amount": 66,
                "reply_to_message_id": 31,
                "context": {"trace_id": "evt_payout_fail", "plugin_key": "game", "entry_key": "main"},
            }
        ]
    )

    enqueue.assert_awaited_once()
    enqueue_kwargs = enqueue.await_args.kwargs
    assert enqueue_kwargs["origin"] == "delivery"
    assert enqueue_kwargs["error_code"] == "userbot_offline"
    assert enqueue_kwargs["trace_id"] == "evt_payout_fail"
    assert enqueue_kwargs["plugin_key"] == "game"
    assert enqueue_kwargs["entry_key"] == "main"
    assert enqueue_kwargs["payload"]["payout_key"].startswith("pay_")
    assert "payout_key" not in run_worker_action.await_args.kwargs["payload"]
    assert record_action.await_args.kwargs["compensation_queued"] is True
    assert record_action.await_args.kwargs["payout_key"] == enqueue_kwargs["payload"]["payout_key"]
    assert record_action.await_args.kwargs["result"]["compensation_queued"] is True
    assert record_action.await_args.kwargs["result"]["payout_key"] == enqueue_kwargs["payload"]["payout_key"]


@pytest.mark.asyncio
async def test_loader_payout_retryable_failures_enqueue(monkeypatch) -> None:
    enqueue = AsyncMock(return_value=SimpleNamespace(id=1))
    monkeypatch.setattr(loader_mod.payout_compensation, "enqueue_payout_compensation", enqueue)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    offline_state = loader_mod._AccountState(account_id=71)
    offline_state.redis = _FakeRedis()
    offline_state.client = None
    assert (
        await loader_mod._apply_userbot_payout_action(
            offline_state,
            SimpleNamespace(chat_id=-100777),
            {"type": "payout", "amount": 5, "chat_id": -100777, "context": {"trace_id": "evt_offline"}},
        )
        is False
    )
    assert enqueue.await_args.kwargs["origin"] == "worker"
    assert enqueue.await_args.kwargs["error_code"] == "userbot_offline"
    assert enqueue.await_args.kwargs["payload"]["payout_key"].startswith("pay_")

    enqueue.reset_mock()
    api_state = loader_mod._AccountState(account_id=72)
    api_state.redis = _FakeRedis()
    api_state.client = MagicMock()
    api_state.client.send_message = AsyncMock(side_effect=ConnectionError("network down"))
    api_state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(True, None)))
    assert (
        await loader_mod._apply_userbot_payout_action(
            api_state,
            SimpleNamespace(chat_id=-100888),
            {"type": "payout", "amount": 6, "chat_id": -100888, "context": {"trace_id": "evt_api"}},
        )
        is False
    )
    assert enqueue.await_args.kwargs["error_code"] == "telegram_api_error"
    assert enqueue.await_args.kwargs["payload"]["text"] == "+6"

    enqueue.reset_mock()
    limited_state = loader_mod._AccountState(account_id=73)
    limited_state.redis = _FakeRedis()
    limited_state.client = MagicMock()
    limited_state.client.send_message = AsyncMock()
    limited_state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=False, wait_seconds=12, outcome="rate_limited"))
    )
    assert (
        await loader_mod._apply_userbot_payout_action(
            limited_state,
            SimpleNamespace(chat_id=-100999),
            {"type": "payout", "amount": 7, "chat_id": -100999, "context": {"trace_id": "evt_limit"}},
        )
        is False
    )
    assert enqueue.await_args.kwargs["error_code"] == "rate_limited"
    limited_state.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_loader_payout_contract_and_payout_limit_failures_do_not_enqueue(monkeypatch) -> None:
    enqueue = AsyncMock(return_value=SimpleNamespace(id=1))
    monkeypatch.setattr(loader_mod.payout_compensation, "enqueue_payout_compensation", enqueue)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    invalid_state = loader_mod._AccountState(account_id=81)
    invalid_state.redis = _FakeRedis()
    invalid_state.client = MagicMock()
    assert (
        await loader_mod._apply_userbot_payout_action(
            invalid_state,
            SimpleNamespace(chat_id=-100111),
            {"type": "payout", "amount": 0, "chat_id": -100111, "context": {"trace_id": "evt_invalid"}},
        )
        is False
    )

    limit_state = loader_mod._AccountState(account_id=82)
    limit_state.redis = _FakeRedis()
    limit_state.client = MagicMock()
    limit_state.client.send_message = AsyncMock()
    limit_state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(False, "payout 单笔上限超限")))
    assert (
        await loader_mod._apply_userbot_payout_action(
            limit_state,
            SimpleNamespace(chat_id=-100222),
            {"type": "payout", "amount": 1000, "chat_id": -100222, "context": {"trace_id": "evt_payout_limit"}},
        )
        is False
    )

    enqueue.assert_not_awaited()
