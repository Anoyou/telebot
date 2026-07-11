"""账号绑定 Bot 联动系统的关键安全单测。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.account_bot_defaults import DEFAULT_TRANSFER_NOTICE_TEMPLATE, LEGACY_TRANSFER_NOTICE_TEMPLATE
from app.api import account_bots
from app.db.models.account import Account
from app.db.models.account_bot import AccountBot
from app.db.models.log import RuntimeLog
from app.schemas.account_bot import AccountBotConfigUpdate, AccountBotInteractionConfig, AccountBotTestRequest
from app.services import account_bot_runtime, account_bot_service, audit
from app.services.interaction import contracts as interaction_contracts
from app.services.interaction.delivery import (
    InteractionDeliveryExecutor,
    action_save_message_id_key,
    namespaced_action_save_message_id_key,
    save_action_reply_target,
)
from app.worker import runtime as worker_runtime
from app.worker.plugins import loader as plugin_loader
from app.worker.plugins.manifest import Manifest
from app.worker.plugins.message_ops import BufferedMessageOps
from app.worker.plugins.textutil import html_escape
from app.worker.ratelimit.humanize import HumanizeOpts


class _MemoryRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.set_calls: list[tuple[str, str, dict[str, object]]] = []

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        if kwargs.get("nx") and key in self.data:
            return False
        self.set_calls.append((key, value, dict(kwargs)))
        self.data[key] = value
        return True

    async def delete(self, *keys: str):
        deleted = 0
        for key in keys:
            deleted += 1 if self.data.pop(key, None) is not None else 0
        return deleted

    async def keys(self, pattern: str):
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return [key for key in self.data if key.startswith(prefix)]
        return [key for key in self.data if key == pattern]

    async def rpush(self, key: str, value: str | bytes):
        self.data.setdefault(key, "")
        return 1

    async def hset(self, key: str, field: str, value: str):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    async def hdel(self, key: str, *fields: str):
        bucket = self.hashes.setdefault(key, {})
        deleted = 0
        for field in fields:
            deleted += 1 if bucket.pop(field, None) is not None else 0
        return deleted

    async def zadd(self, key: str, mapping: dict[str, float]):
        bucket = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            added += 0 if member in bucket else 1
            bucket[member] = float(score)
        return added

    async def zcard(self, key: str):
        return len(self.zsets.get(key, {}))

    async def zrange(self, key: str, start: int, end: int):
        members = [
            member
            for member, _score in sorted(self.zsets.get(key, {}).items(), key=lambda item: (item[1], item[0]))
        ]
        return members[start:end + 1 if end >= 0 else None]

    async def zrevrange(self, key: str, start: int, end: int):
        members = [
            member
            for member, _score in sorted(
                self.zsets.get(key, {}).items(),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )
        ]
        return members[start:end + 1 if end >= 0 else None]

    async def zrem(self, key: str, *members: str):
        bucket = self.zsets.setdefault(key, {})
        deleted = 0
        for member in members:
            deleted += 1 if bucket.pop(member, None) is not None else 0
        return deleted


class _InteractionRuntimeTestResult:
    def __init__(self, feature_keys: list[str] | None = None) -> None:
        self.feature_keys = list(feature_keys or [])

    def scalars(self):
        return self

    def all(self):
        return [SimpleNamespace(feature_key=feature_key) for feature_key in self.feature_keys]

    def scalar_one_or_none(self):
        return None


class _InteractionRuntimeTestDB:
    def __init__(self, *, feature_keys: list[str] | None = None) -> None:
        self.feature_keys = list(feature_keys or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, _stmt):  # noqa: ANN001
        return _InteractionRuntimeTestResult(self.feature_keys)

    async def get(self, model, *_args):  # noqa: ANN002
        if model is Account:
            return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
        return None

    async def commit(self):
        return None


def _interaction_test_message(
    update_id: int,
    text: str,
    *,
    user_id: int = 111,
    first_name: str = "Alice",
    chat_id: int = -100123,
    message_id: int | None = None,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id * 10,
            "text": text,
            "from": {"id": user_id, "first_name": first_name},
            "chat": {"id": chat_id, "type": "supergroup"},
        },
    }


def _interaction_payment_message(
    update_id: int,
    *,
    payer_id: int,
    payer_name: str,
    amount: int = 100,
    receiver: str = "BBB",
    chat_id: int = -100123,
) -> dict[str, object]:
    return _interaction_test_message(
        update_id,
        f"转账成功\n付款人：{payer_name}\n付款人ID：{payer_id}\n收款人：{receiver}\n金额：{amount}",
        user_id=456,
        first_name="TransferBot",
        chat_id=chat_id,
    )


@pytest.fixture(autouse=True)
def _clear_transfer_command_dedupe():
    account_bot_runtime._TRANSFER_COMMAND_DEDUPE.clear()
    account_bot_runtime._EVENT_BUS_KNOWN_USERS_CACHE.clear()
    account_bot_runtime._INTERACTION_ROUTING_INDEX_CACHE.clear()
    yield
    account_bot_runtime._TRANSFER_COMMAND_DEDUPE.clear()
    account_bot_runtime._EVENT_BUS_KNOWN_USERS_CACHE.clear()
    account_bot_runtime._INTERACTION_ROUTING_INDEX_CACHE.clear()


def _patch_math10_worker_entry(monkeypatch) -> AsyncMock:
    run_entry = AsyncMock(return_value=(True, None, [{"type": "result", "success": True}]))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    return run_entry


def _assert_math10_worker_entry(run_entry: AsyncMock, *, prize: int = 456) -> None:
    assert run_entry.await_count == 1
    assert run_entry.await_args.kwargs["plugin_key"] == "math10"
    assert run_entry.await_args.kwargs["entry_key"] == "start_math10"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["prize"] == prize


@pytest.mark.asyncio
async def test_worker_pubsub_idle_timeout_is_treated_as_no_message() -> None:
    class _PubSub:
        async def get_message(self, **_kwargs):  # noqa: ANN003
            raise TimeoutError("idle")

    assert await worker_runtime._next_pubsub_message(_PubSub()) is None


@pytest.mark.asyncio
async def test_userbot_interaction_action_resolves_reply_to_user_recent_message() -> None:
    class _Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def iter_messages(self, chat_id, **kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if chat_id != -100:
                    return
                if kwargs.get("from_user") == 111:
                    yield SimpleNamespace(id=77, sender_id=111, text="我参加")

            return _gen()

        async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
            return SimpleNamespace(id=88)

    client = _Client()

    result = await worker_runtime._run_interaction_userbot_action(
        client,
        {
            "action_type": "send_message",
            "chat_id": -100,
            "text": "+66",
            "reply_to_user_id": 111,
            "reply_to_search_limit": 20,
        },
    )

    assert client.sent == [{"chat_id": -100, "text": "+66", "reply_to": 77, "parse_mode": None}]
    assert result["message_id"] == 88
    assert result["reply_to_message_id"] == 77
    assert result["reply_to_user_id"] == 111


@pytest.mark.asyncio
async def test_userbot_interaction_action_humanize_runs_before_send_when_enabled(monkeypatch) -> None:
    order: list[str] = []

    async def fake_send_message(chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        order.append("send")
        return SimpleNamespace(id=89)

    async def fake_read(client, peer, opts):  # noqa: ANN001, ANN003
        order.append("read")
        assert peer == -100
        assert opts.typing_simulate is True
        assert client is client_obj

    async def fake_typing(client, peer, opts):  # noqa: ANN001, ANN003
        order.append("typing")
        assert peer == -100
        assert opts.read_before_reply is True
        assert client is client_obj

    client_obj = SimpleNamespace(send_message=AsyncMock(side_effect=fake_send_message))
    engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock(side_effect=fake_read)
    simulate_typing = AsyncMock(side_effect=fake_typing)
    monkeypatch.setattr(worker_runtime, "simulate_read", simulate_read)
    monkeypatch.setattr(worker_runtime, "simulate_typing", simulate_typing)

    result = await worker_runtime._run_interaction_userbot_action(
        client_obj,
        {
            "action_type": "send_message",
            "chat_id": -100,
            "text": "humanized",
        },
        account_id=78,
        engine=engine,
        redis=_MemoryRedis(),
    )

    assert result["message_id"] == 89
    assert order == ["read", "typing", "send"]
    simulate_read.assert_awaited_once()
    simulate_typing.assert_awaited_once()


@pytest.mark.asyncio
async def test_userbot_interaction_action_humanize_skips_when_switches_disabled(monkeypatch) -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=90)))
    engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=False, typing_simulate=False),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(worker_runtime, "simulate_read", simulate_read)
    monkeypatch.setattr(worker_runtime, "simulate_typing", simulate_typing)

    await worker_runtime._run_interaction_userbot_action(
        client,
        {
            "action_type": "send_message",
            "chat_id": -100,
            "text": "no humanize",
        },
        account_id=78,
        engine=engine,
        redis=_MemoryRedis(),
    )

    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    client.send_message.assert_awaited_once_with(-100, "no humanize", reply_to=None, parse_mode=None)


@pytest.mark.asyncio
async def test_userbot_interaction_action_payout_skips_humanize(monkeypatch) -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=91)))
    engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(worker_runtime, "simulate_read", simulate_read)
    monkeypatch.setattr(worker_runtime, "simulate_typing", simulate_typing)
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))

    await worker_runtime._run_interaction_userbot_action(
        client,
        {
            "action_type": "payout",
            "chat_id": -100,
            "amount": 7,
            "text": "+7",
        },
        account_id=78,
        engine=engine,
        redis=_MemoryRedis(),
    )

    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    client.send_message.assert_awaited_once_with(-100, "+7", reply_to=None, parse_mode=None)


@pytest.mark.asyncio
async def test_userbot_interaction_action_settlement_send_skips_humanize(monkeypatch) -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=92)))
    engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(worker_runtime, "simulate_read", simulate_read)
    monkeypatch.setattr(worker_runtime, "simulate_typing", simulate_typing)

    await worker_runtime._run_interaction_userbot_action(
        client,
        {
            "action_type": "send_message",
            "chat_id": -100,
            "text": "结算公告",
            "settlement": {"amount": 8, "winner_user_id": 111},
        },
        account_id=78,
        engine=engine,
        redis=_MemoryRedis(),
    )

    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    client.send_message.assert_awaited_once_with(-100, "结算公告", reply_to=None, parse_mode=None)


@pytest.mark.asyncio
async def test_userbot_interaction_action_falls_back_to_sender_scan_for_reply_anchor() -> None:
    class _Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def iter_messages(self, chat_id, **kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if kwargs.get("from_user") is not None:
                    raise RuntimeError("entity lookup failed")
                if chat_id == -100:
                    yield SimpleNamespace(id=80, sender_id=222, text="别人")
                    yield SimpleNamespace(id=79, sender_id=111, text="历史发言")

            return _gen()

        async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
            self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
            return SimpleNamespace(id=88)

    client = _Client()

    result = await worker_runtime._run_interaction_userbot_action(
        client,
        {
            "action_type": "send_message",
            "chat_id": -100,
            "text": "+66",
            "reply_to_user_id": 111,
            "reply_to_search_limit": 20,
        },
    )

    assert client.sent == [{"chat_id": -100, "text": "+66", "reply_to": 79, "parse_mode": None}]
    assert result["reply_to_message_id"] == 79


@pytest.mark.asyncio
async def test_userbot_interaction_action_fails_when_reply_anchor_missing() -> None:
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
            return SimpleNamespace(id=88)

    client = _Client()

    with pytest.raises(ValueError, match="找不到用户 111"):
        await worker_runtime._run_interaction_userbot_action(
            client,
            {
                "action_type": "send_message",
                "chat_id": -100,
                "text": "+66",
                "reply_to_user_id": 111,
                "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言，无法发奖。",
            },
        )

    assert client.sent == [
        {
            "chat_id": -100,
            "text": "没有找到 111 的近期发言，无法发奖。",
            "reply_to": None,
            "parse_mode": None,
        }
    ]


class _IPCRecordingRedis:
    """记录 rpush（运行日志）与 publish（IPC 回包）的极简假 Redis。"""

    def __init__(self) -> None:
        self.logs: list[str | bytes] = []
        self.published: list[tuple[str, str]] = []

    async def rpush(self, _key: str, value: str | bytes):
        self.logs.append(value)
        return len(self.logs)

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


def _decode_action_reply(redis: _IPCRecordingRedis) -> dict[str, object]:
    _channel, raw = redis.published[-1]
    return worker_runtime.IPCMessage.decode(raw).payload


@pytest.mark.asyncio
async def test_run_interaction_action_command_payout_over_limit_reports_error_code(monkeypatch) -> None:
    redis = _IPCRecordingRedis()

    class _Client:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id, text, **_kwargs):  # noqa: ANN001, ANN003
            self.sent.append((chat_id, text))
            return SimpleNamespace(id=1)

    client = _Client()
    monkeypatch.setattr(
        worker_runtime,
        "_check_payout_limit",
        AsyncMock(return_value=(False, "payout 日累计上限超限：今日已用 900，本笔 500，日累计上限 1000。")),
    )

    cmd = worker_runtime.IPCMessage(
        type=worker_runtime.CMD_RUN_INTERACTION_ACTION,
        payload={"payload": {"action_type": "payout", "chat_id": -100, "amount": 500, "text": "+500"}},
    )
    await worker_runtime._handle_run_interaction_action_command(redis, client, 91, cmd, "reply-ch")

    # 超限拦在发送前，userbot 不应发出 "+金额"
    assert client.sent == []
    reply = _decode_action_reply(redis)
    assert reply["ok"] is False
    assert reply["result"]["error_code"] == "payout_limit_exceeded"


@pytest.mark.asyncio
async def test_run_interaction_action_command_floodwait_feeds_engine(monkeypatch) -> None:
    from telethon.errors import FloodWaitError

    flood = FloodWaitError(30)
    flood.seconds = 30  # telethon 构造不吃秒数，显式赋值

    redis = _IPCRecordingRedis()

    class _Client:
        async def send_message(self, *_a, **_k):  # noqa: ANN002, ANN003
            raise flood

    engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
        on_flood_wait=AsyncMock(),
        on_peer_flood=AsyncMock(),
    )
    plugin_loader._STATES[91] = SimpleNamespace(engine=engine)
    try:
        cmd = worker_runtime.IPCMessage(
            type=worker_runtime.CMD_RUN_INTERACTION_ACTION,
            payload={"payload": {"action_type": "send_message", "chat_id": -100, "text": "hi"}},
        )
        await worker_runtime._handle_run_interaction_action_command(redis, _Client(), 91, cmd, "reply-ch")
    finally:
        plugin_loader._STATES.pop(91, None)

    engine.on_flood_wait.assert_awaited_once()
    assert engine.on_flood_wait.await_args.args[0] == "send_message_group"
    engine.on_peer_flood.assert_not_awaited()
    reply = _decode_action_reply(redis)
    assert reply["result"]["flood_wait_seconds"] == 30
    assert reply["result"]["error_code"] == "telegram_api_error"


def test_account_bot_config_response_hides_plain_token() -> None:
    """配置出参只暴露 has_token，不返回明文 token 或加密串。"""

    row = AccountBot(account_id=1, bot_token_enc="encrypted-placeholder")
    out = account_bot_service.config_to_response(row)

    assert out.has_token is True
    assert "token" not in out.model_dump()
    assert out.remote_plugin_policy.enabled is False
    assert out.remote_plugin_policy.install is False


def test_disabled_or_cleared_management_bot_hides_stale_conflict_error() -> None:
    row = AccountBot(
        account_id=1,
        enabled=False,
        status="disabled",
        bot_token_enc=None,
        last_error="Conflict: terminated by other getUpdates request",
    )

    out = account_bot_service.config_to_response(row)

    assert out.enabled is False
    assert out.has_token is False
    assert out.last_error is None


def test_account_bot_role_matrix() -> None:
    """viewer/operator/admin 权限必须逐级包含。"""

    assert account_bot_service.role_allows("viewer", "viewer") is True
    assert account_bot_service.role_allows("viewer", "operator") is False
    assert account_bot_service.role_allows("operator", "viewer") is True
    assert account_bot_service.role_allows("operator", "admin") is False
    assert account_bot_service.role_allows("admin", "operator") is True


def test_account_bot_callback_data_parser() -> None:
    """callback data 必须绑定 aid/action/resource/nonce。"""

    assert account_bot_runtime._parse_callback("ab:12:feature_toggle:game24") == (
        12,
        "feature_toggle",
        "game24",
        None,
    )
    assert account_bot_runtime._parse_callback("ab:12:confirm:restart:n1") == (
        12,
        "confirm",
        "restart",
        "n1",
    )
    assert account_bot_runtime._parse_callback("bad:12:confirm:restart") is None


def test_account_bot_error_sanitizer_masks_token() -> None:
    token = "123456:secret-token"
    text = account_bot_service.sanitize_bot_error(
        f"https://api.telegram.org/bot{token}/sendMessage failed at /Users/me/project/file.py",
        token=token,
    )
    assert token not in text
    assert "api.telegram.org" not in text
    assert "/Users/me" not in text


def test_bot_polling_conflict_error_mentions_role() -> None:
    clean = "Conflict: terminated by other getUpdates request"

    management = account_bot_service.label_bot_polling_error(clean, role="management")
    interaction = account_bot_service.label_bot_polling_error(clean, role="interaction")

    assert "管理 Bot polling 冲突" in management
    assert "交互 Bot polling 冲突" in interaction
    assert "Bbot token" in interaction


def test_interaction_delivery_save_key_keeps_runtime_constraints() -> None:
    assert action_save_message_id_key("abc:123.ok") == "abc:123.ok"
    assert namespaced_action_save_message_id_key(7, "abc:123.ok") == "tp:msgid:7:abc:123.ok"
    assert action_save_message_id_key("bad key") is None
    assert action_save_message_id_key("." * 201) is None


def test_plugin_textutil_html_escape_reuses_account_bot_escape() -> None:
    assert html_escape("<b>Tom & Jerry</b>") == "&lt;b&gt;Tom &amp; Jerry&lt;/b&gt;"


@pytest.mark.asyncio
async def test_account_bot_send_message_plain_omits_parse_mode(monkeypatch) -> None:
    call_bot_api = AsyncMock(return_value={"message_id": 1})
    monkeypatch.setattr(account_bot_service, "call_bot_api", call_bot_api)

    await account_bot_service.send_message("123:token", -100, "<开奖>", parse_mode="plain")

    assert "parse_mode" not in call_bot_api.await_args.args[2]
    assert call_bot_api.await_args.args[2]["text"] == "<开奖>"


@pytest.mark.asyncio
async def test_account_bot_send_message_html_truncates_without_breaking_entity_or_tag(monkeypatch) -> None:
    call_bot_api = AsyncMock(return_value={"message_id": 1})
    monkeypatch.setattr(account_bot_service, "call_bot_api", call_bot_api)

    html_text = "<b>" + ("A" * 3999) + "&amp;X</b>"
    await account_bot_service.send_message("123:token", -100, html_text, parse_mode="html")

    payload = call_bot_api.await_args.args[2]
    assert payload["parse_mode"] == "HTML"
    assert payload["text"].endswith("&amp;</b>")
    assert "X</b>" not in payload["text"]


@pytest.mark.asyncio
async def test_account_bot_bot_api_client_is_reused_and_closable(monkeypatch) -> None:
    await account_bot_service.close_bot_api_client()

    class _Response:
        status_code = 200
        content = b'{"ok": true, "result": {"message_id": 1}}'

        def json(self):  # noqa: ANN201
            return {"ok": True, "result": {"message_id": 1}}

    class _Client:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.posts: list[dict[str, object]] = []
            self.closed = False

        @property
        def is_closed(self) -> bool:
            return self.closed

        async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
            self.posts.append({"url": url, **kwargs})
            return _Response()

        async def aclose(self) -> None:
            self.closed = True

    clients: list[_Client] = []

    def _factory(*args, **kwargs):  # noqa: ANN002, ANN003
        client = _Client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(account_bot_service.httpx, "AsyncClient", _factory)

    await account_bot_service.call_bot_api("123:token", "getMe", {})
    await account_bot_service.send_photo_bytes("123:token", -100, b"img", filename="a.png")

    assert len(clients) == 1
    assert len(clients[0].posts) == 2

    await account_bot_service.close_bot_api_client()

    assert clients[0].closed is True


@pytest.mark.asyncio
async def test_account_bot_service_sends_document_with_reply_markup(monkeypatch) -> None:
    class _Response:
        status_code = 200
        content = b'{"ok": true, "result": {"message_id": 88}}'

        def json(self):  # noqa: ANN201
            return {"ok": True, "result": {"message_id": 88}}

    class _Client:
        def __init__(self) -> None:
            self.posts: list[dict[str, object]] = []

        async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
            self.posts.append({"url": url, **kwargs})
            return _Response()

    client = _Client()
    monkeypatch.setattr(account_bot_service, "get_bot_api_client", AsyncMock(return_value=client))

    result = await account_bot_service.send_document_bytes(
        "123:token",
        -100,
        b"file-bytes",
        filename="round.txt",
        caption="<b>结果</b>",
        reply_to_message_id=7,
        reply_markup={"inline_keyboard": []},
        parse_mode="html",
    )

    assert result == {"message_id": 88}
    assert client.posts[0]["url"].endswith("/bot123:token/sendDocument")
    assert client.posts[0]["files"] == {"document": ("round.txt", b"file-bytes")}
    assert client.posts[0]["data"] == {
        "chat_id": -100,
        "caption": "<b>结果</b>",
        "parse_mode": "HTML",
        "reply_to_message_id": 7,
        "allow_sending_without_reply": True,
        "reply_markup": json.dumps({"inline_keyboard": []}, ensure_ascii=False),
    }


@pytest.mark.asyncio
async def test_account_bot_service_edits_media_caption(monkeypatch) -> None:
    call_bot_api = AsyncMock(return_value={"message_id": 77})
    monkeypatch.setattr(account_bot_service, "call_bot_api", call_bot_api)

    result = await account_bot_service.edit_message_caption(
        "123:token",
        -100,
        77,
        "<b>新 caption</b>",
        parse_mode="html",
        reply_markup={"inline_keyboard": []},
    )

    assert result == {"message_id": 77}
    call_bot_api.assert_awaited_once_with(
        "123:token",
        "editMessageCaption",
        {
            "chat_id": -100,
            "message_id": 77,
            "caption": "<b>新 caption</b>",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        },
    )


@pytest.mark.asyncio
async def test_interaction_delivery_executor_sends_bot_message(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_message = AsyncMock(return_value={"message_id": 55})
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "text": "题面",
            "reply_markup": {"inline_keyboard": []},
        }
    ])

    send_message.assert_awaited_once_with(
        "123:token",
        -100,
        "题面",
        reply_to_message_id=None,
        reply_markup={"inline_keyboard": []},
    )


@pytest.mark.asyncio
async def test_interaction_delivery_send_replaces_saved_message_after_new_send(monkeypatch) -> None:
    redis = _MemoryRedis()
    redis.data["tp:msgid:1:ten_half:join_notice:1:-100"] = "44"
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_message = AsyncMock(return_value={"message_id": 55})
    delete_message = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    monkeypatch.setattr(account_bot_service, "delete_message", delete_message)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "text": "新的加入通知",
            "save_message_id_key": "ten_half:join_notice:1:-100",
            "replace_saved_message_id_key": "ten_half:join_notice:1:-100",
        }
    ])

    send_message.assert_awaited_once()
    delete_message.assert_awaited_once_with("123:token", -100, 44)
    assert redis.data["tp:msgid:1:ten_half:join_notice:1:-100"] == "55"


@pytest.mark.asyncio
async def test_interaction_delivery_media_save_key_and_edit_caption_by_key(monkeypatch) -> None:
    redis = _MemoryRedis()
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_photo = AsyncMock(return_value={"message_id": 66})
    edit_caption = AsyncMock(return_value={"message_id": 66})
    monkeypatch.setattr(account_bot_service, "send_photo_bytes", send_photo)
    monkeypatch.setattr(account_bot_service, "edit_message_caption", edit_caption)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )

    await executor.apply(
        [
            {
                "type": "send_photo",
                "send_via": "interaction_bot",
                "photo_base64": base64.b64encode(b"png-bytes").decode("ascii"),
                "filename": "grid.png",
                "caption": "题面",
                "save_message_id_key": "dice_grid:round:1",
            },
            {
                "type": "edit_caption",
                "send_via": "interaction_bot",
                "message_id_key": "dice_grid:round:1",
                "caption": "<b>答对</b>",
                "parse_mode": "html",
                "reply_markup": {"inline_keyboard": []},
            },
        ]
    )

    assert redis.data["tp:msgid:1:dice_grid:round:1"] == "66"
    edit_caption.assert_awaited_once_with(
        "123:token",
        -100,
        66,
        "<b>答对</b>",
        reply_markup={"inline_keyboard": []},
        parse_mode="html",
    )


@pytest.mark.asyncio
async def test_interaction_delivery_send_file_uses_document_api_and_saves_key(monkeypatch) -> None:
    redis = _MemoryRedis()
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_document = AsyncMock(return_value={"message_id": 67})
    monkeypatch.setattr(account_bot_service, "send_document_bytes", send_document)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )
    reply_markup = {"inline_keyboard": [[{"text": "打开", "url": "https://example.com"}]]}

    await executor.apply([
        {
            "type": "send_file",
            "send_via": "interaction_bot",
            "file_base64": base64.b64encode(b"doc-bytes").decode("ascii"),
            "filename": "round.txt",
            "caption": "文件题面",
            "reply_markup": reply_markup,
            "save_message_id_key": "file:round:1",
        }
    ])

    send_document.assert_awaited_once_with(
        "123:token",
        -100,
        b"doc-bytes",
        filename="round.txt",
        caption="文件题面",
        reply_to_message_id=None,
        reply_markup=reply_markup,
    )
    assert redis.data["tp:msgid:1:file:round:1"] == "67"


@pytest.mark.asyncio
async def test_interaction_delivery_answer_callback_failure_does_not_block_edit(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        callback_id="cb-1",
    )
    answer_callback = AsyncMock(side_effect=RuntimeError("query is too old"))
    edit_message = AsyncMock(return_value={"message_id": 30})
    write_log = AsyncMock()
    monkeypatch.setattr(account_bot_service, "answer_callback", answer_callback)
    monkeypatch.setattr(account_bot_service, "edit_message", edit_message)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "answer_callback",
            "callback_query_id": "cb-1",
            "text": "庄家手牌",
            "show_alert": True,
        },
        {
            "type": "edit_message",
            "message_id": 30,
            "text": "进入庄家行动",
            "reply_markup": {"inline_keyboard": []},
            "send_via": "interaction_bot",
        },
    ])

    answer_callback.assert_awaited_once()
    edit_message.assert_awaited_once_with(
        "123:token",
        -100,
        30,
        "进入庄家行动",
        reply_markup={"inline_keyboard": []},
    )
    assert write_log.await_args.kwargs["error"] == "query is too old"


@pytest.mark.asyncio
async def test_interaction_delivery_executor_routes_userbot_reply() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "低频代发",
            "reply_to_message_id": 30,
        }
    ])

    run_worker_action.assert_awaited_once_with(
        incoming,
        payload={
            "action_type": "send_message",
            "chat_id": -100,
            "text": "低频代发",
            "reply_to_message_id": 30,
            "parse_mode": "plain",
        },
    )


@pytest.mark.asyncio
async def test_interaction_delivery_edit_caption_routes_userbot_reply_from_saved_key() -> None:
    redis = _MemoryRedis()
    redis.data["tp:msgid:1:dice_grid:round:1"] = "66"
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )

    await executor.apply([
        {
            "type": "edit_caption",
            "send_via": "userbot_reply",
            "message_id_key": "dice_grid:round:1",
            "caption": "答对",
        }
    ])

    run_worker_action.assert_awaited_once_with(
        incoming,
        payload={
            "action_type": "edit_caption",
            "chat_id": -100,
            "message_id": 66,
            "caption": "答对",
            "parse_mode": "plain",
        },
    )


@pytest.mark.asyncio
async def test_interaction_delivery_preserves_reply_markup_for_userbot_worker_payload() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "低频代发",
            "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "pick:1"}]]},
        }
    ])

    assert run_worker_action.await_args.kwargs["payload"]["reply_markup"] == {
        "inline_keyboard": [[{"text": "1", "callback_data": "pick:1"}]]
    }


@pytest.mark.asyncio
async def test_interaction_delivery_passes_reply_to_user_id_to_worker() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "+88",
            "reply_to_user_id": 12345,
            "reply_to_search_limit": 50,
        }
    ])

    run_worker_action.assert_awaited_once_with(
        incoming,
        payload={
            "action_type": "send_message",
            "chat_id": -100,
            "text": "+88",
            "reply_to_message_id": None,
            "parse_mode": "plain",
            "reply_to_user_id": 12345,
            "reply_to_search_limit": 50,
        },
    )


@pytest.mark.asyncio
async def test_interaction_delivery_records_userbot_reply_anchor_failure_details(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_userbot_reply_fail",
    )
    worker_result = {
        "chat_id": -100,
        "amount": None,
        "reply_to_message_id": None,
        "reply_to_user_id": 12345,
        "reply_to_search_limit": 20,
        "error": "ValueError: 找不到用户 12345 在当前群的近期消息，无法定位发奖回复目标",
        "error_code": "reply_anchor_missing",
        "worker_offline": False,
        "reply_anchor_missing": True,
    }
    run_worker_action = AsyncMock(return_value=(False, worker_result["error"], worker_result))
    write_log = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "+88",
            "reply_to_user_id": 12345,
            "reply_to_search_limit": 20,
            "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言，无法发奖。",
        }
    ])

    assert record_action.await_args.args[2] == "failed"
    assert record_action.await_args.kwargs["error_code"] == "reply_anchor_missing"
    result = record_action.await_args.kwargs["result"]
    assert result["chat_id"] == -100
    assert result["amount"] is None
    assert result["reply_to_message_id"] is None
    assert result["reply_to_user_id"] == 12345
    assert result["reply_to_search_limit"] == 20
    assert result["worker_offline"] is False
    assert result["reply_anchor_missing"] is True
    assert write_log.await_args.args[2] == "interaction action send_via fallback"
    assert write_log.await_args.kwargs["chat_id"] == -100
    assert write_log.await_args.kwargs["reply_to_user_id"] == 12345
    assert write_log.await_args.kwargs["reply_to_search_limit"] == 20
    assert write_log.await_args.kwargs["error_code"] == "reply_anchor_missing"
    assert write_log.await_args.kwargs["reply_anchor_missing"] is True


@pytest.mark.asyncio
async def test_interaction_delivery_answers_failure_callback_when_userbot_reply_anchor_missing(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        callback_id="cb-join",
        trace_id="evt_userbot_reply_fail_callback",
    )
    worker_result = {
        "chat_id": -100,
        "amount": None,
        "reply_to_message_id": None,
        "reply_to_user_id": 12345,
        "reply_to_search_limit": 20,
        "error": "ValueError: 找不到用户 12345 在当前群的近期消息，无法定位发奖回复目标",
        "error_code": "reply_anchor_missing",
        "worker_offline": False,
        "reply_anchor_missing": True,
    }
    run_worker_action = AsyncMock(return_value=(False, worker_result["error"], worker_result))
    write_log = AsyncMock()
    answer_callback = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "answer_callback", answer_callback)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "+88",
            "reply_to_user_id": 12345,
            "reply_to_search_limit": 20,
            "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言，无法发奖。",
            "suppress_reply_anchor_missing_notice": True,
            "failure_callback": {
                "error_code": "reply_anchor_missing",
                "text": "无法扣款，加入失败。",
                "show_alert": True,
            },
        }
    ])

    payload = run_worker_action.await_args.kwargs["payload"]
    assert payload["suppress_reply_anchor_missing_notice"] is True
    answer_callback.assert_awaited_once_with(
        "123:token",
        "cb-join",
        text="无法扣款，加入失败。",
        show_alert=True,
    )
    assert incoming.callback_already_acked is True
    answer_trace_calls = [
        call for call in record_action.await_args_list if call.args[1].get("type") == "answer_callback"
    ]
    assert answer_trace_calls
    assert answer_trace_calls[0].args[2] == "ok"
    assert record_action.await_args.args[2] == "failed"
    assert record_action.await_args.kwargs["error_code"] == "reply_anchor_missing"


@pytest.mark.asyncio
async def test_interaction_delivery_uses_settlement_winner_as_reply_to_user_id() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "+88",
            "settlement": {"winner_user_id": 12345, "amount": 88},
        }
    ])

    assert run_worker_action.await_args.kwargs["payload"]["reply_to_user_id"] == 12345


@pytest.mark.asyncio
async def test_interaction_delivery_passes_html_parse_mode_to_worker_and_bot(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    send_message = AsyncMock(return_value={"message_id": 77})
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply(
        [
            {"type": "send_message", "send_via": "interaction_bot", "text": "<b>公告</b>", "parse_mode": "html"},
            {"type": "send_message", "send_via": "userbot_reply", "text": "<b>代发</b>", "parse_mode": "html"},
        ]
    )

    send_message.assert_awaited_once_with("123:token", -100, "<b>公告</b>", reply_to_message_id=None, reply_markup=None, parse_mode="html")
    assert run_worker_action.await_args.kwargs["payload"]["parse_mode"] == "html"


@pytest.mark.asyncio
async def test_interaction_delivery_routes_payout_via_worker(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_payout",
    )
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 77}))
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "payout",
            "amount": 88,
            "reply_to_user_id": 12345,
            "reply_to_search_limit": 20,
            "suppress_reply_anchor_missing_notice": True,
        }
    ])

    run_worker_action.assert_awaited_once()
    assert run_worker_action.await_args.args == (incoming,)
    payload = run_worker_action.await_args.kwargs["payload"]
    assert payload == {
        "action_type": "payout",
        "chat_id": -100,
        "amount": 88,
        "text": "+88",
        "reply_to_message_id": None,
        "parse_mode": "plain",
        "payout_key": payload["payout_key"],
        "reply_to_user_id": 12345,
        "reply_to_search_limit": 20,
        "suppress_reply_anchor_missing_notice": True,
    }
    assert payload["payout_key"].startswith("pay_")
    assert record_action.await_args.args[1]["type"] == "payout"
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"


@pytest.mark.parametrize(
    ("action_type", "message_id"),
    [
        ("delete_message", 77),
        ("pin_message", 88),
    ],
)
@pytest.mark.asyncio
async def test_interaction_delivery_routes_userbot_message_ops_via_worker(
    monkeypatch,
    action_type: str,
    message_id: int,
) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    run_worker_action = AsyncMock(
        return_value=(True, None, {"message_id": message_id, "chat_id": -100})
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply(
        [{"type": action_type, "send_via": "userbot_reply", "message_id": message_id, "chat_id": -100}]
    )

    run_worker_action.assert_awaited_once()
    assert run_worker_action.await_args.args == (incoming,)
    assert run_worker_action.await_args.kwargs["payload"] == {
        "action_type": action_type,
        "chat_id": -100,
        "message_id": message_id,
    }
    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == action_type
    assert record_action.await_args.args[2] == "ok"
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"


@pytest.mark.asyncio
async def test_interaction_delivery_payout_saves_message_id(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=7,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_payout_save",
    )
    redis = _MemoryRedis()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", AsyncMock())
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(return_value=(True, None, {"message_id": 77})),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )

    await executor.apply([{"type": "payout", "amount": 88, "save_message_id_key": "ten_half:reward:1"}])

    assert redis.data["tp:msgid:7:ten_half:reward:1"] == "77"


@pytest.mark.asyncio
async def test_interaction_delivery_records_failed_payout(monkeypatch) -> None:
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
    write_log = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "payout",
            "amount": 66,
            "reply_to_message_id": 31,
            "reply_to_user_id": 12345,
            "reply_to_search_limit": 20,
        }
    ])

    assert record_action.await_args.kwargs["error_code"] == "userbot_offline"
    result = record_action.await_args.kwargs["result"]
    assert result["chat_id"] == -100
    assert result["amount"] == 66
    assert result["reply_to_message_id"] == 31
    assert result["reply_to_user_id"] == 12345
    assert result["reply_to_search_limit"] == 20
    assert result["error_code"] == "userbot_offline"
    assert result["worker_offline"] is True
    assert result["reply_anchor_missing"] is False
    assert write_log.await_args.args[2] == "interaction payout failed"
    assert write_log.await_args.kwargs["chat_id"] == -100
    assert write_log.await_args.kwargs["amount"] == 66
    assert write_log.await_args.kwargs["reply_to_message_id"] == 31
    assert write_log.await_args.kwargs["reply_to_user_id"] == 12345
    assert write_log.await_args.kwargs["reply_to_search_limit"] == 20
    assert write_log.await_args.kwargs["error_code"] == "userbot_offline"
    assert write_log.await_args.kwargs["worker_offline"] is True


@pytest.mark.asyncio
async def test_interaction_delivery_records_start_session_and_action_limit(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_limit",
    )
    write_log = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([{"type": "start_session"}] + [{"type": "result"} for _ in range(10)])

    assert any(
        call.args[1]["type"] == "start_session"
        and call.args[2] == account_bot_runtime.TRACE_STATUS_SKIPPED
        and call.kwargs["actual_send_via"] == "interaction_session"
        for call in record_action.await_args_list
    )
    assert any(call.kwargs.get("error_code") == "action_limit_exceeded" for call in record_action.await_args_list)
    assert write_log.await_args.kwargs["reason_code"] == "action_limit_exceeded"


@pytest.mark.asyncio
async def test_interaction_delivery_update_session_merges_data_and_renews_ttl(monkeypatch) -> None:
    now = 1_720_000_000.0
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_session_update",
    )
    rule = {"id": "demo-rule", "module_session_scope": "chat"}
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100)
    expires_at = now + 120
    redis = _MemoryRedis()
    redis.data[session_key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100,
            "rule_id": "demo-rule",
            "channel": "interaction_bot",
            "created_at": now - 60,
            "updated_at": now - 10,
            "expires_at": expires_at,
            "data": {"round": 1},
        }
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    monkeypatch.setattr("app.services.interaction.delivery.time.time", lambda: now)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )

    await executor.apply(
        [
            {
                "type": "update_session",
                "data": {"winner": 111},
                "context": {"trace_id": "evt_session_update", "session_key": session_key},
            }
        ]
    )

    saved = json.loads(redis.data[session_key])
    assert saved["data"] == {"round": 1, "winner": 111}
    assert saved["expires_at"] == expires_at
    assert saved["updated_at"] == now
    assert redis.set_calls[-1][2]["ex"] == 120 + account_bot_runtime._INTERACTION_SESSION_TTL_GRACE_SECONDS
    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_OK
    assert record_action.await_args.kwargs["actual_send_via"] == "interaction_session"


@pytest.mark.asyncio
async def test_interaction_delivery_update_session_extends_expiry_when_requested(monkeypatch) -> None:
    now = 1_720_000_000.0
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_session_extend",
    )
    rule = {"id": "demo-rule", "module_session_scope": "chat"}
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100)
    redis = _MemoryRedis()
    redis.data[session_key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100,
            "rule_id": "demo-rule",
            "channel": "interaction_bot",
            "created_at": now - 60,
            "updated_at": now - 10,
            "expires_at": now + 120,
            "data": {"round": 1},
        }
    )
    monkeypatch.setattr("app.services.interaction.delivery.time.time", lambda: now)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: redis,
    )

    await executor.apply(
        [
            {
                "type": "update_session",
                "data": {"winner": 111},
                "extend_seconds": 45,
                "context": {"trace_id": "evt_session_extend", "session_key": session_key},
            }
        ]
    )

    saved = json.loads(redis.data[session_key])
    assert saved["expires_at"] == now + 165
    assert redis.set_calls[-1][2]["ex"] == 165 + account_bot_runtime._INTERACTION_SESSION_TTL_GRACE_SECONDS


@pytest.mark.asyncio
async def test_interaction_delivery_update_session_failure_records_action_trace(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_session_fail",
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
        get_redis_client=lambda: _MemoryRedis(),
    )

    await executor.apply([{"type": "update_session", "data": {"winner": 111}}])

    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["actual_send_via"] == "interaction_session"
    assert record_action.await_args.kwargs["error_code"] == "interaction_session_error"


@pytest.mark.asyncio
async def test_interaction_delivery_delete_failure_records_failed_action(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    monkeypatch.setattr(account_bot_service, "delete_message", AsyncMock(side_effect=RuntimeError("forbidden")))
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply(
        [
            {
                "type": "delete_message",
                "send_via": "interaction_bot",
                "message_id": 30,
            }
        ]
    )

    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == "failed"
    assert record_action.await_args.kwargs["error_code"] == "telegram_api_error"


@pytest.mark.asyncio
async def test_interaction_delivery_empty_message_records_failed_action(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([{"type": "send_message", "text": "", "context": {"trace_id": "evt_test"}}])

    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == "failed"
    assert record_action.await_args.kwargs["error_code"] == "empty_message_text"


@pytest.mark.asyncio
async def test_interaction_delivery_invalid_media_records_failed_action(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([{"type": "send_photo", "photo_base64": "not base64", "context": {"trace_id": "evt_test"}}])

    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == "failed"
    assert record_action.await_args.kwargs["error_code"] == "media_payload_invalid"


@pytest.mark.asyncio
async def test_interaction_delivery_settlement_records_action(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([{"type": "settlement", "amount": 10, "context": {"trace_id": "evt_test"}}])

    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == "ok"
    assert record_action.await_args.kwargs["actual_send_via"] == "settlement"


@pytest.mark.asyncio
async def test_interaction_delivery_executor_falls_back_between_plugin_channels(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_message = AsyncMock(side_effect=RuntimeError("bot blocked"))
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 66}))
    write_log = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "send_via_options": ["interaction_bot", "userbot_reply"],
            "chat_id": -200,
            "text": "可回退消息",
            "reply_to_message_id": 31,
        }
    ])

    send_message.assert_awaited_once()
    assert send_message.await_args.args[:3] == ("123:token", -200, "可回退消息")
    run_worker_action.assert_awaited_once_with(
        incoming,
        payload={
            "action_type": "send_message",
            "chat_id": -200,
            "text": "可回退消息",
            "reply_to_message_id": 31,
            "parse_mode": "plain",
        },
    )
    assert any(call.args[2] == "interaction action send_via fallback" for call in write_log.await_args_list)


@pytest.mark.asyncio
async def test_interaction_delivery_executor_rejects_removed_channel_before_send(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_message = AsyncMock()
    run_worker_action = AsyncMock()
    write_log = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=write_log,
        run_worker_action=run_worker_action,
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    ok, result = await executor.send_message(
        "旧通道",
        chat_id=-100,
        reply_to_message_id=None,
        send_via="bbot_notice",
    )

    assert ok is False
    assert result == {
        "error": "unsupported send_via: bbot_notice",
        "error_code": "unsupported_send_via",
    }
    send_message.assert_not_awaited()
    run_worker_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_delivery_pin_missing_token_records_bot_token_missing(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    record_action = AsyncMock()
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "pin_message",
            "send_via": "interaction_bot",
            "message_id": 30,
            "context": {"trace_id": "evt_test"},
        }
    ])

    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == "failed"
    assert record_action.await_args.kwargs["error_code"] == "bot_token_missing"


@pytest.mark.asyncio
async def test_interaction_delivery_send_pin_failure_records_dedicated_action(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_message = AsyncMock(return_value={"message_id": 31})
    pin_message = AsyncMock(side_effect=RuntimeError("pin denied"))
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    monkeypatch.setattr(account_bot_service, "call_bot_api", pin_message)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "text": "需要置顶",
            "pin": True,
            "context": {"trace_id": "evt_test"},
        }
    ])

    assert record_action.await_count == 2
    send_call, pin_call = record_action.await_args_list
    assert send_call.args[1]["type"] == "send_message"
    assert send_call.args[2] == "ok"
    assert send_call.kwargs["actual_send_via"] == "interaction_bot"
    assert pin_call.args[1]["type"] == "pin_message"
    assert pin_call.args[2] == "failed"
    assert pin_call.kwargs["error_code"] == "telegram_api_error"
    pin_message.assert_awaited_once_with(
        "123:token",
        "pinChatMessage",
        {"chat_id": -100, "message_id": 31},
    )


@pytest.mark.asyncio
async def test_interaction_delivery_executor_deletes_placeholder_from_trigger_chat(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    send_message = AsyncMock(return_value={"message_id": 90})
    delete_message = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_message", send_message)
    monkeypatch.setattr(account_bot_service, "delete_message", delete_message)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply(
        [
            {
                "type": "send_message",
                "send_via": "interaction_bot",
                "chat_id": -200,
                "text": "发到目标群",
                "context": {"trace_id": "evt_placeholder"},
            }
        ],
        replace_message_id=77,
    )

    send_message.assert_awaited_once_with(
        "123:token",
        -200,
        "发到目标群",
        reply_to_message_id=None,
        reply_markup=None,
    )
    delete_message.assert_awaited_once_with("123:token", -100, 77)
    assert record_action.await_count == 2
    action_types = [call.args[1]["type"] for call in record_action.await_args_list]
    assert action_types == ["delete_message", "send_message"]
    assert record_action.await_args_list[0].args[0] == {"trace_id": "evt_placeholder"}
    assert record_action.await_args_list[0].args[2] == account_bot_runtime.TRACE_STATUS_OK
    assert record_action.await_args_list[1].args[2] == account_bot_runtime.TRACE_STATUS_OK


@pytest.mark.asyncio
async def test_message_ops_buffers_standard_actions() -> None:
    ops = BufferedMessageOps()

    await ops.send(
        channel="interaction_bot",
        chat_id=-100,
        text="题面",
        reply_markup={"inline_keyboard": []},
        save_message_id_key="demo:notice:1",
        replace_saved_message_id_key="demo:notice:1",
    )
    await ops.send_photo(
        channel="interaction_bot",
        chat_id=-100,
        photo=b"img",
        filename="grid.png",
        caption="图题",
        save_message_id_key="demo:photo:1",
    )
    await ops.edit_caption(
        channel="interaction_bot",
        chat_id=-100,
        message_id_key="demo:photo:1",
        caption="新图题",
    )
    await ops.edit(channel="interaction_bot", chat_id=-100, message_id=41, text="新题面")
    await ops.answer_callback(callback_query_id="cb-1", text="收到")
    await ops.delete(message_id=42)

    assert [item["type"] for item in ops.actions] == [
        "send_message",
        "send_photo",
        "edit_caption",
        "edit_message",
        "answer_callback",
        "delete_message",
    ]
    assert ops.actions[0]["send_via"] == "interaction_bot"
    assert ops.actions[0]["parse_mode"] == "plain"
    assert ops.actions[0]["reply_markup"] == {"inline_keyboard": []}
    assert ops.actions[0]["save_message_id_key"] == "demo:notice:1"
    assert ops.actions[0]["replace_saved_message_id_key"] == "demo:notice:1"
    assert ops.actions[1]["photo_base64"] == "aW1n"
    assert ops.actions[1]["save_message_id_key"] == "demo:photo:1"
    assert ops.actions[2]["message_id_key"] == "demo:photo:1"
    assert ops.actions[3]["message_id"] == 41
    assert ops.actions[3]["parse_mode"] == "plain"
    assert ops.actions[4]["callback_query_id"] == "cb-1"


@pytest.mark.asyncio
async def test_message_ops_omits_channel_selector_by_default() -> None:
    ops = BufferedMessageOps()

    await ops.send(text="普通会话消息")
    await ops.edit(message_id=41, text="普通会话编辑")

    assert ops.actions == [
        {
            "type": "send_message",
            "chat_id": None,
            "text": "普通会话消息",
            "parse_mode": "plain",
            "reply_to_message_id": None,
        },
        {
            "type": "edit_message",
            "chat_id": None,
            "message_id": 41,
            "text": "普通会话编辑",
            "parse_mode": "plain",
        },
    ]


@pytest.mark.asyncio
async def test_message_ops_buffers_channel_selector_with_fallback() -> None:
    ops = BufferedMessageOps()

    await ops.send(
        channel={"prefer": ["bot", "userbot"], "fallback": True},
        chat_id=-100,
        text="题面",
    )

    assert ops.actions == [
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "send_via_options": ["interaction_bot", "userbot_reply"],
            "channel_selector": {"prefer": ["bot", "userbot"], "fallback": True},
            "chat_id": -100,
            "text": "题面",
            "parse_mode": "plain",
            "reply_to_message_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_message_ops_can_mark_html_parse_mode() -> None:
    ops = BufferedMessageOps()

    await ops.send(channel="interaction_bot", chat_id=-100, text="<b>题面</b>", parse_mode="html")
    await ops.edit(channel="interaction_bot", chat_id=-100, message_id=41, text="<b>新题面</b>", parse_mode="html")

    assert ops.actions[0]["parse_mode"] == "html"
    assert ops.actions[1]["parse_mode"] == "html"


@pytest.mark.asyncio
async def test_message_ops_can_buffer_payout() -> None:
    ops = BufferedMessageOps()

    await ops.payout(amount=88, chat_id=-100, reply_to_user_id=12345)

    assert ops.actions == [
        {
            "type": "payout",
            "chat_id": -100,
            "amount": 88,
            "parse_mode": "plain",
            "reply_to_message_id": None,
            "reply_to_user_id": 12345,
        }
    ]


@pytest.mark.asyncio
async def test_message_ops_can_buffer_update_session() -> None:
    ops = BufferedMessageOps()

    await ops.update_session(data={"round": 2}, extend_seconds=45)

    assert ops.actions == [
        {
            "type": "update_session",
            "data": {"round": 2},
            "extend_seconds": 45,
        }
    ]


def test_interaction_action_normalize_preserves_raw_selector_for_guard_trace() -> None:
    action = plugin_loader._normalize_interaction_action(  # noqa: SLF001
        {
            "type": "send_message",
            "channel": {"prefer": ["bot", "notice"], "fallback": True},
            "text": "题面",
        }
    )

    assert action == {
        "type": "send_message",
        "send_via": "interaction_bot",
        "channel_selector": {"prefer": ["bot", "notice"], "fallback": True},
        "text": "题面",
    }


def test_interaction_action_normalize_does_not_inject_send_via_for_ack_actions() -> None:
    actions = plugin_loader._normalize_interaction_actions(  # noqa: SLF001
        [
            {"type": "answer_callback", "callback_query_id": "cb-1", "text": "收到"},
            {"type": "answer_inline_query", "inline_query_id": "iq-1", "results": []},
        ],
        default_send_via=["userbot_reply"],
    )

    assert actions == [
        {"type": "answer_callback", "callback_query_id": "cb-1", "text": "收到"},
        {"type": "answer_inline_query", "inline_query_id": "iq-1", "results": []},
    ]


@pytest.mark.asyncio
async def test_interaction_result_contract_warns_undeclared_channel(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    rule = {"module_key": "demo", "module_action": "start"}
    logs: list[dict[str, object]] = []

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {
            "result_contract": {
                "actions": ["send_message", "answer_callback"],
                "send_via": ["interaction_bot"],
            },
        },
    )

    async def _fake_log(_incoming, level, message, **detail):  # noqa: ANN001
        logs.append({"level": level, "message": message, **detail})

    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", _fake_log)

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        rule,
        [
            {"type": "send_message", "send_via": "userbot_reply", "text": "allowed with warning"},
            {"type": "send_message", "send_via": "interaction_bot", "text": "ok"},
            {"type": "delete_message", "message_id": 30},
        ],
    )

    assert guarded == [
        {"type": "send_message", "send_via": "userbot_reply", "text": "allowed with warning"},
        {"type": "send_message", "send_via": "interaction_bot", "text": "ok"},
        {"type": "delete_message", "message_id": 30, "send_via": "interaction_bot"},
    ]
    assert any(item["message"] == "interaction action outside result_contract.send_via" for item in logs)
    assert any(item["message"] == "interaction action outside result_contract.actions: delete_message" for item in logs)
    assert all(item.get("guard_level") == "warning" for item in logs)


@pytest.mark.asyncio
async def test_interaction_result_contract_warns_but_keeps_channel_options(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    rule = {"module_key": "demo", "module_action": "start"}

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {"result_contract": {"send_via": ["userbot_reply"]}},
    )
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        rule,
        [
            {
                "type": "send_message",
                "send_via": "interaction_bot",
                "send_via_options": ["interaction_bot", "userbot_reply"],
                "text": "fallback only",
            },
        ],
    )

    assert guarded == [
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "send_via_options": ["interaction_bot", "userbot_reply"],
            "text": "fallback only",
        }
    ]
    account_bot_runtime._write_interaction_runtime_log.assert_awaited_once()
    assert account_bot_runtime._write_interaction_runtime_log.await_args.kwargs["guard_level"] == "warning"


@pytest.mark.asyncio
async def test_interaction_result_contract_accepts_channel_aliases_without_manifest_normalize(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {"result_contract": {"send_via": ["userbot"]}},
    )
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [{"type": "send_message", "channel": {"prefer": ["bot", "userbot"], "fallback": True}, "text": "alias"}],
    )

    assert guarded == [
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "send_via_options": ["interaction_bot", "userbot_reply"],
            "text": "alias",
        }
    ]


@pytest.mark.asyncio
async def test_interaction_result_contract_narrows_buttons_to_bot_channels(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {"result_contract": {"send_via": ["interaction_bot", "userbot_reply"]}},
    )
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [
            {
                "type": "send_message",
                "send_via": "interaction_bot",
                "send_via_options": ["userbot_reply", "interaction_bot"],
                "text": "button path",
                "reply_markup": {"inline_keyboard": []},
            },
        ],
    )

    assert guarded == [
        {
            "type": "send_message",
            "send_via": "interaction_bot",
            "text": "button path",
            "reply_markup": {"inline_keyboard": []},
        }
    ]


@pytest.mark.asyncio
async def test_interaction_contract_defaults_send_via_to_session_channel() -> None:
    async def _write_log(*_args, **_kwargs):  # noqa: ANN001, ANN002
        return None

    guarded = await interaction_contracts.guard_interaction_actions(
        rule={"module_key": "demo", "module_action": "start"},
        actions=[{"type": "send_message", "text": "走会话默认通道"}],
        resolve_entry_manifest=lambda *_args: {},
        write_log=_write_log,
        session_channel="userbot_reply",
    )

    assert guarded == [{"type": "send_message", "text": "走会话默认通道", "send_via": "userbot_reply"}]


@pytest.mark.asyncio
async def test_interaction_contract_preserves_reply_markup_for_userbot_session() -> None:
    async def _write_log(*_args, **_kwargs):  # noqa: ANN001, ANN002
        return None

    guarded = await interaction_contracts.guard_interaction_actions(
        rule={"module_key": "demo", "module_action": "start"},
        actions=[
            {
                "type": "send_message",
                "text": "请回复序号",
                "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "pick:1"}]]},
            }
        ],
        resolve_entry_manifest=lambda *_args: {},
        write_log=_write_log,
        session_channel="userbot_reply",
    )

    assert guarded == [
        {
            "type": "send_message",
            "text": "请回复序号",
            "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "pick:1"}]]},
            "send_via": "userbot_reply",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_guard_defaults_send_via_to_loaded_session_channel(monkeypatch) -> None:
    redis = _MemoryRedis()
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    rule = {
        "id": "demo-rule",
        "module_key": "demo",
        "module_action": "start",
        "module_session_scope": "chat",
    }
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100)
    redis.data[session_key] = json.dumps({"channel": "userbot"})
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_manifest", lambda *_args: {})
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        rule,
        [
            {
                "type": "send_message",
                "text": "请回复序号",
                "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "pick:1"}]]},
            }
        ],
    )

    assert guarded == [
        {
            "type": "send_message",
            "text": "请回复序号",
            "reply_markup": {"inline_keyboard": [[{"text": "1", "callback_data": "pick:1"}]]},
            "send_via": "userbot_reply",
        }
    ]


@pytest.mark.asyncio
async def test_interaction_contract_does_not_inject_send_via_for_payout() -> None:
    async def _write_log(*_args, **_kwargs):  # noqa: ANN001, ANN002
        return None

    guarded = await interaction_contracts.guard_interaction_actions(
        rule={"module_key": "demo", "module_action": "start"},
        actions=[{"type": "payout", "amount": 88}],
        resolve_entry_manifest=lambda *_args: {},
        write_log=_write_log,
        session_channel="userbot_reply",
    )

    assert guarded == [{"type": "payout", "amount": 88}]


@pytest.mark.asyncio
async def test_interaction_contract_does_not_inject_send_via_for_update_session_or_no_session() -> None:
    async def _write_log(*_args, **_kwargs):  # noqa: ANN001, ANN002
        return None

    guarded = await interaction_contracts.guard_interaction_actions(
        rule={"module_key": "demo", "module_action": "start"},
        actions=[
            {"type": "update_session", "data": {"round": 2}},
            {"type": "no_session"},
        ],
        resolve_entry_manifest=lambda *_args: {},
        write_log=_write_log,
        session_channel="userbot_reply",
    )

    assert guarded == [
        {"type": "update_session", "data": {"round": 2}},
        {"type": "no_session"},
    ]


@pytest.mark.asyncio
async def test_interaction_without_result_contract_trusts_standard_channels(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )

    monkeypatch.setattr(account_bot_service, "declared_module_entry_manifest", lambda *_args: {})
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [
            {"type": "send_message", "send_via": "userbot_reply", "text": "admin path"},
            {"type": "send_message", "send_via": "interaction_bot", "text": "public path"},
            {"type": "send_message", "send_via": "bbot_notice", "text": "notice path"},
            {"type": "send_message", "send_via": "notice", "text": "notice alias"},
        ],
    )

    assert [item["send_via"] for item in guarded] == ["userbot_reply", "interaction_bot"]
    warn_calls = [
        call
        for call in account_bot_runtime._write_interaction_runtime_log.await_args_list
        if "deprecated send_via" in call.args[2]
    ]
    assert len(warn_calls) == 2
    blocked_raw = [call.kwargs["requested_send_via_raw"] for call in warn_calls]
    assert blocked_raw == ["bbot_notice", "notice"]
    assert all(call.kwargs["guard_level"] == "failed" for call in warn_calls)
    assert all("interaction_bot" in call.kwargs["migration_hint"] for call in warn_calls)


@pytest.mark.asyncio
async def test_deprecated_notice_channels_record_failed_action_reason(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_notice",
    )
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "declared_module_entry_manifest", lambda *_args: {})
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [{"type": "send_message", "send_via": "notice", "text": "deprecated"}],
    )

    assert guarded == []
    assert record_action.await_args.kwargs["error_code"] == "send_channel_deprecated"


@pytest.mark.asyncio
async def test_account_bot_answer_callback_records_event_action(monkeypatch) -> None:
    answer = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        callback_id="cb-1",
        trace_id="evt_callback",
    )

    await account_bot_runtime._answer_callback(incoming, text="已处理")

    answer.assert_awaited_once_with("123:token", "cb-1", text="已处理", show_alert=False)
    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == "answer_callback"
    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_OK
    assert record_action.await_args.kwargs["actual_send_via"] == "interaction_bot"


@pytest.mark.asyncio
async def test_interaction_result_contract_strips_buttons_from_userbot_reply(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {"result_contract": {"send_via": ["interaction_bot", "userbot_reply"]}},
    )
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [
            {
                "type": "send_message",
                "send_via": "userbot_reply",
                "text": "ok",
                "reply_markup": {"inline_keyboard": []},
            },
        ],
    )

    assert guarded == [{"type": "send_message", "send_via": "userbot_reply", "text": "ok"}]


def test_account_bot_token_payload_trims_whitespace() -> None:
    payload = AccountBotConfigUpdate(bot_token="  123456:secret-token  ")
    assert payload.bot_token == "123456:secret-token"


@pytest.mark.asyncio
async def test_account_bot_test_with_token_override_relies_on_polling(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "send_message",
        AsyncMock(return_value={"message_id": 123, "from": {"id": 456}}),
    )
    monkeypatch.setattr(account_bot_service, "sanitize_bot_error", lambda exc, *, token: str(exc))
    monkeypatch.setattr(audit, "write", AsyncMock())

    class _DB:
        async def commit(self):
            return None

    user = SimpleNamespace(id=100)
    resp = await account_bots.test_account_bot(
        1,
        AccountBotTestRequest(
            chat_id=-100123,
            text="测试联动消息",
            bot_token_override="123456:override-token",
        ),
        _DB(),
        user,
    )

    assert resp.ok is True
    assert resp.sent == 1
    assert "Bbot 将通过 polling 自然接收并触发联动" in (resp.message or "")
    account_bot_service.send_message.assert_awaited_once()


def test_account_bot_remote_plugin_policy_defaults_closed() -> None:
    policy = account_bot_service.normalize_remote_plugin_policy(None)
    assert policy == {
        "enabled": False,
        "install": False,
        "update": False,
        "uninstall": False,
        "enable_disable": False,
    }


def test_account_bot_remote_plugin_policy_update_is_partial() -> None:
    policy = account_bot_service.normalize_remote_plugin_policy(
        {"enabled": True, "install": True, "unknown": True}
    )
    assert policy["enabled"] is True
    assert policy["install"] is True
    assert policy["update"] is False
    assert "unknown" not in policy


def test_account_bot_transfer_notice_config_normalizes_values() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "chat_id": "-100123",
            "chat_ids": ["-100123", "-100999", "-100123"],
            "trusted_bot_id": "456",
            "trigger_text": " 转账成功 ",
            "trigger_texts": [" 转账成功 ", "交易成功", "转账成功"],
            "query_commands": [" 。玩法 ", "。玩法", "玩法菜单"],
            "reply_chain_verification_enabled": False,
            "receiver_text": " 我 ",
            "amount": "100",
            "response_template": " 检测到 {amount} ",
            "transfer_notice_template": " 测试到账\n付款人：{payer_name}\n收款人：{receiver_name}\n金额：{amount} ",
        }
    )

    assert cfg["enabled"] is True
    assert cfg["chat_id"] == -100123
    assert cfg["chat_ids"] == [-100123, -100999]
    assert cfg["trusted_bot_id"] == 456
    assert cfg["trusted_bot_ids"] == [456]
    assert cfg["reply_chain_verification_enabled"] is False
    assert cfg["trigger_text"] == "转账成功"
    assert cfg["trigger_texts"] == ["转账成功", "交易成功"]
    assert cfg["query_commands"] == ["。玩法", "玩法菜单"]
    assert cfg["receiver_text"] == "我"
    assert cfg["amount"] == 100
    assert cfg["response_template"] == "检测到 {amount}"
    assert cfg["transfer_notice_template"] == "测试到账\n付款人：{payer_name}\n收款人：{receiver_name}\n金额：{amount}"
    assert cfg["rules"][0]["id"] == "legacy-default"
    assert cfg["rules"][0]["chat_ids"] == [-100123, -100999]
    assert cfg["rules"][0]["trigger_texts"] == ["转账成功", "交易成功"]


def test_account_bot_transfer_notice_reply_chain_verification_defaults_on() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config({})

    assert cfg["reply_chain_verification_enabled"] is True


def test_account_bot_transfer_notice_config_upgrades_legacy_default_template() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {"transfer_notice_template": LEGACY_TRANSFER_NOTICE_TEMPLATE}
    )

    assert cfg["transfer_notice_template"] == DEFAULT_TRANSFER_NOTICE_TEMPLATE
    assert "language-转账成功" in cfg["transfer_notice_template"]


def test_account_bot_transfer_notice_config_accepts_multiple_trusted_bots() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "trusted_bot_ids": ["111", "222", "111", "", "bad"],
            "trusted_bot_id": "333",
        }
    )

    assert cfg["trusted_bot_id"] == 333
    assert cfg["trusted_bot_ids"] == [333, 111, 222]


@pytest.mark.asyncio
async def test_clear_transfer_bot_token_also_clears_detected_bot_id() -> None:
    row = SimpleNamespace(
        value={
            "enabled": True,
            "trusted_bot_id": 456,
            "transfer_bot_token_enc": "old-token",
            "transfer_bot_id": 456,
            "rules": [
                {
                    "id": "default",
                    "name": "默认规则",
                    "enabled": True,
                    "trigger_texts": ["转账成功"],
                    "action": "notice",
                    "response_template": "收到 {amount}",
                }
            ],
        }
    )

    class _DB:
        async def get(self, model, _key):  # noqa: ANN001
            if model is Account:
                return SimpleNamespace(id=1)
            if model is account_bot_service.SystemSetting:
                return row
            return None

        async def flush(self):
            return None

        def add(self, _row):  # noqa: ANN001
            raise AssertionError("existing setting row should be updated in place")

    out = await account_bot_service.update_transfer_notice_config(
        _DB(),
        1,
        {"clear_transfer_bot_token": True},
    )

    assert row.value["transfer_bot_token_enc"] is None
    assert row.value["transfer_bot_id"] is None
    assert out["transfer_bot_id"] is None
    assert out["has_transfer_bot_token"] is False


@pytest.mark.asyncio
async def test_update_interaction_bot_config_warns_when_privacy_disabled(monkeypatch) -> None:
    class _DB:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def get(self, model, _key):  # noqa: ANN001
            if model is Account:
                return SimpleNamespace(id=1)
            return None

        def add(self, row):  # noqa: ANN001
            self.added.append(row)

        async def flush(self):
            return None

    db = _DB()
    monkeypatch.setattr(
        account_bot_service,
        "get_me",
        AsyncMock(
            return_value={
                "id": 12345,
                "username": "TelePilotTestBot",
                "can_read_all_group_messages": True,
            }
        ),
    )

    out = await account_bot_service.update_interaction_bot_config(
        db,
        1,
        {"enabled": True, "trusted_bot_id": 456, "interaction_bot_token": "123:token"},
    )

    assert out["interaction_bot_id"] == 12345
    assert out["interaction_bot_username"] == "TelePilotTestBot"
    assert "开启 Privacy Mode" in out["interaction_last_error"]
    assert db.added


@pytest.mark.asyncio
async def test_update_transfer_notice_requires_trusted_bot_for_payment_rules(monkeypatch) -> None:
    class _DB:
        async def get(self, *_args):  # noqa: ANN002
            return None

        def add(self, _row):  # noqa: ANN001
            return None

        async def flush(self):
            return None

    monkeypatch.setattr(account_bot_service, "ensure_account", AsyncMock())

    with pytest.raises(Exception) as exc_info:
        await account_bot_service.update_transfer_notice_config(
            _DB(),
            1,
            {
                "enabled": True,
                "rules": [
                    {
                        "id": "paid",
                        "enabled": True,
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                    }
                ],
            },
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "TRUSTED_BOT_ID_REQUIRED"


@pytest.mark.asyncio
async def test_update_transfer_notice_syncs_enabled_module_rules_to_account_features(monkeypatch) -> None:
    existing = account_bot_service.AccountFeature(
        account_id=1,
        feature_key="dice_grid_hunt",
        enabled=False,
        config={"kept": True},
        state="disabled",
        last_error="old error",
    )

    class _DB:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def get(self, model, key):  # noqa: ANN001
            if model is Account:
                return SimpleNamespace(id=1)
            if model is account_bot_service.SystemSetting:
                return None
            if model is account_bot_service.Feature:
                return SimpleNamespace(key=key) if key in {"game24", "dice_grid_hunt", "math10"} else None
            if model is account_bot_service.AccountFeature:
                if key == {"account_id": 1, "feature_key": "dice_grid_hunt"}:
                    return existing
                return None
            return None

        def add(self, row):  # noqa: ANN001
            self.added.append(row)

        async def flush(self):
            return None

    db = _DB()
    monkeypatch.setattr(account_bot_service, "ensure_account", AsyncMock())

    await account_bot_service.update_transfer_notice_config(
        db,
        1,
        {
            "enabled": True,
            "rules": [
                {
                    "id": "game24",
                    "enabled": True,
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["我要24点"],
                    "action": "module",
                    "module_key": "game24",
                    "module_action": "start_paid_game",
                },
                {
                    "id": "dice",
                    "enabled": True,
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["我要猜骰"],
                    "action": "module",
                    "module_key": "dice_grid_hunt",
                    "module_action": "start_dice_grid_hunt",
                },
                {
                    "id": "disabled-math",
                    "enabled": False,
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["算数题"],
                    "action": "module",
                    "module_key": "math10",
                    "module_action": "start_math_game",
                },
            ],
        },
    )

    added_features = [
        row for row in db.added
        if isinstance(row, account_bot_service.AccountFeature)
    ]
    assert [(row.account_id, row.feature_key, row.enabled) for row in added_features] == [(1, "game24", True)]
    assert existing.enabled is True
    assert existing.config == {"kept": True}
    assert existing.state == "disabled"
    assert existing.last_error is None
    assert all(getattr(row, "feature_key", None) != "math10" for row in db.added)


@pytest.mark.asyncio
async def test_interaction_service_requires_trusted_bot_for_payment_rules(monkeypatch) -> None:
    from app.services import interaction_bot_service

    class _DB:
        async def get(self, *_args):  # noqa: ANN002
            return None

        def add(self, _row):  # noqa: ANN001
            return None

        async def flush(self):
            return None

    monkeypatch.setattr(account_bot_service, "ensure_account", AsyncMock())

    with pytest.raises(Exception) as exc_info:
        await interaction_bot_service.update_transfer_notice_config(
            _DB(),
            1,
            {
                "enabled": True,
                "rules": [
                    {
                        "id": "paid",
                        "enabled": True,
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                    }
                ],
            },
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "TRUSTED_BOT_ID_REQUIRED"


def test_account_bot_interaction_rules_normalize_and_sync_first_rule() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "chat_id": "-100000",
            "trigger_text": "旧关键词",
            "action": "notice",
            "rules": [
                {
                    "id": "math",
                    "name": "算数题",
                    "enabled": True,
                    "chat_ids": ["-100123"],
                    "trigger_texts": ["交易成功"],
                    "amount": "88",
                    "action": "math10",
                    "math_prize": "456",
                },
                {
                    "id": "bad",
                    "enabled": True,
                    "trigger_texts": [],
                    "action": "unknown",
                },
            ],
        }
    )

    assert cfg["chat_id"] == -100123
    assert cfg["chat_ids"] == [-100123]
    assert cfg["trigger_text"] == "交易成功"
    assert cfg["amount"] == 88
    assert cfg["action"] == "math10"
    assert cfg["math_prize"] == 456
    assert cfg["rules"][1]["action"] == "notice"


def test_account_bot_interaction_rules_normalize_new_rule_fields() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "game24",
                    "enabled": True,
                    "chat_ids": ["-100123"],
                    "trigger_mode": "both",
                    "trigger_texts": ["转账成功"],
                    "module_start_keywords": ["开24点", "开24点"],
                    "amount": "100",
                    "amount_match_mode": "gte",
                    "action": "module",
                    "module_key": "game24",
                    "open_commands": ["开启24点"],
                    "close_commands": ["关闭24点"],
                    "status_commands": ["24点状态"],
                    "disabled_message": "今天休息",
                    "valid_seconds": "1200",
                    "concurrency": "chat",
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert cfg["trigger_mode"] == "both"
    assert cfg["amount_match_mode"] == "gte"
    assert cfg["module_start_keywords"] == ["开24点"]
    assert cfg["valid_seconds"] == 1200
    assert rule["open_commands"] == ["开启24点"]
    assert rule["close_commands"] == ["关闭24点"]
    assert rule["status_commands"] == ["24点状态"]
    assert rule["disabled_message"] == "今天休息"


def test_keyword_rules_drop_hidden_payment_filters_but_keep_user_limits() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "pt",
                    "enabled": True,
                    "chat_ids": [-100123],
                    "trigger_mode": "keyword",
                    "trigger_texts": ["转账成功"],
                    "module_start_keywords": ["置顶 id=数字"],
                    "receiver_user_id": "111",
                    "receiver_text": "BBB",
                    "amount": "100",
                    "action": "module",
                    "module_key": "pt_promote",
                    "module_action": "promote_torrent",
                    "concurrency": "chat",
                    "user_cooldown_seconds": "6h",
                    "daily_limit_per_user": 2,
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert rule["trigger_mode"] == "keyword"
    assert rule["amount"] is None
    assert rule["receiver_user_id"] is None
    assert rule["receiver_text"] is None
    assert rule["user_cooldown_seconds"] == "6h"
    assert rule["daily_limit_per_user"] == 2


@pytest.mark.asyncio
async def test_update_interaction_config_rejects_keyword_rule_without_interaction_bot_token(monkeypatch) -> None:
    current = account_bot_service.normalize_transfer_notice_config({"enabled": False})
    update_config = AsyncMock()
    monkeypatch.setattr(account_bots.interaction_bot_service, "get_interaction_bot_config", AsyncMock(return_value=current))
    monkeypatch.setattr(account_bots.interaction_bot_service, "update_interaction_bot_config", update_config)

    payload = AccountBotInteractionConfig(
        enabled=True,
        rules=[
            {
                "id": "keyword-game",
                "name": "关键词玩法",
                "enabled": True,
                "trigger_mode": "keyword",
                "module_start_keywords": ["开局"],
                "action": "module",
                "module_key": "game24",
                "module_action": "start",
            }
        ],
    )

    with pytest.raises(account_bots.HTTPException) as exc_info:
        await account_bots.update_account_bot_interaction(
            1,
            payload,
            SimpleNamespace(),
            SimpleNamespace(id=99),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["message"] == "关键词触发依赖交互 Bot，请先配置交互 Bot Token 或改用命令触发"
    update_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_interaction_config_allows_keyword_rule_with_new_interaction_bot_token(monkeypatch) -> None:
    current = account_bot_service.normalize_transfer_notice_config({"enabled": False})
    saved = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "interaction_bot_token_enc": "enc-token",
            "rules": [
                {
                    "id": "keyword-game",
                    "name": "关键词玩法",
                    "enabled": True,
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["开局"],
                    "action": "module",
                    "module_key": "game24",
                    "module_action": "start",
                }
            ],
        }
    )
    update_config = AsyncMock(return_value=saved)
    monkeypatch.setattr(account_bots.interaction_bot_service, "get_interaction_bot_config", AsyncMock(return_value=current))
    monkeypatch.setattr(account_bots.interaction_bot_service, "update_interaction_bot_config", update_config)
    monkeypatch.setattr(account_bots.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bots.feature_service, "_notify_reload", AsyncMock())
    monkeypatch.setattr(account_bots.interaction_bot_runtime, "restart_interaction_bot", AsyncMock())
    monkeypatch.setattr(account_bots.interaction_bot_runtime, "is_interaction_bot_running", lambda _aid: False)

    class _DB:
        async def commit(self):
            return None

    payload = AccountBotInteractionConfig(
        enabled=True,
        interaction_bot_token="1234567890:interaction-token",
        rules=[
            {
                "id": "keyword-game",
                "name": "关键词玩法",
                "enabled": True,
                "trigger_mode": "keyword",
                "module_start_keywords": ["开局"],
                "action": "module",
                "module_key": "game24",
                "module_action": "start",
            }
        ],
    )

    result = await account_bots.update_account_bot_interaction(1, payload, _DB(), SimpleNamespace(id=99))

    assert result.has_interaction_bot_token is True
    update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_interaction_config_allows_command_only_rule_without_interaction_bot_token(monkeypatch) -> None:
    current = account_bot_service.normalize_transfer_notice_config({"enabled": False})
    saved = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "command-game",
                    "name": "命令玩法",
                    "enabled": True,
                    "trigger_mode": "payment",
                    "module_start_keywords": [],
                    "open_commands": [",game_on"],
                    "action": "module",
                    "module_key": "game24",
                    "module_action": "start",
                }
            ],
        }
    )
    update_config = AsyncMock(return_value=saved)
    monkeypatch.setattr(account_bots.interaction_bot_service, "get_interaction_bot_config", AsyncMock(return_value=current))
    monkeypatch.setattr(account_bots.interaction_bot_service, "update_interaction_bot_config", update_config)
    monkeypatch.setattr(account_bots.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bots.feature_service, "_notify_reload", AsyncMock())
    monkeypatch.setattr(account_bots.interaction_bot_runtime, "restart_interaction_bot", AsyncMock())
    monkeypatch.setattr(account_bots.interaction_bot_runtime, "is_interaction_bot_running", lambda _aid: False)

    class _DB:
        async def commit(self):
            return None

    payload = AccountBotInteractionConfig(
        enabled=True,
        rules=[
            {
                "id": "command-game",
                "name": "命令玩法",
                "enabled": True,
                "trigger_mode": "payment",
                "module_start_keywords": [],
                "open_commands": [",game_on"],
                "action": "module",
                "module_key": "game24",
                "module_action": "start",
            }
        ],
    )

    result = await account_bots.update_account_bot_interaction(1, payload, _DB(), SimpleNamespace(id=99))

    assert result.has_interaction_bot_token is False
    update_config.assert_awaited_once()


def test_account_bot_math10_rule_gets_default_start_keywords() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "math10",
                    "enabled": True,
                    "trigger_mode": "payment",
                    "action": "math10",
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert rule["trigger_mode"] == "both"
    assert rule["module_start_keywords"] == ["发十以内算数", "十以内算数", "开算数题"]


def test_legacy_math10_rule_syncs_account_feature_key() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "math10",
                    "enabled": True,
                    "action": "math10",
                },
                {
                    "id": "dice",
                    "enabled": True,
                    "action": "module",
                    "module_key": "dice_grid_hunt",
                },
            ],
        }
    )

    assert account_bot_service._enabled_interaction_module_keys(cfg) == ["math10", "dice_grid_hunt"]


def test_disabled_or_cleared_interaction_bot_hides_stale_conflict_error() -> None:
    raw = {
        "enabled": False,
        "interaction_bot_token_enc": None,
        "interaction_last_error": "Conflict: terminated by other getUpdates request",
    }

    cfg = account_bot_service.normalize_transfer_notice_config(raw)

    assert cfg["has_interaction_bot_token"] is False
    assert cfg["interaction_runtime_status"] == "stopped"
    assert cfg["interaction_last_error"] is None


def test_account_bot_interaction_rule_normalizes_module_action() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "game24-ticket",
                    "enabled": True,
                    "chat_ids": ["-100123"],
                    "trigger_texts": ["转账成功"],
                    "amount": "100",
                    "action": "module",
                    "module_key": " game24 ",
                    "module_prize": "456",
                    "module_start_text": " 正在开启 24 点 ",
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert cfg["action"] == "module"
    assert cfg["module_key"] == "game24"
    assert cfg["module_prize"] == 456
    assert rule["action"] == "module"
    assert rule["module_key"] == "game24"
    assert rule["module_prize"] == 456
    assert rule["module_start_text"] == "正在开启 24 点"


def test_account_bot_interaction_rule_infers_single_module_action() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "game24-ticket",
                    "enabled": True,
                    "chat_ids": ["-100123"],
                    "trigger_mode": "both",
                    "trigger_texts": ["转账成功"],
                    "module_start_keywords": ["开24点"],
                    "amount": "100",
                    "action": "module",
                    "module_key": "game24",
                    "module_prize": "888",
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert rule["module_key"] == "game24"
    assert rule["module_action"] == "start_paid_game"
    assert rule["module_session_scope"] == "chat"


def test_interaction_rule_uses_declared_installed_entry_session_scope(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "new_game"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "new_game",
                "interaction_entries": [
                    {
                        "key": "start_new_game",
                        "title": "开始新游戏",
                        "session_scope": "chat",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(account_bot_service, "settings", SimpleNamespace(plugins_installed_path=tmp_path))

    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "new-game",
                    "enabled": True,
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["开新游戏"],
                    "action": "module",
                    "module_key": "new_game",
                    "module_action": "start_new_game",
                    "concurrency": "user",
                    "user_cooldown_seconds": "6h",
                    "daily_limit_per_user": 2,
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert rule["concurrency"] == "user"
    assert rule["module_session_scope"] == "chat"
    assert rule["user_cooldown_seconds"] == "6h"
    assert rule["daily_limit_per_user"] == 2


def test_account_bot_interaction_rule_filters_strict_plugin_module_config() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "module_config": {"prize": 1, "timeout": 2, "theme": "dark"},
            "rules": [
                {
                    "id": "dice-ticket",
                    "enabled": True,
                    "action": "module",
                    "module_key": "dice_grid_hunt",
                    "module_config": {
                        "prize": 123,
                        "timeout": 500,
                        "valid_seconds": 600,
                        "theme": "classic",
                    },
                }
            ],
        }
    )

    assert cfg["module_config"] == {"timeout": 500}
    assert cfg["rules"][0]["module_config"] == {"timeout": 500}


def test_interaction_rule_drops_stale_module_config_for_strict_entry(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "math10"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "math10",
                "interaction_entries": [
                    {
                        "key": "start_math_game",
                        "input_schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "prize": {"type": "integer", "default": 123},
                                "valid_seconds": {"type": "integer", "default": 900},
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(account_bot_service, "settings", SimpleNamespace(plugins_installed_path=tmp_path))

    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "silly-math",
                    "enabled": True,
                    "action": "module",
                    "module_key": "math10",
                    "module_action": "start_math_game",
                    "module_prize": "666",
                    "module_config": {
                        "bet": 100,
                        "prize": 123,
                        "valid_seconds": 900,
                    },
                }
            ],
        }
    )

    rule = cfg["rules"][0]
    assert rule["module_prize"] == 666
    assert rule["module_config"] == {}
    assert cfg["module_prize"] == 666
    assert cfg["module_config"] == {}


def test_interaction_rule_keeps_allowed_module_config_for_strict_entry(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "strict_game"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "strict_game",
                "interaction_entries": [
                    {
                        "key": "start_strict_game",
                        "input_schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "timeout": {"type": "integer", "default": 45},
                                "theme": {"type": "string", "default": "classic"},
                                "prize": {"type": "integer", "default": 123},
                                "valid_seconds": {"type": "integer", "default": 900},
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(account_bot_service, "settings", SimpleNamespace(plugins_installed_path=tmp_path))

    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "strict-game",
                    "enabled": True,
                    "action": "module",
                    "module_key": "strict_game",
                    "module_action": "start_strict_game",
                    "module_config": {
                        "timeout": 60,
                        "theme": "dark",
                        "bet": 100,
                        "legacy": True,
                        "prize": 123,
                        "valid_seconds": 900,
                    },
                }
            ],
        }
    )

    assert cfg["rules"][0]["module_config"] == {"timeout": 60, "theme": "dark"}
    assert cfg["module_config"] == {"timeout": 60, "theme": "dark"}


def test_account_bot_transfer_notice_parser() -> None:
    parsed = account_bot_runtime._parse_transfer_notice(
        "转账成功：\n付款人：路人A\n收款人：我的TG名\n金额：100"
    )

    assert parsed == {
        "payer_name": "路人A",
        "receiver_name": "我的TG名",
        "amount": 100,
    }
    compact = account_bot_runtime._parse_transfer_notice(
        "转账成功\nAAA 转出 100\n你心里已经有答案了 收到 100"
    )
    assert compact == {
        "payer_name": "AAA",
        "receiver_name": "你心里已经有答案了",
        "amount": 100,
    }
    assert account_bot_runtime._parse_transfer_notice("转账成功：金额：100") is None


def test_trusted_transfer_notice_sender_requires_configured_sender() -> None:
    assert account_bot_runtime._trusted_transfer_notice_sender_matches({}, 12345) is False
    assert account_bot_runtime._trusted_transfer_notice_sender_matches({"trusted_bot_id": 12345}, 12345) is True
    assert account_bot_runtime._trusted_transfer_notice_sender_matches({"trusted_bot_id": 12345}, 12346) is False
    assert account_bot_runtime._trusted_transfer_notice_sender_matches({"trusted_bot_ids": [12345, 54321]}, 54321) is True
    assert account_bot_runtime._trusted_transfer_notice_sender_matches({"trusted_bot_ids": [12345, 54321]}, 11111) is False
    assert account_bot_runtime._trusted_transfer_notice_sender_matches({"transfer_bot_id": 12345}, 12345) is True
    assert account_bot_runtime._is_configured_bot_user_id({"trusted_bot_ids": [12345], "interaction_bot_id": 54321}, 12345) is True
    assert account_bot_runtime._is_configured_bot_user_id({"trusted_bot_ids": [12345], "interaction_bot_id": 54321}, 54321) is True


@pytest.mark.asyncio
async def test_transfer_notice_configured_bot_receiver_id_does_not_match_rule(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        message_id=70,
        text="转账成功\nAAA 射出 100\nOwner 接收 100\n收款人ID：8875144459",
        display_name="TransferBot",
    )
    parsed = account_bot_runtime._parse_transfer_notice(incoming.text)
    assert parsed is not None
    monkeypatch.setattr(account_bot_runtime, "_list_interaction_sessions_for_rule", AsyncMock(return_value=[]))
    monkeypatch.setattr(account_bot_runtime, "_is_interaction_rule_open", AsyncMock(return_value=True))

    rule = await account_bot_runtime._select_transfer_notice_rule(
        object(),
        incoming,
        {
            "enabled": True,
            "trusted_bot_id": 456,
            "interaction_bot_id": 8875144459,
            "rules": [
                {
                    "id": "paid-game",
                    "enabled": True,
                    "chat_ids": [-100123],
                    "trigger_mode": "payment",
                    "trigger_texts": ["转账成功"],
                    "receiver_text": "Owner",
                    "amount": 100,
                    "amount_match_mode": "eq",
                    "action": "module",
                    "module_key": "math10",
                    "module_action": "start_math_game",
                },
            ],
        },
        parsed,
    )

    assert rule is None


@pytest.mark.asyncio
async def test_transfer_notice_without_matching_rule_records_skipped_route_span(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=12345,
        chat_id=-100123,
        message_id=70,
        text="转账成功\n付款人：AAA\n收款人：Owner\n金额：100",
        display_name="TransferBot",
    )
    record_span = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)

    handled = await account_bot_runtime._try_handle_transfer_notice(
        object(),
        incoming,
        {
            "enabled": True,
            "trusted_bot_ids": [12345],
            "rules": [],
        },
    )

    assert handled is True
    record_span.assert_awaited_once()
    assert record_span.await_args.args[1:3] == ("route", account_bot_runtime.TRACE_STATUS_SKIPPED)
    assert record_span.await_args.kwargs["component"] == "transfer_notice"
    assert record_span.await_args.kwargs["reason_code"] == "filter_not_matched"


@pytest.mark.asyncio
async def test_transfer_notice_hard_rejects_when_trusted_bot_ids_are_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": None,
                "transfer_bot_id": None,
                "rules": [
                    {
                        "id": "paid",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "math10",
                    }
                ],
            }
        ),
    )

    handled = await account_bot_runtime._try_handle_transfer_notice(
        SimpleNamespace(),
        account_bot_runtime.Incoming(
            account_id=1,
            token="bbot-token",
            update_id=1,
            user_id=999999,
            chat_id=-100123,
            message_id=10,
            text="转账成功\nAAA 射出 100\nBBB 接收 100",
        ),
    )

    assert handled is False


@pytest.mark.asyncio
async def test_interaction_update_ignores_message_from_interaction_bot_itself(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    transfer_notice = AsyncMock(return_value=False)
    command_or_keyword = AsyncMock(return_value=False)
    module_message = AsyncMock(return_value=False)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "interaction_bot_id": 8807483916}),
    )
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", transfer_notice)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", command_or_keyword)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 88,
            "message": {
                "message_id": 880,
                "text": "转账成功\n奖金：888",
                "from": {"id": 8807483916, "is_bot": True, "first_name": "Bbot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    transfer_notice.assert_not_awaited()
    command_or_keyword.assert_not_awaited()
    module_message.assert_not_awaited()


def test_account_bot_transfer_notice_template_renders_parseable_notice() -> None:
    template = account_bot_service.default_transfer_notice_config()["transfer_notice_template"]

    notice = account_bot_runtime._render_transfer_bot_notice(
        template,
        "付款方",
        "收款方",
        88,
        payer_user_id=1122,
        receiver_user_id=9988,
    )

    assert notice == (
        '<pre><code class="language-转账成功">付款人：付款方\n'
        "付款人ID：1122\n"
        "收款人：收款方\n"
        "金额：88\n"
        "收款人ID：9988</code></pre>"
    )
    assert "language-转账成功" in notice
    assert account_bot_runtime._parse_transfer_notice(notice) == {
        "payer_name": "付款方",
        "payer_user_id": 1122,
        "receiver_name": "收款方",
        "amount": 88,
        "receiver_user_id": 9988,
    }


def test_account_bot_debit_notice_template_renders_parseable_notice() -> None:
    template = account_bot_service.default_transfer_notice_config()["debit_notice_template"]

    notice = account_bot_runtime._render_transfer_bot_notice(
        template,
        "玩家A",
        "Owner",
        100,
        payer_user_id=111,
        receiver_user_id=999,
    )

    assert notice == (
        '<pre><code class="language-扣减成功">玩家A 扣减 100 蝌蚪\n'
        "Owner 接收 100 蝌蚪</code></pre>"
    )
    assert "language-扣减成功" in notice
    assert account_bot_runtime._parse_transfer_notice(notice) == {
        "payer_name": "玩家A",
        "receiver_name": "Owner",
        "amount": 100,
    }


@pytest.mark.asyncio
async def test_transfer_notice_enriches_anonymous_debit_payer_from_reply_target(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    await save_action_reply_target(
        redis,
        account_id=1,
        chat_id=-100123,
        message_id=700,
        reply_to_user_id=5843467471,
        reply_to_display_name="ㅤㅤ",
    )
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=701,
        text="匿名用户 扣减 1000 蝌蚪\n管理员 接收 1000 蝌蚪",
        display_name="扣款通知Bot",
        reply_to_message_id=700,
        reply_to_user_id=1682400007,
        reply_to_display_name="管理员",
    )

    parsed = account_bot_runtime._parse_incoming_transfer_notice(incoming)
    assert parsed == {"payer_name": "匿名用户", "receiver_name": "管理员", "amount": 1000}
    enriched = await account_bot_runtime._enrich_transfer_notice_payer_from_reply_target(incoming, parsed)

    assert enriched["payer_user_id"] == 5843467471
    assert enriched["payer_name"] == "ㅤㅤ"
    assert enriched["payer_identity_confidence"] == "reply_target"


def _transfer_reply_chain_db(*, account_user_id: int = 222, display_name: str = "Owner"):
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=account_user_id, tg_username="owner", display_name=display_name)
            return None

    return _DB()


def _reply_chain_math_rule(*, trigger_texts: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "paid-chain",
        "name": "链路玩法",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "payment",
        "trigger_texts": trigger_texts or ["转账成功"],
        "receiver_text": None,
        "amount": 100,
        "amount_match_mode": "eq",
        "action": "math10",
        "math_prize": 456,
    }


@pytest.mark.asyncio
async def test_transfer_notice_reply_chain_blocks_forged_same_name_receiver(monkeypatch) -> None:
    redis = _MemoryRedis()
    await save_action_reply_target(
        redis,
        account_id=1,
        chat_id=-100123,
        message_id=619,
        reply_to_user_id=333,
        reply_to_display_name="Owner",
    )
    run_entry = _patch_math10_worker_entry(monkeypatch)
    record_span = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _transfer_reply_chain_db())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "reply_chain_verification_enabled": True,
                "rules": [_reply_chain_math_rule()],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 63,
            "message": {
                "message_id": 620,
                "text": "转账成功\n付款人：玩家A\n收款人：Owner\n金额：100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 619,
                    "text": "+100",
                    "from": {"id": 111, "first_name": "玩家A", "username": "aaa"},
                },
            },
        },
    )

    assert run_entry.await_count == 0
    assert any(call.kwargs.get("reason_code") == "filter_not_matched" for call in record_span.await_args_list)


@pytest.mark.asyncio
async def test_transfer_notice_reply_chain_uses_receiver_id_and_face_amount(monkeypatch) -> None:
    redis = _MemoryRedis()
    await save_action_reply_target(
        redis,
        account_id=1,
        chat_id=-100123,
        message_id=619,
        reply_to_user_id=222,
        reply_to_display_name="Owner",
    )
    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _transfer_reply_chain_db())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "reply_chain_verification_enabled": True,
                "rules": [_reply_chain_math_rule()],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 64,
            "message": {
                "message_id": 620,
                "text": "转账成功\n付款人：玩家A\n收款人：Owner\n金额：95",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 619,
                    "text": "+100",
                    "from": {"id": 111, "first_name": "玩家A", "username": "aaa"},
                },
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["payment"]["amount"] == 100
    assert payload["payment"]["payer_user_id"] == 111
    assert payload["payment"]["receiver_user_id"] == 222
    assert payload["raw"]["parsed"]["notice_amount"] == 95
    assert payload["raw"]["parsed"]["reply_chain_verified"] is True


@pytest.mark.asyncio
async def test_transfer_notice_reply_chain_break_falls_back_to_text_receiver(monkeypatch) -> None:
    redis = _MemoryRedis()
    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _transfer_reply_chain_db())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "reply_chain_verification_enabled": True,
                "rules": [_reply_chain_math_rule()],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 65,
            "message": {
                "message_id": 620,
                "text": "转账成功\n付款人：玩家A\n收款人：Owner\n金额：100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 619,
                    "text": "+100",
                    "from": {"id": 111, "first_name": "玩家A", "username": "aaa"},
                },
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["payment"]["amount"] == 100
    assert payload["payment"]["receiver_display_name"] == "Owner"
    assert "reply_chain_verified" not in payload["raw"]["parsed"]


@pytest.mark.asyncio
async def test_debit_transfer_notice_reply_chain_uses_command_sender_as_receiver(monkeypatch) -> None:
    redis = _MemoryRedis()
    await save_action_reply_target(
        redis,
        account_id=1,
        chat_id=-100123,
        message_id=619,
        reply_to_user_id=333,
        reply_to_display_name="玩家C",
    )
    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _transfer_reply_chain_db())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "reply_chain_verification_enabled": True,
                "rules": [_reply_chain_math_rule(trigger_texts=["扣减成功"])],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 66,
            "message": {
                "message_id": 620,
                "text": "扣减成功",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 619,
                    "text": "-100",
                    "from": {"id": 222, "first_name": "Owner", "username": "owner"},
                },
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["payment"]["amount"] == 100
    assert payload["payment"]["payer_user_id"] == 333
    assert payload["payment"]["receiver_user_id"] == 222
    assert payload["raw"]["parsed"]["reply_chain_mode"] == "debit"


def test_format_user_name_uses_public_name_without_username() -> None:
    assert (
        account_bot_runtime._format_user_name(  # noqa: SLF001
            {"first_name": "你心里已经有答案了", "last_name": "", "username": "uhaveanswer"}
        )
        == "你心里已经有答案了"
    )
    assert account_bot_runtime._format_user_name({"first_name": "A", "last_name": "B", "username": "ab"}) == "A B"  # noqa: SLF001
    assert account_bot_runtime._format_user_name({"username": "only_username"}) is None  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "error_type"),
    [
        ("转账成功\n付款人：{payer_name}\n金额：{amount:.2f}\n收款人：{receiver_name}", "ValueError"),
        ("转账成功\n付款人：{payer_name\n收款人：{receiver_name}\n金额：{amount}", "ValueError"),
    ],
)
async def test_transfer_command_template_render_failure_falls_back_and_logs(
    monkeypatch,
    caplog,
    template: str,
    error_type: str,
) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    send = AsyncMock(return_value={})
    runtime_log = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", runtime_log)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100123],
                "transfer_notice_template": template,
            }
        ),
    )

    with caplog.at_level(logging.WARNING, logger=account_bot_runtime.__name__):
        await account_bot_runtime._handle_transfer_test_update(
            1,
            "bbot-token",
            {
                "update_id": 5,
                "message": {
                    "message_id": 50,
                    "text": "+123",
                    "from": {"id": 999, "first_name": "PayoutUser"},
                    "chat": {"id": -100123, "type": "supergroup"},
                    "reply_to_message": {
                        "message_id": 49,
                        "from": {"id": 111, "first_name": "Winner"},
                        "text": "6",
                    },
                },
            },
        )

    assert send.await_count == 1
    assert send.await_args.args[:3] == (
        "abot-token",
        -100123,
        '<pre><code class="language-转账成功">付款人：PayoutUser\n'
        "付款人ID：999\n"
        "收款人：Winner\n"
        "金额：123\n"
        "收款人ID：111</code></pre>",
    )
    assert runtime_log.await_count == 1
    assert runtime_log.await_args.args[1:] == (
        "warn",
        "转账通知模板渲染失败，已回退默认模板",
    )
    assert runtime_log.await_args.kwargs["error"].startswith(f"{error_type}:")
    assert runtime_log.await_args.kwargs["template"] == template
    assert "transfer notice template render failed aid=1 chat_id=-100123" in caplog.text
    assert error_type in caplog.text


def test_interaction_rule_keywords_match_exact_text_or_notice_line() -> None:
    rule = {"trigger_texts": ["转账成功"]}

    assert account_bot_runtime._rule_matches_trigger(rule, "转账成功\n付款人：AAA")
    assert account_bot_runtime._rule_matches_trigger(rule, "转账成功")
    assert account_bot_runtime._rule_matches_trigger(rule, "aa转账成功") is False
    assert account_bot_runtime._rule_matches_trigger(rule, "转账成功了") is False
    assert account_bot_runtime._message_equals_any("123", ["123"])
    assert account_bot_runtime._message_equals_any("123456", ["123"]) is False
    assert account_bot_runtime._message_equals_any("aa123", ["123"]) is False
    assert account_bot_runtime._message_match_keyword_pattern("置顶 id=12345", ["置顶 id=数字"]) == {"id": "12345"}
    assert account_bot_runtime._message_match_keyword_pattern("置顶 id = 12345", ["置顶 id = 数字"]) == {"id": "12345"}
    assert account_bot_runtime._message_match_keyword_pattern("猜骰 num=1000", ["猜骰 num=数字"]) == {"num": "1000"}
    assert account_bot_runtime._message_match_keyword_pattern("置顶 id=abc", ["置顶 id=数字"]) is None
    assert account_bot_runtime._message_match_keyword_pattern("置顶 id=1 id=2", ["置顶 id=数字 id=数字"]) is None


def test_interaction_payment_payload_preserves_payer_user_id() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        chat_type="group",
        message_id=70,
        text="转账成功",
        display_name="TransferBot",
    )

    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        {"id": "lotto", "module_action": "start_lottery_plus"},
        {"payer_name": "AAA", "payer_user_id": 111, "amount": 10003},
        event_type="payment_confirmed",
    )

    assert payload["payer_user_id"] == 111
    assert payload["payer_name"] == "AAA"
    assert payload["sender_user_id"] == 456
    assert payload["event"]["data"]["payer_user_id"] == 111
    assert payload["source"]["type"] == "payment_confirmed"
    assert payload["source"]["chat_id"] == -100123
    assert payload["source"]["channel"] == "interaction_bot"
    assert payload["source"]["driver"] == "telegram_bot_api"
    assert payload["message"] == {
        "chat_id": -100123,
        "message_id": 70,
        "text": "转账成功",
        "entities": [],
        "media": None,
        "date": None,
        "reply_to_message_id": None,
    }
    assert payload["chat"] == {
        "id": -100123,
        "type": "group",
        "title": None,
        "username": None,
    }
    assert payload["actor"]["user_id"] == 111
    assert payload["actor"]["display_name"] == "AAA"
    assert payload["sender"]["user_id"] == 456
    assert payload["sender"]["display_name"] == "TransferBot"
    assert payload["source_actor"]["user_id"] == 456
    assert payload["source_actor"]["display_name"] == "TransferBot"
    assert payload["payment"]["amount"] == 10003
    assert payload["payment"]["payer_user_id"] == 111
    assert payload["payment"]["payer_display_name"] == "AAA"
    assert payload["payment"]["receiver_display_name"] is None
    assert payload["payment"]["source_message_id"] == 70
    assert payload["payment"]["reply_to_message_id"] is None
    assert payload["payment"]["notice_sender_user_id"] == 456
    assert payload["player"]["user_id"] == 111
    assert payload["player"]["display_name"] == "AAA"
    assert payload["player"]["identity_key"] == "tg:111"
    assert payload["player"]["identity_confidence"] == "verified_user_id"
    assert payload["trigger"]["entry_key"] == "start_lottery_plus"
    assert payload["session"]["scope"] == "chat"
    assert payload["session"]["participant_policy"] == "open_race"
    assert payload["session"]["ttl_seconds"] == 600
    assert payload["raw"]["event_type"] == "payment_confirmed"
    assert payload["raw"]["module_key"] == ""
    assert payload["raw"]["entry_key"] == "start_lottery_plus"
    assert payload["raw"]["parsed"] == {"payer_name": "AAA", "payer_user_id": 111, "amount": 10003}


def test_interaction_module_payload_preserves_normalized_message_and_chat_quality() -> None:
    update = {
        "update_id": 77,
        "message": {
            "message_id": 700,
            "date": 1710000000,
            "caption": "看图猜词",
            "caption_entities": [{"type": "bold", "offset": 0, "length": 2}],
            "photo": [
                {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
                {"file_id": "large", "file_unique_id": "u2", "width": 320, "height": 240},
            ],
            "from": {"id": 111, "first_name": "AAA", "username": "aaa"},
            "chat": {"id": -100777, "type": "supergroup", "title": "玩法群", "username": "game_room"},
        },
    }
    incoming = account_bot_runtime._extract_incoming(1, "bbot-token", update)
    assert incoming is not None

    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        {
            "id": "photo-game",
            "name": "看图",
            "action": "module",
            "module_key": "photo_game",
            "module_action": "answer",
        },
        {"message_text": incoming.text},
        event_type="message",
    )

    assert payload["message"]["date"] == 1710000000
    assert payload["message"]["caption"] == "看图猜词"
    assert payload["message"]["caption_entities"] == [{"type": "bold", "offset": 0, "length": 2}]
    assert payload["message"]["media"]["type"] == "photo"
    assert payload["message"]["media"]["file_id"] == "large"
    assert payload["chat"]["title"] == "玩法群"
    assert payload["chat"]["username"] == "game_room"
    assert payload["raw"]["media"]["type"] == "photo"
    assert payload["raw"]["event_type"] == "message"


def test_interaction_module_payload_normalized_envelope_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=77,
        user_id=111,
        chat_id=-100777,
        chat_type="supergroup",
        message_id=700,
        text="看图猜词",
        display_name="AAA",
        username="aaa",
        reply_to_message_id=699,
        reply_to_text="上一条",
        entity_languages=("zh",),
        native_raw={
            "update_id": 77,
            "message": {
                "message_id": 700,
                "date": 1710000000,
                "caption": "看图猜词",
                "caption_entities": [{"type": "bold", "offset": 0, "length": 2}],
                "photo": [
                    {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
                    {"file_id": "large", "file_unique_id": "u2", "width": 320, "height": 240},
                ],
                "from": {"id": 111, "first_name": "AAA", "username": "aaa"},
                "chat": {"id": -100777, "type": "supergroup", "title": "玩法群", "username": "game_room"},
            },
        },
    )
    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        {
            "id": "photo-game",
            "name": "看图",
            "action": "module",
            "module_key": "photo_game",
            "module_action": "answer",
            "module_config": {"timeout": 45},
        },
        {"keyword": "看图"},
        event_type="message",
    )

    assert sorted(payload.keys()) == [
        "account_id",
        "actor",
        "callback_data",
        "callback_query_id",
        "chat",
        "chat_id",
        "chosen_inline_result",
        "entity_languages",
        "entry_key",
        "event",
        "event_type",
        "inline_query",
        "keyword",
        "message",
        "message_id",
        "message_text",
        "module_config",
        "native_raw",
        "native_raw_meta",
        "payer_name",
        "payer_user_id",
        "payment",
        "player",
        "prize",
        "raw",
        "reply_to",
        "reply_to_sender_chat_id",
        "reply_to_sender_chat_title",
        "reply_to_sender_chat_type",
        "reply_to_sender_chat_username",
        "reply_to_text",
        "rule_id",
        "rule_name",
        "sender",
        "sender_chat_id",
        "sender_chat_title",
        "sender_chat_type",
        "sender_chat_username",
        "sender_name",
        "sender_user_id",
        "sender_username",
        "session",
        "source",
        "source_actor",
        "source_message_id",
        "source_update_id",
        "timeout",
        "trace_id",
        "trigger",
        "valid_seconds",
    ]
    assert payload["message"]["date"] == 1710000000
    assert payload["message"]["media"]["file_id"] == "large"
    assert payload["message"]["reply_to_message_id"] == 699
    assert payload["chat"]["title"] == "玩法群"
    assert payload["chat"]["username"] == "game_room"
    assert payload["raw"]["media"]["file_id"] == "large"
    assert payload["raw"]["parsed"] == {"keyword": "看图"}
    assert payload["source"]["channel"] == "interaction_bot"
    assert payload["inline_query"] is None
    assert payload["chosen_inline_result"] is None


def test_event_bus_payment_payload_uses_replied_user_as_player() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        chat_type="group",
        message_id=70,
        text="玩家A 射出 7895 蝌蚪\nOwner 接收 7895 蝌蚪",
        display_name="TransferBot",
        username="transfer_bot",
        reply_to_user_id=111,
        reply_to_message_id=66,
        reply_to_display_name="玩家A",
        reply_to_username="aaa",
        reply_to_text="+7895",
    )
    event = {
        "source": {"type": "payment_confirmed", "chat_id": -100123, "message_id": 70},
        "sender": {"user_id": 456, "display_name": "TransferBot", "username": "transfer_bot"},
        "actor": {"user_id": None, "display_name": "玩家A"},
        "player": {"user_id": None, "display_name": "玩家A"},
        "raw": {"event_type": "payment_confirmed"},
    }
    decision = SimpleNamespace(
        plugin_key="ten_half",
        entry_key="start_ten_half",
        dispatch_mode="event_subscription",
        scope="all_allowed_chats",
        filters={},
    )

    payload = account_bot_runtime._event_bus_payment_plugin_payload(
        incoming,
        event,
        decision,
        {"payer_name": "玩家A", "receiver_name": "Owner", "amount": 7895},
    )

    assert payload["event_type"] == "payment_confirmed"
    assert payload["sender"]["user_id"] == 456
    assert payload["source_actor"]["user_id"] == 456
    assert payload["actor"]["user_id"] == 111
    assert payload["actor"]["display_name"] == "玩家A"
    assert payload["player"]["user_id"] == 111
    assert payload["player"]["identity_confidence"] == "reply_context"
    assert payload["payment"]["amount"] == 7895
    assert payload["payment"]["payer_user_id"] == 111
    assert payload["payment"]["payer_display_name"] == "玩家A"
    assert payload["payment"]["notice_sender_user_id"] == 456
    assert payload["reply_to"]["user_id"] == 111
    assert payload["payer_user_id"] == 111
    assert payload["payer_name"] == "玩家A"
    assert payload["sender_user_id"] == 456


@pytest.mark.asyncio
async def test_resolve_payout_mode_accepts_math10_module_rule(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "enabled": True,
                        "action": "module",
                        "module_key": "math10",
                        "chat_ids": [-100123],
                    }
                ],
            }
        ),
    )

    assert await account_bot_runtime._resolve_payout_mode(1, -100123) == "auto"


@pytest.mark.asyncio
async def test_resolve_payout_mode_accepts_dice_grid_module_rule(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "enabled": True,
                        "action": "module",
                        "module_key": "dice_grid_hunt",
                        "chat_ids": [-100123],
                    }
                ],
            }
        ),
    )

    assert await account_bot_runtime._resolve_payout_mode(1, -100123) == "auto"


@pytest.mark.asyncio
async def test_resolve_payout_mode_uses_supplied_cfg_for_any_module_rule(monkeypatch) -> None:
    get_config = AsyncMock()
    monkeypatch.setattr(account_bot_service, "get_transfer_notice_config", get_config)

    cfg = {
        "enabled": True,
        "rules": [
            {
                "enabled": True,
                "action": "module",
                "module_key": "custom_game",
                "chat_ids": [-100123],
            }
        ],
    }

    assert await account_bot_runtime._resolve_payout_mode(1, -100123, cfg=cfg) == "auto"
    get_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_payload_sets_auto_payout_mode_for_math10_module(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, key):  # noqa: ANN001
            if model is account_bot_runtime.Account and key == 1:
                return SimpleNamespace(tg_username="owner", tg_user_id=999)
            return None

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        message_id=70,
        text="10",
        display_name="AAA",
    )
    rule = {
        "id": "math10-ticket",
        "action": "module",
        "module_key": "math10",
        "module_action": "start_math10",
        "chat_ids": [-100123],
    }
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [rule],
            }
        ),
    )

    payload = await account_bot_runtime._interaction_module_payload_async(
        incoming,
        rule,
        None,
        event_type="message",
    )

    assert payload["payout_account_label"] == "@owner"
    assert payload["owner_user_ids"] == [999]
    assert payload["admin_user_ids"] == [999]
    assert payload["userbot_user_id"] == 999
    assert payload["payout_mode"] == "auto"
    assert payload["settlement"]["mode"] == "auto"
    assert payload["settlement"]["amount"] == 0
    assert payload["settlement"]["payout_account_label"] == "@owner"


@pytest.mark.asyncio
async def test_interaction_start_session_action_merges_paid_participants(monkeypatch) -> None:
    redis = _MemoryRedis()
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=999,
        chat_id=-100123,
        message_id=801,
        text="加入",
    )
    rule = {
        "id": "ten-half",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "participant_policy": "paid_pool",
    }
    action = {
        "type": "start_session",
        "chat_id": -100123,
        "entry_key": "start_ten_half",
        "event_type": "message",
        "started_by_user_id": 999,
        "paid_user_ids": [111, 999],
        "participant_user_ids": [111, 999],
    }
    record_action = AsyncMock()
    emit_action_event = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)
    monkeypatch.setattr(account_bot_runtime, "emit_action_event", emit_action_event)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_manifest", lambda *_args, **_kwargs: None)

    await account_bot_runtime._apply_interaction_start_session_action(incoming, rule, action)

    key = account_bot_runtime._interaction_session_key(1, rule, -100123, 999)
    saved = json.loads(redis.data[key])
    assert saved["started_by_user_id"] == 999
    assert saved["paid_user_ids"] == [111, 999]
    assert saved["participant_user_ids"] == [111, 999]
    assert saved["channel"] == "interaction_bot"
    assert isinstance(saved["expires_at"], float)
    assert redis.set_calls[-1][2]["ex"] == 600 + account_bot_runtime._INTERACTION_SESSION_TTL_GRACE_SECONDS
    record_action.assert_awaited_once()
    emit_action_event.assert_awaited_once()
    assert emit_action_event.await_args.kwargs["action"]["type"] == "start_session"
    assert emit_action_event.await_args.kwargs["action"]["participant_user_ids"] == [111, 999]
    assert emit_action_event.await_args.kwargs["channel"] == "interaction_session"


def test_interaction_entry_manifest_normalizes_command_fallback() -> None:
    entry = account_bot_service.normalize_interaction_entry_manifest(
        {
            "key": "start_paid_game",
            "launch_mode": "hybrid",
            "session_scope": "chat",
            "events": ["payment_confirmed", "bad", "message", "callback_query"],
            "command_fallback": {"enabled": True, "command": "24d", "mode": "hint_only"},
            "result_contract": {"send_via": ["interaction_bot", "bad", "userbot_reply", "interaction_bot"]},
        }
    )

    assert entry is not None
    assert entry["launch_mode"] == "hybrid"
    assert entry["events"] == ["payment_confirmed", "message", "callback_query"]
    assert entry["dispatch_modes"] == ["admin_command", "public_keyword"]
    assert entry["message_channels"] == {
        "admin_command": "userbot_reply",
        "public_keyword": "interaction_bot",
    }
    assert entry["money_channel"] == "userbot_reply"
    assert entry["preserve_command_trigger"] is True
    assert entry["command_fallback"] == {"enabled": True, "command": "24d", "mode": "hint_only"}
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "bad", "userbot_reply"]


def test_interaction_entry_manifest_keeps_invalid_declared_send_via_for_guard() -> None:
    entry = account_bot_service.normalize_interaction_entry_manifest(
        {
            "key": "trusted_game",
            "launch_mode": "bridge",
            "session_scope": "chat",
            "result_contract": {"send_via": ["bad"]},
        }
    )

    assert entry is not None
    assert entry["dispatch_modes"] == ["public_keyword"]
    assert entry["message_channels"] == {"public_keyword": "interaction_bot"}
    assert entry["result_contract"]["send_via"] == ["bad"]


def test_interaction_entry_manifest_accepts_channel_preferences_and_aliases() -> None:
    entry = account_bot_service.normalize_interaction_entry_manifest(
        {
            "key": "trusted_game",
            "launch_mode": "hybrid",
            "session_scope": "chat",
            "message_channels": {
                "admin_command": ["userbot", "bot"],
                "public_keyword": {"prefer": ["bot", "userbot"], "fallback": True},
            },
            "result_contract": {"send_via": ["bot", "userbot", "notice"]},
        }
    )

    assert entry is not None
    assert entry["message_channels"] == {
        "admin_command": ["userbot_reply", "interaction_bot"],
        "public_keyword": {"prefer": ["interaction_bot", "userbot_reply"], "fallback": True},
    }
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply", "notice"]


@pytest.mark.asyncio
async def test_interaction_result_contract_warns_unsupported_mixed_channel_options(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
        trace_id="evt_mixed_notice",
    )
    write_log = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "declared_module_entry_manifest", lambda *_args: {})
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", write_log)
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [
            {
                "type": "send_message",
                "channel": {"prefer": ["bot", "notice"], "fallback": True},
                "text": "ok",
            },
        ],
    )

    assert guarded == []
    assert write_log.await_count == 1
    assert write_log.await_args.args[1:3] == ("warn", "interaction action failed: deprecated send_via")
    assert write_log.await_args.kwargs["guard_level"] == "failed"
    assert write_log.await_args.kwargs["reason_code"] == "send_channel_deprecated"
    assert write_log.await_args.kwargs["unsupported_send_via"] == ["notice"]
    assert record_action.await_args.kwargs["error_code"] == "send_channel_deprecated"


@pytest.mark.asyncio
async def test_interaction_result_contract_ignores_removed_channel_in_manifest(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="",
    )
    write_log = AsyncMock()
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {"result_contract": {"send_via": ["interaction_bot", "notice"]}},
    )
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", write_log)

    guarded = await account_bot_runtime._guard_interaction_actions(
        incoming,
        {"module_key": "demo", "module_action": "start"},
        [
            {
                "type": "send_message",
                "channel": {"prefer": ["bot", "notice"], "fallback": True},
                "text": "ok",
            },
        ],
    )

    assert guarded == []
    assert write_log.await_count == 1
    assert write_log.await_args.kwargs["guard_level"] == "failed"
    assert write_log.await_args.kwargs["reason_code"] == "send_channel_deprecated"
    assert write_log.await_args.kwargs["unsupported_send_via"] == ["notice"]


def test_interaction_entry_manifest_accepts_result_contract_channel_selector() -> None:
    entry = account_bot_service.normalize_interaction_entry_manifest(
        {
            "key": "trusted_game",
            "launch_mode": "bridge",
            "session_scope": "chat",
            "result_contract": {"send_via": {"prefer": ["bot", "userbot"], "fallback": True}},
        }
    )

    assert entry is not None
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply"]


@pytest.mark.asyncio
async def test_interaction_polling_requests_callback_query_updates(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    captured_payloads: list[dict[str, object]] = []

    async def _load_config(_aid: int):
        return "bbot-token", {
            "enabled": True,
            "interaction_last_update_id": None,
        }

    async def _call_bot_api(_token: str, _method: str, payload: dict[str, object]):
        captured_payloads.append(payload)
        raise asyncio.CancelledError()

    monkeypatch.setattr(account_bot_runtime, "_load_interaction_runtime_config", _load_config)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_set_interaction_runtime_state", AsyncMock())
    monkeypatch.setattr(account_bot_service, "call_bot_api", _call_bot_api)

    with pytest.raises(asyncio.CancelledError):
        await account_bot_runtime._interaction_polling_loop(1)

    assert captured_payloads
    assert captured_payloads[0]["allowed_updates"] == [
        "message",
        "edited_message",
        "callback_query",
        "inline_query",
        "chosen_inline_result",
    ]


@pytest.mark.asyncio
async def test_interaction_polling_respects_inline_updates_switch(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    captured_payloads: list[dict[str, object]] = []

    async def _load_config(_aid: int):
        return "bbot-token", {
            "enabled": True,
            "interaction_last_update_id": None,
        }

    async def _call_bot_api(_token: str, _method: str, payload: dict[str, object]):
        captured_payloads.append(payload)
        raise asyncio.CancelledError()

    monkeypatch.setattr(account_bot_runtime, "_load_interaction_runtime_config", _load_config)
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"inline_updates_enabled": False}))
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_set_interaction_runtime_state", AsyncMock())
    monkeypatch.setattr(account_bot_service, "call_bot_api", _call_bot_api)

    with pytest.raises(asyncio.CancelledError):
        await account_bot_runtime._interaction_polling_loop(1)

    assert captured_payloads
    assert captured_payloads[0]["allowed_updates"] == ["message", "edited_message", "callback_query"]


@pytest.mark.asyncio
async def test_handle_interaction_update_reads_transfer_config_once_for_matching_route(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    cfg = {"enabled": True, "rules": [], "interaction_bot_id": 999}
    get_config = AsyncMock(return_value=cfg)
    payment_confirm = AsyncMock(return_value=False)
    transfer_command = AsyncMock(return_value=True)
    transfer_notice = AsyncMock(return_value=False)
    rule_command = AsyncMock(return_value=False)
    module_message = AsyncMock(return_value=False)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False}))
    monkeypatch.setattr(account_bot_service, "get_transfer_notice_config", get_config)
    monkeypatch.setattr(account_bot_runtime, "_incoming_is_userbot_command_text", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_payment_confirm", payment_confirm)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", transfer_command)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", transfer_notice)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", rule_command)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7001,
            "message": {
                "message_id": 701,
                "text": "+5",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    get_config.assert_awaited_once()
    transfer_command.assert_awaited_once()
    assert transfer_command.await_args.args[-1] is cfg
    payment_confirm.assert_not_awaited()
    transfer_notice.assert_not_awaited()
    rule_command.assert_not_awaited()
    module_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_interaction_update_skips_unmatched_cached_message_without_db(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            raise AssertionError("unmatched cached interaction message should not open a DB session")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    get_config = AsyncMock()
    transfer_command = AsyncMock(return_value=False)
    transfer_notice = AsyncMock(return_value=False)
    event_bus = AsyncMock(return_value=(False, True))
    rule_command = AsyncMock(return_value=False)
    module_message = AsyncMock(return_value=False)
    account_bot_runtime._set_interaction_routing_index_cache(
        1,
        {"enabled": True, "rules": [], "chat_ids": [-100777], "interaction_bot_id": 999},
        subscriptions=[],
        active_session_chat_ids=set(),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_service, "get_transfer_notice_config", get_config)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", transfer_command)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", transfer_notice)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", event_bus)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", rule_command)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7002,
            "message": {
                "message_id": 702,
                "text": "普通消息",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    get_config.assert_not_awaited()
    transfer_command.assert_not_awaited()
    transfer_notice.assert_not_awaited()
    event_bus.assert_not_awaited()
    rule_command.assert_not_awaited()
    module_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_interaction_update_routes_active_session_before_wide_event_bus_on_cold_cache(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    account_bot_runtime._INTERACTION_ROUTING_INDEX_CACHE.pop(1, None)
    event_bus = AsyncMock(return_value=(True, True))
    rule_command = AsyncMock(return_value=False)
    module_message = AsyncMock(return_value=True)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", event_bus)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", rule_command)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 999,
                "rules": [
                    {
                        "id": "active-game",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_game24",
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7003,
            "message": {
                "message_id": 703,
                "text": "24",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    module_message.assert_awaited_once()
    event_bus.assert_not_awaited()
    rule_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_interaction_update_routes_active_callback_session_before_wide_event_bus_on_cold_cache(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    account_bot_runtime._INTERACTION_ROUTING_INDEX_CACHE.pop(1, None)
    event_bus = AsyncMock(return_value=(True, True))
    module_message = AsyncMock(return_value=True)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", event_bus)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 999,
                "rules": [
                    {
                        "id": "active-game",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_game24",
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7004,
            "callback_query": {
                "id": "cb-global",
                "data": "th:dealer_yes:2001",
                "from": {"id": 111, "first_name": "AAA"},
                "message": {
                    "message_id": 704,
                    "text": "十点半选庄",
                    "chat": {"id": -100777, "type": "supergroup"},
                },
            },
        },
    )

    module_message.assert_awaited_once()
    event_bus.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_interaction_update_falls_back_to_callback_event_bus_when_session_misses(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    account_bot_runtime._INTERACTION_ROUTING_INDEX_CACHE.pop(1, None)
    event_bus = AsyncMock(return_value=(True, True))
    module_message = AsyncMock(return_value=False)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", event_bus)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 999,
                "rules": [
                    {
                        "id": "active-game",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_game24",
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7005,
            "callback_query": {
                "id": "cb-global",
                "data": "global:claim",
                "from": {"id": 111, "first_name": "AAA"},
                "message": {
                    "message_id": 705,
                    "text": "全局按钮",
                    "chat": {"id": -100777, "type": "supergroup"},
                },
            },
        },
    )

    module_message.assert_awaited_once()
    event_bus.assert_awaited_once()


@pytest.mark.asyncio
async def test_interaction_runtime_state_uses_dedicated_setting_not_config_row(monkeypatch) -> None:
    transfer_key = account_bot_service.transfer_notice_setting_key(1)
    state_key = account_bot_runtime._interaction_runtime_state_setting_key(1)
    config_row = account_bot_service.SystemSetting(
        key=transfer_key,
        value={
            "enabled": True,
            "interaction_last_update_id": 7,
            "interaction_last_error": "old config error",
        },
    )

    class _DB:
        def __init__(self) -> None:
            self.rows = {transfer_key: config_row}
            self.commits = 0

        async def get(self, model, key):  # noqa: ANN001
            if model is account_bot_service.SystemSetting:
                return self.rows.get(key)
            return None

        def add(self, row):  # noqa: ANN001
            self.rows[row.key] = row

        async def commit(self):
            self.commits += 1

    db = _DB()

    await account_bot_runtime._set_interaction_runtime_state(
        db,
        1,
        last_update_id=42,
        error="handler failed",
    )

    assert config_row.value["interaction_last_update_id"] == 7
    assert config_row.value["interaction_last_error"] == "old config error"
    assert db.rows[state_key].value == {
        "interaction_last_update_id": 42,
        "interaction_last_error": "handler failed",
    }


@pytest.mark.asyncio
async def test_interaction_runtime_config_migrates_legacy_cursor_once(monkeypatch) -> None:
    state_key = account_bot_runtime._interaction_runtime_state_setting_key(1)

    class _Session:
        def __init__(self) -> None:
            self.rows: dict[str, object] = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, key):  # noqa: ANN001
            if model is account_bot_service.SystemSetting:
                return self.rows.get(key)
            return None

        def add(self, row):  # noqa: ANN001
            self.rows[row.key] = row

        async def commit(self):
            return None

    session = _Session()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_last_update_id": 99,
                "interaction_last_error": "legacy error",
            }
        ),
    )
    monkeypatch.setattr(account_bot_service, "get_interaction_bot_token", AsyncMock(return_value="bbot-token"))

    token, cfg = await account_bot_runtime._load_interaction_runtime_config(1)

    assert token == "bbot-token"
    assert cfg["interaction_last_update_id"] == 99
    assert cfg["interaction_last_error"] == "legacy error"
    assert session.rows[state_key].value == {
        "interaction_last_update_id": 99,
        "interaction_last_error": "legacy error",
    }


@pytest.mark.asyncio
async def test_interaction_polling_retries_failed_update_then_advances_offset_with_error(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    call_count = 0

    async def _call_bot_api(_token: str, _method: str, _payload: dict[str, object]):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "result": [
                    {
                        "update_id": 42,
                        "message": {
                            "message_id": 10,
                            "text": "hello",
                            "from": {"id": 111, "first_name": "A"},
                            "chat": {"id": -100, "type": "supergroup"},
                        },
                    }
                ]
            }
        raise asyncio.CancelledError()

    state = AsyncMock()
    handle = AsyncMock(side_effect=RuntimeError("boom"))
    runtime_log = AsyncMock()
    record_dlq = AsyncMock()

    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_load_interaction_runtime_config",
        AsyncMock(return_value=("bbot-token", {"enabled": True, "interaction_last_update_id": None})),
    )
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"inline_updates_enabled": False}))
    monkeypatch.setattr(account_bot_service, "call_bot_api", _call_bot_api)
    monkeypatch.setattr(account_bot_runtime, "_handle_interaction_update", handle)
    monkeypatch.setattr(account_bot_runtime, "_set_interaction_runtime_state", state)
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", runtime_log)
    monkeypatch.setattr(account_bot_runtime, "_record_polling_dead_letter", record_dlq)

    async def _sleep(_seconds: float):
        return None

    monkeypatch.setattr(account_bot_runtime.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await account_bot_runtime._interaction_polling_loop(1)

    assert handle.await_count == 3
    assert any(
        call.kwargs.get("last_update_id") == 42 and "boom" in str(call.kwargs.get("error"))
        for call in state.await_args_list
    )
    runtime_log.assert_awaited_once()
    record_dlq.assert_awaited_once()
    assert record_dlq.await_args.kwargs["loop"] == "interaction"
    assert record_dlq.await_args.kwargs["token_role"] == "interaction"
    assert record_dlq.await_args.kwargs["attempts"] == 3
    assert record_dlq.await_args.kwargs["update"]["update_id"] == 42
    assert record_dlq.await_args.kwargs["update"]["message"]["text"] == "hello"


@pytest.mark.asyncio
async def test_polling_dead_letter_recorded_and_capped(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_POLLING_DLQ_MAX", 3)

    for update_id in range(1, 6):
        await account_bot_runtime._record_polling_dead_letter(
            aid=1,
            loop="interaction",
            token_role="interaction",
            update={"update_id": update_id, "message": {"text": f"m{update_id}"}},
            error=f"boom {update_id}",
            attempts=3,
        )

    items = await account_bot_runtime._list_polling_dead_letters(1, limit=10)

    assert [item["update_id"] for item in items] == [5, 4, 3]
    assert await account_bot_runtime._count_polling_dead_letters(1) == 3
    assert await account_bot_runtime._get_polling_dead_letter(1, "interaction:1") is None
    assert await account_bot_runtime._get_polling_dead_letter(1, "interaction:2") is None


@pytest.mark.asyncio
async def test_polling_dead_letter_replay_success_removes_entry(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    handler = AsyncMock()
    monkeypatch.setattr(
        account_bot_runtime,
        "_load_interaction_runtime_config",
        AsyncMock(return_value=("bbot-token", {"enabled": True})),
    )
    monkeypatch.setattr(account_bot_runtime, "_handle_interaction_update", handler)
    await account_bot_runtime._record_polling_dead_letter(
        aid=1,
        loop="interaction",
        token_role="interaction",
        update={"update_id": 42, "message": {"text": "hello"}},
        error="boom",
        attempts=3,
    )

    result = await account_bot_runtime._replay_polling_dead_letter(1, "interaction:42")

    assert result == {"ok": True, "deleted": True, "error": None}
    handler.assert_awaited_once_with(1, "bbot-token", {"update_id": 42, "message": {"text": "hello"}})
    assert await account_bot_runtime._get_polling_dead_letter(1, "interaction:42") is None
    assert await account_bot_runtime._count_polling_dead_letters(1) == 0


@pytest.mark.asyncio
async def test_polling_dead_letter_replay_failure_keeps_entry(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(
        account_bot_runtime,
        "_load_interaction_runtime_config",
        AsyncMock(return_value=("bbot-token", {"enabled": True})),
    )
    monkeypatch.setattr(account_bot_runtime, "_handle_interaction_update", AsyncMock(side_effect=RuntimeError("boom again")))
    await account_bot_runtime._record_polling_dead_letter(
        aid=1,
        loop="interaction",
        token_role="interaction",
        update={"update_id": 42},
        error="boom",
        attempts=3,
    )

    result = await account_bot_runtime._replay_polling_dead_letter(1, "interaction:42")
    item = await account_bot_runtime._get_polling_dead_letter(1, "interaction:42")

    assert result["ok"] is False
    assert result["deleted"] is False
    assert "boom again" in result["error"]
    assert item is not None
    assert "boom again" in item["error"]
    assert item["replay_attempts"] == 1
    assert item["last_replayed_at"] is not None
    assert item["failed_at"] == redis.zsets[account_bot_runtime._polling_dlq_idx_key(1)]["interaction:42"]


@pytest.mark.asyncio
async def test_dlq_write_failure_does_not_break_offset_advance(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    call_count = 0

    async def _call_bot_api(_token: str, _method: str, _payload: dict[str, object]):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"result": [{"update_id": 42, "message": {"message_id": 10, "text": "hello", "chat": {"id": -100}}}]}
        raise asyncio.CancelledError()

    state = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_load_interaction_runtime_config",
        AsyncMock(return_value=("bbot-token", {"enabled": True, "interaction_last_update_id": None})),
    )
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"inline_updates_enabled": False}))
    monkeypatch.setattr(account_bot_service, "call_bot_api", _call_bot_api)
    monkeypatch.setattr(account_bot_runtime, "_handle_interaction_update", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(account_bot_runtime, "_set_interaction_runtime_state", state)
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_record_polling_dead_letter", AsyncMock(side_effect=RuntimeError("redis down")))

    async def _sleep(_seconds: float):
        return None

    monkeypatch.setattr(account_bot_runtime.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await account_bot_runtime._interaction_polling_loop(1)

    assert any(
        call.kwargs.get("last_update_id") == 42 and "boom" in str(call.kwargs.get("error"))
        for call in state.await_args_list
    )


@pytest.mark.asyncio
async def test_management_polling_failure_records_dlq_with_management_loop(monkeypatch) -> None:
    class _Result:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class _DB:
        def __init__(self, row):
            self.row = row

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):
            return _Result(self.row)

        async def commit(self):
            return None

    row = AccountBot(account_id=1, enabled=True, bot_token_enc="enc")
    call_count = 0

    async def _call_bot_api(_token: str, _method: str, _payload: dict[str, object]):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"result": [{"update_id": 7, "message": {"message_id": 1, "text": "/x", "chat": {"id": 1}}}]}
        raise asyncio.CancelledError()

    record_dlq = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB(row))
    monkeypatch.setattr(account_bot_service, "decrypt_bot_token", lambda _row: "management-token")
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"inline_updates_enabled": False}))
    monkeypatch.setattr(account_bot_service, "call_bot_api", _call_bot_api)
    monkeypatch.setattr(account_bot_runtime, "_handle_update", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_record_polling_dead_letter", record_dlq)

    async def _sleep(_seconds: float):
        return None

    monkeypatch.setattr(account_bot_runtime.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await account_bot_runtime._polling_loop(1)

    record_dlq.assert_awaited_once()
    assert record_dlq.await_args.kwargs["loop"] == "management"
    assert record_dlq.await_args.kwargs["token_role"] == "management"
    assert record_dlq.await_args.kwargs["update"]["update_id"] == 7


@pytest.mark.asyncio
async def test_transfer_test_polling_failure_records_dlq_with_transfer_loop(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    call_count = 0

    async def _call_bot_api(_token: str, _method: str, _payload: dict[str, object]):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"result": [{"update_id": 8, "message": {"message_id": 1, "text": "+1", "chat": {"id": -100}}}]}
        raise asyncio.CancelledError()

    record_dlq = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_load_transfer_test_runtime_config",
        AsyncMock(return_value=("transfer-token", {"enabled": True, "transfer_last_update_id": None})),
    )
    monkeypatch.setattr(account_bot_service, "call_bot_api", _call_bot_api)
    monkeypatch.setattr(account_bot_runtime, "_handle_transfer_test_update", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(account_bot_runtime, "_set_transfer_test_runtime_state", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_record_polling_dead_letter", record_dlq)

    async def _sleep(_seconds: float):
        return None

    monkeypatch.setattr(account_bot_runtime.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await account_bot_runtime._transfer_test_polling_loop(1)

    record_dlq.assert_awaited_once()
    assert record_dlq.await_args.kwargs["loop"] == "transfer_test"
    assert record_dlq.await_args.kwargs["token_role"] == "transfer_test"
    assert record_dlq.await_args.kwargs["update"]["update_id"] == 8


@pytest.mark.asyncio
async def test_polling_dead_letter_ids_do_not_collide_across_loops(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    await account_bot_runtime._record_polling_dead_letter(
        aid=1,
        loop="interaction",
        token_role="interaction",
        update={"update_id": 42, "message": {"text": "interaction"}},
        error="boom",
        attempts=3,
    )
    await account_bot_runtime._record_polling_dead_letter(
        aid=1,
        loop="transfer_test",
        token_role="transfer_test",
        update={"update_id": 42, "message": {"text": "transfer"}},
        error="boom",
        attempts=3,
    )

    assert {item["dlq_id"] for item in await account_bot_runtime._list_polling_dead_letters(1)} == {
        "interaction:42",
        "transfer_test:42",
    }

    assert await account_bot_runtime._discard_polling_dead_letter(1, "interaction:42") is True
    assert await account_bot_runtime._get_polling_dead_letter(1, "interaction:42") is None
    assert await account_bot_runtime._get_polling_dead_letter(1, "transfer_test:42") is not None

    monkeypatch.setattr(
        account_bot_runtime,
        "_load_transfer_test_runtime_config",
        AsyncMock(return_value=("transfer-token", {"enabled": True})),
    )
    monkeypatch.setattr(account_bot_runtime, "_handle_transfer_test_update", AsyncMock())
    result = await account_bot_runtime._replay_polling_dead_letter(1, "transfer_test:42")

    assert result["ok"] is True
    assert await account_bot_runtime._count_polling_dead_letters(1) == 0


@pytest.mark.asyncio
async def test_interaction_polling_batch_keeps_same_chat_order_and_parallelizes_other_chats() -> None:
    events: list[tuple[str, int, int]] = []
    release_first_chat = asyncio.Event()

    async def _handler(_aid: int, _token: str, update: dict[str, object]) -> None:
        message = update["message"]
        chat_id = int(message["chat"]["id"])
        update_id = int(update["update_id"])
        events.append(("start", chat_id, update_id))
        if chat_id == -100 and update_id == 1:
            await release_first_chat.wait()
        events.append(("end", chat_id, update_id))

    task = asyncio.create_task(
        account_bot_runtime._handle_interaction_polling_updates_batch(
            aid=1,
            token="bbot-token",
            updates=[
                {"update_id": 1, "message": {"message_id": 1, "text": "a1", "from": {"id": 1}, "chat": {"id": -100}}},
                {"update_id": 2, "message": {"message_id": 2, "text": "a2", "from": {"id": 1}, "chat": {"id": -100}}},
                {"update_id": 3, "message": {"message_id": 3, "text": "b1", "from": {"id": 2}, "chat": {"id": -200}}},
            ],
            handler=_handler,
        )
    )
    for _ in range(20):
        if ("start", -200, 3) in events:
            break
        await asyncio.sleep(0)

    assert ("start", -200, 3) in events
    assert ("start", -100, 2) not in events
    release_first_chat.set()

    last_update_id, batch_error = await task

    assert last_update_id == 3
    assert batch_error is None
    assert events.index(("end", -100, 1)) < events.index(("start", -100, 2))


@pytest.mark.asyncio
async def test_interaction_polling_batch_limits_global_concurrency() -> None:
    active = 0
    max_active = 0
    started = 0
    first_wave_started = asyncio.Event()
    release = asyncio.Event()

    async def _handler(_aid: int, _token: str, _update: dict[str, object]) -> None:
        nonlocal active, max_active, started
        active += 1
        started += 1
        max_active = max(max_active, active)
        if started == account_bot_runtime._INTERACTION_UPDATE_CONCURRENCY:
            first_wave_started.set()
        await release.wait()
        active -= 1

    updates = [
        {
            "update_id": index + 1,
            "message": {
                "message_id": index + 1,
                "text": f"m{index}",
                "from": {"id": index + 10},
                "chat": {"id": -1000 - index},
            },
        }
        for index in range(account_bot_runtime._INTERACTION_UPDATE_CONCURRENCY + 4)
    ]
    task = asyncio.create_task(
        account_bot_runtime._handle_interaction_polling_updates_batch(
            aid=1,
            token="bbot-token",
            updates=updates,
            handler=_handler,
        )
    )

    await asyncio.wait_for(first_wave_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert started == account_bot_runtime._INTERACTION_UPDATE_CONCURRENCY
    assert max_active == account_bot_runtime._INTERACTION_UPDATE_CONCURRENCY

    release.set()
    last_update_id, batch_error = await task

    assert started == len(updates)
    assert last_update_id == len(updates)
    assert batch_error is None


@pytest.mark.asyncio
async def test_interaction_payload_hides_native_raw_without_declared_capability(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="hello",
        native_raw={"update_id": 10, "message": {"text": "hello"}},
    )
    rule = {
        "id": "r1",
        "name": "demo",
        "module_key": "demo",
        "module_action": "start",
        "module_config": {},
    }
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="auto"))
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)

    payload = await account_bot_runtime._interaction_module_payload_async(
        incoming,
        rule,
        None,
        event_type="message",
    )

    assert payload["native_raw"] is None
    assert payload["native_raw_meta"]["enabled"] is False
    assert payload["native_raw_meta"]["reason_code"] == "native_raw_not_allowed"


@pytest.mark.asyncio
async def test_interaction_payload_includes_native_raw_with_declared_capability(monkeypatch) -> None:
    raw = {"update_id": 10, "message": {"text": "hello"}}
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="hello",
        native_raw=raw,
    )
    rule = {
        "id": "r1",
        "name": "demo",
        "module_key": "demo",
        "module_action": "start",
        "module_config": {},
    }
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="auto"))
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: True)

    payload = await account_bot_runtime._interaction_module_payload_async(
        incoming,
        rule,
        None,
        event_type="message",
    )

    assert payload["native_raw"] == raw
    assert payload["native_raw_meta"]["enabled"] is True
    assert payload["native_raw_meta"]["reason_code"] is None


def test_event_bus_payload_removes_raw_event_backdoor_without_native_raw_capability(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="hello",
        trace_id="evt_raw",
        native_raw={"update_id": 10, "message": {"text": "hello"}},
    )
    event = account_bot_runtime._incoming_trace_payload(incoming)
    event["raw_event"] = {"message": {"text": "should not leak"}}
    decision = SimpleNamespace(
        plugin_key="demo",
        entry_key="start",
        dispatch_mode="event_subscription",
        scope="all_allowed_chats",
        filters={},
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)

    payload = account_bot_runtime._event_bus_plugin_payload(incoming, event, decision)

    assert "raw_event" not in payload
    assert payload["native_raw"] is None
    assert payload["native_raw_meta"]["stored_in_trace"] is False
    assert payload["native_raw_meta"]["reason_code"] == "native_raw_not_allowed"


def test_event_bus_payload_includes_native_raw_only_for_declared_capability(monkeypatch) -> None:
    raw = {"update_id": 10, "message": {"text": "hello"}}
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=-100,
        message_id=30,
        text="hello",
        trace_id="evt_raw_allowed",
        native_raw=raw,
    )
    event = account_bot_runtime._incoming_trace_payload(incoming)
    decision = SimpleNamespace(
        plugin_key="demo",
        entry_key="start",
        dispatch_mode="event_subscription",
        scope="all_allowed_chats",
        filters={},
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: True)

    payload = account_bot_runtime._event_bus_plugin_payload(incoming, event, decision)

    assert payload["native_raw"] == raw
    assert payload["native_raw_meta"]["enabled"] is True
    assert payload["native_raw_meta"]["stored_in_trace"] is False
    assert payload["native_raw_meta"]["reason_code"] is None


def test_telegram_native_raw_boolean_true_is_not_explicit_capability(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "declared_plugin_capabilities",
        lambda _module_key: {"telegram_native_raw": True},
    )

    assert account_bot_service.plugin_declares_telegram_native_raw("demo", source="interaction_bot") is False


def test_telegram_native_raw_requires_enabled_object_and_source(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "declared_plugin_capabilities",
        lambda _module_key: {"telegram_native_raw": {"enabled": True, "sources": ["userbot"]}},
    )

    assert account_bot_service.plugin_declares_telegram_native_raw("demo", source="interaction_bot") is False
    assert account_bot_service.plugin_declares_telegram_native_raw("demo", source="userbot") is True


@pytest.mark.asyncio
async def test_event_bus_subscription_invokes_enabled_plugin(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=2001,
        chat_id=-100,
        chat_type="supergroup",
        message_id=30,
        text="开始",
        display_name="Bob",
        native_raw={"update_id": 10},
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="event_game")]

    class _DB:
        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "start",
                "filters": {"keywords": ["开始"]},
            }
        ],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "ok"}]))
    guard_actions = AsyncMock(side_effect=lambda _incoming, _rule, actions: actions)
    apply_actions = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", guard_actions)
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", apply_actions)
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100]},
    )

    assert handled is True
    assert ok is True
    run_entry.assert_awaited_once()
    _, kwargs = run_entry.await_args
    assert kwargs["plugin_key"] == "event_game"
    assert kwargs["entry_key"] == "start"
    assert kwargs["payload"]["trigger"]["dispatch_mode"] == "event_subscription"
    assert kwargs["payload"]["native_raw"] is None
    apply_actions.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_bus_message_without_actions_does_not_consume_legacy_route(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=2001,
        chat_id=-100,
        chat_type="supergroup",
        message_id=30,
        text="十点半测试",
        display_name="Bob",
        native_raw={"update_id": 10},
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="quick_qa")]

    class _DB:
        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "join_quick_qa",
                "filters": {},
            }
        ],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    run_entry = AsyncMock(return_value=(True, None, []))
    guard_actions = AsyncMock(side_effect=lambda _incoming, _rule, actions: actions)
    apply_actions = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", guard_actions)
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", apply_actions)
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100]},
    )

    assert handled is False
    assert ok is True
    run_entry.assert_awaited_once()
    guard_actions.assert_awaited_once()
    apply_actions.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_bus_callback_without_actions_does_not_consume_session_route(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=11,
        user_id=2001,
        chat_id=-100,
        chat_type="supergroup",
        message_id=31,
        text="十点半选庄",
        display_name="Bob",
        callback_id="cb-ten-half",
        callback_data="th:dealer_yes:2001",
        native_raw={"update_id": 11},
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="quick_qa")]

    class _DB:
        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["callback_query"],
                "scope": "all_allowed_chats",
                "entry_key": "join_quick_qa",
                "filters": {},
            }
        ],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    run_entry = AsyncMock(return_value=(True, None, []))
    guard_actions = AsyncMock(side_effect=lambda _incoming, _rule, actions: actions)
    apply_actions = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", guard_actions)
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", apply_actions)
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100]},
    )

    assert handled is False
    assert ok is True
    run_entry.assert_awaited_once()
    guard_actions.assert_awaited_once()
    apply_actions.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_bus_callback_with_actions_consumes_route(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=12,
        user_id=2001,
        chat_id=-100,
        chat_type="supergroup",
        message_id=32,
        text="十点半选庄",
        display_name="Bob",
        callback_id="cb-quick",
        callback_data="quick:answer",
        native_raw={"update_id": 12},
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="quick_qa")]

    class _DB:
        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["callback_query"],
                "scope": "all_allowed_chats",
                "entry_key": "join_quick_qa",
                "filters": {},
            }
        ],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    run_entry = AsyncMock(return_value=(True, None, [{"type": "answer_callback", "callback_query_id": "cb-quick"}]))
    guard_actions = AsyncMock(side_effect=lambda _incoming, _rule, actions: actions)
    apply_actions = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", guard_actions)
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", apply_actions)
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100]},
    )

    assert handled is True
    assert ok is True
    run_entry.assert_awaited_once()
    guard_actions.assert_awaited_once()
    apply_actions.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_bus_callback_plugin_failure_is_acknowledged(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=13,
        user_id=2001,
        chat_id=-100,
        chat_type="supergroup",
        message_id=33,
        text="十点半选庄",
        display_name="Bob",
        callback_id="cb-fail",
        callback_data="quick:answer",
        native_raw={"update_id": 13},
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="quick_qa")]

    class _DB:
        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["callback_query"],
                "scope": "all_allowed_chats",
                "entry_key": "join_quick_qa",
                "filters": {},
            }
        ],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_runtime,
        "_run_worker_interaction_entry",
        AsyncMock(return_value=(False, "plugin exploded", [])),
    )
    answer = AsyncMock()
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100]},
    )

    assert handled is True
    assert ok is False
    answer.assert_awaited_once_with(
        "123:token",
        "cb-fail",
        text="处理失败，请稍后再试",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_event_bus_known_users_excludes_unknown_sender_and_includes_session_participants(monkeypatch) -> None:
    rule = {
        "id": "game24-active",
        "enabled": True,
        "action": "module",
        "module_key": "game24",
        "module_action": "start",
        "concurrency": "chat",
    }
    redis = _MemoryRedis()
    redis.data[account_bot_runtime._interaction_session_key(1, rule, -100123)] = json.dumps(
        {"participant_user_ids": [333]}
    )

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(tg_user_id=444)]

    class _DB:
        async def get(self, model, _key):  # noqa: ANN001
            if model is Account:
                return SimpleNamespace(tg_user_id=111)
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Rows()

    list_bot_users = AsyncMock(return_value=[SimpleNamespace(enabled=True, tg_user_id=555)])
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "list_bot_users", list_bot_users)

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=1,
        user_id=222,
        chat_id=-100123,
        chat_type="supergroup",
        message_id=10,
        text="hello",
        display_name="Unknown",
        native_raw={"update_id": 1},
    )

    state = await account_bot_runtime._event_bus_account_state(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100123], "rules": [rule]},
    )
    await account_bot_runtime._event_bus_account_state(
        _DB(),
        incoming,
        {"enabled": True, "chat_ids": [-100123], "rules": [rule]},
    )

    assert 222 not in state["known_user_ids"]
    assert {111, 333, 444, 555}.issubset(set(state["known_user_ids"]))
    assert list_bot_users.await_count == 1

    subscription = account_bot_runtime.normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message"],
            "scope": "known_users",
            "entry_key": "start",
        },
        plugin_key="game24",
    )
    unknown_event = account_bot_runtime._incoming_trace_payload(incoming)
    unknown_decision = account_bot_runtime.dispatch_event(unknown_event, [subscription], state).decisions[0]
    assert unknown_decision.matched is False
    assert unknown_decision.reason_code == "scope_not_matched"

    participant = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=2,
        user_id=333,
        chat_id=-100123,
        chat_type="supergroup",
        message_id=11,
        text="answer",
        display_name="Participant",
        native_raw={"update_id": 2},
    )
    participant_event = account_bot_runtime._incoming_trace_payload(participant)
    participant_decision = account_bot_runtime.dispatch_event(participant_event, [subscription], state).decisions[0]
    assert participant_decision.matched is True


@pytest.mark.asyncio
async def test_interaction_update_respects_event_bus_delivery_switch(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    trace = SimpleNamespace(trace_id="evt_interaction")
    event_bus = AsyncMock(return_value=(True, True))
    legacy_rule = AsyncMock(return_value=True)
    record_span = AsyncMock()
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_payment_confirm", AsyncMock(return_value=False))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100],
                "module_start_keywords": ["开始"],
            }
        ),
    )
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", event_bus)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", legacy_rule)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", AsyncMock(return_value=False))

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "text": "开始",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100, "type": "supergroup"},
            },
        },
    )

    event_bus.assert_not_awaited()
    legacy_rule.assert_awaited_once()
    assert any(
        call.kwargs.get("reason_code") == "event_bus_delivery_disabled"
        for call in record_span.await_args_list
    )


@pytest.mark.asyncio
async def test_management_update_respects_trace_enabled_switch(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    user = SimpleNamespace(enabled=True, role="admin", last_chat_id=None, display_name=None)
    handled: list[account_bot_runtime.Incoming] = []

    async def _handle_command(incoming, _role):
        handled.append(incoming)

    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock(return_value=user))
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_should_route_text_to_account_commands", lambda _incoming: True)
    monkeypatch.setattr(account_bot_runtime, "_handle_command", _handle_command)

    await account_bot_runtime._handle_update(
        1,
        "bbot-token",
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "text": "/status",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": 111, "type": "private"},
            },
        },
    )

    account_bot_runtime.start_trace.assert_not_awaited()
    assert handled and handled[0].trace_id is None
    key = account_bot_runtime._router_delivery_stats_key(1, "account_bot", None, 111)
    assert account_bot_runtime._ROUTER_DELIVERY_STATS[key]["delivery_count"] >= 1


def test_plugin_manifest_serializes_strict_trace() -> None:
    manifest = Manifest(key="payout_demo", display_name="Payout Demo", strict_trace=True)

    assert manifest.strict_trace is True
    assert manifest.to_dict()["strict_trace"] is True


@pytest.mark.asyncio
async def test_router_debug_trace_flag_is_temporary_and_scoped(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)

    result = await account_bot_runtime.set_router_debug_trace(
        1,
        plugin_key="payout_demo",
        ttl_seconds=60,
    )

    assert result["scope"] == "plugin"
    assert result["ttl_seconds"] == 60
    assert redis.set_calls[-1][2]["ex"] == 60
    assert await account_bot_runtime._router_debug_trace_enabled(1, plugin_key="payout_demo") is True
    assert await account_bot_runtime._router_debug_trace_enabled(1, plugin_key="other") is False


@pytest.mark.asyncio
async def test_interaction_update_default_uses_light_router_delivery(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    start_trace = AsyncMock()
    rule_command = AsyncMock(return_value=True)
    account_bot_runtime._ROUTER_DELIVERY_STATS.clear()
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "start_trace", start_trace)
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_plugin_declares_strict_trace", lambda _key: False)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_payment_confirm", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", rule_command)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", AsyncMock(return_value=False))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 999,
                "chat_ids": [-100],
                "module_start_keywords": ["开始"],
                "rules": [
                    {
                        "id": "demo",
                        "enabled": True,
                        "chat_ids": [-100],
                        "module_start_keywords": ["开始"],
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 41,
            "message": {
                "message_id": 401,
                "text": "开始",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100, "type": "supergroup"},
            },
        },
    )

    start_trace.assert_not_awaited()
    rule_command.assert_awaited_once()
    key = account_bot_runtime._router_delivery_stats_key(1, "interaction_bot", None, -100)
    assert account_bot_runtime._ROUTER_DELIVERY_STATS[key]["delivery_count"] == 1
    assert account_bot_runtime._ROUTER_DELIVERY_STATS[key]["last_status"] == account_bot_runtime.TRACE_STATUS_OK


def test_router_delivery_stats_are_bounded_and_readable(monkeypatch) -> None:
    account_bot_runtime._ROUTER_DELIVERY_STATS.clear()
    monkeypatch.setattr(account_bot_runtime, "_ROUTER_DELIVERY_STATS_MAX_SIZE", 3)
    try:
        for chat_id in (101, 102, 103):
            account_bot_runtime._record_router_delivery_light(
                SimpleNamespace(account_id=1, chat_id=chat_id),
                "interaction_bot",
                account_bot_runtime.TRACE_STATUS_OK,
            )
        account_bot_runtime._record_router_delivery_light(
            SimpleNamespace(account_id=1, chat_id=102),
            "interaction_bot",
            account_bot_runtime.TRACE_STATUS_OK,
        )
        account_bot_runtime._record_router_delivery_light(
            SimpleNamespace(account_id=1, chat_id=104),
            "interaction_bot",
            account_bot_runtime.TRACE_STATUS_SKIPPED,
            error="missed",
        )

        assert len(account_bot_runtime._ROUTER_DELIVERY_STATS) == 3
        evicted_key = account_bot_runtime._router_delivery_stats_key(1, "interaction_bot", None, 101)
        retained_key = account_bot_runtime._router_delivery_stats_key(1, "interaction_bot", None, 102)
        assert evicted_key not in account_bot_runtime._ROUTER_DELIVERY_STATS
        assert retained_key in account_bot_runtime._ROUTER_DELIVERY_STATS

        summary = account_bot_runtime.get_router_delivery_stats_summary(
            account_id=1,
            channel="interaction_bot",
            limit=10,
        )
        assert summary["count"] == 3
        assert summary["total_count"] == 3
        assert summary["max_size"] == 3
        assert [item["chat_id"] for item in summary["entries"]] == [104, 102, 103]
        assert summary["entries"][0]["last_error"] == "missed"
    finally:
        account_bot_runtime._ROUTER_DELIVERY_STATS.clear()


@pytest.mark.asyncio
async def test_interaction_update_strict_trace_plugin_keeps_full_router_trace(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    trace = SimpleNamespace(trace_id="evt_strict")
    record_span = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime, "finish_trace", finish_trace)
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_plugin_declares_strict_trace", lambda key: key == "payout_demo")
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_payment_confirm", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=True))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", AsyncMock(return_value=False))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 999,
                "chat_ids": [-100],
                "module_start_keywords": ["开始"],
                "rules": [
                    {
                        "id": "strict",
                        "enabled": True,
                        "chat_ids": [-100],
                        "module_start_keywords": ["开始"],
                        "action": "module",
                        "module_key": "payout_demo",
                        "module_action": "start",
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 42,
            "message": {
                "message_id": 402,
                "text": "开始",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100, "type": "supergroup"},
            },
        },
    )

    account_bot_runtime.start_trace.assert_awaited_once()
    assert any(call.args[1] == "receive" and call.kwargs.get("router_trace_reason") == "strict" for call in record_span.await_args_list)
    assert any(call.kwargs.get("component") == "interaction_rule" for call in record_span.await_args_list)
    finish_trace.assert_awaited_once_with(trace, account_bot_runtime.TRACE_STATUS_OK)


@pytest.mark.asyncio
async def test_interaction_payment_confirm_route_keeps_full_router_trace() -> None:
    requested = await account_bot_runtime._interaction_strict_trace_requested(
        object(),
        SimpleNamespace(chat_id=-100),
        {"rules": []},
        [account_bot_runtime._ROUTE_PAYMENT_CONFIRM],
        event_bus_delivery_enabled=False,
    )

    assert requested is True


@pytest.mark.asyncio
async def test_interaction_update_respects_inline_updates_switch_at_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "inline_updates_enabled": False}),
    )
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", AsyncMock())

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 5,
            "inline_query": {
                "id": "iq-closed",
                "query": "hello",
                "from": {"id": 111, "first_name": "AAA"},
            },
        },
    )

    account_bot_runtime.start_trace.assert_not_awaited()
    account_bot_runtime._try_handle_event_bus_subscriptions.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_inline_query_routes_through_event_bus_and_records_trace(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="inline_demo")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    trace = SimpleNamespace(trace_id="evt_inline")
    run_entry = AsyncMock(return_value=(
        True,
        None,
        [{
            "type": "answer_inline_query",
            "results": [{"type": "article", "id": "1", "title": "Demo", "input_message_content": {"message_text": "ok"}}],
        }],
    ))
    apply_actions = AsyncMock()
    record_span = AsyncMock()
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_plugin_declares_strict_trace", lambda key: key == "inline_demo")
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_payment_confirm", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_service, "get_transfer_notice_config", AsyncMock(return_value={"enabled": True, "chat_ids": []}))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [{
            "source": ["interaction_bot"],
            "events": ["inline_query"],
            "scope": "inline_all",
            "entry_key": "inline",
        }],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", apply_actions)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", AsyncMock(return_value=False))

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 11,
            "inline_query": {
                "id": "iq-1",
                "query": "玩法",
                "offset": "",
                "chat_type": "sender",
                "from": {"id": 111, "first_name": "AAA"},
            },
        },
    )

    account_bot_runtime.start_trace.assert_awaited_once()
    run_entry.assert_awaited_once()
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "inline_query"
    assert payload["trace_id"] == "evt_inline"
    assert payload["inline_query"]["id"] == "iq-1"
    assert payload["trigger"]["dispatch_mode"] == "event_subscription"
    apply_actions.assert_awaited_once()
    assert apply_actions.await_args.args[1][0]["type"] == "answer_inline_query"
    assert any(
        call.kwargs.get("component") == "event_bus"
        and call.kwargs.get("reason_code") == "matched"
        for call in record_span.await_args_list
    )
    account_bot_runtime.finish_trace.assert_awaited_once_with(trace, account_bot_runtime.TRACE_STATUS_OK)


@pytest.mark.asyncio
async def test_interaction_chosen_inline_result_routes_through_event_bus(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="inline_demo")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(tg_user_id=999)

    trace = SimpleNamespace(trace_id="evt_chosen_inline")
    run_entry = AsyncMock(return_value=(True, None, []))
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_plugin_declares_strict_trace", lambda key: key == "inline_demo")
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_payment_confirm", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_service, "get_transfer_notice_config", AsyncMock(return_value={"enabled": True, "chat_ids": []}))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [{
            "source": ["interaction_bot"],
            "events": ["chosen_inline_result"],
            "scope": "inline_all",
            "entry_key": "chosen",
        }],
    )
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", AsyncMock(return_value=False))

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 12,
            "chosen_inline_result": {
                "result_id": "result-1",
                "query": "玩法",
                "from": {"id": 111, "first_name": "AAA"},
            },
        },
    )

    run_entry.assert_awaited_once()
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "chosen_inline_result"
    assert payload["chosen_inline_result"]["result_id"] == "result-1"
    assert payload["trigger"]["entry_key"] == "chosen"
    account_bot_runtime.finish_trace.assert_awaited_once_with(trace, account_bot_runtime.TRACE_STATUS_OK)


@pytest.mark.asyncio
async def test_interaction_delivery_executor_answer_inline_query_records_success_and_failure(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=10,
        user_id=20,
        chat_id=None,
        message_id=None,
        text="玩法",
        inline_query_id="iq-ok",
        trace_id="evt_inline_action",
    )
    answer_inline = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "answer_inline_query", answer_inline)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )

    await executor.apply([
        {
            "type": "answer_inline_query",
            "results": [{"type": "article", "id": "1"}],
            "cache_time": 3,
            "is_personal": True,
        }
    ])

    answer_inline.assert_awaited_once_with(
        "123:token",
        "iq-ok",
        results=[{"type": "article", "id": "1"}],
        cache_time=3,
        is_personal=True,
        next_offset="",
        button=None,
    )
    assert record_action.await_args_list[0].args[2] == account_bot_runtime.TRACE_STATUS_OK
    assert record_action.await_args_list[0].kwargs["actual_send_via"] == "interaction_bot"

    missing_incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=11,
        user_id=20,
        chat_id=None,
        message_id=None,
        text="玩法",
        trace_id="evt_inline_action_missing",
    )
    missing_executor = InteractionDeliveryExecutor(
        incoming=missing_incoming,
        write_log=AsyncMock(),
        run_worker_action=AsyncMock(),
        log_context=account_bot_runtime._interaction_log_context,
        trace_context=account_bot_runtime._interaction_trace_context,
    )
    await missing_executor.apply([{"type": "answer_inline_query", "results": []}])

    assert answer_inline.await_count == 1
    assert record_action.await_count == 2
    assert record_action.await_args_list[1].args[2] == account_bot_runtime.TRACE_STATUS_FAILED
    assert record_action.await_args_list[1].kwargs["error_code"] == "inline_query_id_missing"


def test_worker_defer_interaction_entry_error_log_only_for_math10_fallback() -> None:
    assert worker_runtime._should_defer_interaction_entry_error_log(
        "math10",
        "RuntimeError: 模块未加载或未启用：math10",
    )
    assert not worker_runtime._should_defer_interaction_entry_error_log(
        "game24",
        "RuntimeError: 模块未加载或未启用：game24",
    )
    assert not worker_runtime._should_defer_interaction_entry_error_log(
        "math10",
        "RuntimeError: 其他错误",
    )


def test_rule_entry_allows_event_when_declared_events_missing_for_compatibility(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: [],
    )
    rule = {
        "id": "legacy-bridge",
        "action": "module",
        "module_key": "legacy_demo",
        "module_action": "start_legacy_demo",
    }

    assert account_bot_runtime._rule_entry_allows_event(rule, "payment_confirmed") is True
    assert account_bot_runtime._rule_entry_allows_event(rule, "message") is True
    assert account_bot_runtime._rule_entry_allows_event(rule, "session_close") is True


def test_module_config_bet_is_used_as_payment_amount() -> None:
    rule = {
        "id": "ten-half-paid",
        "action": "module",
        "amount": None,
        "amount_match_mode": "eq",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_config": {"bet": 100, "max_players": 2},
    }

    assert account_bot_runtime._rule_amount_matches(rule, 100) is True
    assert account_bot_runtime._rule_amount_matches(rule, 10) is False

    rule["amount_match_mode"] = "gte"
    assert account_bot_runtime._rule_amount_matches(rule, 100) is True
    assert account_bot_runtime._rule_amount_matches(rule, 120) is True
    assert account_bot_runtime._rule_amount_matches(rule, 99) is False


def test_interaction_module_payload_uses_module_config_amount_as_prize() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=20,
        chat_id=-100123,
        message_id=30,
        text="十点半",
        display_name="AAA",
    )
    rule = {
        "id": "ten-half-paid",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "math_prize": 123,
        "module_config": {"bet": 1000, "max_players": 2},
    }

    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        rule,
        None,
        event_type="keyword",
    )

    assert payload["bet"] == 1000
    assert payload["module_config"]["bet"] == 1000
    assert payload["prize"] == 1000


def test_interaction_module_payload_does_not_fallback_to_math_prize_default() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=20,
        chat_id=-100123,
        message_id=30,
        text="开局",
        display_name="AAA",
    )
    rule = {
        "id": "game-paid",
        "action": "module",
        "module_key": "game24",
        "module_action": "start_paid_game",
        "math_prize": 123,
        "module_config": {},
    }

    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        rule,
        None,
        event_type="keyword",
    )

    assert payload["prize"] is None


def test_interaction_module_payload_keeps_explicit_module_prize_default_value() -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=20,
        chat_id=-100123,
        message_id=30,
        text="开局",
        display_name="AAA",
    )
    rule = {
        "id": "game-paid",
        "action": "module",
        "module_key": "game24",
        "module_action": "start_paid_game",
        "module_prize": 123,
        "module_config": {},
    }

    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        rule,
        None,
        event_type="keyword",
    )

    assert payload["prize"] == 123


def test_interaction_module_payload_preserves_plugin_timeout_config(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "plugin_declares_telegram_native_raw",
        lambda *_args, **_kwargs: False,
    )
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=20,
        chat_id=-100123,
        message_id=30,
        text="开局",
        display_name="AAA",
    )
    rule = {
        "id": "ten-half-paid",
        "name": "十点半",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_prize": 888,
        "valid_seconds": 600,
        "module_config": {
            "bet": 100,
            "timeout": 45,
            "lobby_timeout": 90,
            "max_players": 4,
        },
    }

    payload = account_bot_runtime._interaction_module_payload(
        incoming,
        rule,
        None,
        event_type="keyword",
    )

    assert payload["timeout"] == 45
    assert payload["module_config"]["timeout"] == 45
    assert payload["module_config"]["lobby_timeout"] == 90
    assert payload["valid_seconds"] == 600
    assert payload["prize"] == 888


def test_saved_participant_policy_overrides_declared_default(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda module_key, entry_key: {"participant_policy": "paid_pool"}
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else None,
    )
    rule = {
        "id": "ten-half-paid",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "participant_policy": "solo_owner",
    }

    assert account_bot_runtime._interaction_participant_policy(rule) == "solo_owner"


def test_declared_participant_policy_is_default_when_rule_unset(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda module_key, entry_key: {"participant_policy": "paid_pool"}
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else None,
    )
    rule = {
        "id": "ten-half-paid",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
    }

    assert account_bot_runtime._interaction_participant_policy(rule) == "paid_pool"


@pytest.mark.asyncio
async def test_payment_interaction_session_uses_payer_user_scope(monkeypatch) -> None:
    redis = _MemoryRedis()
    emit_action_event = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "emit_action_event", emit_action_event)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        message_id=70,
        text="转账成功",
        display_name="TransferBot",
    )
    rule = {
        "id": "lotto-ticket",
        "module_key": "lottery_plus",
        "module_action": "start_lottery_plus",
        "module_session_scope": "user",
        "concurrency": "chat",
        "valid_seconds": 600,
    }

    await account_bot_runtime._save_interaction_session(
        incoming,
        rule,
        "payment_confirmed",
        {"payer_user_id": 111},
    )

    payer_key = account_bot_runtime._interaction_session_key(1, rule, -100123, 111)
    transfer_bot_key = account_bot_runtime._interaction_session_key(1, rule, -100123, 456)
    assert payer_key in redis.data
    assert transfer_bot_key not in redis.data
    saved = json.loads(redis.data[payer_key])
    assert saved["started_by_user_id"] == 111
    assert saved["source_user_id"] == 456
    assert saved["channel"] == "interaction_bot"
    assert isinstance(saved["expires_at"], float)
    assert redis.set_calls[-1][2]["ex"] == 600 + account_bot_runtime._INTERACTION_SESSION_TTL_GRACE_SECONDS
    emit_action_event.assert_awaited_once()
    tapped = emit_action_event.await_args.kwargs
    assert tapped["action"]["type"] == "start_session"
    assert tapped["action"]["started_by_user_id"] == 111
    assert tapped["action"]["participant_user_ids"] == [111]
    assert tapped["session_key"] == payer_key


@pytest.mark.asyncio
async def test_interaction_session_save_preserves_active_existing_channel(monkeypatch) -> None:
    redis = _MemoryRedis()
    emit_action_event = AsyncMock()
    now = 1_720_100_000.0
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.time, "time", lambda: now)
    monkeypatch.setattr(account_bot_runtime, "emit_action_event", emit_action_event)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=111,
        chat_id=-100123,
        message_id=70,
        text="开始",
    )
    rule = {
        "id": "grab-session",
        "module_key": "game",
        "module_action": "start",
        "module_session_scope": "chat",
        "valid_seconds": 60,
    }
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123, None)
    redis.data[session_key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100123,
            "rule_id": "grab-session",
            "module_key": "game",
            "entry_key": "start",
            "channel": "userbot",
            "created_at": now - 30,
            "expires_at": now + 30,
            "data": {"round": 2},
        }
    )

    await account_bot_runtime._save_interaction_session(incoming, rule, "keyword")

    saved = json.loads(redis.data[session_key])
    assert saved["channel"] == "userbot"
    assert saved["data"] == {"round": 2}
    assert saved["created_at"] == now - 30
    assert saved["updated_at"] == now
    emit_action_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_session_load_failure_writes_runtime_warn(monkeypatch) -> None:
    class _BrokenRedis:
        async def get(self, _key: str):
            raise RuntimeError("redis down")

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=111,
        chat_id=-100123,
        message_id=70,
        text="答案",
    )
    rule = {
        "id": "broken-session",
        "module_key": "game",
        "module_action": "answer",
    }
    runtime_log = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _BrokenRedis())
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", runtime_log)

    assert await account_bot_runtime._load_interaction_session(incoming, rule) is None
    runtime_log.assert_awaited_once()
    assert runtime_log.await_args.args[1:3] == (account_bot_runtime.LEVEL_WARN, "interaction session load failed")
    assert runtime_log.await_args.kwargs["rule_id"] == "broken-session"


@pytest.mark.asyncio
async def test_interaction_session_save_failure_writes_runtime_warn(monkeypatch) -> None:
    class _BrokenRedis:
        async def get(self, _key: str):
            return None

        async def set(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("redis down")

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=111,
        chat_id=-100123,
        message_id=70,
        text="开始",
    )
    rule = {
        "id": "broken-save",
        "module_key": "game",
        "module_action": "start",
        "valid_seconds": 60,
    }
    runtime_log = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _BrokenRedis())
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", runtime_log)

    await account_bot_runtime._save_interaction_session(incoming, rule, "message")
    runtime_log.assert_awaited_once()
    assert runtime_log.await_args.args[1:3] == (account_bot_runtime.LEVEL_WARN, "interaction session save failed")
    assert runtime_log.await_args.kwargs["rule_id"] == "broken-save"
    assert "redis down" in runtime_log.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_interaction_session_expired_scan_dispatches_interaction_bot_session(monkeypatch) -> None:
    redis = _MemoryRedis()
    now = 1710001000.0
    rule = {
        "id": "expire-game",
        "enabled": True,
        "chat_ids": [-100123],
        "action": "module",
        "module_key": "expire_game",
        "module_action": "on_event",
    }
    expired_key = account_bot_runtime._interaction_session_key(1, rule, -100123, None)
    userbot_key = account_bot_runtime._interaction_session_key(
        1,
        {**rule, "id": "userbot-expire"},
        -100123,
        None,
    )
    redis.data[expired_key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100123,
            "rule_id": "expire-game",
            "module_key": "expire_game",
            "entry_key": "on_event",
            "started_by_user_id": 111,
            "started_by_message_id": 70,
            "channel": "interaction_bot",
            "expires_at": now - 1,
            "data": {"round": 3},
        }
    )
    redis.data[userbot_key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100123,
            "rule_id": "userbot-expire",
            "module_key": "expire_game",
            "entry_key": "on_event",
            "started_by_user_id": 111,
            "channel": "userbot",
            "expires_at": now - 1,
        }
    )
    run_entry = AsyncMock(return_value=(True, None, []))
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.time, "time", lambda: now)
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": False}))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_load_account_owner_user_ids", AsyncMock(return_value=[999]))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))

    await account_bot_runtime._scan_interaction_expired_sessions_once(
        1,
        "bbot-token",
        {
            "enabled": True,
            "rules": [rule],
        },
    )

    run_entry.assert_awaited_once()
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "session_expired"
    assert payload["source"]["type"] == "session_expired"
    assert payload["session"]["channel"] == "interaction_bot"
    assert payload["session"]["expires_at"] == int(now - 1)
    assert payload["session"]["data"] == {"round": 3}
    assert payload["trigger"]["session_key"] == expired_key
    assert expired_key not in redis.data
    assert userbot_key in redis.data


@pytest.mark.asyncio
async def test_keyword_start_saves_session_before_update_and_routes_answer_payload(monkeypatch) -> None:
    redis = _MemoryRedis()
    state = {"active": True, "target": 7}
    payloads: list[dict[str, Any]] = []
    rule = {
        "id": "guess-ticket",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "keyword",
        "module_start_keywords": ["猜数字"],
        "action": "module",
        "module_key": "guess_number",
        "module_action": "start_game",
        "valid_seconds": 90,
    }

    async def run_entry(_incoming, *, plugin_key, entry_key, payload):  # noqa: ANN001
        payloads.append(payload)
        assert (plugin_key, entry_key) == ("guess_number", "start_game")
        if payload["event_type"] == "keyword":
            return True, None, [
                {"type": "send_message", "text": "猜数字开始"},
                {"type": "update_session", "data": state},
                {"type": "result", "success": True},
            ]
        return True, None, [
            {"type": "send_message", "text": "答对"},
            {"type": "payout", "amount": 6, "reply_to_user_id": 222},
            {"type": "result", "success": True},
            {"type": "end_session"},
        ]

    record_action = AsyncMock()
    run_worker_action = AsyncMock(return_value=(True, None, {"message_id": 9001}))
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _InteractionRuntimeTestDB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_action", run_worker_action)
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_load_account_owner_user_ids", AsyncMock(return_value=[999]))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock(return_value={"message_id": 7001}))
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_test_message(2001, "猜数字", user_id=111),
    )

    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123)
    assert session_key in redis.data
    saved = json.loads(redis.data[session_key])
    assert saved["data"] == state
    assert any(
        call.args[1]["type"] == "update_session"
        and call.args[2] == account_bot_runtime.TRACE_STATUS_OK
        for call in record_action.await_args_list
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_test_message(2002, "7", user_id=222, first_name="Bob"),
    )

    assert len(payloads) == 2
    assert payloads[1]["event_type"] == "message"
    assert payloads[1]["session"]["data"] == state
    assert run_worker_action.await_args.kwargs["payload"]["amount"] == 6
    assert session_key not in redis.data

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_test_message(2003, "8", user_id=333, first_name="Carol"),
    )

    assert len(payloads) == 2


@pytest.mark.asyncio
async def test_payment_start_preserves_paid_pool_data_on_second_payment(monkeypatch) -> None:
    redis = _MemoryRedis()
    state = {"round": 1, "active": True}
    rule = {
        "id": "paid-pool-ticket",
        "name": "付费池",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "payment",
        "trigger_texts": ["转账成功"],
        "receiver_text": "BBB",
        "amount": 100,
        "amount_match_mode": "eq",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "participant_policy": "paid_pool",
        "valid_seconds": 300,
    }

    async def run_entry(_incoming, *, payload, **_kwargs):  # noqa: ANN001, ANN003
        payer_id = payload["payment"]["payer_user_id"]
        if payer_id == 111:
            return True, None, [
                {"type": "send_message", "text": "第一笔"},
                {"type": "update_session", "data": state},
                {"type": "result", "success": True},
            ]
        return True, None, [
            {"type": "send_message", "text": "第二笔"},
            {"type": "result", "success": True},
        ]

    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _InteractionRuntimeTestDB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_load_account_owner_user_ids", AsyncMock(return_value=[999]))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock(return_value={"message_id": 7101}))
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "trusted_bot_id": 456, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_payment_message(2011, payer_id=111, payer_name="Alice"),
    )
    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_payment_message(2012, payer_id=222, payer_name="Bob"),
    )

    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123)
    saved = json.loads(redis.data[session_key])
    assert saved["data"] == state
    assert saved["paid_user_ids"] == [111, 222]
    assert saved["participant_user_ids"] == [111, 222]
    assert any(
        call.args[1]["type"] == "update_session"
        and call.args[2] == account_bot_runtime.TRACE_STATUS_OK
        for call in record_action.await_args_list
    )


@pytest.mark.asyncio
async def test_opening_update_session_with_end_session_leaves_no_session(monkeypatch) -> None:
    redis = _MemoryRedis()
    rule = {
        "id": "one-shot",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "keyword",
        "module_start_keywords": ["一次性"],
        "action": "module",
        "module_key": "one_shot",
        "module_action": "start",
        "valid_seconds": 90,
    }
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _InteractionRuntimeTestDB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(
        account_bot_runtime,
        "_run_worker_interaction_entry",
        AsyncMock(
            return_value=(
                True,
                None,
                [
                    {"type": "update_session", "data": {"active": False}},
                    {"type": "end_session"},
                ],
            )
        ),
    )
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_load_account_owner_user_ids", AsyncMock(return_value=[999]))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_test_message(2021, "一次性"),
    )

    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123)
    assert session_key not in redis.data
    update_call = next(call for call in record_action.await_args_list if call.args[1]["type"] == "update_session")
    assert update_call.args[2] == account_bot_runtime.TRACE_STATUS_FAILED
    assert update_call.kwargs["error_code"] == "interaction_session_error"


@pytest.mark.asyncio
async def test_opening_start_session_then_update_session_keeps_explicit_fields(monkeypatch) -> None:
    redis = _MemoryRedis()
    rule = {
        "id": "explicit-start",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "keyword",
        "module_start_keywords": ["显式开局"],
        "action": "module",
        "module_key": "explicit_game",
        "module_action": "start",
        "module_session_scope": "chat",
        "valid_seconds": 120,
    }
    record_action = AsyncMock()
    emit_action_event = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _InteractionRuntimeTestDB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "emit_action_event", emit_action_event)
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(
        account_bot_runtime,
        "_run_worker_interaction_entry",
        AsyncMock(
            return_value=(
                True,
                None,
                [
                    {
                        "type": "start_session",
                        "started_by_user_id": 222,
                        "ttl_seconds": 30,
                        "event_type": "keyword",
                    },
                    {"type": "update_session", "data": {"phase": "started"}},
                    {"type": "result", "success": True},
                ],
            )
        ),
    )
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_load_account_owner_user_ids", AsyncMock(return_value=[999]))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        _interaction_test_message(2031, "显式开局", user_id=111),
    )

    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123)
    saved = json.loads(redis.data[session_key])
    assert saved["started_by_user_id"] == 222
    assert saved["event_type"] == "keyword"
    assert saved["data"] == {"phase": "started"}
    assert any(
        call.args[1]["type"] == "update_session"
        and call.args[2] == account_bot_runtime.TRACE_STATUS_OK
        for call in record_action.await_args_list
    )
    emit_action_event.assert_awaited_once()
    assert emit_action_event.await_args.kwargs["action"]["type"] == "start_session"
    assert emit_action_event.await_args.kwargs["action"]["started_by_user_id"] == 222


@pytest.mark.asyncio
async def test_runtime_start_session_action_merges_existing_and_action_data(monkeypatch) -> None:
    redis = _MemoryRedis()
    now = 1_720_200_000.0
    rule = {
        "id": "explicit-merge",
        "enabled": True,
        "chat_ids": [-100123],
        "action": "module",
        "module_key": "explicit_game",
        "module_action": "start",
        "module_session_scope": "chat",
        "valid_seconds": 120,
    }
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123, None)
    redis.data[session_key] = json.dumps(
        {
            "account_id": 1,
            "chat_id": -100123,
            "rule_id": "explicit-merge",
            "module_key": "explicit_game",
            "entry_key": "start",
            "channel": "userbot",
            "created_at": now - 20,
            "started_by_user_id": 111,
            "started_by_message_id": 40,
            "expires_at": now + 80,
            "data": {"round": 1, "phase": "old"},
        }
    )
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=222,
        chat_id=-100123,
        chat_type="supergroup",
        message_id=50,
        text="显式开局",
    )
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.time, "time", lambda: now)
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)

    await account_bot_runtime._apply_interaction_start_session_action(
        incoming,
        rule,
        {
            "type": "start_session",
            "data": {"phase": "new", "answer": 24},
            "ttl_seconds": 60,
        },
    )

    saved = json.loads(redis.data[session_key])
    assert saved["channel"] == "userbot"
    assert saved["data"] == {"round": 1, "phase": "new", "answer": 24}
    assert saved["created_at"] == now - 20
    assert saved["started_by_user_id"] == 111
    assert saved["started_by_message_id"] == 40
    assert saved["updated_at"] == now
    assert saved["expires_at"] == now + 60
    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_OK


@pytest.mark.asyncio
async def test_event_bus_wide_update_session_without_session_still_fails(monkeypatch) -> None:
    redis = _MemoryRedis()
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=2041,
        user_id=111,
        chat_id=-100123,
        chat_type="supergroup",
        message_id=20410,
        text="裸更新",
        display_name="Alice",
        native_raw={"update_id": 2041},
    )
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "on_message",
                "filters": {"keywords": ["裸更新"]},
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_runtime,
        "_run_worker_interaction_entry",
        AsyncMock(return_value=(True, None, [{"type": "update_session", "data": {"active": True}}])),
    )
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _InteractionRuntimeTestDB(feature_keys=["wide_session"]),
        incoming,
        {"enabled": True, "chat_ids": [-100123], "rules": []},
    )

    assert handled is True
    assert ok is True
    assert not any(key.startswith("account_bot:interaction_session:") for key in redis.data)
    update_call = next(call for call in record_action.await_args_list if call.args[1]["type"] == "update_session")
    assert update_call.args[2] == account_bot_runtime.TRACE_STATUS_FAILED
    assert update_call.kwargs["error_code"] == "interaction_session_error"


@pytest.mark.asyncio
async def test_paid_pool_chat_session_accumulates_paid_players(monkeypatch) -> None:
    redis = _MemoryRedis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda module_key, entry_key: {"participant_policy": "paid_pool"}
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else None,
    )
    rule = {
        "id": "ten-half-paid",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "valid_seconds": 600,
    }
    keyword_incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=999,
        chat_id=-100123,
        message_id=60,
        text="十点半测试",
        display_name="Starter",
    )

    await account_bot_runtime._save_interaction_session(keyword_incoming, rule, "keyword", None)
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123, None)
    session = json.loads(redis.data[session_key])
    assert session["started_by_user_id"] == 999
    assert session["paid_user_ids"] == []
    assert account_bot_runtime._interaction_session_participant_ids(session, policy="paid_pool") == set()

    for user_id in (111, 222):
        incoming = account_bot_runtime.Incoming(
            account_id=1,
            token="bbot-token",
            update_id=10 + user_id,
            user_id=456,
            chat_id=-100123,
            message_id=70 + user_id,
            text="转账成功",
            display_name="TransferBot",
        )
        await account_bot_runtime._save_interaction_session(
            incoming,
            rule,
            "payment_confirmed",
            {"payer_user_id": user_id},
        )

    session = json.loads(redis.data[session_key])
    assert session["started_by_user_id"] == 999
    assert session["paid_user_ids"] == [111, 222]
    assert session["participant_user_ids"] == [111, 222]
    assert account_bot_runtime._interaction_session_participant_ids(session, policy="paid_pool") == {111, 222}


def test_paid_pool_callback_allows_session_starter_as_controller(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda module_key, entry_key: {"participant_policy": "paid_pool"}
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else None,
    )
    rule = {
        "id": "ten-half-paid",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
    }
    session = {
        "started_by_user_id": 999,
        "paid_user_ids": [111, 222],
        "participant_user_ids": [111, 222],
    }

    starter_callback = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=999,
        chat_id=-100123,
        message_id=90,
        text="",
        callback_id="cb-owner",
        callback_data="th:stand:999",
        display_name="Owner",
    )
    stranger_callback = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=2,
        user_id=333,
        chat_id=-100123,
        message_id=91,
        text="",
        callback_id="cb-stranger",
        callback_data="th:stand:999",
        display_name="Stranger",
    )
    join_callback = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=3,
        user_id=333,
        chat_id=-100123,
        message_id=92,
        text="",
        callback_id="cb-join",
        callback_data="th:join:0",
        display_name="Stranger",
    )
    rules_callback = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=4,
        user_id=333,
        chat_id=-100123,
        message_id=93,
        text="",
        callback_id="cb-rules",
        callback_data="th:rules:0",
        display_name="Stranger",
    )

    assert account_bot_runtime._interaction_participant_block_message(starter_callback, rule, session) is None
    assert account_bot_runtime._interaction_participant_block_message(join_callback, rule, session) is None
    assert account_bot_runtime._interaction_participant_block_message(rules_callback, rule, session) is None
    assert account_bot_runtime._interaction_participant_block_message(stranger_callback, rule, session) == "请先加入本局，再操作牌桌按钮。"


@pytest.mark.asyncio
async def test_live_message_start_session_action_writes_interaction_session(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    rule = {
        "id": "ten-half-paid",
        "name": "十点半",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "participant_policy": "paid_pool",
        "chat_ids": [-100123],
        "valid_seconds": 600,
    }
    redis = _MemoryRedis()
    state = plugin_loader._AccountState(1)
    state.redis = redis
    record_action = AsyncMock()
    monkeypatch.setattr(plugin_loader, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(plugin_loader.account_bot_service, "get_transfer_notice_config", AsyncMock(return_value={"enabled": True, "rules": [rule]}))
    monkeypatch.setattr(plugin_loader, "record_action", record_action)

    failed = await plugin_loader._apply_userbot_event_bus_actions(
        state,
        None,
        SimpleNamespace(chat_id=-100123),
        plugin_key="ten_half",
        entry_key="start_ten_half",
        actions=[
            {
                "type": "start_session",
                "chat_id": -100123,
                "entry_key": "start_ten_half",
                "started_by_user_id": 999,
                "started_by_message_id": 60,
            }
        ],
        redis=redis,
    )

    assert failed is False
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100123, None)
    session = json.loads(redis.data[session_key])
    assert session["started_by_user_id"] == 999
    assert session["started_by_message_id"] == 60
    assert session["paid_user_ids"] == []
    assert session["participant_user_ids"] == []
    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_OK
    assert record_action.await_args.kwargs["actual_send_via"] == "interaction_session"


@pytest.mark.asyncio
async def test_payment_interaction_payload_uses_replied_user_as_payer(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        message_id=70,
        text="转账成功\n付款人：玩家A\n收款人：Owner\n金额：10",
        display_name="TransferBot",
        reply_to_user_id=111,
        reply_to_message_id=66,
        reply_to_display_name="玩家A",
        reply_to_username="aaa",
    )
    rule = {
        "id": "blackjack-paid",
        "name": "21 点",
        "module_key": "blackjack",
        "module_action": "start_blackjack",
        "module_session_scope": "chat",
        "concurrency": "chat",
        "valid_seconds": 600,
        "module_prize": 10,
    }

    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))

    payload = await account_bot_runtime._interaction_module_payload_async(
        incoming,
        rule,
        {"payer_name": "玩家A", "receiver_name": "Owner", "amount": 10},
        event_type="payment_confirmed",
    )

    assert payload["payer_user_id"] == 111
    assert payload["payer_name"] == "玩家A"
    assert payload["actor"]["user_id"] == 111
    assert payload["actor"]["display_name"] == "玩家A"
    assert payload["actor"]["username"] == "aaa"
    assert payload["sender"]["user_id"] == 456
    assert payload["sender"]["display_name"] == "TransferBot"
    assert payload["source_actor"]["user_id"] == 456
    assert payload["player"]["user_id"] == 111
    assert payload["player"]["identity_confidence"] == "reply_context"
    assert payload["payment"]["payer_user_id"] == 111
    assert payload["payment"]["payer_display_name"] == "玩家A"
    assert payload["payment"]["receiver_display_name"] == "Owner"
    assert payload["payment"]["source_message_id"] == 70
    assert payload["payment"]["reply_to_message_id"] == 66
    assert payload["payment"]["notice_sender_user_id"] == 456
    assert payload["sender_user_id"] == 456
    assert payload["sender_name"] == "TransferBot"
    assert payload["session"]["key"] == account_bot_runtime._interaction_session_key(1, rule, -100123, 111)
    assert payload["settlement"]["winner_user_id"] == 111
    assert payload["settlement"]["winner_name"] == "玩家A"


@pytest.mark.asyncio
async def test_payment_payload_name_only_does_not_fallback_payer_to_notice_bot(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=10,
        user_id=456,
        chat_id=-100123,
        message_id=70,
        text="转账成功\n付款人：玩家A\n收款人：Owner\n金额：10",
        display_name="TransferBot",
    )
    rule = {
        "id": "game24-paid",
        "name": "24 点",
        "module_key": "game24",
        "module_action": "start_paid_game",
        "module_session_scope": "chat",
        "participant_policy": "open_race",
        "valid_seconds": 600,
        "module_prize": 10,
    }

    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))

    payload = await account_bot_runtime._interaction_module_payload_async(
        incoming,
        rule,
        {"payer_name": "玩家A", "receiver_name": "Owner", "amount": 10},
        event_type="payment_confirmed",
    )

    assert payload["payer_user_id"] is None
    assert payload["payer_name"] == "玩家A"
    assert payload["actor"]["user_id"] is None
    assert payload["actor"]["display_name"] == "玩家A"
    assert payload["source_actor"]["user_id"] == 456
    assert payload["player"]["user_id"] is None
    assert payload["player"]["identity_key"] == "name:玩家a"
    assert payload["player"]["identity_confidence"] == "name_only"
    assert payload["payment"]["payer_user_id"] is None
    assert payload["payment"]["notice_sender_user_id"] == 456
    assert payload["settlement"]["winner_user_id"] is None
    assert payload["settlement"]["winner_name"] == "玩家A"


def test_decrypt_bot_token_failure_is_user_fixable(monkeypatch) -> None:
    row = AccountBot(account_id=1, bot_token_enc="old-key-token")
    monkeypatch.setattr(
        account_bot_service,
        "decrypt_str",
        lambda _value: (_ for _ in ()).throw(ValueError("解密失败：可能 MASTER_KEY 已变更")),
    )

    with pytest.raises(account_bot_service.HTTPException) as exc_info:
        account_bot_service.decrypt_bot_token(row)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "ACCOUNT_BOT_TOKEN_DECRYPT_FAILED"


@pytest.mark.asyncio
async def test_transfer_notice_from_unauthed_abot_sends_group_notice(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    send = AsyncMock()
    find_user = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", find_user)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_id": -100123,
                "trusted_bot_id": 456,
                "trigger_text": "转账成功",
                "receiver_text": "我的TG名",
                "amount": 100,
                "response_template": "检测到 {payer_name} 向 {receiver_name} 转账 {amount}，已进入游戏流程。",
            }
        ),
    )
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "text": "转账成功\n付款人：路人A\n收款人：我的TG名\n金额：100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert find_user.await_count == 0
    assert send.await_count == 1
    assert send.await_args.args[:3] == (
        "bbot-token",
        -100123,
        "检测到 路人A 向 我的TG名 转账 100，已进入游戏流程。",
    )


@pytest.mark.asyncio
async def test_reply_plus_amount_sends_transfer_notice_with_abot_token(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 456}, "message_id": 22}, {}])
    find_user = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", find_user)
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_id": -100123,
                "trusted_bot_id": 456,
                "trigger_text": "转账成功",
                "receiver_text": None,
                "amount": None,
                "action": "notice",
                "math_prize": 123,
                "response_template": "检测到 {payer_name} 向 {receiver_name} 转账 {amount}，已进入游戏流程。",
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 2,
            "message": {
                "message_id": 20,
                "text": "+254",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 19,
                    "from": {"id": 222, "first_name": "BBB"},
                    "text": "hello",
                },
            },
        },
    )

    assert find_user.await_count == 0
    assert send.await_count == 1
    assert send.await_args_list[0].args[:3] == (
        "abot-token",
        -100123,
        '<pre><code class="language-转账成功">付款人：AAA\n'
        "付款人ID：111\n"
        "收款人：BBB\n"
        "金额：254\n"
        "收款人ID：222</code></pre>",
    )


@pytest.mark.asyncio
async def test_plus_amount_falls_back_to_receiver_config_when_reply_missing(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 456}, "message_id": 22}, {}])
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_id": -100123,
                "trusted_bot_id": 456,
                "trigger_text": "模拟到账",
                "receiver_text": "BBB",
                "amount": None,
                "action": "notice",
                "math_prize": 123,
                "response_template": "检测到 {payer_name} 向 {receiver_name} 转账 {amount}，已进入游戏流程。",
                "transfer_notice_template": "模拟到账\n付款人：{payer_name}\n收款人：{receiver_name}\n金额：{amount}",
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 3,
            "message": {
                "message_id": 30,
                "text": "+100",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert send.await_args_list[0].args[:3] == (
        "abot-token",
        -100123,
        "模拟到账\n付款人：AAA\n收款人：BBB\n金额：100",
    )
    assert send.await_args_list[0].kwargs["reply_to_message_id"] == 30


@pytest.mark.asyncio
async def test_plus_amount_notice_ignores_rule_amount_threshold(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 456}, "message_id": 22}, {}])
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100123],
                "trusted_bot_id": 456,
                "transfer_notice_template": "转账成功\n{payer_name} 射出 {amount}\n{receiver_name} 接收 {amount}",
                "rules": [
                    {
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "amount_match_mode": "eq",
                        "action": "math10",
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "text": "+88",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 39,
                    "from": {"id": 222, "first_name": "BBB"},
                    "text": "hello",
                },
            },
        },
    )

    assert send.await_args_list[0].args[:3] == (
        "abot-token",
        -100123,
        "转账成功\nAAA 射出 88\nBBB 接收 88",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_to_message", "expected_send_count"),
    [
        (None, 1),
        (
            {
                "message_id": 49,
                "from": {"id": 8875144459, "is_bot": True, "first_name": "玩法Bot"},
                "text": "该傻瓜玩法是付费娱乐模块，请对收款人：@uhaveanswer的任意消息回复+234即可参与。",
            },
            0,
        ),
    ],
    ids=["plain_plus_amount", "reply_to_interaction_bot_prompt"],
)
async def test_interaction_bot_plus_amount_uses_default_receiver_only_without_configured_bot_reply(
    monkeypatch,
    reply_to_message,
    expected_send_count,
) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=1682400007, tg_username="uhaveanswer", display_name="你心里已经有答案了")
            return None

    account_bot_runtime._TRANSFER_COMMAND_DEDUPE.clear()
    send = AsyncMock(return_value={"from": {"id": 456}, "message_id": 50})
    get_transfer_bot_token = AsyncMock(return_value="abot-token")
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", get_transfer_bot_token)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 8875144459,
                "trusted_bot_id": 456,
                "transfer_bot_id": 456,
                "transfer_notice_template": "转账成功\n{payer_name} 射出 {amount}\n{receiver_name} 接收 {amount}\n{receiver_user_id_line}",
                "rules": [
                    {
                        "id": "silly-math",
                        "name": "傻瓜玩法",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["我是傻瓜！"],
                        "amount": 234,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "math10",
                        "module_action": "start_math_game",
                    },
                ],
            }
        ),
    )

    message = {
        "message_id": 50,
        "text": "+234",
        "from": {"id": 5843467471, "first_name": "玩家A"},
        "chat": {"id": -100123, "type": "supergroup"},
    }
    if reply_to_message is not None:
        message["reply_to_message"] = reply_to_message

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 50,
            "message": message,
        },
    )

    assert send.await_count == expected_send_count
    if expected_send_count == 0:
        get_transfer_bot_token.assert_not_awaited()
        account_bot_runtime._TRANSFER_COMMAND_DEDUPE.clear()
        return
    assert send.await_count == 1
    assert send.await_args.args[:2] == ("abot-token", -100123)
    notice = send.await_args.args[2]
    assert "玩家A 射出 234" in notice
    assert "uhaveanswer 接收 234" in notice
    assert "收款人ID：1682400007" in notice
    assert "玩法Bot 接收" not in notice
    assert send.await_args.kwargs["reply_to_message_id"] == 50
    account_bot_runtime._TRANSFER_COMMAND_DEDUPE.clear()


@pytest.mark.asyncio
async def test_interaction_bot_plus_amount_logs_test_notice_send_failure_without_crashing(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=1682400007, tg_username="uhaveanswer", display_name="你心里已经有答案了")
            return None

    send = AsyncMock(side_effect=RuntimeError("Bad Request: chat not found"))
    runtime_log = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", runtime_log)
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 8875144459,
                "trusted_bot_id": 6920251805,
                "transfer_bot_id": 8980553289,
                "transfer_notice_template": "转账成功\n{payer_name} 射出 {amount}\n{receiver_name} 接收 {amount}",
                "rules": [
                    {
                        "id": "silly-math",
                        "name": "傻瓜玩法",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["我是傻瓜！"],
                        "receiver_text": "你心里已经有答案了",
                        "amount": 234,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "math10",
                        "module_action": "start_math_game",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 51,
            "message": {
                "message_id": 510,
                "text": "+234",
                "from": {"id": 5843467471, "first_name": "玩家A"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 49,
                    "from": {"id": 8875144459, "is_bot": True, "first_name": "玩法Bot"},
                    "text": "该傻瓜玩法是付费娱乐模块，请对收款人：@uhaveanswer的任意消息回复+234即可参与。",
                },
            },
        },
    )

    send.assert_awaited_once()
    runtime_log.assert_awaited_once()
    assert runtime_log.await_args.args[1] == account_bot_runtime.LEVEL_WARN
    assert "等待真实转账结果通知" in runtime_log.await_args.args[2]
    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_FAILED


@pytest.mark.asyncio
async def test_reply_plus_amount_without_transfer_bot_token_waits_for_real_notice(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value=None))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100123],
                "trusted_bot_id": None,
                "trigger_texts": ["转账成功", "交易成功"],
                "receiver_text": None,
                "amount": None,
                "action": "notice",
                "math_prize": 123,
                "response_template": "检测到 {payer_name} 向 {receiver_name} 转账 {amount}，已进入游戏流程。",
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "text": "+100",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 39,
                    "from": {"id": 222, "first_name": "BBB"},
                    "text": "hello",
                },
            },
        },
    )

    assert send.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule_patch", "closed", "expected_send_count"),
    [
        ({"trigger_mode": "keyword"}, False, 1),
        ({"trigger_mode": "both"}, True, 1),
    ],
)
async def test_reply_plus_amount_notice_ignores_rule_trigger_mode_and_state(
    monkeypatch,
    rule_patch: dict[str, object],
    closed: bool,
    expected_send_count: int,
) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    rule = {
        "id": "game24-ticket",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_texts": ["转账成功"],
        "receiver_text": "BBB",
        "amount": 100,
        "action": "module",
        "module_key": "game24",
        "module_action": "start_paid_game",
        **rule_patch,
    }
    redis = _MemoryRedis()
    if closed:
        redis.data[account_bot_runtime._rule_state_key(1, rule, -100123)] = "closed"

    send = AsyncMock()
    get_transfer_bot_token = AsyncMock(return_value="abot-token")
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", get_transfer_bot_token)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "trusted_bot_id": 456, "rules": [rule]}),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 6,
            "message": {
                "message_id": 60,
                "text": "+100",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 59,
                    "from": {"id": 222, "first_name": "BBB"},
                    "text": "hello",
                },
            },
        },
    )

    assert send.await_count == expected_send_count
    assert get_transfer_bot_token.await_count == expected_send_count


@pytest.mark.asyncio
async def test_reply_plus_amount_emits_test_notice_without_triggering_module(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    send = AsyncMock()
    run_entry = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "game24-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "A",
                        "amount": 100,
                        "action": "module",
                        "module_key": "game24",
                        "module_prize": 123,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 8,
            "message": {
                "message_id": 80,
                "text": "+100",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 79,
                    "from": {"id": 222, "first_name": "B"},
                    "text": "hello",
                },
            },
        },
    )

    assert send.await_count == 1
    assert send.await_args.args[:3] == (
        "abot-token",
        -100123,
        '<pre><code class="language-转账成功">付款人：AAA\n'
        "付款人ID：111\n"
        "收款人：B\n"
        "金额：100\n"
        "收款人ID：222</code></pre>",
    )
    assert run_entry.await_count == 0


@pytest.mark.asyncio
async def test_transfer_test_bot_ignores_plus_amount_to_configured_bots(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100123],
                "interaction_bot_id": 8807483916,
                "transfer_bot_id": 8980553289,
                "trusted_bot_id": 8980553289,
                "rules": [{"enabled": True, "chat_ids": [-100123]}],
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "abot-token",
        {
            "update_id": 81,
            "message": {
                "message_id": 81,
                "text": "+100",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 80,
                    "from": {"id": 8807483916, "first_name": "交互Bbot", "username": "TelePilotBbot", "is_bot": True},
                    "text": "hello",
                },
            },
        },
    )

    assert send.await_count == 0
    assert account_bot_service.get_transfer_bot_token.await_count == 0


@pytest.mark.asyncio
async def test_transfer_test_bot_only_emits_notice_without_starting_module(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 8980553289}, "message_id": 220}, {}])
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "骰子游戏已开始，奖金 123"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 8980553289,
                "rules": [
                    {
                        "id": "dice-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "receiver_user_id": 8629045843,
                        "receiver_text": "@uhavebnum",
                        "amount": 111,
                        "action": "module",
                        "module_key": "dice_grid_hunt",
                        "module_action": "start_dice_grid_hunt",
                        "module_prize": 123,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 9,
            "message": {
                "message_id": 90,
                "text": "+111",
                "from": {"id": 1682400007, "first_name": "你心里已经有答案了", "username": "uhaveanswer"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 89,
                    "from": {"id": 8629045843, "first_name": "你心里没点数？", "username": "uhavebnum"},
                    "text": "开启游戏",
                },
            },
        },
    )

    assert send.await_count == 1
    assert send.await_args_list[0].args[:3] == (
        "abot-token",
        -100123,
        '<pre><code class="language-转账成功">付款人：你心里已经有答案了\n'
        "付款人ID：1682400007\n"
        "收款人：你心里没点数？\n"
        "金额：111\n"
        "收款人ID：8629045843</code></pre>",
    )
    assert run_entry.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_update_uses_unescaped_notice_text(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 456}, "message_id": 220}, {}])
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "已开始"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "game24-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "B&B",
                        "amount": 100,
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_paid_game",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 10,
            "message": {
                "message_id": 100,
                "text": "转账成功\n付款人：A&B\n付款人ID：111\n收款人：B&B\n金额：100\n收款人ID：222",
                "from": {"id": 456, "first_name": "Abot", "is_bot": True},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["payer_name"] == "A&B"
    assert payload["receiver_name"] == "B&B"


@pytest.mark.asyncio
async def test_transfer_notice_reply_payer_is_used_for_module_owner(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    redis = _MemoryRedis()
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "已开始"}]))
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "blackjack-paid",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "Owner",
                        "amount": 10,
                        "action": "module",
                        "module_key": "blackjack",
                        "module_action": "start_blackjack",
                        "module_session_scope": "chat",
                        "concurrency": "chat",
                        "module_prize": 10,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 11,
            "message": {
                "message_id": 110,
                "text": "转账成功\n玩家A 射出 10\nOwner 接收 10",
                "from": {"id": 456, "first_name": "TransferBot", "is_bot": True},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 109,
                    "from": {"id": 111, "first_name": "玩家A", "username": "aaa"},
                    "text": "+10",
                },
            },
        },
    )

    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["trace_id"]
    assert payload["payer_user_id"] == 111
    assert payload["payer_name"] == "玩家A"
    assert payload["actor"]["user_id"] == 111
    assert payload["actor"]["display_name"] == "玩家A"
    assert payload["source_actor"]["user_id"] == 456
    assert payload["source_actor"]["display_name"] == "TransferBot"
    assert payload["player"]["user_id"] == 111
    assert payload["player"]["identity_confidence"] == "reply_context"
    assert payload["payment"]["payer_user_id"] == 111
    assert payload["payment"]["notice_sender_user_id"] == 456
    assert payload["sender_user_id"] == 456
    assert payload["sender_name"] == "TransferBot"
    assert payload["session"]["key"] == account_bot_runtime._interaction_session_key(
        1,
        account_bot_service.get_transfer_notice_config.return_value["rules"][0],
        -100123,
        111,
    )
    payer_key = account_bot_runtime._interaction_session_key(
        1,
        account_bot_service.get_transfer_notice_config.return_value["rules"][0],
        -100123,
        111,
    )
    assert payer_key in redis.data
    assert '"started_by_user_id": 111' in redis.data[payer_key]
    assert '"source_user_id": 456' in redis.data[payer_key]


@pytest.mark.asyncio
async def test_solo_payment_without_user_id_waits_for_payer_confirmation(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    redis = _MemoryRedis()
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "已开始"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "blackjack-paid",
                        "name": "21 点",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "Owner",
                        "amount": 10,
                        "action": "module",
                        "module_key": "blackjack",
                        "module_action": "start_blackjack",
                        "module_session_scope": "chat",
                        "participant_policy": "solo_owner",
                        "concurrency": "chat",
                        "module_prize": 10,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 12,
            "message": {
                "message_id": 120,
                "text": "转账成功\n付款人：玩家A\n收款人：Owner\n金额：10",
                "from": {"id": 456, "first_name": "TransferBot", "is_bot": True},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_not_awaited()
    assert send.await_count == 1
    assert "还需要绑定真实 Telegram 用户" in send.await_args.args[2]
    reply_markup = send.await_args.kwargs["reply_markup"]
    callback_data = reply_markup["inline_keyboard"][0][0]["callback_data"]
    assert callback_data.startswith("ip:")
    assert any(key.startswith("account_bot:interaction_payment_confirm:") for key in redis.data)


@pytest.mark.asyncio
async def test_payment_confirmation_callback_starts_solo_module_with_clicker_as_player(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    rule = {
        "id": "blackjack-paid",
        "name": "21 点",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "payment",
        "trigger_texts": ["转账成功"],
        "receiver_text": "Owner",
        "amount": 10,
        "action": "module",
        "module_key": "blackjack",
        "module_action": "start_blackjack",
        "module_session_scope": "chat",
        "participant_policy": "solo_owner",
        "concurrency": "chat",
        "module_prize": 10,
    }
    redis = _MemoryRedis()
    nonce = "confirm-token"
    redis.data[account_bot_runtime._interaction_payment_confirm_key(nonce)] = json.dumps(
        {
            "account_id": 1,
            "rule": rule,
            "parsed": {"payer_name": "玩家A", "receiver_name": "Owner", "amount": 10},
            "incoming": {
                "update_id": 12,
                "user_id": 456,
                "chat_id": -100123,
                "chat_type": "supergroup",
                "message_id": 120,
                "text": "转账成功\n付款人：玩家A\n收款人：Owner\n金额：10",
                "display_name": "TransferBot",
                "entity_languages": [],
            },
        },
        ensure_ascii=False,
    )
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "已开始"}]))
    answer = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "trusted_bot_id": 456, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 13,
            "callback_query": {
                "id": "confirm-callback",
                "data": f"ip:{nonce}",
                "from": {"id": 111, "first_name": "玩家A", "username": "aaa"},
                "message": {
                    "message_id": 121,
                    "text": "付款人请点击下方按钮确认开始。",
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        },
    )

    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["payer_user_id"] == 111
    assert payload["payer_name"] == "玩家A"
    assert payload["actor"]["user_id"] == 111
    assert payload["source_actor"]["user_id"] == 456
    assert payload["player"]["user_id"] == 111
    assert payload["player"]["identity_confidence"] == "callback_confirmed"
    assert payload["payment"]["payer_user_id"] == 111
    assert payload["payment"]["notice_sender_user_id"] == 456
    assert account_bot_runtime._interaction_payment_confirm_key(nonce) not in redis.data
    answer.assert_awaited_once_with("bbot-token", "confirm-callback", text="已确认，正在启动玩法。", show_alert=False)


@pytest.mark.asyncio
async def test_payment_confirmation_callback_name_mismatch_keeps_ticket(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    rule = {
        "id": "blackjack-paid",
        "name": "21 点",
        "enabled": True,
        "chat_ids": [-100123],
        "action": "module",
        "module_key": "blackjack",
        "module_action": "start_blackjack",
        "participant_policy": "solo_owner",
    }
    redis = _MemoryRedis()
    nonce = "confirm-token"
    key = account_bot_runtime._interaction_payment_confirm_key(nonce)
    redis.data[key] = json.dumps(
        {
            "account_id": 1,
            "rule": rule,
            "parsed": {"payer_name": "玩家A", "receiver_name": "Owner", "amount": 10},
            "incoming": {
                "update_id": 12,
                "user_id": 456,
                "chat_id": -100123,
                "chat_type": "supergroup",
                "message_id": 120,
                "text": "转账成功\n付款人：玩家A\n收款人：Owner\n金额：10",
                "display_name": "TransferBot",
            },
        },
        ensure_ascii=False,
    )
    run_entry = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "get_transfer_notice_config", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 14,
            "callback_query": {
                "id": "confirm-callback",
                "data": f"ip:{nonce}",
                "from": {"id": 222, "first_name": "别人", "username": "other"},
                "message": {
                    "message_id": 121,
                    "text": "付款人请点击下方按钮确认开始。",
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        },
    )

    run_entry.assert_not_awaited()
    assert key in redis.data
    answer.assert_awaited_once_with(
        "bbot-token",
        "confirm-callback",
        text="这条到账通知的付款人名称与你不一致。",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_solo_owner_session_blocks_other_user_callback(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    rule = {
        "id": "blackjack-paid",
        "name": "21 点",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "payment",
        "action": "module",
        "module_key": "blackjack",
        "module_action": "start_blackjack",
        "module_session_scope": "chat",
        "participant_policy": "solo_owner",
    }
    redis = _MemoryRedis()
    await redis.set(
        account_bot_runtime._interaction_session_key(1, rule, -100123, None),
        json.dumps(
            {
                "account_id": 1,
                "chat_id": -100123,
                "rule_id": "blackjack-paid",
                "module_key": "blackjack",
                "entry_key": "start_blackjack",
                "started_by_user_id": 111,
                "source_user_id": 456,
                "event_type": "payment_confirmed",
            },
            ensure_ascii=False,
        ),
    )
    run_entry = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "trusted_bot_id": 456, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 15,
            "callback_query": {
                "id": "blackjack-callback",
                "data": "hit",
                "from": {"id": 222, "first_name": "别人", "username": "other"},
                "message": {
                    "message_id": 122,
                    "text": "21 点",
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        },
    )

    run_entry.assert_not_awaited()
    answer.assert_awaited_once_with(
        "bbot-token",
        "blackjack-callback",
        text="这不是你的玩法，请由付款或开局本人操作。",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_paid_pool_session_blocks_plain_message_before_plugin(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    rule = {
        "id": "ten-half-paid",
        "name": "十点半",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "both",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "participant_policy": "paid_pool",
    }
    redis = _MemoryRedis()
    await redis.set(
        account_bot_runtime._interaction_session_key(1, rule, -100123, None),
        json.dumps(
            {
                "account_id": 1,
                "chat_id": -100123,
                "rule_id": "ten-half-paid",
                "module_key": "ten_half",
                "entry_key": "start_ten_half",
                "started_by_user_id": 111,
                "participant_user_ids": [111],
                "paid_user_ids": [111],
                "event_type": "payment_confirmed",
            },
            ensure_ascii=False,
        ),
    )
    run_entry = AsyncMock(return_value=(True, None, []))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 16,
            "message": {
                "message_id": 123,
                "text": "1",
                "from": {"id": 222, "first_name": "路过"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_not_awaited()
    send.assert_awaited_once()
    assert send.await_args.args[:3] == ("bbot-token", -100123, "请先加入本局，再操作牌桌按钮。")


@pytest.mark.asyncio
async def test_transfer_notice_uses_first_matching_interaction_rule(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "trigger_text": "转账成功",
                "trigger_texts": ["转账成功"],
                "rules": [
                    {
                        "id": "notice-100",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "amount": 100,
                        "action": "notice",
                        "math_prize": 123,
                        "response_template": "不应该命中",
                    },
                    {
                        "id": "math-200",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["交易成功", "转账成功"],
                        "receiver_text": "BBB",
                        "amount": 200,
                        "action": "math10",
                        "math_prize": 456,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 6,
            "message": {
                "message_id": 60,
                "text": "转账成功\nAAA 射出 200\nBBB 接收 200",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    assert send.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_skips_module_rule_when_configured_bet_mismatches(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_load_enabled_event_bus_subscriptions", AsyncMock(return_value=[]))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "ten-half-paid",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": None,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "ten_half",
                        "module_action": "start_ten_half",
                        "module_session_scope": "chat",
                        "module_config": {"bet": 100, "max_players": 2},
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 600,
            "message": {
                "message_id": 6000,
                "text": "转账成功\nAAA 射出 10\nBBB 接收 10",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_notice_matches_html_code_language_marker(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="answer", display_name="答案", tg_user_id=999)
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "math-language-marker",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "你心里已经有答案了",
                        "amount": 1,
                        "action": "math10",
                        "math_prize": 456,
                    },
                ],
            }
        ),
    )

    text = "Yy 射出 1 蝌蚪\n你心里已经有答案了 接收 1 蝌蚪"
    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 61,
            "message": {
                "message_id": 610,
                "text": text,
                "entities": [{"type": "pre", "offset": 0, "length": len(text), "language": "language-转账成功"}],
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    assert send.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_parses_amount_from_replied_message(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="answer", display_name="答案", tg_user_id=999)
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "math-reply-transfer",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "你心里已经有答案了",
                        "amount": 1,
                        "action": "math10",
                        "math_prize": 456,
                    },
                ],
            }
        ),
    )

    transfer_text = "Yy 射出 1 蝌蚪\n你心里已经有答案了 接收 1 蝌蚪"
    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 62,
            "message": {
                "message_id": 620,
                "text": "转账成功",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 619,
                    "text": transfer_text,
                    "from": {"id": 999, "is_bot": False, "first_name": "Yy"},
                },
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    assert send.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_skips_closed_rule_and_uses_next_open_match(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    closed_rule = {
        "id": "closed-notice",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "payment",
        "trigger_texts": ["转账成功"],
        "receiver_text": "BBB",
        "amount": 200,
        "action": "notice",
        "response_template": "不应该命中",
    }
    open_rule = {
        "id": "open-math",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "payment",
        "trigger_texts": ["转账成功"],
        "receiver_text": "BBB",
        "amount": 200,
        "action": "math10",
        "math_prize": 456,
        "response_template": "忽略",
    }
    redis = _MemoryRedis()
    redis.data[account_bot_runtime._rule_state_key(1, closed_rule, -100123)] = "closed"
    run_entry = _patch_math10_worker_entry(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "trigger_text": "转账成功",
                "trigger_texts": ["转账成功"],
                "rules": [closed_rule, open_rule],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 62,
            "message": {
                "message_id": 620,
                "text": "转账成功\nAAA 射出 200\nBBB 接收 200",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    _assert_math10_worker_entry(run_entry)
    assert send.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_blank_receiver_defaults_to_account_identity(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=999, tg_username="account_a", display_name="A")
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "math-default-receiver",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": None,
                        "amount": 100,
                        "action": "math10",
                        "math_prize": 456,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 61,
            "message": {
                "message_id": 610,
                "text": "转账成功\nAAA 射出 100\nB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 0
    assert send.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_receiver_user_id_takes_priority(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "math-by-id",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_user_id": 222,
                        "receiver_text": "A",
                        "amount": 100,
                        "action": "math10",
                        "math_prize": 456,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    for update_id, receiver_id in [(62, 333), (63, 222)]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id * 10,
                    "text": f"转账成功\nAAA 射出 100\nA 接收 100\n收款人ID：{receiver_id}",
                    "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    _assert_math10_worker_entry(run_entry)


@pytest.mark.asyncio
async def test_transfer_notice_receiver_user_id_only_requires_notice_id(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "math-by-id-only",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_user_id": 222,
                        "receiver_text": None,
                        "amount": 100,
                        "action": "math10",
                        "math_prize": 456,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 64,
            "message": {
                "message_id": 640,
                "text": "转账成功\nAAA 射出 100\nA 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 0


@pytest.mark.asyncio
async def test_transfer_notice_receiver_user_id_can_fallback_to_receiver_text_without_notice_id(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "math-by-id-or-name",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_user_id": 222,
                        "receiver_text": "A",
                        "amount": 100,
                        "action": "math10",
                        "math_prize": 456,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 65,
            "message": {
                "message_id": 650,
                "text": "转账成功\nAAA 射出 100\nA 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    _assert_math10_worker_entry(run_entry)


@pytest.mark.asyncio
async def test_transfer_notice_accepts_detected_test_transfer_bot_id(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 999999,
                "transfer_bot_id": 456,
                "rules": [
                    {
                        "id": "paid-remote",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "@uhavebnum",
                        "amount": 111,
                        "action": "math10",
                        "math_prize": 456,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 65,
            "message": {
                "message_id": 650,
                "text": (
                    "转账成功\n"
                    "你心里已经有答案了 (@uhaveanswer) 射出 111\n"
                    "你心里没点数？ (@uhavebnum) 接收 111\n"
                    "收款人ID：8629045843"
                ),
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    _assert_math10_worker_entry(run_entry)


@pytest.mark.asyncio
async def test_interaction_keyword_invokes_module_interaction_entry_and_payment_mode_ignores_notice(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "24 点开始\n奖金：321"}],
        )
    )
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "keyword-game24",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "keyword",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["开24点"],
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_paid_game",
                        "module_prize": 321,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 991,
            "message": {
                "message_id": 9910,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )
    assert run_entry.await_count == 0

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 992,
            "message": {
                "message_id": 9920,
                "text": "开24点123",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )
    assert run_entry.await_count == 0

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 993,
            "message": {
                "message_id": 9930,
                "text": "开24点",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )
    assert run_entry.await_count == 1
    assert run_entry.await_args.kwargs["plugin_key"] == "game24"
    assert run_entry.await_args.kwargs["entry_key"] == "start_paid_game"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "keyword"
    assert payload["prize"] == 321
    assert send.await_args.args[:3] == ("bbot-token", -100123, "24 点开始\n奖金：321")


@pytest.mark.asyncio
async def test_interaction_keyword_accepts_anonymous_admin_sender_chat_without_owner_identity(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "已启动"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "keyword-game24",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "keyword",
                        "module_start_keywords": ["开24点"],
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_paid_game",
                        "module_prize": 321,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 994,
            "message": {
                "message_id": 9940,
                "text": "开24点",
                "from": {
                    "id": 1087968824,
                    "is_bot": True,
                    "first_name": "GroupAnonymousBot",
                    "username": "GroupAnonymousBot",
                },
                "sender_chat": {
                    "id": -100123,
                    "type": "supergroup",
                    "title": "Demo Group",
                    "username": "demo_group",
                },
                "chat": {"id": -100123, "type": "supergroup", "title": "Demo Group"},
            },
        },
    )

    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["sender_user_id"] is None
    assert payload["sender_chat_id"] == -100123
    assert payload["sender_name"] == "Demo Group"
    assert payload["event"]["user_id"] is None
    assert payload["event"]["sender_chat_id"] == -100123
    assert payload["actor"]["sender_type"] == "chat"
    assert payload["actor"]["sender_chat"]["id"] == -100123
    assert payload["source_actor"]["sender_type"] == "chat"
    assert payload["message"]["sender_chat"]["id"] == -100123
    assert payload["player"]["user_id"] is None
    assert payload["player"]["sender_type"] == "chat"
    assert payload["player"]["identity_key"] == "sender_chat:-100123"
    assert send.await_args.args[:3] == ("bbot-token", -100123, "已启动")


@pytest.mark.asyncio
async def test_interaction_keyword_user_cooldown_and_daily_limit_mark_only_after_success(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    rule = {
        "id": "pt-promote",
        "name": "置顶促销",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "keyword",
        "trigger_texts": ["转账成功"],
        "module_start_keywords": ["置顶 id=数字"],
        "action": "module",
        "module_key": "pt_promote",
        "module_action": "promote_torrent",
        "concurrency": "chat",
        "user_cooldown_seconds": "6h",
        "daily_limit_per_user": 2,
    }
    redis = _MemoryRedis()
    run_entry = AsyncMock(
        side_effect=[
            (False, "模块启动失败", []),
            (True, None, [{"type": "send_message", "text": "置顶成功"}, {"type": "no_session"}]),
        ]
    )
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    base_message = {
        "from": {"id": 111, "first_name": "Alice"},
        "chat": {"id": -100123, "type": "supergroup"},
    }
    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {"update_id": 1001, "message": {"message_id": 10010, "text": "置顶 id=12345", **base_message}},
    )
    assert run_entry.await_count == 1
    assert not any(key.startswith("account_bot:interaction_user_") for key in redis.data)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {"update_id": 1002, "message": {"message_id": 10020, "text": "置顶 id=12345", **base_message}},
    )
    assert run_entry.await_count == 2
    assert send.await_args.args[:3] == ("bbot-token", -100123, "置顶成功")
    assert any(key.startswith("account_bot:interaction_user_cooldown:") for key in redis.data)
    assert any(key.startswith("account_bot:interaction_user_daily:") for key in redis.data)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {"update_id": 1003, "message": {"message_id": 10030, "text": "置顶 id=12345", **base_message}},
    )
    assert run_entry.await_count == 2
    assert "今日已成功置顶促销 1/2 次" in send.await_args.args[2]
    assert "距离下次可用 CD 还剩 6小时" in send.await_args.args[2]


@pytest.mark.asyncio
async def test_interaction_keyword_module_result_false_does_not_mark_usage(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    rule = {
        "id": "pt-promote",
        "name": "置顶促销",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "keyword",
        "trigger_texts": ["转账成功"],
        "module_start_keywords": ["置顶 id=数字"],
        "action": "module",
        "module_key": "pt_promote",
        "module_action": "promote_torrent",
        "concurrency": "user",
        "user_cooldown_seconds": "6h",
        "daily_limit_per_user": 2,
    }
    redis = _MemoryRedis()
    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [
                {"type": "send_message", "text": "ℹ️ ID 为 12345 的种子已处于置顶状态，本次不再处理。"},
                {"type": "result", "success": False},
                {"type": "no_session"},
            ],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    message = {
        "from": {"id": 111, "first_name": "Alice"},
        "chat": {"id": -100123, "type": "supergroup"},
    }
    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {"update_id": 1001, "message": {"message_id": 10010, "text": "置顶 id=12345", **message}},
    )

    assert run_entry.await_count == 1
    assert "本次不再处理" in send.await_args.args[2]
    assert not any(key.startswith("account_bot:interaction_user_cooldown:") for key in redis.data)
    assert not any(key.startswith("account_bot:interaction_user_daily:") for key in redis.data)


@pytest.mark.asyncio
async def test_interaction_module_start_text_is_sent_before_module_actions(monkeypatch) -> None:
    send = AsyncMock(return_value={"message_id": 88})
    edit = AsyncMock(return_value={"message_id": 88})
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "模块已开始"}],
        )
    )
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "edit_message", edit)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=111,
        chat_id=-100123,
        message_id=10,
        text="开局",
        display_name="AAA",
    )

    ok = await account_bot_runtime._start_interaction_module(
        incoming,
        {
            "id": "module",
            "name": "互动模块",
            "module_key": "game24",
            "module_action": "start_paid_game",
            "module_start_text": "正在启动{规则名称}",
        },
    )

    assert ok is True
    send.assert_awaited_once()
    assert send.await_args.args[:3] == ("bbot-token", -100123, "正在启动互动模块")
    assert send.await_args.kwargs["reply_to_message_id"] == 10
    edit.assert_awaited_once()
    assert edit.await_args.args[:4] == ("bbot-token", -100123, 88, "模块已开始")


@pytest.mark.asyncio
async def test_interaction_module_can_request_no_session(monkeypatch) -> None:
    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "下注成功"}, {"type": "end_session"}],
        )
    )
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)

    ok = await account_bot_runtime._start_interaction_module(
        account_bot_runtime.Incoming(
            account_id=1,
            token="bbot-token",
            update_id=1,
            user_id=111,
            chat_id=-100123,
            message_id=10,
            text="转账成功",
            display_name="AAA",
        ),
        {
            "id": "lotto",
            "name": "彩票",
            "module_key": "lottery_plus",
            "module_action": "start_lottery_plus",
        },
        parsed={"amount": 10003},
        event_type="payment_confirmed",
    )

    assert ok is False
    assert send.await_count == 1
    assert send.await_args.args[:3] == ("bbot-token", -100123, "下注成功")


@pytest.mark.asyncio
async def test_execute_interaction_rule_skips_module_when_event_not_declared(monkeypatch) -> None:
    run_module = AsyncMock(return_value=(True, True))
    monkeypatch.setattr(account_bot_runtime, "_run_interaction_module", run_module)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["keyword", "message"] if module_key == "pt_promote" and entry_key == "promote_torrent" else [],
    )

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=11,
        user_id=111,
        chat_id=-100123,
        message_id=99,
        text="转账成功",
        display_name="AAA",
    )
    rule = {
        "id": "pt-promote",
        "name": "置顶促销",
        "action": "module",
        "module_key": "pt_promote",
        "module_action": "promote_torrent",
    }

    executed = await account_bot_runtime._execute_interaction_rule(
        incoming,
        rule,
        {"id": "12345"},
        event_type="payment_confirmed",
    )

    assert executed is False
    run_module.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_keyword_cannot_bypass_paid_threshold(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    run_entry = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "paid-keyword-game24",
                        "name": "24点门票",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["开24点"],
                        "amount": 100,
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_paid_game",
                        "module_prize": 321,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 9921,
            "message": {
                "message_id": 99210,
                "text": "开24点",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 0
    assert send.await_count == 1
    assert send.await_args.args[2] == "该24点门票是付费娱乐模块，请对收款人：@owner的任意消息回复+100即可参与。"


@pytest.mark.asyncio
async def test_paid_pool_keyword_starts_module_before_payment_join(monkeypatch) -> None:
    redis = _MemoryRedis()
    run_entry = AsyncMock(return_value=(True, None, [{"type": "end_session"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda module_key, entry_key: {"participant_policy": "paid_pool"}
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else None,
    )
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["payment_confirmed", "keyword", "message", "callback_query", "session_close"]
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "plugin_declares_telegram_native_raw",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "ten-half-paid",
                        "name": "十点半",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "module_start_keywords": ["十点半测试"],
                        "amount": 1000,
                        "action": "module",
                        "module_key": "ten_half",
                        "module_action": "start_ten_half",
                        "module_session_scope": "chat",
                        "module_config": {"bet": 1000, "max_players": 5, "lobby_timeout": 60},
                        "module_start_text": "",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 9922,
            "message": {
                "message_id": 99220,
                "text": "十点半测试",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert send.await_count == 0
    run_entry.assert_awaited_once()
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "keyword"
    assert payload["trigger"]["keyword"] == "十点半测试"
    assert payload["trigger"]["start_keywords"] == ["十点半测试"]
    assert payload["module_config"]["bet"] == 1000


@pytest.mark.asyncio
async def test_interaction_closed_rule_only_replies_to_keyword(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=111, tg_username="owner", display_name="Owner")
            return None

    redis = _MemoryRedis()
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "keyword-game24",
                        "name": "24点",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["开24点"],
                        "close_commands": ["关闭24点"],
                        "disabled_message": "规则已关闭",
                        "receiver_text": "BBB",
                        "amount_match_mode": "gte",
                        "amount": 100,
                        "action": "notice",
                        "response_template": "不应该发送",
                    },
                ],
            }
        ),
    )

    for update_id, message_id, text, sender_id in [
        (993, 9930, "关闭24点", 111),
        (994, 9940, "转账成功\nAAA 射出 120\nBBB 接收 120", 456),
        (995, 9950, "开24点", 111),
    ]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "text": text,
                    "from": {"id": sender_id, "first_name": "AAA"},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    sent_texts = [call.args[2] for call in send.await_args_list]
    assert any(text.startswith("规则「24点」已关闭，") for text in sent_texts)
    assert "规则已关闭" in sent_texts
    assert "不应该发送" not in sent_texts


@pytest.mark.asyncio
async def test_interaction_rule_open_close_commands_require_account_owner(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=999, tg_username="owner", display_name="Owner")
            return None

    redis = _MemoryRedis()
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "game-rule",
                        "name": "游戏",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "keyword",
                        "module_start_keywords": ["我要游戏"],
                        "close_commands": ["关闭游戏"],
                        "disabled_message": "规则已关闭",
                        "action": "math10",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 996,
            "message": {
                "message_id": 9960,
                "text": "关闭游戏",
                "from": {"id": 111, "first_name": "User"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )
    assert send.await_count == 0

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 997,
            "message": {
                "message_id": 9970,
                "text": "关闭游戏",
                "from": {"id": 999, "first_name": "Owner"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    sent_texts = [call.args[2] for call in send.await_args_list]
    assert any(text.startswith("规则「游戏」已关闭，") for text in sent_texts)


@pytest.mark.asyncio
async def test_math10_builtin_action_starts_from_default_keyword(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = _patch_math10_worker_entry(monkeypatch)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value=account_bot_service.normalize_transfer_notice_config(
                {
                    "enabled": True,
                    "rules": [
                        {
                            "id": "math10",
                            "enabled": True,
                            "chat_ids": [-100123],
                            "trigger_mode": "payment",
                            "action": "math10",
                            "math_prize": 456,
                        }
                    ],
                }
            )
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 998,
            "message": {
                "message_id": 9980,
                "text": "发十以内算数",
                "from": {"id": 111, "first_name": "User"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    _assert_math10_worker_entry(run_entry)


@pytest.mark.asyncio
async def test_math10_module_rule_reports_worker_plugin_not_loaded(monkeypatch) -> None:
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_load_account_holder_label", AsyncMock(return_value="@owner"))
    monkeypatch.setattr(account_bot_runtime, "_resolve_payout_mode", AsyncMock(return_value="manual"))
    dispatch_event = MagicMock(side_effect=account_bot_runtime.dispatch_event)
    monkeypatch.setattr(account_bot_runtime, "dispatch_event", dispatch_event)
    monkeypatch.setattr(
        account_bot_runtime,
        "_run_worker_interaction_entry",
        AsyncMock(return_value=(False, "RuntimeError: 模块未加载或未启用：math10", [])),
    )
    send = AsyncMock(return_value={"message_id": 88})
    edit = AsyncMock(return_value={"message_id": 88})
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "edit_message", edit)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="开算数题",
        display_name="AAA",
    )

    ok, keep_session = await account_bot_runtime._run_interaction_module(
        incoming,
        {
            "id": "math10-module",
            "name": "算数题",
            "action": "module",
            "module_key": "math10",
            "module_action": "start_math_game",
            "module_prize": 456,
            "module_start_text": "正在启动互动模块...",
        },
        event_type="keyword",
    )

    assert ok is False
    assert keep_session is False
    dispatch_event.assert_called_once()
    assert dispatch_event.call_args.args[1][0].dispatch_mode == "rule_bound"
    assert send.await_count == 2
    assert send.await_args_list[0].args[:3] == ("bbot-token", -100123, "正在启动互动模块...")
    assert send.await_args_list[1].args[:3] == (
        "bbot-token",
        -100123,
        "模块启动失败：RuntimeError: 模块未加载或未启用：math10",
    )
    edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_query_command_lists_current_chat_games(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value=account_bot_service.normalize_transfer_notice_config(
                {
                    "enabled": True,
                    "query_commands": ["玩法菜单"],
                    "rules": [
                        {
                            "id": "dice",
                            "name": "九宫格",
                            "enabled": True,
                            "chat_ids": [-100123],
                            "trigger_mode": "both",
                            "trigger_texts": ["转账成功"],
                            "module_start_keywords": ["。ct num=数字"],
                            "receiver_text": "owner",
                            "amount": 123,
                            "amount_match_mode": "eq",
                            "action": "module",
                            "module_key": "dice_grid_hunt",
                            "module_action": "start_dice_grid_hunt",
                            "module_prize": 123,
                            "user_cooldown_seconds": "10s",
                            "daily_limit_per_user": 2,
                            "valid_seconds": 90,
                        },
                        {
                            "id": "math",
                            "name": "其他群算数题",
                            "enabled": True,
                            "chat_ids": [-100999],
                            "trigger_mode": "keyword",
                            "module_start_keywords": ["算数题"],
                            "action": "math10",
                            "math_prize": 88,
                        },
                        {
                            "id": "notice",
                            "name": "通知",
                            "enabled": True,
                            "chat_ids": [-100123],
                            "action": "notice",
                        },
                    ],
                }
            )
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1001,
            "message": {
                "message_id": 10010,
                "text": "玩法菜单",
                "from": {"id": 111, "first_name": "User"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert send.await_count == 1
    text = send.await_args.args[2]
    assert "当前可用联动玩法" in text
    assert "九宫格" in text
    assert "触发方式：转账或关键词" in text
    assert "。ct num=数字" in text
    assert "转账通知" in text
    assert "dice_grid_hunt" not in text
    assert "金额 = <code>123</code>" not in text
    assert "收款人" not in text
    assert "奖金" not in text
    assert "每用户 CD <code>10s</code>" not in text
    assert "每用户日上限 <code>2</code>" not in text
    assert "其他群算数题" not in text
    assert "<b>通知</b>" not in text
    assert send.await_args.kwargs["reply_to_message_id"] == 10010


def test_interaction_rule_drops_stale_module_prize_for_no_prize_entry() -> None:
    cfg = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "query_commands": ["玩法菜单"],
            "rules": [
                {
                    "id": "pt",
                    "name": "置顶促销",
                    "enabled": True,
                    "chat_ids": [-100123],
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["促销 id=12345"],
                    "action": "module",
                    "module_key": "pt_promote",
                    "module_action": "promote_torrent",
                    "module_prize": 123,
                    "valid_seconds": 600,
                }
            ],
        }
    )

    assert cfg["rules"][0]["module_prize"] is None
    assert cfg["module_prize"] is None


@pytest.mark.asyncio
async def test_interaction_query_template_hides_prize_for_no_prize_entry(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value=account_bot_service.normalize_transfer_notice_config(
                {
                    "enabled": True,
                    "query_commands": ["玩法菜单"],
                    "query_response_template": "当前 {count} 个玩法\n{items}\n关闭 {closed_count} 个",
                    "query_item_template": "{index}) {name}｜{trigger}｜{limit}",
                    "rules": [
                        {
                            "id": "pt",
                            "name": "置顶促销",
                            "enabled": True,
                            "chat_ids": [-100123],
                            "trigger_mode": "keyword",
                            "module_start_keywords": ["促销 id=12345"],
                            "action": "module",
                            "module_key": "pt_promote",
                            "module_action": "promote_torrent",
                            "module_prize": 123,
                            "user_cooldown_seconds": "12h",
                            "valid_seconds": 600,
                        }
                    ],
                }
            )
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 10011,
            "message": {
                "message_id": 10011,
                "text": "玩法菜单",
                "from": {"id": 111, "first_name": "User"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert send.await_count == 1
    text = send.await_args.args[2]
    assert "当前 1 个玩法" in text
    assert "1) 置顶促销｜关键词" in text
    assert "促销 id=12345" in text
    assert "每用户 CD <code>12h</code>" in text
    assert "奖金" not in text


@pytest.mark.asyncio
async def test_interaction_query_command_ignores_uncovered_chat(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value=account_bot_service.normalize_transfer_notice_config(
                {
                    "enabled": True,
                    "query_commands": ["玩法菜单"],
                    "rules": [
                        {
                            "id": "dice",
                            "name": "九宫格",
                            "enabled": True,
                            "chat_ids": [-100123],
                            "trigger_mode": "keyword",
                            "module_start_keywords": ["。ct num=数字"],
                            "action": "module",
                            "module_key": "dice_grid_hunt",
                            "module_action": "start_dice_grid_hunt",
                        }
                    ],
                }
            )
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1002,
            "message": {
                "message_id": 10020,
                "text": "玩法菜单",
                "from": {"id": 111, "first_name": "User"},
                "chat": {"id": -100999, "type": "supergroup"},
            },
        },
    )

    assert send.await_count == 0


@pytest.mark.asyncio
async def test_worker_suppresses_interaction_bot_owned_keyword(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(
                value={
                    "enabled": True,
                    "rules": [
                        {
                            "id": "remote-game",
                            "enabled": True,
                            "chat_ids": [-100123],
                            "module_start_keywords": ["我要猜骰"],
                            "open_commands": ["开启游戏"],
                            "close_commands": ["关闭游戏"],
                        }
                    ],
                }
            )

    state = SimpleNamespace(account_id=1)
    event = SimpleNamespace(chat_id=-100123, raw_text="我要猜骰")
    monkeypatch.setattr(plugin_loader, "AsyncSessionLocal", lambda: _DB())

    assert await plugin_loader._interaction_bot_owns_incoming_text(state, event) is True


@pytest.mark.asyncio
async def test_worker_suppresses_interaction_keyword_with_empty_chat_scope(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return SimpleNamespace(
                value={
                    "enabled": True,
                    "rules": [
                        {
                            "id": "global-keyword",
                            "enabled": True,
                            "chat_ids": [],
                            "module_start_keywords": ["我要猜骰"],
                        }
                    ],
                }
            )

    state = SimpleNamespace(account_id=1)
    event = SimpleNamespace(chat_id=-100456, raw_text="我要猜骰")
    monkeypatch.setattr(plugin_loader, "AsyncSessionLocal", lambda: _DB())

    assert await plugin_loader._interaction_bot_owns_incoming_text(state, event) is True


@pytest.mark.asyncio
async def test_interaction_plain_message_routes_to_worker_entry_as_message(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "收到答案"}],
        )
    )
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    dispatch_event = MagicMock(side_effect=account_bot_runtime.dispatch_event)
    monkeypatch.setattr(account_bot_runtime, "dispatch_event", dispatch_event)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "dice-active"}))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "dice-active",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["开始猜骰"],
                        "open_commands": ["开启猜骰"],
                        "close_commands": ["关闭猜骰"],
                        "status_commands": ["猜骰状态"],
                        "action": "module",
                        "module_key": "dice_grid_hunt",
                        "module_action": "answer_dice_grid_hunt",
                        "module_prize": 456,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1366,
            "message": {
                "message_id": 13660,
                "text": "我选第 3 格",
                "from": {"id": 111, "first_name": "AAA", "username": "aaa"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    dispatch_event.assert_called_once()
    assert dispatch_event.call_args.args[1][0].dispatch_mode == "rule_bound"
    assert run_entry.await_args.kwargs["plugin_key"] == "dice_grid_hunt"
    assert run_entry.await_args.kwargs["entry_key"] == "answer_dice_grid_hunt"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "message"
    assert payload["event"]["type"] == "message"
    assert payload["message_text"] == "我选第 3 格"
    assert payload["sender_user_id"] == 111
    assert payload["sender_username"] == "aaa"
    assert payload["message_id"] == 13660
    assert send.await_args.args[:3] == ("bbot-token", -100777, "收到答案")


@pytest.mark.asyncio
async def test_interaction_plain_message_skips_cross_channel_duplicate(monkeypatch) -> None:
    class _DB:
        async def get(self, *_args):  # noqa: ANN002
            return None

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=13661,
        user_id=111,
        chat_id=-100777,
        chat_type="supergroup",
        message_id=13661,
        text="我选第 4 格",
        display_name="AAA",
        trace_id="evt_dupe",
    )
    rule = {
        "id": "dice-active",
        "enabled": True,
        "chat_ids": [-100777],
        "action": "module",
        "module_key": "dice_grid_hunt",
        "module_action": "answer_dice_grid_hunt",
    }
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "nope"}]))
    record_span = AsyncMock()
    claim = AsyncMock(return_value=False)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "dice-active"}))
    monkeypatch.setattr(account_bot_runtime, "claim_interaction_message", claim)
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)

    handled = await account_bot_runtime._try_handle_interaction_module_message(
        _DB(),
        incoming,
        {"enabled": True, "rules": [rule]},
    )

    assert handled is True
    claim.assert_awaited_once_with(
        account_id=1,
        chat_id=-100777,
        message_id=13661,
        rule_id="dice-active",
    )
    run_entry.assert_not_awaited()
    assert any(
        call.args[1:3] == ("route", account_bot_runtime.TRACE_STATUS_SKIPPED)
        and call.kwargs.get("reason_code") == "cross_channel_duplicate"
        for call in record_span.await_args_list
    )


@pytest.mark.asyncio
async def test_interaction_plain_message_claim_miss_continues_to_other_rules(monkeypatch) -> None:
    class _DB:
        async def get(self, *_args):  # noqa: ANN002
            return None

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=13662,
        user_id=111,
        chat_id=-100777,
        chat_type="supergroup",
        message_id=13662,
        text="我选第 5 格",
        display_name="AAA",
        trace_id="evt_dupe_continue",
    )
    rule_a = {
        "id": "dice-a",
        "enabled": True,
        "chat_ids": [-100777],
        "action": "module",
        "module_key": "dice_grid_hunt",
        "module_action": "answer_a",
    }
    rule_b = {
        "id": "dice-b",
        "enabled": True,
        "chat_ids": [-100777],
        "action": "module",
        "module_key": "dice_grid_hunt",
        "module_action": "answer_b",
    }
    run_entry = AsyncMock(return_value=(True, None, []))
    claim = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "active"}))
    monkeypatch.setattr(account_bot_runtime, "claim_interaction_message", claim)
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled = await account_bot_runtime._try_handle_interaction_module_message(
        _DB(),
        incoming,
        {"enabled": True, "rules": [rule_a, rule_b]},
    )

    assert handled is True
    assert claim.await_count == 2
    assert [call.kwargs["rule_id"] for call in claim.await_args_list] == ["dice-a", "dice-b"]
    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["entry_key"] == "answer_b"


@pytest.mark.asyncio
async def test_interaction_media_message_without_text_routes_to_active_session(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "收到骰子"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "dice-active"}))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_events", lambda *_args: ["message"])
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "dice-active",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "dice_grid_hunt",
                        "module_action": "answer_dice_grid_hunt",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1367,
            "message": {
                "message_id": 13670,
                "date": 1710000100,
                "dice": {"emoji": "🎲", "value": 5},
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "message"
    assert payload["message"]["text"] == ""
    assert payload["message"]["media"]["type"] == "dice"
    assert payload["message"]["media"]["emoji"] == "🎲"
    assert payload["message"]["date"] == 1710000100
    assert send.await_args.args[:3] == ("bbot-token", -100777, "收到骰子")


@pytest.mark.asyncio
async def test_interaction_edited_message_routes_to_worker_entry(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "收到编辑"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "edit-active"}))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_events", lambda *_args: ["message_edited"])
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "edit-active",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "edit_game",
                        "module_action": "answer",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1369,
            "edited_message": {
                "message_id": 13690,
                "date": 1710000200,
                "edit_date": 1710000300,
                "text": "改成 B",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "message_edited"
    assert payload["source"]["type"] == "message_edited"
    assert payload["message"]["edited"] is True
    assert payload["raw"]["edit_date"] == 1710000300
    assert send.await_args.args[:3] == ("bbot-token", -100777, "收到编辑")


@pytest.mark.asyncio
async def test_interaction_callback_routes_disabled_active_session_to_worker_entry_and_answers_callback(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [
                {
                    "type": "send_message",
                    "text": "已记录按钮选择",
                    "reply_markup": {"inline_keyboard": [[{"text": "继续", "callback_data": "next"}]]},
                }
            ],
        )
    )
    send = AsyncMock()
    answer = AsyncMock()
    redis = _MemoryRedis()
    rule = {
        "id": "button-game",
        "enabled": False,
        "chat_ids": [-100777],
        "action": "module",
        "module_key": "button_game",
        "module_action": "play",
        "module_prize": 456,
    }
    await redis.set(account_bot_runtime._rule_state_key(1, rule, -100777), "closed")
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    dispatch_event = MagicMock(side_effect=account_bot_runtime.dispatch_event)
    monkeypatch.setattr(account_bot_runtime, "dispatch_event", dispatch_event)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "button-game"}))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["message", "callback_query"]
        if module_key == "button_game" and entry_key == "play"
        else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [rule],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1368,
            "callback_query": {
                "id": "callback-1",
                "data": "pick:3",
                "from": {"id": 111, "first_name": "AAA", "username": "aaa"},
                "message": {
                    "message_id": 13680,
                    "text": "请选择一个选项",
                    "chat": {"id": -100777, "type": "supergroup"},
                },
            },
        },
    )

    assert run_entry.await_count == 1
    dispatch_event.assert_called_once()
    assert dispatch_event.call_args.args[1][0].dispatch_mode == "rule_bound"
    assert run_entry.await_args.kwargs["plugin_key"] == "button_game"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "callback_query"
    assert payload["event"]["type"] == "callback_query"
    assert payload["event"]["text"] == "请选择一个选项"
    assert payload["event"]["callback_query_id"] == "callback-1"
    assert payload["event"]["callback_data"] == "pick:3"
    assert payload["message_text"] == "请选择一个选项"
    assert payload["source"]["callback_data"] == "pick:3"
    assert payload["message"]["text"] == "请选择一个选项"
    assert payload["message"]["message_id"] == 13680
    assert payload["chat"]["id"] == -100777
    assert payload["sender"]["user_id"] == 111
    assert payload["actor"]["user_id"] == 111
    assert payload["source_actor"]["user_id"] == 111
    assert payload["raw"]["callback_query_id"] == "callback-1"
    assert payload["raw"]["callback_data"] == "pick:3"
    assert payload["callback_query_id"] == "callback-1"
    assert payload["callback_data"] == "pick:3"
    assert payload["sender_user_id"] == 111
    assert send.await_args.args[:3] == ("bbot-token", -100777, "已记录按钮选择")
    assert send.await_args.kwargs["reply_markup"] == {
        "inline_keyboard": [[{"text": "继续", "callback_data": "next"}]]
    }
    answer.assert_awaited_once_with("bbot-token", "callback-1", text="", show_alert=False)


@pytest.mark.asyncio
async def test_interaction_callback_without_actions_is_still_acknowledged(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(return_value=(True, None, []))
    answer = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "button-game"}))
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_events", lambda *_args: ["callback_query"])
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "button-game",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "button_game",
                        "module_action": "play",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1369,
            "callback_query": {
                "id": "callback-2",
                "data": "noop",
                "from": {"id": 111, "first_name": "AAA"},
                "message": {"message_id": 13690, "chat": {"id": -100777, "type": "supergroup"}},
            },
        },
    )

    assert run_entry.await_count == 1
    answer.assert_awaited_once_with("bbot-token", "callback-2", text="", show_alert=False)


@pytest.mark.asyncio
async def test_interaction_callback_fast_ack_skips_later_answer_callback(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    events: list[object] = []
    plugin_started = asyncio.Event()
    release_plugin = asyncio.Event()

    async def _run_entry(*_args, **_kwargs):
        events.append("plugin-start")
        plugin_started.set()
        await release_plugin.wait()
        return True, None, [
            {
                "type": "answer_callback",
                "callback_query_id": "callback-fast",
                "text": "稍后弹窗",
                "show_alert": True,
            }
        ]

    async def _answer(_token: str, _callback_id: str, *, text: str = "", show_alert: bool = False):
        events.append(("ack", text, show_alert))

    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False}))
    monkeypatch.setattr(account_bot_runtime, "_incoming_is_userbot_command_text", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", _run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "button-game"}))
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)
    monkeypatch.setattr(account_bot_service, "answer_callback", _answer)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_events", lambda *_args: ["callback_query"])
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_manifest",
        lambda *_args: {"key": "play", "events": ["callback_query"], "callback_fast_ack": True},
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "button-game",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "button_game",
                        "module_action": "play",
                    },
                ],
            }
        ),
    )

    task = asyncio.create_task(
        account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": 1370,
                "callback_query": {
                    "id": "callback-fast",
                    "data": "pick:1",
                    "from": {"id": 111, "first_name": "AAA"},
                    "message": {"message_id": 13700, "chat": {"id": -100777, "type": "supergroup"}},
                },
            },
        )
    )
    await asyncio.wait_for(plugin_started.wait(), timeout=1)

    assert events == [("ack", "", False), "plugin-start"]

    release_plugin.set()
    await task

    assert events == [("ack", "", False), "plugin-start"]
    assert any(
        call.args[2] == account_bot_runtime.TRACE_STATUS_SKIPPED
        and call.kwargs.get("error_code") == "already_acked"
        for call in record_action.await_args_list
    )


@pytest.mark.asyncio
async def test_interaction_callback_without_fast_ack_waits_for_plugin_answer(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    plugin_started = asyncio.Event()
    release_plugin = asyncio.Event()

    async def _run_entry(*_args, **_kwargs):
        plugin_started.set()
        await release_plugin.wait()
        return True, None, [
            {
                "type": "answer_callback",
                "callback_query_id": "callback-normal",
                "text": "已处理",
                "show_alert": True,
            }
        ]

    answer = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": False}))
    monkeypatch.setattr(account_bot_runtime, "_incoming_is_userbot_command_text", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", AsyncMock(return_value=False))
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", _run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "button-game"}))
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_service, "declared_module_entry_events", lambda *_args: ["callback_query"])
    monkeypatch.setattr(account_bot_service, "declared_module_entry_manifest", lambda *_args: {"key": "play", "events": ["callback_query"]})
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "button-game",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "action": "module",
                        "module_key": "button_game",
                        "module_action": "play",
                    },
                ],
            }
        ),
    )

    task = asyncio.create_task(
        account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": 1371,
                "callback_query": {
                    "id": "callback-normal",
                    "data": "pick:1",
                    "from": {"id": 111, "first_name": "AAA"},
                    "message": {"message_id": 13701, "chat": {"id": -100777, "type": "supergroup"}},
                },
            },
        )
    )
    await asyncio.wait_for(plugin_started.wait(), timeout=1)

    answer.assert_not_awaited()

    release_plugin.set()
    await task

    answer.assert_awaited_once_with("bbot-token", "callback-normal", text="已处理", show_alert=True)


@pytest.mark.asyncio
async def test_interaction_plain_message_skips_entries_without_message_event(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "收到答案"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "pt-promote"}))
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["keyword"] if module_key == "pt_promote" and entry_key == "promote_torrent" else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "pt-promote",
                        "enabled": True,
                        "chat_ids": [-100777],
                        "trigger_mode": "keyword",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["置顶 id=数字"],
                        "action": "module",
                        "module_key": "pt_promote",
                        "module_action": "promote_torrent",
                        "module_prize": 456,
                        "concurrency": "user",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 1367,
            "message": {
                "message_id": 13670,
                "text": "继续置顶 12345",
                "from": {"id": 111, "first_name": "AAA", "username": "aaa"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_plain_message_control_action_clears_session(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    rule = {
        "id": "game24-active",
        "enabled": True,
        "chat_ids": [-100777],
        "trigger_mode": "both",
        "trigger_texts": ["转账成功"],
        "action": "module",
        "module_key": "game24",
        "module_action": "start_paid_game",
    }
    redis = _MemoryRedis()
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100777)
    redis.data[session_key] = account_bot_runtime.json.dumps({"rule_id": "game24-active", "started_by_user_id": 111})
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "结束"}, {"type": "end_session"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 13671,
            "message": {
                "message_id": 136710,
                "text": "5*(5-1/5)",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    assert session_key not in redis.data
    assert send.await_args.args[:3] == ("bbot-token", -100777, "结束")


@pytest.mark.asyncio
async def test_chat_scoped_module_session_survives_user_usage_limits(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    rule = account_bot_service.normalize_transfer_notice_config(
        {
            "enabled": True,
            "rules": [
                {
                    "id": "dice-active",
                    "enabled": True,
                    "chat_ids": [-100777],
                    "trigger_mode": "keyword",
                    "module_start_keywords": ["我要猜骰"],
                    "action": "module",
                    "module_key": "dice_grid_hunt",
                    "module_action": "start_dice_grid_hunt",
                    "module_prize": 456,
                    "valid_seconds": 90,
                    "concurrency": "user",
                    "user_cooldown_seconds": "6h",
                    "daily_limit_per_user": 2,
                }
            ],
        }
    )["rules"][0]
    redis = _MemoryRedis()
    run_entry = AsyncMock(
        side_effect=[
            (
                True,
                None,
                [
                    {
                        "type": "send_photo",
                        "photo_base64": base64.b64encode(b"png").decode("ascii"),
                        "filename": "dice_grid_hunt.png",
                        "caption": "开局",
                    }
                ],
            ),
            (True, None, [{"type": "send_message", "text": "答对"}, {"type": "end_session"}]),
        ]
    )
    send_photo = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_photo_bytes", send_photo)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 13673,
            "message": {
                "message_id": 136730,
                "text": "我要猜骰",
                "from": {"id": 111, "first_name": "Starter"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100777)

    assert rule["concurrency"] == "user"
    assert rule["module_session_scope"] == "chat"
    assert session_key in redis.data
    assert not any(key.startswith("account_bot:interaction_session:") and ":user:" in key for key in redis.data)

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 13674,
            "message": {
                "message_id": 136740,
                "text": "3",
                "from": {"id": 222, "first_name": "Player"},
                "chat": {"id": -100777, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 2
    assert run_entry.await_args_list[1].kwargs["payload"]["event_type"] == "message"
    assert run_entry.await_args_list[1].kwargs["payload"]["sender_user_id"] == 222
    assert session_key not in redis.data
    assert send.await_args.args[:3] == ("bbot-token", -100777, "答对")


@pytest.mark.asyncio
async def test_interaction_close_command_clears_user_scoped_sessions(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=999, tg_username="owner", display_name="Owner")
            return None

    rule = {
        "id": "user-game24",
        "name": "用户局",
        "enabled": True,
        "chat_ids": [-100778],
        "close_commands": ["关闭用户局"],
        "action": "module",
        "module_key": "game24",
        "module_action": "start_paid_game",
        "concurrency": "user",
    }
    other_rule = {
        "id": "other-game",
        "name": "其他局",
        "enabled": True,
        "chat_ids": [-100778],
        "close_commands": ["关闭其他局"],
        "action": "module",
        "module_key": "dice_grid_hunt",
        "module_action": "answer_dice_grid_hunt",
        "concurrency": "user",
    }
    redis = _MemoryRedis()
    key_111 = account_bot_runtime._interaction_session_key(1, rule, -100778, 111)
    key_222 = account_bot_runtime._interaction_session_key(1, rule, -100778, 222)
    other_key = account_bot_runtime._interaction_session_key(1, other_rule, -100778, 333)
    redis.data[key_111] = "session-111"
    redis.data[key_222] = "session-222"
    redis.data[other_key] = "session-333"
    send = AsyncMock()
    run_entry = AsyncMock(return_value=(True, None, []))
    trace_counter = {"value": 0}

    async def _start_trace(payload):  # noqa: ANN001
        trace_counter["value"] += 1
        return SimpleNamespace(
            trace_id=f"evt_{payload.get('event_type', 'message')}_{trace_counter['value']}",
            account_id=payload.get("account_id"),
            event_type=payload.get("event_type"),
        )

    start_trace = AsyncMock(side_effect=_start_trace)
    record_span = AsyncMock()
    record_action = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": True}))
    monkeypatch.setattr(account_bot_runtime, "start_trace", start_trace)
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)
    monkeypatch.setattr(account_bot_runtime, "finish_trace", finish_trace)
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule, other_rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 13672,
            "message": {
                "message_id": 136720,
                "text": "关闭用户局",
                "from": {"id": 999, "first_name": "Owner"},
                "chat": {"id": -100778, "type": "supergroup"},
            },
        },
    )

    assert key_111 not in redis.data
    assert key_222 not in redis.data
    assert other_key in redis.data
    assert run_entry.await_count == 2
    assert all(call.kwargs["payload"]["event_type"] == "session_close" for call in run_entry.await_args_list)
    assert all(str(call.kwargs["payload"].get("trace_id", "")).startswith("evt_session_close") for call in run_entry.await_args_list)
    session_close_payloads = [
        call.args[0]
        for call in start_trace.await_args_list
        if isinstance(call.args[0], dict) and call.args[0].get("event_type") == "session_close"
    ]
    assert len(session_close_payloads) == 2
    assert any(
        call.args[1] == "subscription_match"
        and call.kwargs.get("reason_code") == "matched"
        and call.kwargs.get("dispatch_mode") == "rule_bound"
        for call in record_span.await_args_list
    )
    assert any(
        getattr(call.args[0], "event_type", None) == "session_close" and call.args[1] == account_bot_runtime.TRACE_STATUS_OK
        for call in finish_trace.await_args_list
    )
    assert "已结束 2 个进行中的游戏。" in send.await_args.args[2]
    close_confirm_actions = [
        call
        for call in record_action.await_args_list
        if call.args[1].get("type") == "send_message" and "已结束 2 个进行中的游戏。" in call.args[1].get("text", "")
    ]
    assert close_confirm_actions
    assert close_confirm_actions[-1].args[0]["trace_id"] == "evt_message_1"


@pytest.mark.asyncio
async def test_close_interaction_rules_skips_session_close_for_entries_without_declared_event(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    rule = {
        "id": "pt-promote",
        "enabled": True,
        "chat_ids": [-100778],
        "trigger_mode": "keyword",
        "trigger_texts": ["转账成功"],
        "action": "module",
        "module_key": "pt_promote",
        "module_action": "promote_torrent",
        "close_commands": ["关闭置顶"],
        "concurrency": "user",
    }
    redis = _MemoryRedis()
    session_key = account_bot_runtime._interaction_session_key(1, rule, -100778, 111)
    redis.data[session_key] = account_bot_runtime.json.dumps({"rule_id": "pt-promote", "started_by_user_id": 111})
    send = AsyncMock()
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "结束"}, {"type": "end_session"}]))
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_is_account_user_sender", AsyncMock(return_value=True))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["keyword", "message"] if module_key == "pt_promote" and entry_key == "promote_torrent" else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 13673,
            "message": {
                "message_id": 136730,
                "text": "关闭置顶",
                "from": {"id": 999, "first_name": "Owner"},
                "chat": {"id": -100778, "type": "supergroup"},
            },
        },
    )

    assert session_key not in redis.data
    run_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_control_and_start_keywords_do_not_route_as_plain_messages(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_user_id=111, tg_username="aaa", display_name="AAA")
            return None

    run_entry = AsyncMock()
    run_module = AsyncMock(return_value=(True, True))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_run_interaction_module", run_module)
    monkeypatch.setattr(account_bot_runtime, "_close_active_interaction_games", AsyncMock(return_value=0))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "dice-active",
                        "name": "猜骰",
                        "enabled": True,
                        "chat_ids": [-100778],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["开始猜骰"],
                        "open_commands": ["开启猜骰"],
                        "close_commands": ["关闭猜骰"],
                        "status_commands": ["猜骰状态"],
                        "action": "module",
                        "module_key": "dice_grid_hunt",
                        "module_action": "answer_dice_grid_hunt",
                    },
                ],
            }
        ),
    )

    for update_id, text in [
        (1367, "开启猜骰"),
        (1368, "猜骰状态"),
        (1369, "关闭猜骰"),
        (1370, "开始猜骰"),
    ]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id * 10,
                    "text": text,
                    "from": {"id": 111, "first_name": "AAA"},
                    "chat": {"id": -100778, "type": "supergroup"},
                },
            },
        )

    assert run_entry.await_count == 0
    assert run_module.await_count == 1
    assert run_module.await_args.args[0].text == "开始猜骰"


@pytest.mark.asyncio
async def test_interaction_bot_skips_prefixed_userbot_command_messages(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, key=None):  # noqa: ANN002
            if getattr(model, "__name__", "") == "SystemSetting" and key == "command_prefix":
                return SimpleNamespace(value={"value": "。"})
            return None

    event_bus = AsyncMock(return_value=(False, True))
    rule_or_keyword = AsyncMock(return_value=False)
    module_message = AsyncMock(return_value=False)
    transfer_notice = AsyncMock(return_value=False)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_notice", transfer_notice)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_event_bus_subscriptions", event_bus)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_rule_command_or_keyword", rule_or_keyword)
    monkeypatch.setattr(account_bot_runtime, "_try_handle_interaction_module_message", module_message)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "interaction_bot_id": 8875144459,
                "rules": [
                    {
                        "id": "ten-half",
                        "enabled": True,
                        "chat_ids": [-100778],
                        "trigger_mode": "both",
                        "module_start_keywords": ["十点半测试"],
                        "action": "module",
                        "module_key": "ten_half",
                        "module_action": "start_ten_half",
                    }
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 15001,
            "message": {
                "message_id": 150010,
                "text": "。10d 100",
                "from": {"id": 1682400007, "first_name": "Owner"},
                "chat": {"id": -100778, "type": "supergroup"},
            },
        },
    )

    transfer_notice.assert_not_awaited()
    event_bus.assert_not_awaited()
    rule_or_keyword.assert_not_awaited()
    module_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_test_update_skips_prefixed_userbot_command_messages(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, key=None):  # noqa: ANN002
            if getattr(model, "__name__", "") == "SystemSetting" and key == "command_prefix":
                return SimpleNamespace(value={"value": "。"})
            return None

    transfer_command = AsyncMock(return_value=True)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "_try_handle_transfer_command", transfer_command)

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 15002,
            "message": {
                "message_id": 150020,
                "text": "。10d 6789",
                "from": {"id": 1682400007, "first_name": "Owner"},
                "chat": {"id": -100778, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 150019,
                    "from": {"id": 111, "first_name": "玩家A"},
                    "text": "入局",
                },
            },
        },
    )

    transfer_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_module_start_keyword_is_blocked_but_answer_message_routes(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "答案已记录"}],
        )
    )
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "paid-dice"}))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "paid-dice",
                        "name": "付费猜骰",
                        "enabled": True,
                        "chat_ids": [-100779],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "module_start_keywords": ["开始付费猜骰"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "dice_grid_hunt",
                        "module_action": "answer_dice_grid_hunt",
                        "module_prize": 789,
                    },
                ],
            }
        ),
    )

    for update_id, text in [
        (1371, "开始付费猜骰"),
        (1372, "42"),
    ]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id * 10,
                    "text": text,
                    "from": {"id": 111, "first_name": "AAA"},
                    "chat": {"id": -100779, "type": "supergroup"},
                },
            },
        )

    sent_texts = [call.args[2] for call in send.await_args_list]
    assert any("该付费猜骰是付费娱乐模块，请对收款人：BBB的任意消息回复+100即可参与。" == text for text in sent_texts)
    assert run_entry.await_count == 1
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "message"
    assert payload["event"]["type"] == "message"
    assert payload["message_text"] == "42"
    assert payload["prize"] == 789
    assert sent_texts[-1] == "答案已记录"


@pytest.mark.asyncio
async def test_transfer_notice_module_rule_starts_game24_with_interaction_bot(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "24 点开始\n奖金：888"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "game24-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_paid_game",
                        "module_prize": 888,
                        "response_template": "忽略",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    assert run_entry.await_args.kwargs["plugin_key"] == "game24"
    assert run_entry.await_args.kwargs["entry_key"] == "start_paid_game"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "payment_confirmed"
    assert payload["amount"] == 100
    assert payload["prize"] == 888
    assert send.await_count == 1
    assert send.await_args.args[:3] == ("bbot-token", -100123, "24 点开始\n奖金：888")


@pytest.mark.asyncio
async def test_disabled_active_paid_pool_session_payment_bypasses_rule_state(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="你心里已经有答案了", tg_user_id=999)
            return None

    redis = _MemoryRedis()
    rule = {
        "id": "ten-half-paid",
        "enabled": False,
        "chat_ids": [-100123],
        "trigger_mode": "keyword",
        "trigger_texts": ["转账成功"],
        "module_start_keywords": ["10d"],
        "receiver_text": "你心里已经有答案了",
        "amount": 1000,
        "amount_match_mode": "eq",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "module_config": {"bet": 1000, "max_players": 5},
    }
    await redis.set(
        account_bot_runtime._interaction_session_key(1, rule, -100123, None),
        json.dumps(
            {
                "account_id": 1,
                "chat_id": -100123,
                "rule_id": "ten-half-paid",
                "module_key": "ten_half",
                "entry_key": "start_ten_half",
                "started_by_user_id": 111,
                "event_type": "keyword",
            },
            ensure_ascii=False,
        ),
    )
    await redis.set(account_bot_runtime._rule_state_key(1, rule, -100123), "closed")
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "加入成功"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["payment_confirmed", "keyword", "message", "callback_query"]
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [rule],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 8,
            "message": {
                "message_id": 80,
                "text": "转账成功\n你心里没点数？ 射出 1000\n你心里已经有答案了 接收 1000",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["plugin_key"] == "ten_half"
    assert run_entry.await_args.kwargs["entry_key"] == "start_ten_half"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "payment_confirmed"
    assert payload["payment"]["amount"] == 1000
    assert payload["module_config"]["bet"] == 1000
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_active_paid_pool_session_delivers_payment_for_plugin_amount_filter(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="你心里已经有答案了", tg_user_id=999)
            return None

    redis = _MemoryRedis()
    rule = {
        "id": "ten-half-paid",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "both",
        "trigger_texts": ["转账成功"],
        "module_start_keywords": ["10d"],
        "receiver_text": "你心里已经有答案了",
        "amount": 1000,
        "amount_match_mode": "eq",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "module_config": {"bet": 1000, "max_players": 5},
    }
    await redis.set(
        account_bot_runtime._interaction_session_key(1, rule, -100123, None),
        json.dumps(
            {
                "account_id": 1,
                "chat_id": -100123,
                "rule_id": "ten-half-paid",
                "module_key": "ten_half",
                "entry_key": "start_ten_half",
                "started_by_user_id": 111,
                "event_type": "keyword",
            },
            ensure_ascii=False,
        ),
    )
    run_entry = AsyncMock(return_value=(True, None, [{"type": "no_session"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["payment_confirmed", "keyword", "message", "callback_query"]
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [rule],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 81,
            "message": {
                "message_id": 810,
                "text": "转账成功\n你心里没点数？ 射出 100\n你心里已经有答案了 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["plugin_key"] == "ten_half"
    assert run_entry.await_args.kwargs["entry_key"] == "start_ten_half"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "payment_confirmed"
    assert payload["payment"]["amount"] == 100
    assert payload["module_config"]["bet"] == 1000
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_parsed_transfer_notice_without_trigger_text_matches_active_paid_pool_session(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="你心里已经有答案了", tg_user_id=999)
            return None

    redis = _MemoryRedis()
    rule = {
        "id": "ten-half-paid",
        "enabled": True,
        "chat_ids": [-100123],
        "trigger_mode": "both",
        "trigger_texts": ["转账成功"],
        "module_start_keywords": ["10d"],
        "receiver_text": "你心里已经有答案了",
        "amount": 1000,
        "amount_match_mode": "eq",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "module_config": {"bet": 1000, "max_players": 5},
    }
    await redis.set(
        account_bot_runtime._interaction_session_key(1, rule, -100123, None),
        json.dumps(
            {
                "account_id": 1,
                "chat_id": -100123,
                "rule_id": "ten-half-paid",
                "module_key": "ten_half",
                "entry_key": "start_ten_half",
                "started_by_user_id": 111,
                "event_type": "command",
            },
            ensure_ascii=False,
        ),
    )
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "加入成功"}]))
    send = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_entry_events",
        lambda module_key, entry_key: ["payment_confirmed", "keyword", "message", "callback_query"]
        if (module_key, entry_key) == ("ten_half", "start_ten_half")
        else [],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "transfer_bot_id": 456,
                "rules": [rule],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 9,
            "message": {
                "message_id": 90,
                "text": "你心里没点数？ 射出 1000 蝌蚪\n你心里已经有答案了 接收 1000 蝌蚪",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["plugin_key"] == "ten_half"
    assert run_entry.await_args.kwargs["entry_key"] == "start_ten_half"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "payment_confirmed"
    assert payload["payment"]["amount"] == 1000
    assert payload["module_config"]["bet"] == 1000
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_transfer_notice_prefers_event_bus_payment_subscription(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="payment_plugin")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

        async def commit(self):
            return None

    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [
                {"type": "start_session", "chat_id": -100123, "entry_key": "on_payment"},
                {"type": "send_message", "text": "payment ok"},
            ],
        )
    )
    legacy_execute = AsyncMock(return_value=True)
    apply_start_session = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_execute_interaction_rule", legacy_execute)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_start_session_actions", apply_start_session)
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["external_payment_notice"],
                "events": ["payment_confirmed"],
                "scope": "all_allowed_chats",
                "entry_key": "on_payment",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "legacy-paid",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "payment_plugin",
                        "module_action": "on_payment",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["plugin_key"] == "payment_plugin"
    assert run_entry.await_args.kwargs["entry_key"] == "on_payment"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["source"]["channel"] == "external_payment_notice"
    assert payload["event_type"] == "payment_confirmed"
    assert payload["payment"]["amount"] == 100
    assert payload["source_actor"]["type"] == "external_bot"
    apply_start_session.assert_awaited_once()
    assert apply_start_session.await_args.args[2][0]["type"] == "start_session"
    legacy_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_notice_event_bus_payment_uses_matching_rule_only(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="math10"), SimpleNamespace(feature_key="ten_half")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

        async def commit(self):
            return None

    redis = _MemoryRedis()
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "math ok"}]))
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda key: [
            {
                "source": ["external_payment_notice"],
                "events": ["payment_confirmed"],
                "scope": "all_allowed_chats",
                "entry_key": "start_math_game" if key == "math10" else "start_ten_half",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "silly-math",
                        "name": "傻瓜玩法",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "Owner",
                        "amount": 234,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "math10",
                        "module_action": "start_math_game",
                        "module_session_scope": "chat",
                        "module_prize": 666,
                    },
                    {
                        "id": "ten-half",
                        "name": "十点半",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "both",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "Owner",
                        "amount": 1000,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "ten_half",
                        "module_action": "start_ten_half",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "text": "转账成功\nAAA 射出 234\nOwner 接收 234",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["plugin_key"] == "math10"
    assert run_entry.await_args.kwargs["entry_key"] == "start_math_game"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["payment"]["amount"] == 234
    assert payload["prize"] == 666
    assert payload["session"]["key"] == "account_bot:interaction_session:1:silly-math:-100123"
    assert payload["session"]["scope"] == "chat"
    assert payload["rule_id"] == "silly-math"
    saved = await redis.get("account_bot:interaction_session:1:silly-math:-100123")
    assert saved is not None
    session = json.loads(saved)
    assert session["module_key"] == "math10"
    assert session["entry_key"] == "start_math_game"


@pytest.mark.asyncio
async def test_transfer_notice_event_bus_payment_counts_payer_daily_limit(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="math10")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

        async def commit(self):
            return None

    redis = _MemoryRedis()
    send = AsyncMock()
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "math ok"}]))
    legacy_execute = AsyncMock(return_value=True)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_execute_interaction_rule", legacy_execute)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["external_payment_notice"],
                "events": ["payment_confirmed"],
                "scope": "all_allowed_chats",
                "entry_key": "start_math_game",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "silly-math",
                        "name": "傻瓜玩法",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "Owner",
                        "amount": 234,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "math10",
                        "module_action": "start_math_game",
                        "daily_limit_per_user": 1,
                    },
                ],
            }
        ),
    )

    for update_id, message_id in [(17, 170), (18, 180)]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "text": "转账成功\n付款人：AAA\n付款人ID：111\n收款人：Owner\n金额：234",
                    "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert run_entry.await_count == 1
    legacy_execute.assert_not_awaited()
    assert send.await_args.args[:3] == (
        "bbot-token",
        -100123,
        "AAA 今日已成功傻瓜玩法 1/1 次，当日无法再次使用。",
    )
    daily_values = [
        value
        for key, value in redis.data.items()
        if key.startswith("account_bot:interaction_user_daily:")
    ]
    assert daily_values == ["1"]
    assert not any(key.startswith("account_bot:interaction_user_pending:") for key in redis.data)


@pytest.mark.asyncio
async def test_transfer_notice_event_bus_payment_failure_does_not_count_payer_limit(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="math10")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

        async def commit(self):
            return None

    redis = _MemoryRedis()
    send = AsyncMock()
    run_entry = AsyncMock(
        side_effect=[
            (False, "boom", []),
            (True, None, [{"type": "send_message", "text": "math ok"}]),
        ]
    )
    legacy_execute = AsyncMock(return_value=True)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_execute_interaction_rule", legacy_execute)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["external_payment_notice"],
                "events": ["payment_confirmed"],
                "scope": "all_allowed_chats",
                "entry_key": "start_math_game",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "silly-math",
                        "name": "傻瓜玩法",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "Owner",
                        "amount": 234,
                        "amount_match_mode": "eq",
                        "action": "module",
                        "module_key": "math10",
                        "module_action": "start_math_game",
                        "daily_limit_per_user": 1,
                    },
                ],
            }
        ),
    )

    for update_id, message_id in [(27, 270), (28, 280)]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "text": "转账成功\n付款人：AAA\n付款人ID：111\n收款人：Owner\n金额：234",
                    "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert run_entry.await_count == 2
    legacy_execute.assert_not_awaited()
    send.assert_not_awaited()
    daily_values = [
        value
        for key, value in redis.data.items()
        if key.startswith("account_bot:interaction_user_daily:")
    ]
    assert daily_values == ["1"]
    assert not any(key.startswith("account_bot:interaction_user_pending:") for key in redis.data)


@pytest.mark.asyncio
async def test_transfer_notice_respects_event_bus_delivery_switch(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="payment_plugin")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

        async def commit(self):
            return None

    trace = SimpleNamespace(trace_id="evt_payment_disabled")
    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "payment ok"}]))
    legacy_execute = AsyncMock(return_value=True)
    record_span = AsyncMock()
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": True, "event_bus_delivery_enabled": False, "inline_updates_enabled": True}),
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_execute_interaction_rule", legacy_execute)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["external_payment_notice"],
                "events": ["payment_confirmed"],
                "scope": "all_allowed_chats",
                "entry_key": "on_payment",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "legacy-paid",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "legacy_game",
                        "module_action": "start_paid_game",
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_not_awaited()
    legacy_execute.assert_awaited_once()
    assert any(
        call.kwargs.get("component") == "event_bus_payment_notice"
        and call.kwargs.get("reason_code") == "event_bus_delivery_disabled"
        for call in record_span.await_args_list
    )


@pytest.mark.asyncio
async def test_transfer_notice_event_bus_payment_subscription_without_matching_rule_is_consumed(monkeypatch) -> None:
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="payment_plugin")]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

        async def commit(self):
            return None

    run_entry = AsyncMock(return_value=(True, None, [{"type": "send_message", "text": "payment ok"}]))
    legacy_execute = AsyncMock(return_value=True)
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_execute_interaction_rule", legacy_execute)
    monkeypatch.setattr(account_bot_runtime, "_guard_interaction_actions", AsyncMock(side_effect=lambda _incoming, _rule, actions: actions))
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_service, "plugin_declares_telegram_native_raw", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["external_payment_notice"],
                "events": ["payment_confirmed"],
                "scope": "all_allowed_chats",
                "entry_key": "on_payment",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "chat_ids": [-100123],
                "rules": [],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    run_entry.assert_not_awaited()
    legacy_execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_notice_module_rule_counts_payer_daily_limit(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    redis = _MemoryRedis()
    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "置顶成功"}, {"type": "no_session"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "pt-promote",
                        "name": "置顶促销",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_mode": "payment",
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "pt_promote",
                        "module_action": "promote_torrent",
                        "concurrency": "user",
                        "daily_limit_per_user": 1,
                    },
                ],
            }
        ),
    )

    for update_id, message_id in [(71, 710), (72, 720)]:
        await account_bot_runtime._handle_interaction_update(
            1,
            "bbot-token",
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "text": "转账成功\n付款人：AAA\n付款人ID：111\n收款人：BBB\n金额：100",
                    "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert run_entry.await_count == 1
    assert send.await_args.args[:3] == (
        "bbot-token",
        -100123,
        "AAA 今日已成功置顶促销 1/1 次，当日无法再次使用。",
    )


@pytest.mark.asyncio
async def test_transfer_notice_module_rule_invokes_remote_interaction_entry(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "远程猜数字已开始，奖金 777"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "guess-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "guess_number",
                        "module_action": "start_game",
                        "module_prize": 777,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 71,
            "message": {
                "message_id": 710,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    assert run_entry.await_args.kwargs["plugin_key"] == "guess_number"
    assert run_entry.await_args.kwargs["entry_key"] == "start_game"
    assert run_entry.await_args.kwargs["payload"]["amount"] == 100
    assert run_entry.await_args.kwargs["payload"]["prize"] == 777
    assert send.await_args.args[:3] == (
        "bbot-token",
        -100123,
        "远程猜数字已开始，奖金 777",
    )


@pytest.mark.asyncio
async def test_transfer_notice_module_rule_accepts_guess_number_legacy_entry(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, *_args):  # noqa: ANN002
            return None

    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [{"type": "send_message", "text": "旧入口猜数字已开始，奖金 666"}],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "trusted_bot_id": 456,
                "rules": [
                    {
                        "id": "guess-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "receiver_text": "BBB",
                        "amount": 100,
                        "action": "module",
                        "module_key": "guess_number",
                        "module_action": "start_game",
                        "module_prize": 666,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 71,
            "message": {
                "message_id": 710,
                "text": "转账成功\nAAA 射出 100\nBBB 接收 100",
                "from": {"id": 456, "is_bot": True, "first_name": "Abot"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    assert run_entry.await_args.kwargs["plugin_key"] == "guess_number"
    assert run_entry.await_args.kwargs["entry_key"] == "start_game"
    assert run_entry.await_args.kwargs["payload"]["prize"] == 666


@pytest.mark.asyncio
async def test_interaction_action_can_send_photo(monkeypatch) -> None:
    send = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_photo_bytes", send)
    monkeypatch.setattr(account_bot_service, "delete_message", delete)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [
            {
                "type": "send_photo",
                "photo_base64": base64.b64encode(b"png-bytes").decode("ascii"),
                "filename": "grid.png",
                "caption": "九宫格",
                "reply_to_message_id": 9,
            }
        ],
        replace_message_id=77,
    )

    assert send.await_count == 1
    assert send.await_args.args[:3] == ("bbot-token", -100123, b"png-bytes")
    assert send.await_args.kwargs == {
        "filename": "grid.png",
        "caption": "九宫格",
        "reply_to_message_id": 9,
        "reply_markup": None,
    }
    delete.assert_awaited_once_with("bbot-token", -100123, 77)


@pytest.mark.asyncio
async def test_interaction_action_send_message_passes_reply_markup(monkeypatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr(account_bot_service, "send_message", send)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )
    reply_markup = {"inline_keyboard": [[{"text": "选 A", "callback_data": "pick:a"}]]}

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [{"type": "send_message", "text": "请选择", "reply_markup": reply_markup}],
    )

    assert send.await_args.args[:3] == ("bbot-token", -100123, "请选择")
    assert send.await_args.kwargs["reply_markup"] == reply_markup


@pytest.mark.asyncio
async def test_interaction_action_edit_message_passes_reply_markup(monkeypatch) -> None:
    edit = AsyncMock()
    monkeypatch.setattr(account_bot_service, "edit_message", edit)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )
    reply_markup = {"inline_keyboard": [[{"text": "选 B", "callback_data": "pick:b"}]]}

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [{"type": "send_message", "text": "请选择", "reply_markup": reply_markup}],
        replace_message_id=77,
    )

    assert edit.await_args.args[:4] == ("bbot-token", -100123, 77, "请选择")
    assert edit.await_args.kwargs["reply_markup"] == reply_markup


@pytest.mark.asyncio
async def test_interaction_action_edit_message_records_dedicated_action(monkeypatch) -> None:
    edit = AsyncMock(return_value={"message_id": 77})
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "edit_message", edit)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
        trace_id="evt_edit",
    )

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [{"type": "edit_message", "text": "已更新", "message_id": 77}],
    )

    edit.assert_awaited_once_with("bbot-token", -100123, 77, "已更新", reply_markup=None)
    assert record_action.await_args.args[1]["type"] == "edit_message"
    assert record_action.await_args.args[2] == account_bot_runtime.TRACE_STATUS_OK


@pytest.mark.asyncio
async def test_interaction_action_edit_placeholder_failure_records_failed_action(monkeypatch) -> None:
    edit = AsyncMock(side_effect=RuntimeError("edit denied"))
    send = AsyncMock(return_value={"message_id": 88})
    delete = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(account_bot_service, "edit_message", edit)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "delete_message", delete)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
        trace_id="evt_edit_fallback",
    )

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [{"type": "send_message", "text": "新消息", "context": {"trace_id": "evt_edit_fallback"}}],
        replace_message_id=77,
    )

    edit.assert_awaited_once_with("bbot-token", -100123, 77, "新消息", reply_markup=None)
    send.assert_awaited_once_with(
        "bbot-token",
        -100123,
        "新消息",
        reply_to_message_id=None,
        reply_markup=None,
    )
    delete.assert_awaited_once_with("bbot-token", -100123, 77)
    action_types = [call.args[1]["type"] for call in record_action.await_args_list]
    statuses = [call.args[2] for call in record_action.await_args_list]
    assert action_types == ["edit_message", "delete_message", "send_message"]
    assert statuses == [
        account_bot_runtime.TRACE_STATUS_FAILED,
        account_bot_runtime.TRACE_STATUS_OK,
        account_bot_runtime.TRACE_STATUS_OK,
    ]
    assert record_action.await_args_list[0].kwargs["error_code"] == "telegram_api_error"


@pytest.mark.asyncio
async def test_interaction_action_save_message_id_key_is_validated(monkeypatch) -> None:
    redis = _MemoryRedis()
    send = AsyncMock(return_value={"message_id": 88})
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [
            {
                "type": "send_message",
                "text": "大厅",
                "save_message_id_key": "dead_revolver:msg:1:-100123",
            },
            {
                "type": "send_message",
                "text": "非法 key",
                "save_message_id_key": "bad key\nother",
            },
        ],
    )

    assert redis.data == {"tp:msgid:1:dead_revolver:msg:1:-100123": "88"}


@pytest.mark.asyncio
async def test_interaction_action_bbot_notice_no_longer_sends_or_uses_transfer_bot(monkeypatch) -> None:
    send = AsyncMock()
    write_log = AsyncMock()
    record_action = AsyncMock()
    get_transfer_bot_token = AsyncMock(return_value="abot-token")
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", get_transfer_bot_token)
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", write_log)
    monkeypatch.setattr("app.services.interaction.delivery.record_action", record_action)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
        trace_id="evt_direct_notice",
    )

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [
            {
                "type": "send_message",
                "text": "结算公告",
                "send_via": "bbot_notice",
                "reply_to_message_id": 9,
                "settlement": {
                    "mode": "manual",
                    "amount": 123,
                    "winner_user_id": 111,
                    "status": "announced",
                },
            }
        ],
        replace_message_id=77,
    )

    send.assert_not_awaited()
    get_transfer_bot_token.assert_not_awaited()
    assert write_log.await_count == 2
    assert write_log.await_args_list[0].args[1:3] == ("info", "interaction settlement reported")
    assert write_log.await_args_list[0].kwargs["settlement"]["amount"] == 123
    assert write_log.await_args_list[1].kwargs["reason_code"] == "send_channel_deprecated"
    assert record_action.await_args.kwargs["error_code"] == "send_channel_deprecated"


@pytest.mark.asyncio
async def test_interaction_action_send_via_userbot_reply_uses_worker_rpc_and_logs_settlement(monkeypatch) -> None:
    run_action = AsyncMock(return_value=(True, None, {"message_id": 91}))
    write_log = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_action", run_action)
    monkeypatch.setattr(account_bot_runtime, "_write_interaction_runtime_log", write_log)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )

    await account_bot_runtime._apply_interaction_actions(
        incoming,
        [
            {
                "type": "send_message",
                "text": "账号本人回复",
                "send_via": "userbot_reply",
                "reply_to_message_id": 9,
                "settlement": {
                    "mode": "manual",
                    "amount": 123,
                    "winner_user_id": 111,
                    "winner_name": "AAA",
                    "status": "announced",
                },
            }
        ],
    )

    assert run_action.await_count == 1
    payload = run_action.await_args.kwargs["payload"]
    assert payload["action_type"] == "send_message"
    assert payload["chat_id"] == -100123
    assert payload["reply_to_message_id"] == 9
    assert write_log.await_count == 1
    assert write_log.await_args_list[0].args[1:3] == ("info", "interaction settlement reported")


@pytest.mark.asyncio
async def test_interaction_action_unknown_type_writes_runtime_log(monkeypatch) -> None:
    class _Redis:
        def __init__(self) -> None:
            self.items: list[str | bytes] = []

        async def rpush(self, key: str, value: str | bytes):
            self.items.append(value)
            return len(self.items)

    redis = _Redis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )

    await account_bot_runtime._apply_interaction_actions(incoming, [{"type": "wait_answer"}])

    assert len(redis.items) == 1
    payload = redis.items[0].decode("utf-8") if isinstance(redis.items[0], bytes) else redis.items[0]
    assert "runtime_log_stream" not in payload
    assert "unsupported type=wait_answer" in payload
    assert '"level":"info"' in payload


@pytest.mark.asyncio
async def test_run_worker_interaction_entry_returns_timeout(monkeypatch) -> None:
    class _PubSub:
        def __init__(self) -> None:
            self.subscribed: list[str] = []
            self.unsubscribed: list[str] = []
            self.closed = False

        async def subscribe(self, channel: str) -> None:
            self.subscribed.append(channel)

        async def get_message(self, **_kwargs):  # noqa: ANN003
            return None

        async def unsubscribe(self, channel: str) -> None:
            self.unsubscribed.append(channel)

        async def close(self) -> None:
            self.closed = True

    class _Redis:
        def __init__(self) -> None:
            self.pubsub_obj = _PubSub()
            self.published: list[tuple[str, str]] = []

        def pubsub(self):
            return self.pubsub_obj

        async def publish(self, channel: str, payload: str) -> int:
            self.published.append((channel, payload))
            return 1

    redis = _Redis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_INTERACTION_ENTRY_TIMEOUT_SECONDS", 0.01)

    async def update_status(**_kwargs):
        return None

    monkeypatch.setattr(account_bot_runtime, "update_plugin_runtime_status", update_status)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )

    ok, error, actions = await account_bot_runtime._run_worker_interaction_entry(
        incoming,
        plugin_key="game24",
        entry_key="start_paid_game",
        payload={"chat_id": -100123},
    )

    assert ok is False
    assert error == "worker 调用超时"
    assert actions == []
    assert redis.published
    assert redis.pubsub_obj.unsubscribed == redis.pubsub_obj.subscribed
    assert redis.pubsub_obj.closed is True


@pytest.mark.asyncio
async def test_run_worker_interaction_entry_retries_until_worker_subscribes(monkeypatch) -> None:
    class _PubSub:
        def __init__(self) -> None:
            self.closed = False

        async def subscribe(self, _channel: str) -> None:
            return None

        async def get_message(self, **_kwargs):  # noqa: ANN003
            return {
                "data": account_bot_runtime.make_cmd(
                    account_bot_runtime.CMD_RUN_INTERACTION_ENTRY,
                    ok=True,
                    error=None,
                    actions=[{"type": "result", "success": True}],
                )
            }

        async def unsubscribe(self, _channel: str) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    class _Redis:
        def __init__(self) -> None:
            self.pubsub_obj = _PubSub()
            self.publish_calls = 0

        def pubsub(self):
            return self.pubsub_obj

        async def publish(self, _channel: str, _payload: str) -> int:
            self.publish_calls += 1
            return 0 if self.publish_calls == 1 else 1

    async def fast_sleep(_seconds: float) -> None:
        return None

    redis = _Redis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(account_bot_runtime, "update_plugin_runtime_status", AsyncMock())
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
        trace_id="evt_worker_recovered",
    )

    ok, error, actions = await account_bot_runtime._run_worker_interaction_entry(
        incoming,
        plugin_key="game24",
        entry_key="start_paid_game",
        payload={"trace_id": "evt_worker_recovered", "chat_id": -100123},
    )

    assert ok is True
    assert error is None
    assert actions == [{"type": "result", "success": True}]
    assert redis.publish_calls == 2
    assert redis.pubsub_obj.closed is True


@pytest.mark.asyncio
async def test_run_worker_interaction_entry_retries_then_fails_when_worker_offline(monkeypatch) -> None:
    class _PubSub:
        def __init__(self) -> None:
            self.unsubscribed: list[str] = []
            self.closed = False
            self.get_message_called = False

        async def subscribe(self, _channel: str) -> None:
            return None

        async def get_message(self, **_kwargs):  # noqa: ANN003
            self.get_message_called = True
            return None

        async def unsubscribe(self, channel: str) -> None:
            self.unsubscribed.append(channel)

        async def close(self) -> None:
            self.closed = True

    class _Redis:
        def __init__(self) -> None:
            self.pubsub_obj = _PubSub()
            self.publish_calls = 0

        def pubsub(self):
            return self.pubsub_obj

        async def publish(self, _channel: str, _payload: str) -> int:
            self.publish_calls += 1
            return 0

    redis = _Redis()
    record_span = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "_INTERACTION_ENTRY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(account_bot_runtime, "_INTERACTION_WORKER_ONLINE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime, "update_plugin_runtime_status", AsyncMock())
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
        trace_id="evt_worker_offline",
    )

    ok, error, actions = await account_bot_runtime._run_worker_interaction_entry(
        incoming,
        plugin_key="game24",
        entry_key="start_paid_game",
        payload={"trace_id": "evt_worker_offline", "chat_id": -100123},
    )

    assert ok is False
    assert error == "账号 worker 不在线"
    assert actions == []
    assert redis.publish_calls >= 2
    assert redis.pubsub_obj.get_message_called is False
    assert redis.pubsub_obj.closed is True
    assert any(call.kwargs.get("reason_code") == "userbot_offline" for call in record_span.await_args_list)


@pytest.mark.asyncio
async def test_run_worker_interaction_entry_waits_for_slow_plugin_reply(monkeypatch) -> None:
    class _PubSub:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        async def subscribe(self, _channel: str) -> None:
            return None

        async def get_message(self, **_kwargs):  # noqa: ANN003
            self.calls += 1
            if self.calls == 1:
                return None
            return {
                "data": account_bot_runtime.make_cmd(
                    account_bot_runtime.CMD_RUN_INTERACTION_ENTRY,
                    ok=True,
                    error=None,
                    actions=[
                        {"type": "send_message", "text": "置顶成功"},
                        {"type": "result", "success": True},
                        {"type": "no_session"},
                    ],
                )
            }

        async def unsubscribe(self, _channel: str) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    class _Redis:
        def __init__(self) -> None:
            self.pubsub_obj = _PubSub()

        def pubsub(self):
            return self.pubsub_obj

        async def publish(self, _channel: str, _payload: str) -> int:
            return 1

    redis = _Redis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)

    async def update_status(**_kwargs):
        return None

    monkeypatch.setattr(account_bot_runtime, "update_plugin_runtime_status", update_status)
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bbot-token",
        update_id=1,
        user_id=456,
        chat_id=-100123,
        message_id=10,
        text="",
    )

    ok, error, actions = await account_bot_runtime._run_worker_interaction_entry(
        incoming,
        plugin_key="pt_promote",
        entry_key="promote_torrent",
        payload={"chat_id": -100123, "id": "134100"},
    )

    assert ok is True
    assert error is None
    assert actions[0]["text"] == "置顶成功"
    assert actions[1] == {"type": "result", "success": True}
    assert redis.pubsub_obj.closed is True

@pytest.mark.asyncio
async def test_game24_winner_notice_replies_to_winning_answer(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, *_args):  # noqa: ANN002
            if model is Account:
                return SimpleNamespace(tg_username="owner", display_name="Owner", tg_user_id=999)
            return None

    send = AsyncMock()
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [
                {
                    "type": "send_message",
                    "text": "答对了：AAA\n题目：24 点 [1 5 5 5]\n答案：5*(5-1/5) = 24\n奖金：888",
                    "reply_to_message_id": 99,
                }
            ],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _MemoryRedis())
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(account_bot_runtime, "_load_interaction_session", AsyncMock(return_value={"rule_id": "game24-ticket"}))
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "rules": [
                    {
                        "id": "game24-ticket",
                        "enabled": True,
                        "chat_ids": [-100123],
                        "trigger_texts": ["转账成功"],
                        "action": "module",
                        "module_key": "game24",
                        "module_action": "start_paid_game",
                        "module_prize": 888,
                    },
                ],
            }
        ),
    )

    await account_bot_runtime._handle_interaction_update(
        1,
        "bbot-token",
        {
            "update_id": 9,
            "message": {
                "message_id": 99,
                "text": "5*(5-1/5)",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert run_entry.await_count == 1
    assert run_entry.await_args.kwargs["plugin_key"] == "game24"
    assert run_entry.await_args.kwargs["entry_key"] == "start_paid_game"
    payload = run_entry.await_args.kwargs["payload"]
    assert payload["event_type"] == "message"
    assert payload["message_text"] == "5*(5-1/5)"
    assert payload["payout_account_label"] == "@owner"
    assert payload["payout_mode"] == "auto"
    assert send.await_count == 1
    assert send.await_args.args[:3] == (
        "bbot-token",
        -100123,
        "答对了：AAA\n题目：24 点 [1 5 5 5]\n答案：5*(5-1/5) = 24\n奖金：888",
    )
    assert send.await_args.kwargs["reply_to_message_id"] == 99


@pytest.mark.asyncio
async def test_transfer_test_bot_accepts_plus_amount_from_account_user(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, key):  # noqa: ANN001
            if model is account_bot_runtime.Account and key == 1:
                return SimpleNamespace(tg_user_id=999)
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 456}, "message_id": 55}, {}])
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100123],
                "trusted_bot_id": 456,
                "trigger_texts": ["转账成功"],
                "receiver_text": None,
                "transfer_notice_template": "转账成功\n{payer_name} 射出 {amount}\n{receiver_name} 接收 {amount}",
            }
        ),
    )
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 5,
            "message": {
                "message_id": 50,
                "text": "+123",
                "from": {"id": 999, "first_name": "PayoutUser"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 49,
                    "from": {"id": 111, "first_name": "Winner"},
                    "text": "6",
                },
            },
        },
    )

    assert account_bot_service.get_transfer_bot_token.await_count == 1
    assert send.await_args_list[0].args[:3] == (
        "abot-token",
        -100123,
        "转账成功\nPayoutUser 射出 123\nWinner 接收 123",
    )
    assert send.await_args_list[0].kwargs["reply_to_message_id"] == 50


@pytest.mark.asyncio
async def test_transfer_test_bot_accepts_minus_amount_as_debit_notice(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

        async def get(self, model, key):  # noqa: ANN001
            if model is account_bot_runtime.Account and key == 1:
                return SimpleNamespace(tg_user_id=999)
            return None

    send = AsyncMock(side_effect=[{"from": {"id": 456}, "message_id": 55}, {}])
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "send_message", send)
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock())
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(
            return_value={
                "enabled": True,
                "chat_ids": [-100123],
                "trusted_bot_id": 456,
                "trigger_texts": ["转账成功", "扣减成功"],
                "receiver_text": None,
                "transfer_notice_template": "转账成功\n{payer_name} 射出 {amount}\n{receiver_name} 接收 {amount}",
                "debit_notice_template": '<pre><code class="language-扣减成功">{payer_name} 扣减 {amount} 蝌蚪\n{receiver_name} 接收 {amount} 蝌蚪</code></pre>',
            }
        ),
    )
    monkeypatch.setattr(account_bot_service, "get_transfer_bot_token", AsyncMock(return_value="abot-token"))

    await account_bot_runtime._handle_transfer_test_update(
        1,
        "bbot-token",
        {
            "update_id": 5,
            "message": {
                "message_id": 50,
                "text": "-123",
                "from": {"id": 999, "first_name": "PayoutUser"},
                "chat": {"id": -100123, "type": "supergroup"},
                "reply_to_message": {
                    "message_id": 49,
                    "from": {"id": 111, "first_name": "Winner"},
                    "text": "6",
                },
            },
        },
    )

    assert account_bot_service.get_transfer_bot_token.await_count == 1
    assert send.await_args_list[0].args[:3] == (
        "abot-token",
        -100123,
        '<pre><code class="language-扣减成功">Winner 扣减 123 蝌蚪\n'
        "PayoutUser 接收 123 蝌蚪</code></pre>",
    )
    assert send.await_args_list[0].kwargs["reply_to_message_id"] == 50


@pytest.mark.asyncio
async def test_authorized_group_plain_text_does_not_fall_through_to_account_commands(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    user = SimpleNamespace(enabled=True, role="admin", last_chat_id=None, display_name=None)
    handle_command = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock(return_value=user))
    monkeypatch.setattr(
        account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value=account_bot_service.default_transfer_notice_config()),
    )
    monkeypatch.setattr(account_bot_runtime, "_handle_command", handle_command)
    monkeypatch.setattr(account_bot_service, "send_message", AsyncMock())

    await account_bot_runtime._handle_update(
        1,
        "bbot-token",
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "text": "2",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        },
    )

    assert handle_command.await_count == 0
    assert account_bot_service.send_message.await_count == 0


@pytest.mark.asyncio
async def test_management_bot_command_creates_trace_when_router_debug_enabled(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def commit(self):
            return None

    trace = SimpleNamespace(trace_id="evt_management")
    user = SimpleNamespace(enabled=True, role="admin", last_chat_id=None, display_name=None)
    handle_command = AsyncMock()
    record_span = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": True, "inline_updates_enabled": True}))
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "find_bot_user", AsyncMock(return_value=user))
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "record_span", record_span)
    monkeypatch.setattr(account_bot_runtime, "finish_trace", finish_trace)
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_bot_runtime, "_should_route_text_to_account_commands", lambda _incoming: True)
    monkeypatch.setattr(account_bot_runtime, "_handle_command", handle_command)

    await account_bot_runtime._handle_update(
        1,
        "bbot-token",
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "text": "/status",
                "from": {"id": 111, "first_name": "AAA"},
                "chat": {"id": 111, "type": "private"},
            },
        },
    )

    account_bot_runtime.start_trace.assert_awaited_once()
    handle_command.assert_awaited_once()
    assert handle_command.await_args.args[0].trace_id == "evt_management"
    assert any(call.args[1] == "receive" for call in record_span.await_args_list)
    assert any(call.kwargs.get("component") == "account_bot_command" for call in record_span.await_args_list)
    finish_trace.assert_awaited_once_with(trace, "ok")


@pytest.mark.asyncio
async def test_stop_interaction_bot_manager_does_not_stop_management_tasks() -> None:
    management_task = asyncio.create_task(asyncio.sleep(60))
    interaction_task = asyncio.create_task(asyncio.sleep(60))
    account_bot_runtime._TASKS[1] = management_task
    account_bot_runtime._INTERACTION_TASKS[1] = interaction_task
    try:
        assert account_bot_runtime.is_interaction_bot_running(1) is True

        await account_bot_runtime.stop_interaction_bot_manager()

        assert account_bot_runtime.is_interaction_bot_running(1) is False
        assert 1 in account_bot_runtime._TASKS
        assert not management_task.done()
        assert interaction_task.cancelled()
    finally:
        account_bot_runtime._TASKS.clear()
        account_bot_runtime._INTERACTION_TASKS.clear()
        management_task.cancel()
        await asyncio.gather(management_task, return_exceptions=True)


def test_confirm_redis_key_uses_hash_not_plain_token() -> None:
    nonce = "plain-confirm-token"
    key = account_bot_runtime._confirm_redis_key(nonce)
    assert nonce not in key
    assert key.startswith("account_bot_confirm:")


@pytest.mark.asyncio
async def test_request_confirm_redis_only_stores_hashed_token(monkeypatch) -> None:
    class _Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str]] = []

        async def setex(self, key: str, ttl: int, value: str) -> None:
            self.calls.append((key, ttl, value))

    redis = _Redis()
    nonce = "token-for-confirm"
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bot-token",
        update_id=1,
        user_id=1001,
        chat_id=2002,
        message_id=3003,
        text="/restart",
        callback_id=None,
        callback_data=None,
        display_name="tester",
    )
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime.secrets, "token_urlsafe", lambda n: nonce)
    monkeypatch.setattr(account_bot_runtime, "_send", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_audit_confirm_event", AsyncMock())

    await account_bot_runtime._request_confirm(incoming, "admin", "restart", "重启账号 worker")

    assert len(redis.calls) == 1
    key, ttl, value = redis.calls[0]
    assert ttl == 300
    assert nonce not in key
    assert nonce not in value


@pytest.mark.asyncio
async def test_confirm_action_token_can_only_be_consumed_once(monkeypatch) -> None:
    class _Redis:
        def __init__(self) -> None:
            self.value = '{"account_id":1,"tg_user_id":42,"action":"restart","payload":{}}'

        async def get(self, _key: str) -> str | None:
            return self.value

        async def getdel(self, _key: str) -> str | None:
            v = self.value
            self.value = None
            return v

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bot-token",
        update_id=1,
        user_id=42,
        chat_id=1,
        message_id=2,
        text="",
        callback_id="cb-1",
        callback_data="ab:1:confirm:restart:nonce",
        display_name=None,
    )
    answer = AsyncMock()
    execute = AsyncMock()
    redis = _Redis()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime, "_execute_confirmed_action", execute)
    monkeypatch.setattr(account_bot_runtime, "_audit_confirm_event", AsyncMock())

    await account_bot_runtime._confirm_action(incoming, "admin", "restart", "nonce")
    await account_bot_runtime._confirm_action(incoming, "admin", "restart", "nonce")

    assert execute.await_count == 1
    assert answer.await_count == 1
    assert answer.await_args.kwargs.get("text") == "确认已过期"


@pytest.mark.asyncio
async def test_confirm_action_expired_token_is_rejected(monkeypatch) -> None:
    class _Redis:
        async def get(self, _key: str) -> str | None:
            return None

        async def getdel(self, _key: str) -> str | None:
            return None

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bot-token",
        update_id=1,
        user_id=42,
        chat_id=1,
        message_id=2,
        text="",
        callback_id="cb-1",
        callback_data="ab:1:confirm:restart:nonce",
        display_name=None,
    )
    answer = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: _Redis())
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime, "_execute_confirmed_action", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "_audit_confirm_event", AsyncMock())

    await account_bot_runtime._confirm_action(incoming, "admin", "restart", "nonce")

    assert answer.await_count == 1
    assert answer.await_args.kwargs.get("text") == "确认已过期"


@pytest.mark.asyncio
async def test_confirm_action_owner_mismatch_does_not_consume_token(monkeypatch) -> None:
    class _Redis:
        def __init__(self) -> None:
            self.value = '{"account_id":1,"tg_user_id":99,"action":"restart","payload":{}}'
            self.getdel_called = 0

        async def get(self, _key: str) -> str | None:
            return self.value

        async def getdel(self, _key: str) -> str | None:
            self.getdel_called += 1
            v = self.value
            self.value = None
            return v

    redis = _Redis()
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bot-token",
        update_id=1,
        user_id=42,
        chat_id=1,
        message_id=2,
        text="",
        callback_id="cb-1",
        callback_data="ab:1:confirm:restart:nonce",
        display_name=None,
    )
    answer = AsyncMock()
    execute = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime, "_execute_confirmed_action", execute)
    monkeypatch.setattr(account_bot_runtime, "_audit_confirm_event", AsyncMock())

    await account_bot_runtime._confirm_action(incoming, "admin", "restart", "nonce")

    assert redis.getdel_called == 0
    assert redis.value is not None
    assert execute.await_count == 0
    assert answer.await_count == 1
    assert answer.await_args.kwargs.get("text") == "只能由原用户确认"


@pytest.mark.asyncio
async def test_toggle_feature_operator_cannot_toggle_remote_plugin(monkeypatch) -> None:
    class _RemotePlugin:
        enabled = False

    class _DBResult:
        def scalar_one_or_none(self):
            return None

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, model, key):  # noqa: ANN001
            return type("FeatureRow", (), {"is_builtin": False, "display_name": "DemoPlugin"})()

        async def execute(self, _stmt):  # noqa: ANN001
            return _DBResult()

        async def commit(self):
            return None

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="bot-token",
        update_id=1,
        user_id=42,
        chat_id=1,
        message_id=2,
        text="",
        callback_id="cb-1",
        callback_data="ab:1:feature_toggle:demo",
        display_name=None,
    )

    answer = AsyncMock()
    show_features = AsyncMock()
    req_confirm = AsyncMock()
    set_feature = AsyncMock()
    enable_remote = AsyncMock()
    write_audit = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(account_bot_service, "answer_callback", answer)
    monkeypatch.setattr(account_bot_runtime, "_show_features", show_features)
    monkeypatch.setattr(account_bot_runtime, "_request_confirm", req_confirm)
    monkeypatch.setattr(account_bot_runtime.feature_service, "set_account_feature", set_feature)
    monkeypatch.setattr(account_bot_runtime.remote_plugin_service, "get_by_name", AsyncMock(return_value=_RemotePlugin()))
    monkeypatch.setattr(account_bot_runtime.remote_plugin_service, "enable", enable_remote)
    monkeypatch.setattr(account_bot_runtime.audit, "write", write_audit)

    await account_bot_runtime._toggle_feature(incoming, "operator", "demo")

    assert req_confirm.await_count == 0
    assert set_feature.await_count == 0
    assert enable_remote.await_count == 0
    assert write_audit.await_count == 0
    assert answer.await_count == 1
    assert answer.await_args.kwargs.get("show_alert") is True
    assert "仅 admin" in answer.await_args.kwargs.get("text")


@pytest.mark.asyncio
async def test_notify_runtime_log_deduplicates_same_error(monkeypatch) -> None:
    class _Redis:
        def __init__(self) -> None:
            self.keys: set[str] = set()

        async def set(self, key: str, _value: str, *, ex: int, nx: bool) -> bool | None:
            assert ex == account_bot_runtime._RUNTIME_NOTIFY_DEDUPE_TTL_SECONDS
            assert nx is True
            if key in self.keys:
                return None
            self.keys.add(key)
            return True

    redis = _Redis()
    notify = AsyncMock(return_value=1)
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(account_bot_runtime, "notify_account", notify)

    row = RuntimeLog(
        account_id=7,
        level="error",
        source="plugin",
        message="配置错误：x",
    )
    await account_bot_runtime.notify_runtime_log(row)
    await account_bot_runtime.notify_runtime_log(row)

    assert notify.await_count == 1


@pytest.mark.asyncio
async def test_notify_account_records_trace_action(monkeypatch) -> None:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(
                    account_id=7,
                    enabled=True,
                    bot_token_enc="enc-token",
                )
            )

    trace = SimpleNamespace(trace_id="evt_notify_account")
    send_message = AsyncMock(return_value={"message_id": 901})
    record_action = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(account_bot_runtime, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(account_bot_runtime, "_event_framework_flags", AsyncMock(return_value={"trace_enabled": True}))
    monkeypatch.setattr(account_bot_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(account_bot_runtime, "record_action", record_action)
    monkeypatch.setattr(account_bot_runtime, "finish_trace", finish_trace)
    monkeypatch.setattr(account_bot_runtime, "_router_debug_trace_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_bot_service, "decrypt_bot_token", lambda _row: "bot-token")
    monkeypatch.setattr(
        account_bot_service,
        "list_bot_users",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    enabled=True,
                    notify_enabled=True,
                    last_chat_id=12345,
                    tg_user_id=67890,
                )
            ]
        ),
    )
    monkeypatch.setattr(account_bot_service, "send_message", send_message)

    sent = await account_bot_runtime.notify_account(7, "测试通知")

    assert sent == 1
    send_message.assert_awaited_once_with("bot-token", 12345, "测试通知", parse_mode="HTML")
    record_action.assert_awaited_once()
    assert record_action.await_args.args[0] is trace
    assert record_action.await_args.args[1]["type"] == "send_message"
    assert record_action.await_args.kwargs["actual_send_via"] == "account_bot"
    finish_trace.assert_awaited_once_with(
        trace,
        account_bot_runtime.TRACE_STATUS_OK,
        sent_count=1,
        failed_count=0,
        target_count=1,
    )
