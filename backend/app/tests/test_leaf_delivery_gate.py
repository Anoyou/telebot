from __future__ import annotations

import asyncio

import pytest

from app.worker.leaf_delivery_gate import LeafDeliveryGate


@pytest.mark.asyncio
async def test_pause_ack_waits_for_explicit_inflight_delivery() -> None:
    gate = LeafDeliveryGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver() -> None:
        async with gate.delivery() as allowed:
            assert allowed is True
            entered.set()
            await release.wait()

    delivery = asyncio.create_task(deliver())
    await entered.wait()
    pause = asyncio.create_task(gate.pause_and_wait("safe_watch", timeout=1))
    await asyncio.sleep(0)

    assert pause.done() is False
    assert gate.is_set() is False

    release.set()
    await delivery
    assert await pause is True


@pytest.mark.asyncio
async def test_pause_ack_waits_for_tracked_telethon_update_task() -> None:
    gate = LeafDeliveryGate()
    tracked = asyncio.Event()
    release = asyncio.Event()

    async def update_task() -> None:
        assert gate.track_current_task() is True
        tracked.set()
        await release.wait()

    update = asyncio.create_task(update_task())
    await tracked.wait()
    pause = asyncio.create_task(gate.pause_and_wait("safe_watch", timeout=1))
    await asyncio.sleep(0)

    assert pause.done() is False
    release.set()
    await update
    assert await pause is True


def test_normal_resume_cannot_clear_safe_watch_reason() -> None:
    gate = LeafDeliveryGate()
    gate.pause("safe_watch")
    gate.clear()

    gate.set()

    assert gate.is_set() is False
    assert gate.pause_reasons == frozenset({"safe_watch"})

    gate.resume("safe_watch")
    assert gate.is_set() is True
