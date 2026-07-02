"""Controlled delivery executor for interaction plugin actions."""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...redis_client import get_redis
from .. import account_bot_service
from ..event_trace import TRACE_STATUS_FAILED, TRACE_STATUS_OK, TRACE_STATUS_SKIPPED, record_action
from .contracts import (
    INTERACTION_SEND_VIA,
    SEND_CHANNEL_DEPRECATED_REASON_CODE,
    action_send_via_options,
    action_send_via_raw_selector,
    deprecated_send_via_values,
)

log = logging.getLogger(__name__)

INTERACTION_SESSION_CONTROL_ACTIONS = {"end_session", "close_session", "no_session"}
INTERACTION_ACTION_SAVE_KEY_MAX_LENGTH = 200

WriteLog = Callable[..., Awaitable[None]]
RunWorkerAction = Callable[..., Awaitable[tuple[bool, str | None, dict[str, Any]]]]


@dataclass(slots=True)
class InteractionDeliveryExecutor:
    incoming: Any
    write_log: WriteLog
    run_worker_action: RunWorkerAction
    log_context: Callable[[Any], dict[str, Any]]
    trace_context: Callable[[dict[str, Any] | None], dict[str, Any]]
    get_redis_client: Callable[[], Any] = get_redis

    async def apply(
        self,
        actions: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
        replace_message_id: int | None = None,
    ) -> None:
        for raw_action in actions[:10]:
            action = dict(raw_action)
            if context:
                action["context"] = dict(context)
            action_type = str(action.get("type") or "").strip()
            await self._record_settlement(action)
            if action_type == "start_session":
                continue
            if action_type in INTERACTION_SESSION_CONTROL_ACTIONS or action_type == "result":
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_SKIPPED,
                    error_code="session_control_action",
                    error=f"session control action: {action_type}",
                )
                continue
            if action_type in {"send_message", "send_photo", "send_file", "edit_message", "delete_message", "pin_message"}:
                deprecated_channels = deprecated_send_via_values(action_send_via_raw_selector(action))
                if deprecated_channels:
                    await record_action(
                        action.get("context"),
                        action,
                        TRACE_STATUS_FAILED,
                        error_code=SEND_CHANNEL_DEPRECATED_REASON_CODE,
                        error="notice/bbot_notice/notice_bot channel is deprecated",
                        deprecated_send_via=deprecated_channels,
                    )
                    await self.write_log(
                        self.incoming,
                        "warn",
                        "interaction action failed: deprecated send_via",
                        reason_code=SEND_CHANNEL_DEPRECATED_REASON_CODE,
                        action_type=action_type,
                        deprecated_send_via=deprecated_channels,
                        **self.log_context(self.incoming),
                    )
                    continue
            if action_type == "settlement":
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="settlement",
                )
                continue
            reply_to_message_id = _int_or_none(action.get("reply_to_message_id"))
            raw_reply_markup = action.get("reply_markup")
            reply_markup = raw_reply_markup if isinstance(raw_reply_markup, dict) else None
            if action_type == "answer_callback":
                await self._answer_callback(action)
                continue
            if action_type == "answer_inline_query":
                await self._answer_inline_query(action)
                continue
            if action_type == "delete_message":
                await self._apply_delete_message(action)
                continue
            if action_type == "pin_message":
                await self._apply_pin_message(action)
                continue
            if action_type == "edit_message":
                await self._apply_edit_message(action, reply_markup=reply_markup)
                continue
            if action_type == "send_message":
                replace_message_id = await self._apply_send_message(
                    action,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                    replace_message_id=replace_message_id,
                )
                continue
            if action_type in {"send_photo", "send_file"}:
                replace_message_id = await self._apply_send_media(
                    action,
                    reply_to_message_id=reply_to_message_id,
                    replace_message_id=replace_message_id,
                )
                continue
            log.info("interaction action ignored: unsupported type=%s aid=%s", action_type, self.incoming.account_id)
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_SKIPPED,
                error_code="unsupported_send_via",
                error=f"unsupported type: {action_type}",
            )
            await self.write_log(
                self.incoming,
                "info",
                f"interaction action ignored: unsupported type={action_type}",
                action_type=action_type,
                action=action,
                **self.log_context(self.incoming),
            )

    async def delete_message(
        self,
        message_id: int | None,
        *,
        chat_id: int | None = None,
        send_via: str = "interaction_bot",
        context: dict[str, Any] | None = None,
        record: bool = False,
    ) -> bool:
        target_chat_id = self._target_chat_id(chat_id)
        action = {
            "type": "delete_message",
            "send_via": send_via,
            "chat_id": target_chat_id if target_chat_id is not None else chat_id,
            "message_id": message_id,
        }
        if context:
            action["context"] = dict(context)
        if target_chat_id is None or message_id is None:
            if record:
                await record_action(
                    context,
                    action,
                    TRACE_STATUS_FAILED,
                    actual_send_via=send_via,
                    error_code="target_message_id_missing" if message_id is None else "scope_not_matched",
                    error="delete_message target chat_id or message_id is missing",
                )
            return False
        token = await self._resolve_token(send_via)
        if not token:
            if record:
                await record_action(
                    context,
                    action,
                    TRACE_STATUS_FAILED,
                    actual_send_via=send_via,
                    error_code="bot_token_missing",
                    error="bot token unavailable",
                )
            return False
        try:
            await account_bot_service.delete_message(
                token,
                target_chat_id,
                message_id,
            )
            if record:
                await record_action(
                    context,
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via=send_via,
                    result={"chat_id": target_chat_id, "message_id": message_id},
                )
            return True
        except Exception as exc:  # noqa: BLE001
            if record:
                await record_action(
                    context,
                    action,
                    TRACE_STATUS_FAILED,
                    actual_send_via=send_via,
                    error_code="telegram_api_error",
                    error=str(exc),
                )
            await self.write_log(
                self.incoming,
                "warn",
                "interaction placeholder delete failed",
                target_message_id=message_id,
                send_via=send_via,
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return False

    async def send_message(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        reply_to_message_id: int | None,
        send_via: str,
        edit_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        target_chat_id = self._target_chat_id(chat_id)
        if target_chat_id is None:
            return False, {}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            ok, error, result = await self.run_worker_action(
                self.incoming,
                payload={
                    "action_type": "send_message",
                    "chat_id": target_chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                },
            )
            if not ok:
                return False, {"error": error, "error_code": _worker_action_error_code(error)}
            return True, result
        token = await self._resolve_token(send_via)
        if not token:
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction action send_via={send_via} ignored: bot token unavailable",
                send_via=send_via,
                **self.log_context(self.incoming),
            )
            return False, {"error": "bot token unavailable", "error_code": "bot_token_missing"}
        if send_via == "interaction_bot" and edit_message_id is not None:
            edit_action = {
                "type": "edit_message",
                "send_via": send_via,
                "chat_id": target_chat_id,
                "message_id": edit_message_id,
                "text": text,
            }
            if context:
                edit_action["context"] = dict(context)
            try:
                result = await account_bot_service.edit_message(
                    token,
                    target_chat_id,
                    edit_message_id,
                    text,
                    reply_markup=reply_markup,
                )
                await record_action(
                    context,
                    edit_action,
                    TRACE_STATUS_OK,
                    actual_send_via=send_via,
                    result=result,
                )
                return True, result
            except Exception as exc:  # noqa: BLE001
                await record_action(
                    context,
                    edit_action,
                    TRACE_STATUS_FAILED,
                    actual_send_via=send_via,
                    error_code="telegram_api_error",
                    error=str(exc),
                )
                await self.write_log(
                    self.incoming,
                    "warn",
                    "interaction action edit placeholder failed, fallback send",
                    send_via=send_via,
                    edit_message_id=edit_message_id,
                    error=str(exc),
                    **self.log_context(self.incoming),
                )
        try:
            result = await account_bot_service.send_message(
                token,
                target_chat_id,
                text,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction action send_via={send_via} failed",
                send_via=send_via,
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return False, {"error": str(exc), "error_code": "telegram_api_error"}
        if send_via == "interaction_bot" and edit_message_id is not None:
            await self.delete_message(
                edit_message_id,
                chat_id=target_chat_id,
                send_via=send_via,
                context=context,
                record=True,
            )
        return True, result

    async def edit_message(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        message_id: int | None,
        send_via: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        target_chat_id = self._target_chat_id(chat_id)
        if target_chat_id is None:
            return False, {}
        if message_id is None:
            return False, {"error": "message_id missing"}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            ok, error, result = await self.run_worker_action(
                self.incoming,
                payload={
                    "action_type": "edit_message",
                    "chat_id": target_chat_id,
                    "message_id": message_id,
                    "text": text,
                },
            )
            if not ok:
                return False, {"error": error, "error_code": _worker_action_error_code(error)}
            return True, result
        token = await self._resolve_token(send_via)
        if not token:
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction action send_via={send_via} ignored: bot token unavailable",
                send_via=send_via,
                **self.log_context(self.incoming),
            )
            return False, {"error": "bot token unavailable", "error_code": "bot_token_missing"}
        try:
            result = await account_bot_service.edit_message(
                token,
                target_chat_id,
                message_id,
                text,
                reply_markup=reply_markup if send_via == "interaction_bot" else None,
            )
            return True, result
        except Exception as exc:  # noqa: BLE001
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction action edit_message send_via={send_via} failed",
                send_via=send_via,
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return False, {"error": str(exc), "error_code": "telegram_api_error"}

    async def send_photo(
        self,
        photo: bytes,
        *,
        chat_id: int | None = None,
        filename: str,
        caption: str | None,
        reply_to_message_id: int | None,
        send_via: str,
    ) -> tuple[bool, dict[str, Any]]:
        target_chat_id = self._target_chat_id(chat_id)
        if target_chat_id is None:
            return False, {}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            ok, error, result = await self.run_worker_action(
                self.incoming,
                payload={
                    "action_type": "send_photo",
                    "chat_id": target_chat_id,
                    "photo_base64": base64.b64encode(photo).decode("ascii"),
                    "filename": filename,
                    "caption": caption,
                    "reply_to_message_id": reply_to_message_id,
                },
            )
            if not ok:
                return False, {"error": error, "error_code": _worker_action_error_code(error)}
            return True, result
        token = await self._resolve_token(send_via)
        if not token:
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction media action send_via={send_via} ignored: bot token unavailable",
                send_via=send_via,
                **self.log_context(self.incoming),
            )
            return False, {"error": "bot token unavailable", "error_code": "bot_token_missing"}
        try:
            result = await account_bot_service.send_photo_bytes(
                token,
                target_chat_id,
                photo,
                filename=filename,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction media action send_via={send_via} failed",
                send_via=send_via,
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return False, {"error": str(exc), "error_code": "telegram_api_error"}
        return True, result

    async def _apply_send_message(
        self,
        action: dict[str, Any],
        *,
        reply_to_message_id: int | None,
        reply_markup: dict[str, Any] | None,
        replace_message_id: int | None,
    ) -> int | None:
        text = str(action.get("text") or "").strip()
        if not text:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="empty_message_text",
                error="send_message text is empty",
            )
            return replace_message_id
        chat_id = _int_or_none(action.get("chat_id"))
        target_chat_id = self._target_chat_id(chat_id)
        placeholder_chat_id = self.incoming.chat_id
        send_via_options = action_send_via_options(action)
        original_replace_message_id = replace_message_id
        replace_saved_key = action_save_message_id_key(action.get("replace_saved_message_id_key"))
        replace_saved_message_id = await self._read_saved_message_id(replace_saved_key)
        edit_message_id = _int_or_none(action.get("edit_message_id"))
        delete_message_id = None
        can_edit_placeholder = (
            bool(send_via_options)
            and send_via_options[0] == "interaction_bot"
            and target_chat_id == placeholder_chat_id
        )
        if edit_message_id is None and replace_message_id is not None and can_edit_placeholder:
            edit_message_id = replace_message_id
            replace_message_id = None
        elif edit_message_id is None and replace_message_id is not None:
            delete_message_id = replace_message_id
            replace_message_id = None
        ok, result, used_send_via = await self._try_send_message_options(
            text,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            send_via_options=send_via_options,
            edit_message_id=edit_message_id,
            reply_markup=reply_markup,
            context=action.get("context") if isinstance(action.get("context"), dict) else None,
        )
        if ok and delete_message_id is not None:
            await self.delete_message(
                delete_message_id,
                chat_id=placeholder_chat_id,
                context=action.get("context") if isinstance(action.get("context"), dict) else None,
                record=True,
            )
        if ok and delete_message_id is None and original_replace_message_id is not None and used_send_via != "interaction_bot":
            await self.delete_message(
                original_replace_message_id,
                chat_id=placeholder_chat_id,
                context=action.get("context") if isinstance(action.get("context"), dict) else None,
                record=True,
            )
        save_key = action_save_message_id_key(action.get("save_message_id_key"))
        if ok and save_key:
            msg_id = delivery_message_id(result)
            if msg_id is not None:
                await self.get_redis_client().set(save_key, str(msg_id), ex=7200)
        if (
            ok
            and replace_saved_message_id is not None
            and replace_saved_message_id != edit_message_id
            and replace_saved_message_id != delivery_message_id(result)
        ):
            await self.delete_message(
                replace_saved_message_id,
                chat_id=placeholder_chat_id,
                context=action.get("context") if isinstance(action.get("context"), dict) else None,
                record=True,
            )
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED,
            actual_send_via=used_send_via,
            result=result,
            error_code=None if ok else _result_error_code(result, "action_failed"),
            error=result.get("error") if isinstance(result, dict) else None,
        )
        if ok and used_send_via == "interaction_bot" and action.get("pin"):
            await self._apply_pin_message(
                {
                    "type": "pin_message",
                    "message_id": edit_message_id or delivery_message_id(result),
                    "chat_id": chat_id,
                    "send_via": used_send_via,
                    "context": action.get("context"),
                }
            )
        return replace_message_id

    async def _read_saved_message_id(self, key: str | None) -> int | None:
        if not key:
            return None
        try:
            raw = await self.get_redis_client().get(key)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return _int_or_none(raw)

    async def _apply_edit_message(
        self,
        action: dict[str, Any],
        *,
        reply_markup: dict[str, Any] | None,
    ) -> None:
        text = str(action.get("text") or "").strip()
        if not text:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="empty_message_text",
                error="edit_message text is empty",
            )
            return
        message_id = _int_or_none(action.get("message_id") or action.get("edit_message_id"))
        if message_id is None:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="target_message_id_missing",
                error="edit_message message_id is missing",
            )
            return
        chat_id = _int_or_none(action.get("chat_id"))
        ok, result, used_send_via = await self._try_edit_message_options(
            text,
            chat_id=chat_id,
            message_id=message_id,
            send_via_options=action_send_via_options(action),
            reply_markup=reply_markup,
        )
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED,
            actual_send_via=used_send_via,
            result=result,
            error_code=None if ok else _result_error_code(result, "action_failed"),
            error=result.get("error") if isinstance(result, dict) else None,
        )

    async def _apply_send_media(
        self,
        action: dict[str, Any],
        *,
        reply_to_message_id: int | None,
        replace_message_id: int | None,
    ) -> int | None:
        raw_photo = str(action.get("photo_base64") or action.get("file_base64") or "").strip()
        if not raw_photo:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="media_payload_missing",
                error="photo_base64/file_base64 is empty",
            )
            return replace_message_id
        try:
            photo = base64.b64decode(raw_photo, validate=True)
        except (binascii.Error, ValueError):
            log.info("interaction action ignored: invalid base64 media aid=%s", self.incoming.account_id)
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="media_payload_invalid",
                error="photo_base64/file_base64 is not valid base64",
            )
            return replace_message_id
        if not photo:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="media_payload_empty",
                error="decoded media payload is empty",
            )
            return replace_message_id
        filename = str(action.get("filename") or "interaction.png").strip() or "interaction.png"
        caption = str(action.get("caption") or action.get("text") or "").strip() or None
        chat_id = _int_or_none(action.get("chat_id"))
        placeholder_chat_id = self.incoming.chat_id
        ok, _result, _used_send_via = await self._try_send_photo_options(
            photo,
            chat_id=chat_id,
            filename=filename,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            send_via_options=action_send_via_options(action),
        )
        if ok and replace_message_id is not None:
            await self.delete_message(
                replace_message_id,
                chat_id=placeholder_chat_id,
                context=action.get("context") if isinstance(action.get("context"), dict) else None,
                record=True,
            )
            replace_message_id = None
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED,
            actual_send_via=_used_send_via,
            result=_result,
            error_code=None if ok else _result_error_code(_result, "action_failed"),
            error=_result.get("error") if isinstance(_result, dict) else None,
        )
        return replace_message_id

    async def _answer_callback(self, action: dict[str, Any]) -> None:
        callback_query_id = str(action.get("callback_query_id") or self.incoming.callback_id or "").strip()
        if not callback_query_id:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="callback_query_id_missing",
                error="callback_query_id missing",
            )
            return
        try:
            await account_bot_service.answer_callback(
                self.incoming.token,
                callback_query_id,
                text=str(action.get("text") or ""),
                show_alert=bool(action.get("show_alert")),
            )
        except Exception as exc:  # noqa: BLE001
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="telegram_api_error",
                error=str(exc),
            )
            await self.write_log(
                self.incoming,
                "warn",
                "interaction answer_callback failed; continue remaining actions",
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return
        await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot")

    async def _answer_inline_query(self, action: dict[str, Any]) -> None:
        inline_query_id = str(action.get("inline_query_id") or getattr(self.incoming, "inline_query_id", "") or "").strip()
        if not inline_query_id:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="inline_query_id_missing",
                error="inline_query_id missing",
            )
            return
        results = action.get("results")
        if not isinstance(results, list):
            results = []
        try:
            await account_bot_service.answer_inline_query(
                self.incoming.token,
                inline_query_id,
                results=[item for item in results if isinstance(item, dict)],
                cache_time=_int_or_none(action.get("cache_time")) or 0,
                is_personal=bool(action.get("is_personal", True)),
                next_offset=str(action.get("next_offset") or ""),
                button=action.get("button") if isinstance(action.get("button"), dict) else None,
            )
        except Exception as exc:  # noqa: BLE001
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="telegram_api_error",
                error=str(exc),
            )
            await self.write_log(
                self.incoming,
                "warn",
                "interaction inline query answer failed",
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return
        await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot")

    async def _apply_delete_message(self, action: dict[str, Any]) -> None:
        message_id = _int_or_none(action.get("message_id"))
        chat_id = _int_or_none(action.get("chat_id"))
        target_chat_id = self._target_chat_id(chat_id)
        if target_chat_id is None or message_id is None:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="target_message_id_missing",
                error="delete_message target chat_id or message_id missing",
            )
            return
        last_code = "unsupported_send_via"
        last_error = "no supported send_via"
        for send_via in action_send_via_options(action):
            if send_via != "interaction_bot":
                last_code = "unsupported_send_via"
                last_error = f"delete_message does not support send_via={send_via}"
                continue
            token = await self._resolve_token(send_via)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                await account_bot_service.delete_message(token, target_chat_id, message_id)
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via=send_via)
                return
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)

    async def _apply_pin_message(self, action: dict[str, Any]) -> None:
        message_id = _int_or_none(action.get("message_id"))
        chat_id = _int_or_none(action.get("chat_id"))
        target_chat_id = self._target_chat_id(chat_id)
        if target_chat_id is None or message_id is None:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="target_message_id_missing",
                error="pin_message target chat_id or message_id missing",
            )
            return
        last_code = "unsupported_send_via"
        last_error = "no supported send_via"
        for send_via in action_send_via_options(action):
            if send_via != "interaction_bot":
                last_code = "unsupported_send_via"
                last_error = f"pin_message does not support send_via={send_via}"
                continue
            token = await self._resolve_token(send_via)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                await account_bot_service.call_bot_api(
                    token,
                    "pinChatMessage",
                    {"chat_id": target_chat_id, "message_id": message_id},
                )
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via=send_via)
                return
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)

    async def _try_send_message_options(
        self,
        text: str,
        *,
        chat_id: int | None,
        reply_to_message_id: int | None,
        send_via_options: list[str],
        edit_message_id: int | None,
        reply_markup: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.send_message(
                text,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                send_via=send_via,
                edit_message_id=edit_message_id if send_via == "interaction_bot" else None,
                reply_markup=reply_markup if send_via == "interaction_bot" else None,
                context=context,
            )
            if ok:
                return True, result, send_via
            last_result = result
            await self._log_send_via_fallback(send_via, result)
        return False, last_result, send_via_options[0] if send_via_options else "interaction_bot"

    async def _try_edit_message_options(
        self,
        text: str,
        *,
        chat_id: int | None,
        message_id: int,
        send_via_options: list[str],
        reply_markup: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.edit_message(
                text,
                chat_id=chat_id,
                message_id=message_id,
                send_via=send_via,
                reply_markup=reply_markup if send_via == "interaction_bot" else None,
            )
            if ok:
                return True, result, send_via
            last_result = result
            await self._log_send_via_fallback(send_via, result)
        return False, last_result, send_via_options[0] if send_via_options else "interaction_bot"

    async def _try_send_photo_options(
        self,
        photo: bytes,
        *,
        chat_id: int | None,
        filename: str,
        caption: str | None,
        reply_to_message_id: int | None,
        send_via_options: list[str],
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.send_photo(
                photo,
                chat_id=chat_id,
                filename=filename,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                send_via=send_via,
            )
            if ok:
                return True, result, send_via
            last_result = result
            await self._log_send_via_fallback(send_via, result)
        return False, last_result, send_via_options[0] if send_via_options else "interaction_bot"

    async def _log_send_via_fallback(self, send_via: str, result: dict[str, Any]) -> None:
        await self.write_log(
            self.incoming,
            "warn",
            "interaction action send_via fallback",
            send_via=send_via,
            error=result.get("error") if isinstance(result, dict) else None,
            **self.log_context(self.incoming),
        )

    def _target_chat_id(self, chat_id: int | None = None) -> int | None:
        return chat_id if chat_id is not None else self.incoming.chat_id

    async def _resolve_token(self, send_via: str) -> str | None:
        if send_via == "interaction_bot":
            return self.incoming.token
        return None

    def _is_supported_send_via(self, send_via: str) -> bool:
        return send_via in INTERACTION_SEND_VIA

    async def _record_settlement(self, action: dict[str, Any]) -> None:
        settlement = action.get("settlement")
        if not isinstance(settlement, dict) and str(action.get("type") or "").strip() == "settlement":
            settlement = {k: v for k, v in action.items() if k != "type"}
        if not isinstance(settlement, dict):
            return
        context = self.log_context(self.incoming)
        trace_context = self.trace_context(action.get("context"))
        context.update({key: value for key, value in trace_context.items() if value is not None})
        await self.write_log(
            self.incoming,
            "info",
            "interaction settlement reported",
            action_type=str(action.get("type") or ""),
            settlement=settlement,
            **context,
        )


def delivery_message_id(result: dict[str, Any] | Any) -> int | None:
    if not isinstance(result, dict):
        return None
    return _int_or_none(result.get("message_id"))


def action_save_message_id_key(raw: Any) -> str | None:
    key = str(raw or "").strip()
    if not key or len(key) > INTERACTION_ACTION_SAVE_KEY_MAX_LENGTH:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]*", key):
        return None
    return key


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_error_code(result: Any, fallback: str) -> str:
    if isinstance(result, dict):
        code = str(result.get("error_code") or "").strip()
        if code:
            return code
        error = str(result.get("error") or "").strip()
        if error:
            return _worker_action_error_code(error)
    return fallback


def _worker_action_error_code(error: Any) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return "action_failed"
    if "message_id" in text or "消息" in text and "id" in text:
        return "target_message_id_missing"
    if "chat_id" in text:
        return "scope_not_matched"
    if "text" in text or "文本" in text:
        return "empty_message_text"
    if "base64" in text or "媒体" in text:
        return "media_payload_invalid"
    if "token" in text:
        return "bot_token_missing"
    if "unsupported" in text or "不支持" in text:
        return "unsupported_send_via"
    return "telegram_api_error"


__all__ = [
    "INTERACTION_SESSION_CONTROL_ACTIONS",
    "InteractionDeliveryExecutor",
    "action_save_message_id_key",
    "delivery_message_id",
]
