"""账号级插件加载器：连接 Telethon、实例化每个启用的 [账号 × feature] 插件，并维护其生命周期。

安全设计（阶段 E）：
- installed 插件统一授权：安装记录全局开关 / 签名状态 / AccountFeature.enabled 都通过才加载
- 插件命令注册表：追踪 owner/plugin_key/generation，插件 reload/disable 时自动注销
- on_shutdown 保证调用（幂等设计）

使用流程：
1. ``run_worker`` 在 ``client.connect()`` 前调 ``load_plugins_for_account``，本模块会：
   - 触发内置插件 import（``@register`` 写入全局注册表）
   - 在 ``client`` 上挂全局消息派发器（incoming + outgoing），按各插件的 ``message_channels`` 声明过滤
   - 实例化该账号当前通过授权检查的 enabled 插件，并把状态写回为 active
2. 主进程通过 IPC ``CMD_RELOAD_CONFIG`` 触发 ``reload_account_config`` 实现热更新（拉新 rules / config，
   并对新增 / 移除的 feature 做差量加载与卸载）
3. ``CMD_RELOAD_PLUGIN`` 调 ``reload_plugin``：builtin 走 ``importlib.reload``，installed 走按需重载

模块化后插件以"目录"形式存在：
- 内置：``backend/app/worker/plugins/builtin/<key>/{__init__.py, manifest.py, plugin.py}``
- 第三方：``plugins/installed/<key>/{__init__.py, manifest.py, plugin.py}``（阶段 B 引入）
每个插件目录的 ``__init__.py`` 必须暴露 ``PLUGIN_CLASS``（Plugin 子类）与 ``MANIFEST``
（``Manifest`` 实例）两个常量；``discover_plugins()`` 扫描时按目录读取这两个常量装载。

任何插件抛出的异常都不会让整个 worker 崩溃；该 plugin 会被标记为 ``failed`` 状态。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import importlib
import importlib.util
import json
import logging
import re
import shutil
import time
import traceback
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, update
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError

from ... import __version__ as TELEPILOT_VERSION
from ...db.base import AsyncSessionLocal
from ...db.models.account import Account, HumanizeConfig, SudoUser
from ...db.models.feature import (
    FEATURE_SCHEDULER,
    FEATURE_STATE_ACTIVE,
    FEATURE_STATE_DISABLED,
    FEATURE_STATE_FAILED,
    AccountFeature,
)
from ...db.models.ignored_peer import IgnoredPeer
from ...db.models.plugin import PLUGIN_TRUST_ORPHAN, InstalledPlugin
from ...db.models.plugin_global_config import PluginGlobalConfig
from ...db.models.rule import Rule
from ...db.models.system import SystemSetting
from ...redis_client import get_redis
from ...services import account_bot_service, interaction_bot_service, payout_compensation, payout_limit
from ...services.action_tap import (
    ACTION_EVENT_STATUS_DRY_RUN,
    ACTION_EVENT_STATUS_FAILED,
    ACTION_EVENT_STATUS_OK,
    action_context_dry_run_enabled,
    dev_mode_dry_run_enabled,
    dev_mode_recording_enabled,
    emit_action_event,
    emit_inbound_event,
)
from ...services.event_bus import (
    dispatch_event,
    event_subscription_warnings,
    normalize_event_subscription,
    normalize_userbot_event,
    normalize_webhook_event,
)
from ...services.event_trace import (
    TRACE_STATUS_FAILED,
    TRACE_STATUS_OK,
    TRACE_STATUS_SKIPPED,
    finish_trace,
    record_action,
    record_span,
    start_trace,
    trace_log_context,
    update_plugin_runtime_status,
)
from ...services.interaction.contracts import (
    SEND_CHANNEL_DEPRECATED_REASON_CODE,
    action_send_via_options,
    action_send_via_raw_selector,
    apply_action_send_via_options,
    deprecated_send_via_values,
)
from ...services.interaction.dedupe import claim_interaction_message, release_interaction_message
from ...services.interaction.delivery import (
    delivery_message_id,
    namespaced_action_save_message_id_key,
    save_action_reply_target,
)
from ...services.rate_limit_service import get_effective
from ...settings import settings as app_settings
from ...util.sudo_permissions import sudo_chat_allowed
from ..command import (
    current_command_prefix,
    register_plugin_command,
    unregister_all_plugin_commands,
)
from ..ipc import RUNTIME_LOG_STREAM, RuntimeLogPayload
from ..ratelimit.engine import RateLimitEngine
from ..ratelimit.humanize import HumanizeOpts, simulate_read, simulate_typing
from .base import (
    Plugin,
    PluginContext,
    all_plugins,
    build_implicit_plugin,
    clear_simple_commands_for_module,
    get_plugin,
    public_entity_display_name,
)
from .events import attach_tp_event
from .manifest import Manifest
from .message_ops import BufferedMessageOps
from .storage import PluginStorage

log = logging.getLogger(__name__)

_INTERACTION_RULE_OWNED_REASON_CODE = "interaction_rule_owned"
_RECENT_USER_MESSAGE_SEARCH_LIMIT = 200
_RECENT_USER_MESSAGE_SEARCH_LIMIT_MAX = 500
_DEFAULT_REPLY_ANCHOR_MISSING_TEXT = "未找到对应用户（{user_id}）的近期消息。"


@dataclass(frozen=True)
class _InstalledPluginAuthorization:
    """运行期 installed 插件加载授权结果。"""

    allowed: bool
    state: str = FEATURE_STATE_ACTIVE
    last_error: str | None = None
    log_level: str = "info"
    log_message: str = ""


@dataclass(frozen=True)
class _UserbotEventBusDispatch:
    """UserBot 消息进入 Event Bus 后的可投递上下文。"""

    trace: Any | None
    event_payload: dict[str, Any]
    event_bus_enabled: bool = False
    matched_decisions: tuple[Any, ...] = ()
    subscribed_plugin_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _InteractionTextGuardRule:
    chat_ids: frozenset[int]
    texts: frozenset[str]


_USERBOT_SESSION_KEY_PREFIX = "account_bot:interaction_session:"
_USERBOT_SESSION_TTL_GRACE_SECONDS = 90
_USERBOT_BUTTON_MAP_KEY = "_tp_button_map"
_SESSION_CHANNEL_USERBOT = "userbot"
_SESSION_CHANNEL_INTERACTION_BOT = "interaction_bot"
_SESSION_CHANNELS_OBSERVED_BY_USERBOT = frozenset(
    {_SESSION_CHANNEL_USERBOT, _SESSION_CHANNEL_INTERACTION_BOT}
)


class _TracePluginClient:
    """Trace-aware facade for legacy plugin code that still calls ctx.client.

    The facade preserves the familiar Telethon-ish surface for installed and
    built-in plugins, while ensuring user-visible Telegram operations still
    produce event_action rows. Read-only methods fall through to the wrapped
    client via __getattr__.
    """

    def __init__(
        self,
        client: Any,
        trace: Any,
        *,
        plugin_key: str,
        entry_key: str | None = None,
        component: str = "plugin_client",
        account_id: int | None = None,
    ) -> None:
        self._client = client
        self._trace = trace
        self._plugin_key = plugin_key
        self._entry_key = entry_key
        self._component = component
        self._account_id = _int_or_none(account_id if account_id is not None else getattr(trace, "account_id", None))
        self._telepilot_trace_client = True
        try:
            self._allowed = object.__getattribute__(client, "_allowed")
            self._perms = object.__getattribute__(client, "_perms")
        except Exception:  # noqa: BLE001
            self._allowed = frozenset()
            self._perms = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _consume_rate_limit_detail(self) -> dict[str, Any] | None:
        try:
            detail = object.__getattribute__(self._client, "_last_rate_limit_detail")
            object.__setattr__(self._client, "_last_rate_limit_detail", None)
        except Exception:  # noqa: BLE001
            return None
        return detail if isinstance(detail, dict) else None

    def _dry_run_enabled(self) -> bool:
        if self._account_id is None:
            return False
        state = _STATES.get(self._account_id)
        if state is None:
            return False
        return _plugin_dev_mode_dry_run_enabled(state.contexts.get(self._plugin_key))

    def _dry_run_object(self, action: dict[str, Any]) -> Any:
        return SimpleNamespace(
            id=None,
            message_id=None,
            chat_id=_int_or_none(action.get("chat_id")),
            dry_run=True,
        )

    async def send_message(self, chat_id: Any = None, message: Any = None, *args: Any, **kwargs: Any) -> Any:
        if chat_id is None and "entity" in kwargs:
            chat_id = kwargs.pop("entity")
        text = message if message is not None else kwargs.pop("message", None)
        action = self._action(
            "send_message",
            text=text,
            chat_id=chat_id,
            reply_to_message_id=kwargs.get("reply_to") or kwargs.get("reply_to_message_id"),
        )
        if self._dry_run_enabled():
            result = {"dry_run": True, "chat_id": _int_or_none(chat_id), "text": str(text or "")[:4000]}
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=result,
                _tap_status=ACTION_EVENT_STATUS_DRY_RUN,
            )
            return self._dry_run_object(action)
        try:
            result = await self._client.send_message(chat_id, text, *args, **kwargs)
            rate_limit_detail = self._consume_rate_limit_detail()
            if rate_limit_detail is not None:
                await self._record(
                    action,
                    TRACE_STATUS_SKIPPED,
                    actual_send_via="userbot_reply",
                    error_code="rate_limited",
                    result=rate_limit_detail,
                )
                return result
            await self._record(action, TRACE_STATUS_OK, actual_send_via="userbot_reply", result=_trace_client_result(result))
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

    async def send_file(self, chat_id: Any = None, file: Any = None, *args: Any, **kwargs: Any) -> Any:
        if chat_id is None and "entity" in kwargs:
            chat_id = kwargs.pop("entity")
        action = self._action(
            "send_file",
            text=kwargs.get("caption"),
            chat_id=chat_id,
            reply_to_message_id=kwargs.get("reply_to") or kwargs.get("reply_to_message_id"),
        )
        action["filename"] = str(getattr(file, "name", "") or "") or None
        if self._dry_run_enabled():
            result = {
                "dry_run": True,
                "chat_id": _int_or_none(chat_id),
                "filename": action.get("filename"),
                "caption": kwargs.get("caption"),
            }
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=result,
                _tap_status=ACTION_EVENT_STATUS_DRY_RUN,
            )
            return self._dry_run_object(action)
        try:
            result = await self._client.send_file(chat_id, file, *args, **kwargs)
            rate_limit_detail = self._consume_rate_limit_detail()
            if rate_limit_detail is not None:
                await self._record(
                    action,
                    TRACE_STATUS_SKIPPED,
                    actual_send_via="userbot_reply",
                    error_code="rate_limited",
                    result=rate_limit_detail,
                )
                return result
            await self._record(action, TRACE_STATUS_OK, actual_send_via="userbot_reply", result=_trace_client_result(result))
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

    async def edit_message(self, entity: Any = None, message: Any = None, text: Any = None, *args: Any, **kwargs: Any) -> Any:
        action = self._action(
            "edit_message",
            text=text if text is not None else kwargs.get("text"),
            chat_id=entity,
            message_id=message,
        )
        if self._dry_run_enabled():
            result = {
                "dry_run": True,
                "chat_id": _int_or_none(entity),
                "message_id": _int_or_none(message),
                "text": str((text if text is not None else kwargs.get("text")) or "")[:4000],
            }
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=result,
                _tap_status=ACTION_EVENT_STATUS_DRY_RUN,
            )
            return self._dry_run_object(action)
        try:
            result = await self._client.edit_message(entity, message, text, *args, **kwargs)
            await self._record(action, TRACE_STATUS_OK, actual_send_via="userbot_reply", result=_trace_client_result(result))
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
        action = self._action(
            "delete_message",
            chat_id=entity,
            message_id=message_id if not isinstance(message_id, (list, tuple, set)) else None,
        )
        if self._dry_run_enabled():
            result = {
                "dry_run": True,
                "chat_id": _int_or_none(entity),
                "message_id": _int_or_none(action.get("message_id")),
            }
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=result,
                _tap_status=ACTION_EVENT_STATUS_DRY_RUN,
            )
            return []
        try:
            result = await self._client.delete_messages(entity, message_ids, *args, **kwargs)
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=_trace_client_result(result),
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
        action = self._action("pin_message", chat_id=entity, message_id=message)
        if self._dry_run_enabled():
            result = {
                "dry_run": True,
                "chat_id": _int_or_none(entity),
                "message_id": _int_or_none(message),
            }
            await self._record(
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result=result,
                _tap_status=ACTION_EVENT_STATUS_DRY_RUN,
            )
            return self._dry_run_object(action)
        try:
            result = await self._client.pin_message(entity, message, *args, **kwargs)
            await self._record(action, TRACE_STATUS_OK, actual_send_via="userbot_reply", result=_trace_client_result(result))
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

    def _action(
        self,
        action_type: str,
        *,
        text: Any = None,
        chat_id: Any = None,
        message_id: Any = None,
        reply_to_message_id: Any = None,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "type": action_type,
            "send_via": "userbot_reply",
            "send_method": f"ctx.client.{action_type}",
            "context": trace_log_context(self._trace, plugin_key=self._plugin_key, entry_key=self._entry_key),
        }
        if text not in (None, ""):
            action["text"] = str(text)[:4000]
        if chat_id not in (None, ""):
            action["chat_id"] = chat_id
        if message_id not in (None, ""):
            action["message_id"] = message_id
        if reply_to_message_id not in (None, ""):
            action["reply_to_message_id"] = reply_to_message_id
        return action

    async def _record(self, action: dict[str, Any], status: str, **detail: Any) -> None:
        tap_status_override = detail.pop("_tap_status", None)
        detail.setdefault("plugin_key", self._plugin_key)
        if self._entry_key:
            detail.setdefault("entry_key", self._entry_key)
        detail.setdefault("component", self._component)
        await record_action(action.get("context"), action, status, **detail)
        tap_status = tap_status_override or (
            ACTION_EVENT_STATUS_OK if status == TRACE_STATUS_OK
            else ACTION_EVENT_STATUS_FAILED if status == TRACE_STATUS_FAILED
            else None
        )
        if tap_status is not None:
            await emit_action_event(
                account_id=self._account_id,
                action=action,
                status=tap_status,
                channel=detail.get("actual_send_via"),
                error_code=detail.get("error_code"),
                error=detail.get("error"),
                result=detail.get("result"),
            )


def _trace_client_result(result: Any) -> dict[str, Any]:
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


def _trace_plugin_client(
    client: Any,
    trace: Any,
    *,
    plugin_key: str,
    entry_key: str | None = None,
    component: str = "plugin_client",
    account_id: int | None = None,
) -> Any:
    if client is None or trace is None:
        return client
    try:
        if getattr(client, "_telepilot_trace_client", False) is True:
            return client
    except Exception:  # noqa: BLE001
        pass
    return _TracePluginClient(
        client,
        trace,
        plugin_key=plugin_key,
        entry_key=entry_key,
        component=component,
        account_id=account_id,
    )


def _configured_interaction_bot_sender_ids(cfg: dict[str, Any]) -> frozenset[int]:
    ids: set[int] = set()
    for key in ("interaction_bot_id", "trusted_bot_id", "transfer_bot_id"):
        value = _int_or_none(cfg.get(key))
        if value is not None:
            ids.add(value)
    trusted = cfg.get("trusted_bot_ids")
    if isinstance(trusted, list):
        for item in trusted:
            value = _int_or_none(item)
            if value is not None:
                ids.add(value)
    return frozenset(ids)


# 不在每次消息都查 DB；启动 + reload 时刷新一次，足够快
async def _load_log_incoming_messages_setting() -> bool:
    """从 ``system_setting`` 读取「是否记录每条 incoming 消息」开关。

    支持两种存储格式（兼容前端不同 toggle 实现）：
      - ``{"enabled": true}``
      - ``{"value": true}``
      - ``true`` / ``false`` 直接 JSON 布尔
    缺失或异常一律按 ``app_settings.log_incoming_messages_default`` 处理（默认 False）。
    """
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "log_incoming_messages")
        if row is None:
            return bool(app_settings.log_incoming_messages_default)
        v = row.value
        if isinstance(v, dict):
            return bool(v.get("enabled", v.get("value", app_settings.log_incoming_messages_default)))
        if isinstance(v, bool):
            return v
        return bool(app_settings.log_incoming_messages_default)
    except Exception:  # noqa: BLE001
        return bool(app_settings.log_incoming_messages_default)


_EVENT_FRAMEWORK_FLAGS_CACHE: tuple[float, dict[str, bool]] = (0.0, {})


async def _load_event_framework_flags() -> dict[str, bool]:
    """Read Trace/Event Bus runtime switches for the UserBot plugin dispatcher."""

    global _EVENT_FRAMEWORK_FLAGS_CACHE
    now = time.monotonic()
    cached_at, cached = _EVENT_FRAMEWORK_FLAGS_CACHE
    if cached and now - cached_at < 30:
        return cached
    defaults = {
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
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
        }
    except Exception:  # noqa: BLE001
        log.debug("load plugin dispatcher event framework flags failed, using defaults", exc_info=True)
        flags = defaults
    _EVENT_FRAMEWORK_FLAGS_CACHE = (now, flags)
    return flags


def _event_bus_subscriptions_from_state(state: _AccountState) -> list[Any]:
    subscriptions: list[Any] = []
    for plugin_key, inst in list(state.instances.items()):
        manifest = getattr(type(inst), "_manifest", None)
        raw_subscriptions = getattr(manifest, "event_subscriptions", None)
        if not isinstance(raw_subscriptions, list):
            continue
        for raw in raw_subscriptions:
            if isinstance(raw, dict):
                subscriptions.append(
                    normalize_event_subscription(
                        raw,
                        plugin_key=plugin_key,
                    )
                )
    return subscriptions


def _event_bus_state_for_userbot_dispatch(state: _AccountState) -> dict[str, Any]:
    sudo_ids = set(state.sudo_users)
    owner_ids = [state.owner_tg_user_id] if state.owner_tg_user_id is not None else []
    allowed_chat_ids: list[int] | str = sorted(state.ignored_peers) if state.ignored_peers else "*"
    return {
        "allowed_chat_ids": allowed_chat_ids,
        "owner_user_ids": owner_ids,
        "admin_user_ids": sorted(set(owner_ids) | sudo_ids),
        "known_user_ids": sorted(set(owner_ids) | sudo_ids),
    }


async def _start_userbot_message_trace(
    state: _AccountState,
    event: Any,
    *,
    event_label: str,
) -> _UserbotEventBusDispatch:
    event_payload = normalize_userbot_event(state.account_id, event)
    flags = await _load_event_framework_flags()
    trace = None
    if flags.get("trace_enabled", True):
        trace = await start_trace(event_payload)
        event_payload["trace_id"] = trace.trace_id
        await record_span(
            trace,
            "receive",
            TRACE_STATUS_OK,
            component="userbot_message",
            direction=event_label,
        )
    if not flags.get("event_bus_delivery_enabled", True):
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="event_bus_delivery_disabled",
            message="Event Bus 新投递路径已通过运行设置关闭，legacy on_message 继续执行。",
        )
        return _UserbotEventBusDispatch(trace=trace, event_payload=event_payload)

    subscriptions = _event_bus_subscriptions_from_state(state)
    subscribed_plugin_keys = frozenset(
        str(getattr(item, "plugin_key", "") or "").strip()
        for item in subscriptions
        if str(getattr(item, "plugin_key", "") or "").strip()
    )
    if not subscriptions:
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="subscription_not_matched",
            message="没有已启用插件声明 UserBot Event Bus 订阅。",
        )
        return _UserbotEventBusDispatch(
            trace=trace,
            event_payload=event_payload,
            event_bus_enabled=True,
            subscribed_plugin_keys=subscribed_plugin_keys,
        )
    result = dispatch_event(event_payload, subscriptions, _event_bus_state_for_userbot_dispatch(state))
    matched_decisions: list[Any] = []
    for decision in result.decisions:
        if decision.matched:
            matched_decisions.append(decision)
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_OK if decision.matched else TRACE_STATUS_SKIPPED,
            component="event_bus",
            plugin_key=decision.plugin_key,
            entry_key=decision.entry_key,
            reason_code=decision.reason_code,
            message=decision.reason_message,
            dispatch_mode=decision.dispatch_mode,
            scope=decision.scope,
            filters=decision.filters,
        )
    if not matched_decisions:
        await record_span(
            trace,
            "route",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="subscription_not_matched",
            message="Event Bus 未命中新入口；legacy on_message 继续执行。",
        )
    return _UserbotEventBusDispatch(
        trace=trace,
        event_payload=event_payload,
        event_bus_enabled=True,
        matched_decisions=tuple(matched_decisions),
        subscribed_plugin_keys=subscribed_plugin_keys,
    )


async def _dispatch_userbot_event_bus_matches(
    state: _AccountState,
    dispatch: _UserbotEventBusDispatch,
    event: Any,
    *,
    event_label: str,
    redis: Any,
) -> tuple[int, int, frozenset[str]]:
    if not dispatch.event_bus_enabled or not dispatch.matched_decisions:
        return 0, 0, frozenset()
    invoked_count = 0
    failed_count = 0
    consumed_plugin_keys: set[str] = set()
    trace = dispatch.trace
    for decision in dispatch.matched_decisions:
        plugin_key = str(getattr(decision, "plugin_key", "") or "").strip()
        entry_key = str(getattr(decision, "entry_key", "") or "").strip()
        if not plugin_key:
            continue
        inst = state.instances.get(plugin_key)
        ctx = state.contexts.get(plugin_key)
        if inst is None or ctx is None or ctx.generation != state.generation:
            failed_count += 1
            await record_span(
                trace,
                "plugin_load",
                TRACE_STATUS_FAILED,
                component="userbot_event_bus_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                reason_code="plugin_load_failed",
                message="插件未加载、未启用或上下文 generation 已过期。",
            )
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED,
                last_trace_id=getattr(trace, "trace_id", None),
            )
            continue
        has_event_handler = _plugin_overrides(inst, "on_event")
        has_interaction_handler = _plugin_overrides(inst, "on_interaction")
        if not entry_key and not has_event_handler:
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_SKIPPED,
                component="userbot_event_bus_dispatcher",
                plugin_key=plugin_key,
                reason_code="entry_key_missing",
                message="Event Bus 订阅缺少 entry_key，且插件未实现 on_event；legacy on_message 将继续尝试处理。",
            )
            continue
        if not has_event_handler and not has_interaction_handler:
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_SKIPPED,
                component="userbot_event_bus_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                reason_code="handler_error",
                message="插件未实现 on_event 或 on_interaction 入口；legacy on_message 将继续尝试处理。",
            )
            continue
        consumed_plugin_keys.add(plugin_key)
        payload = _userbot_event_payload_for_plugin(dispatch.event_payload, inst, plugin_key)
        payload.setdefault("trigger", {})
        if isinstance(payload["trigger"], dict):
            payload["trigger"].update(
                {
                    "dispatch_mode": getattr(decision, "dispatch_mode", None),
                    "entry_key": entry_key,
                    "event_label": event_label,
                    "scope": getattr(decision, "scope", None),
                }
            )
        started = time.monotonic()
        try:
            actions = await _invoke_userbot_event_bus_entry(
                inst,
                ctx,
                plugin_key=plugin_key,
                entry_key=entry_key,
                payload=payload,
            )
            invoked_count += 1
            duration_ms = int((time.monotonic() - started) * 1000)
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_OK,
                component="userbot_event_bus_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                direction=event_label,
                duration_ms=duration_ms,
            )
            await record_span(
                trace,
                "plugin_return",
                TRACE_STATUS_OK,
                component="userbot_event_bus_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                action_count=len(actions),
            )
            action_failed = await _apply_userbot_event_bus_actions(
                state,
                trace,
                event,
                plugin_key=plugin_key,
                entry_key=entry_key,
                actions=actions,
                redis=redis,
            )
            if action_failed:
                failed_count += 1
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED if action_failed else TRACE_STATUS_OK,
                last_trace_id=getattr(trace, "trace_id", None),
            )
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_FAILED,
                component="userbot_event_bus_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                direction=event_label,
                reason_code="plugin_runtime_error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED,
                last_trace_id=getattr(trace, "trace_id", None),
            )
            await _log(
                redis,
                state.account_id,
                "error",
                f"插件 {plugin_key} 处理 Event Bus {event_label} 事件时出错：{type(exc).__name__}: {exc}。",
                source="plugin",
                plugin_key=plugin_key,
                entry_key=entry_key,
                direction=event_label,
                chat_id=getattr(event, "chat_id", None),
                sender_id=getattr(event, "sender_id", None),
                message_preview=(getattr(event, "raw_text", "") or "")[:200],
                traceback=traceback.format_exc(limit=8),
                **trace_log_context(trace),
            )
    return invoked_count, failed_count, frozenset(consumed_plugin_keys)


async def dispatch_webhook_event(
    account_id: int,
    payload: dict[str, Any] | None,
    *,
    redis: Any | None = None,
) -> dict[str, Any]:
    """Deliver a normalized inbound webhook to plugins that subscribe to webhook events."""

    state = _STATES.get(account_id)
    if state is None:
        raise RuntimeError("账号 worker state 不存在")
    redis = redis or state.redis or get_redis()
    data = payload if isinstance(payload, dict) else {}
    hook_key = str(data.get("hook_key") or "").strip()
    event_payload = normalize_webhook_event(
        account_id,
        hook_key=hook_key,
        body=data.get("body"),
        headers=data.get("headers") if isinstance(data.get("headers"), dict) else {},
        body_size=_int_or_none(data.get("body_size")),
        content_type=str(data.get("content_type") or "") or None,
        received_at=str(data.get("received_at") or "") or None,
    )
    flags = await _load_event_framework_flags()
    trace = None
    if flags.get("trace_enabled", True):
        trace = await start_trace(event_payload)
        event_payload["trace_id"] = trace.trace_id
        await record_span(
            trace,
            "receive",
            TRACE_STATUS_OK,
            component="webhook",
            direction="webhook",
        )
    if not flags.get("event_bus_delivery_enabled", True):
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="event_bus_delivery_disabled",
            message="Event Bus 新投递路径已通过运行设置关闭，webhook 事件未投递。",
        )
        await finish_trace(trace, TRACE_STATUS_SKIPPED, invoked_count=0, failed_count=0, direction="webhook")
        return {"ok": True, "matched": 0, "invoked": 0, "failed": 0, "trace_id": getattr(trace, "trace_id", None)}

    subscriptions = _event_bus_subscriptions_from_state(state)
    matched_decisions: list[Any] = []
    if not subscriptions:
        await record_span(
            trace,
            "subscription_match",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="subscription_not_matched",
            message="没有已启用插件声明 webhook Event Bus 订阅。",
        )
    else:
        result = dispatch_event(event_payload, subscriptions, _event_bus_state_for_userbot_dispatch(state))
        for decision in result.decisions:
            if decision.matched:
                matched_decisions.append(decision)
            await record_span(
                trace,
                "subscription_match",
                TRACE_STATUS_OK if decision.matched else TRACE_STATUS_SKIPPED,
                component="event_bus",
                plugin_key=decision.plugin_key,
                entry_key=decision.entry_key,
                reason_code=decision.reason_code,
                message=decision.reason_message,
                dispatch_mode=decision.dispatch_mode,
                scope=decision.scope,
                filters=decision.filters,
            )
    if not matched_decisions:
        await record_span(
            trace,
            "route",
            TRACE_STATUS_SKIPPED,
            component="event_bus",
            reason_code="subscription_not_matched",
            message="webhook 事件未命中任何插件订阅。",
        )

    synthetic_event = SimpleNamespace(chat_id=None, sender_id=None, raw_text="")
    dispatch_state = _UserbotEventBusDispatch(
        trace=trace,
        event_payload=event_payload,
        event_bus_enabled=True,
        matched_decisions=tuple(matched_decisions),
    )
    invoked_count, failed_count, _consumed = await _dispatch_userbot_event_bus_matches(
        state,
        dispatch_state,
        synthetic_event,
        event_label="webhook",
        redis=redis,
    )
    final_status = (
        TRACE_STATUS_FAILED if failed_count
        else TRACE_STATUS_OK if invoked_count
        else TRACE_STATUS_SKIPPED
    )
    await finish_trace(
        trace,
        final_status,
        invoked_count=invoked_count,
        failed_count=failed_count,
        direction="webhook",
    )
    return {
        "ok": failed_count == 0,
        "matched": len(matched_decisions),
        "invoked": invoked_count,
        "failed": failed_count,
        "trace_id": getattr(trace, "trace_id", None),
    }


def evaluate_dispatch(
    account: _AccountState | int | None = None,
    chat: dict[str, Any] | None = None,
    text: str = "",
    via: str = "userbot",
    *,
    state: _AccountState | None = None,
    direction: str | None = None,
    edited: bool = False,
) -> dict[str, Any]:
    """Evaluate userbot dispatch decisions without side effects.

    The function reads in-memory worker state only. It does not start traces,
    call plugin handlers, touch Redis, or mutate dispatch caches.
    """

    resolved_state = state or (account if isinstance(account, _AccountState) else None)
    account_id = (
        resolved_state.account_id
        if resolved_state is not None
        else _int_or_none(account)
    )
    chat_ctx = dict(chat or {})
    chat_id = _int_or_none(chat_ctx.get("chat_id") or chat_ctx.get("id"))
    sender_id = _int_or_none(chat_ctx.get("sender_id") or chat_ctx.get("user_id"))
    direction_value = str(direction or chat_ctx.get("direction") or "incoming").strip() or "incoming"
    via_value = str(via or chat_ctx.get("via") or "userbot").strip() or "userbot"
    clean_text = str(text or chat_ctx.get("text") or "").strip()
    trace = {
        "account_id": account_id,
        "via": via_value,
        "chat": {
            "chat_id": chat_id,
            "sender_id": sender_id,
            "direction": direction_value,
            "edited": bool(edited),
        },
        "text": clean_text,
        "stages": [],
    }
    if resolved_state is None:
        trace["stages"] = [
            _dispatch_stage("direct_passthrough", False, "plugin_load_failed", "账号 worker state 不存在。"),
            _dispatch_stage("prefix_command", False, "command_not_matched", "账号 worker state 不存在。"),
            _dispatch_stage("keyword", False, "filter_not_matched", "账号 worker state 不存在。"),
            _dispatch_stage("event_subscription", False, "subscription_not_matched", "账号 worker state 不存在。"),
        ]
        return trace

    trace["stages"].append(
        _evaluate_direct_passthrough_stage(
            resolved_state,
            via=via_value,
            direction=direction_value,
            edited=bool(edited),
            sender_id=sender_id,
        )
    )
    trace["stages"].append(
        _evaluate_prefix_command_stage(
            resolved_state,
            text=clean_text,
            direction=direction_value,
            sender_id=sender_id,
        )
    )
    trace["stages"].append(
        _evaluate_keyword_stage(
            resolved_state,
            text=clean_text,
            chat_id=chat_id,
        )
    )
    trace["stages"].append(
        _evaluate_event_subscription_stage(
            resolved_state,
            text=clean_text,
            via=via_value,
            chat_id=chat_id,
            sender_id=sender_id,
            edited=bool(edited),
        )
    )
    return trace


def _dispatch_stage(
    stage: str,
    matched: bool,
    reason_code: str,
    message: str,
    **detail: Any,
) -> dict[str, Any]:
    out = {
        "stage": stage,
        "matched": bool(matched),
        "reason_code": reason_code,
        "message": message,
    }
    out.update(detail)
    return out


def _evaluate_direct_passthrough_stage(
    state: _AccountState,
    *,
    via: str,
    direction: str,
    edited: bool,
    sender_id: int | None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if via != "userbot":
        return _dispatch_stage(
            "direct_passthrough",
            False,
            "source_not_subscribed",
            "直通只适用于 userbot 来源。",
            matches=candidates,
        )
    for plugin_key, inst in list(state.instances.items()):
        ctx = state.contexts.get(plugin_key)
        detail = {"plugin_key": plugin_key}
        if ctx is None or ctx.generation != state.generation:
            detail.update({"matched": False, "reason_code": "plugin_load_failed"})
        elif not _plugin_direct_passthrough_enabled(ctx):
            detail.update({"matched": False, "reason_code": "plugin_disabled"})
        elif not _plugin_declares_direct_passthrough(inst, source="userbot", direction=direction, edited=edited):
            detail.update({"matched": False, "reason_code": "source_not_subscribed"})
        elif getattr(inst, "on_direct_message", None) is None or getattr(type(inst), "on_direct_message", None) is getattr(Plugin, "on_direct_message", None):
            detail.update({"matched": False, "reason_code": "handler_error"})
        elif getattr(inst, "owner_only", True) and not _owner_gate_allows(state, direction=direction, sender_id=sender_id):
            detail.update({"matched": False, "reason_code": "permission_denied"})
        else:
            detail.update({"matched": True, "reason_code": "matched"})
        candidates.append(detail)
    matches = [item for item in candidates if item.get("matched")]
    return _dispatch_stage(
        "direct_passthrough",
        bool(matches),
        "matched" if matches else "filter_not_matched",
        "直通插件命中。" if matches else "没有插件通过直通声明、账号配置和权限检查。",
        matches=matches,
        candidates=candidates,
    )


def _evaluate_prefix_command_stage(
    state: _AccountState,
    *,
    text: str,
    direction: str,
    sender_id: int | None,
) -> dict[str, Any]:
    prefix = current_command_prefix(fallback=app_settings.command_prefix or ",")
    if not prefix or not text.startswith(prefix):
        return _dispatch_stage("prefix_command", False, "command_not_matched", "文本不是当前 userbot 前缀命令。")
    command_text = text[len(prefix):].strip()
    command = command_text.split(maxsplit=1)[0].lstrip("/") if command_text else ""
    if not command:
        return _dispatch_stage("prefix_command", False, "command_not_matched", "前缀后缺少命令名。")
    if direction != "outgoing":
        return _dispatch_stage(
            "prefix_command",
            False,
            "command_not_matched",
            "userbot 前缀命令只在账号 outgoing 消息中触发。",
            command=command,
            prefix=prefix,
        )
    matches: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for plugin_key, inst in list(state.instances.items()):
        ctx = state.contexts.get(plugin_key)
        if ctx is None or ctx.generation != state.generation:
            continue
        for item in _plugin_command_candidates(plugin_key, inst, ctx):
            if item["command"] != command:
                candidates.append({**item, "matched": False, "reason_code": "command_not_matched"})
                continue
            if getattr(inst, "owner_only", True) and not _owner_gate_allows(state, direction=direction, sender_id=sender_id):
                candidates.append({**item, "matched": False, "reason_code": "permission_denied"})
                continue
            matched = {**item, "matched": True, "reason_code": "command_matched"}
            candidates.append(matched)
            matches.append(matched)
    return _dispatch_stage(
        "prefix_command",
        bool(matches),
        "command_matched" if matches else "command_not_matched",
        "前缀命令命中。" if matches else "没有已加载插件声明该命令。",
        command=command,
        prefix=prefix,
        matches=matches,
        candidates=candidates,
    )


def _plugin_command_candidates(plugin_key: str, inst: Plugin, ctx: PluginContext) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    commands = getattr(inst, "commands", None) or getattr(type(inst), "commands", None) or {}
    if isinstance(commands, dict):
        for command in sorted(str(key or "").strip().lstrip("/") for key in commands):
            if command:
                candidates.append({"plugin_key": plugin_key, "command": command, "source": "plugin_commands"})
    manifest = getattr(type(inst), "_manifest", None)
    for raw_entry in getattr(manifest, "interaction_entries", None) or []:
        entry = account_bot_service.normalize_interaction_entry_manifest(raw_entry)
        if not entry:
            continue
        command = _entry_command_trigger(entry)
        if not command:
            continue
        if _entry_trigger_mode(ctx.config if isinstance(ctx.config, dict) else {}, entry, command) == "keyword_only":
            continue
        candidates.append(
            {
                "plugin_key": plugin_key,
                "entry_key": str(entry.get("key") or "").strip() or None,
                "command": command,
                "source": "manifest_interaction_entry",
            }
        )
    return candidates


def _evaluate_keyword_stage(
    state: _AccountState,
    *,
    text: str,
    chat_id: int | None,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for rule in getattr(state, "interaction_text_guard_rules", ()) or ():
        rule_match = bool(text and text in rule.texts and _interaction_text_guard_rule_matches(rule, chat_id))
        detail = {
            "source": "interaction_text_guard",
            "keywords": sorted(rule.texts),
            "matched": rule_match,
            "reason_code": "interaction_rule_owned" if rule_match else "filter_not_matched",
        }
        candidates.append(detail)
        if rule_match:
            matches.append(detail)
    for plugin_key, inst in list(state.instances.items()):
        ctx = state.contexts.get(plugin_key)
        if ctx is None or ctx.generation != state.generation:
            continue
        manifest = getattr(type(inst), "_manifest", None)
        for raw_entry in getattr(manifest, "interaction_entries", None) or []:
            entry = account_bot_service.normalize_interaction_entry_manifest(raw_entry)
            if not entry:
                continue
            keywords = _entry_keyword_triggers(entry)
            if not keywords:
                continue
            matched = text in keywords
            detail = {
                "source": "manifest_interaction_entry",
                "plugin_key": plugin_key,
                "entry_key": str(entry.get("key") or "").strip() or None,
                "keywords": sorted(keywords),
                "matched": matched,
                "reason_code": "matched" if matched else "filter_not_matched",
            }
            candidates.append(detail)
            if matched:
                matches.append(detail)
    return _dispatch_stage(
        "keyword",
        bool(matches),
        "matched" if matches else "filter_not_matched",
        "关键词命中。" if matches else "没有关键词规则命中。",
        matches=matches,
        candidates=candidates,
    )


def _entry_keyword_triggers(entry: dict[str, Any]) -> set[str]:
    triggers = entry.get("triggers") if isinstance(entry.get("triggers"), dict) else {}
    raw_values = [
        entry.get("keyword"),
        entry.get("keywords"),
        entry.get("trigger_text"),
        entry.get("trigger_texts"),
        entry.get("module_start_keywords"),
        triggers.get("keyword"),
        triggers.get("keywords"),
        triggers.get("text"),
        triggers.get("texts"),
    ]
    out: set[str] = set()
    for raw in raw_values:
        if isinstance(raw, list):
            values = raw
        else:
            values = [raw]
        for item in values:
            value = str(item or "").strip()
            if value:
                out.add(value)
    return out


def _evaluate_event_subscription_stage(
    state: _AccountState,
    *,
    text: str,
    via: str,
    chat_id: int | None,
    sender_id: int | None,
    edited: bool,
) -> dict[str, Any]:
    subscriptions = _event_bus_subscriptions_from_state(state)
    if not subscriptions:
        return _dispatch_stage(
            "event_subscription",
            False,
            "subscription_not_matched",
            "没有已启用插件声明 Event Bus 订阅。",
            decisions=[],
        )
    event = SimpleNamespace(
        chat_id=chat_id,
        sender_id=sender_id,
        id=None,
        raw_text=text,
        text=text,
        message=SimpleNamespace(chat_id=chat_id, sender_id=sender_id, id=None, text=text),
    )
    payload = normalize_userbot_event(state.account_id, event)
    source = dict(payload.get("source") if isinstance(payload.get("source"), dict) else {})
    source["channel"] = via
    source["type"] = "message_edited" if edited else "message"
    payload["source"] = source
    payload["event_type"] = source["type"]
    result = dispatch_event(payload, subscriptions, _event_bus_state_for_userbot_dispatch(state))
    decisions = [
        {
            "plugin_key": decision.plugin_key,
            "entry_key": decision.entry_key,
            "matched": bool(decision.matched),
            "reason_code": decision.reason_code,
            "message": decision.reason_message,
            "dispatch_mode": decision.dispatch_mode,
            "scope": decision.scope,
            "filters": decision.filters,
        }
        for decision in result.decisions
    ]
    matches = [item for item in decisions if item.get("matched")]
    return _dispatch_stage(
        "event_subscription",
        bool(matches),
        "matched" if matches else "subscription_not_matched",
        "Event Bus 订阅命中。" if matches else "Event Bus 订阅未命中。",
        decisions=decisions,
        matches=matches,
    )


def _owner_gate_allows(state: _AccountState, *, direction: str, sender_id: int | None) -> bool:
    if direction == "outgoing":
        return True
    if sender_id is None:
        return False
    if state.owner_tg_user_id is not None and sender_id == state.owner_tg_user_id:
        return True
    return sender_id in state.sudo_users


def _plugin_overrides(inst: Plugin, method_name: str) -> bool:
    handler = getattr(inst, method_name, None)
    base_handler = getattr(Plugin, method_name, None)
    return callable(handler) and getattr(type(inst), method_name, None) is not base_handler


async def _invoke_userbot_event_bus_entry(
    inst: Plugin,
    ctx: PluginContext,
    *,
    plugin_key: str,
    entry_key: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    base_log = ctx.log
    base_client = ctx.client
    state = _STATES.get(ctx.account_id)
    if state is not None:
        buffered_messages = _InteractionEntryMessageOps(
            state,
            plugin_key=plugin_key,
            entry_key=entry_key,
            trace=payload.get("trace_id"),
            default_send_via=["userbot_reply"],
        )
    else:
        buffered_messages = BufferedMessageOps()
    call_client = _trace_plugin_client(
        base_client,
        payload.get("trace_id"),
        plugin_key=plugin_key,
        entry_key=entry_key,
        component="userbot_event_bus_dispatcher",
        account_id=ctx.account_id,
    )
    trace_id = str(payload.get("trace_id") or "").strip()
    call_log = base_log
    if base_log is not None and trace_id:
        async def _trace_log(level: str, message: str, **detail: Any) -> None:
            detail.setdefault("trace_id", trace_id)
            detail.setdefault("plugin_key", plugin_key)
            if entry_key:
                detail.setdefault("entry_key", entry_key)
            await base_log(level, message, **detail)

        call_log = _trace_log
    call_ctx = replace(ctx, messages=buffered_messages, client=call_client, log=call_log)
    plugin_payload = attach_tp_event(dict(payload))
    if _plugin_overrides(inst, "on_event"):
        actions = await inst.on_event(call_ctx, plugin_payload)
    elif _plugin_overrides(inst, "on_interaction"):
        actions = await inst.on_interaction(call_ctx, entry_key, plugin_payload)
    else:
        raise RuntimeError("插件未实现 on_event 或 on_interaction 入口")
    if actions is None:
        actions = []
    if not isinstance(actions, list) or not all(isinstance(item, dict) for item in actions):
        raise TypeError("Event Bus 入口必须返回 list[dict] 标准动作")
    return _normalize_interaction_actions([*buffered_messages.actions, *actions], default_send_via=["userbot_reply"])


def _userbot_event_payload_for_plugin(
    event_payload: dict[str, Any],
    inst: Plugin,
    plugin_key: str,
) -> dict[str, Any]:
    payload = dict(event_payload or {})
    trace_id = str(payload.get("trace_id") or "").strip()
    if trace_id:
        payload["trace_id"] = trace_id
    native_raw = payload.get("native_raw")
    allowed = _plugin_declares_native_raw(inst, source="userbot")
    payload["native_raw_meta"] = _userbot_native_raw_meta(native_raw, enabled=allowed)
    payload["native_raw"] = native_raw if allowed else None
    if not allowed:
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
        raw = dict(raw)
        raw["native_raw"] = "[omitted]"
        raw["native_raw_reason_code"] = "native_raw_not_allowed"
        payload["raw"] = raw
    payload.setdefault("source", {})
    if isinstance(payload["source"], dict):
        payload["source"].setdefault("plugin_key", plugin_key)
    return payload


def _plugin_declares_native_raw(inst: Plugin, *, source: str) -> bool:
    manifest = getattr(type(inst), "_manifest", None)
    capabilities = getattr(manifest, "capabilities", None)
    if not isinstance(capabilities, dict):
        return False
    raw = capabilities.get("telegram_native_raw")
    if not isinstance(raw, dict) or not bool(raw.get("enabled")):
        return False
    sources = raw.get("sources")
    if isinstance(sources, list) and sources:
        allowed_sources = {str(item or "").strip() for item in sources}
        return source in allowed_sources or "all" in allowed_sources
    return True


def _plugin_declares_direct_passthrough(
    inst: Plugin,
    *,
    source: str,
    direction: str,
    edited: bool,
) -> bool:
    manifest = getattr(type(inst), "_manifest", None)
    capabilities = getattr(manifest, "capabilities", None)
    if not isinstance(capabilities, dict):
        return False
    raw = capabilities.get("telegram_direct_passthrough")
    if not isinstance(raw, dict) or not bool(raw.get("enabled")):
        return False

    sources = raw.get("sources")
    if isinstance(sources, list) and sources:
        allowed_sources = {str(item or "").strip() for item in sources}
        if source not in allowed_sources and "all" not in allowed_sources:
            return False

    directions = raw.get("directions")
    if isinstance(directions, list) and directions:
        allowed_directions = {str(item or "").strip() for item in directions}
        if direction not in allowed_directions and "all" not in allowed_directions:
            return False

    if edited and not bool(raw.get("include_edited", False)):
        return False
    return True


def _plugin_direct_passthrough_enabled(ctx: PluginContext | None) -> bool:
    if ctx is None or ctx.generation <= 0:
        return False
    cfg = ctx.account_config if isinstance(ctx.account_config, dict) else {}
    raw = cfg.get("direct_passthrough")
    if isinstance(raw, dict):
        return raw.get("enabled") is True
    return False


def _plugin_dev_mode_dry_run_enabled(ctx: PluginContext | None) -> bool:
    if ctx is None or ctx.generation <= 0:
        return False
    cfg = ctx.account_config if isinstance(ctx.account_config, dict) else {}
    return dev_mode_dry_run_enabled(cfg)


def _recording_account_config(state: _AccountState) -> dict[str, Any] | None:
    for ctx in state.contexts.values():
        cfg = ctx.account_config if isinstance(ctx.account_config, dict) else {}
        if dev_mode_recording_enabled(cfg):
            return cfg
    return None


def _action_dev_mode_dry_run_enabled(state: _AccountState, action: dict[str, Any]) -> bool:
    if action_context_dry_run_enabled(action):
        return True
    context = action.get("context")
    plugin_key = str((context or {}).get("plugin_key") or "").strip() if isinstance(context, dict) else ""
    if not plugin_key:
        return False
    return _plugin_dev_mode_dry_run_enabled(state.contexts.get(plugin_key))


async def _emit_userbot_action_tap(
    state: _AccountState,
    action: dict[str, Any],
    status: str,
    *,
    channel: str | None = None,
    error_code: str | None = None,
    error: Any = None,
    result: Any = None,
) -> None:
    await emit_action_event(
        account_id=state.account_id,
        action=action,
        status=status,
        channel=channel,
        error_code=error_code,
        error=error,
        result=result,
        redis=state.redis,
    )


async def _record_userbot_dry_run(
    state: _AccountState,
    action: dict[str, Any],
    *,
    channel: str,
    result: dict[str, Any],
) -> None:
    await record_action(
        action.get("context"),
        action,
        TRACE_STATUS_OK,
        actual_send_via=channel,
        result=result,
    )
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_DRY_RUN,
        channel=channel,
        result=result,
    )


async def _start_userbot_direct_passthrough_trace(
    state: _AccountState,
    event: Any,
    *,
    event_label: str,
) -> Any | None:
    flags = await _load_event_framework_flags()
    if not flags.get("trace_enabled", True):
        return None
    payload = normalize_userbot_event(state.account_id, event)
    trigger = dict(payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {})
    trigger.update(
        {
            "type": "direct_passthrough",
            "channel": "userbot",
            "event_label": event_label,
        }
    )
    payload["trigger"] = trigger
    trace = await start_trace(payload)
    payload["trace_id"] = trace.trace_id
    try:
        event.trace_id = trace.trace_id
    except Exception:  # noqa: BLE001
        pass
    await record_span(
        trace,
        "receive",
        TRACE_STATUS_OK,
        component="userbot_direct_passthrough",
        direction=event_label,
    )
    return trace


async def _dispatch_userbot_direct_passthrough(
    state: _AccountState,
    event: Any,
    *,
    direction: str,
    edited: bool,
    event_label: str,
    redis: Any,
) -> bool:
    """Dispatch raw Telethon events to explicitly opted-in low-latency plugins."""

    invoked = False
    invoked_count = 0
    failed_count = 0
    trace = None
    trace_started = False
    handler_name = "on_direct_message"
    for plugin_key, inst in list(state.instances.items()):
        ctx = state.contexts.get(plugin_key)
        if ctx is None or ctx.generation != state.generation:
            continue
        if not _plugin_direct_passthrough_enabled(ctx):
            continue
        if not _plugin_declares_direct_passthrough(
            inst,
            source="userbot",
            direction=direction,
            edited=edited,
        ):
            continue
        handler = getattr(inst, handler_name, None)
        if handler is None or getattr(type(inst), handler_name, None) is getattr(Plugin, handler_name, None):
            continue
        if getattr(inst, "owner_only", True):
            allowed = await _event_allowed_for_owner_only(state, event)
            if not allowed:
                continue
        invoked = True
        invoked_count += 1
        if not trace_started:
            trace = await _start_userbot_direct_passthrough_trace(
                state,
                event,
                event_label=event_label,
            )
            trace_started = True
        await record_span(
            trace,
            "route",
            TRACE_STATUS_OK,
            component="userbot_direct_passthrough",
            plugin_key=plugin_key,
            reason_code="matched",
            message="插件启用直通模式，原始 Telethon event 将直接下放给 on_direct_message。",
            direction=event_label,
        )
        started = time.monotonic()
        # 直通也做调用级 ctx 隔离：复制一个独立 messages facade，避免并发直通事件
        # 共享常驻 ctx.messages（其 actions 列表会交叉累积/泄漏）。成本仅一次对象构造。
        call_client = ctx.client
        if trace is not None:
            call_client = _trace_plugin_client(
                ctx.client,
                trace,
                plugin_key=plugin_key,
                component="userbot_direct_passthrough",
                account_id=ctx.account_id,
            )
        call_log = ctx.log
        if ctx.log is not None and trace is not None:
            async def _trace_log(
                level: str,
                message: str,
                *,
                _plugin_key: str = plugin_key,
                _trace: Any = trace,
                _base_log: Any = ctx.log,
                **detail: Any,
            ) -> None:
                detail.setdefault("trace_id", getattr(_trace, "trace_id", None))
                detail.setdefault("plugin_key", _plugin_key)
                await _base_log(level, message, **detail)

            call_log = _trace_log
        base_messages = ctx.messages
        if isinstance(base_messages, _LiveMessageOps):
            call_messages = _LiveMessageOps(
                base_messages._state,  # noqa: SLF001
                plugin_key=base_messages._plugin_key,  # noqa: SLF001
                entry_key=base_messages._entry_key,  # noqa: SLF001
                trace=trace,  # noqa: SLF001
                default_send_via=list(base_messages._default_send_via),  # noqa: SLF001
            )
            call_ctx = replace(ctx, client=call_client, log=call_log, messages=call_messages)
        else:
            call_ctx = replace(ctx, client=call_client, log=call_log)
        try:
            await handler(call_ctx, event)
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_OK,
                component="userbot_direct_passthrough",
                plugin_key=plugin_key,
                direction=event_label,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_OK,
                last_trace_id=getattr(trace, "trace_id", None),
            )
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_FAILED,
                component="userbot_direct_passthrough",
                plugin_key=plugin_key,
                direction=event_label,
                reason_code="plugin_runtime_error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED,
                last_trace_id=getattr(trace, "trace_id", None),
            )
            await _log(
                redis,
                state.account_id,
                "error",
                (
                    f"插件 {plugin_key} 处理直通 {event_label} 消息时出错："
                    f"{type(exc).__name__}: {exc}。本条消息不会继续进入普通消息链路。"
                ),
                source="plugin",
                plugin_key=plugin_key,
                direction=event_label,
                chat_id=getattr(event, "chat_id", None),
                sender_id=getattr(event, "sender_id", None),
                message_preview=(getattr(event, "raw_text", "") or "")[:200],
                duration_ms=int((time.monotonic() - started) * 1000),
                traceback=traceback.format_exc(limit=8),
                **trace_log_context(trace),
            )
    if invoked:
        await finish_trace(
            trace,
            TRACE_STATUS_FAILED if failed_count else TRACE_STATUS_OK,
            invoked_count=invoked_count,
            failed_count=failed_count,
            direction=event_label,
            consumed=True,
        )
    return invoked


def _userbot_native_raw_meta(native_raw: Any, *, enabled: bool) -> dict[str, Any]:
    size = 0
    try:
        size = len(json.dumps(native_raw, ensure_ascii=False, default=str).encode("utf-8")) if native_raw is not None else 0
    except (TypeError, ValueError):
        size = len(str(native_raw).encode("utf-8")) if native_raw is not None else 0
    return {
        "enabled": bool(enabled),
        "source": "userbot",
        "driver": "telethon",
        "object": "message",
        "stored_in_trace": False,
        "size_bytes": size,
        "reason_code": None if enabled else "native_raw_not_allowed",
    }


async def _apply_userbot_event_bus_actions(
    state: _AccountState,
    trace: Any | None,
    event: Any,
    *,
    plugin_key: str,
    entry_key: str,
    actions: list[dict[str, Any]],
    redis: Any,
    session_key: str | None = None,
    session: dict[str, Any] | None = None,
    synthetic_callback: bool = False,
) -> bool:
    failed = False
    if len(actions) > 10:
        dropped_actions = actions[10:]
        await _log(
            redis,
            state.account_id,
            "warn",
            "Event Bus actions truncated by worker limit",
            source="plugin",
            plugin_key=plugin_key,
            entry_key=entry_key,
            reason_code="action_limit_exceeded",
            kept_count=10,
            dropped_count=len(dropped_actions),
            dropped_action_types=[str((item or {}).get("type") or "") for item in dropped_actions if isinstance(item, dict)],
            **trace_log_context(trace),
        )
        for raw_action in dropped_actions:
            action = dict(raw_action) if isinstance(raw_action, dict) else {"type": "unknown"}
            action.setdefault(
                "context",
                trace_log_context(trace, plugin_key=plugin_key, entry_key=entry_key),
            )
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_FAILED,
                error_code="action_limit_exceeded",
                error="worker event bus only executes the first 10 actions",
            )
    for raw_action in actions[:10]:
        action = dict(raw_action)
        action.setdefault(
            "context",
            trace_log_context(trace, plugin_key=plugin_key, entry_key=entry_key),
        )
        action_type = str(action.get("type") or "").strip()
        if action_type == "result":
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_SKIPPED,
                error_code="session_control_action",
                error=f"session control action: {action_type}",
            )
            continue
        if action_type in {"end_session", "close_session", "no_session"}:
            deleted = False
            if session_key:
                delete = getattr(redis, "delete", None)
                if callable(delete):
                    deleted = bool(await delete(session_key))
                chat_id = _int_or_none((session or {}).get("chat_id"))
                if chat_id is not None:
                    await _refresh_userbot_session_chat_cache(state)
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_SKIPPED,
                error_code="session_control_action",
                error=f"session control action: {action_type}",
                result={"session_key": session_key, "deleted": deleted} if session_key else None,
            )
            continue
        if action_type == "start_session":
            failed = not await _apply_userbot_start_session_action(
                state,
                action,
                plugin_key=plugin_key,
                entry_key=entry_key,
                redis=redis,
            ) or failed
            continue
        if action_type == "settlement":
            await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="settlement")
            continue
        if action_type == "update_session":
            failed = not await _apply_userbot_update_session_action(
                state,
                action,
                redis=redis,
                session_key=session_key,
                session=session,
            ) or failed
            continue
        if action_type == "payout":
            failed = not await _apply_userbot_payout_action(state, event, action) or failed
            continue
        if action_type in {"send_message", "send_photo", "send_file", "edit_message", "edit_caption", "delete_message", "pin_message"}:
            deprecated_channels = deprecated_send_via_values(action_send_via_raw_selector(action))
            if deprecated_channels:
                failed = True
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_FAILED,
                    error_code=SEND_CHANNEL_DEPRECATED_REASON_CODE,
                    error="notice/bbot_notice/notice_bot channel is deprecated",
                    deprecated_send_via=deprecated_channels,
                )
                await _log(
                    redis,
                    state.account_id,
                    "warn",
                    "Event Bus action failed: deprecated send_via",
                    source="plugin",
                    plugin_key=plugin_key,
                    entry_key=entry_key,
                    reason_code=SEND_CHANNEL_DEPRECATED_REASON_CODE,
                    action_type=action_type,
                    deprecated_send_via=deprecated_channels,
                    **trace_log_context(trace),
                )
                continue
        if action_type == "send_message":
            failed = not await _apply_userbot_send_message_action(
                state,
                event,
                action,
                redis=redis,
                session_key=session_key,
                session=session,
            ) or failed
            continue
        if action_type == "edit_message":
            failed = not await _apply_userbot_edit_message_action(state, event, action) or failed
            continue
        if action_type == "edit_caption":
            failed = not await _apply_userbot_edit_caption_action(state, event, action) or failed
            continue
        if action_type in {"send_photo", "send_file"}:
            failed = not await _apply_userbot_send_media_action(state, event, action) or failed
            continue
        if action_type == "delete_message":
            failed = not await _apply_userbot_delete_message_action(state, event, action) or failed
            continue
        if action_type == "pin_message":
            failed = not await _apply_userbot_pin_message_action(state, event, action) or failed
            continue
        if action_type == "answer_callback":
            if synthetic_callback:
                action["_tp_synthetic_callback"] = True
            failed = not await _apply_userbot_answer_callback_action(state, action) or failed
            continue
        if action_type == "answer_inline_query":
            failed = not await _apply_userbot_answer_inline_query_action(state, action) or failed
            continue
        failed = True
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_SKIPPED,
            error_code="unsupported_send_via",
            error=f"unsupported action type: {action_type}",
        )
    return failed


async def _apply_userbot_start_session_action(
    state: _AccountState,
    action: dict[str, Any],
    *,
    plugin_key: str,
    entry_key: str,
    redis: Any,
) -> bool:
    target_chat_id = _int_or_none(action.get("chat_id"))
    if target_chat_id is None:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="scope_not_matched", error="target chat_id missing")
        return False
    target_entry_key = str(action.get("entry_key") or entry_key or "").strip()
    if not target_entry_key:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="entry_key_missing", error="interaction session entry_key missing")
        return False
    rule = await _find_interaction_rule_for_plugin_session(
        state.account_id,
        plugin_key=plugin_key,
        entry_key=target_entry_key,
        chat_id=target_chat_id,
    )
    if rule is None:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="interaction_rule_missing",
            error=f"no interaction rule for {plugin_key}.{target_entry_key}",
        )
        return False
    try:
        from ...services import account_bot_runtime as account_bot_runtime_service

        started_by_user_id = _int_or_none(action.get("started_by_user_id"))
        session_key = account_bot_runtime_service._interaction_session_key(  # noqa: SLF001
            state.account_id,
            rule,
            target_chat_id,
            started_by_user_id,
        )
        existing: dict[str, Any] = {}
        raw = await redis.get(session_key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                existing = parsed
        paid_ids = _int_list(existing.get("paid_user_ids") or existing.get("participant_user_ids"))
        paid_ids.update(_int_list(action.get("paid_user_ids") or action.get("participant_user_ids")))
        if started_by_user_id is None:
            started_by_user_id = _int_or_none(existing.get("started_by_user_id"))
        ttl = _int_or_none(action.get("ttl_seconds")) or account_bot_runtime_service._interaction_session_ttl(rule)  # noqa: SLF001
        expires_at = time.time() + ttl
        payload = {
            "account_id": state.account_id,
            "chat_id": target_chat_id,
            "rule_id": str(rule.get("id") or "legacy"),
            "rule_name": str(rule.get("name") or ""),
            "module_key": plugin_key,
            "entry_key": target_entry_key,
            "channel": "userbot",
            "started_by_user_id": started_by_user_id,
            "started_by_message_id": _int_or_none(action.get("started_by_message_id")),
            "source_user_id": started_by_user_id,
            "event_type": str(action.get("event_type") or "command"),
            "created_at": existing.get("created_at") or time.time(),
            "updated_at": time.time(),
            "expires_at": expires_at,
            "data": {
                **dict(existing.get("data") if isinstance(existing.get("data"), dict) else {}),
                **dict(action.get("data") if isinstance(action.get("data"), dict) else {}),
            },
        }
        policy = account_bot_runtime_service._interaction_participant_policy(rule)  # noqa: SLF001
        if policy == "paid_pool":
            payload["paid_user_ids"] = sorted(paid_ids)
            payload["participant_user_ids"] = sorted(paid_ids)
        await redis.set(session_key, json.dumps(payload, ensure_ascii=False), ex=ttl + 90)
        state.userbot_session_chats.add(target_chat_id)
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK,
            actual_send_via="interaction_session",
            result={"session_key": session_key, "ttl_seconds": ttl, "channel": "userbot", "expires_at": expires_at},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="interaction_session_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False


def _userbot_session_redis_ttl(session: dict[str, Any], *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    expires_at = session.get("expires_at")
    try:
        remaining = float(expires_at) - now if expires_at is not None else 600
    except (TypeError, ValueError):
        remaining = 600
    return max(1, int(remaining) + _USERBOT_SESSION_TTL_GRACE_SECONDS)


async def _write_userbot_session(redis: Any, key: str, session: dict[str, Any]) -> None:
    await redis.set(
        key,
        json.dumps(session, ensure_ascii=False),
        ex=_userbot_session_redis_ttl(session),
    )


async def _apply_userbot_update_session_action(
    state: _AccountState,
    action: dict[str, Any],
    *,
    redis: Any,
    session_key: str | None = None,
    session: dict[str, Any] | None = None,
) -> bool:
    from ...services.interaction.session_store import (
        SessionNotFoundError,
        SessionRevisionConflictError,
        SessionUpdateError,
        update_interaction_session,
    )

    key = str(action.get("session_key") or session_key or "").strip()
    if not key:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="session_not_found", error="update_session missing session_key")
        return False
    # Channel gate still uses the caller's in-memory snapshot when present so
    # unobserved sessions fail before the Redis CAS write.
    preview = dict(session or {})
    if not preview:
        preview = _json_dict(await redis.get(key))
    session_channel = _session_channel(preview)
    if session_channel not in _SESSION_CHANNELS_OBSERVED_BY_USERBOT:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="session_not_found", error="userbot-observed session not found")
        return False
    patch_data = action.get("data")
    if not isinstance(patch_data, dict):
        patch_data = {}
    extend_seconds = _int_or_none(action.get("extend_seconds"))
    expected_revision = _int_or_none(action.get("expected_revision"))
    try:
        updated = await update_interaction_session(
            redis,
            key,
            data=patch_data,
            extend_seconds=extend_seconds,
            expected_revision=expected_revision,
            grace_seconds=_USERBOT_SESSION_TTL_GRACE_SECONDS,
        )
        current = updated.session
        chat_id = _int_or_none(current.get("chat_id"))
        if chat_id is not None:
            state.userbot_session_chats.add(chat_id)
        if session is not None:
            session.clear()
            session.update(current)
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK,
            actual_send_via="interaction_session",
            result={
                "session_key": key,
                "channel": session_channel,
                "data_keys": sorted(updated.data.keys()),
                "expires_at": updated.expires_at,
                "revision": updated.revision,
            },
        )
        return True
    except SessionRevisionConflictError as exc:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="session_revision_conflict",
            error=str(exc),
        )
        return False
    except SessionNotFoundError as exc:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="session_not_found",
            error=str(exc),
        )
        return False
    except SessionUpdateError as exc:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="interaction_session_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    except Exception as exc:  # noqa: BLE001
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="redis_error", error=f"{type(exc).__name__}: {exc}")
        return False


def _int_list(raw: Any) -> set[int]:
    out: set[int] = set()
    if not isinstance(raw, (list, tuple, set)):
        return out
    for item in raw:
        value = _int_or_none(item)
        if value is not None:
            out.add(value)
    return out


async def _find_interaction_rule_for_plugin_session(
    account_id: int,
    *,
    plugin_key: str,
    entry_key: str,
    chat_id: int,
) -> dict[str, Any] | None:
    try:
        async with AsyncSessionLocal() as db:
            cfg = await account_bot_service.get_transfer_notice_config(db, account_id)
    except Exception:  # noqa: BLE001
        log.debug("load interaction rules for plugin session failed account=%s plugin=%s", account_id, plugin_key, exc_info=True)
        return None
    rules = cfg.get("rules") if isinstance(cfg, dict) else None
    if not isinstance(rules, list):
        return None
    for item in rules:
        if not isinstance(item, dict):
            continue
        if str(item.get("action") or "") != "module":
            continue
        if str(item.get("module_key") or "").strip() != plugin_key:
            continue
        if str(item.get("module_action") or "").strip() != entry_key:
            continue
        chat_ids = item.get("chat_ids")
        if isinstance(chat_ids, list) and chat_ids:
            allowed = {_int_or_none(raw) for raw in chat_ids}
            if chat_id not in allowed:
                continue
        return dict(item)
    return None


def _userbot_rate_limit_action(action_type: str, chat_id: int | None) -> str:
    if action_type in {"send_message", "payout"}:
        return "send_message_private" if chat_id is not None and chat_id > 0 else "send_message_group"
    if action_type in {"edit_message", "edit_caption"}:
        return "edit_message"
    if action_type in {"send_photo", "send_file"}:
        return "upload_file"
    return action_type


async def _acquire_userbot_action_rate_limit(
    state: _AccountState,
    action: dict[str, Any],
    *,
    action_type: str,
    target_chat_id: int | None,
) -> bool:
    limit_action = _userbot_rate_limit_action(action_type, target_chat_id)
    engine = getattr(state, "engine", None)
    redis = state.redis or get_redis()

    async def _best_effort_log(message: str, **detail: Any) -> None:
        try:
            await _log(
                redis,
                state.account_id,
                "warn",
                message,
                source="plugin",
                action_type=action_type,
                rate_limit_action=limit_action,
                chat_id=target_chat_id,
                **detail,
            )
        except Exception:  # noqa: BLE001
            log.debug("userbot rate limit log failed account=%s", state.account_id, exc_info=True)

    if engine is None:
        # 与 command/runtime 对齐：引擎缺失时走本地降级桶，不再 fail-open 直发。
        from ..command import acquire_userbot_action_rate_limit as _cmd_acquire

        allowed, detail = await _cmd_acquire(state.account_id, action_type, target_chat_id)
        await _best_effort_log(
            "UserBot 交互动作未接入限速引擎，已走本地降级限流。",
            rate_limit_backend=detail.get("rate_limit_backend"),
        )
        return bool(allowed)
    try:
        decision = await engine.acquire(state.account_id, limit_action, peer_id=target_chat_id)
    except Exception as exc:  # noqa: BLE001
        from ..command import acquire_userbot_action_rate_limit as _cmd_acquire

        allowed, detail = await _cmd_acquire(state.account_id, action_type, target_chat_id)
        await _best_effort_log(
            f"UserBot 交互动作限速检查失败，已走本地降级限流：{type(exc).__name__}: {exc}",
            rate_limit_backend=detail.get("rate_limit_backend"),
        )
        return bool(allowed)
    if not bool(getattr(decision, "allowed", False)):
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_SKIPPED,
            actual_send_via="userbot_reply",
            error_code="rate_limited",
            error=str(getattr(decision, "reason", None) or getattr(decision, "outcome", "") or "rate limited"),
            result={
                "outcome": getattr(decision, "outcome", None),
                "wait_seconds": getattr(decision, "wait_seconds", None),
                "rate_limit_action": limit_action,
            },
        )
        return False
    wait_seconds = float(getattr(decision, "wait_seconds", 0.0) or 0.0)
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    return True


def _has_userbot_session_context(session_key: str | None, session: dict[str, Any] | None) -> bool:
    return bool(session_key) or isinstance(session, dict)


def _is_settlement_send_action(action: dict[str, Any]) -> bool:
    return isinstance(action.get("settlement"), dict)


async def _simulate_userbot_session_reply_humanize(state: _AccountState, peer: Any) -> None:
    engine = getattr(state, "engine", None)
    opts = getattr(engine, "humanize", None) if engine is not None else None
    client = getattr(state, "client", None)
    if opts is None or client is None:
        return
    try:
        if bool(getattr(opts, "read_before_reply", False)):
            await simulate_read(client, peer, opts)
        if bool(getattr(opts, "typing_simulate", False)):
            await simulate_typing(client, peer, opts)
    except Exception:  # noqa: BLE001
        log.debug("userbot session humanize simulation failed; continue sending", exc_info=True)


def _payout_amount(action: dict[str, Any]) -> int | None:
    amount = _int_or_none(action.get("amount"))
    if amount is None or amount <= 0:
        return None
    return amount


async def _record_userbot_action_failure(
    state: _AccountState,
    action: dict[str, Any],
    *,
    error_code: str,
    error: str,
    target_chat_id: int | None,
    reply_to_message_id: int | None = None,
    reply_to_user_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _userbot_action_failure_result(
        action,
        target_chat_id=target_chat_id,
        error_code=error_code,
        error=error,
        reply_to_message_id=reply_to_message_id,
        reply_to_user_id=reply_to_user_id,
        extra=extra,
    )
    await record_action(
        action.get("context"),
        action,
        TRACE_STATUS_FAILED,
        actual_send_via="userbot_reply",
        error_code=error_code,
        error=error,
        result=result,
    )
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel="userbot_reply",
        error_code=error_code,
        error=error,
        result=result,
    )
    await _log_userbot_action_failure(
        state,
        action,
        result=result,
        message=f"userbot {str(action.get('type') or '').strip() or 'action'} action failed",
    )
    return result


def _userbot_action_failure_result(
    action: dict[str, Any],
    *,
    target_chat_id: int | None,
    error_code: str,
    error: str,
    reply_to_message_id: int | None = None,
    reply_to_user_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _userbot_action_context(
        action,
        target_chat_id=target_chat_id,
        reply_to_message_id=reply_to_message_id,
        reply_to_user_id=reply_to_user_id,
    )
    result["error"] = error
    result["error_code"] = error_code
    result["worker_offline"] = error_code == "userbot_offline"
    result["reply_anchor_missing"] = error_code == "reply_anchor_missing"
    if extra:
        result.update(extra)
    return result


def _userbot_action_context(
    action: dict[str, Any],
    *,
    target_chat_id: int | None,
    reply_to_message_id: int | None = None,
    reply_to_user_id: int | None = None,
) -> dict[str, Any]:
    return {
        "chat_id": target_chat_id,
        "amount": _int_or_none(action.get("amount")),
        "reply_to_message_id": reply_to_message_id
        if reply_to_message_id is not None
        else _int_or_none(action.get("reply_to_message_id")),
        "reply_to_user_id": reply_to_user_id if reply_to_user_id is not None else _int_or_none(action.get("reply_to_user_id")),
        "reply_to_search_limit": _recent_user_message_search_limit(action.get("reply_to_search_limit")),
    }


async def _log_userbot_action_failure(
    state: _AccountState,
    action: dict[str, Any],
    *,
    result: dict[str, Any],
    message: str,
) -> None:
    await _log(
        state.redis or get_redis(),
        state.account_id,
        "warn",
        message,
        source="event",
        action_type=str(action.get("type") or ""),
        **_userbot_action_log_detail(result),
    )


def _userbot_action_log_detail(result: dict[str, Any]) -> dict[str, Any]:
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


async def _feed_engine_flood_wait(
    state: _AccountState,
    exc: Exception,
    *,
    action_type: str,
    target_chat_id: int | None,
) -> dict[str, Any]:
    """把 FloodWait/PeerFlood 异常喂给限速引擎自动降级，返回可并入失败 detail 的补充字段。

    - ``engine`` 可能为 None（未接入限速引擎），此时只回补 detail、不调钩子。
    - 引擎钩子内部虽有 try/except，但为避免其偶发异常打断失败上报路径，这里再兜一层。
    - PeerFloodError 按引擎既有约定统一停用陌生人私聊（``dm_stranger``），与
      ``ratelimit/engine.py`` 自动装饰器一致，不按本次动作粒度停用。
    """
    engine = getattr(state, "engine", None)
    if isinstance(exc, FloodWaitError):
        wait_seconds = int(getattr(exc, "seconds", 0) or 0)
        if engine is not None:
            try:
                await engine.on_flood_wait(_userbot_rate_limit_action(action_type, target_chat_id), exc)
            except Exception:  # noqa: BLE001
                log.debug("on_flood_wait 调用失败 account=%s", state.account_id, exc_info=True)
        return {"flood_wait_seconds": wait_seconds}
    if isinstance(exc, PeerFloodError):
        if engine is not None:
            try:
                await engine.on_peer_flood("dm_stranger")
            except Exception:  # noqa: BLE001
                log.debug("on_peer_flood 调用失败 account=%s", state.account_id, exc_info=True)
        return {"peer_flood": True}
    return {}


def _render_userbot_button_fallback(text: str, reply_markup: dict[str, Any] | None) -> tuple[str, dict[str, str]]:
    if not isinstance(reply_markup, dict):
        return text, {}
    rows = reply_markup.get("inline_keyboard")
    if not isinstance(rows, list):
        return text, {}
    lines: list[str] = []
    mapping: dict[str, str] = {}
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
            callback_data = raw_button.get("callback_data")
            url = str(raw_button.get("url") or "").strip()
            display = f"{label}: {url}" if url else label
            lines.append(f"{index}. {display}")
            if callback_data not in (None, ""):
                data = str(callback_data)
                mapping[str(index)] = data
                mapping[label] = data
            index += 1
    if not lines:
        return text, {}
    return f"{text}\n\n请回复序号选择：\n" + "\n".join(lines), mapping


async def _save_userbot_button_map(
    *,
    redis: Any,
    session_key: str | None,
    session: dict[str, Any] | None,
    message_id: int | None,
    mapping: dict[str, str],
) -> None:
    if not session_key or session is None or not mapping:
        return
    data = dict(session.get("data") if isinstance(session.get("data"), dict) else {})
    data[_USERBOT_BUTTON_MAP_KEY] = {
        "message_id": message_id,
        "map": dict(mapping),
        "updated_at": time.time(),
    }
    session["data"] = data
    session["updated_at"] = time.time()
    await _write_userbot_session(redis, session_key, session)


async def _apply_userbot_payout_action(state: _AccountState, event: Any, action: dict[str, Any]) -> bool:
    target_chat_id = _action_chat_id(action, event)
    amount = _payout_amount(action)
    if amount is None:
        await _record_userbot_action_failure(
            state,
            action,
            error_code="invalid_payout_amount",
            error="payout amount must be a positive integer",
            target_chat_id=target_chat_id,
        )
        return False
    if target_chat_id is None:
        await _record_userbot_action_failure(
            state,
            action,
            error_code="scope_not_matched",
            error="target chat_id missing",
            target_chat_id=target_chat_id,
        )
        return False
    payout_key = payout_compensation.ensure_payout_key(action)
    text = str(action.get("text") or f"+{amount}").strip()
    if not text:
        await _record_userbot_action_failure(
            state,
            action,
            error_code="empty_message_text",
            error="payout text is empty",
            target_chat_id=target_chat_id,
        )
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        await _record_userbot_dry_run(
            state,
            action,
            channel="userbot_reply",
            result={
                "dry_run": True,
                "chat_id": target_chat_id,
                "amount": str(amount),
                "text": text,
                "payout_key": payout_key,
            },
        )
        return True

    async def _enqueue_compensation(
        error_code: str,
        error: Any,
        *,
        reply_to_message_id: int | None = None,
        reply_to_user_id: int | None = None,
    ) -> None:
        payload = dict(action)
        payload["payout_key"] = payout_key
        payload["chat_id"] = target_chat_id
        payload["amount"] = amount
        payload["text"] = text
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_to_user_id is not None:
            payload["reply_to_user_id"] = reply_to_user_id
        try:
            await payout_compensation.enqueue_payout_compensation(
                account_id=state.account_id,
                origin="worker",
                payload=payload,
                error_code=error_code,
                error=error,
                chat_id=target_chat_id,
                amount=amount,
            )
        except Exception:  # noqa: BLE001
            log.debug("payout compensation enqueue failed in worker plugin", exc_info=True)

    if state.client is None:
        await _record_userbot_action_failure(
            state,
            action,
            error_code="userbot_offline",
            error="userbot client unavailable",
            target_chat_id=target_chat_id,
        )
        await _enqueue_compensation("userbot_offline", "userbot client unavailable")
        return False
    reply_to = _int_or_none(action.get("reply_to_message_id"))
    reply_to_user_id = _int_or_none(action.get("reply_to_user_id"))
    try:
        reply_to, reply_to_user_id = await _resolve_userbot_reply_to_message_id(state, action, target_chat_id)
    except ValueError as exc:
        reply_to_user_id = _int_or_none(action.get("reply_to_user_id"))
        await _record_userbot_action_failure(
            state,
            action,
            error_code="reply_anchor_missing",
            error=str(exc),
            target_chat_id=target_chat_id,
            reply_to_message_id=reply_to,
            reply_to_user_id=reply_to_user_id,
        )
        await _send_reply_anchor_missing_notice(
            state,
            action,
            target_chat_id,
            reply_to_user_id=reply_to_user_id,
        )
        return False
    parse_mode = _action_parse_mode(action)
    if not await _acquire_userbot_action_rate_limit(
        state,
        action,
        action_type="payout",
        target_chat_id=target_chat_id,
    ):
        await _enqueue_compensation(
            "rate_limited",
            "rate_limited",
            reply_to_message_id=reply_to,
            reply_to_user_id=reply_to_user_id,
        )
        return False
    payout_ok, payout_reason = await payout_limit.check_and_consume(
        state.account_id,
        amount,
        idempotency_key=payout_key,
    )
    if not payout_ok:
        await _record_userbot_action_failure(
            state,
            action,
            error_code="payout_limit_exceeded",
            error=payout_reason or "payout 超过限额",
            target_chat_id=target_chat_id,
            reply_to_message_id=reply_to,
            reply_to_user_id=reply_to_user_id,
        )
        return False
    # Telegram 发送与本地落标/记账分离：成功发送后绝不因本地异常进入可盲重发补偿。
    try:
        sent = await state.client.send_message(
            target_chat_id,
            text,
            reply_to=reply_to,
            parse_mode=_telethon_parse_mode(parse_mode),
        )
    except Exception as exc:  # noqa: BLE001
        flood_detail = await _feed_engine_flood_wait(
            state,
            exc,
            action_type="payout",
            target_chat_id=target_chat_id,
        )
        await _record_userbot_action_failure(
            state,
            action,
            error_code="telegram_api_error",
            error=f"{type(exc).__name__}: {exc}",
            target_chat_id=target_chat_id,
            reply_to_message_id=reply_to,
            reply_to_user_id=reply_to_user_id,
            extra=flood_detail,
        )
        await _enqueue_compensation(
            "telegram_api_error",
            exc,
            reply_to_message_id=reply_to,
            reply_to_user_id=reply_to_user_id,
        )
        return False

    result = {
        "message_id": getattr(sent, "id", None),
        "chat_id": target_chat_id,
        "reply_to_message_id": reply_to,
        "reply_to_user_id": reply_to_user_id,
        "payout_key": payout_key,
    }
    await payout_compensation.mark_payout_sent_marker(
        state.redis or get_redis(),
        state.account_id,
        payout_key,
        result.get("message_id"),
    )
    try:
        await _save_action_message_id(state, action, result)
        await _save_userbot_reply_target(
            state,
            target_chat_id=target_chat_id,
            result=result,
            reply_to_user_id=reply_to_user_id,
        )
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_OK,
            actual_send_via="userbot_reply",
            result=result,
        )
        await _emit_userbot_action_tap(
            state,
            action,
            ACTION_EVENT_STATUS_OK,
            channel="userbot_reply",
            result=result,
        )
        return True
    except Exception as post_exc:  # noqa: BLE001
        log.warning(
            "payout post-send bookkeeping failed account=%s payout_key=%s error=%s",
            state.account_id,
            payout_key,
            post_exc,
            exc_info=True,
        )
        # 消息已发出：挂歧义单供人工/probe，禁止进入可盲重发路径。
        await _enqueue_compensation(
            payout_compensation.ERROR_AMBIGUOUS_DELIVERY,
            post_exc,
            reply_to_message_id=reply_to,
            reply_to_user_id=reply_to_user_id,
        )
        try:
            await _emit_userbot_action_tap(
                state,
                action,
                ACTION_EVENT_STATUS_OK,
                channel="userbot_reply",
                result={**result, "post_send_bookkeeping_failed": True},
            )
        except Exception:  # noqa: BLE001
            log.debug("payout post-send tap failed", exc_info=True)
        return True


async def _apply_userbot_send_message_action(
    state: _AccountState,
    event: Any,
    action: dict[str, Any],
    *,
    redis: Any | None = None,
    session_key: str | None = None,
    session: dict[str, Any] | None = None,
) -> bool:
    text = str(action.get("text") or "").strip()
    if not text:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="empty_message_text", error="send_message text is empty")
        return False
    target_chat_id = _action_chat_id(action, event)
    if target_chat_id is None:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="scope_not_matched", error="target chat_id missing")
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        options = action_send_via_options(action)
        await _record_userbot_dry_run(
            state,
            action,
            channel=options[0] if options else "userbot_reply",
            result={
                "dry_run": True,
                "chat_id": target_chat_id,
                "text": text,
                "send_via_options": options,
            },
        )
        return True
    reply_to = _int_or_none(action.get("reply_to_message_id"))
    reply_markup = action.get("reply_markup") if isinstance(action.get("reply_markup"), dict) else None
    parse_mode = _action_parse_mode(action)
    last_code = "unsupported_send_via"
    last_error = "no supported send_via"
    last_result = _userbot_action_failure_result(action, target_chat_id=target_chat_id, error_code=last_code, error=last_error)
    for send_via in action_send_via_options(action):
        if send_via == "interaction_bot":
            token = await _interaction_bot_token_for_account(state.account_id)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                last_result = _userbot_action_failure_result(
                    action,
                    target_chat_id=target_chat_id,
                    error_code=last_code,
                    error=last_error,
                    reply_to_message_id=reply_to,
                )
                continue
            try:
                result = await account_bot_service.send_message(
                    token,
                    target_chat_id,
                    text,
                    reply_to_message_id=reply_to,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                await _save_action_message_id(state, action, result)
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot", result=result)
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="interaction_bot",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                last_result = _userbot_action_failure_result(
                    action,
                    target_chat_id=target_chat_id,
                    error_code=last_code,
                    error=last_error,
                    reply_to_message_id=reply_to,
                )
                continue
        if send_via == "userbot_reply":
            if state.client is None:
                last_code = "userbot_offline"
                last_error = "userbot client unavailable"
                last_result = _userbot_action_failure_result(
                    action,
                    target_chat_id=target_chat_id,
                    error_code=last_code,
                    error=last_error,
                    reply_to_message_id=reply_to,
                )
                continue
            try:
                reply_to, reply_to_user_id = await _resolve_userbot_reply_to_message_id(state, action, target_chat_id)
            except ValueError as exc:
                reply_to_user_id = _int_or_none(action.get("reply_to_user_id"))
                last_code = "reply_anchor_missing"
                last_error = str(exc)
                last_result = _userbot_action_failure_result(
                    action,
                    target_chat_id=target_chat_id,
                    error_code=last_code,
                    error=last_error,
                    reply_to_message_id=reply_to,
                    reply_to_user_id=reply_to_user_id,
                )
                continue
            send_text, button_map = _render_userbot_button_fallback(text, reply_markup)
            if not await _acquire_userbot_action_rate_limit(
                state,
                action,
                action_type="send_message",
                target_chat_id=target_chat_id,
            ):
                return False
            try:
                if _has_userbot_session_context(session_key, session) and not _is_settlement_send_action(action):
                    await _simulate_userbot_session_reply_humanize(state, target_chat_id)
                sent = await state.client.send_message(
                    target_chat_id,
                    send_text,
                    reply_to=reply_to,
                    parse_mode=_telethon_parse_mode(parse_mode),
                )
                result = {
                    "message_id": getattr(sent, "id", None),
                    "chat_id": target_chat_id,
                    "reply_to_message_id": reply_to,
                    "reply_to_user_id": reply_to_user_id,
                }
                await _save_userbot_button_map(
                    redis=redis or state.redis or get_redis(),
                    session_key=session_key,
                    session=session,
                    message_id=_int_or_none(result.get("message_id")),
                    mapping=button_map,
                )
                await _save_action_message_id(state, action, result)
                await _save_userbot_reply_target(
                    state,
                    target_chat_id=target_chat_id,
                    result=result,
                    reply_to_user_id=reply_to_user_id,
                )
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result=result,
                )
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="userbot_reply",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                last_result = _userbot_action_failure_result(
                    action,
                    target_chat_id=target_chat_id,
                    error_code=last_code,
                    error=last_error,
                    reply_to_message_id=reply_to,
                    reply_to_user_id=reply_to_user_id,
                )
                continue
    await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error, result=last_result)
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel=action_send_via_options(action)[0] if action_send_via_options(action) else None,
        error_code=last_code,
        error=last_error,
        result=last_result,
    )
    await _log_userbot_action_failure(state, action, result=last_result, message="userbot send_message action failed")
    if last_code == "reply_anchor_missing":
        await _send_reply_anchor_missing_notice(
            state,
            action,
            target_chat_id,
            reply_to_user_id=_int_or_none(action.get("reply_to_user_id")),
        )
    return False


async def _apply_userbot_edit_message_action(state: _AccountState, event: Any, action: dict[str, Any]) -> bool:
    text = str(action.get("text") or "").strip()
    if not text:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="empty_message_text", error="edit_message text is empty")
        return False
    target_chat_id = _action_chat_id(action, event)
    message_id = _int_or_none(action.get("message_id") or action.get("edit_message_id"))
    if target_chat_id is None or message_id is None:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="target_message_id_missing",
            error="target chat_id or message_id missing",
        )
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        options = action_send_via_options(action)
        await _record_userbot_dry_run(
            state,
            action,
            channel=options[0] if options else "userbot_reply",
            result={
                "dry_run": True,
                "chat_id": target_chat_id,
                "message_id": message_id,
                "text": text,
                "send_via_options": options,
            },
        )
        return True
    reply_markup = action.get("reply_markup") if isinstance(action.get("reply_markup"), dict) else None
    parse_mode = _action_parse_mode(action)
    last_code = "unsupported_send_via"
    last_error = "no supported send_via"
    for send_via in action_send_via_options(action):
        if send_via == "interaction_bot":
            token = await _interaction_bot_token_for_account(state.account_id)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                result = await account_bot_service.edit_message(
                    token,
                    target_chat_id,
                    message_id,
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot", result=result)
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="interaction_bot",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        if send_via == "userbot_reply":
            if state.client is None:
                last_code = "userbot_offline"
                last_error = "userbot client unavailable"
                continue
            if not await _acquire_userbot_action_rate_limit(
                state,
                action,
                action_type="edit_message",
                target_chat_id=target_chat_id,
            ):
                return False
            try:
                result = await state.client.edit_message(
                    target_chat_id,
                    message_id,
                    text,
                    parse_mode=_telethon_parse_mode(parse_mode),
                )
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result={"message_id": getattr(result, "id", None) or message_id, "chat_id": target_chat_id},
                )
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="userbot_reply",
                    result={"message_id": getattr(result, "id", None) or message_id, "chat_id": target_chat_id},
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel=action_send_via_options(action)[0] if action_send_via_options(action) else None,
        error_code=last_code,
        error=last_error,
    )
    return False


async def _apply_userbot_edit_caption_action(state: _AccountState, event: Any, action: dict[str, Any]) -> bool:
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
        return False
    target_chat_id = _action_chat_id(action, event)
    message_id = _int_or_none(action.get("message_id") or action.get("edit_message_id"))
    if message_id is None:
        message_id = await _read_action_message_id(state, action.get("message_id_key"))
    if target_chat_id is None or message_id is None:
        await record_action(
            action.get("context"),
            action,
            TRACE_STATUS_FAILED,
            error_code="target_message_id_missing",
            error="target chat_id or message_id missing",
        )
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        options = action_send_via_options(action)
        await _record_userbot_dry_run(
            state,
            action,
            channel=options[0] if options else "userbot_reply",
            result={
                "dry_run": True,
                "chat_id": target_chat_id,
                "message_id": message_id,
                "caption": caption,
                "send_via_options": options,
            },
        )
        return True
    reply_markup = action.get("reply_markup") if isinstance(action.get("reply_markup"), dict) else None
    parse_mode = _action_parse_mode(action)
    last_code = "unsupported_send_via"
    last_error = "no supported send_via"
    for send_via in action_send_via_options(action):
        if send_via == "interaction_bot":
            token = await _interaction_bot_token_for_account(state.account_id)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                result = await account_bot_service.edit_message_caption(
                    token,
                    target_chat_id,
                    message_id,
                    caption,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot", result=result)
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="interaction_bot",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                if _is_message_not_modified_error(exc):
                    result = {"message_id": message_id, "chat_id": target_chat_id, "not_modified": True}
                    await record_action(
                        action.get("context"),
                        action,
                        TRACE_STATUS_OK,
                        actual_send_via="interaction_bot",
                        result=result,
                    )
                    await _emit_userbot_action_tap(
                        state,
                        action,
                        ACTION_EVENT_STATUS_OK,
                        channel="interaction_bot",
                        result=result,
                    )
                    return True
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        if send_via == "userbot_reply":
            if state.client is None:
                last_code = "userbot_offline"
                last_error = "userbot client unavailable"
                continue
            if not await _acquire_userbot_action_rate_limit(
                state,
                action,
                action_type="edit_caption",
                target_chat_id=target_chat_id,
            ):
                return False
            try:
                result = await state.client.edit_message(
                    target_chat_id,
                    message_id,
                    caption,
                    parse_mode=_telethon_parse_mode(parse_mode),
                )
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result={"message_id": getattr(result, "id", None) or message_id, "chat_id": target_chat_id},
                )
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="userbot_reply",
                    result={"message_id": getattr(result, "id", None) or message_id, "chat_id": target_chat_id},
                )
                return True
            except Exception as exc:  # noqa: BLE001
                if _is_message_not_modified_error(exc):
                    result = {"message_id": message_id, "chat_id": target_chat_id, "not_modified": True}
                    await record_action(
                        action.get("context"),
                        action,
                        TRACE_STATUS_OK,
                        actual_send_via="userbot_reply",
                        result=result,
                    )
                    await _emit_userbot_action_tap(
                        state,
                        action,
                        ACTION_EVENT_STATUS_OK,
                        channel="userbot_reply",
                        result=result,
                    )
                    return True
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel=action_send_via_options(action)[0] if action_send_via_options(action) else None,
        error_code=last_code,
        error=last_error,
    )
    return False


async def _apply_userbot_send_media_action(state: _AccountState, event: Any, action: dict[str, Any]) -> bool:
    raw_payload = str(action.get("photo_base64") or action.get("file_base64") or "").strip()
    if not raw_payload:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="media_payload_missing", error="photo_base64/file_base64 is empty")
        return False
    try:
        media_bytes = base64.b64decode(raw_payload, validate=True)
    except (binascii.Error, ValueError):
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="media_payload_invalid", error="photo_base64/file_base64 is not valid base64")
        return False
    if not media_bytes:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="media_payload_empty", error="decoded media payload is empty")
        return False
    target_chat_id = _action_chat_id(action, event)
    if target_chat_id is None:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="scope_not_matched", error="target chat_id missing")
        return False
    filename = str(action.get("filename") or ("interaction.png" if action.get("type") == "send_photo" else "interaction.bin")).strip()
    caption = str(action.get("caption") or action.get("text") or "").strip() or None
    if _action_dev_mode_dry_run_enabled(state, action):
        options = action_send_via_options(action)
        await _record_userbot_dry_run(
            state,
            action,
            channel=options[0] if options else "userbot_reply",
            result={
                "dry_run": True,
                "chat_id": target_chat_id,
                "filename": filename,
                "caption": caption,
                "send_via_options": options,
            },
        )
        return True
    reply_to = _int_or_none(action.get("reply_to_message_id"))
    parse_mode = _action_parse_mode(action)
    last_code = "unsupported_send_via"
    last_error = "no supported send_via"
    for send_via in action_send_via_options(action):
        if send_via == "interaction_bot":
            token = await _interaction_bot_token_for_account(state.account_id)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                reply_markup = action.get("reply_markup") if isinstance(action.get("reply_markup"), dict) else None
                if action.get("type") == "send_file":
                    result = await account_bot_service.send_document_bytes(
                        token,
                        target_chat_id,
                        media_bytes,
                        filename=filename or "file.bin",
                        caption=caption,
                        reply_to_message_id=reply_to,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )
                else:
                    result = await account_bot_service.send_photo_bytes(
                        token,
                        target_chat_id,
                        media_bytes,
                        filename=filename or "photo.png",
                        caption=caption,
                        reply_to_message_id=reply_to,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode,
                    )
                await _save_action_message_id(state, action, result)
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot", result=result)
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="interaction_bot",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        if send_via == "userbot_reply":
            if state.client is None:
                last_code = "userbot_offline"
                last_error = "userbot client unavailable"
                continue
            if not await _acquire_userbot_action_rate_limit(
                state,
                action,
                action_type=str(action.get("type") or ""),
                target_chat_id=target_chat_id,
            ):
                return False
            try:
                file_obj = BytesIO(media_bytes)
                file_obj.name = filename or "interaction.bin"
                kwargs: dict[str, Any] = {"reply_to": reply_to}
                if caption:
                    kwargs["caption"] = caption[:1024]
                    kwargs["parse_mode"] = _telethon_parse_mode(parse_mode)
                if action.get("type") == "send_photo":
                    kwargs["force_document"] = False
                sent = await state.client.send_file(target_chat_id, file_obj, **kwargs)
                result = {"message_id": getattr(sent, "id", None), "chat_id": target_chat_id}
                await _save_action_message_id(state, action, result)
                await record_action(
                    action.get("context"),
                    action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result=result,
                )
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="userbot_reply",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel=action_send_via_options(action)[0] if action_send_via_options(action) else None,
        error_code=last_code,
        error=last_error,
    )
    return False


async def _apply_userbot_delete_message_action(state: _AccountState, event: Any, action: dict[str, Any]) -> bool:
    target_chat_id = _action_chat_id(action, event)
    message_id = _int_or_none(action.get("message_id"))
    if target_chat_id is None or message_id is None:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="scope_not_matched", error="target chat_id or message_id missing")
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        options = action_send_via_options(action)
        await _record_userbot_dry_run(
            state,
            action,
            channel=options[0] if options else "userbot_reply",
            result={"dry_run": True, "chat_id": target_chat_id, "message_id": message_id},
        )
        return True
    last_code = "unsupported_send_via"
    last_error = "no supported send_via"
    for send_via in action_send_via_options(action):
        if send_via == "interaction_bot":
            token = await _interaction_bot_token_for_account(state.account_id)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                result = await account_bot_service.delete_message(token, target_chat_id, message_id)
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot", result=result)
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="interaction_bot",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        if send_via == "userbot_reply":
            if state.client is None:
                last_code = "userbot_offline"
                last_error = "userbot client unavailable"
                continue
            try:
                await state.client.delete_messages(target_chat_id, [message_id])
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="userbot_reply")
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="userbot_reply",
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel=action_send_via_options(action)[0] if action_send_via_options(action) else None,
        error_code=last_code,
        error=last_error,
    )
    return False


async def _apply_userbot_pin_message_action(state: _AccountState, event: Any, action: dict[str, Any]) -> bool:
    target_chat_id = _action_chat_id(action, event)
    message_id = _int_or_none(action.get("message_id"))
    if target_chat_id is None or message_id is None:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="scope_not_matched", error="target chat_id or message_id missing")
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        options = action_send_via_options(action)
        await _record_userbot_dry_run(
            state,
            action,
            channel=options[0] if options else "userbot_reply",
            result={"dry_run": True, "chat_id": target_chat_id, "message_id": message_id},
        )
        return True
    last_code = "unsupported_send_via"
    last_error = "no supported send_via"
    for send_via in action_send_via_options(action):
        if send_via == "interaction_bot":
            token = await _interaction_bot_token_for_account(state.account_id)
            if not token:
                last_code = "bot_token_missing"
                last_error = "interaction bot token unavailable"
                continue
            try:
                result = await account_bot_service.call_bot_api(
                    token,
                    "pinChatMessage",
                    {"chat_id": target_chat_id, "message_id": message_id},
                )
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot", result=result)
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="interaction_bot",
                    result=result,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        if send_via == "userbot_reply":
            if state.client is None:
                last_code = "userbot_offline"
                last_error = "userbot client unavailable"
                continue
            try:
                await state.client.pin_message(target_chat_id, message_id, notify=False)
                await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="userbot_reply")
                await _emit_userbot_action_tap(
                    state,
                    action,
                    ACTION_EVENT_STATUS_OK,
                    channel="userbot_reply",
                )
                return True
            except Exception as exc:  # noqa: BLE001
                last_code = "telegram_api_error"
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code=last_code, error=last_error)
    await _emit_userbot_action_tap(
        state,
        action,
        ACTION_EVENT_STATUS_FAILED,
        channel=action_send_via_options(action)[0] if action_send_via_options(action) else None,
        error_code=last_code,
        error=last_error,
    )
    return False


async def _apply_userbot_answer_callback_action(state: _AccountState, action: dict[str, Any]) -> bool:
    callback_query_id = str(action.get("callback_query_id") or "").strip()
    if not callback_query_id:
        if bool(action.get("_tp_synthetic_callback")):
            await record_action(
                action.get("context"),
                action,
                TRACE_STATUS_SKIPPED,
                error_code="synthetic_callback",
                error="synthetic text_button callback has no callback_query_id",
            )
            return True
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="callback_query_id_missing", error="callback_query_id missing")
        return False
    token = await _interaction_bot_token_for_account(state.account_id)
    if not token:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="bot_token_missing", error="interaction bot token unavailable")
        return False
    if _action_dev_mode_dry_run_enabled(state, action):
        await _record_userbot_dry_run(
            state,
            action,
            channel="interaction_bot",
            result={"dry_run": True, "callback_query_id": callback_query_id},
        )
        return True
    try:
        await account_bot_service.answer_callback(
            token,
            callback_query_id,
            text=str(action.get("text") or ""),
            show_alert=bool(action.get("show_alert")),
        )
    except Exception as exc:  # noqa: BLE001
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="telegram_api_error", error=f"{type(exc).__name__}: {exc}")
        await _emit_userbot_action_tap(
            state,
            action,
            ACTION_EVENT_STATUS_FAILED,
            channel="interaction_bot",
            error_code="telegram_api_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot")
    await _emit_userbot_action_tap(state, action, ACTION_EVENT_STATUS_OK, channel="interaction_bot")
    return True


async def _apply_userbot_answer_inline_query_action(state: _AccountState, action: dict[str, Any]) -> bool:
    inline_query_id = str(action.get("inline_query_id") or "").strip()
    if not inline_query_id:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="inline_query_id_missing", error="inline_query_id missing")
        return False
    token = await _interaction_bot_token_for_account(state.account_id)
    if not token:
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="bot_token_missing", error="interaction bot token unavailable")
        return False
    results = action.get("results")
    if not isinstance(results, list):
        results = []
    if _action_dev_mode_dry_run_enabled(state, action):
        await _record_userbot_dry_run(
            state,
            action,
            channel="interaction_bot",
            result={"dry_run": True, "inline_query_id": inline_query_id, "result_count": len(results)},
        )
        return True
    try:
        await account_bot_service.answer_inline_query(
            token,
            inline_query_id,
            results=[item for item in results if isinstance(item, dict)],
            cache_time=_int_or_none(action.get("cache_time")) or 0,
            is_personal=bool(action.get("is_personal", True)),
            next_offset=str(action.get("next_offset") or ""),
            button=action.get("button") if isinstance(action.get("button"), dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        await record_action(action.get("context"), action, TRACE_STATUS_FAILED, error_code="telegram_api_error", error=f"{type(exc).__name__}: {exc}")
        await _emit_userbot_action_tap(
            state,
            action,
            ACTION_EVENT_STATUS_FAILED,
            channel="interaction_bot",
            error_code="telegram_api_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    await record_action(action.get("context"), action, TRACE_STATUS_OK, actual_send_via="interaction_bot")
    await _emit_userbot_action_tap(state, action, ACTION_EVENT_STATUS_OK, channel="interaction_bot")
    return True


async def _interaction_bot_token_for_account(account_id: int) -> str | None:
    try:
        async with AsyncSessionLocal() as db:
            return await account_bot_service.get_interaction_bot_token(db, account_id)
    except Exception:  # noqa: BLE001
        log.debug("load interaction bot token failed account=%s", account_id, exc_info=True)
        return None


def _action_chat_id(action: dict[str, Any], event: Any) -> int | None:
    return _int_or_none(action.get("chat_id")) or _int_or_none(getattr(event, "chat_id", None))


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _reply_anchor_missing_text(action: dict[str, Any], reply_to_user_id: int | None) -> str:
    template = str(action.get("reply_anchor_missing_text") or _DEFAULT_REPLY_ANCHOR_MISSING_TEXT).strip()
    if not template:
        template = _DEFAULT_REPLY_ANCHOR_MISSING_TEXT
    user_id_text = str(reply_to_user_id or action.get("reply_to_user_id") or "")
    try:
        return template.format(user_id=user_id_text)
    except Exception:  # noqa: BLE001
        return template


async def _send_reply_anchor_missing_notice(
    state: _AccountState,
    action: dict[str, Any],
    target_chat_id: int,
    *,
    reply_to_user_id: int | None,
) -> None:
    if bool(action.get("suppress_reply_anchor_missing_notice")):
        return
    if state.client is None:
        return
    text = _reply_anchor_missing_text(action, reply_to_user_id)
    if not text:
        return
    if not await _acquire_userbot_action_rate_limit(
        state,
        action,
        action_type="send_message",
        target_chat_id=target_chat_id,
    ):
        return
    try:
        await state.client.send_message(target_chat_id, text, reply_to=None, parse_mode=None)
    except Exception:  # noqa: BLE001
        log.debug(
            "reply anchor missing notice send failed account=%s chat=%s user=%s",
            state.account_id,
            target_chat_id,
            reply_to_user_id,
            exc_info=True,
        )


async def _resolve_userbot_reply_to_message_id(
    state: _AccountState,
    action: dict[str, Any],
    target_chat_id: int,
) -> tuple[int | None, int | None]:
    reply_to = _int_or_none(action.get("reply_to_message_id"))
    reply_to_user_id = _int_or_none(action.get("reply_to_user_id"))
    if reply_to is None and action.get("reply_to_user_id") not in (None, "") and reply_to_user_id is None:
        raise ValueError("reply_to_user_id 非法")
    if reply_to is None and reply_to_user_id is not None:
        if state.client is None:
            raise ValueError("userbot client unavailable")
        reply_to = await _find_recent_message_id_for_user(
            state.client,
            target_chat_id,
            reply_to_user_id,
            limit=_recent_user_message_search_limit(action.get("reply_to_search_limit")),
        )
        if reply_to is None:
            raise ValueError(f"找不到用户 {reply_to_user_id} 在当前群的近期消息，无法定位发奖回复目标")
    return reply_to, reply_to_user_id


def _action_parse_mode(action: dict[str, Any]) -> str:
    value = str(action.get("parse_mode") or "plain").strip().lower()
    return "html" if value == "html" else "plain"


def _telethon_parse_mode(parse_mode: str) -> str | None:
    return "html" if parse_mode == "html" else None


async def _save_action_message_id(state: _AccountState, action: dict[str, Any], result: Any) -> None:
    save_key = namespaced_action_save_message_id_key(state.account_id, action.get("save_message_id_key"))
    if not save_key:
        return
    msg_id = delivery_message_id(result)
    if msg_id is None:
        return
    try:
        redis = state.redis or get_redis()
        await redis.set(save_key, str(msg_id), ex=7200)
    except Exception:  # noqa: BLE001
        log.debug("save plugin action message id failed account=%s key=%s", state.account_id, save_key, exc_info=True)


async def _save_userbot_reply_target(
    state: _AccountState,
    *,
    target_chat_id: int | None,
    result: Any,
    reply_to_user_id: int | None,
) -> None:
    if reply_to_user_id is None:
        return
    msg_id = delivery_message_id(result)
    if msg_id is None:
        return
    redis = state.redis or get_redis()
    await save_action_reply_target(
        redis,
        account_id=state.account_id,
        chat_id=target_chat_id,
        message_id=msg_id,
        reply_to_user_id=reply_to_user_id,
    )


async def _read_action_message_id(state: _AccountState, raw_key: Any) -> int | None:
    read_key = namespaced_action_save_message_id_key(state.account_id, raw_key)
    if not read_key:
        return None
    try:
        redis = state.redis or get_redis()
        raw = await redis.get(read_key)
    except Exception:  # noqa: BLE001
        log.debug("read plugin action message id failed account=%s key=%s", state.account_id, read_key, exc_info=True)
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return _int_or_none(raw)


def _is_message_not_modified_error(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


# worker 内存里维护的最近活跃 peer 数量上限（超过则按 LRU 丢弃最旧）
RECENT_PEERS_LIMIT = 50


# 内置插件根目录：``backend/app/worker/plugins/builtin``
_BUILTIN_DIR: Path = Path(__file__).parent / "builtin"
_BACKEND_DIR: Path = Path(__file__).resolve().parents[3]
# 非核心可选插件已经迁出 Core。保留这份跳过名单，是为了防止旧镜像或
# 增量部署残留目录被重新当作 builtin 加载；新安装必须走 official/repo installed。
_NON_CORE_BUILTIN_COMPAT_KEYS: frozenset[str] = frozenset(
    {
        "auto_reply",
        "autorepeat",
        "chatgpt_image",
        "codex_image",
        "game24",
        "math10",
    }
)


def _installed_dir() -> Path:
    """解析第三方插件安装目录：阶段 B 引入，由 ``settings.plugins_installed_dir`` 配置。

    每次调用都重新解析，便于测试通过 monkeypatch settings 实现隔离；
    生产环境下值是稳定的。
    """
    try:
        from ...settings import settings  # 延迟 import 避免循环

        return settings.plugins_installed_path
    except Exception:  # noqa: BLE001
        # settings 加载失败时退化到默认相对路径
        return Path("./plugins/installed").resolve()


def _scan_builtin_dirs() -> list[Path]:
    """扫描 builtin 子目录（仅取目录，跳过 ``__pycache__`` 等下划线开头的私有目录）。

    返回值的顺序按文件名字典序，便于测试稳定。
    """
    if not _BUILTIN_DIR.exists():
        return []
    return sorted(
        [
            p
            for p in _BUILTIN_DIR.iterdir()
            if (
                p.is_dir()
                and not p.name.startswith("_")
                and p.name not in _NON_CORE_BUILTIN_COMPAT_KEYS
            )
        ],
        key=lambda p: p.name,
    )


# 内置插件模块名清单（运行期由扫描得出，保留 tuple 类型以兼容现有测试）
# 每次 import loader 时刷新一次；新增 builtin 子目录无需改这里。
_BUILTIN_MODULES: tuple[str, ...] = tuple(p.name for p in _scan_builtin_dirs())
# installed 插件模块名清单：用来避免每次清理都全量扫 sys.modules。
_INSTALLED_MODULE_NAMES: set[str] = set()

_SUPPORTED_FACADE_PERMISSIONS: set[str] = {
    "external_http",
    "external_http_bypass_proxy",
    "ai_text",
    "ai_agent",
}
_RESERVED_UNSUPPORTED_FACADE_PERMISSIONS: set[str] = {
    "ai_vision",
    "ai_image",
    "ai_stt",
}


def _builtin_plugin_path(plugin_key: str) -> Path | None:
    if not _is_safe_plugin_key(plugin_key):
        return None
    path = (_BUILTIN_DIR / plugin_key).resolve()
    try:
        path.relative_to(_BUILTIN_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_dir() else None


def _missing_plugin_error(feature_key: str) -> tuple[str, str]:
    """为缺失插件提供统一错误码与可读日志，便于前端/运维识别。"""
    if feature_key == "codex_image":
        return (
            "official plugin codex_image not installed",
            (
                "feature codex_image 已启用但未找到插件库插件实现。"
                "请先在“安装插件”页安装 Codex 图片生成，并确认 plugins/installed/codex_image 存在；"
                "已跳过加载并保持 worker 运行。"
            ),
        )
    return ("plugin not found", f"feature {feature_key} 已启用但未找到插件实现")


def _import_builtins() -> None:
    """import 内置插件包，触发各模块的 ``@register`` 装饰器写入注册表。

    模块化重构后此函数等价于"调 ``discover_plugins()`` + 跳过返回值"——
    保留是因为现有调用方（runtime / 测试）仍以这个名字为入口；
    返回值忽略，单纯靠副作用（``@register`` + ``_manifest`` 注入）来工作。
    任意单个插件失败仅记日志，不影响其它插件加载。
    """
    try:
        from . import builtin  # noqa: F401  builtin/__init__.py 也会 re-export
    except Exception:  # noqa: BLE001
        log.exception("import plugins.builtin 失败")
    try:
        # 只扫描 builtin。第三方 installed 插件必须等 DB 双开关检查通过后
        # 再按需加载，避免 worker 启动/配置刷新时执行未启用插件代码。
        discover_plugins()
    except Exception:  # noqa: BLE001
        log.exception("discover_plugins 失败")


def _installed_module_name(plugin_key: str) -> str:
    return f"_telepilot_installed_plugin_{plugin_key}"


def _module_file_path(mod: Any) -> Path | None:
    """Best-effort origin for a loaded module; returns None for namespace/builtins."""

    raw = getattr(mod, "__file__", None)
    spec = getattr(mod, "__spec__", None)
    if not raw and spec is not None:
        raw = getattr(spec, "origin", None)
    if not raw or raw in {"built-in", "frozen"}:
        return None
    try:
        return Path(str(raw)).resolve()
    except Exception:  # noqa: BLE001
        return None


def _is_path_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _installed_plugin_module_names(plugin_key: str) -> set[str]:
    """Find all currently loaded modules that belong to one installed plugin.

    The prefix check covers normal relative imports.  The file-origin check is a
    safety net for lazy imports and legacy plugin code that imports helper files
    as top-level modules after putting the plugin directory on sys.path.
    """

    import sys as _sys

    mod_name = _installed_module_name(plugin_key)
    names: set[str] = {
        name
        for name in _sys.modules
        if name == mod_name or name.startswith(f"{mod_name}.")
    }
    try:
        root = _installed_dir().resolve()
        path = (root / plugin_key).resolve()
        path.relative_to(root)
    except Exception:  # noqa: BLE001
        return names
    if not path.is_dir():
        return names
    for name, mod in list(_sys.modules.items()):
        if name in names or mod is None:
            continue
        origin = _module_file_path(mod)
        if origin is not None and _is_path_inside(origin, path):
            names.add(name)
    return names


def _clear_installed_module_cache(plugin_key: str) -> None:
    """清掉第三方插件包、子模块和注册表旧类，保证热加载读到磁盘最新代码。"""
    import importlib as _importlib
    import sys as _sys

    mod_name = _installed_module_name(plugin_key)
    names = _installed_plugin_module_names(plugin_key)
    if _INSTALLED_MODULE_NAMES:
        names.update(
            name
            for name in _INSTALLED_MODULE_NAMES
            if name == mod_name or name.startswith(f"{mod_name}.")
        )
    for name in names:
        _sys.modules.pop(name, None)
        _INSTALLED_MODULE_NAMES.discard(name)
    _importlib.invalidate_caches()
    try:
        from .base import _REGISTRY

        cls = _REGISTRY.get(plugin_key)
        if cls is not None and getattr(cls, "_source", None) == "installed":
            _REGISTRY.pop(plugin_key, None)
    except Exception:  # noqa: BLE001
        log.debug("清理 installed 插件注册表失败 plugin=%s", plugin_key, exc_info=True)
    try:
        root = _installed_dir().resolve()
        path = (root / plugin_key).resolve()
        path.relative_to(root)
        if path.is_dir():
            for cache_dir in path.rglob("__pycache__"):
                shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.debug("清理 installed 插件 pycache 失败 plugin=%s", plugin_key, exc_info=True)


def _clear_builtin_module_cache(plugin_key: str) -> None:
    """清掉 builtin 插件包和子模块，保证目录型插件热重载能读到辅助模块变更。"""
    import sys as _sys

    mod_name = f"{__package__}.builtin.{plugin_key}"
    for name in list(_sys.modules):
        if name == mod_name or name.startswith(f"{mod_name}."):
            _sys.modules.pop(name, None)
    importlib.invalidate_caches()
    try:
        from .base import _REGISTRY

        cls = _REGISTRY.get(plugin_key)
        if cls is not None and getattr(cls, "_source", "builtin") == "builtin":
            _REGISTRY.pop(plugin_key, None)
    except Exception:  # noqa: BLE001
        log.debug("清理 builtin 插件注册表失败 plugin=%s", plugin_key, exc_info=True)
    try:
        path = _builtin_plugin_path(plugin_key)
        if path is not None:
            for cache_dir in path.rglob("__pycache__"):
                shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.debug("清理 builtin 插件 pycache 失败 plugin=%s", plugin_key, exc_info=True)


def _is_safe_plugin_key(plugin_key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", plugin_key or ""))


def _version_tuple(v: str | None) -> tuple[int, ...]:
    """把 ``0.9.6`` / ``v0.9.6-beta`` 转成可比较 tuple。"""
    if not v:
        return ()
    parts = [int(p) for p in re.findall(r"\d+", str(v))]
    return tuple(parts[:3])


def _manifest_compatible(manifest: Manifest) -> tuple[bool, str | None]:
    """检查 manifest 的版本和插件依赖声明。"""
    min_version = (
        getattr(manifest, "min_telepilot_version", None)
        or getattr(manifest, "min_telebot_version", None)
    )
    if min_version and _version_tuple(TELEPILOT_VERSION) < _version_tuple(min_version):
        return False, f"需要 TelePilot >= {min_version}，当前 {TELEPILOT_VERSION}"

    missing = [
        key for key in list(getattr(manifest, "requires_features", None) or [])
        if key not in all_plugins()
    ]
    if missing:
        return False, f"缺少依赖插件: {', '.join(missing)}"

    return True, None


def _warn_manifest_event_subscription_lint(manifest: Manifest) -> None:
    raw_subscriptions = getattr(manifest, "event_subscriptions", None)
    if not isinstance(raw_subscriptions, list):
        return
    plugin_key = str(getattr(manifest, "key", "") or "").strip()
    for idx, raw in enumerate(raw_subscriptions, start=1):
        if not isinstance(raw, dict):
            continue
        subscription = normalize_event_subscription(raw, plugin_key=plugin_key)
        warnings = event_subscription_warnings(subscription)
        if not warnings:
            continue
        log.warning(
            "插件 %s event_subscriptions[%s] 声明存在风险: %s",
            plugin_key or "<unknown>",
            idx,
            "；".join(warnings),
        )


def _loaded_plugin_manifest(cls: type[Plugin] | None, inst: Plugin | None = None) -> Manifest | None:
    manifest = getattr(cls, "_manifest", None) if cls is not None else None
    if isinstance(manifest, Manifest):
        return manifest
    if inst is not None:
        manifest = getattr(type(inst), "_manifest", None)
        if isinstance(manifest, Manifest):
            return manifest
    return None


def _timestamp_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return float(timestamp())
        except Exception:  # noqa: BLE001
            return None
    return None


def _installed_plugin_runtime_drift(
    cls: type[Plugin] | None,
    inst: Plugin | None,
    installed_plugin: Any,
) -> tuple[bool, str | None]:
    """Return whether a loaded installed plugin is older than installed_plugin.

    This is the periodic-reconcile safety net for missed reload IPC messages:
    the DB row and disk may already be updated while the worker still holds the
    old in-memory plugin instance.
    """

    if installed_plugin is None:
        return False, None
    manifest = _loaded_plugin_manifest(cls, inst)
    loaded_version = str(getattr(manifest, "version", "") or "").strip() if manifest is not None else ""
    installed_version = str(getattr(installed_plugin, "version", "") or "").strip()
    if installed_version and loaded_version != installed_version:
        return True, f"version {loaded_version or '<missing>'} -> {installed_version}"

    loaded_at = _timestamp_or_none(getattr(cls, "_loaded_at", None) if cls is not None else None)
    if loaded_at is None and inst is not None:
        loaded_at = _timestamp_or_none(getattr(type(inst), "_loaded_at", None))
    updated_at = _timestamp_or_none(getattr(installed_plugin, "updated_at", None))
    if loaded_at is not None and updated_at is not None and updated_at > loaded_at + 1.0:
        return True, f"updated_at {updated_at:.3f} > loaded_at {loaded_at:.3f}"
    return False, None


def _restore_registry_after_failed_load(
    snapshot: dict[str, type[Plugin]],
    *,
    path_key: str,
    cls: Any = None,
    manifest: Manifest | None = None,
) -> None:
    """Rollback registry side effects from a plugin import that failed validation."""

    from .base import _REGISTRY

    candidate_keys = {path_key}
    for value in (getattr(cls, "key", None), getattr(manifest, "key", None)):
        if value:
            candidate_keys.add(str(value))
    for key in candidate_keys:
        previous = snapshot.get(key)
        if previous is None:
            _REGISTRY.pop(key, None)
        else:
            _REGISTRY[key] = previous


def _restore_full_registry_snapshot(snapshot: dict[str, type[Plugin]]) -> None:
    from .base import _REGISTRY

    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


def _validate_plugin_identity(path: Path, cls: Any, manifest: Manifest) -> tuple[bool, str | None]:
    """Require directory name, manifest key and Plugin.key to describe one plugin."""

    if not isinstance(cls, type) or not issubclass(cls, Plugin):
        return False, "PLUGIN_CLASS 必须是 Plugin 子类"
    path_key = path.name
    class_key = str(getattr(cls, "key", "") or "")
    manifest_key = str(getattr(manifest, "key", "") or "")
    if not manifest_key:
        return False, "MANIFEST.key 不能为空"
    if manifest_key != path_key:
        return False, f"MANIFEST.key={manifest_key!r} 与目录名 {path_key!r} 不一致"
    if class_key != manifest_key:
        return False, f"Plugin.key={class_key!r} 与 MANIFEST.key={manifest_key!r} 不一致"
    return True, None


def _load_dir(path: Path, source: str) -> dict[str, type[Plugin]]:
    """从单个插件目录加载 ``PLUGIN_CLASS`` 与 ``MANIFEST``；失败返回 {} 并写日志。

    - ``source="builtin"``：走正常的 ``importlib.import_module`` 路径，包名是
      ``app.worker.plugins.builtin.<key>``，能享受 Python 的 import 缓存。
    - ``source="installed"``：第三方插件解压在 ``plugins/installed/<key>/``，
      不属于 ``app.*`` 包；用 ``spec_from_file_location`` + ``submodule_search_locations``
      手工创建模块对象再执行，使其能 ``from .plugin import ...`` 等相对 import。

    无论哪种来源，最终都把 ``Manifest`` 与 ``source`` 写到 plugin 类的 ``_manifest`` /
    ``_source`` 属性上，方便后续运行期、API 层直接读取。
    """
    init_file = path / "__init__.py"
    if not init_file.exists():
        log.warning("插件目录 %s 缺少 __init__.py，跳过", path)
        return {}
    from .base import _REGISTRY  # 延迟 import 避免循环

    registry_snapshot = dict(_REGISTRY)

    try:
        if source == "builtin":
            clear_simple_commands_for_module(f"{__package__}.builtin.{path.name}")
            mod = importlib.import_module(
                f".builtin.{path.name}", package=__package__
            )
        else:
            # 第三方插件：构造一个独立的模块对象，避免污染 app 包命名空间。
            # 关键：必须把 mod 注册到 sys.modules，否则 ``from .plugin import X``
            # 这种相对 import 会找不到父包。
            import sys as _sys

            mod_name = _installed_module_name(path.name)
            _clear_installed_module_cache(path.name)
            clear_simple_commands_for_module(mod_name)
            spec = importlib.util.spec_from_file_location(
                mod_name,
                init_file,
                submodule_search_locations=[str(path)],
            )
            if spec is None or spec.loader is None:
                log.warning("无法为插件 %s 构造 spec", path)
                return {}
            mod = importlib.util.module_from_spec(spec)
            _sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
                _INSTALLED_MODULE_NAMES.update(_installed_plugin_module_names(path.name))
            except Exception:
                _sys.modules.pop(mod_name, None)
                raise
    except Exception:  # noqa: BLE001
        log.exception("加载插件目录 %s 失败", path)
        _restore_full_registry_snapshot(registry_snapshot)
        if source == "installed":
            _clear_installed_module_cache(path.name)
            _restore_full_registry_snapshot(registry_snapshot)
        return {}

    cls = getattr(mod, "PLUGIN_CLASS", None)
    manifest = getattr(mod, "MANIFEST", None)
    if cls is None and manifest is None:
        implicit = build_implicit_plugin(module_name=mod.__name__, plugin_key=path.name)
        if implicit is not None:
            cls, manifest = implicit
    if cls is None or manifest is None:
        log.warning("插件 %s 缺少 PLUGIN_CLASS 或 MANIFEST，跳过", path)
        if source == "installed":
            _clear_installed_module_cache(path.name)
        _restore_registry_after_failed_load(registry_snapshot, path_key=path.name, cls=cls)
        return {}
    if not isinstance(manifest, Manifest):
        log.warning(
            "插件 %s 的 MANIFEST 不是 Manifest 实例 (got %s)，跳过",
            path,
            type(manifest).__name__,
        )
        if source == "installed":
            _clear_installed_module_cache(path.name)
        _restore_registry_after_failed_load(registry_snapshot, path_key=path.name, cls=cls)
        return {}
    identity_ok, identity_reason = _validate_plugin_identity(path, cls, manifest)
    if not identity_ok:
        log.warning("插件 %s 身份不一致，跳过: %s", path, identity_reason)
        if source == "installed":
            _clear_installed_module_cache(path.name)
        _restore_registry_after_failed_load(
            registry_snapshot,
            path_key=path.name,
            cls=cls,
            manifest=manifest,
        )
        return {}
    ok, reason = _manifest_compatible(manifest)
    if not ok:
        log.warning("插件 %s manifest 不兼容，跳过: %s", manifest.key, reason)
        if source == "installed":
            _clear_installed_module_cache(path.name)
        _restore_registry_after_failed_load(
            registry_snapshot,
            path_key=path.name,
            cls=cls,
            manifest=manifest,
        )
        return {}

    # 把 manifest / source 挂到 plugin 类上，方便 API 层暴露给前端
    cls._manifest = manifest
    cls._source = source
    cls._loaded_at = time.time()
    _warn_manifest_event_subscription_lint(manifest)

    # 防御性写入注册表：plugin.py 里若有 @register 已经写过；此处再写一次幂等
    # （主要是为了第三方插件——它们的 plugin.py 也应当 @register，但兜底一下）
    _REGISTRY[manifest.key] = cls
    return {manifest.key: cls}


def _load_installed_plugin(plugin_key: str) -> dict[str, type[Plugin]]:
    """按 key 加载单个 installed 插件。

    调用方必须先完成 DB 层授权检查；此函数只负责路径约束和 import。
    """
    if not _is_safe_plugin_key(plugin_key):
        log.warning("installed 插件 key 非法，拒绝加载: %r", plugin_key)
        return {}

    root = _installed_dir().resolve()
    path = (root / plugin_key).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        log.warning("installed 插件路径越界，拒绝加载: %s", path)
        return {}
    if not path.is_dir():
        legacy_path = (_BACKEND_DIR / "plugins" / "installed" / plugin_key).resolve()
        try:
            legacy_path.relative_to((_BACKEND_DIR / "plugins" / "installed").resolve())
        except ValueError:
            return {}
        if not legacy_path.is_dir():
            return {}
        log.warning(
            "installed 插件 %s 位于旧路径 %s；建议移动到 %s",
            plugin_key,
            legacy_path,
            path,
        )
        path = legacy_path
    return _load_dir(path, source="installed")


def _installed_plugin_exists(plugin_key: str) -> bool:
    """判断 installed 插件目录是否存在；仅用于兼容旧 account_feature 行。"""
    if not _is_safe_plugin_key(plugin_key):
        return False
    root = _installed_dir().resolve()
    path = (root / plugin_key).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return False
    if path.is_dir():
        return True
    legacy_root = (_BACKEND_DIR / "plugins" / "installed").resolve()
    legacy_path = (legacy_root / plugin_key).resolve()
    try:
        legacy_path.relative_to(legacy_root)
    except ValueError:
        return False
    return legacy_path.is_dir()


async def _authorize_via_installed_plugin(
    db: Any, plugin_key: str
) -> _InstalledPluginAuthorization:
    """按 installed_plugin 表做 installed 插件授权判断。"""

    installed_plugin = await db.get(InstalledPlugin, plugin_key)
    if installed_plugin is None:
        return _InstalledPluginAuthorization(
            allowed=False,
            state=FEATURE_STATE_FAILED,
            last_error="PLUGIN_LOAD_ORPHAN: installed_plugin missing",
            log_level="warn",
            log_message=f"installed 插件 {plugin_key} 缺少 installed_plugin 记录，已拒绝加载",
        )

    if not bool(getattr(installed_plugin, "enabled", False)):
        return _InstalledPluginAuthorization(
            allowed=False,
            state=FEATURE_STATE_DISABLED,
            last_error="PLUGIN_DISABLED: installed_plugin.enabled=False",
            log_level="info",
            log_message=f"installed 插件 {plugin_key} 的 installed_plugin.enabled=False，跳过加载",
        )

    if str(getattr(installed_plugin, "trust_tier", "")) == PLUGIN_TRUST_ORPHAN:
        return _InstalledPluginAuthorization(
            allowed=False,
            state=FEATURE_STATE_FAILED,
            last_error="PLUGIN_LOAD_ORPHAN: installed_plugin.trust_tier=orphan",
            log_level="warn",
            log_message=f"installed 插件 {plugin_key} trust_tier=orphan，已拒绝加载",
        )

    signature_ok = getattr(installed_plugin, "signature_ok", None)
    if signature_ok is False:
        return _InstalledPluginAuthorization(
            allowed=False,
            state=FEATURE_STATE_FAILED,
            last_error="PLUGIN_SIGNATURE_FAILED: installed_plugin.signature_ok=False",
            log_level="warn",
            log_message=f"installed 插件 {plugin_key} 的 installed_plugin.signature_ok=False，已拒绝加载",
        )
    if signature_ok is None and not bool(app_settings.plugin_allow_legacy_unsigned_plugins):
        return _InstalledPluginAuthorization(
            allowed=False,
            state=FEATURE_STATE_FAILED,
            last_error="PLUGIN_SIGNATURE_UNKNOWN: installed_plugin.signature_ok is null",
            log_level="warn",
            log_message=(
                f"installed 插件 {plugin_key} 的 installed_plugin.signature_ok=NULL，"
                "且 plugin_allow_legacy_unsigned_plugins=False，已拒绝加载"
            ),
        )

    last_install_error = str(getattr(installed_plugin, "last_install_error", "") or "").strip()
    if last_install_error:
        return _InstalledPluginAuthorization(
            allowed=False,
            state=FEATURE_STATE_FAILED,
            last_error=f"PLUGIN_INSTALL_FAILED: {last_install_error}",
            log_level="warn",
            log_message=(
                f"installed 插件 {plugin_key} 的 installed_plugin.last_install_error 非空，"
                "已拒绝加载"
            ),
        )

    return _InstalledPluginAuthorization(allowed=True)


async def _authorize_installed_plugin(
    db: Any,
    plugin_key: str,
    *,
    redis: Any | None = None,
    account_id: int | None = None,
) -> _InstalledPluginAuthorization:
    """统一判断 installed 插件是否允许被 worker 加载。

    第三方插件目录可能来自 zip、Git 远程仓库、历史本地残留或手工拷贝。worker 只认
    installed_plugin 里的权威安装状态；磁盘上有目录但缺少 installed_plugin 记录时，
    一律视为 orphan 并拒绝加载。``redis`` / ``account_id`` 保留在签名中，供调用方
    统一传参和未来扩展 runtime_log 使用。
    """

    return await _authorize_via_installed_plugin(db, plugin_key)


async def _write_account_feature_load_state(
    db: Any,
    account_id: int,
    feature_key: str,
    *,
    state: str,
    last_error: str | None,
) -> None:
    """写回插件加载状态，供前端模块中心展示。"""

    await db.execute(
        update(AccountFeature)
        .where(
            AccountFeature.account_id == account_id,
            AccountFeature.feature_key == feature_key,
        )
        .values(state=state, last_error=last_error)
    )
    await db.commit()
    await update_plugin_runtime_status(
        account_id=account_id,
        plugin_key=feature_key,
        enabled=state == FEATURE_STATE_ACTIVE,
        load_status="loaded" if state == FEATURE_STATE_ACTIVE else "failed",
        last_load_error=last_error,
    )


async def _deny_installed_plugin_load(
    db: Any,
    redis: Any,
    account_id: int,
    plugin_key: str,
    auth: _InstalledPluginAuthorization,
) -> None:
    """记录 installed 插件授权失败并同步账号功能状态。"""

    await _log(
        redis,
        account_id,
        auth.log_level,
        auth.log_message or f"installed 插件 {plugin_key} 未通过加载授权",
        source="system",
        plugin_key=plugin_key,
    )
    await _write_account_feature_load_state(
        db,
        account_id,
        plugin_key,
        state=auth.state,
        last_error=auth.last_error,
    )


def _load_builtin_plugin(plugin_key: str) -> dict[str, type[Plugin]]:
    """按 key 加载单个 builtin 插件；worker 启动时只为启用项付内存成本。"""

    path = _builtin_plugin_path(plugin_key)
    if path is None:
        return {}
    return _load_dir(path, source="builtin")


def discover_plugins(*, include_installed: bool = False) -> dict[str, type[Plugin]]:
    """按目录扫描插件根，返回 ``{key -> Plugin 子类}``。

    - 默认只扫描 builtin，避免无条件执行 installed 插件代码。
    - ``include_installed=True`` 仅保留给受控测试/迁移脚本；运行路径不要使用。
    - 单个插件失败只记日志，不影响其它插件。
    - 不存在 ``plugins/installed`` 目录时直接跳过该源。
    """
    out: dict[str, type[Plugin]] = {}
    for sub in _scan_builtin_dirs():
        out.update(_load_dir(sub, source="builtin"))
    if not include_installed:
        return out
    installed_dir = _installed_dir()
    if installed_dir.exists():
        for sub in sorted(installed_dir.iterdir(), key=lambda p: p.name):
            if not sub.is_dir() or sub.name.startswith("_"):
                continue
            out.update(_load_dir(sub, source="installed"))
    return out


# ─────────────────────────────────────────────────────
# 每账号一份运行态（worker 进程内单例）
# ─────────────────────────────────────────────────────
class _AccountState:
    """单账号 worker 的插件运行态，包含 engine、client、各插件实例与 ctx。"""

    def __init__(self, account_id: int) -> None:
        self.account_id = account_id
        self.generation: int = 1
        self.engine: RateLimitEngine | None = None
        self.client: TelegramClient | None = None
        self.redis: Any = None  # redis.asyncio.Redis
        self.scheduler: Any = None  # PlatformScheduler
        self.contexts: dict[str, PluginContext] = {}  # feature_key -> ctx
        self.instances: dict[str, Plugin] = {}  # feature_key -> Plugin 实例
        # paused 由 runtime 创建并传入；is_set() == True 表示正常运行
        self.paused: asyncio.Event | None = None
        # Sprint2 #3：忽略 peer 名单（int set），从 ignored_peer 表加载，IPC 触发热更
        self.ignored_peers: set[int] = set()
        self.owner_tg_user_id: int | None = None
        self.sudo_users: dict[int, dict[str, Any]] = {}
        # Sprint2 #3：最近活跃 peer 的 LRU（peer_id -> {peer_kind, peer_label, ts}）
        # 仅 worker 内存维护；重启后清空。前端不能假设它持久。
        self.recent_peers: collections.OrderedDict[int, dict[str, Any]] = collections.OrderedDict()
        # 是否对每条 incoming 消息都额外写一行可见性 runtime_log。
        # 默认 False（小机器场景能省大量 Redis stream + DB 写入）。
        # 通过 system_setting key=``log_incoming_messages`` 全局打开，账号
        # 启动 / reload_config 时同步。命令派发、插件错误、业务事件不受影响。
        self.log_incoming_messages: bool = False
        self.account_proxy_url: str | None = None
        self.interaction_text_guard_rules: tuple[_InteractionTextGuardRule, ...] = ()
        self.interaction_bot_sender_ids: frozenset[int] = frozenset()
        self.userbot_session_chats: set[int] = set()
        # 仅当全量刷新 _refresh_userbot_session_chat_cache 成功后置 True。
        # 未就绪时不启用“空集合=无会话”短路，避免进程刚起 / reload 后 / Redis 抖动
        # 导致历史会话不在集合里而产生全量假阴性（漏会话）。
        self.userbot_session_chats_ready: bool = False


# 进程级状态字典（一个 worker 进程通常只服务一个账号；用 dict 是为了灵活）
_STATES: dict[int, _AccountState] = {}


class _LiveMessageOps:
    """实时执行标准消息动作的插件 facade。

    交互入口内 ``ctx.messages`` 会被临时替换为 ``BufferedMessageOps``；
    常驻上下文使用本 facade，供插件命令和后台任务继续走平台受控通道。
    """

    def __init__(
        self,
        state: _AccountState,
        *,
        plugin_key: str,
        entry_key: str = "",
        trace: Any | None = None,
        default_send_via: list[str] | None = None,
    ) -> None:
        self._state = state
        self._plugin_key = plugin_key
        self._entry_key = entry_key
        self._trace = trace
        self._default_send_via = list(default_send_via or ["userbot_reply"])
        self.actions: list[dict[str, Any]] = []

    async def apply(self, actions: list[dict[str, Any]], *, entry_key: str | None = None) -> None:
        normalized = _normalize_interaction_actions(actions, default_send_via=self._default_send_via)
        if not normalized:
            return
        effective_entry_key = entry_key if entry_key is not None else self._entry_key
        context = trace_log_context(
            self._trace,
            plugin_key=self._plugin_key,
            entry_key=effective_entry_key,
        )
        for action in normalized:
            action.setdefault("context", dict(context))
        # actions 字段保留（对外 facade），但不再累积：进入前先清老残留，退出时再清本次。
        self.actions.clear()
        self.actions.extend(normalized)
        redis = self._state.redis or get_redis()
        event = SimpleNamespace(chat_id=None)
        try:
            failed = await _apply_userbot_event_bus_actions(
                self._state,
                self._trace,
                event,
                plugin_key=self._plugin_key,
                entry_key=effective_entry_key,
                actions=normalized,
                redis=redis,
            )
            if failed:
                await _log(
                    redis,
                    self._state.account_id,
                    "warn",
                    "插件消息动作部分执行失败，请在消息链路动作记录中查看具体原因。",
                    source="plugin",
                    action_count=len(normalized),
                    **context,
                )
        finally:
            self.actions.clear()

    async def send(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.send(**kwargs)
        await self.apply([action])
        return action

    async def edit(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.edit(**kwargs)
        await self.apply([action])
        return action

    async def delete(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.delete(**kwargs)
        await self.apply([action])
        return action

    async def pin(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.pin(**kwargs)
        await self.apply([action])
        return action

    async def answer_callback(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.answer_callback(**kwargs)
        await self.apply([action])
        return action

    async def answer_inline_query(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.answer_inline_query(**kwargs)
        await self.apply([action])
        return action

    async def payout(self, **kwargs: Any) -> dict[str, Any]:
        from .message_ops import BufferedMessageOps

        buffered = BufferedMessageOps()
        action = await buffered.payout(**kwargs)
        await self.apply([action])
        return action


class _InteractionEntryMessageOps(BufferedMessageOps):
    """Buffer current interaction actions while keeping live apply for delayed work."""

    def __init__(
        self,
        state: _AccountState,
        *,
        plugin_key: str,
        entry_key: str,
        trace: Any | None = None,
        default_send_via: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._live = _LiveMessageOps(
            state,
            plugin_key=plugin_key,
            entry_key=entry_key,
            trace=trace,
            default_send_via=default_send_via,
        )

    async def apply(self, actions: list[dict[str, Any]], *, entry_key: str | None = None) -> None:
        await self._live.apply(actions, entry_key=entry_key)


async def _event_sender_id(event: Any) -> int | None:
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "id", None)
    if sender_id is not None:
        return int(sender_id)
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        return int(sender_id) if sender_id is not None else None
    except Exception:  # noqa: BLE001
        return None


async def _event_allowed_for_owner_only(state: _AccountState, event: Any) -> bool:
    """owner_only 插件的统一消息门禁：账号本人或授权 sudo 用户才可触发。"""
    if bool(getattr(event, "outgoing", False)):
        return True
    sender_id = await _event_sender_id(event)
    if sender_id is None:
        return False
    if state.owner_tg_user_id is not None and sender_id == state.owner_tg_user_id:
        return True
    sudo_cfg = state.sudo_users.get(sender_id)
    if sudo_cfg is None:
        return False
    allowed_chats = sudo_cfg.get("allowed_chat_ids") or []
    if not sudo_chat_allowed(allowed_chats, getattr(event, "chat_id", None)):
        return False
    return True


def _rule_chat_matches_for_interaction_guard(rule: dict[str, Any], chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    chat_ids = rule.get("chat_ids")
    if isinstance(chat_ids, list) and chat_ids:
        try:
            return int(chat_id) in {int(item) for item in chat_ids}
        except (TypeError, ValueError):
            return False
    return True


def _text_equals_any(text: str, values: Any) -> bool:
    if not isinstance(values, list):
        return False
    clean = str(text or "").strip()
    return bool(clean and any(clean == str(item or "").strip() for item in values if str(item or "").strip()))


def _normalize_guard_texts(*values: Any) -> frozenset[str]:
    out: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            text = str(item or "").strip()
            if text:
                out.add(text)
    return frozenset(out)


def _normalize_guard_chat_ids(raw: Any) -> frozenset[int]:
    if not isinstance(raw, list):
        return frozenset()
    out: set[int] = set()
    for item in raw:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


async def _refresh_interaction_text_guard_cache(state: Any) -> None:
    """Refresh interaction Bot owned text rules once per startup/reload."""

    account_id = getattr(state, "account_id", None)
    rules: list[_InteractionTextGuardRule] = []
    bot_sender_ids: frozenset[int] = frozenset()
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, interaction_bot_service.transfer_notice_setting_key(int(account_id)))
        cfg = interaction_bot_service.normalize_transfer_notice_config(row.value if row is not None else None)
        bot_sender_ids = _configured_interaction_bot_sender_ids(cfg)
        if cfg.get("enabled"):
            for rule in cfg.get("rules") or []:
                if not isinstance(rule, dict) or not bool(rule.get("enabled", True)):
                    continue
                texts = _normalize_guard_texts(
                    rule.get("open_commands"),
                    rule.get("close_commands"),
                    rule.get("module_start_keywords"),
                )
                if not texts:
                    continue
                rules.append(
                    _InteractionTextGuardRule(
                        chat_ids=_normalize_guard_chat_ids(rule.get("chat_ids")),
                        texts=texts,
                    )
                )
    except Exception:  # noqa: BLE001
        log.debug("刷新交互 Bot 文本守卫缓存失败 account=%s", account_id, exc_info=True)
    try:
        state.interaction_text_guard_rules = tuple(rules)
        state.interaction_bot_sender_ids = bot_sender_ids
    except Exception:  # noqa: BLE001
        pass


def _interaction_text_guard_rule_matches(rule: _InteractionTextGuardRule, chat_id: int | None) -> bool:
    if not rule.chat_ids:
        return True
    if chat_id is None:
        return False
    try:
        return int(chat_id) in rule.chat_ids
    except (TypeError, ValueError):
        return False


async def _interaction_bot_owns_incoming_text(state: _AccountState, event: Any) -> bool:
    """交互 Bot 规则接管的关键词不再交给普通插件 on_message 自行开局。"""

    text = str(getattr(event, "raw_text", "") or "").strip()
    if not text:
        return False
    chat_id = getattr(event, "chat_id", None)
    rules = getattr(state, "interaction_text_guard_rules", None)
    if rules is None:
        await _refresh_interaction_text_guard_cache(state)
        rules = getattr(state, "interaction_text_guard_rules", ())
    for rule in rules or ():
        if text in rule.texts and _interaction_text_guard_rule_matches(rule, chat_id):
            return True
    return False


async def _record_interaction_text_guard_skip(
    state: _AccountState,
    event: Any,
    *,
    event_label: str,
) -> None:
    event_payload = normalize_userbot_event(state.account_id, event)
    flags = await _load_event_framework_flags()
    trace = None
    if flags.get("trace_enabled", True):
        trace = await start_trace(event_payload)
        event_payload["trace_id"] = trace.trace_id
        await record_span(
            trace,
            "receive",
            TRACE_STATUS_OK,
            component="userbot_message",
            direction=event_label,
        )
    await record_span(
        trace,
        "route",
        TRACE_STATUS_SKIPPED,
        component="interaction_bot_text_guard",
        reason_code=_INTERACTION_RULE_OWNED_REASON_CODE,
        message="交互 Bot 关键词/开关命令已接管该消息，UserBot legacy 分发跳过。",
        direction=event_label,
        chat_id=getattr(event, "chat_id", None),
        sender_id=getattr(event, "sender_id", None),
        message_preview=(getattr(event, "raw_text", "") or "")[:200],
    )
    await finish_trace(
        trace,
        TRACE_STATUS_SKIPPED,
        invoked_count=0,
        failed_count=0,
        direction=event_label,
        reason_code=_INTERACTION_RULE_OWNED_REASON_CODE,
    )


def _decode_redis_key(raw: Any) -> str:
    return raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)


async def _redis_keys(redis: Any, match: str) -> list[str]:
    scan_iter = getattr(redis, "scan_iter", None)
    if callable(scan_iter):
        keys: list[str] = []
        async for key in scan_iter(match=match):
            keys.append(_decode_redis_key(key))
        return keys
    keys_fn = getattr(redis, "keys", None)
    if callable(keys_fn):
        return [_decode_redis_key(key) for key in await keys_fn(match)]
    return []


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


def _session_channel(session: dict[str, Any]) -> str:
    channel = str(session.get("channel") or "").strip()
    return channel if channel in _SESSION_CHANNELS_OBSERVED_BY_USERBOT else ""


def _userbot_observed_session_is_active(session: dict[str, Any], chat_id: int | None = None) -> bool:
    if _session_channel(session) not in _SESSION_CHANNELS_OBSERVED_BY_USERBOT:
        return False
    if chat_id is not None and _int_or_none(session.get("chat_id")) != chat_id:
        return False
    expires_at = session.get("expires_at")
    try:
        if expires_at is not None and float(expires_at) <= time.time():
            return False
    except (TypeError, ValueError):
        return False
    return True


async def _refresh_userbot_session_chat_cache(state: Any) -> None:
    account_id = _int_or_none(getattr(state, "account_id", None))
    if account_id is None:
        return
    redis = getattr(state, "redis", None) or get_redis()
    chats: set[int] = set()
    try:
        for key in await _redis_keys(redis, f"{_USERBOT_SESSION_KEY_PREFIX}{account_id}:*"):
            raw = await redis.get(key)
            session = _json_dict(raw)
            if not _userbot_observed_session_is_active(session):
                continue
            chat_id = _int_or_none(session.get("chat_id"))
            if chat_id is not None:
                chats.add(chat_id)
    except Exception:  # noqa: BLE001
        log.debug("刷新 userbot 会话 chat 缓存失败 account=%s", account_id, exc_info=True)
        return
    try:
        state.userbot_session_chats = chats
        # 全量重建成功后才点亮 ready：此后"空集合"可被信任为"真的无会话"，
        # 短路 guard 方可启用。Redis 抖动导致上面 except→return 时不会执行到这里，
        # ready 保持原值，短路不启用、照常 SCAN，避免长期假阴性。
        state.userbot_session_chats_ready = True
    except Exception:  # noqa: BLE001
        pass


def _userbot_session_key_user_id(key: str) -> int | None:
    parts = str(key or "").split(":")
    if len(parts) >= 2 and parts[-2] == "user":
        return _int_or_none(parts[-1])
    return None


async def _load_userbot_sessions_for_chat(
    state: _AccountState,
    redis: Any,
    *,
    chat_id: int,
    sender_id: int | None,
) -> list[tuple[str, dict[str, Any]]]:
    prefix = f"{_USERBOT_SESSION_KEY_PREFIX}{state.account_id}:"
    patterns = [
        f"{prefix}*:{chat_id}",
        f"{prefix}*:{chat_id}:user:*",
    ]
    sessions: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    try:
        for pattern in patterns:
            for key in await _redis_keys(redis, pattern):
                if key in seen:
                    continue
                seen.add(key)
                keyed_user_id = _userbot_session_key_user_id(key)
                if keyed_user_id is not None and keyed_user_id != sender_id:
                    continue
                raw = await redis.get(key)
                session = _json_dict(raw)
                if not _userbot_observed_session_is_active(session, chat_id):
                    continue
                sessions.append((key, session))
    except Exception:  # noqa: BLE001
        log.debug("读取 userbot 会话失败 account=%s chat=%s", state.account_id, chat_id, exc_info=True)
        return []
    if not sessions:
        state.userbot_session_chats.discard(chat_id)
    return sessions


def _looks_like_worker_command_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    prefix = current_command_prefix(fallback=app_settings.command_prefix or ",")
    return bool(prefix and value.startswith(prefix))


def _entry_includes_outgoing(plugin_key: str, entry_key: str) -> bool:
    manifest = account_bot_service.declared_module_entry_manifest(plugin_key, entry_key)
    return bool(manifest and manifest.get("include_outgoing"))


def _entry_declares_session_expired(plugin_key: str, entry_key: str) -> bool:
    """对齐 bot 侧 ``_rule_entry_allows_event`` 语义：入口未声明任何事件视为放行全部；
    一旦有显式声明，则必须含 ``session_expired`` 或 ``all_events`` 才允许过期派发。"""
    declared = account_bot_service.declared_module_entry_events(plugin_key, entry_key)
    if not declared:
        return True
    return "session_expired" in declared or "all_events" in declared


def _userbot_text_button_callback_data(session: dict[str, Any], text: str) -> str | None:
    data = session.get("data") if isinstance(session.get("data"), dict) else {}
    button_map = data.get(_USERBOT_BUTTON_MAP_KEY) if isinstance(data.get(_USERBOT_BUTTON_MAP_KEY), dict) else {}
    mapping = button_map.get("map") if isinstance(button_map.get("map"), dict) else {}
    clean = str(text or "").strip()
    if not clean:
        return None
    value = mapping.get(clean)
    return str(value) if value not in (None, "") else None


def _userbot_session_event_payload(
    base_payload: dict[str, Any],
    *,
    session_key: str,
    session: dict[str, Any],
    event_label: str,
    callback_data: str | None = None,
) -> dict[str, Any]:
    payload = dict(base_payload)
    session_channel = _session_channel(session)
    source = dict(payload.get("source") if isinstance(payload.get("source"), dict) else {})
    source["type"] = "callback_query" if callback_data is not None else "message"
    source["channel"] = session_channel
    if session_channel != _SESSION_CHANNEL_USERBOT:
        source["observed_channel"] = _SESSION_CHANNEL_USERBOT
    if callback_data is not None:
        source["callback_data"] = callback_data
        source["callback_query_id"] = None
        source["synthetic"] = "text_button"
    payload["source"] = source
    payload["event_type"] = "callback_query" if callback_data is not None else "message"
    session_data = session.get("data") if isinstance(session.get("data"), dict) else {}
    payload["session"] = {
        "key": session_key,
        "active": True,
        "channel": session_channel,
        "data": dict(session_data),
        "rule_id": session.get("rule_id"),
        "rule_name": session.get("rule_name"),
        "module_key": session.get("module_key"),
        "entry_key": session.get("entry_key"),
        "chat_id": _int_or_none(session.get("chat_id")),
        "started_by_user_id": _int_or_none(session.get("started_by_user_id")),
        "started_by_message_id": _int_or_none(session.get("started_by_message_id")),
        "source_user_id": _int_or_none(session.get("source_user_id")),
        "event_type": session.get("event_type"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "expires_at": session.get("expires_at"),
    }
    trigger = dict(payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {})
    trigger.update(
        {
            "type": "session_callback" if callback_data is not None else "session_message",
            "channel": session_channel,
            "rule_id": session.get("rule_id"),
            "module_key": session.get("module_key"),
            "entry_key": session.get("entry_key"),
            "session_key": session_key,
            "event_label": event_label,
        }
    )
    if session_channel != _SESSION_CHANNEL_USERBOT:
        trigger["observed_channel"] = _SESSION_CHANNEL_USERBOT
    if callback_data is not None:
        payload["callback_data"] = callback_data
        payload["callback_query_id"] = None
        trigger["callback_data"] = callback_data
        trigger["synthetic"] = "text_button"
    if session_channel == _SESSION_CHANNEL_INTERACTION_BOT:
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        payload["message_text"] = str(message.get("text") or "")
    payload["trigger"] = trigger
    return payload


async def _start_userbot_session_trace(
    state: _AccountState,
    payload: dict[str, Any],
    *,
    event_label: str,
) -> Any | None:
    flags = await _load_event_framework_flags()
    if not flags.get("trace_enabled", True):
        return None
    trace = await start_trace(payload)
    payload["trace_id"] = trace.trace_id
    await record_span(
        trace,
        "receive",
        TRACE_STATUS_OK,
        component="userbot_session_message",
        direction=event_label,
    )
    return trace


async def _dispatch_userbot_session_message(
    state: _AccountState,
    event: Any,
    *,
    direction: str,
    edited: bool,
    event_label: str,
    redis: Any,
) -> bool:
    if edited:
        return False
    chat_id = _int_or_none(getattr(event, "chat_id", None))
    if chat_id is None:
        return False
    text = str(getattr(event, "raw_text", "") or getattr(event, "text", "") or "")
    if _looks_like_worker_command_text(text):
        return False
    sender_id = _int_or_none(getattr(event, "sender_id", None))
    if direction == "incoming" and sender_id is not None and sender_id in state.interaction_bot_sender_ids:
        return False
    # 预筛短路：缓存已成功建过全量（ready）且该 chat 不在活跃会话集合里，
    # 说明该 chat 无 userbot 会话，直接跳过每消息一次的 Redis SCAN。
    # ready=False（进程刚起 / reload 后 / 刷新曾失败）时不短路，照常 SCAN，
    # 避免"缓存尚未初始化"导致的全量假阴性漏会话。
    if (
        getattr(state, "userbot_session_chats_ready", False)
        and chat_id not in state.userbot_session_chats
    ):
        return False
    sessions = await _load_userbot_sessions_for_chat(state, redis, chat_id=chat_id, sender_id=sender_id)
    if sessions:
        state.userbot_session_chats.add(chat_id)
    candidates: list[tuple[str, dict[str, Any], str, str, str, str | None]] = []
    for session_key, session in sessions:
        plugin_key = str(session.get("module_key") or "").strip()
        entry_key = str(session.get("entry_key") or "").strip()
        if not plugin_key or not entry_key:
            continue
        session_channel = _session_channel(session)
        if (
            session_channel == _SESSION_CHANNEL_USERBOT
            and direction == "outgoing"
            and not _entry_includes_outgoing(plugin_key, entry_key)
        ):
            continue
        candidates.append(
            (
                session_key,
                session,
                session_channel,
                plugin_key,
                entry_key,
                _userbot_text_button_callback_data(session, text),
            )
        )
    if not candidates:
        return False

    base_payload = normalize_userbot_event(state.account_id, event)
    trace = await _start_userbot_session_trace(state, base_payload, event_label=event_label)
    invoked_count = 0
    failed_count = 0
    consumed = False
    message_id = _int_or_none(
        getattr(getattr(event, "message", None), "id", None)
        or getattr(event, "id", None)
        or getattr(event, "message_id", None)
    )
    for session_key, session, session_channel, plugin_key, entry_key, callback_data in candidates:
        if not await claim_interaction_message(
            account_id=state.account_id,
            chat_id=chat_id,
            message_id=message_id,
            rule_id=session.get("rule_id"),
            redis=redis,
        ):
            await record_span(
                trace,
                "route",
                TRACE_STATUS_SKIPPED,
                component="userbot_session",
                plugin_key=plugin_key,
                entry_key=entry_key,
                reason_code="cross_channel_duplicate",
                message="UserBot 会话消息已由另一条交互管道处理，跳过重复投递。",
                direction=event_label,
                session_key=session_key,
                session_channel=session_channel,
            )
            continue
        payload = _userbot_session_event_payload(
            base_payload,
            session_key=session_key,
            session=session,
            event_label=event_label,
            callback_data=callback_data,
        )
        if trace is not None:
            payload["trace_id"] = trace.trace_id
        await record_span(
            trace,
            "route",
            TRACE_STATUS_OK,
            component="userbot_session",
            plugin_key=plugin_key,
            entry_key=entry_key,
            reason_code="matched",
            message="UserBot 观察到活跃会话消息，按会话逻辑通道投递给交互入口。",
            direction=event_label,
            session_key=session_key,
            session_channel=session_channel,
        )
        started = time.monotonic()
        try:
            default_send_via = (
                ["userbot_reply"]
                if session_channel == _SESSION_CHANNEL_USERBOT
                else ["interaction_bot"]
            )
            actions = await invoke_interaction_entry(
                state.account_id,
                plugin_key=plugin_key,
                entry_key=entry_key,
                payload=payload,
                default_send_via=default_send_via,
            )
            invoked_count += 1
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_OK,
                component="userbot_session_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                direction=event_label,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await record_span(
                trace,
                "plugin_return",
                TRACE_STATUS_OK,
                component="userbot_session_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                action_count=len(actions),
            )
            if not actions and session_channel != _SESSION_CHANNEL_USERBOT:
                await release_interaction_message(
                    account_id=state.account_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    rule_id=session.get("rule_id"),
                    redis=redis,
                )
                await record_span(
                    trace,
                    "route",
                    TRACE_STATUS_SKIPPED,
                    component="userbot_session_dispatcher",
                    plugin_key=plugin_key,
                    entry_key=entry_key,
                    reason_code="claim_released_no_actions",
                    message="插件零动作，释放跨管道去重占用，允许交互 Bot 原路径处理。",
                    direction=event_label,
                    session_key=session_key,
                    session_channel=session_channel,
                    message_id=message_id,
                )
            if session_channel == _SESSION_CHANNEL_USERBOT or actions:
                consumed = True
            action_failed = await _apply_userbot_event_bus_actions(
                state,
                trace,
                event,
                plugin_key=plugin_key,
                entry_key=entry_key,
                actions=actions,
                redis=redis,
                session_key=session_key,
                session=session,
                synthetic_callback=callback_data is not None,
            )
            if action_failed:
                failed_count += 1
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED if action_failed else TRACE_STATUS_OK,
                last_trace_id=getattr(trace, "trace_id", None),
            )
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            consumed = True
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_FAILED,
                component="userbot_session_dispatcher",
                plugin_key=plugin_key,
                entry_key=entry_key,
                direction=event_label,
                reason_code="plugin_runtime_error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED,
                last_trace_id=getattr(trace, "trace_id", None),
            )
            await _log(
                redis,
                state.account_id,
                "error",
                f"插件 {plugin_key} 处理 UserBot 会话消息时出错：{type(exc).__name__}: {exc}。",
                source="plugin",
                plugin_key=plugin_key,
                entry_key=entry_key,
                direction=event_label,
                chat_id=chat_id,
                sender_id=sender_id,
                message_preview=text[:200],
                traceback=traceback.format_exc(limit=8),
                **trace_log_context(trace),
            )
    final_status = TRACE_STATUS_FAILED if failed_count else TRACE_STATUS_OK if invoked_count else TRACE_STATUS_SKIPPED
    await finish_trace(
        trace,
        final_status,
        invoked_count=invoked_count,
        failed_count=failed_count,
        direction=event_label,
        consumed=consumed,
    )
    return consumed


def _userbot_session_expired_payload(
    state: _AccountState,
    *,
    session_key: str,
    session: dict[str, Any],
) -> dict[str, Any]:
    event = SimpleNamespace(
        chat_id=_int_or_none(session.get("chat_id")),
        sender_id=_int_or_none(session.get("started_by_user_id") or session.get("source_user_id")),
        id=_int_or_none(session.get("started_by_message_id")),
        raw_text="",
        text="",
        message=SimpleNamespace(
            chat_id=_int_or_none(session.get("chat_id")),
            sender_id=_int_or_none(session.get("started_by_user_id") or session.get("source_user_id")),
            id=_int_or_none(session.get("started_by_message_id")),
            text="",
        ),
    )
    payload = normalize_userbot_event(state.account_id, event)
    source = dict(payload.get("source") if isinstance(payload.get("source"), dict) else {})
    source.update({"type": "session_expired", "channel": "userbot"})
    payload["source"] = source
    payload["event_type"] = "session_expired"
    payload["session"] = _userbot_session_envelope_from_session(session_key, session)
    payload["trigger"] = {
        "type": "session_expired",
        "channel": "userbot",
        "rule_id": session.get("rule_id"),
        "module_key": session.get("module_key"),
        "entry_key": session.get("entry_key"),
        "session_key": session_key,
    }
    return payload


async def scan_userbot_expired_sessions_once(account_id: int) -> int:
    state = _STATES.get(account_id)
    if state is None:
        return 0
    redis = state.redis or get_redis()
    processed = 0
    for key in await _redis_keys(redis, f"{_USERBOT_SESSION_KEY_PREFIX}{account_id}:*"):
        session = _json_dict(await redis.get(key))
        if str(session.get("channel") or "").strip() != "userbot":
            continue
        try:
            expired = float(session.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            expired = True
        if not expired:
            continue
        plugin_key = str(session.get("module_key") or "").strip()
        entry_key = str(session.get("entry_key") or "").strip()
        if not plugin_key or not entry_key:
            delete = getattr(redis, "delete", None)
            if callable(delete):
                await delete(key)
            continue
        if not _entry_declares_session_expired(plugin_key, entry_key):
            # 对齐 bot 侧：入口未声明 session_expired 事件则不派发插件，仅按原逻辑清理会话 key。
            log.debug(
                "UserBot 会话过期跳过派发：入口未声明 session_expired plugin=%s entry=%s key=%s",
                plugin_key,
                entry_key,
                key,
            )
            await record_span(
                None,
                "subscription_match",
                TRACE_STATUS_SKIPPED,
                component="userbot_session_expired",
                plugin_key=plugin_key,
                entry_key=entry_key,
                reason_code="event_type_not_subscribed",
                message="插件入口未声明 session_expired 事件。",
                session_key=key,
            )
            delete = getattr(redis, "delete", None)
            if callable(delete):
                await delete(key)
            continue
        payload = _userbot_session_expired_payload(state, session_key=key, session=session)
        trace = await _start_userbot_session_trace(state, payload, event_label="session_expired")
        if trace is not None:
            payload["trace_id"] = trace.trace_id
        failed = False
        try:
            actions = await invoke_interaction_entry(
                state.account_id,
                plugin_key=plugin_key,
                entry_key=entry_key,
                payload=payload,
                default_send_via=["userbot_reply"],
            )
            failed = await _apply_userbot_event_bus_actions(
                state,
                trace,
                SimpleNamespace(chat_id=_int_or_none(session.get("chat_id"))),
                plugin_key=plugin_key,
                entry_key=entry_key,
                actions=actions,
                redis=redis,
                session_key=key,
                session=session,
            )
            await update_plugin_runtime_status(
                account_id=state.account_id,
                plugin_key=plugin_key,
                last_invocation_status=TRACE_STATUS_FAILED if failed else TRACE_STATUS_OK,
                last_trace_id=getattr(trace, "trace_id", None),
            )
        except Exception as exc:  # noqa: BLE001
            failed = True
            await record_span(
                trace,
                "plugin_invoke",
                TRACE_STATUS_FAILED,
                component="userbot_session_expired",
                plugin_key=plugin_key,
                entry_key=entry_key,
                reason_code="plugin_runtime_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            await _log(
                redis,
                state.account_id,
                "error",
                f"插件 {plugin_key} 处理 UserBot 会话超时时出错：{type(exc).__name__}: {exc}。",
                source="plugin",
                plugin_key=plugin_key,
                entry_key=entry_key,
                session_key=key,
                traceback=traceback.format_exc(limit=8),
                **trace_log_context(trace),
            )
        finally:
            delete = getattr(redis, "delete", None)
            if callable(delete):
                await delete(key)
            chat_id = _int_or_none(session.get("chat_id"))
            if chat_id is not None:
                await _refresh_userbot_session_chat_cache(state)
            await finish_trace(trace, TRACE_STATUS_FAILED if failed else TRACE_STATUS_OK)
            processed += 1
    return processed


# ─────────────────────────────────────────────────────
# 主入口：load_plugins_for_account
# ─────────────────────────────────────────────────────
async def load_plugins_for_account(
    client: TelegramClient,
    account_id: int,
    paused: asyncio.Event,
    redis: Any,
    scheduler: Any | None = None,
    account_proxy_url: str | None = None,
) -> None:
    """runtime 在 ``client.connect()`` 之前调一次。

    步骤：
      1. 构造账号插件运行态
      2. 构造 ``RateLimitEngine``（依赖 humanize 配置 + service 层 ``get_effective``）
      3. 在 client 上注册全局消息派发，把每条消息按 instances 顺序广播
      4. 加载该账号已启用的 features → ``_activate`` 按需导入对应插件
    """
    state = _AccountState(account_id)
    state.client = client
    state.paused = paused
    state.redis = redis
    state.scheduler = scheduler
    state.account_proxy_url = account_proxy_url
    state.log_incoming_messages = await _load_log_incoming_messages_setting()
    _STATES[account_id] = state

    # ── 1) 拉取拟人化 + 账号信息构造 engine ──
    async with AsyncSessionLocal() as db:
        acc = await db.get(Account, account_id)
        humanize_row = await db.get(HumanizeConfig, account_id)
        sudo_rows = (
            await db.execute(select(SudoUser).where(SudoUser.account_id == account_id))
        ).scalars().all()
    owner_id = getattr(acc, "tg_user_id", None)
    state.owner_tg_user_id = int(owner_id) if owner_id else None
    state.sudo_users = {
        int(r.tg_user_id): {
            "allowed_chat_ids": list(r.allowed_chat_ids or []),
            "allowed_commands": list(r.allowed_commands or []),
        }
        for r in sudo_rows
    }
    opts = HumanizeOpts(
        jitter_pct=humanize_row.jitter_pct if humanize_row else 15,
        typing_simulate=bool(humanize_row.typing_simulate) if humanize_row else True,
        typing_min_ms=humanize_row.typing_min_ms if humanize_row else 1000,
        typing_max_ms=humanize_row.typing_max_ms if humanize_row else 3000,
        typing_probability=humanize_row.typing_probability if humanize_row else 80,
        read_before_reply=bool(humanize_row.read_before_reply) if humanize_row else True,
        active_window_start=humanize_row.active_window_start if humanize_row else None,
        active_window_end=humanize_row.active_window_end if humanize_row else None,
        cold_start_days=humanize_row.cold_start_days if humanize_row else 7,
        cold_start_until=acc.cold_start_until if acc else None,
    )

    async def _get_eff(aid: int, action: str):
        """engine 用的 get_effective 工厂闭包：每次新开 session，避免共享。"""
        async with AsyncSessionLocal() as db:
            return await get_effective(db, aid, action)

    state.engine = RateLimitEngine(account_id, opts, _get_eff, redis=redis)
    if scheduler is not None:
        try:
            scheduler.attach_engine(state.engine)
        except Exception:  # noqa: BLE001
            log.exception("注入平台调度器 engine 失败 account=%s", account_id)

    # ── 1.5) 拉取允许 peer 名单（沿用 ignored_peer 表存储） ──
    await _load_ignored_peers(state)
    await _refresh_interaction_text_guard_cache(state)
    await _refresh_userbot_session_chat_cache(state)

    # ── 2) 全局事件派发 ──
    def _make_dispatcher(direction: str, *, edited: bool = False):  # "incoming" or "outgoing"
        """创建消息派发闭包。direction 对应 Plugin.message_channels 的值。"""
        kwargs = {"incoming": True} if direction == "incoming" else {"outgoing": True}
        handler_name = "on_message_edited" if edited else "on_message"
        event_label = f"{direction}_edited" if edited else direction
        event_builder = events.MessageEdited if edited else events.NewMessage

        @client.on(event_builder(**kwargs))
        async def _dispatch(event):  # noqa: ANN001
            if state.paused is not None and not state.paused.is_set():
                return
            trace = None

            direct_consumed = await _dispatch_userbot_direct_passthrough(
                state,
                event,
                direction=direction,
                edited=edited,
                event_label=event_label,
                redis=redis,
            )
            if direct_consumed:
                return

            # incoming 普通链路需要允许名单检查和 LRU 维护；低延迟直通已在上方广播。
            if direction == "incoming":
                pid = event.chat_id
                if pid is not None:
                    await _record_recent_peer(state, event)
                    # 白名单模式：配置为空 = 放行全部；非空 = 仅放行名单内会话
                    if state.ignored_peers and pid not in state.ignored_peers:
                        log.debug("[allowed] drop account=%s chat_id=%s", account_id, pid)
                        return
                # 调试日志：每条 incoming 消息记一行；
                # 默认关闭，small VPS 上活跃账号能产生数百条/分钟。
                # 在 system_setting.log_incoming_messages = true 时打开。
                if state.log_incoming_messages:
                    try:
                        peer_kind = (
                            "private" if event.is_private
                            else "channel" if event.is_channel
                            else "group" if event.is_group
                            else "?"
                        )
                        text_preview = (event.raw_text or "")[:80]
                        edit_label = "编辑" if edited else ""
                        await _log(
                            redis,
                            account_id,
                            "info",
                            (
                                f"收到一条{peer_kind}{edit_label}消息：聊天 ID={event.chat_id}，"
                                f"内容预览={text_preview!r}。已进入插件分发流程。"
                            ),
                            source="event",
                            chat_id=event.chat_id,
                            peer_kind=peer_kind,
                            message_preview=text_preview,
                            edited=edited,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                if not edited and await _interaction_bot_owns_incoming_text(state, event):
                    await _record_interaction_text_guard_skip(state, event, event_label=event_label)
                    return

            if direction == "incoming":
                await emit_inbound_event(
                    account_id=state.account_id,
                    envelope=normalize_userbot_event(state.account_id, event),
                    account_config=_recording_account_config(state),
                )

            session_consumed = await _dispatch_userbot_session_message(
                state,
                event,
                direction=direction,
                edited=edited,
                event_label=event_label,
                redis=redis,
            )
            if session_consumed:
                return

            dispatch_state = await _start_userbot_message_trace(state, event, event_label=event_label)
            trace = dispatch_state.trace
            if trace is not None:
                try:
                    event.trace_id = trace.trace_id
                except Exception:  # noqa: BLE001
                    pass
            invoked_count, failed_count, event_bus_consumed_plugin_keys = await _dispatch_userbot_event_bus_matches(
                state,
                dispatch_state,
                event,
                event_label=event_label,
                redis=redis,
            )
            for fkey, inst in list(state.instances.items()):
                if dispatch_state.event_bus_enabled and fkey in event_bus_consumed_plugin_keys:
                    continue
                if direction not in inst.message_channels:
                    continue
                if getattr(inst, "owner_only", True):
                    allowed = await _event_allowed_for_owner_only(state, event)
                    if not allowed:
                        continue
                ctx = state.contexts.get(fkey)
                if ctx is None:
                    continue
                if ctx.generation != state.generation:
                    continue
                handler = getattr(inst, handler_name, None)
                if handler is None:
                    continue
                if edited and getattr(type(inst), handler_name, None) is getattr(Plugin, handler_name, None):
                    continue
                await record_span(
                    trace,
                    "subscription_match",
                    TRACE_STATUS_OK,
                    component="legacy_userbot_dispatcher",
                    plugin_key=fkey,
                    reason_code="matched",
                    message="legacy on_message 兼容入口已记录为 Event Bus 外层 decision 后投递。",
                    dispatch_mode="legacy_compat",
                    scope="legacy_message_channels",
                    filters={
                        "direction": direction,
                        "edited": edited,
                        "owner_only": getattr(inst, "owner_only", True),
                        "message_channels": sorted(str(item) for item in getattr(inst, "message_channels", set())),
                    },
                )
                base_client = ctx.client
                base_log = ctx.log
                base_messages = ctx.messages
                call_client = _trace_plugin_client(
                    base_client,
                    trace,
                    plugin_key=fkey,
                    component="legacy_userbot_dispatcher",
                    account_id=state.account_id,
                )
                call_log = base_log
                if base_log is not None and trace is not None:
                    async def _trace_log(
                        level: str,
                        message: str,
                        *,
                        _plugin_key: str = fkey,
                        _base_log: Any = base_log,
                        **detail: Any,
                    ) -> None:
                        detail.setdefault("trace_id", getattr(trace, "trace_id", None))
                        detail.setdefault("plugin_key", _plugin_key)
                        await _base_log(level, message, **detail)

                    call_log = _trace_log
                call_messages = base_messages
                if isinstance(base_messages, _LiveMessageOps):
                    call_messages = _LiveMessageOps(
                        base_messages._state,  # noqa: SLF001
                        plugin_key=fkey,
                        trace=trace,
                    )
                call_ctx = replace(ctx, client=call_client, log=call_log, messages=call_messages)
                plugin_event = _wrap_event_for_context(event, call_ctx)
                started = time.monotonic()
                try:
                    await handler(call_ctx, plugin_event)
                    invoked_count += 1
                    await record_span(
                        trace,
                        "plugin_invoke",
                        TRACE_STATUS_OK,
                        component="legacy_userbot_dispatcher",
                        plugin_key=fkey,
                        direction=event_label,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    await update_plugin_runtime_status(
                        account_id=account_id,
                        plugin_key=fkey,
                        last_invocation_status=TRACE_STATUS_OK,
                        last_trace_id=getattr(trace, "trace_id", None),
                    )
                except Exception as exc:  # noqa: BLE001
                    failed_count += 1
                    await record_span(
                        trace,
                        "plugin_invoke",
                        TRACE_STATUS_FAILED,
                        component="legacy_userbot_dispatcher",
                        plugin_key=fkey,
                        direction=event_label,
                        reason_code="plugin_runtime_error",
                        error=f"{type(exc).__name__}: {exc}",
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    await update_plugin_runtime_status(
                        account_id=account_id,
                        plugin_key=fkey,
                        last_invocation_status=TRACE_STATUS_FAILED,
                        last_trace_id=getattr(trace, "trace_id", None),
                    )
                    await _log(
                        redis,
                        account_id,
                        "error",
                        (
                            f"插件 {fkey} 处理{event_label}消息时出错："
                            f"{type(exc).__name__}: {exc}。"
                            "这条消息已跳过，其他插件和 worker 会继续运行。"
                        ),
                        source="plugin",
                        plugin_key=fkey,
                        direction=event_label,
                        chat_id=getattr(event, "chat_id", None),
                        sender_id=getattr(event, "sender_id", None),
                        message_preview=(getattr(event, "raw_text", "") or "")[:200],
                        traceback=traceback.format_exc(limit=8),
                        **trace_log_context(trace),
                    )
            final_status = (
                TRACE_STATUS_FAILED if failed_count
                else TRACE_STATUS_OK if invoked_count
                else TRACE_STATUS_SKIPPED
            )
            await finish_trace(
                trace,
                final_status,
                invoked_count=invoked_count,
                failed_count=failed_count,
                direction=event_label,
            )

        return _dispatch

    _make_dispatcher("outgoing", edited=True)
    _make_dispatcher("incoming", edited=True)
    _make_dispatcher("outgoing")
    _make_dispatcher("incoming")

    # ── 3) 加载该账号所有已启用 feature ──
    async with AsyncSessionLocal() as db:
        afs = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == account_id,
                    AccountFeature.enabled.is_(True),
                )
            )
        ).scalars().all()
        for af in afs:
            await _activate(db, state, af, redis)


# ─────────────────────────────────────────────────────
# 单 feature 激活（安全：双开关检查）
# ─────────────────────────────────────────────────────
async def _activate(db, state: _AccountState, af: AccountFeature, redis: Any) -> None:
    """根据 ``account_feature`` 行实例化对应插件，调 ``on_startup``，写状态。

    **安全：加载授权检查**
    - builtin 插件只看 AccountFeature.enabled
    - installed 插件必须有 installed_plugin 安装记录
    - installed 插件的全局开关、签名状态、账号开关都允许时才加载

    Args:
        db: AsyncSession
        state: _AccountState 实例
        af: AccountFeature 行
        redis: Redis 客户端
    """
    if af.feature_key == FEATURE_SCHEDULER:
        # scheduler 已是 worker 级平台基础能力，不再作为普通插件实例加载。
        # 保留 account_feature 行仅用于历史兼容和前端配置入口。
        await db.execute(
            update(AccountFeature)
            .where(
                AccountFeature.account_id == state.account_id,
                AccountFeature.feature_key == af.feature_key,
            )
            .values(state=FEATURE_STATE_ACTIVE, last_error=None)
        )
        await db.commit()
        return

    cls = get_plugin(af.feature_key)
    load_attempted = False
    if cls is None and _builtin_plugin_path(af.feature_key) is not None:
        load_attempted = True
        _load_builtin_plugin(af.feature_key)
        cls = get_plugin(af.feature_key)
    if cls is None:
        if _installed_plugin_exists(af.feature_key):
            auth = await _authorize_installed_plugin(
                db,
                af.feature_key,
                redis=redis,
                account_id=state.account_id,
            )
            if not auth.allowed:
                await _deny_installed_plugin_load(
                    db,
                    redis,
                    state.account_id,
                    af.feature_key,
                    auth,
                )
                return
            load_attempted = True
            _load_installed_plugin(af.feature_key)
            cls = get_plugin(af.feature_key)
    if cls is None:
        if load_attempted:
            last_error = "PLUGIN_LOAD_FAILED: plugin import failed or manifest invalid"
            log_message = f"feature {af.feature_key} 插件目录存在但加载失败或 manifest 无效"
        else:
            last_error, log_message = _missing_plugin_error(af.feature_key)
        await _log(
            redis,
            state.account_id,
            "warn",
            log_message,
        )
        await _write_account_feature_load_state(
            db,
            state.account_id,
            af.feature_key,
            state=FEATURE_STATE_FAILED,
            last_error=last_error,
        )
        return

    # ── 安全：installed 插件统一授权检查 ──
    plugin_source = getattr(cls, "_source", "builtin")
    if plugin_source == "installed":
        auth = await _authorize_installed_plugin(
            db,
            af.feature_key,
            redis=redis,
            account_id=state.account_id,
        )
        if not auth.allowed:
            await _deny_installed_plugin_load(
                db,
                redis,
                state.account_id,
                af.feature_key,
                auth,
            )
            return
    # ── installed 授权检查结束 ──

    # 拉规则（按 priority 倒序：值越大越先匹配）
    rules = (
        await db.execute(
            select(Rule)
            .where(
                Rule.account_id == state.account_id,
                Rule.feature_key == af.feature_key,
                Rule.enabled.is_(True),
            )
            .order_by(Rule.priority.desc())
        )
    ).scalars().all()

    inst = cls()
    # 阶段 C：第三方插件 (source="installed") 拿到的 client 走沙箱包装；
    # builtin 仍直接拿真 client（避免对原代码做改动）。
    plugin_client: Any = state.client
    plugin_source = getattr(cls, "_source", "builtin")
    plugin_manifest = getattr(cls, "_manifest", None)
    if plugin_source == "installed" and state.client is not None:
        from .sandbox import SandboxClient  # 延迟 import 避免循环

        perms = list(plugin_manifest.permissions) if plugin_manifest else []
        plugin_client = SandboxClient(
            state.client,
            perms,
            plugin_key=af.feature_key,
            account_id=state.account_id,
        )

    effective_config = await _merge_plugin_config(
        db, state.account_id, af.feature_key, dict(af.config or {})
    )
    account_config = dict(af.config or {})
    plugin_permissions = set(plugin_manifest.permissions or []) if plugin_manifest is not None else set()
    plugin_http: Any = None
    if plugin_manifest is not None and "external_http" in plugin_permissions:
        allowed_hosts = list(getattr(plugin_manifest, "allowed_hosts", None) or [])
        if allowed_hosts:
            from .http_facade import PluginHTTP  # 延迟 import，避免未用 HTTP 的插件增加依赖面

            plugin_http = PluginHTTP.from_context(
                PluginContext(
                    account_id=state.account_id,
                    feature_key=af.feature_key,
                    config=effective_config,
                    account_proxy_url=state.account_proxy_url,
                ),
                allowed_hosts=allowed_hosts,
                manifest_http=getattr(plugin_manifest, "http", None),
            )
    plugin_ai: Any = None
    if plugin_manifest is not None and plugin_permissions & {"ai_text", "ai_agent"}:
        from ...services.ai_feature import is_ai_enabled

        if await is_ai_enabled(db):
            from .ai_facade import PluginAI  # 延迟 import，避免未用 AI 的插件增加依赖面

            plugin_ai = PluginAI(
                account_id=state.account_id,
                plugin_key=af.feature_key,
                allow_agent="ai_agent" in plugin_permissions,
                manifest=plugin_manifest.to_dict(),
            )
    declared_facade_permissions = plugin_permissions & (
        _SUPPORTED_FACADE_PERMISSIONS | _RESERVED_UNSUPPORTED_FACADE_PERMISSIONS
    )
    unsupported_facade_permissions = (
        declared_facade_permissions & _RESERVED_UNSUPPORTED_FACADE_PERMISSIONS
    )
    for perm in sorted(unsupported_facade_permissions):
        await _log(
            redis,
            state.account_id,
            "warn",
            f"manifest 声明权限 {perm} 但当前平台未提供对应 facade，将被忽略",
            source="system",
            plugin_key=af.feature_key,
        )

    plugin_redis = state.redis or redis
    ctx = PluginContext(
        account_id=state.account_id,
        feature_key=af.feature_key,
        config=effective_config,
        account_config=account_config,
        rules=list(rules),
        client=plugin_client,
        engine=state.engine if plugin_source != "installed" else None,
        redis=plugin_redis,
        log=_make_logger(redis, state.account_id, af.feature_key),
        scheduler=(
            state.scheduler.for_plugin(af.feature_key, state.generation)
            if state.scheduler is not None else None
        ),
        http=plugin_http,
        ai=plugin_ai,
        messages=_LiveMessageOps(state, plugin_key=af.feature_key),
        generation=state.generation,
        account_proxy_url=state.account_proxy_url,
    )
    ctx.storage = PluginStorage.from_context(ctx)

    try:
        await inst.on_startup(ctx)
    except Exception as exc:  # noqa: BLE001
        if state.scheduler is not None:
            state.scheduler.unregister_owner(af.feature_key)
        await db.execute(
            update(AccountFeature)
            .where(
                AccountFeature.account_id == state.account_id,
                AccountFeature.feature_key == af.feature_key,
            )
            .values(state=FEATURE_STATE_FAILED, last_error=str(exc))
        )
        await db.commit()
        await update_plugin_runtime_status(
            account_id=state.account_id,
            plugin_key=af.feature_key,
            enabled=True,
            load_status="failed",
            installed_version=str(getattr(plugin_manifest, "version", "") or "") or None,
            last_load_error=str(exc),
        )
        await _log(
            redis,
            state.account_id,
            "error",
            f"插件 {af.feature_key} startup 失败: {exc}",
        )
        return

    state.instances[af.feature_key] = inst
    state.contexts[af.feature_key] = ctx

    # 暴露插件命令到 TG 命令分发表
    # 安全：传入 generation 和 plugin_key，以便 reload/disable 时能追踪并注销旧命令
    cmds = getattr(inst, "commands", None) or cls.commands or {}
    for cname, fn in cmds.items():
        register_plugin_command(
            cname,
            _wrap_cmd(fn, ctx),
            owner_plugin_key=af.feature_key,
            generation=state.generation,
        )
    _register_manifest_interaction_commands(
        state,
        ctx,
        plugin_manifest,
        effective_config,
    )

    await db.execute(
        update(AccountFeature)
        .where(
            AccountFeature.account_id == state.account_id,
            AccountFeature.feature_key == af.feature_key,
        )
        .values(state=FEATURE_STATE_ACTIVE, last_error=None)
    )
    await db.commit()
    await update_plugin_runtime_status(
        account_id=state.account_id,
        plugin_key=af.feature_key,
        enabled=True,
        load_status="loaded",
        installed_version=str(getattr(plugin_manifest, "version", "") or "") or None,
        last_load_error=None,
    )


def _entry_command_trigger(entry: dict[str, Any]) -> str | None:
    triggers = entry.get("triggers")
    command = None
    if isinstance(triggers, dict):
        command = triggers.get("command")
    value = str(command or "").strip().lstrip("/")
    return value or None


def _entry_trigger_mode(config: dict[str, Any], entry: dict[str, Any], command: str) -> str:
    raw = config.get("interaction_trigger_modes")
    entry_key = str(entry.get("key") or "").strip()
    if isinstance(raw, dict):
        raw = raw.get(entry_key, raw.get(command, raw.get("default")))
    if raw in (None, ""):
        raw = entry.get("default_trigger_modes")
    mode = str(raw or "all").strip().lower()
    return "keyword_only" if mode == "keyword_only" or bool(entry.get("keyword_only")) else "all"


def _entry_session_ttl(entry: dict[str, Any]) -> int:
    ttl = _int_or_none(entry.get("ttl_seconds") or entry.get("valid_seconds") or entry.get("session_ttl_seconds"))
    if ttl is None:
        ttl = 600
    return min(max(ttl, 30), 86400)


def _manifest_command_session_key(
    account_id: int,
    *,
    plugin_key: str,
    entry_key: str,
    entry: dict[str, Any],
    chat_id: int | None,
    user_id: int | None,
) -> str:
    scope = str(entry.get("session_scope") or "chat").strip()
    if scope == "none":
        scoped = "global"
    elif scope == "user":
        scoped = f"{chat_id or 0}:user:{user_id or 0}"
    else:
        scoped = str(chat_id or 0)
    return f"{_USERBOT_SESSION_KEY_PREFIX}{int(account_id)}:manifest_command:{plugin_key}:{entry_key}:{scoped}"


def _userbot_session_envelope_from_session(session_key: str, session: dict[str, Any]) -> dict[str, Any]:
    data = session.get("data") if isinstance(session.get("data"), dict) else {}
    return {
        "key": session_key,
        "active": True,
        "channel": "userbot",
        "data": dict(data),
        "rule_id": session.get("rule_id"),
        "rule_name": session.get("rule_name"),
        "module_key": session.get("module_key"),
        "entry_key": session.get("entry_key"),
        "chat_id": _int_or_none(session.get("chat_id")),
        "started_by_user_id": _int_or_none(session.get("started_by_user_id")),
        "started_by_message_id": _int_or_none(session.get("started_by_message_id")),
        "source_user_id": _int_or_none(session.get("source_user_id")),
        "event_type": session.get("event_type"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "expires_at": session.get("expires_at"),
    }


async def _create_manifest_command_userbot_session(
    state: _AccountState,
    *,
    plugin_key: str,
    entry: dict[str, Any],
    command: str,
    args: list[str],
    event: Any,
    redis: Any,
) -> tuple[str, dict[str, Any]]:
    entry_key = str(entry.get("key") or "").strip()
    chat_id = _int_or_none(getattr(event, "chat_id", None))
    sender_id = _int_or_none(getattr(event, "sender_id", None))
    message_id = _int_or_none(getattr(getattr(event, "message", None), "id", None) or getattr(event, "id", None))
    session_key = _manifest_command_session_key(
        state.account_id,
        plugin_key=plugin_key,
        entry_key=entry_key,
        entry=entry,
        chat_id=chat_id,
        user_id=sender_id,
    )
    existing = _json_dict(await redis.get(session_key))
    ttl = _entry_session_ttl(entry)
    now = time.time()
    session = {
        "account_id": state.account_id,
        "chat_id": chat_id,
        "rule_id": f"manifest_command:{plugin_key}:{entry_key}",
        "rule_name": str(entry.get("name") or entry.get("title") or ""),
        "module_key": plugin_key,
        "entry_key": entry_key,
        "channel": "userbot",
        "started_by_user_id": sender_id,
        "started_by_message_id": message_id,
        "source_user_id": sender_id,
        "event_type": "command",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "expires_at": now + ttl,
        "data": {
            **dict(existing.get("data") if isinstance(existing.get("data"), dict) else {}),
            "command": command,
            "args": list(args),
            "args_raw": " ".join(args),
        },
    }
    await redis.set(session_key, json.dumps(session, ensure_ascii=False), ex=ttl + _USERBOT_SESSION_TTL_GRACE_SECONDS)
    if chat_id is not None:
        state.userbot_session_chats.add(chat_id)
    return session_key, session


def _manifest_command_payload(
    state: _AccountState,
    event: Any,
    *,
    plugin_key: str,
    entry_key: str,
    command: str,
    args: list[str],
    session_key: str,
    session: dict[str, Any],
) -> dict[str, Any]:
    payload = normalize_userbot_event(
        state.account_id,
        event,
        command_meta={
            "type": "command",
            "channel": "userbot",
            "command": command,
            "args": list(args),
            "args_raw": " ".join(args),
            "plugin_key": plugin_key,
            "entry_key": entry_key,
            "session_key": session_key,
        },
    )
    trace_id = str(getattr(event, "trace_id", "") or "").strip()
    if trace_id:
        payload["trace_id"] = trace_id
    source = dict(payload.get("source") if isinstance(payload.get("source"), dict) else {})
    source.update({"type": "command", "channel": "userbot"})
    payload["source"] = source
    payload["event_type"] = "command"
    trigger = dict(payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {})
    trigger.update({"args": list(args), "args_raw": " ".join(args)})
    payload["trigger"] = trigger
    payload["session"] = _userbot_session_envelope_from_session(session_key, session)
    return payload


def _wrap_manifest_interaction_command(
    state: _AccountState,
    *,
    plugin_key: str,
    entry: dict[str, Any],
    command: str,
):
    async def w(client, event, args, account_id):  # noqa: ANN001
        redis = state.redis or get_redis()
        entry_key = str(entry.get("key") or "").strip()
        session_key, session = await _create_manifest_command_userbot_session(
            state,
            plugin_key=plugin_key,
            entry=entry,
            command=command,
            args=list(args or []),
            event=event,
            redis=redis,
        )
        payload = _manifest_command_payload(
            state,
            event,
            plugin_key=plugin_key,
            entry_key=entry_key,
            command=command,
            args=list(args or []),
            session_key=session_key,
            session=session,
        )
        try:
            actions = await invoke_interaction_entry(
                state.account_id,
                plugin_key=plugin_key,
                entry_key=entry_key,
                payload=payload,
                default_send_via=["userbot_reply"],
            )
        except Exception:
            delete = getattr(redis, "delete", None)
            if callable(delete):
                await delete(session_key)
            raise
        await _apply_userbot_event_bus_actions(
            state,
            str(payload.get("trace_id") or "") or None,
            event,
            plugin_key=plugin_key,
            entry_key=entry_key,
            actions=actions,
            redis=redis,
            session_key=session_key,
            session=session,
        )

    return w


def _register_manifest_interaction_commands(
    state: _AccountState,
    ctx: PluginContext,
    manifest: Manifest | None,
    config: dict[str, Any],
) -> None:
    if manifest is None:
        return
    for raw_entry in getattr(manifest, "interaction_entries", None) or []:
        entry = account_bot_service.normalize_interaction_entry_manifest(raw_entry)
        if not entry:
            continue
        command = _entry_command_trigger(entry)
        if not command:
            continue
        if _entry_trigger_mode(config, entry, command) == "keyword_only":
            continue
        register_plugin_command(
            command,
            _wrap_manifest_interaction_command(
                state,
                plugin_key=ctx.feature_key,
                entry=entry,
                command=command,
            ),
            owner_plugin_key=ctx.feature_key,
            generation=state.generation,
        )


def _wrap_cmd(fn, ctx: PluginContext):
    """把插件 ``commands`` 里登记的 5 参数 handler 包成命令分发期望的 4 参数签名。"""

    async def w(client, event, args, account_id):  # noqa: ANN001
        trace_id = str(getattr(event, "trace_id", "") or "").strip() or None
        base_client = ctx.client
        base_log = ctx.log
        base_messages = ctx.messages
        call_messages = base_messages
        if isinstance(base_messages, _LiveMessageOps):
            call_messages = _LiveMessageOps(
                base_messages._state,  # noqa: SLF001
                plugin_key=ctx.feature_key,
                trace=trace_id,
            )
        call_log = base_log
        if base_log is not None and trace_id:
            async def _trace_log(level: str, message: str, **detail: Any) -> None:
                detail.setdefault("trace_id", trace_id)
                detail.setdefault("plugin_key", ctx.feature_key)
                await base_log(level, message, **detail)

            call_log = _trace_log
        call_client = _trace_plugin_client(
            base_client if base_client is not None else client,
            trace_id,
            plugin_key=ctx.feature_key,
            component="plugin_command",
            account_id=ctx.account_id,
        )
        call_ctx = replace(ctx, client=call_client, log=call_log, messages=call_messages)
        plugin_event = _wrap_event_for_context(event, call_ctx)
        await fn(call_ctx.client if call_ctx.client is not None else client, plugin_event, args, account_id, call_ctx)

    return w


def _wrap_event_for_context(event: Any, ctx: PluginContext) -> Any:
    """Installed plugins receive a SandboxEvent so event helpers honor permissions."""

    client = ctx.client
    if client is None:
        return event
    try:
        from .sandbox import SandboxEvent
    except Exception:  # noqa: BLE001
        return event
    if bool(getattr(client, "_is_sandboxed", False)) or bool(getattr(client, "is_sandbox_client", False)):
        return SandboxEvent(event, client, plugin_key=ctx.feature_key)
    return event


def _make_logger(redis: Any, account_id: int, plugin_key: str):
    """构造一个 ctx.log 协程，写到 ``runtime_log_stream``。"""

    async def _writer(level: str, message: str, **detail: Any) -> None:
        source = str(detail.pop("source", "plugin"))
        detail.pop("plugin_key", None)
        await _log(
            redis,
            account_id,
            level,
            message,
            source=source,
            plugin_key=plugin_key,
            **detail,
        )

    return _writer


# ─────────────────────────────────────────────────────
# 配置合并：_merge_plugin_config
# ─────────────────────────────────────────────────────
async def _merge_plugin_config(
    db: AsyncSessionLocal,
    account_id: int,
    feature_key: str,
    account_config: dict[str, Any],
) -> dict[str, Any]:
    """合并插件配置。

    合并顺序：schema defaults < global config < account config

    - global config 存储在 plugin_global_config 表中
    - 新表缺行时兼容读取 Feature.manifest["global_config"]
    - 合并时只取 account_config 中非 global 字段
    """
    from ...db.models.feature import Feature

    # 获取 feature manifest
    feature = await db.get(Feature, feature_key)
    if feature is None:
        return account_config

    manifest = feature.manifest or {}
    config_schema = manifest.get("config_schema", {})
    global_row = await db.get(PluginGlobalConfig, feature_key)
    if global_row is not None:
        global_config = dict(global_row.config or {})
    else:
        legacy_config = manifest.get("global_config", {})
        global_config = dict(legacy_config) if isinstance(legacy_config, dict) else {}

    # 提取 schema defaults
    defaults: dict[str, Any] = {}
    properties = config_schema.get("properties", {})
    for prop_name, prop_def in properties.items():
        if isinstance(prop_def, dict) and "default" in prop_def:
            defaults[prop_name] = prop_def["default"]

    # 提取 global 字段名
    global_fields = {
        k for k, v in properties.items()
        if isinstance(v, dict) and v.get("level") == "global"
    }

    # 提取 account 专属字段（排除 global 字段）
    account_only_config = {k: v for k, v in account_config.items() if k not in global_fields}

    # 合并：defaults < global < account_only
    result = {**defaults}
    for key in global_fields:
        if key in global_config:
            result[key] = global_config[key]
        elif key in account_config:
            # Compatibility for configs saved before a plugin moved a field to
            # level="global". Keep the old account-level value usable until
            # the next successful global-config save migrates it.
            result[key] = account_config[key]
    result.update(account_only_config)

    return result


# ─────────────────────────────────────────────────────
# 配置热更新：reload_account_config
# ─────────────────────────────────────────────────────
async def reload_account_config(account_id: int, payload: dict | None = None) -> None:
    """收到 IPC ``reload_config`` 时调用：

    - **先刷新 BUILTIN_FEATURES**：动态重扫 builtin 目录，让新增插件立即可见
    - builtin / installed 插件都在 ``_activate`` 中按需加载，避免每次热更新导入全部实现
    - 已实例化的 feature：刷新 ``ctx.config`` / ``ctx.rules``；若该 feature 已被禁用则 shutdown
    - 数据库新增的 enabled feature：调 ``_activate`` 加载

    任何异常都吞掉，热更新失败不应让 worker 崩溃。
    """
    state = _STATES.get(account_id)
    if state is None:
        return
    next_generation = state.generation + 1
    redis = state.redis or get_redis()

    # 同步全局开关：让 reload_config 也能让 incoming-message 可见性日志即时生效
    state.log_incoming_messages = await _load_log_incoming_messages_setting()
    await _refresh_interaction_text_guard_cache(state)
    await _refresh_userbot_session_chat_cache(state)

    # 刷新动态发现的 BUILTIN_FEATURES，让新增 builtin 插件目录立即可见
    try:
        from ...db.models.feature import BUILTIN_FEATURES  # noqa: PLC0415
        BUILTIN_FEATURES.refresh()
    except Exception:  # noqa: BLE001
        log.exception("reload_account_config 时刷新 BUILTIN_FEATURES 失败")

    reload_plugin_key = None
    if isinstance(payload, dict):
        raw_key = payload.get("plugin_key")
        if isinstance(raw_key, str) and raw_key:
            reload_plugin_key = raw_key
            _clear_installed_module_cache(raw_key)

    async with AsyncSessionLocal() as db:
        acc = await db.get(Account, account_id)
        owner_id = getattr(acc, "tg_user_id", None)
        state.owner_tg_user_id = int(owner_id) if owner_id else None
        sudo_rows = (
            await db.execute(select(SudoUser).where(SudoUser.account_id == account_id))
        ).scalars().all()
        state.sudo_users = {
            int(r.tg_user_id): {
                "allowed_chat_ids": list(r.allowed_chat_ids or []),
                "allowed_commands": list(r.allowed_commands or []),
            }
            for r in sudo_rows
        }

        # 1) 现有实例：刷新或卸载
        for fkey, inst in list(state.instances.items()):
            af = (
                await db.execute(
                    select(AccountFeature).where(
                        AccountFeature.account_id == account_id,
                        AccountFeature.feature_key == fkey,
                    )
                )
            ).scalar_one_or_none()
            cls = get_plugin(fkey) or type(inst)
            plugin_source = getattr(cls, "_source", getattr(type(inst), "_source", "builtin"))
            auth_denied: _InstalledPluginAuthorization | None = None
            runtime_drift = False
            runtime_drift_reason: str | None = None
            if plugin_source == "installed":
                installed_plugin = await db.get(InstalledPlugin, fkey)
                auth = await _authorize_installed_plugin(
                    db,
                    fkey,
                    redis=redis,
                    account_id=account_id,
                )
                if not auth.allowed:
                    auth_denied = auth
                elif reload_plugin_key != fkey:
                    runtime_drift, runtime_drift_reason = _installed_plugin_runtime_drift(
                        cls,
                        inst,
                        installed_plugin,
                    )

            force_reload = reload_plugin_key == fkey or runtime_drift
            if af is None or not af.enabled or auth_denied is not None or force_reload:
                ctx = state.contexts.get(fkey)
                inst = state.instances.get(fkey)

                if runtime_drift:
                    _clear_installed_module_cache(fkey)
                    await _log(
                        redis,
                        account_id,
                        "info",
                        f"检测到插件 {fkey} 已更新但内存实例仍是旧版本，正在重载。",
                        source="system",
                        plugin_key=fkey,
                        reason=runtime_drift_reason,
                    )

                # ── 安全：先注销该插件的所有命令 ──
                if inst is not None:
                    unregister_all_plugin_commands(owner_plugin_key=fkey)
                    if state.scheduler is not None:
                        state.scheduler.unregister_owner(fkey)

                # 调用 shutdown（幂等设计）
                if ctx is not None and inst is not None:
                    try:
                        await inst.on_shutdown(ctx)
                    except Exception:  # noqa: BLE001
                        log.exception("on_shutdown 失败 feature=%s", fkey)

                state.instances.pop(fkey, None)
                state.contexts.pop(fkey, None)
                if af is not None and not af.enabled:
                    # 同时写状态为 disabled，便于前端展示
                    await _write_account_feature_load_state(
                        db,
                        account_id,
                        fkey,
                        state=FEATURE_STATE_DISABLED,
                        last_error=None,
                    )
                elif af is not None and auth_denied is not None:
                    await _deny_installed_plugin_load(
                        db,
                        redis,
                        account_id,
                        fkey,
                        auth_denied,
                    )
                continue
            # 仍启用：刷新 rules + config
            rules = (
                await db.execute(
                    select(Rule)
                    .where(
                        Rule.account_id == account_id,
                        Rule.feature_key == fkey,
                        Rule.enabled.is_(True),
                    )
                    .order_by(Rule.priority.desc())
                )
            ).scalars().all()
            ctx = state.contexts[fkey]

            # 合并配置：schema defaults < global config < account config
            old_config = dict(ctx.config or {})
            new_config = await _merge_plugin_config(db, account_id, fkey, dict(af.config or {}))
            command_config_keys = set(getattr(inst, "command_config_keys", set()) or set())
            command_config_changed = any(
                old_config.get(k) != new_config.get(k) for k in command_config_keys
            )
            if command_config_changed:
                unregister_all_plugin_commands(owner_plugin_key=fkey)
                if state.scheduler is not None:
                    state.scheduler.unregister_owner(fkey)
                try:
                    await inst.on_shutdown(ctx)
                except Exception:  # noqa: BLE001
                    log.exception("命令配置变化后 on_shutdown 失败 feature=%s", fkey)
                state.instances.pop(fkey, None)
                state.contexts.pop(fkey, None)
                await _activate(db, state, af, redis)
                continue

            ctx.config = new_config
            ctx.account_config = dict(af.config or {})
            ctx.rules = list(rules)
            ctx.generation = state.generation
            ctx.scheduler = (
                state.scheduler.for_plugin(fkey, state.generation)
                if state.scheduler is not None else None
            )

        # 2) 处理新增的 enabled feature
        afs = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == account_id,
                    AccountFeature.enabled.is_(True),
                )
            )
        ).scalars().all()
        for af in afs:
            if af.feature_key not in state.instances:
                await _activate(db, state, af, redis)

    state.generation = next_generation
    for fkey, ctx in state.contexts.items():
        ctx.generation = next_generation
        ctx.scheduler = (
            state.scheduler.for_plugin(fkey, next_generation)
            if state.scheduler is not None else None
        )

    reload_source = str(payload.get("source") or "") if isinstance(payload, dict) else ""
    if reload_source != "periodic_reconcile":
        await _log(
            redis,
            account_id,
            "info",
            "插件配置已热更新",
            reload_source=reload_source or None,
        )


# ─────────────────────────────────────────────────────
# 单插件热重载：reload_plugin
# ─────────────────────────────────────────────────────
async def reload_plugin(account_id: int, plugin_key: str | None) -> None:
    """热重载单个插件并重新激活。

    builtin 走 importlib.reload；installed 先清模块缓存，再让 _activate 在 DB 双开关
    通过后重新加载。
    """
    if not plugin_key:
        return
    state = _STATES.get(account_id)
    if state is None:
        return
    state.generation += 1
    redis = state.redis or get_redis()
    await _refresh_interaction_text_guard_cache(state)
    await _refresh_userbot_session_chat_cache(state)

    # 1) 先注销旧插件命令（如果有）
    if plugin_key in state.instances:
        unregister_all_plugin_commands(owner_plugin_key=plugin_key)
        if state.scheduler is not None:
            state.scheduler.unregister_owner(plugin_key)

    # 2) shutdown 旧实例（幂等设计）
    if plugin_key in state.instances:
        try:
            await state.instances[plugin_key].on_shutdown(state.contexts[plugin_key])
        except Exception:  # noqa: BLE001
            log.exception("on_shutdown 失败 feature=%s", plugin_key)
        state.instances.pop(plugin_key, None)
        state.contexts.pop(plugin_key, None)

    # 2) reload 模块
    builtin_path = _builtin_plugin_path(plugin_key)
    if builtin_path is None:
        _clear_installed_module_cache(plugin_key)
    else:
        try:
            # 目录型 builtin 可能有 client/importers/token_pool 等辅助模块。
            # 直接清掉整个包再重新加载，避免 plugin.py 引到旧的子模块对象。
            _clear_builtin_module_cache(plugin_key)
            loaded = _load_dir(builtin_path, source="builtin")
            if plugin_key not in loaded:
                raise RuntimeError(f"builtin plugin {plugin_key} reload returned no plugin class")
        except Exception as exc:  # noqa: BLE001
            await _log(redis, account_id, "error", f"reload {plugin_key} 失败: {exc}")
            return

    # 3) 重新激活
    async with AsyncSessionLocal() as db:
        af = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == account_id,
                    AccountFeature.feature_key == plugin_key,
                )
            )
        ).scalar_one_or_none()
        if af is not None and af.enabled:
            await _activate(db, state, af, redis)
    await _log(redis, account_id, "info", f"插件 {plugin_key} 已重载")


# ─────────────────────────────────────────────────────
# 写运行日志的便利函数
# ─────────────────────────────────────────────────────
async def _log(
    redis: Any,
    account_id: int | None,
    level: str,
    message: str,
    *,
    source: str = "event",
    **detail: Any,
) -> None:
    """写入 ``runtime_log_stream``，主进程批量消费落库。任何异常吞掉。

    source 语义（前端 Logs 页 tab 区分）：
    - ``"event"``（loader 默认）  — incoming 消息事件 / plugin 命中 / 命令派发
    - ``"system"``                — plugin 内部错误 / 加载失败等技术异常应显式传

    历史数据里也会出现 ``"plugin"`` 旧值，API 层做了别名映射。
    """
    try:
        payload = RuntimeLogPayload(
            account_id=account_id,
            level=level,  # type: ignore[arg-type]
            source=source,
            message=message,
            detail=detail or None,
        )
        await redis.rpush(RUNTIME_LOG_STREAM, payload.encode())
    except Exception:  # noqa: BLE001
        log.exception("写 runtime_log_stream 失败 account=%s", account_id)


# 测试与外部需要时可用：列出当前所有已注册的 plugin 类
def registered_plugins() -> dict[str, type[Plugin]]:
    """便于测试 / 调试：返回当前注册表副本。"""
    return all_plugins()


# ─────────────────────────────────────────────────────
# Sprint2 #3：允许 peer（沿用 ignored_peer 表）+ 最近活跃 peer
# ─────────────────────────────────────────────────────
async def _load_ignored_peers(state: _AccountState) -> None:
    """从 ``ignored_peer`` 表把当前账号的所有 peer_id 装进内存 set（允许名单语义）。

    任何异常都吞掉——失败时退化为"空名单"，等价于允许全部，业务侧不至于挂。
    """
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(IgnoredPeer.peer_id).where(
                        IgnoredPeer.account_id == state.account_id
                    )
                )
            ).scalars().all()
        state.ignored_peers = {int(pid) for pid in rows}
    except Exception:  # noqa: BLE001
        log.exception("加载允许名单失败 account=%s", state.account_id)
        state.ignored_peers = set()


def _classify_peer(event: Any) -> str:
    """把 Telethon event 的会话类型归一化为 ``private/group/supergroup/channel``。

    supergroup 与 channel 都属于 ``is_channel``；通过 chat_id 的 -100 前缀区分
    （Telegram 协议约定，supergroup 与 channel 的 chat_id 都以 -100 开头，
    我们这里粗略把"是 group 又是 channel 的"当作 supergroup）。
    """
    try:
        if event.is_private:
            return "private"
        if event.is_channel and not event.is_group:
            return "channel"
        if event.is_group and event.is_channel:
            return "supergroup"
        return "group"
    except Exception:  # noqa: BLE001
        return "private"


async def _record_recent_peer(state: _AccountState, event: Any) -> None:
    """把当前 event 的 peer 写入 LRU；超出上限则丢最旧。

    会尝试调 ``event.get_chat()`` 拿群名/用户名作为 ``peer_label``，失败则用 chat_id 字符串兜底。
    异常一律吞掉——这条 LRU 只是 UI 辅助，不能影响主流程。
    """
    pid = event.chat_id
    if pid is None:
        return
    try:
        kind = _classify_peer(event)
        label: str | None
        try:
            chat = await event.get_chat()
            label = public_entity_display_name(chat, fallback_id=pid)
        except Exception:  # noqa: BLE001
            label = str(pid)
        state.recent_peers[pid] = {
            "peer_kind": kind,
            "peer_label": label,
            "ts": time.time(),
        }
        # OrderedDict.move_to_end 把"最近一次写入的 peer"挪到末尾，实现 LRU
        state.recent_peers.move_to_end(pid)
        while len(state.recent_peers) > RECENT_PEERS_LIMIT:
            state.recent_peers.popitem(last=False)
    except Exception:  # noqa: BLE001
        log.exception("维护 recent_peers 失败 account=%s pid=%s", state.account_id, pid)


async def reload_ignored_peers(account_id: int, *, log_level: str | None = "info") -> None:
    """IPC ``reload_ignored`` 入口：从 DB 重新拉一遍名单。

    若该账号在本进程没有运行态（worker 未起 / 已退出），静默忽略。
    """
    state = _STATES.get(account_id)
    if state is None:
        return
    await _load_ignored_peers(state)
    if log_level is None or log_level == "silent":
        return
    redis = state.redis or get_redis()
    await _log(
        redis,
        account_id,
        log_level,
        f"允许群组名单已热更新（共 {len(state.ignored_peers)} 个 peer）",
    )


def get_recent_peers(account_id: int) -> list[dict[str, Any]]:
    """IPC ``get_recent_peers`` 应答：返回当前账号最近活跃 peer 列表。

    顺序：最新 → 最旧（OrderedDict 末尾是最近写入的，所以反向遍历）。
    若该账号在本进程没有运行态，返回空列表。
    """
    state = _STATES.get(account_id)
    if state is None:
        return []
    out: list[dict[str, Any]] = []
    for pid, info in reversed(state.recent_peers.items()):
        out.append(
            {
                "peer_id": int(pid),
                "peer_kind": info.get("peer_kind") or "private",
                "peer_label": info.get("peer_label"),
                "ts": float(info.get("ts") or 0.0),
            }
        )
    return out


_INTERACTION_SEND_ACTIONS = {"send_message", "send_photo", "send_file", "edit_message", "edit_caption"}
_INTERACTION_CONTROL_ACTIONS = {"delete_message", "pin_message"}


def _normalize_interaction_action(
    raw: dict[str, Any],
    *,
    default_send_via: list[str] | None = None,
) -> dict[str, Any]:
    """保持旧动作兼容，同时给新版发送动作补齐默认发送通道。"""

    action = dict(raw)
    raw_channel_selector = _raw_interaction_channel_selector(action)
    has_explicit_send_via = action_send_via_raw_selector(action) not in (None, "")
    action_type = str(action.get("type") or "").strip()
    action["type"] = action_type
    if action_type in _INTERACTION_SEND_ACTIONS or action_type in _INTERACTION_CONTROL_ACTIONS:
        send_via_options = action_send_via_options(action)
        if not has_explicit_send_via and default_send_via:
            send_via_options = list(default_send_via)
        apply_action_send_via_options(action, send_via_options)
        if raw_channel_selector is not None and _channel_selector_needs_guard_trace(raw_channel_selector):
            action["channel_selector"] = raw_channel_selector
    if isinstance(action.get("settlement"), dict):
        action["settlement"] = dict(action["settlement"])
    return action


def _raw_interaction_channel_selector(action: dict[str, Any]) -> Any:
    if "channel_selector" in action:
        return action.get("channel_selector")
    if "channel" in action:
        return action.get("channel")
    if "send_via_options" in action:
        return action.get("send_via_options")
    return None


def _channel_selector_needs_guard_trace(selector: Any) -> bool:
    return isinstance(selector, (dict, list, tuple, set))


def _normalize_interaction_actions(
    actions: list[dict[str, Any]],
    *,
    default_send_via: list[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        _normalize_interaction_action(item, default_send_via=default_send_via)
        for item in actions
        if isinstance(item, dict)
    ]


async def invoke_interaction_entry(
    account_id: int,
    *,
    plugin_key: str,
    entry_key: str,
    payload: dict[str, Any],
    default_send_via: list[str] | None = None,
) -> list[dict[str, Any]]:
    """调用已加载插件的交互 Bot 入口，返回平台标准动作。"""

    state = _STATES.get(account_id)
    if state is None:
        raise RuntimeError("账号 worker 尚未运行")
    plugin_key = str(plugin_key or "").strip()
    entry_key = str(entry_key or "").strip()
    if not plugin_key or not entry_key:
        raise ValueError("缺少 plugin_key 或 entry_key")
    inst = state.instances.get(plugin_key)
    ctx = state.contexts.get(plugin_key)
    if inst is None or ctx is None:
        raise RuntimeError(f"模块未加载或未启用：{plugin_key}")

    base_log = ctx.log
    base_client = ctx.client
    trace_id = str((payload or {}).get("trace_id") or "").strip()
    buffered_messages = _InteractionEntryMessageOps(
        state,
        plugin_key=plugin_key,
        entry_key=entry_key,
        trace=trace_id,
        default_send_via=default_send_via,
    )
    call_client = _trace_plugin_client(
        base_client,
        trace_id,
        plugin_key=plugin_key,
        entry_key=entry_key,
        component="interaction_entry",
        account_id=ctx.account_id,
    )

    call_log = base_log
    if base_log is not None and trace_id:
        async def _trace_log(level: str, message: str, **detail: Any) -> None:
            detail.setdefault("trace_id", trace_id)
            detail.setdefault("plugin_key", plugin_key)
            detail.setdefault("entry_key", entry_key)
            await base_log(level, message, **detail)

        call_log = _trace_log
    call_ctx = replace(ctx, messages=buffered_messages, client=call_client, log=call_log)
    plugin_payload = attach_tp_event(dict(payload or {}))
    actions = await inst.on_interaction(call_ctx, entry_key, plugin_payload)
    if actions is None and not buffered_messages.actions:
        raise RuntimeError(f"模块尚未实现交互入口：{plugin_key}.{entry_key}")
    if actions is None:
        actions = []
    if not isinstance(actions, list) or not all(isinstance(item, dict) for item in actions):
        raise TypeError("交互入口必须返回 list[dict] 标准动作")
    return _normalize_interaction_actions([*buffered_messages.actions, *actions], default_send_via=default_send_via)


__all__ = [
    "RECENT_PEERS_LIMIT",
    "discover_plugins",
    "dispatch_webhook_event",
    "evaluate_dispatch",
    "get_recent_peers",
    "invoke_interaction_entry",
    "load_plugins_for_account",
    "registered_plugins",
    "reload_account_config",
    "reload_ignored_peers",
    "reload_plugin",
    "scan_userbot_expired_sessions_once",
]
