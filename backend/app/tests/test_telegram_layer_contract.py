"""Lock the raw Telegram Rich Message schema required by TelePilot."""

from __future__ import annotations

import inspect

from telethon import utils
from telethon.tl import alltlobjects, functions, types


def test_telethon_uses_layer_228_with_rich_message_types() -> None:
    assert alltlobjects.LAYER == 228

    for type_name in (
        "InputRichMessageHTML",
        "InputRichMessageMarkdown",
        "InputRichMessage",
        "RichMessage",
        "InputSendMessageRichMessageDraftAction",
    ):
        assert getattr(types, type_name, None) is not None


def test_telethon_rich_message_fields_are_exposed_by_raw_types() -> None:
    for request_type in (
        functions.messages.SendMessageRequest,
        functions.messages.EditMessageRequest,
        functions.messages.SaveDraftRequest,
    ):
        assert "rich_message" in inspect.signature(request_type).parameters

    assert "rich_message" in inspect.signature(types.Message).parameters
    assert "rich_message" in inspect.signature(types.InputSendMessageRichMessageDraftAction).parameters


def test_input_rich_message_html_has_stable_minimal_raw_serialization() -> None:
    rich_message = types.InputRichMessageHTML(html="x")

    assert bytes(rich_message) == bytes.fromhex("6a83cbda0000000001780000")


def test_community_peers_use_channel_marking_and_input_peer() -> None:
    community = types.Community(
        id=123,
        title="社区",
        photo=types.ChatPhotoEmpty(),
        date=None,
        access_hash=456,
    )
    forbidden = types.CommunityForbidden(id=789, title="不可访问社区", access_hash=654)

    for entity, entity_id, access_hash in (
        (community, 123, 456),
        (forbidden, 789, 654),
    ):
        assert utils.get_peer_id(entity) == -(1_000_000_000_000 + entity_id)
        input_peer = utils.get_input_peer(entity)
        assert isinstance(input_peer, types.InputPeerChannel)
        assert input_peer.channel_id == entity_id
        assert input_peer.access_hash == access_hash
