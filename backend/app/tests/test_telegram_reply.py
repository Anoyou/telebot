import pytest
from telethon.tl import types

from app.services.telegram_reply import (
    ReplyParameters,
    ReplyParametersBuilder,
    UnmappedReplyParameters,
)
from app.services.telegram_text import FormattedText, TextEntity


def test_bot_api_reply_builder_covers_forum_quote_and_checklist() -> None:
    result = (
        ReplyParametersBuilder(41)
        .forum_topic(9)
        .with_quote(
            FormattedText("😀quoted", (TextEntity("bold", 2, 6),)),
            position=2,
        )
        .checklist_task(3)
        .build()
        .build_bot_api()
    )
    assert result.kwargs["message_thread_id"] == 9
    params = result.kwargs["reply_parameters"]
    assert params["message_id"] == 41
    assert params["quote_position"] == 2
    assert params["quote_entities"][0]["type"] == "bold"
    assert params["checklist_task_id"] == 3
    assert not result.unmapped


def test_telethon_reply_builder_maps_quote_forum_and_todo_fields() -> None:
    result = ReplyParameters(
        message_id=41,
        message_thread_id=9,
        quote="quoted",
        quote_entities=(TextEntity("bold", 0, 6),),
        quote_position=4,
        checklist_task_id=3,
    ).build_telethon()
    reply = result.kwargs["reply_to"]
    assert isinstance(reply, types.InputReplyToMessage)
    assert reply.reply_to_msg_id == 41
    assert reply.top_msg_id == 9
    assert reply.quote_text == "quoted"
    assert reply.quote_offset == 4
    assert reply.todo_item_id == 3
    assert not result.unmapped


def test_direct_message_and_poll_fields_are_not_silently_dropped() -> None:
    result = (
        ReplyParametersBuilder(5).direct_messages_topic(12).poll_option("opaque-id").build().build_telethon()
    )
    assert result.unmapped["direct_messages_topic_id"] == 12
    assert result.unmapped["poll_option_id"] == "opaque-id"
    with pytest.raises(UnmappedReplyParameters):
        ReplyParameters(message_id=5, direct_messages_topic_id=12).to_telethon()
    bot = ReplyParameters(message_id=5, direct_messages_topic_id=12, poll_option_id="opaque-id").to_bot_api()
    assert bot["direct_messages_topic_id"] == 12
    assert bot["reply_parameters"]["poll_option_id"] == "opaque-id"


def test_unknown_reply_fields_are_kept_and_can_be_required_lossless() -> None:
    result = ReplyParameters(message_id=1, extra_fields={"future_field": True}).build_bot_api()
    assert result.unmapped == {"future_field": True}
    with pytest.raises(UnmappedReplyParameters, match="future_field"):
        result.require_lossless()


def test_parsed_unknown_reply_and_send_fields_keep_their_namespaces() -> None:
    parsed = ReplyParameters.from_bot_api(
        {
            "reply_parameters": {"message_id": 1, "future": "nested"},
            "future": "outer",
        }
    )
    assert parsed.reply_extra_fields == {"future": "nested"}
    assert parsed.send_extra_fields == {"future": "outer"}


def test_resolved_direct_message_and_poll_values_map_to_telethon() -> None:
    peer = types.InputPeerChannel(channel_id=8, access_hash=9)
    result = ReplyParameters(
        message_id=5,
        direct_messages_topic_id=12,
        poll_option_id="opaque-id",
    ).build_telethon(monoforum_peer=peer, poll_option=b"opaque-value")
    reply = result.kwargs["reply_to"]
    assert reply.monoforum_peer_id == peer
    assert reply.poll_option == b"opaque-value"
    assert not result.unmapped
