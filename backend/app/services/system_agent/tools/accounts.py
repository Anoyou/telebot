"""账号工具：列表/详情 + 暂停恢复/重启 Worker。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.account import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_PAUSED,
    Account,
)
from ....db.models.feature import AccountFeature
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
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
