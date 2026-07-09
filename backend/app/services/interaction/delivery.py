"""Controlled delivery executor for interaction plugin actions."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...redis_client import get_redis
from .. import account_bot_service
from .. import payout_compensation as payout_compensation_service
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
INTERACTION_ACTION_LIMIT = 10
MESSAGE_ID_NAMESPACE_PREFIX = "tp:msgid"
REPLY_TARGET_NAMESPACE_PREFIX = "tp:replytarget"

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
        if len(actions) > INTERACTION_ACTION_LIMIT:
            dropped_actions = actions[INTERACTION_ACTION_LIMIT:]
            await self.write_log(
                self.incoming,
                "warn",
                "interaction actions truncated by delivery limit",
                reason_code="action_limit_exceeded",
                kept_count=INTERACTION_ACTION_LIMIT,
                dropped_count=len(dropped_actions),
                dropped_action_types=[str((item or {}).get("type") or "") for item in dropped_actions if isinstance(item, dict)],
                **self.log_context(self.incoming),
            )
            for raw_action in dropped_actions:
                action = dict(raw_action) if isinstance(raw_action, dict) else {"type": "unknown"}
                if context and isinstance(action.get("context"), dict):
                    merged_context = dict(action["context"])
                    merged_context.update(context)
                    action["context"] = merged_context
                elif context:
                    action["context"] = dict(context)
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_FAILED,
                    error_code="action_limit_exceeded",
                    error="interaction delivery only executes the first 10 actions",
                )
        for raw_action in actions[:INTERACTION_ACTION_LIMIT]:
            action = dict(raw_action)
            if context:
                action["context"] = dict(context)
            action_type = str(action.get("type") or "").strip()
            await self._record_settlement(action)
            if action_type == "start_session":
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_SKIPPED,
                    actual_send_via="interaction_session",
                    error_code="session_control_action",
                    error="session control action: start_session",
                )
                continue
            if action_type == "update_session":
                await self._apply_update_session(action)
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
            if action_type in {"send_message", "send_photo", "send_file", "edit_message", "edit_caption", "delete_message", "pin_message"}:
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
            reply_to_user_id = _reply_to_user_id_from_action(action)
            reply_to_search_limit = _int_or_none(action.get("reply_to_search_limit"))
            parse_mode = _action_parse_mode(action)
            raw_reply_markup = action.get("reply_markup")
            reply_markup = raw_reply_markup if isinstance(raw_reply_markup, dict) else None
            if action_type == "payout":
                await self._apply_payout(
                    action,
                    reply_to_message_id=reply_to_message_id,
                    reply_to_user_id=reply_to_user_id,
                    reply_to_search_limit=reply_to_search_limit,
                    parse_mode=parse_mode,
                )
                continue
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
                await self._apply_edit_message(action, parse_mode=parse_mode, reply_markup=reply_markup)
                continue
            if action_type == "edit_caption":
                await self._apply_edit_caption(action, parse_mode=parse_mode, reply_markup=reply_markup)
                continue
            if action_type == "send_message":
                replace_message_id = await self._apply_send_message(
                    action,
                    reply_to_message_id=reply_to_message_id,
                    reply_to_user_id=reply_to_user_id,
                    reply_to_search_limit=reply_to_search_limit,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    replace_message_id=replace_message_id,
                )
                continue
            if action_type in {"send_photo", "send_file"}:
                replace_message_id = await self._apply_send_media(
                    action,
                    reply_to_message_id=reply_to_message_id,
                    reply_to_user_id=reply_to_user_id,
                    reply_to_search_limit=reply_to_search_limit,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
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
        reply_to_user_id: int | None = None,
        reply_to_search_limit: int | None = None,
        parse_mode: str = "plain",
        send_via: str,
        edit_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        reply_anchor_missing_text: str | None = None,
        context: dict[str, Any] | None = None,
        action: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        action = action or {}
        target_chat_id = self._target_chat_id(chat_id)
        service_parse_mode = None if parse_mode == "plain" else parse_mode
        if target_chat_id is None:
            return False, {}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            payload = {
                "action_type": "send_message",
                "chat_id": target_chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "parse_mode": parse_mode,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            if reply_to_user_id is not None:
                payload["reply_to_user_id"] = reply_to_user_id
            if reply_to_search_limit is not None:
                payload["reply_to_search_limit"] = reply_to_search_limit
            if reply_anchor_missing_text:
                payload["reply_anchor_missing_text"] = reply_anchor_missing_text
            if bool(action.get("suppress_reply_anchor_missing_notice")):
                payload["suppress_reply_anchor_missing_notice"] = True
            ok, error, result = await self.run_worker_action(
                self.incoming,
                payload=payload,
            )
            if not ok:
                error_code = _result_error_code(result, _worker_action_error_code(error))
                detail = _userbot_action_failure_result(payload, error=error, error_code=error_code, result=result)
                await self._maybe_answer_failure_callback(action, detail)
                return False, detail
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
                edit_kwargs: dict[str, Any] = {"reply_markup": reply_markup}
                if service_parse_mode is not None:
                    edit_kwargs["parse_mode"] = service_parse_mode
                result = await account_bot_service.edit_message(
                    token,
                    target_chat_id,
                    edit_message_id,
                    text,
                    **edit_kwargs,
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
            send_kwargs: dict[str, Any] = {
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
            if service_parse_mode is not None:
                send_kwargs["parse_mode"] = service_parse_mode
            result = await account_bot_service.send_message(
                token,
                target_chat_id,
                text,
                **send_kwargs,
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
        parse_mode: str = "plain",
        reply_markup: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        target_chat_id = self._target_chat_id(chat_id)
        service_parse_mode = None if parse_mode == "plain" else parse_mode
        if target_chat_id is None:
            return False, {}
        if message_id is None:
            return False, {"error": "message_id missing"}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            payload = {
                "action_type": "edit_message",
                "chat_id": target_chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            ok, error, result = await self.run_worker_action(self.incoming, payload=payload)
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
            edit_kwargs: dict[str, Any] = {"reply_markup": reply_markup if send_via == "interaction_bot" else None}
            if service_parse_mode is not None:
                edit_kwargs["parse_mode"] = service_parse_mode
            result = await account_bot_service.edit_message(
                token,
                target_chat_id,
                message_id,
                text,
                **edit_kwargs,
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

    async def edit_caption(
        self,
        caption: str,
        *,
        chat_id: int | None = None,
        message_id: int | None,
        send_via: str,
        parse_mode: str = "plain",
        reply_markup: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        target_chat_id = self._target_chat_id(chat_id)
        service_parse_mode = None if parse_mode == "plain" else parse_mode
        if target_chat_id is None:
            return False, {}
        if message_id is None:
            return False, {"error": "message_id missing", "error_code": "target_message_id_missing"}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            payload = {
                "action_type": "edit_caption",
                "chat_id": target_chat_id,
                "message_id": message_id,
                "caption": caption,
                "parse_mode": parse_mode,
            }
            ok, error, result = await self.run_worker_action(self.incoming, payload=payload)
            if not ok:
                return False, {"error": error, "error_code": _worker_action_error_code(error)}
            return True, result
        token = await self._resolve_token(send_via)
        if not token:
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction action edit_caption send_via={send_via} ignored: bot token unavailable",
                send_via=send_via,
                **self.log_context(self.incoming),
            )
            return False, {"error": "bot token unavailable", "error_code": "bot_token_missing"}
        try:
            edit_kwargs: dict[str, Any] = {"reply_markup": reply_markup if send_via == "interaction_bot" else None}
            if service_parse_mode is not None:
                edit_kwargs["parse_mode"] = service_parse_mode
            result = await account_bot_service.edit_message_caption(
                token,
                target_chat_id,
                message_id,
                caption,
                **edit_kwargs,
            )
            return True, result
        except Exception as exc:  # noqa: BLE001
            if _is_message_not_modified_error(exc):
                return True, {"message_id": message_id, "chat_id": target_chat_id, "not_modified": True}
            await self.write_log(
                self.incoming,
                "warn",
                f"interaction action edit_caption send_via={send_via} failed",
                send_via=send_via,
                error=str(exc),
                **self.log_context(self.incoming),
            )
            return False, {"error": str(exc), "error_code": "telegram_api_error"}

    async def send_photo(
        self,
        photo: bytes,
        *,
        action_type: str = "send_photo",
        chat_id: int | None = None,
        filename: str,
        caption: str | None,
        reply_to_message_id: int | None,
        reply_to_user_id: int | None = None,
        reply_to_search_limit: int | None = None,
        parse_mode: str = "plain",
        reply_markup: dict[str, Any] | None = None,
        send_via: str,
    ) -> tuple[bool, dict[str, Any]]:
        target_chat_id = self._target_chat_id(chat_id)
        service_parse_mode = None if parse_mode == "plain" else parse_mode
        if target_chat_id is None:
            return False, {}
        if not self._is_supported_send_via(send_via):
            return False, {"error": f"unsupported send_via: {send_via}", "error_code": "unsupported_send_via"}
        if send_via == "userbot_reply":
            payload_field = "photo_base64" if action_type == "send_photo" else "file_base64"
            payload = {
                "action_type": action_type,
                "chat_id": target_chat_id,
                payload_field: base64.b64encode(photo).decode("ascii"),
                "filename": filename,
                "caption": caption,
                "reply_to_message_id": reply_to_message_id,
                "parse_mode": parse_mode,
            }
            if reply_to_user_id is not None:
                payload["reply_to_user_id"] = reply_to_user_id
            if reply_to_search_limit is not None:
                payload["reply_to_search_limit"] = reply_to_search_limit
            ok, error, result = await self.run_worker_action(
                self.incoming,
                payload=payload,
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
            send_kwargs: dict[str, Any] = {
                "filename": filename,
                "caption": caption,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup if send_via == "interaction_bot" else None,
            }
            if service_parse_mode is not None:
                send_kwargs["parse_mode"] = service_parse_mode
            if action_type == "send_file":
                result = await account_bot_service.send_document_bytes(
                    token,
                    target_chat_id,
                    photo,
                    **send_kwargs,
                )
            else:
                result = await account_bot_service.send_photo_bytes(
                    token,
                    target_chat_id,
                    photo,
                    **send_kwargs,
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

    async def _apply_payout(
        self,
        action: dict[str, Any],
        *,
        reply_to_message_id: int | None,
        reply_to_user_id: int | None,
        reply_to_search_limit: int | None,
        parse_mode: str,
    ) -> None:
        amount = _int_or_none(action.get("amount"))
        if amount is None or amount <= 0:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="action_failed",
                error="payout amount must be > 0",
            )
            return
        chat_id = _int_or_none(action.get("chat_id"))
        target_chat_id = self._target_chat_id(chat_id)
        if target_chat_id is None:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="scope_not_matched",
                error="payout target chat_id missing",
            )
            return
        text = str(action.get("text") or f"+{amount}").strip()
        if not text:
            text = f"+{amount}"
        payload: dict[str, Any] = {
            "action_type": "payout",
            "chat_id": target_chat_id,
            "amount": amount,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "parse_mode": parse_mode,
        }
        if action.get("payout_key") not in (None, ""):
            payload["payout_key"] = str(action.get("payout_key"))
        payout_key = payout_compensation_service.ensure_payout_key(payload)
        if reply_to_user_id is not None:
            payload["reply_to_user_id"] = reply_to_user_id
        if reply_to_search_limit is not None:
            payload["reply_to_search_limit"] = reply_to_search_limit
        if action.get("reply_anchor_missing_text") not in (None, ""):
            payload["reply_anchor_missing_text"] = str(action.get("reply_anchor_missing_text"))
        ok, error, result = await self.run_worker_action(self.incoming, payload=payload)
        detail = result if isinstance(result, dict) else {}
        if not ok:
            error_code = _result_error_code(detail, _worker_action_error_code(error))
            error_code = payout_compensation_service.normalize_payout_error_code(error_code, error)
            detail = _userbot_action_failure_result(payload, error=error, error_code=error_code, result=detail)
            payout_payload = dict(payload)
            context = action.get("context")
            if isinstance(context, dict):
                payout_payload["context"] = dict(context)
            compensation_queued = False
            try:
                queued = await payout_compensation_service.enqueue_payout_compensation(
                    account_id=self.incoming.account_id,
                    origin="delivery",
                    payload=payout_payload,
                    error_code=error_code,
                    error=error,
                    trace_id=getattr(self.incoming, "trace_id", None),
                    plugin_key=context.get("plugin_key") if isinstance(context, dict) else None,
                    entry_key=context.get("entry_key") if isinstance(context, dict) else None,
                    chat_id=target_chat_id,
                    amount=amount,
                )
                compensation_queued = queued is not None
            except Exception:  # noqa: BLE001
                log.debug("payout compensation enqueue failed in delivery", exc_info=True)
            detail["compensation_queued"] = compensation_queued
            detail["payout_key"] = payout_key
        save_key = namespaced_action_save_message_id_key(self.incoming.account_id, action.get("save_message_id_key"))
        if ok and save_key:
            msg_id = delivery_message_id(detail)
            if msg_id is not None:
                await self.get_redis_client().set(save_key, str(msg_id), ex=7200)
        record_detail: dict[str, Any] = {
            "actual_send_via": "userbot_reply",
            "result": detail,
            "error_code": None if ok else detail.get("error_code") or _worker_action_error_code(error),
            "error": None if ok else error,
        }
        if not ok:
            record_detail["compensation_queued"] = detail.get("compensation_queued")
            record_detail["payout_key"] = detail.get("payout_key")
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED,
            **record_detail,
        )
        if not ok:
            log_detail = self.log_context(self.incoming)
            log_detail.update(_userbot_action_log_detail(detail))
            await self.write_log(
                self.incoming,
                "warn",
                "interaction payout failed",
                action_type="payout",
                error=error,
                **log_detail,
            )

    async def _apply_send_message(
        self,
        action: dict[str, Any],
        *,
        reply_to_message_id: int | None,
        reply_to_user_id: int | None,
        reply_to_search_limit: int | None,
        parse_mode: str,
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
        replace_saved_key = namespaced_action_save_message_id_key(self.incoming.account_id, action.get("replace_saved_message_id_key"))
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
            reply_to_user_id=reply_to_user_id,
            reply_to_search_limit=reply_to_search_limit,
            parse_mode=parse_mode,
            send_via_options=send_via_options,
            edit_message_id=edit_message_id,
            reply_markup=reply_markup,
            reply_anchor_missing_text=str(action.get("reply_anchor_missing_text") or "") or None,
            context=action.get("context") if isinstance(action.get("context"), dict) else None,
            action=action,
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
        save_key = namespaced_action_save_message_id_key(self.incoming.account_id, action.get("save_message_id_key"))
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

    async def _apply_update_session(self, action: dict[str, Any]) -> None:
        raw_data = action.get("data")
        if raw_data is None:
            session_data_update: dict[str, Any] = {}
        elif isinstance(raw_data, dict):
            session_data_update = dict(raw_data)
        else:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="interaction_session",
                error_code="interaction_session_error",
                error="update_session data must be an object",
            )
            return

        raw_extend_seconds = action.get("extend_seconds")
        extend_seconds = _int_or_none(raw_extend_seconds)
        if raw_extend_seconds not in (None, "") and extend_seconds is None:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="interaction_session",
                error_code="interaction_session_error",
                error="update_session extend_seconds must be an integer",
            )
            return
        if extend_seconds is not None and extend_seconds < 0:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="interaction_session",
                error_code="interaction_session_error",
                error="update_session extend_seconds must be >= 0",
            )
            return

        session_key = _session_key_from_action(action)
        if not session_key:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="interaction_session",
                error_code="interaction_session_error",
                error="update_session session_key missing",
            )
            return

        try:
            from .. import account_bot_runtime as account_bot_runtime_service

            redis = self.get_redis_client()
            existing = _json_dict(await redis.get(session_key))
            if not existing:
                raise KeyError("session not found")

            expires_at = _float_or_none(existing.get("expires_at"))
            if expires_at is None:
                raise ValueError("session expires_at missing")

            now = time.time()
            if extend_seconds:
                expires_at = max(expires_at, now) + extend_seconds

            merged_session_data = dict(existing.get("data") if isinstance(existing.get("data"), dict) else {})
            merged_session_data.update(session_data_update)

            payload = dict(existing)
            payload["data"] = merged_session_data
            payload["updated_at"] = now
            payload["expires_at"] = expires_at

            grace_seconds = int(account_bot_runtime_service._INTERACTION_SESSION_TTL_GRACE_SECONDS)  # noqa: SLF001
            ttl_seconds = max(1, int(math.ceil(max(0.0, expires_at - now)))) + grace_seconds
            await redis.set(
                session_key,
                json.dumps(payload, ensure_ascii=False),
                ex=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="interaction_session",
                error_code="interaction_session_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            await self.write_log(
                self.incoming,
                "warn",
                "interaction session update failed",
                action_type="update_session",
                session_key=session_key,
                error=f"{type(exc).__name__}: {exc}",
                **self.log_context(self.incoming),
            )
            return

        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK,
            actual_send_via="interaction_session",
            result={
                "session_key": session_key,
                "expires_at": expires_at,
                "extend_seconds": extend_seconds,
                "data_keys": sorted(merged_session_data),
            },
        )

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
        parse_mode: str,
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
            parse_mode=parse_mode,
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

    async def _apply_edit_caption(
        self,
        action: dict[str, Any],
        *,
        parse_mode: str,
        reply_markup: dict[str, Any] | None,
    ) -> None:
        if "caption" in action:
            caption = str(action.get("caption") or "")
        elif "text" in action:
            caption = str(action.get("text") or "")
        else:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="caption_missing",
                error="edit_caption caption is missing",
            )
            return
        message_id = _int_or_none(action.get("message_id") or action.get("edit_message_id"))
        message_id_key = None
        if message_id is None:
            message_id_key = namespaced_action_save_message_id_key(self.incoming.account_id, action.get("message_id_key"))
            message_id = await self._read_saved_message_id(message_id_key)
        if message_id is None:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="target_message_id_missing",
                error="edit_caption message_id or message_id_key is missing",
                result={"message_id_key": message_id_key} if message_id_key else None,
            )
            return
        chat_id = _int_or_none(action.get("chat_id"))
        ok, result, used_send_via = await self._try_edit_caption_options(
            caption,
            chat_id=chat_id,
            message_id=message_id,
            send_via_options=action_send_via_options(action),
            parse_mode=parse_mode,
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
        reply_to_user_id: int | None,
        reply_to_search_limit: int | None,
        parse_mode: str,
        reply_markup: dict[str, Any] | None,
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
        action_type = str(action.get("type") or "send_photo").strip()
        filename_default = "interaction.png" if action_type == "send_photo" else "interaction.bin"
        filename = str(action.get("filename") or filename_default).strip() or filename_default
        caption = str(action.get("caption") or action.get("text") or "").strip() or None
        chat_id = _int_or_none(action.get("chat_id"))
        placeholder_chat_id = self.incoming.chat_id
        ok, _result, _used_send_via = await self._try_send_photo_options(
            photo,
            action_type=action_type,
            chat_id=chat_id,
            filename=filename,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            reply_to_user_id=reply_to_user_id,
            reply_to_search_limit=reply_to_search_limit,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
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
        save_key = namespaced_action_save_message_id_key(self.incoming.account_id, action.get("save_message_id_key"))
        if ok and save_key:
            msg_id = delivery_message_id(_result)
            if msg_id is not None:
                await self.get_redis_client().set(save_key, str(msg_id), ex=7200)
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
        if self.incoming.callback_already_acked:
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_SKIPPED,
                actual_send_via="interaction_bot",
                error_code="already_acked",
                reason_code="already_acked",
                error="callback query already acknowledged",
            )
            return
        try:
            await account_bot_service.answer_callback(
                self.incoming.token,
                callback_query_id,
                text=str(action.get("text") or ""),
                show_alert=bool(action.get("show_alert")),
            )
            self.incoming.callback_already_acked = True
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

    async def _maybe_answer_failure_callback(self, action: dict[str, Any], result: dict[str, Any]) -> None:
        failure = action.get("failure_callback")
        if not isinstance(failure, dict):
            return
        error_code = str(result.get("error_code") or "").strip()
        expected = str(failure.get("error_code") or failure.get("on_error_code") or error_code).strip()
        if expected and expected != "*" and error_code != expected:
            return
        text = str(failure.get("text") or "").strip()
        if not text:
            return
        await self._answer_callback({
            "type": "answer_callback",
            "callback_query_id": failure.get("callback_query_id") or action.get("callback_query_id") or self.incoming.callback_id,
            "text": text,
            "show_alert": bool(failure.get("show_alert", True)),
            "context": action.get("context"),
        })

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
        reply_to_user_id: int | None,
        reply_to_search_limit: int | None,
        parse_mode: str,
        send_via_options: list[str],
        edit_message_id: int | None,
        reply_markup: dict[str, Any] | None,
        reply_anchor_missing_text: str | None = None,
        context: dict[str, Any] | None = None,
        action: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.send_message(
                text,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                reply_to_user_id=reply_to_user_id,
                reply_to_search_limit=reply_to_search_limit,
                parse_mode=parse_mode,
                send_via=send_via,
                edit_message_id=edit_message_id if send_via == "interaction_bot" else None,
                reply_markup=reply_markup,
                reply_anchor_missing_text=reply_anchor_missing_text,
                context=context,
                action=action or {},
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
        parse_mode: str,
        reply_markup: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.edit_message(
                text,
                chat_id=chat_id,
                message_id=message_id,
                send_via=send_via,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            if ok:
                return True, result, send_via
            last_result = result
            await self._log_send_via_fallback(send_via, result)
        return False, last_result, send_via_options[0] if send_via_options else "interaction_bot"

    async def _try_edit_caption_options(
        self,
        caption: str,
        *,
        chat_id: int | None,
        message_id: int,
        send_via_options: list[str],
        parse_mode: str,
        reply_markup: dict[str, Any] | None,
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.edit_caption(
                caption,
                chat_id=chat_id,
                message_id=message_id,
                send_via=send_via,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
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
        action_type: str,
        chat_id: int | None,
        filename: str,
        caption: str | None,
        reply_to_message_id: int | None,
        reply_to_user_id: int | None,
        reply_to_search_limit: int | None,
        parse_mode: str,
        reply_markup: dict[str, Any] | None,
        send_via_options: list[str],
    ) -> tuple[bool, dict[str, Any], str]:
        last_result: dict[str, Any] = {}
        for send_via in send_via_options:
            ok, result = await self.send_photo(
                photo,
                action_type=action_type,
                chat_id=chat_id,
                filename=filename,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                reply_to_user_id=reply_to_user_id,
                reply_to_search_limit=reply_to_search_limit,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                send_via=send_via,
            )
            if ok:
                return True, result, send_via
            last_result = result
            await self._log_send_via_fallback(send_via, result)
        return False, last_result, send_via_options[0] if send_via_options else "interaction_bot"

    async def _log_send_via_fallback(self, send_via: str, result: dict[str, Any]) -> None:
        detail = _userbot_action_log_detail(result) if isinstance(result, dict) else {}
        log_detail = self.log_context(self.incoming)
        log_detail.update(detail)
        await self.write_log(
            self.incoming,
            "warn",
            "interaction action send_via fallback",
            send_via=send_via,
            error=result.get("error") if isinstance(result, dict) else None,
            **log_detail,
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


def namespaced_action_save_message_id_key(account_id: Any, raw: Any) -> str | None:
    key = action_save_message_id_key(raw)
    account = _int_or_none(account_id)
    if not key or account is None:
        return None
    return f"{MESSAGE_ID_NAMESPACE_PREFIX}:{account}:{key}"


def action_reply_target_key(account_id: Any, chat_id: Any, message_id: Any) -> str | None:
    account = _int_or_none(account_id)
    chat = _int_or_none(chat_id)
    message = _int_or_none(message_id)
    if account is None or chat is None or message is None or message <= 0:
        return None
    return f"{REPLY_TARGET_NAMESPACE_PREFIX}:{account}:{chat}:{message}"


async def save_action_reply_target(
    redis: Any,
    *,
    account_id: Any,
    chat_id: Any,
    message_id: Any,
    reply_to_user_id: Any,
    reply_to_display_name: str | None = None,
    reply_to_username: str | None = None,
    ttl: int = 7200,
) -> None:
    key = action_reply_target_key(account_id, chat_id, message_id)
    user_id = _int_or_none(reply_to_user_id)
    if not key or user_id is None:
        return
    data = {
        "reply_to_user_id": user_id,
        "reply_to_display_name": str(reply_to_display_name or "").strip() or None,
        "reply_to_username": str(reply_to_username or "").strip() or None,
        "saved_at": time.time(),
    }
    try:
        await redis.set(key, json.dumps(data, ensure_ascii=False), ex=ttl)
    except Exception:  # noqa: BLE001
        log.debug("save plugin action reply target failed account=%s key=%s", account_id, key, exc_info=True)


async def read_action_reply_target(
    redis: Any,
    *,
    account_id: Any,
    chat_id: Any,
    message_id: Any,
) -> dict[str, Any] | None:
    key = action_reply_target_key(account_id, chat_id, message_id)
    if not key:
        return None
    try:
        raw = await redis.get(key)
    except Exception:  # noqa: BLE001
        log.debug("read plugin action reply target failed account=%s key=%s", account_id, key, exc_info=True)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_message_not_modified_error(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _session_key_from_action(action: dict[str, Any]) -> str:
    if isinstance(action.get("session"), dict):
        session_key = str(action["session"].get("key") or "").strip()
        if session_key:
            return session_key
    session_key = str(action.get("session_key") or "").strip()
    if session_key:
        return session_key
    context = action.get("context")
    if isinstance(context, dict):
        return str(context.get("session_key") or "").strip()
    return ""


def _reply_to_user_id_from_action(action: dict[str, Any]) -> int | None:
    explicit = _int_or_none(action.get("reply_to_user_id"))
    if explicit is not None:
        return explicit
    settlement = action.get("settlement")
    if isinstance(settlement, dict):
        return _int_or_none(settlement.get("winner_user_id") or settlement.get("player_user_id"))
    return _int_or_none(action.get("winner_user_id") or action.get("player_user_id"))


def _action_parse_mode(action: dict[str, Any]) -> str:
    value = str(action.get("parse_mode") or "plain").strip().lower()
    return "html" if value == "html" else "plain"


def _result_error_code(result: Any, fallback: str) -> str:
    if isinstance(result, dict):
        code = str(result.get("error_code") or "").strip()
        if code:
            return code
        error = str(result.get("error") or "").strip()
        if error:
            return _worker_action_error_code(error)
    return fallback


def _userbot_action_failure_result(
    payload: dict[str, Any],
    *,
    error: Any,
    error_code: str,
    result: Any = None,
) -> dict[str, Any]:
    detail = _userbot_action_context(payload)
    if isinstance(result, dict):
        detail.update(result)
    detail["error"] = str(error or "")
    detail["error_code"] = error_code
    detail["worker_offline"] = error_code == "userbot_offline"
    detail["reply_anchor_missing"] = error_code == "reply_anchor_missing"
    return detail


def _userbot_action_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "chat_id": _int_or_none(payload.get("chat_id")),
        "amount": _int_or_none(payload.get("amount")),
        "reply_to_message_id": _int_or_none(payload.get("reply_to_message_id")),
        "reply_to_user_id": _int_or_none(payload.get("reply_to_user_id")),
        "reply_to_search_limit": _int_or_none(payload.get("reply_to_search_limit")),
    }


def _userbot_action_log_detail(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "chat_id",
            "amount",
            "reply_to_message_id",
            "reply_to_user_id",
            "reply_to_search_limit",
            "error_code",
            "worker_offline",
            "reply_anchor_missing",
        )
        if key in result
    }


def _worker_action_error_code(error: Any) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return "action_failed"
    if "reply_anchor_missing" in text or "近期消息" in text or "定位发奖回复目标" in text:
        return "reply_anchor_missing"
    if "worker 不在线" in text or "userbot" in text:
        return "userbot_offline"
    if "amount" in text or "金额" in text:
        return "invalid_payout_amount"
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
    "namespaced_action_save_message_id_key",
]
