"""Shared interaction action dispatch core (Wave 5).

E1 (userbot loader) and E2 (bot delivery) keep channel-specific *handlers*, but
the batch loop, action classification, limit truncation, and control/settlement
bookkeeping shape live here so new action types cannot drift silently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import (
    SEND_CHANNEL_DEPRECATED_REASON_CODE,
    action_send_via_raw_selector,
    deprecated_send_via_values,
)

# Keep in sync with test_interaction_executor_parity.CANONICAL_ACTION_TYPES
CANONICAL_ACTION_TYPES = frozenset(
    {
        "send_message",
        "send_rich_message",
        "send_photo",
        "send_file",
        "edit_message",
        "edit_caption",
        "delete_message",
        "pin_message",
        "answer_callback",
        "answer_inline_query",
        "payout",
        "update_session",
        "start_session",
        "settlement",
        "result",
        "end_session",
        "close_session",
        "no_session",
    }
)

SESSION_CONTROL_ACTIONS = frozenset({"end_session", "close_session", "no_session"})
SEND_MEDIA_ACTIONS = frozenset({"send_photo", "send_file"})
SEND_TEXT_OR_MEDIA_ACTIONS = frozenset(
    {
        "send_message",
        "send_rich_message",
        "send_photo",
        "send_file",
        "edit_message",
        "edit_caption",
        "delete_message",
        "pin_message",
    }
)
INTERACTION_ACTION_LIMIT = 10

ActionHandler = Callable[[dict[str, Any]], Awaitable[bool]]
# True = success (or handled); False = failed (batch continues)


class ActionKind(StrEnum):
    START_SESSION = "start_session"
    UPDATE_SESSION = "update_session"
    SESSION_CONTROL = "session_control"  # end/close/no
    RESULT = "result"
    SETTLEMENT = "settlement"
    PAYOUT = "payout"
    SEND_MESSAGE = "send_message"
    SEND_RICH_MESSAGE = "send_rich_message"
    SEND_MEDIA = "send_media"
    EDIT_MESSAGE = "edit_message"
    EDIT_CAPTION = "edit_caption"
    DELETE_MESSAGE = "delete_message"
    PIN_MESSAGE = "pin_message"
    ANSWER_CALLBACK = "answer_callback"
    ANSWER_INLINE_QUERY = "answer_inline_query"
    DEPRECATED_SEND_VIA = "deprecated_send_via"
    UNSUPPORTED = "unsupported"


def action_type_of(action: dict[str, Any] | None) -> str:
    return str((action or {}).get("type") or "").strip()


def classify_action(action: dict[str, Any]) -> ActionKind:
    action_type = action_type_of(action)
    if action_type == "start_session":
        return ActionKind.START_SESSION
    if action_type == "update_session":
        return ActionKind.UPDATE_SESSION
    if action_type in SESSION_CONTROL_ACTIONS:
        return ActionKind.SESSION_CONTROL
    if action_type == "result":
        return ActionKind.RESULT
    if action_type == "settlement":
        return ActionKind.SETTLEMENT
    if action_type == "payout":
        return ActionKind.PAYOUT
    if action_type in SEND_TEXT_OR_MEDIA_ACTIONS:
        if deprecated_send_via_values(action_send_via_raw_selector(action)):
            return ActionKind.DEPRECATED_SEND_VIA
    if action_type == "send_message":
        return ActionKind.SEND_MESSAGE
    if action_type == "send_rich_message":
        return ActionKind.SEND_RICH_MESSAGE
    if action_type in SEND_MEDIA_ACTIONS:
        return ActionKind.SEND_MEDIA
    if action_type == "edit_message":
        return ActionKind.EDIT_MESSAGE
    if action_type == "edit_caption":
        return ActionKind.EDIT_CAPTION
    if action_type == "delete_message":
        return ActionKind.DELETE_MESSAGE
    if action_type == "pin_message":
        return ActionKind.PIN_MESSAGE
    if action_type == "answer_callback":
        return ActionKind.ANSWER_CALLBACK
    if action_type == "answer_inline_query":
        return ActionKind.ANSWER_INLINE_QUERY
    return ActionKind.UNSUPPORTED


@dataclass(slots=True)
class ActionHandlers:
    """Channel adapters implement only the kinds they support."""

    on_start_session: ActionHandler | None = None
    on_update_session: ActionHandler | None = None
    on_session_control: ActionHandler | None = None
    on_result: ActionHandler | None = None
    on_settlement: ActionHandler | None = None
    on_payout: ActionHandler | None = None
    on_send_message: ActionHandler | None = None
    on_send_rich_message: ActionHandler | None = None
    on_send_media: ActionHandler | None = None
    on_edit_message: ActionHandler | None = None
    on_edit_caption: ActionHandler | None = None
    on_delete_message: ActionHandler | None = None
    on_pin_message: ActionHandler | None = None
    on_answer_callback: ActionHandler | None = None
    on_answer_inline_query: ActionHandler | None = None
    on_deprecated_send_via: ActionHandler | None = None
    on_unsupported: ActionHandler | None = None
    on_truncated: ActionHandler | None = None  # actions beyond limit


@dataclass(slots=True)
class ActionBatchResult:
    executed: int = 0
    failed: int = 0
    skipped: int = 0
    dropped: int = 0
    kinds: list[str] = field(default_factory=list)


def _handler_for(kind: ActionKind, handlers: ActionHandlers) -> ActionHandler | None:
    return {
        ActionKind.START_SESSION: handlers.on_start_session,
        ActionKind.UPDATE_SESSION: handlers.on_update_session,
        ActionKind.SESSION_CONTROL: handlers.on_session_control,
        ActionKind.RESULT: handlers.on_result,
        ActionKind.SETTLEMENT: handlers.on_settlement,
        ActionKind.PAYOUT: handlers.on_payout,
        ActionKind.SEND_MESSAGE: handlers.on_send_message,
        ActionKind.SEND_RICH_MESSAGE: handlers.on_send_rich_message,
        ActionKind.SEND_MEDIA: handlers.on_send_media,
        ActionKind.EDIT_MESSAGE: handlers.on_edit_message,
        ActionKind.EDIT_CAPTION: handlers.on_edit_caption,
        ActionKind.DELETE_MESSAGE: handlers.on_delete_message,
        ActionKind.PIN_MESSAGE: handlers.on_pin_message,
        ActionKind.ANSWER_CALLBACK: handlers.on_answer_callback,
        ActionKind.ANSWER_INLINE_QUERY: handlers.on_answer_inline_query,
        ActionKind.DEPRECATED_SEND_VIA: handlers.on_deprecated_send_via,
        ActionKind.UNSUPPORTED: handlers.on_unsupported,
    }.get(kind)


async def run_action_batch(
    actions: list[dict[str, Any]],
    handlers: ActionHandlers,
    *,
    limit: int = INTERACTION_ACTION_LIMIT,
    prepare_action: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> ActionBatchResult:
    """Shared dispatch loop. Handlers return True on success, False on failure."""

    result = ActionBatchResult()
    raw_list = [item for item in (actions or []) if isinstance(item, dict)]
    kept = raw_list[: max(0, int(limit))]
    dropped = raw_list[max(0, int(limit)) :]
    result.dropped = len(dropped)

    if handlers.on_truncated is not None:
        for raw in dropped:
            action = dict(raw)
            if prepare_action is not None:
                action = prepare_action(action)
            await handlers.on_truncated(action)

    for raw in kept:
        action = dict(raw)
        if prepare_action is not None:
            action = prepare_action(action)
        kind = classify_action(action)
        result.kinds.append(kind.value)
        handler = _handler_for(kind, handlers)
        if handler is None:
            # No adapter for this kind → treat as unsupported if possible
            if handlers.on_unsupported is not None and kind != ActionKind.UNSUPPORTED:
                ok = await handlers.on_unsupported(action)
            else:
                ok = True
                result.skipped += 1
                result.executed += 1
                continue
        else:
            ok = await handler(action)
        result.executed += 1
        if ok:
            if kind in {
                ActionKind.SESSION_CONTROL,
                ActionKind.RESULT,
                ActionKind.SETTLEMENT,
            }:
                result.skipped += 1
        else:
            result.failed += 1
    return result


__all__ = [
    "CANONICAL_ACTION_TYPES",
    "INTERACTION_ACTION_LIMIT",
    "SEND_CHANNEL_DEPRECATED_REASON_CODE",
    "SEND_MEDIA_ACTIONS",
    "SEND_TEXT_OR_MEDIA_ACTIONS",
    "SESSION_CONTROL_ACTIONS",
    "ActionBatchResult",
    "ActionHandlers",
    "ActionKind",
    "action_type_of",
    "classify_action",
    "run_action_batch",
]
