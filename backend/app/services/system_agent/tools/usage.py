"""LLM 调用记录与插件用量工作流。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, delete, func, select

from ....db.models.llm_usage import LLMUsage
from ....services.redactor import redact_text
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter, clamp_limit


def _account_scope(ctx: ToolContext, args: dict[str, Any]) -> int | None:
    return account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )


def _usage_view(row: LLMUsage) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "provider_id": row.provider_id,
        "provider_name": row.provider_name,
        "model": row.model,
        "client_identity_profile": row.client_identity_profile,
        "source": row.source,
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "latency_ms": int(row.latency_ms or 0),
        "success": bool(row.success),
        "error_type": row.error_type,
        "used_fallback": bool(row.used_fallback),
        "request_preview": redact_text(row.request_preview or "") or None,
        "response_preview": redact_text(row.response_preview or "") or None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def recent(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=20, maximum=100)
    account_id = _account_scope(ctx, args)
    query = select(LLMUsage)
    if account_id is not None:
        query = query.where(LLMUsage.account_id == account_id)
    rows = list(
        (
            await ctx.db.execute(
                query.order_by(LLMUsage.created_at.desc(), LLMUsage.id.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [_usage_view(row) for row in rows]
    success_count = sum(1 for item in items if item["success"])
    return {
        "account_id": account_id,
        "items": items,
        "summary": {
            "request_count": len(items),
            "success_count": success_count,
            "failed_count": len(items) - success_count,
            "fallback_count": sum(1 for item in items if item["used_fallback"]),
            "total_tokens": sum(int(item["input_tokens"]) + int(item["output_tokens"]) for item in items),
            "avg_latency_ms": (
                int(sum(int(item["latency_ms"]) for item in items) / len(items)) if items else 0
            ),
        },
    }


async def plugin_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    plugin_key = str(args.get("plugin_key") or "").strip()
    conditions = [LLMUsage.source.like("plugin:%")]
    account_id = _account_scope(ctx, args)
    if account_id is not None:
        conditions.append(LLMUsage.account_id == account_id)
    if plugin_key:
        conditions.append(LLMUsage.source == f"plugin:{plugin_key}")
    result = await ctx.db.execute(
        select(
            LLMUsage.source.label("source"),
            func.count(LLMUsage.id).label("request_count"),
            func.coalesce(func.sum(case((LLMUsage.success.is_(True), 1), else_=0)), 0).label("success_count"),
            func.coalesce(func.sum(LLMUsage.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(LLMUsage.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.avg(LLMUsage.latency_ms), 0).label("avg_latency_ms"),
            func.max(LLMUsage.created_at).label("last_used_at"),
        )
        .where(*conditions)
        .group_by(LLMUsage.source)
        .order_by(func.max(LLMUsage.created_at).desc())
        .limit(limit)
    )
    items = []
    for row in result.all():
        requests = int(row.request_count or 0)
        success = int(row.success_count or 0)
        input_tokens = int(row.input_tokens or 0)
        output_tokens = int(row.output_tokens or 0)
        source = str(row.source or "")
        items.append(
            {
                "plugin_key": source.removeprefix("plugin:"),
                "source": source,
                "request_count": requests,
                "success_count": success,
                "failed_count": max(0, requests - success),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "avg_latency_ms": int(row.avg_latency_ms or 0),
                "last_used_at": (row.last_used_at.isoformat() if row.last_used_at else None),
            }
        )
    return {"account_id": account_id, "items": items}


async def reset_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_scope(ctx, args)
    query = select(func.count(LLMUsage.id))
    if account_id is not None:
        query = query.where(LLMUsage.account_id == account_id)
    count = int((await ctx.db.execute(query)).scalar_one())
    return {
        "summary": (
            f"清空账号 #{account_id} 的近期 LLM 调用记录"
            if account_id is not None
            else "清空全部近期 LLM 调用记录"
        ),
        "account_id": account_id,
        "current_count": count,
        "warning": "删除后 AI 中心的近期统计会从零开始，记录不可恢复。",
    }


async def reset_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = _account_scope(ctx, args)
    stmt = delete(LLMUsage)
    if account_id is not None:
        stmt = stmt.where(LLMUsage.account_id == account_id)
    result = await ctx.db.execute(stmt)
    return {
        "account_id": account_id,
        "deleted": max(0, int(getattr(result, "rowcount", 0) or 0)),
        "business_changed": True,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="usage.recent",
            channels=("web",),
            description="读取最近 LLM 调用、成功率、fallback、Token 与延迟，正文已脱敏。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_handler=recent,
        )
    )
    registry.register(
        ToolSpec(
            name="usage.plugins",
            channels=("web",),
            description="按插件聚合 LLM 请求数、成功率、Token 和平均延迟。",
            input_schema={
                "type": "object",
                "properties": {
                    "plugin_key": {"type": "string"},
                    "account_id": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_handler=plugin_summary,
        )
    )
    registry.register(
        ToolSpec(
            name="usage.reset",
            channels=("web",),
            description="清空全部近期 LLM 调用记录。",
            input_schema={
                "type": "object",
                "properties": {"account_id": {"type": "integer"}},
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=reset_preview,
            execute_handler=reset_execute,
        )
    )


__all__ = ["register"]
