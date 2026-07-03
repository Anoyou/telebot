"""TelePilot-level plugin event objects.

These dataclasses describe the framework-facing shape of Telegram activity.
MTProto events and Bot API updates are drivers; plugins should prefer these
stable TelePilot references when they do not need driver-specific details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TP_EVENT_PAYLOAD_KEY = "tp_event"


@dataclass(slots=True)
class ActorRef:
    user_id: int | None = None
    display_name: str | None = None
    username: str | None = None


@dataclass(slots=True)
class MessageRef:
    chat_id: int | None = None
    message_id: int | None = None
    text: str = ""
    caption: str | None = None
    date: Any = None
    chat_type: str | None = None
    thread_id: int | None = None
    entities: list[dict[str, Any]] = field(default_factory=list)
    media: dict[str, Any] | None = None
    forward: dict[str, Any] | None = None
    sender_chat: dict[str, Any] | None = None
    edited: bool = False
    reply_to_message_id: int | None = None
    reply_to_text: str | None = None


@dataclass(slots=True)
class CallbackRef:
    id: str | None = None
    data: str | None = None
    synthetic: str | None = None
    message: MessageRef | None = None


@dataclass(slots=True)
class PaymentRef:
    status: str | None = None
    amount: int | None = None
    currency: str | None = None
    payer: ActorRef | None = None
    receiver: ActorRef | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionRef:
    key: str | None = None
    scope: str | None = None
    channel: str | None = None
    active: bool = True
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TelePilotEvent:
    type: str
    source_channel: str = "interaction_bot"
    source_synthetic: str | None = None
    account_id: int | None = None
    message: MessageRef = field(default_factory=MessageRef)
    sender: ActorRef = field(default_factory=ActorRef)
    actor: ActorRef = field(default_factory=ActorRef)
    source_actor: ActorRef = field(default_factory=ActorRef)
    player: ActorRef = field(default_factory=ActorRef)
    callback: CallbackRef | None = None
    payment: PaymentRef | None = None
    session: SessionRef | None = None
    trigger: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def event_from_interaction_payload(payload: dict[str, Any]) -> TelePilotEvent:
    """Build a TelePilot event from the current interaction payload envelope."""

    data = payload if isinstance(payload, dict) else {}
    snapshot = _payload_snapshot(data)
    existing = data.get(TP_EVENT_PAYLOAD_KEY)
    if isinstance(existing, TelePilotEvent) and existing.raw == snapshot:
        return existing

    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    message_raw = data.get("message") if isinstance(data.get("message"), dict) else {}
    chat_raw = data.get("chat") if isinstance(data.get("chat"), dict) else {}
    sender_raw = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    actor_raw = data.get("actor") if isinstance(data.get("actor"), dict) else {}
    source_actor_raw = data.get("source_actor") if isinstance(data.get("source_actor"), dict) else {}
    player_raw = data.get("player") if isinstance(data.get("player"), dict) else {}
    payment_raw = data.get("payment") if isinstance(data.get("payment"), dict) else None
    session_raw = data.get("session") if isinstance(data.get("session"), dict) else None
    trigger_raw = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
    reply_to = data.get("reply_to") if isinstance(data.get("reply_to"), dict) else {}

    message = MessageRef(
        chat_id=_int_or_none(message_raw.get("chat_id") or chat_raw.get("id") or source.get("chat_id") or data.get("chat_id")),
        message_id=_int_or_none(message_raw.get("message_id") or source.get("message_id") or data.get("message_id")),
        text=str(message_raw.get("text") or source.get("text") or data.get("message_text") or ""),
        caption=str(message_raw.get("caption") or data.get("message_caption") or "") or None,
        date=message_raw.get("date"),
        chat_type=str(chat_raw.get("type") or source.get("chat_type") or "") or None,
        thread_id=_int_or_none(message_raw.get("thread_id") or data.get("message_thread_id")),
        entities=_entity_list(message_raw.get("entities") or message_raw.get("caption_entities")),
        media=_dict_or_none(message_raw.get("media")),
        forward=_dict_or_none(message_raw.get("forward")),
        sender_chat=_dict_or_none(message_raw.get("sender_chat")),
        edited=bool(message_raw.get("edited") or str(source.get("type") or data.get("event_type") or "") == "message_edited"),
        reply_to_message_id=_int_or_none(
            message_raw.get("reply_to_message_id") or reply_to.get("message_id") or data.get("reply_to_message_id")
        ),
        reply_to_text=str(reply_to.get("text") or data.get("reply_to_text") or "") or None,
    )
    sender = _actor_from_payload(sender_raw, data)
    actor = _actor_from_payload(actor_raw, data)
    source_actor = _actor_from_payload(source_actor_raw, data)
    player = _actor_from_payload(player_raw, {})
    source_synthetic = str(source.get("synthetic") or "") or None
    callback_id = str(source.get("callback_query_id") or data.get("callback_query_id") or "") or None
    callback_data = str(source.get("callback_data") or data.get("callback_data") or "") or None
    callback = (
        CallbackRef(id=callback_id, data=callback_data, synthetic=source_synthetic, message=message)
        if callback_id or callback_data or source_synthetic
        else None
    )

    payment = None
    if isinstance(payment_raw, dict):
        payer_raw = payment_raw.get("payer") if isinstance(payment_raw.get("payer"), dict) else {}
        receiver_raw = payment_raw.get("receiver") if isinstance(payment_raw.get("receiver"), dict) else {}
        payment = PaymentRef(
            status=str(payment_raw.get("status") or "") or None,
            amount=_int_or_none(payment_raw.get("amount")),
            currency=str(payment_raw.get("currency") or "") or None,
            payer=ActorRef(
                user_id=_int_or_none(payer_raw.get("user_id") or payment_raw.get("payer_user_id")),
                display_name=str(
                    payer_raw.get("display_name")
                    or payment_raw.get("payer_display_name")
                    or payment_raw.get("payer_name")
                    or ""
                ) or None,
                username=str(payer_raw.get("username") or "") or None,
            ),
            receiver=ActorRef(
                user_id=_int_or_none(receiver_raw.get("user_id") or payment_raw.get("receiver_user_id")),
                display_name=str(
                    receiver_raw.get("display_name")
                    or payment_raw.get("receiver_display_name")
                    or payment_raw.get("receiver_name")
                    or ""
                ) or None,
                username=str(receiver_raw.get("username") or "") or None,
            ),
            raw=dict(payment_raw),
        )

    session = None
    if isinstance(session_raw, dict):
        session = SessionRef(
            key=str(session_raw.get("key") or "") or None,
            scope=str(session_raw.get("scope") or "") or None,
            channel=str(session_raw.get("channel") or "") or None,
            active=bool(session_raw.get("active", True)),
            data=dict(session_raw.get("data") or {}) if isinstance(session_raw.get("data"), dict) else {},
        )

    return TelePilotEvent(
        type=str(source.get("type") or data.get("event_type") or "message"),
        source_channel=str(source.get("channel") or source.get("bot_role") or "interaction_bot"),
        source_synthetic=source_synthetic,
        account_id=_int_or_none(source.get("account_id") or data.get("account_id")),
        message=message,
        sender=sender,
        actor=actor,
        source_actor=source_actor,
        player=player,
        callback=callback,
        payment=payment,
        session=session,
        trigger=dict(trigger_raw),
        raw=snapshot,
    )


def attach_tp_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach a reusable TelePilotEvent object for in-process plugin invocation."""

    if not isinstance(payload, dict):
        raise TypeError("payload 必须是 dict")
    payload[TP_EVENT_PAYLOAD_KEY] = event_from_interaction_payload(payload)
    return payload


def _actor_from_payload(raw: dict[str, Any], fallback: dict[str, Any]) -> ActorRef:
    return ActorRef(
        user_id=_int_or_none(raw.get("user_id") or fallback.get("sender_user_id")),
        display_name=str(raw.get("display_name") or fallback.get("sender_name") or "") or None,
        username=str(raw.get("username") or fallback.get("sender_username") or "") or None,
    )


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    clean = {key: item for key, item in value.items() if item not in (None, "", [], {})}
    return clean or None


def _entity_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        clean = {key: entry for key, entry in item.items() if entry not in (None, "", [], {})}
        if clean:
            out.append(clean)
    return out


def _payload_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != TP_EVENT_PAYLOAD_KEY}


__all__ = [
    "ActorRef",
    "CallbackRef",
    "MessageRef",
    "PaymentRef",
    "SessionRef",
    "TelePilotEvent",
    "TP_EVENT_PAYLOAD_KEY",
    "attach_tp_event",
    "event_from_interaction_payload",
]
