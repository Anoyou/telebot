"""TelePilot interaction framework services."""

from __future__ import annotations

from typing import Any

from .contracts import guard_interaction_actions


def __getattr__(name: str) -> Any:
    if name == "InteractionDeliveryExecutor":
        from .delivery import InteractionDeliveryExecutor

        return InteractionDeliveryExecutor
    if name == "SessionRecord":
        from .session_record import SessionRecord

        return SessionRecord
    if name in {
        "run_action_batch",
        "ActionHandlers",
        "classify_action",
        "CANONICAL_ACTION_TYPES",
    }:
        from . import action_core

        return getattr(action_core, name)
    raise AttributeError(name)


__all__ = [
    "CANONICAL_ACTION_TYPES",
    "ActionHandlers",
    "InteractionDeliveryExecutor",
    "SessionRecord",
    "classify_action",
    "guard_interaction_actions",
    "run_action_batch",
]
