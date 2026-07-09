"""每账号 worker 子进程主入口。

设计要点：
- 子进程 entrypoint 是 ``worker_main(account_id)``；主进程 supervisor 用
  ``multiprocessing.Process(target=worker_main, args=(aid,))`` 拉起。
- worker 负责连 TG / 注册事件 / 监听 IPC / 把日志和限速事件写回 Redis stream。
- 所有 DB 写操作由主进程统一处理（消费 Redis stream）；worker 只读 DB（启动时拉一次配置）。
"""
from __future__ import annotations

import asyncio
import gc
import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

from sqlalchemy import select, update
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    SessionRevokedError,
    UserDeactivatedError,
)

from ..crypto import decrypt_str
from ..db.base import AsyncSessionLocal
from ..db.models.account import Account, Proxy, SudoUser
from ..db.models.command import AccountCommandLink, CommandAlias, CommandTemplate, LLMProvider
from ..db.models.feature import FEATURE_SCHEDULER, AccountFeature
from ..db.models.payout_compensation import (
    PAYOUT_COMPENSATION_STATUS_ABANDONED,
    PAYOUT_COMPENSATION_STATUS_PENDING,
    PAYOUT_COMPENSATION_STATUS_SENT,
    PayoutCompensation,
)
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..services import payout_compensation
from ..services.ai_feature import is_ai_enabled
from ..services.event_trace import (
    TRACE_STATUS_OK,
    record_action,
    refresh_trace_settings,
    stop_trace_writer,
)
from ..services.interaction.delivery import namespaced_action_save_message_id_key, save_action_reply_target
from ..services.payout_limit import PayoutLimitExceeded
from ..services.payout_limit import check_and_consume as _check_payout_limit
from ..settings import settings as app_settings
from .command import (
    CommandContext,
    make_command_handler,
    normalize_command_echo_guard_limit,
    normalize_command_whitelist,
    set_command_context,
)
from .ipc import (
    CMD_DISPATCH_SIMULATE,
    CMD_EXECUTE_RULE,
    CMD_FETCH_AVATAR,
    CMD_GET_RECENT_PEERS,
    CMD_PAUSE,
    CMD_PING,
    CMD_RELOAD_COMMANDS,
    CMD_RELOAD_CONFIG,
    CMD_RELOAD_IGNORED,
    CMD_RELOAD_PLUGIN,
    CMD_RESUME,
    CMD_RUN_INTERACTION_ACTION,
    CMD_RUN_INTERACTION_ENTRY,
    CMD_STOP,
    CMD_WEBHOOK_DELIVER,
    EVT_ACK,
    EVT_LOGIN_REQUIRED,
    EVT_PONG,
    EVT_STATUS,
    GCMD_KILL_SWITCH,
    GCMD_RELOAD_GLOBAL,
    GLOBAL_CHANNEL,
    RUNTIME_LOG_STREAM,
    IPCMessage,
    RuntimeLogPayload,
    cmd_channel,
    event_channel,
    make_cmd,
    make_event,
)
from .ratelimit.humanize import simulate_read, simulate_typing
from .scheduler_runtime import PlatformScheduler
from .tg_client import build_client

log = logging.getLogger(__name__)

_CONFIG_RECONCILE_SECONDS = max(30, int(app_settings.worker_reconcile_seconds or 180))
_USERBOT_SESSION_EXPIRE_SCAN_SECONDS = 15
_RECENT_USER_MESSAGE_SEARCH_LIMIT = 200
_RECENT_USER_MESSAGE_SEARCH_LIMIT_MAX = 500
_DEFAULT_REPLY_ANCHOR_MISSING_TEXT = "未找到对应用户（{user_id}）的近期消息。"
_PAYOUT_COMPENSATION_LEASE_SECONDS = 120
_PAYOUT_COMPENSATION_AMBIGUOUS_PROBE_PAGE_SIZE = 100
_BACKGROUND_RPC_COMMAND_TYPES = {
    CMD_FETCH_AVATAR,
    CMD_GET_RECENT_PEERS,
    CMD_EXECUTE_RULE,
    CMD_RUN_INTERACTION_ENTRY,
    CMD_RUN_INTERACTION_ACTION,
}


class _AmbiguousPayoutProbeError(RuntimeError):
    """Raised when Telegram history cannot be inspected safely."""


def _interaction_userbot_rate_limit_action(action_type: str, chat_id: int | None) -> str:
    if action_type in {"send_message", "payout"}:
        return "send_message_private" if chat_id is not None and chat_id > 0 else "send_message_group"
    if action_type in {"edit_message", "edit_caption"}:
        return "edit_message"
    if action_type in {"send_photo", "send_file"}:
        return "upload_file"
    return action_type


async def _acquire_interaction_userbot_rate_limit(
    *,
    redis: Any | None,
    account_id: int | None,
    engine: Any | None,
    action_type: str,
    chat_id: int | None,
) -> None:
    if account_id is None:
        return
    limit_action = _interaction_userbot_rate_limit_action(action_type, chat_id)
    if engine is None:
        if redis is not None:
            await _log(
                redis,
                account_id,
                "warn",
                "UserBot 交互动作未接入限速引擎，已降级直发。",
                action_type=action_type,
                rate_limit_action=limit_action,
                chat_id=chat_id,
            )
        return
    try:
        decision = await engine.acquire(account_id, limit_action, peer_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        if redis is not None:
            await _log(
                redis,
                account_id,
                "warn",
                f"UserBot 交互动作限速检查失败，已降级直发：{type(exc).__name__}: {exc}",
                action_type=action_type,
                rate_limit_action=limit_action,
                chat_id=chat_id,
            )
        return
    if not bool(getattr(decision, "allowed", False)):
        outcome = str(getattr(decision, "outcome", "") or "rate_limited")
        reason = str(getattr(decision, "reason", "") or outcome)
        raise RuntimeError(f"rate_limited: {reason}")
    wait_seconds = float(getattr(decision, "wait_seconds", 0.0) or 0.0)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)


async def _simulate_interaction_userbot_reply_humanize(client: Any, peer: Any, engine: Any | None) -> None:
    opts = getattr(engine, "humanize", None) if engine is not None else None
    if opts is None:
        return
    try:
        if bool(getattr(opts, "read_before_reply", False)):
            await simulate_read(client, peer, opts)
        if bool(getattr(opts, "typing_simulate", False)):
            await simulate_typing(client, peer, opts)
    except Exception:  # noqa: BLE001
        log.debug("interaction userbot humanize simulation failed; continue sending", exc_info=True)


def _is_settlement_send_payload(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("settlement"), dict)


def _should_defer_interaction_entry_error_log(plugin_key: str, error: str | None) -> bool:
    """math10 可在主进程本地 fallback，worker 不应先写误导性 WARN。"""

    return plugin_key == "math10" and "模块未加载或未启用" in str(error or "")


def _httpx_proxy_url_from_proxy(proxy: Proxy | None) -> str | None:
    """把账号 Telegram 代理转换成 httpx 可用的 HTTP/SOCKS 出口。

    MTProxy 只能给 Telethon 使用，ChatGPT/CPA/sub2api 这类 HTTP 请求不能复用。
    """

    from ..util.proxy import parse_proxy_url

    if proxy is None:
        parsed_default = parse_proxy_url(app_settings.tg_default_proxy)
        if parsed_default is None:
            return None
        ptype, host, port, _rdns, username, password = parsed_default
        return _build_proxy_url(ptype, host, port, username, password or "")

    password = decrypt_str(proxy.password_enc) if proxy.password_enc else ""
    if "://" in proxy.host:
        parsed = parse_proxy_url(proxy.host)
        if parsed is None:
            return None
        ptype, host, port, _rdns, parsed_user, parsed_password = parsed
        return _build_proxy_url(
            ptype,
            host,
            port,
            proxy.username or parsed_user,
            password or parsed_password or "",
        )
    return _build_proxy_url(proxy.type, proxy.host, proxy.port, proxy.username, password)


def _normalize_tg_username(value: str | None) -> str | None:
    username = str(value or "").strip().lstrip("@").lower()
    return username or None


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


def _render_interaction_userbot_button_fallback(
    text: str,
    reply_markup: dict[str, Any] | None,
) -> str:
    if not isinstance(reply_markup, dict):
        return text
    rows = reply_markup.get("inline_keyboard")
    if not isinstance(rows, list):
        return text
    lines: list[str] = []
    index = 1
    for row in rows:
        if not isinstance(row, list):
            continue
        for raw_button in row:
            if not isinstance(raw_button, dict):
                continue
            label = str(raw_button.get("text") or "").strip()
            if not label:
                continue
            url = str(raw_button.get("url") or "").strip()
            display = f"{label}: {url}" if url else label
            lines.append(f"{index}. {display}")
            index += 1
    if not lines:
        return text
    return f"{text}\n\n请回复序号选择：\n" + "\n".join(lines)


def _recent_user_message_search_limit(raw: Any) -> int:
    value = _int_or_none(raw)
    if value is None:
        value = _RECENT_USER_MESSAGE_SEARCH_LIMIT
    return max(1, min(_RECENT_USER_MESSAGE_SEARCH_LIMIT_MAX, value))


def _telegram_message_id(msg: Any) -> int | None:
    return _int_or_none(getattr(msg, "id", None) or getattr(msg, "message_id", None))


def _telegram_message_sender_id(msg: Any) -> int | None:
    sender_id = _int_or_none(getattr(msg, "sender_id", None))
    if sender_id is not None:
        return sender_id
    from_id = getattr(msg, "from_id", None)
    return _int_or_none(getattr(from_id, "user_id", None) or getattr(from_id, "channel_id", None))


async def _find_recent_message_id_for_user(
    client: Any,
    chat_id: int,
    user_id: int,
    *,
    limit: int,
) -> int | None:
    """查找参与者近期消息，让 userbot 发奖时能回复到真实玩家消息。

    主路径沿用参考搜索插件的思路，但搜索条件从关键词改成 Telegram
    sender。部分 peer/驱动可能解析不了 ``from_user``，因此保留扫描近期消息
    并本地比对 sender_id 的兜底路径。
    """

    try:
        async for msg in client.iter_messages(chat_id, from_user=user_id, limit=limit):
            msg_id = _telegram_message_id(msg)
            if msg_id is not None:
                return msg_id
    except Exception:  # noqa: BLE001
        log.debug(
            "recent participant message search via from_user failed chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )

    try:
        async for msg in client.iter_messages(chat_id, limit=limit):
            if _telegram_message_sender_id(msg) != user_id:
                continue
            msg_id = _telegram_message_id(msg)
            if msg_id is not None:
                return msg_id
    except Exception:  # noqa: BLE001
        log.debug(
            "recent participant message fallback search failed chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )
    return None


def _reply_anchor_missing_text(payload: dict[str, Any], reply_to_user_id: int | None) -> str:
    template = str(payload.get("reply_anchor_missing_text") or _DEFAULT_REPLY_ANCHOR_MISSING_TEXT).strip()
    if not template:
        template = _DEFAULT_REPLY_ANCHOR_MISSING_TEXT
    user_id_text = str(reply_to_user_id or payload.get("reply_to_user_id") or "")
    try:
        return template.format(user_id=user_id_text)
    except Exception:  # noqa: BLE001
        return template


async def _read_saved_interaction_message_id(
    redis: Any | None,
    account_id: int | None,
    raw_key: Any,
) -> int | None:
    if redis is None:
        return None
    key = namespaced_action_save_message_id_key(account_id, raw_key)
    if not key:
        return None
    try:
        raw = await redis.get(key)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return _int_or_none(raw)


async def _read_payout_sent_marker(
    redis: Any | None,
    account_id: int | None,
    payout_key: Any,
) -> int | None:
    key = payout_compensation.payout_sent_key(account_id, payout_key)
    if redis is None or key is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception:  # noqa: BLE001
        log.debug("read payout sent marker failed account=%s key=%s", account_id, key, exc_info=True)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return _int_or_none(raw) or 0


async def _run_interaction_userbot_action(
    client: Any,
    payload: dict[str, Any],
    *,
    account_id: int | None = None,
    engine: Any | None = None,
    redis: Any | None = None,
) -> dict[str, Any]:
    """用账号自身的 userbot 身份执行平台交互动作。"""

    action_type = str(payload.get("action_type") or "").strip()
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
        reply_to = await _find_recent_message_id_for_user(
            client,
            chat_id,
            reply_to_user_id,
            limit=_recent_user_message_search_limit(payload.get("reply_to_search_limit")),
        )
        if reply_to is None:
            text = _reply_anchor_missing_text(payload, reply_to_user_id)
            if text and not bool(payload.get("suppress_reply_anchor_missing_notice")):
                await client.send_message(chat_id, text, reply_to=None, parse_mode=None)
            raise ValueError(f"找不到用户 {reply_to_user_id} 在当前群的近期消息，无法定位发奖回复目标")

    if action_type in {"send_message", "payout"}:
        text = str(payload.get("text") or "").strip()
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
            text = _render_interaction_userbot_button_fallback(text, reply_markup)
        parse_mode = _interaction_action_parse_mode(payload)
        await _acquire_interaction_userbot_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type=action_type,
            chat_id=chat_id,
        )
        if action_type == "payout":
            payout_key = _str_or_none(payload.get("payout_key"))
            if payout_key:
                payout_ok, payout_reason = await _check_payout_limit(
                    account_id,
                    amount,
                    idempotency_key=payout_key,
                )
            else:
                payout_ok, payout_reason = await _check_payout_limit(account_id, amount)
            if not payout_ok:
                raise PayoutLimitExceeded(payout_reason or "payout 超过限额")
        if action_type == "send_message" and not _is_settlement_send_payload(payload):
            await _simulate_interaction_userbot_reply_humanize(client, chat_id, engine)
        msg = await client.send_message(chat_id, text, reply_to=reply_to, parse_mode=_telethon_parse_mode(parse_mode))
        result = {
            "message_id": int(getattr(msg, "id", 0) or 0) or None,
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "reply_to_user_id": reply_to_user_id,
        }
        if action_type == "payout":
            await payout_compensation.mark_payout_sent_marker(
                redis,
                account_id,
                payload.get("payout_key"),
                result.get("message_id"),
            )
        await save_action_reply_target(
            redis or get_redis(),
            account_id=account_id,
            chat_id=chat_id,
            message_id=result.get("message_id"),
            reply_to_user_id=reply_to_user_id,
        )
        return result

    if action_type == "edit_message":
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("缺少 text")
        try:
            message_id = int(payload["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("缺少 message_id") from exc
        parse_mode = _interaction_action_parse_mode(payload)
        await _acquire_interaction_userbot_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type="edit_message",
            chat_id=chat_id,
        )
        msg = await client.edit_message(chat_id, message_id, text, parse_mode=_telethon_parse_mode(parse_mode))
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
            message_id = await _read_saved_interaction_message_id(redis, account_id, payload.get("message_id_key"))
        if message_id is None:
            raise ValueError("缺少 message_id")
        parse_mode = _interaction_action_parse_mode(payload)
        await _acquire_interaction_userbot_rate_limit(
            redis=redis,
            account_id=account_id,
            engine=engine,
            action_type="edit_caption",
            chat_id=chat_id,
        )
        try:
            msg = await client.edit_message(chat_id, message_id, caption, parse_mode=_telethon_parse_mode(parse_mode))
        except Exception as exc:  # noqa: BLE001
            if _is_message_not_modified_error(exc):
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
        return {
            "message_id": message_id,
            "chat_id": chat_id,
        }

    if action_type == "pin_message":
        message_id = _int_or_none(payload.get("message_id"))
        if message_id is None:
            raise ValueError("缺少 message_id")
        await client.pin_message(chat_id, message_id, notify=False)
        return {
            "message_id": message_id,
            "chat_id": chat_id,
        }

    if action_type in {"send_photo", "send_file"}:
        raw_base64 = str(payload.get("file_base64") or payload.get("photo_base64") or "").strip()
        if not raw_base64:
            raise ValueError("缺少媒体内容")
        import base64
        import binascii

        try:
            file_bytes = base64.b64decode(raw_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("媒体 base64 非法") from exc
        if not file_bytes:
            raise ValueError("媒体内容为空")
        filename = str(payload.get("filename") or ("interaction.png" if action_type == "send_photo" else "interaction.bin")).strip()
        caption = str(payload.get("caption") or payload.get("text") or "").strip() or None
        file_obj = BytesIO(file_bytes)
        file_obj.name = filename or "interaction.bin"
        kwargs: dict[str, Any] = {
            "reply_to": reply_to,
        }
        if caption:
            kwargs["caption"] = caption[:1024]
            kwargs["parse_mode"] = _telethon_parse_mode(_interaction_action_parse_mode(payload))
        if action_type == "send_photo":
            kwargs["force_document"] = False
        await _acquire_interaction_userbot_rate_limit(
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
        )
        return result

    raise ValueError(f"不支持的交互动作: {action_type}")


def _interaction_action_parse_mode(payload: dict[str, Any]) -> str:
    value = str(payload.get("parse_mode") or "plain").strip().lower()
    return "html" if value == "html" else "plain"


def _telethon_parse_mode(parse_mode: str) -> str | None:
    return "html" if parse_mode == "html" else None


def _is_message_not_modified_error(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


async def run_worker(account_id: int) -> None:
    """worker 主协程；返回即代表退出（supervisor 决定是否重启）。"""
    redis = get_redis()
    try:
        from ..services.llm_usage_service import ensure_llm_usage_callback_registered

        ensure_llm_usage_callback_registered()
    except Exception:  # noqa: BLE001
        log.debug("LLM usage callback 注册失败", exc_info=True)

    # 启动时一次性读取账号 + 代理 + 设备伪装 profile
    async with AsyncSessionLocal() as db:
        account = (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if not account:
            await _log(redis, account_id, "error", f"账号 {account_id} 不存在")
            return
        proxy = await db.get(Proxy, account.proxy_id) if account.proxy_id else None
        account_proxy_url = _httpx_proxy_url_from_proxy(proxy)
        # 解析设备伪装：账号绑定 → 系统默认 → 硬编码兜底
        from ..services.device_profile import resolve_for_account
        device_profile = await resolve_for_account(db, account)

    # paused.is_set() == True  → 正常运行
    # paused.is_set() == False → 主动动作被暂停（被动接收照常）
    paused = asyncio.Event()
    paused.set()

    try:
        client = build_client(account, proxy, device_profile)
    except ValueError as exc:
        await _mark_login_required(account_id)
        await _log(
            redis,
            account_id,
            "error",
            "账号登录凭据无法解密，请恢复原 MASTER_KEY 或重新登录该账号。",
            detail={"error": str(exc)},
        )
        return
    make_command_handler(client, account_id)

    # 初始化命令派发上下文（含模板 + LLM provider 字典；由 IPC reload_commands 热更新）
    await _refresh_command_context(account_id)
    await refresh_trace_settings()

    # ⚠ 顺序：必须先 connect，再加载插件。
    #
    # 插件的 on_startup 钩子可能要直接访问 TG（注册 event handler 之外，
    # 比如查 dialogs / 启动定时任务用的 self_id）；如果在 connect 之前调用，
    # 这些 API 会因 "not connected" 报错。把 connect 放最前面，并在 connect
    # 失败时直接返回，避免给插件留半连接的 client。
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await _publish(
                redis, account_id, EVT_LOGIN_REQUIRED, message="session 失效，请重新登录"
            )
            return

        platform_scheduler = PlatformScheduler(
            account_id=account_id,
            client=client,
            redis=redis,
            paused=paused,
            log_writer=_log,
        )

        # connect 成功后再加载插件
        # D Agent 的 plugin loader 会通过 hook 接到 client 上；
        # 这里 try-import：D 没写完时不影响 worker 拉起。
        try:
            from .plugins.loader import load_plugins_for_account  # type: ignore

            await load_plugins_for_account(
                client,
                account_id,
                paused,
                redis,
                scheduler=platform_scheduler,
                account_proxy_url=account_proxy_url,
            )
        except ImportError:
            await _log(redis, account_id, "warn", "插件系统尚未就绪（D Agent 待完成）")
        except Exception as e:
            await _log(redis, account_id, "error", f"加载插件失败: {e}")

        me = await client.get_me()
        # 顺便回填 tg_user_id / tg_username（旧账号迁移 + 用户在 TG 改用户名时同步）
        try:
            new_tg_user_id = getattr(me, "id", None)
            new_tg_username = getattr(me, "username", None) or None
            async with AsyncSessionLocal() as db:
                acc = await db.get(Account, account_id)
                if acc is not None:
                    changed = False
                    if new_tg_user_id is not None and acc.tg_user_id != new_tg_user_id:
                        acc.tg_user_id = new_tg_user_id
                        changed = True
                    if acc.tg_username != new_tg_username:
                        acc.tg_username = new_tg_username
                        changed = True
                    if changed:
                        await db.commit()
        except Exception as e:  # noqa: BLE001
            # 回填失败不影响 worker 继续运行
            await _log(redis, account_id, "warn", f"同步 TG 身份失败: {type(e).__name__}: {e}")
        await _log(
            redis,
            account_id,
            "info",
            f"已上线: {me.first_name or me.username or me.id}",
        )
        await _publish(redis, account_id, EVT_STATUS, status="active")

        # 后台协程：监听 IPC 指令通道与全局通道
        ipc_task = asyncio.create_task(
            _listen_cmd(redis, client, account_id, paused, platform_scheduler)
        )
        global_task = asyncio.create_task(_listen_global(redis, account_id, paused))
        reconcile_task = asyncio.create_task(_periodic_config_reconcile(redis, account_id))
        session_expire_task = asyncio.create_task(_periodic_userbot_session_expire_scan(redis, account_id))
        payout_compensation_task = asyncio.create_task(
            _periodic_payout_compensation_scan(redis, client, account_id)
        )
        scheduler_task = asyncio.create_task(platform_scheduler.run())

        # 启动期临时对象（迁移、insp、Telethon TLS handshake buffer 等）此时已不再需要；
        # 主动 GC 一次让 RSS 在长跑前先收一收，对小机器多账号场景能稳定省 5-15MB。
        try:
            gc.collect()
        except Exception:  # noqa: BLE001
            pass

        try:
            # 阻塞直到 client.disconnect() 被调用
            await client.run_until_disconnected()
        finally:
            for t in (
                ipc_task,
                global_task,
                reconcile_task,
                session_expire_task,
                payout_compensation_task,
                scheduler_task,
            ):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
    except (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError) as e:
        # session 失效类异常：通知主进程置 status=login_required
        await _publish(redis, account_id, EVT_LOGIN_REQUIRED, reason=type(e).__name__)
        await _log(redis, account_id, "error", f"session 失效: {type(e).__name__}")
    except Exception as e:
        await _log(
            redis, account_id, "error", f"worker 异常退出: {type(e).__name__}: {e}"
        )
    finally:
        # ── 安全：调用所有已加载插件的 on_shutdown（幂等设计）──
        try:
            from .plugins.loader import _STATES  # 延迟 import 避免循环

            state = _STATES.get(account_id)
            if state is not None:
                for fkey, inst in list(state.instances.items()):
                    ctx = state.contexts.get(fkey)
                    if ctx is not None and inst is not None:
                        try:
                            await inst.on_shutdown(ctx)
                            log.info("插件 %s on_shutdown 完成", fkey)
                        except Exception:  # noqa: BLE001
                            # on_shutdown 失败不阻止 worker 退出，只记日志
                            log.exception("插件 %s on_shutdown 失败", fkey)
                    if getattr(state, "scheduler", None) is not None:
                        state.scheduler.unregister_owner(fkey)
        except ImportError:
            # 插件系统未就绪
            pass
        except Exception:  # noqa: BLE001
            log.exception("worker shutdown 时插件清理失败 account_id=%s", account_id)

        # ── 断开 client ──
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        try:
            await stop_trace_writer()
        except Exception:  # noqa: BLE001
            log.exception("worker shutdown 时停止 Trace 后台写入器失败 account_id=%s", account_id)
        await _publish(redis, account_id, EVT_STATUS, status="stopped")


async def _next_pubsub_message(pubsub: Any, *, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
    except TimeoutError:
        return None
    return msg if isinstance(msg, dict) else None


async def _listen_cmd(
    redis,
    client,
    account_id: int,
    paused: asyncio.Event,
    platform_scheduler: PlatformScheduler | None = None,
) -> None:
    """监听 ``worker_cmd:{aid}`` 频道，处理 pause/resume/stop/ping/reload/*。

    内置自动重连：Redis 连接断开（如 Docker 重启、网络抖动）时，
    等待 3s 后重新 subscribe，不会让 IPC 命令通道永久失效。
    仅在收到 CMD_STOP（主动退出）时才真正退出循环。
    """
    inflight_rpc_tasks: set[asyncio.Task[None]] = set()
    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(cmd_channel(account_id))
            try:
                while True:
                    msg = await _next_pubsub_message(pubsub)
                    if not msg or msg.get("type") != "message":
                        continue
                    try:
                        cmd = IPCMessage.decode(msg["data"])
                    except Exception:
                        continue
                    ack_ok = True
                    ack_error: str | None = None
                    if _should_schedule_background_rpc(cmd):
                        _schedule_background_rpc(
                            inflight_rpc_tasks,
                            redis=redis,
                            client=client,
                            account_id=account_id,
                            platform_scheduler=platform_scheduler,
                            cmd=cmd,
                        )
                        continue
                    if cmd.type == CMD_PAUSE:
                        paused.clear()
                        await _publish(redis, account_id, EVT_STATUS, status="paused")
                        await _log(redis, account_id, "info", "已暂停")
                    elif cmd.type == CMD_RESUME:
                        paused.set()
                        await _publish(redis, account_id, EVT_STATUS, status="active")
                        await _log(redis, account_id, "info", "已恢复")
                    elif cmd.type == CMD_STOP:
                        await _log(redis, account_id, "info", "收到 stop 指令")
                        await _cancel_inflight_rpc_tasks(inflight_rpc_tasks)
                        # ── 安全：先调用插件 on_shutdown，再断开 client ──
                        try:
                            from .plugins.loader import _STATES  # 延迟 import 避免循环

                            state = _STATES.get(account_id)
                            if state is not None:
                                for fkey, inst in list(state.instances.items()):
                                    ctx = state.contexts.get(fkey)
                                    if ctx is not None and inst is not None:
                                        try:
                                            await inst.on_shutdown(ctx)
                                        except Exception:  # noqa: BLE001
                                            log.exception("插件 %s on_shutdown 失败", fkey)
                                    if getattr(state, "scheduler", None) is not None:
                                        state.scheduler.unregister_owner(fkey)
                        except ImportError:
                            pass
                        except Exception:  # noqa: BLE001
                            log.exception("stop 时插件清理失败")
                        # 主动退出前关闭 pubsub
                        try:
                            await pubsub.unsubscribe(cmd_channel(account_id))
                            await pubsub.close()
                        except Exception:  # noqa: BLE001
                            pass
                        await client.disconnect()
                        return  # CMD_STOP → 正常退出，不重连
                    elif cmd.type == CMD_PING:
                        await _publish(redis, account_id, EVT_PONG)
                    elif cmd.type == CMD_RELOAD_CONFIG:
                        # 让 plugin loader 自己处理（如果存在）
                        try:
                            from .plugins.loader import reload_account_config  # type: ignore

                            await reload_account_config(account_id, cmd.payload)
                            await _refresh_command_context(account_id)
                        except Exception as e:  # noqa: BLE001
                            ack_ok = False
                            ack_error = f"{type(e).__name__}: {e}"
                        await _log(redis, account_id, "info", "reload_config 完成")
                    elif cmd.type == CMD_RELOAD_PLUGIN:
                        try:
                            from .plugins.loader import reload_plugin  # type: ignore

                            await reload_plugin(account_id, cmd.payload.get("plugin_key"))
                        except Exception as e:
                            ack_ok = False
                            ack_error = f"{type(e).__name__}: {e}"
                            await _log(redis, account_id, "error", f"reload_plugin 失败: {e}")
                    elif cmd.type == CMD_RELOAD_COMMANDS:
                        # Sprint2 #2：账号启用/禁用模板、LLM provider 增删后通知 worker 热加载
                        try:
                            await _refresh_command_context(account_id)
                        except Exception as e:  # noqa: BLE001
                            ack_ok = False
                            ack_error = f"{type(e).__name__}: {e}"
                            await _log(
                                redis, account_id, "warn",
                                f"reload_commands 失败: {type(e).__name__}: {e}",
                            )
                        else:
                            await _log(redis, account_id, "info", "reload_commands 完成")
                    elif cmd.type == CMD_RELOAD_IGNORED:
                        # Sprint2 #3：忽略名单变更后，让 plugin loader 从 DB 重拉 set
                        try:
                            from .plugins.loader import reload_ignored_peers  # type: ignore

                            await reload_ignored_peers(account_id)
                        except Exception as e:  # noqa: BLE001
                            ack_ok = False
                            ack_error = f"{type(e).__name__}: {e}"
                            await _log(
                                redis, account_id, "warn", f"reload_ignored 失败: {type(e).__name__}: {e}"
                            )
                    elif cmd.type == CMD_DISPATCH_SIMULATE:
                        try:
                            from .plugins.loader import _STATES, evaluate_dispatch  # type: ignore

                            chat = {
                                "chat_id": cmd.payload.get("chat_id"),
                                "id": cmd.payload.get("chat_id"),
                                "chat_type": cmd.payload.get("chat_type"),
                                "type": cmd.payload.get("chat_type"),
                                "sender_id": cmd.payload.get("sender_id"),
                                "user_id": cmd.payload.get("sender_id"),
                            }
                            trace = evaluate_dispatch(
                                account=account_id,
                                state=_STATES.get(account_id),
                                chat=chat,
                                text=str(cmd.payload.get("text") or ""),
                                via=str(cmd.payload.get("via") or "userbot"),
                            )
                            reply_to = cmd.payload.get("reply_to")
                            cmd_id = cmd.payload.get("cmd_id")
                            if isinstance(reply_to, str) and reply_to and isinstance(cmd_id, str) and cmd_id:
                                await redis.publish(
                                    reply_to,
                                    make_event(
                                        EVT_ACK,
                                        cmd_id=cmd_id,
                                        cmd_type=cmd.type,
                                        ok=True,
                                        error=None,
                                        trace=trace,
                                    ),
                                )
                            continue
                        except Exception as e:  # noqa: BLE001
                            ack_ok = False
                            ack_error = f"{type(e).__name__}: {e}"
                    elif cmd.type == CMD_WEBHOOK_DELIVER:
                        try:
                            from .plugins.loader import dispatch_webhook_event  # type: ignore

                            await dispatch_webhook_event(account_id, cmd.payload, redis=redis)
                        except Exception as e:  # noqa: BLE001
                            ack_ok = False
                            ack_error = f"{type(e).__name__}: {e}"
                    elif cmd.type in _BACKGROUND_RPC_COMMAND_TYPES:
                        handled, ack_ok, ack_error = await _handle_rpc_command(
                            redis,
                            client,
                            account_id,
                            platform_scheduler,
                            cmd,
                        )
                        if not handled:
                            continue
                    await _ack_cmd(redis, cmd, ok=ack_ok, error=ack_error)
            finally:
                try:
                    await pubsub.unsubscribe(cmd_channel(account_id))
                    await pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            # Redis 断连等异常 → 等 3s 后重新 subscribe
            log.warning("worker_cmd listener 异常，3s 后重连: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(3)


def _valid_reply_to(cmd: IPCMessage) -> str | None:
    reply_to = cmd.payload.get("reply_to")
    return reply_to if isinstance(reply_to, str) and reply_to else None


def _should_schedule_background_rpc(cmd: IPCMessage) -> bool:
    if cmd.type == CMD_FETCH_AVATAR:
        return True
    return cmd.type in _BACKGROUND_RPC_COMMAND_TYPES and _valid_reply_to(cmd) is not None


def _schedule_background_rpc(
    tasks: set[asyncio.Task[None]],
    *,
    redis: Any,
    client: Any,
    account_id: int,
    platform_scheduler: PlatformScheduler | None,
    cmd: IPCMessage,
) -> None:
    task = asyncio.create_task(
        _run_background_rpc_command(
            redis,
            client,
            account_id,
            platform_scheduler,
            cmd,
        )
    )
    tasks.add(task)

    def _forget(done: asyncio.Task[None]) -> None:
        tasks.discard(done)
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except Exception as err:  # noqa: BLE001
            log.warning("worker RPC task state check failed: %s: %s", type(err).__name__, err)
            return
        if exc is not None:
            log.error(
                "worker RPC task failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_forget)


async def _run_background_rpc_command(
    redis: Any,
    client: Any,
    account_id: int,
    platform_scheduler: PlatformScheduler | None,
    cmd: IPCMessage,
) -> None:
    try:
        handled, ack_ok, ack_error = await _handle_rpc_command(
            redis,
            client,
            account_id,
            platform_scheduler,
            cmd,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        handled = True
        ack_ok = False
        ack_error = f"{type(exc).__name__}: {exc}"
        await _log(redis, account_id, "warn", f"{cmd.type} 后台 RPC 失败: {ack_error}")
    if handled:
        await _ack_cmd(redis, cmd, ok=ack_ok, error=ack_error)


async def _cancel_inflight_rpc_tasks(tasks: set[asyncio.Task[None]]) -> None:
    pending = [task for task in list(tasks) if not task.done()]
    if not pending:
        tasks.clear()
        return
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    tasks.difference_update(pending)


async def _handle_rpc_command(
    redis: Any,
    client: Any,
    account_id: int,
    platform_scheduler: PlatformScheduler | None,
    cmd: IPCMessage,
) -> tuple[bool, bool, str | None]:
    if cmd.type == CMD_FETCH_AVATAR:
        return True, await _handle_fetch_avatar_command(redis, client, account_id, cmd), None
    if cmd.type == CMD_GET_RECENT_PEERS:
        reply_to = _valid_reply_to(cmd)
        if reply_to is None:
            return False, True, None
        await _handle_get_recent_peers_command(redis, account_id, reply_to)
        return True, True, None
    if cmd.type == CMD_EXECUTE_RULE:
        reply_to = _valid_reply_to(cmd)
        rule_id = cmd.payload.get("rule_id")
        if reply_to is None or not isinstance(rule_id, int):
            return False, True, None
        await _handle_execute_rule_command(redis, account_id, platform_scheduler, reply_to, rule_id)
        return True, True, None
    if cmd.type == CMD_RUN_INTERACTION_ENTRY:
        reply_to = _valid_reply_to(cmd)
        if reply_to is None:
            return False, True, None
        await _handle_run_interaction_entry_command(redis, account_id, cmd, reply_to)
        return True, True, None
    if cmd.type == CMD_RUN_INTERACTION_ACTION:
        reply_to = _valid_reply_to(cmd)
        if reply_to is None:
            return False, True, None
        await _handle_run_interaction_action_command(redis, client, account_id, cmd, reply_to)
        return True, True, None
    return False, True, None


async def _handle_fetch_avatar_command(redis: Any, client: Any, account_id: int, cmd: IPCMessage) -> bool:
    # 主进程懒加载头像：worker 端调用 download_profile_photo 写盘。
    target_path = cmd.payload.get("path")
    if not target_path:
        return True
    try:
        import os
        from pathlib import Path

        out = Path(str(target_path))
        out.parent.mkdir(parents=True, exist_ok=True)
        result = await client.download_profile_photo("me", file=str(out))
        if result is None and out.exists():
            try:
                if os.path.getsize(str(out)) == 0:
                    out.unlink()
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception as e:  # noqa: BLE001
        await _log(redis, account_id, "warn", f"fetch_avatar 失败: {type(e).__name__}: {e}")
        return False


async def _handle_get_recent_peers_command(redis: Any, account_id: int, reply_to: str) -> None:
    # Sprint2 #3 RPC：把内存里的最近活跃 peer 列表回发到 reply_to 频道。
    items: list[dict] = []
    try:
        from .plugins.loader import get_recent_peers  # type: ignore

        items = get_recent_peers(account_id)
    except Exception as e:  # noqa: BLE001
        await _log(
            redis, account_id, "warn",
            f"get_recent_peers 失败: {type(e).__name__}: {e}",
        )
    try:
        await redis.publish(reply_to, make_cmd(CMD_GET_RECENT_PEERS, items=items))
    except Exception:  # noqa: BLE001
        pass


async def _handle_execute_rule_command(
    redis: Any,
    account_id: int,
    platform_scheduler: PlatformScheduler | None,
    reply_to: str,
    rule_id: int,
) -> None:
    result_ok = False
    result_error: str | None = None
    try:
        if platform_scheduler is None:
            result_error = "定时任务调度器尚未初始化"
        else:
            result = await platform_scheduler.execute_rule(rule_id)
            result_ok = result.ok
            result_error = result.error
    except Exception as e:  # noqa: BLE001
        result_error = f"{type(e).__name__}: {e}"
        await _log(redis, account_id, "warn", f"execute_rule 失败: {result_error}")
    try:
        await redis.publish(
            reply_to,
            make_cmd(CMD_EXECUTE_RULE, ok=result_ok, error=result_error),
        )
    except Exception:  # noqa: BLE001
        pass


async def _handle_run_interaction_entry_command(
    redis: Any,
    account_id: int,
    cmd: IPCMessage,
    reply_to: str,
) -> None:
    result_ok = False
    result_error: str | None = None
    actions: list[dict[str, Any]] = []
    try:
        from .plugins.loader import invoke_interaction_entry  # type: ignore

        actions = await invoke_interaction_entry(
            account_id,
            plugin_key=str(cmd.payload.get("plugin_key") or ""),
            entry_key=str(cmd.payload.get("entry_key") or ""),
            payload=dict(cmd.payload.get("payload") or {}),
        )
        result_ok = True
    except Exception as e:  # noqa: BLE001
        result_error = f"{type(e).__name__}: {e}"
        plugin_key = str(cmd.payload.get("plugin_key") or "")
        if not _should_defer_interaction_entry_error_log(plugin_key, result_error):
            await _log(redis, account_id, "warn", f"run_interaction_entry 失败: {result_error}")
    try:
        await redis.publish(
            reply_to,
            make_cmd(CMD_RUN_INTERACTION_ENTRY, ok=result_ok, error=result_error, actions=actions),
        )
    except Exception:  # noqa: BLE001
        pass


async def _handle_run_interaction_action_command(
    redis: Any,
    client: Any,
    account_id: int,
    cmd: IPCMessage,
    reply_to: str,
) -> None:
    result_ok = False
    result_error: str | None = None
    result_payload: dict[str, Any] = {}
    payload = dict(cmd.payload.get("payload") or {})
    try:
        engine = None
        try:
            from .plugins.loader import _STATES  # type: ignore

            state = _STATES.get(account_id)
            engine = getattr(state, "engine", None) if state is not None else None
        except Exception:  # noqa: BLE001
            engine = None
        result_payload = await _run_interaction_userbot_action(
            client,
            payload,
            account_id=account_id,
            engine=engine,
            redis=redis,
        )
        result_ok = True
    except Exception as e:  # noqa: BLE001
        result_error = f"{type(e).__name__}: {e}"
        if isinstance(e, PayoutLimitExceeded):
            error_code = "payout_limit_exceeded"
        else:
            error_code = _interaction_action_error_code(result_error)
        result_payload = _interaction_action_failure_result(payload, error=result_error, error_code=error_code)
        # FloodWait/PeerFlood：喂给限速引擎自动降级（engine 可能为 None），detail 带上 wait 秒数。
        # 引擎钩子内部虽有 try/except，这里再兜一层以免打断下方 IPC 回包。
        if isinstance(e, FloodWaitError):
            result_payload["flood_wait_seconds"] = int(getattr(e, "seconds", 0) or 0)
            if engine is not None:
                try:
                    await engine.on_flood_wait(
                        _interaction_userbot_rate_limit_action(
                            str(payload.get("action_type") or ""),
                            _int_or_none(payload.get("chat_id")),
                        ),
                        e,
                    )
                except Exception:  # noqa: BLE001
                    pass
        elif isinstance(e, PeerFloodError):
            result_payload["peer_flood"] = True
            if engine is not None:
                try:
                    await engine.on_peer_flood("dm_stranger")
                except Exception:  # noqa: BLE001
                    pass
        await _log(
            redis,
            account_id,
            "warn",
            f"run_interaction_action 失败: {result_error}",
            source="event",
            action_type=payload.get("action_type"),
            **_interaction_action_log_detail(result_payload),
        )
    try:
        await redis.publish(
            reply_to,
            make_cmd(
                CMD_RUN_INTERACTION_ACTION,
                ok=result_ok,
                error=result_error,
                result=result_payload,
            ),
        )
    except Exception:  # noqa: BLE001
        pass


def _interaction_action_failure_result(
    payload: dict[str, Any],
    *,
    error: Any,
    error_code: str,
) -> dict[str, Any]:
    detail = _interaction_action_context(payload)
    detail["error"] = str(error or "")
    detail["error_code"] = error_code
    detail["worker_offline"] = error_code == "userbot_offline"
    detail["reply_anchor_missing"] = error_code == "reply_anchor_missing"
    return detail


def _interaction_action_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "chat_id": _int_or_none(payload.get("chat_id")),
        "amount": _int_or_none(payload.get("amount")),
        "reply_to_message_id": _int_or_none(payload.get("reply_to_message_id")),
        "reply_to_user_id": _int_or_none(payload.get("reply_to_user_id")),
        "reply_to_search_limit": _recent_user_message_search_limit(payload.get("reply_to_search_limit")),
    }


def _interaction_action_log_detail(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "chat_id",
            "amount",
            "reply_to_message_id",
            "reply_to_user_id",
            "reply_to_search_limit",
            "error",
            "error_code",
            "worker_offline",
            "reply_anchor_missing",
            "flood_wait_seconds",
            "peer_flood",
        )
        if key in result
    }


def _interaction_action_error_code(error: Any) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return "action_failed"
    if "reply_anchor_missing" in text or "近期消息" in text or "定位发奖回复目标" in text:
        return "reply_anchor_missing"
    if "worker 不在线" in text or "userbot client unavailable" in text:
        return "userbot_offline"
    if "amount" in text or "金额" in text:
        return "invalid_payout_amount"
    if "chat_id" in text:
        return "scope_not_matched"
    if "text" in text or "文本" in text:
        return "empty_message_text"
    if "message_id" in text or ("消息" in text and "id" in text):
        return "target_message_id_missing"
    if "base64" in text or "媒体" in text:
        return "media_payload_invalid"
    if "unsupported" in text or "不支持" in text:
        return "unsupported_send_via"
    return "telegram_api_error"


async def _load_payout_compensation_config() -> dict[str, Any]:
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, payout_compensation.SETTING_KEY)
        value = row.value if row is not None else None
    except Exception:  # noqa: BLE001
        log.debug("load payout compensation config failed; use defaults", exc_info=True)
        value = None
    return payout_compensation.normalize_config(value)


async def _periodic_payout_compensation_scan(redis: Any, client: Any, account_id: int) -> None:
    while True:
        config = await _load_payout_compensation_config()
        try:
            if bool(config.get("enabled", True)):
                await _scan_payout_compensations_once(
                    redis,
                    client,
                    account_id,
                    config=config,
                )
        except Exception as exc:  # noqa: BLE001
            await _log(redis, account_id, "warn", f"payout 补偿扫描失败: {type(exc).__name__}: {exc}")
        scan_interval = int(
            config.get("scan_interval_seconds")
            or payout_compensation.DEFAULT_CONFIG["scan_interval_seconds"]
        )
        await asyncio.sleep(scan_interval)


async def _scan_payout_compensations_once(
    redis: Any,
    client: Any,
    account_id: int,
    *,
    config: dict[str, Any] | None = None,
) -> int:
    config = payout_compensation.normalize_config(config or payout_compensation.DEFAULT_CONFIG)
    if not bool(config.get("enabled", True)):
        return 0
    now = _utc_now()
    ids = await _due_payout_compensation_ids(account_id, now, int(config["batch_size"]))
    processed = 0
    for row_id in ids:
        row = await _lease_payout_compensation(row_id, now)
        if row is None:
            continue
        try:
            await _replay_payout_compensation_row(redis, client, row, config=config)
        except Exception as exc:  # noqa: BLE001
            await _log(
                redis,
                account_id,
                "warn",
                f"payout 补偿单处理失败: {type(exc).__name__}: {exc}",
                payout_compensation_id=row_id,
                payout_key=getattr(row, "payout_key", None),
            )
        processed += 1
    return processed


async def _due_payout_compensation_ids(account_id: int, now: datetime, batch_size: int) -> list[int]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PayoutCompensation.id)
            .where(
                PayoutCompensation.account_id == int(account_id),
                PayoutCompensation.status == PAYOUT_COMPENSATION_STATUS_PENDING,
                PayoutCompensation.next_attempt_at <= now,
            )
            .order_by(PayoutCompensation.id.asc())
            .limit(max(1, int(batch_size)))
        )
        return [int(item) for item in result.scalars().all()]


async def _lease_payout_compensation(row_id: int, now: datetime) -> PayoutCompensation | None:
    lease_until = now + timedelta(seconds=_PAYOUT_COMPENSATION_LEASE_SECONDS)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(PayoutCompensation)
            .where(
                PayoutCompensation.id == int(row_id),
                PayoutCompensation.status == PAYOUT_COMPENSATION_STATUS_PENDING,
                PayoutCompensation.next_attempt_at <= now,
            )
            .values(next_attempt_at=lease_until, updated_at=now)
        )
        if int(result.rowcount or 0) <= 0:
            await db.rollback()
            return None
        row = await db.get(PayoutCompensation, int(row_id))
        await db.commit()
        return row


async def _replay_payout_compensation_row(
    redis: Any,
    client: Any,
    row: PayoutCompensation,
    *,
    config: dict[str, Any],
) -> None:
    now = _utc_now()
    sent_message_id = await _read_payout_sent_marker(redis, row.account_id, row.payout_key)
    if sent_message_id is not None:
        if await _mark_payout_compensation_sent(row.id, sent_message_id, now):
            await _record_payout_replay_action(
                row,
                {"message_id": sent_message_id or None, "chat_id": row.chat_id, "replay_recovered": True},
                replay_recovered=True,
            )
        return

    if bool(row.ambiguous) and bool(config.get("ambiguous_probe", True)):
        try:
            probe_message_id = await _probe_ambiguous_payout_message(client, row)
        except _AmbiguousPayoutProbeError as exc:
            await _apply_payout_replay_failure(
                redis,
                row,
                error_code=payout_compensation.ERROR_TELEGRAM_API,
                error_text=f"{type(exc).__name__}: {exc}",
                config=config,
                now=now,
            )
            return
        if probe_message_id is not None:
            await payout_compensation.mark_payout_sent_marker(redis, row.account_id, row.payout_key, probe_message_id)
            if await _mark_payout_compensation_sent(row.id, probe_message_id, now):
                await _record_payout_replay_action(
                    row,
                    {
                        "message_id": probe_message_id,
                        "chat_id": row.chat_id,
                        "replay_recovered": True,
                        "ambiguous_probe": True,
                    },
                    replay_recovered=True,
                    ambiguous_probe=True,
                )
            return

    payload = _payout_replay_payload(row)
    try:
        result = await _run_interaction_userbot_action(
            client,
            payload,
            account_id=row.account_id,
            engine=_interaction_userbot_engine(row.account_id),
            redis=redis,
        )
        if await _mark_payout_compensation_sent(row.id, result.get("message_id"), now):
            await _record_payout_replay_action(row, result, replay=True)
        return
    except Exception as exc:  # noqa: BLE001
        error_code = _payout_replay_error_code(exc)
        error_text = f"{type(exc).__name__}: {exc}"

    if error_code == payout_compensation.ERROR_REPLY_ANCHOR_MISSING and bool(
        config.get("replay_drop_reply_anchor", True)
    ):
        retry_payload = _drop_payout_reply_anchor(payload)
        try:
            result = await _run_interaction_userbot_action(
                client,
                retry_payload,
                account_id=row.account_id,
                engine=_interaction_userbot_engine(row.account_id),
                redis=redis,
            )
            if await _mark_payout_compensation_sent(row.id, result.get("message_id"), now):
                await _record_payout_replay_action(
                    row,
                    result,
                    replay=True,
                    replay_drop_reply_anchor=True,
                )
            return
        except Exception as retry_exc:  # noqa: BLE001
            error_code = _payout_replay_error_code(retry_exc)
            error_text = f"{type(retry_exc).__name__}: {retry_exc}"

    await _apply_payout_replay_failure(
        redis,
        row,
        error_code=error_code,
        error_text=error_text,
        config=config,
        now=now,
    )


async def _probe_ambiguous_payout_message(client: Any, row: PayoutCompensation) -> int | None:
    target_text = _payout_replay_text(row)
    created_at = _as_utc(row.created_at)
    expected_reply_to = _payout_probe_expected_reply_to(row)
    if expected_reply_to is None:
        return None
    offset_date: datetime | None = None
    try:
        while True:
            seen = False
            reached_lower_bound = False
            oldest_date: datetime | None = None
            kwargs: dict[str, Any] = {
                "from_user": "me",
                "limit": _PAYOUT_COMPENSATION_AMBIGUOUS_PROBE_PAGE_SIZE,
            }
            if offset_date is not None:
                kwargs["offset_date"] = offset_date
            async for msg in client.iter_messages(row.chat_id, **kwargs):
                seen = True
                msg_id = _telegram_message_id(msg)
                msg_date = _as_utc(getattr(msg, "date", None))
                if msg_date is not None:
                    oldest_date = msg_date
                if created_at is not None and msg_date is not None and msg_date < created_at:
                    reached_lower_bound = True
                    break
                if msg_id is None:
                    continue
                if _telegram_message_text(msg).strip() != target_text:
                    continue
                if _telegram_message_reply_to_id(msg) == expected_reply_to:
                    return msg_id
            if reached_lower_bound or not seen or oldest_date is None:
                return None
            offset_date = oldest_date
    except Exception:  # noqa: BLE001
        log.debug("ambiguous payout probe failed row=%s", row.id, exc_info=True)
        raise _AmbiguousPayoutProbeError(f"ambiguous payout probe failed row={row.id}") from None
    return None


def _payout_probe_expected_reply_to(row: PayoutCompensation) -> int | None:
    return _int_or_none((row.payload or {}).get("reply_to_message_id"))


def _telegram_message_reply_to_id(msg: Any) -> int | None:
    direct = _int_or_none(getattr(msg, "reply_to_msg_id", None))
    if direct is not None:
        return direct
    reply_to = getattr(msg, "reply_to", None)
    return _int_or_none(
        getattr(reply_to, "reply_to_msg_id", None)
        or getattr(reply_to, "reply_to_message_id", None)
        or getattr(reply_to, "msg_id", None)
    )


def _payout_replay_payload(row: PayoutCompensation) -> dict[str, Any]:
    payload = dict(row.payload or {})
    payload["action_type"] = "payout"
    payload["chat_id"] = int(row.chat_id)
    payload["amount"] = int(row.amount)
    payload["payout_key"] = row.payout_key
    payload["text"] = _payout_replay_text(row)
    if row.trace_id:
        context = dict(payload.get("context") or {})
        context.setdefault("trace_id", row.trace_id)
        if row.plugin_key:
            context.setdefault("plugin_key", row.plugin_key)
        if row.entry_key:
            context.setdefault("entry_key", row.entry_key)
        payload["context"] = context
    payload.setdefault("suppress_reply_anchor_missing_notice", True)
    return payload


def _payout_replay_text(row: PayoutCompensation) -> str:
    text = str((row.payload or {}).get("text") or "").strip()
    if text:
        return text
    return f"+{int(row.amount)}"


def _drop_payout_reply_anchor(payload: dict[str, Any]) -> dict[str, Any]:
    retry_payload = dict(payload)
    for key in (
        "reply_to_message_id",
        "reply_to_user_id",
        "reply_to_search_limit",
        "reply_anchor_missing_text",
    ):
        retry_payload.pop(key, None)
    retry_payload["suppress_reply_anchor_missing_notice"] = True
    return retry_payload


def _payout_replay_action(row: PayoutCompensation) -> dict[str, Any]:
    action = dict(row.payload or {})
    action["type"] = "payout"
    action["chat_id"] = int(row.chat_id)
    action["amount"] = int(row.amount)
    action["payout_key"] = row.payout_key
    action["text"] = _payout_replay_text(row)
    context = dict(action.get("context") or {})
    if row.trace_id:
        context.setdefault("trace_id", row.trace_id)
    if row.plugin_key:
        context.setdefault("plugin_key", row.plugin_key)
    if row.entry_key:
        context.setdefault("entry_key", row.entry_key)
    if context:
        action["context"] = context
    return action


async def _record_payout_replay_action(
    row: PayoutCompensation,
    result: dict[str, Any],
    **detail: Any,
) -> None:
    action = _payout_replay_action(row)
    result_detail = dict(result or {})
    result_detail.setdefault("payout_key", row.payout_key)
    await record_action(
        action.get("context"),
        action,
        TRACE_STATUS_OK,
        actual_send_via="userbot_reply",
        result=result_detail,
        payout_key=row.payout_key,
        retry_count=row.retry_count,
        **detail,
    )


async def _mark_payout_compensation_sent(row_id: int, message_id: Any, now: datetime) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(PayoutCompensation, int(row_id))
        if row is None or row.status != PAYOUT_COMPENSATION_STATUS_PENDING:
            return False
        row.status = PAYOUT_COMPENSATION_STATUS_SENT
        row.sent_message_id = _int_or_none(message_id)
        row.sent_at = now
        row.updated_at = now
        await db.commit()
        return True


async def _apply_payout_replay_failure(
    redis: Any,
    row: PayoutCompensation,
    *,
    error_code: str,
    error_text: str,
    config: dict[str, Any],
    now: datetime,
) -> None:
    if error_code == payout_compensation.ERROR_PAYOUT_LIMIT_EXCEEDED and _is_daily_payout_limit_error(error_text):
        should_notify = await _defer_payout_compensation_to_next_day(row.id, error_code, error_text, now)
        if should_notify:
            await _log_payout_compensation_error(
                redis,
                row,
                "payout 补偿因日累计上限阻塞，已延后到次日重试。",
                error_code=error_code,
                error=error_text,
            )
        return

    if error_code == payout_compensation.ERROR_PAYOUT_LIMIT_EXCEEDED or not _is_retryable_payout_error(
        error_code,
        error_text,
    ):
        should_notify = await _abandon_payout_compensation(row.id, error_code, error_text, now)
        if should_notify:
            await _log_payout_compensation_error(
                redis,
                row,
                "payout 补偿已放弃。",
                error_code=error_code,
                error=error_text,
            )
        return

    retry_count = int(row.retry_count or 0) + 1
    max_retries = int(config.get("max_retries") or payout_compensation.DEFAULT_CONFIG["max_retries"])
    if retry_count >= max_retries:
        should_notify = await _abandon_payout_compensation(
            row.id,
            error_code,
            error_text,
            now,
            retry_count=retry_count,
        )
        if should_notify:
            await _log_payout_compensation_error(
                redis,
                row,
                "payout 补偿重试耗尽，已放弃。",
                error_code=error_code,
                error=error_text,
                retry_count=retry_count,
            )
        return

    next_attempt_at = now + timedelta(
        seconds=_payout_replay_backoff_seconds(
            retry_count,
            base_seconds=int(config["backoff_base_seconds"]),
            max_seconds=int(config["backoff_max_seconds"]),
        )
    )
    async with AsyncSessionLocal() as db:
        current = await db.get(PayoutCompensation, row.id)
        if current is None or current.status != PAYOUT_COMPENSATION_STATUS_PENDING:
            return
        current.retry_count = retry_count
        current.next_attempt_at = next_attempt_at
        current.error_code_last = error_code
        current.error_last = error_text
        current.updated_at = now
        await db.commit()


async def _defer_payout_compensation_to_next_day(
    row_id: int,
    error_code: str,
    error_text: str,
    now: datetime,
) -> bool:
    next_attempt_at = _next_utc_day_retry_at(now, row_id)
    async with AsyncSessionLocal() as db:
        row = await db.get(PayoutCompensation, int(row_id))
        if row is None or row.status != PAYOUT_COMPENSATION_STATUS_PENDING:
            return False
        should_notify = row.notified_at is None
        row.next_attempt_at = next_attempt_at
        row.error_code_last = error_code
        row.error_last = error_text
        if should_notify:
            row.notified_at = now
        row.updated_at = now
        await db.commit()
        return should_notify


async def _abandon_payout_compensation(
    row_id: int,
    error_code: str,
    error_text: str,
    now: datetime,
    *,
    retry_count: int | None = None,
) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(PayoutCompensation, int(row_id))
        if row is None or row.status != PAYOUT_COMPENSATION_STATUS_PENDING:
            return False
        should_notify = row.notified_at is None
        row.status = PAYOUT_COMPENSATION_STATUS_ABANDONED
        if retry_count is not None:
            row.retry_count = int(retry_count)
        row.error_code_last = error_code
        row.error_last = error_text
        if should_notify:
            row.notified_at = now
        row.updated_at = now
        await db.commit()
        return should_notify


async def _log_payout_compensation_error(
    redis: Any,
    row: PayoutCompensation,
    message: str,
    **detail: Any,
) -> None:
    await _log(
        redis,
        row.account_id,
        "error",
        message,
        source="event",
        payout_compensation_id=row.id,
        payout_key=row.payout_key,
        chat_id=row.chat_id,
        amount=row.amount,
        trace_id=row.trace_id,
        **detail,
    )


def _payout_replay_error_code(exc: BaseException) -> str:
    if isinstance(exc, PayoutLimitExceeded):
        return payout_compensation.ERROR_PAYOUT_LIMIT_EXCEEDED
    return payout_compensation.normalize_payout_error_code(
        _interaction_action_error_code(f"{type(exc).__name__}: {exc}"),
        exc,
    )


def _is_retryable_payout_error(error_code: str, error_text: str) -> bool:
    classification = payout_compensation.classify_payout_error(error_code, error_text)
    return bool(classification.retryable)


def _is_daily_payout_limit_error(error_text: str) -> bool:
    return "日累计" in str(error_text or "")


def _payout_replay_backoff_seconds(retry_count: int, *, base_seconds: int, max_seconds: int) -> int:
    return min(max_seconds, base_seconds * (2 ** max(0, int(retry_count) - 1)))


def _next_utc_day_retry_at(now: datetime, row_id: int) -> datetime:
    now_utc = _as_utc(now) or _utc_now()
    next_day = (now_utc + timedelta(days=1)).date()
    jitter_seconds = int(row_id) % 300
    return datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC) + timedelta(seconds=jitter_seconds)


def _telegram_message_text(msg: Any) -> str:
    return str(
        getattr(msg, "raw_text", None)
        or getattr(msg, "message", None)
        or getattr(msg, "text", None)
        or ""
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _interaction_userbot_engine(account_id: int) -> Any | None:
    try:
        from .plugins.loader import _STATES  # type: ignore

        state = _STATES.get(account_id)
        return getattr(state, "engine", None) if state is not None else None
    except Exception:  # noqa: BLE001
        return None


async def _ack_cmd(redis, cmd: IPCMessage, *, ok: bool, error: str | None = None) -> None:
    """向主进程回 ACK；没有 reply_to 的旧调用保持 fire-and-forget。"""
    reply_to = cmd.payload.get("reply_to")
    cmd_id = cmd.payload.get("cmd_id")
    if not isinstance(reply_to, str) or not reply_to or not isinstance(cmd_id, str) or not cmd_id:
        return
    try:
        await redis.publish(
            reply_to,
            make_event(EVT_ACK, cmd_id=cmd_id, cmd_type=cmd.type, ok=ok, error=error),
        )
    except Exception:  # noqa: BLE001
        pass


async def _periodic_config_reconcile(redis, account_id: int) -> None:
    """周期性从 DB 重拉可变配置，给 Redis pub/sub 控制面做丢消息兜底。

    这不替代实时 IPC；它保证 reload_config / reload_commands / reload_ignored
    类消息即使在 worker 重连窗口丢失，也会在下一轮 reconcile 内收敛。
    """
    while True:
        await asyncio.sleep(_CONFIG_RECONCILE_SECONDS)
        try:
            await _refresh_command_context(account_id)
        except Exception as e:  # noqa: BLE001
            await _log(redis, account_id, "warn", f"periodic reload_commands 失败: {type(e).__name__}: {e}")
        try:
            from .plugins.loader import reload_account_config, reload_ignored_peers  # type: ignore

            await reload_account_config(account_id, {"source": "periodic_reconcile"})
            await reload_ignored_peers(account_id, log_level=None)
        except Exception as e:  # noqa: BLE001
            await _log(redis, account_id, "warn", f"periodic plugin reload 失败: {type(e).__name__}: {e}")


async def _periodic_userbot_session_expire_scan(redis, account_id: int) -> None:
    while True:
        await asyncio.sleep(_USERBOT_SESSION_EXPIRE_SCAN_SECONDS)
        try:
            from .plugins.loader import scan_userbot_expired_sessions_once  # type: ignore

            await scan_userbot_expired_sessions_once(account_id)
        except Exception as e:  # noqa: BLE001
            await _log(redis, account_id, "warn", f"userbot session_expired 扫描失败: {type(e).__name__}: {e}")


async def _listen_global(redis, account_id: int, paused: asyncio.Event) -> None:
    """监听全局广播通道（kill switch / 全局配置 reload）。

    内置自动重连逻辑，与 _listen_cmd 一致。
    """
    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(GLOBAL_CHANNEL)
            try:
                while True:
                    msg = await _next_pubsub_message(pubsub)
                    if not msg or msg.get("type") != "message":
                        continue
                    try:
                        cmd = IPCMessage.decode(msg["data"])
                    except Exception:
                        continue
                    if cmd.type == GCMD_KILL_SWITCH:
                        if cmd.payload.get("enabled"):
                            paused.clear()
                            await _log(redis, account_id, "warn", "全局 kill switch 已启动")
                        else:
                            paused.set()
                            await _log(redis, account_id, "info", "全局 kill switch 已解除")
                    elif cmd.type == GCMD_RELOAD_GLOBAL:
                        # 命令前缀 / 风控模板等全局设置变更后，主进程广播这条让所有 worker 重拉
                        # 当前会刷新写入 worker-local CommandContext 的系统设置。
                        # 风控相关 reload 由 ratelimit 模块自己监听，不在这里处理
                        try:
                            await _refresh_command_context(account_id)
                            await refresh_trace_settings()
                        except Exception as e:  # noqa: BLE001
                            await _log(
                                redis, account_id, "warn",
                                f"reload_global 失败: {type(e).__name__}: {e}",
                            )
                        else:
                            await _log(redis, account_id, "info", "reload_global 完成（命令前缀等）")
            finally:
                try:
                    await pubsub.unsubscribe(GLOBAL_CHANNEL)
                    await pubsub.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            # Redis 断连等异常 → 等 3s 后重新 subscribe
            log.warning("worker_global listener 异常，3s 后重连: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(3)


async def _publish(redis, account_id: int, type_: str, **payload):
    """向 worker_event:{aid} 发一条事件。"""
    await redis.publish(event_channel(account_id), make_event(type_, **payload))


async def _mark_login_required(account_id: int) -> None:
    """worker 自检发现凭据不可用时，直接把账号置为需要重新登录。"""

    from ..db.models.account import ACCOUNT_STATUS_LOGIN_REQUIRED

    async with AsyncSessionLocal() as db:
        account = await db.get(Account, account_id)
        if account is not None:
            account.status = ACCOUNT_STATUS_LOGIN_REQUIRED
            await db.commit()


def _build_proxy_url(
    ptype: str, host: str, port: int, username: str | None, password: str
) -> str | None:
    """把 Proxy ORM 字段拼成 httpx 接受的 URL。

    支持的类型映射（与 ``app.util.proxy._VALID_TYPES`` 对齐 + httpx 实际支持）：
    - ``socks5``        →  ``socks5://``    需 socksio（``httpx[socks]``）
    - ``http`` / ``https``  →  ``http://``  HTTP CONNECT 代理
    - ``mtproxy`` / 其它   →  None          httpx 不支持，调用方应已经过滤

    用户名密码用 ``urllib.parse.quote`` 转义；空字符串视为不设。
    """
    from urllib.parse import quote

    t = (ptype or "").lower()
    if t == "socks5":
        scheme = "socks5"
    elif t in ("http", "https"):
        scheme = "http"
    else:
        # mtproxy / unknown → 不能给 httpx 用
        return None

    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth = f"{auth}:{quote(password, safe='')}"
        auth = f"{auth}@"
    return f"{scheme}://{auth}{host}:{int(port)}"


async def _refresh_command_context(account_id: int) -> None:
    """从 DB 拉本账号已启用的命令模板 + 全部 LLM provider，写入 worker-local ctx。

    用作两个时机：
    - worker 启动时一次（确保新连上 TG 就能响应 ``,模板名``）
    - 收到 IPC ``CMD_RELOAD_COMMANDS`` 时热更新

    实现细节：
    - 避免拿原 ORM 实例（脱离 session 后属性访问会报 DetachedInstanceError），转 dict
    - LLM provider 仍持有 ``api_key_enc``（Fernet token）；解密在调用前的 ``build_client`` 里做
    """
    templates: dict[str, dict] = {}
    providers: dict[int, dict] = {}
    ai_enabled = True
    # 命令前缀：DB 里 system_setting.command_prefix 优先，没有则用 .env 默认
    prefix: str = app_settings.command_prefix or ","
    command_prefix_required = True
    sudo_prefix: str = "."
    sudo_enabled = False
    command_echo_guard_previous_messages = normalize_command_echo_guard_limit(
        app_settings.command_echo_guard_previous_messages
    )
    self_tg_user_id: int | None = None
    scheduler_command_whitelist: list[str] = []
    async with AsyncSessionLocal() as db:
        # 0) 命令前缀（系统设置）
        try:
            row0 = await db.get(SystemSetting, "command_prefix")
            if row0 is not None and isinstance(row0.value, dict):
                v = str(row0.value.get("value", "") or "").strip()
                if v:
                    prefix = v
            elif row0 is not None and isinstance(row0.value, str):
                v = row0.value.strip()
                if v:
                    prefix = v
        except Exception:  # noqa: BLE001
            # DB 读不到（如迁移没跑）就退回 .env 默认；不影响其它字段加载
            pass

        # 0.1) 账号本人命令是否必须带系统前缀（默认必须）
        try:
            row_prefix_required = await db.get(SystemSetting, "command_prefix_required")
            raw_prefix_required = row_prefix_required.value if row_prefix_required is not None else None
            if isinstance(raw_prefix_required, dict):
                command_prefix_required = bool(raw_prefix_required.get("enabled", True))
            elif raw_prefix_required is not None:
                command_prefix_required = bool(raw_prefix_required)
        except Exception:  # noqa: BLE001
            command_prefix_required = True
        
        # 0.5) Sudo 前缀（系统设置）
        try:
            row_sudo = await db.get(SystemSetting, "sudo_prefix")
            if row_sudo is not None and isinstance(row_sudo.value, dict):
                v = str(row_sudo.value.get("value", "") or "").strip()
                if v:
                    sudo_prefix = v
            elif row_sudo is not None and isinstance(row_sudo.value, str):
                v = row_sudo.value.strip()
                if v:
                    sudo_prefix = v
        except Exception:  # noqa: BLE001
            pass

        # 0.6) Sudo 总开关（默认关闭）
        try:
            row_sudo_enabled = await db.get(SystemSetting, "sudo_enabled")
            raw_enabled = row_sudo_enabled.value if row_sudo_enabled is not None else None
            if isinstance(raw_enabled, dict):
                sudo_enabled = bool(raw_enabled.get("enabled", False))
            elif raw_enabled is not None:
                sudo_enabled = bool(raw_enabled)
        except Exception:  # noqa: BLE001
            sudo_enabled = False

        # 0.7) 命令回声防误触窗口（默认取环境变量，可被 system_setting 热更新覆盖）
        try:
            row_echo_guard = await db.get(SystemSetting, "command_echo_guard_previous_messages")
            raw_echo_guard = row_echo_guard.value if row_echo_guard is not None else None
            if isinstance(raw_echo_guard, dict):
                raw_echo_guard = raw_echo_guard.get("value")
            if raw_echo_guard is not None:
                command_echo_guard_previous_messages = normalize_command_echo_guard_limit(raw_echo_guard)
        except Exception:  # noqa: BLE001
            command_echo_guard_previous_messages = normalize_command_echo_guard_limit(
                app_settings.command_echo_guard_previous_messages
            )

        # 0.8) AI 能力热插拔开关。关闭时不加载 LLM provider，避免把密钥、
        # proxy 和模型清单放进 worker 内存。
        try:
            ai_enabled = await is_ai_enabled(db)
        except Exception:  # noqa: BLE001
            ai_enabled = True

        # 1) 该账号启用中的命令模板
        rows = (
            await db.execute(
                select(CommandTemplate)
                .join(
                    AccountCommandLink,
                    AccountCommandLink.template_id == CommandTemplate.id,
                )
                .where(
                    AccountCommandLink.account_id == account_id,
                    AccountCommandLink.enabled.is_(True),
                )
                .order_by(CommandTemplate.id.asc())
            )
        ).scalars().all()
        for r in rows:
            payload = {
                "id": r.id,
                "name": r.name,
                "aliases": list(r.aliases or []),
                "type": r.type,
                "config": dict(r.config or {}),
                "description": r.description,
            }
            templates[r.name] = payload
            for alias in (r.aliases or []):
                templates[alias] = payload

        if ai_enabled:
            # 2) 全部 LLM provider（AI 命令在调用时按 provider_id 索引；不预解密 key）
            #    顺带把 proxy 信息一起拉出来，让 worker 端调 LLM 时也能走代理
            prov_rows = (
                await db.execute(select(LLMProvider))
            ).scalars().all()

            # 收集所有用到的 proxy_id 一次性查出
            proxy_ids = {p.proxy_id for p in prov_rows if p.proxy_id is not None}
            proxy_rows: dict[int, Proxy] = {}
            if proxy_ids:
                rows2 = (
                    await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
                ).scalars().all()
                proxy_rows = {r.id: r for r in rows2}

            for p in prov_rows:
                proxy_url: str | None = None
                if p.proxy_id is not None:
                    pr = proxy_rows.get(p.proxy_id)
                    if pr is not None and (pr.type or "").lower() != "mtproxy":
                        # 主进程在这里就把 password 解密 + 拼成 httpx 接受的 URL；
                        # 比把 password_enc 下发到 worker 让它再解密少一次往返，明文也只在
                        # ctx 内存里活到 LLM 调用结束（worker 进程私有，不进 Redis / 日志）
                        pwd = ""
                        if pr.password_enc:
                            try:
                                pwd = decrypt_str(pr.password_enc)
                            except Exception:  # noqa: BLE001
                                # 密码解密失败时退化为无认证连接，避免一条坏 proxy 把所有 ai 命令打死
                                pwd = ""
                        proxy_url = _build_proxy_url(
                            pr.type, pr.host, pr.port, pr.username, pwd
                        )
                providers[p.id] = {
                    "id": p.id,
                    "name": p.name,
                    "provider": p.provider,
                    "api_key_enc": p.api_key_enc,
                    "base_url": p.base_url,
                    "default_model": p.default_model,
                    # API 协议格式：build_client 据此决定走哪条 client 实现
                    "api_format": getattr(p, "api_format", None),
                    "web_search_api_format": getattr(p, "web_search_api_format", None),
                    # 路由元数据：worker 选 provider 时要看
                    "modality": getattr(p, "modality", None) or "text",
                    "tags": list(getattr(p, "tags", None) or []),
                    "cost_tier": int(getattr(p, "cost_tier", None) or 2),
                    "notes": getattr(p, "notes", None),
                    # 出口代理 URL；None = 直连（DIRECT）
                    "proxy_url": proxy_url,
                    # 候选模型清单（worker 通常不直接读，但保持一致）
                    "models": list(getattr(p, "models", None) or []),
                }

        # 3) 命令别名
        alias_rows = (
            await db.execute(
                select(CommandAlias).where(
                    (CommandAlias.account_id == account_id)
                    | (CommandAlias.account_id.is_(None))
                )
            )
        ).scalars().all()
        aliases: dict[str, str] = {r.alias: r.target for r in alias_rows}

        account_row = await db.get(Account, account_id)
        if account_row is not None and account_row.tg_user_id is not None:
            self_tg_user_id = int(account_row.tg_user_id)

        # 4) Sudo users
        sudo_rows = (
            await db.execute(
                select(SudoUser).where(SudoUser.account_id == account_id)
            )
        ).scalars().all()
        sudo_users: dict[int, dict[str, Any]] = {}
        for r in sudo_rows:
            sudo_users[r.tg_user_id] = {
                "display_name": r.display_name,
                "allowed_chat_ids": list(r.allowed_chat_ids or []),
                "allowed_commands": list(r.allowed_commands or []),
            }

        # 5) scheduler 命令白名单（账号级 feature config）
        af_scheduler = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == account_id,
                    AccountFeature.feature_key == FEATURE_SCHEDULER,
                )
            )
        ).scalar_one_or_none()
        if af_scheduler is not None and isinstance(af_scheduler.config, dict):
            raw_whitelist = af_scheduler.config.get("allowed_command_whitelist")
            scheduler_command_whitelist = normalize_command_whitelist(raw_whitelist)

    set_command_context(
        CommandContext(
            account_id=account_id,
            templates=templates,
            providers=providers,
            ai_enabled=ai_enabled,
            command_prefix=prefix,
            command_prefix_required=command_prefix_required,
            aliases=aliases,
            sudo_users=sudo_users,
            sudo_prefix=sudo_prefix,
            sudo_enabled=sudo_enabled,
            self_tg_user_id=self_tg_user_id,
            command_echo_guard_previous_messages=command_echo_guard_previous_messages,
            scheduler_command_whitelist=scheduler_command_whitelist,
        )
    )


async def _log(
    redis, account_id: int | None, level: str, message: str, *, source: str = "system", **detail
):
    """写运行日志到 Redis stream，主进程批量消费落库。

    source 语义（前端 Logs 页 tab 区分）：
    - ``"system"``（默认） — worker 启停 / 错误 / IPC / 风控状态变化（runtime.py 几乎全是这种）
    - ``"event"``          — incoming 消息事件、plugin 命中、命令派发（业务/监控向）

    历史数据里也会出现 ``"worker"`` / ``"plugin"`` 两个旧值，API 层做了别名映射，
    前端不必关心。
    """
    payload = RuntimeLogPayload(
        account_id=account_id,
        level=level,
        source=source,
        message=message,
        detail=detail or None,
    )
    await redis.rpush(RUNTIME_LOG_STREAM, payload.encode())


def worker_main(account_id: int) -> None:
    """子进程 entrypoint。

    注意：multiprocessing 在 macOS 默认是 spawn，子进程不继承父进程的 logging handler，
    所以这里要重新初始化 logging 配置。
    """
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [worker:{account_id}] %(levelname)s %(message)s",
    )
    asyncio.run(run_worker(account_id))
