"""全局系统设置查询与确认修改工作流。"""

from __future__ import annotations

import os
import re
from typing import Any
from zoneinfo import available_timezones

from ....db.models.system import SystemSetting
from ....services import auth_login_security
from ....settings import settings as app_settings
from ....util.update_target import normalize_update_branch, normalize_update_remote
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec

_NESTED_KEYS = (
    "login_security",
    "remote_plugin_update_check",
    "app_update_target",
    "llm_limits",
    "payout_limits",
    "log_retention",
    "ui_preferences",
)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_UI_ROUTES = {
    "/plugins",
    "/ai",
    "/interaction",
    "/operations",
    "/overview",
    "/ledger",
    "/webhooks",
    "/dispatch-debug",
    "/logs",
    "/settings",
}


async def _value(ctx: ToolContext, key: str, default: Any) -> Any:
    row = await ctx.db.get(SystemSetting, key)
    return row.value if row is not None else default


def _wrapped_value(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return default if value is None else value


async def get_settings(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    prefix_raw = await _value(ctx, "command_prefix", None)
    timezone_raw = await _value(ctx, "timezone", {"value": "Asia/Shanghai"})
    sudo_raw = await _value(ctx, "sudo_enabled", {"enabled": False})
    prefix_required_raw = await _value(
        ctx, "command_prefix_required", {"enabled": True}
    )
    echo_raw = await _value(
        ctx,
        "command_echo_guard_previous_messages",
        {"value": app_settings.command_echo_guard_previous_messages},
    )
    result = {
        "command_prefix": str(
            _wrapped_value(prefix_raw, "value", app_settings.command_prefix)
        ),
        "timezone": str(_wrapped_value(timezone_raw, "value", "Asia/Shanghai")),
        "sudo_enabled": bool(_wrapped_value(sudo_raw, "enabled", False)),
        "command_prefix_required": bool(
            _wrapped_value(prefix_required_raw, "enabled", True)
        ),
        "command_echo_guard_previous_messages": int(
            _wrapped_value(echo_raw, "value", 8) or 0
        ),
    }
    defaults = {
        "login_security": {},
        "remote_plugin_update_check": {
            "enabled": True,
            "interval_minutes": 360,
        },
        "app_update_target": {
            "remote": os.getenv("TELEPILOT_UPDATE_REMOTE", "origin"),
            "branch": os.getenv("TELEPILOT_UPDATE_BRANCH", "main"),
        },
        "llm_limits": {},
        "payout_limits": {},
        "log_retention": {},
        "ui_preferences": {},
    }
    for key in _NESTED_KEYS:
        value = await _value(ctx, key, defaults[key])
        result[key] = value if isinstance(value, dict) else defaults[key]
    result["login_security"] = auth_login_security.login_security_config_to_dict(
        auth_login_security.normalize_login_security_config(result["login_security"])
    )
    return result


def _merge(current: Any, patch: Any) -> dict[str, Any]:
    base = current if isinstance(current, dict) else {}
    incoming = patch if isinstance(patch, dict) else {}
    return {**base, **{key: value for key, value in incoming.items() if value is not None}}


def _validate_changes(changes: dict[str, Any]) -> None:
    if "command_prefix" in changes:
        prefix = str(changes["command_prefix"] or "").strip()
        if not prefix or len(prefix) > 3:
            raise ValueError("命令前缀必须是 1 到 3 个字符")
    if "timezone" in changes:
        timezone = str(changes["timezone"] or "Asia/Shanghai").strip()
        if timezone not in available_timezones():
            raise ValueError(f"无效 IANA 时区：{timezone}")
    if "command_echo_guard_previous_messages" in changes:
        value = int(changes["command_echo_guard_previous_messages"])
        if not 0 <= value <= 50:
            raise ValueError("回显防护历史消息数必须在 0 到 50 之间")
    login = changes.get("login_security")
    if isinstance(login, dict):
        bounds = {
            "notify_otp_failed_attempt_threshold": (0, 50),
            "notify_otp_fail_window_seconds": (60, 86400),
            "notify_otp_ttl_seconds": (60, 1800),
            "notify_otp_max_attempts": (1, 10),
            "totp_failed_attempt_threshold": (1, 50),
            "recovery_code_ttl_seconds": (60, 86400),
        }
        allowed = {*bounds, "notify_otp_enabled", "totp_enabled", "totp_mode"}
        unknown = sorted(set(login) - allowed)
        if unknown:
            raise ValueError(f"login_security 包含未知字段：{', '.join(unknown)}")
        for key, (lower, upper) in bounds.items():
            if login.get(key) is None:
                continue
            value = int(login[key])
            if not lower <= value <= upper:
                raise ValueError(f"login_security.{key} 必须在 {lower} 到 {upper} 之间")
        if login.get("totp_mode") is not None and str(login["totp_mode"]).lower() not in {
            "always",
            "after_failures",
        }:
            raise ValueError("login_security.totp_mode 必须是 always 或 after_failures")
    remote = changes.get("remote_plugin_update_check")
    if isinstance(remote, dict) and remote.get("interval_minutes") is not None:
        interval = int(remote["interval_minutes"])
        if not 30 <= interval <= 10080:
            raise ValueError("插件更新检查间隔必须在 30 到 10080 分钟之间")
    target = changes.get("app_update_target")
    if isinstance(target, dict):
        for key in ("remote", "branch"):
            if target.get(key) is not None and not _SAFE_REF_RE.fullmatch(
                str(target[key])
            ):
                raise ValueError(f"应用更新 {key} 包含不安全字符")
        if target.get("remote") is not None:
            normalize_update_remote(str(target["remote"]))
        if target.get("branch") is not None:
            normalize_update_branch(str(target["branch"]))
    allowed_limit_keys = {
        "llm_limits": {"per_minute", "daily_requests", "daily_tokens", "premium_daily"},
        "payout_limits": {"single_max", "daily_max"},
    }
    for group, allowed in allowed_limit_keys.items():
        value = changes.get(group)
        if isinstance(value, dict):
            unknown = sorted(set(value) - allowed)
            if unknown:
                raise ValueError(f"{group} 包含未知字段：{', '.join(unknown)}")
            for key, item in value.items():
                if item is not None and int(item) < 0:
                    raise ValueError(f"{group}.{key} 不能为负数")
    retention = changes.get("log_retention")
    if isinstance(retention, dict):
        bounds = {
            "runtime_log_retention_days": (0, 3650),
            "runtime_log_max_message_chars": (200, 20000),
            "runtime_log_max_detail_chars": (0, 50000),
            "trace_retention_days": (0, 3650),
            "trace_payload_snapshot_retention_days": (0, 3650),
            "native_raw_retention_days": (0, 30),
        }
        bool_keys = {
            "trace_enabled",
            "event_bus_delivery_enabled",
            "inline_updates_enabled",
            "native_raw_persist_enabled",
        }
        allowed = {*bounds, *bool_keys, "runtime_log_min_level"}
        unknown = sorted(set(retention) - allowed)
        if unknown:
            raise ValueError(f"log_retention 包含未知字段：{', '.join(unknown)}")
        for key, (lower, upper) in bounds.items():
            if retention.get(key) is None:
                continue
            value = int(retention[key])
            if not lower <= value <= upper:
                raise ValueError(f"log_retention.{key} 必须在 {lower} 到 {upper} 之间")
        level = retention.get("runtime_log_min_level")
        if level is not None and str(level).lower() not in {"debug", "info", "warn", "error"}:
            raise ValueError("log_retention.runtime_log_min_level 必须是 debug/info/warn/error")
    ui = changes.get("ui_preferences")
    if isinstance(ui, dict):
        for key in ("sidebar_order", "mobile_nav_order", "provider_order"):
            if key in ui and ui[key] is not None:
                items = list(ui[key])
                if len(items) != len(set(items)):
                    raise ValueError(f"ui_preferences.{key} 不能包含重复项")
                if key == "sidebar_order" and (
                    len(items) > 10 or any(str(item) not in _UI_ROUTES for item in items)
                ):
                    raise ValueError("ui_preferences.sidebar_order 包含未知页面或超过 10 项")
                if key == "mobile_nav_order" and (
                    len(items) > 4 or any(str(item) not in _UI_ROUTES for item in items)
                ):
                    raise ValueError("ui_preferences.mobile_nav_order 包含未知页面或超过 4 项")
                if key == "provider_order" and (
                    len(items) > 2048
                    or any(isinstance(item, bool) or int(item) <= 0 for item in items)
                ):
                    raise ValueError("ui_preferences.provider_order 包含无效 Provider ID")


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    changes = {key: value for key, value in args.items() if value is not None}
    if not changes:
        raise ValueError("至少提供一个要修改的系统设置")
    _validate_changes(changes)
    current = await get_settings(ctx, {})
    return {
        "summary": "更新全局系统设置",
        "current": {key: current.get(key) for key in changes},
        "target": changes,
        "warning": "全局设置会影响所有账号；Worker 将在提交后重新加载配置。",
    }


async def _set(ctx: ToolContext, key: str, value: Any) -> None:
    row = await ctx.db.get(SystemSetting, key)
    if row is None:
        ctx.db.add(SystemSetting(key=key, value=value))
    else:
        row.value = value


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    changes = {key: value for key, value in args.items() if value is not None}
    _validate_changes(changes)
    if "command_prefix" in changes:
        await _set(
            ctx,
            "command_prefix",
            {"value": str(changes["command_prefix"]).strip()},
        )
    if "timezone" in changes:
        await _set(ctx, "timezone", {"value": str(changes["timezone"]).strip()})
    for key in ("sudo_enabled", "command_prefix_required"):
        if key in changes:
            await _set(ctx, key, {"enabled": bool(changes[key])})
    if "command_echo_guard_previous_messages" in changes:
        await _set(
            ctx,
            "command_echo_guard_previous_messages",
            {"value": int(changes["command_echo_guard_previous_messages"])},
        )
    for key in _NESTED_KEYS:
        if key not in changes:
            continue
        current = await _value(ctx, key, {})
        merged = _merge(current, changes[key])
        if key == "login_security":
            merged = auth_login_security.login_security_config_to_dict(
                auth_login_security.normalize_login_security_config(merged)
            )
            if int(merged.get("notify_otp_failed_attempt_threshold", 0) or 0) <= 0:
                merged["notify_otp_enabled"] = False
        await _set(ctx, key, merged)
    await ctx.db.flush()
    return {
        "updated_fields": sorted(changes),
        "business_changed": bool(changes),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="settings.get",
            description="读取命令前缀、时区、登录安全、配额、日志保留、更新目标与 UI 偏好。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            channels=("web",),
            read_handler=get_settings,
        )
    )
    properties = {
        "command_prefix": {"type": "string"},
        "timezone": {"type": "string"},
        "sudo_enabled": {"type": "boolean"},
        "command_prefix_required": {"type": "boolean"},
        "command_echo_guard_previous_messages": {"type": "integer"},
        "login_security": {"type": "object"},
        "remote_plugin_update_check": {"type": "object"},
        "app_update_target": {"type": "object"},
        "llm_limits": {"type": "object"},
        "payout_limits": {"type": "object"},
        "log_retention": {"type": "object"},
        "ui_preferences": {"type": "object"},
    }
    registry.register(
        ToolSpec(
            name="settings.save",
            description="局部更新全局系统设置；AI 模块与总闸使用各自专用工具。",
            input_schema={
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            channels=("web",),
            risk="dangerous",
            preview_handler=save_preview,
            execute_handler=save_execute,
            runtime_effects=("reload_global_settings",),
        )
    )


__all__ = ["register"]
