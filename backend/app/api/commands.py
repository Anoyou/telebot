"""自定义命令 + LLM Provider REST API（Sprint2 #2）。

路由前缀：
- ``/api/commands/templates``       全局模板 CRUD
- ``/api/commands/llm-providers``   LLM provider CRUD + fetch-models + test-model
- ``/api/accounts/{aid}/commands``  账号 × 模板 启用关系

安全红线：
- LLM provider 列表和普通配置接口不返回明文 ``api_key``，只返 ``has_api_key:bool``
- 明文仅由单 Provider 专用鉴权接口按需返回，响应禁用缓存且记录审计事件
- 模板内容不含敏感信息，可正常 audit；audit log 里会写命令名和类型，不写完整 config
"""

from __future__ import annotations

import asyncio
import json
import re
import time as _time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

from ..deps import CurrentUser, DBSession
from ..schemas.command import (
    AccountCommandItem,
    AICommandEnablementSummary,
    BuiltinCommandItem,
    ChatTestModelResult,
    ChatTestModelsRequest,
    ChatTestModelsResponse,
    ClientIdentityVersionDetectItem,
    ClientIdentityVersionDetectResponse,
    ClientIdentityVersionItem,
    ClientIdentityVersionsResponse,
    ClientIdentityVersionsUpdateRequest,
    CommandTemplateCreate,
    CommandTemplateOut,
    CommandTemplateUpdate,
    DetectProviderProtocolsRequest,
    DetectProviderProtocolsResponse,
    FetchModelsPreviewRequest,
    FetchModelsPreviewResponse,
    FetchModelsResponse,
    FullLivenessPreviewRequest,
    FullLivenessPreviewResponse,
    FullLivenessRunRequest,
    FullLivenessRunResponse,
    FullLivenessRunStartResponse,
    LivenessResultItem,
    LLMProviderApiKeyReveal,
    LLMProviderCreate,
    LLMProviderOut,
    LLMProviderUpdate,
    ProtocolIdentityAttempt,
    ProtocolProbeResult,
    QuickVerifyProviderRequest,
    TestModelRequest,
    TestModelResponse,
)
from ..services import audit, command_service, llm_diagnostics, llm_identity, llm_liveness
from ..services.ai_feature import is_ai_enabled
from ..services.llm_identity import (
    IDENTITY_PROBE_ORDER,
    default_identity_for_format,
    resolve_identity,
)
from ..services.llm_profiles import infer_protocol_profile, resolve_protocol_profile
from ..services.llm_protocol import (
    normalize_base_url,
    provider_endpoint,
    provider_models_endpoints,
)
from ..services.llm_request_headers import (
    REQUEST_SCOPE_LIVENESS,
    REQUEST_SCOPE_MODELS,
    RequestHeaderConfigError,
    plan_request_headers,
    request_headers_for_scope,
)

router = APIRouter(tags=["commands"])


def _is_deepseek_v4_responses_target(base_url: str, model: str) -> bool:
    """DeepSeek 官方 Responses API 当前仅开放给 deepseek-v4-flash。"""

    host = (urlsplit(base_url).hostname or "").lower()
    return host == "api.deepseek.com" and model.strip().lower() == "deepseek-v4-flash"


async def _require_ai_enabled(db: DBSession) -> None:
    if not await is_ai_enabled(db):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AI_DISABLED",
                "message": "AI 能力已在系统设置中关闭，请先启用后再配置或调用模型。",
            },
        )


async def _emit_llm_diagnostic_usage(
    *,
    provider_row,
    source: str,
    started: float,
    system: str,
    user_prompt: str,
    model: str | None,
    result=None,
    error: Exception | None = None,
    api_format_override: str | None = None,
    identity_override: str | None = None,
) -> None:
    """Record diagnostic LLM probes without reserving account budget.

    Provider test calls are admin diagnostics rather than account business
    traffic, so they intentionally skip budget pre-reservation. They still must
    be visible in usage with a diagnostic source for cost/risk review.
    """

    from ..db.models.command import default_api_format_for
    from ..services import llm_runtime
    from ..services.llm_identity import resolve_identity
    from ..services.llm_runtime import (
        UsageRecord,
        preview_text_for_usage,
        request_preview_for_usage,
    )
    from ..services.llm_usage_service import ensure_llm_usage_callback_registered

    try:
        ensure_llm_usage_callback_registered()
    except Exception:  # noqa: BLE001
        pass

    success = error is None
    effective_api_format = (
        api_format_override
        or getattr(provider_row, "api_format", None)
        or default_api_format_for(getattr(provider_row, "provider", "openai"))
    )
    client_identity_profile = resolve_identity(
        identity_override or getattr(provider_row, "client_identity_profile", None),
        effective_api_format,
        recommended_profile=resolve_protocol_profile(
            effective_api_format,
            getattr(provider_row, "protocol_profile", None),
            base_url=getattr(provider_row, "base_url", None),
            model=model or getattr(provider_row, "default_model", None),
        ).recommended_identity,
    ).profile
    await llm_runtime._emit_usage(
        UsageRecord(
            provider_id=getattr(provider_row, "id", None),
            provider_name=getattr(provider_row, "name", None),
            model=model or getattr(provider_row, "default_model", None),
            client_identity_profile=client_identity_profile,
            input_tokens=int(getattr(result, "input_tokens", 0) or 0),
            output_tokens=int(getattr(result, "output_tokens", 0) or 0),
            latency_ms=max(0, int((_time.monotonic() - started) * 1000)),
            success=success,
            error_type=None if success else _diagnostic_error_type(error),
            source=source,
            used_fallback=False,
            fallback_chain=[str(getattr(provider_row, "name", "") or getattr(provider_row, "id", ""))],
            request_preview=request_preview_for_usage(system, user_prompt),
            response_preview=preview_text_for_usage(getattr(result, "text", None)),
        )
    )


def _diagnostic_error_type(error: Exception | None) -> str | None:
    if error is None:
        return None
    msg = str(error).lower()
    if "timeout" in msg:
        return "timeout"
    if "429" in msg or "限流" in msg:
        return "rate_limit"
    if "401" in msg or "403" in msg or "auth" in msg or "unauthorized" in msg:
        return "auth"
    if "connect" in msg or "network" in msg or "proxy" in msg:
        return "network"
    return type(error).__name__.lower()


# ════════════════════════════════════════════════════════════
# 0.4.1 内置命令只读接口
# ════════════════════════════════════════════════════════════
@router.get("/api/commands/builtin", response_model=list[BuiltinCommandItem])
async def list_builtin_commands(_user: CurrentUser) -> list[BuiltinCommandItem]:
    """返回所有内置命令的元数据：name + aliases + doc。

    数据源是 worker 进程里的 ``_BUILTIN`` 字典（``@builtin`` 装饰器声明），
    主进程 import 该模块即可读到——内置命令是静态注册的，不依赖运行时状态。

    用途：
    - 前端「自定义命令模板」编辑器顶部展示，让用户知道哪些 name/alias 已被占用
    - 与自定义模板的 aliases 校验配合：API 创建/更新时已会拒绝撞内置名
    """
    from ..worker.command import _BUILTIN

    out: list[BuiltinCommandItem] = []
    for name, item in sorted(_BUILTIN.items()):
        out.append(
            BuiltinCommandItem(
                name=name,
                aliases=list(item.aliases),
                doc=item.doc or "",
            )
        )
    return out


# ════════════════════════════════════════════════════════════
# 命令模板 CRUD
# ════════════════════════════════════════════════════════════


@router.get("/api/commands/templates", response_model=list[CommandTemplateOut])
async def list_templates(db: DBSession, _user: CurrentUser) -> list[CommandTemplateOut]:
    """列出全部命令模板。"""
    rows = await command_service.list_templates(db)
    return [CommandTemplateOut.model_validate(r) for r in rows]


@router.post("/api/commands/templates", response_model=CommandTemplateOut)
async def create_template(
    payload: CommandTemplateCreate,
    db: DBSession,
    user: CurrentUser,
) -> CommandTemplateOut:
    """新建命令模板。"""
    tpl = await command_service.create_template(db, payload)
    await audit.write(
        db,
        user.id,
        "command_template.create",
        target=f"command_template:{tpl.id}",
        # 不记录完整 config（可能含 system_prompt 较长）
        detail={"name": tpl.name, "type": tpl.type},
    )
    await db.commit()
    return CommandTemplateOut.model_validate(tpl)


@router.patch(
    "/api/commands/templates/{tpl_id}", response_model=CommandTemplateOut
)
async def update_template(
    tpl_id: int,
    payload: CommandTemplateUpdate,
    db: DBSession,
    user: CurrentUser,
) -> CommandTemplateOut:
    """更新命令模板；任何字段变化都会通知所有启用了它的 worker reload。"""
    tpl = await command_service.update_template(db, tpl_id, payload)
    await audit.write(
        db,
        user.id,
        "command_template.update",
        target=f"command_template:{tpl.id}",
        detail=payload.model_dump(exclude_unset=True, exclude={"config"}),
    )
    await db.commit()
    # 通知所有启用此模板的 worker reload
    aids = await _aids_using_template(db, tpl.id)
    await command_service.notify_reload(aids)
    return CommandTemplateOut.model_validate(tpl)


@router.delete("/api/commands/templates/{tpl_id}")
async def delete_template(
    tpl_id: int, db: DBSession, user: CurrentUser
) -> dict[str, bool]:
    """删除命令模板；级联删 link。"""
    aids = await command_service.delete_template(db, tpl_id)
    await audit.write(
        db,
        user.id,
        "command_template.delete",
        target=f"command_template:{tpl_id}",
    )
    await db.commit()
    await command_service.notify_reload(aids)
    return {"ok": True}


async def _aids_using_template(db, tpl_id: int) -> list[int]:
    """收集启用了某模板的 account_id 列表（用于 reload 通知）。"""
    from sqlalchemy import select

    from ..db.models.command import AccountCommandLink

    rows = (
        await db.execute(
            select(AccountCommandLink.account_id).where(
                AccountCommandLink.template_id == tpl_id,
                AccountCommandLink.enabled.is_(True),
            )
        )
    ).scalars().all()
    return list(rows)


# ════════════════════════════════════════════════════════════
# LLM Provider CRUD
# ════════════════════════════════════════════════════════════


@router.get(
    "/api/commands/llm-providers", response_model=list[LLMProviderOut]
)
async def list_providers(db: DBSession, _user: CurrentUser) -> list[LLMProviderOut]:
    """列出全部 LLM provider；不含明文 key。"""
    await _require_ai_enabled(db)
    return await command_service.list_providers(db)


@router.get(
    "/api/commands/llm-providers/{pid}/api-key",
    response_model=LLMProviderApiKeyReveal,
)
async def reveal_provider_api_key(
    pid: int,
    response: Response,
    db: DBSession,
    user: CurrentUser,
) -> LLMProviderApiKeyReveal:
    """按需解密单个 Provider 的 API Key，不影响普通列表与配置响应。"""

    await _require_ai_enabled(db)
    row = await command_service.get_provider_row(db, pid)
    if not row.api_key_enc:
        raise _llm_err(
            "LLM_PROVIDER_API_KEY_NOT_CONFIGURED",
            "该模型提供商尚未配置 API Key",
            409,
        )
    from ..crypto import decrypt_str

    try:
        api_key = decrypt_str(row.api_key_enc)
    except Exception:  # noqa: BLE001
        raise _llm_err(
            "LLM_PROVIDER_API_KEY_DECRYPT_FAILED",
            "API Key 解密失败，请重新填写并保存",
            422,
        ) from None

    await audit.write(
        db,
        user.id,
        "llm_provider.api_key_reveal",
        target=f"llm_provider:{pid}",
        detail={"name": row.name},
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return LLMProviderApiKeyReveal(api_key=api_key)


@router.post(
    "/api/commands/llm-providers", response_model=LLMProviderOut
)
async def create_provider(
    payload: LLMProviderCreate, db: DBSession, user: CurrentUser
) -> LLMProviderOut:
    """新建 LLM provider；api_key 加密落库。

    通知 worker reload：理论上新建的 provider 还没有模板引用它，但用户场景里
    经常先 create 再立刻去模板 PATCH 一次去关联，那时就要 worker 已知道这条
    新 provider；统一让所有"启用了 ai 模板"的账号 reload 一次最简单——
    worker 重新拉一次 DB，新 provider 进 ctx.providers，下次模板 PATCH 触发的
    第二次 reload 也无害（重新拉同样数据）。
    """
    await _require_ai_enabled(db)
    out = await command_service.create_provider(db, payload)
    await audit.write(
        db,
        user.id,
        "llm_provider.create",
        target=f"llm_provider:{out.id}",
        # 仅记录元信息，不记录 api_key 是否提供（元信息有限）
        detail={"name": out.name, "provider": out.provider, "default_model": out.default_model},
    )
    await db.commit()
    aids = await command_service.list_all_account_ids(db)
    await command_service.notify_reload(aids)
    return out


def _provider_update_audit_detail(payload: LLMProviderUpdate) -> dict[str, object]:
    """构造 Provider 更新审计摘要，排除所有可复用凭据。"""

    submitted = payload.model_dump(exclude_unset=True)
    detail = payload.model_dump(
        exclude_unset=True,
        exclude={"api_key", "request_headers"},
    )
    if "api_key" in submitted:
        detail["api_key_changed"] = True
    if "request_headers" in submitted:
        detail["request_headers_changed"] = True
        detail["request_headers_count"] = len(submitted["request_headers"] or [])
    return detail


@router.patch(
    "/api/commands/llm-providers/{pid}", response_model=LLMProviderOut
)
async def update_provider(
    pid: int,
    payload: LLMProviderUpdate,
    db: DBSession,
    user: CurrentUser,
) -> LLMProviderOut:
    """更新 LLM provider。

    api_key 行为约定：``""`` 清空、非空替换、None / 缺省不动。
    audit detail 中**绝不写** api_key 或兼容请求头值。

    通知 worker reload：所有启用了 type=ai 模板的账号都会被通知，
    避免 api_key / base_url / tags 改动后"TG 里没生效"。
    """
    await _require_ai_enabled(db)
    out = await command_service.update_provider(db, pid, payload)
    audit_detail = _provider_update_audit_detail(payload)
    await audit.write(
        db,
        user.id,
        "llm_provider.update",
        target=f"llm_provider:{out.id}",
        detail=audit_detail,
    )
    await db.commit()
    # 通知所有启用了 ai 类型模板的账号热加载
    aids = await command_service.list_all_account_ids(db)
    await command_service.notify_reload(aids)
    return out


@router.delete("/api/commands/llm-providers/{pid}")
async def delete_provider(
    pid: int, db: DBSession, user: CurrentUser
) -> dict[str, bool]:
    """删除 LLM provider；引用此 provider 的 ai 命令调用之后会失败。

    同样要通知 worker reload，让 ctx.providers 把这条删掉——否则被引用的
    模板下一次还会用 worker 内存里的旧条目跑（还能跑通），等用户疑惑为什么
    "我都删了它还在用"。
    """
    await _require_ai_enabled(db)
    aids = await command_service.list_all_account_ids(db)
    await command_service.delete_provider(db, pid)
    await audit.write(
        db,
        user.id,
        "llm_provider.delete",
        target=f"llm_provider:{pid}",
    )
    await db.commit()
    await command_service.notify_reload(aids)
    return {"ok": True}


# ════════════════════════════════════════════════════════════
# 账号 × 模板 启用关系
# ════════════════════════════════════════════════════════════


@router.get(
    "/api/accounts/{aid}/commands", response_model=list[AccountCommandItem]
)
async def list_account_commands(
    aid: int, db: DBSession, _user: CurrentUser
) -> list[AccountCommandItem]:
    """列出该账号已启用 + 可用全部命令模板。"""
    return await command_service.list_for_account(db, aid)


@router.get(
    "/api/commands/ai/enablement-summary",
    response_model=AICommandEnablementSummary,
)
async def ai_command_enablement_summary(
    db: DBSession, _user: CurrentUser
) -> AICommandEnablementSummary:
    """统计已有多少账号启用了至少一条 AI 命令模板。"""
    return AICommandEnablementSummary(
        **await command_service.ai_command_enablement_summary(db)
    )


@router.post(
    "/api/accounts/{aid}/commands/{tpl_id}",
    response_model=dict,
)
async def enable_account_command(
    aid: int, tpl_id: int, db: DBSession, user: CurrentUser
) -> dict[str, bool]:
    """启用某账号的某模板。"""
    await command_service.enable_for_account(db, aid, tpl_id)
    await audit.write(
        db,
        user.id,
        "account_command.enable",
        target=f"account:{aid}/command_template:{tpl_id}",
    )
    await db.commit()
    await command_service.notify_reload(aid)
    return {"ok": True}


@router.delete(
    "/api/accounts/{aid}/commands/{tpl_id}",
    response_model=dict,
)
async def disable_account_command(
    aid: int, tpl_id: int, db: DBSession, user: CurrentUser
) -> dict[str, bool]:
    """禁用某账号的某模板。"""
    await command_service.disable_for_account(db, aid, tpl_id)
    await audit.write(
        db,
        user.id,
        "account_command.disable",
        target=f"account:{aid}/command_template:{tpl_id}",
    )
    await db.commit()
    await command_service.notify_reload(aid)
    return {"ok": True}


# ════════════════════════════════════════════════════════════
# LLM Provider 模型管理（Fetch + Test）
# ════════════════════════════════════════════════════════════


def _llm_err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _resolve_proxy_url(db, proxy_id: int | None) -> str | None:
    """把 provider.proxy_id 翻译成 httpx 接受的 ``socks5://...`` / ``http://...`` URL。

    与 ``worker/runtime._build_proxy_url`` 同一逻辑；这里独立实现是因为本模块跑在
    主进程内（不能 import worker.runtime——后者持有 telethon 等重依赖）。
    """
    from ..services.llm_proxy_service import resolve_proxy_url

    return await resolve_proxy_url(db, proxy_id)


@router.post(
    "/api/commands/llm-providers/fetch-models-preview",
    response_model=FetchModelsPreviewResponse,
)
async def fetch_models_preview(
    payload: FetchModelsPreviewRequest, db: DBSession, user: CurrentUser
) -> FetchModelsPreviewResponse:
    """用编辑表单里的当前值（provider / base_url / api_key / api_format / proxy_id）
    发一次 ``GET {base_url}/models``，**只返 ID 列表**，不落库。

    用途：让用户在「编辑」对话框里填完字段就能直接 Fetch，
    不必先点保存、再重新打开编辑。

    api_key 取值优先级：
    1. 入参 ``api_key`` 非空 → 用入参；
    2. 入参 ``api_key`` 留空 / None 且给了 ``pid`` → 用 DB 里已存的（解密）；
    3. 都没有 → 不带 Authorization（如本地 Ollama）。
    """
    await _require_ai_enabled(db)

    from ..crypto import decrypt_str
    from ..db.models.command import LLM_API_FORMAT_ANTHROPIC_MESSAGES

    # api_key：优先入参，否则回落到 DB 里已存的
    api_key = (payload.api_key or "").strip()
    stored_request_headers_token: str | None = None
    if payload.pid is not None:
        try:
            row = await command_service.get_provider_row(db, payload.pid)
            if not api_key and row.api_key_enc:
                api_key = decrypt_str(row.api_key_enc) or ""
            stored_request_headers_token = getattr(row, "request_headers_enc", None)
        except Exception:  # noqa: BLE001
            # pid 错也无所谓，继续走"无 key"路径让用户看到具体的 401
            api_key = ""

    default_base_urls = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "ollama": "http://localhost:11434/v1",
    }
    base_url = normalize_base_url(
        payload.base_url or default_base_urls[payload.provider]
    )
    proxy_url = await _resolve_proxy_url(db, payload.proxy_id)

    headers = {"Accept": "application/json"}
    if api_key and payload.api_format == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        compatibility_headers = request_headers_for_scope(
            payload.request_headers
            if payload.request_headers is not None
            else stored_request_headers_token,
            REQUEST_SCOPE_MODELS,
            existing_token=stored_request_headers_token,
        )
        headers = plan_request_headers(
            system_headers=headers,
            compatibility_headers=compatibility_headers,
        )
    except RequestHeaderConfigError as exc:
        raise _llm_err("FETCH_REQUEST_HEADERS_INVALID", str(exc), 422) from None

    client_kwargs: dict[str, object] = {"timeout": httpx.Timeout(15.0, connect=8.0)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    else:
        client_kwargs["trust_env"] = False

    try:
        async with httpx.AsyncClient(**client_kwargs) as cli:
            for endpoint in provider_models_endpoints(
                base_url,
                payload.api_format,
                protocol_profile=payload.protocol_profile,
            ):
                resp = await cli.get(endpoint, headers=headers)
                if resp.status_code not in {404, 405}:
                    break
    except httpx.HTTPError as exc:
        raise _llm_err(
            "FETCH_NETWORK",
            f"拉取失败：{type(exc).__name__}: {str(exc) or '(无详情；常见 SSL/DNS/代理问题)'}",
            502,
        ) from None

    if resp.status_code >= 400:
        body = resp.text[:300]
        if api_key:
            body = body.replace(api_key, "<redacted>")
        raise _llm_err(
            "FETCH_HTTP",
            f"接口返回 {resp.status_code}: {body}",
            502,
        )

    try:
        data = resp.json()
    except Exception:
        raise _llm_err("FETCH_BAD_JSON", "响应不是合法 JSON") from None

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise _llm_err(
            "FETCH_BAD_SHAPE",
            f"响应缺 'data' 数组（实际顶层 keys: {list(data.keys())[:5] if isinstance(data, dict) else type(data).__name__}）",
        )
    new_ids: list[str] = []
    seen_ids: set[str] = set()
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            mid = it["id"].strip()
            if mid and len(mid) <= 128 and mid not in seen_ids:
                seen_ids.add(mid)
                new_ids.append(mid)
            if len(new_ids) >= 200:
                break

    await audit.write(
        db,
        user.id,
        "llm_provider.fetch_models_preview",
        target=f"llm_provider:{payload.pid or 'new'}",
        detail={"fetched": len(new_ids), "provider": payload.provider},
    )
    await db.commit()
    return FetchModelsPreviewResponse(fetched=len(new_ids), ids=new_ids)


@router.post("/api/commands/llm-providers/quick-verify/stream")
async def stream_quick_verify_provider(
    payload: QuickVerifyProviderRequest,
    db: DBSession,
    _user: CurrentUser,
) -> StreamingResponse:
    """用临时凭据发现模型并发起真实对话，凭据不落库、不写审计。"""

    await _require_ai_enabled(db)

    from ..services import llm_quick_verify

    if len(payload.base_url) > 255:
        raise _llm_err("QUICK_VERIFY_BASE_URL", "Base URL 不能超过 255 字符", 422)
    if len(payload.api_key or "") > 512:
        raise _llm_err("QUICK_VERIFY_API_KEY", "API Key 不能超过 512 字符", 422)
    try:
        base_url = llm_quick_verify.normalize_quick_verify_base_url(payload.base_url)
    except ValueError as exc:
        raise _llm_err("QUICK_VERIFY_BASE_URL", str(exc), 422) from None
    proxy_url = await _resolve_proxy_url(db, payload.proxy_id)
    try:
        request_headers_for_scope(payload.request_headers, REQUEST_SCOPE_LIVENESS)
    except RequestHeaderConfigError as exc:
        raise _llm_err("QUICK_VERIFY_REQUEST_HEADERS", str(exc), 422) from None

    async def event_source():
        async with llm_liveness.diagnostic_slot():
            async for event in llm_quick_verify.quick_verify_events(
                base_url=base_url,
                api_key=payload.api_key or "",
                api_format=payload.api_format,
                protocol_profile=payload.protocol_profile,
                client_identity_profile=payload.client_identity_profile,
                proxy_url=proxy_url,
                model=payload.model,
                reasoning_effort=payload.reasoning_effort,
                system_prompt=payload.system_prompt,
                message=payload.message,
                max_tokens=payload.max_tokens,
                timeout_seconds=payload.timeout_seconds,
                request_headers=payload.request_headers,
            ):
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        event_source(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/api/commands/llm-providers/detect-protocols",
    response_model=DetectProviderProtocolsResponse,
)
async def detect_provider_protocols(
    payload: DetectProviderProtocolsRequest, db: DBSession, user: CurrentUser
) -> DetectProviderProtocolsResponse:
    """用编辑表单当前值轻量探测 provider 支持的 API 协议。

    探测结果不落库；用于新建/编辑 provider 时帮用户选择 api_format。
    """
    await _require_ai_enabled(db)

    from ..crypto import decrypt_str

    api_key = (payload.api_key or "").strip()
    stored_request_headers_token: str | None = None
    if payload.pid is not None:
        try:
            row = await command_service.get_provider_row(db, payload.pid)
            if not api_key and row.api_key_enc:
                api_key = decrypt_str(row.api_key_enc) or ""
            stored_request_headers_token = getattr(row, "request_headers_enc", None)
        except Exception:  # noqa: BLE001
            api_key = ""

    provider = payload.provider
    if provider == "anthropic":
        base_url = normalize_base_url(payload.base_url or "https://api.anthropic.com/v1")
        model = (payload.model or "claude-haiku-4-5").strip()
    elif provider == "ollama":
        base_url = normalize_base_url(payload.base_url or "http://localhost:11434/v1")
        model = (payload.model or "llama3:8b").strip()
    else:
        base_url = normalize_base_url(payload.base_url or "https://api.openai.com/v1")
        model = (payload.model or "gpt-4o-mini").strip()
    proxy_url = await _resolve_proxy_url(db, payload.proxy_id)
    try:
        header_source = (
            payload.request_headers
            if payload.request_headers is not None
            else stored_request_headers_token
        )
        liveness_headers = request_headers_for_scope(
            header_source,
            REQUEST_SCOPE_LIVENESS,
            existing_token=stored_request_headers_token,
        )
        model_headers = request_headers_for_scope(
            header_source,
            REQUEST_SCOPE_MODELS,
            existing_token=stored_request_headers_token,
        )
    except RequestHeaderConfigError as exc:
        raise _llm_err("PROTOCOL_REQUEST_HEADERS_INVALID", str(exc), 422) from None

    client_kwargs: dict[str, object] = {"timeout": httpx.Timeout(12.0, connect=6.0)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    else:
        client_kwargs["trust_env"] = False

    # 阶段 B：使用自然提示词（而非字面量 ping）与足够输出上限（64 tokens）。
    probe_system = (payload.system_prompt or "").strip() or "You are a helpful assistant. Answer briefly."
    probe_message = (payload.message or "").strip() or "用一句话简单介绍你自己。"
    probe_max_tokens = 64
    usage_provider = SimpleNamespace(id=payload.pid, name=f"detect:{provider}", default_model=model)

    async def _record_protocol_probe(
        result: ProtocolProbeResult,
        started: float,
        api_format: str,
    ) -> None:
        """把每次真实探测请求记入诊断 usage，失败也要可审计。"""
        try:
            await _emit_llm_diagnostic_usage(
                provider_row=usage_provider,
                source="diagnostic:protocol_detection",
                started=started,
                system=probe_system,
                user_prompt=probe_message,
                model=model,
                result=None if not result.ok else SimpleNamespace(text=None),
                error=None if result.ok else ValueError(result.error or result.error_category or api_format),
                api_format_override=api_format if api_format != "models" else "chat_completions",
                identity_override=result.client_identity_profile or "minimal",
            )
        except Exception:  # noqa: BLE001 - 诊断记录不得改变探测结论
            pass

    def _probe_body(api_format: str) -> dict:
        if api_format == "responses":
            return {
                "model": model,
                "instructions": probe_system,
                "input": [{"role": "user", "content": probe_message}],
                "max_output_tokens": probe_max_tokens,
            }
        if api_format == "anthropic_messages":
            return {
                "model": model,
                "max_tokens": probe_max_tokens,
                "system": probe_system,
                "messages": [{"role": "user", "content": probe_message}],
            }
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": probe_system},
                {"role": "user", "content": probe_message},
            ],
            "max_tokens": probe_max_tokens,
        }

    def _identity_headers(api_format: str, identity_profile: str) -> dict[str, str]:
        """构造某协议 + 身份的探测请求头（含 UA / 身份头 + 鉴权 + 协议必需头）。"""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        identity = resolve_identity(identity_profile, api_format)
        headers.update(identity.headers())
        if api_format == "anthropic_messages":
            headers["anthropic-version"] = "2023-06-01"
            if api_key:
                headers["x-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return plan_request_headers(
            system_headers=headers,
            compatibility_headers=liveness_headers,
        )

    async def probe_models(cli: httpx.AsyncClient) -> ProtocolProbeResult:
        headers = {"Accept": "application/json"}
        if provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if api_key:
                headers["x-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers = plan_request_headers(
            system_headers=headers,
            compatibility_headers=model_headers,
        )
        started = _time.monotonic()
        try:
            for endpoint in provider_models_endpoints(
                base_url,
                "chat_completions",
            ):
                resp = await cli.get(endpoint, headers=headers)
                if resp.status_code not in {404, 405}:
                    break
            latency_ms = int((_time.monotonic() - started) * 1000)
            result = _probe_result(resp, latency_ms, api_key=api_key, base_url=base_url, stage="credentials")
            await _record_protocol_probe(result, started, "models")
            return result
        except httpx.HTTPError as exc:
            result = _probe_error(exc, started, api_key=api_key, base_url=base_url)
            await _record_protocol_probe(result, started, "models")
            return result

    async def probe_with_identity(
        cli: httpx.AsyncClient,
        api_format: str,
        identity_profile: str,
    ) -> ProtocolProbeResult:
        headers = _identity_headers(api_format, identity_profile)
        body = _probe_body(api_format)
        started = _time.monotonic()
        try:
            resp = await cli.post(
                provider_endpoint(base_url, api_format),
                headers=headers,
                json=body,
            )
            latency_ms = int((_time.monotonic() - started) * 1000)
            result = _probe_result(
                resp,
                latency_ms,
                api_key=api_key,
                base_url=base_url,
                stage="protocol",
                api_format=api_format,
            )
            result.client_identity_profile = identity_profile
            if (
                api_format == "responses"
                and _probe_unsupported_parameter(resp, "max_output_tokens")
            ):
                result.error = "该 Responses 接口拒绝 max_output_tokens；为避免失去输出与成本上限，运行时不会自动省略该参数。"
            await _record_protocol_probe(result, started, api_format)
            return result
        except httpx.HTTPError as exc:
            result = _probe_error(exc, started, api_key=api_key, base_url=base_url)
            result.client_identity_profile = identity_profile
            await _record_protocol_probe(result, started, api_format)
            return result

    async def probe_protocol(
        cli: httpx.AsyncClient, api_format: str
    ) -> tuple[ProtocolProbeResult, list[ProtocolIdentityAttempt]]:
        """按身份顺序探测某协议；标准身份成功即停止，不再尝试其它身份。"""
        attempts: list[ProtocolIdentityAttempt] = []
        best: ProtocolProbeResult | None = None
        for identity_profile in IDENTITY_PROBE_ORDER.get(api_format, ("minimal",)):
            result = await probe_with_identity(cli, api_format, identity_profile)
            attempts.append(
                ProtocolIdentityAttempt(
                    api_format=api_format,
                    client_identity_profile=identity_profile,
                    ok=result.ok,
                    status_code=result.status_code,
                    latency_ms=result.latency_ms,
                    error_category=result.error_category,
                    error=result.error,
                    suggestion=result.suggestion,
                )
            )
            if best is None or result.ok:
                best = result
            if result.ok:
                break
            # 只有明确的 client_rejected 才继续尝试下一个身份；
            # 401/429/超时/5xx 不换身份（换身份无意义且可能重复计费/触发限流）。
            if result.error_category != llm_diagnostics.DIAG_CLIENT_REJECTED:
                break
        return (best or ProtocolProbeResult(ok=False, latency_ms=0), attempts)

    identity_attempts: list[ProtocolIdentityAttempt] = []
    async with httpx.AsyncClient(**client_kwargs) as cli:
        models = await probe_models(cli)
        chat, chat_attempts = await probe_protocol(cli, "chat_completions")
        responses, responses_attempts = await probe_protocol(cli, "responses")
        anthropic, anthropic_attempts = await probe_protocol(cli, "anthropic_messages")
    identity_attempts = chat_attempts + responses_attempts + anthropic_attempts

    recommended_api_format: str | None = None
    recommended_web_search_api_format = "auto"
    note: str | None = None
    if provider == "anthropic":
        if anthropic.ok:
            recommended_api_format = "anthropic_messages"
        else:
            note = "Anthropic provider 需要 /messages 可用。"
    else:
        if chat.ok:
            # DeepSeek 官方文档当前仅为 deepseek-v4-flash 开放 Responses；
            # 该模型即使同时保留 Chat 兼容入口，也应优先落到原生协议。
            if responses.ok and _is_deepseek_v4_responses_target(base_url, model):
                recommended_api_format = "responses"
                recommended_web_search_api_format = "responses"
            else:
                recommended_api_format = "chat_completions"
                recommended_web_search_api_format = "auto" if responses.ok else "chat_completions"
        elif responses.ok:
            recommended_api_format = "responses"
            recommended_web_search_api_format = "responses"
        response_compat_note = responses.error if responses.ok and responses.error else ""
        if (
            chat.ok
            and responses.ok
            and _is_deepseek_v4_responses_target(base_url, model)
        ):
            note = (
                "检测到 DeepSeek 官方 deepseek-v4-flash；已优先选择原生 Responses API。"
                "该协议当前不支持 conversation、previous_response_id、图片输入等 OpenAI 扩展参数。"
            )
        elif chat.ok and responses.ok:
            note = (
                "该 API 同时支持 chat/completions 与 responses；建议日常 chat，联网搜索自动切 responses。"
                if not response_compat_note
                else f"该 API 同时支持 chat/completions 与 responses 兼容模式；建议日常 chat，联网搜索自动切 responses。{response_compat_note}"
            )
        elif responses.ok:
            note = (
                "该 API 支持 responses；可直接作为默认协议，也可用于联网搜索。"
                if not response_compat_note
                else f"该 API 支持 responses 兼容模式；可直接作为默认协议，也可用于联网搜索。{response_compat_note}"
            )
        elif chat.ok:
            note = "该 API 支持 chat/completions，但未探测到 responses；联网搜索可能不可用。"
        else:
            note = "未探测到可用聊天协议；请检查 Base URL、API Key、模型 ID 或代理。"

    # 阶段 B：推荐身份 = 推荐协议下探测成功所用的身份；无则按协议 auto 默认。
    recommended_client_identity_profile: str | None = None
    recommended_protocol_profile: str | None = None
    if recommended_api_format:
        result_by_format = {
            "chat_completions": chat,
            "responses": responses,
            "anthropic_messages": anthropic,
        }
        chosen = result_by_format.get(recommended_api_format)
        if chosen is not None and chosen.ok and chosen.client_identity_profile:
            recommended_client_identity_profile = chosen.client_identity_profile
        else:
            recommended_client_identity_profile = default_identity_for_format(
                recommended_api_format
            )
        recommended_protocol_profile = infer_protocol_profile(
            recommended_api_format,
            base_url=base_url,
            model=model,
        )
        if (
            recommended_api_format == "responses"
            and recommended_client_identity_profile in {"codex_tui", "codex_desktop"}
            and recommended_protocol_profile == "standard"
        ):
            recommended_protocol_profile = "codex_responses"

    await audit.write(
        db,
        user.id,
        "llm_provider.detect_protocols",
        target=f"llm_provider:{payload.pid or 'new'}",
        detail={
            "provider": provider,
            "chat": chat.ok,
            "responses": responses.ok,
            "anthropic": anthropic.ok,
            "models": models.ok,
            "recommended_identity": recommended_client_identity_profile,
            "recommended_protocol_profile": recommended_protocol_profile,
        },
    )
    await db.commit()

    return DetectProviderProtocolsResponse(
        chat_completions=chat,
        responses=responses,
        anthropic_messages=anthropic,
        models=models,
        recommended_api_format=recommended_api_format,
        recommended_protocol_profile=recommended_protocol_profile,
        recommended_client_identity_profile=recommended_client_identity_profile,
        identity_attempts=identity_attempts,
        recommended_web_search_api_format=recommended_web_search_api_format,
        note=note,
    )


def _probe_response_is_valid(api_format: str | None, data: Any) -> bool:
    """校验 2xx 响应体是否为该协议的最小有效结构（阶段 F 收口 #3）。

    只有真正长得像该协议生成结果的响应才算协议可用——正常文本、拒答（refusal）、
    工具调用（tool call）都接受；空响应、HTML 登录页、无关 JSON（如 ``{"ok":true}``、
    模型列表）一律判为无效，避免把任意 2xx 误判为协议可用。
    """
    if not isinstance(data, dict):
        return False
    fmt = (api_format or "").strip().lower()
    if fmt == "chat_completions":
        # {choices: [{message|delta|text|finish_reason ...}], ...}
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        first = choices[0]
        if not isinstance(first, dict):
            return False
        return any(k in first for k in ("message", "delta", "text", "finish_reason"))
    if fmt == "responses":
        # Responses：{output: [...]} / {output_text: ...} / {status: ...} / {response: ...}
        if any(k in data for k in ("output", "output_text", "response")):
            return True
        # 有些实现直接回 {id, object: "response", status: ...}
        return str(data.get("object") or "").startswith("response") or "status" in data
    if fmt == "anthropic_messages":
        # Anthropic：{type: "message", content: [...], role: "assistant", ...}
        if data.get("type") == "message" and isinstance(data.get("content"), list):
            return True
        # 兼容部分反代：{content: [...]} 或 {role: "assistant", content: ...}
        return isinstance(data.get("content"), list) or data.get("role") == "assistant"
    # 未知协议：保守起见，只要是 JSON 对象即接受（不额外收紧既有行为）。
    return True


def _probe_result(
    resp: httpx.Response,
    latency_ms: int,
    *,
    api_key: str,
    base_url: str | None = None,
    stage: str | None = None,
    api_format: str | None = None,
) -> ProtocolProbeResult:
    from ..services import llm_diagnostics as diag

    if resp.status_code < 400:
        # 阶段 F 收口 #3：2xx 不再无条件判成功。空响应 / 非 JSON / 结构不符该协议
        # （HTML 登录页、无关 JSON）一律 ok=False，只有最小有效结构才算协议可用。
        text = resp.text or ""
        if not text.strip():
            return ProtocolProbeResult(
                ok=False,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                stage=stage,
                error_category=diag.DIAG_EMPTY_RESPONSE,
                suggestion=diag.suggestion_for(diag.DIAG_EMPTY_RESPONSE),
                error="上游返回 2xx 但响应体为空。",
            )
        if not diag.is_valid_json(text):
            return ProtocolProbeResult(
                ok=False,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                stage=stage,
                error_category=diag.DIAG_PROTOCOL_REJECTED,
                suggestion=diag.suggestion_for(diag.DIAG_PROTOCOL_REJECTED),
                error="上游返回 2xx 但响应体非 JSON（可能是 HTML 登录页或错误网关）。",
            )
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        if not _probe_response_is_valid(api_format, data):
            return ProtocolProbeResult(
                ok=False,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                stage=stage,
                error_category=diag.DIAG_PROTOCOL_REJECTED,
                suggestion=diag.suggestion_for(diag.DIAG_PROTOCOL_REJECTED),
                error="上游返回 2xx 但响应结构不符合该协议（非有效的生成结果）。",
            )
        return ProtocolProbeResult(
            ok=True,
            status_code=resp.status_code,
            latency_ms=latency_ms,
            stage=stage,
        )
    body = diag.redact(resp.text, api_key=api_key or None, base_url=base_url)
    category = diag.classify_status_code(resp.status_code, resp.text or "")
    return ProtocolProbeResult(
        ok=False,
        status_code=resp.status_code,
        latency_ms=latency_ms,
        stage=stage,
        error_category=category,
        suggestion=diag.suggestion_for(category),
        error=f"HTTP {resp.status_code}: {body}",
    )


def _probe_unsupported_parameter(resp: httpx.Response, parameter: str) -> bool:
    if resp.status_code < 400:
        return False
    lowered = (resp.text or "").lower()
    parameter = parameter.lower()
    return (
        parameter in lowered
        and (
            "unsupported parameter" in lowered
            or "unknown parameter" in lowered
            or "unrecognized parameter" in lowered
            or "invalid parameter" in lowered
        )
    )


def _probe_error(
    exc: httpx.HTTPError,
    started: float,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ProtocolProbeResult:
    category = llm_diagnostics.classify_exception(exc)
    # 阶段 F 收口：网络异常文本也可能带 base_url / 代理凭据，统一脱敏。
    raw = f"{type(exc).__name__}: {str(exc) or '(无详情；常见 SSL/DNS/代理问题)'}"
    return ProtocolProbeResult(
        ok=False,
        status_code=None,
        latency_ms=int((_time.monotonic() - started) * 1000),
        error=llm_diagnostics.redact(raw, api_key=api_key or None, base_url=base_url),
        stage="network",
        error_category=category,
        suggestion=llm_diagnostics.suggestion_for(category),
    )


@router.post(
    "/api/commands/llm-providers/{pid}/fetch-models",
    response_model=FetchModelsResponse,
)
async def fetch_models(
    pid: int, db: DBSession, user: CurrentUser
) -> FetchModelsResponse:
    """从 ``GET {base_url}/models`` 拉模型列表，合并到 provider.models。

    URL 选择基于 ``api_format``：
    - ``chat_completions`` / ``responses`` → ``GET {base_url}/models``（OpenAI 兼容；
      Responses API 与 chat/completions 共用同一 ``/models`` 端点）
    - ``anthropic_messages`` → 没有 list models 接口；返 422 让用户手填

    合并策略：保留已有 enabled 状态 + 用户自定义条目；fetch 来的新条目默认 enabled=False，
    用户自己决定要启用哪些。
    """
    await _require_ai_enabled(db)

    from ..crypto import decrypt_str
    from ..db.models.command import (
        LLM_API_FORMAT_ANTHROPIC_MESSAGES,
        default_api_format_for,
    )

    row = await command_service.get_provider_row(db, pid)

    fmt = (
        getattr(row, "api_format", None)
        or default_api_format_for(row.provider)
    )
    if fmt == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        raise _llm_err(
            "FETCH_NOT_SUPPORTED",
            "Anthropic Messages 协议没有列出模型接口；请去 docs.anthropic.com 查模型 ID 后手动添加",
            422,
        )

    base_url = normalize_base_url(row.base_url or "https://api.openai.com/v1")
    api_key = decrypt_str(row.api_key_enc) if row.api_key_enc else ""
    proxy_url = await _resolve_proxy_url(db, row.proxy_id)

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        headers = plan_request_headers(
            system_headers=headers,
            compatibility_headers=request_headers_for_scope(
                getattr(row, "request_headers_enc", None),
                REQUEST_SCOPE_MODELS,
            ),
        )
    except RequestHeaderConfigError as exc:
        raise _llm_err("FETCH_REQUEST_HEADERS_INVALID", str(exc), 422) from None

    client_kwargs: dict[str, object] = {"timeout": httpx.Timeout(15.0, connect=8.0)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as cli:
            for endpoint in provider_models_endpoints(
                base_url,
                fmt,
                protocol_profile=getattr(row, "protocol_profile", None),
            ):
                resp = await cli.get(endpoint, headers=headers)
                if resp.status_code not in {404, 405}:
                    break
    except httpx.HTTPError as exc:
        raise _llm_err(
            "FETCH_NETWORK",
            f"拉取失败：{type(exc).__name__}: {str(exc) or '(无详情；常见 SSL/DNS/代理问题)'}",
            502,
        ) from None

    if resp.status_code >= 400:
        # 把 api_key 从 body 里剥掉再返
        body = resp.text[:300]
        if api_key:
            body = body.replace(api_key, "<redacted>")
        raise _llm_err(
            "FETCH_HTTP",
            f"接口返回 {resp.status_code}: {body}",
            502,
        )

    try:
        data = resp.json()
    except Exception:
        raise _llm_err("FETCH_BAD_JSON", "响应不是合法 JSON") from None

    # OpenAI 兼容：{data: [{id, object: "model", ...}, ...]}
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise _llm_err(
            "FETCH_BAD_SHAPE",
            f"响应缺 'data' 数组（实际顶层 keys: {list(data.keys())[:5] if isinstance(data, dict) else type(data).__name__}）",
        )
    new_ids: list[str] = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            mid = it["id"].strip()
            if mid:
                new_ids.append(mid)

    # 合并：保留已 enabled 状态 + custom 条目
    existing: dict[str, dict] = {
        m["id"]: m for m in (row.models or []) if isinstance(m, dict) and "id" in m
    }
    merged: list[dict] = []
    for mid in new_ids:
        if mid in existing:
            # 老条目：保留 enabled / label，custom 改成 false（毕竟现在 fetch 拿到了）
            old = existing[mid]
            merged.append(
                {
                    **old,
                    "id": mid,
                    "enabled": bool(old.get("enabled", False)),
                    "custom": False,
                    "label": old.get("label"),
                }
            )
        else:
            merged.append({"id": mid, "enabled": False, "custom": False, "label": None})

    # 用户的自定义条目（fetch 没拿到 ID 的）保留
    fetched_ids = set(new_ids)
    for mid, old in existing.items():
        if mid not in fetched_ids and old.get("custom"):
            merged.append(
                {
                    **old,
                    "id": mid,
                    "enabled": bool(old.get("enabled", False)),
                    "custom": True,
                    "label": old.get("label"),
                }
            )

    row.models = merged
    await audit.write(
        db,
        user.id,
        "llm_provider.fetch_models",
        target=f"llm_provider:{pid}",
        detail={"fetched": len(new_ids), "total": len(merged)},
    )
    await db.commit()
    await db.refresh(row)
    # 通知 worker reload；让下游能看到新模型清单
    aids = await command_service.list_all_account_ids(db)
    await command_service.notify_reload(aids)

    return FetchModelsResponse(
        fetched=len(new_ids),
        provider=command_service._provider_to_out(row),
    )


@router.post(
    "/api/commands/llm-providers/{pid}/test-model",
    response_model=TestModelResponse,
)
async def test_model(
    pid: int, payload: TestModelRequest, db: DBSession, user: CurrentUser
) -> TestModelResponse:
    """用一次 max_tokens=4 的最小调用测某个 model 通不通 + 测延时。

    用 ``services.llm_client.build_client``（与正式 ai 命令同路径），
    一并验证 api_key / base_url / proxy_url 都对。
    """
    await _require_ai_enabled(db)

    from ..services.llm_client import LLMError, build_client

    row = await command_service.get_provider_row(db, pid)
    proxy_url = await _resolve_proxy_url(db, row.proxy_id)

    started = _time.monotonic()
    try:
        cli = build_client(
            row,
            override_model=payload.model.strip(),
            proxy_url=proxy_url,
            request_scope=REQUEST_SCOPE_LIVENESS,
        )
        result = await cli.complete("ping", "ping", max_tokens=4, timeout_seconds=90)
    except LLMError as e:
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        await _emit_llm_diagnostic_usage(
            provider_row=row,
            source="diagnostic:test-model",
            started=started,
            system="ping",
            user_prompt="ping",
            model=payload.model.strip(),
            error=e,
        )
        # LLMError 已脱敏
        return TestModelResponse(ok=False, latency_ms=elapsed_ms, error=str(e))
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        await _emit_llm_diagnostic_usage(
            provider_row=row,
            source="diagnostic:test-model",
            started=started,
            system="ping",
            user_prompt="ping",
            model=payload.model.strip(),
            error=e,
        )
        return TestModelResponse(
            ok=False,
            latency_ms=elapsed_ms,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )

    elapsed_ms = int((_time.monotonic() - started) * 1000)
    await _emit_llm_diagnostic_usage(
        provider_row=row,
        source="diagnostic:test-model",
        started=started,
        system="ping",
        user_prompt="ping",
        model=result.model or payload.model.strip(),
        result=result,
    )
    # 不写 audit（测试调用频繁，写多了刷屏）
    return TestModelResponse(
        ok=True,
        latency_ms=elapsed_ms,
        model=result.model,
        preview=(result.text or "").strip()[:80] or None,
    )


def _build_chat_test_prompt(
    payload: ChatTestModelsRequest,
    model_id: str | None = None,
) -> str:
    lines: list[str] = []
    history = payload.history_by_model.get(model_id, payload.history) if model_id else payload.history
    if history:
        lines.append("以下是同一个测试窗口里的最近对话，请只当作上下文：")
        for turn in history[-12:]:
            name = "用户" if turn.role == "user" else "助手"
            lines.append(f"{name}: {turn.content}")
        lines.append("")
    lines.append("用户刚刚说：")
    lines.append(payload.message)
    return "\n".join(lines).strip()


@router.post(
    "/api/commands/llm-providers/{pid}/chat-test-models",
    response_model=ChatTestModelsResponse,
)
async def chat_test_models(
    pid: int,
    payload: ChatTestModelsRequest,
    db: DBSession,
    _user: CurrentUser,
) -> ChatTestModelsResponse:
    """按真实聊天路径并发测试一个 Provider 下的多个模型。

    与 ``test-model`` 的 ping 轻量探测不同，这里使用用户自定义测试语、
    可选历史上下文、完整 max_tokens 和 provider 当前 API 协议，便于模拟
    Telegram / 插件里的真实 LLM 调用表现。
    """
    await _require_ai_enabled(db)

    from ..services.llm_client import LLMError, build_client

    row = await command_service.get_provider_row(db, pid)
    proxy_url = await _resolve_proxy_url(db, row.proxy_id)
    transport_metadata = _liveness_transport_metadata(
        row,
        api_format_override=payload.api_format_override,
        identity_override=payload.client_identity_profile_override,
    )

    async def run_one_unbounded(model_id: str) -> ChatTestModelResult:
        started = _time.monotonic()
        user_prompt = _build_chat_test_prompt(payload, model_id)
        try:
            cli = build_client(
                row,
                override_model=model_id,
                proxy_url=proxy_url,
                api_format_override=payload.api_format_override,
                identity_override=payload.client_identity_profile_override,
                request_scope=REQUEST_SCOPE_LIVENESS,
            )
            result = await cli.complete(
                payload.system_prompt,
                user_prompt,
                max_tokens=payload.max_tokens,
                timeout_seconds=payload.timeout_seconds,
            )
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            await _emit_llm_diagnostic_usage(
                provider_row=row,
                source="diagnostic:chat-test",
                api_format_override=payload.api_format_override,
                identity_override=payload.client_identity_profile_override,
                started=started,
                system=payload.system_prompt,
                user_prompt=user_prompt,
                model=result.model or model_id,
                result=result,
            )
            text = (result.text or "").strip()
            return ChatTestModelResult(
                ok=bool(text),
                requested_model=model_id,
                model=result.model,
                latency_ms=elapsed_ms,
                response=text or None,
                preview=text[:240] if text else None,
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                empty_response=not bool(text),
                error=None if text else "上游请求已完成，但返回文本为空。",
                **transport_metadata,
            )
        except asyncio.CancelledError:
            cancelled = LLMError("模型对话测活已取消")
            try:
                await asyncio.shield(
                    _emit_llm_diagnostic_usage(
                        provider_row=row,
                        source="diagnostic:chat-test",
                        api_format_override=payload.api_format_override,
                        identity_override=payload.client_identity_profile_override,
                        started=started,
                        system=payload.system_prompt,
                        user_prompt=user_prompt,
                        model=model_id,
                        error=cancelled,
                    )
                )
            except Exception:  # noqa: BLE001 - 取消路径不能被记账失败阻塞
                pass
            raise
        except LLMError as exc:
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            await _emit_llm_diagnostic_usage(
                provider_row=row,
                source="diagnostic:chat-test",
                api_format_override=payload.api_format_override,
                identity_override=payload.client_identity_profile_override,
                started=started,
                system=payload.system_prompt,
                user_prompt=user_prompt,
                model=model_id,
                error=exc,
            )
            return ChatTestModelResult(
                ok=False,
                requested_model=model_id,
                latency_ms=elapsed_ms,
                error=str(exc),
                status_code=exc.status_code,
                **transport_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            await _emit_llm_diagnostic_usage(
                provider_row=row,
                source="diagnostic:chat-test",
                api_format_override=payload.api_format_override,
                identity_override=payload.client_identity_profile_override,
                started=started,
                system=payload.system_prompt,
                user_prompt=user_prompt,
                model=model_id,
                error=exc,
            )
            return ChatTestModelResult(
                ok=False,
                requested_model=model_id,
                latency_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
                **transport_metadata,
            )

    async def run_one(model_id: str) -> ChatTestModelResult:
        async with llm_liveness.diagnostic_slot():
            return await run_one_unbounded(model_id)

    results = await asyncio.gather(*(run_one(model_id) for model_id in payload.models))
    return ChatTestModelsResponse(
        provider_id=pid,
        provider_name=row.name,
        results=list(results),
    )


def _chat_stream_can_fallback(exc: Exception) -> bool:
    """只在流式能力不兼容时改走完整响应，避免掩盖真实上游故障。"""

    from ..services.llm_client import LLMError, LLMErrorScope

    if isinstance(exc, NotImplementedError):
        return True
    if not isinstance(exc, LLMError):
        return False
    if exc.scope is LLMErrorScope.CAPABILITY_MISMATCH:
        return True
    if exc.status_code in {405, 406, 415, 501}:
        return True
    message = str(exc).lower()
    return exc.status_code in {400, 422} and "stream" in message and any(
        marker in message
        for marker in ("不支持", "unsupported", "not support", "unknown parameter")
    )


@router.post("/api/commands/llm-providers/{pid}/chat-test-models/stream")
async def stream_chat_test_models(
    pid: int,
    payload: ChatTestModelsRequest,
    db: DBSession,
    _user: CurrentUser,
) -> StreamingResponse:
    """优先使用上游原生流式协议，以 NDJSON 并发推送各模型结果。"""

    await _require_ai_enabled(db)

    from ..services.llm_client import LLMError, LLMResult, build_client

    row = await command_service.get_provider_row(db, pid)
    proxy_url = await _resolve_proxy_url(db, row.proxy_id)
    transport_metadata = _liveness_transport_metadata(
        row,
        api_format_override=payload.api_format_override,
        identity_override=payload.client_identity_profile_override,
    )

    async def event_source():
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def run_one_unbounded(model_id: str) -> None:
            started = _time.monotonic()
            user_prompt = _build_chat_test_prompt(payload, model_id)
            text_parts: list[str] = []
            response_chars = 0
            max_response_chars = max(16_384, payload.max_tokens * 16)
            actual_model: str | None = None
            input_tokens = 0
            output_tokens = 0
            streaming = True
            stream_fallback = False
            fallback_response_consumed = False
            stream_terminal_received = False
            await emit(
                {
                    "type": "start",
                    "requested_model": model_id,
                    "streaming": True,
                    **transport_metadata,
                }
            )
            try:
                cli = build_client(
                    row,
                    override_model=model_id,
                    proxy_url=proxy_url,
                    api_format_override=payload.api_format_override,
                    identity_override=payload.client_identity_profile_override,
                    request_scope=REQUEST_SCOPE_LIVENESS,
                )
                try:
                    async with asyncio.timeout(payload.timeout_seconds):
                        try:
                            async for chunk in cli.stream_complete(
                                payload.system_prompt,
                                user_prompt,
                                max_tokens=payload.max_tokens,
                                timeout_seconds=payload.timeout_seconds,
                            ):
                                if chunk.model:
                                    actual_model = chunk.model
                                if chunk.input_tokens is not None:
                                    input_tokens = int(chunk.input_tokens)
                                if chunk.output_tokens is not None:
                                    output_tokens = int(chunk.output_tokens)
                                if chunk.done:
                                    stream_terminal_received = True
                                if getattr(chunk, "stream_fallback", False):
                                    streaming = False
                                    stream_fallback = True
                                    fallback_response_consumed = True
                                if chunk.delta:
                                    response_chars += len(chunk.delta)
                                    if response_chars > max_response_chars:
                                        raise LLMError("模型流式输出超过测活内容上限。")
                                    text_parts.append(chunk.delta)
                                    await emit(
                                        {
                                            "type": "delta",
                                            "requested_model": model_id,
                                            "delta": chunk.delta,
                                            "model": actual_model,
                                            "stream_fallback": bool(
                                                getattr(chunk, "stream_fallback", False)
                                            ),
                                        }
                                    )
                        except (LLMError, NotImplementedError) as exc:
                            if any(part.strip() for part in text_parts) or not _chat_stream_can_fallback(exc):
                                raise
                            stream_fallback = True

                        if stream_fallback and not fallback_response_consumed:
                            streaming = False
                            elapsed_seconds = _time.monotonic() - started
                            remaining_seconds = max(
                                1,
                                payload.timeout_seconds - int(elapsed_seconds),
                            )
                            completed = await cli.complete(
                                payload.system_prompt,
                                user_prompt,
                                max_tokens=payload.max_tokens,
                                timeout_seconds=remaining_seconds,
                            )
                            if len(completed.text or "") > max_response_chars:
                                raise LLMError("模型完整响应超过测活内容上限。")
                            text_parts = [completed.text or ""]
                            actual_model = completed.model or actual_model
                            input_tokens = int(completed.input_tokens or 0)
                            output_tokens = int(completed.output_tokens or 0)
                        elif not stream_terminal_received:
                            raise LLMError(
                                "模型流式响应提前结束，没有返回最终状态。",
                                retryable=True,
                            )
                except TimeoutError:
                    raise LLMError("模型测活超过总超时时间。", retryable=True) from None

                elapsed_ms = int((_time.monotonic() - started) * 1000)
                text = "".join(text_parts).strip()
                usage_result = LLMResult(
                    text=text,
                    model=actual_model or model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                await _emit_llm_diagnostic_usage(
                    provider_row=row,
                    source="diagnostic:chat-test",
                    api_format_override=payload.api_format_override,
                    identity_override=payload.client_identity_profile_override,
                    started=started,
                    system=payload.system_prompt,
                    user_prompt=user_prompt,
                    model=actual_model or model_id,
                    result=usage_result,
                )
                result = ChatTestModelResult(
                    ok=bool(text),
                    requested_model=model_id,
                    model=actual_model,
                    latency_ms=elapsed_ms,
                    response=text or None,
                    preview=text[:240] if text else None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    empty_response=not bool(text),
                    error=None if text else "上游请求已完成，但返回文本为空。",
                    streaming=streaming,
                    stream_fallback=stream_fallback,
                    **transport_metadata,
                )
                await emit(
                    {
                        "type": "done",
                        "requested_model": model_id,
                        "result": result.model_dump(mode="json"),
                    }
                )
            except asyncio.CancelledError:
                partial = LLMResult(
                    text="".join(text_parts).strip(),
                    model=actual_model or model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                cancelled = LLMError("模型流式测活已取消")
                try:
                    await asyncio.shield(
                        _emit_llm_diagnostic_usage(
                            provider_row=row,
                            source="diagnostic:chat-test",
                            api_format_override=payload.api_format_override,
                            identity_override=payload.client_identity_profile_override,
                            started=started,
                            system=payload.system_prompt,
                            user_prompt=user_prompt,
                            model=actual_model or model_id,
                            result=partial,
                            error=cancelled,
                        )
                    )
                except Exception:  # noqa: BLE001 - 取消路径不能被记账失败阻塞
                    pass
                raise
            except LLMError as exc:
                elapsed_ms = int((_time.monotonic() - started) * 1000)
                await _emit_llm_diagnostic_usage(
                    provider_row=row,
                    source="diagnostic:chat-test",
                    api_format_override=payload.api_format_override,
                    identity_override=payload.client_identity_profile_override,
                    started=started,
                    system=payload.system_prompt,
                    user_prompt=user_prompt,
                    model=actual_model or model_id,
                    error=exc,
                )
                result = ChatTestModelResult(
                    ok=False,
                    requested_model=model_id,
                    model=actual_model,
                    latency_ms=elapsed_ms,
                    response="".join(text_parts).strip() or None,
                    error=str(exc),
                    status_code=exc.status_code,
                    streaming=streaming,
                    stream_fallback=stream_fallback,
                    **transport_metadata,
                )
                await emit(
                    {
                        "type": "error",
                        "requested_model": model_id,
                        "result": result.model_dump(mode="json"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = int((_time.monotonic() - started) * 1000)
                await _emit_llm_diagnostic_usage(
                    provider_row=row,
                    source="diagnostic:chat-test",
                    api_format_override=payload.api_format_override,
                    identity_override=payload.client_identity_profile_override,
                    started=started,
                    system=payload.system_prompt,
                    user_prompt=user_prompt,
                    model=actual_model or model_id,
                    error=exc,
                )
                result = ChatTestModelResult(
                    ok=False,
                    requested_model=model_id,
                    model=actual_model,
                    latency_ms=elapsed_ms,
                    response="".join(text_parts).strip() or None,
                    error=f"上游流式响应解析失败（{type(exc).__name__}）。",
                    streaming=streaming,
                    stream_fallback=stream_fallback,
                    **transport_metadata,
                )
                await emit(
                    {
                        "type": "error",
                        "requested_model": model_id,
                        "result": result.model_dump(mode="json"),
                    }
                )

        async def run_one(model_id: str) -> None:
            async with llm_liveness.diagnostic_slot():
                await run_one_unbounded(model_id)

        tasks = [asyncio.create_task(run_one(model_id)) for model_id in payload.models]
        remaining = len(tasks)
        try:
            while remaining:
                event = await queue.get()
                if event.get("type") in {"done", "error"}:
                    remaining -= 1
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return StreamingResponse(
        event_source(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# ════════════════════════════════════════════════════════════
# 阶段 C：全量已启用模型测活（当前 Provider / 全部 Provider）
# ════════════════════════════════════════════════════════════


async def _load_liveness_provider_rows(
    db: DBSession, only_provider_ids: list[int] | None
) -> list[Any]:
    """加载测活范围内的 Provider 行（None=全部；否则按 id 过滤）。"""
    from sqlalchemy import select

    from ..db.models.command import LLMProvider

    query = select(LLMProvider).order_by(LLMProvider.id.asc())
    if only_provider_ids:
        query = query.where(LLMProvider.id.in_(list(only_provider_ids)))
    return list((await db.execute(query)).scalars().all())


@router.get("/api/commands/llm-providers/runtime-health")
async def list_provider_runtime_health(_user: CurrentUser) -> list[dict[str, Any]]:
    """运行时健康只读视图（进程内存真相源；测活不写入）。

    供测活页只读栏展示 degraded/冷却，不改变任何生产状态。
    """
    from ..services.provider_health import list_health

    return list_health()


@router.post(
    "/api/commands/llm-providers/liveness/preview",
    response_model=FullLivenessPreviewResponse,
)
async def full_liveness_preview(
    payload: FullLivenessPreviewRequest, db: DBSession, _user: CurrentUser
) -> FullLivenessPreviewResponse:
    """全量测活执行预览：只读数据库，不调用上游、不消耗 quota。

    仅统计 ``models[].enabled == True`` 的模型；缺凭据 / 无启用模型的 Provider
    标记为不可执行并给出原因。
    """
    await _require_ai_enabled(db)
    rows = await _load_liveness_provider_rows(db, payload.only_provider_ids)
    preview = llm_liveness.build_preview(
        rows,
        max_tokens=payload.max_tokens,
        global_concurrency=payload.global_concurrency,
        models_by_provider=payload.models_by_provider,
    )
    data = preview.to_dict()
    return FullLivenessPreviewResponse(**data)


def _liveness_result_items(raw: list[dict[str, Any]]) -> list[LivenessResultItem]:
    return [
        LivenessResultItem(
            provider_id=r["provider_id"],
            provider_name=r["provider_name"],
            model_id=r["model_id"],
            status=r["status"],
            latency_ms=int(r.get("latency_ms") or 0),
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            preview=r.get("preview"),
            error=r.get("error"),
            status_code=r.get("status_code"),
            error_category=r.get("error_category"),
            suggestion=r.get("suggestion"),
            client_identity_profile=r.get("client_identity_profile"),
            effective_api_format=r.get("effective_api_format"),
            skipped=bool(r.get("skipped")),
        )
        for r in raw
    ]


def _liveness_transport_metadata(
    row: Any,
    *,
    api_format_override: str | None = None,
    identity_override: str | None = None,
) -> dict[str, str | None]:
    """返回本次测活真正采用的协议与客户端身份。"""
    from ..db.models.command import default_api_format_for

    effective_api_format = (
        api_format_override
        or getattr(row, "api_format", None)
        or default_api_format_for(getattr(row, "provider", "openai"))
    )
    protocol_profile = resolve_protocol_profile(
        effective_api_format,
        getattr(row, "protocol_profile", None),
        base_url=getattr(row, "base_url", None),
        model=getattr(row, "default_model", None),
    )
    effective_identity = llm_identity.resolve_identity(
        identity_override or getattr(row, "client_identity_profile", None),
        effective_api_format,
        recommended_profile=protocol_profile.recommended_identity,
    ).profile
    return {
        "effective_api_format": effective_api_format,
        "client_identity_profile": effective_identity,
    }


def _liveness_snapshot_response(job: llm_liveness.LivenessJob) -> FullLivenessRunResponse:
    snap = job.snapshot()
    return FullLivenessRunResponse(
        run_id=snap["run_id"],
        status=snap["status"],
        task_total=int(snap["task_total"]),
        completed=int(snap["completed"]),
        healthy=int(snap["healthy"]),
        failed=int(snap["failed"]),
        skipped=int(snap["skipped"]),
        cancelled=int(snap["cancelled"]),
        results=_liveness_result_items(snap["results"]),
        error=snap.get("error"),
    )


@router.post(
    "/api/commands/llm-providers/liveness/run",
    response_model=FullLivenessRunStartResponse,
)
async def full_liveness_run(
    payload: FullLivenessRunRequest, db: DBSession, _user: CurrentUser
) -> FullLivenessRunStartResponse:
    """启动全量 / 范围测活异步 Job，立即返回 ``run_id``。

    - 有界公平调度：全局并发 + 单 Provider 并发；Provider 轮询防独占。
    - 429 降对应 Provider 并发；401 停止该 Provider 剩余任务。
    - 手工测活不改生产 cooldown、不自动禁用模型；仅返回脱敏诊断结果。
    - 前端凭 ``run_id`` 轮询 ``GET …/liveness/{run_id}`` 获取逐项进度；
      ``POST …/liveness/{run_id}/cancel`` 真实取消（停新任务 + 中断在途请求）。
    """
    await _require_ai_enabled(db)

    from ..services.llm_client import LLMError, build_client

    rows = await _load_liveness_provider_rows(db, payload.only_provider_ids)
    # 后台任务不能复用请求级 ORM 会话；把 runner 所需字段快照到内存。
    row_snapshots: dict[int, Any] = {int(r.id): r for r in rows}
    proxy_by_id: dict[int, str | None] = {}
    for r in rows:
        proxy_by_id[int(r.id)] = await _resolve_proxy_url(db, getattr(r, "proxy_id", None))

    preview = llm_liveness.build_preview(
        rows,
        max_tokens=payload.max_tokens,
        global_concurrency=payload.global_concurrency,
        models_by_provider=payload.models_by_provider,
    )
    tasks = llm_liveness.build_task_pool(preview)
    if payload.only_models:
        only = {m.strip() for m in payload.only_models if m.strip()}
        tasks = [t for t in tasks if t.model_id in only]

    if len(tasks) > llm_liveness.MAX_LIVENESS_TASKS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "LIVENESS_TASK_LIMIT",
                "message": (
                    f"单次测活最多允许 {llm_liveness.MAX_LIVENESS_TASKS} 个模型，"
                    "请缩小 Provider 或模型范围"
                ),
            },
        )

    if len(tasks) > llm_liveness.LARGE_RUN_MODEL_THRESHOLD and not payload.confirm_large_run:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LIVENESS_CONFIRMATION_REQUIRED",
                "message": (
                    f"本次将测试 {len(tasks)} 个模型，请先确认真实上游额度消耗"
                ),
                "task_total": len(tasks),
            },
        )

    try:
        job = await llm_liveness.liveness_jobs.create(task_total=len(tasks))
    except llm_liveness.LivenessCapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "LIVENESS_BUSY", "message": str(exc)},
            headers={"Retry-After": "3"},
        ) from exc
    system_prompt = payload.system_prompt
    message = payload.message
    max_tokens = payload.max_tokens
    timeout_seconds = payload.timeout_seconds
    global_concurrency = payload.global_concurrency

    async def runner(task: llm_liveness.LivenessTask) -> tuple[str, dict[str, Any]]:
        row = row_snapshots.get(task.provider_id)
        if row is None:
            return (llm_liveness.diag.DIAG_SKIPPED_PROVIDER_MISSING, {"skipped": True})
        started = _time.monotonic()
        proxy_url = proxy_by_id.get(task.provider_id)
        transport_metadata = _liveness_transport_metadata(row)
        try:
            cli = build_client(
                row,
                override_model=task.model_id,
                proxy_url=proxy_url,
                request_scope=REQUEST_SCOPE_LIVENESS,
            )
            result = await cli.complete(
                system_prompt,
                message,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            await _emit_llm_diagnostic_usage(
                provider_row=row,
                source="diagnostic:full-liveness",
                started=started,
                system=system_prompt,
                user_prompt=message,
                model=result.model or task.model_id,
                result=result,
            )
            text = (result.text or "").strip()
            status = (
                llm_liveness.diag.DIAG_HEALTHY
                if text
                else llm_liveness.diag.DIAG_EMPTY_RESPONSE
            )
            return (
                status,
                {
                    "latency_ms": elapsed_ms,
                    "input_tokens": int(result.input_tokens or 0),
                    "output_tokens": int(result.output_tokens or 0),
                    "preview": text[:240] or None,
                    **transport_metadata,
                },
            )
        except asyncio.CancelledError:
            cancelled = LLMError("全局模型巡检已取消")
            try:
                await asyncio.shield(
                    _emit_llm_diagnostic_usage(
                        provider_row=row,
                        source="diagnostic:full-liveness",
                        started=started,
                        system=system_prompt,
                        user_prompt=message,
                        model=task.model_id,
                        error=cancelled,
                    )
                )
            except Exception:  # noqa: BLE001 - 取消路径不能被记账失败阻塞
                pass
            raise
        except LLMError as exc:
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            await _emit_llm_diagnostic_usage(
                provider_row=row,
                source="diagnostic:full-liveness",
                started=started,
                system=system_prompt,
                user_prompt=message,
                model=task.model_id,
                error=exc,
            )
            status = _liveness_status_from_error(exc)
            return (
                status,
                {
                    "latency_ms": elapsed_ms,
                    "error": llm_liveness.diag.redact(str(exc)),
                    "status_code": exc.status_code,
                    "error_category": status,
                    "suggestion": llm_liveness.diag.suggestion_for(status),
                    **transport_metadata,
                },
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            await _emit_llm_diagnostic_usage(
                provider_row=row,
                source="diagnostic:full-liveness",
                started=started,
                system=system_prompt,
                user_prompt=message,
                model=task.model_id,
                error=exc,
            )
            status = llm_liveness.diag.classify_exception(exc)
            return (
                status,
                {
                    "latency_ms": elapsed_ms,
                    "error": llm_liveness.diag.redact(f"{type(exc).__name__}: {exc}"),
                    "error_category": status,
                    "suggestion": llm_liveness.diag.suggestion_for(status),
                    **transport_metadata,
                },
            )

    def on_result(
        _task: llm_liveness.LivenessTask, _status: str, entry: dict[str, Any]
    ) -> None:
        job.results.append(entry)
        job.updated_at = _time.monotonic()

    async def _run_job() -> None:
        import time as _time_mod

        job.status = "running"
        job.updated_at = _time_mod.monotonic()
        try:
            await llm_liveness.run_liveness_pool(
                tasks,
                runner,
                global_concurrency=global_concurrency,
                provider_concurrency=llm_liveness.DEFAULT_PROVIDER_CONCURRENCY,
                cancel_token=job.cancel_token,
                on_result=on_result,
            )
        except Exception as exc:  # noqa: BLE001
            job.error = llm_liveness.diag.redact(f"{type(exc).__name__}: {exc}")
        finally:
            # 若用户已 cancel，保持 cancelled；否则 completed。
            if job.cancel_token.cancelled:
                job.status = "cancelled"
            else:
                job.status = "completed"
            job.updated_at = _time_mod.monotonic()

    job.bg_task = asyncio.create_task(_run_job())
    return FullLivenessRunStartResponse(
        run_id=job.run_id,
        status=job.status,
        task_total=job.task_total,
    )


@router.get(
    "/api/commands/llm-providers/liveness/{run_id}",
    response_model=FullLivenessRunResponse,
)
async def full_liveness_status(
    run_id: str, _user: CurrentUser
) -> FullLivenessRunResponse:
    """轮询测活 Job：返回当前已完成的逐项结果与汇总计数。"""
    job = llm_liveness.liveness_jobs.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="测活运行不存在或已过期")
    return _liveness_snapshot_response(job)


@router.post(
    "/api/commands/llm-providers/liveness/{run_id}/cancel",
    response_model=FullLivenessRunResponse,
)
async def full_liveness_cancel(
    run_id: str, _user: CurrentUser
) -> FullLivenessRunResponse:
    """真实取消测活：停止发起新任务，并 cancel 在途 asyncio Task（中断上游请求）。"""
    job = llm_liveness.liveness_jobs.cancel(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="测活运行不存在或已过期")
    return _liveness_snapshot_response(job)


def _liveness_status_from_error(exc: Any) -> str:
    """把 LLMError 映射为诊断状态（用于测活结果分类）。"""
    text = str(exc).lower()
    if "429" in text or "rate" in text or "too many" in text:
        return llm_liveness.diag.DIAG_RATE_LIMITED
    if "401" in text or "unauthor" in text or "api key" in text:
        return llm_liveness.diag.DIAG_AUTH_FAILED
    if "timeout" in text or "timed out" in text:
        return llm_liveness.diag.DIAG_TIMEOUT
    if "空" in text or "empty" in text:
        return llm_liveness.diag.DIAG_EMPTY_RESPONSE
    return llm_liveness.diag.DIAG_UPSTREAM_ERROR


# ═══════════════ 客户端身份 UA 版本配置（0.57.0 收口） ═══════════════


def _client_identity_version_items() -> list[ClientIdentityVersionItem]:
    """组装当前版本总览（生效值 + 默认值 + 检测源元数据）。"""
    current = llm_identity.current_client_versions()
    defaults = llm_identity.default_client_versions()
    meta = llm_identity.version_key_metadata()
    items: list[ClientIdentityVersionItem] = []
    for key, info in meta.items():
        registry = info.get("registry")
        items.append(
            ClientIdentityVersionItem(
                key=key,
                label=str(info.get("label") or key),
                current=current.get(key, defaults.get(key, "")),
                default=defaults.get(key, ""),
                registry=registry,
                detectable=bool(registry),
            )
        )
    return items


@router.get(
    "/api/commands/llm-providers/identity-versions",
    response_model=ClientIdentityVersionsResponse,
)
async def get_client_identity_versions(_user: CurrentUser) -> ClientIdentityVersionsResponse:
    """读取客户端身份 UA 版本配置（生效值 + 默认值 + 检测源）。"""
    return ClientIdentityVersionsResponse(
        items=_client_identity_version_items(),
        profiles=llm_identity.request_configuration_profiles(),
    )


async def _detect_registry_latest(registry: str) -> tuple[str | None, str | None]:
    """通过公共 registry 或本机只读 CLI 查询最新版本（不安装、不落库）。

    返回 ``(latest, error)``。支持 ``npm:<pkg>``、``pypi:<pkg>`` 与
    ``cli:grok-update-check``。
    """
    try:
        kind, _, pkg = registry.partition(":")
        if not pkg:
            return None, "无效的检测源"
        if kind == "cli" and pkg == "grok-update-check":
            return await _detect_grok_cli_latest()
        async with httpx.AsyncClient(timeout=10.0) as client:
            if kind == "npm":
                resp = await client.get(f"https://registry.npmjs.org/{pkg}/latest")
                if resp.status_code != 200:
                    return None, f"registry 返回 {resp.status_code}"
                return str(resp.json().get("version") or "") or None, None
            if kind == "pypi":
                resp = await client.get(f"https://pypi.org/pypi/{pkg}/json")
                if resp.status_code != 200:
                    return None, f"registry 返回 {resp.status_code}"
                return str(resp.json().get("info", {}).get("version") or "") or None, None
        return None, f"未知检测源类型: {kind}"
    except Exception as exc:  # noqa: BLE001 - 网络异常降级为逐项 error，不抛 500
        return None, f"{type(exc).__name__}: {str(exc)[:80]}"


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SEMVER_RE = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?![\d.])")
_GROK_STABLE_CHANNEL_URL = "https://x.ai/cli/stable"


def _parse_grok_update_check(output: str) -> str | None:
    """提取 ``grok update --check`` 输出中的远端最新版本。"""

    cleaned = _ANSI_ESCAPE_RE.sub("", output or "")
    try:
        payload = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        latest = str(payload.get("latestVersion") or "").strip()
        if _SEMVER_RE.fullmatch(latest):
            return latest
    arrow = re.search(
        r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*->\s*"
        r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)",
        cleaned,
    )
    if arrow:
        return arrow.group(2)
    versions = _SEMVER_RE.findall(cleaned)
    return versions[-1] if versions else None


async def _detect_grok_stable_latest() -> tuple[str | None, str | None]:
    """读取 xAI 官方安装器使用的 stable 通道指针。"""

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.get(_GROK_STABLE_CHANNEL_URL)
    except httpx.HTTPError as exc:
        return None, f"官方 stable 通道请求失败：{type(exc).__name__}"
    if response.status_code != 200:
        return None, f"官方 stable 通道返回 {response.status_code}"
    latest = response.text.strip().splitlines()[0] if response.text.strip() else ""
    if _SEMVER_RE.fullmatch(latest):
        return latest, None
    return None, "官方 stable 通道未返回可识别的版本号"


async def _grok_detection_fallback(primary_error: str) -> tuple[str | None, str | None]:
    latest, fallback_error = await _detect_grok_stable_latest()
    if latest:
        return latest, None
    return None, f"{primary_error}；{fallback_error}"


async def _detect_grok_cli_latest() -> tuple[str | None, str | None]:
    """执行固定只读命令；无 CLI 时回退 xAI 官方 stable 通道。"""

    try:
        process = await asyncio.create_subprocess_exec(
            "grok",
            "update",
            "--check",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return await _grok_detection_fallback("当前运行环境未安装 Grok CLI")
    try:
        async with asyncio.timeout(12):
            stdout, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        return await _grok_detection_fallback("grok update --check 超时")
    output = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0:
        detail = _ANSI_ESCAPE_RE.sub("", output).strip()[:120]
        return await _grok_detection_fallback(
            f"grok update --check 失败（{process.returncode}）：{detail or '无输出'}"
        )
    latest = _parse_grok_update_check(output)
    if latest:
        return latest, None
    return await _grok_detection_fallback(
        "grok update --check 未返回可识别的版本号"
    )


@router.post(
    "/api/commands/llm-providers/identity-versions/detect",
    response_model=ClientIdentityVersionDetectResponse,
)
async def detect_client_identity_versions(_user: CurrentUser) -> ClientIdentityVersionDetectResponse:
    """检测有公共 registry 的版本键的远端最新版本（只读、不落库、不消耗 quota）。"""
    current = llm_identity.current_client_versions()
    meta = llm_identity.version_key_metadata()
    detect_targets = [(k, str(v["registry"])) for k, v in meta.items() if v.get("registry")]
    results = await asyncio.gather(
        *(_detect_registry_latest(reg) for _, reg in detect_targets)
    )
    items: list[ClientIdentityVersionDetectItem] = []
    for (key, _registry), (latest, error) in zip(detect_targets, results, strict=True):
        cur = current.get(key, "")
        items.append(
            ClientIdentityVersionDetectItem(
                key=key,
                current=cur,
                latest=latest,
                up_to_date=(latest == cur) if latest else None,
                error=error,
            )
        )
    return ClientIdentityVersionDetectResponse(items=items)


@router.put(
    "/api/commands/llm-providers/identity-versions",
    response_model=ClientIdentityVersionsResponse,
)
async def update_client_identity_versions(
    payload: ClientIdentityVersionsUpdateRequest,
    _user: CurrentUser,
    db: DBSession,
) -> ClientIdentityVersionsResponse:
    """保存 UA 版本覆盖（只改版本号，不动 UA 结构与请求头）。

    非法版本值（不符合版本格式）会被后端 ``apply_version_overrides`` 静默忽略；
    这里先做一次显式校验，把明确非法的键回报为 400，避免用户以为已保存。
    """
    from ..db.models.system import SystemSetting

    valid_keys = set(llm_identity.version_key_metadata().keys())
    cleaned: dict[str, str] = {}
    invalid: list[str] = []
    for key, value in (payload.overrides or {}).items():
        if key not in valid_keys:
            continue
        if llm_identity.is_valid_version(value):
            cleaned[key] = str(value).strip()
        else:
            invalid.append(key)
    if invalid:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_VERSION", "message": f"版本号格式非法: {', '.join(invalid)}"},
        )

    row = await db.get(SystemSetting, llm_identity.CLIENT_IDENTITY_VERSIONS_SETTING_KEY)
    if row is None:
        db.add(SystemSetting(key=llm_identity.CLIENT_IDENTITY_VERSIONS_SETTING_KEY, value=cleaned))
    else:
        row.value = cleaned
    await db.commit()

    # 立即应用到进程内目录，使新版本对后续请求生效。
    llm_identity.apply_version_overrides(cleaned)
    # Telegram 命令与插件运行在 multiprocessing spawn worker 中，各自持有独立的
    # 身份目录。复用 reload_commands 让全部账号 worker 从 DB 重载版本覆盖；Redis
    # 消息未确认时仍会由 worker 的周期 reconcile 最终收敛。
    aids = await command_service.list_all_account_ids(db)
    await command_service.notify_reload(aids)
    return ClientIdentityVersionsResponse(
        items=_client_identity_version_items(),
        profiles=llm_identity.request_configuration_profiles(),
    )


__all__ = ["router"]
