"""Provider 工具：列表 + 创建/更新/删除/验证（写操作需确认）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.command import LLMProvider
from ....schemas.command import LLMProviderCreate, LLMProviderUpdate
from ....services import command_service
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
    try:
        provider_id = int(args.get("id") or 0)
    except (TypeError, ValueError):
        provider_id = 0
    if provider_id > 0:
        q = q.where(LLMProvider.id == provider_id)
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


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    provider_id = args.get("id") or args.get("provider_id")
    if provider_id not in (None, ""):
        row = await ctx.db.get(LLMProvider, int(provider_id))
        if row is None:
            raise ValueError(f"Provider #{provider_id} 不存在")
        fields = {
            k: args[k]
            for k in (
                "name",
                "provider",
                "base_url",
                "default_model",
                "api_format",
                "api_key",
            )
            if k in args
        }
        return {
            "summary": f"更新 Provider #{row.id} {row.name}",
            "mode": "update",
            "current": _provider_view(row),
            "target_fields": {k: ("***" if k == "api_key" and v else v) for k, v in fields.items()},
            "has_api_key_input": bool(args.get("api_key")),
        }
    name = str(args.get("name") or "").strip()
    provider = str(args.get("provider") or "openai").strip()
    default_model = str(args.get("default_model") or "").strip()
    if not name or not default_model:
        raise ValueError("创建 Provider 需要 name 与 default_model")
    return {
        "summary": f"创建 Provider「{name}」",
        "mode": "create",
        "name": name,
        "provider": provider,
        "base_url": args.get("base_url"),
        "default_model": default_model,
        "api_format": args.get("api_format") or "chat_completions",
        "has_api_key_input": bool(args.get("api_key")),
        "note": "API Key 可从聊天中粘贴，或在确认卡片补填（Web）。",
    }


async def save_precheck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """保存前做真实上游验证；失败保持 pending 并清除密钥。"""

    from ..provider_verify import resolve_provider_verify_args, run_quick_verify

    resolved = await resolve_provider_verify_args(ctx.db, args)
    # 更新且未提供新 Key、也未改连接参数时，跳过上游验证
    provider_id = args.get("id") or args.get("provider_id")
    touching_key = bool(args.get("api_key"))
    touching_conn = any(k in args for k in ("base_url", "default_model", "api_format", "provider"))
    if provider_id not in (None, "") and not touching_key and not touching_conn:
        return {"skipped": True, "reason": "no_connection_change"}
    return await run_quick_verify(
        base_url=resolved.get("base_url"),
        api_key=resolved.get("api_key"),
        api_format=resolved.get("api_format"),
        default_model=resolved.get("default_model"),
        provider=resolved.get("provider"),
        protocol_profile=str(resolved.get("protocol_profile") or "standard"),
        client_identity_profile=str(resolved.get("client_identity_profile") or "auto"),
        request_headers=resolved.get("request_headers"),
    )


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    provider_id = args.get("id") or args.get("provider_id")
    try:
        if provider_id not in (None, ""):
            data: dict[str, Any] = {}
            for key in (
                "name",
                "provider",
                "base_url",
                "default_model",
                "api_format",
                "api_key",
                "modality",
                "tags",
                "cost_tier",
                "notes",
                "proxy_id",
            ):
                if key in args:
                    data[key] = args[key]
            payload = LLMProviderUpdate(**data)
            out = await command_service.update_provider(ctx.db, int(provider_id), payload)
            dumped = out.model_dump() if hasattr(out, "model_dump") else dict(out)
            # 永不返回 key
            dumped.pop("api_key", None)
            return {"mode": "update", "provider": dumped, "business_changed": True}

        create = LLMProviderCreate(
            name=str(args.get("name") or "").strip(),
            provider=str(args.get("provider") or "openai"),  # type: ignore[arg-type]
            api_key=args.get("api_key"),
            base_url=args.get("base_url"),
            default_model=str(args.get("default_model") or "").strip(),
            api_format=args.get("api_format") or "chat_completions",
        )
        out = await command_service.create_provider(ctx.db, create)
        dumped = out.model_dump() if hasattr(out, "model_dump") else dict(out)
        dumped.pop("api_key", None)
        return {"mode": "create", "provider": dumped, "business_changed": True}
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            raise ValueError(str(detail.get("message") or detail.get("code") or exc)) from None
        raise ValueError(str(detail or exc)) from None


async def delete_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    provider_id = int(args.get("id") or args.get("provider_id"))
    row = await ctx.db.get(LLMProvider, provider_id)
    if row is None:
        raise ValueError(f"Provider #{provider_id} 不存在")
    return {
        "summary": f"删除 Provider #{provider_id} {row.name}",
        "provider": _provider_view(row),
        "warning": "危险操作：删除后依赖该 Provider 的指令可能失效。",
    }


async def delete_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    provider_id = int(args.get("id") or args.get("provider_id"))
    await command_service.delete_provider(ctx.db, provider_id)
    return {"id": provider_id, "deleted": True, "business_changed": True}


async def verify_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    provider_id = args.get("id") or args.get("provider_id")
    if provider_id not in (None, ""):
        row = await ctx.db.get(LLMProvider, int(provider_id))
        if row is None:
            raise ValueError(f"Provider #{provider_id} 不存在")
        return {
            "summary": f"验证 Provider #{row.id} {row.name}",
            "mode": "existing",
            "provider": _provider_view(row),
            "note": "真实验证在确认执行时发起；失败不会修改 Provider。",
        }
    return {
        "summary": "验证新 Provider 接入参数",
        "mode": "draft",
        "base_url": args.get("base_url"),
        "default_model": args.get("default_model"),
        "has_api_key_input": bool(args.get("api_key")),
        "note": "草稿验证不会落库。",
    }


async def verify_precheck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """真实验证放在 precheck：失败保持 pending 并清密钥。"""

    from ..provider_verify import resolve_provider_verify_args, run_quick_verify

    resolved = await resolve_provider_verify_args(ctx.db, args)
    result = await run_quick_verify(
        base_url=resolved.get("base_url"),
        api_key=resolved.get("api_key"),
        api_format=resolved.get("api_format"),
        default_model=resolved.get("default_model"),
        provider=resolved.get("provider"),
        protocol_profile=str(resolved.get("protocol_profile") or "standard"),
        client_identity_profile=str(resolved.get("client_identity_profile") or "auto"),
        request_headers=resolved.get("request_headers"),
    )
    return result


async def verify_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """precheck 已完成上游验证；execute 仅返回成功摘要（不落库）。"""

    provider_id = args.get("id") or args.get("provider_id")
    return {
        "ok": True,
        "provider_id": int(provider_id) if provider_id not in (None, "") else None,
        "business_changed": False,
        "note": "上游验证已通过，未修改任何 Provider 配置。",
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="providers.list",
            description="列出模型提供商（脱敏）：ID/名称/模型清单/has_api_key/tools 支持情况。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
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
    registry.register(
        ToolSpec(
            name="providers.save",
            description="创建或更新 Provider。ID 为空时创建；支持聊天粘贴 api_key 或 Web 卡片补填。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "provider_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "provider": {"type": "string"},
                    "api_key": {"type": "string"},
                    "base_url": {"type": "string"},
                    "default_model": {"type": "string"},
                    "api_format": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            secret_argument_names=("api_key",),
            preview_handler=save_preview,
            precheck_handler=save_precheck,
            execute_handler=save_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="providers.delete",
            description="删除 Provider（危险）。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "provider_id": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=delete_preview,
            execute_handler=delete_execute,
        )
    )
    registry.register(
        ToolSpec(
            name="providers.verify",
            description="验证 Provider 可调用性（不落库）。可指定已有 ID 或草稿参数。",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "provider_id": {"type": "integer"},
                    "provider": {"type": "string"},
                    "api_key": {"type": "string"},
                    "base_url": {"type": "string"},
                    "default_model": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            secret_argument_names=("api_key",),
            preview_handler=verify_preview,
            precheck_handler=verify_precheck,
            execute_handler=verify_execute,
        )
    )
