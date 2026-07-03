from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.event_bus import (
    EVENT_REASON_CODES,
    EVENT_TRACE_STATUSES,
    VALID_EVENT_TYPES,
    dispatch_event,
    normalize_bot_update,
    normalize_event_subscription,
    normalize_payment_notice,
    normalize_userbot_event,
)


def test_event_bus_exports_stable_status_and_reason_code_dictionary() -> None:
    assert {
        "received",
        "normalized",
        "matched",
        "skipped",
        "delivered",
        "plugin_succeeded",
        "plugin_failed",
        "action_succeeded",
        "action_failed",
        "trace_degraded",
    } <= EVENT_TRACE_STATUSES
    assert {
        "account_not_matched",
        "account_bot_user_unauthorized",
        "action_failed",
        "plugin_not_installed",
        "plugin_disabled",
        "manifest_invalid",
        "plugin_load_failed",
        "matched",
        "event_type_not_subscribed",
        "source_not_subscribed",
        "scope_not_matched",
        "filter_not_matched",
        "session_not_found",
        "session_expired",
        "rate_limited",
        "callback_query",
        "command_matched",
        "command_not_matched",
        "command_unauthorized",
        "contract_warning",
        "callback_query_id_missing",
        "entry_key_missing",
        "empty_message_text",
        "event_bus_delivery_disabled",
        "handler_error",
        "inline_disabled",
        "inline_query_answer_failed",
        "inline_query_id_missing",
        "media_payload_empty",
        "media_payload_invalid",
        "media_payload_missing",
        "native_raw_not_allowed",
        "native_raw_skipped",
        "permission_denied",
        "send_channel_deprecated",
        "session_control_action",
        "bot_not_configured",
        "bot_self_message",
        "bot_token_missing",
        "userbot_offline",
        "settlement_requires_userbot",
        "subscription_load_failed",
        "subscription_not_matched",
        "telegram_api_error",
        "plugin_runtime_error",
        "trace_write_failed",
        "unsupported_send_via",
    } <= EVENT_REASON_CODES
    assert {
        "all_messages",
        "inline_query",
        "chosen_inline_result",
        "payment_confirmed",
        "command",
        "callback_query",
    } <= VALID_EVENT_TYPES


def test_runtime_reason_code_literals_are_registered() -> None:
    app_root = Path(__file__).resolve().parents[1]
    used: set[str] = set()

    def _collect_constant_strings(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return _collect_constant_strings(node.body) | _collect_constant_strings(node.orelse)
        if isinstance(node, ast.BoolOp):
            out: set[str] = set()
            for value in node.values:
                out.update(_collect_constant_strings(value))
            return out
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            out: set[str] = set()
            for item in node.elts:
                out.update(_collect_constant_strings(item))
            return out
        return set()

    for path in app_root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "reason_code":
                used.update(_collect_constant_strings(node.value))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "reason_code"
                    ):
                        used.update(_collect_constant_strings(value))
    assert used <= EVENT_REASON_CODES


def test_normalize_bot_update_projects_inline_query() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 42,
            "inline_query": {
                "id": "iq-1",
                "query": "玩法",
                "offset": "",
                "chat_type": "sender",
                "from": {"id": 1001, "first_name": "Alice", "username": "alice"},
            },
        },
    )

    assert event["source"]["type"] == "inline_query"
    assert event["source"]["channel"] == "interaction_bot"
    assert event["inline_query"]["id"] == "iq-1"
    assert event["inline_query"]["from"]["user_id"] == 1001
    assert event["message"]["text"] == "玩法"
    assert event["native_raw"]["update_id"] == 42
    assert event["native_raw_meta"]["enabled"] is False


def test_normalize_bot_update_projects_developer_message_summary() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 43,
            "message": {
                "message_id": 9,
                "message_thread_id": 77,
                "text": "打开 文档",
                "chat": {"id": -100, "type": "supergroup", "title": "Demo"},
                "from": {"id": 1001, "first_name": "Alice"},
                "entities": [{"type": "bot_command", "offset": 0, "length": 2}],
                "document": {
                    "file_id": "file-1",
                    "file_unique_id": "uniq-1",
                    "file_name": "demo.txt",
                    "mime_type": "text/plain",
                    "file_size": 10,
                },
                "reply_markup": {
                    "inline_keyboard": [[{"text": "开始", "callback_data": "demo:start"}]]
                },
                "reply_to_message": {
                    "message_id": 8,
                    "text": "上一条",
                    "from": {"id": 1002, "first_name": "Bob"},
                },
            },
        },
    )

    assert event["message"]["thread_id"] == 77
    assert event["message"]["reply_to_message_id"] == 8
    assert event["message"]["entities"][0]["type"] == "bot_command"
    assert event["message"]["media"]["type"] == "document"
    assert event["message"]["reply_markup"]["button_count"] == 1
    assert event["reply_to"]["sender"]["user_id"] == 1002
    assert event["raw"]["media"]["file_name"] == "demo.txt"
    assert event["raw"]["reply_markup"]["buttons"][0]["callback_data"] == "demo:start"


def test_normalize_bot_update_projects_anonymous_admin_sender_chat() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 44,
            "message": {
                "message_id": 10,
                "text": "开24点",
                "chat": {"id": -100123, "type": "supergroup", "title": "Demo Group"},
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
            },
        },
    )
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message"],
            "scope": "all_allowed_chats",
            "filters": {"keywords": ["开24点"]},
            "entry_key": "start_paid_game",
        },
        plugin_key="game24",
    )
    owner_subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message"],
            "scope": "owner_only",
            "filters": {"keywords": ["开24点"]},
            "entry_key": "admin_start",
        },
        plugin_key="admin_game",
    )

    decisions = dispatch_event(
        event,
        [subscription, owner_subscription],
        {"allowed_chat_ids": [-100123], "owner_user_ids": [1087968824]},
    ).decisions

    assert event["sender"]["user_id"] is None
    assert event["sender"]["sender_type"] == "chat"
    assert event["sender"]["sender_chat"]["id"] == -100123
    assert event["sender"]["display_name"] == "Demo Group"
    assert event["message"]["sender_chat"]["id"] == -100123
    assert decisions[0].matched is True
    assert decisions[0].reason_code == "matched"
    assert decisions[1].matched is False
    assert decisions[1].reason_code == "scope_not_matched"


def test_match_subscription_accepts_inline_all_scope() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 42,
            "inline_query": {
                "id": "iq-1",
                "query": "玩法",
                "from": {"id": 1001},
            },
        },
    )
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["inline_query"],
            "scope": "inline_all",
            "entry_key": "inline_search",
        },
        plugin_key="inline_game",
    )

    result = dispatch_event(event, [subscription], {})

    assert len(result.matched) == 1
    assert result.matched[0].plugin_key == "inline_game"
    assert result.matched[0].entry_key == "inline_search"
    assert result.matched[0].reason_code == "matched"


def test_match_subscription_explains_source_event_scope_and_filter_skips() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 7,
            "message": {
                "message_id": 5,
                "text": "开始",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
            },
        },
    )
    subscriptions = [
        normalize_event_subscription(
            {"source": ["userbot"], "events": ["message"], "scope": "all_allowed_chats"},
            plugin_key="wrong_source",
        ),
        normalize_event_subscription(
            {"source": ["interaction_bot"], "events": ["callback_query"], "scope": "all_allowed_chats"},
            plugin_key="wrong_event",
        ),
        normalize_event_subscription(
            {"source": ["interaction_bot"], "events": ["message"], "scope": "owner_only"},
            plugin_key="wrong_scope",
        ),
        normalize_event_subscription(
            {
                "source": ["interaction_bot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "filters": {"keywords": ["其他"]},
            },
            plugin_key="wrong_filter",
        ),
    ]

    decisions = dispatch_event(event, subscriptions, {"allowed_chat_ids": [-100], "owner_user_ids": [999]}).decisions

    assert [item.reason_code for item in decisions] == [
        "source_not_subscribed",
        "event_type_not_subscribed",
        "scope_not_matched",
        "filter_not_matched",
    ]


def test_match_subscription_accepts_allowed_chat_keyword() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 8,
            "message": {
                "message_id": 6,
                "text": "开始",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
            },
        },
    )
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message"],
            "scope": "all_allowed_chats",
            "filters": {"keywords": ["开始"]},
        },
        plugin_key="game",
        entry_key="start",
    )

    decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert decision.matched is True
    assert decision.reason_code == "matched"


def test_match_subscription_all_messages_covers_message_and_command() -> None:
    message_event = normalize_bot_update(
        1,
        {
            "update_id": 8,
            "message": {
                "message_id": 6,
                "text": "hello",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
            },
        },
    )
    command_event = normalize_userbot_event(
        1,
        SimpleNamespace(message=SimpleNamespace(id=9, chat_id=-100, sender_id=2001, text=",reload")),
        command_meta={"command": "reload"},
    )
    subscription = normalize_event_subscription(
        {"source": ["interaction_bot", "userbot"], "events": ["all_messages"], "scope": "all_allowed_chats"},
        plugin_key="audit",
        entry_key="main",
    )

    message_decision = dispatch_event(message_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]
    command_decision = dispatch_event(command_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert message_decision.matched is True
    assert command_decision.matched is True


def test_normalize_userbot_event_projects_anonymous_admin_sender_chat() -> None:
    sender = SimpleNamespace(id=-100123, title="Demo Group", username="demo_group", megagroup=True, photo=SimpleNamespace(dc_id=5))
    message = SimpleNamespace(
        id=10,
        chat_id=-100123,
        sender_id=-100123,
        text="开24点",
        post_author="匿名管理员",
        sender=sender,
    )
    event = normalize_userbot_event(1, SimpleNamespace(message=message))
    subscription = normalize_event_subscription(
        {
            "source": ["userbot"],
            "events": ["message"],
            "scope": "all_allowed_chats",
            "filters": {"keywords": ["开24点"]},
            "entry_key": "start_paid_game",
        },
        plugin_key="game24",
    )
    owner_subscription = normalize_event_subscription(
        {
            "source": ["userbot"],
            "events": ["message"],
            "scope": "owner_only",
            "filters": {"keywords": ["开24点"]},
            "entry_key": "admin_start",
        },
        plugin_key="admin_game",
    )

    decisions = dispatch_event(
        event,
        [subscription, owner_subscription],
        {"allowed_chat_ids": [-100123], "owner_user_ids": [-100123]},
    ).decisions

    assert event["sender"]["user_id"] is None
    assert event["sender"]["sender_type"] == "chat"
    assert event["sender"]["display_name"] == "匿名管理员"
    assert event["sender"]["sender_chat"]["id"] == -100123
    assert event["sender"]["sender_chat"]["title"] == "Demo Group"
    assert event["sender"]["sender_chat"]["signature"] == "匿名管理员"
    assert event["message"]["sender_chat"]["id"] == -100123
    assert decisions[0].matched is True
    assert decisions[0].reason_code == "matched"
    assert decisions[1].matched is False
    assert decisions[1].reason_code == "scope_not_matched"


def test_normalize_userbot_event_detects_raw_peer_channel_sender() -> None:
    class _Message:
        id = 11
        chat_id = -100456
        sender_id = -100777
        text = "hello"

        def to_dict(self):
            return {
                "id": self.id,
                "message": self.text,
                "peer_id": {"_": "PeerChannel", "channel_id": 456},
                "from_id": {"_": "PeerChannel", "channel_id": 777},
                "post_author": "频道身份",
            }

    event = normalize_userbot_event(1, SimpleNamespace(message=_Message()))

    assert event["sender"]["user_id"] is None
    assert event["sender"]["sender_type"] == "chat"
    assert event["sender"]["display_name"] == "频道身份"
    assert event["sender"]["sender_chat"]["id"] == -100777
    assert event["sender"]["sender_chat"]["type"] == "channel"
    assert event["raw"]["signature"] == "频道身份"


def test_match_subscription_owner_only_uses_account_owner() -> None:
    event = normalize_userbot_event(
        1,
        SimpleNamespace(
            message=SimpleNamespace(id=9, chat_id=-100, sender_id=3001, text=",reload"),
        ),
        command_meta={"command": "reload"},
    )
    subscription = normalize_event_subscription(
        {"source": ["userbot"], "events": ["command"], "scope": "owner_only", "filters": {"commands": ["reload"]}},
        plugin_key="admin_tool",
    )

    decision = dispatch_event(event, [subscription], {"owner_user_ids": [3001]}).decisions[0]

    assert decision.matched is True
    assert event["source"]["type"] == "command"


def _event_for_type(event_type: str) -> dict:
    if event_type == "callback_query":
        return normalize_bot_update(
            1,
            {
                "update_id": 11,
                "callback_query": {
                    "id": "cb-1",
                    "data": "start",
                    "from": {"id": 2001},
                    "message": {
                        "message_id": 6,
                        "text": "button",
                        "chat": {"id": -100, "type": "supergroup"},
                    },
                },
            },
        )
    if event_type == "inline_query":
        return normalize_bot_update(
            1,
            {"update_id": 12, "inline_query": {"id": "iq-1", "query": "玩法", "from": {"id": 2001}}},
        )
    if event_type == "chosen_inline_result":
        return normalize_bot_update(
            1,
            {
                "update_id": 13,
                "chosen_inline_result": {
                    "result_id": "res-1",
                    "query": "玩法",
                    "from": {"id": 2001},
                },
            },
        )
    if event_type == "payment_confirmed":
        return normalize_payment_notice(
            1,
            {
                "update_id": 14,
                "message": {
                    "message_id": 7,
                    "text": "付款人：Bob\n金额：100",
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": 2001},
                },
            },
            {"payer_name": "Bob", "amount": 100},
        )
    if event_type == "command":
        return normalize_userbot_event(
            1,
            SimpleNamespace(message=SimpleNamespace(id=15, chat_id=-100, sender_id=2001, text=",reload")),
            command_meta={"command": "reload"},
        )
    return normalize_bot_update(
        1,
        {
            "update_id": 15,
            "message": {
                "message_id": 8,
                "text": "hello",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001},
            },
        },
    )


@pytest.mark.parametrize(
    ("event_type", "scope"),
    [
        ("all_messages", "all_allowed_chats"),
        ("inline_query", "inline_all"),
        ("chosen_inline_result", "inline_all"),
        ("payment_confirmed", "all_allowed_chats"),
        ("command", "all_allowed_chats"),
        ("callback_query", "all_allowed_chats"),
    ],
)
def test_required_event_types_match_and_explain_event_skip(event_type: str, scope: str) -> None:
    actual_event_type = "message" if event_type == "all_messages" else event_type
    event = _event_for_type(actual_event_type)
    subscription = normalize_event_subscription(
        {"source": [event["source"]["channel"]], "events": [event_type], "scope": scope, "entry_key": "main"},
        plugin_key=f"{event_type}_plugin",
    )
    wrong_event_subscription = normalize_event_subscription(
        {"source": [event["source"]["channel"]], "events": ["callback_query"], "scope": scope, "entry_key": "main"},
        plugin_key="wrong_event",
    )
    if actual_event_type == "callback_query":
        wrong_event_subscription = normalize_event_subscription(
            {"source": [event["source"]["channel"]], "events": ["message"], "scope": scope, "entry_key": "main"},
            plugin_key="wrong_event",
        )

    matched, skipped = dispatch_event(
        event,
        [subscription, wrong_event_subscription],
        {"allowed_chat_ids": [-100], "owner_user_ids": [2001], "known_user_ids": [2001]},
    ).decisions

    assert matched.matched is True
    assert matched.reason_code == "matched"
    assert skipped.matched is False
    assert skipped.reason_code == "event_type_not_subscribed"


def test_inline_scope_skip_uses_stable_reason_code() -> None:
    event = _event_for_type("message")
    subscription = normalize_event_subscription(
        {"source": ["interaction_bot"], "events": ["message"], "scope": "inline_all", "entry_key": "main"},
        plugin_key="inline_only",
    )

    decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert decision.matched is False
    assert decision.reason_code == "scope_not_matched"
