"""Telethon Layer 228 adapter for native Userbot rich messages."""

from __future__ import annotations

import inspect
import time
import weakref
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

try:
    from telethon.tl import alltlobjects as _tl_alltlobjects
    from telethon.tl import functions as _tl_functions
    from telethon.tl import types as _tl_types
except ImportError:  # pragma: no cover - production dependency, retained for stable diagnostics
    _tl_alltlobjects = None
    _tl_functions = None
    _tl_types = None

from .rich_message import (
    InputRichMessage,
    RichMessageFormat,
    RichMessageValidationError,
    build_input_rich_message,
)
from .telegram_reply import ReplyParameters
from .telegram_text import UnsupportedRichBlock, text_only_blocks_to_html

TELETHON_RICH_MESSAGE_LAYER = 228
CAPABILITY_CACHE_TTL_SECONDS = 300.0

ERROR_TELETHON_LAYER_TOO_OLD = "telethon_layer_too_old"
ERROR_RICH_MESSAGE_NOT_SUPPORTED = "rich_message_not_supported"
ERROR_PREMIUM_REQUIRED = "premium_required"
ERROR_RICH_MESSAGE_POSTING_DISABLED = "rich_message_posting_disabled"
ERROR_RICH_MESSAGE_MEDIA_UNSUPPORTED = "rich_message_media_unsupported"
ERROR_RICH_MESSAGE_BLOCKS_UNSUPPORTED = "rich_message_blocks_unsupported"
ERROR_RICH_MESSAGE_DRAFT_DISABLED = "rich_message_draft_disabled"
ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN = "rich_message_capability_unknown"
ERROR_INVALID_RICH_MESSAGE = "invalid_rich_message"
ERROR_TELEGRAM_API = "telegram_api_error"

_ERROR_MESSAGES = {
    ERROR_TELETHON_LAYER_TOO_OLD: f"Telethon 必须支持 Telegram Layer {TELETHON_RICH_MESSAGE_LAYER}",
    ERROR_RICH_MESSAGE_NOT_SUPPORTED: "当前 Telethon 不支持 Userbot Rich Message raw API",
    ERROR_PREMIUM_REQUIRED: "Userbot Rich Message 需要 Telegram Premium 账号",
    ERROR_RICH_MESSAGE_POSTING_DISABLED: "Telegram app config 已禁用 rich_message_posting",
    ERROR_RICH_MESSAGE_MEDIA_UNSUPPORTED: "Userbot Rich Message 暂不支持本地媒体上传或 media 映射",
    ERROR_RICH_MESSAGE_BLOCKS_UNSUPPORTED: "Userbot Rich Message 暂不支持 blocks 字典到 PageBlock 的映射",
    ERROR_RICH_MESSAGE_DRAFT_DISABLED: "实验性 Userbot Rich Message Draft 默认关闭",
    ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN: "无法确认 Userbot Rich Message 的 Premium 或 app config 能力",
    ERROR_INVALID_RICH_MESSAGE: "rich_message 参数无效",
    ERROR_TELEGRAM_API: "Telegram Rich Message 请求失败",
}

_UNSET = object()


class UserbotRichMessageResult(TypedDict):
    message_id: int | None
    chat_id: int | str | None
    rich_message_format: Literal["html", "markdown", "blocks"]
    actual_send_via: Literal["userbot_reply"]
    reply_to_message_id: NotRequired[int | None]


class UserbotRichMessageError(RuntimeError):
    """Rich-message failure with a stable action-chain error code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = str(message or _ERROR_MESSAGES.get(code) or code)
        super().__init__(self.message)

    def to_result(
        self,
        *,
        chat_id: int | str | None = None,
        message_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "message_id": message_id,
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_message_id,
            "error_code": self.code,
            "error": self.message,
        }


@dataclass(frozen=True, slots=True)
class RichMessageCapability:
    available: bool
    error_code: str | None
    layer: int | None
    raw_types_available: bool
    is_premium: bool | None
    rich_message_posting: bool | None
    probe_errors: tuple[str, ...] = ()

    def require(self) -> None:
        if not self.available:
            raise UserbotRichMessageError(
                self.error_code or ERROR_RICH_MESSAGE_NOT_SUPPORTED,
            )


@dataclass(frozen=True, slots=True)
class _CachedCapability:
    cached_at: float
    capability: RichMessageCapability
    queried_me: bool
    queried_app_config: bool


_CAPABILITY_CACHE: weakref.WeakKeyDictionary[Any, _CachedCapability] = weakref.WeakKeyDictionary()


def evaluate_rich_message_capability(
    *,
    layer: int | None,
    raw_types_available: bool,
    is_premium: bool | None = None,
    rich_message_posting: bool | None = None,
    probe_errors: tuple[str, ...] = (),
) -> RichMessageCapability:
    """Pure capability gate; unknown remote values do not imply rejection."""

    error_code: str | None = None
    if layer is not None and layer < TELETHON_RICH_MESSAGE_LAYER:
        error_code = ERROR_TELETHON_LAYER_TOO_OLD
    elif not raw_types_available or layer is None:
        error_code = ERROR_RICH_MESSAGE_NOT_SUPPORTED
    elif is_premium is False:
        error_code = ERROR_PREMIUM_REQUIRED
    elif rich_message_posting is False:
        error_code = ERROR_RICH_MESSAGE_POSTING_DISABLED
    return RichMessageCapability(
        available=error_code is None,
        error_code=error_code,
        layer=layer,
        raw_types_available=raw_types_available,
        is_premium=is_premium,
        rich_message_posting=rich_message_posting,
        probe_errors=probe_errors,
    )


def local_telethon_rich_message_capability() -> tuple[int | None, bool]:
    """Inspect the installed Telethon schema without performing network I/O."""

    if _tl_alltlobjects is None or _tl_functions is None or _tl_types is None:
        return None, False
    layer = getattr(_tl_alltlobjects, "LAYER", None)
    try:
        required_types = (
            _tl_types.InputRichMessage,
            _tl_types.InputRichMessageHTML,
            _tl_types.InputRichMessageMarkdown,
            _tl_types.InputReplyToMessage,
        )
        send_request = _tl_functions.messages.SendMessageRequest
        edit_request = _tl_functions.messages.EditMessageRequest
        request_fields_present = all(
            "rich_message" in inspect.signature(request).parameters
            for request in (send_request, edit_request)
        )
    except (AttributeError, TypeError, ValueError):
        return int(layer) if isinstance(layer, int) else None, False
    return (
        int(layer) if isinstance(layer, int) else None,
        bool(all(required_types) and request_fields_present),
    )


async def detect_rich_message_capability(
    client: Any,
    *,
    me: Any = _UNSET,
    app_config: Any = _UNSET,
    query_me: bool = True,
    query_app_config: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
    layer: int | None = None,
    raw_types_available: bool | None = None,
) -> RichMessageCapability:
    """Detect local and account capabilities, caching remote probes per client.

    Callers that already cache ``get_me`` or ``help.getAppConfig`` can pass
    those values directly. Probe failures fail closed, are exposed through
    ``probe_errors``, and are not cached so a later call can retry them.
    """

    explicit_inputs = (
        me is not _UNSET or app_config is not _UNSET or layer is not None or raw_types_available is not None
    )
    if use_cache and not force_refresh and not explicit_inputs:
        cached = _get_cached_capability(
            client,
            query_me=query_me,
            query_app_config=query_app_config,
        )
        if cached is not None:
            return cached

    local_layer, local_raw_types = local_telethon_rich_message_capability()
    effective_layer = local_layer if layer is None else layer
    effective_raw_types = local_raw_types if raw_types_available is None else raw_types_available
    local_result = evaluate_rich_message_capability(
        layer=effective_layer,
        raw_types_available=effective_raw_types,
    )
    if not local_result.available:
        return local_result

    probe_errors: list[str] = []
    me_value = me
    if me_value is _UNSET and query_me:
        get_me = getattr(client, "get_me", None)
        if callable(get_me):
            try:
                me_value = await _maybe_await(get_me())
            except Exception as exc:  # noqa: BLE001 - a failed optional probe is recorded
                probe_errors.append(f"get_me:{type(exc).__name__}")
                me_value = None
    is_premium = _extract_premium(me_value) if me_value is not _UNSET else None

    app_config_value = app_config
    if app_config_value is _UNSET and query_app_config and callable(client):
        try:
            request_type = _tl_functions.help.GetAppConfigRequest
            app_config_value = await _maybe_await(client(request_type(hash=0)))
        except Exception as exc:  # noqa: BLE001 - a failed optional probe is recorded
            probe_errors.append(f"get_app_config:{type(exc).__name__}")
            app_config_value = None
    posting_enabled = (
        _extract_rich_message_posting(app_config_value) if app_config_value is not _UNSET else None
    )

    result = evaluate_rich_message_capability(
        layer=effective_layer,
        raw_types_available=effective_raw_types,
        is_premium=is_premium,
        rich_message_posting=posting_enabled,
        probe_errors=tuple(probe_errors),
    )
    premium_required = query_me or me is not _UNSET
    app_config_required = query_app_config or app_config is not _UNSET
    if result.available and (
        (premium_required and is_premium is None) or (app_config_required and posting_enabled is None)
    ):
        result = RichMessageCapability(
            available=False,
            error_code=ERROR_RICH_MESSAGE_CAPABILITY_UNKNOWN,
            layer=effective_layer,
            raw_types_available=effective_raw_types,
            is_premium=is_premium,
            rich_message_posting=posting_enabled,
            probe_errors=tuple(probe_errors),
        )
    if use_cache and not explicit_inputs and not probe_errors:
        _set_cached_capability(
            client,
            result,
            queried_me=query_me,
            queried_app_config=query_app_config,
        )
    return result


def clear_rich_message_capability_cache(client: Any | None = None) -> None:
    if client is None:
        _CAPABILITY_CACHE.clear()
        return
    client = _capability_cache_key(client)
    try:
        _CAPABILITY_CACHE.pop(client, None)
    except TypeError:
        pass


def build_telethon_input_rich_message(raw: Any) -> Any:
    """Build a Layer 228 raw HTML/Markdown input without text fallback."""

    try:
        rich_message = build_input_rich_message(raw)
    except RichMessageValidationError as exc:
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, str(exc)) from exc
    if rich_message.media:
        raise UserbotRichMessageError(ERROR_RICH_MESSAGE_MEDIA_UNSUPPORTED)
    if _tl_types is None:
        raise UserbotRichMessageError(ERROR_RICH_MESSAGE_NOT_SUPPORTED)

    kwargs = {
        "rtl": rich_message.is_rtl,
        "noautolink": rich_message.skip_entity_detection,
        "files": None,
    }
    try:
        if rich_message.format is RichMessageFormat.BLOCKS:
            try:
                html = text_only_blocks_to_html(rich_message.content)
            except (TypeError, ValueError, UnsupportedRichBlock) as exc:
                raise UserbotRichMessageError(ERROR_RICH_MESSAGE_BLOCKS_UNSUPPORTED) from exc
            return _tl_types.InputRichMessageHTML(html=html, **kwargs)
        if rich_message.format is RichMessageFormat.HTML:
            return _tl_types.InputRichMessageHTML(html=str(rich_message.content), **kwargs)
        return _tl_types.InputRichMessageMarkdown(markdown=str(rich_message.content), **kwargs)
    except UserbotRichMessageError:
        raise
    except (AttributeError, TypeError) as exc:
        raise UserbotRichMessageError(ERROR_RICH_MESSAGE_NOT_SUPPORTED) from exc


async def send_rich_message(
    client: Any,
    peer: Any,
    rich_message: Any,
    *,
    reply_to_message_id: int | None = None,
    reply_markup: Any | None = None,
    capability: RichMessageCapability | None = None,
) -> UserbotRichMessageResult:
    """Send a native rich message via raw ``messages.sendMessage``."""

    normalized = _build_neutral_input(rich_message)
    raw_rich_message = build_telethon_input_rich_message(normalized)
    effective_capability = capability or await detect_rich_message_capability(client)
    effective_capability.require()
    reply_to_id = _positive_int_or_none(reply_to_message_id, field="reply_to_message_id")

    try:
        input_peer = await _resolve_input_peer(client, peer)
        reply_to = (
            ReplyParameters(message_id=reply_to_id).build_telethon().kwargs["reply_to"]
            if reply_to_id is not None
            else None
        )
        request = _tl_functions.messages.SendMessageRequest(
            peer=input_peer,
            message="",
            reply_to=reply_to,
            reply_markup=reply_markup,
            rich_message=raw_rich_message,
        )
        response = await _maybe_await(client(request))
    except UserbotRichMessageError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize Telethon RPC/serialization errors
        raise UserbotRichMessageError(
            ERROR_TELEGRAM_API,
            f"{_ERROR_MESSAGES[ERROR_TELEGRAM_API]}: {type(exc).__name__}: {exc}",
        ) from exc

    return {
        "message_id": _extract_message_id(response),
        "chat_id": _result_chat_id(peer),
        "reply_to_message_id": reply_to_id,
        "rich_message_format": normalized.format.value,
        "actual_send_via": "userbot_reply",
    }


async def edit_rich_message(
    client: Any,
    peer: Any,
    message_id: int,
    rich_message: Any,
    *,
    reply_markup: Any | None = None,
    capability: RichMessageCapability | None = None,
) -> UserbotRichMessageResult:
    """Edit a native rich message via raw ``messages.editMessage``."""

    normalized = _build_neutral_input(rich_message)
    raw_rich_message = build_telethon_input_rich_message(normalized)
    effective_capability = capability or await detect_rich_message_capability(client)
    effective_capability.require()
    target_message_id = _positive_int_or_none(message_id, field="message_id")
    if target_message_id is None:  # pragma: no cover - guarded by field conversion
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, "message_id 必须是正整数")

    try:
        input_peer = await _resolve_input_peer(client, peer)
        request = _tl_functions.messages.EditMessageRequest(
            peer=input_peer,
            id=target_message_id,
            reply_markup=reply_markup,
            rich_message=raw_rich_message,
        )
        response = await _maybe_await(client(request))
    except UserbotRichMessageError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize Telethon RPC/serialization errors
        raise UserbotRichMessageError(
            ERROR_TELEGRAM_API,
            f"{_ERROR_MESSAGES[ERROR_TELEGRAM_API]}: {type(exc).__name__}: {exc}",
        ) from exc

    return {
        "message_id": _extract_message_id(response) or target_message_id,
        "chat_id": _result_chat_id(peer),
        "rich_message_format": normalized.format.value,
        "actual_send_via": "userbot_reply",
    }


async def send_rich_message_draft(
    client: Any,
    peer: Any,
    draft_id: int,
    rich_message: Any,
    *,
    message_thread_id: int | None = None,
    enabled: bool = False,
    capability: RichMessageCapability | None = None,
) -> dict[str, Any]:
    """Stream an experimental ephemeral Rich Message draft via ``setTyping``."""

    if not enabled:
        raise UserbotRichMessageError(ERROR_RICH_MESSAGE_DRAFT_DISABLED)
    if not isinstance(draft_id, int) or isinstance(draft_id, bool) or draft_id == 0:
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, "draft_id 必须是非零整数")
    normalized = _build_neutral_input(rich_message)
    raw_rich_message = build_telethon_input_rich_message(normalized)
    effective_capability = capability or await detect_rich_message_capability(client)
    effective_capability.require()
    try:
        input_peer = await _resolve_input_peer(client, peer)
        action = _tl_types.InputSendMessageRichMessageDraftAction(
            rich_message=raw_rich_message,
            random_id=draft_id,
        )
        response = await _maybe_await(
            client(
                _tl_functions.messages.SetTypingRequest(
                    peer=input_peer,
                    action=action,
                    top_msg_id=message_thread_id,
                )
            )
        )
    except UserbotRichMessageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UserbotRichMessageError(
            ERROR_TELEGRAM_API,
            f"{_ERROR_MESSAGES[ERROR_TELEGRAM_API]}: {type(exc).__name__}: {exc}",
        ) from exc
    return {
        "result": bool(response),
        "chat_id": _result_chat_id(peer),
        "draft_id": draft_id,
        "rich_message_format": normalized.format.value,
        "actual_send_via": "userbot_reply",
        "ephemeral": True,
    }


def _build_neutral_input(raw: Any) -> InputRichMessage:
    try:
        return build_input_rich_message(raw)
    except RichMessageValidationError as exc:
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, str(exc)) from exc


def _extract_premium(me: Any) -> bool | None:
    if me is None or me is _UNSET:
        return None
    if isinstance(me, Mapping):
        if "premium" not in me:
            return None
        return me.get("premium") is True
    if not hasattr(me, "premium"):
        return None
    # Telegram represents an unset optional ``premium`` TL flag as ``None``.
    return getattr(me, "premium", None) is True


def _extract_rich_message_posting(app_config: Any) -> bool | None:
    if app_config is None or app_config is _UNSET:
        return None
    config = (
        app_config.get("config", app_config)
        if isinstance(app_config, Mapping)
        else getattr(
            app_config,
            "config",
            app_config,
        )
    )
    native = _json_value_to_python(config)
    if not isinstance(native, Mapping):
        return None
    value = native.get("rich_message_posting")
    return value if isinstance(value, bool) else None


def _json_value_to_python(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_value_to_python(item) for item in value]
    if isinstance(value, Mapping):
        type_name = value.get("_")
        if type_name == "JsonObject":
            return _json_object_entries(value.get("value"))
        if type_name == "JsonObjectValue":
            return (
                str(value.get("key") or ""),
                _json_value_to_python(value.get("value")),
            )
        if type_name == "JsonNull":
            return None
        if type_name in {"JsonBool", "JsonNumber", "JsonString"}:
            return _json_value_to_python(value.get("value"))
        return {str(key): _json_value_to_python(item) for key, item in value.items()}

    type_name = type(value).__name__
    if type_name == "JsonObject":
        return _json_object_entries(getattr(value, "value", None))
    if type_name == "JsonObjectValue":
        return (
            str(getattr(value, "key", "")),
            _json_value_to_python(getattr(value, "value", None)),
        )
    if type_name == "JsonNull":
        return None
    if type_name in {"JsonBool", "JsonNumber", "JsonString"}:
        return _json_value_to_python(getattr(value, "value", None))
    return value


def _json_object_entries(entries: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in entries or []:
        converted = _json_value_to_python(entry)
        if isinstance(converted, tuple) and len(converted) == 2:
            result[str(converted[0])] = converted[1]
    return result


async def _resolve_input_peer(client: Any, peer: Any) -> Any:
    if getattr(peer, "SUBCLASS_OF_ID", None) == 0xC91C90B6:
        return peer
    resolver = getattr(client, "get_input_entity", None)
    if not callable(resolver):
        raise UserbotRichMessageError(
            ERROR_RICH_MESSAGE_NOT_SUPPORTED,
            "Telethon client 缺少 get_input_entity，无法构造 raw peer",
        )
    return await _maybe_await(resolver(peer))


def _positive_int_or_none(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, f"{field} 必须是正整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, f"{field} 必须是正整数") from exc
    if normalized <= 0:
        raise UserbotRichMessageError(ERROR_INVALID_RICH_MESSAGE, f"{field} 必须是正整数")
    return normalized


def _extract_message_id(response: Any) -> int | None:
    direct = _int_id(getattr(response, "id", None))
    if direct is not None:
        return direct
    message = getattr(response, "message", None)
    direct = _int_id(getattr(message, "id", None))
    if direct is not None:
        return direct
    for update in getattr(response, "updates", None) or []:
        message = getattr(update, "message", None)
        update_id = _int_id(getattr(message, "id", None)) or _int_id(getattr(update, "id", None))
        if update_id is not None:
            return update_id
    return None


def _int_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _result_chat_id(peer: Any) -> int | str | None:
    if isinstance(peer, bool):
        return None
    if isinstance(peer, (int, str)):
        return peer
    return _int_id(getattr(peer, "chat_id", None) or getattr(peer, "channel_id", None))


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def _get_cached_capability(
    client: Any,
    *,
    query_me: bool,
    query_app_config: bool,
) -> RichMessageCapability | None:
    client = _capability_cache_key(client)
    try:
        cached = _CAPABILITY_CACHE.get(client)
    except TypeError:
        return None
    if cached is None:
        return None
    if time.monotonic() - cached.cached_at > CAPABILITY_CACHE_TTL_SECONDS:
        clear_rich_message_capability_cache(client)
        return None
    if query_me and not cached.queried_me:
        return None
    if query_app_config and not cached.queried_app_config:
        return None
    return cached.capability


def _set_cached_capability(
    client: Any,
    capability: RichMessageCapability,
    *,
    queried_me: bool,
    queried_app_config: bool,
) -> None:
    client = _capability_cache_key(client)
    try:
        _CAPABILITY_CACHE[client] = _CachedCapability(
            time.monotonic(),
            capability,
            queried_me,
            queried_app_config,
        )
    except TypeError:
        pass


def _capability_cache_key(client: Any) -> Any:
    """Share capability state across short-lived wrappers around one client."""

    return getattr(client, "_rich_message_capability_cache_key", client)
