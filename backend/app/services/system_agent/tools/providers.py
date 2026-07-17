"""Provider 只读工具。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.command import LLMProvider
from ....services.llm_dto import LLMProviderDTO
from ..config import tools_model_for_dto
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import clamp_limit


def _provider_view(row: LLMProvider) -> dict[str, Any]:
    dto = LLMProviderDTO.from_orm_row(row)
    models = []
    for item in dto.models or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        models.append(
            {
                "id": mid,
                "enabled": bool(item.get("enabled", True)),
                "supports_tools": bool(item.get("supports_tools"))
                if "supports_tools" in item
                else dto.capabilities_for_model(mid).tools,
            }
        )
    tools_model = tools_model_for_dto(dto)
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "base_url": getattr(row, "base_url", None),
        "default_model": row.default_model,
        "api_format": getattr(row, "api_format", None),
        "has_api_key": dto.has_api_key,
        "modality": getattr(row, "modality", None),
        "tags": list(getattr(row, "tags", None) or []),
        "cost_tier": getattr(row, "cost_tier", None),
        "models": models,
        "tools_model": tools_model,
    }


async def list_providers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    q = select(LLMProvider).order_by(LLMProvider.id.asc()).limit(limit)
    if args.get("id") not in (None, ""):
        try:
            q = q.where(LLMProvider.id == int(args["id"]))
        except (TypeError, ValueError):
            pass
    name = str(args.get("name") or "").strip()
    if name:
        q = q.where(LLMProvider.name.ilike(f"%{name}%"))
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    return {
        "count": len(rows),
        "providers": [_provider_view(r) for r in rows],
        "note": "不返回 API Key 明文；has_api_key 表示是否已配置。",
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="providers.list",
            description="列出模型提供商（脱敏）：ID/名称/模型清单/has_api_key/tools 支持情况。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_providers,
        )
    )
