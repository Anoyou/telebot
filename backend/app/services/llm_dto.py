"""LLM Provider DTO —— 统一 provider 传递，替代手搓 fake ORM row。

设计原则：
- 所有 LLM 调用路径统一使用 LLMProviderDTO，不再手搓 ORM mock 对象
- DTO 只包含数据字段，不包含业务逻辑
- 提供从 dict/ORM row 构造 DTO 的工厂函数

Fallback 优先级（从高到低）：
1. 显式 inline provider（用户 @provider 指定）
2. command/template configured provider
3. router fallback_provider_id
4. tag/capability 匹配且 cost_tier 更低的 provider
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db.models.command import (
    normalize_client_identity_profile,
    normalize_protocol_profile,
)

_REASONING_LEVEL_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


def _reasoning_levels(metadata: dict[str, Any]) -> tuple[tuple[str, ...], str | None]:
    raw = metadata.get("supported_reasoning_levels")
    if raw is None:
        raw = metadata.get("reasoning_efforts")
    if not isinstance(raw, list):
        return (), None
    declared = {str(item) for item in raw}
    levels = tuple(level for level in _REASONING_LEVEL_ORDER if level in declared)
    if not levels:
        return (), None
    explicit_default = str(metadata.get("default_reasoning_level") or "")
    if explicit_default in levels:
        return levels, explicit_default
    if "medium" in levels:
        return levels, "medium"
    return levels, levels[-1]


@dataclass
class LLMProviderDTO:
    """LLM Provider 数据传输对象。

    统一所有 LLM 调用路径的 provider 表示，不再手搓 ORM fake row。

    字段说明：
    - id: provider 数据库 ID
    - name: 友好名称（前端展示）
    - provider: 厂商类型（openai/anthropic/ollama）
    - api_format: API 协议格式（chat_completions/responses/anthropic_messages）
    - protocol_profile: Anthropic Messages 请求兼容档案
    - web_search_api_format: 联网搜索时的 API 协议覆盖（auto/responses/...）
    - base_url: API 端点 base URL
    - default_model: 默认模型名
    - api_key_enc: 加密后的 API key（仅内部使用，不打印）
    - proxy_url: 代理 URL（socks5/http/https）
    - modality: 能力模态（text/vision/audio/multimodal）
    - tags: 路由标签列表
    - cost_tier: 成本档（1=便宜/3=旗舰）
    - models: 候选模型清单（用于把模型 ID 映射为展示名）
    """

    id: int
    name: str
    provider: str
    api_format: str | None = None
    protocol_profile: str = "standard"
    client_identity_profile: str = "auto"
    execution_backend: str = "direct"
    web_search_api_format: str | None = None
    base_url: str | None = None
    default_model: str = ""
    api_key_enc: str | None = None
    request_headers_enc: str | None = None
    proxy_url: str | None = None
    modality: str = "text"
    tags: list[str] = field(default_factory=list)
    cost_tier: int = 2
    models: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """规范化字段类型。"""
        self.id = int(self.id)
        self.cost_tier = int(self.cost_tier)
        self.protocol_profile = normalize_protocol_profile(
            self.api_format,
            self.protocol_profile,
        )
        self.client_identity_profile = normalize_client_identity_profile(self.client_identity_profile)
        if self.tags is None:
            self.tags = []
        if self.models is None:
            self.models = []

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMProviderDTO:
        """从 dict（runtime ctx 中的 provider_dict）构造 DTO。"""
        return cls(
            id=int(d.get("id", 0)),
            name=str(d.get("name", "")),
            provider=str(d.get("provider", "")),
            api_format=d.get("api_format"),
            protocol_profile=str(d.get("protocol_profile", "standard") or "standard"),
            client_identity_profile=str(d.get("client_identity_profile", "auto") or "auto"),
            execution_backend=str(d.get("execution_backend", "direct") or "direct"),
            web_search_api_format=d.get("web_search_api_format"),
            base_url=d.get("base_url"),
            default_model=str(d.get("default_model", "") or ""),
            api_key_enc=d.get("api_key_enc"),
            request_headers_enc=d.get("request_headers_enc"),
            proxy_url=d.get("proxy_url"),
            modality=str(d.get("modality", "text") or "text"),
            tags=list(d.get("tags") or []),
            cost_tier=int(d.get("cost_tier", 2) or 2),
            models=[dict(m) for m in (d.get("models") or []) if isinstance(m, dict)],
        )

    @classmethod
    def from_orm_row(cls, row: Any) -> LLMProviderDTO:
        """从 ORM LLMProvider 行构造 DTO。"""
        return cls(
            id=int(row.id),
            name=str(row.name or ""),
            provider=str(row.provider or ""),
            api_format=getattr(row, "api_format", None),
            protocol_profile=str(getattr(row, "protocol_profile", "standard") or "standard"),
            client_identity_profile=str(getattr(row, "client_identity_profile", "auto") or "auto"),
            execution_backend=str(getattr(row, "execution_backend", "direct") or "direct"),
            web_search_api_format=getattr(row, "web_search_api_format", None),
            base_url=row.base_url,
            default_model=str(row.default_model or ""),
            api_key_enc=row.api_key_enc,
            request_headers_enc=getattr(row, "request_headers_enc", None),
            proxy_url=getattr(row, "proxy_url", None),
            modality=str(getattr(row, "modality", "text") or "text"),
            tags=list(getattr(row, "tags", []) or []),
            cost_tier=int(getattr(row, "cost_tier", 2) or 2),
            models=[dict(m) for m in (getattr(row, "models", None) or []) if isinstance(m, dict)],
        )

    def to_dict(self) -> dict[str, Any]:
        """转为脱敏展示 dict，不含任何加密凭据。"""
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "api_format": self.api_format,
            "protocol_profile": self.protocol_profile,
            "client_identity_profile": self.client_identity_profile,
            "execution_backend": self.execution_backend,
            "web_search_api_format": self.web_search_api_format,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "proxy_url": self.proxy_url,
            "modality": self.modality,
            "tags": self.tags,
            "cost_tier": self.cost_tier,
            "models": self.models,
            # 注意：不含 api_key_enc 明文
        }

    def to_runtime_dict(self) -> dict[str, Any]:
        """转为进程内运行时 dict，保留装配上游请求所需的加密字段。"""

        return {
            **self.to_dict(),
            "api_key_enc": self.api_key_enc,
            "request_headers_enc": self.request_headers_enc,
        }

    @property
    def is_ollama(self) -> bool:
        """是否是 ollama 本地部署。"""
        return self.provider.lower() == "ollama"

    @property
    def has_api_key(self) -> bool:
        """是否有 API key（ollama 本地部署例外，可不要 key）。"""
        if self.is_ollama:
            return True
        return bool(self.api_key_enc)

    def enabled_model_ids(self) -> list[str]:
        """严格返回 models[].enabled == True 的模型 id（顺序保留、去重）。"""
        out: list[str] = []
        seen: set[str] = set()
        for item in self.models or []:
            if not isinstance(item, dict) or not bool(item.get("enabled")):
                continue
            mid = str(item.get("id") or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    def has_model_list(self) -> bool:
        """是否声明了显式 models 清单（至少一条带 id）。"""
        return any(isinstance(m, dict) and str(m.get("id") or "").strip() for m in (self.models or []))

    def pick_enabled_model(self) -> str | None:
        """为该 provider 选一个可用模型（fallback 重选模型时用）。

        - 有 enabled 模型：default_model∈enabled 优先，否则第一个 enabled。
        - 有显式清单但全部禁用：返回 None（该 provider 无可用模型）。
        - 无显式清单（老配置）：回落 default_model。
        """
        enabled = self.enabled_model_ids()
        default_model = str(self.default_model or "").strip()
        if enabled:
            if default_model and default_model in enabled:
                return default_model
            return enabled[0]
        if self.has_model_list():
            return None
        return default_model or None

    def capabilities_for_model(self, model: str):
        """Merge protocol facts with optional per-model metadata.

        Model metadata may refine normal capabilities, but it cannot re-enable a
        capability that the selected protocol profile explicitly forbids.
        """

        from .llm_profiles import resolve_protocol_profile
        from .llm_protocol import capabilities_for_api_format

        api_format = str(self.api_format or "chat_completions")
        profile = resolve_protocol_profile(
            api_format,
            self.protocol_profile,
            base_url=self.base_url,
            model=model,
            infer_when_standard=True,
        )
        capabilities = capabilities_for_api_format(api_format)
        capabilities = capabilities.with_overrides(
            reasoning_transport=profile.reasoning_transport,
        )
        if api_format == "anthropic_messages":
            default_efforts = {"low", "medium", "high"}
            if "opus" in str(model or "").lower():
                default_efforts.add("max")
            capabilities = capabilities.with_overrides(
                reasoning=True,
                reasoning_efforts=frozenset(default_efforts),
            )
        metadata = next(
            (item for item in self.models if str(item.get("id") or "").strip() == str(model or "").strip()),
            None,
        )
        hard_disabled = {
            capability: False
            for capability in profile.hard_disabled_capabilities
            if capability
            in {"images", "tools", "parallel_tool_calls", "web_search", "temperature"}
        }
        if not metadata:
            return capabilities.with_overrides(**hard_disabled)
        overrides: dict[str, Any] = {}
        for key, capability_key in (
            ("supports_tools", "tools"),
            ("supports_images", "images"),
            ("supports_temperature", "temperature"),
            ("supports_parallel_tool_calls", "parallel_tool_calls"),
            ("supports_web_search", "web_search"),
        ):
            if isinstance(metadata.get(key), bool):
                overrides[capability_key] = metadata[key]
        for key in ("context_window", "max_output_tokens"):
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                overrides[key] = value
        for key in ("input_modalities", "output_modalities", "supported_api_formats"):
            values = metadata.get(key)
            if isinstance(values, list):
                overrides[key] = frozenset(str(item) for item in values if str(item))
        supported_formats = overrides.get("supported_api_formats")
        if isinstance(supported_formats, frozenset):
            overrides["protocol_compatible"] = api_format in supported_formats
        reasoning_transport = metadata.get("reasoning_transport")
        if (
            profile.name == "standard"
            and isinstance(reasoning_transport, str)
            and reasoning_transport
        ):
            overrides["reasoning_transport"] = reasoning_transport
        levels, default_level = _reasoning_levels(metadata)
        if isinstance(metadata.get("supported_reasoning_levels"), list) or isinstance(
            metadata.get("reasoning_efforts"), list
        ):
            normalized = frozenset(levels)
            overrides["reasoning"] = bool(normalized)
            overrides["reasoning_efforts"] = normalized
            overrides["default_reasoning_level"] = default_level
        capabilities = capabilities.with_overrides(**overrides)
        return capabilities.with_overrides(**hard_disabled)


def provider_to_dto(provider_dict: dict[str, Any]) -> LLMProviderDTO:
    """兼容别名：从 dict 构造 LLMProviderDTO。"""
    return LLMProviderDTO.from_dict(provider_dict)


__all__ = [
    "LLMProviderDTO",
    "provider_to_dto",
]
