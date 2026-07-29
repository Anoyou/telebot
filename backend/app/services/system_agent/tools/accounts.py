"""账号工具：列表/详情 + 暂停恢复/重启 Worker。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.account import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_PAUSED,
    Account,
    DeviceProfile,
    Proxy,
)
from ....db.models.feature import AccountFeature
from ....db.models.rate_limit import RateLimitTemplate
from ....schemas.account import AccountUpdateRequest
from ....services import account_service
from ..context import ToolContext
from ..registry import PreparedAction, ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit


def _account_summary(row: Account, enabled_features: int | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "display_name": row.display_name,
        "username": row.tg_username,
        "phone": row.phone,
        "status": row.status,
        "paused": str(row.status or "").lower() == "paused",
        "enabled_features": enabled_features,
        "proxy_id": row.proxy_id,
        "template_id": row.template_id,
        "device_profile_id": row.device_profile_id,
    }


async def list_accounts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    q = select(Account).order_by(Account.id.asc()).limit(limit)
    if ctx.channel == "bot" and ctx.account_id is not None:
        q = q.where(Account.id == ctx.account_id)
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    # 批量统计启用功能
    feature_counts: dict[int, int] = {}
    if rows:
        ids = [r.id for r in rows]
        fr = await ctx.db.execute(
            select(AccountFeature.account_id, AccountFeature.enabled).where(
                AccountFeature.account_id.in_(ids)
            )
        )
        for aid, enabled in fr.all():
            if enabled:
                feature_counts[int(aid)] = feature_counts.get(int(aid), 0) + 1
    return {
        "count": len(rows),
        "accounts": [_account_summary(r, feature_counts.get(r.id, 0)) for r in rows],
    }


async def get_account(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        return {"error": "account_id_required", "message": "请提供 account_id"}
    row = await ctx.db.get(Account, account_id)
    if row is None:
        return {"error": "not_found", "message": f"账号 {account_id} 不存在"}
    fr = await ctx.db.execute(
        select(AccountFeature).where(AccountFeature.account_id == account_id)
    )
    features = list(fr.scalars().all())
    enabled = [f.feature_key for f in features if f.enabled]
    disabled = [f.feature_key for f in features if not f.enabled]
    return {
        "account": _account_summary(row, len(enabled)),
        "enabled_features": enabled,
        "disabled_features": disabled,
        "notes": row.notes,
        "tags": list(row.tags or []) if row.tags else [],
    }


async def set_paused_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("请提供 account_id")
    paused = bool(args.get("paused"))
    row = await ctx.db.get(Account, account_id)
    if row is None:
        raise ValueError(f"账号 {account_id} 不存在")
    return {
        "summary": f"{'暂停' if paused else '恢复'}账号 #{account_id}",
        "account_id": account_id,
        "current_status": row.status,
        "target_paused": paused,
        "current_paused": str(row.status or "").lower() == "paused",
        "warning": None if not paused else "暂停后 worker 将停止；不会自动恢复。",
    }


async def set_paused_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ....services import account_service

    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("请提供 account_id")
    paused = bool(args.get("paused"))
    row = await ctx.db.get(Account, account_id)
    if row is None:
        raise ValueError(f"账号 {account_id} 不存在")
    if not paused:
        try:
            account_service.ensure_account_secrets_decryptable(row)
        except ValueError as exc:
            raise ValueError(
                "账号登录凭据无法解密，不能恢复；请恢复原 MASTER_KEY 或重新登录账号"
            ) from exc
    row.status = ACCOUNT_STATUS_PAUSED if paused else ACCOUNT_STATUS_ACTIVE
    await ctx.db.flush()
    return {
        "account_id": account_id,
        "status": row.status,
        "paused": paused,
        "business_changed": True,
    }


async def restart_worker_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("请提供 account_id")
    row = await ctx.db.get(Account, account_id)
    if row is None:
        raise ValueError(f"账号 {account_id} 不存在")
    return {
        "summary": f"重启账号 #{account_id} 的 Worker",
        "account_id": account_id,
        "current_status": row.status,
        "warning": "危险操作：将停止并重新拉起 worker，短暂中断消息处理。",
    }


async def restart_worker_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("请提供 account_id")
    row = await ctx.db.get(Account, account_id)
    if row is None:
        raise ValueError(f"账号 {account_id} 不存在")
    # 数据库无变更；运行时由 runtime_effects 处理
    return {
        "account_id": account_id,
        "status": row.status,
        "restart_requested": True,
        "business_changed": False,
        "note": "Worker 重启在事务提交后执行",
    }


def _account_id(ctx: ToolContext, args: dict[str, Any]) -> int:
    value = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if value is None:
        raise ValueError("请提供 account_id")
    return value


async def _validated_update(
    ctx: ToolContext, args: dict[str, Any]
) -> tuple[Account, dict[str, Any]]:
    account_id = _account_id(ctx, args)
    row = await ctx.db.get(Account, account_id)
    if row is None:
        raise ValueError(f"账号 {account_id} 不存在")
    raw = {
        key: args[key]
        for key in (
            "display_name",
            "notes",
            "tags",
            "template_id",
            "proxy_id",
            "device_profile_id",
        )
        if key in args
    }
    if not raw:
        raise ValueError("至少提供一个要修改的账号字段")
    payload = AccountUpdateRequest(**raw).model_dump(exclude_unset=True)
    if payload.get("proxy_id") is not None and await ctx.db.get(
        Proxy, int(payload["proxy_id"])
    ) is None:
        raise ValueError(f"代理 #{payload['proxy_id']} 不存在")
    if payload.get("template_id") is not None and await ctx.db.get(
        RateLimitTemplate, int(payload["template_id"])
    ) is None:
        raise ValueError(f"风控模板 #{payload['template_id']} 不存在")
    if payload.get("device_profile_id") is not None and await ctx.db.get(
        DeviceProfile, int(payload["device_profile_id"])
    ) is None:
        raise ValueError(f"设备档案 #{payload['device_profile_id']} 不存在")
    return row, payload


async def update_preview(ctx: ToolContext, args: dict[str, Any]) -> PreparedAction:
    row, payload = await _validated_update(ctx, args)
    current = {key: getattr(row, key) for key in payload}
    restart_required = any(
        key in {"proxy_id", "template_id"} and current.get(key) != value
        for key, value in payload.items()
    )
    preview = {
        "summary": f"更新账号 #{row.id} 的资料与运行配置",
        "account_id": row.id,
        "current": current,
        "target": payload,
        "worker_restart_required": restart_required,
        "device_note": (
            "device_profile_id 只影响下次重新登录，当前 Telegram session 不会改变。"
            if "device_profile_id" in payload
            else None
        ),
    }
    return PreparedAction(
        arguments={"account_id": row.id, **payload, "_restart_required": restart_required},
        preview=preview,
    )


async def update_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    row, payload = await _validated_update(ctx, args)
    changed: list[str] = []
    restart_required = False
    for key, value in payload.items():
        if getattr(row, key) == value:
            continue
        setattr(row, key, value)
        changed.append(key)
        if key in {"proxy_id", "template_id"}:
            restart_required = True
    await ctx.db.flush()
    return {
        "account_id": row.id,
        "changed_fields": changed,
        "restart_required": restart_required,
        "business_changed": bool(changed),
    }


async def clone_config_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dst_id = _account_id(ctx, args)
    src_id = int(args.get("from_account_id") or 0)
    if src_id <= 0:
        raise ValueError("需要 from_account_id")
    if src_id == dst_id:
        raise ValueError("源账号和目标账号不能相同")
    src = await ctx.db.get(Account, src_id)
    dst = await ctx.db.get(Account, dst_id)
    if src is None or dst is None:
        raise ValueError("源账号或目标账号不存在")
    features = [str(item).strip() for item in (args.get("features") or []) if str(item).strip()]
    return {
        "summary": f"从账号 #{src_id} 复制配置到账号 #{dst_id}",
        "account_id": dst_id,
        "from_account_id": src_id,
        "features": features,
        "scope": "指定功能" if features else "全部功能与对应规则",
        "warning": "目标账号同功能的现有配置和规则会被覆盖。",
    }


async def clone_config_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # 复用 preview 的完整存在性/范围校验。
    preview = await clone_config_preview(ctx, args)
    stats = await account_service.clone_config(
        ctx.db,
        int(preview["from_account_id"]),
        int(preview["account_id"]),
        features=preview["features"] or None,
        notify=False,
        commit=False,
        web_user_id=ctx.web_user_id,
    )
    return {
        "account_id": preview["account_id"],
        "from_account_id": preview["from_account_id"],
        **stats,
        "business_changed": bool(stats["features"] or stats["rules"]),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="accounts.list",
            description="列出账号摘要（状态、标签、启用功能数）。Bot 渠道仅返回当前绑定账号。",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回数量，默认 50，最大 200"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_accounts,
        )
    )
    registry.register(
        ToolSpec(
            name="accounts.get",
            description="获取单个账号详情及功能启停列表。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "description": "账号 ID"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_account,
        )
    )
    registry.register(
        ToolSpec(
            name="accounts.set_paused",
            description="暂停或恢复账号。paused=true 暂停，false 恢复。不会自动定时恢复。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "paused": {"type": "boolean"},
                },
                "required": ["paused"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=set_paused_preview,
            execute_handler=set_paused_execute,
            # 实际 pause/resume 由执行器根据 arguments.paused 选择
            runtime_effects=("pause_or_resume_worker",),
        )
    )
    registry.register(
        ToolSpec(
            name="accounts.restart_worker",
            description="重启账号 Worker（危险）。数据库状态不变，仅重启运行时。",
            input_schema={
                "type": "object",
                "properties": {"account_id": {"type": "integer"}},
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=restart_worker_preview,
            execute_handler=restart_worker_execute,
            runtime_effects=("restart_worker",),
        )
    )
    registry.register(
        ToolSpec(
            name="accounts.update",
            description=(
                "更新账号显示名、备注、标签、风控模板、代理或设备档案。代理/模板变化后自动重启 Worker。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "display_name": {"type": ["string", "null"]},
                    "notes": {"type": ["string", "null"]},
                    "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                    "template_id": {"type": ["integer", "null"]},
                    "proxy_id": {"type": ["integer", "null"]},
                    "device_profile_id": {"type": ["integer", "null"]},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=update_preview,
            execute_handler=update_execute,
            runtime_effects=("update_account_runtime",),
        )
    )
    registry.register(
        ToolSpec(
            name="accounts.clone_config",
            description="从源账号复制全部或指定功能配置及其通用规则到目标账号。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "description": "目标账号 ID"},
                    "from_account_id": {"type": "integer", "description": "源账号 ID"},
                    "features": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["from_account_id"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            channels=("web",),
            risk="dangerous",
            preview_handler=clone_config_preview,
            execute_handler=clone_config_execute,
            runtime_effects=("reload_config",),
        )
    )
