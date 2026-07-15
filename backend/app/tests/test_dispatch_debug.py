from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.api import dispatch_debug
from app.services import account_bot_runtime
from app.worker import runtime as runtime_mod
from app.worker.ipc import CMD_DISPATCH_SIMULATE, CMD_STOP, EVT_ACK, IPCMessage, make_cmd
from app.worker.plugins import loader as loader_mod


class _FakeCmdPubSub:
    def __init__(self, queue: asyncio.Queue[dict]) -> None:
        self._queue = queue
        self.closed = False

    async def subscribe(self, *_args, **_kwargs) -> None:
        return None

    async def unsubscribe(self, *_args, **_kwargs) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def get_message(self, *_args, **_kwargs) -> dict:
        return await self._queue.get()


class _FakeCmdRedis:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        self.published: list[tuple[str, str]] = []
        self.logs: list[tuple[str, str]] = []

    def pubsub(self) -> _FakeCmdPubSub:
        return _FakeCmdPubSub(self.messages)

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def rpush(self, key: str, payload: str) -> int:
        self.logs.append((key, payload))
        return len(self.logs)

    async def send_cmd(self, payload: str) -> None:
        await self.messages.put({"type": "message", "data": payload})


async def _wait_for_publish(redis: _FakeCmdRedis, predicate, *, timeout: float = 1.0) -> tuple[str, IPCMessage]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for channel, payload in redis.published:
            msg = IPCMessage.decode(payload)
            if predicate(channel, msg):
                return channel, msg
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for redis publish")


def _stage(trace: dict, name: str) -> dict:
    for item in trace["stages"]:
        if item["stage"] == name:
            return item
    raise AssertionError(f"stage missing: {name}")


async def _run_dispatch_simulation(account_id: int, text: str) -> dict:
    redis = _FakeCmdRedis()
    client = AsyncMock()
    paused = asyncio.Event()
    paused.set()
    listener = asyncio.create_task(runtime_mod._listen_cmd(redis, client, account_id, paused))
    try:
        await redis.send_cmd(
            make_cmd(
                CMD_DISPATCH_SIMULATE,
                reply_to="dispatch-debug-reply",
                cmd_id="dispatch-debug-1",
                account_id=account_id,
                chat_type="group",
                chat_id=-100123,
                sender_id=42,
                text=text,
                via="userbot",
            )
        )
        _, ack = await _wait_for_publish(
            redis,
            lambda ch, item: (
                ch == "dispatch-debug-reply"
                and item.type == EVT_ACK
                and item.payload.get("cmd_id") == "dispatch-debug-1"
            ),
        )
        assert ack.payload["ok"] is True
        return ack.payload["trace"]
    finally:
        await redis.send_cmd(make_cmd(CMD_STOP))
        await asyncio.wait_for(listener, timeout=1)


@pytest.mark.asyncio
async def test_dispatch_debug_simulate_returns_keyword_hit(monkeypatch) -> None:
    account_id = 907
    state = loader_mod._AccountState(account_id=account_id)
    state.interaction_text_guard_rules = (
        loader_mod._InteractionTextGuardRule(chat_ids=frozenset({-100123}), texts=frozenset({"开局"})),
    )
    monkeypatch.setitem(loader_mod._STATES, account_id, state)

    trace = await _run_dispatch_simulation(account_id, "开局")

    assert trace["account_id"] == account_id
    assert trace["via"] == "userbot"
    assert trace["chat"]["chat_id"] == -100123
    keyword = _stage(trace, "keyword")
    assert keyword["matched"] is True
    assert keyword["reason_code"] == "matched"
    assert keyword["matches"][0]["reason_code"] == "interaction_rule_owned"


@pytest.mark.asyncio
async def test_dispatch_debug_simulate_returns_no_match_trace(monkeypatch) -> None:
    account_id = 908
    state = loader_mod._AccountState(account_id=account_id)
    monkeypatch.setitem(loader_mod._STATES, account_id, state)

    trace = await _run_dispatch_simulation(account_id, "nothing")

    assert [item["stage"] for item in trace["stages"]] == [
        "direct_passthrough",
        "prefix_command",
        "keyword",
        "event_subscription",
    ]
    assert _stage(trace, "direct_passthrough")["matched"] is False
    assert _stage(trace, "prefix_command")["matched"] is False
    assert _stage(trace, "keyword")["matched"] is False
    assert _stage(trace, "event_subscription")["matched"] is False


@pytest.mark.asyncio
async def test_dispatch_debug_router_delivery_stats_reads_light_summary() -> None:
    account_bot_runtime._ROUTER_DELIVERY_STATS.clear()
    try:
        account_bot_runtime._record_router_delivery_light(
            909,
            "account_bot",
            account_bot_runtime.TRACE_STATUS_OK,
        )

        summary = await dispatch_debug.get_router_delivery_stats(
            object(),
            account_id=909,
            channel="account_bot",
        )

        assert summary["count"] == 1
        assert summary["entries"][0]["account_id"] == 909
        assert summary["entries"][0]["channel"] == "account_bot"
        assert summary["entries"][0]["last_status"] == account_bot_runtime.TRACE_STATUS_OK
    finally:
        account_bot_runtime._ROUTER_DELIVERY_STATS.clear()
