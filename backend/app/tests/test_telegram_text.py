from datetime import UTC, datetime

import pytest
from telethon.tl import types

from app.services.telegram_text import (
    FormattedText,
    InvalidUtf16Boundary,
    RichBlock,
    RichMessage,
    RichText,
    TextEntity,
    UnknownTelegramEntity,
    UnsupportedRichBlock,
    parse_rich_text,
    rich_blocks_to_html,
    utf16_length,
    utf16_offset_to_index,
    utf16_slice,
)


def test_utf16_helpers_keep_astral_code_points_intact() -> None:
    text = "a😀b"
    assert utf16_length(text) == 4
    assert utf16_offset_to_index(text, 1) == 1
    assert utf16_slice(text, 1, 2) == "😀"
    with pytest.raises(InvalidUtf16Boundary):
        utf16_offset_to_index(text, 2)


def test_formatted_text_round_trips_telethon_entities_and_utf16_slice() -> None:
    source = FormattedText.from_telethon(
        "a😀bc",
        [
            types.MessageEntityBold(offset=1, length=2),
            types.MessageEntityCustomEmoji(offset=3, length=1, document_id=777),
        ],
    )
    assert source.entities[0].offset == 1
    assert source.entities[1].raw_fields == {"document_id": 777}
    text, entities = source.to_telethon()
    assert text == source.text
    assert entities[0].to_dict() == types.MessageEntityBold(offset=1, length=2).to_dict()
    sliced = source.slice(1, 3)
    assert sliced.text == "😀b"
    assert [(item.offset, item.length) for item in sliced.entities] == [(0, 2), (2, 1)]


def test_formatted_date_blockquote_and_diff_are_replayable() -> None:
    date = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    entities = [
        TextEntity.from_telethon(types.MessageEntityFormattedDate(0, 1, date, short_time=True)),
        TextEntity.from_telethon(types.MessageEntityBlockquote(1, 1, collapsed=True)),
        TextEntity.from_telethon(types.MessageEntityDiffReplace(2, 1, old_text="old")),
    ]
    assert [item.type for item in entities] == ["date_time", "blockquote", "diff_replace"]
    assert entities[0].to_telethon().date == date
    assert entities[1].to_bot_api()["type"] == "expandable_blockquote"
    assert entities[2].to_telethon().old_text == "old"


def test_bot_api_custom_emoji_id_replays_as_telethon_integer() -> None:
    entity = TextEntity.from_bot_api(
        {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "777"}
    )
    replayed = entity.to_telethon()
    assert isinstance(replayed, types.MessageEntityCustomEmoji)
    assert replayed.document_id == 777


def test_unknown_entity_preserves_raw_identifier_and_fields() -> None:
    entity = TextEntity.from_dict(
        {
            "type": "future_entity",
            "offset": 0,
            "length": 1,
            "raw_type": "MessageEntityFutureThing",
            "raw_fields": {"flag": 3, "payload": "keep"},
        }
    )
    assert TextEntity.from_dict(entity.to_dict()).to_dict() == entity.to_dict()
    with pytest.raises(UnknownTelegramEntity):
        entity.to_telethon()


def test_entities_without_a_bot_api_equivalent_fail_explicitly() -> None:
    mention = TextEntity.from_telethon(types.MessageEntityMentionName(0, 1, user_id=7))
    diff = TextEntity.from_telethon(types.MessageEntityDiffInsert(0, 1))
    with pytest.raises(UnknownTelegramEntity, match="user object"):
        mention.to_bot_api()
    with pytest.raises(UnknownTelegramEntity, match="no Bot API mapping"):
        diff.to_bot_api()


def test_bot_api_future_entity_fields_round_trip_without_loss() -> None:
    raw = {"type": "bold", "offset": 0, "length": 1, "future_flag": "keep"}
    assert TextEntity.from_bot_api(raw).to_bot_api() == raw


def test_rich_nodes_parse_telethon_shape_and_render_text_only_blocks() -> None:
    node = parse_rich_text(types.TextBold(types.TextPlain("hello")))
    assert node == RichText("bold", children=(RichText.plain("hello"),))
    assert node.to_html() == "<b>hello</b>"
    message = RichMessage(
        blocks=(
            RichBlock("heading", RichText.plain("Title"), attrs={"size": 2}),
            RichBlock("paragraph", node),
            RichBlock("divider"),
        )
    )
    assert rich_blocks_to_html(message.blocks) == "<h2>Title</h2>\n<p><b>hello</b></p>\n<hr>"
    assert RichMessage.from_dict(message.to_dict()).to_dict() == message.to_dict()


def test_rich_block_parser_normalizes_telethon_page_blocks() -> None:
    paragraph = RichBlock.from_dict(types.PageBlockParagraph(types.TextPlain("body")))
    heading = RichBlock.from_dict(types.PageBlockHeading3(types.TextPlain("title")))
    assert paragraph.to_html() == "<p>body</p>"
    assert heading.to_html() == "<h3>title</h3>"


def test_rich_message_plain_text_includes_real_list_and_table_contents() -> None:
    message = RichMessage.from_dict(
        {
            "blocks": [
                types.PageBlockList(
                    [
                        types.PageListItemText(types.TextPlain("第一项")),
                        types.PageListItemBlocks(
                            [types.PageBlockParagraph(types.TextBold(types.TextPlain("第二项")))]
                        ),
                    ]
                ),
                types.PageBlockTable(
                    title=types.TextPlain("状态表"),
                    rows=[
                        types.PageTableRow(
                            [
                                types.PageTableCell(text=types.TextPlain("服务")),
                                types.PageTableCell(text=types.TextPlain("正常")),
                            ]
                        )
                    ],
                ),
            ]
        }
    )

    assert message.to_plain_text() == "第一项\n第二项\n状态表\n服务\n正常"


def test_text_only_html_rejects_unsupported_blocks_instead_of_dropping_them() -> None:
    with pytest.raises(UnsupportedRichBlock, match="list"):
        rich_blocks_to_html((RichBlock("list", attrs={"items": []}),))
