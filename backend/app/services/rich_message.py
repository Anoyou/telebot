"""Provider-neutral validation for Telegram-style rich-message inputs.

This module owns protocol limits shared by Bot API and MTProto adapters.  The
text limit here is measured in UTF-8 bytes.  Channel adapters may additionally
enforce channel-specific limits (for example, a Bot API character limit)
without weakening these protocol checks.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

RICH_MESSAGE_TEXT_BYTE_LIMIT = 32_768
RICH_MESSAGE_BLOCK_LIMIT = 500
RICH_MESSAGE_NESTING_LIMIT = 16
RICH_MESSAGE_MEDIA_LIMIT = 50
RICH_MESSAGE_TABLE_COLUMN_LIMIT = 20
RICH_MESSAGE_JSON_BYTE_LIMIT = 1_048_576

_ALLOWED_KEYS = frozenset(
    {
        "blocks",
        "html",
        "markdown",
        "media",
        "is_rtl",
        "skip_entity_detection",
    }
)
_TEXT_FIELDS = frozenset({"alternative_text", "caption", "credit", "expression", "label", "summary", "text"})
_MEDIA_BLOCK_TYPES = frozenset({"animation", "audio", "photo", "video", "voice_note"})


class RichMessageFormat(StrEnum):
    HTML = "html"
    MARKDOWN = "markdown"
    BLOCKS = "blocks"


class RichMessageValidationError(ValueError):
    """A stable validation failure consumable by every channel adapter."""

    code = "invalid_rich_message"


@dataclass(frozen=True, slots=True)
class InputRichMessage:
    """Validated, provider-neutral rich-message input.

    Mutable JSON values are defensively copied both on construction and when
    returned through :meth:`to_dict`, so callers cannot invalidate a previously
    checked payload by mutating their original object.
    """

    format: RichMessageFormat
    content: str | list[dict[str, Any]]
    media: list[Any] | None = None
    is_rtl: bool | None = None
    skip_entity_detection: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {self.format.value: deepcopy(self.content)}
        if self.media is not None:
            payload["media"] = deepcopy(self.media)
        if self.is_rtl is not None:
            payload["is_rtl"] = self.is_rtl
        if self.skip_entity_detection is not None:
            payload["skip_entity_detection"] = self.skip_entity_detection
        return payload


def build_input_rich_message(
    raw: Any,
    *,
    text_limit_unit: Literal["utf8_bytes", "characters"] = "utf8_bytes",
) -> InputRichMessage:
    """Validate an official-style ``InputRichMessage`` object.

    Exactly one of ``html``, ``markdown`` and ``blocks`` is required.  Unknown
    fields and non-JSON values are rejected rather than being silently dropped.
    """

    if isinstance(raw, InputRichMessage):
        raw = raw.to_dict()
    if not isinstance(raw, dict):
        raise RichMessageValidationError("rich_message 必须是对象")
    if any(not isinstance(key, str) for key in raw):
        raise RichMessageValidationError("rich_message 对象字段名必须是字符串")

    unsupported = sorted(set(raw) - _ALLOWED_KEYS)
    if unsupported:
        raise RichMessageValidationError(f"rich_message 包含不支持的字段：{', '.join(unsupported)}")

    selected: list[RichMessageFormat] = []
    for message_format in RichMessageFormat:
        key = message_format.value
        value = raw.get(key)
        if message_format in {RichMessageFormat.HTML, RichMessageFormat.MARKDOWN}:
            if value in (None, ""):
                continue
            if not isinstance(value, str) or not value.strip():
                raise RichMessageValidationError(f"rich_message.{key} 必须是非空字符串")
        else:
            if value in (None, []):
                continue
            if not isinstance(value, list) or not value:
                raise RichMessageValidationError("rich_message.blocks 必须是非空数组")
        selected.append(message_format)
    if len(selected) != 1:
        raise RichMessageValidationError("rich_message 必须且只能提供 html、markdown、blocks 其中一个")

    for flag in ("is_rtl", "skip_entity_detection"):
        value = raw.get(flag)
        if value is not None and not isinstance(value, bool):
            raise RichMessageValidationError(f"rich_message.{flag} 必须是布尔值")

    media = raw.get("media")
    if media is not None:
        if not isinstance(media, list):
            raise RichMessageValidationError("rich_message.media 必须是数组")
        if len(media) > RICH_MESSAGE_MEDIA_LIMIT:
            raise RichMessageValidationError(f"rich_message.media 最多 {RICH_MESSAGE_MEDIA_LIMIT} 项")
        if selected[0] is RichMessageFormat.BLOCKS and media:
            raise RichMessageValidationError("rich_message.media 只可与 html 或 markdown 一起使用")

    stats = {"text_bytes": 0, "text_characters": 0, "blocks": 0, "media": len(media or [])}
    content = raw[selected[0].value]
    _validate_json_node(
        content,
        depth=0,
        stats=stats,
        text_field=selected[0] in {RichMessageFormat.HTML, RichMessageFormat.MARKDOWN},
    )
    if media is not None:
        _validate_json_node(media, depth=0, stats=stats)
    if selected[0] is RichMessageFormat.BLOCKS:
        _validate_blocks(content, stats=stats)

    if text_limit_unit == "utf8_bytes":
        if stats["text_bytes"] > RICH_MESSAGE_TEXT_BYTE_LIMIT:
            raise RichMessageValidationError(
                f"rich_message 文本最多 {RICH_MESSAGE_TEXT_BYTE_LIMIT} bytes（按 UTF-8 编码）"
            )
    elif text_limit_unit == "characters":
        if stats["text_characters"] > RICH_MESSAGE_TEXT_BYTE_LIMIT:
            raise RichMessageValidationError(f"rich_message 文本最多 {RICH_MESSAGE_TEXT_BYTE_LIMIT} 个字符")
    else:  # pragma: no cover - typed callers cannot reach this branch
        raise ValueError(f"unsupported rich message text limit unit: {text_limit_unit}")
    if stats["blocks"] > RICH_MESSAGE_BLOCK_LIMIT:
        raise RichMessageValidationError(f"rich_message 最多 {RICH_MESSAGE_BLOCK_LIMIT} 个结构块")
    if stats["media"] > RICH_MESSAGE_MEDIA_LIMIT:
        raise RichMessageValidationError(f"rich_message 最多 {RICH_MESSAGE_MEDIA_LIMIT} 个媒体附件")

    normalized: dict[str, Any] = {selected[0].value: deepcopy(content)}
    for key in ("media", "is_rtl", "skip_entity_detection"):
        if key in raw:
            normalized[key] = deepcopy(raw[key])
    try:
        encoded_size = len(
            json.dumps(
                normalized,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise RichMessageValidationError("rich_message 必须可以序列化为标准 JSON") from exc
    if encoded_size > RICH_MESSAGE_JSON_BYTE_LIMIT:
        raise RichMessageValidationError("rich_message JSON 超过 1 MiB 限制")

    return InputRichMessage(
        format=selected[0],
        content=deepcopy(content),
        media=deepcopy(media),
        is_rtl=raw.get("is_rtl"),
        skip_entity_detection=raw.get("skip_entity_detection"),
    )


def normalize_rich_message(
    raw: Any,
    *,
    text_limit_unit: Literal["utf8_bytes", "characters"] = "utf8_bytes",
) -> dict[str, Any]:
    """Return the normalized JSON representation of a validated input."""

    return build_input_rich_message(raw, text_limit_unit=text_limit_unit).to_dict()


def _validate_json_node(
    value: Any,
    *,
    depth: int,
    stats: dict[str, int],
    text_field: bool = False,
) -> None:
    if isinstance(value, str):
        if text_field:
            stats["text_bytes"] += len(value.encode("utf-8"))
            stats["text_characters"] += len(value)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RichMessageValidationError("rich_message 只能包含标准 JSON 数字")
        return
    if isinstance(value, list):
        next_depth = depth + 1
        if next_depth > RICH_MESSAGE_NESTING_LIMIT:
            raise RichMessageValidationError(f"rich_message 最多嵌套 {RICH_MESSAGE_NESTING_LIMIT} 层")
        for item in value:
            _validate_json_node(item, depth=next_depth, stats=stats, text_field=text_field)
        return
    if not isinstance(value, dict):
        raise RichMessageValidationError("rich_message 只能包含 JSON 类型")

    next_depth = depth + 1
    if next_depth > RICH_MESSAGE_NESTING_LIMIT:
        raise RichMessageValidationError(f"rich_message 最多嵌套 {RICH_MESSAGE_NESTING_LIMIT} 层")
    for key, item in value.items():
        if not isinstance(key, str):
            raise RichMessageValidationError("rich_message 对象字段名必须是字符串")
        _validate_json_node(
            item,
            depth=next_depth,
            stats=stats,
            text_field=key in _TEXT_FIELDS,
        )


def _validate_blocks(blocks: Any, *, stats: dict[str, int]) -> None:
    if not isinstance(blocks, list):
        raise RichMessageValidationError("rich_message.blocks 必须是数组")
    stats["blocks"] += len(blocks)
    for block in blocks:
        if not isinstance(block, dict):
            raise RichMessageValidationError("rich_message.blocks 每一项必须是对象")
        block_type = str(block.get("type") or "").strip()
        if block_type in _MEDIA_BLOCK_TYPES:
            stats["media"] += 1

        nested_blocks = block.get("blocks")
        if nested_blocks is not None:
            _validate_blocks(nested_blocks, stats=stats)

        if block_type == "list":
            items = block.get("items")
            if not isinstance(items, list):
                raise RichMessageValidationError("rich_message 列表 items 必须是数组")
            stats["blocks"] += len(items)
            for item in items:
                if not isinstance(item, dict):
                    raise RichMessageValidationError("rich_message 列表项必须是对象")
                _validate_blocks(item.get("blocks"), stats=stats)

        if block_type == "table":
            rows = block.get("cells")
            if not isinstance(rows, list):
                raise RichMessageValidationError("rich_message 表格 cells 必须是二维数组")
            stats["blocks"] += len(rows)
            for row in rows:
                if not isinstance(row, list):
                    raise RichMessageValidationError("rich_message 表格每一行必须是数组")
                columns = 0
                for cell in row:
                    if not isinstance(cell, dict):
                        raise RichMessageValidationError("rich_message 表格单元格必须是对象")
                    colspan = cell.get("colspan", 1)
                    if not isinstance(colspan, int) or isinstance(colspan, bool) or colspan < 1:
                        raise RichMessageValidationError("rich_message 表格 colspan 必须是正整数")
                    columns += colspan
                if columns > RICH_MESSAGE_TABLE_COLUMN_LIMIT:
                    raise RichMessageValidationError(
                        f"rich_message 表格每行最多 {RICH_MESSAGE_TABLE_COLUMN_LIMIT} 列"
                    )
