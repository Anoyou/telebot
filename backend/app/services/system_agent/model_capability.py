"""System Agent 模型工具调用能力探测与持久缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.system import SystemSetting
from ..llm_client import LLMErrorScope, build_client_from_dto
from ..llm_dto import LLMProviderDTO
from ..llm_protocol import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    NamedToolChoice,
    ToolSpec,
)
from .config import ResolvedAgentProviders, tools_models_for_dto

CACHE_KEY = "system_agent_model_capability_cache"
PROBE_TOOL_NAME = "telepilot_capability_check"
PROBE_NONCE = "telepilot-tools-v1"
SUPPORTED_TTL = timedelta(days=7)
UNSUPPORTED_TTL = timedelta(days=1)
UNAVAILABLE_TTL = timedelta(minutes=5)
MAX_CACHE_ENTRIES = 256
MAX_CONCURRENT_PROBES = 3


@dataclass(frozen=True)
class CapabilityProbeResult:
    status: str
    checked_at: datetime
    error_type: str | None = None

    @property
    def supported(self) -> bool:
        return self.status == "supported"


def _provider_signature(provider: LLMProviderDTO, model: str) -> str:
    payload = {
        "provider_id": provider.id,
        "provider": provider.provider,
        "api_format": provider.api_format,
        "protocol_profile": provider.protocol_profile,
        "client_identity_profile": provider.client_identity_profile,
        "base_url": provider.base_url,
        "proxy_url": provider.proxy_url,
        "model": model,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cached_result(entry: Any, *, now: datetime) -> CapabilityProbeResult | None:
    if not isinstance(entry, dict):
        return None
    status = str(entry.get("status") or "")
    checked_at = _parse_datetime(entry.get("checked_at"))
    if status not in {"supported", "unsupported", "unavailable"} or checked_at is None:
        return None
    ttl = {
        "supported": SUPPORTED_TTL,
        "unsupported": UNSUPPORTED_TTL,
        "unavailable": UNAVAILABLE_TTL,
    }[status]
    if checked_at + ttl <= now:
        return None
    error_type = str(entry.get("error_type") or "").strip() or None
    return CapabilityProbeResult(status, checked_at, error_type)


async def probe_model_tool_capability(
    provider: LLMProviderDTO,
    model: str,
) -> CapabilityProbeResult:
    """强制模型调用无副作用工具，并验证工具名与参数。"""

    now = datetime.now(UTC)
    request = ModelRequest(
        model=model,
        messages=(
            ModelMessage.text(
                MessageRole.SYSTEM,
                "这是协议能力检查。不要回答文本，只调用指定工具。",
            ),
            ModelMessage.text(
                MessageRole.USER,
                f"调用 {PROBE_TOOL_NAME}，参数 nonce 必须是 {PROBE_NONCE}。",
            ),
        ),
        tools=(
            ToolSpec(
                name=PROBE_TOOL_NAME,
                description="确认当前模型支持结构化工具调用。",
                parameters={
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            ),
        ),
        tool_choice=NamedToolChoice(PROBE_TOOL_NAME),
        max_output_tokens=64,
        metadata={"model_pinned": True, "max_retries_per_model": 0},
    )
    try:
        client = build_client_from_dto(
            provider,
            override_model=model,
            proxy_url=provider.proxy_url,
        )
        async with asyncio.timeout(45):
            response = await client.invoke(request)
    except Exception as exc:  # 上游临时故障不能永久判为不支持
        scope = getattr(exc, "scope", None)
        unsupported = isinstance(exc, (NotImplementedError, ValueError)) or (
            scope == LLMErrorScope.CAPABILITY_MISMATCH
        )
        return CapabilityProbeResult(
            "unsupported" if unsupported else "unavailable",
            now,
            type(exc).__name__,
        )
    matched = any(
        call.name == PROBE_TOOL_NAME and call.arguments.get("nonce") == PROBE_NONCE
        for call in response.tool_calls
    )
    return CapabilityProbeResult("supported" if matched else "unsupported", now)


def _dto_with_verified_models(
    provider: LLMProviderDTO,
    supported_models: list[str],
) -> LLMProviderDTO:
    existing = {
        str(item.get("id") or "").strip(): dict(item)
        for item in provider.models
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    models: list[dict[str, Any]] = []
    for model in supported_models:
        metadata = existing.get(model, {"id": model})
        metadata.update({"enabled": True, "supports_tools": True})
        models.append(metadata)
    return replace(provider, models=models)


def _dto_with_unavailable_models(
    provider: LLMProviderDTO,
    unavailable_models: list[str],
) -> LLMProviderDTO:
    """保留暂时不可探测的 fallback 模型，但不把它伪装成已验证支持。

    ``supports_tools=False`` 的显式配置仍会在 ``tools_models_for_dto`` 入口被
    排除；缺省能力沿用协议级声明。该 DTO 只会作为跨 Provider 候选，真正调用前
    仍受用户切换确认约束。
    """

    existing = {
        str(item.get("id") or "").strip(): dict(item)
        for item in provider.models
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    models: list[dict[str, Any]] = []
    for model in unavailable_models:
        metadata = existing.get(model, {"id": model})
        metadata["enabled"] = True
        models.append(metadata)
    return replace(provider, models=models)


async def verify_resolved_agent_providers(
    db: AsyncSession,
    resolved: ResolvedAgentProviders,
) -> ResolvedAgentProviders | str:
    """只保留经真实工具调用探测确认的 Provider/模型。"""

    row = await db.get(SystemSetting, CACHE_KEY)
    raw = row.value if row is not None and isinstance(row.value, dict) else {}
    entries = dict(raw.get("entries") or {}) if isinstance(raw, dict) else {}
    now = datetime.now(UTC)
    candidates: list[tuple[LLMProviderDTO, str, str]] = []
    results: dict[tuple[int, str], CapabilityProbeResult] = {}
    for provider in resolved.providers.values():
        explicit = resolved.model if provider.id == resolved.primary.id else None
        for model in tools_models_for_dto(provider, explicit):
            signature = _provider_signature(provider, model)
            cached = _cached_result(entries.get(signature), now=now)
            if cached is None:
                candidates.append((provider, model, signature))
            else:
                results[(provider.id, model)] = cached

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def run_probe(
        provider: LLMProviderDTO,
        model: str,
        signature: str,
    ) -> tuple[int, str, str, CapabilityProbeResult]:
        async with semaphore:
            result = await probe_model_tool_capability(provider, model)
        return provider.id, model, signature, result

    if candidates:
        probed = await asyncio.gather(
            *(run_probe(provider, model, signature) for provider, model, signature in candidates)
        )
        for provider_id, model, signature, result in probed:
            results[(provider_id, model)] = result
            entries[signature] = {
                "status": result.status,
                "checked_at": result.checked_at.isoformat(),
                "error_type": result.error_type,
            }
        ordered_entries = sorted(
            entries.items(),
            key=lambda item: str((item[1] or {}).get("checked_at") or ""),
            reverse=True,
        )[:MAX_CACHE_ENTRIES]
        value = {"version": 1, "entries": dict(ordered_entries)}
        if row is None:
            db.add(SystemSetting(key=CACHE_KEY, value=value))
        else:
            row.value = value
        await db.flush()

    verified: dict[int, LLMProviderDTO] = {}
    selected_model: str | None = None
    for provider in resolved.providers.values():
        explicit = resolved.model if provider.id == resolved.primary.id else None
        models = [
            model
            for model in tools_models_for_dto(provider, explicit)
            if results.get((provider.id, model), CapabilityProbeResult("unavailable", now)).supported
        ]
        if not models:
            # fallback 的临时网络故障不等于不支持 tools。保留为待用户确认的
            # Provider 切换候选；primary 仍必须有至少一个已验证模型。
            unavailable_models = [
                model
                for model in tools_models_for_dto(provider, explicit)
                if results.get(
                    (provider.id, model),
                    CapabilityProbeResult("unavailable", now),
                ).status
                == "unavailable"
            ]
            if provider.id != resolved.primary.id and unavailable_models:
                verified[provider.id] = _dto_with_unavailable_models(
                    provider,
                    unavailable_models,
                )
            continue
        verified[provider.id] = _dto_with_verified_models(provider, models)
        if provider.id == resolved.primary.id:
            selected_model = models[0]

    if selected_model is None or resolved.primary.id not in verified:
        primary_candidates = [
            result
            for (provider_id, _model), result in results.items()
            if provider_id == resolved.primary.id
        ]
        if any(result.status == "unavailable" for result in primary_candidates):
            return f"Provider「{resolved.primary.name}」的工具调用能力暂时无法验证，请稍后重试。"
        return f"Provider「{resolved.primary.name}」的候选模型不支持 Agent 工具调用。"
    return ResolvedAgentProviders(
        primary=verified[resolved.primary.id],
        model=selected_model,
        providers=verified,
    )


__all__ = [
    "CACHE_KEY",
    "CapabilityProbeResult",
    "probe_model_tool_capability",
    "verify_resolved_agent_providers",
]
