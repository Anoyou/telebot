from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.event_bus import (
    EVENT_REASON_CODES,
    EVENT_TRACE_STATUSES,
    SUPPORTED_FILTER_KEYS,
    VALID_EVENT_TYPES,
    _reply_markup_summary,
    dispatch_event,
    normalize_bot_update,
    normalize_event_subscription,
    normalize_payment_notice,
    normalize_userbot_event,
    normalize_webhook_event,
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
        "already_acked",
        "account_bot_user_unauthorized",
        "action_failed",
        "action_limit_exceeded",
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
        "interaction_rule_owned",
        "media_payload_empty",
        "media_payload_invalid",
        "media_payload_missing",
        "native_raw_not_allowed",
        "native_raw_skipped",
        "permission_denied",
        "payout_failed",
        "send_channel_deprecated",
        "session_control_action",
        "bot_not_configured",
        "bot_self_message",
        "bot_token_missing",
        "userbot_offline",
        "settlement_requires_userbot",
        "subscription_load_failed",
        "subscription_not_matched",
        "synthetic_callback",
        "telegram_api_error",
        "plugin_declared_failed",
        "plugin_runtime_error",
        "trace_write_failed",
        "unsupported_send_via",
    } <= EVENT_REASON_CODES
    assert {
        "all_events",
        "all_messages",
        "inline_query",
        "chosen_inline_result",
        "payment_confirmed",
        "command",
        "callback_query",
        "message_edited",
        "session_expired",
        "webhook",
    } <= VALID_EVENT_TYPES
    assert {"keywords", "contains", "callback_data", "commands", "rule_id", "hook_key"} <= SUPPORTED_FILTER_KEYS


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
    assert event["source"]["display_name"] == "交互 Bot"
    assert event["inline_query"]["id"] == "iq-1"
    assert event["inline_query"]["from"]["user_id"] == 1001
    assert event["message"]["text"] == "玩法"
    assert event["native_raw"]["update_id"] == 42
    assert event["native_raw_meta"]["enabled"] is False


def test_normalize_userbot_event_preserves_rich_message_and_uses_text_fallback() -> None:
    rich_message = {
        "blocks": [
            {"_": "PageBlockHeading2", "text": {"_": "TextPlain", "text": "巡检"}},
            {"_": "PageBlockParagraph", "text": {"_": "TextBold", "text": {"_": "TextPlain", "text": "正常"}}},
        ],
        "photos": [],
        "documents": [],
    }
    message = SimpleNamespace(
        text="",
        message="",
        rich_message=rich_message,
        chat_id=-100,
        sender_id=42,
        id=9,
        to_dict=lambda: {"id": 9, "rich_message": rich_message},
    )

    event = normalize_userbot_event(1, SimpleNamespace(message=message))

    assert event["message"]["text"] == "巡检\n正常"
    assert event["message"]["text_source"] == "rich_message_fallback"
    assert event["message"]["rich_message"]["blocks"][0]["type"] == "heading"
    assert event["raw"]["rich_message"] == event["message"]["rich_message"]


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
    assert event["chat"]["title"] == "Demo"
    assert event["message"]["chat_title"] == "Demo"
    assert event["message"]["chat"]["title"] == "Demo"
    assert event["raw"]["chat"]["title"] == "Demo"
    assert event["raw"]["media"]["file_name"] == "demo.txt"
    button = event["raw"]["reply_markup"]["buttons"][0]
    assert button == {
        "row": 0,
        "col": 0,
        "column": 0,
        "text": "开始",
        "kind": "callback",
    }
    assert "callback_data" not in button
    assert "url" not in button


def test_normalize_bot_update_does_not_expose_inline_button_url() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 44,
            "message": {
                "message_id": 10,
                "text": "请选择",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 1001, "first_name": "Alice"},
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "登录", "url": "https://example.test/login?token=secret"}]
                    ]
                },
            },
        },
    )

    button = event["message"]["reply_markup"]["buttons"][0]
    assert button == {
        "row": 0,
        "col": 0,
        "column": 0,
        "text": "登录",
        "kind": "url",
    }
    assert event["raw"]["reply_markup"]["buttons"][0] == button
    assert "url" not in button


def test_normalize_bot_update_preserves_reply_keyboard_type() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 440,
            "message": {
                "message_id": 10,
                "text": "请选择",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 1001, "first_name": "Alice"},
                "reply_markup": {"keyboard": [[{"text": "同意"}]]},
            },
        },
    )

    markup = event["message"]["reply_markup"]
    assert markup["type"] == "reply_keyboard"
    assert markup["buttons"][0]["kind"] == "text"


@pytest.mark.parametrize(
    ("native_type", "expected_type"),
    [
        ("ReplyInlineMarkup", "inline_keyboard"),
        ("ReplyKeyboardMarkup", "reply_keyboard"),
    ],
)
def test_telethon_reply_markup_projection_preserves_keyboard_type(
    native_type: str,
    expected_type: str,
) -> None:
    markup = _reply_markup_summary(
        {
            "_": native_type,
            "rows": [
                {
                    "buttons": [
                        {"_": "KeyboardButtonCallback", "text": "确认", "data": b"secret"}
                    ]
                }
            ],
        }
    )

    assert markup is not None
    assert markup["type"] == expected_type
    assert markup["buttons"][0]["kind"] == "callback"
    assert "data" not in markup["buttons"][0]


def test_normalize_bot_update_projects_edited_message_event() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 45,
            "edited_message": {
                "message_id": 12,
                "text": "已编辑",
                "date": 1710000000,
                "edit_date": 1710000015,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 1001, "first_name": "Alice"},
            },
        },
    )

    assert event["source"]["type"] == "message_edited"
    assert event["message"]["date"] == 1710000000
    assert event["message"]["edited"] is True


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
    assert event["sender"]["is_anonymous_admin"] is True
    assert event["message"]["sender_chat"]["id"] == -100123
    assert decisions[0].matched is True
    assert decisions[0].reason_code == "matched"
    assert decisions[1].matched is False
    assert decisions[1].reason_code == "scope_not_matched"


def test_normalize_bot_update_uses_anonymous_admin_tag_without_exposing_fake_sender() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 45,
            "message": {
                "message_id": 11,
                "text": "匿名消息",
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
                },
                "sender_tag": "值班管理员",
                "author_signature": "旧版管理员标题",
            },
        },
    )

    assert event["sender"]["user_id"] is None
    assert event["sender"]["display_name"] == "值班管理员"
    assert event["sender"]["tag"] == "值班管理员"
    assert event["sender"]["is_anonymous_admin"] is True
    assert event["message"]["sender_tag"] == "值班管理员"
    assert event["message"]["author_signature"] == "旧版管理员标题"


def test_normalize_bot_update_keeps_regular_member_name_when_sender_tag_exists() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 46,
            "message": {
                "message_id": 12,
                "text": "普通消息",
                "chat": {"id": -100123, "type": "supergroup", "title": "Demo Group"},
                "from": {"id": 1001, "first_name": "普通成员"},
                "sender_tag": "普通成员标签",
            },
        },
    )

    assert event["sender"]["display_name"] == "普通成员"
    assert event["sender"]["tag"] == "普通成员标签"
    assert event["sender"]["is_anonymous_admin"] is False


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


def test_commands_filter_does_not_crash_on_empty_text_events() -> None:
    """带 commands filter 的订阅遇到无文本事件（空串 / 纯媒体 / callback）不得抛异常。

    回归：此前 ``text.lstrip('/,').split(maxsplit=1)[0]`` 在空文本时 IndexError，
    会冒泡出 match_subscriptions 崩溃整条匹配流程。
    """
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["all_events"],
            "scope": "all_allowed_chats",
            "filters": {"commands": ["start"]},
        },
        plugin_key="cmd_plugin",
        entry_key="main",
    )

    # 1) message.text 为空串
    empty_text_event = normalize_bot_update(
        1,
        {
            "update_id": 20,
            "message": {
                "message_id": 8,
                "text": "",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
            },
        },
    )
    # 2) 纯媒体消息（无 text 字段，仅 photo）
    media_event = normalize_bot_update(
        1,
        {
            "update_id": 21,
            "message": {
                "message_id": 9,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
                "photo": [{"file_id": "photo-1", "width": 90, "height": 90}],
            },
        },
    )
    # 3) callback_query（message.text 为空的场景）
    callback_event = normalize_bot_update(
        1,
        {
            "update_id": 22,
            "callback_query": {
                "id": "cb-2",
                "data": "start",
                "from": {"id": 2001},
                "message": {
                    "message_id": 10,
                    "chat": {"id": -100, "type": "supergroup"},
                },
            },
        },
    )

    for event in (empty_text_event, media_event, callback_event):
        # 不抛异常，且命令过滤器对无文本事件必然不匹配。
        decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]
        assert decision.matched is False
        assert decision.reason_code == "filter_not_matched"


def test_commands_filter_still_matches_real_command_text() -> None:
    """确保空文本防护没有回归正常命令匹配。"""
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message"],
            "scope": "all_allowed_chats",
            "filters": {"commands": ["start"]},
        },
        plugin_key="cmd_plugin",
        entry_key="main",
    )
    match_event = normalize_bot_update(
        1,
        {
            "update_id": 23,
            "message": {
                "message_id": 11,
                "text": "/start now",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
            },
        },
    )
    miss_event = normalize_bot_update(
        1,
        {
            "update_id": 24,
            "message": {
                "message_id": 12,
                "text": "/other",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001, "first_name": "Bob"},
            },
        },
    )

    match_decision = dispatch_event(match_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]
    miss_decision = dispatch_event(miss_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert match_decision.matched is True
    assert miss_decision.matched is False
    assert miss_decision.reason_code == "filter_not_matched"


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


def test_all_events_matches_callback_and_session_events_but_not_inline() -> None:
    subscription = normalize_event_subscription(
        {"source": ["interaction_bot"], "events": ["all_events"], "scope": "all_allowed_chats"},
        plugin_key="audit",
        entry_key="main",
    )

    callback_event = _event_for_type("callback_query")
    callback_decision = dispatch_event(callback_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    session_event = {
        "source": {"type": "session_expired", "channel": "interaction_bot", "chat_id": -100},
        "event_type": "session_expired",
        "message": {"chat_id": -100},
        "chat": {"id": -100, "type": "supergroup"},
        "sender": {"user_id": 2001},
    }
    session_decision = dispatch_event(session_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    inline_event = _event_for_type("inline_query")
    inline_decision = dispatch_event(inline_event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert callback_decision.matched is True
    assert session_decision.matched is True
    assert inline_decision.matched is False
    assert inline_decision.reason_code == "event_type_not_subscribed"


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
    assert event["sender"]["is_anonymous_admin"] is True
    assert event["sender"]["tag"] == "匿名管理员"
    assert event["sender"]["sender_chat"]["id"] == -100123
    assert event["sender"]["sender_chat"]["title"] == "Demo Group"
    assert event["sender"]["sender_chat"]["signature"] == "匿名管理员"
    assert event["message"]["sender_chat"]["id"] == -100123
    assert decisions[0].matched is True
    assert decisions[0].reason_code == "matched"
    assert decisions[1].matched is False
    assert decisions[1].reason_code == "scope_not_matched"


def test_normalize_userbot_event_projects_chat_title() -> None:
    chat = SimpleNamespace(id=-100456, title="UserBot 群", username="userbot_room", megagroup=True)
    message = SimpleNamespace(
        id=12,
        chat_id=-100456,
        sender_id=1001,
        text="hello",
        chat=chat,
        sender=SimpleNamespace(id=1001, first_name="Alice", username="alice"),
    )

    event = normalize_userbot_event(1, SimpleNamespace(message=message, chat=chat))

    assert event["chat"]["title"] == "UserBot 群"
    assert event["source"]["display_name"] == "主号"
    assert event["chat"]["username"] == "userbot_room"
    assert event["message"]["chat_title"] == "UserBot 群"
    assert event["message"]["chat"]["title"] == "UserBot 群"


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


def test_normalize_userbot_event_marks_same_peer_channel_as_anonymous_admin() -> None:
    class _Message:
        id = 13
        chat_id = -100456
        sender_id = -100456
        text = "匿名发言"

        def to_dict(self):
            return {
                "id": self.id,
                "message": self.text,
                "peer_id": {"_": "PeerChannel", "channel_id": 456},
                "from_id": {"_": "PeerChannel", "channel_id": 456},
                "post_author": "心里测试管理员",
            }

    event = normalize_userbot_event(1, SimpleNamespace(message=_Message()))

    assert event["sender"]["user_id"] is None
    assert event["sender"]["sender_type"] == "chat"
    assert event["sender"]["display_name"] == "心里测试管理员"
    assert event["sender"]["tag"] == "心里测试管理员"
    assert event["sender"]["is_anonymous_admin"] is True
    assert event["sender"]["sender_chat"]["id"] == -100456
    assert event["sender"]["sender_chat"]["type"] == "channel"


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


def test_normalize_event_subscription_marks_unknown_filter_keys() -> None:
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message"],
            "filters": {"keywords": ["开始"], "mystery": True},
        },
        plugin_key="game",
    )
    event = normalize_bot_update(
        1,
        {
            "update_id": 16,
            "message": {
                "message_id": 8,
                "text": "开始",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001},
            },
        },
    )

    decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert subscription.unknown_filter_keys == ["mystery"]
    assert decision.unknown_filter_keys == ["mystery"]
    assert decision.warnings == [
        "filters 含未知 key: mystery，该过滤条件不会生效，订阅可能匹配到预期外的事件。"
    ]
    assert "不会生效" in decision.reason_message


def test_normalize_event_subscription_marks_unknown_events_without_breaking_valid_matches() -> None:
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": ["message", "ghost_event"],
            "filters": {"keywords": ["开始"]},
        },
        plugin_key="game",
    )
    event = normalize_bot_update(
        1,
        {
            "update_id": 160,
            "message": {
                "message_id": 80,
                "text": "开始",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001},
            },
        },
    )

    decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100]}).decisions[0]

    assert subscription.unknown_events == ["ghost_event"]
    assert decision.unknown_events == ["ghost_event"]
    assert decision.matched is True
    assert decision.warnings == [
        "events 含未知类型: ghost_event，这些事件类型不会匹配任何当前支持的事件。"
    ]


def test_normalize_event_subscription_accepts_all_event_umbrella_values() -> None:
    subscription = normalize_event_subscription(
        {"source": ["interaction_bot"], "events": ["all_events", "all_messages"]},
        plugin_key="game",
    )

    assert subscription.unknown_events == []


def test_rule_bound_scope_requires_matching_rule_id_filter() -> None:
    event = normalize_payment_notice(
        1,
        {
            "update_id": 17,
            "message": {
                "message_id": 7,
                "text": "付款人：Bob\n金额：100",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 2001},
            },
        },
        {"payer_name": "Bob", "amount": 100},
    )
    event["trigger"] = {"rule_id": "rule-1"}
    matched = normalize_event_subscription(
        {
            "source": ["external_payment_notice"],
            "events": ["payment_confirmed"],
            "scope": "rule_bound",
            "filters": {"rule_id": "rule-1"},
        },
        plugin_key="paid_game",
    )
    skipped = normalize_event_subscription(
        {
            "source": ["external_payment_notice"],
            "events": ["payment_confirmed"],
            "scope": "rule_bound",
            "filters": {"rule_id": "rule-2"},
        },
        plugin_key="paid_game_wrong",
    )

    matched_decision, skipped_decision = dispatch_event(
        event,
        [matched, skipped],
        {"allowed_chat_ids": "*", "trigger": {"rule_id": "rule-1"}},
    ).decisions

    assert matched_decision.matched is True
    assert skipped_decision.matched is False
    assert skipped_decision.reason_code == "scope_not_matched"


def test_known_users_scope_only_uses_state_provided_set() -> None:
    event = normalize_bot_update(
        1,
        {
            "update_id": 18,
            "message": {
                "message_id": 9,
                "text": "hi",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 3001, "first_name": "Guest"},
            },
        },
    )
    subscription = normalize_event_subscription(
        {"source": ["interaction_bot"], "events": ["message"], "scope": "known_users"},
        plugin_key="known_only",
    )

    unknown_decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100], "known_user_ids": []}).decisions[0]
    known_decision = dispatch_event(event, [subscription], {"allowed_chat_ids": [-100], "known_user_ids": [3001]}).decisions[0]

    assert unknown_decision.matched is False
    assert unknown_decision.reason_code == "scope_not_matched"
    assert known_decision.matched is True


def test_normalize_webhook_event_matches_hook_key_filter_and_trigger_shorthand() -> None:
    event = normalize_webhook_event(
        9,
        hook_key="orders",
        body={"order_id": "A-1", "status": "paid"},
        headers={"content-type": "application/json", "user-agent": "pytest"},
        body_size=39,
        content_type="application/json",
        received_at="2026-07-10T00:00:00+00:00",
    )
    filtered = normalize_event_subscription(
        {
            "source": ["webhook"],
            "events": ["webhook"],
            "scope": "all_allowed_chats",
            "filters": {"hook_key": "orders"},
            "entry_key": "main",
        },
        plugin_key="orders_plugin",
    )
    shorthand = normalize_event_subscription(
        {
            "source": ["webhook"],
            "events": ["webhook"],
            "scope": "all_allowed_chats",
            "triggers": {"webhook": "orders"},
            "entry_key": "main",
        },
        plugin_key="orders_shorthand",
    )
    skipped = normalize_event_subscription(
        {
            "source": ["webhook"],
            "events": ["webhook"],
            "scope": "all_allowed_chats",
            "filters": {"hook_key": "billing"},
            "entry_key": "main",
        },
        plugin_key="billing_plugin",
    )

    decisions = dispatch_event(event, [filtered, shorthand, skipped], {}).decisions

    assert event["source"]["channel"] == "webhook"
    assert event["source"]["display_name"] == "Webhook"
    assert event["source"]["hook_key"] == "orders"
    assert event["webhook"]["body"]["order_id"] == "A-1"
    assert event["webhook"]["headers"]["content-type"] == "application/json"
    assert decisions[0].matched is True
    assert decisions[1].matched is True
    assert decisions[2].matched is False
    assert decisions[2].reason_code == "filter_not_matched"


def _event_for_type(event_type: str) -> dict:
    if event_type == "webhook":
        return normalize_webhook_event(
            1,
            hook_key="default",
            body={"hello": "world"},
            headers={"content-type": "application/json"},
            body_size=17,
        )
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
        ("webhook", "all_allowed_chats"),
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
