"""Interaction entry result contract guard."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..event_bus import EVENT_REASON_CODES

INTERACTION_SEND_VIA = {"interaction_bot", "userbot_reply"}
INTERACTION_BUTTON_CHANNELS = {"interaction_bot"}
TRUSTED_DEFAULT_SEND_VIA = ("interaction_bot", "userbot_reply")
DEPRECATED_SEND_VIA = {"notice", "bbot_notice", "notice_bot"}
SEND_CHANNEL_DEPRECATED_REASON_CODE = "send_channel_deprecated"
assert SEND_CHANNEL_DEPRECATED_REASON_CODE in EVENT_REASON_CODES
INTERACTION_SEND_VIA_ALIASES = {
    "auto": "auto",
    "bot": "interaction_bot",
    "interaction": "interaction_bot",
    "interaction_bot": "interaction_bot",
    "userbot": "userbot_reply",
    "userbot_reply": "userbot_reply",
    "user": "userbot_reply",
    "human": "userbot_reply",
}

WriteLog = Callable[[str, str], Awaitable[None]]
EntryManifestResolver = Callable[[str | None, str | None], dict[str, Any] | None]


def action_send_via(action: dict[str, Any]) -> str:
    return (action_send_via_options(action) or ["interaction_bot"])[0]


def action_send_via_options(action: dict[str, Any]) -> list[str]:
    options = _raw_send_via_options(action)
    if options:
        return options
    return [] if _has_explicit_send_via_selector(action) else ["interaction_bot"]


def send_via_selector_options(selector: Any) -> list[str]:
    if selector is None or selector == "":
        return []
    if isinstance(selector, dict):
        raw = (
            selector.get("prefer")
            or selector.get("channels")
            or selector.get("send_via")
            or selector.get("channel")
        )
        if raw is None or raw == "":
            return []
    return _normalize_send_via_selector(selector)


def unsupported_send_via_values(selector: Any) -> list[str]:
    values: list[str] = []
    _collect_unsupported_send_via_values(selector, values)
    return values


def deprecated_send_via_values(selector: Any) -> list[str]:
    return [item for item in unsupported_send_via_values(selector) if item in DEPRECATED_SEND_VIA]


def apply_action_send_via_options(action: dict[str, Any], options: list[str]) -> dict[str, Any]:
    clean = _dedupe_valid_send_via(options)
    if not clean:
        if _has_explicit_send_via_selector(action):
            return action
        clean = ["interaction_bot"]
    action["send_via"] = clean[0]
    if len(clean) > 1:
        action["send_via_options"] = clean
    else:
        action.pop("send_via_options", None)
    action.pop("channel", None)
    action.pop("channel_selector", None)
    return action


def action_send_via_raw_selector(action: dict[str, Any]) -> Any:
    return _raw_send_via_selector(action)


def _raw_send_via_options(action: dict[str, Any]) -> list[str]:
    return _normalize_send_via_selector(_raw_send_via_selector(action))


def _raw_send_via_selector(action: dict[str, Any]) -> Any:
    selector = action.get("channel_selector")
    if selector is None and "channel" in action:
        selector = action.get("channel")
    if selector is None and "send_via_options" in action:
        selector = action.get("send_via_options")
    if selector is None and "send_via" in action:
        selector = action.get("send_via")
    return selector


def _has_explicit_send_via_selector(action: dict[str, Any]) -> bool:
    return _raw_send_via_selector(action) not in (None, "")


def _normalize_send_via_selector(selector: Any) -> list[str]:
    if selector is None or selector == "":
        return ["interaction_bot"]
    if isinstance(selector, dict):
        fallback = bool(selector.get("fallback", True))
        raw = (
            selector.get("prefer")
            or selector.get("channels")
            or selector.get("send_via")
            or selector.get("channel")
        )
        options = _normalize_send_via_selector(raw)
        return options if fallback else options[:1]
    if isinstance(selector, str):
        key = selector.strip().lower()
        mapped = INTERACTION_SEND_VIA_ALIASES.get(key)
        if mapped == "auto":
            return list(TRUSTED_DEFAULT_SEND_VIA)
        if mapped:
            return [mapped]
        return []
    if isinstance(selector, (list, tuple, set)):
        options: list[str] = []
        for item in selector:
            options.extend(_normalize_send_via_selector(item))
        return _dedupe_valid_send_via(options)
    return []


def _collect_unsupported_send_via_values(selector: Any, values: list[str]) -> None:
    if selector is None or selector == "":
        return
    if isinstance(selector, dict):
        raw = (
            selector.get("prefer")
            or selector.get("channels")
            or selector.get("send_via")
            or selector.get("channel")
        )
        _collect_unsupported_send_via_values(raw, values)
        return
    if isinstance(selector, str):
        key = selector.strip().lower()
        if key and key not in INTERACTION_SEND_VIA_ALIASES and key not in values:
            values.append(key)
        return
    if isinstance(selector, (list, tuple, set)):
        for item in selector:
            _collect_unsupported_send_via_values(item, values)


def _dedupe_valid_send_via(options: list[str]) -> list[str]:
    out: list[str] = []
    for option in options:
        item = str(option or "").strip()
        if item in INTERACTION_SEND_VIA and item not in out:
            out.append(item)
    return out


async def guard_interaction_actions(
    *,
    rule: dict[str, Any],
    actions: list[dict[str, Any]],
    resolve_entry_manifest: EntryManifestResolver,
    write_log: Callable[..., Awaitable[None]],
    session_channel: str | None = None,
    log_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize actions and emit contract warnings before delivery.

    TelePilot is a personal trusted-plugin system. ``result_contract`` is used
    as a visible contract and debug aid, not as a public-market hard sandbox.
    Invalid/removed channels still fail because they are no longer executable
    platform capabilities.
    """

    contract = _entry_result_contract(rule, resolve_entry_manifest)
    raw_actions = contract.get("actions")
    allowed_actions = (
        {str(item or "").strip() for item in raw_actions if str(item or "").strip()}
        if isinstance(raw_actions, list)
        else set()
    )
    if "send_via" in contract:
        raw_send_via = contract.get("send_via")
        raw_send_via_items = raw_send_via if isinstance(raw_send_via, list) else [raw_send_via]
        allowed_send_via = {
            option
            for item in raw_send_via_items
            for option in send_via_selector_options(item)
            if option in INTERACTION_SEND_VIA
        }
    else:
        allowed_send_via = set(TRUSTED_DEFAULT_SEND_VIA)
    normalized_session_channel = session_channel if session_channel in INTERACTION_SEND_VIA else "interaction_bot"
    context = dict(log_context or {})
    guarded: list[dict[str, Any]] = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            continue
        action = dict(raw_action)
        action_type = str(action.get("type") or "").strip()
        explicit_selector = _has_explicit_send_via_selector(action)
        if allowed_actions and action_type not in allowed_actions:
            await write_log(
                "warn",
                f"interaction action outside result_contract.actions: {action_type}",
                guard_level="warning",
                action_type=action_type,
                allowed_actions=sorted(allowed_actions),
                **context,
            )
        if action_type in {"send_message", "send_photo", "send_file", "edit_message", "edit_caption", "delete_message", "pin_message"}:
            requested_raw = action_send_via_raw_selector(action)
            requested_send_via = action_send_via_options(action) if explicit_selector else [normalized_session_channel]
            unsupported_send_via = unsupported_send_via_values(requested_raw)
            deprecated_send_via = [item for item in unsupported_send_via if item in DEPRECATED_SEND_VIA]
            if deprecated_send_via:
                await write_log(
                    "warn",
                    "interaction action failed: deprecated send_via",
                    guard_level="failed",
                    reason_code=SEND_CHANNEL_DEPRECATED_REASON_CODE,
                    action_type=action_type,
                    send_via=requested_send_via,
                    requested_send_via_raw=requested_raw,
                    unsupported_send_via=unsupported_send_via,
                    migration_hint=(
                        "notice/bbot_notice/notice_bot 已不是系统发送通道，请改用 "
                        "interaction_bot、userbot_reply 或 auto；外部转账通知 Bot 仅作为消息来源。"
                    ),
                    **context,
                )
                continue
            if unsupported_send_via and requested_send_via:
                await write_log(
                    "warn",
                    "interaction action contains unsupported send_via options",
                    guard_level="warning",
                    reason_code="unsupported_send_via",
                    action_type=action_type,
                    send_via=requested_send_via,
                    unsupported_send_via=unsupported_send_via,
                    requested_send_via_raw=requested_raw,
                    **context,
                )
            if not requested_send_via:
                await write_log(
                    "warn",
                    "interaction action failed: removed or unsupported send_via",
                    guard_level="failed",
                    reason_code="unsupported_send_via",
                    action_type=action_type,
                    requested_send_via_raw=requested_raw,
                    unsupported_send_via=unsupported_send_via,
                    migration_hint=(
                        "notice/bbot_notice/notice_bot 已不是系统发送通道，请改用 "
                        "interaction_bot、userbot_reply 或 auto；外部转账通知 Bot 仅作为消息来源。"
                    ),
                    **context,
                )
                continue
            send_via_options = list(requested_send_via)
            undeclared_send_via = [item for item in send_via_options if item not in allowed_send_via]
            if undeclared_send_via:
                await write_log(
                    "warn",
                    "interaction action outside result_contract.send_via",
                    guard_level="warning",
                    action_type=action_type,
                    send_via=requested_send_via,
                    undeclared_send_via=undeclared_send_via,
                    requested_send_via_raw=requested_raw,
                    allowed_send_via=sorted(allowed_send_via),
                    **context,
                )
            if "reply_markup" in action:
                preserve_userbot_markup = normalized_session_channel == "userbot_reply" and "userbot_reply" in send_via_options
                button_channels = [item for item in send_via_options if item in INTERACTION_BUTTON_CHANNELS]
                if preserve_userbot_markup:
                    pass
                elif button_channels:
                    if button_channels != send_via_options:
                        await write_log(
                            "info",
                            "interaction action send_via narrowed for reply_markup",
                            action_type=action_type,
                            send_via=send_via_options,
                            narrowed_send_via=button_channels,
                            **context,
                        )
                    send_via_options = button_channels
                else:
                    send_via = send_via_options[0]
                    action.pop("reply_markup", None)
                    await write_log(
                        "info",
                        "interaction action reply_markup stripped for non-bot channel",
                        action_type=action_type,
                        send_via=send_via,
                        **context,
                    )
            apply_action_send_via_options(action, send_via_options)
        guarded.append(action)
    return guarded


def _entry_result_contract(
    rule: dict[str, Any],
    resolve_entry_manifest: EntryManifestResolver,
) -> dict[str, Any]:
    module_key = str(rule.get("module_key") or "").strip() or None
    entry_key = str(rule.get("module_action") or "").strip() or None
    entry = resolve_entry_manifest(module_key, entry_key)
    contract = entry.get("result_contract") if isinstance(entry, dict) else None
    return dict(contract) if isinstance(contract, dict) else {}


__all__ = [
    "INTERACTION_BUTTON_CHANNELS",
    "INTERACTION_SEND_VIA",
    "INTERACTION_SEND_VIA_ALIASES",
    "SEND_CHANNEL_DEPRECATED_REASON_CODE",
    "TRUSTED_DEFAULT_SEND_VIA",
    "action_send_via",
    "action_send_via_options",
    "action_send_via_raw_selector",
    "apply_action_send_via_options",
    "deprecated_send_via_values",
    "guard_interaction_actions",
    "send_via_selector_options",
    "unsupported_send_via_values",
]
