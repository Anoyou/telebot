"""Reply parameters shared by Telegram Bot API and Telethon send paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .telegram_text import FormattedText, TextEntity

try:
    from telethon.tl import types as tl_types
except ImportError:  # pragma: no cover - project runtime always has Telethon
    tl_types = None  # type: ignore[assignment]


class UnmappedReplyParameters(ValueError):
    """Raised when a caller explicitly requires a lossless API mapping."""


@dataclass(frozen=True)
class ReplyBuildResult:
    """Send kwargs plus fields that need caller-specific resolution."""

    kwargs: Mapping[str, Any]
    unmapped: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kwargs", dict(self.kwargs or {}))
        object.__setattr__(self, "unmapped", dict(self.unmapped or {}))

    @property
    def parameters(self) -> Mapping[str, Any]:
        return self.kwargs

    def require_lossless(self) -> dict[str, Any]:
        if self.unmapped:
            names = ", ".join(sorted(self.unmapped))
            raise UnmappedReplyParameters(f"reply fields require explicit mapping: {names}")
        return dict(self.kwargs)


@dataclass(frozen=True)
class ReplyParameters:
    """Canonical reply description independent of a Telegram client library.

    ``message_thread_id`` and ``direct_messages_topic_id`` are send-method
    parameters in the Bot API, not members of ``reply_parameters``.  Keeping
    them here lets one builder describe the whole reply context without
    emitting an invalid Bot API object.
    """

    message_id: int | None = None
    chat_id: int | str | None = None
    ephemeral_message_id: int | None = None
    allow_sending_without_reply: bool | None = None
    quote: FormattedText | str | None = None
    quote_parse_mode: str | None = None
    quote_entities: tuple[TextEntity, ...] = ()
    quote_position: int | None = None
    message_thread_id: int | None = None
    direct_messages_topic_id: int | None = None
    checklist_task_id: int | None = None
    poll_option_id: str | None = None
    extra_fields: Mapping[str, Any] = field(default_factory=dict)
    reply_extra_fields: Mapping[str, Any] = field(default_factory=dict)
    send_extra_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.message_id is None and self.ephemeral_message_id is None:
            raise ValueError("message_id or ephemeral_message_id is required")
        for name in (
            "message_id",
            "ephemeral_message_id",
            "quote_position",
            "message_thread_id",
            "direct_messages_topic_id",
            "checklist_task_id",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.message_id is not None and self.ephemeral_message_id is not None:
            raise ValueError("message_id and ephemeral_message_id are mutually exclusive")
        entities = tuple(
            item if isinstance(item, TextEntity) else TextEntity.from_dict(item)
            for item in self.quote_entities
        )
        quote = self.quote
        if isinstance(quote, FormattedText):
            if entities:
                raise ValueError("quote_entities must not duplicate FormattedText.entities")
            entities = quote.entities
        elif quote is not None and not isinstance(quote, str):
            raise TypeError("quote must be str, FormattedText, or None")
        if entities:
            quote_text = quote.text if isinstance(quote, FormattedText) else quote
            if quote_text is None:
                raise ValueError("quote_entities require quote text")
            FormattedText(str(quote_text), entities)
        object.__setattr__(self, "quote_entities", entities)
        object.__setattr__(self, "extra_fields", dict(self.extra_fields or {}))
        object.__setattr__(self, "reply_extra_fields", dict(self.reply_extra_fields or {}))
        object.__setattr__(self, "send_extra_fields", dict(self.send_extra_fields or {}))

    @property
    def quote_text(self) -> str | None:
        if isinstance(self.quote, FormattedText):
            return self.quote.text
        return self.quote

    @classmethod
    def from_bot_api(cls, raw: Mapping[str, Any]) -> ReplyParameters:
        """Parse either a ReplyParameters object or enclosing send kwargs."""

        outer = dict(raw)
        nested = outer.pop("reply_parameters", None)
        if nested is not None and not isinstance(nested, Mapping):
            raise TypeError("reply_parameters must be a mapping")
        values = dict(nested) if isinstance(nested, Mapping) else outer
        message_thread_id = (
            outer.pop("message_thread_id", None)
            if nested is not None
            else values.pop("message_thread_id", None)
        )
        direct_topic_id = (
            outer.pop("direct_messages_topic_id", None)
            if nested is not None
            else values.pop("direct_messages_topic_id", None)
        )
        quote_entities = tuple(TextEntity.from_bot_api(item) for item in values.pop("quote_entities", ()))
        known = {
            "message_id",
            "chat_id",
            "ephemeral_message_id",
            "allow_sending_without_reply",
            "quote",
            "quote_parse_mode",
            "quote_position",
            "checklist_task_id",
            "poll_option_id",
        }
        kwargs = {key: values.pop(key) for key in tuple(values) if key in known}
        return cls(
            **kwargs,
            quote_entities=quote_entities,
            message_thread_id=message_thread_id,
            direct_messages_topic_id=direct_topic_id,
            reply_extra_fields=values,
            send_extra_fields=outer if nested is not None else {},
        )

    def build_bot_api(self) -> ReplyBuildResult:
        reply: dict[str, Any] = {}
        if self.message_id is not None:
            reply["message_id"] = self.message_id
        if self.ephemeral_message_id is not None:
            reply["ephemeral_message_id"] = self.ephemeral_message_id
        if self.chat_id is not None:
            reply["chat_id"] = self.chat_id
        if self.allow_sending_without_reply is not None:
            reply["allow_sending_without_reply"] = self.allow_sending_without_reply
        if self.quote_text is not None:
            reply["quote"] = self.quote_text
        if self.quote_entities:
            reply["quote_entities"] = [entity.to_bot_api() for entity in self.quote_entities]
        elif self.quote_parse_mode is not None:
            reply["quote_parse_mode"] = self.quote_parse_mode
        if self.quote_position is not None:
            reply["quote_position"] = self.quote_position
        if self.checklist_task_id is not None:
            reply["checklist_task_id"] = self.checklist_task_id
        if self.poll_option_id is not None:
            reply["poll_option_id"] = self.poll_option_id

        kwargs: dict[str, Any] = {"reply_parameters": reply}
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        if self.direct_messages_topic_id is not None:
            kwargs["direct_messages_topic_id"] = self.direct_messages_topic_id
        unmapped = dict(self.extra_fields)
        if self.reply_extra_fields:
            unmapped["reply_parameters"] = dict(self.reply_extra_fields)
        if self.send_extra_fields:
            unmapped["send_parameters"] = dict(self.send_extra_fields)
        if self.quote_entities and self.quote_parse_mode is not None:
            unmapped["quote_parse_mode"] = self.quote_parse_mode
        return ReplyBuildResult(kwargs, unmapped)

    def to_bot_api(self) -> dict[str, Any]:
        """Return Bot API send kwargs, raising if any field cannot be mapped."""

        return self.build_bot_api().require_lossless()

    def build_telethon(
        self,
        *,
        reply_to_peer: Any | None = None,
        monoforum_peer: Any | None = None,
        poll_option: bytes | None = None,
    ) -> ReplyBuildResult:
        if tl_types is None:
            raise RuntimeError("Telethon is not installed")
        unmapped = dict(self.extra_fields)
        if self.reply_extra_fields:
            unmapped["reply_parameters"] = dict(self.reply_extra_fields)
        if self.send_extra_fields:
            unmapped["send_parameters"] = dict(self.send_extra_fields)
        if self.ephemeral_message_id is not None:
            reply_to = tl_types.InputReplyToEphemeralMessage(self.ephemeral_message_id)
            for name, value in (
                ("allow_sending_without_reply", self.allow_sending_without_reply),
                ("quote_parse_mode", self.quote_parse_mode),
                ("message_thread_id", self.message_thread_id),
                ("direct_messages_topic_id", self.direct_messages_topic_id),
                ("checklist_task_id", self.checklist_task_id),
                ("poll_option_id", self.poll_option_id),
            ):
                if value is not None:
                    unmapped[name] = value
            return ReplyBuildResult({"reply_to": reply_to}, unmapped)

        quote_entities: list[Any] | None = None
        if self.quote_entities:
            quote_entities = [entity.to_telethon() for entity in self.quote_entities]
        reply_to = tl_types.InputReplyToMessage(
            reply_to_msg_id=int(self.message_id),
            top_msg_id=self.message_thread_id,
            reply_to_peer_id=reply_to_peer,
            quote_text=self.quote_text,
            quote_entities=quote_entities,
            quote_offset=self.quote_position,
            monoforum_peer_id=monoforum_peer,
            todo_item_id=self.checklist_task_id,
            poll_option=poll_option,
        )
        if self.chat_id is not None and reply_to_peer is None:
            unmapped["chat_id"] = self.chat_id
        if self.direct_messages_topic_id is not None and monoforum_peer is None:
            unmapped["direct_messages_topic_id"] = self.direct_messages_topic_id
        if self.poll_option_id is not None and poll_option is None:
            unmapped["poll_option_id"] = self.poll_option_id
        if self.allow_sending_without_reply is not None:
            unmapped["allow_sending_without_reply"] = self.allow_sending_without_reply
        if self.quote_parse_mode is not None:
            unmapped["quote_parse_mode"] = self.quote_parse_mode
        return ReplyBuildResult({"reply_to": reply_to}, unmapped)

    def to_telethon(
        self,
        *,
        reply_to_peer: Any | None = None,
        monoforum_peer: Any | None = None,
        poll_option: bytes | None = None,
    ) -> dict[str, Any]:
        """Return raw Telethon send-request kwargs, requiring a lossless mapping."""

        return self.build_telethon(
            reply_to_peer=reply_to_peer,
            monoforum_peer=monoforum_peer,
            poll_option=poll_option,
        ).require_lossless()


class ReplyParametersBuilder:
    """Small fluent builder for ``ReplyParameters``."""

    def __init__(self, message_id: int | None = None, **values: Any) -> None:
        self._values: dict[str, Any] = dict(values)
        if message_id is not None:
            self._values["message_id"] = message_id

    def reply_to(self, message_id: int, *, chat_id: int | str | None = None) -> ReplyParametersBuilder:
        self._values["message_id"] = message_id
        self._values["chat_id"] = chat_id
        self._values.pop("ephemeral_message_id", None)
        return self

    def forum_topic(self, message_thread_id: int) -> ReplyParametersBuilder:
        self._values["message_thread_id"] = message_thread_id
        return self

    def direct_messages_topic(self, topic_id: int) -> ReplyParametersBuilder:
        self._values["direct_messages_topic_id"] = topic_id
        return self

    def with_quote(
        self,
        quote: FormattedText | str,
        *,
        entities: Sequence[TextEntity | Mapping[str, Any]] = (),
        position: int | None = None,
        parse_mode: str | None = None,
    ) -> ReplyParametersBuilder:
        self._values.update(
            quote=quote,
            quote_entities=tuple(entities),
            quote_position=position,
            quote_parse_mode=parse_mode,
        )
        return self

    def checklist_task(self, task_id: int) -> ReplyParametersBuilder:
        self._values["checklist_task_id"] = task_id
        return self

    def poll_option(self, option_id: str) -> ReplyParametersBuilder:
        self._values["poll_option_id"] = option_id
        return self

    def preserve(self, **fields: Any) -> ReplyParametersBuilder:
        extras = dict(self._values.get("extra_fields") or {})
        extras.update(fields)
        self._values["extra_fields"] = extras
        return self

    def build(self) -> ReplyParameters:
        return ReplyParameters(**self._values)


def build_reply_parameters(message_id: int | None = None, **values: Any) -> ReplyParameters:
    return ReplyParameters(message_id=message_id, **values)
