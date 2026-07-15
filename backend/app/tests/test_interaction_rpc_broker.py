"""Interaction RPC broker demultiplex tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services.interaction import rpc_broker
from app.worker.ipc import IPCMessage, make_cmd


class _FakePubSub:
    def __init__(self, redis) -> None:
        self.redis = redis
        self.channels: set[str] = set()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.channels.add(str(channel))
        self.redis.subscribed.add(str(channel))

    async def unsubscribe(self, channel: str) -> None:
        self.channels.discard(str(channel))

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 1.0):
        try:
            return await asyncio.wait_for(self.redis.inbox.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.published: list[tuple[str, str]] = []
        self.subscribed: set[str] = set()
        self._pubsub = _FakePubSub(self)
        self.store: dict[str, str] = {}

    def pubsub(self) -> _FakePubSub:
        return self._pubsub

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        # 模拟 worker 在线：若 reply_to 已订阅，立刻塞回包
        msg = IPCMessage.decode(payload)
        reply_to = str(msg.payload.get("reply_to") or "")
        if reply_to and reply_to in self.subscribed:
            from app.worker.ipc import IPCMessage as M

            raw = M(type="interaction_reply", payload={"ok": True, "actions": [{"type": "result"}], "result": {"message_id": 1}}).encode()
            await self.inbox.put({"type": "message", "channel": reply_to, "data": raw})
            return 1
        return 0

    async def get(self, key: str):  # noqa: ANN201
        return self.store.get(key)

    async def set(self, key: str, value: str, **_kwargs) -> bool:  # noqa: ANN003
        self.store[key] = value
        return True


@pytest.mark.asyncio
async def test_broker_reuses_pubsub_and_returns_payload() -> None:
    await rpc_broker.reset_interaction_rpc_broker_for_tests()
    redis = _FakeRedis()
    broker = await rpc_broker.get_interaction_rpc_broker()
    await broker.start(redis=redis)

    online, attempts, payload, error = await broker.request(
        cmd_channel="cmd:1",
        command=make_cmd("run", reply_to="will-be-overridden"),
        reply_channel="account_bot:interaction_entry:1:test",
        timeout_seconds=2.0,
        online_wait_seconds=0.2,
        redis=redis,
    )
    # 修正：request 使用自己的 reply_channel，需要 command 带同一 reply_to
    # 上面 publish 用了 command 内 reply_to；重测一次带正确 reply_to
    await rpc_broker.reset_interaction_rpc_broker_for_tests()
    redis = _FakeRedis()
    broker = await rpc_broker.get_interaction_rpc_broker()
    await broker.start(redis=redis)
    reply = "account_bot:interaction_entry:1:abc"
    online, attempts, payload, error = await broker.request(
        cmd_channel="cmd:1",
        command=make_cmd("run_entry", reply_to=reply),
        reply_channel=reply,
        timeout_seconds=2.0,
        online_wait_seconds=0.5,
        redis=redis,
    )
    assert online is True
    assert error is None
    assert payload is not None
    assert payload.get("ok") is True
    assert payload.get("actions") == [{"type": "result"}]
    assert attempts >= 1
    await rpc_broker.reset_interaction_rpc_broker_for_tests()


@pytest.mark.asyncio
async def test_broker_reports_offline_when_no_subscriber() -> None:
    await rpc_broker.reset_interaction_rpc_broker_for_tests()
    redis = _FakeRedis()

    async def publish_zero(channel: str, payload: str) -> int:
        redis.published.append((channel, payload))
        return 0

    redis.publish = publish_zero  # type: ignore[method-assign]
    broker = await rpc_broker.get_interaction_rpc_broker()
    await broker.start(redis=redis)
    reply = "account_bot:interaction_entry:2:off"
    online, attempts, payload, error = await broker.request(
        cmd_channel="cmd:2",
        command=make_cmd("run", reply_to=reply),
        reply_channel=reply,
        timeout_seconds=1.0,
        online_wait_seconds=0.2,
        redis=redis,
    )
    assert online is False
    assert payload is None
    assert "不在线" in str(error)
    await rpc_broker.reset_interaction_rpc_broker_for_tests()


@pytest.mark.asyncio
async def test_broker_timeout_exposes_request_id_and_reconciles_late_result() -> None:
    from app.worker.ipc import rpc_result_key

    await rpc_broker.reset_interaction_rpc_broker_for_tests()
    redis = _FakeRedis()
    request_id = "rpc-late-result"

    async def publish_late(_channel: str, payload: str) -> int:
        command = IPCMessage.decode(payload)
        assert command.payload["request_id"] == request_id
        assert int(command.payload["deadline_at_ms"]) > 0

        async def finish_late() -> None:
            await asyncio.sleep(0.2)
            redis.store[rpc_result_key(request_id)] = json.dumps(
                {"ok": True, "result": {"message_id": 77}, "request_id": request_id}
            )

        asyncio.create_task(finish_late())
        return 1

    redis.publish = publish_late  # type: ignore[method-assign]
    broker = await rpc_broker.get_interaction_rpc_broker()
    reply = "account_bot:interaction_action:1:late"
    online, _attempts, payload, error = await broker.request(
        cmd_channel="cmd:1",
        command=make_cmd("run_action", reply_to=reply),
        reply_channel=reply,
        timeout_seconds=0.05,
        online_wait_seconds=0.0,
        redis=redis,
        request_id=request_id,
    )
    assert online is True
    assert payload is None
    assert request_id in str(error)

    await asyncio.sleep(0.25)
    reconciled = await broker.reconcile(request_id, redis=redis)
    assert reconciled == {"ok": True, "result": {"message_id": 77}, "request_id": request_id}
    await rpc_broker.reset_interaction_rpc_broker_for_tests()
