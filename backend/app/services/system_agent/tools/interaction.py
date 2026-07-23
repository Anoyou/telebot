"""交互规则只读工具。"""

from __future__ import annotations

from typing import Any

from ....db.models.account import Account
from ....redis_client import get_redis
from ....services import account_bot_service
from ....services.interaction import session_index
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit


def _rule_public_view(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "enabled": bool(rule.get("enabled", True)),
        "trigger_mode": rule.get("trigger_mode"),
        "trigger_texts": rule.get("trigger_texts") or [],
        "chat_ids": rule.get("chat_ids") or [],
        "amount": rule.get("amount"),
        "amount_match_mode": rule.get("amount_match_mode"),
        "receiver_user_id": rule.get("receiver_user_id"),
        "receiver_text": rule.get("receiver_text"),
        "concurrency": rule.get("concurrency"),
        "valid_seconds": rule.get("valid_seconds"),
        "action": rule.get("action"),
        "module_key": rule.get("module_key"),
        "module_action": rule.get("module_action"),
        "module_start_keywords": rule.get("module_start_keywords") or [],
        "open_commands": rule.get("open_commands") or [],
        "close_commands": rule.get("close_commands") or [],
        "status_commands": rule.get("status_commands") or [],
        "pause_keywords": rule.get("pause_keywords") or rule.get("pause_texts") or [],
        "end_keywords": rule.get("end_keywords") or rule.get("end_texts") or [],
    }


async def _load_rules(ctx: ToolContext, account_id: int) -> list[dict[str, Any]]:
    account = await ctx.db.get(Account, account_id)
    if account is None:
        return []
    try:
        cfg = await account_bot_service.get_transfer_notice_config(ctx.db, account_id)
    except Exception:  # noqa: BLE001
        cfg = {}
    rules_raw = cfg.get("rules") if isinstance(cfg, dict) else None
    return account_bot_service.normalize_interaction_rules(rules_raw)


async def _interaction_module_note(ctx: ToolContext) -> dict[str, Any]:
    """只读工具可继续；附带模块状态。写操作由 _require_interaction_module 拦截。"""

    from ....services import platform_capabilities as platform_caps

    enabled = await platform_caps.is_module_enabled(ctx.db, "interaction_bot")
    return {
        "interaction_bot_module_enabled": enabled,
        "module_note": (
            None
            if enabled
            else "Interaction Bot 平台能力已暂停：polling 与 interaction 通道不可用；"
            "规则配置与历史会话记录保留，管理 Bot / userbot 不受影响。"
        ),
    }


async def _require_interaction_module(ctx: ToolContext) -> None:
    from ....services import platform_capabilities as platform_caps

    enabled = await platform_caps.is_module_enabled(ctx.db, "interaction_bot")
    if not enabled:
        raise ValueError(
            "Interaction Bot 平台能力已暂停，无法修改交互规则或依赖交互通道的配置。"
            "请先在系统设置的平台能力中重新启用。"
        )


async def list_rules(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        return {"error": "account_id_required", "message": "请提供 account_id"}
    rules = await _load_rules(ctx, account_id)
    enabled_only = bool(args.get("enabled_only", False))
    items = [_rule_public_view(r) for r in rules]
    if enabled_only:
        items = [r for r in items if r.get("enabled")]
    module_meta = await _interaction_module_note(ctx)
    return {
        "account_id": account_id,
        "count": len(items),
        "note": "交互规则保存在账号级配置 JSON，不属于通用 Rule 表。",
        "rules": items,
        **module_meta,
    }


async def get_rule(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = str(args.get("rule_id") or "").strip()
    if account_id is None or not rule_id:
        return {"error": "args_required", "message": "需要 account_id 与 rule_id"}
    rules = await _load_rules(ctx, account_id)
    for rule in rules:
        if str(rule.get("id")) == rule_id:
            return {
                "account_id": account_id,
                "rule": _rule_public_view(rule),
                "note": "交互规则在账号级配置 JSON 中，触发/暂停/结束条件见返回字段。",
            }
    return {"error": "not_found", "message": f"交互规则 {rule_id} 不存在"}


async def list_active_sessions(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ._helpers import mark_external_fields, mark_external_text

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        return {"error": "account_id_required", "message": "请提供 account_id"}
    limit = clamp_limit(args.get("limit"), default=20, maximum=100)
    chat_id = args.get("chat_id")
    try:
        chat_id_int = int(chat_id) if chat_id not in (None, "") else None
    except (TypeError, ValueError):
        chat_id_int = None

    sessions: list[dict[str, Any]] = []
    try:
        redis = get_redis()
        prefix = session_index.SESSION_KEY_PREFIX
        if chat_id_int is not None:
            keys = await session_index.list_indexed_session_keys(
                redis, account_id=account_id, chat_id=chat_id_int
            )
            key_list = list(keys or [])
        else:
            # 有限 SCAN
            key_list = []
            cursor = 0
            pattern = f"{prefix}{account_id}:*"
            while len(key_list) < limit:
                cursor, batch = await redis.scan(cursor=cursor, match=pattern, count=50)
                key_list.extend(batch or [])
                if cursor == 0:
                    break
        for key in key_list[:limit]:
            raw = await redis.get(key)
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            import json

            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {"raw": str(raw)[:200]}
            if isinstance(data, dict):
                safe = mark_external_fields(
                    data,
                    {"text", "message", "last_message", "prompt", "label", "title", "note", "error"},
                )
                sessions.append(
                    {
                        "session_key": key,
                        "rule_id": safe.get("rule_id"),
                        "chat_id": safe.get("chat_id"),
                        "status": safe.get("status") or safe.get("state"),
                        "user_id": safe.get("user_id") or safe.get("payer_user_id"),
                        "started_at": safe.get("started_at") or safe.get("created_at"),
                        "text": safe.get("text") or safe.get("message") or safe.get("last_message"),
                    }
                )
            else:
                sessions.append({"session_key": key, "data": mark_external_text(str(data)[:200])})
    except Exception as exc:  # noqa: BLE001
        return {
            "account_id": account_id,
            "count": 0,
            "sessions": [],
            "warning": f"读取活跃会话失败：{str(exc)[:200]}",
        }
    return {"account_id": account_id, "count": len(sessions), "sessions": sessions}


async def save_rule_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_interaction_module(ctx)
    from ....services import interaction_rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("需要 account_id")
    rule_id = str(args.get("id") or args.get("rule_id") or "").strip() or None
    fields = {
        k: args[k]
        for k in args
        if k not in {"account_id", "id", "rule_id"} and args[k] is not None
    }
    current = None
    if rule_id:
        current = await interaction_rule_service.get_rule(ctx.db, account_id, rule_id)
        if current is None:
            raise ValueError(f"交互规则 {rule_id} 不存在")
    return {
        "summary": (
            f"更新交互规则 {rule_id}" if rule_id else f"创建交互规则到账号 #{account_id}"
        ),
        "mode": "update" if rule_id else "create",
        "account_id": account_id,
        "rule_id": rule_id,
        "current": _rule_public_view(current) if current else None,
        "target_fields": fields,
        "note": "交互规则保存在账号配置 JSON，不属于通用 Rule 表。",
    }


async def save_rule_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_interaction_module(ctx)
    from ....services import interaction_rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("需要 account_id")
    rule_id = str(args.get("id") or args.get("rule_id") or "").strip() or None
    fields = {
        k: args[k]
        for k in args
        if k not in {"account_id", "id", "rule_id"}
    }
    rule = await interaction_rule_service.save_rule(
        ctx.db, account_id, rule_id=rule_id, fields=fields
    )
    return {
        "mode": "update" if rule_id else "create",
        "rule": _rule_public_view(rule),
        "business_changed": True,
    }


async def set_enabled_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_interaction_module(ctx)
    from ....services import interaction_rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = str(args.get("rule_id") or args.get("id") or "").strip()
    enabled = bool(args.get("enabled"))
    if account_id is None or not rule_id:
        raise ValueError("需要 account_id 与 rule_id")
    current = await interaction_rule_service.get_rule(ctx.db, account_id, rule_id)
    if current is None:
        raise ValueError(f"交互规则 {rule_id} 不存在")
    return {
        "summary": f"{'启用' if enabled else '禁用'}交互规则 {rule_id}",
        "account_id": account_id,
        "rule_id": rule_id,
        "current_enabled": bool(current.get("enabled", True)),
        "target_enabled": enabled,
        "note": "暂时禁用不会自动恢复。若用户要求“停两小时”，请明确告知需手动重新启用。",
    }


async def set_enabled_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_interaction_module(ctx)
    from ....services import interaction_rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = str(args.get("rule_id") or args.get("id") or "").strip()
    enabled = bool(args.get("enabled"))
    if account_id is None or not rule_id:
        raise ValueError("需要 account_id 与 rule_id")
    rule = await interaction_rule_service.set_enabled(ctx.db, account_id, rule_id, enabled)
    return {"rule": _rule_public_view(rule), "business_changed": True}


async def delete_rule_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_interaction_module(ctx)
    from ....services import interaction_rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = str(args.get("rule_id") or args.get("id") or "").strip()
    if account_id is None or not rule_id:
        raise ValueError("需要 account_id 与 rule_id")
    current = await interaction_rule_service.get_rule(ctx.db, account_id, rule_id)
    if current is None:
        raise ValueError(f"交互规则 {rule_id} 不存在")
    return {
        "summary": f"删除交互规则 {rule_id}",
        "account_id": account_id,
        "rule": _rule_public_view(current),
        "warning": "危险操作：删除后不可恢复。",
    }


async def delete_rule_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _require_interaction_module(ctx)
    from ....services import interaction_rule_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    rule_id = str(args.get("rule_id") or args.get("id") or "").strip()
    if account_id is None or not rule_id:
        raise ValueError("需要 account_id 与 rule_id")
    info = await interaction_rule_service.delete_rule(ctx.db, account_id, rule_id)
    return {**info, "business_changed": True}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="interaction.list_rules",
            description="列出账号的交互规则（账号级配置 JSON，非通用 Rule 表）。返回触发、启用状态等结构化字段。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "enabled_only": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_rules,
        )
    )
    registry.register(
        ToolSpec(
            name="interaction.get_rule",
            description="获取单条交互规则详情，含触发/暂停/结束条件与启用状态。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "rule_id": {"type": "string"},
                },
                "required": ["rule_id"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_rule,
        )
    )
    registry.register(
        ToolSpec(
            name="interaction.list_active_sessions",
            description="列出账号当前活跃的交互会话（Redis），可按 chat_id 过滤。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "chat_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_active_sessions,
        )
    )
    registry.register(
        ToolSpec(
            name="interaction.save_rule",
            description="创建或更新交互规则。有 rule_id 时只更新明确字段；无 ID 时创建。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "id": {"type": "string"},
                    "rule_id": {"type": "string"},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "trigger_mode": {"type": "string"},
                    "trigger_texts": {"type": "array", "items": {"type": "string"}},
                    "chat_ids": {"type": "array", "items": {"type": "integer"}},
                    "amount": {},
                    "module_key": {"type": "string"},
                    "module_action": {"type": "string"},
                    "pause_keywords": {"type": "array", "items": {"type": "string"}},
                    "end_keywords": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=save_rule_preview,
            execute_handler=save_rule_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="interaction.set_enabled",
            description="启用或禁用交互规则。禁用不会自动恢复。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "rule_id": {"type": "string"},
                    "id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["enabled"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=set_enabled_preview,
            execute_handler=set_enabled_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="interaction.delete_rule",
            description="删除交互规则（危险）。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "rule_id": {"type": "string"},
                    "id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_rule_preview,
            execute_handler=delete_rule_execute,
            runtime_effects=("reload_config",),
        )
    )
