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
from ..registry import PreparedAction, ToolRegistry, ToolSpec
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
        "protocol_profile": getattr(row, "protocol_profile", None) or "standard",
        "web_search_api_format": getattr(row, "web_search_api_format", None) or "auto",
        "client_identity_profile": getattr(row, "client_identity_profile", None) or "auto",
        "has_api_key": dto.has_api_key,
        "modality": getattr(row, "modality", None),
        "tags": list(getattr(row, "tags", None) or []),
        "cost_tier": getattr(row, "cost_tier", None),
        "notes": getattr(row, "notes", None),
        "proxy_id": getattr(row, "proxy_id", None),
        "models": models,
        "tools_model": tools_model,
    }


def _mark_reload_ai_commands(ctx: ToolContext) -> None:
    if ctx.action is None:
        return
    stored = dict(ctx.action.arguments or {})
    stored["reload_ai_command_accounts"] = True
    ctx.action.arguments = stored


def _mark_gateway_candidate_sync(ctx: ToolContext) -> None:
    ctx.gateway_candidate_sync = True


def _reject_request_headers(args: dict[str, Any]) -> None:
    if "request_headers" in args:
        raise ValueError(
            "System Agent 工具参数包含自定义请求头，已被本地安全策略拒绝，"
            "尚未向上游发起请求；请使用 AI Provider 设置页配置请求头"
        )


async def list_providers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"), default=50, maximum=200)
    q = select(LLMProvider).order_by(LLMProvider.id.asc()).limit(limit)
    result = await ctx.db.execute(q)
    rows = list(result.scalars().all())
    return {
        "count": len(rows),
        "providers": [_provider_view(r) for r in rows],
        "note": "不返回 API Key 明文；has_api_key 表示是否已配置。",
    }


async def save_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _reject_request_headers(args)
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
                "protocol_profile",
                "web_search_api_format",
                "client_identity_profile",
                "api_key",
                "modality",
                "tags",
                "cost_tier",
                "notes",
                "proxy_id",
                "clear_proxy",
                "models",
                "request_headers",
            )
            if k in args
        }
        return {
            "summary": f"更新 Provider #{row.id} {row.name}",
            "mode": "update",
            "current": _provider_view(row),
            "target_fields": {
                k: (
                    "***"
                    if k == "api_key" and v
                    else ([{"name": item.get("name"), "scopes": item.get("scopes", [])} for item in v]
                          if k == "request_headers" and isinstance(v, list)
                          else v)
                )
                for k, v in fields.items()
            },
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
    """保存前做真实上游验证；失败保持 pending，仅鉴权失败清除 Key。"""

    _reject_request_headers(args)
    from ..provider_verify import resolve_provider_verify_args, run_quick_verify

    resolved = await resolve_provider_verify_args(ctx.db, args)
    # 更新且未提供新 Key、也未改连接参数时，跳过上游验证
    provider_id = args.get("id") or args.get("provider_id")
    touching_key = bool(args.get("api_key"))
    touching_conn = any(
        key in args
        for key in (
            "base_url",
            "default_model",
            "api_format",
            "provider",
            "protocol_profile",
            "client_identity_profile",
            "request_headers",
            "proxy_id",
            "clear_proxy",
        )
    )
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
        proxy_url=resolved.get("proxy_url"),
        using_saved_key=bool(provider_id not in (None, "") and not touching_key),
        retain_temporary_key=True,
    )


async def save_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _reject_request_headers(args)
    from fastapi import HTTPException

    provider_id = args.get("id") or args.get("provider_id")
    try:
        if provider_id not in (None, ""):
            current = await ctx.db.get(LLMProvider, int(provider_id))
            if current is None:
                raise ValueError(f"Provider #{provider_id} 不存在")
            if (
                str(getattr(current, "execution_backend", "direct") or "direct")
                == "codex_gateway"
            ):
                _mark_gateway_candidate_sync(ctx)
            data: dict[str, Any] = {}
            for key in (
                "name",
                "provider",
                "base_url",
                "default_model",
                "api_format",
                "protocol_profile",
                "web_search_api_format",
                "client_identity_profile",
                "api_key",
                "modality",
                "tags",
                "cost_tier",
                "notes",
                "proxy_id",
                "clear_proxy",
                "models",
                "request_headers",
            ):
                if key in args:
                    data[key] = args[key]
            payload = LLMProviderUpdate(**data)
            out = await command_service.update_provider(ctx.db, int(provider_id), payload)
            dumped = out.model_dump() if hasattr(out, "model_dump") else dict(out)
            # 永不返回 key
            dumped.pop("api_key", None)
            _mark_reload_ai_commands(ctx)
            return {"mode": "update", "provider": dumped, "business_changed": True}

        create = LLMProviderCreate(
            name=str(args.get("name") or "").strip(),
            provider=str(args.get("provider") or "openai"),  # type: ignore[arg-type]
            api_key=args.get("api_key"),
            base_url=args.get("base_url"),
            default_model=str(args.get("default_model") or "").strip(),
            api_format=args.get("api_format") or "chat_completions",
            protocol_profile=args.get("protocol_profile") or "standard",
            web_search_api_format=args.get("web_search_api_format") or "auto",
            client_identity_profile=args.get("client_identity_profile") or "auto",
            modality=args.get("modality") or "text",
            tags=list(args.get("tags") or []),
            cost_tier=int(args.get("cost_tier") or 2),
            notes=args.get("notes"),
            proxy_id=args.get("proxy_id"),
            models=list(args.get("models") or []),
            request_headers=list(args.get("request_headers") or []),
        )
        out = await command_service.create_provider(ctx.db, create)
        dumped = out.model_dump() if hasattr(out, "model_dump") else dict(out)
        dumped.pop("api_key", None)
        _mark_reload_ai_commands(ctx)
        return {"mode": "create", "provider": dumped, "business_changed": True}
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            raise ValueError(str(detail.get("message") or detail.get("code") or exc)) from None
        raise ValueError(str(detail or exc)) from None


async def probe_and_add_preview(ctx: ToolContext, args: dict[str, Any]) -> PreparedAction:
    """用临时凭据立即测活；成功后把发现结果固化为待确认创建参数。"""

    _reject_request_headers(args)
    from ..provider_verify import resolve_provider_verify_args, run_quick_verify

    if (args.get("id") or args.get("provider_id")) not in (None, ""):
        raise ValueError("新 Provider 测活请提供 Base URL 与 API Key，不要传已有 Provider ID")

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
        proxy_url=resolved.get("proxy_url"),
        using_saved_key=False,
    )

    base_url = str(result.get("base_url") or resolved.get("base_url") or "").strip()
    api_format = str(result.get("api_format") or resolved.get("api_format") or "chat_completions")
    provider = str(result.get("provider") or resolved.get("provider") or "openai")
    default_model = str(
        result.get("requested_model")
        or resolved.get("default_model")
        or result.get("model")
        or ""
    ).strip()
    name = str(args.get("name") or result.get("suggested_name") or "模型提供商").strip()[:64]
    if not base_url or not default_model:
        raise ValueError("测活成功但未能确定 Base URL 或默认模型，请补充后重试")
    key = str(resolved.get("api_key") or "")
    public_config = (name, provider, base_url, default_model, api_format)
    if key and any(key in value for value in public_config):
        raise ValueError("测活结果的公开配置字段包含当前凭据，已拒绝创建 Provider Action")

    canonical_arguments = {
        **args,
        "name": name,
        "provider": provider,
        "base_url": base_url,
        "default_model": default_model,
        "api_format": api_format,
    }
    discovered_models = [
        item if isinstance(item, dict) else {"id": str(item), "enabled": True, "custom": False}
        for item in (result.get("models") or [])
        if (isinstance(item, dict) and str(item.get("id") or "").strip())
        or (not isinstance(item, dict) and str(item).strip())
    ]
    if not discovered_models:
        discovered_models = [{"id": default_model, "enabled": True, "custom": False}]
    canonical_arguments["models"] = discovered_models[:200]
    return PreparedAction(
        arguments=canonical_arguments,
        preview={
            "summary": f"测活成功，是否添加 Provider「{name}」？",
            "mode": "verified_create",
            "provider": {
                "name": name,
                "provider": provider,
                "base_url": base_url,
                "default_model": default_model,
                "api_format": api_format,
            },
            "liveness": {
                "ok": True,
                "model": default_model,
                "latency_ms": result.get("latency_ms"),
            },
            "discovered_model_count": len(discovered_models),
            "has_api_key_input": bool(args.get("api_key")),
            "note": "测活已通过，尚未保存。确认后才会添加 Provider；拒绝或过期会清除临时密钥。",
        },
    )


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
    current = await ctx.db.get(LLMProvider, provider_id)
    if current is None:
        raise ValueError(f"Provider #{provider_id} 不存在")
    if str(getattr(current, "execution_backend", "direct") or "direct") == "codex_gateway":
        _mark_gateway_candidate_sync(ctx)
    await command_service.delete_provider(ctx.db, provider_id)
    _mark_reload_ai_commands(ctx)
    return {"id": provider_id, "deleted": True, "business_changed": True}


async def verify_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _reject_request_headers(args)
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
    """真实验证放在 precheck：失败保持 pending，仅鉴权失败清 Key。"""

    _reject_request_headers(args)
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
        proxy_url=resolved.get("proxy_url"),
        using_saved_key=bool(
            (args.get("id") or args.get("provider_id")) not in (None, "")
            and not args.get("api_key")
        ),
        retain_temporary_key=True,
    )
    return result


async def verify_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """precheck 已完成上游验证；execute 仅返回成功摘要（不落库）。"""

    _reject_request_headers(args)
    provider_id = args.get("id") or args.get("provider_id")
    return {
        "ok": True,
        "provider_id": int(provider_id) if provider_id not in (None, "") else None,
        "business_changed": False,
        "note": "上游验证已通过，未修改任何 Provider 配置。",
    }


def register(registry: ToolRegistry) -> None:
    compatibility_fields = {
        "protocol_profile": {"type": "string"},
        "web_search_api_format": {"type": "string"},
        "client_identity_profile": {"type": "string"},
    }
    routing_fields = {
        "modality": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "cost_tier": {"type": "integer"},
        "notes": {"type": ["string", "null"]},
        "proxy_id": {"type": ["integer", "null"]},
        "clear_proxy": {"type": "boolean"},
        "models": {"type": "array", "items": {"type": "object"}},
    }
    registry.register(
        ToolSpec(
            name="providers.list",
            channels=("web",),
            description="列出全部模型提供商（脱敏）：ID/名称/模型清单/has_api_key/tools 支持情况。无需 ID 或名称筛选。",
            input_schema={
                "type": "object",
                "properties": {
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
            name="providers.probe_and_add",
            channels=("web",),
            description=(
                "收到新 Provider 的 Base URL 与 API Key 时使用：立即执行不落库测活，"
                "自动发现可用模型；仅测活成功才生成“是否添加 Provider”的待确认操作。"
                "失败时不创建 Action。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "provider": {"type": "string"},
                    "api_key": {"type": "string"},
                    "base_url": {"type": "string"},
                    "default_model": {"type": "string"},
                    "api_format": {"type": "string"},
                    **compatibility_fields,
                },
                "required": ["base_url"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            diagnostic_safe=True,
            secret_argument_names=("api_key",),
            precheck_clear_secret_argument_names=("api_key",),
            allow_secret_input=False,
            preview_handler=probe_and_add_preview,
            execute_handler=save_execute,
            runtime_effects=("reload_commands",),
        )
    )
    registry.register(
        ToolSpec(
            name="providers.save",
            channels=("web",),
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
                    **compatibility_fields,
                    **routing_fields,
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            secret_argument_names=("api_key",),
            precheck_clear_secret_argument_names=("api_key",),
            preview_handler=save_preview,
            precheck_handler=save_precheck,
            execute_handler=save_execute,
            runtime_effects=("reload_commands",),
        )
    )
    registry.register(
        ToolSpec(
            name="providers.delete",
            channels=("web",),
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
            runtime_effects=("reload_commands",),
        )
    )
    registry.register(
        ToolSpec(
            name="providers.verify",
            channels=("web",),
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
                    "api_format": {"type": "string"},
                    **compatibility_fields,
                },
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="normal",
            diagnostic_safe=True,
            secret_argument_names=("api_key",),
            precheck_clear_secret_argument_names=("api_key",),
            preview_handler=verify_preview,
            precheck_handler=verify_precheck,
            execute_handler=verify_execute,
        )
    )
