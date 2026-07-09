from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.account import Account  # noqa: F401 - registers FK target table metadata
from app.db.models.action_event import ACTION_EVENT_STATUS_FAILED, ACTION_EVENT_STATUS_OK, ActionEvent
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


@pytest.mark.asyncio
async def test_ledger_summary_matches_filtered_entries_sum(ledger_session_factory) -> None:
    base = datetime(2026, 7, 9, 8, 0, tzinfo=UTC)
    await _insert_action_event(
        ledger_session_factory,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "100.50", "chat_id": -100123},
        created_at=base,
    )
    await _insert_action_event(
        ledger_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "30.25", "chat_id": -100123, "payout_key": "pay_1"},
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

    income = sum((Decimal(item.amount) for item in entries if item.direction == "in"), Decimal("0"))
    payout = sum((Decimal(item.amount) for item in entries if item.direction == "out"), Decimal("0"))

    assert len(entries) == 3
    assert summary.count == len(entries)
    assert Decimal(summary.income) == income == Decimal("100.50")
    assert Decimal(summary.payout) == payout == Decimal("40.25")
    assert Decimal(summary.net) == income - payout == Decimal("60.25")
    assert {item.key for item in summary.by_day} == {"2026-07-09", "2026-07-10"}
    assert {item.key for item in summary.by_chat} == {"-100123", "-100456"}


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
