"""TG 内命令派发。

用户在 TG 中**自己给自己发**（任何对话，含收藏夹）以前缀（默认 ``,``）开头的消息时，
worker 拦截命令并**编辑原消息**为执行结果。

内置命令：``,help`` ``,status`` ``,ping`` ``,pause`` ``,resume`` ``,restart``（账号级）``,id``。
插件可以通过 ``register_plugin_command`` 追加额外命令（不会覆盖内置）。

Sprint2 #2 起新增 4 类"模板命令"：reply_text / forward_to / run_plugin / ai。
模板命令由主进程 DB 维护，worker 启动 / IPC reload 时拉取并合并到派发链路。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient, events

from ..db.base import AsyncSessionLocal
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..services import audit as audit_svc
from ..services.event_bus import (
    EventSubscription,
    SubscriptionDecision,
    dispatch_event,
    normalize_userbot_event,
)
from ..services.event_trace import (
    TRACE_STATUS_FAILED,
    TRACE_STATUS_OK,
    TRACE_STATUS_SKIPPED,
    finish_trace,
    record_action,
    record_span,
    start_trace,
    trace_log_context,
)
from ..services.rate_limit_service import get_effective_factory, get_humanize_opts
from ..settings import settings
from ..util.sudo_permissions import (
    normalize_sudo_chat_ids,
    normalize_sudo_commands,
    sudo_scope_all,
)
from . import ai_runtime
from .commands.sudo_guard import (
    check_sudo_permission as _check_sudo_permission_impl,
)
from .commands.sudo_guard import (
    has_dispatch_target as _has_dispatch_target_impl,
)
from .commands.sudo_guard import (
    is_self_chat as _is_self_chat_impl,
)
from .commands.sudo_guard import (
    looks_like_command_name as _looks_like_command_name_impl,
)
from .commands.sudo_guard import (
    should_report_incoming_sudo_denial as _should_report_incoming_sudo_denial_impl,
)
from .ipc import CMD_PAUSE, CMD_RESUME, cmd_channel, make_cmd
from .plugins.base import public_entity_display_name
from .ratelimit.engine import RateLimitDecision, RateLimitEngine

log = logging.getLogger(__name__)

# 长消息分段常量
_LONG_MESSAGE_THRESHOLD = 3900  # TG 单条上限约 4096，预留缓冲
_LONG_MESSAGE_SAFE_THRESHOLD = 3900

BuiltinHandler = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class BuiltinCmd:
    handler: BuiltinHandler
    aliases: tuple[str, ...] = ()
    doc: str = ""


@dataclass(frozen=True)
class PluginCmd:
    """插件命令记录（用于追踪和注销）。"""

    handler: BuiltinHandler
    owner_plugin_key: str  # 所属插件的 key
    generation: int  # 插件实例的 generation，用于检测旧 handler


# key 是主命令名（不含前缀）
_BUILTIN: dict[str, BuiltinCmd] = {}
# key 是"主命令 + alias"全集，value 是主命令名
_BUILTIN_ALIAS_TO_PRIMARY: dict[str, str] = {}

# 插件命令注册表：追踪命令 -> (plugin_key, generation, handler)
# 用于插件 reload/disable 时注销旧命令
_PLUGIN_COMMANDS: dict[str, PluginCmd] = {}
_TRACE_FLAG_CACHE: tuple[float, bool] = (0.0, True)
_COMMAND_RATE_LIMIT_ENGINES: dict[int, RateLimitEngine] = {}


def normalize_command_echo_guard_limit(value: Any) -> int:
    try:
        return max(0, min(50, int(value)))
    except (TypeError, ValueError):
        return 8


# ── 模板命令派发上下文 ──────────────────────────────────────────
# 由 runtime.py 在 worker 启动 / IPC reload 时填充；handler 直接读
@dataclass
class CommandContext:
    """worker-local 命令派发上下文。

    - ``account_id``      当前 worker 服务的账号 id
    - ``templates``       {模板名: 模板 dict}；模板 dict 由 ``runtime.py`` 从 DB 拉出后投递
    - ``providers``       {provider_id: provider dict}；同样从 DB 拉，含 api_key 加密 token
    - ``command_prefix``  当前生效的命令前缀（``,`` / ``-`` / ``/`` 等）；
                          系统设置改了 → 主进程发 IPC 让 ``runtime`` 重拉，再写到这里
                          → handler 每次匹配时从 ctx 取，所以前缀热加载对已注册 handler 也生效
    - ``command_prefix_required`` 账号本人 outgoing 命令是否必须带系统前缀；关闭后只对
                          当前账号本人消息启用裸命令，incoming 群成员消息仍不会触发 userbot 命令
    """

    account_id: int
    templates: dict[str, dict[str, Any]]
    providers: dict[int, dict[str, Any]]
    ai_enabled: bool = True
    command_prefix: str = ","
    command_prefix_required: bool = True
    aliases: dict[str, str] = None  # type: ignore[assignment]  # {alias: target}
    sudo_users: dict[int, dict[str, Any]] = None  # type: ignore[assignment]  # {tg_user_id: config}
    sudo_prefix: str = "."
    sudo_enabled: bool = False
    self_tg_user_id: int | None = None
    command_echo_guard_previous_messages: int = 8
    scheduler_command_whitelist: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.aliases is None:
            self.aliases = {}
        if self.sudo_users is None:
            self.sudo_users = {}
        if self.scheduler_command_whitelist is None:
            self.scheduler_command_whitelist = []
        else:
            self.scheduler_command_whitelist = normalize_command_whitelist(
                self.scheduler_command_whitelist
            )
        self.command_echo_guard_previous_messages = normalize_command_echo_guard_limit(
            self.command_echo_guard_previous_messages
        )


# 全局 ctx 由 runtime.py 在 worker 进程启动时初始化并通过闭包传给 handler；
# 同一进程只服务一个 account_id，所以可以直接用模块级单例
_ctx: CommandContext | None = None


def set_command_context(ctx: CommandContext) -> None:
    """runtime.py 启动 worker 后调用一次，IPC reload 时也调用更新内容。"""
    global _ctx
    _ctx = ctx


def get_command_context() -> CommandContext | None:
    """主要供测试 / 调试使用。"""
    return _ctx


def userbot_rate_limit_action(action_type: str, chat_id: Any) -> str:
    """Map Telethon-ish userbot calls to the platform action bucket names."""

    try:
        chat = int(chat_id) if chat_id not in (None, "") else None
    except (TypeError, ValueError):
        chat = None
    # payout 与 send_message 共用发送桶，避免分布式引擎无默认阈值的裸 "payout" 桶。
    if action_type in {"send_message", "respond", "reply", "payout"}:
        return "send_message_private" if chat is not None and chat > 0 else "send_message_group"
    if action_type in {"edit", "edit_message", "edit_caption"}:
        return "edit_message"
    if action_type in {"send_file", "send_photo"}:
        return "upload_file"
    if action_type in {"delete", "delete_message", "delete_messages"}:
        return "delete_message"
    if action_type in {"forward_message", "forward_messages", "forward_to"}:
        return "forward_message"
    return action_type


def _rate_limit_peer_id(chat_id: Any) -> int | None:
    try:
        return int(chat_id) if chat_id not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _command_account_id(account_id: int | None = None, trace: Any | None = None) -> int | None:
    if account_id is not None:
        return account_id
    trace_account_id = getattr(trace, "account_id", None)
    if trace_account_id is not None:
        try:
            return int(trace_account_id)
        except (TypeError, ValueError):
            pass
    if _ctx is not None:
        return _ctx.account_id
    return None


async def _command_rate_limit_engine(account_id: int) -> RateLimitEngine | None:
    cached = _COMMAND_RATE_LIMIT_ENGINES.get(account_id)
    if cached is not None:
        return cached
    if os.environ.get("TELEBOT_WORKER_PROC") != "1":
        return None
    try:
        async with AsyncSessionLocal() as db:
            opts = await get_humanize_opts(db, account_id)
        engine = RateLimitEngine(
            account_id,
            opts,
            get_effective_factory(AsyncSessionLocal),
            redis=get_redis(),
        )
    except Exception:  # noqa: BLE001
        log.debug("init command userbot rate limit engine failed account=%s", account_id, exc_info=True)
        return None
    _COMMAND_RATE_LIMIT_ENGINES[account_id] = engine
    return engine


# 进程内保守令牌桶：Redis/DB 不可用时保护 Telegram 账号，避免无限放行。
# key -> (tokens, last_refill_monotonic)
_LOCAL_RATE_LIMIT_BUCKETS: dict[str, tuple[float, float]] = {}
_LOCAL_RATE_LIMIT_LOCK = asyncio.Lock()
# peer 桶：单会话突发保护；account 桶：跨群总发送速率保护。
_LOCAL_FALLBACK_PEER_CAPACITY = 3.0
_LOCAL_FALLBACK_PEER_REFILL_PER_SEC = 1.0
_LOCAL_FALLBACK_ACCOUNT_CAPACITY = 8.0
_LOCAL_FALLBACK_ACCOUNT_REFILL_PER_SEC = 2.0
_LOCAL_RATE_LIMIT_TTL_SECONDS = 600.0
_LOCAL_RATE_LIMIT_MAX_KEYS = 4096


def _local_rate_limit_peer_key(account_id: int, limit_action: str, peer_id: int | None) -> str:
    peer = "none" if peer_id is None else str(int(peer_id))
    return f"peer:{int(account_id)}:{limit_action}:{peer}"


def _local_rate_limit_account_key(account_id: int, limit_action: str) -> str:
    return f"acct:{int(account_id)}:{limit_action}"


def reset_local_rate_limit_buckets() -> None:
    """测试与运维用：清空进程内本地限流状态。"""

    _LOCAL_RATE_LIMIT_BUCKETS.clear()


def _prune_local_rate_limit_buckets(now: float) -> None:
    """丢弃过期条目；超容量时按最久未刷新淘汰。"""

    stale = [
        key
        for key, (_tokens, last) in _LOCAL_RATE_LIMIT_BUCKETS.items()
        if (now - last) > _LOCAL_RATE_LIMIT_TTL_SECONDS
    ]
    for key in stale:
        _LOCAL_RATE_LIMIT_BUCKETS.pop(key, None)
    overflow = len(_LOCAL_RATE_LIMIT_BUCKETS) - _LOCAL_RATE_LIMIT_MAX_KEYS
    if overflow <= 0:
        return
    oldest = sorted(_LOCAL_RATE_LIMIT_BUCKETS.items(), key=lambda item: item[1][1])[:overflow]
    for key, _ in oldest:
        _LOCAL_RATE_LIMIT_BUCKETS.pop(key, None)


def _consume_local_bucket(
    key: str,
    *,
    capacity: float,
    refill_per_sec: float,
    now: float,
) -> tuple[bool, float]:
    """Try consume 1 token; return (allowed, wait_seconds_if_denied)."""

    tokens, last = _LOCAL_RATE_LIMIT_BUCKETS.get(key, (capacity, now))
    elapsed = max(0.0, now - last)
    tokens = min(capacity, tokens + elapsed * refill_per_sec)
    if tokens < 1.0:
        _LOCAL_RATE_LIMIT_BUCKETS[key] = (tokens, now)
        wait_seconds = (1.0 - tokens) / refill_per_sec if refill_per_sec > 0 else 1.0
        return False, wait_seconds
    tokens -= 1.0
    _LOCAL_RATE_LIMIT_BUCKETS[key] = (tokens, now)
    return True, 0.0


async def _acquire_local_rate_limit_fallback(
    account_id: int,
    limit_action: str,
    peer_id: int | None,
    *,
    action_type: str,
) -> tuple[bool, dict[str, Any]]:
    """Conservative in-process token bucket used when distributed limiting fails."""

    # payout / 资金相关动作：基础设施不稳时宁可不发，也不盲放。
    if str(action_type or "").strip().lower() == "payout" or str(limit_action).startswith("payout"):
        return False, {
            "rate_limit_action": limit_action,
            "rate_limit_backend": "local_fallback",
            "outcome": "rejected",
            "reason": "distributed_rate_limit_unavailable",
        }

    peer_key = _local_rate_limit_peer_key(account_id, limit_action, peer_id)
    account_key = _local_rate_limit_account_key(account_id, limit_action)
    now = time.monotonic()
    async with _LOCAL_RATE_LIMIT_LOCK:
        _prune_local_rate_limit_buckets(now)
        account_ok, account_wait = _consume_local_bucket(
            account_key,
            capacity=_LOCAL_FALLBACK_ACCOUNT_CAPACITY,
            refill_per_sec=_LOCAL_FALLBACK_ACCOUNT_REFILL_PER_SEC,
            now=now,
        )
        if not account_ok:
            return False, {
                "rate_limit_action": limit_action,
                "rate_limit_backend": "local_fallback",
                "outcome": "rejected",
                "wait_seconds": account_wait,
                "reason": "local_fallback_account_throttled",
            }
        peer_ok, peer_wait = _consume_local_bucket(
            peer_key,
            capacity=_LOCAL_FALLBACK_PEER_CAPACITY,
            refill_per_sec=_LOCAL_FALLBACK_PEER_REFILL_PER_SEC,
            now=now,
        )
        if not peer_ok:
            # 归还账号桶 1 token，避免 peer 拒绝却扣了账号配额。
            tokens, last = _LOCAL_RATE_LIMIT_BUCKETS.get(
                account_key, (_LOCAL_FALLBACK_ACCOUNT_CAPACITY, now)
            )
            _LOCAL_RATE_LIMIT_BUCKETS[account_key] = (
                min(_LOCAL_FALLBACK_ACCOUNT_CAPACITY, tokens + 1.0),
                last,
            )
            return False, {
                "rate_limit_action": limit_action,
                "rate_limit_backend": "local_fallback",
                "outcome": "rejected",
                "wait_seconds": peer_wait,
                "reason": "local_fallback_throttled",
            }
    return True, {
        "rate_limit_action": limit_action,
        "rate_limit_backend": "local_fallback",
        "outcome": "allowed",
    }


async def acquire_userbot_action_rate_limit(
    account_id: int | None,
    action_type: str,
    chat_id: Any,
) -> tuple[bool, dict[str, Any]]:
    """UserBot action rate-limit check for direct Telethon send paths.

    Redis/DB 正常时走分布式引擎；异常时降级到进程内保守令牌桶。
    payout 在分布式不可用时 fail-closed，避免资金路径在风控失效时放行。
    """

    if account_id is None:
        return True, {"rate_limit_backend": "skipped", "outcome": "allowed"}
    limit_action = userbot_rate_limit_action(action_type, chat_id)
    peer_id = _rate_limit_peer_id(chat_id)
    try:
        engine = await _command_rate_limit_engine(int(account_id))
    except Exception:  # noqa: BLE001
        log.debug("load command rate limit engine failed account=%s", account_id, exc_info=True)
        return await _acquire_local_rate_limit_fallback(
            int(account_id),
            limit_action,
            peer_id,
            action_type=str(action_type or ""),
        )
    if engine is None:
        return await _acquire_local_rate_limit_fallback(
            int(account_id),
            limit_action,
            peer_id,
            action_type=str(action_type or ""),
        )
    try:
        decision: RateLimitDecision = await engine.acquire(int(account_id), limit_action, peer_id=peer_id)
    except Exception:  # noqa: BLE001
        log.debug(
            "command userbot rate limit check failed account=%s action=%s chat=%s",
            account_id,
            limit_action,
            chat_id,
            exc_info=True,
        )
        return await _acquire_local_rate_limit_fallback(
            int(account_id),
            limit_action,
            peer_id,
            action_type=str(action_type or ""),
        )
    detail = {
        "outcome": getattr(decision, "outcome", None),
        "wait_seconds": getattr(decision, "wait_seconds", None),
        "rate_limit_action": limit_action,
        "rate_limit_backend": "distributed",
    }
    if not bool(getattr(decision, "allowed", False)):
        reason = getattr(decision, "reason", None)
        if reason:
            detail["reason"] = reason
        detail["outcome"] = detail.get("outcome") or "rejected"
        return False, detail
    wait_seconds = float(getattr(decision, "wait_seconds", 0.0) or 0.0)
    if math.isfinite(wait_seconds) and wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    detail["outcome"] = detail.get("outcome") or "allowed"
    return True, detail


async def acquire_direct_userbot_action_rate_limit(
    client: Any,
    account_id: int | None,
    action_type: str,
    chat_id: Any,
) -> tuple[bool, dict[str, Any]]:
    """Rate-limit raw Telethon clients; traced clients already check internally."""

    try:
        if getattr(client, "_telepilot_trace_client", False) is True:
            return True, {}
    except Exception:  # noqa: BLE001
        pass
    return await acquire_userbot_action_rate_limit(account_id, action_type, chat_id)


def current_command_prefix(*, fallback: str | None = None) -> str:
    """Return the worker's live command prefix for user-facing examples."""
    return ((_ctx.command_prefix if _ctx is not None else "") or fallback or settings.command_prefix or ",")


async def _command_trace_enabled() -> bool:
    global _TRACE_FLAG_CACHE
    now = time.monotonic()
    cached_at, cached = _TRACE_FLAG_CACHE
    if now - cached_at < 30:
        return cached
    enabled = True
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "log_retention")
        raw = row.value if row is not None and isinstance(row.value, dict) else {}
        enabled = bool(raw.get("trace_enabled", True))
    except Exception:  # noqa: BLE001
        log.debug("load command trace setting failed, trace stays enabled", exc_info=True)
    _TRACE_FLAG_CACHE = (now, enabled)
    return enabled


class _TraceCommandEvent:
    """Proxy command event methods that produce user-visible Telegram actions."""

    def __init__(
        self,
        event: Any,
        trace: Any,
        *,
        command: str,
        plugin_key: str | None = None,
        account_id: int | None = None,
        record_actions: bool = True,
    ) -> None:
        self._event = event
        self._trace = trace
        self._command = command
        self._plugin_key = plugin_key
        self._account_id = _command_account_id(account_id, trace)
        self._record_actions = record_actions
        raw_client = getattr(event, "client", None)
        self.client = (
            _TraceCommandClient(
                raw_client,
                trace,
                command=command,
                plugin_key=plugin_key,
                account_id=self._account_id,
                record_actions=record_actions,
            )
            if raw_client is not None
            else raw_client
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)

    async def edit(self, text: Any = None, *args: Any, **kwargs: Any) -> Any:
        return await self._call_event_action("edit_message", "edit", text, *args, **kwargs)

    async def respond(self, text: Any = None, *args: Any, **kwargs: Any) -> Any:
        return await self._call_event_action("send_message", "respond", text, *args, **kwargs)

    async def reply(self, text: Any = None, *args: Any, **kwargs: Any) -> Any:
        return await self._call_event_action("send_message", "reply", text, *args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_event_action("delete_message", "delete", None, *args, **kwargs)

    async def get_reply_message(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._event.get_reply_message(*args, **kwargs)
        if result is None:
            return None
        return _TraceForwardMessage(
            result,
            self._trace,
            command=self._command,
            plugin_key=self._plugin_key,
            account_id=self._account_id,
            record_actions=self._record_actions,
        )

    async def _call_event_action(self, action_type: str, method: str, text: Any, *args: Any, **kwargs: Any) -> Any:
        action = _command_trace_action(
            action_type,
            command=self._command,
            plugin_key=self._plugin_key,
            text=text,
            chat_id=getattr(self._event, "chat_id", None),
            message_id=getattr(getattr(self._event, "message", None), "id", None) or getattr(self._event, "id", None),
        )
        allowed, limit_detail = await acquire_userbot_action_rate_limit(
            self._account_id,
            action_type,
            action.get("chat_id"),
        )
        if not allowed:
            if self._record_actions:
                await record_action(
                    trace_log_context(self._trace, plugin_key=self._plugin_key),
                    action,
                    TRACE_STATUS_SKIPPED,
                    actual_send_via="userbot_reply",
                    error_code="rate_limited",
                    result=limit_detail,
                )
            return None
        try:
            fn = getattr(self._event, method)
            if text is None and action_type == "delete_message":
                result = await fn(*args, **kwargs)
            else:
                result = await fn(text, *args, **kwargs)
            if self._record_actions:
                await record_action(
                    trace_log_context(self._trace, plugin_key=self._plugin_key),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result=_command_result_payload(result),
                )
            return result
        except Exception as exc:  # noqa: BLE001
            if self._record_actions:
                await record_action(
                    trace_log_context(self._trace, plugin_key=self._plugin_key),
                    action,
                    TRACE_STATUS_FAILED,
                    actual_send_via="userbot_reply",
                    error_code="telegram_api_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise


class _TraceCommandClient:
    """Proxy client send methods used by command handlers."""

    def __init__(
        self,
        client: Any,
        trace: Any,
        *,
        command: str,
        plugin_key: str | None = None,
        account_id: int | None = None,
        record_actions: bool = True,
    ) -> None:
        self._client = client
        self._trace = trace
        self._command = command
        self._plugin_key = plugin_key
        self._account_id = _command_account_id(account_id, trace)
        self._record_actions = record_actions
        self._telepilot_trace_client = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def _check_rate_limit(self, action: dict[str, Any], action_type: str, chat_id: Any) -> bool:
        allowed, limit_detail = await acquire_userbot_action_rate_limit(
            self._account_id,
            action_type,
            chat_id,
        )
        if allowed:
            return True
        if self._record_actions:
            await record_action(
                trace_log_context(self._trace, plugin_key=self._plugin_key),
                action,
                TRACE_STATUS_SKIPPED,
                actual_send_via="userbot_reply",
                error_code="rate_limited",
                result=limit_detail,
            )
        return False

    async def _record(self, action: dict[str, Any], status: str, **detail: Any) -> None:
        if not self._record_actions:
            return
        await record_action(
            trace_log_context(self._trace, plugin_key=self._plugin_key),
            action,
            status,
            **detail,
        )

    async def send_message(self, chat_id: Any, text: Any = None, *args: Any, **kwargs: Any) -> Any:
        action = _command_trace_action(
            "send_message",
            command=self._command,
            plugin_key=self._plugin_key,
            text=text,
            chat_id=chat_id,
            reply_to_message_id=kwargs.get("reply_to") or kwargs.get("reply_to_message_id"),
        )
        if not await self._check_rate_limit(action, "send_message", chat_id):
            return None
        try:
            result = await self._client.send_message(chat_id, text, *args, **kwargs)
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=_command_result_payload(result),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            await self._record(
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="telegram_api_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def send_file(self, chat_id: Any, file: Any = None, *args: Any, **kwargs: Any) -> Any:
        action = _command_trace_action(
            "send_file",
            command=self._command,
            plugin_key=self._plugin_key,
            text=kwargs.get("caption"),
            chat_id=chat_id,
            reply_to_message_id=kwargs.get("reply_to") or kwargs.get("reply_to_message_id"),
        )
        if not await self._check_rate_limit(action, "send_file", chat_id):
            return None
        try:
            result = await self._client.send_file(chat_id, file, *args, **kwargs)
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=_command_result_payload(result),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            await self._record(
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="telegram_api_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def edit_message(self, chat_id: Any, message_id: Any, text: Any = None, *args: Any, **kwargs: Any) -> Any:
        action = _command_trace_action(
            "edit_message",
            command=self._command,
            plugin_key=self._plugin_key,
            text=text,
            chat_id=chat_id,
            message_id=message_id,
        )
        if not await self._check_rate_limit(action, "edit_message", chat_id):
            return None
        try:
            result = await self._client.edit_message(chat_id, message_id, text, *args, **kwargs)
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=_command_result_payload(result),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            await self._record(
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="telegram_api_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def delete_messages(self, entity: Any, message_ids: Any, *args: Any, **kwargs: Any) -> Any:
        message_id = message_ids[0] if isinstance(message_ids, list) and len(message_ids) == 1 else message_ids
        action = _command_trace_action(
            "delete_message",
            command=self._command,
            plugin_key=self._plugin_key,
            chat_id=entity,
            message_id=message_id if not isinstance(message_id, (list, tuple, set)) else None,
        )
        if not await self._check_rate_limit(action, "delete_message", entity):
            return None
        try:
            result = await self._client.delete_messages(entity, message_ids, *args, **kwargs)
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=_command_result_payload(result),
                message_ids=list(message_ids) if isinstance(message_ids, list) else message_ids,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            await self._record(
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="telegram_api_error",
                error=f"{type(exc).__name__}: {exc}",
                message_ids=list(message_ids) if isinstance(message_ids, list) else message_ids,
            )
            raise

    async def pin_message(self, entity: Any, message: Any, *args: Any, **kwargs: Any) -> Any:
        action = _command_trace_action(
            "pin_message",
            command=self._command,
            plugin_key=self._plugin_key,
            chat_id=entity,
            message_id=getattr(message, "id", None) or message,
        )
        if not await self._check_rate_limit(action, "pin_message", entity):
            return None
        try:
            result = await self._client.pin_message(entity, message, *args, **kwargs)
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=_command_result_payload(result),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            await self._record(
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="telegram_api_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise


class _TraceForwardMessage:
    """Proxy replied message native forwarding so command templates remain traceable."""

    def __init__(
        self,
        message: Any,
        trace: Any,
        *,
        command: str,
        plugin_key: str | None = None,
        account_id: int | None = None,
        record_actions: bool = True,
    ) -> None:
        self._message = message
        self._trace = trace
        self._command = command
        self._plugin_key = plugin_key
        self._account_id = _command_account_id(account_id, trace)
        self._record_actions = record_actions

    def __getattr__(self, name: str) -> Any:
        return getattr(self._message, name)

    async def forward_to(self, target: Any, *args: Any, **kwargs: Any) -> Any:
        action = _command_trace_action(
            "send_message",
            command=self._command,
            plugin_key=self._plugin_key,
            chat_id=target,
        )
        action["forward_mode"] = "forward_native"
        source_chat_id = getattr(self._message, "chat_id", None)
        source_message_id = getattr(self._message, "id", None)
        if source_chat_id is not None:
            action["source_chat_id"] = source_chat_id
        if source_message_id is not None:
            action["source_message_id"] = source_message_id
        allowed, limit_detail = await acquire_userbot_action_rate_limit(
            self._account_id,
            "forward_message",
            target,
        )
        if not allowed:
            if self._record_actions:
                await record_action(
                    trace_log_context(self._trace, plugin_key=self._plugin_key),
                    action,
                    TRACE_STATUS_SKIPPED,
                    actual_send_via="userbot_reply",
                    error_code="rate_limited",
                    result=limit_detail,
                )
            return None
        try:
            result = await self._message.forward_to(target, *args, **kwargs)
            if self._record_actions:
                await record_action(
                    trace_log_context(self._trace, plugin_key=self._plugin_key),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result=_command_result_payload(result),
                )
            return result
        except Exception as exc:  # noqa: BLE001
            if self._record_actions:
                await record_action(
                    trace_log_context(self._trace, plugin_key=self._plugin_key),
                    action,
                    TRACE_STATUS_FAILED,
                    actual_send_via="userbot_reply",
                    error_code="telegram_api_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise


def _trace_command_io(
    client: TelegramClient,
    event: Any,
    trace: Any,
    *,
    command: str,
    plugin_key: str | None,
    account_id: int | None = None,
) -> tuple[Any, Any]:
    record_actions = trace is not None
    return (
        _TraceCommandClient(
            client,
            trace,
            command=command,
            plugin_key=plugin_key,
            account_id=account_id,
            record_actions=record_actions,
        ),
        _TraceCommandEvent(
            event,
            trace,
            command=command,
            plugin_key=plugin_key,
            account_id=account_id,
            record_actions=record_actions,
        ),
    )


def _command_trace_action(
    action_type: str,
    *,
    command: str,
    plugin_key: str | None,
    text: Any = None,
    chat_id: Any = None,
    message_id: Any = None,
    reply_to_message_id: Any = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": action_type,
        "send_via": "userbot_reply",
        "command": command,
    }
    if plugin_key:
        action["plugin_key"] = plugin_key
    if chat_id not in (None, ""):
        action["chat_id"] = chat_id
    if message_id not in (None, ""):
        action["message_id"] = message_id
    if reply_to_message_id not in (None, ""):
        action["reply_to_message_id"] = reply_to_message_id
    if text not in (None, ""):
        action["text"] = str(text)[:4000]
    return action


def _command_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        first = result[0] if result else None
        return {
            "message_id": getattr(first, "id", None) or getattr(first, "message_id", None),
            "chat_id": getattr(first, "chat_id", None),
            "count": len(result),
        }
    return {
        "message_id": getattr(result, "id", None) or getattr(result, "message_id", None),
        "chat_id": getattr(result, "chat_id", None),
    }


def current_sudo_prefix(*, fallback: str | None = None) -> str:
    """Return the worker's live sudo prefix for user-facing examples."""
    return ((_ctx.sudo_prefix if _ctx is not None else "") or fallback or ".")


def _format_sudo_chat_scope(values: Any) -> str:
    if sudo_scope_all(values):
        return "全部（显式）"
    chat_ids = normalize_sudo_chat_ids(values)
    return ",".join(str(chat_id) for chat_id in chat_ids) or "未授权"


def _format_sudo_command_scope(values: Any) -> str:
    if sudo_scope_all(values):
        return "全部（显式）"
    commands = normalize_sudo_commands(values)
    return ",".join(commands) or "未授权"


def normalize_command_whitelist(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        out.append(key)
        seen.add(key)
    return out


def parse_command_key_from_text(text: str, prefix: str) -> str | None:
    raw = str(text or "").strip()
    if not raw or not prefix or not raw.startswith(prefix):
        return None
    rest = raw[len(prefix):].lstrip()
    if not rest:
        return None
    token = rest.split(None, 1)[0].strip()
    if not token:
        return None
    if not _looks_like_command_name(token, prefix=prefix):
        return None
    return token


def _event_message_id(event: Any) -> int | None:
    msg_id = getattr(event, "id", None)
    if msg_id is None:
        msg_id = getattr(getattr(event, "message", None), "id", None)
    try:
        return int(msg_id) if msg_id is not None else None
    except (TypeError, ValueError):
        return None


def _message_text(msg: Any) -> str:
    for attr in ("raw_text", "message", "text"):
        value = getattr(msg, attr, None)
        if isinstance(value, str):
            return value
    return ""


def _is_own_message(msg: Any, self_tg_user_id: int | None) -> bool:
    if bool(getattr(msg, "out", False)) or bool(getattr(msg, "outgoing", False)):
        return True
    sender_id = getattr(msg, "sender_id", None)
    try:
        return self_tg_user_id is not None and sender_id is not None and int(sender_id) == int(self_tg_user_id)
    except (TypeError, ValueError):
        return False


def _command_echo_guard_limit() -> int:
    if _ctx is not None:
        return normalize_command_echo_guard_limit(_ctx.command_echo_guard_previous_messages)
    return normalize_command_echo_guard_limit(settings.command_echo_guard_previous_messages)


async def should_skip_outgoing_command_echo(
    client: TelegramClient,
    event: Any,
    text: str,
    args_raw: str,
) -> bool:
    """群聊抽奖/接龙防误触：近期他人发过同样纯命令时，不执行自己的回声消息。"""

    limit = _command_echo_guard_limit()
    if limit <= 0 or args_raw.strip():
        return False
    if bool(getattr(event, "is_private", False)):
        return False
    needle = str(text or "").strip()
    if not needle:
        return False
    chat_id = getattr(event, "chat_id", None)
    msg_id = _event_message_id(event)
    if chat_id is None or msg_id is None:
        return False

    self_tg_user_id = _ctx.self_tg_user_id if _ctx is not None else None
    try:
        async for msg in client.iter_messages(chat_id, limit=limit, max_id=msg_id):
            if _message_text(msg).strip() != needle:
                continue
            if _is_own_message(msg, self_tg_user_id):
                continue
            return True
    except Exception:  # noqa: BLE001
        log.debug("命令回声防误触检查失败，继续正常派发", exc_info=True)
    return False


def should_allow_auto_command_text(text: str, *, prefix: str | None = None) -> tuple[bool, str | None]:
    effective_prefix = (
        prefix
        if prefix is not None
        else current_command_prefix()
    )
    cmd_key = parse_command_key_from_text(text, effective_prefix)
    if cmd_key is None:
        return True, None
    raw_whitelist = _ctx.scheduler_command_whitelist if _ctx is not None else []
    whitelist = set(normalize_command_whitelist(raw_whitelist))
    if cmd_key in whitelist:
        return True, cmd_key
    return False, cmd_key


def builtin(name: str, *, aliases: tuple[str, ...] = (), doc: str = ""):
    """装饰器：把命令注册到 ``_BUILTIN``。"""

    def deco(fn):
        _BUILTIN[name] = BuiltinCmd(handler=fn, aliases=aliases, doc=doc)
        return fn

    return deco


def _register_builtin_aliases() -> None:
    _BUILTIN_ALIAS_TO_PRIMARY.clear()
    for name, item in _BUILTIN.items():
        _BUILTIN_ALIAS_TO_PRIMARY[name] = name
        for alias in item.aliases:
            _BUILTIN_ALIAS_TO_PRIMARY[alias] = name


def _safe_exception_text(e: BaseException, max_len: int = 200) -> str:
    """把异常信息净化成"安全可在 TG 里显示"的短字符串。

    具体做：
    - 去掉文件绝对路径（``/Users/.../foo.py`` / ``C:\\...\\foo.py``）—— 暴露目录结构是
      安全 & 隐私问题（用户截图里就泄漏过 ``/Users/anoyou/Desktop/telebot/...``）
    - 去掉 ``sk-`` / ``Bearer xxx`` 一类 token 字样
    - 截断到 ``max_len`` 字符
    """
    import re

    msg = f"{type(e).__name__}: {e}"
    # 去 unix 绝对路径 (含括号包裹的也匹配)
    msg = re.sub(r"\(?/[^()\s'\"]+\.py\)?", "<path>", msg)
    # 去 windows 绝对路径
    msg = re.sub(r"\(?[A-Za-z]:[\\/][^()\s'\"]+\.py\)?", "<path>", msg)
    # 去常见 token
    msg = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "<redacted>", msg)
    msg = re.sub(r"Bearer\s+[A-Za-z0-9_.\-]{8,}", "Bearer <redacted>", msg)
    if len(msg) > max_len:
        msg = msg[:max_len] + "…"
    return msg


def _humanize_llm_error(e: BaseException, max_len: int = 360) -> str:
    """把 LLM 调用错误翻译成用户可执行的提示，同时复用脱敏规则。"""
    raw = str(e)
    text = _safe_exception_text(e, max_len=max_len)
    lowered = raw.lower()

    if "budget_exceeded" in lowered or "已达上限" in raw:
        return _safe_exception_text(RuntimeError(raw), max_len=max_len)
    if "usage_limit" in lowered or "quota" in lowered or "insufficient_quota" in lowered:
        return "模型服务额度已用完或账户余额不足。请更换 provider / API Key，或等待额度恢复。"
    if "429" in raw or "rate_limit" in lowered or "too many requests" in lowered:
        return "模型服务正在限流。请稍后重试，或切换到备用 provider。"
    if "401" in raw or "403" in raw or "unauthorized" in lowered or "forbidden" in lowered or "auth" in lowered:
        return "模型鉴权失败：API Key 无效、过期，或当前账号没有权限。请检查 provider 配置。"
    if "404" in raw or "model not found" in lowered:
        return "模型或接口不存在。请检查 provider endpoint、api_format 和模型名称。"
    if "联网搜索需要使用 openai responses api" in lowered or "api_format=responses" in lowered:
        return "联网搜索需要使用 OpenAI Responses API provider（api_format=responses）。请把该 AI 命令绑定到支持 Responses 的 OpenAI provider，或关闭联网搜索。"
    if "timeout" in lowered:
        return "模型响应超时。请稍后重试，或调低 max_tokens / 换更快的 provider。"
    if "connect" in lowered or "network" in lowered or "proxy" in lowered or "ssl" in lowered:
        return "连接模型服务失败。请检查网络、代理和 provider endpoint。"
    if "所有 provider 都失败" in raw:
        detail = raw.split("最后错误:", 1)[1].strip() if "最后错误:" in raw else ""
        if detail:
            return f"所有可用 provider 都调用失败。最后错误：{_safe_exception_text(RuntimeError(detail), max_len=max_len)}"
        return "所有可用 provider 都调用失败。请检查主 provider 和 fallback provider 配置。"
    return text


def _safe_log_text(text: str, max_len: int = 200) -> str:
    """把用户内容净化成"可安全记录日志"的形式。

    不记录完整原文，只记录长度和前 N 个字符的预览。
    用于 debug 日志，避免完整私聊内容被写入日志。
    """
    if not text:
        return "<empty>"
    if not isinstance(text, str):
        text = str(text)
    length = len(text)
    preview_len = max(0, max_len - 1) if len(text) > max_len else max_len
    preview = text[:preview_len] if len(text) > max_len else text
    # 对预览做简单脱敏（去掉可能的 token）
    import re
    preview = re.sub(r"sk-[A-Za-z0-9_-]{4,}", "<sk>", preview)
    if length > max_len:
        return f'<len={length}> "{preview}..."'
    return f'<len={length}> "{preview}"'


def _split_long_message(
    text: str,
    threshold: int = _LONG_MESSAGE_THRESHOLD,
) -> list[str]:
    """将长文本分割为多个短消息。

    策略：
    1. 如果文本长度 <= threshold，直接返回单段
    2. 否则按段落/句子分割，确保每段不超过 threshold
    3. 优先按双换行分割（段落），其次按单换行，最后按句子

    Args:
        text: 原始文本
        threshold: 每段最大字符数（默认 3900）

    Returns:
        分割后的文本列表
    """
    if len(text) <= threshold:
        return [text]

    parts: list[str] = []

    # 策略 1: 按双换行分割（段落）
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= threshold:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                parts.append(current)
            # 单段落超长，继续分割
            if len(para) > threshold:
                current = _split_single_block(para, threshold)
            else:
                current = para

    if current:
        parts.append(current)

    # 合并策略 2: 如果分割后仍然有过长的段，进一步拆分
    final_parts: list[str] = []
    for part in parts:
        if len(part) <= threshold:
            final_parts.append(part)
        else:
            final_parts.extend(_split_single_block(part, threshold))

    return final_parts


def _split_single_block(text: str, threshold: int) -> str:
    """分割超长文本块，优先按换行，其次按句子。"""
    if len(text) <= threshold:
        return text

    # 尝试按换行分割
    lines = text.split("\n")
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 <= threshold:
            current = (current + "\n" + line).strip()
        else:
            if current:
                # 递归处理剩余部分
                remaining = "\n".join(lines[lines.index(line):])
                return current + "\n\n" + _split_single_block(remaining, threshold)
            # 单行就超长，按句子分割
            return _split_by_sentence(text, threshold)

    return current


def _split_by_sentence(text: str, threshold: int) -> str:
    """按句子分割超长文本。"""
    import re
    sentences = re.split(r"([。！？.!?\n])", text)
    current = ""
    result_parts: list[str] = []

    for i in range(0, len(sentences) - 1, 2):
        sent = sentences[i] + (sentences[i + 1] if i + 1 < len(sentences) else "")
        if len(current) + len(sent) <= threshold:
            current += sent
        else:
            if current:
                result_parts.append(current)
            if len(sent) > threshold:
                # 超长句子，按字符硬截断
                result_parts.append(sent[:threshold])
                current = sent[threshold:]
            else:
                current = sent

    if current:
        result_parts.append(current)

    return "\n\n".join(result_parts)


async def _send_long_message(
    client,
    chat_id: int,
    text: str,
    first_msg_id: int | None,
    parse_mode: str | None = None,
    *,
    account_id: int | None = None,
    _max_chunk: int = _LONG_MESSAGE_THRESHOLD,
) -> None:
    """发送长消息，自动分段。

    策略：
    1. 将消息分割成多个短段落
    2. 第一段用 edit_message（保留 reply 链）
    3. 后续段落用 send_message

    Args:
        client: Telegram client
        chat_id: 目标 chat ID
        text: 消息文本
        first_msg_id: 原始命令消息 ID（用于第一段 edit）
        parse_mode: parse_mode（'html' / 'md' / None）
    """
    chunks = _split_long_message(text, _max_chunk)

    if not chunks:
        return

    first = chunks[0]
    remaining = chunks[1:]

    # 第一段：优先用 edit
    if first_msg_id:
        try:
            allowed, _ = await acquire_direct_userbot_action_rate_limit(
                client,
                _command_account_id(account_id),
                "edit_message",
                chat_id,
            )
            if not allowed:
                return
            await client.edit_message(chat_id, first_msg_id, first, parse_mode=parse_mode)
        except Exception:
            # edit 失败时降级为纯文本
            try:
                allowed, _ = await acquire_direct_userbot_action_rate_limit(
                    client,
                    _command_account_id(account_id),
                    "edit_message",
                    chat_id,
                )
                if not allowed:
                    return
                await client.edit_message(chat_id, first_msg_id, first)
            except Exception:
                # 再失败就发送新消息
                allowed, _ = await acquire_direct_userbot_action_rate_limit(
                    client,
                    _command_account_id(account_id),
                    "send_message",
                    chat_id,
                )
                if not allowed:
                    return
                await client.send_message(chat_id, first)
    else:
        allowed, _ = await acquire_direct_userbot_action_rate_limit(
            client,
            _command_account_id(account_id),
            "send_message",
            chat_id,
        )
        if not allowed:
            return
        await client.send_message(chat_id, first, parse_mode=parse_mode)

    # 后续段落：send_message
    for chunk in remaining:
        # 检查是否是 HTML 模式，如果是，需要确保标签闭合
        if parse_mode == "html":
            chunk = _ensure_html_safe(chunk)
        try:
            allowed, _ = await acquire_direct_userbot_action_rate_limit(
                client,
                _command_account_id(account_id),
                "send_message",
                chat_id,
            )
            if not allowed:
                return
            await client.send_message(chat_id, chunk, parse_mode=parse_mode)
        except Exception:
            # 发送失败时降级为纯文本
            try:
                allowed, _ = await acquire_direct_userbot_action_rate_limit(
                    client,
                    _command_account_id(account_id),
                    "send_message",
                    chat_id,
                )
                if not allowed:
                    return
                await client.send_message(chat_id, chunk)
            except Exception:
                # 最坏情况：丢弃该段落
                pass


def _ensure_html_safe(text: str) -> str:
    """确保 HTML 文本安全（避免截断导致标签不闭合）。

    策略：
    1. 检测未闭合的标签
    2. 补全或移除未闭合标签
    """
    import re

    # 检测可能未闭合的标签
    # 匹配 <tag...> 但没有对应的 </tag>
    unclosed_patterns = [
        (r"<b>(?!</b>)", "</b>"),
        (r"<i>(?!</i>)", "</i>"),
        (r"<code>(?!</code>)", "</code>"),
        (r"<pre>(?!</pre>)", "</pre>"),
        (r"<blockquote>(?!</blockquote>)", "</blockquote>"),
    ]

    result = text
    for pattern, closing in unclosed_patterns:
        if re.search(pattern, result) and closing not in result:
            # 找到未闭合标签，在文本末尾补上
            result = result + "\n" + closing

    return result


async def _check_sudo_permission(event, cmd: str, account_id: int) -> tuple[bool, str]:
    return await _check_sudo_permission_impl(_ctx, event, cmd)


async def _write_sudo_audit_log(
    account_id: int,
    status: str,
    *,
    subcommand: str | None = None,
    row_count: int | None = None,
) -> None:
    """写入 sudo 查询审计日志；失败不影响命令语义。"""
    detail: dict[str, Any] = {"status": status}
    if subcommand is not None:
        detail["subcommand"] = subcommand
    if row_count is not None:
        detail["row_count"] = row_count
    try:
        async with AsyncSessionLocal() as db:
            await audit_svc.write(
                db,
                None,
                "worker.sudo",
                target=f"account:{account_id}",
                detail=detail,
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        log.warning(
            "写 sudo audit_log 失败 account_id=%s status=%s",
            account_id,
            status,
            exc_info=True,
        )


def _should_report_incoming_sudo_denial(error_msg: str) -> bool:
    return _should_report_incoming_sudo_denial_impl(error_msg)


def _looks_like_command_name(cmd: str, *, prefix: str = ".") -> bool:
    return _looks_like_command_name_impl(cmd, prefix=prefix)


def _has_dispatch_target(cmd: str, args_raw: str = "") -> bool:
    return _has_dispatch_target_impl(
        cmd,
        args_raw=args_raw,
        builtin_alias_to_primary=_BUILTIN_ALIAS_TO_PRIMARY,
        ctx=_ctx,
    )


def _is_self_chat(event) -> bool:
    return _is_self_chat_impl(event, ctx=_ctx)


def _replied_media_placeholder(msg: Any) -> str:
    """被回复消息没正文（媒体类）时返回个 emoji+标签占位字符串。

    用途：
    - UI 上 ``{quoted}`` blockquote 不至于显示空白
    - LLM 收到 ``[原文]\\n📷 [图片]`` 时知道用户在问图，能体面地说"我看不到图片"

    支持的媒体类型与 telethon 1.36 ``Message`` 上对应字段同名（``photo`` / ``video`` /
    ``voice`` / ``sticker`` / ``audio`` / ``gif`` / ``document`` / ``geo`` / ``contact``
    / ``poll`` / ``video_note``）。匹配不到任何媒体返回空串。
    """
    if getattr(msg, "photo", None) is not None:
        return "📷 [图片]"
    if getattr(msg, "video_note", None) is not None:
        return "📹 [视频留言]"
    if getattr(msg, "video", None) is not None:
        return "🎬 [视频]"
    if getattr(msg, "voice", None) is not None:
        return "🎤 [语音]"
    if getattr(msg, "sticker", None) is not None:
        return "[贴纸]"
    if getattr(msg, "audio", None) is not None:
        return "🎵 [音频]"
    if getattr(msg, "gif", None) is not None:
        return "🖼️ [GIF]"
    if getattr(msg, "document", None) is not None:
        return "📎 [文件]"
    if getattr(msg, "geo", None) is not None:
        return "📍 [位置]"
    if getattr(msg, "contact", None) is not None:
        return "👤 [联系人]"
    if getattr(msg, "poll", None) is not None:
        return "📊 [投票]"
    return ""


@builtin("help", aliases=("h",), doc="显示可用命令列表")
async def _cmd_help(client, event, args, account_id):
    """列出所有可用命令及简短说明。

    每个 builtin 取其 docstring 第一行作为说明；插件注册的命令同样支持。
    模板命令也合并展示，标记 [模板]。
    """
    p = current_command_prefix()
    try:
        raw_text = getattr(event, "raw_text", "")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        for probe in ("help", "h"):
            suffix = f"{probe}"
            if text.startswith(p) and text[len(p):].startswith(suffix):
                break
            idx = text.find(suffix)
            if idx > 0:
                p = text[:idx]
                break
    except Exception:  # noqa: BLE001
        pass
    lines = [f"📋 可用命令（前缀 `{p}`）：", "", "**内置：**"]
    for name in sorted(_BUILTIN.keys()):
        item = _BUILTIN[name]
        alias_text = f" ({', '.join(item.aliases)})" if item.aliases else ""
        desc = item.doc or "（无说明）"
        if name == "del":
            lines.append(f"• `{p}del N`{alias_text} — {desc}")
        else:
            lines.append(f"• `{p}{name}`{alias_text} — {desc}")
    # 模板命令（如有启用）
    if _ctx and _ctx.templates:
        lines.append("")
        lines.append("**自定义模板：**")
        shown: set[str] = set()
        for name in sorted(_ctx.templates.keys()):
            tpl = _ctx.templates[name]
            tid = int(tpl.get("id") or 0)
            if tid in shown:
                continue
            shown.add(tid)
            t = tpl.get("type", "?")
            desc = tpl.get("description") or f"模板：{t}"
            aliases = [a for a in (tpl.get("aliases") or []) if a != name]
            alias_text = f" / {p}" + f" / {p}".join(aliases) if aliases else ""
            lines.append(f"• `{p}{name}{alias_text}` — {desc}（[{t}]）")
    await event.edit("\n".join(lines))


@builtin("status", aliases=("s", "st"), doc="查看账号运行状态")
async def _cmd_status(client, event, args, account_id):
    """显示当前账号信息。"""
    import platform
    import sys

    import telethon

    from ..db.base import AsyncSessionLocal
    from ..db.models.account import Account, DeviceProfile, Proxy
    from ..db.models.system import SystemSetting

    me = await client.get_me()
    name = me.first_name or me.username or "<unnamed>"
    uname = f"@{me.username}" if getattr(me, "username", None) else "-"

    db_status = "-"
    proxy_text = "DIRECT"
    profile_text = "默认"
    prefix = current_command_prefix()

    try:
        async with AsyncSessionLocal() as db:
            acc = await db.get(Account, account_id)
            if acc is not None:
                db_status = acc.status
                if acc.proxy_id:
                    pr = await db.get(Proxy, acc.proxy_id)
                    if pr is not None:
                        proxy_text = f"{pr.type}://{pr.host}:{pr.port}"
                if acc.device_profile_id:
                    dp = await db.get(DeviceProfile, acc.device_profile_id)
                    if dp is not None:
                        profile_text = f"{dp.name} · {dp.device_model} / {dp.system_version}"
            row = await db.get(SystemSetting, "command_prefix")
            if row is not None:
                raw = row.value
                if isinstance(raw, dict):
                    v = str(raw.get("value", "") or "").strip()
                    if v:
                        prefix = v
                elif isinstance(raw, str):
                    v = raw.strip()
                    if v:
                        prefix = v
    except Exception:  # noqa: BLE001
        pass

    tlv = getattr(telethon, "__version__", "?")
    text = (
        f"账号 #{account_id} · {name} ({uname})\n"
        f"在线状态：在线 ✓\n"
        f"DB 状态：{db_status}\n"
        f"命令前缀：`{prefix}`\n"
        f"代理：{proxy_text}\n"
        f"设备档案：{profile_text}\n"
        f"系统：{platform.system()} {platform.release()}\n"
        f"运行时：Python {sys.version.split()[0]} · Telethon {tlv}"
    )
    await event.edit(text)


@builtin("ping", doc="测试 worker 是否在线")
async def _cmd_ping(client, event, args, account_id):
    """连通性自检。"""
    await event.edit("pong")


@builtin("id", aliases=("i",), doc="返回当前会话 chat_id")
async def _cmd_id(client, event, args, account_id):
    """显示当前会话 chat_id（用于配置 auto_reply 的指定群）。"""
    chat_id = event.chat_id
    peer_kind = (
        "私聊" if event.is_private
        else "频道" if event.is_channel
        else "群" if event.is_group
        else "?"
    )
    # supergroup / channel：去掉 -100 前缀给一个"裸 id"，方便用户对照 t.me/c/<id> URL
    bare = ""
    a = abs(int(chat_id)) if chat_id is not None else 0
    if a > 1_000_000_000_000:
        bare = f"\n裸 id（去掉 -100 前缀）：{a - 1_000_000_000_000}"
    text = (
        f"类型：{peer_kind}\n"
        f"chat_id：{chat_id}{bare}\n\n"
        "把上面任一格式填到 auto_reply 规则的「指定群 ID」即可。"
    )
    await event.edit(text)


@builtin("pause", doc="暂停本账号")
async def _cmd_pause(client, event, args, account_id):
    """通过 IPC 通知本 worker 暂停主动动作。"""
    redis = get_redis()
    await redis.publish(cmd_channel(account_id), make_cmd(CMD_PAUSE))
    await event.edit("已暂停（仅暂停主动动作；被动接收照常）")


@builtin("resume", doc="恢复本账号")
async def _cmd_resume(client, event, args, account_id):
    """通过 IPC 通知本 worker 恢复主动动作。"""
    redis = get_redis()
    await redis.publish(cmd_channel(account_id), make_cmd(CMD_RESUME))
    await event.edit("已恢复")


@builtin("restart", aliases=("rs",), doc="重启本账号 worker")
async def _cmd_restart_account(client, event, args, account_id):
    """重启当前账号 worker（账号级，不影响其它账号与前后端）。"""
    await event.edit("正在重启本账号 worker...")
    await client.disconnect()


@builtin("version", aliases=("v",), doc="显示版本号")
async def _cmd_version(client, event, args, account_id):
    """显示当前 TelePilot 版本与运行环境。"""
    import platform
    import sys

    import telethon

    from .. import __version__

    tlv = getattr(telethon, "__version__", "?")
    text = (
        f"📦 telepilot v{__version__}\n"
        f"Python {sys.version.split()[0]} · Telethon {tlv}\n"
        f"Platform {platform.system()} {platform.release()}"
    )
    await event.edit(text)


@builtin("del", doc="撤回自己最近 N 条消息")
async def _cmd_del(client, event, args, account_id):
    """撤回自己在当前会话最近发出的 N 条消息。"""
    p = current_command_prefix()
    if not args or not args[0].isdigit():
        await event.edit(f"用法：{p}del <数字>，例如 {p}del 5")
        return
    n = int(args[0])
    if n <= 0 or n > 100:
        await event.edit("N 必须在 1-100 之间")
        return
    me = await client.get_me()
    chat = await event.get_chat()
    to_delete: list[int] = []
    async for msg in client.iter_messages(chat, limit=200, from_user=me.id):
        to_delete.append(msg.id)
        if len(to_delete) >= n + 1:
            break
    if not to_delete:
        await event.edit("没找到可撤回的消息")
        return
    await client.delete_messages(chat, to_delete[: n + 1])


@builtin("alias", doc="管理命令别名（set/del/ls）")
async def _cmd_alias(client, event, args, account_id):
    """命令别名管理。"""
    from sqlalchemy import delete, select

    from ..db.base import AsyncSessionLocal
    from ..db.models.command import CommandAlias

    p = current_command_prefix()
    if not args:
        await event.edit(f"用法：{p}alias set <别名> <目标> / {p}alias del <别名> / {p}alias ls")
        return

    sub = args[0]

    if sub in ("ls", "list"):
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(CommandAlias).where(
                        (CommandAlias.account_id == account_id)
                        | (CommandAlias.account_id.is_(None))
                    )
                )
            ).scalars().all()
        if not rows:
            await event.edit("当前没有任何别名")
            return
        lines = [f"• {r.alias} → {r.target}" for r in rows]
        await event.edit("命令别名列表：\n" + "\n".join(lines))
        return

    if sub == "del":
        alias_name = " ".join(args[1:]).strip()
        if not alias_name:
            await event.edit(f"用法：{p}alias del <别名>")
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(CommandAlias).where(CommandAlias.alias == alias_name)
            )
            await db.commit()
        if result.rowcount:
            if _ctx is not None:
                _ctx.aliases.pop(alias_name, None)
            await event.edit(f"已删除别名：{alias_name}")
        else:
            await event.edit(f"别名 {alias_name!r} 不存在")
        return

    if sub == "set":
        rest = " ".join(args[1:]).strip()
        for sep in (" -> ", " → "):
            if sep in rest:
                parts = rest.split(sep, 1)
                alias_name = parts[0].strip()
                target_name = parts[1].strip()
                break
        else:
            tokens = rest.split()
            if len(tokens) < 2:
                await event.edit(f"用法：{p}alias set <别名> -> <目标命令>")
                return
            alias_name = tokens[0]
            target_name = " ".join(tokens[1:])

        if not alias_name or not target_name:
            await event.edit("别名和目标不能为空")
            return

        async with AsyncSessionLocal() as db:
            existing = (
                await db.execute(
                    select(CommandAlias).where(CommandAlias.alias == alias_name)
                )
            ).scalar_one_or_none()
            if existing:
                existing.target = target_name
            else:
                db.add(CommandAlias(alias=alias_name, target=target_name, account_id=account_id))
            await db.commit()
        if _ctx is not None:
            _ctx.aliases[alias_name] = target_name
        await event.edit(f"别名已设置：{alias_name} → {target_name}")
        return

    await event.edit(f"未知子命令：{sub}（支持 set/del/ls）")


@builtin("sudo", doc="查看 sudo 用户列表（ls）")
async def _cmd_sudo(client, event, args, account_id):
    """查看 sudo 用户（超级用户，可代表账号执行命令）授权摘要。"""
    from sqlalchemy import select

    from ..db.models.account import SudoUser

    sudo_p = current_sudo_prefix()
    if not args:
        await _write_sudo_audit_log(account_id, "usage")
        await event.edit(f"用法：{sudo_p}sudo ls（仅只读查询）")
        return

    sub = args[0].lower()

    if sub in ("ls", "list"):
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(SudoUser).where(SudoUser.account_id == account_id)
                )
            ).scalars().all()
        if not rows:
            await _write_sudo_audit_log(account_id, "empty", subcommand=sub, row_count=0)
            await event.edit("当前没有任何 sudo 用户")
            return
        lines = []
        for r in rows:
            chat_str = _format_sudo_chat_scope(r.allowed_chat_ids)
            cmd_str = _format_sudo_command_scope(r.allowed_commands)
            lines.append(f"• TG用户 {r.tg_user_id}（{r.display_name or '无'}）\n"
                         f"  允许对话：{chat_str}\n"
                         f"  允许命令：{cmd_str}")
        await _write_sudo_audit_log(
            account_id,
            "ok",
            subcommand=sub,
            row_count=len(rows),
        )
        await event.edit("Sudo 用户列表：\n" + "\n".join(lines))
        return

    await _write_sudo_audit_log(account_id, "invalid_subcommand", subcommand=sub)
    await event.edit(f"仅支持只读查询：{sudo_p}sudo ls")


_register_builtin_aliases()


def register_plugin_command(name: str, fn: Callable, *, owner_plugin_key: str = "", generation: int = 0):
    """允许其他模块（主要是 D Agent 插件）注册命令；不会覆盖内置。

    **安全：追踪 owner_plugin_key 和 generation，用于插件 reload/disable 时注销旧命令。**

    Args:
        name: 命令名（不含前缀）
        fn: 命令处理函数
        owner_plugin_key: 所属插件的 key
        generation: 插件实例的 generation
    """
    if name in _BUILTIN_ALIAS_TO_PRIMARY:
        return  # 不覆盖内置命令
    _BUILTIN[name] = BuiltinCmd(handler=fn)
    _PLUGIN_COMMANDS[name] = PluginCmd(
        handler=fn,
        owner_plugin_key=owner_plugin_key,
        generation=generation,
    )
    _register_builtin_aliases()


def unregister_plugin_command(name: str, *, owner_plugin_key: str | None = None):
    """注销插件命令。

    **安全设计**：
    - 如果指定了 owner_plugin_key，只注销该插件注册的命令
    - 如果未指定 owner_plugin_key，注销所有同名命令
    - 命令注销后，旧 handler 不会再被触发

    Args:
        name: 命令名
        owner_plugin_key: 如果指定，只注销该插件的命令
    """
    if name not in _PLUGIN_COMMANDS:
        # 内置命令不在插件命令表中，不能被注销。
        return

    # 从插件命令表中注销
    pcmd = _PLUGIN_COMMANDS[name]
    if owner_plugin_key is None or pcmd.owner_plugin_key == owner_plugin_key:
        _PLUGIN_COMMANDS.pop(name, None)
        # 插件命令复用 _BUILTIN 分发表；注销时必须一起移除。
        _BUILTIN.pop(name, None)
        _register_builtin_aliases()


def unregister_all_plugin_commands(*, owner_plugin_key: str):
    """注销指定插件注册的所有命令。

    Args:
        owner_plugin_key: 插件的 key
    """
    to_remove = [
        name for name, pcmd in _PLUGIN_COMMANDS.items()
        if pcmd.owner_plugin_key == owner_plugin_key
    ]
    for name in to_remove:
        unregister_plugin_command(name, owner_plugin_key=owner_plugin_key)


# ════════════════════════════════════════════════════════════
# 模板命令执行（Sprint2 #2）
# ════════════════════════════════════════════════════════════


async def _run_template(client, event, args, tpl: dict[str, Any], account_id: int) -> None:
    """根据 ``tpl["type"]`` 分支执行模板命令。

    模板 dict 的字段（与 ``CommandTemplate`` 模型对应）：
    - ``id``        模板 id
    - ``name``      命令名（不含前缀）
    - ``type``      reply_text / forward_to / run_plugin / ai
    - ``config``    按 type 不同结构
    - ``description``  可选
    """
    t = tpl.get("type")
    cfg: dict[str, Any] = tpl.get("config") or {}

    if t == "reply_text":
        # 简单变量替换：{args} → 用户拼接的剩余参数
        text = str(cfg.get("text", "")).replace("{args}", " ".join(args))
        await event.edit(text or "(空文本)")
        return

    if t == "forward_to":
        replied = await event.get_reply_message()
        if not replied:
            await event.edit("✗ 请回复要转发的消息再用此命令")
            return
        # target_chat_id 留空 / 缺省 → 转发到触发命令所在的 chat
        raw_target = cfg.get("target_chat_id")
        if raw_target is None or raw_target == "":
            target = event.chat_id
        else:
            try:
                target = int(raw_target)
            except (ValueError, TypeError):
                await event.edit("✗ 模板配置错误：target_chat_id 不是合法的整数")
                return
        # 按 mode 分支处理
        mode = cfg.get("mode", "forward_native")
        try:
            if mode == "forward_native":
                await replied.forward_to(target)
            elif mode == "copy_text":
                text = replied.text or "(empty)"
                await event.client.send_message(target, text)
            elif mode == "copy_media":
                media = (
                    getattr(replied, "media", None)
                    or getattr(replied, "document", None)
                    or getattr(replied, "photo", None)
                )
                text = getattr(replied, "text", None) or ""
                if media is not None:
                    kwargs = {"caption": text} if text else {}
                    await event.client.send_file(target, media, **kwargs)
                else:
                    await event.client.send_message(target, text or "(empty)")
            elif mode == "quote":
                try:
                    src = await replied.get_chat()
                except Exception:  # noqa: BLE001
                    src = None
                chat_label = public_entity_display_name(
                    src,
                    fallback_id=replied.chat_id if hasattr(replied, "chat_id") else "?",
                )
                body = f"📨 来自 {chat_label}\n\n{replied.text or '(no text)'}"
                await event.client.send_message(target, body)
            elif mode == "link_only":
                # 为 replied 构造 link：取 replied 的 chat_id + message.id
                cid = getattr(replied, "chat_id", None)
                mid = getattr(replied, "id", None)
                if cid and mid:
                    sid = str(cid)
                    if sid.startswith("-100"):
                        link = f"https://t.me/c/{sid[4:]}/{mid}"
                    else:
                        link = f"消息引用：chat={cid}, id={mid}"
                else:
                    link = "消息引用：无法生成链接"
                await event.client.send_message(target, link)
            else:
                await event.edit(f"✗ 未知转发方式：{mode}")
                return
        except Exception as e:  # noqa: BLE001
            await event.edit(f"✗ 转发失败：{type(e).__name__}: {str(e)[:80]}")
            return
        # 自动删除命令消息
        delete_immediately = cfg.get("delete_immediately")
        if delete_immediately:
            import asyncio as _aio

            async def _delete_now() -> None:
                try:
                    await event.delete()
                except Exception:  # noqa: BLE001
                    pass

            _aio.create_task(_delete_now())
        else:
            mode_label = {
                "forward_native": "转发",
                "copy_text": "复制文本",
                "copy_media": "复读",
                "quote": "引用转发",
                "link_only": "链接",
            }.get(mode, mode)
            await event.edit(f"✓ 已{mode_label}到 {target}")
            delete_after_raw = cfg.get("delete_after")
            if delete_after_raw:
                try:
                    seconds = int(delete_after_raw)
                except (ValueError, TypeError):
                    seconds = 0
                if seconds > 0:
                    import asyncio as _aio

                    async def _delete_later() -> None:
                        try:
                            await _aio.sleep(seconds)
                            await event.delete()
                        except Exception:  # noqa: BLE001
                            # TG 端权限/网络异常都不影响主流程
                            pass

                    _aio.create_task(_delete_later())
        return

    if t == "ai":
        await _run_ai(client, event, args, tpl, account_id)
        return

    if t == "run_plugin":
        plugin_key = str(cfg.get("plugin_key") or "").strip()
        method = str(cfg.get("method") or cfg.get("command") or plugin_key).strip()
        if not plugin_key:
            await event.edit("✗ run_plugin 需要配置 plugin_key 和 method/command")
            return
        dispatched = await dispatch_plugin_command(
            client, event, args, account_id, plugin_key=plugin_key, method=method or None
        )
        if not dispatched:
            await event.edit(f"✗ 插件命令不可用：{plugin_key}.{method or plugin_key}")
        return

    await event.edit(f"✗ 未知模板类型：{t}")


async def _run_ai(client, event, args, tpl: dict[str, Any], account_id: int) -> None:
    if _ctx is not None and not _ctx.ai_enabled:
        await event.edit("✗ AI 能力已在系统设置中关闭。需要使用 AI 指令时，请先重新启用 AI 能力。")
        return
    await ai_runtime.invoke(client, event, args, tpl, account_id)


async def dispatch_plugin_command(
    client,
    event,
    args: list[str],
    account_id: int,
    *,
    plugin_key: str,
    method: str | None = None,
) -> bool:
    """按 plugin_key 调用已注册插件命令。

    ``method`` 为空时会寻找该插件当前注册的第一条命令，适合命令名可配置的插件
    （例如 codex_image 的 command 字段）。
    """
    plugin_key = str(plugin_key or "").strip()
    method = str(method or "").strip()
    if not plugin_key:
        return False
    if method:
        pcmd = _PLUGIN_COMMANDS.get(method)
        if pcmd is None or pcmd.owner_plugin_key != plugin_key:
            return False
        await pcmd.handler(client, event, args, account_id)
        return True
    for pcmd in _PLUGIN_COMMANDS.values():
        if pcmd.owner_plugin_key == plugin_key:
            await pcmd.handler(client, event, args, account_id)
            return True
    return False


def _command_dispatch_target(cmd: str, args_raw: str) -> tuple[str, str | None]:
    if cmd in _PLUGIN_COMMANDS:
        return "plugin_command", _PLUGIN_COMMANDS[cmd].owner_plugin_key or None
    primary = _BUILTIN_ALIAS_TO_PRIMARY.get(cmd)
    if primary and _BUILTIN.get(primary) is not None:
        return "builtin", None
    if _ctx is not None and _ctx.aliases:
        full_rest = f"{cmd} {args_raw}".strip() if args_raw else cmd
        for alias in sorted(_ctx.aliases.keys(), key=len, reverse=True):
            if full_rest == alias or full_rest.startswith(alias + " "):
                return "alias", None
    if _ctx is not None and _ctx.templates.get(cmd) is not None:
        tpl = _ctx.templates[cmd]
        if str(tpl.get("type") or "").strip() == "run_plugin":
            return "template_run_plugin", str((tpl.get("config") or {}).get("plugin_key") or "").strip() or None
        return "template", None
    return "unknown", None


def _has_userbot_command_target(cmd: str, args_raw: str = "") -> bool:
    target_type, _plugin_key = _command_dispatch_target(cmd, args_raw)
    return target_type != "unknown"


def _split_command_parts(text: str) -> tuple[str, str] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    cmd = parts[0].strip()
    if not cmd:
        return None
    return cmd, (parts[1].strip() if len(parts) > 1 else "")


def _command_event_bus_decisions(
    event_payload: dict[str, Any],
    *,
    cmd: str,
    target_type: str,
    plugin_key: str | None,
) -> list[SubscriptionDecision]:
    """Run administrator commands through a virtual Event Bus decision.

    Builtin/template commands are not installed plugins, but final Trace must
    still show a real Event Bus-style decision before the compatibility command
    handler runs.
    """

    if target_type == "unknown":
        return dispatch_event(event_payload, [], {"allowed_chat_ids": "*"}).decisions
    sender = event_payload.get("sender") if isinstance(event_payload.get("sender"), dict) else {}
    sender_user_id = sender.get("user_id")
    scope = "owner_only" if sender_user_id is not None else "all_allowed_chats"
    account_state = {"allowed_chat_ids": "*"}
    if sender_user_id is not None:
        account_state["owner_user_ids"] = [sender_user_id]
    subscription = EventSubscription(
        plugin_key=plugin_key or f"system:{target_type}",
        entry_key=target_type,
        sources=["userbot"],
        events=["command"],
        scope=scope,
        filters={"commands": [cmd]},
        dispatch_mode="admin_command",
    )
    return dispatch_event(event_payload, [subscription], account_state).decisions


def _command_decisions_detail(decisions: list[SubscriptionDecision]) -> list[dict[str, Any]]:
    return [
        {
            "plugin_key": item.plugin_key,
            "entry_key": item.entry_key,
            "matched": item.matched,
            "reason_code": item.reason_code,
            "dispatch_mode": item.dispatch_mode,
            "scope": item.scope,
            "filters": item.filters,
        }
        for item in decisions
    ]


async def _dispatch_command(
    client: TelegramClient,
    event,
    cmd: str,
    args_raw: str,
    *,
    account_id: int,
    help_prefix: str,
) -> None:
    target_type, plugin_key = _command_dispatch_target(cmd, args_raw)
    trace = None
    final_status = TRACE_STATUS_SKIPPED if target_type == "unknown" else TRACE_STATUS_OK
    if await _command_trace_enabled():
        event_payload = normalize_userbot_event(
            account_id,
            event,
            command_meta={
                "command": cmd,
                "args_raw": args_raw,
                "prefix": help_prefix,
                "target_type": target_type,
                "plugin_key": plugin_key,
            },
        )
        command_decisions = _command_event_bus_decisions(
            event_payload,
            cmd=cmd,
            target_type=target_type,
            plugin_key=plugin_key,
        )
        trace = await start_trace(event_payload)
        try:
            event.trace_id = trace.trace_id
        except Exception:  # noqa: BLE001
            pass
        await record_span(
            trace,
            "receive",
            TRACE_STATUS_OK,
            component="userbot_command",
            command=cmd,
            args_raw=args_raw,
        )
        await record_span(
            trace,
            "command_parse",
            TRACE_STATUS_OK if target_type != "unknown" else TRACE_STATUS_SKIPPED,
            component="userbot_command",
            reason_code="command_matched" if target_type != "unknown" else "command_not_matched",
            command=cmd,
            target_type=target_type,
            plugin_key=plugin_key,
            event_bus_decisions=_command_decisions_detail(command_decisions),
        )
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_OK if target_type != "unknown" else TRACE_STATUS_SKIPPED,
            component="userbot_command",
            reason_code="command_matched" if target_type != "unknown" else "command_not_matched",
            message="管理员命令已映射为 Event Bus command decision。",
            command=cmd,
            target_type=target_type,
            plugin_key=plugin_key,
            dispatch_mode="admin_command",
            scope="owner_only",
            event_bus_decisions=_command_decisions_detail(command_decisions),
        )
    try:
        traced_client, traced_event = _trace_command_io(
            client,
            event,
            trace,
            command=cmd,
            plugin_key=plugin_key,
            account_id=account_id,
        )
        await _dispatch_command_inner(
            traced_client,
            traced_event,
            cmd,
            args_raw,
            account_id=account_id,
            help_prefix=help_prefix,
        )
    except Exception as exc:  # noqa: BLE001
        final_status = TRACE_STATUS_FAILED
        await record_span(
            trace,
            "finish",
            TRACE_STATUS_FAILED,
            component="userbot_command",
            reason_code="handler_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        await finish_trace(trace, final_status)


async def _dispatch_sudo_denial(
    client: TelegramClient,
    event,
    cmd: str,
    args_raw: str,
    *,
    account_id: int,
    help_prefix: str,
    error_msg: str,
    respond: bool,
) -> None:
    target_type, plugin_key = _command_dispatch_target(cmd, args_raw)
    trace = None
    if await _command_trace_enabled():
        event_payload = normalize_userbot_event(
            account_id,
            event,
            command_meta={
                "command": cmd,
                "args_raw": args_raw,
                "prefix": help_prefix,
                "target_type": target_type,
                "plugin_key": plugin_key,
                "sudo_denied": True,
            },
        )
        command_decisions = _command_event_bus_decisions(
            event_payload,
            cmd=cmd,
            target_type=target_type,
            plugin_key=plugin_key,
        )
        trace = await start_trace(event_payload)
        try:
            event.trace_id = trace.trace_id
        except Exception:  # noqa: BLE001
            pass
        await record_span(
            trace,
            "receive",
            TRACE_STATUS_OK,
            component="userbot_command",
            command=cmd,
            args_raw=args_raw,
        )
        await record_span(
            trace,
            "command_parse",
            TRACE_STATUS_FAILED,
            component="userbot_command",
            reason_code="command_unauthorized",
            command=cmd,
            target_type=target_type,
            plugin_key=plugin_key,
            error=error_msg,
            event_bus_decisions=_command_decisions_detail(command_decisions),
        )
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_FAILED,
            component="userbot_command",
            reason_code="command_unauthorized",
            message="管理员命令权限校验失败，未进入处理器。",
            command=cmd,
            target_type=target_type,
            plugin_key=plugin_key,
            dispatch_mode="admin_command",
            scope="owner_only",
            event_bus_decisions=_command_decisions_detail(command_decisions),
        )
    try:
        _, traced_event = _trace_command_io(
            client,
            event,
            trace,
            command=cmd,
            plugin_key=plugin_key,
            account_id=account_id,
        )
        text = f"✗ Sudo 权限拒绝：{error_msg}"
        if respond:
            await traced_event.respond(text)
        else:
            await traced_event.edit(text)
    finally:
        await finish_trace(trace, TRACE_STATUS_FAILED)


async def _dispatch_command_inner(
    client: TelegramClient,
    event,
    cmd: str,
    args_raw: str,
    *,
    account_id: int,
    help_prefix: str,
) -> None:
    args = args_raw.split() if args_raw else []
    # 1. 内置命令优先
    primary = _BUILTIN_ALIAS_TO_PRIMARY.get(cmd)
    item = _BUILTIN.get(primary) if primary else None
    if item is not None:
        try:
            await item.handler(client, event, args, account_id)
        except Exception as e:  # noqa: BLE001
            # 命令执行异常时，把错误原地写回消息，方便排查（消息已脱敏：去路径/token）
            try:
                await event.edit(f"✗ 执行失败：{_safe_exception_text(e)}")
            except Exception:
                pass
        return

    # 2. 别名解析（贪心最长匹配）
    if _ctx is not None and _ctx.aliases:
        # 尝试从 "cmd arg1 arg2..." 中匹配最长的别名
        full_rest = f"{cmd} {args_raw}".strip() if args_raw else cmd
        matched_alias: str | None = None
        for alias in sorted(_ctx.aliases.keys(), key=len, reverse=True):
            if full_rest == alias or full_rest.startswith(alias + " "):
                matched_alias = alias
                break
        if matched_alias is not None:
            target = _ctx.aliases[matched_alias]
            remaining = full_rest[len(matched_alias):].strip()
            # 重新拼接：target + remaining args
            new_text = f"{target} {remaining}".strip() if remaining else target
            new_parts = new_text.split(None, 1)
            new_cmd = new_parts[0] if new_parts else ""
            new_args_raw = new_parts[1] if len(new_parts) > 1 else ""
            new_args = new_args_raw.split() if new_args_raw else []
            # 重新派发到 builtin
            primary2 = _BUILTIN_ALIAS_TO_PRIMARY.get(new_cmd)
            item2 = _BUILTIN.get(primary2) if primary2 else None
            if item2 is not None:
                try:
                    await item2.handler(client, event, new_args, account_id)
                except Exception as e:  # noqa: BLE001
                    try:
                        await event.edit(f"✗ 执行失败：{_safe_exception_text(e)}")
                    except Exception:
                        pass
                return
            # 重新派发到模板
            tpl2 = _ctx.templates.get(new_cmd)
            if tpl2 is not None:
                try:
                    await _run_template(client, event, new_args, tpl2, account_id)
                except Exception as e:  # noqa: BLE001
                    try:
                        await event.edit(f"✗ 执行失败：{_safe_exception_text(e)}")
                    except Exception:
                        pass
                return

    # 3. 模板命令（按 name 查 worker-local ctx）
    if _ctx is not None:
        tpl = _ctx.templates.get(cmd)
        if tpl is not None:
            try:
                await _run_template(client, event, args, tpl, account_id)
            except Exception as e:  # noqa: BLE001
                try:
                    await event.edit(f"✗ 执行失败：{_safe_exception_text(e)}")
                except Exception:
                    pass
            return

    # 4. 未知命令
    try:
        await event.edit(f"未知命令：{cmd}（{help_prefix}help 查看可用列表）")
    except Exception:
        pass


def make_command_handler(client: TelegramClient, account_id: int, prefix: str | None = None):
    """创建并注册 TG 命令派发 handler。

    普通命令监听 ``outgoing=True``；sudo 命令额外监听 ``incoming=True``，
    允许白名单用户用 sudo_prefix 触发命令。

    前缀热加载：handler 每次拦截消息时**从 ctx 读 prefix**，不再用闭包里固定 pattern。
    系统设置改前缀 → 主进程广播 IPC ``reload_global`` → runtime 重拉 ctx → 下一条消息立刻按
    新前缀匹配。``prefix`` 参数仅作"启动期默认"，正常运行靠 ctx 动态。
    """
    fallback_prefix = prefix or settings.command_prefix or ","

    class _IncomingSudoEvent:
        """把 incoming sudo 的 edit() 转成 respond()，避免尝试编辑他人消息。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def edit(self, *args, **kwargs):
            responder = getattr(self._inner, "respond", None) or getattr(self._inner, "reply", None)
            if responder is None:
                return None
            return await responder(*args, **kwargs)

    async def _handle(event, *, allow_normal: bool, incoming_sudo: bool = False):
        text = event.raw_text or ""
        sudo_p = current_sudo_prefix()
        pattern_sudo = re.compile(rf"^{re.escape(sudo_p)}(\S+)(?:\s+(.*))?$", re.S)
        m = pattern_sudo.match(text)
        if m and incoming_sudo:
            cmd = m.group(1)
            args_raw = (m.group(2) or "").strip()
            if not _looks_like_command_name(cmd, prefix=sudo_p):
                return
            if incoming_sudo and not _has_dispatch_target(cmd, args_raw):
                return
            if incoming_sudo and not _is_self_chat(event):
                return
            allowed, error_msg = await _check_sudo_permission(event, cmd, account_id)
            if not allowed:
                if incoming_sudo:
                    if not _should_report_incoming_sudo_denial(error_msg):
                        return
                    await _dispatch_sudo_denial(
                        client,
                        event,
                        cmd,
                        args_raw,
                        account_id=account_id,
                        help_prefix=sudo_p,
                        error_msg=error_msg,
                        respond=True,
                    )
                else:
                    await _dispatch_sudo_denial(
                        client,
                        event,
                        cmd,
                        args_raw,
                        account_id=account_id,
                        help_prefix=sudo_p,
                        error_msg=error_msg,
                        respond=False,
                    )
                return
            dispatch_event = _IncomingSudoEvent(event) if incoming_sudo else event
            await _dispatch_command(
                client,
                dispatch_event,
                cmd,
                args_raw,
                account_id=account_id,
                help_prefix=sudo_p,
            )
            return

        if not allow_normal:
            return

        p = current_command_prefix(fallback=fallback_prefix)
        pattern = re.compile(rf"^{re.escape(p)}(\S+)(?:\s+(.*))?$", re.S)
        m = pattern.match(text)
        help_prefix = p
        bare_command = False
        if m:
            cmd = m.group(1)
            args_raw = (m.group(2) or "").strip()
            if not _looks_like_command_name(cmd, prefix=p):
                return
        else:
            if _ctx is None or _ctx.command_prefix_required:
                return
            bare_parts = _split_command_parts(text)
            if bare_parts is None:
                return
            cmd, args_raw = bare_parts
            if not _looks_like_command_name(cmd, prefix=p):
                return
            if not _has_userbot_command_target(cmd, args_raw):
                return
            help_prefix = ""
            bare_command = True
        if not bare_command and await should_skip_outgoing_command_echo(client, event, text, args_raw):
            log.info(
                "跳过疑似抽奖/接龙回声命令 account=%s chat_id=%s command=%s",
                account_id,
                getattr(event, "chat_id", None),
                cmd,
            )
            return
        await _dispatch_command(
            client,
            event,
            cmd,
            args_raw,
            account_id=account_id,
            help_prefix=help_prefix,
        )

    @client.on(events.NewMessage(incoming=True))
    async def _sudo_incoming_h(event):
        await _handle(event, allow_normal=False, incoming_sudo=True)

    @client.on(events.NewMessage(outgoing=True))
    async def _h(event):
        await _handle(event, allow_normal=True, incoming_sudo=False)

    return _h
