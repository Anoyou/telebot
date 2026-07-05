"""账号绑定 Bot 的配置、授权用户与 Bot API 轻量封装。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from hmac import compare_digest
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_bot_defaults import (
    DEFAULT_INTERACTION_DISABLED_MESSAGE,
    DEFAULT_INTERACTION_QUERY_COMMANDS,
    DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE,
    DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
    DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
    DEFAULT_INTERACTION_RESPONSE_TEMPLATE,
    DEFAULT_DEBIT_NOTICE_TEMPLATE,
    DEFAULT_TRANSFER_NOTICE_TEMPLATE,
    LEGACY_TRANSFER_NOTICE_TEMPLATE,
)
from ..crypto import decrypt_str, encrypt_str
from ..db.models.account import Account
from ..db.models.account_bot import (
    ACCOUNT_BOT_ROLE_ADMIN,
    ACCOUNT_BOT_ROLE_OPERATOR,
    ACCOUNT_BOT_ROLE_VIEWER,
    ACCOUNT_BOT_ROLES,
    ACCOUNT_BOT_STATUS_DISABLED,
    ACCOUNT_BOT_STATUS_ERROR,
    AccountBot,
    AccountBotUser,
)
from ..db.models.feature import FEATURE_STATE_DISABLED, AccountFeature, Feature
from ..db.models.system import SystemSetting
from ..feature_registry import BUILTIN_FEATURES
from ..schemas.account_bot import (
    AccountBotConfigResponse,
    AccountBotConfigUpdate,
    AccountBotRemotePluginPolicy,
    AccountBotUserCreate,
    AccountBotUserUpdate,
)
from ..settings import settings
from .event_bus import VALID_EVENT_TYPES
from .interaction.contracts import send_via_selector_options, unsupported_send_via_values

log = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"
BOT_API_TIMEOUT = httpx.Timeout(connect=5.0, read=35.0, write=10.0, pool=5.0)
BOT_MESSAGE_TEXT_LIMIT = 4000
BOT_MESSAGE_CAPTION_LIMIT = 1024
TRANSFER_NOTICE_SETTING_PREFIX = "account_bot_transfer_notice:"
VALID_TRIGGER_MODES = {"payment", "keyword", "both"}
VALID_AMOUNT_MATCH_MODES = {"eq", "gte"}
VALID_CONCURRENCY = {"chat", "user", "none"}
VALID_INTERACTION_EVENTS = set(VALID_EVENT_TYPES)
VALID_INTERACTION_LAUNCH_MODES = {"bridge", "direct", "hybrid"}
VALID_INTERACTION_SEND_VIA = {"interaction_bot", "userbot_reply"}
TRUSTED_INTERACTION_SEND_VIA = ["interaction_bot", "userbot_reply"]
VALID_INTERACTION_DISPATCH_MODES = {"admin_command", "public_keyword"}
VALID_INTERACTION_MESSAGE_CHANNELS = {"interaction_bot", "userbot_reply", "auto"}
VALID_INTERACTION_PARTICIPANT_POLICIES = {"open_race", "solo_owner", "paid_pool", "notify_only"}
FALLBACK_CHAT_SESSION_MODULE_ENTRIES = {
    ("dice_grid_hunt", "start_dice_grid_hunt"),
    ("dice_grid_hunt", "answer_dice_grid_hunt"),
    ("game24", "start_paid_game"),
    ("math10", "start_math10"),
    ("guess_number", "start_game"),
}
FALLBACK_NO_PRIZE_MODULE_ENTRIES = {
    ("pt_promote", "promote_torrent"),
}
FALLBACK_SOLO_OWNER_MODULE_ENTRIES = {
    ("blackjack", "start_blackjack"),
}
RULE_CONTROLLED_MODULE_CONFIG_KEYS = {"prize", "valid_seconds"}
DEFAULT_MATH10_START_KEYWORDS = ["发十以内算数", "十以内算数", "开算数题"]

ROLE_RANK = {
    ACCOUNT_BOT_ROLE_VIEWER: 0,
    ACCOUNT_BOT_ROLE_OPERATOR: 1,
    ACCOUNT_BOT_ROLE_ADMIN: 2,
}

_BOT_API_CLIENT: httpx.AsyncClient | None = None
_BOT_API_CLIENT_LOCK = asyncio.Lock()


def role_allows(role: str, required: str) -> bool:
    """角色权限比较：viewer < operator < admin。"""

    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(required, 99)


def _bad(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def sanitize_bot_error(exc: BaseException | str, *, token: str | None = None) -> str:
    """把 Bot API 错误脱敏，避免把 token、URL、路径打到 DB 或前端。"""

    text = str(exc)
    if token:
        text = text.replace(token, "***")
    text = text.replace("https://api.telegram.org", "Bot API")
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", text)
    text = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", text)
    text = re.sub(r"(?<!\w)/(?:Users|private|var|tmp|opt|home)/[^\s\"']+", "[path]", text)
    if len(text) > 500:
        text = text[:500] + "..."
    return text or type(exc).__name__


def label_bot_polling_error(clean: str, *, role: str) -> str:
    """给 getUpdates 冲突补上 Bot 角色，方便区分管理 Bot / 交互 Bot。"""

    if "terminated by other getUpdates request" not in clean and "Conflict" not in clean:
        return clean
    if role == "interaction":
        return "交互 Bot polling 冲突：同一个 Bbot token 正在被另一个实例监听。请确认它没有被管理 Bot、其他账号、本地/Docker/VPS 中的另一套 TelePilot，或其他程序同时使用。"
    if role == "transfer_test":
        return "转账结果通知 Bot polling 冲突：同一个测试 Abot token 正在被另一个实例监听。请确认它没有被管理 Bot、交互 Bot、其他账号、本地/Docker/VPS 中的另一套 TelePilot，或其他程序同时使用。"
    return "管理 Bot polling 冲突：同一个管理 Bot token 正在被另一个实例监听。请确认它没有被交互 Bot、其他账号、本地/Docker/VPS 中的另一套 TelePilot，或其他程序同时使用。"


def _encrypted_token_matches_plain(token_enc: str | None, token: str) -> bool:
    if not token_enc or not token:
        return False
    try:
        return compare_digest(decrypt_str(token_enc), token)
    except ValueError:
        return False


def default_remote_plugin_policy() -> dict[str, bool]:
    return {
        "enabled": False,
        "install": False,
        "update": False,
        "uninstall": False,
        "enable_disable": False,
    }


def normalize_remote_plugin_policy(raw: Any) -> dict[str, bool]:
    base = default_remote_plugin_policy()
    if not isinstance(raw, dict):
        return base
    for key in base:
        if key in raw:
            base[key] = bool(raw[key])
    return base


def _strip_rule_controlled_module_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if str(key) not in RULE_CONTROLLED_MODULE_CONFIG_KEYS
    }


def _sanitize_module_config_for_entry(
    raw: Any,
    module_key: str | None,
    module_action: str | None,
) -> dict[str, Any]:
    config = _strip_rule_controlled_module_config(raw)
    if not config or not module_key or not module_action:
        return config
    entry = declared_module_entry_manifest(module_key, module_action)
    schema = entry.get("input_schema") if isinstance(entry, dict) else None
    if not isinstance(schema, dict) or schema.get("additionalProperties", True) is not False:
        return config
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    allowed = {str(key) for key in properties if str(key) not in RULE_CONTROLLED_MODULE_CONFIG_KEYS}
    if not allowed:
        return {}
    return {key: value for key, value in config.items() if key in allowed}


def transfer_notice_setting_key(aid: int) -> str:
    return f"{TRANSFER_NOTICE_SETTING_PREFIX}{int(aid)}"


def default_transfer_notice_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "chat_id": None,
        "chat_ids": [],
        "interaction_bot_token_enc": None,
        "has_interaction_bot_token": False,
        "interaction_bot_username": None,
        "interaction_bot_id": None,
        "interaction_last_update_id": None,
        "interaction_last_error": None,
        "trusted_bot_id": None,
        "trusted_bot_ids": [],
        "transfer_bot_id": None,
        "transfer_bot_token_enc": None,
        "has_transfer_bot_token": False,
        "transfer_last_update_id": None,
        "transfer_last_error": None,
        "trigger_mode": "payment",
        "trigger_text": "转账成功",
        "trigger_texts": ["转账成功"],
        "module_start_keywords": [],
        "receiver_user_id": None,
        "receiver_text": None,
        "amount": None,
        "amount_match_mode": "eq",
        "action": "notice",
        "math_prize": 123,
        "module_key": None,
        "module_action": None,
        "module_session_scope": None,
        "participant_policy": None,
        "module_prize": None,
        "module_config": {},
        "module_start_text": None,
        "user_cooldown_seconds": None,
        "daily_limit_per_user": None,
        "open_commands": [],
        "close_commands": [],
        "status_commands": [],
        "query_commands": list(DEFAULT_INTERACTION_QUERY_COMMANDS),
        "query_response_template": DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE,
        "query_item_template": DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE,
        "query_empty_message": DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE,
        "disabled_message": DEFAULT_INTERACTION_DISABLED_MESSAGE,
        "valid_seconds": 600,
        "concurrency": "chat",
        "response_template": DEFAULT_INTERACTION_RESPONSE_TEMPLATE,
        "transfer_notice_template": DEFAULT_TRANSFER_NOTICE_TEMPLATE,
        "debit_notice_template": DEFAULT_DEBIT_NOTICE_TEMPLATE,
        "rules": [],
    }


def normalize_transfer_notice_template(value: Any) -> str:
    template = str(value or "").strip()
    if not template or template == LEGACY_TRANSFER_NOTICE_TEMPLATE:
        return DEFAULT_TRANSFER_NOTICE_TEMPLATE
    return template[:1000]


def normalize_debit_notice_template(value: Any) -> str:
    template = str(value or "").strip()
    return (template or DEFAULT_DEBIT_NOTICE_TEMPLATE)[:1000]


def normalize_transfer_notice_config(raw: Any) -> dict[str, Any]:
    base = default_transfer_notice_config()
    if isinstance(raw, dict):
        for key in base:
            if key in raw:
                base[key] = raw[key]
    base["enabled"] = bool(base.get("enabled", False))
    for key in (
        "chat_id",
        "interaction_bot_id",
        "interaction_last_update_id",
        "trusted_bot_id",
        "transfer_bot_id",
        "transfer_last_update_id",
        "amount",
        "math_prize",
        "module_prize",
        "daily_limit_per_user",
        "valid_seconds",
        "receiver_user_id",
    ):
        try:
            base[key] = int(base[key]) if base[key] not in (None, "") else None
        except (TypeError, ValueError):
            base[key] = None
    trusted_bot_ids = _normalize_positive_int_list(base.get("trusted_bot_ids"), max_items=50)
    if base.get("trusted_bot_id") is not None and int(base["trusted_bot_id"]) not in trusted_bot_ids:
        trusted_bot_ids.insert(0, int(base["trusted_bot_id"]))
    base["trusted_bot_ids"] = trusted_bot_ids
    base["trusted_bot_id"] = trusted_bot_ids[0] if trusted_bot_ids else None
    chat_ids: list[int] = []
    raw_chat_ids = base.get("chat_ids")
    if isinstance(raw_chat_ids, list):
        for raw_id in raw_chat_ids:
            try:
                chat_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if chat_id not in chat_ids:
                chat_ids.append(chat_id)
    if base.get("chat_id") is not None and int(base["chat_id"]) not in chat_ids:
        chat_ids.insert(0, int(base["chat_id"]))
    base["chat_ids"] = chat_ids
    base["chat_id"] = chat_ids[0] if chat_ids else None
    if base.get("math_prize") is None or int(base["math_prize"]) <= 0:
        base["math_prize"] = 123
    action = str(base.get("action") or "notice").strip()
    base["action"] = action if action in {"notice", "math10", "module"} else "notice"
    trigger_mode = str(base.get("trigger_mode") or "payment").strip()
    base["trigger_mode"] = trigger_mode if trigger_mode in VALID_TRIGGER_MODES else "payment"
    amount_match_mode = str(base.get("amount_match_mode") or "eq").strip()
    base["amount_match_mode"] = amount_match_mode if amount_match_mode in VALID_AMOUNT_MATCH_MODES else "eq"
    concurrency = str(base.get("concurrency") or "chat").strip()
    base["concurrency"] = concurrency if concurrency in VALID_CONCURRENCY else "chat"
    module_session_scope = str(base.get("module_session_scope") or "").strip() or None
    base["module_session_scope"] = module_session_scope if module_session_scope in VALID_CONCURRENCY else None
    participant_policy = str(base.get("participant_policy") or "").strip() or None
    base["participant_policy"] = (
        participant_policy if participant_policy in VALID_INTERACTION_PARTICIPANT_POLICIES else None
    )
    if base.get("valid_seconds") is None or int(base["valid_seconds"]) < 30:
        base["valid_seconds"] = 600
    base["valid_seconds"] = min(int(base["valid_seconds"]), 86400)
    if base.get("module_prize") is not None and int(base["module_prize"]) <= 0:
        base["module_prize"] = None
    if base.get("daily_limit_per_user") is not None and int(base["daily_limit_per_user"]) <= 0:
        base["daily_limit_per_user"] = None
    base["module_config"] = _strip_rule_controlled_module_config(base.get("module_config"))
    for key in ("module_key", "module_action", "module_start_text", "disabled_message"):
        value = str(base.get(key) or "").strip()
        base[key] = value or None
    if base["action"] == "module" and base.get("module_key") and base.get("module_action") is None:
        base["module_action"] = _declared_module_single_entry_key(base.get("module_key"))
    if (
        base["action"] == "module"
        and base.get("module_prize") is not None
        and declared_module_entry_has_field(base.get("module_key"), base.get("module_action"), "prize") is False
    ):
        base["module_prize"] = None
    base["has_interaction_bot_token"] = bool(base.get("interaction_bot_token_enc"))
    username = str(base.get("interaction_bot_username") or "").strip().lstrip("@")
    base["interaction_bot_username"] = username or None
    error = str(base.get("interaction_last_error") or "").strip()
    base["interaction_last_error"] = label_bot_polling_error(error, role="interaction") if error else None
    if not base["enabled"] or not base["has_interaction_bot_token"]:
        base["interaction_running"] = False
        base["interaction_runtime_status"] = "stopped"
        base["interaction_last_error"] = None
    base["has_transfer_bot_token"] = bool(base.get("transfer_bot_token_enc"))
    transfer_error = str(base.get("transfer_last_error") or "").strip()
    base["transfer_last_error"] = label_bot_polling_error(transfer_error, role="transfer_test") if transfer_error else None
    trigger = str(base.get("trigger_text") or "").strip()
    base["trigger_text"] = trigger or "转账成功"
    triggers: list[str] = []
    raw_triggers = base.get("trigger_texts")
    if isinstance(raw_triggers, list):
        for raw_trigger in raw_triggers:
            item = str(raw_trigger or "").strip()
            if item and item not in triggers:
                triggers.append(item)
    if base["trigger_text"] not in triggers:
        triggers.insert(0, base["trigger_text"])
    base["trigger_texts"] = triggers or ["转账成功"]
    base["trigger_text"] = base["trigger_texts"][0]
    base["module_start_keywords"] = _normalize_string_list(base.get("module_start_keywords"))
    base["open_commands"] = _normalize_string_list(base.get("open_commands"))
    base["close_commands"] = _normalize_string_list(base.get("close_commands"))
    base["status_commands"] = _normalize_string_list(base.get("status_commands"))
    raw_query_commands = base.get("query_commands")
    base["query_commands"] = (
        _normalize_string_list(raw_query_commands)
        if isinstance(raw_query_commands, list)
        else list(DEFAULT_INTERACTION_QUERY_COMMANDS)
    )
    query_response_template = str(base.get("query_response_template") or "").strip()
    base["query_response_template"] = query_response_template or DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE
    base["query_response_template"] = base["query_response_template"][:2000]
    query_item_template = str(base.get("query_item_template") or "").strip()
    base["query_item_template"] = query_item_template or DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE
    base["query_item_template"] = base["query_item_template"][:1000]
    query_empty_message = str(base.get("query_empty_message") or "").strip()
    base["query_empty_message"] = query_empty_message or DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE
    base["query_empty_message"] = base["query_empty_message"][:500]
    receiver = str(base.get("receiver_text") or "").strip()
    base["receiver_text"] = receiver or None
    template = str(base.get("response_template") or "").strip()
    base["response_template"] = template or default_transfer_notice_config()["response_template"]
    base["transfer_notice_template"] = normalize_transfer_notice_template(base.get("transfer_notice_template"))
    base["debit_notice_template"] = normalize_debit_notice_template(base.get("debit_notice_template"))
    rules = normalize_interaction_rules(base.get("rules"))
    if not rules:
        rules = [
            {
                "id": "legacy-default",
                "name": "默认转账联动",
                "enabled": True,
                "chat_ids": list(base["chat_ids"]),
                "trigger_mode": base["trigger_mode"],
                "trigger_texts": list(base["trigger_texts"]),
                "module_start_keywords": list(base["module_start_keywords"]),
                "receiver_user_id": base["receiver_user_id"],
                "receiver_text": base["receiver_text"],
                "amount": base["amount"],
                "amount_match_mode": base["amount_match_mode"],
                "action": base["action"],
                "math_prize": base["math_prize"],
                "module_key": base["module_key"],
                "module_action": base["module_action"],
                "module_session_scope": base.get("module_session_scope"),
                "participant_policy": base.get("participant_policy"),
                "module_prize": base["module_prize"],
                "module_config": dict(base["module_config"]) if isinstance(base.get("module_config"), dict) else {},
                "module_start_text": base["module_start_text"],
                "user_cooldown_seconds": base.get("user_cooldown_seconds"),
                "daily_limit_per_user": base.get("daily_limit_per_user"),
                "open_commands": list(base["open_commands"]),
                "close_commands": list(base["close_commands"]),
                "status_commands": list(base["status_commands"]),
                "disabled_message": base["disabled_message"],
                "valid_seconds": base["valid_seconds"],
                "concurrency": base["concurrency"],
                "response_template": base["response_template"],
            }
        ]
    first_enabled = next((rule for rule in rules if rule.get("enabled", True)), rules[0])
    base["chat_ids"] = list(first_enabled.get("chat_ids") or base["chat_ids"])
    base["chat_id"] = base["chat_ids"][0] if base["chat_ids"] else None
    base["trigger_mode"] = first_enabled.get("trigger_mode") or "payment"
    base["trigger_texts"] = list(first_enabled.get("trigger_texts") or base["trigger_texts"])
    base["trigger_text"] = base["trigger_texts"][0] if base["trigger_texts"] else "转账成功"
    base["module_start_keywords"] = list(first_enabled.get("module_start_keywords") or [])
    base["receiver_user_id"] = first_enabled.get("receiver_user_id")
    base["receiver_text"] = first_enabled.get("receiver_text")
    base["amount"] = first_enabled.get("amount")
    base["amount_match_mode"] = first_enabled.get("amount_match_mode") or "eq"
    base["action"] = first_enabled.get("action") or "notice"
    base["math_prize"] = first_enabled.get("math_prize") or 123
    base["module_key"] = first_enabled.get("module_key")
    base["module_action"] = first_enabled.get("module_action")
    base["module_session_scope"] = first_enabled.get("module_session_scope")
    base["participant_policy"] = first_enabled.get("participant_policy")
    base["module_prize"] = first_enabled.get("module_prize")
    base["module_config"] = dict(first_enabled.get("module_config") or {})
    base["module_start_text"] = first_enabled.get("module_start_text")
    base["user_cooldown_seconds"] = first_enabled.get("user_cooldown_seconds")
    base["daily_limit_per_user"] = first_enabled.get("daily_limit_per_user")
    base["open_commands"] = list(first_enabled.get("open_commands") or [])
    base["close_commands"] = list(first_enabled.get("close_commands") or [])
    base["status_commands"] = list(first_enabled.get("status_commands") or [])
    base["query_commands"] = _normalize_string_list(base.get("query_commands"))
    base["disabled_message"] = first_enabled.get("disabled_message")
    base["valid_seconds"] = first_enabled.get("valid_seconds") or 600
    base["concurrency"] = first_enabled.get("concurrency") or "chat"
    base["response_template"] = first_enabled.get("response_template") or base["response_template"]
    base["rules"] = rules
    return base


def _normalize_string_list(raw: Any, *, default: list[str] | None = None) -> list[str]:
    out: list[str] = []
    if isinstance(raw, list):
        for raw_item in raw:
            item = str(raw_item or "").strip()
            if item and "\n" not in item and item not in out:
                out.append(item)
    return out or list(default or [])


def _normalize_positive_int_list(raw: Any, *, max_items: int) -> list[int]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        items: list[Any] = re.split(r"[\n,，\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    out: list[int] = []
    for raw_item in items:
        if len(out) >= max_items:
            break
        try:
            item = int(str(raw_item).strip())
        except (TypeError, ValueError):
            continue
        if item <= 0 or item in out:
            continue
        out.append(item)
    return out


def normalize_interaction_entry_manifest(raw: Any) -> dict[str, Any] | None:
    """把 builtin/installed 入口声明整理成统一形态，同时保留扩展字段。"""

    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    if not key:
        return None
    launch_mode = str(raw.get("launch_mode") or "bridge").strip()
    if launch_mode not in VALID_INTERACTION_LAUNCH_MODES:
        launch_mode = "bridge"
    scope = str(raw.get("session_scope") or "chat").strip()
    if scope not in VALID_CONCURRENCY:
        scope = "chat"
    events: list[str] = []
    raw_events = raw.get("events")
    if isinstance(raw_events, list):
        for item in raw_events:
            event = str(item or "").strip()
            if event in VALID_INTERACTION_EVENTS and event not in events:
                events.append(event)
    if not events:
        events = ["payment_confirmed", "keyword", "message", "session_close"]
    command_fallback = raw.get("command_fallback")
    has_command_fallback = isinstance(command_fallback, dict) and bool(command_fallback.get("enabled", True))
    dispatch_modes: list[str] = []
    raw_dispatch_modes = raw.get("dispatch_modes")
    if isinstance(raw_dispatch_modes, list):
        for raw_item in raw_dispatch_modes:
            item = str(raw_item or "").strip()
            if item in VALID_INTERACTION_DISPATCH_MODES and item not in dispatch_modes:
                dispatch_modes.append(item)
    if not dispatch_modes:
        if launch_mode in {"direct", "hybrid"} or has_command_fallback:
            dispatch_modes.append("admin_command")
        if launch_mode in {"bridge", "hybrid"}:
            dispatch_modes.append("public_keyword")
    if not dispatch_modes:
        dispatch_modes = ["public_keyword"]
    raw_message_channels = raw.get("message_channels")
    message_channels: dict[str, Any] = {}
    if isinstance(raw_message_channels, dict):
        for mode, channel in raw_message_channels.items():
            mode_key = str(mode or "").strip()
            channel_value = _normalize_interaction_message_channel(channel)
            if mode_key in VALID_INTERACTION_DISPATCH_MODES and channel_value is not None:
                message_channels[mode_key] = channel_value
    if "admin_command" in dispatch_modes:
        message_channels.setdefault("admin_command", "userbot_reply")
    if "public_keyword" in dispatch_modes:
        message_channels.setdefault("public_keyword", "interaction_bot")
    money_channel = str(raw.get("money_channel") or "userbot_reply").strip()
    if money_channel not in {"userbot_reply"}:
        money_channel = "userbot_reply"
    out = dict(raw)
    out.update(
        {
            "key": key,
            "launch_mode": launch_mode,
            "session_scope": scope,
            "events": events,
            "dispatch_modes": dispatch_modes,
            "message_channels": message_channels,
            "money_channel": money_channel,
            "preserve_command_trigger": bool(raw.get("preserve_command_trigger", True)),
        }
    )
    profile = str(raw.get("interaction_profile") or "").strip()
    if profile:
        out["interaction_profile"] = profile
    participant_policy = str(raw.get("participant_policy") or "").strip()
    if participant_policy in VALID_INTERACTION_PARTICIPANT_POLICIES:
        out["participant_policy"] = participant_policy
    result_contract = raw.get("result_contract")
    if isinstance(result_contract, dict):
        normalized_result_contract = dict(result_contract)
        send_via = _normalize_result_contract_send_via(result_contract.get("send_via"))
        if send_via:
            normalized_result_contract["send_via"] = send_via
        out["result_contract"] = normalized_result_contract
    if isinstance(command_fallback, dict):
        out["command_fallback"] = dict(command_fallback)
    return out


def _normalize_interaction_message_channel(raw: Any) -> Any:
    if isinstance(raw, str):
        channel = raw.strip()
        if channel in VALID_INTERACTION_MESSAGE_CHANNELS:
            return channel
    options = send_via_selector_options(raw)
    if not options:
        return None
    if isinstance(raw, dict):
        out: dict[str, Any] = {"prefer": options}
        if "fallback" in raw:
            out["fallback"] = bool(raw.get("fallback"))
        return out
    if isinstance(raw, (list, tuple, set)):
        return options
    return options[0]


def _normalize_result_contract_send_via(raw: Any) -> list[str]:
    raw_items = raw if isinstance(raw, list) else [raw]
    send_via: list[str] = []
    for raw_item in raw_items:
        options = send_via_selector_options(raw_item)
        for item in options:
            if item in VALID_INTERACTION_SEND_VIA and item not in send_via:
                send_via.append(item)
        for item in unsupported_send_via_values(raw_item):
            if item not in send_via:
                send_via.append(item)
    return send_via


def _entry_session_scope_from_entries(entries: Any, entry_key: str | None) -> str | None:
    if not entry_key or not isinstance(entries, list):
        return None
    for raw_entry in entries:
        entry = normalize_interaction_entry_manifest(raw_entry)
        if entry is None:
            continue
        if str(entry.get("key") or "").strip() != entry_key:
            continue
        return str(entry.get("session_scope") or "chat")
    return None


def _entry_participant_policy_from_entries(entries: Any, entry_key: str | None) -> str | None:
    if not entry_key or not isinstance(entries, list):
        return None
    for raw_entry in entries:
        entry = normalize_interaction_entry_manifest(raw_entry)
        if entry is None:
            continue
        if str(entry.get("key") or "").strip() != entry_key:
            continue
        policy = str(entry.get("participant_policy") or "").strip()
        return policy if policy in VALID_INTERACTION_PARTICIPANT_POLICIES else None
    return None


def _entry_events_from_entries(entries: Any, entry_key: str | None) -> list[str]:
    if not entry_key or not isinstance(entries, list):
        return []
    for raw_entry in entries:
        entry = normalize_interaction_entry_manifest(raw_entry)
        if entry is None:
            continue
        if str(entry.get("key") or "").strip() != entry_key:
            continue
        raw_events = entry.get("events")
        if isinstance(raw_events, list):
            return [
                str(item).strip()
                for item in raw_events
                if str(item or "").strip() in VALID_INTERACTION_EVENTS
            ]
        break
    return []


def _entry_manifest_from_entries(entries: Any, entry_key: str | None) -> dict[str, Any] | None:
    if not entry_key or not isinstance(entries, list):
        return None
    for raw_entry in entries:
        entry = normalize_interaction_entry_manifest(raw_entry)
        if entry is None:
            continue
        if str(entry.get("key") or "").strip() == entry_key:
            return entry
    return None


def _entry_has_field_from_entries(entries: Any, entry_key: str | None, field_name: str) -> bool | None:
    if not entry_key or not isinstance(entries, list):
        return None
    for raw_entry in entries:
        entry = normalize_interaction_entry_manifest(raw_entry)
        if entry is None:
            continue
        if str(entry.get("key") or "").strip() != entry_key:
            continue
        schema = entry.get("input_schema")
        if not isinstance(schema, dict):
            return None
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return None
        return field_name in properties
    return None


def _entry_key_from_entries(entries: Any) -> str | None:
    if not isinstance(entries, list):
        return None
    keys: list[str] = []
    for raw_entry in entries:
        entry = normalize_interaction_entry_manifest(raw_entry)
        if entry is None:
            continue
        key = str(entry.get("key") or "")
        if key and key not in keys:
            keys.append(key)
    return keys[0] if len(keys) == 1 else None


def _plugin_json_metadata(module_key: str | None) -> dict[str, Any] | None:
    if not module_key:
        return None
    plugin_json = settings.plugins_installed_path / module_key / "plugin.json"
    if not plugin_json.exists():
        return None
    meta = json.loads(plugin_json.read_text(encoding="utf-8"))
    return meta if isinstance(meta, dict) else None


def _plugin_json_interaction_entries(module_key: str | None) -> Any:
    meta = _plugin_json_metadata(module_key)
    if not isinstance(meta, dict):
        return None
    raw_entries = meta.get("interaction_entries")
    if raw_entries is None and isinstance(meta.get("config_schema"), dict):
        raw_entries = meta["config_schema"].get("x-interaction-entries")
    return raw_entries


def declared_module_event_subscriptions(module_key: str | None) -> list[dict[str, Any]]:
    """Return Event Bus subscriptions declared by builtin or installed plugin metadata."""

    if not module_key:
        return []
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        raw_subscriptions = getattr(manifest, "event_subscriptions", None) if manifest is not None else None
        if isinstance(raw_subscriptions, list):
            subscriptions = [dict(item) for item in raw_subscriptions if isinstance(item, dict)]
            if subscriptions:
                return subscriptions
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块事件订阅失败: %s", module_key, exc_info=True)
    try:
        meta = _plugin_json_metadata(module_key)
        raw_subscriptions = meta.get("event_subscriptions") if isinstance(meta, dict) else None
        if isinstance(raw_subscriptions, list):
            return [dict(item) for item in raw_subscriptions if isinstance(item, dict)]
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块事件订阅失败: %s", module_key, exc_info=True)
    return []


def declared_plugin_capabilities(module_key: str | None) -> dict[str, Any]:
    """Return high-risk capability declarations for a plugin."""

    if not module_key:
        return {}
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        raw_capabilities = getattr(manifest, "capabilities", None) if manifest is not None else None
        if isinstance(raw_capabilities, dict) and raw_capabilities:
            return dict(raw_capabilities)
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块能力声明失败: %s", module_key, exc_info=True)
    try:
        meta = _plugin_json_metadata(module_key)
        raw_capabilities = meta.get("capabilities") if isinstance(meta, dict) else None
        if isinstance(raw_capabilities, dict) and raw_capabilities:
            return dict(raw_capabilities)
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块能力声明失败: %s", module_key, exc_info=True)
    return {}


def plugin_declares_telegram_native_raw(module_key: str | None, *, source: str = "interaction_bot") -> bool:
    """Whether a plugin explicitly asks for native Telegram raw event payloads."""

    capabilities = declared_plugin_capabilities(module_key)
    raw = capabilities.get("telegram_native_raw")
    if not isinstance(raw, dict) or not bool(raw.get("enabled")):
        return False
    sources = raw.get("sources")
    if isinstance(sources, list) and sources:
        allowed_sources = {str(item or "").strip() for item in sources}
        return source in allowed_sources or "all" in allowed_sources
    return True


def _declared_module_single_entry_key(module_key: str | None) -> str | None:
    if not module_key:
        return None
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        key = _entry_key_from_entries(getattr(manifest, "interaction_entries", None))
        if key:
            return key
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块交互入口失败: %s", module_key, exc_info=True)
    try:
        key = _entry_key_from_entries(_plugin_json_interaction_entries(module_key))
        if key:
            return key
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块交互入口失败: %s", module_key, exc_info=True)
    fallback_keys = sorted(
        entry_key
        for fallback_module_key, entry_key in (
            set(FALLBACK_CHAT_SESSION_MODULE_ENTRIES)
            | set(FALLBACK_NO_PRIZE_MODULE_ENTRIES)
            | set(FALLBACK_SOLO_OWNER_MODULE_ENTRIES)
        )
        if fallback_module_key == module_key
    )
    if len(fallback_keys) == 1:
        return fallback_keys[0]
    return None


def _declared_module_entry_session_scope(module_key: str | None, module_action: str | None) -> str | None:
    if not module_key or not module_action:
        return None
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        scope = _entry_session_scope_from_entries(getattr(manifest, "interaction_entries", None), module_action)
        if scope:
            return scope
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块交互入口作用域失败: %s.%s", module_key, module_action, exc_info=True)
    try:
        scope = _entry_session_scope_from_entries(_plugin_json_interaction_entries(module_key), module_action)
        if scope:
            return scope
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块交互入口作用域失败: %s.%s", module_key, module_action, exc_info=True)
    if (module_key, module_action) in FALLBACK_CHAT_SESSION_MODULE_ENTRIES:
        return "chat"
    return None


def _declared_module_entry_participant_policy(module_key: str | None, module_action: str | None) -> str | None:
    if not module_key or not module_action:
        return None
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        policy = _entry_participant_policy_from_entries(getattr(manifest, "interaction_entries", None), module_action)
        if policy:
            return policy
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块交互入口参与策略失败: %s.%s", module_key, module_action, exc_info=True)
    try:
        policy = _entry_participant_policy_from_entries(_plugin_json_interaction_entries(module_key), module_action)
        if policy:
            return policy
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块交互入口参与策略失败: %s.%s", module_key, module_action, exc_info=True)
    if (module_key, module_action) in FALLBACK_SOLO_OWNER_MODULE_ENTRIES:
        return "solo_owner"
    return None


def declared_module_entry_events(module_key: str | None, module_action: str | None) -> list[str]:
    if not module_key or not module_action:
        return []
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        events = _entry_events_from_entries(getattr(manifest, "interaction_entries", None), module_action)
        if events:
            return events
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块交互入口事件失败: %s.%s", module_key, module_action, exc_info=True)
    try:
        events = _entry_events_from_entries(_plugin_json_interaction_entries(module_key), module_action)
        if events:
            return events
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块交互入口事件失败: %s.%s", module_key, module_action, exc_info=True)
    return []


def declared_module_entry_manifest(module_key: str | None, module_action: str | None) -> dict[str, Any] | None:
    """返回交互入口的规范化声明；未知入口返回 None 以保留旧配置兼容。"""

    if not module_key or not module_action:
        return None
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        entry = _entry_manifest_from_entries(getattr(manifest, "interaction_entries", None), module_action)
        if entry:
            return entry
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块交互入口声明失败: %s.%s", module_key, module_action, exc_info=True)
    try:
        entry = _entry_manifest_from_entries(_plugin_json_interaction_entries(module_key), module_action)
        if entry:
            return entry
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块交互入口声明失败: %s.%s", module_key, module_action, exc_info=True)
    return None


def declared_module_entry_has_field(module_key: str | None, module_action: str | None, field_name: str) -> bool | None:
    """返回入口 schema 是否声明字段；未知入口返回 None 以保留旧配置兼容。"""

    if not module_key or not module_action or not field_name:
        return None
    try:
        manifest = BUILTIN_FEATURES.manifest_for(module_key)
        declared = _entry_has_field_from_entries(getattr(manifest, "interaction_entries", None), module_action, field_name)
        if declared is not None:
            return declared
    except Exception:  # noqa: BLE001
        log.debug("读取 builtin 模块交互入口字段失败: %s.%s", module_key, module_action, exc_info=True)
    try:
        declared = _entry_has_field_from_entries(_plugin_json_interaction_entries(module_key), module_action, field_name)
        if declared is not None:
            return declared
    except Exception:  # noqa: BLE001
        log.debug("读取 installed 模块交互入口字段失败: %s.%s", module_key, module_action, exc_info=True)
    if field_name == "prize" and (module_key, module_action) in FALLBACK_NO_PRIZE_MODULE_ENTRIES:
        return False
    return None


def normalize_interaction_rules(raw: Any) -> list[dict[str, Any]]:
    """归一化后续多模块规则列表；旧单规则字段仍保留在顶层。"""

    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw[:20]):
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id") or f"rule-{index + 1}").strip()[:64] or f"rule-{index + 1}"
        if rule_id in seen_ids:
            rule_id = f"{rule_id}-{index + 1}"[:64]
        seen_ids.add(rule_id)
        name = str(item.get("name") or rule_id).strip()[:64] or rule_id
        chat_ids: list[int] = []
        raw_chat_ids = item.get("chat_ids")
        if isinstance(raw_chat_ids, list):
            for raw_chat_id in raw_chat_ids:
                try:
                    chat_id = int(raw_chat_id)
                except (TypeError, ValueError):
                    continue
                if chat_id not in chat_ids:
                    chat_ids.append(chat_id)
        triggers: list[str] = []
        raw_triggers = item.get("trigger_texts")
        if isinstance(raw_triggers, list):
            for raw_trigger in raw_triggers:
                trigger = str(raw_trigger or "").strip()
                if trigger and trigger not in triggers:
                    triggers.append(trigger)
        if not triggers:
            triggers = ["转账成功"]
        module_start_keywords = _normalize_string_list(item.get("module_start_keywords"))
        open_commands = _normalize_string_list(item.get("open_commands"))
        close_commands = _normalize_string_list(item.get("close_commands"))
        status_commands = _normalize_string_list(item.get("status_commands"))
        try:
            amount = int(item["amount"]) if item.get("amount") not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        try:
            receiver_user_id = int(item["receiver_user_id"]) if item.get("receiver_user_id") not in (None, "") else None
        except (TypeError, ValueError):
            receiver_user_id = None
        receiver_text = str(item.get("receiver_text") or "").strip() or None
        amount_match_mode = str(item.get("amount_match_mode") or "eq").strip()
        if amount_match_mode not in VALID_AMOUNT_MATCH_MODES:
            amount_match_mode = "eq"
        trigger_mode = str(item.get("trigger_mode") or "payment").strip()
        if trigger_mode not in VALID_TRIGGER_MODES:
            trigger_mode = "payment"
        concurrency = str(item.get("concurrency") or "chat").strip()
        if concurrency not in VALID_CONCURRENCY:
            concurrency = "chat"
        try:
            valid_seconds = int(item.get("valid_seconds") or 600)
        except (TypeError, ValueError):
            valid_seconds = 600
        valid_seconds = min(max(valid_seconds, 30), 86400)
        try:
            math_prize = int(item.get("math_prize") or 123)
        except (TypeError, ValueError):
            math_prize = 123
        action = str(item.get("action") or "notice").strip()
        if action not in {"notice", "math10", "module"}:
            action = "notice"
        if action == "math10" and not module_start_keywords:
            module_start_keywords = list(DEFAULT_MATH10_START_KEYWORDS)
        if action == "math10" and amount is None and trigger_mode == "payment" and item.get("trigger_mode") in (None, "", "payment"):
            trigger_mode = "both"
        if trigger_mode == "keyword":
            amount = None
            receiver_user_id = None
            receiver_text = None
        module_key = str(item.get("module_key") or "").strip() or None
        module_action = str(item.get("module_action") or "").strip() or None
        if action == "module" and module_key and module_action is None:
            module_action = _declared_module_single_entry_key(module_key)
        module_session_scope = str(item.get("module_session_scope") or "").strip() or None
        if module_session_scope not in VALID_CONCURRENCY:
            module_session_scope = None
        if module_session_scope is None:
            module_session_scope = _declared_module_entry_session_scope(module_key, module_action)
        participant_policy = str(item.get("participant_policy") or "").strip() or None
        if participant_policy not in VALID_INTERACTION_PARTICIPANT_POLICIES:
            participant_policy = _declared_module_entry_participant_policy(module_key, module_action)
        module_config = _sanitize_module_config_for_entry(item.get("module_config"), module_key, module_action)
        module_start_text = str(item.get("module_start_text") or "").strip() or None
        user_cooldown_seconds = str(item.get("user_cooldown_seconds") or "").strip() or None
        try:
            module_prize = int(item["module_prize"]) if item.get("module_prize") not in (None, "") else None
        except (TypeError, ValueError):
            module_prize = None
        if (
            action == "module"
            and module_prize is not None
            and declared_module_entry_has_field(module_key, module_action, "prize") is False
        ):
            module_prize = None
        try:
            daily_limit_per_user = int(item["daily_limit_per_user"]) if item.get("daily_limit_per_user") not in (None, "") else None
        except (TypeError, ValueError):
            daily_limit_per_user = None
        response_template = str(item.get("response_template") or "").strip()
        if not response_template:
            response_template = default_transfer_notice_config()["response_template"]
        disabled_message = str(item.get("disabled_message") or "").strip() or None
        out.append(
            {
                "id": rule_id,
                "name": name,
                "enabled": bool(item.get("enabled", True)),
                "chat_ids": chat_ids,
                "trigger_mode": trigger_mode,
                "trigger_texts": triggers,
                "module_start_keywords": module_start_keywords,
                "receiver_user_id": receiver_user_id if receiver_user_id is None or receiver_user_id > 0 else None,
                "receiver_text": receiver_text,
                "amount": amount if amount is None or amount > 0 else None,
                "amount_match_mode": amount_match_mode,
                "action": action,
                "math_prize": math_prize if math_prize > 0 else 123,
                "module_key": module_key,
                "module_action": module_action,
                "module_session_scope": module_session_scope,
                "participant_policy": participant_policy,
                "module_prize": module_prize if module_prize is None or module_prize > 0 else None,
                "module_config": module_config,
                "module_start_text": module_start_text,
                "user_cooldown_seconds": user_cooldown_seconds,
                "daily_limit_per_user": daily_limit_per_user if daily_limit_per_user is None or daily_limit_per_user > 0 else None,
                "open_commands": open_commands,
                "close_commands": close_commands,
                "status_commands": status_commands,
                "disabled_message": disabled_message,
                "valid_seconds": valid_seconds,
                "concurrency": concurrency,
                "response_template": response_template,
            }
        )
    return out


def _requires_trusted_transfer_notice_sender(data: dict[str, Any]) -> bool:
    if not data.get("enabled"):
        return False
    for rule in data.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        if str(rule.get("trigger_mode") or "payment") in {"payment", "both"}:
            return True
    return False


def _has_trusted_transfer_notice_sender(data: dict[str, Any]) -> bool:
    trusted_ids = data.get("trusted_bot_ids")
    return (
        isinstance(trusted_ids, list)
        and len(trusted_ids) > 0
    ) or any(data.get(key) not in (None, "") for key in ("trusted_bot_id", "transfer_bot_id"))


def _enabled_interaction_module_keys(data: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict) or not bool(rule.get("enabled", True)):
            continue
        action = str(rule.get("action") or "").strip()
        if action == "math10":
            module_key = "math10"
        elif action == "module":
            module_key = str(rule.get("module_key") or "").strip()
        else:
            continue
        if module_key and module_key not in keys:
            keys.append(module_key)
    return keys


async def _ensure_interaction_module_account_features(
    db: AsyncSession,
    aid: int,
    data: dict[str, Any],
) -> list[str]:
    synced: list[str] = []
    for module_key in _enabled_interaction_module_keys(data):
        feature = await db.get(Feature, module_key)
        if feature is None:
            log.warning(
                "interaction rule references module without feature row aid=%s module=%s",
                aid,
                module_key,
            )
            continue
        af = await db.get(
            AccountFeature,
            {"account_id": aid, "feature_key": module_key},
        )
        if af is None:
            db.add(
                AccountFeature(
                    account_id=aid,
                    feature_key=module_key,
                    enabled=True,
                    config={},
                    state=FEATURE_STATE_DISABLED,
                )
            )
            synced.append(module_key)
            continue
        if not bool(af.enabled):
            af.enabled = True
            af.state = FEATURE_STATE_DISABLED
            af.last_error = None
            synced.append(module_key)
    return synced


async def get_transfer_notice_config(db: AsyncSession, aid: int) -> dict[str, Any]:
    await ensure_account(db, aid)
    row = await db.get(SystemSetting, transfer_notice_setting_key(aid))
    data = normalize_transfer_notice_config(row.value if row is not None else None)
    data.pop("interaction_bot_token_enc", None)
    data.pop("transfer_bot_token_enc", None)
    data["interaction_bot_token"] = None
    data["transfer_bot_token"] = None
    return data


async def get_interaction_bot_config(db: AsyncSession, aid: int) -> dict[str, Any]:
    return await get_transfer_notice_config(db, aid)


async def update_transfer_notice_config(
    db: AsyncSession,
    aid: int,
    payload: Any,
) -> dict[str, Any]:
    await ensure_account(db, aid)
    setting_key = transfer_notice_setting_key(aid)
    row = await db.get(SystemSetting, setting_key)
    current = normalize_transfer_notice_config(row.value if row is not None else None)
    incoming = dict(payload or {})
    if "rules" not in incoming:
        current.pop("rules", None)
    if incoming.get("clear_interaction_bot_token"):
        current["interaction_bot_token_enc"] = None
        current["interaction_bot_username"] = None
        current["interaction_bot_id"] = None
        current["interaction_last_update_id"] = None
        current["interaction_last_error"] = None
    interaction_token = str(incoming.get("interaction_bot_token") or "").strip()
    if interaction_token:
        management_row = await db.get(AccountBot, aid)
        if management_row is not None and _encrypted_token_matches_plain(management_row.bot_token_enc, interaction_token):
            raise _bad(
                "INTERACTION_BOT_TOKEN_CONFLICTS_WITH_ACCOUNT_BOT",
                "交互 Bot Token 不能和管理 Bot Token 使用同一个 Bot。请为 Bbot 创建独立 Bot。",
                422,
            )
        current["interaction_bot_token_enc"] = encrypt_str(interaction_token)
        current["interaction_last_update_id"] = None
        current["interaction_last_error"] = None
        try:
            me = await get_me(interaction_token)
            username = me.get("username")
            bot_id = me.get("id")
            current["interaction_bot_username"] = username if isinstance(username, str) else None
            current["interaction_bot_id"] = int(bot_id) if bot_id is not None else None
        except Exception as exc:  # noqa: BLE001
            current["interaction_last_error"] = sanitize_bot_error(exc, token=interaction_token)
    if incoming.get("clear_transfer_bot_token"):
        current["transfer_bot_token_enc"] = None
        current["transfer_bot_id"] = None
    token = str(incoming.get("transfer_bot_token") or "").strip()
    if token:
        current["transfer_bot_token_enc"] = encrypt_str(token)
        try:
            me = await get_me(token)
            bot_id = me.get("id")
            current["transfer_bot_id"] = int(bot_id) if bot_id is not None else None
        except Exception:
            current["transfer_bot_id"] = None
    for transient_key in (
        "interaction_bot_token",
        "clear_interaction_bot_token",
        "has_interaction_bot_token",
        "interaction_bot_username",
        "interaction_bot_id",
        "interaction_last_update_id",
        "interaction_last_error",
        "transfer_bot_id",
        "transfer_bot_token",
        "clear_transfer_bot_token",
        "has_transfer_bot_token",
    ):
        incoming.pop(transient_key, None)
    data = normalize_transfer_notice_config({**current, **incoming})
    if _requires_trusted_transfer_notice_sender(data) and not _has_trusted_transfer_notice_sender(data):
        raise _bad(
            "TRUSTED_BOT_ID_REQUIRED",
            "启用转账触发的规则前，必须配置可信通知 Bot ID（测试 Abot 或官方通知 Bot）",
            422,
        )
    synced_module_keys = await _ensure_interaction_module_account_features(db, aid, data)
    if synced_module_keys:
        log.info(
            "interaction rule synced account features aid=%s modules=%s",
            aid,
            ",".join(synced_module_keys),
        )
    if row is None:
        row = SystemSetting(key=setting_key, value=data)
        db.add(row)
    else:
        row.value = data
    await db.flush()
    out = dict(data)
    out.pop("interaction_bot_token_enc", None)
    out.pop("transfer_bot_token_enc", None)
    out["interaction_bot_token"] = None
    out["transfer_bot_token"] = None
    return out


async def update_interaction_bot_config(
    db: AsyncSession,
    aid: int,
    payload: Any,
) -> dict[str, Any]:
    return await update_transfer_notice_config(db, aid, payload)


async def get_interaction_bot_token(db: AsyncSession, aid: int) -> str | None:
    row = await db.get(SystemSetting, transfer_notice_setting_key(aid))
    data = normalize_transfer_notice_config(row.value if row is not None else None)
    token_enc = data.get("interaction_bot_token_enc")
    if not token_enc:
        return None
    try:
        return decrypt_str(str(token_enc))
    except ValueError:
        return None


async def get_transfer_bot_token(db: AsyncSession, aid: int) -> str | None:
    row = await db.get(SystemSetting, transfer_notice_setting_key(aid))
    data = normalize_transfer_notice_config(row.value if row is not None else None)
    token_enc = data.get("transfer_bot_token_enc")
    if not token_enc:
        return None
    try:
        return decrypt_str(str(token_enc))
    except ValueError:
        return None


def config_to_response(row: AccountBot, *, account_id: int | None = None) -> AccountBotConfigResponse:
    remote_plugin_policy = normalize_remote_plugin_policy(row.remote_plugin_policy)
    has_token = bool(row.bot_token_enc)
    last_error = row.last_error if row.enabled and has_token else None
    return AccountBotConfigResponse(
        account_id=int(account_id or row.account_id),
        enabled=bool(row.enabled),
        status=row.status or ACCOUNT_BOT_STATUS_DISABLED,
        has_token=has_token,
        username=row.username,
        remote_plugin_policy=AccountBotRemotePluginPolicy(**remote_plugin_policy),
        last_update_id=row.last_update_id,
        last_error=label_bot_polling_error(last_error, role="management") if last_error else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def ensure_account(db: AsyncSession, aid: int) -> Account:
    acc = await db.get(Account, aid)
    if acc is None:
        raise _bad("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    return acc


async def get_bot_config(db: AsyncSession, aid: int, *, create: bool = True) -> AccountBot:
    await ensure_account(db, aid)
    row = (
        await db.execute(select(AccountBot).where(AccountBot.account_id == aid))
    ).scalar_one_or_none()
    if row is None:
        if not create:
            raise _bad("ACCOUNT_BOT_NOT_FOUND", "该账号尚未配置 Bot", 404)
        row = AccountBot(
            account_id=aid,
            enabled=False,
            status=ACCOUNT_BOT_STATUS_DISABLED,
            remote_plugin_policy=default_remote_plugin_policy(),
        )
        db.add(row)
        await db.flush()
    elif not row.remote_plugin_policy:
        row.remote_plugin_policy = default_remote_plugin_policy()
    return row


async def update_bot_config(
    db: AsyncSession,
    aid: int,
    payload: AccountBotConfigUpdate,
) -> AccountBot:
    row = await get_bot_config(db, aid, create=True)
    if payload.clear_token:
        row.bot_token_enc = None
        row.username = None
    if payload.bot_token:
        setting = await db.get(SystemSetting, transfer_notice_setting_key(aid))
        cfg = normalize_transfer_notice_config(setting.value if setting is not None else None)
        if _encrypted_token_matches_plain(str(cfg.get("interaction_bot_token_enc") or ""), payload.bot_token):
            raise _bad(
                "ACCOUNT_BOT_TOKEN_CONFLICTS_WITH_INTERACTION_BOT",
                "管理 Bot Token 不能和交互 Bot Token 使用同一个 Bot。请为管理 Bot 和 Bbot 分别创建独立 Bot。",
                422,
            )
        row.bot_token_enc = encrypt_str(payload.bot_token)
        row.last_error = None
        try:
            me = await get_me(payload.bot_token)
            username = me.get("username")
            if isinstance(username, str):
                row.username = username
        except Exception as exc:  # noqa: BLE001
            row.status = ACCOUNT_BOT_STATUS_ERROR
            row.last_error = sanitize_bot_error(exc, token=payload.bot_token)
    if payload.enabled is not None:
        row.enabled = bool(payload.enabled)
        if not row.enabled:
            row.status = ACCOUNT_BOT_STATUS_DISABLED
    if payload.remote_plugin_policy is not None:
        current = normalize_remote_plugin_policy(row.remote_plugin_policy)
        patch = payload.remote_plugin_policy.model_dump(exclude_unset=True)
        merged = {**current, **{k: bool(v) for k, v in patch.items()}}
        row.remote_plugin_policy = normalize_remote_plugin_policy(merged)
    if row.enabled and not row.bot_token_enc:
        raise _bad("ACCOUNT_BOT_TOKEN_REQUIRED", "启用 Bot 前必须填写 Bot Token", 422)
    if row.enabled:
        try:
            decrypt_bot_token(row)
        except HTTPException:
            row.enabled = False
            row.status = ACCOUNT_BOT_STATUS_ERROR
            row.last_error = "Bot Token 解密失败，请重新保存"
            raise
    await db.flush()
    return row


async def list_bot_users(db: AsyncSession, aid: int) -> list[AccountBotUser]:
    await ensure_account(db, aid)
    rows = (
        await db.execute(
            select(AccountBotUser)
            .where(AccountBotUser.account_id == aid)
            .order_by(AccountBotUser.id.asc())
        )
    ).scalars().all()
    return list(rows)


async def get_bot_user(db: AsyncSession, aid: int, uid: int) -> AccountBotUser:
    row = await db.get(AccountBotUser, uid)
    if row is None or row.account_id != aid:
        raise _bad("ACCOUNT_BOT_USER_NOT_FOUND", "授权用户不存在", 404)
    return row


async def find_bot_user(
    db: AsyncSession, aid: int, tg_user_id: int
) -> AccountBotUser | None:
    return (
        await db.execute(
            select(AccountBotUser).where(
                AccountBotUser.account_id == aid,
                AccountBotUser.tg_user_id == tg_user_id,
            )
        )
    ).scalar_one_or_none()


async def create_bot_user(
    db: AsyncSession,
    aid: int,
    payload: AccountBotUserCreate,
) -> AccountBotUser:
    await ensure_account(db, aid)
    if payload.role not in ACCOUNT_BOT_ROLES:
        raise _bad("ACCOUNT_BOT_ROLE_INVALID", "role 只能是 viewer / operator / admin", 422)
    row = AccountBotUser(
        account_id=aid,
        tg_user_id=payload.tg_user_id,
        display_name=payload.display_name,
        role=payload.role,
        notify_enabled=payload.notify_enabled,
        enabled=payload.enabled,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _bad("ACCOUNT_BOT_USER_DUPLICATE", "该 Telegram 用户已授权", 409) from exc
    return row


async def update_bot_user(
    db: AsyncSession,
    aid: int,
    uid: int,
    payload: AccountBotUserUpdate,
) -> AccountBotUser:
    row = await get_bot_user(db, aid, uid)
    data = payload.model_dump(exclude_unset=True)
    if "role" in data and data["role"] not in ACCOUNT_BOT_ROLES:
        raise _bad("ACCOUNT_BOT_ROLE_INVALID", "role 只能是 viewer / operator / admin", 422)
    for key, value in data.items():
        setattr(row, key, value)
    await db.flush()
    return row


async def delete_bot_user(db: AsyncSession, aid: int, uid: int) -> None:
    row = await get_bot_user(db, aid, uid)
    await db.delete(row)
    await db.flush()


def decrypt_bot_token(row: AccountBot) -> str:
    if not row.bot_token_enc:
        raise _bad("ACCOUNT_BOT_TOKEN_REQUIRED", "该账号未配置 Bot Token", 422)
    try:
        return decrypt_str(row.bot_token_enc)
    except ValueError as exc:
        raise _bad("ACCOUNT_BOT_TOKEN_DECRYPT_FAILED", "Bot Token 解密失败，请重新保存", 422) from exc


async def call_bot_api(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    """调用 Bot API；只在这里拼接 token URL。"""

    url = f"{BOT_API_BASE}/bot{token}/{method}"
    client = await get_bot_api_client()
    resp = await client.post(url, json=payload or {}, timeout=timeout or BOT_API_TIMEOUT)
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("ok", False):
        desc = data.get("description") or f"HTTP {resp.status_code}"
        raise RuntimeError(desc)
    result = data.get("result")
    return result if isinstance(result, dict) else {"result": result}


async def get_me(token: str) -> dict[str, Any]:
    return await call_bot_api(token, "getMe", {}, timeout=httpx.Timeout(10.0))


async def send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = "HTML",
) -> dict[str, Any]:
    normalized_parse_mode = normalize_bot_parse_mode(parse_mode)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": _truncate_bot_text(text, parse_mode=normalized_parse_mode, limit=BOT_MESSAGE_TEXT_LIMIT),
        "disable_web_page_preview": True,
    }
    if normalized_parse_mode:
        payload["parse_mode"] = normalized_parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    return await call_bot_api(token, "sendMessage", payload)


async def send_photo_bytes(
    token: str,
    chat_id: int,
    photo: bytes,
    *,
    filename: str = "photo.png",
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
) -> dict[str, Any]:
    url = f"{BOT_API_BASE}/bot{token}/sendPhoto"
    normalized_parse_mode = normalize_bot_parse_mode(parse_mode)
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = _truncate_bot_text(caption, parse_mode=normalized_parse_mode, limit=BOT_MESSAGE_CAPTION_LIMIT)
    if normalized_parse_mode:
        data["parse_mode"] = normalized_parse_mode
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = reply_to_message_id
        data["allow_sending_without_reply"] = True
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"photo": (filename or "photo.png", photo)}
    client = await get_bot_api_client()
    resp = await client.post(url, data=data, files=files, timeout=BOT_API_TIMEOUT)
    payload = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not payload.get("ok", False):
        desc = payload.get("description") or f"HTTP {resp.status_code}"
        raise RuntimeError(desc)
    result = payload.get("result")
    return result if isinstance(result, dict) else {"result": result}


async def send_document_bytes(
    token: str,
    chat_id: int,
    document: bytes,
    *,
    filename: str = "file.bin",
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
) -> dict[str, Any]:
    url = f"{BOT_API_BASE}/bot{token}/sendDocument"
    normalized_parse_mode = normalize_bot_parse_mode(parse_mode)
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = _truncate_bot_text(caption, parse_mode=normalized_parse_mode, limit=BOT_MESSAGE_CAPTION_LIMIT)
    if normalized_parse_mode:
        data["parse_mode"] = normalized_parse_mode
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = reply_to_message_id
        data["allow_sending_without_reply"] = True
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"document": (filename or "file.bin", document)}
    client = await get_bot_api_client()
    resp = await client.post(url, data=data, files=files, timeout=BOT_API_TIMEOUT)
    payload = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not payload.get("ok", False):
        desc = payload.get("description") or f"HTTP {resp.status_code}"
        raise RuntimeError(desc)
    result = payload.get("result")
    return result if isinstance(result, dict) else {"result": result}


async def edit_message(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
) -> dict[str, Any]:
    normalized_parse_mode = normalize_bot_parse_mode(parse_mode)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": _truncate_bot_text(text, parse_mode=normalized_parse_mode, limit=BOT_MESSAGE_TEXT_LIMIT),
        "disable_web_page_preview": True,
    }
    if normalized_parse_mode:
        payload["parse_mode"] = normalized_parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await call_bot_api(token, "editMessageText", payload)


async def edit_message_caption(
    token: str,
    chat_id: int,
    message_id: int,
    caption: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
) -> dict[str, Any]:
    normalized_parse_mode = normalize_bot_parse_mode(parse_mode)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": _truncate_bot_text(
            caption,
            parse_mode=normalized_parse_mode,
            limit=BOT_MESSAGE_CAPTION_LIMIT,
        ),
    }
    if normalized_parse_mode:
        payload["parse_mode"] = normalized_parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await call_bot_api(token, "editMessageCaption", payload)


async def delete_message(
    token: str,
    chat_id: int,
    message_id: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
    }
    return await call_bot_api(token, "deleteMessage", payload)


async def answer_callback(
    token: str,
    callback_query_id: str,
    *,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text[:200]
    await call_bot_api(token, "answerCallbackQuery", payload, timeout=httpx.Timeout(10.0))


async def answer_inline_query(
    token: str,
    inline_query_id: str,
    *,
    results: list[dict[str, Any]],
    cache_time: int = 0,
    is_personal: bool = True,
    next_offset: str | None = None,
    button: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "inline_query_id": inline_query_id,
        "results": results[:50],
        "cache_time": max(0, int(cache_time or 0)),
        "is_personal": bool(is_personal),
    }
    if next_offset is not None:
        payload["next_offset"] = str(next_offset)
    if isinstance(button, dict):
        payload["button"] = button
    await call_bot_api(token, "answerInlineQuery", payload, timeout=httpx.Timeout(10.0))


def html_text(value: Any) -> str:
    """Bot HTML 消息统一转义。"""

    return escape("" if value is None else str(value), quote=False)


async def get_bot_api_client() -> httpx.AsyncClient:
    global _BOT_API_CLIENT
    client = _BOT_API_CLIENT
    if client is not None and not getattr(client, "is_closed", False):
        return client
    async with _BOT_API_CLIENT_LOCK:
        client = _BOT_API_CLIENT
        if client is None or getattr(client, "is_closed", False):
            client = httpx.AsyncClient(
                timeout=BOT_API_TIMEOUT,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            )
            _BOT_API_CLIENT = client
    return client


async def close_bot_api_client() -> None:
    global _BOT_API_CLIENT
    async with _BOT_API_CLIENT_LOCK:
        client = _BOT_API_CLIENT
        _BOT_API_CLIENT = None
    if client is not None and not getattr(client, "is_closed", False):
        await client.aclose()


def normalize_bot_parse_mode(parse_mode: str | None) -> str | None:
    value = str(parse_mode or "").strip().lower()
    if value in {"", "plain", "none"}:
        return None
    if value == "html":
        return "HTML"
    return str(parse_mode).strip() or None


def _truncate_bot_text(text: Any, *, parse_mode: str | None, limit: int) -> str:
    raw = "" if text is None else str(text)
    if limit <= 0 or len(raw) <= limit:
        return raw
    if parse_mode == "HTML":
        return _safe_truncate_html(raw, limit=limit)
    return raw[:limit]


class _HTMLTruncator(HTMLParser):
    _VOID_TAGS = {"br"}

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=False)
        self.limit = max(0, int(limit))
        self.visible_length = 0
        self.parts: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done():
            return
        raw = self.get_starttag_text()
        if raw:
            self.parts.append(raw)
        if tag not in self._VOID_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.open_tags:
            return
        while self.open_tags:
            current = self.open_tags.pop()
            self.parts.append(f"</{current}>")
            if current == tag:
                break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done():
            return
        raw = self.get_starttag_text()
        if raw:
            self.parts.append(raw)

    def handle_data(self, data: str) -> None:
        if self._done() or not data:
            return
        remaining = self.limit - self.visible_length
        if len(data) <= remaining:
            self.parts.append(data)
            self.visible_length += len(data)
            return
        self.parts.append(data[:remaining])
        self.visible_length += remaining

    def handle_entityref(self, name: str) -> None:
        self._append_entity(f"&{name};")

    def handle_charref(self, name: str) -> None:
        prefix = "&#x" if str(name).lower().startswith("x") else "&#"
        self._append_entity(f"{prefix}{name};")

    def finish(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)

    def _append_entity(self, raw: str) -> None:
        if self._done():
            return
        remaining = self.limit - self.visible_length
        if remaining <= 0:
            return
        self.parts.append(raw)
        self.visible_length += len(unescape(raw))

    def _done(self) -> bool:
        return self.visible_length >= self.limit


def _safe_truncate_html(text: str, *, limit: int) -> str:
    parser = _HTMLTruncator(limit)
    parser.feed(text)
    parser.close()
    return parser.finish()
