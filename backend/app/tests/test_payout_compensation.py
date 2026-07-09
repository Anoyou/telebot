"""payout 失败补偿阶段 1 测试：只验证入队，不触发重放。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.account import Account  # noqa: F401 - registers FK target table metadata
from app.db.models.payout_compensation import (
    PAYOUT_COMPENSATION_STATUS_ABANDONED,
    PAYOUT_COMPENSATION_STATUS_PENDING,
    PAYOUT_COMPENSATION_STATUS_SENT,
    PayoutCompensation,
)
from app.services import account_bot_runtime
from app.services import payout_compensation as payout_compensation_service
from app.services.interaction.delivery import InteractionDeliveryExecutor
from app.worker import runtime as worker_runtime
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
        if _kwargs.get("nx") and str(key) in self.values:
            return False
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
    monkeypatch.setattr(worker_runtime, "AsyncSessionLocal", session_factory)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _insert_compensation_row(session_factory, **overrides) -> PayoutCompensation:
    now = overrides.pop("now", datetime.now(UTC))
    payout_key = overrides.pop("payout_key", "pay_row")
    chat_id = overrides.pop("chat_id", -100123)
    amount = overrides.pop("amount", 10)
    text = overrides.pop("text", "+10")
    trace_id = overrides.pop("trace_id", "evt_row")
    payload = {
        "action_type": "payout",
        "payout_key": payout_key,
        "chat_id": chat_id,
        "amount": amount,
        "text": text,
        "context": {"trace_id": trace_id, "plugin_key": "game", "entry_key": "main"},
    }
    payload.update(overrides.pop("payload", {}))
    row = PayoutCompensation(
        payout_key=payload["payout_key"],
        account_id=overrides.pop("account_id", 7),
        trace_id=trace_id,
        plugin_key=overrides.pop("plugin_key", "game"),
        entry_key=overrides.pop("entry_key", "main"),
        origin=overrides.pop("origin", "delivery"),
        chat_id=payload["chat_id"],
        amount=payload["amount"],
        payload=payload,
        status=overrides.pop("status", PAYOUT_COMPENSATION_STATUS_PENDING),
        error_code_first=overrides.pop("error_code_first", "telegram_api_error"),
        error_code_last=overrides.pop("error_code_last", "telegram_api_error"),
        error_last=overrides.pop("error_last", "ConnectionError: network down"),
        ambiguous=overrides.pop("ambiguous", False),
        retry_count=overrides.pop("retry_count", 0),
        next_attempt_at=overrides.pop("next_attempt_at", now - timedelta(seconds=1)),
        created_at=overrides.pop("created_at", now - timedelta(seconds=30)),
        updated_at=overrides.pop("updated_at", now - timedelta(seconds=30)),
        **overrides,
    )
    async with session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _get_compensation_row(session_factory, row_id: int) -> PayoutCompensation:
    async with session_factory() as db:
        row = await db.get(PayoutCompensation, row_id)
        assert row is not None
        return row


def _scan_config(**overrides) -> dict:
    config = dict(payout_compensation_service.DEFAULT_CONFIG)
    config.update(overrides)
    return config


class _ReplayClient:
    def __init__(
        self,
        *,
        send_ids: list[int] | None = None,
        send_error: Exception | None = None,
        messages: list[SimpleNamespace] | None = None,
        iter_error: Exception | None = None,
    ) -> None:
        self.send_ids = list(send_ids or [900])
        self.send_error = send_error
        self.messages = list(messages or [])
        self.iter_error = iter_error
        self.sent: list[dict[str, object]] = []

    def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
        async def _gen():
            if self.iter_error is not None:
                raise self.iter_error
            for msg in self.messages:
                yield msg

        return _gen()

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        message_id = self.send_ids.pop(0) if self.send_ids else 900
        return SimpleNamespace(id=message_id)


@pytest.fixture(autouse=True)
def _runtime_rate_limit_engine(monkeypatch):
    engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    monkeypatch.setattr(worker_runtime, "_interaction_userbot_engine", lambda _account_id: engine)
    return engine


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


@pytest.mark.asyncio
async def test_payout_replay_backoff_then_abandons_and_notifies_once(
    payout_session_factory,
    monkeypatch,
) -> None:
    row = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_retry",
        trace_id="evt_retry",
    )
    redis = _FakeRedis()
    client = _ReplayClient(send_error=ConnectionError("network down"))
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(worker_runtime, "record_action", AsyncMock())

    config = _scan_config(max_retries=2, backoff_base_seconds=10, backoff_max_seconds=60)
    assert await worker_runtime._scan_payout_compensations_once(redis, client, 7, config=config) == 1

    first = await _get_compensation_row(payout_session_factory, row.id)
    assert first.status == PAYOUT_COMPENSATION_STATUS_PENDING
    assert first.retry_count == 1
    assert worker_runtime._as_utc(first.next_attempt_at) > worker_runtime._as_utc(row.next_attempt_at)
    assert redis.list_pushes == []

    async with payout_session_factory() as db:
        current = await db.get(PayoutCompensation, row.id)
        assert current is not None
        current.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    assert await worker_runtime._scan_payout_compensations_once(redis, client, 7, config=config) == 1
    abandoned = await _get_compensation_row(payout_session_factory, row.id)
    assert abandoned.status == PAYOUT_COMPENSATION_STATUS_ABANDONED
    assert abandoned.retry_count == 2
    assert abandoned.notified_at is not None
    assert len(redis.list_pushes) == 1

    assert await worker_runtime._scan_payout_compensations_once(redis, client, 7, config=config) == 0
    assert len(redis.list_pushes) == 1


@pytest.mark.asyncio
async def test_payout_replay_recovers_from_sent_marker_without_sending(
    payout_session_factory,
    monkeypatch,
) -> None:
    row = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_sent_marker",
        trace_id="evt_sent_marker",
    )
    redis = _FakeRedis()
    redis.values["payout:sent:7:pay_sent_marker"] = "321"
    client = _ReplayClient()
    record_action = AsyncMock()
    monkeypatch.setattr(worker_runtime, "record_action", record_action)

    assert await worker_runtime._scan_payout_compensations_once(redis, client, 7, config=_scan_config()) == 1

    saved = await _get_compensation_row(payout_session_factory, row.id)
    assert saved.status == PAYOUT_COMPENSATION_STATUS_SENT
    assert saved.sent_message_id == 321
    assert client.sent == []
    assert record_action.await_args.kwargs["replay_recovered"] is True
    assert record_action.await_args.args[0]["trace_id"] == "evt_sent_marker"


@pytest.mark.asyncio
async def test_payout_replay_ambiguous_probe_recovers_and_probe_error_falls_back_to_send(
    payout_session_factory,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    recovered = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_ambiguous_hit",
        trace_id="evt_ambiguous_hit",
        ambiguous=True,
        created_at=now - timedelta(seconds=10),
    )
    redis = _FakeRedis()
    client = _ReplayClient(
        messages=[SimpleNamespace(id=555, raw_text="+10", date=now - timedelta(seconds=5))]
    )
    record_action = AsyncMock()
    monkeypatch.setattr(worker_runtime, "record_action", record_action)

    await worker_runtime._scan_payout_compensations_once(redis, client, 7, config=_scan_config())

    saved = await _get_compensation_row(payout_session_factory, recovered.id)
    assert saved.status == PAYOUT_COMPENSATION_STATUS_SENT
    assert saved.sent_message_id == 555
    assert client.sent == []
    assert record_action.await_args.kwargs["ambiguous_probe"] is True

    fallback = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_ambiguous_error",
        trace_id="evt_ambiguous_error",
        ambiguous=True,
    )
    fallback_client = _ReplayClient(send_ids=[556], iter_error=RuntimeError("probe failed"))
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))

    await worker_runtime._scan_payout_compensations_once(redis, fallback_client, 7, config=_scan_config())

    saved_fallback = await _get_compensation_row(payout_session_factory, fallback.id)
    assert saved_fallback.status == PAYOUT_COMPENSATION_STATUS_SENT
    assert saved_fallback.sent_message_id == 556
    assert fallback_client.sent == [{"chat_id": -100123, "text": "+10", "reply_to": None, "parse_mode": None}]


@pytest.mark.asyncio
async def test_payout_replay_limit_state_machine_single_abandon_daily_defer(
    payout_session_factory,
    monkeypatch,
) -> None:
    single = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_single_limit",
        trace_id="evt_single_limit",
        amount=500,
        text="+500",
    )
    redis = _FakeRedis()
    monkeypatch.setattr(
        worker_runtime,
        "_check_payout_limit",
        AsyncMock(return_value=(False, "payout 单笔上限超限：本笔 500，单笔上限 100。")),
    )
    monkeypatch.setattr(worker_runtime, "record_action", AsyncMock())

    await worker_runtime._scan_payout_compensations_once(redis, _ReplayClient(), 7, config=_scan_config())

    single_saved = await _get_compensation_row(payout_session_factory, single.id)
    assert single_saved.status == PAYOUT_COMPENSATION_STATUS_ABANDONED
    assert single_saved.notified_at is not None

    daily = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_daily_limit",
        trace_id="evt_daily_limit",
        amount=50,
        text="+50",
        retry_count=3,
    )
    monkeypatch.setattr(
        worker_runtime,
        "_check_payout_limit",
        AsyncMock(return_value=(False, "payout 日累计上限超限：今日已用 100，本笔 50，日累计上限 100。")),
    )
    before = datetime.now(UTC)
    await worker_runtime._scan_payout_compensations_once(redis, _ReplayClient(), 7, config=_scan_config())

    daily_saved = await _get_compensation_row(payout_session_factory, daily.id)
    assert daily_saved.status == PAYOUT_COMPENSATION_STATUS_PENDING
    assert daily_saved.retry_count == 3
    assert daily_saved.notified_at is not None
    assert worker_runtime._as_utc(daily_saved.next_attempt_at) >= datetime(
        (before + timedelta(days=1)).year,
        (before + timedelta(days=1)).month,
        (before + timedelta(days=1)).day,
        tzinfo=UTC,
    )


@pytest.mark.asyncio
async def test_payout_replay_success_records_ok_action_with_original_trace(
    payout_session_factory,
    monkeypatch,
) -> None:
    row = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_replay_success",
        trace_id="evt_replay_success",
    )
    record_action = AsyncMock()
    monkeypatch.setattr(worker_runtime, "record_action", record_action)
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))

    await worker_runtime._scan_payout_compensations_once(
        _FakeRedis(),
        _ReplayClient(send_ids=[777]),
        7,
        config=_scan_config(),
    )

    saved = await _get_compensation_row(payout_session_factory, row.id)
    assert saved.status == PAYOUT_COMPENSATION_STATUS_SENT
    assert saved.sent_message_id == 777
    assert record_action.await_args.args[0]["trace_id"] == "evt_replay_success"
    assert record_action.await_args.args[2] == "ok"
    assert record_action.await_args.kwargs["replay"] is True
    assert record_action.await_args.kwargs["result"]["message_id"] == 777


@pytest.mark.asyncio
async def test_payout_replay_drops_reply_anchor_and_retries_once(
    payout_session_factory,
    monkeypatch,
) -> None:
    row = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_drop_anchor",
        trace_id="evt_drop_anchor",
        payload={"reply_to_user_id": 111, "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言。"},
    )
    client = _ReplayClient(send_ids=[778])
    record_action = AsyncMock()
    monkeypatch.setattr(worker_runtime, "record_action", record_action)
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))

    await worker_runtime._scan_payout_compensations_once(_FakeRedis(), client, 7, config=_scan_config())

    saved = await _get_compensation_row(payout_session_factory, row.id)
    assert saved.status == PAYOUT_COMPENSATION_STATUS_SENT
    assert saved.sent_message_id == 778
    assert client.sent == [{"chat_id": -100123, "text": "+10", "reply_to": None, "parse_mode": None}]
    assert record_action.await_args.kwargs["replay_drop_reply_anchor"] is True


@pytest.mark.asyncio
async def test_payout_replay_concurrent_scan_lease_sends_once(
    payout_session_factory,
    monkeypatch,
) -> None:
    row = await _insert_compensation_row(
        payout_session_factory,
        payout_key="pay_concurrent",
        trace_id="evt_concurrent",
    )
    redis = _FakeRedis()
    client = _ReplayClient(send_ids=[779])
    monkeypatch.setattr(worker_runtime, "record_action", AsyncMock())
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))

    await asyncio.gather(
        worker_runtime._scan_payout_compensations_once(redis, client, 7, config=_scan_config()),
        worker_runtime._scan_payout_compensations_once(redis, client, 7, config=_scan_config()),
    )

    saved = await _get_compensation_row(payout_session_factory, row.id)
    assert saved.status == PAYOUT_COMPENSATION_STATUS_SENT
    assert saved.sent_message_id == 779
    assert len(client.sent) == 1
