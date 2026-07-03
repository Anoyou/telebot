"""账号绑定 Bot 的 polling runtime 与命令处理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import desc, select

from ..account_bot_defaults import (
    DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE,
    DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
    DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
    DEFAULT_INTERACTION_RESPONSE_TEMPLATE,
    DEFAULT_TRANSFER_NOTICE_TEMPLATE,
)
from ..db.base import AsyncSessionLocal
from ..db.models.account import Account
from ..db.models.account_bot import (
    ACCOUNT_BOT_ROLE_ADMIN,
    ACCOUNT_BOT_ROLE_OPERATOR,
    ACCOUNT_BOT_STATUS_DISABLED,
    ACCOUNT_BOT_STATUS_ERROR,
    ACCOUNT_BOT_STATUS_RUNNING,
    ACCOUNT_BOT_STATUS_STOPPED,
    AccountBot,
)
from ..db.models.command import AccountCommandLink
from ..db.models.feature import AccountFeature, Feature
from ..db.models.log import LEVEL_ERROR, LEVEL_WARN, RuntimeLog
from ..db.models.rule import Rule
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..settings import settings
from ..worker.ipc import (
    CMD_EXECUTE_RULE,
    CMD_RELOAD_CONFIG,
    CMD_RUN_INTERACTION_ACTION,
    CMD_RUN_INTERACTION_ENTRY,
    GLOBAL_CHANNEL,
    RUNTIME_LOG_STREAM,
    IPCMessage,
    RuntimeLogPayload,
    cmd_channel,
    make_cmd,
    publish_cmd_with_ack,
)
from . import (
    account_bot_service,
    account_service,
    audit,
    command_service,
    feature_service,
    remote_plugin_service,
)
from .event_bus import (
    EVENT_REASON_CODES,
    dispatch_event,
    normalize_bot_update,
    normalize_event_subscription,
    normalize_payment_notice,
)
from .event_trace import (
    TRACE_STATUS_FAILED,
    TRACE_STATUS_OK,
    TRACE_STATUS_SKIPPED,
    TRACE_STATUS_WARNING,
    finish_trace,
    record_action,
    record_span,
    start_trace,
    trace_log_context,
    update_plugin_runtime_status,
)
from .interaction.contracts import guard_interaction_actions
from .interaction.delivery import (
    INTERACTION_SESSION_CONTROL_ACTIONS as _INTERACTION_SESSION_CONTROL_ACTIONS,
)
from .interaction.delivery import (
    InteractionDeliveryExecutor,
    action_save_message_id_key,
    delivery_message_id,
)

log = logging.getLogger(__name__)

_TASKS: dict[int, asyncio.Task[None]] = {}
_INTERACTION_TASKS: dict[int, asyncio.Task[None]] = {}
_TRANSFER_TEST_TASKS: dict[int, asyncio.Task[None]] = {}
_TASK_LOCK = asyncio.Lock()
_CONFIRM_PREFIX = "account_bot_confirm:"
_CONFIRM_TTL_SECONDS = 300
_MAX_BUTTON_ROWS = 24
_REMOTE_POLICY_HINT = "该功能默认关闭，请管理员在 Web 控制台启用后重试（高风险操作，仍需二次确认）。"
_RUNTIME_NOTIFY_DEDUPE_TTL_SECONDS = 180
_RUNTIME_NOTIFY_DEDUPE_PREFIX = "account_bot:runtime_notify:"
_MATH_GAME_PREFIX = "account_bot:math_game:"
_MATH_GAME_CLAIM_PREFIX = "account_bot:math_game_claim:"
_MATH_GAME_TTL_SECONDS = 900
_INTERACTION_RULE_STATE_PREFIX = "account_bot:interaction_rule_state:"
_INTERACTION_TRIGGER_DEDUPE_PREFIX = "account_bot:interaction_trigger:"
_INTERACTION_SESSION_PREFIX = "account_bot:interaction_session:"
_INTERACTION_USER_COOLDOWN_PREFIX = "account_bot:interaction_user_cooldown:"
_INTERACTION_USER_DAILY_PREFIX = "account_bot:interaction_user_daily:"
_INTERACTION_USER_PENDING_PREFIX = "account_bot:interaction_user_pending:"
_INTERACTION_PAYMENT_CONFIRM_PREFIX = "account_bot:interaction_payment_confirm:"
_INTERACTION_PAYMENT_CONFIRM_TTL_SECONDS = 300
_TRANSFER_COMMAND_DEDUPE_TTL_SECONDS = 30.0
_INTERACTION_ENTRY_TIMEOUT_SECONDS = 60.0
_INTERACTION_DEBUG_STATE_PREFIX = "account_bot:interaction_debug:"
_INTERACTION_DEBUG_WARNINGS_PREFIX = "account_bot:interaction_debug_warnings:"
_INTERACTION_DEBUG_TTL_SECONDS = 86400
_INTERACTION_DEBUG_WARNING_LIMIT = 20
AUTO_PAYOUT_MODULE_KEYS = {"game24", "math10", "dice_grid_hunt", "guess_number", "poetry_blank"}
_INTERACTION_PAYMENT_CONFIRM_CALLBACK_PREFIX = "ip"
_EVENT_FRAMEWORK_FLAGS_CACHE: tuple[float, dict[str, bool]] = (0.0, {})
_PLAYER_IDENTITY_CONFIDENCE_VERIFIED = "verified_user_id"
_PLAYER_IDENTITY_CONFIDENCE_REPLY = "reply_context"
_PLAYER_IDENTITY_CONFIDENCE_CALLBACK = "callback_confirmed"
_PLAYER_IDENTITY_CONFIDENCE_NAME_ONLY = "name_only"
_PLAYER_IDENTITY_CONFIDENCE_UNKNOWN = "unknown"
_MODULE_PAYMENT_AMOUNT_KEYS = ("amount", "bet", "entry_amount", "entry_fee", "stake")
_COMMON_COMMAND_PREFIXES = (",", "。", ".", "/", "!", "！", "-", "、")
_TRANSFER_COMMAND_DEDUPE: dict[str, float] = {}


async def _load_command_prefix(db) -> str:
    prefix = settings.command_prefix or ","
    row = await db.get(SystemSetting, "command_prefix")
    if row is not None:
        raw = row.value
        if isinstance(raw, dict):
            value = str(raw.get("value", "") or "").strip()
        else:
            value = str(raw or "").strip()
        if value:
            prefix = value
    return prefix


def _looks_like_command_name(cmd: str, *, prefix: str) -> bool:
    name = str(cmd or "").strip()
    if not name:
        return False
    if prefix and name.startswith(prefix):
        return False
    return any(ch.isalnum() or ch == "_" for ch in name)


async def _incoming_is_userbot_command_text(db: Any, incoming: Incoming) -> bool:
    text = str(incoming.text or "").strip()
    if incoming.callback_id or not text:
        return False
    candidate_prefixes = {settings.command_prefix or "", *_COMMON_COMMAND_PREFIXES}
    if not any(prefix and text.startswith(prefix) for prefix in candidate_prefixes):
        return False
    prefix = await _load_command_prefix(db)
    if not prefix or not text.startswith(prefix):
        return False
    rest = text[len(prefix):].lstrip()
    if not rest:
        return False
    token = rest.split(None, 1)[0].strip()
    return _looks_like_command_name(token, prefix=prefix)


async def _event_framework_flags() -> dict[str, bool]:
    """Read runtime safety switches for Trace/Event Bus/Inline delivery."""

    global _EVENT_FRAMEWORK_FLAGS_CACHE
    now = time.monotonic()
    cached_at, cached = _EVENT_FRAMEWORK_FLAGS_CACHE
    if cached and now - cached_at < 30:
        return cached
    defaults = {
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
        "inline_updates_enabled": True,
        "native_raw_persist_enabled": False,
    }
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "log_retention")
        raw = row.value if row is not None and isinstance(row.value, dict) else {}
        flags = {
            "trace_enabled": bool(raw.get("trace_enabled", defaults["trace_enabled"])),
            "event_bus_delivery_enabled": bool(
                raw.get("event_bus_delivery_enabled", defaults["event_bus_delivery_enabled"])
            ),
            "inline_updates_enabled": bool(raw.get("inline_updates_enabled", defaults["inline_updates_enabled"])),
            "native_raw_persist_enabled": bool(
                raw.get("native_raw_persist_enabled", defaults["native_raw_persist_enabled"])
            ),
        }
    except Exception:  # noqa: BLE001
        log.debug("load event framework flags failed, using defaults", exc_info=True)
        flags = defaults
    _EVENT_FRAMEWORK_FLAGS_CACHE = (now, flags)
    return flags


def _inline_update_allowed_updates(flags: dict[str, bool], base: list[str]) -> list[str]:
    updates = list(base)
    if flags.get("inline_updates_enabled", True):
        for item in ("inline_query", "chosen_inline_result"):
            if item not in updates:
                updates.append(item)
    return updates


@dataclass(slots=True)
class Incoming:
    account_id: int
    token: str
    update_id: int
    user_id: int | None
    chat_id: int | None
    message_id: int | None
    text: str
    chat_type: str | None = None
    callback_id: str | None = None
    callback_data: str | None = None
    inline_query_id: str | None = None
    inline_query_text: str | None = None
    inline_offset: str | None = None
    inline_chat_type: str | None = None
    chosen_inline_result_id: str | None = None
    display_name: str | None = None
    username: str | None = None
    sender_chat_id: int | None = None
    sender_chat_type: str | None = None
    sender_chat_title: str | None = None
    sender_chat_username: str | None = None
    reply_to_user_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_display_name: str | None = None
    reply_to_username: str | None = None
    reply_to_sender_chat_id: int | None = None
    reply_to_sender_chat_type: str | None = None
    reply_to_sender_chat_title: str | None = None
    reply_to_sender_chat_username: str | None = None
    reply_to_text: str | None = None
    entity_languages: tuple[str, ...] = ()
    trace_id: str | None = None
    native_raw: dict[str, Any] | None = None


@dataclass
class MathGameState:
    account_id: int
    chat_id: int
    question: str
    answer: int
    prize: int = 123
    active: bool = True
    game_id: str = ""
    created_at: float = 0.0
    source_update_id: int | None = None
    source_message_id: int | None = None
    winner_update_id: int | None = None
    winner_message_id: int | None = None


_MATH_GAMES: dict[tuple[int, int], MathGameState] = {}


@dataclass
class InteractionEvent:
    """交互 Bot 投递给模块的标准事件。"""

    type: str
    account_id: int
    chat_id: int | None
    rule_id: str
    rule_name: str
    module_key: str
    entry_key: str
    update_id: int
    message_id: int | None
    user_id: int | None
    chat_type: str | None
    display_name: str | None
    username: str | None
    text: str
    sender_chat_id: int | None = None
    sender_chat_type: str | None = None
    sender_chat_title: str | None = None
    sender_chat_username: str | None = None
    callback_query_id: str | None = None
    callback_data: str | None = None
    reply_to_user_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_display_name: str | None = None
    reply_to_username: str | None = None
    reply_to_sender_chat_id: int | None = None
    reply_to_sender_chat_type: str | None = None
    reply_to_sender_chat_title: str | None = None
    reply_to_sender_chat_username: str | None = None
    reply_to_text: str | None = None
    entity_languages: tuple[str, ...] = ()
    data: dict[str, Any] | None = None


def _button(text: str, action: str, resource: str = "_", *, aid: int, nonce: str | None = None) -> dict[str, str]:
    data = f"ab:{aid}:{action}:{resource}"
    if nonce:
        data += f":{nonce}"
    return {"text": text, "callback_data": data[:64]}


def _keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _main_keyboard(aid: int) -> dict[str, Any]:
    return _keyboard(
        [
            [
                _button("状态", "view", "status", aid=aid),
                _button("功能", "view", "features", aid=aid),
                _button("命令", "view", "commands", aid=aid),
            ],
            [
                _button("插件", "view", "plugins", aid=aid),
                _button("规则", "view", "rules", aid=aid),
                _button("日志", "view", "logs", aid=aid),
            ],
            [
                _button("帮助", "view", "help", aid=aid),
            ],
        ]
    )


def _parse_callback(data: str) -> tuple[int, str, str, str | None] | None:
    parts = data.split(":")
    if len(parts) not in {4, 5} or parts[0] != "ab":
        return None
    try:
        aid = int(parts[1])
    except ValueError:
        return None
    return aid, parts[2], parts[3], parts[4] if len(parts) == 5 else None


def _interaction_payment_confirm_callback_data(nonce: str) -> str:
    return f"{_INTERACTION_PAYMENT_CONFIRM_CALLBACK_PREFIX}:{nonce}"[:64]


def _parse_interaction_payment_confirm_callback(data: str | None) -> str | None:
    parts = str(data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != _INTERACTION_PAYMENT_CONFIRM_CALLBACK_PREFIX:
        return None
    nonce = parts[1].strip()
    return nonce or None


def _interaction_payment_confirm_key(nonce: str) -> str:
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    return _INTERACTION_PAYMENT_CONFIRM_PREFIX + digest


async def _consume_interaction_payment_confirm_payload(redis: Any, nonce: str) -> str | None:
    key = _interaction_payment_confirm_key(nonce)
    getdel = getattr(redis, "getdel", None)
    if callable(getdel):
        raw = await getdel(key)
        return raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
    raw = await redis.get(key)
    if raw:
        delete = getattr(redis, "delete", None)
        if callable(delete):
            await delete(key)
    return raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw


async def _read_interaction_payment_confirm_payload(redis: Any, nonce: str) -> str | None:
    raw = await redis.get(_interaction_payment_confirm_key(nonce))
    return raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw


def _payment_confirm_name_matches(expected: Any, incoming: Incoming) -> bool:
    expected_name = str(expected or "").strip().casefold()
    if not expected_name:
        return True
    actuals = {
        str(value or "").strip().casefold()
        for value in (
            incoming.display_name,
            incoming.username,
            f"@{incoming.username}" if incoming.username else None,
        )
        if str(value or "").strip()
    }
    return expected_name in actuals


def _confirm_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _confirm_redis_key(token: str) -> str:
    return _CONFIRM_PREFIX + _confirm_token_hash(token)


async def _consume_confirm_payload(redis: Any, token: str) -> str | None:
    key = _confirm_redis_key(token)
    getdel = getattr(redis, "getdel", None)
    if callable(getdel):
        return await getdel(key)
    return await redis.eval(
        "local v=redis.call('GET',KEYS[1]); if v then redis.call('DEL',KEYS[1]); end; return v;",
        1,
        key,
    )


async def _read_confirm_payload(redis: Any, token: str) -> str | None:
    return await redis.get(_confirm_redis_key(token))


async def start_account_bot_manager() -> None:
    """启动所有 enabled 的账号 Bot polling task。"""

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(AccountBot).where(AccountBot.enabled.is_(True))
            )
        ).scalars().all()
    for row in rows:
        await restart_account_bot(int(row.account_id))
    log.info("account bot manager started: %d management task(s)", len(rows))


async def stop_account_bot_manager() -> None:
    """停止所有账号 Bot polling task。"""

    async with _TASK_LOCK:
        tasks = list(_TASKS.values())
        _TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def restart_account_bot(aid: int) -> None:
    """重启单个账号 Bot polling task。"""

    async with _TASK_LOCK:
        old = _TASKS.pop(aid, None)
        if old is not None:
            old.cancel()
        should_start = False
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(AccountBot).where(AccountBot.account_id == aid)
                )
            ).scalar_one_or_none()
            should_start = bool(row and row.enabled and row.bot_token_enc)
            if row and not should_start:
                row.status = ACCOUNT_BOT_STATUS_DISABLED if not row.enabled else ACCOUNT_BOT_STATUS_STOPPED
                await db.commit()
        if should_start:
            _TASKS[aid] = asyncio.create_task(_polling_loop(aid), name=f"account-bot:{aid}")
    if old is not None:
        await asyncio.gather(old, return_exceptions=True)


async def sync_account_bot(aid: int) -> None:
    """配置变更后同步运行时。"""

    await restart_account_bot(aid)


async def start_interaction_bot_manager() -> int:
    """启动所有已启用的交互 Bot polling task。"""

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(SystemSetting).where(
                    SystemSetting.key.like(f"{account_bot_service.TRANSFER_NOTICE_SETTING_PREFIX}%")
                )
            )
        ).scalars().all()
    count = 0
    for row in rows:
        try:
            aid = int(str(row.key).removeprefix(account_bot_service.TRANSFER_NOTICE_SETTING_PREFIX))
        except ValueError:
            continue
        cfg = account_bot_service.normalize_transfer_notice_config(row.value)
        if cfg.get("enabled") and (cfg.get("interaction_bot_token_enc") or cfg.get("transfer_bot_token_enc")):
            await restart_interaction_bot(aid)
            count += 1
    return count


async def stop_interaction_bot_manager() -> None:
    """停止所有交互 Bot polling task。"""

    async with _TASK_LOCK:
        tasks = list(_INTERACTION_TASKS.values()) + list(_TRANSFER_TEST_TASKS.values())
        _INTERACTION_TASKS.clear()
        _TRANSFER_TEST_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def is_interaction_bot_running(aid: int) -> bool:
    """返回指定账号的交互 Bot polling task 是否仍在运行。"""

    task = _INTERACTION_TASKS.get(int(aid))
    return bool(task and not task.done())


async def restart_interaction_bot(aid: int) -> None:
    """重启单个交互 Bot polling task。"""

    async with _TASK_LOCK:
        old = _INTERACTION_TASKS.pop(aid, None)
        old_transfer = _TRANSFER_TEST_TASKS.pop(aid, None)
        if old is not None:
            old.cancel()
        if old_transfer is not None:
            old_transfer.cancel()
        should_start = False
        should_start_transfer = False
        async with AsyncSessionLocal() as db:
            cfg = await account_bot_service.get_transfer_notice_config(db, aid)
            should_start = bool(cfg.get("enabled") and cfg.get("has_interaction_bot_token"))
            should_start_transfer = bool(cfg.get("enabled") and cfg.get("has_transfer_bot_token"))
            await _set_interaction_runtime_state(db, aid, error=None)
            await _set_transfer_test_runtime_state(db, aid, error=None)
        if should_start:
            _INTERACTION_TASKS[aid] = asyncio.create_task(
                _interaction_polling_loop(aid),
                name=f"interaction-bot:{aid}",
            )
        if should_start_transfer:
            _TRANSFER_TEST_TASKS[aid] = asyncio.create_task(
                _transfer_test_polling_loop(aid),
                name=f"transfer-test-bot:{aid}",
            )
    if old is not None:
        await asyncio.gather(old, return_exceptions=True)
    if old_transfer is not None:
        await asyncio.gather(old_transfer, return_exceptions=True)


async def notify_account(account_id: int, text: str) -> int:
    """给某账号 Bot 的已授权通知用户发送消息。"""

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(AccountBot).where(
                    AccountBot.account_id == account_id,
                    AccountBot.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None or not row.bot_token_enc:
            return 0
        token = account_bot_service.decrypt_bot_token(row)
        users = await account_bot_service.list_bot_users(db, account_id)
        targets = [
            u for u in users
            if u.enabled and u.notify_enabled and u.last_chat_id is not None
        ]
    sent = 0
    failed = 0
    trace = None
    final_status = TRACE_STATUS_SKIPPED
    flags = await _event_framework_flags()
    if targets and flags.get("trace_enabled", True):
        trace = await start_trace(
            {
                "source": {
                    "account_id": account_id,
                    "channel": "account_bot",
                    "type": "system_notice",
                },
                "message": {"text": text},
            }
        )
    for user in targets:
        action = {
            "type": "send_message",
            "send_via": "account_bot",
            "chat_id": int(user.last_chat_id),
            "text": text,
            "context": trace_log_context(trace, plugin_key="system_notify"),
        }
        try:
            result = await account_bot_service.send_message(
                token,
                int(user.last_chat_id),
                text,
                parse_mode="HTML",
            )
            await record_action(trace, action, TRACE_STATUS_OK, actual_send_via="account_bot", result=result)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
            await record_action(
                trace,
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="account_bot",
                error_code="telegram_api_error",
                error="account bot notify failed",
            )
            log.debug("account bot notify failed aid=%s tg_user=%s", account_id, user.tg_user_id, exc_info=True)
    final_status = TRACE_STATUS_FAILED if failed else TRACE_STATUS_OK if sent else final_status
    await finish_trace(trace, final_status, sent_count=sent, failed_count=failed, target_count=len(targets))
    return sent


async def notify_runtime_log(row: RuntimeLog) -> None:
    """运行日志落库后触发账号级错误通知。"""

    if row.account_id is None or row.level not in {LEVEL_ERROR, LEVEL_WARN}:
        return
    source = account_bot_service.html_text(row.source or "worker")
    message = account_bot_service.html_text(row.message)
    digest = hashlib.sha256(
        f"{int(row.account_id)}|{row.level}|{row.source or 'worker'}|{row.message}".encode()
    ).hexdigest()
    dedupe_key = f"{_RUNTIME_NOTIFY_DEDUPE_PREFIX}{int(row.account_id)}:{digest}"
    redis = get_redis()
    try:
        first_hit = await redis.set(
            dedupe_key,
            "1",
            ex=_RUNTIME_NOTIFY_DEDUPE_TTL_SECONDS,
            nx=True,
        )
    except Exception:  # noqa: BLE001
        first_hit = True
    if not first_hit:
        return
    await notify_account(
        int(row.account_id),
        f"⚠️ <b>账号运行告警</b>\n来源：<code>{source}</code>\n内容：{message}",
    )


async def _polling_loop(aid: int) -> None:
    backoff = 2.0
    token = ""
    try:
        while True:
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(
                        select(AccountBot).where(AccountBot.account_id == aid)
                    )
                ).scalar_one_or_none()
                if row is None or not row.enabled or not row.bot_token_enc:
                    if row is not None:
                        row.status = ACCOUNT_BOT_STATUS_DISABLED
                        await db.commit()
                    return
                try:
                    token = account_bot_service.decrypt_bot_token(row)
                except Exception as exc:  # noqa: BLE001
                    row.status = ACCOUNT_BOT_STATUS_ERROR
                    row.last_error = account_bot_service.sanitize_bot_error(exc)
                    await db.commit()
                    return
                offset = (row.last_update_id + 1) if row.last_update_id is not None else None
                if row.status != ACCOUNT_BOT_STATUS_RUNNING:
                    row.status = ACCOUNT_BOT_STATUS_RUNNING
                    row.last_error = None
                    await db.commit()
                event_flags = await _event_framework_flags()

            try:
                result = await account_bot_service.call_bot_api(
                    token,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "allowed_updates": _inline_update_allowed_updates(
                            event_flags,
                            ["message", "callback_query"],
                        ),
                    },
                )
                updates = result.get("result") if isinstance(result, dict) else result
                if not isinstance(updates, list):
                    updates = []
                backoff = 2.0
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    try:
                        await _handle_update(aid, token, update)
                    except Exception:  # noqa: BLE001
                        log.exception("account bot update failed aid=%s update_id=%s", aid, update_id)
                    finally:
                        async with AsyncSessionLocal() as db:
                            row = (
                                await db.execute(
                                    select(AccountBot).where(AccountBot.account_id == aid)
                                )
                            ).scalar_one_or_none()
                            if row is not None:
                                if row.last_update_id is None or update_id > row.last_update_id:
                                    row.last_update_id = update_id
                                row.status = ACCOUNT_BOT_STATUS_RUNNING
                                row.last_error = None
                                await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                clean = account_bot_service.sanitize_bot_error(exc, token=token)
                clean = account_bot_service.label_bot_polling_error(clean, role="management")
                async with AsyncSessionLocal() as db:
                    row = (
                        await db.execute(
                            select(AccountBot).where(AccountBot.account_id == aid)
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        row.status = ACCOUNT_BOT_STATUS_ERROR
                        row.last_error = clean
                        await db.commit()
                log.warning("account bot polling error aid=%s: %s", aid, clean)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    except asyncio.CancelledError:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(AccountBot).where(AccountBot.account_id == aid)
                )
            ).scalar_one_or_none()
            if row is not None and row.enabled:
                row.status = ACCOUNT_BOT_STATUS_STOPPED
                await db.commit()
        raise


async def _load_interaction_runtime_config(aid: int) -> tuple[str | None, dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        cfg = await account_bot_service.get_transfer_notice_config(db, aid)
        token = await account_bot_service.get_interaction_bot_token(db, aid)
    return token, cfg


async def _set_interaction_runtime_state(
    db: Any,
    aid: int,
    *,
    last_update_id: int | None = None,
    error: str | None = None,
) -> None:
    row = await db.get(SystemSetting, account_bot_service.transfer_notice_setting_key(aid))
    if row is None or not isinstance(row.value, dict):
        return
    data = account_bot_service.normalize_transfer_notice_config(row.value)
    if last_update_id is not None:
        data["interaction_last_update_id"] = last_update_id
    data["interaction_last_error"] = error
    row.value = data
    await db.commit()


async def _load_transfer_test_runtime_config(aid: int) -> tuple[str | None, dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        cfg = await account_bot_service.get_transfer_notice_config(db, aid)
        token = await account_bot_service.get_transfer_bot_token(db, aid)
    return token, cfg


async def _set_transfer_test_runtime_state(
    db: Any,
    aid: int,
    *,
    last_update_id: int | None = None,
    error: str | None = None,
) -> None:
    row = await db.get(SystemSetting, account_bot_service.transfer_notice_setting_key(aid))
    if row is None or not isinstance(row.value, dict):
        return
    data = account_bot_service.normalize_transfer_notice_config(row.value)
    if last_update_id is not None:
        data["transfer_last_update_id"] = last_update_id
    data["transfer_last_error"] = error
    row.value = data
    await db.commit()


async def _interaction_polling_loop(aid: int) -> None:
    backoff = 2.0
    token = ""
    try:
        while True:
            token_opt, cfg = await _load_interaction_runtime_config(aid)
            if not cfg.get("enabled") or not token_opt:
                async with AsyncSessionLocal() as db:
                    await _set_interaction_runtime_state(db, aid, error=None)
                return
            token = token_opt
            offset = (int(cfg["interaction_last_update_id"]) + 1) if cfg.get("interaction_last_update_id") is not None else None
            event_flags = await _event_framework_flags()

            try:
                result = await account_bot_service.call_bot_api(
                    token,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "allowed_updates": _inline_update_allowed_updates(
                            event_flags,
                            ["message", "callback_query"],
                        ),
                    },
                )
                updates = result.get("result") if isinstance(result, dict) else result
                if not isinstance(updates, list):
                    updates = []
                backoff = 2.0
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    try:
                        await _handle_interaction_update(aid, token, update)
                    except Exception:  # noqa: BLE001
                        log.exception("interaction bot update failed aid=%s update_id=%s", aid, update_id)
                    finally:
                        async with AsyncSessionLocal() as db:
                            await _set_interaction_runtime_state(db, aid, last_update_id=update_id, error=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                clean = account_bot_service.sanitize_bot_error(exc, token=token)
                clean = account_bot_service.label_bot_polling_error(clean, role="interaction")
                async with AsyncSessionLocal() as db:
                    await _set_interaction_runtime_state(db, aid, error=clean)
                log.warning("interaction bot polling error aid=%s: %s", aid, clean)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    except asyncio.CancelledError:
        async with AsyncSessionLocal() as db:
            await _set_interaction_runtime_state(db, aid, error="已停止")
        raise


async def _transfer_test_polling_loop(aid: int) -> None:
    backoff = 2.0
    token = ""
    try:
        while True:
            token_opt, cfg = await _load_transfer_test_runtime_config(aid)
            if not cfg.get("enabled") or not token_opt:
                async with AsyncSessionLocal() as db:
                    await _set_transfer_test_runtime_state(db, aid, error=None)
                return
            token = token_opt
            offset = (int(cfg["transfer_last_update_id"]) + 1) if cfg.get("transfer_last_update_id") is not None else None

            try:
                result = await account_bot_service.call_bot_api(
                    token,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    },
                )
                updates = result.get("result") if isinstance(result, dict) else result
                if not isinstance(updates, list):
                    updates = []
                backoff = 2.0
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    try:
                        await _handle_transfer_test_update(aid, token, update)
                    except Exception:  # noqa: BLE001
                        log.exception("transfer test bot update failed aid=%s update_id=%s", aid, update_id)
                    finally:
                        async with AsyncSessionLocal() as db:
                            await _set_transfer_test_runtime_state(db, aid, last_update_id=update_id, error=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                clean = account_bot_service.sanitize_bot_error(exc, token=token)
                clean = account_bot_service.label_bot_polling_error(clean, role="transfer_test")
                async with AsyncSessionLocal() as db:
                    await _set_transfer_test_runtime_state(db, aid, error=clean)
                log.warning("transfer test bot polling error aid=%s: %s", aid, clean)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    except asyncio.CancelledError:
        async with AsyncSessionLocal() as db:
            await _set_transfer_test_runtime_state(db, aid, error="已停止")
        raise


async def _handle_update(aid: int, token: str, update: dict[str, Any]) -> None:
    incoming = _extract_incoming(aid, token, update)
    if incoming is None:
        return
    flags = await _event_framework_flags()
    if (incoming.inline_query_id or incoming.chosen_inline_result_id) and not flags.get("inline_updates_enabled", True):
        return
    trace = None
    if flags.get("trace_enabled", True):
        trace = await start_trace(_incoming_trace_payload(incoming, channel="account_bot"))
        incoming.trace_id = trace.trace_id
        await record_span(trace, "receive", TRACE_STATUS_OK, component="account_bot", **_interaction_log_context(incoming))
    final_status = TRACE_STATUS_SKIPPED
    try:
        async with AsyncSessionLocal() as db:
            user = None
            if incoming.user_id is not None:
                user = await account_bot_service.find_bot_user(db, aid, incoming.user_id)
            if user is None or not user.enabled:
                await record_span(
                    trace,
                    "route",
                    TRACE_STATUS_SKIPPED,
                    component="account_bot",
                    reason_code="account_bot_user_unauthorized",
                )
                if incoming.text.startswith("/start") or incoming.text.startswith("/help"):
                    await _send(
                        incoming,
                        "你还没有被授权使用这个账号 Bot。\n"
                        f"请在 GUI 的账号详情 → Bot 联动里添加 Telegram 用户 ID：<code>{incoming.user_id}</code>",
                        reply_markup=None,
                    )
                elif incoming.callback_id:
                    await _answer_callback(incoming, text="未授权", show_alert=True)
                return
            if incoming.chat_id is not None:
                user.last_chat_id = incoming.chat_id
            if incoming.display_name and not user.display_name:
                user.display_name = incoming.display_name
            await db.commit()
            role = user.role

        if incoming.callback_id and incoming.callback_data:
            await record_span(
                trace,
                "route",
                TRACE_STATUS_OK,
                component="account_bot_callback",
                reason_code="callback_query",
            )
            await _handle_callback(incoming, role)
            final_status = TRACE_STATUS_OK
            return
        if not _should_route_text_to_account_commands(incoming):
            await record_span(
                trace,
                "route",
                TRACE_STATUS_SKIPPED,
                component="account_bot",
                reason_code="command_not_matched",
            )
            return
        await record_span(
            trace,
            "route",
            TRACE_STATUS_OK,
            component="account_bot_command",
            reason_code="command_matched",
        )
        await _handle_command(incoming, role)
        final_status = TRACE_STATUS_OK
    except PermissionError as exc:
        final_status = TRACE_STATUS_FAILED
        await record_span(
            trace,
            "finish",
            TRACE_STATUS_FAILED,
            component="account_bot",
            reason_code="permission_denied",
            error=str(exc),
        )
        if incoming.callback_id:
            await _answer_callback(incoming, text=str(exc), show_alert=True)
        else:
            await _send(incoming, f"权限不足：{account_bot_service.html_text(exc)}")
    except Exception as exc:  # noqa: BLE001
        final_status = TRACE_STATUS_FAILED
        await record_span(
            trace,
            "finish",
            TRACE_STATUS_FAILED,
            component="account_bot",
            reason_code="handler_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        await finish_trace(trace, final_status)


async def _handle_interaction_update(aid: int, token: str, update: dict[str, Any]) -> None:
    incoming = _extract_incoming(aid, token, update)
    if incoming is None:
        return
    flags = await _event_framework_flags()
    if (incoming.inline_query_id or incoming.chosen_inline_result_id) and not flags.get("inline_updates_enabled", True):
        return
    trace = None
    if flags.get("trace_enabled", True):
        trace = await start_trace(_incoming_trace_payload(incoming))
        incoming.trace_id = trace.trace_id
        await record_span(trace, "receive", TRACE_STATUS_OK, component="interaction_bot", **_interaction_log_context(incoming))
    final_status = TRACE_STATUS_SKIPPED
    try:
        async with AsyncSessionLocal() as db:
            if await _try_handle_interaction_payment_confirm(db, incoming):
                final_status = TRACE_STATUS_OK
                await record_span(trace, "route", TRACE_STATUS_OK, component="interaction_payment_confirm")
                return
            cfg = await account_bot_service.get_transfer_notice_config(db, incoming.account_id)
            if incoming.user_id is not None and _int_or_none(cfg.get("interaction_bot_id")) == incoming.user_id:
                await record_span(trace, "route", TRACE_STATUS_SKIPPED, component="interaction_bot", reason_code="bot_self_message")
                return
            if await _incoming_is_userbot_command_text(db, incoming):
                await record_span(
                    trace,
                    "route",
                    TRACE_STATUS_SKIPPED,
                    component="interaction_bot",
                    reason_code="userbot_command_message",
                    message="系统前缀命令由 userbot 命令链路处理，交互 Bot 不投递规则或会话。",
                )
                return
            if await _try_handle_transfer_command(db, incoming):
                final_status = TRACE_STATUS_OK
                await record_span(trace, "route", TRACE_STATUS_OK, component="transfer_command")
                return
            event_bus_delivery_enabled = flags.get("event_bus_delivery_enabled", True)
            if await _try_handle_transfer_notice(
                db,
                incoming,
                event_bus_enabled=event_bus_delivery_enabled,
            ):
                final_status = TRACE_STATUS_OK
                await record_span(trace, "route", TRACE_STATUS_OK, component="transfer_notice")
                return
            event_bus_handled, event_bus_ok = (
                await _try_handle_event_bus_subscriptions(db, incoming, cfg)
                if event_bus_delivery_enabled
                else (False, True)
            )
            if event_bus_handled:
                final_status = TRACE_STATUS_OK if event_bus_ok else TRACE_STATUS_FAILED
                await record_span(
                    trace,
                    "route",
                    final_status,
                    component="event_bus",
                    reason_code=None if event_bus_ok else "plugin_runtime_error",
                )
                return
            if not event_bus_delivery_enabled:
                await record_span(
                    trace,
                    "subscription_match",
                    TRACE_STATUS_SKIPPED,
                    component="event_bus",
                    reason_code="event_bus_delivery_disabled",
                    message="Event Bus 新投递路径已通过运行设置关闭，回退旧规则链路。",
                )
            if await _try_handle_interaction_rule_command_or_keyword(db, incoming):
                final_status = TRACE_STATUS_OK
                await record_span(trace, "route", TRACE_STATUS_OK, component="interaction_rule")
                return
            if await _try_handle_interaction_module_message(db, incoming):
                final_status = TRACE_STATUS_OK
                await record_span(trace, "route", TRACE_STATUS_OK, component="interaction_session")
                return
            if await _try_handle_math_answer(incoming):
                final_status = TRACE_STATUS_OK
                await record_span(trace, "route", TRACE_STATUS_OK, component="math_answer")
                return
            await record_span(trace, "route", TRACE_STATUS_SKIPPED, component="interaction_bot", reason_code="subscription_not_matched")
    except Exception as exc:  # noqa: BLE001
        final_status = TRACE_STATUS_FAILED
        await record_span(
            trace,
            "finish",
            TRACE_STATUS_FAILED,
            component="interaction_bot",
            reason_code="handler_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        await finish_trace(trace, final_status)


async def _handle_transfer_test_update(aid: int, token: str, update: dict[str, Any]) -> None:
    incoming = _extract_incoming(aid, token, update)
    if incoming is None:
        return
    async with AsyncSessionLocal() as db:
        if await _incoming_is_userbot_command_text(db, incoming):
            log.info(
                "transfer command skipped: userbot command text aid=%s chat_id=%s sender_id=%s",
                incoming.account_id,
                incoming.chat_id,
                incoming.user_id,
            )
            return
        await _try_handle_transfer_command(db, incoming)


def _parse_transfer_notice(text: str) -> dict[str, Any] | None:
    """解析官方或测试阶段自定义转账通知文案。"""

    if not text.strip():
        return None
    labeled = {
        "payer_name": r"付款人\s*[:：]\s*(.+)",
        "receiver_name": r"收款人\s*[:：]\s*(.+)",
        "amount": r"金额\s*[:：]\s*(\d+)",
    }
    out: dict[str, Any] = {}
    for key, pattern in labeled.items():
        match = re.search(pattern, text)
        if not match:
            break
        value = match.group(1).strip()
        out[key] = int(value) if key == "amount" else value
    if set(out) == {"payer_name", "receiver_name", "amount"}:
        payer_id_match = re.search(r"付款人\s*(?:用户)?(?:ID|id)\s*[:：]\s*(\d+)", text)
        if payer_id_match:
            out["payer_user_id"] = int(payer_id_match.group(1))
        receiver_id_match = re.search(r"收款人\s*(?:ID|id)\s*[:：]\s*(\d+)", text)
        if receiver_id_match:
            out["receiver_user_id"] = int(receiver_id_match.group(1))
        return out

    payer_match = re.search(r"^\s*(.+?)\s*(?:转出|射出|转账)\s*(\d+)\b", text, re.M)
    receiver_match = re.search(r"^\s*(.+?)\s*(?:收到|接收|收款)\s*(\d+)\b", text, re.M)
    if payer_match and receiver_match:
        payer_amount = int(payer_match.group(2))
        receiver_amount = int(receiver_match.group(2))
        if payer_amount != receiver_amount:
            return None
        parsed = {
            "payer_name": payer_match.group(1).strip(),
            "receiver_name": receiver_match.group(1).strip(),
            "amount": payer_amount,
        }
        payer_id_match = re.search(r"付款人\s*(?:用户)?(?:ID|id)\s*[:：]\s*(\d+)", text)
        if payer_id_match:
            parsed["payer_user_id"] = int(payer_id_match.group(1))
        receiver_id_match = re.search(r"收款人\s*(?:ID|id)\s*[:：]\s*(\d+)", text)
        if receiver_id_match:
            parsed["receiver_user_id"] = int(receiver_id_match.group(1))
        return parsed
    return None


def _render_transfer_notice_response(template: str, data: dict[str, Any]) -> str:
    values = {
        "payer_name": account_bot_service.html_text(data.get("payer_name", "")),
        "payer_user_id": account_bot_service.html_text(data.get("payer_user_id", "")),
        "receiver_name": account_bot_service.html_text(data.get("receiver_name", "")),
        "amount": account_bot_service.html_text(data.get("amount", "")),
        "receiver_user_id": account_bot_service.html_text(data.get("receiver_user_id", "")),
    }
    raw_template = str(template or "").strip() or DEFAULT_INTERACTION_RESPONSE_TEMPLATE
    try:
        return raw_template.format_map(_TemplateValues(values))
    except Exception:
        return DEFAULT_INTERACTION_RESPONSE_TEMPLATE.format_map(_TemplateValues(values))


def _parse_transfer_command(text: str) -> int | None:
    match = re.fullmatch(r"\+(\d{1,9})", text.strip())
    if not match:
        return None
    amount = int(match.group(1))
    return amount if amount > 0 else None


class _TemplateValues(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


def _compact_rendered_template(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _plain_callback_text(text: str, *, limit: int = 180) -> str:
    plain = re.sub(r"<[^>]+>", "", str(text or ""))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]


def _interaction_action_save_message_id_key(raw: Any) -> str | None:
    return action_save_message_id_key(raw)


def _render_transfer_bot_notice_with_error(
    template: str,
    payer_name: str,
    receiver_name: str,
    amount: int,
    *,
    payer_user_id: int | None = None,
    receiver_user_id: int | None = None,
    escape_html: bool = True,
) -> tuple[str, Exception | None]:
    render_error: Exception | None = None

    def _value(value: Any) -> str:
        text = str(value)
        return account_bot_service.html_text(text) if escape_html else text

    payer_id_text = "" if payer_user_id is None else str(payer_user_id)
    receiver_id_text = "" if receiver_user_id is None else str(receiver_user_id)
    values = _TemplateValues(
        {
            "payer_name": _value(payer_name),
            "payer_user_id": _value(payer_id_text),
            "payer_user_id_line": f"付款人ID：{_value(payer_id_text)}" if payer_id_text else "",
            "receiver_name": _value(receiver_name),
            "amount": _value(amount),
            "receiver_user_id": _value(receiver_id_text),
            "receiver_user_id_line": f"收款人ID：{_value(receiver_id_text)}" if receiver_id_text else "",
        }
    )
    raw_template = str(template or "").strip() or DEFAULT_TRANSFER_NOTICE_TEMPLATE
    try:
        rendered = raw_template.format_map(values)
    except Exception as exc:
        render_error = exc
        rendered = DEFAULT_TRANSFER_NOTICE_TEMPLATE.format_map(values)
    rendered = _compact_rendered_template(rendered)
    if rendered:
        return rendered[:4000], render_error
    return _compact_rendered_template(DEFAULT_TRANSFER_NOTICE_TEMPLATE.format_map(values))[:4000], render_error


def _render_transfer_bot_notice(
    template: str,
    payer_name: str,
    receiver_name: str,
    amount: int,
    *,
    payer_user_id: int | None = None,
    receiver_user_id: int | None = None,
    escape_html: bool = True,
) -> str:
    rendered, _render_error = _render_transfer_bot_notice_with_error(
        template,
        payer_name,
        receiver_name,
        amount,
        payer_user_id=payer_user_id,
        receiver_user_id=receiver_user_id,
        escape_html=escape_html,
    )
    return rendered


def _interaction_chat_matches(cfg: dict[str, Any], chat_id: int) -> bool:
    raw_chat_ids = cfg.get("chat_ids")
    if isinstance(raw_chat_ids, list) and raw_chat_ids:
        try:
            return int(chat_id) in {int(item) for item in raw_chat_ids}
        except (TypeError, ValueError):
            return False
    return cfg.get("chat_id") is None or int(cfg["chat_id"]) == int(chat_id)


def _interaction_triggers(cfg: dict[str, Any]) -> list[str]:
    raw_triggers = cfg.get("trigger_texts")
    if isinstance(raw_triggers, list):
        out = [str(item).strip() for item in raw_triggers if str(item or "").strip()]
        if out:
            return out
    return [str(cfg.get("trigger_text") or "转账成功")]


def _matches_interaction_trigger(cfg: dict[str, Any], text: str) -> bool:
    return _message_line_equals_any(text, _interaction_triggers(cfg))


def _legacy_interaction_rule(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "legacy",
        "name": "兼容单规则",
        "enabled": True,
        "chat_ids": cfg.get("chat_ids") or ([cfg["chat_id"]] if cfg.get("chat_id") is not None else []),
        "trigger_mode": cfg.get("trigger_mode") or "payment",
        "trigger_texts": _interaction_triggers(cfg),
        "module_start_keywords": cfg.get("module_start_keywords") or [],
        "receiver_user_id": cfg.get("receiver_user_id"),
        "receiver_text": cfg.get("receiver_text"),
        "amount": cfg.get("amount"),
        "amount_match_mode": cfg.get("amount_match_mode") or "eq",
        "action": cfg.get("action") or "notice",
        "math_prize": cfg.get("math_prize") or 123,
        "module_key": cfg.get("module_key"),
        "module_action": cfg.get("module_action"),
        "module_prize": cfg.get("module_prize"),
        "module_start_text": cfg.get("module_start_text"),
        "open_commands": cfg.get("open_commands") or [],
        "close_commands": cfg.get("close_commands") or [],
        "status_commands": cfg.get("status_commands") or [],
        "disabled_message": cfg.get("disabled_message"),
        "valid_seconds": cfg.get("valid_seconds") or 600,
        "concurrency": cfg.get("concurrency") or "chat",
        "response_template": cfg.get("response_template") or account_bot_service.default_transfer_notice_config()["response_template"],
    }


def _interaction_rules(cfg: dict[str, Any], *, include_disabled: bool = False) -> list[dict[str, Any]]:
    raw_rules = cfg.get("rules")
    if isinstance(raw_rules, list) and raw_rules:
        rules = [rule for rule in raw_rules if isinstance(rule, dict)]
        if include_disabled:
            return rules
        return [rule for rule in rules if rule.get("enabled", True)]
    return [_legacy_interaction_rule(cfg)]


def _rule_chat_matches(rule: dict[str, Any], chat_id: int) -> bool:
    chat_ids = rule.get("chat_ids")
    if not isinstance(chat_ids, list) or not chat_ids:
        return True
    try:
        return int(chat_id) in {int(item) for item in chat_ids}
    except (TypeError, ValueError):
        return False


def _rule_triggers(rule: dict[str, Any]) -> list[str]:
    raw_triggers = rule.get("trigger_texts")
    if isinstance(raw_triggers, list):
        out = [str(item).strip() for item in raw_triggers if str(item or "").strip()]
        if out:
            return out
    return ["转账成功"]


def _rule_matches_trigger(rule: dict[str, Any], text: str) -> bool:
    return _message_line_equals_any(text, _rule_triggers(rule))


def _incoming_trigger_texts(incoming: Incoming) -> list[str]:
    texts: list[str] = []
    for value in (incoming.text, incoming.reply_to_text, *incoming.entity_languages):
        text = str(value or "").strip()
        if text and text not in texts:
            texts.append(text)
    return texts


def _incoming_notice_texts(incoming: Incoming) -> list[str]:
    texts: list[str] = []
    for value in (incoming.text, incoming.reply_to_text):
        text = str(value or "").strip()
        if text and text not in texts:
            texts.append(text)
    return texts


def _parse_incoming_transfer_notice(incoming: Incoming) -> dict[str, Any] | None:
    for text in _incoming_notice_texts(incoming):
        parsed = _parse_transfer_notice(text)
        if parsed is not None:
            return parsed
    return None


def _entity_languages(*entity_lists: Any) -> tuple[str, ...]:
    languages: list[str] = []
    for entity_list in entity_lists:
        if not isinstance(entity_list, list):
            continue
        for entity in entity_list:
            if not isinstance(entity, dict):
                continue
            language = str(entity.get("language") or "").strip()
            if not language:
                continue
            candidates = [language]
            if language.startswith("language-"):
                candidates.append(language.removeprefix("language-").strip())
            for candidate in candidates:
                if candidate and candidate not in languages:
                    languages.append(candidate)
    return tuple(languages)


def _incoming_matches_interaction_trigger(cfg: dict[str, Any], incoming: Incoming) -> bool:
    return any(_matches_interaction_trigger(cfg, text) for text in _incoming_trigger_texts(incoming))


def _rule_matches_incoming_trigger(rule: dict[str, Any], incoming: Incoming) -> bool:
    return any(_rule_matches_trigger(rule, text) for text in _incoming_trigger_texts(incoming))


def _rule_matches_payment_notice_trigger(
    rule: dict[str, Any],
    incoming: Incoming,
    parsed: dict[str, Any] | None,
) -> bool:
    if _rule_matches_incoming_trigger(rule, incoming):
        return True
    # A trusted transfer bot notice can be parsed even when Telegram strips the
    # human trigger marker from the rendered message body.
    return parsed is not None


def _rule_amount_matches(rule: dict[str, Any], amount: int) -> bool:
    expected = _rule_expected_payment_amount(rule)
    if expected is None:
        return True
    if str(rule.get("amount_match_mode") or "eq") == "gte":
        return int(amount) >= int(expected)
    return int(expected) == int(amount)


def _rule_expected_payment_amount(rule: dict[str, Any]) -> int | None:
    expected = _int_or_none(rule.get("amount"))
    if expected is not None:
        return expected if expected > 0 else None
    if str(rule.get("action") or "") != "module":
        return None
    module_config = rule.get("module_config")
    if not isinstance(module_config, dict):
        return None
    for key in _MODULE_PAYMENT_AMOUNT_KEYS:
        parsed = _int_or_none(module_config.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _rule_has_paid_threshold(rule: dict[str, Any]) -> bool:
    return rule.get("amount") is not None


def _rule_trigger_mode_allows(rule: dict[str, Any], trigger_type: str) -> bool:
    mode = str(rule.get("trigger_mode") or "payment")
    if mode == "both":
        return True
    if trigger_type == "payment":
        return mode == "payment"
    if trigger_type == "keyword":
        return mode == "keyword"
    return False


def _rule_entry_events(rule: dict[str, Any]) -> list[str]:
    module_key = str(rule.get("module_key") or "").strip() or None
    entry_key = str(rule.get("module_action") or "").strip() or None
    if not module_key or not entry_key:
        return []
    return account_bot_service.declared_module_entry_events(module_key, entry_key)


def _rule_entry_allows_event(rule: dict[str, Any], event_type: str) -> bool:
    declared = _rule_entry_events(rule)
    if not declared:
        return True
    return event_type in declared or "all_messages" in declared


def _rule_keyword_list(rule: dict[str, Any], key: str) -> list[str]:
    raw = rule.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _message_equals_any(text: str, candidates: list[str]) -> bool:
    clean = text.strip()
    return bool(clean and any(clean == candidate for candidate in candidates))


def _message_match_keyword_pattern(text: str, candidates: list[str]) -> dict[str, str] | None:
    clean = text.strip()
    if not clean:
        return None
    for candidate in candidates:
        pattern = candidate.strip()
        if not pattern:
            continue
        if clean == pattern:
            return {}
        regex = re.escape(pattern)
        regex = re.sub(
            r"id(?:\\ )*=(?:\\ )*数字",
            lambda _match: r"id\s*=\s*(?P<id>\d+)",
            regex,
        )
        regex = re.sub(
            r"num(?:\\ )*=(?:\\ )*数字",
            lambda _match: r"num\s*=\s*(?P<num>\d+)",
            regex,
        )
        if regex == re.escape(pattern):
            continue
        try:
            match = re.fullmatch(regex, clean, flags=re.IGNORECASE)
        except re.error:
            continue
        if match:
            return {key: value for key, value in match.groupdict().items() if value}
    return None


def _message_line_equals_any(text: str, candidates: list[str]) -> bool:
    units = [text.strip(), *(line.strip() for line in text.splitlines())]
    return any(_message_equals_any(unit, candidates) for unit in units if unit)


def _interaction_query_commands(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("query_commands")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        command = str(item or "").strip()
        if command and command not in out:
            out.append(command)
    return out


def _interaction_rule_kind_label(rule: dict[str, Any]) -> str:
    action = str(rule.get("action") or "")
    if action == "math10":
        return "算数题"
    if action == "module":
        module_key = str(rule.get("module_key") or "").strip()
        return f"玩法 <code>{account_bot_service.html_text(module_key)}</code>" if module_key else "玩法"
    return "通知"


def _interaction_trigger_mode_label(rule: dict[str, Any]) -> str:
    mode = str(rule.get("trigger_mode") or "payment")
    if mode == "both":
        return "转账或关键词"
    if mode == "keyword":
        return "关键词"
    return "转账"


def _interaction_amount_condition_label(rule: dict[str, Any]) -> str | None:
    amount = rule.get("amount")
    if amount is None:
        return None
    prefix = "≥" if str(rule.get("amount_match_mode") or "eq") == "gte" else "="
    return f"金额 {prefix} <code>{account_bot_service.html_text(amount)}</code>"


def _interaction_receiver_condition_label(rule: dict[str, Any]) -> str:
    receiver_text = str(rule.get("receiver_text") or "").strip()
    receiver_user_id = _int_or_none(rule.get("receiver_user_id"))
    if receiver_text:
        return f"收款人 {account_bot_service.html_text(receiver_text)}"
    if receiver_user_id is not None:
        return f"收款人 ID <code>{receiver_user_id}</code>"
    return "收款人 当前账号"


def _interaction_rule_limit_label(rule: dict[str, Any]) -> str:
    parts: list[str] = []
    action = str(rule.get("action") or "")
    prize = rule.get("module_prize") if action == "module" else rule.get("math_prize")
    if (
        action == "module"
        and prize is not None
        and account_bot_service.declared_module_entry_has_field(
            str(rule.get("module_key") or "").strip() or None,
            str(rule.get("module_action") or "").strip() or None,
            "prize",
        ) is False
    ):
        prize = None
    if prize is not None:
        parts.append(f"奖金 <code>{account_bot_service.html_text(prize)}</code>")
    if rule.get("valid_seconds") is not None:
        parts.append(f"限时 <code>{account_bot_service.html_text(rule.get('valid_seconds'))}</code> 秒")
    cooldown = str(rule.get("user_cooldown_seconds") or "").strip()
    if cooldown:
        parts.append(f"每用户 CD <code>{account_bot_service.html_text(cooldown)}</code>")
    if rule.get("daily_limit_per_user") is not None:
        parts.append(f"每用户日上限 <code>{account_bot_service.html_text(rule.get('daily_limit_per_user'))}</code>")
    return "；".join(parts) if parts else "无限制"


def _interaction_rule_trigger_labels(rule: dict[str, Any]) -> list[str]:
    labels = [f"方式：{_interaction_trigger_mode_label(rule)}"]
    if _rule_trigger_mode_allows(rule, "keyword"):
        keywords = _rule_keyword_list(rule, "module_start_keywords")
        if keywords:
            labels.append("关键词：" + " / ".join(f"<code>{account_bot_service.html_text(item)}</code>" for item in keywords[:5]))
        else:
            labels.append("关键词：未配置")
    if _rule_trigger_mode_allows(rule, "payment"):
        triggers = _rule_triggers(rule)
        labels.append("转账通知：" + " / ".join(f"<code>{account_bot_service.html_text(item)}</code>" for item in triggers[:5]))
        amount_label = _interaction_amount_condition_label(rule)
        if amount_label:
            labels.append(amount_label)
        labels.append(_interaction_receiver_condition_label(rule))
    return labels


def _interaction_rule_query_trigger_label(rule: dict[str, Any]) -> str:
    labels = [_interaction_trigger_mode_label(rule)]
    if _rule_trigger_mode_allows(rule, "keyword"):
        keywords = _rule_keyword_list(rule, "module_start_keywords")
        if keywords:
            labels.append("关键词：" + " / ".join(f"<code>{account_bot_service.html_text(item)}</code>" for item in keywords[:5]))
        else:
            labels.append("关键词未配置")
    if _rule_trigger_mode_allows(rule, "payment"):
        labels.append("转账通知")
    return "；".join(labels)


def _interaction_query_template_value(cfg: dict[str, Any], key: str, fallback: str) -> str:
    value = str(cfg.get(key) or "").strip()
    return value or fallback


def _render_interaction_query_template(template: str, values: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*|[\u4e00-\u9fa5]+)\}", repl, template).strip()


def _render_rule_text_template(template: str, rule: dict[str, Any]) -> str:
    rule_name = str(rule.get("name") or rule.get("id") or "互动玩法").strip()
    return _render_interaction_query_template(
        template,
        {
            "rule_name": account_bot_service.html_text(rule_name),
            "规则名称": account_bot_service.html_text(rule_name),
        },
    )


async def _render_interaction_rules_query(_db: Any, incoming: Incoming, cfg: dict[str, Any]) -> str | None:
    if incoming.chat_id is None:
        return None
    matched: list[tuple[dict[str, Any], bool]] = []
    for rule in _interaction_rules(cfg):
        if not _rule_chat_matches(rule, incoming.chat_id):
            continue
        if str(rule.get("action") or "") not in {"math10", "module"}:
            continue
        matched.append((rule, await _is_interaction_rule_open(incoming.account_id, rule, incoming.chat_id)))
    if not matched:
        return None

    open_rules = [rule for rule, open_ in matched if open_]
    if not open_rules:
        return _interaction_query_template_value(cfg, "query_empty_message", DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE)

    lines: list[str] = []
    item_template = _interaction_query_template_value(
        cfg,
        "query_item_template",
        DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
    )
    for index, rule in enumerate(open_rules, start=1):
        raw_name = str(rule.get("name") or rule.get("id") or f"玩法 {index}")
        lines.append(
            _render_interaction_query_template(
                item_template,
                {
                    "index": account_bot_service.html_text(index),
                    "name": account_bot_service.html_text(raw_name),
                    "trigger": _interaction_rule_query_trigger_label(rule),
                    "kind": _interaction_rule_kind_label(rule),
                    "limit": _interaction_rule_limit_label(rule),
                    "module_key": account_bot_service.html_text(str(rule.get("module_key") or "")),
                    "module_action": account_bot_service.html_text(str(rule.get("module_action") or "")),
                    "chat_id": account_bot_service.html_text(incoming.chat_id),
                },
            )
        )
    closed_count = len(matched) - len(open_rules)
    if closed_count > 0:
        lines.append(f"另有 {closed_count} 个玩法已临时关闭。")
    template = _interaction_query_template_value(
        cfg,
        "query_response_template",
        DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
    )
    return _render_interaction_query_template(
        template,
        {
            "items": "\n".join(lines),
            "count": account_bot_service.html_text(len(open_rules)),
            "closed_count": account_bot_service.html_text(closed_count),
            "chat_id": account_bot_service.html_text(incoming.chat_id),
        },
    )


def _rule_state_key(account_id: int, rule: dict[str, Any], chat_id: int | None) -> str:
    scope = str(rule.get("concurrency") or "chat")
    if scope == "none":
        scoped = "global"
    elif scope == "user":
        scoped = f"chat:{chat_id or 0}"
    else:
        scoped = str(chat_id or 0)
    return f"{_INTERACTION_RULE_STATE_PREFIX}{int(account_id)}:{rule.get('id') or 'legacy'}:{scoped}"


def _interaction_session_scope(rule: dict[str, Any], chat_id: int | None, user_id: int | None) -> str:
    scope = str(rule.get("module_session_scope") or rule.get("concurrency") or "chat")
    if scope == "none":
        return "global"
    if scope == "user":
        return f"{chat_id or 0}:user:{user_id or 0}"
    return str(chat_id or 0)


def _interaction_session_key(account_id: int, rule: dict[str, Any], chat_id: int | None, user_id: int | None = None) -> str:
    scoped = _interaction_session_scope(rule, chat_id, user_id)
    return f"{_INTERACTION_SESSION_PREFIX}{int(account_id)}:{rule.get('id') or 'legacy'}:{scoped}"


def _interaction_session_ttl(rule: dict[str, Any]) -> int:
    try:
        ttl = int(rule.get("valid_seconds") or 600)
    except (TypeError, ValueError):
        ttl = 600
    return min(max(ttl, 30), 86400)


def _duration_seconds(value: Any, default: int = 0) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return default
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text)
    if not match:
        return default
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return max(0, min(amount * multiplier, 30 * 86400))


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds >= 86400:
        days, rest = divmod(seconds, 86400)
        hours = rest // 3600
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours}小时{minutes}分钟" if minutes else f"{hours}小时"
    if seconds >= 60:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}分钟{rest}秒" if rest else f"{minutes}分钟"
    return f"{seconds}秒"


def _seconds_until_local_midnight(now: float | None = None) -> int:
    ts = time.time() if now is None else now
    current = time.localtime(ts)
    next_midnight = time.mktime(
        (
            current.tm_year,
            current.tm_mon,
            current.tm_mday + 1,
            0,
            0,
            0,
            current.tm_wday,
            current.tm_yday,
            current.tm_isdst,
        )
    )
    return max(60, int(next_midnight - ts))


def _interaction_payment_payer_user_id(incoming: Incoming, data: dict[str, Any] | None = None) -> int | None:
    payload = data if isinstance(data, dict) else {}
    payer_id = _int_or_none(payload.get("payer_user_id"))
    if payer_id is not None:
        return payer_id
    event_type = str(
        payload.get("event_type")
        or (payload.get("event") if isinstance(payload.get("event"), dict) else {}).get("type")
        or (payload.get("source") if isinstance(payload.get("source"), dict) else {}).get("type")
        or ""
    ).strip()
    if event_type == "payment_confirmed":
        return incoming.reply_to_user_id
    return None


def _interaction_payment_payer_name(incoming: Incoming, data: dict[str, Any] | None = None) -> str:
    payload = data if isinstance(data, dict) else {}
    return str(payload.get("payer_name") or incoming.reply_to_display_name or "").strip()


def _interaction_participant_policy(rule: dict[str, Any]) -> str:
    declared = _declared_interaction_participant_policy(rule)
    if declared:
        return declared
    raw = str(rule.get("participant_policy") or "").strip()
    if raw in account_bot_service.VALID_INTERACTION_PARTICIPANT_POLICIES:
        return raw
    scope = str(rule.get("module_session_scope") or rule.get("concurrency") or "chat")
    if scope == "user":
        return "solo_owner"
    return "open_race"


def _declared_interaction_participant_policy(rule: dict[str, Any]) -> str | None:
    if str(rule.get("action") or "") != "module":
        return None
    module_key = str(rule.get("module_key") or "").strip() or None
    entry_key = str(rule.get("module_action") or "").strip() or None
    if not module_key or not entry_key:
        return None
    entry = account_bot_service.declared_module_entry_manifest(module_key, entry_key)
    if not isinstance(entry, dict):
        return None
    policy = str(entry.get("participant_policy") or "").strip()
    if policy in account_bot_service.VALID_INTERACTION_PARTICIPANT_POLICIES:
        return policy
    return None


def _interaction_requires_verified_player(rule: dict[str, Any]) -> bool:
    return _interaction_participant_policy(rule) in {"solo_owner", "paid_pool"}


def _interaction_payment_identity_confidence(incoming: Incoming, data: dict[str, Any] | None = None) -> str:
    payload = data if isinstance(data, dict) else {}
    if _int_or_none(payload.get("payer_user_id")) is not None:
        return str(payload.get("payer_identity_confidence") or _PLAYER_IDENTITY_CONFIDENCE_VERIFIED)
    event_type = str(payload.get("event_type") or "").strip()
    if event_type == "payment_confirmed" and incoming.reply_to_user_id is not None:
        return _PLAYER_IDENTITY_CONFIDENCE_REPLY
    if _interaction_payment_payer_name(incoming, payload):
        return _PLAYER_IDENTITY_CONFIDENCE_NAME_ONLY
    return _PLAYER_IDENTITY_CONFIDENCE_UNKNOWN


def _interaction_player_identity_key(
    user_id: int | None,
    display_name: str | None,
    confidence: str,
) -> str | None:
    if user_id is not None:
        return f"tg:{int(user_id)}"
    name = str(display_name or "").strip()
    if name:
        return f"name:{name.casefold()}"
    return f"unknown:{confidence}" if confidence else None


def _interaction_payment_envelope(
    incoming: Incoming,
    data: dict[str, Any] | None = None,
    *,
    payer_user_id: int | None = None,
    payer_name: str | None = None,
) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    receiver_user_id = _int_or_none(payload.get("receiver_user_id"))
    amount = _int_or_none(payload.get("amount"))
    return {
        "status": "confirmed",
        "amount": amount,
        "payer_user_id": payer_user_id,
        "payer_name": payer_name or None,
        "payer_display_name": payer_name or None,
        "receiver_user_id": receiver_user_id,
        "receiver_name": str(payload.get("receiver_name") or "").strip() or None,
        "receiver_display_name": str(payload.get("receiver_name") or "").strip() or None,
        "notice_message_id": incoming.message_id,
        "source_message_id": incoming.message_id,
        "reply_to_message_id": incoming.reply_to_message_id,
        "notice_sender_user_id": incoming.user_id,
    }


def _interaction_player_envelope(
    incoming: Incoming,
    data: dict[str, Any] | None = None,
    *,
    event_type: str | None = None,
) -> dict[str, Any]:
    payload = dict(data or {}) if isinstance(data, dict) else {}
    if event_type:
        payload.setdefault("event_type", event_type)
    user_id = _interaction_payment_payer_user_id(incoming, payload)
    display_name = _interaction_payment_payer_name(incoming, payload)
    username = incoming.reply_to_username if user_id is not None and incoming.reply_to_user_id == user_id else None
    confidence = _interaction_payment_identity_confidence(incoming, payload)
    if user_id is None and str(payload.get("event_type") or "").strip() != "payment_confirmed":
        user_id = _int_or_none(payload.get("sender_user_id")) or incoming.user_id
        display_name = str(payload.get("sender_name") or incoming.display_name or "").strip()
        username = str(payload.get("sender_username") or incoming.username or "").strip() or None
        confidence = _PLAYER_IDENTITY_CONFIDENCE_VERIFIED if user_id is not None else _PLAYER_IDENTITY_CONFIDENCE_UNKNOWN
    player = {
        "user_id": user_id,
        "display_name": display_name or None,
        "username": username,
        "identity_key": _interaction_player_identity_key(user_id, display_name, confidence),
        "identity_confidence": confidence,
    }
    sender_chat = _incoming_sender_chat_envelope(incoming)
    if sender_chat and user_id is None:
        player["sender_type"] = "chat"
        player["sender_chat"] = sender_chat
        player["identity_key"] = f"sender_chat:{incoming.sender_chat_id}"
    return player


def _interaction_payment_needs_player_confirm(
    incoming: Incoming,
    rule: dict[str, Any],
    parsed: dict[str, Any] | None,
) -> bool:
    if not _interaction_requires_verified_player(rule):
        return False
    data = dict(parsed or {})
    data["event_type"] = "payment_confirmed"
    return _interaction_payment_payer_user_id(incoming, data) is None


async def _request_interaction_payment_player_confirm(
    incoming: Incoming,
    rule: dict[str, Any],
    parsed: dict[str, Any],
) -> None:
    nonce = secrets.token_urlsafe(8)
    payload = {
        "account_id": incoming.account_id,
        "rule": rule,
        "parsed": parsed,
        "incoming": {
            "update_id": incoming.update_id,
            "user_id": incoming.user_id,
            "chat_id": incoming.chat_id,
            "chat_type": incoming.chat_type,
            "message_id": incoming.message_id,
            "text": incoming.text,
            "display_name": incoming.display_name,
            "username": incoming.username,
            "reply_to_message_id": incoming.reply_to_message_id,
            "reply_to_text": incoming.reply_to_text,
            "entity_languages": list(incoming.entity_languages),
        },
    }
    redis = get_redis()
    await redis.set(
        _interaction_payment_confirm_key(nonce),
        json.dumps(payload, ensure_ascii=False),
        ex=_INTERACTION_PAYMENT_CONFIRM_TTL_SECONDS,
    )
    payer_name = account_bot_service.html_text(str(parsed.get("payer_name") or "付款人"))
    amount = account_bot_service.html_text(str(parsed.get("amount") or ""))
    rule_name = account_bot_service.html_text(str(rule.get("name") or rule.get("id") or "该玩法"))
    await _send(
        incoming,
        (
            f"已确认 {payer_name} 到账 {amount}，但还需要绑定真实 Telegram 用户后才能启动「{rule_name}」。\n"
            "付款人请点击下方按钮确认开始。"
        ),
        reply_to_message_id=incoming.message_id,
        reply_markup=_keyboard(
            [[{"text": "我是付款人，开始玩法", "callback_data": _interaction_payment_confirm_callback_data(nonce)}]]
        ),
    )


def _incoming_from_payment_confirm_payload(
    token: str,
    payload: dict[str, Any],
    confirmer: Incoming,
) -> Incoming | None:
    raw_incoming = payload.get("incoming") if isinstance(payload.get("incoming"), dict) else {}
    chat_id = _int_or_none(raw_incoming.get("chat_id"))
    if chat_id is None:
        return None
    return Incoming(
        account_id=int(payload.get("account_id") or confirmer.account_id),
        token=token,
        update_id=_int_or_none(raw_incoming.get("update_id")) or confirmer.update_id,
        user_id=_int_or_none(raw_incoming.get("user_id")),
        chat_id=chat_id,
        chat_type=str(raw_incoming.get("chat_type") or "") or confirmer.chat_type,
        message_id=_int_or_none(raw_incoming.get("message_id")),
        text=str(raw_incoming.get("text") or ""),
        display_name=str(raw_incoming.get("display_name") or "") or None,
        username=str(raw_incoming.get("username") or "") or None,
        reply_to_user_id=confirmer.user_id,
        reply_to_message_id=confirmer.message_id,
        reply_to_display_name=confirmer.display_name,
        reply_to_username=confirmer.username,
        reply_to_text=str(raw_incoming.get("reply_to_text") or "") or None,
        entity_languages=tuple(
            str(item)
            for item in raw_incoming.get("entity_languages", [])
            if str(item or "").strip()
        ),
        trace_id=confirmer.trace_id,
    )


def _interaction_session_user_id(incoming: Incoming, data: dict[str, Any] | None = None) -> int | None:
    payload = data if isinstance(data, dict) else {}
    if str(payload.get("event_type") or "").strip() == "payment_confirmed":
        return _interaction_payment_payer_user_id(incoming, payload)
    return (
        _interaction_payment_payer_user_id(incoming, payload)
        or _int_or_none(payload.get("sender_user_id"))
        or incoming.user_id
    )


async def _save_interaction_session(
    incoming: Incoming,
    rule: dict[str, Any],
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    module_key = str(rule.get("module_key") or "").strip()
    entry_key = str(rule.get("module_action") or "").strip()
    if not module_key or not entry_key:
        return
    session_user_id = _interaction_session_user_id(
        incoming,
        {**(data or {}), "event_type": event_type},
    )
    session_key = _interaction_session_key(incoming.account_id, rule, incoming.chat_id, session_user_id)
    existing: dict[str, Any] = {}
    redis = None
    try:
        redis = get_redis()
        raw = await redis.get(session_key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing = parsed
    except Exception:  # noqa: BLE001
        existing = {}
    policy = _interaction_participant_policy(rule)
    started_by_user_id = _int_or_none(existing.get("started_by_user_id"))
    if started_by_user_id is None:
        started_by_user_id = session_user_id
    payload = {
        "account_id": incoming.account_id,
        "chat_id": incoming.chat_id,
        "rule_id": str(rule.get("id") or "legacy"),
        "rule_name": str(rule.get("name") or ""),
        "module_key": module_key,
        "entry_key": entry_key,
        "started_by_user_id": started_by_user_id,
        "source_user_id": incoming.user_id,
        "started_by_message_id": incoming.message_id,
        "event_type": event_type,
        "created_at": existing.get("created_at") or time.time(),
        "updated_at": time.time(),
    }
    if policy == "paid_pool":
        paid_ids = _interaction_session_list_participant_ids(existing)
        if event_type == "payment_confirmed" and session_user_id is not None:
            paid_ids.add(int(session_user_id))
            payload["payer_user_id"] = int(session_user_id)
        payload["paid_user_ids"] = sorted(paid_ids)
        payload["participant_user_ids"] = sorted(paid_ids)
    try:
        if redis is None:
            redis = get_redis()
        await redis.set(
            session_key,
            json.dumps(payload, ensure_ascii=False),
            ex=_interaction_session_ttl(rule),
        )
    except Exception:  # noqa: BLE001
        log.debug("save interaction session failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)


def _interaction_action_participant_ids(action: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for key in ("participant_user_ids", "paid_user_ids", "player_user_ids"):
        raw = action.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            user_id = _int_or_none(item)
            if user_id is not None:
                ids.add(user_id)
    return ids


async def _apply_interaction_start_session_action(
    incoming: Incoming,
    rule: dict[str, Any],
    action: dict[str, Any],
) -> None:
    module_key = str(rule.get("module_key") or "").strip()
    entry_key = str(action.get("entry_key") or rule.get("module_action") or "").strip()
    target_chat_id = _int_or_none(action.get("chat_id")) or incoming.chat_id
    if not module_key or not entry_key or target_chat_id is None:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="entry_key_missing" if not entry_key else "scope_not_matched",
            error="interaction start_session action missing module, entry_key or chat_id",
        )
        return

    redis = get_redis()
    started_by_user_id = _int_or_none(action.get("started_by_user_id"))
    session_key = _interaction_session_key(incoming.account_id, rule, target_chat_id, started_by_user_id)
    existing: dict[str, Any] = {}
    try:
        raw = await redis.get(session_key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing = parsed
    except Exception:  # noqa: BLE001
        existing = {}

    if started_by_user_id is None:
        started_by_user_id = _int_or_none(existing.get("started_by_user_id")) or incoming.user_id
    participant_ids = _interaction_session_list_participant_ids(existing)
    participant_ids.update(_interaction_action_participant_ids(action))
    if started_by_user_id is not None and action.get("include_started_by", False):
        participant_ids.add(int(started_by_user_id))

    payload = {
        "account_id": incoming.account_id,
        "chat_id": target_chat_id,
        "rule_id": str(rule.get("id") or "legacy"),
        "rule_name": str(rule.get("name") or ""),
        "module_key": module_key,
        "entry_key": entry_key,
        "started_by_user_id": started_by_user_id,
        "started_by_message_id": _int_or_none(action.get("started_by_message_id")) or existing.get("started_by_message_id") or incoming.message_id,
        "source_user_id": incoming.user_id,
        "event_type": str(action.get("event_type") or existing.get("event_type") or "message"),
        "created_at": existing.get("created_at") or time.time(),
        "updated_at": time.time(),
    }
    if _interaction_participant_policy(rule) == "paid_pool":
        payload["paid_user_ids"] = sorted(participant_ids)
        payload["participant_user_ids"] = sorted(participant_ids)
    ttl = _int_or_none(action.get("ttl_seconds")) or _interaction_session_ttl(rule)
    try:
        await redis.set(session_key, json.dumps(payload, ensure_ascii=False), ex=ttl)
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK,
            actual_send_via="interaction_session",
            result={"chat_id": target_chat_id, "participant_user_ids": sorted(participant_ids)},
        )
    except Exception as exc:  # noqa: BLE001
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="redis_error",
            error=str(exc),
        )
        log.debug("apply interaction start_session failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)


async def _apply_interaction_start_session_actions(
    incoming: Incoming,
    rule: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    for action in actions:
        if isinstance(action, dict) and str(action.get("type") or "").strip() == "start_session":
            await _apply_interaction_start_session_action(incoming, rule, action)


async def _load_interaction_session(incoming: Incoming, rule: dict[str, Any]) -> dict[str, Any] | None:
    keys = [_interaction_session_key(incoming.account_id, rule, incoming.chat_id, incoming.user_id)]
    if str(rule.get("module_session_scope") or rule.get("concurrency") or "chat") == "user":
        keys.append(_interaction_session_key(incoming.account_id, rule, incoming.chat_id, None))
    try:
        redis = get_redis()
        for key in keys:
            raw = await redis.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        log.debug("load interaction session failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)
    return None


def _interaction_session_list_participant_ids(session: dict[str, Any] | None) -> set[int]:
    if not isinstance(session, dict):
        return set()
    ids: set[int] = set()
    for key in ("participant_user_ids", "paid_user_ids", "player_user_ids"):
        raw = session.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            user_id = _int_or_none(item)
            if user_id is not None:
                ids.add(user_id)
    return ids


def _interaction_session_participant_ids(session: dict[str, Any] | None, *, policy: str | None = None) -> set[int]:
    if not isinstance(session, dict):
        return set()
    if policy == "paid_pool":
        ids = _interaction_session_list_participant_ids(session)
        has_explicit_list = any(isinstance(session.get(key), list) for key in ("participant_user_ids", "paid_user_ids", "player_user_ids"))
        if ids or has_explicit_list:
            return ids
    ids = set()
    for key in ("started_by_user_id", "player_user_id", "payer_user_id"):
        user_id = _int_or_none(session.get(key))
        if user_id is not None:
            ids.add(user_id)
    ids.update(_interaction_session_list_participant_ids(session))
    return ids


def _interaction_participant_block_message(
    incoming: Incoming,
    rule: dict[str, Any],
    session: dict[str, Any] | None,
) -> str | None:
    policy = _interaction_participant_policy(rule)
    if policy not in {"solo_owner", "paid_pool"}:
        return None
    if incoming.user_id is None:
        return "请用真实 Telegram 用户身份操作该玩法。"
    participant_ids = _interaction_session_participant_ids(session, policy=policy)
    if not participant_ids:
        return None
    if int(incoming.user_id) in participant_ids:
        return None
    if policy == "paid_pool":
        started_by_user_id = _int_or_none(session.get("started_by_user_id")) if isinstance(session, dict) else None
        if started_by_user_id is not None and int(incoming.user_id) == started_by_user_id:
            return None
    if policy == "paid_pool":
        return "点点点！啥你都点！"
    return "这不是你的玩法，请由付款或开局本人操作。"


async def _interaction_session_keys_for_rule(account_id: int, rule: dict[str, Any], chat_id: int | None) -> list[str]:
    if str(rule.get("module_session_scope") or rule.get("concurrency") or "chat") != "user":
        return [_interaction_session_key(account_id, rule, chat_id)]

    prefix = _interaction_session_key(account_id, rule, chat_id, None).rsplit(":", 1)[0] + ":"
    redis = get_redis()
    keys: list[str] = []
    scan_iter = getattr(redis, "scan_iter", None)
    if callable(scan_iter):
        async for key in scan_iter(match=f"{prefix}*"):
            keys.append(key.decode("utf-8", errors="ignore") if isinstance(key, bytes) else str(key))
        return keys
    keys_fn = getattr(redis, "keys", None)
    if callable(keys_fn):
        raw_keys = await keys_fn(f"{prefix}*")
        return [key.decode("utf-8", errors="ignore") if isinstance(key, bytes) else str(key) for key in raw_keys]
    return []


async def _list_interaction_sessions_for_rule(account_id: int, rule: dict[str, Any], chat_id: int | None) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    try:
        redis = get_redis()
        for key in await _interaction_session_keys_for_rule(account_id, rule, chat_id):
            raw = await redis.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"session_key": key}
            if isinstance(data, dict):
                sessions.append(data)
    except Exception:  # noqa: BLE001
        log.debug("list interaction sessions failed aid=%s rule=%s", account_id, rule.get("id"), exc_info=True)
    return sessions


async def _clear_interaction_session(account_id: int, rule: dict[str, Any], chat_id: int | None, user_id: int | None = None) -> bool:
    try:
        redis = get_redis()
        deleted = await redis.delete(_interaction_session_key(account_id, rule, chat_id, user_id))
        return bool(deleted)
    except Exception:  # noqa: BLE001
        log.debug("clear interaction session failed aid=%s rule=%s", account_id, rule.get("id"), exc_info=True)
        return False


async def _clear_interaction_sessions_for_rule(account_id: int, rule: dict[str, Any], chat_id: int | None) -> int:
    deleted = 0
    try:
        redis = get_redis()
        for key in await _interaction_session_keys_for_rule(account_id, rule, chat_id):
            deleted += int(await redis.delete(key))
        return deleted
    except Exception:  # noqa: BLE001
        log.debug("clear interaction user sessions failed aid=%s rule=%s", account_id, rule.get("id"), exc_info=True)
        return deleted


async def _clear_loaded_interaction_session(
    account_id: int,
    rule: dict[str, Any],
    chat_id: int | None,
    session: dict[str, Any] | None,
    *,
    incoming_user_id: int | None = None,
) -> int:
    if str(rule.get("module_session_scope") or rule.get("concurrency") or "chat") != "user":
        return 1 if await _clear_interaction_session(account_id, rule, chat_id) else 0

    user_ids: list[int | None] = []
    if isinstance(session, dict):
        user_ids.append(_int_or_none(session.get("started_by_user_id")))
    user_ids.append(incoming_user_id)

    deleted = 0
    seen: set[int | None] = set()
    for user_id in user_ids:
        if user_id in seen:
            continue
        seen.add(user_id)
        if await _clear_interaction_session(account_id, rule, chat_id, user_id):
            deleted += 1
    return deleted


async def _is_interaction_rule_open(account_id: int, rule: dict[str, Any], chat_id: int | None) -> bool:
    try:
        redis = get_redis()
        value = await redis.get(_rule_state_key(account_id, rule, chat_id))
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        return str(value or "open") != "closed"
    except Exception:  # noqa: BLE001
        log.debug("read interaction rule state failed aid=%s rule=%s", account_id, rule.get("id"), exc_info=True)
        return True


async def _set_interaction_rule_open(account_id: int, rule: dict[str, Any], chat_id: int | None, open_: bool) -> None:
    try:
        redis = get_redis()
        await redis.set(_rule_state_key(account_id, rule, chat_id), "open" if open_ else "closed")
    except Exception:  # noqa: BLE001
        log.debug("write interaction rule state failed aid=%s rule=%s", account_id, rule.get("id"), exc_info=True)


def _account_holder_label_from_row(account: Any, account_id: int) -> str:
    username = str(getattr(account, "tg_username", "") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    display_name = str(getattr(account, "display_name", "") or "").strip()
    if display_name:
        return account_bot_service.html_text(display_name)
    tg_user_id = _int_or_none(getattr(account, "tg_user_id", None))
    if tg_user_id is not None:
        return str(tg_user_id)
    return f"账号 #{int(account_id)}"


async def _load_account_holder_label(account_id: int) -> str:
    try:
        async with AsyncSessionLocal() as db:
            account = await db.get(Account, account_id)
        return _account_holder_label_from_row(account, account_id)
    except Exception:  # noqa: BLE001
        log.debug("load account holder label failed aid=%s", account_id, exc_info=True)
        return f"账号 #{int(account_id)}"


async def _load_account_owner_user_ids(account_id: int) -> list[int]:
    try:
        async with AsyncSessionLocal() as db:
            account = await db.get(Account, account_id)
        tg_user_id = _int_or_none(getattr(account, "tg_user_id", None))
        return [tg_user_id] if tg_user_id is not None else []
    except Exception:  # noqa: BLE001
        log.debug("load account owner user id failed aid=%s", account_id, exc_info=True)
        return []


async def _resolve_payout_mode(account_id: int, chat_id: int | None) -> str:
    """根据当前聊天是否纳入自动发奖监听范围，决定公告文案。"""

    if chat_id is None:
        return "manual"
    try:
        async with AsyncSessionLocal() as db:
            cfg = await account_bot_service.get_transfer_notice_config(db, account_id)
    except Exception:  # noqa: BLE001
        log.debug("resolve payout mode failed aid=%s chat_id=%s", account_id, chat_id, exc_info=True)
        return "manual"
    if not bool(cfg.get("enabled")):
        return "manual"
    for rule in _interaction_rules(cfg):
        action = str(rule.get("action") or "")
        module_key = str(rule.get("module_key") or "").strip()
        if action not in {"math10", "module"}:
            continue
        if action == "module" and module_key not in AUTO_PAYOUT_MODULE_KEYS:
            continue
        if _rule_chat_matches(rule, int(chat_id)):
            return "auto"
    return "manual"


async def _interaction_paid_threshold_message(db: Any, incoming: Incoming, rule: dict[str, Any]) -> str:
    rule_name = str(rule.get("name") or rule.get("id") or "该规则").strip()
    amount = rule.get("amount")
    amount_text = str(amount) if amount is not None else "门槛金额"
    receiver_filter = await _rule_receiver_filter(db, incoming.account_id, rule)
    if not receiver_filter.get("explicit"):
        receiver = await _load_account_holder_label(incoming.account_id)
    else:
        receiver_texts = receiver_filter.get("texts") if isinstance(receiver_filter.get("texts"), list) else []
        if receiver_texts:
            receiver = account_bot_service.html_text(str(receiver_texts[0]))
        elif receiver_filter.get("user_id") is not None:
            receiver = account_bot_service.html_text(str(receiver_filter["user_id"]))
        else:
            receiver = await _load_account_holder_label(incoming.account_id)
    return (
        f"该{account_bot_service.html_text(rule_name)}是付费娱乐模块，"
        f"请对收款人：{receiver}的任意消息回复+{account_bot_service.html_text(amount_text)}即可参与。"
    )


async def _close_active_interaction_games(incoming: Incoming, target_rule: dict[str, Any] | None = None) -> int:
    account_id = incoming.account_id
    chat_id = incoming.chat_id
    if chat_id is None:
        return 0
    closed = 0
    close_math = target_rule is None or str(target_rule.get("action") or "") == "math10"
    if close_math:
        math_state = await _load_math_game_state(account_id, chat_id)
        if math_state is not None and math_state.active:
            math_state.active = False
            await _save_math_game_state(math_state)
            closed += 1
    try:
        if target_rule is None:
            async with AsyncSessionLocal() as db:
                cfg = await account_bot_service.get_transfer_notice_config(db, account_id)
            rules = _interaction_rules(cfg)
        else:
            rules = [target_rule]
        for rule in rules:
            if not _rule_chat_matches(rule, chat_id or 0):
                continue
            if str(rule.get("action") or "") != "module":
                continue
            module_key = str(rule.get("module_key") or "").strip()
            entry_key = str(rule.get("module_action") or "").strip()
            if not _rule_entry_allows_event(rule, "session_close"):
                closed += await _clear_interaction_sessions_for_rule(account_id, rule, chat_id)
                continue
            sessions = await _list_interaction_sessions_for_rule(account_id, rule, chat_id)
            close_targets = sessions or [None]
            for session in close_targets:
                if not module_key or not entry_key:
                    continue
                payload = await _interaction_module_payload_async(
                    incoming,
                    rule,
                    {"session": session} if session is not None else None,
                    event_type="session_close",
                )
                session_trace = None
                session_status = TRACE_STATUS_SKIPPED
                parent_trace_id = incoming.trace_id
                try:
                    flags = await _event_framework_flags()
                    if flags.get("trace_enabled", True):
                        trace_payload = dict(payload)
                        trace_payload["trace_id"] = None
                        session_trace = await start_trace(trace_payload)
                        payload["trace_id"] = session_trace.trace_id
                        incoming.trace_id = session_trace.trace_id
                        _event, decision = _legacy_rule_event_bus_decision(incoming, rule, event_type="session_close")
                        await record_span(
                            session_trace,
                            "receive",
                            TRACE_STATUS_OK,
                            component="interaction_session",
                            plugin_key=module_key,
                            entry_key=entry_key,
                        )
                        await record_span(
                            session_trace,
                            "subscription_match",
                            TRACE_STATUS_OK if decision is not None and decision.matched else TRACE_STATUS_SKIPPED,
                            component="interaction_session",
                            plugin_key=module_key,
                            entry_key=entry_key,
                            reason_code=getattr(decision, "reason_code", "subscription_not_matched"),
                            message=getattr(decision, "reason_message", "session_close 未通过 Event Bus rule_bound decision。"),
                            dispatch_mode=getattr(decision, "dispatch_mode", "rule_bound"),
                            scope=getattr(decision, "scope", "rule_bound"),
                            filters=getattr(decision, "filters", {
                                "rule_id": rule.get("id"),
                                "event_type": "session_close",
                                "chat_id": chat_id,
                            }),
                        )
                        if decision is None or not decision.matched:
                            continue
                    ok, _error, actions = await _run_worker_interaction_entry(
                        incoming,
                        plugin_key=module_key,
                        entry_key=entry_key,
                        payload=payload,
                    )
                    session_status = TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED
                    if ok and actions:
                        actions = await _guard_interaction_actions(incoming, rule, actions)
                        await _apply_interaction_actions(
                            incoming,
                            actions,
                            context=_interaction_trace_context(payload),
                        )
                finally:
                    try:
                        await finish_trace(session_trace, session_status)
                    finally:
                        incoming.trace_id = parent_trace_id
            closed += await _clear_interaction_sessions_for_rule(account_id, rule, chat_id)
    except Exception:  # noqa: BLE001
        log.debug("close interaction module sessions failed aid=%s chat_id=%s", account_id, chat_id, exc_info=True)
    return closed


def _interaction_dedupe_key(incoming: Incoming, rule: dict[str, Any], kind: str, payload: Any) -> str:
    if incoming.message_id is not None:
        raw = f"{incoming.account_id}:{incoming.chat_id}:{rule.get('id')}:{kind}:msg:{incoming.message_id}"
    else:
        raw = f"{incoming.account_id}:{incoming.chat_id}:{rule.get('id')}:{kind}:{payload!r}:{incoming.update_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{_INTERACTION_TRIGGER_DEDUPE_PREFIX}{digest}"


async def _claim_interaction_trigger(incoming: Incoming, rule: dict[str, Any], kind: str, payload: Any) -> bool:
    try:
        ttl = int(rule.get("valid_seconds") or 600)
    except (TypeError, ValueError):
        ttl = 600
    ttl = min(max(ttl, 30), 86400)
    try:
        redis = get_redis()
        return bool(await redis.set(_interaction_dedupe_key(incoming, rule, kind, payload), "1", ex=ttl, nx=True))
    except Exception:  # noqa: BLE001
        log.debug("claim interaction trigger failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)
        return True


def _interaction_user_usage_identity(incoming: Incoming, data: dict[str, Any] | None = None) -> tuple[str, str] | None:
    payload = data if isinstance(data, dict) else {}
    payer_id = _int_or_none(payload.get("payer_user_id"))
    if payer_id is not None:
        label = str(payload.get("payer_name") or payer_id).strip()
        return f"id:{payer_id}", account_bot_service.html_text(label)
    payer_name = str(payload.get("payer_name") or "").strip()
    if payer_name:
        return f"name:{payer_name.casefold()}", account_bot_service.html_text(payer_name)
    if incoming.sender_chat_id is not None:
        label = incoming.sender_chat_title or incoming.display_name or str(incoming.sender_chat_id)
        return f"sender_chat:{incoming.sender_chat_id}", account_bot_service.html_text(label)
    if incoming.user_id is None:
        return None
    return f"id:{incoming.user_id}", _interaction_user_label(incoming)


def _interaction_user_usage_keys(
    incoming: Incoming,
    rule: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    identity = _interaction_user_usage_identity(incoming, data)
    if identity is None:
        return None
    identity_key, _label = identity
    raw = f"{incoming.account_id}:{incoming.chat_id or 0}:{rule.get('id') or 'legacy'}:{identity_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    today = time.strftime("%Y%m%d", time.localtime())
    return (
        f"{_INTERACTION_USER_COOLDOWN_PREFIX}{digest}",
        f"{_INTERACTION_USER_DAILY_PREFIX}{today}:{digest}",
        f"{_INTERACTION_USER_PENDING_PREFIX}{digest}",
    )


def _interaction_user_usage_limits(rule: dict[str, Any]) -> tuple[int, int]:
    return (
        _duration_seconds(rule.get("user_cooldown_seconds"), 0),
        _int_or_none(rule.get("daily_limit_per_user")) or 0,
    )


def _interaction_user_pending_ttl(cooldown_seconds: int) -> int:
    return min(max(int(cooldown_seconds or 0), 30), 300)


def _interaction_user_label(incoming: Incoming) -> str:
    if incoming.username:
        return account_bot_service.html_text(f"@{incoming.username.strip().lstrip('@')}")
    if incoming.display_name:
        return account_bot_service.html_text(incoming.display_name)
    if incoming.user_id is not None:
        return str(incoming.user_id)
    return "该用户"


def _interaction_rule_feature_label(rule: dict[str, Any]) -> str:
    return account_bot_service.html_text(str(rule.get("name") or "该功能").strip() or "该功能")


async def _interaction_user_usage_block_message(
    incoming: Incoming,
    rule: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> str | None:
    keys = _interaction_user_usage_keys(incoming, rule, data)
    if keys is None:
        return None
    cooldown_seconds, daily_limit = _interaction_user_usage_limits(rule)
    if cooldown_seconds <= 0 and daily_limit <= 0:
        return None
    cooldown_key, daily_key, pending_key = keys
    identity = _interaction_user_usage_identity(incoming, data)
    user = identity[1] if identity is not None else _interaction_user_label(incoming)
    feature = _interaction_rule_feature_label(rule)
    try:
        redis = get_redis()
        raw_count = await redis.get(daily_key)
        if isinstance(raw_count, bytes):
            raw_count = raw_count.decode("utf-8", errors="ignore")
        count = max(0, int(raw_count or 0))
        if daily_limit > 0 and count >= daily_limit:
            return f"{user} 今日已成功{feature} {count}/{daily_limit} 次，当日无法再次使用。"
        if await redis.get(pending_key):
            remaining = _interaction_user_pending_ttl(cooldown_seconds)
            ttl_fn = getattr(redis, "ttl", None)
            if callable(ttl_fn):
                try:
                    ttl_value = int(await ttl_fn(pending_key))
                    if ttl_value > 0:
                        remaining = ttl_value
                except Exception:  # noqa: BLE001
                    remaining = _interaction_user_pending_ttl(cooldown_seconds)
            return f"{user} 正在处理{feature}，请稍后再试（预计 {_format_duration(remaining)}）。"
        if cooldown_seconds > 0 and await redis.get(cooldown_key):
            ttl_fn = getattr(redis, "ttl", None)
            remaining = cooldown_seconds
            if callable(ttl_fn):
                try:
                    ttl_value = int(await ttl_fn(cooldown_key))
                    if ttl_value > 0:
                        remaining = ttl_value
                except Exception:  # noqa: BLE001
                    remaining = cooldown_seconds
            limit_part = f" {count}/{daily_limit} 次，" if daily_limit > 0 else "，"
            return f"{user} 今日已成功{feature}{limit_part}距离下次可用 CD 还剩 {_format_duration(remaining)}。"
    except Exception:  # noqa: BLE001
        log.debug("interaction user usage check failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)
    return None


async def _claim_interaction_user_usage(
    incoming: Incoming,
    rule: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    keys = _interaction_user_usage_keys(incoming, rule, data)
    if keys is None:
        return True, None
    cooldown_seconds, daily_limit = _interaction_user_usage_limits(rule)
    if cooldown_seconds <= 0 and daily_limit <= 0:
        return True, None
    _cooldown_key, _daily_key, pending_key = keys
    try:
        redis = get_redis()
        claimed = await redis.set(
            pending_key,
            "1",
            ex=_interaction_user_pending_ttl(cooldown_seconds),
            nx=True,
        )
        return bool(claimed), pending_key if claimed else None
    except Exception:  # noqa: BLE001
        log.debug("interaction user usage claim failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)
        return True, None


async def _release_interaction_user_usage_claim(pending_key: str | None) -> None:
    if not pending_key:
        return
    try:
        redis = get_redis()
        delete = getattr(redis, "delete", None)
        if callable(delete):
            await delete(pending_key)
    except Exception:  # noqa: BLE001
        log.debug("interaction user usage claim release failed", exc_info=True)


async def _mark_interaction_user_usage(
    incoming: Incoming,
    rule: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> None:
    keys = _interaction_user_usage_keys(incoming, rule, data)
    if keys is None:
        return
    cooldown_seconds, daily_limit = _interaction_user_usage_limits(rule)
    if cooldown_seconds <= 0 and daily_limit <= 0:
        return
    cooldown_key, daily_key, _pending_key = keys
    try:
        redis = get_redis()
        if cooldown_seconds > 0:
            await redis.set(cooldown_key, "1", ex=cooldown_seconds)
        if daily_limit > 0:
            incr = getattr(redis, "incr", None)
            expire = getattr(redis, "expire", None)
            if callable(incr):
                count = int(await incr(daily_key))
                if count == 1 and callable(expire):
                    await expire(daily_key, _seconds_until_local_midnight())
            else:
                raw_count = await redis.get(daily_key)
                if isinstance(raw_count, bytes):
                    raw_count = raw_count.decode("utf-8", errors="ignore")
                count = max(0, int(raw_count or 0)) + 1
                await redis.set(daily_key, str(count), ex=_seconds_until_local_midnight())
    except Exception:  # noqa: BLE001
        log.debug("interaction user usage mark failed aid=%s rule=%s", incoming.account_id, rule.get("id"), exc_info=True)


def _receiver_name_matches(receiver_filter: str | None, receiver_name: str) -> bool:
    if not receiver_filter:
        return True
    expected = str(receiver_filter or "").strip().lstrip("@").casefold()
    actual = str(receiver_name or "").strip().lstrip("@").casefold()
    return bool(expected and actual and expected == actual)


def _user_identity_texts(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        candidates = [text]
        username_match = re.search(r"@([A-Za-z0-9_]{3,})", text)
        if username_match:
            candidates.append(username_match.group(1))
            candidates.append("@" + username_match.group(1))
        for item in candidates:
            normalized = item.strip()
            if normalized and normalized not in out:
                out.append(normalized)
    return out


async def _rule_receiver_filter(db: Any, account_id: int, rule: dict[str, Any]) -> dict[str, Any]:
    explicit_text = str(rule.get("receiver_text") or "").strip()
    try:
        explicit_user_id = int(rule["receiver_user_id"]) if rule.get("receiver_user_id") not in (None, "") else None
    except (TypeError, ValueError):
        explicit_user_id = None
    if explicit_text or explicit_user_id is not None:
        return {"user_id": explicit_user_id, "texts": _user_identity_texts(explicit_text), "explicit": True}
    get_account = getattr(db, "get", None)
    account = await get_account(Account, account_id) if callable(get_account) else None
    account_user_id = _int_or_none(getattr(account, "tg_user_id", None)) if account is not None else None
    username = str(getattr(account, "tg_username", "") or "").strip()
    display_name = str(getattr(account, "display_name", "") or "").strip()
    return {"user_id": account_user_id, "texts": _user_identity_texts(username, f"@{username}" if username else "", display_name), "explicit": False}


def _receiver_matches_filter(receiver_filter: dict[str, Any], *, user_id: int | None, name: str | None, username: str | None = None) -> bool:
    expected_user_id = receiver_filter.get("user_id")
    texts = receiver_filter.get("texts") if isinstance(receiver_filter.get("texts"), list) else []
    if expected_user_id is not None:
        if user_id is not None:
            return int(user_id) == int(expected_user_id)
        if not texts:
            return False
    if not texts:
        return False
    actuals = _user_identity_texts(name, username, f"@{username}" if username else "")
    return any(_receiver_name_matches(expected, actual) for expected in texts for actual in actuals)


def _transfer_command_receiver_from_filter(receiver_filter: dict[str, Any]) -> dict[str, Any] | None:
    texts = receiver_filter.get("texts") if isinstance(receiver_filter.get("texts"), list) else []
    receiver_user_id = _int_or_none(receiver_filter.get("user_id"))
    receiver_name = next((str(item).strip() for item in texts if str(item or "").strip()), "")
    if not receiver_name and receiver_user_id is not None:
        receiver_name = str(receiver_user_id)
    if not receiver_name:
        return None
    receiver_username = receiver_name.removeprefix("@") if receiver_name.startswith("@") else None
    return {
        "receiver_name": receiver_name,
        "receiver_user_id": receiver_user_id,
        "receiver_username": receiver_username,
    }


def _trusted_transfer_notice_sender_matches(cfg: dict[str, Any], sender_id: int | None) -> bool:
    if sender_id is None:
        return False
    trusted_ids = {
        int(value)
        for value in (cfg.get("trusted_bot_id"), cfg.get("transfer_bot_id"))
        if value not in (None, "")
    }
    if not trusted_ids:
        return False
    return int(sender_id) in trusted_ids


async def _is_account_user_sender(db: Any, account_id: int, user_id: int) -> bool:
    get_account = getattr(db, "get", None)
    if not callable(get_account):
        return False
    account = await get_account(Account, account_id)
    account_tg_user_id = getattr(account, "tg_user_id", None) if account is not None else None
    return account_tg_user_id is not None and int(account_tg_user_id) == int(user_id)


async def _select_transfer_command_receiver(
    db: Any,
    incoming: Incoming,
    cfg: dict[str, Any],
    amount: int,
) -> dict[str, Any] | None:
    if incoming.reply_to_display_name and not _is_configured_bot_user_id(cfg, incoming.reply_to_user_id):
        return {
            "receiver_name": incoming.reply_to_display_name,
            "receiver_user_id": incoming.reply_to_user_id,
            "receiver_username": incoming.reply_to_username,
        }
    for rule in _interaction_rules(cfg):
        if not _rule_chat_matches(rule, incoming.chat_id or 0):
            continue
        if not _rule_trigger_mode_allows(rule, "payment"):
            continue
        if not _rule_amount_matches(rule, amount):
            continue
        receiver_filter = await _rule_receiver_filter(db, incoming.account_id, rule)
        receiver_info = _transfer_command_receiver_from_filter(receiver_filter)
        if receiver_info is not None:
            return receiver_info
    return None


def _transfer_command_chat_is_monitored(incoming: Incoming, cfg: dict[str, Any]) -> bool:
    if incoming.chat_id is None:
        return False
    if _interaction_chat_matches(cfg, incoming.chat_id):
        return True
    for rule in _interaction_rules(cfg):
        if not _rule_chat_matches(rule, incoming.chat_id or 0):
            continue
        return True
    return False


def _is_configured_bot_user_id(cfg: dict[str, Any], user_id: int | None) -> bool:
    if user_id is None:
        return False
    bot_ids = {
        int(value)
        for value in (cfg.get("interaction_bot_id"), cfg.get("transfer_bot_id"), cfg.get("trusted_bot_id"))
        if value not in (None, "")
    }
    return int(user_id) in bot_ids


async def _select_transfer_notice_rule(
    db: Any,
    incoming: Incoming,
    cfg: dict[str, Any],
    parsed: dict[str, Any],
) -> dict[str, Any] | None:
    parsed_amount = int(parsed.get("amount") or 0)
    parsed_receiver = str(parsed.get("receiver_name") or "")
    parsed_receiver_id = _int_or_none(parsed.get("receiver_user_id"))
    for rule in _interaction_rules(cfg, include_disabled=True):
        if not _rule_chat_matches(rule, incoming.chat_id or 0):
            continue
        has_active_session = bool(
            await _list_interaction_sessions_for_rule(incoming.account_id, rule, incoming.chat_id)
        )
        if has_active_session:
            if not _rule_entry_allows_event(rule, "payment_confirmed"):
                continue
            if not _rule_amount_matches(rule, parsed_amount):
                continue
        else:
            if not rule.get("enabled", True):
                continue
            if not _rule_trigger_mode_allows(rule, "payment"):
                continue
            if not _rule_matches_payment_notice_trigger(rule, incoming, parsed):
                continue
            if not _rule_amount_matches(rule, parsed_amount):
                continue
            if not await _is_interaction_rule_open(incoming.account_id, rule, incoming.chat_id):
                continue
        receiver_filter = await _rule_receiver_filter(db, incoming.account_id, rule)
        if not _receiver_matches_filter(receiver_filter, user_id=parsed_receiver_id, name=parsed_receiver):
            continue
        return rule
    return None


async def _execute_interaction_rule(
    incoming: Incoming,
    rule: dict[str, Any],
    parsed: dict[str, Any] | None = None,
    *,
    event_type: str = "payment_confirmed",
) -> bool:
    if str(rule.get("action") or "") == "module" and not _rule_entry_allows_event(rule, event_type):
        log.info(
            "interaction module event ignored by declared entry events aid=%s chat_id=%s rule=%s event=%s",
            incoming.account_id,
            incoming.chat_id,
            rule.get("id"),
            event_type,
        )
        return False
    if rule.get("action") == "math10":
        log.warning(
            "deprecated interaction rule action=math10 used aid=%s chat_id=%s rule=%s; use action=module module_key=math10 instead",
            incoming.account_id,
            incoming.chat_id,
            rule.get("id"),
        )
        await _start_math_game(incoming, prize=int(rule.get("math_prize") or 123))
        return True
    if rule.get("action") == "module":
        ok, keep_session = await _run_interaction_module(
            incoming,
            rule,
            parsed=parsed,
            event_type=event_type,
        )
        if ok and keep_session:
            await _save_interaction_session(incoming, rule, event_type, parsed)
        return ok
    text = _render_transfer_notice_response(str(rule.get("response_template") or ""), parsed or {})
    await _send(incoming, text)
    return True


async def _try_handle_interaction_payment_confirm(db: Any, incoming: Incoming) -> bool:
    nonce = _parse_interaction_payment_confirm_callback(incoming.callback_data)
    if nonce is None:
        return False
    if not incoming.callback_id:
        return False
    if incoming.user_id is None:
        await _answer_callback(
            incoming,
            text="无法识别你的 Telegram 身份",
            show_alert=True,
        )
        return True
    redis = get_redis()
    raw = await _read_interaction_payment_confirm_payload(redis, nonce)
    if not raw:
        await _answer_callback(
            incoming,
            text="确认已过期，请重新付款触发。",
            show_alert=True,
        )
        return True
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        await _answer_callback(
            incoming,
            text="确认数据无效，请重新触发。",
            show_alert=True,
        )
        return True
    if not isinstance(payload, dict) or int(payload.get("account_id") or 0) != incoming.account_id:
        await _answer_callback(
            incoming,
            text="确认票据不匹配。",
            show_alert=True,
        )
        return True
    if _int_or_none((payload.get("incoming") if isinstance(payload.get("incoming"), dict) else {}).get("chat_id")) != incoming.chat_id:
        await _answer_callback(
            incoming,
            text="请在原群内确认。",
            show_alert=True,
        )
        return True
    rule = payload.get("rule") if isinstance(payload.get("rule"), dict) else None
    parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else None
    replay_incoming = _incoming_from_payment_confirm_payload(incoming.token, payload, incoming)
    if rule is None or parsed is None or replay_incoming is None:
        await _answer_callback(
            incoming,
            text="确认数据不完整，请重新触发。",
            show_alert=True,
        )
        return True
    if not _payment_confirm_name_matches(parsed.get("payer_name"), incoming):
        await _answer_callback(
            incoming,
            text="这条到账通知的付款人名称与你不一致。",
            show_alert=True,
        )
        return True
    consumed = await _consume_interaction_payment_confirm_payload(redis, nonce)
    if not consumed:
        await _answer_callback(
            incoming,
            text="确认已被处理，请勿重复点击。",
            show_alert=True,
        )
        return True
    parsed = dict(parsed)
    parsed["payer_user_id"] = incoming.user_id
    parsed["payer_name"] = str(parsed.get("payer_name") or incoming.display_name or incoming.user_id)
    parsed["payer_identity_confidence"] = _PLAYER_IDENTITY_CONFIDENCE_CALLBACK
    replay_incoming.reply_to_user_id = incoming.user_id
    replay_incoming.reply_to_display_name = incoming.display_name
    replay_incoming.reply_to_username = incoming.username
    usage_block = await _interaction_user_usage_block_message(replay_incoming, rule, parsed)
    if usage_block:
        await _answer_callback(
            incoming,
            text=_plain_callback_text(usage_block),
            show_alert=True,
        )
        return True
    claimed_usage, usage_pending_key = await _claim_interaction_user_usage(replay_incoming, rule, parsed)
    if not claimed_usage:
        await _answer_callback(
            incoming,
            text="你正在处理该功能，请稍后再试。",
            show_alert=True,
        )
        return True
    executed = False
    try:
        executed = await _execute_interaction_rule(replay_incoming, rule, parsed)
        if executed:
            await _mark_interaction_user_usage(replay_incoming, rule, parsed)
    finally:
        await _release_interaction_user_usage_claim(usage_pending_key)
    await _answer_callback(
        incoming,
        text="已确认，正在启动玩法。" if executed else "玩法启动失败，请稍后重试。",
        show_alert=not executed,
    )
    if executed:
        await _write_interaction_runtime_log(
            replay_incoming,
            "info",
            "interaction payment player confirmed",
            payer_user_id=incoming.user_id,
            payer_name=parsed.get("payer_name"),
            rule_id=rule.get("id"),
            **_interaction_log_context(replay_incoming),
        )
    return True


async def _try_handle_event_bus_subscriptions(
    db: Any,
    incoming: Incoming,
    cfg: dict[str, Any],
) -> tuple[bool, bool]:
    """Deliver interaction bot events to plugins that declare Event Bus subscriptions.

    The legacy rule pipeline remains the fallback for plugins without
    ``event_subscriptions``.  This path is intentionally narrow and trace-heavy:
    every candidate produces a stable matched/skipped span.
    """

    try:
        subscriptions = await _load_enabled_event_bus_subscriptions(db, incoming.account_id)
    except Exception as exc:  # noqa: BLE001
        await record_span(
            trace_log_context(incoming.trace_id),
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="subscription_load_failed",
            message="加载 Event Bus 订阅失败，回退旧规则链路。",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False, True
    if not subscriptions:
        await record_span(
            trace_log_context(incoming.trace_id),
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="subscription_not_matched",
            message="没有已启用插件声明 Event Bus 订阅。",
        )
        return False, True
    event = _incoming_trace_payload(incoming)
    event["trace_id"] = incoming.trace_id
    account_state = await _event_bus_account_state(db, incoming, cfg)
    result = dispatch_event(event, subscriptions, account_state)
    terminal_handled = False
    all_ok = True
    event_type = _incoming_event_type(incoming)
    for decision in result.decisions:
        span_status = TRACE_STATUS_OK if decision.matched else TRACE_STATUS_SKIPPED
        await record_span(
            trace_log_context(incoming.trace_id),
            "subscription_match",
            span_status,
            component="event_bus",
            plugin_key=decision.plugin_key,
            entry_key=decision.entry_key,
            reason_code=decision.reason_code,
            message=decision.reason_message,
            dispatch_mode=decision.dispatch_mode,
            scope=decision.scope,
            filters=decision.filters,
        )
        if not decision.matched:
            continue
        entry_key = str(decision.entry_key or "").strip()
        if not entry_key:
            all_ok = False
            await record_span(
                trace_log_context(incoming.trace_id),
                "plugin_invoke",
                TRACE_STATUS_SKIPPED,
                component="event_bus",
                plugin_key=decision.plugin_key,
                reason_code="entry_key_missing",
                message="Event Bus 订阅缺少 entry_key，无法投递给插件入口。",
            )
            continue
        payload = _event_bus_plugin_payload(incoming, event, decision)
        ok, error, actions = await _run_worker_interaction_entry(
            incoming,
            plugin_key=decision.plugin_key,
            entry_key=entry_key,
            payload=payload,
        )
        if not ok:
            all_ok = False
            if event_type != "message":
                terminal_handled = True
            await _remember_interaction_debug_state(incoming, stage="plugin_error", payload=payload, error=error)
            continue
        rule = _event_bus_virtual_rule(decision)
        guarded = await _guard_interaction_actions(incoming, rule, actions)
        await _apply_interaction_actions(
            incoming,
            guarded,
            context=_interaction_trace_context(payload),
        )
        if actions or guarded or event_type not in {"message", "callback_query"}:
            terminal_handled = True
    return terminal_handled, all_ok


def _legacy_rule_event_bus_decision(
    incoming: Incoming,
    rule: dict[str, Any],
    *,
    event_type: str,
) -> tuple[dict[str, Any], Any | None]:
    module_key = str(rule.get("module_key") or "").strip()
    entry_key = str(rule.get("module_action") or "").strip()
    event = _incoming_trace_payload(incoming, event_type=event_type)
    event["trace_id"] = incoming.trace_id
    subscription = normalize_event_subscription(
        {
            "source": ["interaction_bot"],
            "events": [event_type],
            "scope": "rule_bound",
            "entry_key": entry_key,
            "dispatch_mode": "rule_bound",
            "filters": {
                "rule_id": rule.get("id"),
                "event_type": event_type,
                "chat_id": incoming.chat_id,
            },
        },
        plugin_key=module_key,
        entry_key=entry_key,
    )
    result = dispatch_event(
        event,
        [subscription],
        {
            "allowed_chat_ids": "*",
            "known_user_ids": [incoming.user_id] if incoming.user_id is not None else [],
            "trigger": {"rule_id": rule.get("id")},
        },
    )
    return event, result.decisions[0] if result.decisions else None


async def _try_handle_event_bus_payment_notice(
    db: Any,
    incoming: Incoming,
    cfg: dict[str, Any],
    parsed: dict[str, Any],
    rule: dict[str, Any],
) -> tuple[bool, bool]:
    """Deliver an external transfer notice to the rule-bound payment subscriber."""

    try:
        subscriptions = await _load_enabled_event_bus_subscriptions(db, incoming.account_id)
    except Exception as exc:  # noqa: BLE001
        await record_span(
            trace_log_context(incoming.trace_id),
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus_payment_notice",
            reason_code="subscription_load_failed",
            message="加载 Event Bus 付款订阅失败，回退旧付款规则。",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False, True
    subscriptions = _event_bus_subscriptions_for_rule(subscriptions, rule)
    if not subscriptions:
        return False, True
    raw_update = incoming.native_raw if isinstance(incoming.native_raw, dict) else {
        "update_id": incoming.update_id,
        "message": {
            "message_id": incoming.message_id,
            "text": incoming.text,
            "chat": {"id": incoming.chat_id, "type": incoming.chat_type},
            "from": {
                "id": incoming.user_id,
                "first_name": incoming.display_name,
                "username": incoming.username,
            },
        },
    }
    event = normalize_payment_notice(incoming.account_id, raw_update, parsed)
    event["trace_id"] = incoming.trace_id
    event["source_actor"] = {
        "type": "external_bot",
        "user_id": incoming.user_id,
        "display_name": incoming.display_name,
        "username": incoming.username,
    }
    event["actor"] = {
        "user_id": parsed.get("payer_user_id"),
        "display_name": parsed.get("payer_name"),
    }
    event["player"] = dict(event["actor"])
    event["reply_to"] = {
        "user_id": incoming.reply_to_user_id,
        "message_id": incoming.reply_to_message_id,
        "display_name": incoming.reply_to_display_name,
        "username": incoming.reply_to_username,
        "text": incoming.reply_to_text,
    } if incoming.reply_to_message_id or incoming.reply_to_text else event.get("reply_to")
    account_state = await _event_bus_account_state(db, incoming, cfg)
    account_state["trigger"] = {
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "module_key": rule.get("module_key"),
        "entry_key": rule.get("module_action"),
    }
    result = dispatch_event(event, subscriptions, account_state)
    handled = False
    all_ok = True
    for decision in result.decisions:
        await record_span(
            trace_log_context(incoming.trace_id),
            "subscription_match",
            TRACE_STATUS_OK if decision.matched else TRACE_STATUS_SKIPPED,
            component="event_bus_payment_notice",
            plugin_key=decision.plugin_key,
            entry_key=decision.entry_key,
            reason_code=decision.reason_code,
            message=decision.reason_message,
            dispatch_mode=decision.dispatch_mode,
            scope=decision.scope,
            filters=decision.filters,
        )
        if not decision.matched:
            continue
        handled = True
        entry_key = str(decision.entry_key or "").strip()
        if not entry_key:
            all_ok = False
            await record_span(
                trace_log_context(incoming.trace_id),
                "plugin_invoke",
                TRACE_STATUS_SKIPPED,
                component="event_bus_payment_notice",
                plugin_key=decision.plugin_key,
                reason_code="entry_key_missing",
                message="Event Bus 付款订阅缺少 entry_key，无法投递给插件入口。",
            )
            continue
        payload = _event_bus_payment_plugin_payload(incoming, event, decision, parsed)
        payload.update(
            {
                "rule_id": str(rule.get("id") or ""),
                "rule_name": str(rule.get("name") or ""),
                "entry_key": str(rule.get("module_action") or ""),
                "module_config": dict(rule.get("module_config") or {}) if isinstance(rule.get("module_config"), dict) else {},
                "valid_seconds": _interaction_session_ttl(rule),
            }
        )
        trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
        trigger.update(_interaction_trigger_envelope(rule, "payment_confirmed", parsed))
        trigger.update(
            {
                "dispatch_mode": decision.dispatch_mode,
                "scope": decision.scope,
                "filters": dict(decision.filters or {}),
            }
        )
        payload["trigger"] = trigger
        ok, error, actions = await _run_worker_interaction_entry(
            incoming,
            plugin_key=decision.plugin_key,
            entry_key=entry_key,
            payload=payload,
        )
        if not ok:
            all_ok = False
            await _remember_interaction_debug_state(incoming, stage="plugin_error", payload=payload, error=error)
            continue
        rule = _event_bus_virtual_rule(decision)
        guarded = await _guard_interaction_actions(incoming, rule, actions)
        await _apply_interaction_actions(
            incoming,
            guarded,
            context=_interaction_trace_context(payload),
        )
    return handled, all_ok


def _event_bus_subscriptions_for_rule(subscriptions: list[Any], rule: dict[str, Any]) -> list[Any]:
    module_key = str(rule.get("module_key") or "").strip()
    entry_key = str(rule.get("module_action") or "").strip()
    if not module_key or not entry_key:
        return []
    return [
        subscription
        for subscription in subscriptions
        if getattr(subscription, "plugin_key", None) == module_key
        and getattr(subscription, "entry_key", None) == entry_key
    ]


async def _load_enabled_event_bus_subscriptions(db: Any, account_id: int) -> list[Any]:
    rows = (
        await db.execute(
            select(AccountFeature).where(
                AccountFeature.account_id == account_id,
                AccountFeature.enabled.is_(True),
            )
        )
    ).scalars().all()
    out: list[Any] = []
    for row in rows:
        plugin_key = str(getattr(row, "feature_key", "") or "").strip()
        if not plugin_key:
            continue
        for raw in account_bot_service.declared_module_event_subscriptions(plugin_key):
            subscription = normalize_event_subscription(raw, plugin_key=plugin_key)
            out.append(subscription)
    return out


async def _event_bus_account_state(db: Any, incoming: Incoming, cfg: dict[str, Any]) -> dict[str, Any]:
    owner_ids: list[int] = []
    try:
        account = await db.get(Account, incoming.account_id)
        owner_id = _int_or_none(getattr(account, "tg_user_id", None))
        if owner_id is not None:
            owner_ids.append(owner_id)
    except Exception:  # noqa: BLE001
        log.debug("load event bus account owner failed aid=%s", incoming.account_id, exc_info=True)
    return {
        "allowed_chat_ids": _interaction_allowed_chat_ids(cfg),
        "owner_user_ids": owner_ids,
        "known_user_ids": owner_ids + ([incoming.user_id] if incoming.user_id is not None else []),
    }


def _interaction_allowed_chat_ids(cfg: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for key in ("chat_id",):
        chat_id = _int_or_none(cfg.get(key))
        if chat_id is not None and chat_id not in ids:
            ids.append(chat_id)
    raw_chat_ids = cfg.get("chat_ids")
    if isinstance(raw_chat_ids, list):
        for raw in raw_chat_ids:
            chat_id = _int_or_none(raw)
            if chat_id is not None and chat_id not in ids:
                ids.append(chat_id)
    for rule in _interaction_rules(cfg):
        raw_rule_chat_ids = rule.get("chat_ids")
        if not isinstance(raw_rule_chat_ids, list):
            continue
        for raw in raw_rule_chat_ids:
            chat_id = _int_or_none(raw)
            if chat_id is not None and chat_id not in ids:
                ids.append(chat_id)
    return ids


def _event_bus_plugin_payload(incoming: Incoming, event: dict[str, Any], decision: Any) -> dict[str, Any]:
    payload = dict(event)
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    payload["trace_id"] = incoming.trace_id
    payload["account_id"] = incoming.account_id
    payload["chat_id"] = incoming.chat_id
    payload["message_text"] = incoming.text
    payload["callback_query_id"] = incoming.callback_id
    payload["callback_data"] = incoming.callback_data
    payload["sender_user_id"] = incoming.user_id
    payload["sender_name"] = incoming.display_name
    payload["sender_username"] = incoming.username
    payload["actor"] = dict(sender)
    payload["source_actor"] = dict(sender)
    payload["player"] = dict(sender)
    trigger = dict(payload.get("trigger") or {}) if isinstance(payload.get("trigger"), dict) else {}
    trigger.update(
        {
            "rule_id": f"eventbus:{decision.plugin_key}:{decision.entry_key or 'main'}",
            "rule_name": f"Event Bus / {decision.plugin_key}",
            "module_key": decision.plugin_key,
            "entry_key": decision.entry_key,
            "dispatch_mode": decision.dispatch_mode,
            "scope": decision.scope,
            "filters": dict(decision.filters or {}),
        }
    )
    payload["trigger"] = trigger
    allowed = account_bot_service.plugin_declares_telegram_native_raw(decision.plugin_key, source="interaction_bot")
    payload["native_raw_meta"] = _native_raw_meta(incoming, object_name="update", enabled=allowed)
    payload["native_raw"] = incoming.native_raw if allowed else None
    if not allowed:
        payload.pop("raw_event", None)
    return payload


def _event_bus_payment_plugin_payload(
    incoming: Incoming,
    event: dict[str, Any],
    decision: Any,
    parsed: dict[str, Any],
) -> dict[str, Any]:
    """Build a payment_confirmed payload where actor/player is the payer.

    The Telegram message sender is the transfer-notice bot. Plugins should see
    that bot as sender/source_actor, while actor/player/payment describe the
    user who actually paid.
    """

    payload = _event_bus_plugin_payload(incoming, event, decision)
    data = dict(parsed or {})
    data["event_type"] = "payment_confirmed"
    actor = _interaction_actor_envelope(incoming, data)
    player = _interaction_player_envelope(incoming, data, event_type="payment_confirmed")
    source_actor = _interaction_source_actor_envelope(incoming, data)
    existing_source_actor = event.get("source_actor")
    if isinstance(existing_source_actor, dict):
        source_actor = {**existing_source_actor, **source_actor}
        if existing_source_actor.get("type") is not None:
            source_actor["type"] = existing_source_actor["type"]
    payer_user_id = _int_or_none(player.get("user_id"))
    payer_name = str(player.get("display_name") or data.get("payer_name") or "").strip()
    amount = _int_or_none(data.get("amount"))
    payload.update(
        {
            "event_type": "payment_confirmed",
            "payment": _interaction_payment_envelope(
                incoming,
                data,
                payer_user_id=payer_user_id,
                payer_name=payer_name or None,
            ),
            "raw_payment": data,
            "sender": source_actor,
            "source_actor": source_actor,
            "actor": actor,
            "player": player,
            "reply_to": _interaction_reply_to_envelope(incoming),
            "payer_user_id": payer_user_id,
            "payer_name": payer_name or None,
            "amount": amount,
            "payment_amount": amount,
        }
    )
    return payload


def _event_bus_virtual_rule(decision: Any) -> dict[str, Any]:
    return {
        "id": f"eventbus:{decision.plugin_key}:{decision.entry_key or 'main'}",
        "name": f"Event Bus / {decision.plugin_key}",
        "action": "module",
        "module_key": decision.plugin_key,
        "module_action": decision.entry_key,
    }


async def _try_handle_interaction_rule_command_or_keyword(db: Any, incoming: Incoming) -> bool:
    if incoming.callback_id or incoming.chat_id is None:
        return False
    cfg = await account_bot_service.get_transfer_notice_config(db, incoming.account_id)
    if not cfg.get("enabled"):
        return False
    if _message_equals_any(incoming.text, _interaction_query_commands(cfg)):
        message = await _render_interaction_rules_query(db, incoming, cfg)
        if message is not None:
            await _send(incoming, message, reply_to_message_id=incoming.message_id)
            return True
        return False
    for rule in _interaction_rules(cfg):
        if not _rule_chat_matches(rule, incoming.chat_id):
            continue
        if _message_equals_any(incoming.text, _rule_keyword_list(rule, "open_commands")):
            if incoming.user_id is None or not await _is_account_user_sender(db, incoming.account_id, incoming.user_id):
                log.info(
                    "interaction rule open command ignored: sender is not account owner aid=%s chat_id=%s sender_id=%s rule=%s",
                    incoming.account_id,
                    incoming.chat_id,
                    incoming.user_id,
                    rule.get("id"),
                )
                return True
            await _set_interaction_rule_open(incoming.account_id, rule, incoming.chat_id, True)
            await _send(incoming, f"规则「{account_bot_service.html_text(str(rule.get('name') or rule.get('id') or '未命名'))}」已开启。")
            return True
        if _message_equals_any(incoming.text, _rule_keyword_list(rule, "close_commands")):
            if incoming.user_id is None or not await _is_account_user_sender(db, incoming.account_id, incoming.user_id):
                log.info(
                    "interaction rule close command ignored: sender is not account owner aid=%s chat_id=%s sender_id=%s rule=%s",
                    incoming.account_id,
                    incoming.chat_id,
                    incoming.user_id,
                    rule.get("id"),
                )
                return True
            await _set_interaction_rule_open(incoming.account_id, rule, incoming.chat_id, False)
            closed = await _close_active_interaction_games(incoming, rule)
            suffix = f"已结束 {closed} 个进行中的游戏。" if closed else "当前没有进行中的游戏。"
            await _send(incoming, f"规则「{account_bot_service.html_text(str(rule.get('name') or rule.get('id') or '未命名'))}」已关闭，{suffix}")
            return True
        if _message_equals_any(incoming.text, _rule_keyword_list(rule, "status_commands")):
            open_ = await _is_interaction_rule_open(incoming.account_id, rule, incoming.chat_id)
            status = "开启中" if open_ else "已关闭"
            await _send(incoming, f"规则「{account_bot_service.html_text(str(rule.get('name') or rule.get('id') or '未命名'))}」当前状态：{status}。")
            return True
        if not _rule_trigger_mode_allows(rule, "keyword"):
            continue
        keyword_payload = _message_match_keyword_pattern(
            incoming.text,
            _rule_keyword_list(rule, "module_start_keywords"),
        )
        if keyword_payload is None:
            continue
        if not await _is_interaction_rule_open(incoming.account_id, rule, incoming.chat_id):
            message = str(rule.get("disabled_message") or "").strip()
            if message:
                await _send(incoming, message)
            return True
        if _rule_has_paid_threshold(rule) and _interaction_participant_policy(rule) != "paid_pool":
            await _send(
                incoming,
                await _interaction_paid_threshold_message(db, incoming, rule),
                reply_to_message_id=incoming.message_id,
            )
            return True
        keyword_payload = dict(keyword_payload or {})
        keyword_payload.setdefault("keyword", incoming.text.strip())
        keyword_payload.setdefault("text", incoming.text.strip())
        usage_block = await _interaction_user_usage_block_message(incoming, rule)
        if usage_block:
            await _send(incoming, usage_block, reply_to_message_id=incoming.message_id)
            return True
        claimed_usage, usage_pending_key = await _claim_interaction_user_usage(incoming, rule)
        if not claimed_usage:
            usage_block = await _interaction_user_usage_block_message(incoming, rule)
            await _send(
                incoming,
                usage_block or "该用户正在处理该功能，请稍后再试。",
                reply_to_message_id=incoming.message_id,
            )
            return True
        if not await _claim_interaction_trigger(incoming, rule, "keyword", incoming.text):
            await _release_interaction_user_usage_claim(usage_pending_key)
            return True
        try:
            executed = await _execute_interaction_rule(
                incoming,
                rule,
                parsed=keyword_payload,
                event_type="keyword",
            )
            if executed:
                await _mark_interaction_user_usage(incoming, rule)
        finally:
            await _release_interaction_user_usage_claim(usage_pending_key)
        return True
    return False


async def _try_handle_interaction_module_message(db: Any, incoming: Incoming) -> bool:
    is_callback = bool(incoming.callback_id)
    event_type = "callback_query" if is_callback else "message"
    if incoming.chat_id is None:
        return False
    text = str(incoming.callback_data if is_callback else incoming.text or "").strip()
    if not text:
        return False
    if not is_callback and (text.startswith("/") or text.startswith(",")):
        return False
    cfg = await account_bot_service.get_transfer_notice_config(db, incoming.account_id)
    if not cfg.get("enabled"):
        return False
    for rule in _interaction_rules(cfg, include_disabled=True):
        if not _rule_chat_matches(rule, incoming.chat_id):
            continue
        if str(rule.get("action") or "") != "module":
            continue
        module_key = str(rule.get("module_key") or "").strip()
        entry_key = str(rule.get("module_action") or "").strip()
        if not module_key or not entry_key:
            continue
        if not _rule_entry_allows_event(rule, event_type):
            continue
        session = await _load_interaction_session(incoming, rule)
        if session is None:
            continue
        participant_block = _interaction_participant_block_message(incoming, rule, session) if is_callback else None
        if participant_block:
            if is_callback:
                await _answer_callback(
                    incoming,
                    text=participant_block,
                    show_alert=True,
                )
            else:
                await _send(incoming, participant_block, reply_to_message_id=incoming.message_id)
            return True
        if not is_callback:
            if _message_equals_any(text, _rule_keyword_list(rule, "open_commands")):
                continue
            if _message_equals_any(text, _rule_keyword_list(rule, "close_commands")):
                continue
            if _message_equals_any(text, _rule_keyword_list(rule, "status_commands")):
                continue
            if _message_equals_any(text, _rule_keyword_list(rule, "module_start_keywords")):
                continue
        _event, decision = _legacy_rule_event_bus_decision(incoming, rule, event_type=event_type)
        reason_code = getattr(decision, "reason_code", None) or "subscription_not_matched"
        decision_filters = dict(getattr(decision, "filters", None) or {})
        decision_filters["session_id"] = (
            session.get("session_id") if isinstance(session, dict) else getattr(session, "session_id", None)
        )
        await record_span(
            trace_log_context(incoming.trace_id, plugin_key=module_key, entry_key=entry_key),
            "subscription_match",
            TRACE_STATUS_OK if decision is not None and decision.matched else TRACE_STATUS_SKIPPED,
            component="interaction_session",
            plugin_key=module_key,
            entry_key=entry_key,
            reason_code=reason_code,
            message=getattr(decision, "reason_message", None) or "旧交互会话消息未命中 Event Bus rule_bound decision。",
            dispatch_mode=getattr(decision, "dispatch_mode", None) or "rule_bound",
            scope=getattr(decision, "scope", None) or "rule_bound",
            filters=decision_filters,
        )
        if decision is None or not decision.matched:
            continue
        payload = await _interaction_module_payload_async(
            incoming,
            rule,
            {
                "message_text": incoming.text,
                "callback_query_id": incoming.callback_id,
                "callback_data": incoming.callback_data,
                "sender_user_id": incoming.user_id,
                "sender_name": incoming.display_name,
                "sender_username": incoming.username,
                "message_id": incoming.message_id,
                "session": session,
            },
            event_type=event_type,
        )
        trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
        trigger.update(
            {
                "dispatch_mode": decision.dispatch_mode,
                "scope": decision.scope,
                "filters": dict(decision.filters or {}),
            }
        )
        payload["trigger"] = trigger
        await _remember_interaction_debug_state(incoming, stage="payload_built", payload=payload)
        ok, error, actions = await _run_worker_interaction_entry(
            incoming,
            plugin_key=module_key,
            entry_key=entry_key,
            payload=payload,
        )
        if not ok and _should_try_local_interaction_fallback(module_key, error):
            ok, error, actions = await _run_local_interaction_entry_fallback(
                incoming,
                plugin_key=module_key,
                entry_key=entry_key,
                payload=payload,
            )
        if not ok:
            await _remember_interaction_debug_state(
                incoming,
                stage="plugin_error",
                payload=payload,
                error=error,
            )
            log.info(
                "interaction module message ignored aid=%s plugin=%s entry=%s error=%s",
                incoming.account_id,
                module_key,
                entry_key,
                error,
            )
            continue
        if not actions:
            if is_callback:
                await _answer_callback(incoming)
                return True
            continue
        raw_actions = [dict(action) for action in actions]
        actions = await _guard_interaction_actions(incoming, rule, actions)
        await _remember_interaction_debug_state(
            incoming,
            stage="actions_guarded",
            payload=payload,
            raw_actions=raw_actions,
            guarded_actions=actions,
        )
        await _apply_interaction_start_session_actions(incoming, rule, actions)
        await _apply_interaction_actions(
            incoming,
            actions,
            context=_interaction_trace_context(payload),
        )
        if is_callback and not _interaction_actions_answer_callback(actions):
            await _answer_callback(incoming)
        if _interaction_actions_request_no_session(actions):
            await _clear_loaded_interaction_session(
                incoming.account_id,
                rule,
                incoming.chat_id,
                session,
                incoming_user_id=incoming.user_id,
            )
        return True
    return False


def _new_math_question() -> tuple[str, int]:
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    op = random.choice(["+", "-", "x"])
    if op == "+":
        return f"{a} + {b}", a + b
    if op == "-":
        high, low = max(a, b), min(a, b)
        return f"{high} - {low}", high - low
    return f"{a} x {b}", a * b


def _math_game_key(account_id: int, chat_id: int) -> str:
    return f"{_MATH_GAME_PREFIX}{int(account_id)}:{int(chat_id)}"


def _math_game_claim_key(state: MathGameState) -> str:
    return f"{_MATH_GAME_CLAIM_PREFIX}{state.account_id}:{state.chat_id}:{state.game_id}"


def _math_state_from_payload(payload: Any) -> MathGameState | None:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="ignore")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    try:
        return MathGameState(
            account_id=int(payload["account_id"]),
            chat_id=int(payload["chat_id"]),
            question=str(payload["question"]),
            answer=int(payload["answer"]),
            prize=int(payload.get("prize") or 123),
            active=bool(payload.get("active", True)),
            game_id=str(payload.get("game_id") or ""),
            created_at=float(payload.get("created_at") or 0.0),
            source_update_id=_int_or_none(payload.get("source_update_id")),
            source_message_id=_int_or_none(payload.get("source_message_id")),
            winner_update_id=_int_or_none(payload.get("winner_update_id")),
            winner_message_id=_int_or_none(payload.get("winner_message_id")),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _save_math_game_state(state: MathGameState) -> None:
    _MATH_GAMES[(state.account_id, state.chat_id)] = state
    try:
        redis = get_redis()
        await redis.set(
            _math_game_key(state.account_id, state.chat_id),
            json.dumps(asdict(state), ensure_ascii=False),
            ex=_MATH_GAME_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        log.debug("save math game state failed aid=%s chat_id=%s", state.account_id, state.chat_id, exc_info=True)


async def _load_math_game_state(account_id: int, chat_id: int) -> MathGameState | None:
    try:
        redis = get_redis()
        state = _math_state_from_payload(await redis.get(_math_game_key(account_id, chat_id)))
        if state is not None:
            _MATH_GAMES[(state.account_id, state.chat_id)] = state
            return state
    except Exception:  # noqa: BLE001
        log.debug("load math game state failed aid=%s chat_id=%s", account_id, chat_id, exc_info=True)
    return _MATH_GAMES.get((account_id, chat_id))


async def _claim_math_winner(state: MathGameState, incoming: Incoming) -> bool:
    try:
        redis = get_redis()
        acquired = await redis.set(
            _math_game_claim_key(state),
            str(incoming.message_id or incoming.update_id),
            ex=_MATH_GAME_TTL_SECONDS,
            nx=True,
        )
        if not acquired:
            return False
    except Exception:  # noqa: BLE001
        cached = _MATH_GAMES.get((state.account_id, state.chat_id))
        if cached is not state and (cached is None or not cached.active):
            return False
        log.debug("claim math winner fell back to memory aid=%s chat_id=%s", state.account_id, state.chat_id, exc_info=True)

    state.active = False
    state.winner_update_id = incoming.update_id
    state.winner_message_id = incoming.message_id
    await _save_math_game_state(state)
    return True


async def _start_math_game(incoming: Incoming, *, prize: int = 123) -> None:
    if incoming.chat_id is None:
        return
    question, answer = _new_math_question()
    account_holder = await _load_account_holder_label(incoming.account_id)
    state = MathGameState(
        account_id=incoming.account_id,
        chat_id=incoming.chat_id,
        question=question,
        answer=answer,
        prize=prize,
        game_id=secrets.token_hex(8),
        created_at=time.time(),
        source_update_id=incoming.update_id,
        source_message_id=incoming.message_id,
    )
    await _save_math_game_state(state)
    await _send(
        incoming,
        (
            "算数题测试开始\n"
            f"题目：{question} = ?\n"
            f"奖金：{prize}\n"
            f"直接发送答案，答对后我会公告赢家；奖金由 {account_holder} 人工发放。"
        ),
    )


async def _try_handle_math_answer(incoming: Incoming) -> bool:
    if incoming.chat_id is None or incoming.callback_id:
        return False
    state = await _load_math_game_state(incoming.account_id, incoming.chat_id)
    if state is None or not state.active:
        return False
    try:
        answer = int(incoming.text.strip())
    except ValueError:
        return False
    if answer != state.answer:
        return False
    if not await _claim_math_winner(state, incoming):
        return True
    winner = account_bot_service.html_text(incoming.display_name or str(incoming.user_id or "未知用户"))
    account_holder = await _load_account_holder_label(incoming.account_id)
    payout_mode = await _resolve_payout_mode(incoming.account_id, incoming.chat_id)
    payout_line = (
        f"奖金将由 {account_holder} 账号自动发放。"
        if payout_mode == "auto"
        else f"请由 {account_holder} 人工回复赢家发放奖金。"
    )
    await _send(
        incoming,
        (
            f"答对了：{winner}\n"
            f"题目：{state.question} = {state.answer}\n"
            f"奖金：{state.prize}\n"
            f"{payout_line}"
        ),
        reply_to_message_id=incoming.message_id,
    )
    return True


def _interaction_event_payload(
    incoming: Incoming,
    rule: dict[str, Any],
    event_type: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = InteractionEvent(
        type=event_type,
        account_id=incoming.account_id,
        chat_id=incoming.chat_id,
        rule_id=str(rule.get("id") or ""),
        rule_name=str(rule.get("name") or ""),
        module_key=str(rule.get("module_key") or ""),
        entry_key=str(rule.get("module_action") or ""),
        update_id=incoming.update_id,
        message_id=incoming.message_id,
        user_id=incoming.user_id,
        chat_type=incoming.chat_type,
        display_name=incoming.display_name,
        username=incoming.username,
        text=incoming.text,
        sender_chat_id=incoming.sender_chat_id,
        sender_chat_type=incoming.sender_chat_type,
        sender_chat_title=incoming.sender_chat_title,
        sender_chat_username=incoming.sender_chat_username,
        callback_query_id=incoming.callback_id,
        callback_data=incoming.callback_data,
        reply_to_user_id=incoming.reply_to_user_id,
        reply_to_message_id=incoming.reply_to_message_id,
        reply_to_display_name=incoming.reply_to_display_name,
        reply_to_username=incoming.reply_to_username,
        reply_to_sender_chat_id=incoming.reply_to_sender_chat_id,
        reply_to_sender_chat_type=incoming.reply_to_sender_chat_type,
        reply_to_sender_chat_title=incoming.reply_to_sender_chat_title,
        reply_to_sender_chat_username=incoming.reply_to_sender_chat_username,
        reply_to_text=incoming.reply_to_text,
        entity_languages=incoming.entity_languages,
        data=dict(data or {}),
    )
    return asdict(event)


def _interaction_source_envelope(incoming: Incoming, event_type: str) -> dict[str, Any]:
    source = {
        "type": event_type,
        "channel": "interaction_bot",
        "driver": "telegram_bot_api",
        "account_id": incoming.account_id,
        "chat_id": incoming.chat_id,
        "chat_type": incoming.chat_type,
        "update_id": incoming.update_id,
        "message_id": incoming.message_id,
        "text": incoming.text,
        "callback_query_id": incoming.callback_id,
        "callback_data": incoming.callback_data,
        "entity_languages": list(incoming.entity_languages),
    }
    sender_chat = _incoming_sender_chat_envelope(incoming)
    if sender_chat:
        source["sender_chat"] = sender_chat
    return source


def _interaction_message_envelope(incoming: Incoming) -> dict[str, Any]:
    message = {
        "chat_id": incoming.chat_id,
        "message_id": incoming.message_id,
        "text": incoming.text,
        "entities": [],
        "media": None,
        "date": None,
        "reply_to_message_id": incoming.reply_to_message_id,
    }
    sender_chat = _incoming_sender_chat_envelope(incoming)
    if sender_chat:
        message["sender_chat"] = sender_chat
    return message


def _interaction_chat_envelope(incoming: Incoming) -> dict[str, Any]:
    return {
        "id": incoming.chat_id,
        "type": incoming.chat_type,
        "title": None,
        "username": None,
    }


def _interaction_actor_envelope(incoming: Incoming, data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    payer_user_id = _interaction_payment_payer_user_id(incoming, payload)
    payer_name = _interaction_payment_payer_name(incoming, payload)
    if payer_user_id is not None:
        return {
            "user_id": payer_user_id,
            "display_name": payer_name or None,
            "username": incoming.reply_to_username,
        }
    if str(payload.get("event_type") or "").strip() == "payment_confirmed" and payer_name:
        return {
            "user_id": None,
            "display_name": payer_name,
            "username": None,
        }
    sender_chat = _incoming_sender_chat_envelope(incoming)
    if incoming.user_id is None and sender_chat:
        return {
            "user_id": None,
            "display_name": incoming.display_name or sender_chat.get("title"),
            "username": incoming.username,
            "sender_type": "chat",
            "sender_chat": sender_chat,
        }
    return {
        "user_id": _int_or_none(payload.get("sender_user_id")) or incoming.user_id,
        "display_name": str(payload.get("sender_name") or incoming.display_name or "").strip() or None,
        "username": str(payload.get("sender_username") or incoming.username or "").strip() or None,
    }


def _interaction_source_actor_envelope(incoming: Incoming, data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    actor = {
        "user_id": _int_or_none(payload.get("sender_user_id")) or incoming.user_id,
        "display_name": str(payload.get("sender_name") or incoming.display_name or "").strip() or None,
        "username": str(payload.get("sender_username") or incoming.username or "").strip() or None,
    }
    sender_chat = _incoming_sender_chat_envelope(incoming)
    if sender_chat:
        actor["sender_type"] = "chat" if actor["user_id"] is None else "user_via_chat"
        actor["sender_chat"] = sender_chat
    return actor


def _interaction_reply_to_envelope(incoming: Incoming) -> dict[str, Any] | None:
    if (
        incoming.reply_to_user_id is None
        and not incoming.reply_to_display_name
        and not incoming.reply_to_username
        and not incoming.reply_to_text
    ):
        return None
    reply = {
        "user_id": incoming.reply_to_user_id,
        "display_name": incoming.reply_to_display_name,
        "username": incoming.reply_to_username,
        "message_id": incoming.reply_to_message_id,
        "text": incoming.reply_to_text,
    }
    sender_chat = _incoming_reply_sender_chat_envelope(incoming)
    if sender_chat:
        reply["sender_type"] = "chat" if incoming.reply_to_user_id is None else "user_via_chat"
        reply["sender_chat"] = sender_chat
    return reply


def _incoming_sender_chat_envelope(incoming: Incoming) -> dict[str, Any] | None:
    if incoming.sender_chat_id is None and not incoming.sender_chat_title and not incoming.sender_chat_username:
        return None
    return {
        "id": incoming.sender_chat_id,
        "type": incoming.sender_chat_type,
        "title": incoming.sender_chat_title,
        "username": incoming.sender_chat_username,
    }


def _incoming_reply_sender_chat_envelope(incoming: Incoming) -> dict[str, Any] | None:
    if (
        incoming.reply_to_sender_chat_id is None
        and not incoming.reply_to_sender_chat_title
        and not incoming.reply_to_sender_chat_username
    ):
        return None
    return {
        "id": incoming.reply_to_sender_chat_id,
        "type": incoming.reply_to_sender_chat_type,
        "title": incoming.reply_to_sender_chat_title,
        "username": incoming.reply_to_sender_chat_username,
    }


def _interaction_raw_envelope(
    incoming: Incoming,
    rule: dict[str, Any],
    parsed: dict[str, Any] | None,
    event_type: str,
) -> dict[str, Any]:
    parsed_summary = {
        str(key): value
        for key, value in dict(parsed or {}).items()
        if str(key) not in {"bot_token", "token", "session", "secret", "api_key"}
    }
    return {
        "update_id": incoming.update_id,
        "message_id": incoming.message_id,
        "callback_query_id": incoming.callback_id,
        "callback_data": incoming.callback_data,
        "text": incoming.text,
        "event_type": event_type,
        "rule_id": str(rule.get("id") or ""),
        "module_key": str(rule.get("module_key") or ""),
        "entry_key": str(rule.get("module_action") or ""),
        "parsed": parsed_summary,
    }


def _interaction_trigger_envelope(
    rule: dict[str, Any],
    event_type: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(data or {})
    start_keywords = _rule_keyword_list(rule, "module_start_keywords")
    trigger = {
        "type": event_type,
        "rule_id": str(rule.get("id") or ""),
        "rule_name": str(rule.get("name") or ""),
        "module_key": str(rule.get("module_key") or ""),
        "entry_key": str(rule.get("module_action") or ""),
        "payload": payload,
    }
    if start_keywords:
        trigger["start_keywords"] = start_keywords
    keyword = str(payload.get("keyword") or "").strip()
    if keyword:
        trigger["keyword"] = keyword
    return trigger


def _interaction_session_envelope(
    incoming: Incoming,
    rule: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    session = payload.get("session")
    session_data = session if isinstance(session, dict) else {}
    session_user_id = _interaction_session_user_id(incoming, payload)
    return {
        "key": _interaction_session_key(incoming.account_id, rule, incoming.chat_id, session_user_id),
        "scope": str(rule.get("module_session_scope") or rule.get("concurrency") or "chat"),
        "participant_policy": _interaction_participant_policy(rule),
        "ttl_seconds": _interaction_session_ttl(rule),
        "active": True,
        "data": dict(session_data),
    }


def _interaction_settlement_envelope(
    data: dict[str, Any],
    *,
    prize: int,
    payout_account_label: str | None = None,
    payout_mode: str | None = None,
) -> dict[str, Any]:
    mode = str(payout_mode or "manual").strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "manual"
    is_payment = str(data.get("event_type") or "").strip() == "payment_confirmed"
    winner_user_id = _int_or_none(data.get("payer_user_id") if is_payment else data.get("sender_user_id") or data.get("payer_user_id"))
    winner_name = str(
        data.get("payer_name") if is_payment else data.get("sender_name") or data.get("payer_name") or ""
    ).strip() or None
    return {
        "mode": mode,
        "status": "pending",
        "amount": prize,
        "currency": None,
        "winner_user_id": winner_user_id,
        "winner_name": winner_name,
        "payout_account_label": payout_account_label,
        "data": {},
    }


def _interaction_module_prize(rule: dict[str, Any], data: dict[str, Any]) -> int | None:
    module_prize = _int_or_none(rule.get("module_prize"))
    if module_prize is not None and module_prize > 0:
        return module_prize

    module_config = rule.get("module_config")
    if isinstance(module_config, dict):
        for key in (*_MODULE_PAYMENT_AMOUNT_KEYS, "prize"):
            parsed = _int_or_none(module_config.get(key))
            if parsed is not None and parsed > 0:
                return parsed

    rule_amount = _int_or_none(rule.get("amount"))
    if rule_amount is not None and rule_amount > 0:
        return rule_amount

    for key in (*_MODULE_PAYMENT_AMOUNT_KEYS, "prize"):
        parsed = _int_or_none(data.get(key))
        if parsed is not None and parsed > 0:
            return parsed

    math_prize = _int_or_none(rule.get("math_prize"))
    if math_prize is not None and math_prize > 0 and math_prize != 123:
        return math_prize
    return None


def _interaction_module_payload(
    incoming: Incoming,
    rule: dict[str, Any],
    parsed: dict[str, Any] | None,
    *,
    event_type: str = "payment_confirmed",
) -> dict[str, Any]:
    data = dict(rule.get("module_config") or {}) if isinstance(rule.get("module_config"), dict) else {}
    data.update(dict(parsed or {}))
    prize = _interaction_module_prize(rule, data)
    data["event_type"] = event_type
    resolved_payer_user_id = _interaction_payment_payer_user_id(incoming, data)
    payer_user_id = resolved_payer_user_id if event_type == "payment_confirmed" else resolved_payer_user_id or incoming.user_id
    payer_name = _interaction_payment_payer_name(incoming, data) or (incoming.display_name or "" if event_type != "payment_confirmed" else "")
    event = _interaction_event_payload(incoming, rule, event_type, parsed)
    source = _interaction_source_envelope(incoming, event_type)
    message = _interaction_message_envelope(incoming)
    chat = _interaction_chat_envelope(incoming)
    actor = _interaction_actor_envelope(incoming, data)
    source_actor = _interaction_source_actor_envelope(incoming, data)
    player = _interaction_player_envelope(incoming, data, event_type=event_type)
    payment = _interaction_payment_envelope(
        incoming,
        data,
        payer_user_id=_int_or_none(player.get("user_id")),
        payer_name=str(player.get("display_name") or payer_name or "").strip() or None,
    ) if event_type == "payment_confirmed" else None
    reply_to = _interaction_reply_to_envelope(incoming)
    trigger = _interaction_trigger_envelope(rule, event_type, parsed)
    session = _interaction_session_envelope(incoming, rule, data)
    raw = _interaction_raw_envelope(incoming, rule, parsed, event_type)
    module_key = str(rule.get("module_key") or "").strip()
    native_raw_allowed = account_bot_service.plugin_declares_telegram_native_raw(module_key, source="interaction_bot")
    data.update(
        {
            "trace_id": incoming.trace_id,
            "event": event,
            "source": source,
            "message": message,
            "chat": chat,
            "sender": source_actor,
            "actor": actor,
            "source_actor": source_actor,
            "player": player,
            "payment": payment,
            "reply_to": reply_to,
            "trigger": trigger,
            "session": session,
            "raw": raw,
            "native_raw_meta": _native_raw_meta(incoming, object_name="update", enabled=native_raw_allowed),
            "native_raw": incoming.native_raw if native_raw_allowed else None,
            "inline_query": None,
            "chosen_inline_result": None,
            "event_type": event_type,
            "account_id": incoming.account_id,
            "chat_id": incoming.chat_id,
            "rule_id": str(rule.get("id") or ""),
            "rule_name": str(rule.get("name") or ""),
            "entry_key": str(rule.get("module_action") or ""),
            "module_config": dict(rule.get("module_config") or {}) if isinstance(rule.get("module_config"), dict) else {},
            "valid_seconds": _interaction_session_ttl(rule),
            "payer_user_id": payer_user_id,
            "payer_name": payer_name,
            "source_update_id": incoming.update_id,
            "source_message_id": incoming.message_id,
            "message_text": incoming.text,
            "callback_query_id": incoming.callback_id,
            "callback_data": incoming.callback_data,
            "sender_user_id": incoming.user_id,
            "sender_name": incoming.display_name,
            "sender_username": incoming.username,
            "sender_chat_id": incoming.sender_chat_id,
            "sender_chat_type": incoming.sender_chat_type,
            "sender_chat_title": incoming.sender_chat_title,
            "sender_chat_username": incoming.sender_chat_username,
            "message_id": incoming.message_id,
            "reply_to_text": incoming.reply_to_text,
            "reply_to_sender_chat_id": incoming.reply_to_sender_chat_id,
            "reply_to_sender_chat_type": incoming.reply_to_sender_chat_type,
            "reply_to_sender_chat_title": incoming.reply_to_sender_chat_title,
            "reply_to_sender_chat_username": incoming.reply_to_sender_chat_username,
            "entity_languages": list(incoming.entity_languages),
            "prize": prize,
        }
    )
    return data


async def _interaction_module_payload_async(
    incoming: Incoming,
    rule: dict[str, Any],
    parsed: dict[str, Any] | None,
    *,
    event_type: str = "payment_confirmed",
) -> dict[str, Any]:
    data = _interaction_module_payload(incoming, rule, parsed, event_type=event_type)
    payout_account_label = await _load_account_holder_label(incoming.account_id)
    owner_user_ids = await _load_account_owner_user_ids(incoming.account_id)
    payout_mode = await _resolve_payout_mode(incoming.account_id, incoming.chat_id)
    data["payout_account_label"] = payout_account_label
    data["owner_user_ids"] = owner_user_ids
    data["admin_user_ids"] = list(owner_user_ids)
    data["userbot_user_id"] = owner_user_ids[0] if owner_user_ids else None
    data["payout_mode"] = payout_mode
    settlement_prize = _int_or_none(data.get("prize")) or 0
    data["settlement"] = _interaction_settlement_envelope(
        data,
        prize=settlement_prize,
        payout_account_label=payout_account_label,
        payout_mode=payout_mode,
    )
    return data


async def _run_worker_interaction_entry(
    incoming: Incoming,
    *,
    plugin_key: str,
    entry_key: str,
    payload: dict[str, Any],
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    reply_channel = f"account_bot:interaction_entry:{incoming.account_id}:{secrets.token_hex(8)}"
    redis = get_redis()
    pubsub = redis.pubsub()
    trace_ctx = _interaction_trace_context(payload)
    trace_id = trace_ctx.get("trace_id") or incoming.trace_id
    started = time.time()
    await record_span(
        trace_ctx,
        "plugin_invoke",
        TRACE_STATUS_OK,
        component="worker",
        plugin_key=plugin_key,
        entry_key=entry_key,
    )
    try:
        await pubsub.subscribe(reply_channel)
        await redis.publish(
            cmd_channel(incoming.account_id),
            make_cmd(
                CMD_RUN_INTERACTION_ENTRY,
                plugin_key=plugin_key,
                entry_key=entry_key,
                payload=payload,
                reply_to=reply_channel,
            ),
        )
        deadline = time.time() + _INTERACTION_ENTRY_TIMEOUT_SECONDS
        while time.time() < deadline:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if not msg:
                continue
            response = IPCMessage.decode(msg["data"]).payload
            actions = response.get("actions") if isinstance(response.get("actions"), list) else []
            ok = bool(response.get("ok"))
            error = response.get("error")
            await record_span(
                trace_ctx,
                "plugin_return",
                TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED,
                component="worker",
                plugin_key=plugin_key,
                entry_key=entry_key,
                reason_code=None if ok else "plugin_load_failed",
                error=error,
                action_count=len(actions),
                duration_ms=int((time.time() - started) * 1000),
            )
            await update_plugin_runtime_status(
                account_id=incoming.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_OK if ok else TRACE_STATUS_FAILED,
                last_trace_id=str(trace_id or ""),
            )
            return ok, error, [item for item in actions if isinstance(item, dict)]
    except Exception as exc:  # noqa: BLE001
        log.warning("interaction module ipc failed aid=%s plugin=%s entry=%s error=%s", incoming.account_id, plugin_key, entry_key, exc)
        await record_span(
            trace_ctx,
            "plugin_return",
            TRACE_STATUS_FAILED,
            component="worker",
            plugin_key=plugin_key,
            entry_key=entry_key,
            reason_code="plugin_load_failed",
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )
        await update_plugin_runtime_status(
            account_id=incoming.account_id,
            plugin_key=plugin_key,
            last_invocation_status=TRACE_STATUS_FAILED,
            last_trace_id=str(trace_id or ""),
        )
        return False, f"{type(exc).__name__}: {exc}", []
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close is not None:
                ret = close()
                if hasattr(ret, "__await__"):
                    await ret
    await record_span(
        trace_ctx,
        "plugin_return",
        TRACE_STATUS_FAILED,
        component="worker",
        plugin_key=plugin_key,
        entry_key=entry_key,
        reason_code="plugin_load_failed",
        error="worker 调用超时",
        duration_ms=int((time.time() - started) * 1000),
    )
    await update_plugin_runtime_status(
        account_id=incoming.account_id,
        plugin_key=plugin_key,
        last_invocation_status=TRACE_STATUS_FAILED,
        last_trace_id=str(trace_id or ""),
    )
    return False, "worker 调用超时", []


async def _run_local_interaction_entry_fallback(
    incoming: Incoming,
    *,
    plugin_key: str,
    entry_key: str,
    payload: dict[str, Any],
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """Run lightweight builtin interaction entries that do not need userbot state."""

    if plugin_key != "math10":
        return False, None, []
    try:
        from app.worker.plugins.base import PluginContext
        from app.worker.plugins.loader import _load_installed_plugin

        loaded = _load_installed_plugin(plugin_key)
        plugin_cls = loaded.get(plugin_key)
        if plugin_cls is None:
            return False, "math10 插件库插件未安装，请先在插件安装页安装随机算数题。", []

        async def _log(level: str, message: str, **detail: Any) -> None:
            source = str(detail.pop("source", "plugin"))
            detail.pop("plugin_key", None)
            await _write_interaction_runtime_log(
                incoming,
                level,
                message,
                source=source,
                plugin_key=plugin_key,
                **detail,
            )

        plugin = plugin_cls()
        ctx = PluginContext(
            account_id=incoming.account_id,
            feature_key=plugin_key,
            redis=get_redis(),
            log=_log,
        )
        await plugin.on_startup(ctx)
        actions = await plugin.on_interaction(ctx, entry_key, dict(payload or {}))
        if actions is None:
            return False, f"模块尚未实现交互入口：{plugin_key}.{entry_key}", []
        if not isinstance(actions, list) or not all(isinstance(item, dict) for item in actions):
            return False, "交互入口必须返回 list[dict] 标准动作", []
        return True, None, [dict(item) for item in actions]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", []


def _should_try_local_interaction_fallback(plugin_key: str, error: str | None) -> bool:
    if plugin_key != "math10":
        return False
    text = str(error or "")
    return "模块未加载或未启用" in text


async def _run_worker_interaction_action(
    incoming: Incoming,
    *,
    payload: dict[str, Any],
) -> tuple[bool, str | None, dict[str, Any]]:
    reply_channel = f"account_bot:interaction_action:{incoming.account_id}:{secrets.token_hex(8)}"
    redis = get_redis()
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_channel)
        await redis.publish(
            cmd_channel(incoming.account_id),
            make_cmd(
                CMD_RUN_INTERACTION_ACTION,
                payload=payload,
                reply_to=reply_channel,
            ),
        )
        deadline = time.time() + _INTERACTION_ENTRY_TIMEOUT_SECONDS
        while time.time() < deadline:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if not msg:
                continue
            response = IPCMessage.decode(msg["data"]).payload
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            return bool(response.get("ok")), response.get("error"), result
    except Exception as exc:  # noqa: BLE001
        log.warning("interaction action ipc failed aid=%s error=%s", incoming.account_id, exc)
        return False, f"{type(exc).__name__}: {exc}", {}
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close is not None:
                ret = close()
                if hasattr(ret, "__await__"):
                    await ret
    return False, "worker 调用超时", {}


async def _write_interaction_runtime_log(
    incoming: Incoming,
    level: str,
    message: str,
    **detail: Any,
) -> None:
    try:
        if incoming.trace_id and not detail.get("trace_id"):
            detail["trace_id"] = incoming.trace_id
        if detail.get("guard_level"):
            await _remember_interaction_debug_warning(incoming, level, message, detail)
        payload = RuntimeLogPayload(
            account_id=incoming.account_id,
            level=level,  # type: ignore[arg-type]
            source="event",
            message=message,
            detail=detail or None,
        )
        await get_redis().rpush(RUNTIME_LOG_STREAM, payload.encode())
    except Exception:  # noqa: BLE001
        log.debug("write interaction runtime log failed aid=%s", incoming.account_id, exc_info=True)


def _interaction_log_context(incoming: Incoming) -> dict[str, Any]:
    context = {
        "chat_id": incoming.chat_id,
        "message_id": incoming.message_id,
        "reply_to_message_id": incoming.reply_to_message_id,
        "user_id": incoming.user_id,
        "username": incoming.username,
        "display_name": incoming.display_name,
    }
    if incoming.trace_id:
        context["trace_id"] = incoming.trace_id
    return context


def _incoming_event_type(incoming: Incoming) -> str:
    if incoming.inline_query_id:
        return "inline_query"
    if incoming.chosen_inline_result_id:
        return "chosen_inline_result"
    return "callback_query" if incoming.callback_id else "message"


def _incoming_trace_payload(
    incoming: Incoming,
    *,
    event_type: str | None = None,
    channel: str = "interaction_bot",
) -> dict[str, Any]:
    kind = event_type or _incoming_event_type(incoming)
    if isinstance(incoming.native_raw, dict):
        payload = normalize_bot_update(incoming.account_id, incoming.native_raw, channel=channel)
        payload["trace_id"] = incoming.trace_id
        payload["event_type"] = kind
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        source.update(
            {
                "type": kind,
                "channel": channel,
                "account_id": incoming.account_id,
                "chat_id": incoming.chat_id,
                "message_id": incoming.message_id,
                "callback_query_id": incoming.callback_id,
                "callback_data": incoming.callback_data,
                "inline_query_id": incoming.inline_query_id,
            }
        )
        payload["source"] = source
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        message.update(
            {
                "chat_id": incoming.chat_id,
                "message_id": incoming.message_id,
                "text": incoming.text,
                "reply_to_message_id": incoming.reply_to_message_id,
            }
        )
        payload["message"] = message
        payload["chat"] = {"id": incoming.chat_id, "type": incoming.chat_type}
        payload["sender"] = {
            "user_id": incoming.user_id,
            "display_name": incoming.display_name,
            "username": incoming.username,
        }
        payload["actor"] = dict(payload["sender"])
        payload["source_actor"] = dict(payload["sender"])
        payload["player"] = dict(payload["sender"])
        payload["reply_to"] = {
            "user_id": incoming.reply_to_user_id,
            "message_id": incoming.reply_to_message_id,
            "display_name": incoming.reply_to_display_name,
            "username": incoming.reply_to_username,
            "text": incoming.reply_to_text,
        } if incoming.reply_to_message_id or incoming.reply_to_text else None
        payload["raw"] = {
            "update_id": incoming.update_id,
            "message_id": incoming.message_id,
            "callback_query_id": incoming.callback_id,
            "callback_data": incoming.callback_data,
            "inline_query_id": incoming.inline_query_id,
            "chosen_inline_result_id": incoming.chosen_inline_result_id,
            "text": incoming.text,
            "event_type": kind,
        }
        payload["native_raw_meta"] = _native_raw_meta(incoming, object_name="update", enabled=False, source=channel)
        payload["native_raw"] = None
        return payload
    return {
        "trace_id": incoming.trace_id,
        "source": {
            "type": kind,
            "channel": channel,
            "driver": "telegram_bot_api",
            "account_id": incoming.account_id,
            "chat_id": incoming.chat_id,
            "chat_type": incoming.chat_type,
            "update_id": incoming.update_id,
            "message_id": incoming.message_id,
            "callback_query_id": incoming.callback_id,
            "callback_data": incoming.callback_data,
            "inline_query_id": incoming.inline_query_id,
        },
        "message": {
            "chat_id": incoming.chat_id,
            "message_id": incoming.message_id,
            "text": incoming.text,
            "reply_to_message_id": incoming.reply_to_message_id,
        },
        "chat": {
            "id": incoming.chat_id,
            "type": incoming.chat_type,
        },
        "sender": {
            "user_id": incoming.user_id,
            "display_name": incoming.display_name,
            "username": incoming.username,
        },
        "inline_query": {
            "id": incoming.inline_query_id,
            "query": incoming.inline_query_text or incoming.text,
            "offset": incoming.inline_offset or "",
            "chat_type": incoming.inline_chat_type,
            "from": {
                "user_id": incoming.user_id,
                "display_name": incoming.display_name,
                "username": incoming.username,
            },
        } if incoming.inline_query_id else None,
        "chosen_inline_result": {
            "result_id": incoming.chosen_inline_result_id,
            "query": incoming.inline_query_text or incoming.text,
            "from": {
                "user_id": incoming.user_id,
                "display_name": incoming.display_name,
                "username": incoming.username,
            },
        } if incoming.chosen_inline_result_id else None,
        "reply_to": {
            "user_id": incoming.reply_to_user_id,
            "message_id": incoming.reply_to_message_id,
            "display_name": incoming.reply_to_display_name,
            "username": incoming.reply_to_username,
            "text": incoming.reply_to_text,
        } if incoming.reply_to_message_id or incoming.reply_to_text else None,
        "raw": {
            "update_id": incoming.update_id,
            "message_id": incoming.message_id,
            "callback_query_id": incoming.callback_id,
            "callback_data": incoming.callback_data,
            "inline_query_id": incoming.inline_query_id,
            "chosen_inline_result_id": incoming.chosen_inline_result_id,
            "text": incoming.text,
            "event_type": kind,
        },
        "native_raw_meta": _native_raw_meta(incoming, object_name="update", enabled=False, source=channel),
        "native_raw": None,
    }


def _interaction_trace_context(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    trace_id = str(data.get("trace_id") or "").strip() or None
    return {
        "trace_id": trace_id,
        "rule_id": str(trigger.get("rule_id") or "").strip() or None,
        "rule_name": str(trigger.get("rule_name") or "").strip() or None,
        "plugin_key": str(trigger.get("module_key") or "").strip() or None,
        "entry_key": str(trigger.get("entry_key") or "").strip() or None,
        "session_key": str(session.get("key") or "").strip() or None,
        "session_scope": str(session.get("scope") or "").strip() or None,
    }


def _native_raw_meta(
    incoming: Incoming,
    *,
    object_name: str,
    enabled: bool,
    source: str = "interaction_bot",
) -> dict[str, Any]:
    raw = incoming.native_raw if isinstance(incoming.native_raw, dict) else None
    try:
        size = len(json.dumps(raw or {}, ensure_ascii=False, default=str).encode("utf-8")) if raw is not None else 0
    except (TypeError, ValueError):
        size = 0
    if enabled and raw is None:
        reason_code = "native_raw_skipped"
    elif enabled:
        reason_code = None
    else:
        reason_code = "native_raw_not_allowed"
    if reason_code is not None and reason_code not in EVENT_REASON_CODES:
        reason_code = "native_raw_skipped"
    return {
        "enabled": bool(enabled and raw is not None),
        "source": source,
        "driver": "telegram_bot_api",
        "object": object_name,
        "stored_in_trace": False,
        "size_bytes": size,
        "reason_code": reason_code,
    }


def _json_safe(value: Any, *, max_text: int = 1200) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v, max_text=max_text) for k, v in list(value.items())[:80]}
    if isinstance(value, list):
        return [_json_safe(item, max_text=max_text) for item in value[:80]]
    if isinstance(value, (tuple, set)):
        return [_json_safe(item, max_text=max_text) for item in list(value)[:80]]
    if isinstance(value, str):
        return value[:max_text]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_text]


def _interaction_debug_key(account_id: int) -> str:
    return f"{_INTERACTION_DEBUG_STATE_PREFIX}{int(account_id)}"


def _interaction_debug_warnings_key(account_id: int) -> str:
    return f"{_INTERACTION_DEBUG_WARNINGS_PREFIX}{int(account_id)}"


async def _remember_interaction_debug_state(
    incoming: Incoming,
    *,
    stage: str,
    payload: dict[str, Any] | None = None,
    raw_actions: list[dict[str, Any]] | None = None,
    guarded_actions: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    snapshot = {
        "ts": time.time(),
        "stage": stage,
        "chat_id": incoming.chat_id,
        "message_id": incoming.message_id,
        "update_id": incoming.update_id,
        "payload": _json_safe(payload or {}),
        "actions": _json_safe(raw_actions or []),
        "guarded_actions": _json_safe(guarded_actions or []),
        "error": error,
    }
    try:
        await get_redis().set(
            _interaction_debug_key(incoming.account_id),
            json.dumps(snapshot, ensure_ascii=False),
            ex=_INTERACTION_DEBUG_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        log.debug("remember interaction debug state failed aid=%s", incoming.account_id, exc_info=True)


async def _remember_interaction_debug_warning(incoming: Incoming, level: str, message: str, detail: dict[str, Any]) -> None:
    item = {
        "ts": time.time(),
        "level": level,
        "message": message,
        "detail": _json_safe(detail),
    }
    try:
        redis = get_redis()
        key = _interaction_debug_warnings_key(incoming.account_id)
        await redis.lpush(key, json.dumps(item, ensure_ascii=False))
        await redis.ltrim(key, 0, _INTERACTION_DEBUG_WARNING_LIMIT - 1)
        await redis.expire(key, _INTERACTION_DEBUG_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        log.debug("remember interaction debug warning failed aid=%s", incoming.account_id, exc_info=True)


async def get_interaction_debug_snapshot(account_id: int) -> dict[str, Any]:
    """Return recent interaction payload/actions/warnings for the Web UI."""

    try:
        redis = get_redis()
        raw = await redis.get(_interaction_debug_key(account_id))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        snapshot = json.loads(raw) if raw else {}
        warning_rows = await redis.lrange(_interaction_debug_warnings_key(account_id), 0, _INTERACTION_DEBUG_WARNING_LIMIT - 1)
        warnings: list[dict[str, Any]] = []
        for row in warning_rows:
            if isinstance(row, bytes):
                row = row.decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(row)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                warnings.append(parsed)
        if warnings:
            snapshot["warnings"] = warnings
        return snapshot if isinstance(snapshot, dict) else {}
    except Exception:  # noqa: BLE001
        log.debug("read interaction debug snapshot failed aid=%s", account_id, exc_info=True)
        return {}


def _interaction_actions_request_no_session(actions: list[dict[str, Any]]) -> bool:
    return any(str(action.get("type") or "").strip() in _INTERACTION_SESSION_CONTROL_ACTIONS for action in actions)


def _interaction_actions_answer_callback(actions: list[dict[str, Any]]) -> bool:
    return any(str(action.get("type") or "").strip() == "answer_callback" for action in actions)


def _interaction_actions_mark_success(actions: list[dict[str, Any]]) -> bool:
    markers = [
        action.get("success")
        for action in actions
        if str(action.get("type") or "").strip() == "result"
    ]
    if markers:
        return any(bool(marker) for marker in markers)
    return True


async def _guard_interaction_actions(
    incoming: Incoming,
    rule: dict[str, Any],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    guard_events: list[tuple[str, str, dict[str, Any]]] = []

    async def _guard_log(level: str, message: str, **detail: Any) -> None:
        guard_events.append((level, message, dict(detail)))
        await _write_interaction_runtime_log(
            incoming,
            level,
            message,
            **detail,
        )

    await record_span(
        trace_log_context(incoming.trace_id),
        "contract_guard",
        TRACE_STATUS_OK,
        component="interaction_contract",
        action_count=len(actions),
        **_interaction_trace_context({"trace_id": incoming.trace_id, "trigger": {
            "rule_id": rule.get("id"),
            "rule_name": rule.get("name"),
            "module_key": rule.get("module_key"),
            "entry_key": rule.get("module_action"),
        }}),
    )
    guarded = await guard_interaction_actions(
        rule=rule,
        actions=actions,
        resolve_entry_manifest=account_bot_service.declared_module_entry_manifest,
        write_log=_guard_log,
        log_context=_interaction_log_context(incoming),
    )
    plugin_key = str(rule.get("module_key") or "").strip() or None
    entry_key = str(rule.get("module_action") or "").strip() or None
    for _level, message, detail in guard_events:
        guard_level = str(detail.get("guard_level") or "").strip()
        status = TRACE_STATUS_FAILED if guard_level == "failed" else TRACE_STATUS_WARNING
        reason_code = str(detail.get("reason_code") or "contract_warning").strip()
        await record_span(
            trace_log_context(incoming.trace_id),
            "contract_guard",
            status,
            component="interaction_contract",
            plugin_key=plugin_key,
            entry_key=entry_key,
            reason_code=reason_code,
            message=message,
            guard_level=guard_level,
            action_type=detail.get("action_type"),
            unsupported_send_via=detail.get("unsupported_send_via"),
            requested_send_via_raw=detail.get("requested_send_via_raw"),
            migration_hint=detail.get("migration_hint"),
        )
        if guard_level == "failed":
            await record_action(
                trace_log_context(incoming.trace_id, plugin_key=plugin_key, entry_key=entry_key),
                {
                    "type": detail.get("action_type") or "unknown",
                    "channel_selector": detail.get("requested_send_via_raw"),
                },
                TRACE_STATUS_FAILED,
                plugin_key=plugin_key,
                error_code=reason_code,
                error_message=message,
            )
    await record_span(
        trace_log_context(incoming.trace_id),
        "contract_guard",
        TRACE_STATUS_OK,
        component="interaction_contract",
        action_count=len(guarded),
        plugin_key=plugin_key,
        entry_key=entry_key,
    )
    return guarded


def _interaction_delivery_message_id(result: dict[str, Any] | Any) -> int | None:
    return delivery_message_id(result)


async def _apply_interaction_actions(
    incoming: Incoming,
    actions: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
    replace_message_id: int | None = None,
) -> None:
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=_write_interaction_runtime_log,
        run_worker_action=_run_worker_interaction_action,
        log_context=_interaction_log_context,
        trace_context=_interaction_trace_context,
        get_redis_client=get_redis,
    )
    await executor.apply(actions, context=context, replace_message_id=replace_message_id)


async def _run_interaction_module(
    incoming: Incoming,
    rule: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    event_type: str = "payment_confirmed",
) -> tuple[bool, bool]:
    module_key = str(rule.get("module_key") or "").strip()
    entry_key = str(rule.get("module_action") or "").strip()
    if not module_key or not entry_key:
        await _send(incoming, "模块启动失败：请先选择模块和交互入口。")
        return False, False
    start_text = str(rule.get("module_start_text") or "").strip()
    start_message_id: int | None = None
    if start_text:
        start_result = await _send(
            incoming,
            _render_rule_text_template(start_text, rule),
            reply_to_message_id=incoming.message_id,
        )
        start_message_id = _interaction_delivery_message_id(start_result)
    payload = await _interaction_module_payload_async(incoming, rule, parsed, event_type=event_type)
    await _remember_interaction_debug_state(incoming, stage="payload_built", payload=payload)
    trace_context = _interaction_trace_context(payload)
    native_raw_meta = payload.get("native_raw_meta") if isinstance(payload.get("native_raw_meta"), dict) else {}
    _event, decision = _legacy_rule_event_bus_decision(incoming, rule, event_type=event_type)
    if decision is None or not decision.matched:
        await record_span(
            trace_context,
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="interaction_rule",
            plugin_key=module_key,
            entry_key=entry_key,
            reason_code=getattr(decision, "reason_code", "subscription_not_matched"),
            message=getattr(decision, "reason_message", "旧交互规则未通过 Event Bus rule_bound decision。"),
            dispatch_mode="rule_bound",
            scope="rule_bound",
            filters={
                "rule_id": rule.get("id"),
                "event_type": event_type,
                "chat_id": incoming.chat_id,
            },
        )
        return False, False
    await record_span(
        trace_context,
        "subscription_match",
        TRACE_STATUS_OK,
        component="interaction_rule",
        plugin_key=decision.plugin_key,
        entry_key=decision.entry_key,
        reason_code=decision.reason_code,
        message=decision.reason_message,
        dispatch_mode=decision.dispatch_mode,
        scope=decision.scope,
        filters=decision.filters,
    )
    if incoming.native_raw is not None and not payload.get("native_raw"):
        await record_span(
            trace_context,
            "native_raw",
            TRACE_STATUS_SKIPPED,
            component="interaction_payload",
            plugin_key=module_key,
            entry_key=entry_key,
            reason_code=str(native_raw_meta.get("reason_code") or "native_raw_not_allowed"),
            message="插件未声明 telegram_native_raw，平台只下发 native_raw_meta。",
        )
    ok, error, actions = await _run_worker_interaction_entry(
        incoming,
        plugin_key=module_key,
        entry_key=entry_key,
        payload=payload,
    )
    if not ok and _should_try_local_interaction_fallback(module_key, error):
        ok, error, actions = await _run_local_interaction_entry_fallback(
            incoming,
            plugin_key=module_key,
            entry_key=entry_key,
            payload=payload,
        )
    if not ok:
        await _remember_interaction_debug_state(
            incoming,
            stage="plugin_error",
            payload=payload,
            error=error,
        )
        await _send(
            incoming,
            f"模块启动失败：{account_bot_service.html_text(error or f'{module_key}.{entry_key} 不可用')}",
        )
        return False, False
    raw_actions = [dict(action) for action in actions]
    actions = await _guard_interaction_actions(incoming, rule, actions)
    await _remember_interaction_debug_state(
        incoming,
        stage="actions_guarded",
        payload=payload,
        raw_actions=raw_actions,
        guarded_actions=actions,
    )
    keep_session = not _interaction_actions_request_no_session(actions)
    await _apply_interaction_start_session_actions(incoming, rule, actions)
    await _apply_interaction_actions(
        incoming,
        actions,
        context=trace_context,
        replace_message_id=start_message_id,
    )
    return _interaction_actions_mark_success(actions), keep_session


async def _start_interaction_module(
    incoming: Incoming,
    rule: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    event_type: str = "payment_confirmed",
) -> bool:
    _ok, keep_session = await _run_interaction_module(
        incoming,
        rule,
        parsed=parsed,
        event_type=event_type,
    )
    return keep_session


def _is_private_chat(incoming: Incoming) -> bool:
    return incoming.chat_type == "private" or (
        incoming.chat_id is not None and incoming.user_id is not None and incoming.chat_id == incoming.user_id
    )


def _should_route_text_to_account_commands(incoming: Incoming) -> bool:
    if _is_private_chat(incoming):
        return True
    return incoming.text.startswith("/")


def _claim_transfer_command_message(incoming: Incoming, amount: int) -> bool:
    identity = incoming.message_id if incoming.message_id is not None else f"u:{incoming.user_id}:upd:{incoming.update_id}"
    raw = f"{incoming.account_id}:{incoming.chat_id}:{incoming.user_id}:{identity}:{amount}:{incoming.text.strip()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    key = f"{incoming.account_id}:{digest}"
    now = time.monotonic()
    if len(_TRANSFER_COMMAND_DEDUPE) > 2048:
        expired = [item for item, expires_at in _TRANSFER_COMMAND_DEDUPE.items() if expires_at <= now]
        for item in expired:
            _TRANSFER_COMMAND_DEDUPE.pop(item, None)
    expires_at = _TRANSFER_COMMAND_DEDUPE.get(key)
    if expires_at is not None and expires_at > now:
        return False
    _TRANSFER_COMMAND_DEDUPE[key] = now + _TRANSFER_COMMAND_DEDUPE_TTL_SECONDS
    return True


async def _try_handle_transfer_command(db: Any, incoming: Incoming) -> bool:
    if incoming.callback_id or incoming.chat_id is None or incoming.user_id is None:
        return False

    amount = _parse_transfer_command(incoming.text)
    if amount is None:
        return False

    log.info(
        "transfer command candidate aid=%s chat_id=%s sender_id=%s amount=%s reply_to=%s",
        incoming.account_id,
        incoming.chat_id,
        incoming.user_id,
        amount,
        incoming.reply_to_display_name,
    )

    cfg = await account_bot_service.get_transfer_notice_config(db, incoming.account_id)
    if not cfg.get("enabled"):
        log.info("transfer command skipped: disabled aid=%s", incoming.account_id)
        return False
    if _is_configured_bot_user_id(cfg, incoming.user_id):
        log.info(
            "transfer command skipped: bot sender aid=%s incoming_user=%s reply_to=%s",
            incoming.account_id,
            incoming.user_id,
            incoming.reply_to_user_id,
        )
        return False
    if not _transfer_command_chat_is_monitored(incoming, cfg):
        log.info(
            "transfer command skipped: chat not monitored aid=%s incoming_chat=%s amount=%s",
            incoming.account_id,
            incoming.chat_id,
            amount,
        )
        return False
    receiver_info = await _select_transfer_command_receiver(db, incoming, cfg, amount)
    if receiver_info is None:
        log.info(
            "transfer command skipped: receiver unknown aid=%s incoming_chat=%s amount=%s",
            incoming.account_id,
            incoming.chat_id,
            amount,
        )
        return False

    transfer_token = await account_bot_service.get_transfer_bot_token(db, incoming.account_id)
    if not transfer_token:
        log.info("transfer command skipped: missing transfer bot token aid=%s", incoming.account_id)
        return False
    if not _claim_transfer_command_message(incoming, amount):
        log.info(
            "transfer command skipped: duplicate aid=%s incoming_chat=%s message_id=%s amount=%s",
            incoming.account_id,
            incoming.chat_id,
            incoming.message_id,
            amount,
        )
        return True

    payer = incoming.display_name or str(incoming.user_id)
    receiver = str(receiver_info["receiver_name"])
    raw_notice_template = str(cfg.get("transfer_notice_template") or DEFAULT_TRANSFER_NOTICE_TEMPLATE)
    notice, render_error = _render_transfer_bot_notice_with_error(
        raw_notice_template,
        payer,
        receiver,
        amount,
        payer_user_id=incoming.user_id,
        receiver_user_id=_int_or_none(receiver_info.get("receiver_user_id")),
    )
    if render_error is not None:
        error_text = f"{type(render_error).__name__}: {render_error}"
        log.warning(
            "transfer notice template render failed aid=%s chat_id=%s error=%s template=%r",
            incoming.account_id,
            incoming.chat_id,
            error_text,
            raw_notice_template[:500],
        )
        await _write_interaction_runtime_log(
            incoming,
            LEVEL_WARN,
            "转账通知模板渲染失败，已回退默认模板",
            error=error_text,
            template=raw_notice_template[:1000],
        )
    action = {
        "type": "send_message",
        "send_via": "transfer_test_notice",
        "chat_id": incoming.chat_id,
        "reply_to_message_id": incoming.message_id,
        "text": notice,
    }
    try:
        result = await account_bot_service.send_message(
            transfer_token,
            incoming.chat_id,
            notice,
            reply_to_message_id=incoming.message_id,
        )
        await record_action(
            trace_log_context(incoming.trace_id),
            action,
            TRACE_STATUS_OK,
            actual_send_via="transfer_test_notice",
            result=result,
            transfer_test_notice=True,
        )
    except Exception as exc:
        await record_action(
            trace_log_context(incoming.trace_id),
            action,
            TRACE_STATUS_FAILED,
            actual_send_via="transfer_test_notice",
            error_code="telegram_api_error",
            error=f"{type(exc).__name__}: {exc}",
            transfer_test_notice=True,
        )
        raise
    log.info(
        "transfer command emitted notice aid=%s chat_id=%s payer=%r receiver=%r amount=%s",
        incoming.account_id,
        incoming.chat_id,
        payer,
        receiver,
        amount,
    )
    from_user = result.get("from") if isinstance(result, dict) else None
    sender_id = (
        int(from_user["id"])
        if isinstance(from_user, dict) and from_user.get("id") is not None
        else None
    )
    if sender_id is not None:
        cfg_bot_id = cfg.get("trusted_bot_id")
        if cfg_bot_id is None:
            log.info("transfer bot user id detected aid=%s bot_id=%s", incoming.account_id, sender_id)
        elif int(cfg_bot_id) != int(sender_id):
            log.info(
                "transfer bot sent notice but trusted_bot_id differs aid=%s sent_by=%s expected=%s",
                incoming.account_id,
                sender_id,
                cfg_bot_id,
            )
        await _remember_transfer_bot_id(db, incoming.account_id, sender_id)

    return True


async def _remember_transfer_bot_id(db: Any, account_id: int, bot_id: int) -> None:
    try:
        row = await db.get(SystemSetting, account_bot_service.transfer_notice_setting_key(account_id))
        if row is None or not isinstance(row.value, dict):
            return
        current = dict(row.value)
        if _int_or_none(current.get("transfer_bot_id")) == int(bot_id):
            return
        current["transfer_bot_id"] = int(bot_id)
        row.value = current
        await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("remember transfer bot id failed aid=%s bot_id=%s", account_id, bot_id, exc_info=True)


async def _try_handle_transfer_notice(
    db: Any,
    incoming: Incoming,
    *,
    event_bus_enabled: bool = True,
) -> bool:
    if incoming.callback_id or incoming.chat_id is None or incoming.user_id is None:
        return False

    cfg = await account_bot_service.get_transfer_notice_config(db, incoming.account_id)
    if not cfg.get("enabled"):
        return False
    if not _trusted_transfer_notice_sender_matches(cfg, incoming.user_id):
        if _incoming_matches_interaction_trigger(cfg, incoming):
            log.info(
                "transfer notice skipped: sender mismatch aid=%s incoming_user=%s trusted_bot_id=%s transfer_bot_id=%s",
                incoming.account_id,
                incoming.user_id,
                cfg.get("trusted_bot_id"),
                cfg.get("transfer_bot_id"),
            )
        return False

    parsed = _parse_incoming_transfer_notice(incoming)
    if parsed is None:
        log.info(
            "transfer notice skipped: parse failed aid=%s chat_id=%s sender_id=%s",
            incoming.account_id,
            incoming.chat_id,
            incoming.user_id,
        )
        return False
    rule = await _select_transfer_notice_rule(db, incoming, cfg, parsed)
    if rule is None:
        log.info(
            "transfer notice skipped: no matching rule aid=%s parsed_receiver=%r parsed_amount=%s",
            incoming.account_id,
            parsed.get("receiver_name"),
            parsed.get("amount"),
        )
        return True
    if event_bus_enabled:
        event_bus_handled, event_bus_ok = await _try_handle_event_bus_payment_notice(db, incoming, cfg, parsed, rule)
    else:
        event_bus_handled, event_bus_ok = False, True
        await record_span(
            trace_log_context(incoming.trace_id),
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus_payment_notice",
            reason_code="event_bus_delivery_disabled",
            message="Event Bus 新投递路径已通过运行设置关闭，付款通知回退旧规则链路。",
        )
    if event_bus_handled:
        await _audit_transfer_notice(db, incoming, parsed)
        if not event_bus_ok:
            await record_span(
                trace_log_context(incoming.trace_id),
                "route",
                TRACE_STATUS_FAILED,
                component="event_bus_payment_notice",
                reason_code="plugin_runtime_error",
                message="外部转账通知已进入 Event Bus，但插件执行失败。",
            )
        return True
    if not await _claim_interaction_trigger(incoming, rule, "transfer_notice", parsed):
        log.info("transfer notice skipped: duplicate aid=%s rule=%s", incoming.account_id, rule.get("id"))
        return True
    if _interaction_payment_needs_player_confirm(incoming, rule, parsed):
        await _request_interaction_payment_player_confirm(incoming, rule, parsed)
        await _audit_transfer_notice(db, incoming, parsed)
        log.info(
            "transfer notice waiting for payer confirm aid=%s chat_id=%s sender_id=%s amount=%s",
            incoming.account_id,
            incoming.chat_id,
            incoming.user_id,
            parsed.get("amount"),
        )
        return True
    usage_block = await _interaction_user_usage_block_message(incoming, rule, parsed)
    if usage_block:
        await _send(incoming, usage_block, reply_to_message_id=incoming.message_id)
        return True
    claimed_usage, usage_pending_key = await _claim_interaction_user_usage(incoming, rule, parsed)
    if not claimed_usage:
        usage_block = await _interaction_user_usage_block_message(incoming, rule, parsed)
        await _send(
            incoming,
            usage_block or "该用户正在处理该功能，请稍后再试。",
            reply_to_message_id=incoming.message_id,
        )
        return True

    executed = False
    try:
        executed = await _execute_interaction_rule(incoming, rule, parsed)
        if executed:
            await _mark_interaction_user_usage(incoming, rule, parsed)
    finally:
        await _release_interaction_user_usage_claim(usage_pending_key)
    await _audit_transfer_notice(db, incoming, parsed)
    log.info(
        "transfer notice matched aid=%s chat_id=%s sender_id=%s amount=%s",
        incoming.account_id,
        incoming.chat_id,
        incoming.user_id,
        parsed.get("amount"),
    )
    return True


async def _audit_transfer_notice(db: Any, incoming: Incoming, parsed: dict[str, Any]) -> None:
    try:
        await audit.write(
            db,
            None,
            "account_bot.transfer_notice_matched",
            target=f"account:{incoming.account_id}/chat:{incoming.chat_id}",
            detail={
                "trusted_bot_id": incoming.user_id,
                "message_id": incoming.message_id,
                "payer_name": parsed.get("payer_name"),
                "receiver_name": parsed.get("receiver_name"),
                "amount": parsed.get("amount"),
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.debug("account bot transfer notice audit failed aid=%s", incoming.account_id, exc_info=True)


def _extract_incoming(aid: int, token: str, update: dict[str, Any]) -> Incoming | None:
    if "inline_query" in update:
        iq = update["inline_query"] or {}
        from_user = iq.get("from") or {}
        query = str(iq.get("query") or "")
        return Incoming(
            account_id=aid,
            token=token,
            update_id=int(update.get("update_id", 0)),
            user_id=_int_or_none(from_user.get("id")),
            chat_id=None,
            chat_type=str(iq.get("chat_type") or "") or None,
            message_id=None,
            text=query,
            inline_query_id=str(iq.get("id") or ""),
            inline_query_text=query,
            inline_offset=str(iq.get("offset") or ""),
            inline_chat_type=str(iq.get("chat_type") or "") or None,
            display_name=_format_user_name(from_user),
            username=str(from_user.get("username") or "").strip() or None,
            native_raw=dict(update),
        )
    if "chosen_inline_result" in update:
        chosen = update["chosen_inline_result"] or {}
        from_user = chosen.get("from") or {}
        query = str(chosen.get("query") or "")
        return Incoming(
            account_id=aid,
            token=token,
            update_id=int(update.get("update_id", 0)),
            user_id=_int_or_none(from_user.get("id")),
            chat_id=None,
            chat_type=None,
            message_id=None,
            text=query,
            inline_query_text=query,
            chosen_inline_result_id=str(chosen.get("result_id") or ""),
            display_name=_format_user_name(from_user),
            username=str(from_user.get("username") or "").strip() or None,
            native_raw=dict(update),
        )
    if "callback_query" in update:
        cq = update["callback_query"] or {}
        msg = cq.get("message") or {}
        from_user = cq.get("from") or {}
        chat = msg.get("chat") or {}
        return Incoming(
            account_id=aid,
            token=token,
            update_id=int(update.get("update_id", 0)),
            user_id=_int_or_none(from_user.get("id")),
            chat_id=_int_or_none(chat.get("id")),
            chat_type=str(chat.get("type") or "") or None,
            message_id=_int_or_none(msg.get("message_id")),
            text=str(msg.get("text") or msg.get("caption") or "").strip(),
            callback_id=str(cq.get("id") or ""),
            callback_data=str(cq.get("data") or ""),
            display_name=_format_user_name(from_user),
            username=str(from_user.get("username") or "").strip() or None,
            native_raw=dict(update),
        )
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    sender = _message_sender_fields(msg)
    chat = msg.get("chat") or {}
    reply = msg.get("reply_to_message") if isinstance(msg.get("reply_to_message"), dict) else {}
    reply_sender = _message_sender_fields(reply) if reply else {}
    reply_text = str(reply.get("text") or reply.get("caption") or "").strip()
    return Incoming(
        account_id=aid,
        token=token,
        update_id=int(update.get("update_id", 0)),
        user_id=_int_or_none(sender.get("user_id")),
        chat_id=_int_or_none(chat.get("id")),
        chat_type=str(chat.get("type") or "") or None,
        message_id=_int_or_none(msg.get("message_id")),
        text=str(msg.get("text") or msg.get("caption") or "").strip(),
        display_name=str(sender.get("display_name") or "").strip() or None,
        username=str(sender.get("username") or "").strip() or None,
        sender_chat_id=_int_or_none(sender.get("sender_chat_id")),
        sender_chat_type=str(sender.get("sender_chat_type") or "").strip() or None,
        sender_chat_title=str(sender.get("sender_chat_title") or "").strip() or None,
        sender_chat_username=str(sender.get("sender_chat_username") or "").strip() or None,
        reply_to_user_id=_int_or_none(reply_sender.get("user_id")),
        reply_to_message_id=_int_or_none(reply.get("message_id")),
        reply_to_display_name=str(reply_sender.get("display_name") or "").strip() or None,
        reply_to_username=str(reply_sender.get("username") or "").strip() or None,
        reply_to_sender_chat_id=_int_or_none(reply_sender.get("sender_chat_id")),
        reply_to_sender_chat_type=str(reply_sender.get("sender_chat_type") or "").strip() or None,
        reply_to_sender_chat_title=str(reply_sender.get("sender_chat_title") or "").strip() or None,
        reply_to_sender_chat_username=str(reply_sender.get("sender_chat_username") or "").strip() or None,
        reply_to_text=reply_text or None,
        entity_languages=_entity_languages(
            msg.get("entities"),
            msg.get("caption_entities"),
            reply.get("entities"),
            reply.get("caption_entities"),
        ),
        native_raw=dict(update),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_user_name(raw: dict[str, Any]) -> str | None:
    first = str(raw.get("first_name") or "").strip()
    last = str(raw.get("last_name") or "").strip()
    name = " ".join(x for x in [first, last] if x)
    return name or None


def _format_chat_name(raw: dict[str, Any]) -> str | None:
    title = str(raw.get("title") or "").strip()
    username = str(raw.get("username") or "").strip().lstrip("@")
    if title:
        return title
    if username:
        return f"@{username}"
    chat_id = _int_or_none(raw.get("id"))
    return str(chat_id) if chat_id is not None else None


def _sender_chat_ref(raw: Any) -> dict[str, Any] | None:
    data = raw if isinstance(raw, dict) else {}
    chat_id = _int_or_none(data.get("id"))
    title = _format_chat_name(data)
    username = str(data.get("username") or "").strip() or None
    chat_type = str(data.get("type") or "").strip() or None
    if chat_id is None and not title and not username:
        return None
    return {
        "id": chat_id,
        "type": chat_type,
        "title": title,
        "username": username,
    }


def _sender_chat_is_message_author(msg: dict[str, Any]) -> bool:
    sender_chat = msg.get("sender_chat") if isinstance(msg.get("sender_chat"), dict) else {}
    if not sender_chat:
        return False
    from_user = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    if not from_user:
        return True
    # Telegram may expose anonymous group admins as GroupAnonymousBot; the
    # actual actor is intentionally hidden, so preserve only sender_chat.
    return bool(from_user.get("is_bot")) and (
        _int_or_none(from_user.get("id")) == 1087968824
        or str(from_user.get("username") or "").casefold() == "groupanonymousbot"
        or str(from_user.get("first_name") or "").casefold() == "groupanonymousbot"
    )


def _message_sender_fields(msg: dict[str, Any]) -> dict[str, Any]:
    sender_chat = _sender_chat_ref(msg.get("sender_chat"))
    from_user = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    if sender_chat and _sender_chat_is_message_author(msg):
        return {
            "user_id": None,
            "display_name": sender_chat.get("title"),
            "username": sender_chat.get("username"),
            "sender_chat_id": sender_chat.get("id"),
            "sender_chat_type": sender_chat.get("type"),
            "sender_chat_title": sender_chat.get("title"),
            "sender_chat_username": sender_chat.get("username"),
        }
    return {
        "user_id": _int_or_none(from_user.get("id")),
        "display_name": _format_user_name(from_user),
        "username": str(from_user.get("username") or "").strip() or None,
        "sender_chat_id": sender_chat.get("id") if sender_chat else None,
        "sender_chat_type": sender_chat.get("type") if sender_chat else None,
        "sender_chat_title": sender_chat.get("title") if sender_chat else None,
        "sender_chat_username": sender_chat.get("username") if sender_chat else None,
    }


def _command_tail(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _handle_command(incoming: Incoming, role: str) -> None:
    command = (incoming.text.split(maxsplit=1)[0] or "/start").lower()
    if command.startswith("/start"):
        await _show_start(incoming, role)
    elif command.startswith("/help"):
        await _show_help(incoming, role)
    elif command.startswith("/status"):
        await _show_status(incoming)
    elif command.startswith("/features"):
        await _show_features(incoming, role)
    elif command.startswith("/commands"):
        await _show_commands(incoming, role)
    elif command.startswith("/plugins"):
        await _handle_plugins_command(incoming, role)
    elif command.startswith("/rules"):
        await _show_rules(incoming, role)
    elif command.startswith("/logs"):
        await _show_logs(incoming)
    elif command.startswith("/pause"):
        await _pause_account(incoming, role)
    elif command.startswith("/resume"):
        await _resume_account(incoming, role)
    elif command.startswith("/restart"):
        await _request_confirm(incoming, role, "restart", "重启账号 worker")
    else:
        await _send(incoming, "未知命令。发送 /help 查看可用操作。", reply_markup=_main_keyboard(incoming.account_id))


async def _handle_callback(incoming: Incoming, role: str) -> None:
    parsed = _parse_callback(incoming.callback_data or "")
    if parsed is None:
        await _answer_callback(incoming, text="按钮已过期")
        return
    aid, action, resource, nonce = parsed
    if aid != incoming.account_id:
        await _answer_callback(incoming, text="账号不匹配", show_alert=True)
        return
    try:
        if action == "view":
            await _answer_callback(incoming)
            if resource == "status":
                await _show_status(incoming, edit=True)
            elif resource == "features":
                await _show_features(incoming, role, edit=True)
            elif resource == "commands":
                await _show_commands(incoming, role, edit=True)
            elif resource == "plugins":
                await _show_plugins(incoming, role, edit=True)
            elif resource == "rules":
                await _show_rules(incoming, role, edit=True)
            elif resource == "logs":
                await _show_logs(incoming, edit=True)
            elif resource == "help":
                await _show_help(incoming, role, edit=True)
            else:
                await _show_start(incoming, role, edit=True)
        elif action == "feature_toggle":
            await _toggle_feature(incoming, role, resource)
        elif action == "command_toggle":
            await _toggle_command(incoming, role, resource)
        elif action == "rule_toggle":
            await _toggle_rule(incoming, role, resource)
        elif action == "rule_exec":
            await _execute_rule(incoming, role, resource)
        elif action == "pause":
            await _pause_account(incoming, role, edit=True)
        elif action == "resume":
            await _resume_account(incoming, role, edit=True)
        elif action == "confirm":
            await _confirm_action(incoming, role, resource, nonce)
        elif action == "cancel":
            await _answer_callback(incoming, text="已取消")
            await _show_start(incoming, role, edit=True)
        else:
            await _answer_callback(incoming, text="按钮已过期")
    except PermissionError as exc:
        await _answer_callback(incoming, text=str(exc), show_alert=True)
    except Exception as exc:  # noqa: BLE001
        clean = account_bot_service.sanitize_bot_error(exc, token=incoming.token)
        log.exception("account bot callback failed aid=%s action=%s", incoming.account_id, action)
        await _answer_callback(
            incoming,
            text=clean[:180],
            show_alert=True,
        )


async def _show_start(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    text = (
        "🤖 <b>账号 Bot 联动</b>\n"
        f"账号：<code>{incoming.account_id}</code>\n"
        f"你的角色：<code>{account_bot_service.html_text(role)}</code>\n\n"
        "这个 Bot 是当前 UserBot 账号的移动控制入口；复杂配置仍建议回到 GUI。"
    )
    await _send(incoming, text, reply_markup=_main_keyboard(incoming.account_id), edit=edit)


async def _show_help(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    text = (
        "📖 <b>可用命令</b>\n"
        "/status 查看账号、worker 与最近错误\n"
        "/features 查看并启停账号功能\n"
        "/commands 查看并启停自定义命令模板\n"
        "/plugins 查看插件入口（远程插件高风险能力默认关闭）\n"
        "/rules 查看规则，scheduler 规则可手动执行\n"
        "/logs 查看最近运行日志\n"
        "/pause /resume 暂停或恢复账号\n"
        "/restart 重启账号 worker（admin + 二次确认）\n\n"
        "<b>角色说明</b>\n"
        "viewer：只读查看；operator：可启停功能/命令/规则和暂停恢复；admin：可执行危险动作。"
    )
    await _send(incoming, text, reply_markup=_main_keyboard(incoming.account_id), edit=edit)


async def _show_status(incoming: Incoming, *, edit: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        acc = await db.get(Account, incoming.account_id)
        enabled_features = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == incoming.account_id,
                    AccountFeature.enabled.is_(True),
                )
            )
        ).scalars().all()
        last_log = (
            await db.execute(
                select(RuntimeLog)
                .where(RuntimeLog.account_id == incoming.account_id)
                .order_by(desc(RuntimeLog.ts))
                .limit(1)
            )
        ).scalar_one_or_none()
    if acc is None:
        text = "账号不存在。"
    else:
        name = acc.display_name or (f"@{acc.tg_username}" if acc.tg_username else f"#{acc.id}")
        text = (
            "📌 <b>账号状态</b>\n"
            f"账号：{account_bot_service.html_text(name)}\n"
            f"系统 ID：<code>{acc.id}</code>\n"
            f"Telegram ID：<code>{acc.tg_user_id or '未同步'}</code>\n"
            f"状态：<code>{account_bot_service.html_text(acc.status)}</code>\n"
            f"已启用功能：<code>{len(enabled_features)}</code>\n"
        )
        if last_log:
            text += (
                "\n<b>最近日志</b>\n"
                f"{account_bot_service.html_text(last_log.level)} · "
                f"{account_bot_service.html_text(last_log.source or 'worker')}\n"
                f"{account_bot_service.html_text(last_log.message)}"
            )
    await _send(incoming, text, reply_markup=_main_keyboard(incoming.account_id), edit=edit)


async def _show_features(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        features = await feature_service.list_features(db)
        afs = await feature_service.get_account_features(db, incoming.account_id)
    state = {af.feature_key: af for af in afs}
    lines = ["🧩 <b>账号功能</b>", "点击按钮可启停；复杂配置请用 GUI。", ""]
    rows: list[list[dict[str, str]]] = []
    for feature in features[:_MAX_BUTTON_ROWS]:
        af = state.get(feature.key)
        enabled = bool(af and af.enabled)
        lines.append(f"{'✅' if enabled else '⬜️'} {account_bot_service.html_text(feature.display_name)} <code>{feature.key}</code>")
        if account_bot_service.role_allows(role, ACCOUNT_BOT_ROLE_OPERATOR):
            rows.append([
                _button(
                    f"{'停用' if enabled else '启用'} {feature.display_name}"[:32],
                    "feature_toggle",
                    feature.key,
                    aid=incoming.account_id,
                )
            ])
    rows.append([_button("返回主菜单", "view", "main", aid=incoming.account_id)])
    await _send(incoming, "\n".join(lines), reply_markup=_keyboard(rows), edit=edit)


async def _show_plugins(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    policy = await _get_remote_plugin_policy(incoming.account_id)
    policy_summary = (
        f"总开关：{'开' if policy['enabled'] else '关'}，"
        f"install：{'开' if policy['install'] else '关'}，"
        f"update：{'开' if policy['update'] else '关'}，"
        f"uninstall：{'开' if policy['uninstall'] else '关'}，"
        f"第三方启停：{'开' if policy['enable_disable'] else '关'}"
    )
    async with AsyncSessionLocal() as db:
        features = await feature_service.list_features(db)
        afs = await feature_service.get_account_features(db, incoming.account_id)
        remotes = await remote_plugin_service.list_installed(db)
    state = {af.feature_key: af for af in afs}
    lines = [
        "🧱 <b>插件列表</b>",
        "这里按账号启停插件。远程安装/更新/卸载可用：",
        "<code>/plugins install &lt;git-url&gt;</code>",
        "<code>/plugins update &lt;name&gt;</code>",
        "<code>/plugins uninstall &lt;name&gt;</code>",
        f"远程高风险开关：{policy_summary}",
        "",
    ]
    rows: list[list[dict[str, str]]] = []
    for feature in features[:_MAX_BUTTON_ROWS]:
        af = state.get(feature.key)
        enabled = bool(af and af.enabled)
        source = "内置" if feature.is_builtin else "第三方"
        lines.append(
            f"{'✅' if enabled else '⬜️'} {account_bot_service.html_text(feature.display_name)}"
            f" · {source} · <code>{feature.key}</code>"
        )
        if account_bot_service.role_allows(role, ACCOUNT_BOT_ROLE_OPERATOR):
            rows.append([
                _button(
                    f"{'停用' if enabled else '启用'} {feature.display_name}"[:32],
                    "feature_toggle",
                    feature.key,
                    aid=incoming.account_id,
                )
            ])
    if remotes:
        lines.append("")
        lines.append("<b>远程插件</b>")
        for row in remotes[:12]:
            lines.append(
                f"{'✅' if row.enabled else '⬜️'} "
                f"{account_bot_service.html_text(row.display_name or row.name)}"
                f" · v{account_bot_service.html_text(row.version)} · <code>{row.name}</code>"
            )
    rows.append([_button("返回主菜单", "view", "main", aid=incoming.account_id)])
    await _send(incoming, "\n".join(lines), reply_markup=_keyboard(rows), edit=edit)


async def _handle_plugins_command(incoming: Incoming, role: str) -> None:
    args = _command_tail(incoming.text)
    if not args:
        await _show_plugins(incoming, role)
        return
    parts = args.split(maxsplit=1)
    sub = parts[0].lower()
    value = parts[1].strip() if len(parts) > 1 else ""
    if sub == "install" and value:
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "install")
        if not allowed:
            await _send(incoming, message, reply_markup=_main_keyboard(incoming.account_id))
            return
        await _request_confirm(
            incoming,
            role,
            "plugin_install",
            "安装远程插件",
            payload={"source_url": value},
        )
        return
    if sub == "update" and value:
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "update")
        if not allowed:
            await _send(incoming, message, reply_markup=_main_keyboard(incoming.account_id))
            return
        await _request_confirm(
            incoming,
            role,
            "plugin_update",
            f"更新远程插件 {value}",
            payload={"name": value},
        )
        return
    if sub in {"uninstall", "remove", "delete"} and value:
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "uninstall")
        if not allowed:
            await _send(incoming, message, reply_markup=_main_keyboard(incoming.account_id))
            return
        await _request_confirm(
            incoming,
            role,
            "plugin_uninstall",
            f"卸载远程插件 {value}",
            payload={"name": value},
        )
        return
    await _send(
        incoming,
        "插件命令格式：\n"
        "<code>/plugins</code>\n"
        "<code>/plugins install &lt;git-url&gt;</code>\n"
        "<code>/plugins update &lt;name&gt;</code>\n"
        "<code>/plugins uninstall &lt;name&gt;</code>",
        reply_markup=_main_keyboard(incoming.account_id),
    )


async def _show_commands(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        cmd_prefix = await _load_command_prefix(db)
        items = await command_service.list_for_account(db, incoming.account_id)
    lines = ["⌨️ <b>自定义命令模板</b>", "点击按钮可启停当前账号的模板。", ""]
    rows: list[list[dict[str, str]]] = []
    for item in items[:_MAX_BUTTON_ROWS]:
        tpl = item.template
        lines.append(
            f"{'✅' if item.enabled else '⬜️'} <code>{account_bot_service.html_text(cmd_prefix)}{account_bot_service.html_text(tpl.name)}</code>"
            f" · {account_bot_service.html_text(tpl.type)}"
        )
        if account_bot_service.role_allows(role, ACCOUNT_BOT_ROLE_OPERATOR):
            rows.append([
                _button(
                    f"{'停用' if item.enabled else '启用'} {cmd_prefix}{tpl.name}"[:32],
                    "command_toggle",
                    str(tpl.id),
                    aid=incoming.account_id,
                )
            ])
    rows.append([_button("返回主菜单", "view", "main", aid=incoming.account_id)])
    await _send(incoming, "\n".join(lines), reply_markup=_keyboard(rows), edit=edit)


async def _show_rules(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        rules = (
            await db.execute(
                select(Rule)
                .where(Rule.account_id == incoming.account_id)
                .order_by(Rule.feature_key.asc(), Rule.priority.desc(), Rule.id.asc())
                .limit(20)
            )
        ).scalars().all()
    lines = ["📋 <b>规则</b>", "展示最近 20 条规则；scheduler 规则可手动执行。", ""]
    rows: list[list[dict[str, str]]] = []
    for rule in rules:
        lines.append(
            f"{'✅' if rule.enabled else '⬜️'} #{rule.id} "
            f"{account_bot_service.html_text(rule.name)} · <code>{rule.feature_key}</code>"
        )
        if account_bot_service.role_allows(role, ACCOUNT_BOT_ROLE_OPERATOR):
            row = [
                _button(
                    f"{'停用' if rule.enabled else '启用'} #{rule.id}",
                    "rule_toggle",
                    str(rule.id),
                    aid=incoming.account_id,
                )
            ]
            if rule.feature_key == "scheduler":
                row.append(_button(f"执行 #{rule.id}", "rule_exec", str(rule.id), aid=incoming.account_id))
            rows.append(row)
    if not rules:
        lines.append("暂无规则。")
    rows.append([_button("返回主菜单", "view", "main", aid=incoming.account_id)])
    await _send(incoming, "\n".join(lines), reply_markup=_keyboard(rows), edit=edit)


async def _show_logs(incoming: Incoming, *, edit: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        logs = (
            await db.execute(
                select(RuntimeLog)
                .where(RuntimeLog.account_id == incoming.account_id)
                .order_by(desc(RuntimeLog.ts))
                .limit(8)
            )
        ).scalars().all()
    lines = ["🧾 <b>最近运行日志</b>"]
    for row in logs:
        lines.append(
            f"{account_bot_service.html_text(row.level)} · "
            f"{account_bot_service.html_text(row.source or 'worker')} · "
            f"{account_bot_service.html_text(row.message)}"
        )
    if not logs:
        lines.append("暂无日志。")
    await _send(
        incoming,
        "\n".join(lines),
        reply_markup=_main_keyboard(incoming.account_id),
        edit=edit,
    )


async def _toggle_feature(incoming: Incoming, role: str, key: str) -> None:
    _require(role, ACCOUNT_BOT_ROLE_OPERATOR)
    async with AsyncSessionLocal() as db:
        feature = await db.get(Feature, key)
        if feature is None:
            await _answer_callback(incoming, text="功能不存在", show_alert=True)
            return
        current = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == incoming.account_id,
                    AccountFeature.feature_key == key,
                )
            )
        ).scalar_one_or_none()
        enabled = not bool(current and current.enabled)
        if not feature.is_builtin:
            allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "enable_disable")
            if not allowed:
                await _answer_callback(
                    incoming,
                    text=message[:100],
                    show_alert=True,
                )
                return
            if incoming.callback_id:
                await _answer_callback(incoming, text="请确认")
            await _request_confirm(
                incoming,
                role,
                "plugin_toggle",
                f"{'启用' if enabled else '停用'}插件 {feature.display_name}",
                payload={"feature_key": key, "enabled": enabled},
            )
            return
        if enabled:
            remote = await remote_plugin_service.get_by_name(db, key)
            if remote is not None and not remote.enabled:
                await remote_plugin_service.enable(db, key)
        await feature_service.set_account_feature(db, incoming.account_id, key, enabled)
        await audit.write(
            db,
            None,
            "account_bot.feature_toggle",
            target=f"account:{incoming.account_id}/feature:{key}",
            detail=_audit_detail(incoming, role, {"enabled": enabled}),
        )
        await db.commit()
    await _answer_callback(incoming, text="已更新")
    await _show_features(incoming, role, edit=True)


async def _toggle_command(incoming: Incoming, role: str, resource: str) -> None:
    _require(role, ACCOUNT_BOT_ROLE_OPERATOR)
    try:
        tpl_id = int(resource)
    except ValueError:
        await _answer_callback(incoming, text="模板不存在", show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        link = (
            await db.execute(
                select(AccountCommandLink).where(
                    AccountCommandLink.account_id == incoming.account_id,
                    AccountCommandLink.template_id == tpl_id,
                )
            )
        ).scalar_one_or_none()
        if link and link.enabled:
            await command_service.disable_for_account(db, incoming.account_id, tpl_id)
            enabled = False
        else:
            await command_service.enable_for_account(db, incoming.account_id, tpl_id)
            enabled = True
        await audit.write(
            db,
            None,
            "account_bot.command_toggle",
            target=f"account:{incoming.account_id}/command_template:{tpl_id}",
            detail=_audit_detail(incoming, role, {"enabled": enabled}),
        )
        await db.commit()
        await command_service.notify_reload(incoming.account_id)
    await _answer_callback(incoming, text="已更新")
    await _show_commands(incoming, role, edit=True)


async def _toggle_rule(incoming: Incoming, role: str, resource: str) -> None:
    _require(role, ACCOUNT_BOT_ROLE_OPERATOR)
    try:
        rid = int(resource)
    except ValueError:
        await _answer_callback(incoming, text="规则不存在", show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        rule = await db.get(Rule, rid)
        if rule is None or rule.account_id != incoming.account_id:
            await _answer_callback(incoming, text="规则不存在", show_alert=True)
            return
        rule.enabled = not bool(rule.enabled)
        await audit.write(
            db,
            None,
            "account_bot.rule_toggle",
            target=f"account:{incoming.account_id}/rule:{rid}",
            detail=_audit_detail(incoming, role, {"enabled": rule.enabled}),
        )
        await db.commit()
        try:
            redis = get_redis()
            await publish_cmd_with_ack(redis, incoming.account_id, CMD_RELOAD_CONFIG)
        except Exception:
            log.debug("account bot rule reload failed aid=%s", incoming.account_id, exc_info=True)
    await _answer_callback(incoming, text="已更新")
    await _show_rules(incoming, role, edit=True)


async def _execute_rule(incoming: Incoming, role: str, resource: str) -> None:
    _require(role, ACCOUNT_BOT_ROLE_OPERATOR)
    try:
        rid = int(resource)
    except ValueError:
        await _answer_callback(incoming, text="规则不存在", show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        rule = await db.get(Rule, rid)
        if rule is None or rule.account_id != incoming.account_id or rule.feature_key != "scheduler":
            await _answer_callback(incoming, text="仅 scheduler 规则可执行", show_alert=True)
            return
        await audit.write(
            db,
            None,
            "account_bot.rule_execute",
            target=f"account:{incoming.account_id}/rule:{rid}",
            detail=_audit_detail(incoming, role),
        )
        await db.commit()
    redis = get_redis()
    reply_channel = f"worker_reply:{incoming.account_id}:exec_rule:{secrets.token_hex(8)}"
    pubsub = redis.pubsub()
    ok = False
    error = None
    try:
        await pubsub.subscribe(reply_channel)
        await redis.publish(cmd_channel(incoming.account_id), make_cmd(CMD_EXECUTE_RULE, rule_id=rid, reply_to=reply_channel))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            msg = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                timeout=remaining + 0.1,
            )
            if not msg or msg.get("type") != "message":
                continue
            payload = IPCMessage.decode(msg["data"]).payload
            ok = bool(payload.get("ok"))
            error = payload.get("error")
            break
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            ret = close() if close else None
            if hasattr(ret, "__await__"):
                await ret
        except Exception:
            pass
    await _answer_callback(
        incoming,
        text="已执行" if ok else (str(error or "worker 响应超时")[:100]),
        show_alert=not ok,
    )


async def _pause_account(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    _require(role, ACCOUNT_BOT_ROLE_OPERATOR)
    async with AsyncSessionLocal() as db:
        await account_service.pause(db, incoming.account_id)
        await audit.write(
            db,
            None,
            "account_bot.account_pause",
            target=f"account:{incoming.account_id}",
            detail=_audit_detail(incoming, role),
        )
        await db.commit()
    if incoming.callback_id:
        await _answer_callback(incoming, text="已暂停")
    await _send(incoming, "账号已暂停。", reply_markup=_main_keyboard(incoming.account_id), edit=edit)


async def _resume_account(incoming: Incoming, role: str, *, edit: bool = False) -> None:
    _require(role, ACCOUNT_BOT_ROLE_OPERATOR)
    async with AsyncSessionLocal() as db:
        await account_service.resume(db, incoming.account_id)
        await audit.write(
            db,
            None,
            "account_bot.account_resume",
            target=f"account:{incoming.account_id}",
            detail=_audit_detail(incoming, role),
        )
        await db.commit()
    if incoming.callback_id:
        await _answer_callback(incoming, text="已恢复")
    await _send(incoming, "账号已恢复。", reply_markup=_main_keyboard(incoming.account_id), edit=edit)


async def _request_confirm(
    incoming: Incoming,
    role: str,
    action: str,
    label: str,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    _require(role, ACCOUNT_BOT_ROLE_ADMIN)
    nonce = secrets.token_urlsafe(8)
    redis = get_redis()
    confirm_payload = {
        "account_id": incoming.account_id,
        "tg_user_id": incoming.user_id,
        "action": action,
        "label": label,
        "payload": payload or {},
    }
    await redis.setex(_confirm_redis_key(nonce), _CONFIRM_TTL_SECONDS, json.dumps(confirm_payload, ensure_ascii=False))
    await _audit_confirm_event(
        incoming,
        role,
        "account_bot.confirm_requested",
        action=action,
        extra={"label": label},
    )
    rows = [
        [
            _button("确认执行", "confirm", action, aid=incoming.account_id, nonce=nonce),
            _button("取消", "cancel", action, aid=incoming.account_id, nonce=nonce),
        ]
    ]
    await _send(
        incoming,
        f"⚠️ <b>二次确认</b>\n操作：{account_bot_service.html_text(label)}\n确认票据 5 分钟内有效。",
        reply_markup=_keyboard(rows),
        edit=bool(incoming.callback_id),
    )


async def _confirm_action(
    incoming: Incoming,
    role: str,
    resource: str,
    nonce: str | None,
) -> None:
    _require(role, ACCOUNT_BOT_ROLE_ADMIN)
    if not nonce:
        await _answer_callback(incoming, text="确认已过期", show_alert=True)
        return
    redis = get_redis()
    raw = await _read_confirm_payload(redis, nonce)
    if not raw:
        await _audit_confirm_event(
            incoming,
            role,
            "account_bot.confirm_expired",
            action=resource,
            extra={"reason": "missing_or_expired"},
        )
        await _answer_callback(incoming, text="确认已过期", show_alert=True)
        return
    data = json.loads(raw)
    if data.get("account_id") != incoming.account_id or data.get("tg_user_id") != incoming.user_id:
        await _audit_confirm_event(
            incoming,
            role,
            "account_bot.confirm_rejected",
            action=resource,
            extra={"reason": "owner_mismatch"},
        )
        await _answer_callback(incoming, text="只能由原用户确认", show_alert=True)
        return
    if data.get("action") != resource:
        await _audit_confirm_event(
            incoming,
            role,
            "account_bot.confirm_rejected",
            action=resource,
            extra={"reason": "action_mismatch"},
        )
        await _answer_callback(incoming, text="确认资源不匹配", show_alert=True)
        return
    consumed = await _consume_confirm_payload(redis, nonce)
    if not consumed:
        await _audit_confirm_event(
            incoming,
            role,
            "account_bot.confirm_expired",
            action=resource,
            extra={"reason": "already_consumed"},
        )
        await _answer_callback(incoming, text="确认已过期", show_alert=True)
        return
    await _audit_confirm_event(
        incoming,
        role,
        "account_bot.confirm_consumed",
        action=resource,
    )
    await _execute_confirmed_action(incoming, role, json.loads(consumed))


async def _restart_account_worker(incoming: Incoming, role: str) -> None:
    async with AsyncSessionLocal() as db:
        await audit.write(
            db,
            None,
            "account_bot.account_restart",
            target=f"account:{incoming.account_id}",
            detail=_audit_detail(incoming, role),
        )
        await db.commit()
    redis = get_redis()
    await redis.publish(cmd_channel(incoming.account_id), make_cmd("stop"))
    await redis.publish(GLOBAL_CHANNEL, make_cmd("start_worker", account_id=incoming.account_id))
    if incoming.callback_id:
        await _answer_callback(incoming, text="已下发重启")
    await _send(incoming, "已下发账号 worker 重启。", reply_markup=_main_keyboard(incoming.account_id), edit=True)


async def _execute_confirmed_action(
    incoming: Incoming,
    role: str,
    data: dict[str, Any],
) -> None:
    action = str(data.get("action") or "")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    if action == "restart":
        await _restart_account_worker(incoming, role)
        return
    if action == "plugin_install":
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "install")
        if not allowed:
            await _answer_callback(incoming, text=message[:100], show_alert=True)
            return
        source_url = str(payload.get("source_url") or "").strip()
        if not source_url:
            await _answer_callback(incoming, text="缺少 Git URL", show_alert=True)
            return
        async with AsyncSessionLocal() as db:
            row = await remote_plugin_service.install(db, source_url, default_enabled=False)
            await audit.write(
                db,
                None,
                "account_bot.plugin_install",
                target=f"remote_plugin:{row.name}",
                detail=_audit_detail(incoming, role, {"name": row.name}),
            )
            await db.commit()
        await _answer_callback(incoming, text="插件已安装")
        await _show_plugins(incoming, role, edit=True)
        return
    if action == "plugin_update":
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "update")
        if not allowed:
            await _answer_callback(incoming, text=message[:100], show_alert=True)
            return
        name = str(payload.get("name") or "").strip()
        async with AsyncSessionLocal() as db:
            row = await remote_plugin_service.update(db, name)
            await audit.write(
                db,
                None,
                "account_bot.plugin_update",
                target=f"remote_plugin:{row.name}",
                detail=_audit_detail(incoming, role, {"name": row.name}),
            )
            await db.commit()
        await _answer_callback(incoming, text="插件已更新")
        await _show_plugins(incoming, role, edit=True)
        return
    if action == "plugin_uninstall":
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "uninstall")
        if not allowed:
            await _answer_callback(incoming, text=message[:100], show_alert=True)
            return
        name = str(payload.get("name") or "").strip()
        async with AsyncSessionLocal() as db:
            found = await remote_plugin_service.uninstall(db, name)
            if not found:
                await _answer_callback(incoming, text="插件不存在", show_alert=True)
                return
            await audit.write(
                db,
                None,
                "account_bot.plugin_uninstall",
                target=f"remote_plugin:{name}",
                detail=_audit_detail(incoming, role, {"name": name}),
            )
            await db.commit()
        await _answer_callback(incoming, text="插件已卸载")
        await _show_plugins(incoming, role, edit=True)
        return
    if action == "plugin_toggle":
        allowed, message = await _check_remote_plugin_permission(incoming.account_id, role, "enable_disable")
        if not allowed:
            await _answer_callback(incoming, text=message[:100], show_alert=True)
            return
        key = str(payload.get("feature_key") or "").strip()
        enabled = bool(payload.get("enabled"))
        if not key:
            await _answer_callback(incoming, text="缺少插件 key", show_alert=True)
            return
        async with AsyncSessionLocal() as db:
            feature = await db.get(Feature, key)
            if feature is None or feature.is_builtin:
                await _answer_callback(incoming, text="插件不存在", show_alert=True)
                return
            if enabled:
                remote = await remote_plugin_service.get_by_name(db, key)
                if remote is not None and not remote.enabled:
                    await remote_plugin_service.enable(db, key)
            await feature_service.set_account_feature(db, incoming.account_id, key, enabled)
            await audit.write(
                db,
                None,
                "account_bot.feature_toggle",
                target=f"account:{incoming.account_id}/feature:{key}",
                detail=_audit_detail(incoming, role, {"enabled": enabled}),
            )
            await db.commit()
        await _answer_callback(incoming, text="已更新")
        await _show_plugins(incoming, role, edit=True)
        return
    await _answer_callback(incoming, text="未知确认动作", show_alert=True)


async def _get_remote_plugin_policy(account_id: int) -> dict[str, bool]:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(AccountBot).where(AccountBot.account_id == account_id))
        ).scalar_one_or_none()
    return account_bot_service.normalize_remote_plugin_policy(
        row.remote_plugin_policy if row is not None else None
    )


async def _check_remote_plugin_permission(account_id: int, role: str, action: str) -> tuple[bool, str]:
    if not account_bot_service.role_allows(role, ACCOUNT_BOT_ROLE_ADMIN):
        return False, "仅 admin 可执行远程插件高风险操作。"
    policy = await _get_remote_plugin_policy(account_id)
    if not policy.get("enabled", False):
        return False, _REMOTE_POLICY_HINT
    key = action if action in {"install", "update", "uninstall", "enable_disable"} else ""
    if key and not policy.get(key, False):
        return False, f"远程插件 {key} 未开启。{_REMOTE_POLICY_HINT}"
    return True, ""


async def _send(
    incoming: Incoming,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    reply_to_message_id: int | None = None,
    edit: bool = False,
) -> dict[str, Any] | None:
    if incoming.chat_id is None:
        return None
    action = {
        "type": "edit_message" if edit and incoming.message_id is not None else "send_message",
        "send_via": "interaction_bot",
        "chat_id": incoming.chat_id,
        "message_id": incoming.message_id if edit and incoming.message_id is not None else None,
        "reply_to_message_id": reply_to_message_id,
        "text": text,
    }
    if edit and incoming.message_id is not None:
        try:
            result = await account_bot_service.edit_message(
                incoming.token,
                incoming.chat_id,
                incoming.message_id,
                text,
                reply_markup=reply_markup,
            )
            await record_action(
                trace_log_context(incoming.trace_id),
                action,
                TRACE_STATUS_OK,
                actual_send_via="interaction_bot",
                result=result,
            )
            return result
        except Exception:
            log.debug("edit account bot message failed, fallback send", exc_info=True)
            await record_action(
                trace_log_context(incoming.trace_id),
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="interaction_bot",
                error_code="telegram_api_error",
                error="edit account bot message failed, fallback send",
            )
    send_action = {
        "type": "send_message",
        "send_via": "interaction_bot",
        "chat_id": incoming.chat_id,
        "reply_to_message_id": reply_to_message_id,
        "text": text,
    }
    try:
        result = await account_bot_service.send_message(
            incoming.token,
            incoming.chat_id,
            text,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )
        await record_action(
            trace_log_context(incoming.trace_id),
            send_action,
            TRACE_STATUS_OK,
            actual_send_via="interaction_bot",
            result=result,
        )
        return result
    except Exception as exc:
        await record_action(
            trace_log_context(incoming.trace_id),
            send_action,
            TRACE_STATUS_FAILED,
            actual_send_via="interaction_bot",
            error_code="telegram_api_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


async def _answer_callback(
    incoming: Incoming,
    *,
    callback_id: str | None = None,
    text: str = "",
    show_alert: bool = False,
) -> None:
    query_id = str(callback_id or incoming.callback_id or "").strip()
    action = {
        "type": "answer_callback",
        "callback_query_id": query_id,
        "text": text,
        "show_alert": show_alert,
    }
    if not query_id:
        await record_action(
            trace_log_context(incoming.trace_id),
            action,
            TRACE_STATUS_FAILED,
            error_code="callback_query_id_missing",
            error="callback query id is missing",
        )
        return
    try:
        await account_bot_service.answer_callback(incoming.token, query_id, text=text, show_alert=show_alert)
        await record_action(
            trace_log_context(incoming.trace_id),
            action,
            TRACE_STATUS_OK,
            actual_send_via="interaction_bot",
        )
    except Exception as exc:
        await record_action(
            trace_log_context(incoming.trace_id),
            action,
            TRACE_STATUS_FAILED,
            actual_send_via="interaction_bot",
            error_code="telegram_api_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def _require(role: str, required: str) -> None:
    if not account_bot_service.role_allows(role, required):
        raise PermissionError("权限不足")


def _audit_detail(
    incoming: Incoming,
    role: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "source": "account_bot",
        "tg_user_id": incoming.user_id,
        "role": role,
        "account_id": incoming.account_id,
    }
    if extra:
        detail.update(extra)
    return detail


async def _audit_confirm_event(
    incoming: Incoming,
    role: str,
    event: str,
    *,
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    detail = _audit_detail(incoming, role, {"confirm_action": action})
    if extra:
        detail.update(extra)
    try:
        async with AsyncSessionLocal() as db:
            await audit.write(
                db,
                None,
                event,
                target=f"account:{incoming.account_id}",
                detail=detail,
            )
            await db.commit()
    except Exception:
        log.warning("account bot confirm audit failed aid=%s event=%s", incoming.account_id, event, exc_info=True)
