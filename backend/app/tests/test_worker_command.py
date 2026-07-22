"""worker 命令派发的纯函数测试。

不连真 Telethon，不起子进程；只验证内置命令能正确调用 ``event.edit``。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import event_trace
from app.worker.command import (
    _BUILTIN,
    CommandContext,
    parse_command_key_from_text,
    set_command_context,
    should_allow_auto_command_text,
    should_skip_outgoing_command_echo,
)
from app.worker.ipc import (
    CMD_PING,
    CMD_RELOAD_CONFIG,
    CMD_RUN_INTERACTION_ACTION,
    CMD_RUN_INTERACTION_ENTRY,
    CMD_STOP,
    EVT_ACK,
    EVT_PONG,
    IPCMessage,
    event_channel,
    make_cmd,
)


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
        self.store: dict[str, str] = {}

    def pubsub(self) -> _FakeCmdPubSub:
        return _FakeCmdPubSub(self.messages)

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    async def rpush(self, key: str, payload: str) -> int:
        self.logs.append((key, payload))
        return len(self.logs)

    async def get(self, key: str):  # noqa: ANN201
        return self.store.get(key)

    async def set(self, key: str, value: str, **_kwargs) -> bool:  # noqa: ANN003
        self.store[key] = value
        return True

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


@pytest.mark.asyncio
async def test_help():
    """``,help`` 应当 edit 一次原消息列出命令。"""
    client = AsyncMock()
    event = AsyncMock()
    await _BUILTIN["help"].handler(client, event, [], 1)
    event.edit.assert_called_once()


@pytest.mark.asyncio
async def test_status():
    """``,status`` 应当列出账号 id 与昵称。"""
    client = AsyncMock()
    # client.get_me 是 async；返回一个带 first_name 字段的 mock 对象
    me = AsyncMock()
    me.first_name = "alice"
    me.username = None
    me.id = 1
    client.get_me.return_value = me
    event = AsyncMock()
    await _BUILTIN["status"].handler(client, event, [], 42)
    event.edit.assert_called_once()
    args = event.edit.call_args[0][0]
    assert "#42" in args


@pytest.mark.asyncio
async def test_ping():
    """``,ping`` 必须回复 pong。"""
    client = AsyncMock()
    event = AsyncMock()
    await _BUILTIN["ping"].handler(client, event, [], 1)
    event.edit.assert_called_once_with("pong")


@pytest.mark.asyncio
async def test_worker_rpc_does_not_block_ping(monkeypatch):
    """慢交互入口 RPC 后台执行时，ping 仍应立即得到 pong。"""
    from app.worker import runtime as runtime_mod
    from app.worker.plugins import loader as loader_mod

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_entry(*_args, **_kwargs):
        started.set()
        await release.wait()
        return [{"type": "send_message", "text": "done"}]

    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", _slow_entry)

    redis = _FakeCmdRedis()
    client = AsyncMock()
    paused = asyncio.Event()
    paused.set()
    listener = asyncio.create_task(runtime_mod._listen_cmd(redis, client, 101, paused))
    try:
        await redis.send_cmd(
            make_cmd(
                CMD_RUN_INTERACTION_ENTRY,
                reply_to="rpc-reply",
                cmd_id="rpc-1",
                plugin_key="demo",
                entry_key="main",
                payload={},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        await redis.send_cmd(make_cmd(CMD_PING))
        channel, msg = await _wait_for_publish(
            redis,
            lambda ch, item: ch == event_channel(101) and item.type == EVT_PONG,
        )
        assert channel == event_channel(101)
        assert msg.type == EVT_PONG

        assert release.is_set() is False
        release.set()
        _, rpc_msg = await _wait_for_publish(
            redis,
            lambda ch, item: ch == "rpc-reply" and item.type == CMD_RUN_INTERACTION_ENTRY,
        )
        assert rpc_msg.payload["ok"] is True
        assert rpc_msg.payload["actions"][0]["text"] == "done"

        _, ack_msg = await _wait_for_publish(
            redis,
            lambda ch, item: ch == "rpc-reply" and item.type == EVT_ACK and item.payload.get("cmd_id") == "rpc-1",
        )
        assert ack_msg.payload["ok"] is True
    finally:
        await redis.send_cmd(make_cmd(CMD_STOP))
        await asyncio.wait_for(listener, timeout=1)


@pytest.mark.asyncio
async def test_platform_capability_reload_failure_returns_negative_ack(monkeypatch) -> None:
    from app.worker import runtime as runtime_mod
    from app.worker.plugins import loader as loader_mod

    monkeypatch.setattr(
        loader_mod,
        "reload_account_config",
        AsyncMock(side_effect=RuntimeError("平台能力缓存刷新失败")),
    )
    redis = _FakeCmdRedis()
    client = AsyncMock()
    paused = asyncio.Event()
    paused.set()
    listener = asyncio.create_task(runtime_mod._listen_cmd(redis, client, 104, paused))
    try:
        await redis.send_cmd(
            make_cmd(
                CMD_RELOAD_CONFIG,
                reply_to="reload-ack",
                cmd_id="reload-1",
                source="platform_capabilities",
                module_key="ai",
                generation=4,
                enabled=False,
            )
        )
        _, ack = await _wait_for_publish(
            redis,
            lambda ch, item: (
                ch == "reload-ack"
                and item.type == EVT_ACK
                and item.payload.get("cmd_id") == "reload-1"
            ),
        )
        assert ack.payload["ok"] is False
        assert "平台能力缓存刷新失败" in ack.payload["error"]
    finally:
        await redis.send_cmd(make_cmd(CMD_STOP))
        await asyncio.wait_for(listener, timeout=1)


@pytest.mark.asyncio
async def test_worker_stop_cancels_inflight_rpc(monkeypatch):
    """stop 控制命令要取消尚未完成的后台 RPC 任务。"""
    from app.worker import runtime as runtime_mod
    from app.worker.plugins import loader as loader_mod

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _never_finish(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", _never_finish)

    redis = _FakeCmdRedis()
    client = AsyncMock()
    paused = asyncio.Event()
    paused.set()
    listener = asyncio.create_task(runtime_mod._listen_cmd(redis, client, 102, paused))
    try:
        await redis.send_cmd(
            make_cmd(
                CMD_RUN_INTERACTION_ENTRY,
                reply_to="rpc-cancel",
                cmd_id="rpc-cancel-1",
                plugin_key="demo",
                entry_key="main",
                payload={},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await redis.send_cmd(make_cmd(CMD_STOP))
        await asyncio.wait_for(listener, timeout=1)

        assert cancelled.is_set() is True
        assert not any(
            channel == "rpc-cancel" and IPCMessage.decode(payload).type == CMD_RUN_INTERACTION_ENTRY
            for channel, payload in redis.published
        )
    finally:
        if not listener.done():
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_paused_worker_rejects_interaction_payout_rpc() -> None:
    from app.worker import runtime as runtime_mod

    redis = _FakeCmdRedis()
    client = AsyncMock()
    paused = asyncio.Event()
    listener = asyncio.create_task(runtime_mod._listen_cmd(redis, client, 103, paused))
    try:
        await redis.send_cmd(
            make_cmd(
                CMD_RUN_INTERACTION_ACTION,
                reply_to="rpc-paused",
                request_id="paused-1",
                payload={"action_type": "payout", "chat_id": -100, "amount": 10},
            )
        )
        _, rpc_msg = await _wait_for_publish(
            redis,
            lambda ch, item: ch == "rpc-paused" and item.type == CMD_RUN_INTERACTION_ACTION,
        )
        assert rpc_msg.payload["ok"] is False
        assert "pause" in rpc_msg.payload["error"]
        client.send_message.assert_not_awaited()
    finally:
        await redis.send_cmd(make_cmd(CMD_STOP))
        await asyncio.wait_for(listener, timeout=1)


@pytest.mark.asyncio
async def test_worker_rpc_executor_bounds_one_hundred_slow_requests(monkeypatch):
    from app.worker import runtime as runtime_mod
    from app.worker.plugins import loader as loader_mod

    release = asyncio.Event()
    started = 0

    async def _slow_entry(*_args, **_kwargs):
        nonlocal started
        started += 1
        await release.wait()
        return []

    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", _slow_entry)
    redis = _FakeCmdRedis()
    executor = runtime_mod._RpcCommandExecutor(
        redis=redis,
        client=AsyncMock(),
        account_id=109,
        platform_scheduler=None,
    )
    executor.start()
    await asyncio.sleep(0)
    deadline_at_ms = int(__import__("time").time() * 1000) + 60_000
    for index in range(100):
        await executor.submit(
            IPCMessage(
                CMD_RUN_INTERACTION_ENTRY,
                {
                    "request_id": f"slow-{index}",
                    "deadline_at_ms": deadline_at_ms,
                    "reply_to": f"slow-reply-{index}",
                    "plugin_key": "demo",
                    "entry_key": "main",
                    "payload": {},
                },
            )
        )

    await asyncio.sleep(0)
    stats = runtime_mod.worker_rpc_executor_stats(109)
    assert started == runtime_mod._RPC_MAX_CONCURRENCY
    assert stats["running"] == runtime_mod._RPC_MAX_CONCURRENCY
    assert stats["queued"] <= runtime_mod._RPC_QUEUE_CAPACITY
    assert stats["accepted"] == stats["running"] + stats["queued"]
    assert stats["accepted"] <= runtime_mod._RPC_MAX_CONCURRENCY + runtime_mod._RPC_QUEUE_CAPACITY
    assert stats["rejected"] == 100 - stats["accepted"]

    await executor.stop()
    stopped = runtime_mod.worker_rpc_executor_stats(109)
    assert stopped["running"] == 0
    assert stopped["queued"] == 0


@pytest.mark.asyncio
async def test_periodic_userbot_session_expire_scan_calls_loader(monkeypatch):
    """worker 后台扫描器应周期性调用 loader 的 userbot 会话过期扫描入口。"""
    from app.worker import runtime as runtime_mod
    from app.worker.plugins import loader as loader_mod

    scan = AsyncMock()
    monkeypatch.setattr(loader_mod, "scan_userbot_expired_sessions_once", scan)
    sleep_calls = 0

    async def fake_sleep(seconds):
        nonlocal sleep_calls
        assert seconds == runtime_mod._USERBOT_SESSION_EXPIRE_SCAN_SECONDS
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(runtime_mod.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await runtime_mod._periodic_userbot_session_expire_scan(_FakeCmdRedis(), 103)

    scan.assert_awaited_once_with(103)


@pytest.mark.asyncio
async def test_run_interaction_userbot_action_payout_uses_rate_limit_and_parse_mode(monkeypatch):
    from app.worker import runtime as runtime_mod

    monkeypatch.setattr(
        runtime_mod.payout_compensation,
        "claim_payout_delivery",
        AsyncMock(
            return_value=runtime_mod.payout_compensation.PayoutDeliveryClaim(
                status="acquired",
                row_id=1,
                claim_token="test-token",
            )
        ),
    )
    monkeypatch.setattr(
        runtime_mod.payout_compensation,
        "complete_payout_delivery",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        runtime_mod.payout_compensation,
        "release_payout_delivery_claim",
        AsyncMock(),
    )
    client = AsyncMock()
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=808))
    engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    monkeypatch.setattr(runtime_mod, "_check_payout_limit", AsyncMock(return_value=(True, None)))

    result = await runtime_mod._run_interaction_userbot_action(
        client,
        {
            "action_type": "payout",
            "chat_id": -100333,
            "amount": 25,
            "reply_to_message_id": 44,
            "parse_mode": "html",
        },
        account_id=55,
        engine=engine,
    )

    engine.acquire.assert_awaited_once_with(55, "send_message_group", peer_id=-100333)
    client.send_message.assert_awaited_once_with(
        -100333,
        "+25",
        reply_to=44,
        parse_mode="html",
    )
    assert result["message_id"] == 808
    assert result["chat_id"] == -100333
    assert result["reply_to_message_id"] == 44
    assert result["reply_to_user_id"] is None
    assert str(result["payout_key"]).startswith("pay_")


@pytest.mark.asyncio
async def test_run_interaction_userbot_action_sends_native_rich_message(monkeypatch) -> None:
    from app.services import userbot_rich_message
    from app.worker import runtime as runtime_mod

    send_rich = AsyncMock(return_value={"message_id": 45, "chat_id": -100333})
    monkeypatch.setattr(userbot_rich_message, "send_rich_message", send_rich)

    result = await runtime_mod._run_interaction_userbot_action(
        AsyncMock(),
        {
            "action_type": "send_rich_message",
            "chat_id": -100333,
            "rich_message": {"html": "<h1>状态</h1>"},
        },
    )
    assert result["message_id"] == 45
    send_rich.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_interaction_userbot_action_edits_native_rich_message(monkeypatch) -> None:
    from app.services import userbot_rich_message
    from app.worker import runtime as runtime_mod

    edit_rich = AsyncMock(return_value={"message_id": 44, "chat_id": -100333})
    monkeypatch.setattr(userbot_rich_message, "edit_rich_message", edit_rich)

    result = await runtime_mod._run_interaction_userbot_action(
        AsyncMock(),
        {
            "action_type": "edit_message",
            "chat_id": -100333,
            "message_id": 44,
            "rich_message": {"html": "<h1>更新</h1>"},
        },
    )
    assert result["message_id"] == 44
    edit_rich.assert_awaited_once()


@pytest.mark.asyncio
async def test_deadline_proxy_executes_real_raw_rich_message_request(monkeypatch) -> None:
    from telethon.tl import functions, types

    from app.worker import runtime as runtime_mod

    class _RawClient:
        def __init__(self) -> None:
            self.requests: list[object] = []
            self.get_me_calls = 0

        async def get_me(self):  # noqa: ANN201
            self.get_me_calls += 1
            return SimpleNamespace(premium=True)

        async def get_input_entity(self, _peer):  # noqa: ANN001, ANN201
            return types.InputPeerChat(chat_id=333)

        async def __call__(self, request):  # noqa: ANN001, ANN201
            self.requests.append(request)
            if isinstance(request, functions.help.GetAppConfigRequest):
                return types.help.AppConfig(
                    hash=1,
                    config=types.JsonObject(
                        [
                            types.JsonObjectValue(
                                key="rich_message_posting",
                                value=types.JsonBool(True),
                            )
                        ]
                    ),
                )
            return SimpleNamespace(id=901)

    async def _allow(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(runtime_mod, "_acquire_interaction_userbot_rate_limit", _allow)
    client = _RawClient()
    proxy = runtime_mod._DeadlineClientProxy(
        client,
        IPCMessage(CMD_RUN_INTERACTION_ACTION, {"deadline_at_ms": int(time.time() * 1000) + 10_000}),
    )

    result = await runtime_mod._run_interaction_userbot_action(
        proxy,
        {
            "action_type": "send_rich_message",
            "chat_id": -100333,
            "rich_message": {"html": "<h1>状态</h1>"},
        },
    )
    second_result = await runtime_mod._run_interaction_userbot_action(
        runtime_mod._DeadlineClientProxy(
            client,
            IPCMessage(
                CMD_RUN_INTERACTION_ACTION,
                {"deadline_at_ms": int(time.time() * 1000) + 10_000},
            ),
        ),
        {
            "action_type": "send_rich_message",
            "chat_id": -100333,
            "rich_message": {"html": "<h1>第二条</h1>"},
        },
    )

    assert result["message_id"] == 901
    assert second_result["message_id"] == 901
    assert client.get_me_calls == 1
    assert sum(isinstance(request, functions.help.GetAppConfigRequest) for request in client.requests) == 1
    assert sum(isinstance(request, functions.messages.SendMessageRequest) for request in client.requests) == 2


@pytest.mark.asyncio
async def test_acquire_userbot_rate_limit_falls_back_to_local_bucket(monkeypatch):
    from app.worker import command as command_mod

    command_mod.reset_local_rate_limit_buckets()
    monkeypatch.setattr(command_mod, "_command_rate_limit_engine", AsyncMock(return_value=None))

    allowed, detail = await command_mod.acquire_userbot_action_rate_limit(9, "send_message", -1001)
    assert allowed is True
    assert detail["rate_limit_backend"] == "local_fallback"
    assert detail["outcome"] == "allowed"

    # 耗尽本地突发配额后应拒绝
    for _ in range(5):
        await command_mod.acquire_userbot_action_rate_limit(9, "send_message", -1001)
    allowed_after, detail_after = await command_mod.acquire_userbot_action_rate_limit(9, "send_message", -1001)
    assert allowed_after is False
    assert detail_after["rate_limit_backend"] == "local_fallback"
    assert detail_after["outcome"] == "rejected"
    command_mod.reset_local_rate_limit_buckets()


@pytest.mark.asyncio
async def test_acquire_userbot_rate_limit_payout_fail_closed_when_engine_missing(monkeypatch):
    from app.worker import command as command_mod

    command_mod.reset_local_rate_limit_buckets()
    monkeypatch.setattr(command_mod, "_command_rate_limit_engine", AsyncMock(return_value=None))

    allowed, detail = await command_mod.acquire_userbot_action_rate_limit(9, "payout", -1001)
    assert allowed is False
    assert detail["rate_limit_backend"] == "local_fallback"
    assert detail["outcome"] == "rejected"
    assert detail["reason"] == "distributed_rate_limit_unavailable"
    # payout 必须映射到发送桶名，不能落到无默认阈值的裸 "payout" 动作。
    assert command_mod.userbot_rate_limit_action("payout", -1001) == "send_message_group"
    assert command_mod.userbot_rate_limit_action("payout", 42) == "send_message_private"
    command_mod.reset_local_rate_limit_buckets()


@pytest.mark.asyncio
async def test_local_rate_limit_account_bucket_caps_cross_peer_burst(monkeypatch):
    from app.worker import command as command_mod

    command_mod.reset_local_rate_limit_buckets()
    monkeypatch.setattr(command_mod, "_command_rate_limit_engine", AsyncMock(return_value=None))

    allowed_count = 0
    for peer in range(100):
        allowed, _detail = await command_mod.acquire_userbot_action_rate_limit(
            9, "send_message", -(1000 + peer)
        )
        if allowed:
            allowed_count += 1
    # 账号级桶 capacity=8，不能靠切换 peer 无限放行。
    assert allowed_count <= int(command_mod._LOCAL_FALLBACK_ACCOUNT_CAPACITY)
    command_mod.reset_local_rate_limit_buckets()


@pytest.mark.asyncio
async def test_run_interaction_userbot_action_edit_caption_uses_saved_key_and_rate_limit():
    from app.worker import runtime as runtime_mod

    client = AsyncMock()
    client.edit_message = AsyncMock(return_value=SimpleNamespace(id=909))
    engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )

    class _Redis:
        async def get(self, key):
            assert key == "tp:msgid:55:dice_grid:round:1"
            return "707"

    result = await runtime_mod._run_interaction_userbot_action(
        client,
        {
            "action_type": "edit_caption",
            "chat_id": -100333,
            "message_id_key": "dice_grid:round:1",
            "caption": "<b>答对</b>",
            "parse_mode": "html",
        },
        account_id=55,
        engine=engine,
        redis=_Redis(),
    )

    engine.acquire.assert_awaited_once_with(55, "edit_message", peer_id=-100333)
    client.edit_message.assert_awaited_once_with(
        -100333,
        707,
        "<b>答对</b>",
        parse_mode="html",
    )
    assert result == {"message_id": 909, "chat_id": -100333}


@pytest.mark.asyncio
async def test_run_interaction_userbot_action_delete_and_pin_message():
    from app.worker import runtime as runtime_mod

    client = AsyncMock()
    client.delete_messages = AsyncMock(return_value=True)
    client.pin_message = AsyncMock(return_value=True)

    delete_result = await runtime_mod._run_interaction_userbot_action(
        client,
        {
            "action_type": "delete_message",
            "chat_id": -100333,
            "message_id": 44,
        },
        account_id=55,
    )
    pin_result = await runtime_mod._run_interaction_userbot_action(
        client,
        {
            "action_type": "pin_message",
            "chat_id": -100333,
            "message_id": 45,
        },
        account_id=55,
    )

    client.delete_messages.assert_awaited_once_with(-100333, [44])
    client.pin_message.assert_awaited_once_with(-100333, 45, notify=False)
    assert delete_result == {"message_id": 44, "chat_id": -100333}
    assert pin_result == {"message_id": 45, "chat_id": -100333}


@pytest.mark.asyncio
async def test_run_interaction_action_command_reports_reply_anchor_diagnostics():
    from app.worker import runtime as runtime_mod

    class _Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if False:
                    yield None

            return _gen()

        async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
            return SimpleNamespace(id=900)

    redis = _FakeCmdRedis()
    payload = {
        "action_type": "payout",
        "chat_id": -100333,
        "amount": 25,
        "parse_mode": "html",
        "reply_to_user_id": 111,
        "reply_to_search_limit": 20,
        "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言。<code>/airp list</code>",
    }

    client = _Client()

    await runtime_mod._handle_run_interaction_action_command(
        redis,
        client,
        55,
        IPCMessage(CMD_RUN_INTERACTION_ACTION, {"payload": payload}),
        "rpc-action",
    )

    channel, msg = await _wait_for_publish(
        redis,
        lambda channel, msg: channel == "rpc-action" and msg.type == CMD_RUN_INTERACTION_ACTION,
    )
    assert channel == "rpc-action"
    assert msg.payload["ok"] is False
    result = msg.payload["result"]
    assert result["chat_id"] == -100333
    assert result["amount"] == 25
    assert result["reply_to_message_id"] is None
    assert result["reply_to_user_id"] == 111
    assert result["reply_to_search_limit"] == 20
    assert result["error_code"] == "reply_anchor_missing"
    assert result["worker_offline"] is False
    assert result["reply_anchor_missing"] is True
    assert client.sent == [
        {
            "chat_id": -100333,
            "text": "没有找到 111 的近期发言。<code>/airp list</code>",
            "reply_to": None,
            "parse_mode": "html",
        }
    ]

    log_payload = json.loads(redis.logs[-1][1])
    assert log_payload["detail"]["chat_id"] == -100333
    assert log_payload["detail"]["amount"] == 25
    assert log_payload["detail"]["reply_to_user_id"] == 111
    assert log_payload["detail"]["reply_to_search_limit"] == 20
    assert log_payload["detail"]["error_code"] == "reply_anchor_missing"
    assert log_payload["detail"]["reply_anchor_missing"] is True


@pytest.mark.asyncio
async def test_run_interaction_action_command_can_suppress_reply_anchor_notice():
    from app.worker import runtime as runtime_mod

    class _Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if False:
                    yield None

            return _gen()

        async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
            return SimpleNamespace(id=900)

    redis = _FakeCmdRedis()
    client = _Client()

    await runtime_mod._handle_run_interaction_action_command(
        redis,
        client,
        55,
        IPCMessage(
            CMD_RUN_INTERACTION_ACTION,
            {
                "payload": {
                    "action_type": "payout",
                    "chat_id": -100333,
                    "amount": 25,
                    "reply_to_user_id": 111,
                    "reply_to_search_limit": 20,
                    "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言，无法发奖。",
                    "suppress_reply_anchor_missing_notice": True,
                }
            },
        ),
        "rpc-action",
    )

    channel, msg = await _wait_for_publish(
        redis,
        lambda channel, msg: channel == "rpc-action" and msg.type == CMD_RUN_INTERACTION_ACTION,
    )
    assert channel == "rpc-action"
    assert msg.payload["ok"] is False
    assert client.sent == []
    result = msg.payload["result"]
    assert result["error_code"] == "reply_anchor_missing"
    assert result["worker_offline"] is False
    assert result["reply_anchor_missing"] is True

    log_payload = json.loads(redis.logs[-1][1])
    assert log_payload["detail"]["error_code"] == "reply_anchor_missing"
    assert log_payload["detail"]["reply_anchor_missing"] is True


@pytest.mark.asyncio
async def test_dispatch_command_creates_trace(monkeypatch):
    """UserBot 命令分发必须产生 Trace，避免命令链路成为日志盲区。"""
    from app.worker import command as wcmd

    trace = event_trace.TraceContext(trace_id="evt_cmd", account_id=1, event_type="command")
    start_trace = AsyncMock(return_value=trace)
    record_span = AsyncMock()
    record_action = AsyncMock()
    finish_trace = AsyncMock()
    dispatch_event = MagicMock(side_effect=wcmd.dispatch_event)
    monkeypatch.setattr(wcmd, "_command_trace_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(wcmd, "start_trace", start_trace)
    monkeypatch.setattr(wcmd, "record_span", record_span)
    monkeypatch.setattr(wcmd, "record_action", record_action)
    monkeypatch.setattr(wcmd, "finish_trace", finish_trace)
    monkeypatch.setattr(wcmd, "dispatch_event", dispatch_event)

    client = AsyncMock()
    event = AsyncMock()
    event.raw_text = ",ping"
    event.message = SimpleNamespace(id=1, chat_id=10, sender_id=20, text=",ping")

    await wcmd._dispatch_command(client, event, "ping", "", account_id=1, help_prefix=",")

    start_trace.assert_awaited_once()
    assert start_trace.await_args.args[0]["source"]["type"] == "command"
    assert start_trace.await_args.args[0]["trigger"]["command"] == "ping"
    assert any(call.args[1] == "receive" for call in record_span.await_args_list)
    assert any(call.args[1] == "command_parse" for call in record_span.await_args_list)
    assert any(
        call.args[1] == "subscription_match"
        and call.kwargs.get("reason_code") == "command_matched"
        and call.kwargs.get("dispatch_mode") == "admin_command"
        and call.kwargs.get("event_bus_decisions", [{}])[0].get("matched") is True
        for call in record_span.await_args_list
    )
    dispatch_event.assert_called_once()
    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == "edit_message"
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"
    finish_trace.assert_awaited_once_with(trace, "ok")


@pytest.mark.asyncio
async def test_dispatch_plugin_command_creates_event_bus_decision(monkeypatch):
    """插件注册命令也必须经过 command decision 后再调用 handler。"""
    from app.worker import command as wcmd

    trace = event_trace.TraceContext(trace_id="evt_plugin_cmd", account_id=1, event_type="command")
    start_trace = AsyncMock(return_value=trace)
    record_span = AsyncMock()
    record_action = AsyncMock()
    finish_trace = AsyncMock()
    dispatch_event = MagicMock(side_effect=wcmd.dispatch_event)
    monkeypatch.setattr(wcmd, "_command_trace_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(wcmd, "start_trace", start_trace)
    monkeypatch.setattr(wcmd, "record_span", record_span)
    monkeypatch.setattr(wcmd, "record_action", record_action)
    monkeypatch.setattr(wcmd, "finish_trace", finish_trace)
    monkeypatch.setattr(wcmd, "dispatch_event", dispatch_event)

    async def handler(_client, event, args, _account_id):
        await event.edit(f"plugin ok {' '.join(args)}")

    command_name = "demo_plugin_cmd"
    wcmd.register_plugin_command(command_name, handler, owner_plugin_key="demo_plugin", generation=1)
    try:
        client = AsyncMock()
        event = AsyncMock()
        event.raw_text = f",{command_name} alpha"
        event.message = SimpleNamespace(id=1, chat_id=10, sender_id=20, text=f",{command_name} alpha")

        await wcmd._dispatch_command(client, event, command_name, "alpha", account_id=1, help_prefix=",")
    finally:
        wcmd.unregister_plugin_command(command_name, owner_plugin_key="demo_plugin")

    dispatch_event.assert_called_once()
    assert start_trace.await_args.args[0]["trigger"]["plugin_key"] == "demo_plugin"
    assert any(
        call.args[1] == "subscription_match"
        and call.kwargs.get("plugin_key") == "demo_plugin"
        and call.kwargs.get("event_bus_decisions", [{}])[0].get("plugin_key") == "demo_plugin"
        and call.kwargs.get("event_bus_decisions", [{}])[0].get("matched") is True
        for call in record_span.await_args_list
    )
    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == "edit_message"
    assert record_action.await_args.args[1]["plugin_key"] == "demo_plugin"
    finish_trace.assert_awaited_once_with(trace, "ok")


@pytest.mark.asyncio
async def test_trace_command_client_pin_message_records_action(monkeypatch):
    """命令 handler 通过 client 置顶消息也必须落 event_action。"""
    from app.worker import command as wcmd

    trace = event_trace.TraceContext(trace_id="evt_cmd_pin", account_id=1, event_type="command")
    record_action = AsyncMock()
    monkeypatch.setattr(wcmd, "record_action", record_action)
    raw_client = AsyncMock()
    raw_client.pin_message = AsyncMock(return_value=SimpleNamespace(id=55))
    traced_client = wcmd._TraceCommandClient(raw_client, trace, command="pin", plugin_key="demo")

    await traced_client.pin_message(-100, 55, notify=False)

    raw_client.pin_message.assert_awaited_once_with(-100, 55, notify=False)
    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == "pin_message"
    assert record_action.await_args.args[1]["message_id"] == 55
    assert record_action.await_args.args[2] == "ok"
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"


# ════════════════════════════════════════════════════════════
# 命令前缀热加载：handler 应每次消息从 ctx 读 prefix
# 见 worker/command.py:make_command_handler
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handler_uses_dynamic_prefix_from_ctx():
    """改 ctx.command_prefix 后，**已注册** 的 handler 下一条消息就要按新前缀匹配。

    回归用例：以前 prefix 是闭包里固定 pattern，改系统设置不会生效。
    """
    from app.worker.command import make_command_handler

    # 用 MagicMock 而非真 TelegramClient；只关心 .on(...) 装饰器是否能拿到 handler
    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on

    # 注册 handler；初始 prefix 闭包默认 ","
    make_command_handler(client, account_id=1, prefix=",")
    handler = captured["fn"]

    # ctx 用 "-" 前缀，模拟用户在 web 上把前缀改成 "-"
    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix="-",
        )
    )

    # 发一条 "-ping"——按新 prefix 应该命中 ping，event.edit 被调用为 "pong"
    event = AsyncMock()
    event.raw_text = "-ping"
    await handler(event)
    event.edit.assert_called_with("pong")

    # 发一条 ",ping" 用旧 prefix——不应匹配新 pattern，handler 直接 return；
    # event.edit 不会被调用
    event2 = AsyncMock()
    event2.raw_text = ",ping"
    await handler(event2)
    event2.edit.assert_not_called()

    # 发一条 "-bogus"——已用新前缀但是未知命令；提示里要含新前缀 "-help"
    event3 = AsyncMock()
    event3.raw_text = "-bogus"
    await handler(event3)
    msg = event3.edit.call_args[0][0]
    assert "未知命令" in msg
    assert "-help" in msg  # 提示用新前缀，不是 ",help"


@pytest.mark.asyncio
async def test_outgoing_prefix_plugin_command_runs_for_account_owner():
    """账号本人发系统前缀插件命令时，应进入 userbot 插件命令链路。"""
    from app.worker import command as wcmd
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    plugin_handler = AsyncMock()
    wcmd.register_plugin_command("10d", plugin_handler, owner_plugin_key="ten_half", generation=1)
    try:
        client = MagicMock()
        client.on = fake_on
        make_command_handler(client, account_id=1, prefix="。")
        handler = captured["fn"]
        set_command_context(
            CommandContext(
                account_id=1,
                templates={},
                providers={},
                command_prefix="。",
            )
        )

        event = AsyncMock()
        event.raw_text = "。10d 6789"
        await handler(event)

        plugin_handler.assert_awaited_once()
        assert plugin_handler.await_args.args[2] == ["6789"]
        assert plugin_handler.await_args.args[3] == 1
    finally:
        wcmd.unregister_plugin_command("10d", owner_plugin_key="ten_half")


@pytest.mark.asyncio
async def test_outgoing_bare_command_requires_setting_to_be_disabled():
    """默认必须带系统前缀，账号本人裸命令也不会触发。"""
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix="。")
    handler = captured["fn"]
    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix="。",
            command_prefix_required=True,
        )
    )

    event = AsyncMock()
    event.raw_text = "ping"
    await handler(event)
    event.edit.assert_not_called()


@pytest.mark.asyncio
async def test_outgoing_bare_command_runs_when_prefix_not_required():
    """关闭必须带前缀后，仅账号本人 outgoing 裸命令可触发已有命令。"""
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix="。")
    handler = captured["fn"]
    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix="。",
            command_prefix_required=False,
        )
    )

    event = AsyncMock()
    event.raw_text = "ping"
    await handler(event)
    event.edit.assert_called_with("pong")

    unknown = AsyncMock()
    unknown.raw_text = "普通聊天"
    await handler(unknown)
    unknown.edit.assert_not_called()


@pytest.mark.asyncio
async def test_outgoing_bare_plugin_command_runs_when_prefix_not_required():
    """关闭必须带前缀后，账号本人可裸写插件注册命令。"""
    from app.worker import command as wcmd
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    plugin_handler = AsyncMock()
    wcmd.register_plugin_command("10d", plugin_handler, owner_plugin_key="ten_half", generation=1)
    try:
        client = MagicMock()
        client.on = fake_on
        make_command_handler(client, account_id=1, prefix="。")
        handler = captured["fn"]
        set_command_context(
            CommandContext(
                account_id=1,
                templates={},
                providers={},
                command_prefix="。",
                command_prefix_required=False,
            )
        )

        event = AsyncMock()
        event.raw_text = "10d 6789"
        await handler(event)

        plugin_handler.assert_awaited_once()
        assert plugin_handler.await_args.args[2] == ["6789"]
    finally:
        wcmd.unregister_plugin_command("10d", owner_plugin_key="ten_half")


@pytest.mark.asyncio
async def test_outgoing_bare_hyphenated_plugin_command_bypasses_echo_guard():
    """关闭前缀后，裸插件命令即使与近期消息同文也应按管理员命令执行。"""
    from app.worker import command as wcmd
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    class Client:
        on = staticmethod(fake_on)

        async def iter_messages(self, chat_id, *, limit, max_id):
            yield SimpleNamespace(raw_text="airp-7", sender_id=10001, out=False)

    plugin_handler = AsyncMock()
    wcmd.register_plugin_command("airp-7", plugin_handler, owner_plugin_key="ai_redpacket", generation=1)
    try:
        make_command_handler(Client(), account_id=1, prefix="。")
        set_command_context(
            CommandContext(
                account_id=1,
                templates={},
                providers={},
                command_prefix="。",
                command_prefix_required=False,
                self_tg_user_id=42,
            )
        )

        event = AsyncMock()
        event.raw_text = "airp-7"
        event.chat_id = -100123
        event.id = 50
        event.is_private = False
        await captured["fn"](event)

        plugin_handler.assert_awaited_once()
        assert plugin_handler.await_args.args[2] == []
    finally:
        wcmd.unregister_plugin_command("airp-7", owner_plugin_key="ai_redpacket")


@pytest.mark.asyncio
async def test_handler_falls_back_when_ctx_missing():
    """ctx 为空时（worker 启动早期）handler 应用闭包 fallback prefix 工作。"""
    from app.worker import command as wcmd
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix=";")
    handler = captured["fn"]

    # 模拟 ctx 还没初始化
    wcmd._ctx = None  # type: ignore[attr-defined]
    try:
        event = AsyncMock()
        event.raw_text = ";ping"
        await handler(event)
        event.edit.assert_called_with("pong")
    finally:
        # 恢复一个空 ctx，避免影响其它测试
        wcmd._ctx = CommandContext(
            account_id=1, templates={}, providers={}, command_prefix=","
        )


@pytest.mark.asyncio
async def test_repeated_global_prefix_is_silent():
    """全局命令前缀后仍是前缀时静默，不提示未知命令。"""
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix="。")
    handler = captured["fn"]

    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix="。",
        )
    )

    event = AsyncMock()
    event.raw_text = "。。。"
    await handler(event)
    event.edit.assert_not_called()

    event2 = AsyncMock()
    event2.raw_text = "。ping"
    await handler(event2)
    event2.edit.assert_called_with("pong")


@pytest.mark.asyncio
async def test_outgoing_pure_command_echo_is_skipped_in_group(monkeypatch):
    """群里前几条有人发过同样纯命令时，自己的回声消息应视为抽奖/接龙，不触发。"""
    from app.worker.command import make_command_handler

    captured = {}

    def fake_on(_event_type):
        def deco(fn):
            captured["fn"] = fn
            return fn

        return deco

    class Client:
        on = staticmethod(fake_on)

        async def iter_messages(self, chat_id, *, limit, max_id):
            assert chat_id == -100123
            assert limit == 8
            assert max_id == 50
            yield SimpleNamespace(raw_text="。ai", sender_id=10001, out=False)

    set_command_context(
        CommandContext(
            account_id=1,
            templates={"ai": {"name": "ai", "type": "reply_text", "config": {"text": "ok"}}},
            providers={},
            command_prefix="。",
            self_tg_user_id=42,
        )
    )
    make_command_handler(Client(), account_id=1, prefix="。")
    handler = captured["fn"]

    event = AsyncMock()
    event.raw_text = "。ai"
    event.chat_id = -100123
    event.id = 50
    event.is_private = False

    await handler(event)

    event.edit.assert_not_called()


@pytest.mark.asyncio
async def test_outgoing_command_with_args_bypasses_echo_guard():
    class Client:
        def __init__(self) -> None:
            self.checked = False

        async def iter_messages(self, *_args, **_kwargs):
            self.checked = True
            yield SimpleNamespace(raw_text="。ai", sender_id=10001, out=False)

    event = SimpleNamespace(raw_text="。ai 帮我总结", chat_id=-100123, id=51, is_private=False)
    client = Client()

    skipped = await should_skip_outgoing_command_echo(client, event, "。ai 帮我总结", "帮我总结")

    assert skipped is False
    assert client.checked is False


def test_command_context_has_command_prefix_field():
    """守门测试：CommandContext 必须有 command_prefix 字段且默认 ","。"""
    ctx = CommandContext(account_id=1, templates={}, providers={})
    assert ctx.command_prefix == ","
    assert ctx.command_prefix_required is True
    ctx2 = CommandContext(
        account_id=1,
        templates={},
        providers={},
        command_prefix="-",
        command_prefix_required=False,
    )
    assert ctx2.command_prefix == "-"
    assert ctx2.command_prefix_required is False


def test_re_escape_special_prefix():
    """守门测试：handler 内对 prefix 用 ``re.escape``，所以特殊字符（如 ``.``）也安全。"""
    # 模拟 handler 里那条 pattern 编译；以前出过 bug 让点 = 任意字符
    p = "."
    pat = re.compile(rf"^{re.escape(p)}(\w+)(?:\s+(.*))?$", re.S)
    assert pat.match(".ping")
    # ``aping`` 不应该命中（如果没 escape，"." 会匹配 "a"）
    assert not pat.match("aping")


def test_low_risk_commands_still_registered_and_high_risk_removed():
    """守门测试：低风险命令仍注册；高危入口已移除。"""
    for name in (
        "help",
        "status",
        "ping",
        "id",
        "version",
        "del",
        "pause",
        "resume",
        "restart",
        "sudo",
    ):
        assert name in _BUILTIN
    assert "reboot" not in _BUILTIN
    assert "rb" not in _BUILTIN
    assert "plugin" not in _BUILTIN


@pytest.mark.asyncio
async def test_help_hides_removed_high_risk_commands():
    """help 不应展示已删除高危命令。"""
    client = AsyncMock()
    event = AsyncMock()
    await _BUILTIN["help"].handler(client, event, [], 1)
    msg = event.edit.call_args[0][0]
    assert "reboot" not in msg
    assert "rb" not in msg
    assert "plugin" not in msg
    assert "sudo add" not in msg
    assert "sudo del" not in msg
    assert "restart" in msg


def test_parse_command_key_from_text() -> None:
    assert parse_command_key_from_text("。测试", "。") == "测试"
    assert parse_command_key_from_text("。测试 参数", "。") == "测试"
    assert parse_command_key_from_text("测试", "。") is None


def test_should_allow_auto_command_text_by_whitelist() -> None:
    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix="。",
            scheduler_command_whitelist=["测试"],
        )
    )
    allowed, key = should_allow_auto_command_text("。测试")
    assert allowed is True
    assert key == "测试"

    denied, denied_key = should_allow_auto_command_text("。帮助")
    assert denied is False
    assert denied_key == "帮助"


def test_should_block_auto_command_text_when_ctx_missing() -> None:
    from app.worker import command as wcmd

    old_ctx = wcmd._ctx  # type: ignore[attr-defined]
    wcmd._ctx = None  # type: ignore[attr-defined]
    try:
        allowed, key = should_allow_auto_command_text(",help")
        assert allowed is False
        assert key == "help"
    finally:
        wcmd._ctx = old_ctx  # type: ignore[attr-defined]
