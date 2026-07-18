"""Recording replay helpers for plugin/event pipelines.

WARNING: default replay dispatch loads account plugins through the normal
``AsyncSessionLocal`` path. Run it only against a development database.

Replay is intentionally isolated from Telegram and durable ledgers:
- inbound recording is disabled while replaying;
- Telegram Bot API and Telethon clients are mocked;
- action events are captured in memory instead of written to DB/Redis;
- delivery is forced through dry-run paths.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..services import account_bot_runtime, account_bot_service, action_tap
from ..services.interaction import delivery as delivery_mod
from .plugins import loader

ReplayDispatch = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class ReplayResult:
    source: Path | None
    account_id: int | None
    envelope_count: int
    action_events: list[dict[str, Any]]


def load_recording(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL recording file into normalized inbound envelopes."""

    source = Path(path)
    envelopes: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}:{lineno} is not valid JSON") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{source}:{lineno} must be a JSON object")
            envelopes.append(item)
    return envelopes


async def replay_recording(
    path: str | Path,
    *,
    account_id: int | None = None,
    token: str = "replay-token",
    dispatch: ReplayDispatch | None = None,
) -> ReplayResult:
    """Replay one recording file and return captured action_event payloads."""

    source = Path(path)
    envelopes = load_recording(source)
    result = await replay_envelopes(envelopes, account_id=account_id, token=token, dispatch=dispatch)
    result.source = source
    return result


async def replay_envelopes(
    envelopes: list[dict[str, Any]],
    *,
    account_id: int | None = None,
    token: str = "replay-token",
    dispatch: ReplayDispatch | None = None,
) -> ReplayResult:
    """Replay normalized inbound envelopes through the selected dispatcher."""

    resolved_account_id = account_id or _account_id_from_envelopes(envelopes)
    capture = _ActionEventCapture()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(_replay_runtime_patches(capture))
        if dispatch is None:
            dispatcher = _DefaultReplayDispatcher(account_id=resolved_account_id, token=token)
            await stack.enter_async_context(dispatcher)
            dispatch_func = dispatcher.dispatch
        else:
            dispatch_func = dispatch
        await stack.enter_async_context(_force_loader_dry_run(resolved_account_id))
        for envelope in envelopes:
            await dispatch_func(_force_envelope_dry_run(envelope))
    return ReplayResult(
        source=None,
        account_id=resolved_account_id,
        envelope_count=len(envelopes),
        action_events=list(capture.events),
    )


class _ActionEventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(
        self,
        *,
        account_id: int | None,
        action: dict[str, Any] | None,
        status: str,
        channel: str | None = None,
        session_key: str | None = None,
        plugin_key: str | None = None,
        entry_key: str | None = None,
        error_code: str | None = None,
        error: Any = None,
        result: Any = None,
        redis: Any | None = None,  # noqa: ARG002
    ) -> SimpleNamespace | None:
        account = _int_or_none(account_id)
        normalized_status = _normalize_status(status)
        if account is None or normalized_status is None:
            return None
        payload = dict(action or {})
        context = action_tap.action_context(payload)
        action_type = str(payload.get("type") or payload.get("action_type") or "unknown").strip() or "unknown"
        event = {
            "account_id": account,
            "channel": _first_text(channel, payload.get("actual_send_via"), payload.get("send_via"), payload.get("channel")),
            "session_key": _first_text(session_key, context.get("session_key"), payload.get("session_key")),
            "plugin_key": _first_text(plugin_key, context.get("plugin_key")),
            "entry_key": _first_text(entry_key, context.get("entry_key")),
            "action_type": action_type,
            "params_summary": action_tap.summarize_action_params(payload, result=result),
            "status": normalized_status,
            "error_code": _first_text(error_code),
            "error_summary": str(error)[: action_tap.ACTION_TAP_ERROR_LIMIT] if error not in (None, "") else None,
        }
        self.events.append(event)
        return SimpleNamespace(**event)


class _DefaultReplayDispatcher:
    def __init__(self, *, account_id: int | None, token: str) -> None:
        self.account_id = account_id
        self.token = token
        self.redis = _ReplayRedis()
        self.client = _ReplayTelegramClient()
        self._previous_states: dict[int, Any] = {}
        self._loaded_accounts: set[int] = set()

    async def __aenter__(self) -> _DefaultReplayDispatcher:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for account_id, previous in self._previous_states.items():
            if previous is _MISSING:
                loader._STATES.pop(account_id, None)  # noqa: SLF001
            else:
                loader._STATES[account_id] = previous  # noqa: SLF001

    async def dispatch(self, envelope: dict[str, Any]) -> None:
        account_id = self.account_id or _account_id_from_envelope(envelope)
        if account_id is None:
            raise ValueError("replay envelope is missing account_id")
        source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
        channel = str(source.get("channel") or source.get("observed_channel") or "").strip()
        if channel == "userbot":
            await self._dispatch_userbot(account_id, envelope)
            return
        await self._dispatch_interaction_bot(account_id, envelope)

    async def _dispatch_userbot(self, account_id: int, envelope: dict[str, Any]) -> None:
        await self._ensure_userbot_loaded(account_id)
        handler = self.client.incoming_message_handler
        if handler is None:
            raise RuntimeError("userbot replay dispatcher was not registered")
        await handler(_userbot_event_from_envelope(envelope))

    async def _ensure_userbot_loaded(self, account_id: int) -> None:
        if account_id in self._loaded_accounts:
            return
        self._previous_states[account_id] = loader._STATES.get(account_id, _MISSING)  # noqa: SLF001
        paused = asyncio.Event()
        paused.set()
        await loader.load_plugins_for_account(
            self.client,
            account_id=account_id,
            paused=paused,
            redis=self.redis,
        )
        _force_state_contexts_dry_run(account_id)
        self._loaded_accounts.add(account_id)

    async def _dispatch_interaction_bot(self, account_id: int, envelope: dict[str, Any]) -> None:
        if _can_replay_interaction_module(envelope):
            incoming = _incoming_from_envelope(account_id, self.token, envelope)
            rule = _rule_from_envelope(envelope)
            await account_bot_runtime._run_interaction_module(
                incoming,
                rule,
                parsed=_parsed_from_envelope(envelope),
                event_type=str(envelope.get("event_type") or (envelope.get("source") or {}).get("type") or "message"),
                cfg=_force_config_dry_run({}),
            )
            return
        update = _bot_update_from_envelope(envelope)
        await account_bot_runtime._handle_interaction_update(account_id, self.token, update)


class _ReplayTelegramClient:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.sent: list[dict[str, Any]] = []

    def on(self, _event_builder: Any) -> Callable[[Any], Any]:
        def _wrap(fn: Any) -> Any:
            self.handlers.append(fn)
            return fn

        return _wrap

    @property
    def incoming_message_handler(self) -> Any | None:
        return self.handlers[-1] if self.handlers else None

    async def send_message(self, chat_id: Any = None, message: Any = None, *args: Any, **kwargs: Any) -> Any:
        if chat_id is None and "entity" in kwargs:
            chat_id = kwargs.pop("entity")
        text = message if message is not None else kwargs.pop("message", None)
        item = {"type": "send_message", "chat_id": _int_or_none(chat_id), "text": text, "dry_run": True}
        self.sent.append(item)
        return SimpleNamespace(id=len(self.sent), message_id=len(self.sent), chat_id=item["chat_id"], dry_run=True)

    async def edit_message(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append({"type": "edit_message", "args": args, "kwargs": kwargs, "dry_run": True})
        return SimpleNamespace(id=len(self.sent), message_id=len(self.sent), dry_run=True)

    async def delete_messages(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.sent.append({"type": "delete_messages", "args": args, "kwargs": kwargs, "dry_run": True})
        return []

    async def pin_message(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append({"type": "pin_message", "args": args, "kwargs": kwargs, "dry_run": True})
        return SimpleNamespace(dry_run=True)

    async def send_file(self, *args: Any, **kwargs: Any) -> Any:
        self.sent.append({"type": "send_file", "args": args, "kwargs": kwargs, "dry_run": True})
        return SimpleNamespace(id=len(self.sent), message_id=len(self.sent), dry_run=True)


class _ReplayRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 0

    async def rpush(self, key: str, value: str) -> int:
        values = self.lists.setdefault(str(key), [])
        values.append(value)
        return len(values)

    async def lpush(self, key: str, value: str) -> int:
        values = self.lists.setdefault(str(key), [])
        values.insert(0, value)
        return len(values)

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        values = self.lists.setdefault(str(key), [])
        self.lists[str(key)] = values[start : end + 1]
        return True

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(str(key), [])
        return values[start : end + 1]

    async def expire(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    async def get(self, key: str, *_args: Any, **_kwargs: Any) -> str | None:
        return self.values.get(str(key))

    async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
        self.values[str(key)] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if str(key) in self.values:
                removed += 1
                self.values.pop(str(key), None)
        return removed

    async def keys(self, pattern: str) -> list[str]:
        return [key for key in self.values if fnmatch.fnmatch(key, pattern)]

    async def scan_iter(self, match: str):
        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key

    async def script_load(self, *_args: Any, **_kwargs: Any) -> str:
        return "replay-sha"

    async def evalsha(self, *_args: Any, **_kwargs: Any) -> list[int]:
        return [1, 0, 0]

    def pubsub(self) -> _ReplayPubSub:
        return _ReplayPubSub()


class _ReplayPubSub:
    async def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def unsubscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def get_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _ReplayUserbotEvent:
    def __init__(self, envelope: dict[str, Any]) -> None:
        source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
        message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        chat = envelope.get("chat") if isinstance(envelope.get("chat"), dict) else {}
        sender = envelope.get("sender") if isinstance(envelope.get("sender"), dict) else {}
        self.raw_text = str(message.get("text") or source.get("text") or "")
        self.text = self.raw_text
        self.chat_id = _int_or_none(message.get("chat_id") or chat.get("id") or source.get("chat_id"))
        self.sender_id = _int_or_none(sender.get("user_id") or source.get("sender_user_id"))
        self.id = _int_or_none(message.get("message_id") or source.get("message_id"))
        self.message_id = self.id
        self.outgoing = False
        chat_type = str(message.get("chat_type") or chat.get("type") or "").strip()
        self.is_private = chat_type == "private" or (self.chat_id is not None and self.chat_id == self.sender_id)
        self.is_group = chat_type in {"group", "supergroup"} or (self.chat_id is not None and self.chat_id < 0)
        self.is_channel = chat_type == "channel"
        self.message = SimpleNamespace(
            text=self.text,
            message=self.text,
            rich_message=message.get("rich_message"),
            text_source=str(message.get("text_source") or "message"),
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            id=self.id,
            to_dict=lambda: dict(envelope.get("native_raw") or envelope.get("raw") or {}),
        )

    async def get_chat(self) -> Any:
        return SimpleNamespace(id=self.chat_id)


def _userbot_event_from_envelope(envelope: dict[str, Any]) -> _ReplayUserbotEvent:
    return _ReplayUserbotEvent(envelope)


@asynccontextmanager
async def _replay_runtime_patches(capture: _ActionEventCapture):
    redis = _ReplayRedis()
    patches: list[tuple[Any, str, Any]] = []

    async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _noop_inbound(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _start_trace(event: Any) -> SimpleNamespace:
        trace_id = str((event if isinstance(event, dict) else {}).get("trace_id") or f"evt_replay_{int(time.time() * 1000)}")
        return SimpleNamespace(trace_id=trace_id, account_id=_account_id_from_envelope(event) if isinstance(event, dict) else None)

    async def _record_action(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _run_worker_entry(incoming: Any, *, plugin_key: str, entry_key: str, payload: dict[str, Any]) -> tuple[bool, str | None, list[dict[str, Any]]]:
        try:
            actions = await loader.invoke_interaction_entry(
                incoming.account_id,
                plugin_key=plugin_key,
                entry_key=entry_key,
                payload=payload,
            )
            return True, None, actions
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}", []

    async def _run_worker_action(_incoming: Any, *, payload: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any]]:
        return True, None, {"dry_run": True, **dict(payload or {})}

    async def _send_message(_token: str, chat_id: Any, text: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"message_id": 1, "chat_id": _int_or_none(chat_id), "text": str(text or ""), "dry_run": True}

    async def _edit_message(_token: str, chat_id: Any, message_id: Any, text: Any = None, **_kwargs: Any) -> dict[str, Any]:
        return {
            "message_id": _int_or_none(message_id) or 1,
            "chat_id": _int_or_none(chat_id),
            "text": str(text or ""),
            "dry_run": True,
        }

    async def _edit_caption(_token: str, chat_id: Any, message_id: Any, caption: Any = None, **_kwargs: Any) -> dict[str, Any]:
        return {"message_id": _int_or_none(message_id) or 1, "chat_id": _int_or_none(chat_id), "caption": caption, "dry_run": True}

    async def _send_photo(_token: str, chat_id: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"message_id": 1, "chat_id": _int_or_none(chat_id), "dry_run": True}

    async def _delete_message(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _answer_callback(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _answer_inline_query(*_args: Any, **_kwargs: Any) -> bool:
        return True

    def _patch(obj: Any, name: str, value: Any) -> None:
        patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    for module in (action_tap, loader, account_bot_runtime, delivery_mod):
        if hasattr(module, "emit_action_event"):
            _patch(module, "emit_action_event", capture.emit)
        if hasattr(module, "record_action"):
            _patch(module, "record_action", _record_action)
        if hasattr(module, "record_span"):
            _patch(module, "record_span", _noop_async)
        if hasattr(module, "start_trace"):
            _patch(module, "start_trace", _start_trace)
        if hasattr(module, "finish_trace"):
            _patch(module, "finish_trace", _noop_async)
        if hasattr(module, "update_plugin_runtime_status"):
            _patch(module, "update_plugin_runtime_status", _noop_async)
        if hasattr(module, "emit_inbound_event"):
            _patch(module, "emit_inbound_event", _noop_inbound)
        if hasattr(module, "get_redis"):
            _patch(module, "get_redis", lambda _redis=redis: _redis)

    _patch(account_bot_runtime, "_run_worker_interaction_entry", _run_worker_entry)
    _patch(account_bot_runtime, "_run_worker_interaction_action", _run_worker_action)
    _patch(delivery_mod.InteractionDeliveryExecutor, "_dry_run_enabled", lambda self, action: True)
    _patch(account_bot_service, "send_message", _send_message)
    _patch(account_bot_service, "edit_message", _edit_message)
    _patch(account_bot_service, "edit_message_caption", _edit_caption)
    _patch(account_bot_service, "send_photo_bytes", _send_photo)
    _patch(account_bot_service, "delete_message", _delete_message)
    _patch(account_bot_service, "answer_callback", _answer_callback)
    _patch(account_bot_service, "answer_inline_query", _answer_inline_query)

    try:
        yield
    finally:
        for obj, name, original in reversed(patches):
            setattr(obj, name, original)


@asynccontextmanager
async def _force_loader_dry_run(account_id: int | None):
    originals: list[tuple[Any, dict[str, Any]]] = []
    for state_id, state in list(loader._STATES.items()):  # noqa: SLF001
        if account_id is not None and int(state_id) != int(account_id):
            continue
        for ctx in state.contexts.values():
            originals.append((ctx, dict(ctx.account_config or {})))
            ctx.account_config = _force_config_dry_run(ctx.account_config)
    try:
        yield
    finally:
        for ctx, original in originals:
            ctx.account_config = original


def _force_state_contexts_dry_run(account_id: int) -> None:
    state = loader._STATES.get(account_id)  # noqa: SLF001
    if state is None:
        return
    for ctx in state.contexts.values():
        ctx.account_config = _force_config_dry_run(ctx.account_config)


def _force_envelope_dry_run(envelope: dict[str, Any]) -> dict[str, Any]:
    out = dict(envelope)
    context = dict(out.get("context") if isinstance(out.get("context"), dict) else {})
    context["dev_mode"] = {"dry_run": True, "recording": False}
    out["context"] = context
    return out


def _force_config_dry_run(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(config or {})
    dev_mode = dict(out.get("dev_mode") if isinstance(out.get("dev_mode"), dict) else {})
    dev_mode["dry_run"] = True
    dev_mode["recording"] = False
    out["dev_mode"] = dev_mode
    return out


def _can_replay_interaction_module(envelope: dict[str, Any]) -> bool:
    trigger = envelope.get("trigger") if isinstance(envelope.get("trigger"), dict) else {}
    return bool(str(trigger.get("module_key") or "").strip() and str(trigger.get("entry_key") or "").strip())


def _incoming_from_envelope(account_id: int, token: str, envelope: dict[str, Any]) -> account_bot_runtime.Incoming:
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
    chat = envelope.get("chat") if isinstance(envelope.get("chat"), dict) else {}
    sender = envelope.get("sender") if isinstance(envelope.get("sender"), dict) else {}
    callback = envelope.get("callback") if isinstance(envelope.get("callback"), dict) else {}
    inline_query = envelope.get("inline_query") if isinstance(envelope.get("inline_query"), dict) else {}
    chosen = envelope.get("chosen_inline_result") if isinstance(envelope.get("chosen_inline_result"), dict) else {}
    reply = envelope.get("reply_to") if isinstance(envelope.get("reply_to"), dict) else {}
    raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {}
    return account_bot_runtime.Incoming(
        account_id=account_id,
        token=token,
        update_id=_int_or_none(source.get("update_id") or raw.get("update_id")) or 0,
        user_id=_int_or_none(sender.get("user_id")),
        chat_id=_int_or_none(message.get("chat_id") or chat.get("id") or source.get("chat_id")),
        chat_type=str(message.get("chat_type") or chat.get("type") or "") or None,
        message_id=_int_or_none(message.get("message_id") or source.get("message_id") or raw.get("message_id")),
        text=str(message.get("text") or raw.get("text") or ""),
        callback_id=str(callback.get("id") or source.get("callback_query_id") or raw.get("callback_query_id") or "") or None,
        callback_data=str(callback.get("data") or source.get("callback_data") or raw.get("callback_data") or "") or None,
        inline_query_id=str(inline_query.get("id") or source.get("inline_query_id") or raw.get("inline_query_id") or "") or None,
        inline_query_text=str(inline_query.get("query") or "") or None,
        inline_offset=str(inline_query.get("offset") or "") or None,
        inline_chat_type=str(inline_query.get("chat_type") or "") or None,
        chosen_inline_result_id=str(chosen.get("result_id") or raw.get("chosen_inline_result_id") or "") or None,
        event_type=str(envelope.get("event_type") or source.get("type") or raw.get("event_type") or "") or None,
        display_name=str(sender.get("display_name") or "") or None,
        username=str(sender.get("username") or "") or None,
        reply_to_user_id=_int_or_none(reply.get("user_id")),
        reply_to_message_id=_int_or_none(reply.get("message_id") or message.get("reply_to_message_id")),
        reply_to_display_name=str(reply.get("display_name") or "") or None,
        reply_to_username=str(reply.get("username") or "") or None,
        reply_to_text=str(reply.get("text") or "") or None,
        trace_id=str(envelope.get("trace_id") or "") or None,
        native_raw=envelope.get("native_raw") if isinstance(envelope.get("native_raw"), dict) else None,
    )


def _rule_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    trigger = envelope.get("trigger") if isinstance(envelope.get("trigger"), dict) else {}
    session = envelope.get("session") if isinstance(envelope.get("session"), dict) else {}
    return {
        "id": str(trigger.get("rule_id") or envelope.get("rule_id") or session.get("rule_id") or "replay"),
        "name": str(trigger.get("rule_name") or envelope.get("rule_name") or session.get("rule_name") or "replay"),
        "action": "module",
        "enabled": True,
        "module_key": str(trigger.get("module_key") or session.get("module_key") or ""),
        "module_action": str(trigger.get("entry_key") or session.get("entry_key") or envelope.get("entry_key") or ""),
        "module_config": dict(envelope.get("module_config") if isinstance(envelope.get("module_config"), dict) else {}),
        "module_session_scope": str(session.get("scope") or "chat"),
        "module_send_via": str(session.get("channel") or "interaction_bot"),
    }


def _parsed_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key in ("payment", "player", "actor"):
        value = envelope.get(key)
        if isinstance(value, dict):
            parsed.update(value)
    for key in ("amount", "payer_name", "payer_user_id", "receiver_name", "receiver_user_id", "prize"):
        if key in envelope:
            parsed[key] = envelope[key]
    return parsed


def _bot_update_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    native = envelope.get("native_raw")
    if isinstance(native, dict):
        return dict(native)
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
    chat = envelope.get("chat") if isinstance(envelope.get("chat"), dict) else {}
    sender = envelope.get("sender") if isinstance(envelope.get("sender"), dict) else {}
    raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {}
    update_id = _int_or_none(source.get("update_id") or raw.get("update_id")) or 0
    event_type = str(envelope.get("event_type") or source.get("type") or raw.get("event_type") or "message")
    from_user = _bot_user_from_sender(sender)
    if event_type == "inline_query":
        return {
            "update_id": update_id,
            "inline_query": {
                "id": str(source.get("inline_query_id") or raw.get("inline_query_id") or ""),
                "from": from_user,
                "query": str((envelope.get("inline_query") or {}).get("query") or message.get("text") or ""),
                "offset": str((envelope.get("inline_query") or {}).get("offset") or ""),
                "chat_type": str((envelope.get("inline_query") or {}).get("chat_type") or ""),
            },
        }
    msg = {
        "message_id": _int_or_none(message.get("message_id") or raw.get("message_id")) or 0,
        "text": str(message.get("text") or raw.get("text") or ""),
        "chat": {
            "id": _int_or_none(message.get("chat_id") or chat.get("id") or source.get("chat_id")) or 0,
            "type": str(message.get("chat_type") or chat.get("type") or "private"),
            "title": chat.get("title"),
            "username": chat.get("username"),
        },
        "from": from_user,
    }
    callback_id = str(source.get("callback_query_id") or raw.get("callback_query_id") or "")
    callback_data = str(source.get("callback_data") or raw.get("callback_data") or "")
    if event_type == "callback_query" or callback_id or callback_data:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "data": callback_data,
                "from": from_user,
                "message": msg,
            },
        }
    key = "edited_message" if event_type == "message_edited" else "message"
    return {"update_id": update_id, key: msg}


def _bot_user_from_sender(sender: dict[str, Any]) -> dict[str, Any]:
    display = str(sender.get("display_name") or "").strip()
    first_name, _, last_name = display.partition(" ")
    return {
        "id": _int_or_none(sender.get("user_id")) or 0,
        "first_name": first_name or display or "Replay",
        "last_name": last_name or None,
        "username": str(sender.get("username") or "").lstrip("@") or None,
    }


def _account_id_from_envelopes(envelopes: list[dict[str, Any]]) -> int | None:
    for envelope in envelopes:
        account_id = _account_id_from_envelope(envelope)
        if account_id is not None:
            return account_id
    return None


def _account_id_from_envelope(envelope: dict[str, Any]) -> int | None:
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    return _int_or_none(source.get("account_id") or envelope.get("account_id"))


def _normalize_status(status: str) -> str | None:
    value = str(status or "").strip().upper()
    if value == "SUCCESS":
        value = action_tap.ACTION_EVENT_STATUS_OK
    elif value == "ERROR":
        value = action_tap.ACTION_EVENT_STATUS_FAILED
    statuses = {
        action_tap.ACTION_EVENT_STATUS_COMPENSATED,
        action_tap.ACTION_EVENT_STATUS_DRY_RUN,
        action_tap.ACTION_EVENT_STATUS_FAILED,
        action_tap.ACTION_EVENT_STATUS_OK,
        action_tap.ACTION_EVENT_STATUS_PENDING,
    }
    return value if value in statuses else None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


_MISSING = object()


__all__ = [
    "ReplayResult",
    "load_recording",
    "replay_envelopes",
    "replay_recording",
]
