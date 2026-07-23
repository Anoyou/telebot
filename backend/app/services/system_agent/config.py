"""System Agent 固定 Provider 配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.command import LLMProvider
from ...db.models.system import SystemSetting
from ..llm_agent import AgentLimits
from ..llm_dto import LLMProviderDTO

CONFIG_KEY = "system_agent_config"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "provider_id": None,
    "model": None,
    "fallback_provider_ids": [],
    "require_tool_approval": False,
    "max_steps": AgentLimits.max_steps,
    "max_tool_calls": AgentLimits.max_tool_calls,
    # 单轮上下文增长 + 输出预算（增量口径，见 llm_agent.run_agent）
    "session_token_limit": 16_384,
}


@dataclass(frozen=True)
class ResolvedAgentProviders:
    primary: LLMProviderDTO
    model: str
    providers: dict[int, LLMProviderDTO]


def normalize_config(raw: Any) -> dict[str, Any]:
    base = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return base
    enabled = bool(raw.get("enabled", False))
    require_tool_approval = bool(raw.get("require_tool_approval", False))
    provider_id = raw.get("provider_id")
    try:
        provider_id = int(provider_id) if provider_id not in (None, "") else None
    except (TypeError, ValueError):
        provider_id = None
    model = str(raw.get("model") or "").strip() or None
    fallback_provider_ids: list[int] = []
    for value in raw.get("fallback_provider_ids") or []:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate > 0 and candidate != provider_id and candidate not in fallback_provider_ids:
            fallback_provider_ids.append(candidate)
    try:
        max_steps = int(raw.get("max_steps") or DEFAULT_CONFIG["max_steps"])
    except (TypeError, ValueError):
        max_steps = DEFAULT_CONFIG["max_steps"]
    try:
        max_tool_calls = int(raw.get("max_tool_calls") or DEFAULT_CONFIG["max_tool_calls"])
    except (TypeError, ValueError):
        max_tool_calls = DEFAULT_CONFIG["max_tool_calls"]
    try:
        session_token_limit = int(
            raw.get("session_token_limit") or DEFAULT_CONFIG["session_token_limit"]
        )
    except (TypeError, ValueError):
        session_token_limit = DEFAULT_CONFIG["session_token_limit"]
    return {
        "enabled": enabled,
        "provider_id": provider_id,
        "model": model,
        "fallback_provider_ids": fallback_provider_ids[:16],
        "require_tool_approval": require_tool_approval,
        "max_steps": max(1, min(max_steps, 16)),
        "max_tool_calls": max(1, min(max_tool_calls, 64)),
        "session_token_limit": max(1024, min(session_token_limit, 100_000)),
    }


async def load_config(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(SystemSetting, CONFIG_KEY)
    return normalize_config(row.value if row else None)


async def save_config(db: AsyncSession, patch: dict[str, Any]) -> dict[str, Any]:
    current = await load_config(db)
    merged = {**current, **{k: v for k, v in patch.items() if k in DEFAULT_CONFIG}}
    normalized = normalize_config(merged)
    row = await db.get(SystemSetting, CONFIG_KEY)
    if row is None:
        row = SystemSetting(key=CONFIG_KEY, value=normalized)
        db.add(row)
    else:
        row.value = normalized
    await db.flush()
    return normalized


def tools_model_for_dto(dto: LLMProviderDTO, explicit: str | None = None) -> str | None:
    """最小共享 helper：返回已启用且声明支持 tools 的模型。"""

    explicit_clean = str(explicit or "").strip()
    if explicit_clean:
        enabled = dto.enabled_model_ids()
        if dto.has_model_list() and explicit_clean not in enabled:
            return None
        return explicit_clean if dto.capabilities_for_model(explicit_clean).tools else None
    candidate = dto.pick_enabled_model()
    if candidate and dto.capabilities_for_model(candidate).tools:
        return candidate
    for model_id in dto.enabled_model_ids():
        if dto.capabilities_for_model(model_id).tools:
            return model_id
    return None


def tools_models_for_dto(
    dto: LLMProviderDTO,
    explicit: str | None = None,
) -> list[str]:
    """返回同一 Provider 内可用于 Agent 的模型，首选模型排在最前。"""

    preferred = tools_model_for_dto(dto, explicit)
    candidates = list(dto.enabled_model_ids())
    if not candidates and not dto.has_model_list():
        default_model = str(dto.default_model or "").strip()
        if default_model:
            candidates = [default_model]
    ordered = [preferred] if preferred else []
    ordered.extend(model for model in candidates if model and model != preferred)
    return [
        model
        for model in ordered
        if dto.capabilities_for_model(model).tools
    ]


async def resolve_fixed_provider(
    db: AsyncSession,
    config: dict[str, Any] | None = None,
) -> tuple[LLMProviderDTO, str] | tuple[None, str]:
    """解析固定 Provider 与 tools 模型。

    返回 ``(dto, model)`` 或 ``(None, error_message)``。
    """

    resolved = await resolve_agent_providers(db, config)
    if isinstance(resolved, str):
        return None, resolved
    return resolved.primary, resolved.model


async def resolve_agent_providers(
    db: AsyncSession,
    config: dict[str, Any] | None = None,
) -> ResolvedAgentProviders | str:
    """解析主 Provider，并加载可用于 tools fallback 的候选。"""

    cfg = config or await load_config(db)
    if not cfg.get("enabled"):
        return "系统助手未启用，请在配置中开启。"
    provider_id = cfg.get("provider_id")
    if not provider_id:
        return "未配置系统助手固定 Provider，请到 AI 中心选择支持 tools 的模型。"
    rows = list((await db.execute(select(LLMProvider).order_by(LLMProvider.id))).scalars().all())
    dtos = {int(row.id): LLMProviderDTO.from_orm_row(row) for row in rows}
    dto = dtos.get(int(provider_id))
    if dto is None:
        return f"配置的 Provider #{provider_id} 不存在。"
    if not dto.has_api_key:
        return f"Provider「{dto.name}」缺少 API Key。"
    model = tools_model_for_dto(dto, cfg.get("model"))
    if model is None:
        return (
            f"Provider「{dto.name}」没有可用的 tools 模型。"
            "请选择声明支持 tools 的已启用模型。"
        )
    compatible: dict[int, LLMProviderDTO] = {dto.id: dto}
    for fallback_id in cfg.get("fallback_provider_ids") or []:
        provider = dtos.get(int(fallback_id))
        if (
            provider is not None
            and provider.has_api_key
            and tools_model_for_dto(provider) is not None
        ):
            compatible[provider.id] = provider
    return ResolvedAgentProviders(primary=dto, model=model, providers=compatible)


async def load_system_context_flags(db: AsyncSession) -> dict[str, Any]:
    keys = ("timezone", "command_prefix", "ai_enabled", CONFIG_KEY)
    result = await db.execute(select(SystemSetting).where(SystemSetting.key.in_(keys)))
    rows = {r.key: r.value for r in result.scalars().all()}
    agent_cfg = normalize_config(rows.get(CONFIG_KEY))
    tz = rows.get("timezone")
    if isinstance(tz, dict):
        timezone_name = str(tz.get("value") or tz.get("timezone") or "UTC")
    else:
        timezone_name = str(tz or "UTC")
    prefix = rows.get("command_prefix")
    if isinstance(prefix, dict):
        command_prefix = str(prefix.get("value") or "/")
    else:
        command_prefix = str(prefix or "/")
    ai_raw = rows.get("ai_enabled")
    if isinstance(ai_raw, dict):
        ai_enabled = bool(ai_raw.get("value", True))
    elif ai_raw is None:
        ai_enabled = True
    else:
        ai_enabled = bool(ai_raw)
    return {
        "timezone": timezone_name or "UTC",
        "command_prefix": command_prefix or "/",
        "ai_enabled": ai_enabled,
        "agent_config": agent_cfg,
    }


__all__ = [
    "CONFIG_KEY",
    "DEFAULT_CONFIG",
    "ResolvedAgentProviders",
    "load_config",
    "load_system_context_flags",
    "normalize_config",
    "resolve_agent_providers",
    "resolve_fixed_provider",
    "save_config",
    "tools_model_for_dto",
    "tools_models_for_dto",
]
