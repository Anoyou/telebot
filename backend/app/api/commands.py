"""自定义命令 + LLM Provider REST API（Sprint2 #2）。

路由前缀：
- ``/api/commands/templates``       全局模板 CRUD
- ``/api/commands/llm-providers``   LLM provider CRUD + fetch-models + test-model
- ``/api/accounts/{aid}/commands``  账号 × 模板 启用关系

安全红线：
- LLM provider 任何 GET 接口都不返回明文 ``api_key``，只返 ``has_api_key:bool``
- 模板内容不含敏感信息，可正常 audit；audit log 里会写命令名和类型，不写完整 config
"""

from __future__ import annotations

import asyncio
import time as _time

import httpx
from fastapi import APIRouter, HTTPException

from ..deps import CurrentUser, DBSession
from ..schemas.command import (
    AccountCommandItem,
    AICommandEnablementSummary,
    BuiltinCommandItem,
    ChatTestModelResult,
    ChatTestModelsRequest,
    ChatTestModelsResponse,
    CommandTemplateCreate,
    CommandTemplateOut,
    CommandTemplateUpdate,
    DetectProviderProtocolsRequest,
    DetectProviderProtocolsResponse,
    FetchModelsPreviewRequest,
    FetchModelsPreviewResponse,
    FetchModelsResponse,
    LLMProviderCreate,
    LLMProviderOut,
    LLMProviderUpdate,
    ProtocolProbeResult,
    TestModelRequest,
    TestModelResponse,
)
from ..services import audit, command_service
from ..services.ai_feature import is_ai_enabled

router = APIRouter(tags=["commands"])


async def _require_ai_enabled(db: DBSession) -> None:
    if not await is_ai_enabled(db):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AI_DISABLED",
                "message": "AI 能力已在系统设置中关闭，请先启用后再配置或调用模型。",
            },
        )


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
    audit detail 中**绝不写** api_key 字段。

    通知 worker reload：所有启用了 type=ai 模板的账号都会被通知，
    避免 api_key / base_url / tags 改动后"TG 里没生效"。
    """
    await _require_ai_enabled(db)
    out = await command_service.update_provider(db, pid, payload)
    audit_detail = payload.model_dump(
        exclude_unset=True, exclude={"api_key"}
    )
    if "api_key" in payload.model_dump(exclude_unset=True):
        audit_detail["api_key_changed"] = True
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
    if proxy_id is None:
        return None
    from urllib.parse import quote

    from ..crypto import decrypt_str
    from ..db.models.account import Proxy

    p = await db.get(Proxy, proxy_id)
    if p is None:
        return None
    if "://" in p.host:
        from ..util.proxy import parse_proxy_url
        parsed = parse_proxy_url(p.host)
        if parsed is not None:
            ptype, host, port, _rdns, parsed_user, parsed_password = parsed
            if ptype not in ("socks5", "http"):
                return None
            user = p.username or parsed_user
            pwd = decrypt_str(p.password_enc) if p.password_enc else (parsed_password or "")
            auth = ""
            if user:
                auth = quote(user, safe="")
                if pwd:
                    auth = f"{auth}:{quote(pwd, safe='')}"
                auth = f"{auth}@"
            return f"{ptype}://{auth}{host}:{int(port)}"
    t = (p.type or "").lower()
    if t == "socks5":
        scheme = "socks5"
    elif t in ("http", "https"):
        scheme = "http"
    else:
        return None  # mtproxy / 不支持的类型
    pwd = ""
    if p.password_enc:
        try:
            pwd = decrypt_str(p.password_enc)
        except Exception:  # noqa: BLE001
            pwd = ""
    auth = ""
    if p.username:
        auth = quote(p.username, safe="")
        if pwd:
            auth = f"{auth}:{quote(pwd, safe='')}"
        auth = f"{auth}@"
    return f"{scheme}://{auth}{p.host}:{int(p.port)}"


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

    if payload.api_format == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        raise _llm_err(
            "FETCH_NOT_SUPPORTED",
            "Anthropic Messages 协议没有列出模型接口；请去 docs.anthropic.com 查模型 ID 后手动添加",
            422,
        )

    # api_key：优先入参，否则回落到 DB 里已存的
    api_key = (payload.api_key or "").strip()
    if not api_key and payload.pid is not None:
        try:
            row = await command_service.get_provider_row(db, payload.pid)
            if row.api_key_enc:
                api_key = decrypt_str(row.api_key_enc) or ""
        except Exception:  # noqa: BLE001
            # pid 错也无所谓，继续走"无 key"路径让用户看到具体的 401
            api_key = ""

    base_url = (payload.base_url or "https://api.openai.com/v1").rstrip("/")
    proxy_url = await _resolve_proxy_url(db, payload.proxy_id)

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    client_kwargs: dict[str, object] = {"timeout": httpx.Timeout(15.0, connect=8.0)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as cli:
            resp = await cli.get(f"{base_url}/models", headers=headers)
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
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            mid = it["id"].strip()
            if mid:
                new_ids.append(mid)

    await audit.write(
        db,
        user.id,
        "llm_provider.fetch_models_preview",
        target=f"llm_provider:{payload.pid or 'new'}",
        detail={"fetched": len(new_ids), "provider": payload.provider},
    )
    await db.commit()
    return FetchModelsPreviewResponse(fetched=len(new_ids), ids=new_ids)


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
    if not api_key and payload.pid is not None:
        try:
            row = await command_service.get_provider_row(db, payload.pid)
            if row.api_key_enc:
                api_key = decrypt_str(row.api_key_enc) or ""
        except Exception:  # noqa: BLE001
            api_key = ""

    provider = payload.provider
    if provider == "anthropic":
        base_url = (payload.base_url or "https://api.anthropic.com/v1").rstrip("/")
        model = (payload.model or "claude-haiku-4-5").strip()
    elif provider == "ollama":
        base_url = (payload.base_url or "http://localhost:11434/v1").rstrip("/")
        model = (payload.model or "llama3:8b").strip()
    else:
        base_url = (payload.base_url or "https://api.openai.com/v1").rstrip("/")
        model = (payload.model or "gpt-4o-mini").strip()
    proxy_url = await _resolve_proxy_url(db, payload.proxy_id)

    client_kwargs: dict[str, object] = {"timeout": httpx.Timeout(12.0, connect=6.0)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    else:
        client_kwargs["trust_env"] = False

    async def probe_models(cli: httpx.AsyncClient) -> ProtocolProbeResult:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        started = _time.monotonic()
        try:
            resp = await cli.get(f"{base_url}/models", headers=headers)
            latency_ms = int((_time.monotonic() - started) * 1000)
            return _probe_result(resp, latency_ms, api_key=api_key)
        except httpx.HTTPError as exc:
            return _probe_error(exc, started)

    async def probe_chat(cli: httpx.AsyncClient) -> ProtocolProbeResult:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        started = _time.monotonic()
        try:
            resp = await cli.post(f"{base_url}/chat/completions", headers=headers, json=body)
            latency_ms = int((_time.monotonic() - started) * 1000)
            return _probe_result(resp, latency_ms, api_key=api_key)
        except httpx.HTTPError as exc:
            return _probe_error(exc, started)

    async def probe_responses(cli: httpx.AsyncClient) -> ProtocolProbeResult:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "input": [{"role": "user", "content": "ping"}],
            "max_output_tokens": 1,
        }
        started = _time.monotonic()
        try:
            resp = await cli.post(f"{base_url}/responses", headers=headers, json=body)
            latency_ms = int((_time.monotonic() - started) * 1000)
            if _probe_unsupported_parameter(resp, "max_output_tokens"):
                compat_body = dict(body)
                compat_body.pop("max_output_tokens", None)
                compat_started = _time.monotonic()
                compat_resp = await cli.post(f"{base_url}/responses", headers=headers, json=compat_body)
                compat_latency_ms = int((_time.monotonic() - compat_started) * 1000)
                compat_result = _probe_result(compat_resp, compat_latency_ms, api_key=api_key)
                if compat_result.ok:
                    compat_result.error = "Responses 兼容模式可用：该接口不支持 max_output_tokens，运行时会自动省略。"
                return compat_result
            return _probe_result(resp, latency_ms, api_key=api_key)
        except httpx.HTTPError as exc:
            return _probe_error(exc, started)

    async def probe_anthropic(cli: httpx.AsyncClient) -> ProtocolProbeResult:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        started = _time.monotonic()
        try:
            resp = await cli.post(f"{base_url}/messages", headers=headers, json=body)
            latency_ms = int((_time.monotonic() - started) * 1000)
            return _probe_result(resp, latency_ms, api_key=api_key)
        except httpx.HTTPError as exc:
            return _probe_error(exc, started)

    async with httpx.AsyncClient(**client_kwargs) as cli:
        models = await probe_models(cli)
        chat = await probe_chat(cli)
        responses = await probe_responses(cli)
        anthropic = await probe_anthropic(cli)

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
            recommended_api_format = "chat_completions"
            recommended_web_search_api_format = "auto" if responses.ok else "chat_completions"
        elif responses.ok:
            recommended_api_format = "responses"
            recommended_web_search_api_format = "responses"
        response_compat_note = responses.error if responses.ok and responses.error else ""
        if chat.ok and responses.ok:
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
        },
    )
    await db.commit()

    return DetectProviderProtocolsResponse(
        chat_completions=chat,
        responses=responses,
        anthropic_messages=anthropic,
        models=models,
        recommended_api_format=recommended_api_format,
        recommended_web_search_api_format=recommended_web_search_api_format,
        note=note,
    )


def _probe_result(resp: httpx.Response, latency_ms: int, *, api_key: str) -> ProtocolProbeResult:
    if resp.status_code < 400:
        return ProtocolProbeResult(ok=True, status_code=resp.status_code, latency_ms=latency_ms)
    body = resp.text[:220]
    if api_key:
        body = body.replace(api_key, "<redacted>")
    return ProtocolProbeResult(
        ok=False,
        status_code=resp.status_code,
        latency_ms=latency_ms,
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


def _probe_error(exc: httpx.HTTPError, started: float) -> ProtocolProbeResult:
    return ProtocolProbeResult(
        ok=False,
        status_code=None,
        latency_ms=int((_time.monotonic() - started) * 1000),
        error=f"{type(exc).__name__}: {str(exc) or '(无详情；常见 SSL/DNS/代理问题)'}",
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

    base_url = (row.base_url or "https://api.openai.com/v1").rstrip("/")
    api_key = decrypt_str(row.api_key_enc) if row.api_key_enc else ""
    proxy_url = await _resolve_proxy_url(db, row.proxy_id)

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    client_kwargs: dict[str, object] = {"timeout": httpx.Timeout(15.0, connect=8.0)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as cli:
            resp = await cli.get(f"{base_url}/models", headers=headers)
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
            merged.append({
                "id": mid,
                "enabled": bool(old.get("enabled", False)),
                "custom": False,
                "label": old.get("label"),
            })
        else:
            merged.append({"id": mid, "enabled": False, "custom": False, "label": None})

    # 用户的自定义条目（fetch 没拿到 ID 的）保留
    fetched_ids = set(new_ids)
    for mid, old in existing.items():
        if mid not in fetched_ids and old.get("custom"):
            merged.append({
                "id": mid,
                "enabled": bool(old.get("enabled", False)),
                "custom": True,
                "label": old.get("label"),
            })

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
        cli = build_client(row, override_model=payload.model.strip(), proxy_url=proxy_url)
        result = await cli.complete("ping", "ping", max_tokens=4, timeout_seconds=90)
    except LLMError as e:
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        # LLMError 已脱敏
        return TestModelResponse(ok=False, latency_ms=elapsed_ms, error=str(e))
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        return TestModelResponse(
            ok=False,
            latency_ms=elapsed_ms,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )

    elapsed_ms = int((_time.monotonic() - started) * 1000)
    # 不写 audit（测试调用频繁，写多了刷屏）
    return TestModelResponse(
        ok=True,
        latency_ms=elapsed_ms,
        model=result.model,
        preview=(result.text or "").strip()[:80] or None,
    )


def _build_chat_test_prompt(payload: ChatTestModelsRequest) -> str:
    lines: list[str] = []
    if payload.history:
        lines.append("以下是同一个测试窗口里的最近对话，请只当作上下文：")
        for turn in payload.history[-12:]:
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
    user_prompt = _build_chat_test_prompt(payload)

    async def run_one(model_id: str) -> ChatTestModelResult:
        started = _time.monotonic()
        try:
            cli = build_client(row, override_model=model_id, proxy_url=proxy_url)
            result = await cli.complete(
                payload.system_prompt,
                user_prompt,
                max_tokens=payload.max_tokens,
                timeout_seconds=payload.timeout_seconds,
            )
            elapsed_ms = int((_time.monotonic() - started) * 1000)
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
            )
        except LLMError as exc:
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            return ChatTestModelResult(
                ok=False,
                requested_model=model_id,
                latency_ms=elapsed_ms,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            return ChatTestModelResult(
                ok=False,
                requested_model=model_id,
                latency_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )

    results = await asyncio.gather(*(run_one(model_id) for model_id in payload.models))
    return ChatTestModelsResponse(
        provider_id=pid,
        provider_name=row.name,
        results=list(results),
    )


__all__ = ["router"]
