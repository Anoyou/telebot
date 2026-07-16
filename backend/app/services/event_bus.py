"""Event Bus helpers for trusted TelePilot plugins.

The service is intentionally framework-level and side-effect free: adapters
normalize Telegram-shaped inputs into a stable envelope, while matchers explain
why each plugin subscription is delivered or skipped. Runtime code is
responsible for invoking plugins and recording spans.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

VALID_EVENT_SOURCES = {"userbot", "interaction_bot", "external_payment_notice", "webhook"}
VALID_EVENT_TYPES = {
    "all_events",
    "all_messages",
    "message",
    "command",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "payment_confirmed",
    "keyword",
    "session_close",
    "session_expired",
    "message_edited",
    "webhook",
}
VALID_EVENT_SCOPES = {"all_allowed_chats", "owner_only", "known_users", "inline_all", "rule_bound"}
SUPPORTED_FILTER_KEYS = {
    "callback_data",
    "chat_id",
    "command",
    "commands",
    "contains",
    "event_type",
    "keyword",
    "keywords",
    "hook_key",
    "hook_keys",
    "rule_id",
}
_ALL_EVENTS_IMPLICIT_TYPES = {
    "message",
    "command",
    "callback_query",
    "keyword",
    "payment_confirmed",
    "session_close",
    "session_expired",
    "message_edited",
    "webhook",
}
EVENT_TRACE_STATUSES = {
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
}
EVENT_REASON_CODES = {
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
    "rich_message_requires_interaction_bot",
    "invalid_rich_message",
    "callback_query",
    "command_matched",
    "command_not_matched",
    "command_unauthorized",
    "contract_failed",
    "contract_warning",
    "cross_channel_duplicate",
    "claim_released_no_actions",
    "callback_query_id_missing",
    "entry_key_missing",
    "empty_message_text",
    "event_bus_delivery_disabled",
    "handler_error",
    "inline_disabled",
    "inline_query_id_missing",
    "inline_query_answer_failed",
    "interaction_rule_owned",
    "interaction_bot_not_in_chat",
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
    "target_message_id_missing",
    "telegram_api_error",
    "plugin_runtime_error",
    "trace_write_failed",
    "transfer_rule_not_matched",
    "unsupported_send_via",
    "usage_limited",
    "usage_pending",
    "userbot_command_message",
}
EVENT_MATCHED_REASON_CODE = "matched"


@dataclass(slots=True)
class EventSubscription:
    plugin_key: str
    entry_key: str | None
    sources: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    scope: str = "all_allowed_chats"
    filters: dict[str, Any] = field(default_factory=dict)
    unknown_filter_keys: list[str] = field(default_factory=list)
    unknown_events: list[str] = field(default_factory=list)
    dispatch_mode: str = "event_subscription"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SubscriptionDecision:
    plugin_key: str
    entry_key: str | None
    matched: bool
    reason_code: str
    reason_message: str
    dispatch_mode: str
    scope: str
    filters: dict[str, Any] = field(default_factory=dict)
    unknown_filter_keys: list[str] = field(default_factory=list)
    unknown_events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    subscription: EventSubscription | None = None


@dataclass(slots=True)
class DispatchResult:
    event: dict[str, Any]
    decisions: list[SubscriptionDecision]

    @property
    def matched(self) -> list[SubscriptionDecision]:
        return [item for item in self.decisions if item.matched]


def normalize_bot_update(account_id: int, update: dict[str, Any], *, channel: str = "interaction_bot") -> dict[str, Any]:
    """Normalize a Telegram Bot API update into the TelePilot event envelope."""

    update_id = _int_or_none(update.get("update_id"))
    if isinstance(update.get("inline_query"), dict):
        inline = update["inline_query"]
        sender = _user_ref(inline.get("from"))
        query = str(inline.get("query") or "")
        return _event(
            account_id=account_id,
            channel=channel,
            driver="telegram_bot_api",
            event_type="inline_query",
            update_id=update_id,
            sender=sender,
            message={"text": query},
            inline_query={
                "id": str(inline.get("id") or ""),
                "query": query,
                "offset": str(inline.get("offset") or ""),
                "chat_type": str(inline.get("chat_type") or "") or None,
                "from": sender,
            },
            raw_summary={"update_id": update_id, "event_type": "inline_query", "query": query},
            native_raw=update,
        )
    if isinstance(update.get("chosen_inline_result"), dict):
        chosen = update["chosen_inline_result"]
        sender = _user_ref(chosen.get("from"))
        query = str(chosen.get("query") or "")
        return _event(
            account_id=account_id,
            channel=channel,
            driver="telegram_bot_api",
            event_type="chosen_inline_result",
            update_id=update_id,
            sender=sender,
            message={"text": query},
            chosen_inline_result={
                "result_id": str(chosen.get("result_id") or ""),
                "query": query,
                "from": sender,
            },
            raw_summary={"update_id": update_id, "event_type": "chosen_inline_result", "query": query},
            native_raw=update,
        )
    if isinstance(update.get("callback_query"), dict):
        callback = update["callback_query"]
        msg = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        sender = _user_ref(callback.get("from"))
        text = str(msg.get("text") or msg.get("caption") or "")
        message_payload = _bot_message_payload(msg, chat=chat)
        return _event(
            account_id=account_id,
            channel=channel,
            driver="telegram_bot_api",
            event_type="callback_query",
            update_id=update_id,
            chat=_chat_ref(chat),
            sender=sender,
            message=message_payload,
            callback={
                "id": str(callback.get("id") or ""),
                "data": str(callback.get("data") or ""),
            },
            reply_to=_bot_reply_to(msg),
            raw_summary=_bot_raw_summary(
                update_id=update_id,
                event_type="callback_query",
                msg=msg,
                callback_data=callback.get("data"),
                callback_query_id=callback.get("id"),
                text=text,
            ),
            native_raw=update,
        )
    if isinstance(update.get("edited_message"), dict):
        msg = update["edited_message"]
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        sender = _bot_sender_ref(msg)
        text = str(msg.get("text") or msg.get("caption") or "")
        message_payload = _bot_message_payload(msg, chat=chat)
        return _event(
            account_id=account_id,
            channel=channel,
            driver="telegram_bot_api",
            event_type="message_edited",
            update_id=update_id,
            chat=_chat_ref(chat),
            sender=sender,
            message=message_payload,
            reply_to=_bot_reply_to(msg),
            raw_summary=_bot_raw_summary(update_id=update_id, event_type="message_edited", msg=msg, text=text),
            native_raw=update,
        )
    msg = update.get("message") if isinstance(update.get("message"), dict) else {}
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    sender = _bot_sender_ref(msg)
    text = str(msg.get("text") or msg.get("caption") or "")
    message_payload = _bot_message_payload(msg, chat=chat)
    return _event(
        account_id=account_id,
        channel=channel,
        driver="telegram_bot_api",
        event_type="message",
        update_id=update_id,
        chat=_chat_ref(chat),
        sender=sender,
        message=message_payload,
        reply_to=_bot_reply_to(msg),
        raw_summary=_bot_raw_summary(update_id=update_id, event_type="message", msg=msg, text=text),
        native_raw=update,
    )


def normalize_userbot_event(account_id: int, event: Any, *, command_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize a Telethon-like userbot event without exposing the live object."""

    message = getattr(event, "message", event)
    text = str(getattr(message, "text", None) or getattr(message, "message", None) or "")
    chat_id = _int_or_none(getattr(message, "chat_id", None) or getattr(event, "chat_id", None))
    sender_id = _int_or_none(getattr(message, "sender_id", None) or getattr(event, "sender_id", None))
    message_id = _int_or_none(getattr(message, "id", None) or getattr(event, "id", None))
    event_type = "command" if command_meta else "message"
    native_raw = _safe_to_dict(message)
    chat = _telethon_chat_ref(native_raw, message=message, event=event, chat_id=chat_id)
    sender = _telethon_sender_ref(
        raw=native_raw,
        message=message,
        event=event,
        chat_id=chat_id,
        sender_id=sender_id,
    )
    message_payload = _telethon_message_payload(
        raw=native_raw,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        chat=chat,
        sender_chat=sender.get("sender_chat") if isinstance(sender.get("sender_chat"), dict) else None,
    )
    return _event(
        account_id=account_id,
        channel="userbot",
        driver="telethon",
        event_type=event_type,
        chat=chat or {"id": chat_id},
        sender=sender,
        message=message_payload,
        reply_to=_telethon_reply_to(native_raw),
        trigger=dict(command_meta or {}),
        raw_summary=_telethon_raw_summary(
            native_raw,
            event_type=event_type,
            text=text,
            sender_chat=sender.get("sender_chat") if isinstance(sender.get("sender_chat"), dict) else None,
        ),
        native_raw=native_raw,
    )


def normalize_payment_notice(account_id: int, event: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize an external transfer notice into a payment_confirmed event."""

    payload = normalize_bot_update(account_id, event, channel="external_payment_notice")
    payload["source"]["type"] = "payment_confirmed"
    payload["event_type"] = "payment_confirmed"
    payload["payment"] = dict(parsed or {})
    payload["raw"]["event_type"] = "payment_confirmed"
    return payload


def normalize_webhook_event(
    account_id: int,
    *,
    hook_key: str,
    body: Any,
    headers: dict[str, str] | None = None,
    body_size: int | None = None,
    content_type: str | None = None,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Normalize an inbound HTTP webhook into the TelePilot Event Bus envelope."""

    normalized_hook_key = str(hook_key or "").strip()
    safe_headers = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
        if str(key).strip()
    }
    webhook = {
        "hook_key": normalized_hook_key,
        "body": body,
        "headers": safe_headers,
        "body_size": body_size,
        "content_type": content_type,
        "received_at": received_at,
    }
    webhook = {key: value for key, value in webhook.items() if value not in (None, "", [], {})}
    payload = _event(
        account_id=account_id,
        channel="webhook",
        driver="http_webhook",
        event_type="webhook",
        message={"text": _webhook_body_text(body)},
        trigger={"hook_key": normalized_hook_key},
        raw_summary={
            "event_type": "webhook",
            "hook_key": normalized_hook_key,
            "headers": safe_headers,
            "body_size": body_size,
            "content_type": content_type,
        },
        native_raw=None,
    )
    payload["source"]["hook_key"] = normalized_hook_key
    payload["webhook"] = webhook
    payload["hook_key"] = normalized_hook_key
    payload["body"] = body
    payload["headers"] = safe_headers
    return payload


def normalize_event_subscription(
    raw: dict[str, Any] | Any,
    *,
    plugin_key: str,
    entry_key: str | None = None,
) -> EventSubscription:
    """Parse one manifest event subscription into a stable matcher object."""

    data = raw if isinstance(raw, dict) else {}
    sources = _string_list(data.get("source") or data.get("sources")) or ["interaction_bot"]
    events = _string_list(data.get("events") or data.get("event")) or ["message"]
    scope = str(data.get("scope") or "all_allowed_chats").strip() or "all_allowed_chats"
    if scope not in VALID_EVENT_SCOPES:
        scope = "all_allowed_chats"
    filters = dict(data.get("filters")) if isinstance(data.get("filters"), dict) else {}
    webhook_trigger = _webhook_trigger_filter(data.get("triggers"))
    if webhook_trigger not in (None, "", [], {}):
        filters.setdefault("hook_key", webhook_trigger)
    unknown_events = sorted(
        {
            event
            for event in events
            if event and event not in VALID_EVENT_TYPES
        }
    )
    unknown_filter_keys = sorted(
        {
            str(key).strip()
            for key in filters
            if str(key).strip() and str(key).strip() not in SUPPORTED_FILTER_KEYS
        }
    )
    return EventSubscription(
        plugin_key=str(plugin_key or "").strip(),
        entry_key=str(data.get("entry_key") or entry_key or "").strip() or None,
        sources=[item for item in sources if item],
        events=[item for item in events if item],
        scope=scope,
        filters=dict(filters),
        unknown_filter_keys=unknown_filter_keys,
        unknown_events=unknown_events,
        dispatch_mode=str(data.get("dispatch_mode") or "event_subscription").strip() or "event_subscription",
        raw=dict(data),
    )


def match_subscriptions(
    event: dict[str, Any],
    subscriptions: list[EventSubscription],
    account_state: dict[str, Any] | None = None,
) -> list[SubscriptionDecision]:
    """Return matched/skipped decisions with stable reason codes."""

    state = account_state if isinstance(account_state, dict) else {}
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    chat = event.get("chat") if isinstance(event.get("chat"), dict) else {}
    event_source = str(source.get("channel") or source.get("source_channel") or "").strip()
    event_type = str(source.get("type") or event.get("event_type") or "").strip() or "message"
    chat_id = _int_or_none(message.get("chat_id") or chat.get("id") or source.get("chat_id"))
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_user_id = _int_or_none(sender.get("user_id"))
    decisions: list[SubscriptionDecision] = []
    for subscription in subscriptions:
        decision = _match_one(subscription, event, state, event_source, event_type, chat_id, sender_user_id)
        decisions.append(decision)
    return decisions


def dispatch_event(
    event: dict[str, Any],
    subscriptions: list[EventSubscription],
    account_state: dict[str, Any] | None = None,
) -> DispatchResult:
    """Match an event against candidate subscriptions."""

    return DispatchResult(event=event, decisions=match_subscriptions(event, subscriptions, account_state))


def _match_one(
    subscription: EventSubscription,
    event: dict[str, Any],
    state: dict[str, Any],
    event_source: str,
    event_type: str,
    chat_id: int | None,
    sender_user_id: int | None,
) -> SubscriptionDecision:
    base = {
        "plugin_key": subscription.plugin_key,
        "entry_key": subscription.entry_key,
        "dispatch_mode": subscription.dispatch_mode,
        "scope": subscription.scope,
        "filters": dict(subscription.filters),
        "unknown_filter_keys": list(subscription.unknown_filter_keys),
        "unknown_events": list(subscription.unknown_events),
        "warnings": _subscription_warnings(subscription),
        "subscription": subscription,
    }
    if not subscription.plugin_key:
        return SubscriptionDecision(
            matched=False,
            reason_code="plugin_not_installed",
            reason_message=_with_warnings("插件 key 为空", base["warnings"]),
            **base,
        )
    if event_source not in subscription.sources:
        return SubscriptionDecision(
            matched=False,
            reason_code="source_not_subscribed",
            reason_message=_with_warnings("事件来源未订阅", base["warnings"]),
            **base,
        )
    subscribed_events = set(subscription.events)
    if not _event_type_matches(subscribed_events, event_type):
        return SubscriptionDecision(
            matched=False,
            reason_code="event_type_not_subscribed",
            reason_message=_with_warnings("事件类型未订阅", base["warnings"]),
            **base,
        )
    scope_ok, scope_reason = _scope_matches(subscription.scope, event_type, chat_id, sender_user_id, state, subscription.filters)
    if not scope_ok:
        return SubscriptionDecision(
            matched=False,
            reason_code=scope_reason,
            reason_message=_with_warnings("事件范围不匹配", base["warnings"]),
            **base,
        )
    filter_ok, filter_reason = _filters_match(event, subscription.filters)
    if not filter_ok:
        return SubscriptionDecision(
            matched=False,
            reason_code=filter_reason,
            reason_message=_with_warnings("事件过滤条件不匹配", base["warnings"]),
            **base,
        )
    return SubscriptionDecision(
        matched=True,
        reason_code=EVENT_MATCHED_REASON_CODE,
        reason_message=_with_warnings("订阅匹配", base["warnings"]),
        **base,
    )


def _scope_matches(
    scope: str,
    event_type: str,
    chat_id: int | None,
    sender_user_id: int | None,
    state: dict[str, Any],
    filters: dict[str, Any],
) -> tuple[bool, str]:
    if scope == "inline_all":
        return (event_type in {"inline_query", "chosen_inline_result"}, "scope_not_matched")
    if event_type == "webhook" and scope == "all_allowed_chats":
        return True, "scope_not_matched"
    owner_ids = _int_set(state.get("owner_user_ids") or state.get("admin_user_ids"))
    if scope == "owner_only":
        return (sender_user_id is not None and sender_user_id in owner_ids, "scope_not_matched")
    known_ids = _int_set(state.get("known_user_ids")) | owner_ids
    if scope == "known_users":
        return (sender_user_id is not None and sender_user_id in known_ids, "scope_not_matched")
    if scope == "rule_bound":
        trigger = state.get("trigger") if isinstance(state.get("trigger"), dict) else {}
        trigger_rule_id = trigger.get("rule_id")
        if not trigger_rule_id:
            return False, "scope_not_matched"
        filter_rule_id = filters.get("rule_id")
        if filter_rule_id not in (None, "") and str(filter_rule_id) != str(trigger_rule_id):
            return False, "scope_not_matched"
        return True, "scope_not_matched"
    allowed = state.get("allowed_chat_ids")
    if allowed == "*" or allowed == ["*"]:
        return (chat_id is not None, "scope_not_matched")
    allowed_ids = _int_set(allowed)
    return (chat_id is not None and chat_id in allowed_ids, "scope_not_matched")


def _filters_match(event: dict[str, Any], filters: dict[str, Any]) -> tuple[bool, str]:
    if not filters:
        return True, "matched"
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    trigger = event.get("trigger") if isinstance(event.get("trigger"), dict) else {}
    chat = event.get("chat") if isinstance(event.get("chat"), dict) else {}
    text = str(message.get("text") or "").strip()
    keywords = _string_list(filters.get("keywords") or filters.get("keyword"))
    if keywords and text not in keywords:
        return False, "filter_not_matched"
    contains = _string_list(filters.get("contains"))
    if contains and not any(item in text for item in contains):
        return False, "filter_not_matched"
    callback_data = _string_list(filters.get("callback_data"))
    if callback_data and str(source.get("callback_data") or "") not in callback_data:
        return False, "filter_not_matched"
    commands = _string_list(filters.get("commands") or filters.get("command"))
    if commands:
        if not text:
            return False, "filter_not_matched"
        head = text.lstrip("/,").split(maxsplit=1)
        if not head or head[0] not in [item.lstrip("/,") for item in commands]:
            return False, "filter_not_matched"
    rule_ids = _string_list(filters.get("rule_id"))
    if rule_ids and str(trigger.get("rule_id") or "") not in rule_ids:
        return False, "filter_not_matched"
    event_types = _string_list(filters.get("event_type"))
    current_event_type = str(source.get("type") or event.get("event_type") or "")
    if event_types and current_event_type not in event_types:
        return False, "filter_not_matched"
    hook_keys = _string_list(filters.get("hook_keys") or filters.get("hook_key"))
    webhook = event.get("webhook") if isinstance(event.get("webhook"), dict) else {}
    current_hook_key = str(
        trigger.get("hook_key")
        or source.get("hook_key")
        or webhook.get("hook_key")
        or event.get("hook_key")
        or ""
    ).strip()
    if hook_keys and current_hook_key not in hook_keys:
        return False, "filter_not_matched"
    expected_chat_ids = {_int_or_none(item) for item in _string_list(filters.get("chat_id"))}
    expected_chat_ids.discard(None)
    current_chat_id = _int_or_none(message.get("chat_id") or chat.get("id") or source.get("chat_id"))
    if expected_chat_ids and current_chat_id not in expected_chat_ids:
        return False, "filter_not_matched"
    return True, "matched"


def _event_type_matches(subscribed_events: set[str], event_type: str) -> bool:
    if event_type in subscribed_events:
        return True
    if "all_messages" in subscribed_events and event_type in {"message", "command"}:
        return True
    if "all_events" in subscribed_events and event_type in _ALL_EVENTS_IMPLICIT_TYPES:
        return True
    return False


def event_subscription_warnings(subscription: EventSubscription) -> list[str]:
    warnings: list[str] = []
    if subscription.unknown_filter_keys:
        warnings.append(
            "filters 含未知 key: "
            f"{', '.join(subscription.unknown_filter_keys)}，该过滤条件不会生效，订阅可能匹配到预期外的事件。"
        )
    if subscription.unknown_events:
        warnings.append(
            "events 含未知类型: "
            f"{', '.join(subscription.unknown_events)}，这些事件类型不会匹配任何当前支持的事件。"
        )
    return warnings


def _subscription_warnings(subscription: EventSubscription) -> list[str]:
    return event_subscription_warnings(subscription)


def _with_warnings(message: str, warnings: list[str]) -> str:
    if not warnings:
        return message
    return f"{message}（警告：{'；'.join(warnings)}）"


def _webhook_trigger_filter(triggers: Any) -> Any:
    if not isinstance(triggers, dict):
        return None
    raw = triggers.get("webhook")
    if isinstance(raw, dict):
        return raw.get("hook_key") or raw.get("hook_keys") or raw.get("key") or raw.get("keys")
    return raw


def _webhook_body_text(body: Any) -> str:
    if body in (None, ""):
        return ""
    if isinstance(body, str):
        return body[:2000]
    if isinstance(body, bytes):
        return body[:2000].decode("utf-8", errors="replace")
    return str(body)[:2000]


def _event(
    *,
    account_id: int,
    channel: str,
    driver: str,
    event_type: str,
    update_id: int | None = None,
    chat: dict[str, Any] | None = None,
    sender: dict[str, Any] | None = None,
    message: dict[str, Any] | None = None,
    callback: dict[str, Any] | None = None,
    payment: dict[str, Any] | None = None,
    reply_to: dict[str, Any] | None = None,
    trigger: dict[str, Any] | None = None,
    inline_query: dict[str, Any] | None = None,
    chosen_inline_result: dict[str, Any] | None = None,
    raw_summary: dict[str, Any] | None = None,
    native_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "type": event_type,
        "channel": channel,
        "driver": driver,
        "account_id": account_id,
        "update_id": update_id,
        "display_name": _source_display_name(channel),
    }
    if callback:
        source["callback_query_id"] = callback.get("id")
        source["callback_data"] = callback.get("data")
    if inline_query:
        source["inline_query_id"] = inline_query.get("id")
    payload = {
        "source": source,
        "event_type": event_type,
        "message": message or {},
        "chat": chat or {},
        "sender": sender or {},
        "actor": sender or {},
        "source_actor": sender or {},
        "player": sender or {},
        "payment": payment,
        "reply_to": reply_to,
        "session": None,
        "trigger": trigger or {},
        "inline_query": inline_query,
        "chosen_inline_result": chosen_inline_result,
        "raw": raw_summary or {},
        "native_raw_meta": {"enabled": False, "reason_code": "native_raw_not_allowed"},
        "native_raw": native_raw,
    }
    return payload


def _source_display_name(channel: str) -> str:
    labels = {
        "userbot": "主号",
        "interaction_bot": "交互 Bot",
        "external_payment_notice": "记账 Bot",
        "webhook": "Webhook",
    }
    return labels.get(str(channel or "").strip(), str(channel or "").strip() or "未知来源")


def _bot_message_payload(msg: dict[str, Any], *, chat: dict[str, Any]) -> dict[str, Any]:
    reply = msg.get("reply_to_message") if isinstance(msg.get("reply_to_message"), dict) else {}
    text = str(msg.get("text") or msg.get("caption") or "")
    payload: dict[str, Any] = {
        "chat_id": _int_or_none(chat.get("id")),
        "message_id": _int_or_none(msg.get("message_id")),
        "text": text,
        "reply_to_message_id": _int_or_none(reply.get("message_id")),
    }
    optional = {
        "chat_type": str(chat.get("type") or "") or None,
        "chat_title": str(chat.get("title") or "") or None,
        "chat_username": str(chat.get("username") or "") or None,
        "caption": str(msg.get("caption") or "") or None,
        "date": msg.get("date"),
        "edited": bool(msg.get("edit_date")),
        "thread_id": _int_or_none(msg.get("message_thread_id")),
        "entities": _entity_summary(msg.get("entities")),
        "caption_entities": _entity_summary(msg.get("caption_entities")),
        "media": _bot_media_summary(msg),
        "reply_markup": _reply_markup_summary(msg.get("reply_markup")),
        "forward": _forward_summary(msg),
        "chat": _chat_ref(chat),
        "sender_chat": _chat_ref(msg.get("sender_chat")),
        "sender_tag": _first_text(msg.get("sender_tag")),
        "author_signature": _first_text(msg.get("author_signature")),
        "via_bot": _user_ref_or_none(msg.get("via_bot")),
    }
    for key, value in optional.items():
        if value not in (None, "", [], {}):
            payload[key] = value
    return payload


def _bot_reply_to(msg: dict[str, Any]) -> dict[str, Any] | None:
    reply = msg.get("reply_to_message") if isinstance(msg.get("reply_to_message"), dict) else {}
    if not reply:
        return None
    sender = _bot_sender_ref_or_none(reply)
    out = {
        "message_id": _int_or_none(reply.get("message_id")),
        "text": str(reply.get("text") or reply.get("caption") or "") or None,
        "sender": sender if sender else None,
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _bot_raw_summary(
    *,
    update_id: int | None,
    event_type: str,
    msg: dict[str, Any],
    text: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "update_id": update_id,
        "event_type": event_type,
        "text": text if text is not None else str(msg.get("text") or msg.get("caption") or ""),
        "date": msg.get("date"),
        "entities": _entity_summary(msg.get("entities")),
        "caption_entities": _entity_summary(msg.get("caption_entities")),
        "media": _bot_media_summary(msg),
        "reply_markup": _reply_markup_summary(msg.get("reply_markup")),
        "forward": _forward_summary(msg),
        "chat": _chat_ref(msg.get("chat")),
        "sender_chat": _chat_ref(msg.get("sender_chat")),
        "sender_tag": _first_text(msg.get("sender_tag")),
        "author_signature": _first_text(msg.get("author_signature")),
        "via_bot": _user_ref_or_none(msg.get("via_bot")),
        "thread_id": _int_or_none(msg.get("message_thread_id")),
        "edit_date": msg.get("edit_date"),
        "service": _service_summary(msg),
    }
    summary.update({key: value for key, value in extra.items() if value not in (None, "", [], {})})
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _telethon_message_payload(
    *,
    raw: dict[str, Any] | None,
    chat_id: int | None,
    message_id: int | None,
    text: str,
    chat: dict[str, Any] | None = None,
    sender_chat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    reply_to = data.get("reply_to") if isinstance(data.get("reply_to"), dict) else {}
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_to_message_id": _int_or_none(
            reply_to.get("reply_to_msg_id")
            or reply_to.get("reply_to_top_id")
            or data.get("reply_to_msg_id")
        ),
    }
    optional = {
        "chat_type": str(chat.get("type") or "") if chat else None,
        "chat_title": str(chat.get("title") or "") if chat else None,
        "chat_username": str(chat.get("username") or "") if chat else None,
        "caption": str(data.get("caption") or "") or None,
        "date": data.get("date"),
        "edited": bool(data.get("edit_date")),
        "thread_id": _int_or_none(reply_to.get("reply_to_top_id") or data.get("message_thread_id")),
        "entities": _entity_summary(data.get("entities")),
        "media": _native_media_summary(data.get("media")),
        "reply_markup": _reply_markup_summary(data.get("reply_markup")),
        "forward": _telethon_forward_summary(data),
        "chat": chat,
        "sender_chat": _chat_ref(data.get("sender_chat")) or sender_chat,
        "sender_tag": _first_text(data.get("from_rank"), data.get("sender_tag")),
        "author_signature": _first_text(data.get("post_author"), data.get("signature")),
        "via_bot": _user_ref_or_none(data.get("via_bot")),
    }
    for key, value in optional.items():
        if value not in (None, "", [], {}):
            payload[key] = value
    return payload


def _telethon_reply_to(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {}
    reply_to = data.get("reply_to") if isinstance(data.get("reply_to"), dict) else {}
    message_id = _int_or_none(reply_to.get("reply_to_msg_id") or reply_to.get("reply_to_top_id"))
    if message_id is None:
        return None
    return {"message_id": message_id}


def _telethon_raw_summary(
    raw: dict[str, Any] | None,
    *,
    event_type: str,
    text: str,
    sender_chat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    summary = {
        "event_type": event_type,
        "text": text,
        "date": data.get("date"),
        "entities": _entity_summary(data.get("entities")),
        "media": _native_media_summary(data.get("media")),
        "reply_markup": _reply_markup_summary(data.get("reply_markup")),
        "forward": _telethon_forward_summary(data),
        "chat": _chat_ref(data.get("chat")),
        "sender_chat": _chat_ref(data.get("sender_chat")) or sender_chat,
        "signature": str(data.get("post_author") or data.get("signature") or "") or None,
        "sender_tag": _first_text(data.get("from_rank"), data.get("sender_tag")),
        "via_bot": _user_ref_or_none(data.get("via_bot")),
        "thread_id": _int_or_none(_nested(data, "reply_to", "reply_to_top_id")),
        "edit_date": data.get("edit_date"),
        "service": str(data.get("_") or "") or None,
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _telethon_sender_ref(
    *,
    raw: dict[str, Any] | None,
    message: Any,
    event: Any,
    chat_id: int | None,
    sender_id: int | None,
) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    sender_obj = getattr(message, "sender", None) or getattr(event, "sender", None)
    signature = _first_text(
        getattr(message, "post_author", None),
        getattr(message, "signature", None),
        data.get("post_author"),
        data.get("signature"),
    )
    sender_chat = _telethon_sender_chat_ref(
        raw=data,
        sender=sender_obj,
        chat_id=chat_id,
        sender_id=sender_id,
        signature=signature,
    )
    if sender_chat and (signature or _telethon_raw_sender_is_chat(data) or _object_is_chat_sender(sender_obj)):
        is_anonymous_admin = bool(
            signature
            and _int_or_none(sender_chat.get("id")) == chat_id
            and sender_chat.get("type") in {"supergroup", "channel"}
        )
        return {
            "user_id": None,
            "display_name": signature or sender_chat.get("title") or sender_chat.get("username") or str(sender_chat.get("id") or ""),
            "username": sender_chat.get("username"),
            "sender_type": "chat",
            "sender_chat": sender_chat,
            "is_anonymous_admin": is_anonymous_admin,
            "tag": signature,
        }
    sender = {"user_id": sender_id}
    display_name = _first_text(
        getattr(sender_obj, "first_name", None),
        getattr(sender_obj, "title", None),
        getattr(sender_obj, "username", None),
    )
    username = _first_text(getattr(sender_obj, "username", None))
    if display_name:
        sender["display_name"] = display_name
    if username:
        sender["username"] = username
    tag = _first_text(getattr(message, "from_rank", None), data.get("from_rank"), data.get("sender_tag"))
    if tag:
        sender["tag"] = tag
    sender["is_anonymous_admin"] = False
    if sender_chat:
        sender["sender_type"] = "user_via_chat"
        sender["sender_chat"] = sender_chat
    return sender


def _telethon_chat_ref(
    raw: dict[str, Any] | None,
    *,
    message: Any,
    event: Any,
    chat_id: int | None,
) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {}
    raw_chat = _chat_ref(data.get("chat"))
    if raw_chat:
        return raw_chat
    chat_obj = getattr(message, "chat", None) or getattr(event, "chat", None)
    title = _first_text(
        getattr(chat_obj, "title", None),
        getattr(chat_obj, "first_name", None),
        getattr(chat_obj, "username", None),
    )
    username = _first_text(getattr(chat_obj, "username", None))
    chat_type = _object_chat_type(chat_obj) or _telethon_raw_peer_type(data.get("peer_id"))
    out = {
        "id": chat_id,
        "type": chat_type,
        "title": title,
        "username": username,
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})} or None


def _telethon_sender_chat_ref(
    *,
    raw: dict[str, Any],
    sender: Any,
    chat_id: int | None,
    sender_id: int | None,
    signature: str | None,
) -> dict[str, Any] | None:
    raw_sender_chat = _chat_ref(raw.get("sender_chat"))
    if raw_sender_chat:
        if signature:
            raw_sender_chat["signature"] = signature
        return raw_sender_chat
    if not (signature or _telethon_raw_sender_is_chat(raw) or _object_is_chat_sender(sender)):
        return None
    sender_title = _first_text(getattr(sender, "title", None))
    username = _first_text(getattr(sender, "username", None))
    sender_chat_id = sender_id or _telethon_raw_peer_id(raw.get("from_id")) or _int_or_none(getattr(sender, "id", None)) or chat_id
    chat_type = _telethon_raw_peer_type(raw.get("from_id")) or _object_chat_type(sender) or "channel"
    out = {
        "id": sender_chat_id,
        "type": chat_type,
        "title": sender_title or signature,
        "username": username,
    }
    if signature:
        out["signature"] = signature
    return {key: value for key, value in out.items() if value not in (None, "", [], {})} or None


def _telethon_raw_sender_is_chat(raw: dict[str, Any]) -> bool:
    from_id = raw.get("from_id")
    return _telethon_raw_peer_type(from_id) in {"channel", "chat"}


def _telethon_raw_peer_type(peer: Any) -> str | None:
    if isinstance(peer, dict):
        raw_type = str(peer.get("_") or peer.get("type") or "").lower()
        if "channel" in raw_type:
            return "channel"
        if "chat" in raw_type:
            return "chat"
    class_name = peer.__class__.__name__.lower() if peer is not None else ""
    if "channel" in class_name:
        return "channel"
    if "chat" in class_name:
        return "chat"
    return None


def _telethon_raw_peer_id(peer: Any) -> int | None:
    if isinstance(peer, dict):
        return _int_or_none(peer.get("channel_id") or peer.get("chat_id") or peer.get("id"))
    return _int_or_none(getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None) or getattr(peer, "id", None))


def _object_is_chat_sender(sender: Any) -> bool:
    if sender is None:
        return False
    class_name = sender.__class__.__name__.lower()
    if class_name in {"channel", "chat"} or "channel" in class_name:
        return True
    return bool(_first_text(getattr(sender, "title", None))) and not bool(
        _first_text(getattr(sender, "first_name", None), getattr(sender, "last_name", None))
    )


def _object_chat_type(sender: Any) -> str | None:
    if sender is None:
        return None
    if bool(getattr(sender, "broadcast", False)):
        return "channel"
    if bool(getattr(sender, "megagroup", False)) or bool(getattr(sender, "gigagroup", False)):
        return "supergroup"
    if _object_is_chat_sender(sender):
        return "chat"
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _user_ref(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    first = str(data.get("first_name") or "").strip()
    last = str(data.get("last_name") or "").strip()
    display = " ".join(item for item in (first, last) if item).strip()
    return {
        "user_id": _int_or_none(data.get("id")),
        "display_name": display or str(data.get("username") or "").strip() or None,
        "username": str(data.get("username") or "").strip() or None,
    }


def _bot_sender_ref(msg: dict[str, Any]) -> dict[str, Any]:
    sender_chat = _chat_ref(msg.get("sender_chat"))
    from_ref = _user_ref_or_none(msg.get("from"))
    if sender_chat and _sender_chat_is_message_author(msg):
        tag = _first_text(msg.get("sender_tag"), msg.get("author_signature"))
        return {
            "user_id": None,
            "display_name": tag or sender_chat.get("title") or sender_chat.get("username") or str(sender_chat.get("id") or ""),
            "username": sender_chat.get("username"),
            "sender_type": "chat",
            "sender_chat": sender_chat,
            "is_anonymous_admin": True,
            "tag": tag,
        }
    sender = from_ref or _user_ref(msg.get("from"))
    tag = _first_text(msg.get("sender_tag"))
    if tag:
        sender["tag"] = tag
    sender["is_anonymous_admin"] = False
    if sender_chat:
        sender["sender_type"] = "user_via_chat"
        sender["sender_chat"] = sender_chat
    return sender


def _bot_sender_ref_or_none(msg: dict[str, Any]) -> dict[str, Any] | None:
    sender = _bot_sender_ref(msg)
    clean = {key: value for key, value in sender.items() if value not in (None, "", [], {})}
    return clean or None


def _sender_chat_is_message_author(msg: dict[str, Any]) -> bool:
    sender_chat = msg.get("sender_chat") if isinstance(msg.get("sender_chat"), dict) else {}
    if not sender_chat:
        return False
    from_user = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    if not from_user:
        return True
    return bool(from_user.get("is_bot")) and (
        _int_or_none(from_user.get("id")) == 1087968824
        or str(from_user.get("username") or "").casefold() == "groupanonymousbot"
        or str(from_user.get("first_name") or "").casefold() == "groupanonymousbot"
    )


def _user_ref_or_none(raw: Any) -> dict[str, Any] | None:
    ref = _user_ref(raw)
    clean = {key: value for key, value in ref.items() if value not in (None, "", [], {})}
    return clean or None


def _chat_ref(raw: Any) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {}
    if not data:
        return None
    out = {
        "id": _int_or_none(data.get("id") or data.get("channel_id") or data.get("chat_id")),
        "type": str(data.get("type") or data.get("_") or "") or None,
        "title": str(data.get("title") or "") or None,
        "username": str(data.get("username") or "") or None,
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})} or None


def _entity_summary(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:20]:
        data = item if isinstance(item, dict) else {}
        entity_type = str(data.get("type") or data.get("_") or "").replace("MessageEntity", "").lower()
        row = {
            "type": entity_type or None,
            "offset": _int_or_none(data.get("offset")),
            "length": _int_or_none(data.get("length")),
            "url": str(data.get("url") or "") or None,
            "language": str(data.get("language") or "") or None,
            "custom_emoji_id": str(data.get("custom_emoji_id") or "") or None,
            "user_id": _int_or_none(_nested(data, "user", "id") or data.get("user_id")),
        }
        clean = {key: value for key, value in row.items() if value not in (None, "", [], {})}
        if clean:
            out.append(clean)
    return out


def _bot_media_summary(msg: dict[str, Any]) -> dict[str, Any] | None:
    for key in (
        "photo",
        "document",
        "video",
        "audio",
        "voice",
        "sticker",
        "animation",
        "video_note",
        "contact",
        "location",
        "venue",
        "poll",
        "dice",
        "game",
        "web_page",
        "story",
    ):
        value = msg.get(key)
        if value:
            return _media_value_summary(key, value)
    return None


def _native_media_summary(raw: Any) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {}
    if not data:
        return None
    media_type = str(data.get("_") or data.get("type") or "media")
    return _media_value_summary(media_type, data)


def _media_value_summary(media_type: str, raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        data = raw[-1] if raw and isinstance(raw[-1], dict) else {}
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {}
    fields = {
        "type": media_type,
        "file_id": data.get("file_id"),
        "file_unique_id": data.get("file_unique_id"),
        "file_name": data.get("file_name"),
        "mime_type": data.get("mime_type"),
        "file_size": _int_or_none(data.get("file_size") or data.get("size")),
        "width": _int_or_none(data.get("width") or data.get("w")),
        "height": _int_or_none(data.get("height") or data.get("h")),
        "duration": _int_or_none(data.get("duration")),
        "emoji": data.get("emoji"),
        "question": data.get("question"),
    }
    return {key: value for key, value in fields.items() if value not in (None, "", [], {})}


def _reply_markup_summary(raw: Any) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {}
    if not data:
        return None
    rows = data.get("inline_keyboard") or data.get("rows") or data.get("keyboard")
    if not isinstance(rows, list):
        return {"type": str(data.get("_") or data.get("type") or "reply_markup")}
    buttons: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows[:12]):
        cells = row if isinstance(row, list) else row.get("buttons") if isinstance(row, dict) else []
        if not isinstance(cells, list):
            continue
        for col_index, button in enumerate(cells[:8]):
            btn = button if isinstance(button, dict) else {}
            text = str(btn.get("text") or "") or None
            callback_data = btn.get("callback_data") or btn.get("data")
            if isinstance(callback_data, bytes):
                callback_data = callback_data.decode("utf-8", "replace")
            item = {
                "row": row_index,
                "col": col_index,
                "text": text,
                "callback_data": str(callback_data or "") or None,
                "url": str(btn.get("url") or "") or None,
            }
            clean = {key: value for key, value in item.items() if value not in (None, "", [], {})}
            if clean:
                buttons.append(clean)
    return {"type": "inline_keyboard", "button_count": len(buttons), "buttons": buttons} if buttons else None


def _forward_summary(msg: dict[str, Any]) -> dict[str, Any] | None:
    origin = msg.get("forward_origin") if isinstance(msg.get("forward_origin"), dict) else {}
    if origin:
        sender_user = _user_ref_or_none(origin.get("sender_user"))
        sender_chat = _chat_ref(origin.get("chat") or origin.get("sender_chat"))
        out = {
            "type": str(origin.get("type") or "") or None,
            "sender_user": sender_user if sender_user else None,
            "sender_chat": sender_chat,
            "sender_name": str(origin.get("sender_user_name") or "") or None,
            "date": origin.get("date"),
        }
        return {key: value for key, value in out.items() if value not in (None, "", [], {})} or None
    if msg.get("forward_from") or msg.get("forward_from_chat") or msg.get("forward_sender_name"):
        out = {
            "sender_user": _user_ref_or_none(msg.get("forward_from")),
            "sender_chat": _chat_ref(msg.get("forward_from_chat")),
            "sender_name": str(msg.get("forward_sender_name") or "") or None,
            "date": msg.get("forward_date"),
        }
        return {key: value for key, value in out.items() if value not in (None, "", [], {})} or None
    return None


def _telethon_forward_summary(raw: dict[str, Any]) -> dict[str, Any] | None:
    fwd = raw.get("fwd_from") if isinstance(raw.get("fwd_from"), dict) else {}
    if not fwd:
        return None
    out = {
        "from_id": fwd.get("from_id"),
        "from_name": fwd.get("from_name"),
        "channel_post": fwd.get("channel_post"),
        "date": fwd.get("date"),
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})} or None


def _service_summary(msg: dict[str, Any]) -> dict[str, Any] | None:
    service_keys = [
        "new_chat_members",
        "left_chat_member",
        "pinned_message",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "forum_topic_created",
        "forum_topic_closed",
        "forum_topic_reopened",
    ]
    present = [key for key in service_keys if msg.get(key) not in (None, "", [], {})]
    return {"types": present} if present else None


def _nested(source: dict[str, Any], *path: str) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out
    text = str(value or "").strip()
    return [text] if text else []


def _int_set(value: Any) -> set[int]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    out: set[int] = set()
    for item in values:
        parsed = _int_or_none(item)
        if parsed is not None:
            out.add(parsed)
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_to_dict(value: Any) -> dict[str, Any] | None:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict()
            if inspect.isawaitable(raw):
                close = getattr(raw, "close", None)
                if callable(close):
                    close()
                return None
            return raw if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


__all__ = [
    "DispatchResult",
    "EVENT_MATCHED_REASON_CODE",
    "EVENT_REASON_CODES",
    "EVENT_TRACE_STATUSES",
    "EventSubscription",
    "SUPPORTED_FILTER_KEYS",
    "SubscriptionDecision",
    "VALID_EVENT_SCOPES",
    "VALID_EVENT_SOURCES",
    "VALID_EVENT_TYPES",
    "dispatch_event",
    "event_subscription_warnings",
    "match_subscriptions",
    "normalize_bot_update",
    "normalize_event_subscription",
    "normalize_payment_notice",
    "normalize_webhook_event",
    "normalize_userbot_event",
]
