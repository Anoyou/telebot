from __future__ import annotations

from types import SimpleNamespace

from app.services.event_probe import build_event_probe_report


def test_event_probe_report_explains_callback_subscription_and_actions() -> None:
    report = build_event_probe_report(
        trace={
            "event_type": "callback_query",
            "source_channel": "interaction_bot",
            "chat_id": -100,
            "message_id": 8,
            "sender_user_id": 1001,
        },
        raw_summary={
            "event_type": "callback_query",
            "callback_data": "game:start:42",
            "reply_markup": {
                "type": "inline_keyboard",
                "button_count": 1,
                "buttons": [{"row": 0, "col": 0, "text": "开始", "callback_data": "game:start:42"}],
            },
        },
        payload_snapshot={
            "source": {
                "type": "callback_query",
                "channel": "interaction_bot",
                "driver": "telegram_bot_api",
                "callback_query_id": "cb-1",
                "callback_data": "game:start:42",
            },
            "message": {"chat_id": -100, "message_id": 8, "text": "请选择"},
            "chat": {"id": -100, "type": "supergroup"},
            "sender": {"user_id": 1001, "display_name": "Alice"},
            "callback": {"id": "cb-1", "data": "game:start:42"},
        },
        native_raw_meta={"enabled": False, "reason_code": "native_raw_not_allowed"},
        spans=[
            SimpleNamespace(
                phase="subscription_match",
                status="ok",
                plugin_key="game",
                entry_key="start",
                reason_code="matched",
                message="订阅匹配",
                detail={"filters": {"callback_data": ["game:start:42"]}},
            )
        ],
        actions=[],
    )

    assert report["headline"] == "callback_query / interaction_bot"
    assert report["subscription_suggestions"][0]["manifest"]["events"] == ["callback_query"]
    assert report["subscription_suggestions"][0]["manifest"]["filters"]["callback_data"] == ["game:start:42"]
    assert [item["action"]["type"] for item in report["action_suggestions"]] == ["answer_callback", "edit_message"]
    assert report["routing"][0]["matched"] is True
    assert any(item["path"] == "payload.callback.data" for item in report["field_paths"])
    assert any(item["path"] == "payload.raw.reply_markup" for item in report["message_facts"])


def test_event_probe_report_warns_when_no_subscription_matched() -> None:
    report = build_event_probe_report(
        trace={"event_type": "message", "source_channel": "userbot"},
        payload_snapshot={
            "source": {"type": "message", "channel": "userbot"},
            "message": {"chat_id": -100, "message_id": 1, "text": "ping"},
            "chat": {"id": -100},
            "sender": {"user_id": 10},
        },
        spans=[
            {
                "phase": "subscription_match",
                "status": "skipped",
                "plugin_key": "hello_ping",
                "entry_key": None,
                "reason_code": "filter_not_matched",
                "message": "事件过滤条件不匹配",
            }
        ],
    )

    assert report["subscription_suggestions"][0]["manifest"]["events"] == ["message"]
    assert report["warnings"] == ["当前链路没有命中 Event Bus 订阅，优先检查 source/events/scope/filters。"]
