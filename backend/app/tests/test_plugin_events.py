"""Plugin event envelope helpers."""

from __future__ import annotations

from app.worker.plugins.events import (
    TP_EVENT_PAYLOAD_KEY,
    TelePilotEvent,
    attach_tp_event,
    event_from_interaction_payload,
)


def test_event_from_interaction_payload_projects_standard_envelope() -> None:
    event = event_from_interaction_payload(
        {
            "source": {
                "type": "payment_confirmed",
                "channel": "interaction_bot",
                "account_id": 1,
                "chat_id": -100123,
                "message_id": 70,
                "update_id": 10,
            },
            "message": {
                "chat_id": -100123,
                "message_id": 70,
                "text": "转账成功",
                "caption": "付款成功",
                "date": 1710000000,
                "thread_id": 7,
                "entities": [{"type": "bold", "offset": 0, "length": 4}],
                "media": {"type": "photo", "file_id": "photo-1"},
                "forward": {"sender_name": "Forwarded"},
                "sender_chat": {"id": -100999, "type": "channel", "title": "Source Channel"},
                "edited": True,
                "reply_to_message_id": 66,
            },
            "chat": {"id": -100123, "type": "supergroup"},
            "sender": {"user_id": 456, "display_name": "TransferBot", "username": "transfer_bot"},
            "actor": {"user_id": 111, "display_name": "Alice", "username": "alice"},
            "source_actor": {"user_id": 456, "display_name": "TransferBot", "username": "transfer_bot"},
            "player": {"user_id": 111, "display_name": "Alice", "username": "alice"},
            "reply_to": {"message_id": 66, "text": "+10"},
            "payment": {
                "status": "confirmed",
                "amount": 10,
                "payer_user_id": 111,
                "payer_display_name": "Alice",
                "receiver_user_id": 222,
                "receiver_display_name": "Owner",
                "source_message_id": 70,
                "reply_to_message_id": 66,
            },
            "session": {"key": "session-key", "scope": "chat", "channel": "interaction_bot", "active": True, "data": {"round": 1}},
            "trigger": {"rule_id": "paid-game", "entry_key": "start"},
            "raw": {"update_id": 10},
        }
    )

    assert event.type == "payment_confirmed"
    assert event.source_channel == "interaction_bot"
    assert event.account_id == 1
    assert event.message.chat_id == -100123
    assert event.message.chat_type == "supergroup"
    assert event.message.message_id == 70
    assert event.message.text == "转账成功"
    assert event.message.caption == "付款成功"
    assert event.message.date == 1710000000
    assert event.message.thread_id == 7
    assert event.message.entities == [{"type": "bold", "offset": 0, "length": 4}]
    assert event.message.media == {"type": "photo", "file_id": "photo-1"}
    assert event.message.forward == {"sender_name": "Forwarded"}
    assert event.message.sender_chat == {"id": -100999, "type": "channel", "title": "Source Channel"}
    assert event.message.edited is True
    assert event.message.reply_to_message_id == 66
    assert event.message.reply_to_text == "+10"
    assert event.sender.user_id == 456
    assert event.sender.display_name == "TransferBot"
    assert event.actor.user_id == 111
    assert event.actor.display_name == "Alice"
    assert event.source_actor.user_id == 456
    assert event.player.user_id == 111
    assert event.payment is not None
    assert event.payment.amount == 10
    assert event.payment.payer is not None
    assert event.payment.payer.user_id == 111
    assert event.payment.payer.display_name == "Alice"
    assert event.payment.receiver is not None
    assert event.payment.receiver.user_id == 222
    assert event.payment.receiver.display_name == "Owner"
    assert event.session is not None
    assert event.session.key == "session-key"
    assert event.session.channel == "interaction_bot"
    assert event.session.data == {"round": 1}
    assert event.trigger["entry_key"] == "start"
    assert event.raw["source_actor"]["display_name"] == "TransferBot"
    assert event.raw["player"]["display_name"] == "Alice"


def test_attach_tp_event_reuses_cached_projection_and_keeps_raw_serializable() -> None:
    payload = {
        "source": {"type": "message", "channel": "userbot", "account_id": 2, "chat_id": -42, "message_id": 9},
        "message": {"chat_id": -42, "message_id": 9, "text": "hello"},
        "chat": {"id": -42, "type": "supergroup"},
        "sender": {"user_id": 100, "display_name": "Bob"},
        "trigger": {"entry_key": "main"},
    }

    attached = attach_tp_event(payload)
    cached = attached[TP_EVENT_PAYLOAD_KEY]

    assert attached is payload
    assert isinstance(cached, TelePilotEvent)
    assert TP_EVENT_PAYLOAD_KEY not in cached.raw
    assert event_from_interaction_payload(attached) is cached


def test_attach_tp_event_and_ipc_rebuild_produce_equal_projection() -> None:
    payload = {
        "source": {
            "type": "callback_query",
            "channel": "interaction_bot",
            "account_id": 3,
            "chat_id": -100123,
            "message_id": 88,
            "callback_query_id": "cb-1",
            "callback_data": "demo:start",
        },
        "message": {
            "chat_id": -100123,
            "message_id": 88,
            "text": "请点击",
            "caption": "题面",
            "date": 1710001000,
            "thread_id": 12,
            "entities": [{"type": "bold", "offset": 0, "length": 2}],
            "media": {"type": "photo", "file_id": "ph-1"},
            "forward": {"sender_name": "Forwarded"},
            "sender_chat": {"id": -1009, "type": "channel", "title": "Source"},
            "edited": False,
            "reply_to_message_id": 77,
        },
        "chat": {"id": -100123, "type": "supergroup"},
        "sender": {"user_id": 301, "display_name": "Alice", "username": "alice"},
        "actor": {"user_id": 301, "display_name": "Alice", "username": "alice"},
        "source_actor": {"user_id": 301, "display_name": "Alice", "username": "alice"},
        "player": {"user_id": 301, "display_name": "Alice", "username": "alice"},
        "reply_to": {"message_id": 77, "text": "上一条"},
        "session": {"key": "session-1", "scope": "chat", "channel": "interaction_bot", "active": True, "data": {"round": 2}},
        "trigger": {"rule_id": "rule-1", "entry_key": "play"},
        "payment": {"status": "confirmed", "amount": 5},
    }

    direct_payload = attach_tp_event(dict(payload))
    direct_event = direct_payload[TP_EVENT_PAYLOAD_KEY]
    ipc_payload = {key: value for key, value in direct_payload.items() if key != TP_EVENT_PAYLOAD_KEY}
    rebuilt_event = event_from_interaction_payload(ipc_payload)

    assert isinstance(direct_event, TelePilotEvent)
    assert direct_event == rebuilt_event


def test_event_from_interaction_payload_projects_synthetic_callback_semantics() -> None:
    event = event_from_interaction_payload(
        {
            "source": {
                "type": "callback_query",
                "channel": "userbot",
                "synthetic": "text_button",
                "account_id": 9,
                "chat_id": -100123,
                "message_id": 501,
                "callback_data": "guess:2",
            },
            "message": {
                "chat_id": -100123,
                "message_id": 501,
                "text": "2",
                "date": 1710002000,
            },
            "chat": {"id": -100123, "type": "supergroup"},
            "sender": {"user_id": 301, "display_name": "Alice", "username": "alice"},
            "session": {"key": "session-2", "scope": "chat", "channel": "userbot", "active": True, "data": {"round": 3}},
        }
    )

    assert event.type == "callback_query"
    assert event.source_channel == "userbot"
    assert event.source_synthetic == "text_button"
    assert event.callback is not None
    assert event.callback.id is None
    assert event.callback.data == "guess:2"
    assert event.callback.synthetic == "text_button"
    assert event.callback.message == event.message
    assert event.message.text == "2"
    assert event.session is not None
    assert event.session.channel == "userbot"
    assert event.raw["source"]["synthetic"] == "text_button"
