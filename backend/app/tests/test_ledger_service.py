from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.account import Account  # noqa: F401 - registers FK target table metadata
from app.db.models.action_event import (
    ACTION_EVENT_STATUS_COMPENSATED,
    ACTION_EVENT_STATUS_DRY_RUN,
    ACTION_EVENT_STATUS_FAILED,
    ACTION_EVENT_STATUS_OK,
    ActionEvent,
)
from app.db.models.log import EventTrace
from app.db.models.payout_compensation import (
    PAYOUT_COMPENSATION_STATUS_COMPENSATED,
    PAYOUT_COMPENSATION_STATUS_PENDING,
    PayoutCompensation,
)
from app.services import ledger_service


@pytest.fixture
async def ledger_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE account (id BIGINT PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO account (id) VALUES (7), (8)"))
        await conn.run_sync(ActionEvent.__table__.create)
        await conn.run_sync(PayoutCompensation.__table__.create)
        await conn.run_sync(EventTrace.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _insert_action_event(session_factory, **overrides) -> ActionEvent:
    row = ActionEvent(
        account_id=overrides.pop("account_id", 7),
        channel=overrides.pop("channel", "interaction_bot"),
        session_key=overrides.pop("session_key", "session_1"),
        plugin_key=overrides.pop("plugin_key", "game"),
        entry_key=overrides.pop("entry_key", "main"),
        action_type=overrides.pop("action_type"),
        params_summary=overrides.pop("params_summary"),
        status=overrides.pop("status", ACTION_EVENT_STATUS_OK),
        error_code=overrides.pop("error_code", None),
        error_summary=overrides.pop("error_summary", None),
        created_at=overrides.pop("created_at"),
    )
    async with session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _insert_compensation(session_factory, **overrides) -> PayoutCompensation:
    now = overrides.pop("now", datetime.now(UTC))
    row = PayoutCompensation(
        payout_key=overrides.pop("payout_key", "pay_manual"),
        account_id=overrides.pop("account_id", 7),
        trace_id=overrides.pop("trace_id", "evt_manual"),
        plugin_key=overrides.pop("plugin_key", "game"),
        entry_key=overrides.pop("entry_key", "main"),
        origin=overrides.pop("origin", "delivery"),
        chat_id=overrides.pop("chat_id", -100123),
        amount=overrides.pop("amount", 88),
        payload=overrides.pop(
            "payload",
            {"action_type": "payout", "payout_key": "pay_manual", "chat_id": -100123, "amount": 88},
        ),
        status=overrides.pop("status", PAYOUT_COMPENSATION_STATUS_PENDING),
        error_code_first=overrides.pop("error_code_first", "telegram_api_error"),
        error_code_last=overrides.pop("error_code_last", "telegram_api_error"),
        error_last=overrides.pop("error_last", "ConnectionError: network down"),
        ambiguous=overrides.pop("ambiguous", False),
        retry_count=overrides.pop("retry_count", 1),
        next_attempt_at=overrides.pop("next_attempt_at", now - timedelta(seconds=1)),
        created_at=overrides.pop("created_at", now - timedelta(minutes=10)),
        updated_at=overrides.pop("updated_at", now - timedelta(minutes=10)),
        **overrides,
    )
    async with session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _insert_event_trace(session_factory, **overrides) -> EventTrace:
    row = EventTrace(
        id=overrides.pop("id", 1),
        trace_id=overrides.pop("trace_id", "trace_1"),
        account_id=overrides.pop("account_id", 7),
        source_channel=overrides.pop("source_channel", "interaction_bot"),
        event_type=overrides.pop("event_type", "message"),
        chat_id=overrides.pop("chat_id", -100123),
        sender_user_id=overrides.pop("sender_user_id", 9001),
        sender_name=overrides.pop("sender_name", "历史收款人"),
        status=overrides.pop("status", "ok"),
        payload_snapshot=overrides.pop("payload_snapshot", {"chat": {"title": "历史测试群"}}),
        started_at=overrides.pop("started_at", datetime.now(UTC)),
        **overrides,
    )
    async with session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_ledger_summary_matches_filtered_entries_sum(ledger_session_factory) -> None:
    base = datetime.now(UTC) - timedelta(days=1)
    await _insert_action_event(
        ledger_session_factory,
        action_type="payment_confirmed",
        params_summary={
            "event_type": "payment_confirmed",
            "amount": "100.50",
            "chat_id": -100123,
            "chat_title": "测试群",
            "payer_user_id": 8001,
            "payer_name": "付款人",
            "receiver_user_id": 9001,
            "receiver_name": "收款人甲",
        },
        created_at=base,
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={
            "type": "payout",
            "amount": "30.25",
            "chat_id": -100123,
            "chat_title": "测试群",
            "payout_key": "pay_1",
            "receiver_user_id": 9002,
            "receiver_name": "收款人乙",
        },
        created_at=base + timedelta(minutes=5),
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100456, "payout_key": "pay_2"},
        status=ACTION_EVENT_STATUS_FAILED,
        error_code="telegram_api_error",
        created_at=base + timedelta(days=1),
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "99", "chat_id": -100456, "payout_key": "pay_dry"},
        status=ACTION_EVENT_STATUS_DRY_RUN,
        created_at=base + timedelta(days=1, minutes=1),
    )
    await _insert_action_event(
        ledger_session_factory,
        account_id=8,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "999", "chat_id": -100123},
        created_at=base,
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="send_message",
        params_summary={"type": "send_message", "amount": "999", "chat_id": -100123},
        created_at=base,
    )

    filters = ledger_service.LedgerFilters(account_id=7, plugin_key="game", limit=None)
    async with ledger_session_factory() as db:
        entries = await ledger_service.list_ledger_entries(db, filters)
        summary = await ledger_service.summarize_ledger(db, filters)

    real_entries = {
        item.id: item
        for item in entries
        if item.status in {ACTION_EVENT_STATUS_OK, ACTION_EVENT_STATUS_COMPENSATED}
    }
    income = sum((Decimal(item.amount) for item in real_entries.values() if item.direction == "in"), Decimal("0"))
    payout = sum((Decimal(item.amount) for item in real_entries.values() if item.direction == "out"), Decimal("0"))

    assert len(entries) == 4
    assert summary.count == len(real_entries) == 2
    assert Decimal(summary.income) == income == Decimal("100.50")
    assert Decimal(summary.payout) == payout == Decimal("30.25")
    assert Decimal(summary.net) == income - payout == Decimal("70.25")
    assert {item.key for item in summary.by_day} == {base.date().isoformat()}
    assert {item.key for item in summary.by_chat} == {"-100123"}
    assert summary.by_chat[0].label == "测试群"
    assert {item.user_id for item in summary.by_recipient} == {9001, 9002}
    recipient_amounts = {item.user_id: Decimal(item.received) for item in summary.by_recipient}
    assert recipient_amounts == {9001: Decimal("100.50"), 9002: Decimal("30.25")}
    income_entry = next(item for item in entries if item.direction == "in")
    assert income_entry.chat_title == "测试群"
    assert income_entry.payer_user_id == 8001
    assert income_entry.payer_name == "付款人"
    assert income_entry.receiver_user_id == 9001
    assert income_entry.receiver_name == "收款人甲"


@pytest.mark.asyncio
async def test_ledger_summary_excludes_failed_replay_when_compensation_ok_exists(ledger_session_factory) -> None:
    base = datetime.now(UTC) - timedelta(days=1)
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100456, "payout_key": "pay_retry"},
        status=ACTION_EVENT_STATUS_FAILED,
        error_code="telegram_api_error",
        created_at=base,
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100456, "payout_key": "pay_retry"},
        status=ACTION_EVENT_STATUS_OK,
        created_at=base + timedelta(minutes=1),
    )

    async with ledger_session_factory() as db:
        summary = await ledger_service.summarize_ledger(
            db,
            ledger_service.LedgerFilters(account_id=7, plugin_key="game", limit=None),
        )

    assert summary.count == 1
    assert Decimal(summary.payout) == Decimal("10")
    assert Decimal(summary.net) == Decimal("-10")


@pytest.mark.asyncio
async def test_ledger_summary_includes_compensated_but_rejects_failed_status_filter(ledger_session_factory) -> None:
    base = datetime.now(UTC) - timedelta(days=1)
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "12", "chat_id": -100456, "payout_key": "pay_manual"},
        status=ACTION_EVENT_STATUS_COMPENSATED,
        created_at=base,
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "7", "chat_id": -100456, "payout_key": "pay_failed"},
        status=ACTION_EVENT_STATUS_FAILED,
        created_at=base + timedelta(minutes=1),
    )

    async with ledger_session_factory() as db:
        summary = await ledger_service.summarize_ledger(db, ledger_service.LedgerFilters(account_id=7, limit=None))
        failed_summary = await ledger_service.summarize_ledger(
            db,
            ledger_service.LedgerFilters(account_id=7, status=ACTION_EVENT_STATUS_FAILED, limit=None),
        )

    assert summary.count == 1
    assert Decimal(summary.payout) == Decimal("12")
    assert Decimal(summary.net) == Decimal("-12")
    assert failed_summary.count == 0
    assert Decimal(failed_summary.payout) == Decimal("0")


@pytest.mark.asyncio
async def test_ledger_summary_applies_default_time_window(ledger_session_factory) -> None:
    now = datetime.now(UTC)
    await _insert_action_event(
        ledger_session_factory,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "100", "chat_id": -100123},
        created_at=now - timedelta(days=ledger_service.DEFAULT_LEDGER_SUMMARY_WINDOW_DAYS + 1),
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "9", "chat_id": -100123},
        created_at=now - timedelta(days=1),
    )

    async with ledger_session_factory() as db:
        summary = await ledger_service.summarize_ledger(db, ledger_service.LedgerFilters(account_id=7, limit=None))

    assert summary.count == 1
    assert Decimal(summary.income) == Decimal("9")


@pytest.mark.asyncio
async def test_ledger_hydrates_historical_chat_and_recipient_labels(ledger_session_factory) -> None:
    now = datetime.now(UTC)
    await _insert_event_trace(ledger_session_factory, started_at=now)
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={
            "type": "payout",
            "amount": "66",
            "chat_id": -100123,
            "reply_to_user_id": 9001,
            "payout_key": "pay_historical",
        },
        created_at=now,
    )

    async with ledger_session_factory() as db:
        entries = await ledger_service.list_ledger_entries(db)
        summary = await ledger_service.summarize_ledger(db)

    assert entries[0].chat_title == "历史测试群"
    assert entries[0].receiver_user_id == 9001
    assert entries[0].receiver_name == "历史收款人"
    assert summary.by_chat[0].label == "历史测试群"
    assert summary.by_recipient[0].label == "历史收款人"


@pytest.mark.asyncio
async def test_manual_paid_compensation_writes_audit_and_closes_row(
    ledger_session_factory,
    monkeypatch,
) -> None:
    row = await _insert_compensation(ledger_session_factory)
    audit_write = AsyncMock()
    monkeypatch.setattr(ledger_service.audit, "write", audit_write)

    async with ledger_session_factory() as db:
        paid = await ledger_service.mark_compensation_manual_paid(
            db,
            row.id,
            user_id=42,
            note="线下已补",
        )
        await db.commit()

    assert paid.status == PAYOUT_COMPENSATION_STATUS_COMPENSATED
    assert paid.sent_at is not None
    audit_write.assert_awaited_once()
    assert audit_write.await_args.args[1] == 42
    assert audit_write.await_args.args[2] == "ledger.compensation.manual_paid"
    assert audit_write.await_args.kwargs["target"] == f"payout_compensation:{row.id}"
    assert audit_write.await_args.kwargs["detail"] == {
        "previous_status": PAYOUT_COMPENSATION_STATUS_PENDING,
        "status": PAYOUT_COMPENSATION_STATUS_COMPENSATED,
        "payout_key": "pay_manual",
        "account_id": 7,
        "chat_id": -100123,
        "amount": "88",
        "plugin_key": "game",
        "entry_key": "main",
        "note": "线下已补",
    }

    async with ledger_session_factory() as db:
        current = await db.get(PayoutCompensation, row.id)
        assert current is not None
        assert current.status == PAYOUT_COMPENSATION_STATUS_COMPENSATED
        assert current.sent_at is not None
        open_rows = await ledger_service.list_compensations(db)
        all_rows = (await db.execute(select(PayoutCompensation))).scalars().all()

    assert open_rows == []
    assert len(all_rows) == 1


@pytest.mark.asyncio
async def test_reset_ledger_data_removes_financial_and_operational_rows_only(
    ledger_session_factory,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    for action_type in ("payment_confirmed", "payout", "start_session", "send_message"):
        await _insert_action_event(
            ledger_session_factory,
            action_type=action_type,
            params_summary={"type": action_type, "amount": "10", "chat_id": -100123},
            created_at=now,
        )
    await _insert_compensation(ledger_session_factory, payout_key="pay_reset")
    audit_write = AsyncMock()
    monkeypatch.setattr(ledger_service.audit, "write", audit_write)

    async with ledger_session_factory() as db:
        result = await ledger_service.reset_ledger_data(db, user_id=42)
        await db.commit()

    assert result.deleted_action_events == 3
    assert result.deleted_compensations == 1
    audit_write.assert_awaited_once()
    assert audit_write.await_args.args[2] == "ledger.reset"
    async with ledger_session_factory() as db:
        action_rows = (await db.execute(select(ActionEvent))).scalars().all()
        compensation_rows = (await db.execute(select(PayoutCompensation))).scalars().all()
    assert [row.action_type for row in action_rows] == ["send_message"]
    assert compensation_rows == []
