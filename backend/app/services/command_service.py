"""自定义命令业务层（Sprint2 #2）。

职责：
- ``command_template`` CRUD + 名称冲突检测
- ``llm_provider`` CRUD + Fernet 加密落库 + has_api_key 出参
- ``account_command_link`` 启用 / 禁用 + 通知 worker reload

约定：
- 服务层不在内部 ``commit``；事务边界由 API 层（``api/commands.py``）控制
- IPC ``CMD_RELOAD_COMMANDS`` 失败静默，redis 不可用时不阻塞 DB 操作
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt_str
from ..db.models.account import Account, Proxy
from ..db.models.command import (
    AccountCommandLink,
    CommandTemplate,
    LLMProvider,
    normalize_client_identity_profile,
    normalize_protocol_profile,
)
from ..redis_client import get_redis
from ..schemas.command import (
    AccountCommandItem,
    CommandTemplateCreate,
    CommandTemplateOut,
    CommandTemplateUpdate,
    LLMProviderCreate,
    LLMProviderOut,
    LLMProviderUpdate,
)
from ..worker.ipc import CMD_RELOAD_COMMANDS, publish_cmd_with_ack

log = logging.getLogger(__name__)

_BUILTIN_RESERVED_WORDS: set[str] = {
    "help", "h",
    "status", "s", "st",
    "id", "i",
    "ping",
    "pause",
    "resume",
    "restart", "rs",
    "version", "v",
    "del",
}


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _validate_template_keywords_unique(
    db: AsyncSession,
    *,
    name: str,
    aliases: Sequence[str],
    current_id: int | None = None,
) -> None:
    normalized_aliases = [a.strip() for a in aliases if a and a.strip()]
    keywords = [name.strip(), *normalized_aliases]
    keyword_set = set(keywords)
    if len(keyword_set) != len(keywords):
        raise _err("TEMPLATE_ALIAS_CONFLICT", "命令名与 aliases 不能重复", 409)
    for word in sorted(keyword_set):
        if word in _BUILTIN_RESERVED_WORDS:
            raise _err("TEMPLATE_ALIAS_CONFLICT", f"命令名/别名与内置命令冲突：{word}", 409)

    rows = (await db.execute(select(CommandTemplate))).scalars().all()
    for row in rows:
        if current_id is not None and row.id == current_id:
            continue
        existing = {row.name, *(row.aliases or [])}
        hit = existing & keyword_set
        if hit:
            conflict_word = sorted(hit)[0]
            raise _err("TEMPLATE_ALIAS_CONFLICT", f"命令名/别名冲突：{conflict_word}", 409)


# ════════════════════════════════════════════════════════════
# CommandTemplate CRUD
# ════════════════════════════════════════════════════════════


async def list_templates(db: AsyncSession) -> list[CommandTemplate]:
    """列出全部模板（按 id 升序，便于 UI 稳定排序）。"""
    rows = (
        await db.execute(select(CommandTemplate).order_by(CommandTemplate.id.asc()))
    ).scalars().all()
    return list(rows)


async def get_template(db: AsyncSession, tpl_id: int) -> CommandTemplate:
    """取单条；不存在则 404。"""
    row = await db.get(CommandTemplate, tpl_id)
    if row is None:
        raise _err("TEMPLATE_NOT_FOUND", "命令模板不存在", 404)
    return row


async def create_template(db: AsyncSession, payload: CommandTemplateCreate) -> CommandTemplate:
    """新建模板；name 冲突 → 409。"""
    await _validate_template_keywords_unique(
        db, name=payload.name, aliases=payload.aliases, current_id=None
    )

    tpl = CommandTemplate(
        name=payload.name,
        aliases=list(payload.aliases or []),
        type=payload.type,
        config=dict(payload.config or {}),
        description=payload.description,
    )
    db.add(tpl)
    await db.flush()
    return tpl


async def update_template(
    db: AsyncSession, tpl_id: int, payload: CommandTemplateUpdate
) -> CommandTemplate:
    """PATCH 模板；只更新显式给出的字段。"""
    tpl = await get_template(db, tpl_id)
    data = payload.model_dump(exclude_unset=True)

    # 改名时查重
    if "name" in data or "aliases" in data:
        await _validate_template_keywords_unique(
            db,
            name=str(data.get("name") or tpl.name),
            aliases=list(data.get("aliases") if "aliases" in data else (tpl.aliases or [])),
            current_id=tpl.id,
        )

    # 校验 config / type 一致性：要么二者都改，要么 schema validator 已经在 base 校验过；
    # 这里只在 PATCH 模式下额外保障 config 跟随 type 走
    new_type = data.get("type") or tpl.type
    new_config = data.get("config") if "config" in data else tpl.config
    if "type" in data or "config" in data:
        # 复用 base 验证：构造一个完整对象走一遍
        from ..schemas.command import CommandTemplateBase

        CommandTemplateBase(
            name=data.get("name") or tpl.name,
            type=new_type,
            config=dict(new_config or {}),
            description=data.get("description", tpl.description),
        )

    for k, v in data.items():
        setattr(tpl, k, v)
    await db.flush()
    return tpl


async def delete_template(db: AsyncSession, tpl_id: int) -> set[int]:
    """删除模板；返回受影响的 account_id 集合（用于 IPC 通知 reload）。

    级联删除会自动带走 ``account_command_link``；这里先抓 aid 集合再删，便于回调通知。
    """
    tpl = await get_template(db, tpl_id)
    aids = (
        await db.execute(
            select(AccountCommandLink.account_id).where(
                AccountCommandLink.template_id == tpl.id
            )
        )
    ).scalars().all()
    await db.delete(tpl)
    await db.flush()
    return set(aids)


# ════════════════════════════════════════════════════════════
# 账号 × 模板 关联
# ════════════════════════════════════════════════════════════


async def list_for_account(
    db: AsyncSession, account_id: int
) -> list[AccountCommandItem]:
    """列出某账号已启用 + 可用全部模板，标记 enabled 状态。

    返回顺序：模板按 id 升序；前端按需要再排序。
    """
    if await db.get(Account, account_id) is None:
        raise _err("ACCOUNT_NOT_FOUND", "账号不存在", 404)

    templates = await list_templates(db)
    links = {
        link.template_id: link.enabled
        for link in (
            await db.execute(
                select(AccountCommandLink).where(
                    AccountCommandLink.account_id == account_id
                )
            )
        ).scalars().all()
    }

    out: list[AccountCommandItem] = []
    for tpl in templates:
        enabled = bool(links.get(tpl.id, False))
        out.append(
            AccountCommandItem(
                template=CommandTemplateOut.model_validate(tpl),
                enabled=enabled,
            )
        )
    return out


async def list_active_for_worker(
    db: AsyncSession, account_id: int
) -> list[CommandTemplate]:
    """worker 启动 / reload 时调用：仅返回该账号实际启用的模板。"""
    rows = (
        await db.execute(
            select(CommandTemplate)
            .join(
                AccountCommandLink, AccountCommandLink.template_id == CommandTemplate.id
            )
            .where(
                AccountCommandLink.account_id == account_id,
                AccountCommandLink.enabled.is_(True),
            )
            .order_by(CommandTemplate.id.asc())
        )
    ).scalars().all()
    return list(rows)


async def enable_for_account(
    db: AsyncSession, account_id: int, template_id: int
) -> AccountCommandLink:
    """启用某账号的某模板（upsert：若已存在但 enabled=False → 改为 True）。"""
    if await db.get(Account, account_id) is None:
        raise _err("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    if await db.get(CommandTemplate, template_id) is None:
        raise _err("TEMPLATE_NOT_FOUND", "命令模板不存在", 404)

    link = (
        await db.execute(
            select(AccountCommandLink).where(
                AccountCommandLink.account_id == account_id,
                AccountCommandLink.template_id == template_id,
            )
        )
    ).scalar_one_or_none()

    if link is None:
        link = AccountCommandLink(
            account_id=account_id, template_id=template_id, enabled=True
        )
        db.add(link)
    else:
        link.enabled = True
    await db.flush()
    return link


async def disable_for_account(
    db: AsyncSession, account_id: int, template_id: int
) -> None:
    """禁用某账号的某模板（直接删 link 行；下次启用再 upsert）。"""
    link = (
        await db.execute(
            select(AccountCommandLink).where(
                AccountCommandLink.account_id == account_id,
                AccountCommandLink.template_id == template_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        return
    await db.delete(link)
    await db.flush()


# ════════════════════════════════════════════════════════════
# LLM Provider CRUD
# ════════════════════════════════════════════════════════════


def _provider_to_out(row: LLMProvider) -> LLMProviderOut:
    """ORM → 出参；屏蔽明文 api_key。"""
    return LLMProviderOut(
        id=row.id,
        name=row.name,
        provider=row.provider,
        has_api_key=bool(row.api_key_enc),
        base_url=row.base_url,
        default_model=row.default_model,
        api_format=getattr(row, "api_format", None) or "chat_completions",
        protocol_profile=normalize_protocol_profile(
            getattr(row, "api_format", None),
            getattr(row, "protocol_profile", None),
        ),
        web_search_api_format=getattr(row, "web_search_api_format", None) or "auto",
        client_identity_profile=normalize_client_identity_profile(
            getattr(row, "client_identity_profile", None)
        ),
        # 路由元数据（老数据可能为 None / [] / 缺字段；用属性 getattr 兼容）
        modality=getattr(row, "modality", None) or "text",
        tags=list(getattr(row, "tags", None) or []),
        cost_tier=int(getattr(row, "cost_tier", None) or 2),
        notes=getattr(row, "notes", None),
        proxy_id=getattr(row, "proxy_id", None),
        # 候选模型清单
        models=list(getattr(row, "models", None) or []),
        created_at=row.created_at,
    )


async def _validate_proxy_for_llm(db: AsyncSession, proxy_id: int) -> Proxy:
    """校验 proxy_id 指向的 Proxy 行可用作 LLM 出口。

    拒绝条件：
    - 不存在 → 404
    - type=mtproxy → 422（HTTP 客户端不支持 Telegram MTProto）
    """
    p = await db.get(Proxy, proxy_id)
    if p is None:
        raise _err("PROXY_NOT_FOUND", f"proxy_id={proxy_id} 不存在", 404)
    if (p.type or "").lower() == "mtproxy":
        raise _err(
            "PROXY_KIND_NOT_SUPPORTED",
            "mtproxy 仅支持 Telegram，不能用于 LLM 调用；请选 socks5/http/https 类型的代理",
            422,
        )
    return p


async def list_providers(db: AsyncSession) -> list[LLMProviderOut]:
    """列出全部 LLM provider（不返明文 key）。"""
    rows = (
        await db.execute(select(LLMProvider).order_by(LLMProvider.id.asc()))
    ).scalars().all()
    return [_provider_to_out(r) for r in rows]


async def get_provider_row(db: AsyncSession, pid: int) -> LLMProvider:
    """内部使用；返回原始 ORM（含 api_key_enc）。worker 用来调 LLM 之前需要解密。"""
    row = await db.get(LLMProvider, pid)
    if row is None:
        raise _err("LLM_PROVIDER_NOT_FOUND", "LLM provider 不存在", 404)
    return row


async def create_provider(
    db: AsyncSession, payload: LLMProviderCreate
) -> LLMProviderOut:
    """新建 provider；api_key 给非空字符串则加密落库。"""
    dup = (
        await db.execute(select(LLMProvider).where(LLMProvider.name == payload.name))
    ).scalar_one_or_none()
    if dup is not None:
        raise _err("LLM_PROVIDER_NAME_CONFLICT", f"已存在同名 provider：{payload.name}", 409)

    # 校验 proxy_id（如果指定了）
    if payload.proxy_id is not None:
        await _validate_proxy_for_llm(db, payload.proxy_id)

    row = LLMProvider(
        name=payload.name,
        provider=payload.provider,
        # 空字符串视同未设置（避免存空 token 让 fernet 误判）
        api_key_enc=encrypt_str(payload.api_key) if payload.api_key else None,
        base_url=payload.base_url,
        default_model=payload.default_model,
        api_format=payload.api_format,
        protocol_profile=normalize_protocol_profile(
            payload.api_format,
            payload.protocol_profile,
        ),
        web_search_api_format=payload.web_search_api_format,
        client_identity_profile=normalize_client_identity_profile(
            payload.client_identity_profile
        ),
        # 路由元数据
        modality=payload.modality,
        tags=list(payload.tags or []),
        cost_tier=int(payload.cost_tier),
        notes=payload.notes,
        proxy_id=payload.proxy_id,
        # 候选模型清单（前端可以建完之后再调 fetch-models 自动填）
        models=[m.model_dump() for m in (payload.models or [])],
    )
    db.add(row)
    await db.flush()
    return _provider_to_out(row)


async def update_provider(
    db: AsyncSession, pid: int, payload: LLMProviderUpdate
) -> LLMProviderOut:
    """PATCH provider；
    api_key 行为：
    - None  → 不动
    - ""    → 清空
    - 非空  → 加密落库
    """
    row = await get_provider_row(db, pid)
    data = payload.model_dump(exclude_unset=True)

    if "name" in data and data["name"] != row.name:
        dup = (
            await db.execute(
                select(LLMProvider).where(LLMProvider.name == data["name"])
            )
        ).scalar_one_or_none()
        if dup is not None and dup.id != row.id:
            raise _err("LLM_PROVIDER_NAME_CONFLICT", f"已存在同名 provider：{data['name']}", 409)
        row.name = data["name"]

    if "provider" in data:
        row.provider = data["provider"]
    if "base_url" in data:
        row.base_url = data["base_url"]
    if "default_model" in data and data["default_model"]:
        row.default_model = data["default_model"]
    effective_api_format = str(data.get("api_format") or row.api_format or "chat_completions")
    if "api_format" in data and data["api_format"]:
        row.api_format = data["api_format"]
    row.protocol_profile = normalize_protocol_profile(
        effective_api_format,
        data.get("protocol_profile", getattr(row, "protocol_profile", None)),
    )
    if "web_search_api_format" in data and data["web_search_api_format"]:
        row.web_search_api_format = data["web_search_api_format"]
    if "client_identity_profile" in data and data["client_identity_profile"]:
        row.client_identity_profile = normalize_client_identity_profile(
            data["client_identity_profile"]
        )

    # 路由元数据：明确出现在 patch 内才覆盖
    if "modality" in data and data["modality"] is not None:
        row.modality = data["modality"]
    if "tags" in data and data["tags"] is not None:
        row.tags = list(data["tags"])
    if "cost_tier" in data and data["cost_tier"] is not None:
        row.cost_tier = int(data["cost_tier"])
    if "notes" in data:
        # notes 允许显式 None 清空
        row.notes = data["notes"]

    # proxy 处理：clear_proxy=True → 切回直连；否则只在 proxy_id 显式给值时改
    # （exclude_unset=True 已经过滤掉前端没传的字段，所以 proxy_id 出现 = 用户主动选了一条）
    if data.get("clear_proxy"):
        row.proxy_id = None
    elif "proxy_id" in data and data["proxy_id"] is not None:
        await _validate_proxy_for_llm(db, int(data["proxy_id"]))
        row.proxy_id = int(data["proxy_id"])

    # models：整体替换（前端 PATCH 整个 list；fetch-models / test-model 走独立接口）
    if "models" in data and data["models"] is not None:
        # data["models"] 此时可能是 list[dict]（pydantic 已把 ProviderModel 序列化）
        # 也可能是 list[ProviderModel]，两种都做 dict 化兜底
        new_models = []
        for m in data["models"]:
            if hasattr(m, "model_dump"):
                new_models.append(m.model_dump())
            else:
                new_models.append(dict(m))
        row.models = new_models

    if "api_key" in data:
        v = data["api_key"]
        if v is None:
            # 显式 None 表示前端没改 key 字段；保持原样
            pass
        elif v == "":
            row.api_key_enc = None
        else:
            row.api_key_enc = encrypt_str(v)

    await db.flush()
    return _provider_to_out(row)


async def delete_provider(db: AsyncSession, pid: int) -> None:
    """删除 provider；引用此 provider 的 ai 类模板调用会在 worker 内报错（friendly message）。"""
    row = await get_provider_row(db, pid)
    await db.delete(row)
    await db.flush()


# ════════════════════════════════════════════════════════════
# IPC：通知 worker 重新拉取启用模板
# ════════════════════════════════════════════════════════════


async def list_aids_with_ai_commands(db: AsyncSession) -> list[int]:
    """返回所有"启用了 type=ai 模板"的账号 id（去重）。

    用途：改 / 删 LLM Provider 后通知这些账号 reload——worker 端
    ``_refresh_command_context`` 是无差别全量拉 provider 表的，所以一次
    reload 就能让所有 ai 模板看到新配置（包括 api_key 轮换、tags 调整、
    base_url 切到反代等）。

    这里故意不去深扒每条模板 config.provider_id 是否真的引用了改动的那条
    provider——多发一次 reload 是廉价的（worker 只重读一次 DB），但漏发
    会让用户的 api_key 轮换"在 TG 里发现没生效"，体验差。
    """
    rows = (
        await db.execute(
            select(AccountCommandLink.account_id)
            .join(
                CommandTemplate,
                CommandTemplate.id == AccountCommandLink.template_id,
            )
            .where(
                AccountCommandLink.enabled.is_(True),
                CommandTemplate.type == "ai",
            )
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def ai_command_enablement_summary(db: AsyncSession) -> dict[str, int]:
    """返回 AI 命令模板在账号上的启用摘要。"""
    total_accounts = (
        await db.execute(select(Account.id))
    ).scalars().all()
    ai_templates = (
        await db.execute(select(CommandTemplate.id).where(CommandTemplate.type == "ai"))
    ).scalars().all()
    enabled_accounts = await list_aids_with_ai_commands(db)
    return {
        "total_accounts": len(total_accounts),
        "enabled_accounts": len(enabled_accounts),
        "ai_templates": len(ai_templates),
    }


async def list_all_account_ids(db: AsyncSession) -> list[int]:
    """返回所有账号 id（用于 provider 变更时全量广播 reload）。"""
    rows = (await db.execute(select(Account.id))).scalars().all()
    return list(rows)

async def notify_reload(account_ids: int | Sequence[int]) -> None:
    """对一个或多个账号发 ``CMD_RELOAD_COMMANDS`` IPC。

    0.5.2 行为收紧：不再静默吞异常。
    - 逐账号 publish，单个失败不影响其它账号
    - 汇总失败账号并抛 RuntimeError，让 API 层可见并记录
    """
    if isinstance(account_ids, int):
        aids: list[int] = [account_ids]
    else:
        aids = list(dict.fromkeys(account_ids))
    if not aids:
        return

    redis = get_redis()
    failed: list[int] = []
    for aid in aids:
        try:
            ok = await publish_cmd_with_ack(redis, int(aid), CMD_RELOAD_COMMANDS)
            if not ok:
                log.debug("worker reload_commands 未确认 aid=%s，将由周期 reconcile 收敛", aid)
        except Exception:  # noqa: BLE001
            failed.append(int(aid))
            log.exception("通知 worker reload_commands 失败 aid=%s", aid)

    if failed:
        raise RuntimeError(f"reload_commands publish failed for aids={failed}")


__all__ = [
    "create_provider",
    "create_template",
    "delete_provider",
    "delete_template",
    "disable_for_account",
    "enable_for_account",
    "get_provider_row",
    "get_template",
    "list_active_for_worker",
    "list_all_account_ids",
    "list_for_account",
    "list_providers",
    "list_templates",
    "notify_reload",
    "update_provider",
    "update_template",
]
