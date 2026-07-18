"""Shared userbot (E3) interaction action implementations.

``_run_interaction_userbot_action`` in worker runtime and any future adapters
should call into this module so send/payout/media semantics cannot drift.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Callable
from io import BytesIO
from typing import Any

from ...redis_client import get_redis
from .. import payout_compensation, userbot_rich_message
from ..payout_limit import PayoutLimitExceeded
from .action_core import CANONICAL_ACTION_TYPES, ActionKind, classify_action
from .delivery import save_action_reply_target

log = logging.getLogger(__name__)

# Runtime injects rate-limit / humanize / payout-limit / reply-anchor helpers
# to avoid circular imports with worker.runtime.


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_userbot_payload_action_type(payload: dict[str, Any]) -> str:
    """E3 payloads use ``action_type``; normalize to shared ``type`` for classify."""

    action_type = str(payload.get("action_type") or payload.get("type") or "").strip()
    return action_type


def classify_userbot_payload(payload: dict[str, Any]) -> ActionKind:
    action_type = normalize_userbot_payload_action_type(payload)
    return classify_action({"type": action_type, **{k: v for k, v in payload.items() if k != "type"}})


async def execute_userbot_interaction_action(
    client: Any,
    payload: dict[str, Any],
    *,
    account_id: int | None = None,
    engine: Any | None = None,
    redis: Any | None = None,
    acquire_rate_limit: Callable[..., Any],
    check_payout_limit: Callable[..., Any],
    find_recent_message_id: Callable[..., Any],
    render_button_fallback: Callable[[str, dict[str, Any] | None], str],
    recent_search_limit: Callable[[Any], int],
    reply_anchor_missing_text: Callable[[dict[str, Any], int | None], str],
    parse_mode_of: Callable[[dict[str, Any]], str],
    telethon_parse_mode: Callable[[str], str | None],
    is_settlement_send: Callable[[dict[str, Any]], bool],
    simulate_humanize: Callable[..., Any],
    read_saved_message_id: Callable[..., Any],
    is_message_not_modified: Callable[[BaseException], bool],
) -> dict[str, Any]:
    """Execute one userbot interaction action; raises ValueError/PayoutLimitExceeded."""

    action_type = normalize_userbot_payload_action_type(payload)
    if not action_type:
        raise ValueError("缺少 action_type")

    # Classify for unsupported / control actions that E3 should not execute.
    kind = classify_action({"type": action_type, "send_via": payload.get("send_via")})
    if kind in {
        ActionKind.START_SESSION,
        ActionKind.UPDATE_SESSION,
        ActionKind.SESSION_CONTROL,
        ActionKind.RESULT,
        ActionKind.SETTLEMENT,
        ActionKind.ANSWER_CALLBACK,
        ActionKind.ANSWER_INLINE_QUERY,
        ActionKind.UNSUPPORTED,
    }:
        raise ValueError(f"不支持的交互动作: {action_type}")
    try:
        chat_id = int(payload["chat_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("缺少 chat_id") from exc

    reply_to_message_id = payload.get("reply_to_message_id")
    try:
        reply_to = int(reply_to_message_id) if reply_to_message_id is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("reply_to_message_id 非法") from exc
    reply_to_user_id = _int_or_none(payload.get("reply_to_user_id"))
    if reply_to is None and payload.get("reply_to_user_id") not in (None, "") and reply_to_user_id is None:
        raise ValueError("reply_to_user_id 非法") from None
    if reply_to is None and reply_to_user_id is not None:
        reply_to = await find_recent_message_id(
            client,
            chat_id,
            reply_to_user_id,
            limit=recent_search_limit(payload.get("reply_to_search_limit")),
        )
        if reply_to is None:
            text = reply_anchor_missing_text(payload, reply_to_user_id)
            if text and not bool(payload.get("suppress_reply_anchor_missing_notice")):
                await client.send_message(
                    chat_id,
                    text,
                    reply_to=None,
                    parse_mode=telethon_parse_mode(parse_mode_of(payload)),
                )
            raise ValueError(f"找不到用户 {reply_to_user_id} 在当前群的近期消息，无法定位发奖回复目标")

    if action_type in {"send_message", "payout"}:
        text = str(payload.get("text") or "").strip()
        amount: int | None = None
        compensation_replay = action_type == "payout" and bool(payload.get("_payout_compensation_replay"))
        if action_type == "payout":
            amount = _int_or_none(payload.get("amount"))
            if amount is None or amount <= 0:
                raise ValueError("payout amount 必须为正整数")
            if not text:
                text = f"+{amount}"
        if not text:
            raise ValueError("缺少 text")
        if action_type == "send_message":
            reply_markup = payload.get("reply_markup") if isinstance(payload.get("reply_markup"), dict) else None
            text = render_button_fallback(text, reply_markup)
        parse_mode = parse_mode_of(payload)
        payout_claim: payout_compensation.PayoutDeliveryClaim | None = None
        payout_redis = redis
        if action_type == "payout":
            payout_key = payout_compensation.ensure_payout_key(payload)
            payout_ok, payout_reason = await check_payout_limit(
                account_id,
                amount,
                idempotency_key=payout_key,
            )
            if not payout_ok:
                raise PayoutLimitExceeded(payout_reason or "payout 超过限额")
            payout_redis = redis or get_redis()
            if not compensation_replay:
                payout_claim = await payout_compensation.claim_payout_delivery(
                    payout_redis,
                    account_id,
                    payout_key,
                    payload=payload,
                    origin="worker_rpc",
                )
                if payout_claim.status == "sent":
                    return {
                        "message_id": payout_claim.sent_message_id or None,
                        "chat_id": chat_id,
                        "reply_to_message_id": reply_to,
                        "reply_to_user_id": reply_to_user_id,
                        "payout_key": payout_key,
                        "idempotent_replay": True,
                    }
                if payout_claim.status != "acquired":
                    raise RuntimeError("payout delivery already in progress")
        await acquire_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type=action_type,
            chat_id=chat_id,
        )
        if action_type == "send_message" and not is_settlement_send(payload):
            await simulate_humanize(client, chat_id, engine)
        try:
            msg = await client.send_message(
                chat_id,
                text,
                reply_to=reply_to,
                parse_mode=telethon_parse_mode(parse_mode),
            )
        except Exception as exc:
            if action_type == "payout" and payout_claim is not None:
                if payout_compensation.payout_error_definitely_rejected(exc):
                    await payout_compensation.release_payout_delivery_claim(payout_redis, payout_claim)
                else:
                    await payout_compensation.mark_payout_delivery_ambiguous(payout_claim, exc)
            raise
        result: dict[str, Any] = {
            "message_id": int(getattr(msg, "id", 0) or 0) or None,
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "reply_to_user_id": reply_to_user_id,
        }
        try:
            await save_action_reply_target(
                redis or get_redis(),
                account_id=account_id,
                chat_id=chat_id,
                message_id=result.get("message_id"),
                reply_to_user_id=reply_to_user_id,
                reply_to_display_name=_str_or_none(payload.get("reply_to_display_name")),
                reply_to_username=_str_or_none(payload.get("reply_to_username")),
            )
        except Exception as post_exc:  # noqa: BLE001
            if action_type == "payout":
                log.warning(
                    "payout post-send reply target save failed account=%s payout_key=%s error=%s",
                    account_id,
                    payload.get("payout_key"),
                    post_exc,
                    exc_info=True,
                )
                result["post_send_bookkeeping_failed"] = True
            else:
                raise
        if action_type == "payout" and payout_claim is not None:
            result["payout_key"] = _str_or_none(payload.get("payout_key"))
            marker_ok = await payout_compensation.complete_payout_delivery(
                payout_redis,
                payout_claim,
                account_id,
                payload.get("payout_key"),
                result.get("message_id"),
                ledger_action=payload,
                ledger_result=result,
            )
            if not marker_ok:
                result["post_send_bookkeeping_failed"] = True
                result["delivery_ambiguous"] = True
                result["error_code"] = payout_compensation.ERROR_AMBIGUOUS_DELIVERY
                await payout_compensation.mark_payout_delivery_ambiguous(
                    payout_claim,
                    "payout sent but durable completion failed",
                )
        elif action_type == "payout":
            result["payout_key"] = _str_or_none(payload.get("payout_key"))
        return result

    if kind == ActionKind.SEND_RICH_MESSAGE:
        rich_message = payload.get("rich_message")
        if not isinstance(rich_message, dict):
            raise ValueError("invalid_rich_message: rich_message 必须是对象")
        if payload.get("reply_markup") is not None:
            raise ValueError("rich_message_reply_markup_unsupported")
        await acquire_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type=action_type,
            chat_id=chat_id,
        )
        if not is_settlement_send(payload):
            await simulate_humanize(client, chat_id, engine)
        try:
            result = await userbot_rich_message.send_rich_message(
                client,
                chat_id,
                rich_message,
                reply_to_message_id=reply_to,
            )
        except userbot_rich_message.UserbotRichMessageError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc
        try:
            await save_action_reply_target(
                redis or get_redis(),
                account_id=account_id,
                chat_id=chat_id,
                message_id=result.get("message_id"),
                reply_to_user_id=reply_to_user_id,
                reply_to_display_name=_str_or_none(payload.get("reply_to_display_name")),
                reply_to_username=_str_or_none(payload.get("reply_to_username")),
            )
        except Exception:  # noqa: BLE001
            log.warning("rich message reply target save failed account=%s", account_id, exc_info=True)
            result["post_send_bookkeeping_failed"] = True
        return result

    if action_type == "edit_message":
        if isinstance(payload.get("rich_message"), dict):
            if payload.get("reply_markup") is not None:
                raise ValueError("rich_message_reply_markup_unsupported")
            try:
                message_id = int(payload["message_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("缺少 message_id") from exc
            await acquire_rate_limit(
                redis=redis,
                account_id=account_id,
                engine=engine,
                action_type=action_type,
                chat_id=chat_id,
            )
            try:
                return await userbot_rich_message.edit_rich_message(
                    client,
                    chat_id,
                    message_id,
                    payload["rich_message"],
                )
            except userbot_rich_message.UserbotRichMessageError as exc:
                raise ValueError(f"{exc.code}: {exc.message}") from exc
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("缺少 text")
        try:
            message_id = int(payload["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("缺少 message_id") from exc
        parse_mode = parse_mode_of(payload)
        await acquire_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type="edit_message",
            chat_id=chat_id,
        )
        msg = await client.edit_message(chat_id, message_id, text, parse_mode=telethon_parse_mode(parse_mode))
        return {
            "message_id": int(getattr(msg, "id", 0) or message_id) or None,
            "chat_id": chat_id,
        }

    if action_type == "edit_caption":
        if "caption" in payload:
            caption = str(payload.get("caption") or "")
        elif "text" in payload:
            caption = str(payload.get("text") or "")
        else:
            raise ValueError("缺少 caption")
        message_id = _int_or_none(payload.get("message_id") or payload.get("edit_message_id"))
        if message_id is None:
            message_id = await read_saved_message_id(redis, account_id, payload.get("message_id_key"))
        if message_id is None:
            raise ValueError("缺少 message_id")
        parse_mode = parse_mode_of(payload)
        await acquire_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type="edit_caption",
            chat_id=chat_id,
        )
        try:
            msg = await client.edit_message(
                chat_id, message_id, caption, parse_mode=telethon_parse_mode(parse_mode)
            )
        except Exception as exc:  # noqa: BLE001
            if is_message_not_modified(exc):
                return {"message_id": message_id, "chat_id": chat_id, "not_modified": True}
            raise
        return {
            "message_id": int(getattr(msg, "id", 0) or message_id) or None,
            "chat_id": chat_id,
        }

    if action_type == "delete_message":
        message_id = _int_or_none(payload.get("message_id"))
        if message_id is None:
            raise ValueError("缺少 message_id")
        await client.delete_messages(chat_id, [message_id])
        return {"message_id": message_id, "chat_id": chat_id}

    if action_type == "pin_message":
        message_id = _int_or_none(payload.get("message_id"))
        if message_id is None:
            raise ValueError("缺少 message_id")
        await client.pin_message(chat_id, message_id, notify=False)
        return {"message_id": message_id, "chat_id": chat_id}

    if action_type in {"send_photo", "send_file"}:
        raw_base64 = str(payload.get("file_base64") or payload.get("photo_base64") or "").strip()
        if not raw_base64:
            raise ValueError("缺少媒体内容")
        try:
            file_bytes = base64.b64decode(raw_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("媒体 base64 非法") from exc
        if not file_bytes:
            raise ValueError("媒体内容为空")
        filename = str(
            payload.get("filename")
            or ("interaction.png" if action_type == "send_photo" else "interaction.bin")
        ).strip()
        caption = str(payload.get("caption") or payload.get("text") or "").strip() or None
        file_obj = BytesIO(file_bytes)
        file_obj.name = filename or "interaction.bin"
        kwargs: dict[str, Any] = {"reply_to": reply_to}
        if caption:
            kwargs["caption"] = caption[:1024]
            kwargs["parse_mode"] = telethon_parse_mode(parse_mode_of(payload))
        if action_type == "send_photo":
            kwargs["force_document"] = False
        await acquire_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type=action_type,
            chat_id=chat_id,
        )
        msg = await client.send_file(chat_id, file_obj, **kwargs)
        result = {
            "message_id": int(getattr(msg, "id", 0) or 0) or None,
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "reply_to_user_id": reply_to_user_id,
        }
        await save_action_reply_target(
            redis or get_redis(),
            account_id=account_id,
            chat_id=chat_id,
            message_id=result.get("message_id"),
            reply_to_user_id=reply_to_user_id,
            reply_to_display_name=_str_or_none(payload.get("reply_to_display_name")),
            reply_to_username=_str_or_none(payload.get("reply_to_username")),
        )
        return result

    raise ValueError(f"不支持的交互动作: {action_type}")


__all__ = [
    "CANONICAL_ACTION_TYPES",
    "classify_userbot_payload",
    "execute_userbot_interaction_action",
    "normalize_userbot_payload_action_type",
]
