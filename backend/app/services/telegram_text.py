"""Telegram text and rich-message value objects.

The Telegram APIs measure entity offsets and lengths in UTF-16 code units while
Python strings are indexed by Unicode code points.  This module keeps that
conversion in one place and deliberately treats unknown entities as data, so a
new Telegram entity can pass through TelePilot before the dependency is
upgraded.
"""

from __future__ import annotations

import html
import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

try:  # Telethon is a project dependency, but keep text-only imports usable.
    from telethon.tl import types as tl_types
except ImportError:  # pragma: no cover - useful for documentation tooling
    tl_types = None  # type: ignore[assignment]


class TelegramTextError(ValueError):
    """Base error for invalid Telegram text offsets or entities."""


class InvalidUtf16Boundary(TelegramTextError):
    """An offset falls between the two UTF-16 units of a supplementary code point."""


class UnknownTelegramEntity(TelegramTextError):
    """An entity cannot be reconstructed as a Telethon TL object."""


class UnsupportedRichBlock(TelegramTextError):
    """A RichBlock cannot be represented by the text-only HTML serializer."""


def utf16_length(text: str) -> int:
    """Return the number of UTF-16 code units in *text*."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    return len(text.encode("utf-16-le")) // 2


def utf16_offset_to_index(text: str, offset: int, *, strict: bool = True) -> int:
    """Convert a UTF-16 offset to a Python string index.

    Telegram never puts an entity boundary inside a surrogate pair.  In strict
    mode an invalid boundary raises instead of silently slicing half a code
    point; non-strict mode rounds to the containing Python index.
    """

    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    total = utf16_length(text)
    if offset > total:
        raise ValueError(f"offset {offset} exceeds UTF-16 length {total}")
    units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units == offset:
            return index
        if units < offset < units + width:
            if strict:
                raise InvalidUtf16Boundary(f"offset {offset} splits a surrogate pair")
            return index + 1
        units += width
    return len(text)


def python_index_to_utf16_offset(text: str, index: int) -> int:
    """Convert a Python string index to a UTF-16 offset."""

    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index > len(text):
        raise ValueError("index must be between 0 and len(text)")
    return utf16_length(text[:index])


def utf16_slice(text: str, offset: int = 0, length: int | None = None) -> str:
    """Slice *text* using Telegram's UTF-16 offset/length convention."""

    start = utf16_offset_to_index(text, offset)
    if length is None:
        end = len(text)
    else:
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ValueError("length must be a non-negative integer")
        end = utf16_offset_to_index(text, offset + length)
    return text[start:end]


_TELETHON_TO_NORMALIZED = {
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityUrl": "url",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone_number",
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityMentionName": "text_mention",
    "MessageEntityBankCard": "bank_card",
    "MessageEntityCustomEmoji": "custom_emoji",
    "MessageEntityFormattedDate": "date_time",
    "MessageEntityDiffInsert": "diff_insert",
    "MessageEntityDiffReplace": "diff_replace",
    "MessageEntityDiffDelete": "diff_delete",
    "MessageEntityUnknown": "unknown",
}
_NORMALIZED_TO_TELETHON = {value: key for key, value in _TELETHON_TO_NORMALIZED.items()}
_BOT_TO_NORMALIZED = {
    "text_link": "text_link",
    "expandable_blockquote": "blockquote",
    "phone_number": "phone_number",
    "strikethrough": "strikethrough",
    "custom_emoji": "custom_emoji",
    "date_time": "date_time",
    "bank_card": "bank_card",
}
_BOT_ENTITY_TYPES = {
    "mention",
    "hashtag",
    "cashtag",
    "bot_command",
    "url",
    "email",
    "phone_number",
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "spoiler",
    "blockquote",
    "expandable_blockquote",
    "code",
    "pre",
    "text_link",
    "text_mention",
    "custom_emoji",
    "date_time",
}

_RICH_TEXT_RAW_TYPES = {
    "TextPlain": "plain",
    "TextConcat": "concat",
    "TextBold": "bold",
    "TextItalic": "italic",
    "TextUnderline": "underline",
    "TextStrike": "strikethrough",
    "TextSpoiler": "spoiler",
    "TextFixed": "code",
    "TextMarked": "marked",
    "TextSubscript": "subscript",
    "TextSuperscript": "superscript",
    "TextDate": "date_time",
    "TextMentionName": "text_mention",
    "TextCustomEmoji": "custom_emoji",
    "TextMath": "mathematical_expression",
    "TextUrl": "url",
    "TextEmail": "email_address",
    "TextPhone": "phone_number",
    "TextBankCard": "bank_card_number",
    "TextMention": "mention",
    "TextHashtag": "hashtag",
    "TextCashtag": "cashtag",
    "TextBotCommand": "bot_command",
    "TextAutoEmail": "email_address",
    "TextAutoPhone": "phone_number",
    "TextAutoUrl": "url",
    "TextAnchor": "anchor",
    "TextDiff": "diff",
    "TextEmpty": "plain",
}
_RICH_BLOCK_RAW_TYPES = {
    "PageBlockParagraph": "paragraph",
    "PageBlockTitle": "heading",
    "PageBlockHeader": "heading",
    "PageBlockSubheader": "heading",
    "PageBlockSubtitle": "heading",
    "PageBlockKicker": "heading",
    "PageBlockPreformatted": "pre",
    "PageBlockFooter": "footer",
    "PageBlockDivider": "divider",
    "PageBlockMath": "mathematical_expression",
    "PageBlockAnchor": "anchor",
    "PageBlockList": "list",
    "PageBlockOrderedList": "list",
    "PageBlockBlockquote": "blockquote",
    "PageBlockBlockquoteBlocks": "blockquote",
    "PageBlockPullquote": "pullquote",
    "PageBlockCollage": "collage",
    "PageBlockSlideshow": "slideshow",
    "PageBlockTable": "table",
    "PageBlockDetails": "details",
    "PageBlockMap": "map",
    "PageBlockAudio": "audio",
    "PageBlockPhoto": "photo",
    "PageBlockVideo": "video",
    "PageBlockThinking": "thinking",
}


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _date_time_format(fields: Mapping[str, Any]) -> str:
    if fields.get("relative"):
        return "r"
    return "".join(
        key
        for key, flag in (
            ("w", fields.get("day_of_week")),
            ("d", fields.get("short_date")),
            ("D", fields.get("long_date")),
            ("t", fields.get("short_time")),
            ("T", fields.get("long_time")),
        )
        if flag
    )


@dataclass(frozen=True)
class TextEntity:
    """A formatting entity with UTF-16 coordinates and raw replay fields."""

    type: str
    offset: int
    length: int
    raw_type: str | None = None
    raw_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("entity type must be a non-empty string")
        if not isinstance(self.offset, int) or isinstance(self.offset, bool) or self.offset < 0:
            raise ValueError("entity offset must be a non-negative integer")
        if not isinstance(self.length, int) or isinstance(self.length, bool) or self.length < 0:
            raise ValueError("entity length must be a non-negative integer")
        object.__setattr__(self, "type", self.type.strip())
        object.__setattr__(self, "raw_type", self.raw_type or None)
        object.__setattr__(self, "raw_fields", dict(self.raw_fields or {}))

    @property
    def end(self) -> int:
        return self.offset + self.length

    @property
    def fields(self) -> Mapping[str, Any]:
        """Alias useful to callers that call the preserved fields ``data``."""

        return self.raw_fields

    @classmethod
    def from_telethon(cls, entity: Any) -> TextEntity:
        raw_type = type(entity).__name__
        values = dict(vars(entity)) if hasattr(entity, "__dict__") else {}
        if not values and hasattr(entity, "to_dict"):
            values = dict(entity.to_dict())
            values.pop("_", None)
        offset = int(values.pop("offset", getattr(entity, "offset", 0)))
        length = int(values.pop("length", getattr(entity, "length", 0)))
        normalized = _TELETHON_TO_NORMALIZED.get(
            raw_type,
            _camel_to_snake(raw_type.removeprefix("MessageEntity")),
        )
        return cls(normalized, offset, length, raw_type=raw_type, raw_fields=values)

    @classmethod
    def from_bot_api(cls, raw: Mapping[str, Any]) -> TextEntity:
        values = dict(raw)
        raw_type = str(values.pop("_", values.pop("raw_type", "")) or "") or None
        entity_type = str(values.pop("type", "unknown"))
        normalized = _BOT_TO_NORMALIZED.get(entity_type, entity_type)
        offset = int(values.pop("offset", 0))
        length = int(values.pop("length", 0))
        if entity_type == "expandable_blockquote":
            values["collapsed"] = True
        if "custom_emoji_id" in values:
            values.setdefault("document_id", values["custom_emoji_id"])
        return cls(normalized, offset, length, raw_type=raw_type, raw_fields=values)

    def to_telethon(self) -> Any:
        """Reconstruct the original Telethon entity, including optional fields."""

        if tl_types is None:
            raise UnknownTelegramEntity("Telethon is not installed")
        raw_type = self.raw_type or _NORMALIZED_TO_TELETHON.get(self.type)
        cls = getattr(tl_types, raw_type, None) if raw_type else None
        if cls is None:
            raise UnknownTelegramEntity(f"unsupported Telethon entity: {raw_type or self.type}")
        values = dict(self.raw_fields)
        if self.type == "custom_emoji":
            document_id = values.get("document_id", values.get("custom_emoji_id"))
            if document_id is not None:
                values["document_id"] = int(document_id)
        values.pop("custom_emoji_id", None)
        if self.type == "date_time" and "date" not in values and values.get("unix_time") is not None:
            values["date"] = datetime.fromtimestamp(int(values.pop("unix_time")), tz=UTC)
            date_format = str(values.pop("date_time_format", ""))
            values.update(
                relative="r" in date_format or None,
                day_of_week="w" in date_format or None,
                short_date="d" in date_format or None,
                long_date="D" in date_format or None,
                short_time="t" in date_format or None,
                long_time="T" in date_format or None,
            )
        if self.type == "text_mention" and "user_id" not in values:
            user = values.pop("user", None)
            if isinstance(user, Mapping) and user.get("id") is not None:
                values["user_id"] = int(user["id"])
        try:
            parameters = inspect.signature(cls).parameters
            unsupported = set(values) - set(parameters)
            if unsupported:
                names = ", ".join(sorted(unsupported))
                raise UnknownTelegramEntity(f"cannot replay {raw_type}; unknown fields: {names}")
            values = {key: value for key, value in values.items() if key in parameters}
            return cls(offset=self.offset, length=self.length, **values)
        except UnknownTelegramEntity:
            raise
        except (TypeError, ValueError) as exc:
            raise UnknownTelegramEntity(f"cannot replay {raw_type}: {exc}") from exc

    def to_bot_api(self) -> dict[str, Any]:
        """Return a Bot API ``MessageEntity`` dictionary without losing fields."""

        entity_type = self.type
        if entity_type == "blockquote" and self.raw_fields.get("collapsed"):
            entity_type = "expandable_blockquote"
        if entity_type == "text_link":
            api_type = "text_link"
        else:
            api_type = entity_type
        if api_type not in _BOT_ENTITY_TYPES:
            raise UnknownTelegramEntity(f"entity {self.raw_type or self.type} has no Bot API mapping")
        out: dict[str, Any] = {"type": api_type, "offset": self.offset, "length": self.length}
        fields = dict(self.raw_fields)
        if entity_type == "custom_emoji":
            document_id = fields.get("custom_emoji_id", fields.get("document_id"))
            if document_id is not None:
                out["custom_emoji_id"] = str(document_id)
        elif entity_type == "text_mention":
            if "user" not in fields:
                raise UnknownTelegramEntity("text_mention requires a resolved Bot API user object")
            out["user"] = fields["user"]
        elif entity_type == "date_time":
            date = fields.get("date")
            if isinstance(date, datetime):
                out["unix_time"] = int(date.timestamp())
            elif fields.get("unix_time") is not None:
                out["unix_time"] = int(fields["unix_time"])
            out["date_time_format"] = str(fields.get("date_time_format") or _date_time_format(fields))
        else:
            for key in ("url", "language"):
                if fields.get(key) is not None:
                    out[key] = fields[key]
        if self.raw_type is None:
            for key, value in fields.items():
                if key not in {"collapsed", "document_id"}:
                    out.setdefault(key, value)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "offset": self.offset,
            "length": self.length,
            "raw_type": self.raw_type,
            "raw_fields": dict(self.raw_fields),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TextEntity:
        if "raw_fields" in raw:
            return cls(
                str(raw.get("type") or "unknown"),
                int(raw.get("offset", 0)),
                int(raw.get("length", 0)),
                raw_type=str(raw.get("raw_type") or "") or None,
                raw_fields=raw.get("raw_fields") or {},
            )
        return cls.from_bot_api(raw)


@dataclass(frozen=True)
class FormattedText:
    """Text plus Telegram entities, safe to slice and round-trip."""

    text: str
    entities: tuple[TextEntity, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be str")
        normalized = tuple(
            entity if isinstance(entity, TextEntity) else TextEntity.from_dict(entity)
            for entity in self.entities
        )
        total = utf16_length(self.text)
        for entity in normalized:
            if entity.end > total:
                raise ValueError(f"entity {entity.type} exceeds UTF-16 text length")
            utf16_offset_to_index(self.text, entity.offset)
            utf16_offset_to_index(self.text, entity.end)
        object.__setattr__(self, "entities", normalized)

    @property
    def length(self) -> int:
        return utf16_length(self.text)

    @classmethod
    def from_telethon(cls, text: str, entities: Sequence[Any] | None = None) -> FormattedText:
        return cls(text, tuple(TextEntity.from_telethon(entity) for entity in (entities or ())))

    @classmethod
    def from_bot_api(cls, text: str, entities: Sequence[Mapping[str, Any]] | None = None) -> FormattedText:
        return cls(text, tuple(TextEntity.from_bot_api(entity) for entity in (entities or ())))

    def to_telethon(self) -> tuple[str, list[Any]]:
        return self.text, [entity.to_telethon() for entity in self.entities]

    def to_bot_api(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "entities": [entity.to_bot_api() for entity in self.entities],
        }

    def slice(self, offset: int = 0, length: int | None = None) -> FormattedText:
        """Slice text in UTF-16 units and clip intersecting entities safely."""

        if length is None:
            length = self.length - offset
        if length < 0:
            raise ValueError("length must be non-negative")
        utf16_slice(self.text, offset, length)
        end = offset + length
        clipped: list[TextEntity] = []
        for entity in self.entities:
            if entity.length == 0 and offset <= entity.offset <= end:
                clipped.append(
                    TextEntity(
                        entity.type,
                        min(entity.offset, end) - offset,
                        0,
                        raw_type=entity.raw_type,
                        raw_fields=entity.raw_fields,
                    )
                )
                continue
            left, right = max(entity.offset, offset), min(entity.end, end)
            if left >= right:
                continue
            clipped.append(
                TextEntity(
                    entity.type,
                    left - offset,
                    right - left,
                    raw_type=entity.raw_type,
                    raw_fields=entity.raw_fields,
                )
            )
        return FormattedText(utf16_slice(self.text, offset, length), tuple(clipped))

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "entities": [entity.to_dict() for entity in self.entities]}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FormattedText:
        return cls(
            str(raw.get("text") or ""), tuple(TextEntity.from_dict(item) for item in raw.get("entities", ()))
        )


@dataclass(frozen=True)
class RichText:
    """Normalized RichText node; ``plain`` nodes serialize to a string."""

    type: str = "plain"
    text: str | None = None
    children: tuple[RichText, ...] = ()
    attrs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("RichText type must not be empty")
        object.__setattr__(self, "children", tuple(self.children or ()))
        object.__setattr__(self, "attrs", dict(self.attrs or {}))

    @classmethod
    def plain(cls, text: str) -> RichText:
        return cls("plain", text=str(text))

    @classmethod
    def from_dict(cls, raw: Any) -> RichText:
        if isinstance(raw, str):
            return cls.plain(raw)
        if isinstance(raw, (list, tuple)):
            return cls("concat", children=tuple(cls.from_dict(item) for item in raw))
        if hasattr(raw, "to_dict") and not isinstance(raw, Mapping):
            raw = raw.to_dict()
        if not isinstance(raw, Mapping):
            raise TypeError("RichText must be a string, list, mapping, or TL object")
        data = dict(raw)
        raw_type = str(data.pop("type", data.pop("_", "plain")))
        node_type = _RICH_TEXT_RAW_TYPES.get(raw_type, raw_type.removeprefix("text_").lower())
        value = data.pop("text", None)
        if node_type == "concat" and value is None:
            value = data.pop("texts", ())
        if node_type == "custom_emoji" and value is None:
            value = str(data.get("alt") or "")
        if value is None and "children" in data:
            value = data.pop("children")
        if isinstance(value, (list, tuple)):
            children = tuple(cls.from_dict(item) for item in value)
            value = None
        elif value is not None and not isinstance(value, str):
            children = (cls.from_dict(value),)
            value = None
        else:
            children = ()
        return cls(node_type, text=value, children=children, attrs=data)

    def to_dict(self) -> Any:
        if self.type == "plain" and not self.attrs and not self.children:
            return self.text or ""
        if self.type == "concat" and not self.attrs:
            return [child.to_dict() for child in self.children]
        body: dict[str, Any] = {"type": self.type}
        if self.children:
            body["text"] = [child.to_dict() for child in self.children]
        elif self.text is not None:
            body["text"] = self.text
        body.update(self.attrs)
        return body

    def to_html(self) -> str:
        content = (
            "".join(child.to_html() for child in self.children)
            if self.children
            else html.escape(self.text or "")
        )
        tags = {
            "bold": "b",
            "italic": "i",
            "underline": "u",
            "strikethrough": "s",
            "strike": "s",
            "spoiler": "tg-spoiler",
            "code": "code",
            "marked": "mark",
            "subscript": "sub",
            "superscript": "sup",
        }
        tag = tags.get(self.type)
        if tag:
            return f"<{tag}>{content}</{tag}>"
        if self.type in {"url", "text_url", "anchor_link"}:
            url = html.escape(str(self.attrs.get("url") or self.attrs.get("href") or ""), quote=True)
            return f'<a href="{url}">{content}</a>' if url else content
        if self.type == "custom_emoji":
            emoji_id = html.escape(
                str(self.attrs.get("custom_emoji_id") or self.attrs.get("document_id") or ""), quote=True
            )
            return f'<tg-emoji emoji-id="{emoji_id}">{content}</tg-emoji>' if emoji_id else content
        if self.type == "date_time":
            unix_time = self.attrs.get("unix_time")
            if unix_time is None and isinstance(self.attrs.get("date"), datetime):
                unix_time = int(self.attrs["date"].timestamp())
            if unix_time is not None:
                fmt = html.escape(str(self.attrs.get("date_time_format") or ""), quote=True)
                return f'<tg-time unix="{int(unix_time)}" format="{fmt}">{content}</tg-time>'
        return content

    def to_plain_text(self) -> str:
        if self.children:
            return "".join(child.to_plain_text() for child in self.children)
        return self.text or ""


@dataclass(frozen=True)
class RichBlock:
    """Normalized RichBlock node preserving unknown attributes."""

    type: str = "paragraph"
    text: RichText | None = None
    children: tuple[RichBlock, ...] = ()
    attrs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "text",
            RichText.from_dict(self.text)
            if self.text is not None and not isinstance(self.text, RichText)
            else self.text,
        )
        object.__setattr__(self, "children", tuple(self.children or ()))
        object.__setattr__(self, "attrs", dict(self.attrs or {}))

    @classmethod
    def from_dict(cls, raw: Any) -> RichBlock:
        if hasattr(raw, "to_dict") and not isinstance(raw, Mapping):
            raw = raw.to_dict()
        if not isinstance(raw, Mapping):
            raise TypeError("RichBlock must be a mapping or TL object")
        data = dict(raw)
        raw_type = str(data.pop("type", data.pop("_", "paragraph")))
        block_type = _RICH_BLOCK_RAW_TYPES.get(raw_type, raw_type.removeprefix("page_block_").lower())
        heading_match = re.fullmatch(r"PageBlockHeading([1-6])", raw_type)
        if heading_match:
            block_type = "heading"
            data.setdefault("size", int(heading_match.group(1)))
        value = data.pop("text", None)
        children_raw = data.pop("blocks", data.pop("children", ()))
        children = (
            tuple(cls.from_dict(item) for item in children_raw)
            if isinstance(children_raw, (list, tuple))
            else ()
        )
        return cls(block_type, RichText.from_dict(value) if value is not None else None, children, data)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            body["text"] = self.text.to_dict()
        if self.children:
            body["blocks"] = [child.to_dict() for child in self.children]
        body.update(self.attrs)
        return body

    def to_html(self) -> str:
        content = (
            self.text.to_html()
            if self.text is not None
            else "".join(child.to_html() for child in self.children)
        )
        if self.type in {"paragraph", "p"}:
            return f"<p>{content}</p>"
        if self.type in {"heading", "title", "header"}:
            level = min(max(int(self.attrs.get("size", self.attrs.get("level", 1)) or 1), 1), 6)
            return f"<h{level}>{content}</h{level}>"
        if self.type in {"preformatted", "pre"}:
            language = self.attrs.get("language")
            code = (
                f'<code class="language-{html.escape(str(language), quote=True)}">{content}</code>'
                if language
                else content
            )
            return f"<pre>{code}</pre>"
        if self.type in {"blockquote", "block_quotation", "pullquote", "pull_quotation"}:
            expandable = " expandable" if self.attrs.get("expandable") or self.attrs.get("collapsed") else ""
            return f"<blockquote{expandable}>{content}</blockquote>"
        if self.type in {"divider", "hr"}:
            return "<hr>"
        if self.type == "details":
            open_attr = " open" if self.attrs.get("open") else ""
            return f"<details{open_attr}>{content}</details>"
        if self.type in {"footer", "caption"}:
            return f"<footer>{content}</footer>"
        return content

    def to_plain_text(self) -> str:
        parts: list[str] = []
        if self.text is not None:
            parts.append(self.text.to_plain_text())
        for key in ("title", "caption", "credit", "items", "rows", "cells"):
            if key in self.attrs:
                parts.append(_rich_value_to_plain_text(self.attrs[key]))
        parts.extend(child.to_plain_text() for child in self.children)
        return "\n".join(part for part in parts if part)


@dataclass(frozen=True)
class RichMessage:
    blocks: tuple[RichBlock, ...] = ()
    is_rtl: bool | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | Sequence[Any]) -> RichMessage:
        if isinstance(raw, Mapping):
            blocks = raw.get("blocks", ())
            rtl = raw.get("is_rtl", raw.get("rtl"))
        else:
            blocks, rtl = raw, None
        return cls(tuple(RichBlock.from_dict(item) for item in blocks), rtl)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"blocks": [block.to_dict() for block in self.blocks]}
        if self.is_rtl is not None:
            body["is_rtl"] = self.is_rtl
        return body

    def to_html(self) -> str:
        return "\n".join(block.to_html() for block in self.blocks)

    def to_plain_text(self) -> str:
        return "\n".join(filter(None, (block.to_plain_text() for block in self.blocks)))


def _rich_value_to_plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(filter(None, (_rich_value_to_plain_text(item) for item in value)))
    if hasattr(value, "to_dict") and not isinstance(value, Mapping):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        return ""
    raw_type = str(value.get("_") or value.get("type") or "")
    if raw_type.startswith("Text"):
        return RichText.from_dict(value).to_plain_text()
    if raw_type.startswith("PageBlock"):
        return RichBlock.from_dict(value).to_plain_text()
    parts = []
    for key in ("text", "title", "caption", "credit", "items", "rows", "cells", "blocks"):
        if key in value:
            parts.append(_rich_value_to_plain_text(value[key]))
    return "\n".join(part for part in parts if part)


def parse_rich_text(raw: Any) -> RichText:
    return RichText.from_dict(raw)


def serialize_rich_text(node: RichText | Any) -> Any:
    return RichText.from_dict(node).to_dict()


def parse_rich_block(raw: Any) -> RichBlock:
    return RichBlock.from_dict(raw)


def serialize_rich_block(node: RichBlock | Any) -> dict[str, Any]:
    return RichBlock.from_dict(node).to_dict()


def parse_rich_message(raw: Mapping[str, Any] | Sequence[Any]) -> RichMessage:
    return RichMessage.from_dict(raw)


def serialize_rich_message(message: RichMessage | Any) -> dict[str, Any]:
    return (
        RichMessage.from_dict(message).to_dict()
        if not isinstance(message, RichMessage)
        else message.to_dict()
    )


def rich_blocks_to_html(blocks: Sequence[RichBlock | Mapping[str, Any] | Any]) -> str:
    normalized = tuple(RichBlock.from_dict(block) for block in blocks)
    _validate_text_only_blocks(normalized)
    return RichMessage(normalized).to_html()


def text_only_blocks_to_html(blocks: Sequence[RichBlock | Mapping[str, Any] | Any]) -> str:
    """Serialize a text-only block list to Bot API rich-message HTML."""

    return rich_blocks_to_html(blocks)


def _validate_text_only_blocks(blocks: Sequence[RichBlock]) -> None:
    supported = {
        "paragraph",
        "p",
        "heading",
        "title",
        "header",
        "pre",
        "preformatted",
        "blockquote",
        "block_quotation",
        "pullquote",
        "pull_quotation",
        "divider",
        "hr",
        "details",
        "footer",
        "caption",
    }
    for block in blocks:
        if block.type not in supported:
            raise UnsupportedRichBlock(f"RichBlock {block.type!r} is not supported by text-only HTML")
        _validate_text_only_blocks(block.children)


# Explicit aliases make the small model convenient for callers migrating from
# ``RichTextNode``/``RichBlockNode`` terminology.
RichTextNode = RichText
RichBlockNode = RichBlock
TelegramEntity = TextEntity
