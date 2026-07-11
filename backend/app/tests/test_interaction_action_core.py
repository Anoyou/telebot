"""Shared action dispatch core + SessionRecord unit tests (Wave 5)."""

from __future__ import annotations

import pytest

from app.services.interaction.action_core import (
    CANONICAL_ACTION_TYPES,
    ActionHandlers,
    ActionKind,
    classify_action,
    run_action_batch,
)
from app.services.interaction.session_record import SessionRecord
from app.tests.test_interaction_executor_parity import CANONICAL_ACTION_TYPES as PARITY_CANONICAL


def test_canonical_types_match_parity_guard() -> None:
    assert CANONICAL_ACTION_TYPES == PARITY_CANONICAL


def test_classify_action_kinds() -> None:
    assert classify_action({"type": "update_session"}) == ActionKind.UPDATE_SESSION
    assert classify_action({"type": "end_session"}) == ActionKind.SESSION_CONTROL
    assert classify_action({"type": "payout"}) == ActionKind.PAYOUT
    assert classify_action({"type": "send_photo"}) == ActionKind.SEND_MEDIA
    assert classify_action({"type": "unknown_x"}) == ActionKind.UNSUPPORTED
    assert (
        classify_action({"type": "send_message", "send_via": "notice"})
        == ActionKind.DEPRECATED_SEND_VIA
    )


@pytest.mark.asyncio
async def test_run_action_batch_dispatches_and_counts() -> None:
    seen: list[str] = []

    async def mark(action: dict) -> bool:
        seen.append(str(action.get("type")))
        return True

    async def fail_payout(action: dict) -> bool:
        seen.append("payout")
        return False

    handlers = ActionHandlers(
        on_update_session=mark,
        on_settlement=mark,
        on_payout=fail_payout,
        on_unsupported=mark,
    )
    result = await run_action_batch(
        [
            {"type": "update_session", "data": {}},
            {"type": "settlement"},
            {"type": "payout", "amount": 1},
            {"type": "weird"},
        ],
        handlers,
    )
    assert result.executed == 4
    assert result.failed == 1
    assert "update_session" in seen
    assert "payout" in seen
    assert "weird" in seen


@pytest.mark.asyncio
async def test_run_action_batch_truncates() -> None:
    calls = 0

    async def ok(action: dict) -> bool:
        nonlocal calls
        calls += 1
        return True

    dropped = 0

    async def on_truncated(action: dict) -> bool:
        nonlocal dropped
        dropped += 1
        return False

    handlers = ActionHandlers(on_settlement=ok, on_truncated=on_truncated)
    actions = [{"type": "settlement"} for _ in range(12)]
    result = await run_action_batch(actions, handlers, limit=10)
    assert result.executed == 10
    assert result.dropped == 2
    assert calls == 10
    assert dropped == 2


def test_session_record_roundtrip() -> None:
    raw = {
        "account_id": 7,
        "chat_id": -100,
        "module_key": "game",
        "entry_key": "main",
        "channel": "userbot",
        "data": {"n": 1},
        "expires_at": 9_999_999_999.0,
        "created_at": 1.0,
        "updated_at": 2.0,
        "revision": 3,
        "payer_user_id": 42,
        "paid_user_ids": [42, 43],
    }
    rec = SessionRecord.from_dict(raw)
    assert rec is not None
    assert rec.module_key == "game"
    assert rec.is_active()
    out = rec.to_dict()
    assert out["account_id"] == 7
    assert out["data"]["n"] == 1
    assert out["payer_user_id"] == 42

    rec.touch(now=100.0, ttl_seconds=60)
    assert rec.updated_at == 100.0
    assert rec.expires_at == 160.0
    assert rec.revision == 4
