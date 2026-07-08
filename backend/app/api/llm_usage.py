"""LLM 调用记录查询 API。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, delete, func, select

from ..db.models.llm_usage import LLMUsage
from ..deps import CurrentUser, DBSession
from ..services.redactor import redact_text

router = APIRouter(prefix="/api/llm/usage", tags=["llm-usage"])


class LLMUsageItem(BaseModel):
    """最近一次 LLM 调用记录。"""

    id: int
    account_id: int | None
    provider_id: int | None
    provider_name: str | None
    model: str | None
    source: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    error_type: str | None
    used_fallback: bool
    request_preview: str | None = None
    response_preview: str | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, row: LLMUsage) -> LLMUsageItem:
        return cls(
            id=row.id,
            account_id=row.account_id,
            provider_id=row.provider_id,
            provider_name=row.provider_name,
            model=row.model,
            source=row.source,
            input_tokens=int(row.input_tokens or 0),
            output_tokens=int(row.output_tokens or 0),
            latency_ms=int(row.latency_ms or 0),
            success=bool(row.success),
            error_type=row.error_type,
            used_fallback=bool(row.used_fallback),
            request_preview=redact_text(row.request_preview or "") or None,
            response_preview=redact_text(row.response_preview or "") or None,
            created_at=row.created_at,
        )

    model_config = ConfigDict(from_attributes=True)


class LLMUsageSummary(BaseModel):
    """最近调用摘要。"""

    request_count: int
    success_count: int
    failed_count: int
    fallback_count: int
    total_tokens: int
    avg_latency_ms: int


class LLMUsageRecentResponse(BaseModel):
    """最近 LLM 调用记录列表。"""

    items: list[LLMUsageItem]
    summary: LLMUsageSummary


class PluginLLMUsageSummaryItem(BaseModel):
    """按插件 source 聚合的 LLM 用量。"""

    plugin_key: str
    source: str
    request_count: int
    success_count: int
    failed_count: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: int
    last_used_at: datetime | None = None


class PluginLLMUsageSummaryResponse(BaseModel):
    """插件 LLM 用量聚合列表。"""

    items: list[PluginLLMUsageSummaryItem]


class LLMUsageResetResponse(BaseModel):
    """清空 LLM 调用记录后的结果。"""

    deleted: int


@router.get("/recent", response_model=LLMUsageRecentResponse)
async def list_recent_llm_usage(
    db: DBSession,
    _user: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
) -> LLMUsageRecentResponse:
    """返回最近 LLM 调用记录与摘要，供 AI 中心 Usage 页展示。"""
    rows = (
        await db.execute(
            select(LLMUsage)
            .order_by(LLMUsage.created_at.desc(), LLMUsage.id.desc())
            .limit(limit)
        )
    ).scalars().all()

    items = [LLMUsageItem.from_row(row) for row in rows]
    request_count = len(items)
    success_count = sum(1 for item in items if item.success)
    failed_count = request_count - success_count
    fallback_count = sum(1 for item in items if item.used_fallback)
    total_tokens = sum(item.input_tokens + item.output_tokens for item in items)
    avg_latency_ms = int(sum(item.latency_ms for item in items) / request_count) if request_count else 0

    return LLMUsageRecentResponse(
        items=items,
        summary=LLMUsageSummary(
            request_count=request_count,
            success_count=success_count,
            failed_count=failed_count,
            fallback_count=fallback_count,
            total_tokens=total_tokens,
            avg_latency_ms=avg_latency_ms,
        ),
    )


@router.delete("/recent", response_model=LLMUsageResetResponse)
async def reset_recent_llm_usage(
    db: DBSession,
    _user: CurrentUser,
) -> LLMUsageResetResponse:
    """清空 LLM 调用记录，让 AI 中心的近期统计从零开始。"""

    result = await db.execute(delete(LLMUsage))
    await db.commit()
    return LLMUsageResetResponse(deleted=max(0, int(result.rowcount or 0)))


@router.get("/plugins/summary", response_model=PluginLLMUsageSummaryResponse)
async def list_plugin_llm_usage_summary(
    db: DBSession,
    _user: CurrentUser,
    plugin_key: str | None = Query(None, min_length=1, max_length=48),
    limit: int = Query(50, ge=1, le=200),
) -> PluginLLMUsageSummaryResponse:
    """按 ``source=plugin:<key>`` 聚合插件 LLM 用量。"""

    source_expr = LLMUsage.source
    conditions = [LLMUsage.source.like("plugin:%")]
    if plugin_key:
        conditions.append(LLMUsage.source == f"plugin:{plugin_key.strip()}")

    result = (
        await db.execute(
            select(
                source_expr.label("source"),
                func.count(LLMUsage.id).label("request_count"),
                func.coalesce(
                    func.sum(case((LLMUsage.success.is_(True), 1), else_=0)),
                    0,
                ).label("success_count"),
                func.coalesce(func.sum(LLMUsage.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(LLMUsage.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.avg(LLMUsage.latency_ms), 0).label("avg_latency_ms"),
                func.max(LLMUsage.created_at).label("last_used_at"),
            )
            .where(*conditions)
            .group_by(source_expr)
            .order_by(func.max(LLMUsage.created_at).desc())
            .limit(limit)
        )
    )
    items: list[PluginLLMUsageSummaryItem] = []
    for row in result.all():
        source = str(row.source or "")
        request_count = int(row.request_count or 0)
        success_count = int(row.success_count or 0)
        input_tokens = int(row.input_tokens or 0)
        output_tokens = int(row.output_tokens or 0)
        items.append(
            PluginLLMUsageSummaryItem(
                plugin_key=source.removeprefix("plugin:"),
                source=source,
                request_count=request_count,
                success_count=success_count,
                failed_count=max(0, request_count - success_count),
                total_tokens=input_tokens + output_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                avg_latency_ms=int(row.avg_latency_ms or 0),
                last_used_at=row.last_used_at,
            )
        )
    return PluginLLMUsageSummaryResponse(items=items)
