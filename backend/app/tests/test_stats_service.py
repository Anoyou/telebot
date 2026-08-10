from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.account import Account  # noqa: F401 - registers FK target table metadata
from app.db.models.action_event import ACTION_EVENT_STATUS_FAILED, ACTION_EVENT_STATUS_OK, ActionEvent
from app.db.models.log import EventTrace
from app.services import ledger_service, stats_service


@pytest.fixture
async def stats_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE account (id BIGINT PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO account (id) VALUES (7), (8)"))
        await conn.run_sync(ActionEvent.__table__.create)
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
        **overrides,
    )
    async with session_factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_operational_stats_net_matches_ledger_summary(stats_session_factory) -> None:
    base = datetime(2026, 7, 9, 8, 0, tzinfo=UTC)
    await _insert_action_event(
        stats_session_factory,
        action_type="start_session",
        session_key="game:-100123:round-1",
        params_summary={"type": "start_session", "chat_id": -100123, "participant_user_ids": [111, 222]},
        created_at=base,
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="start_session",
        session_key="game:-100123:round-1",
        params_summary={"type": "start_session", "chat_id": -100123},
        created_at=base + timedelta(seconds=1),
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="start_session",
        session_key="game:-100456:round-2",
        params_summary={"type": "start_session", "chat_id": -100456},
        status=ACTION_EVENT_STATUS_FAILED,
        error_code="session_error",
        created_at=base + timedelta(days=1),
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "100", "chat_id": -100123, "payer_user_id": 333},
        created_at=base + timedelta(minutes=2),
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "30", "chat_id": -100123, "payout_key": "pay_ok"},
        created_at=base + timedelta(minutes=3),
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100456, "payout_key": "pay_failed"},
        status=ACTION_EVENT_STATUS_FAILED,
        error_code="telegram_api_error",
        created_at=base + timedelta(days=1),
    )
    await _insert_action_event(
        stats_session_factory,
        account_id=8,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "999", "chat_id": -100123},
        created_at=base,
    )

    stats_filters = stats_service.StatsFilters(
        since=base,
        until=base + timedelta(days=2),
        account_id=7,
        plugin_key="game",
    )
    ledger_filters = ledger_service.LedgerFilters(
        since=base,
        until=base + timedelta(days=2),
        account_id=7,
        plugin_key="game",
        limit=None,
    )
    async with stats_session_factory() as db:
        stats = await stats_service.summarize_operational_stats(db, stats_filters)
        ledger = await ledger_service.summarize_ledger(db, ledger_filters)

    assert stats.total.started_sessions == 2
    assert stats.total.participant_count == 3
    assert stats.total.payout_success_count == 1
    assert stats.total.payout_failure_count == 1
    assert stats.total.payout_attempt_count == 2
    assert Decimal(stats.total.payout_success_rate or "0") == Decimal("50.00")
    assert Decimal(stats.total.ledger_income) == Decimal(ledger.income) == Decimal("100")
    assert Decimal(stats.total.ledger_payout) == Decimal(ledger.payout) == Decimal("30")
    assert Decimal(stats.total.ledger_net) == Decimal(ledger.net) == Decimal("70")
    assert stats.total.ledger_count == ledger.count == 2

    by_day = {item.key: item for item in stats.by_day}
    assert by_day["2026-07-09"].started_sessions == 2
    assert by_day["2026-07-09"].payout_success_count == 1
    assert Decimal(by_day["2026-07-09"].ledger_net) == Decimal("70")
    assert by_day["2026-07-10"].started_sessions == 0
    assert by_day["2026-07-10"].payout_failure_count == 1
    assert Decimal(by_day["2026-07-10"].ledger_net) == Decimal("0")

    by_chat = {item.key: item for item in stats.by_chat}
    assert by_chat["-100123"].started_sessions == 2
    assert by_chat["-100123"].payout_success_count == 1
    assert Decimal(by_chat["-100123"].ledger_net) == Decimal("70")
    assert by_chat["-100456"].started_sessions == 0
    assert by_chat["-100456"].payout_failure_count == 1
    assert Decimal(by_chat["-100456"].ledger_net) == Decimal("0")


@pytest.mark.asyncio
async def test_operational_stats_chat_filter_matches_ledger_filter(stats_session_factory) -> None:
    base = datetime(2026, 7, 9, 8, 0, tzinfo=UTC)
    await _insert_action_event(
        stats_session_factory,
        action_type="payment_confirmed",
        params_summary={"event_type": "payment_confirmed", "amount": "100", "chat_id": -100123},
        created_at=base,
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "35", "chat_id": -100456, "payout_key": "pay_other"},
        created_at=base + timedelta(minutes=1),
    )

    stats_filters = stats_service.StatsFilters(
        since=base,
        until=base + timedelta(days=1),
        account_id=7,
        chat_id=-100123,
    )
    ledger_filters = ledger_service.LedgerFilters(
        since=base,
        until=base + timedelta(days=1),
        account_id=7,
        chat_id=-100123,
        limit=None,
    )
    async with stats_session_factory() as db:
        stats = await stats_service.summarize_operational_stats(db, stats_filters)
        ledger = await ledger_service.summarize_ledger(db, ledger_filters)

    assert stats.total.payout_attempt_count == 0
    assert Decimal(stats.total.ledger_net) == Decimal(ledger.net) == Decimal("100")
    assert [item.key for item in stats.by_chat] == ["-100123"]


@pytest.mark.asyncio
async def test_operational_stats_recovers_participants_from_successful_payouts(stats_session_factory) -> None:
    base = datetime(2026, 7, 9, 8, 0, tzinfo=UTC)
    await _insert_action_event(
        stats_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100123, "reply_to_user_id": 111},
        created_at=base,
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100123, "result": {"reply_to_user_id": 222}},
        created_at=base + timedelta(seconds=1),
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payout",
        params_summary={"type": "payout", "amount": "10", "chat_id": -100123, "receiver_user_id": 999},
        status=ACTION_EVENT_STATUS_FAILED,
        created_at=base + timedelta(seconds=2),
    )

    async with stats_session_factory() as db:
        stats = await stats_service.summarize_operational_stats(
            db,
            stats_service.StatsFilters(
                since=base,
                until=base + timedelta(days=1),
                account_id=7,
            ),
        )

    assert stats.total.participant_count == 2
    assert stats.total.payout_success_count == 2
    assert stats.total.payout_failure_count == 1


@pytest.mark.asyncio
async def test_operational_stats_default_window_matches_ledger_window(stats_session_factory) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=ledger_service.DEFAULT_LEDGER_SUMMARY_WINDOW_DAYS + 1)
    recent = now - timedelta(days=1)
    await _insert_action_event(
        stats_session_factory,
        action_type="start_session",
        session_key="game:-100123:old",
        params_summary={"type": "start_session", "chat_id": -100123},
        created_at=old,
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payment_confirmed",
        session_key="game:-100123:old",
        params_summary={"event_type": "payment_confirmed", "amount": "90", "chat_id": -100123},
        created_at=old,
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="start_session",
        session_key="game:-100123:recent",
        params_summary={"type": "start_session", "chat_id": -100123},
        created_at=recent,
    )
    await _insert_action_event(
        stats_session_factory,
        action_type="payment_confirmed",
        session_key="game:-100123:recent",
        params_summary={"event_type": "payment_confirmed", "amount": "10", "chat_id": -100123},
        created_at=recent,
    )

    async with stats_session_factory() as db:
        stats = await stats_service.summarize_operational_stats(
            db,
            stats_service.StatsFilters(account_id=7, plugin_key="game"),
        )

    assert stats.total.started_sessions == 1
    assert stats.total.ledger_count == 1
    assert Decimal(stats.total.ledger_net) == Decimal("10")
    assert [item.key for item in stats.by_day] == [recent.date().isoformat()]
