from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services import account_bot_runtime, account_bot_service
from app.worker.plugins import loader as loader_mod
from app.worker.plugins.base import Plugin, PluginContext


@pytest.mark.asyncio
async def test_interaction_event_bus_empty_allowed_chats_means_all(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=1,
        user_id=2001,
        chat_id=-100123,
        message_id=10,
        text="",
        callback_id="callback-empty-scope",
        callback_data="plugin_a:confirm",
    )
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_bus_cached_known_user_ids",
        AsyncMock(return_value=([], set())),
    )
    monkeypatch.setattr(
        account_bot_runtime,
        "_event_bus_active_session_participant_ids",
        AsyncMock(return_value=set()),
    )

    state = await account_bot_runtime._event_bus_account_state(object(), incoming, {"enabled": True})

    assert state["allowed_chat_ids"] == "*"
    subscription = account_bot_runtime.normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["callback_query"],
            "scope": "all_allowed_chats",
        },
        plugin_key="plugin_a",
    )
    decision = account_bot_runtime.dispatch_event(
        account_bot_runtime._incoming_trace_payload(incoming),
        [subscription],
        state,
    ).decisions[0]
    assert decision.matched is True


def test_interaction_callback_subscription_bypasses_empty_rule_prefilter() -> None:
    subscription = account_bot_runtime.normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["callback_query"],
            "scope": "all_allowed_chats",
        },
        plugin_key="plugin_a",
    )
    index = account_bot_runtime._build_interaction_routing_index(
        {
            "enabled": True,
            "rules": [],
            "chat_ids": [],
            "interaction_bot_id": 999,
        },
        subscriptions=[subscription],
        active_session_chat_ids=set(),
    )
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=2,
        user_id=2001,
        chat_id=-100123,
        message_id=20,
        text="",
        callback_id="callback-prefilter",
        callback_data="plugin_a:confirm",
    )

    routes = account_bot_runtime._classify_interaction_routes(
        incoming,
        index,
        event_bus_enabled=True,
    )

    assert routes == [account_bot_runtime._ROUTE_EVENT_BUS]


@pytest.mark.asyncio
async def test_interaction_callback_without_rule_or_entry_reaches_event_handler(monkeypatch) -> None:
    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=2,
        user_id=2001,
        chat_id=-100123,
        chat_type="supergroup",
        message_id=20,
        text="插件 A 操作",
        callback_id="callback-autonomous",
        callback_data="plugin_a:confirm",
        native_raw={"update_id": 2},
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(feature_key="plugin_a")]

    class _DB:
        async def execute(self, _stmt):  # noqa: ANN001
            return _Result()

        async def get(self, *_args):  # noqa: ANN002
            return None

    monkeypatch.setattr(
        account_bot_service,
        "declared_module_event_subscriptions",
        lambda _key: [
            {
                "source": ["interaction_bot"],
                "events": ["callback_query"],
                "scope": "all_allowed_chats",
            }
        ],
    )
    monkeypatch.setattr(
        account_bot_service,
        "plugin_declares_telegram_native_raw",
        lambda *_args, **_kwargs: False,
    )
    run_entry = AsyncMock(
        return_value=(
            True,
            None,
            [
                {
                    "type": "answer_callback",
                    "callback_query_id": "callback-autonomous",
                    "text": "按钮已收到",
                }
            ],
        )
    )
    monkeypatch.setattr(account_bot_runtime, "_run_worker_interaction_entry", run_entry)
    monkeypatch.setattr(
        account_bot_runtime,
        "_guard_interaction_actions",
        AsyncMock(side_effect=lambda _incoming, _rule, actions: actions),
    )
    monkeypatch.setattr(account_bot_runtime, "_apply_interaction_actions", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "record_span", AsyncMock())

    handled, ok = await account_bot_runtime._try_handle_event_bus_subscriptions(
        _DB(),
        incoming,
        {"enabled": True},
    )

    assert handled is True
    assert ok is True
    run_entry.assert_awaited_once()
    assert run_entry.await_args.kwargs["entry_key"] == ""
    assert run_entry.await_args.kwargs["payload"]["trigger"]["dispatch_mode"] == "event_subscription"


@pytest.mark.asyncio
async def test_interaction_worker_event_subscription_prefers_on_event_without_entry_key() -> None:
    calls: list[tuple[str, str]] = []

    class _AutonomousButtonPlugin(Plugin):
        key = "_test_autonomous_button"
        display_name = "自主按钮测试"

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            calls.append(("on_event", payload["callback_data"]))
            return [
                {
                    "type": "answer_callback",
                    "callback_query_id": payload["callback_query_id"],
                    "text": "按钮已收到",
                }
            ]

        async def on_interaction(
            self,
            ctx: PluginContext,
            entry_key: str,
            payload: dict[str, Any],
        ) -> list[dict[str, Any]]:
            calls.append(("on_interaction", entry_key))
            return []

    state = loader_mod._AccountState(account_id=901)
    state.instances["_test_autonomous_button"] = _AutonomousButtonPlugin()
    state.contexts["_test_autonomous_button"] = PluginContext(
        account_id=901,
        feature_key="_test_autonomous_button",
    )
    loader_mod._STATES[901] = state
    try:
        actions = await loader_mod.invoke_interaction_entry(
            901,
            plugin_key="_test_autonomous_button",
            entry_key="",
            payload={
                "trace_id": "evt-autonomous-button",
                "callback_query_id": "callback-autonomous",
                "callback_data": "plugin_a:confirm",
                "trigger": {"dispatch_mode": "event_subscription"},
            },
        )
    finally:
        loader_mod._STATES.pop(901, None)

    assert calls == [("on_event", "plugin_a:confirm")]
    assert actions == [
        {
            "type": "answer_callback",
            "callback_query_id": "callback-autonomous",
            "text": "按钮已收到",
        }
    ]
