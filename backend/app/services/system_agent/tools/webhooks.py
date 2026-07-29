"""账号入站 Webhook 状态与凭据轮换工作流。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from ....crypto import encrypt_str
from ....db.models.account import Account
from ....db.models.system import SystemSetting
from ....services import platform_capabilities, rate_limit_service
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter

_SETTING_PREFIX = "account_webhooks:"
_TOKEN_HEADER = "X-TelePilot-Webhook-Token"
_DEFAULT_HOOKS = [{"key": "default", "label": "默认入口", "enabled": True}]


def _account_id(ctx: ToolContext, args: dict[str, Any]) -> int:
    raw = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if raw is None:
        raise ValueError("需要 account_id")
    return int(raw)


def _setting_key(account_id: int) -> str:
    return f"{_SETTING_PREFIX}{account_id}"


def _hooks(value: Any) -> list[dict[str, Any]]:
    raw = value.get("hooks") if isinstance(value, dict) else None
    if not isinstance(raw, list):
        return list(_DEFAULT_HOOKS)
    items = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("key") or "").strip():
            continue
        items.append(
            {
                "key": str(item["key"]),
                "label": str(item.get("label") or item["key"]),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return items or list(_DEFAULT_HOOKS)


async def get_config(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    enabled = await platform_capabilities.is_module_enabled(ctx.db, "webhooks")
    row = await ctx.db.get(SystemSetting, _setting_key(account_id))
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    limits = await rate_limit_service.get_effective(
        ctx.db, account_id, "webhook_deliver"
    )
    return {
        "account_id": account_id,
        "module_enabled": enabled,
        "configured": row is not None,
        "has_token": bool(value.get("token_enc")),
        "token_header": _TOKEN_HEADER,
        "hooks": _hooks(value),
        "max_body_bytes": 65536,
        "rate_limit": {
            "per_second": limits.per_second,
            "per_minute": limits.per_minute,
            "per_hour": limits.per_hour,
            "per_day": limits.per_day,
        },
        "endpoint_pattern": f"/api/webhooks/{account_id}/{{hook_key}}",
        "web_path": f"/webhooks?aid={account_id}",
        "note": "Token 明文只在 Webhooks 页面显示，Agent 不返回或复述。",
    }


async def reset_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    current = await get_config(ctx, {"account_id": account_id})
    return {
        "summary": f"轮换账号 #{account_id} 的入站 Webhook Token",
        "current": current,
        "warning": "旧 Token 会立即失效；调用方必须到 Webhooks 页面复制新 Token 并更新。",
    }


async def reset_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_id(ctx, args)
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    row = await ctx.db.get(SystemSetting, _setting_key(account_id))
    now = datetime.now(UTC).isoformat()
    current = row.value if row is not None and isinstance(row.value, dict) else {}
    value = {
        **current,
        "token_enc": encrypt_str(secrets.token_urlsafe(32)),
        "hooks": _hooks(current),
        "created_at": current.get("created_at") or now,
        "updated_at": now,
    }
    if row is None:
        ctx.db.add(SystemSetting(key=_setting_key(account_id), value=value))
    else:
        row.value = value
    await ctx.db.flush()
    return {
        "account_id": account_id,
        "rotated": True,
        "web_path": f"/webhooks?aid={account_id}",
        "note": "新 Token 已生成。为避免在对话中暴露，请到 Webhooks 页面复制。",
        "business_changed": True,
    }


def register(registry: ToolRegistry) -> None:
    schema = {
        "type": "object",
        "properties": {"account_id": {"type": "integer"}},
        "required": ["account_id"],
        "additionalProperties": False,
    }
    registry.register(
        ToolSpec(
            name="webhooks.get",
            description="读取账号 Webhook 模块、入口、Hook、限速和凭据是否配置，不返回 Token。",
            input_schema=schema,
            read_handler=get_config,
        )
    )
    registry.register(
        ToolSpec(
            name="webhooks.reset_token",
            description="轮换账号共享 Webhook Token；新 Token 仅在 Webhooks 页面复制。",
            input_schema=schema,
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=reset_preview,
            execute_handler=reset_execute,
        )
    )


__all__ = ["register"]
