"""Provider protocol profiles and their executable compatibility facts.

``api_format`` selects the wire family.  A protocol profile narrows that family
to a provider dialect without conflating it with the client identity used in
request headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
    LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY,
    LLM_PROTOCOL_PROFILE_CODEX_RESPONSES,
    LLM_PROTOCOL_PROFILE_DEEPSEEK_RESPONSES,
    LLM_PROTOCOL_PROFILE_OPENAI_RESPONSES,
    LLM_PROTOCOL_PROFILE_STANDARD,
    normalize_protocol_profile,
)


@dataclass(frozen=True)
class ProviderProtocolProfile:
    """Facts that affect request planning for one provider dialect."""

    name: str
    api_formats: frozenset[str]
    recommended_identity: str
    request_defaults: tuple[tuple[str, Any], ...] = ()
    forbidden_request_fields: frozenset[str] = frozenset()
    hard_disabled_capabilities: frozenset[str] = frozenset()
    supports_store: bool = False
    supports_previous_response_id: bool = False
    include_reasoning_encrypted: bool = False
    reasoning_transport: str = "native"
    models_endpoint_suffixes: tuple[str, ...] = ("/models",)

    def defaults(self) -> dict[str, Any]:
        return dict(self.request_defaults)


_STANDARD_BY_FORMAT: dict[str, ProviderProtocolProfile] = {
    LLM_API_FORMAT_CHAT_COMPLETIONS: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_STANDARD,
        api_formats=frozenset({LLM_API_FORMAT_CHAT_COMPLETIONS}),
        recommended_identity="openai_sdk",
        # OpenAI-compatible chat implementations differ on whether assistant
        # reasoning history is accepted. Models must opt in through metadata.
        reasoning_transport="native",
    ),
    LLM_API_FORMAT_RESPONSES: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_STANDARD,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        recommended_identity="openai_sdk",
        supports_store=False,
        supports_previous_response_id=False,
        reasoning_transport="responses_item",
    ),
    LLM_API_FORMAT_ANTHROPIC_MESSAGES: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_STANDARD,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        recommended_identity="claude_code",
        reasoning_transport="anthropic_thinking",
    ),
}

_PROFILES: dict[str, ProviderProtocolProfile] = {
    LLM_PROTOCOL_PROFILE_OPENAI_RESPONSES: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_OPENAI_RESPONSES,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        recommended_identity="openai_sdk",
        request_defaults=(("store", False),),
        supports_store=True,
        supports_previous_response_id=True,
        reasoning_transport="responses_item",
    ),
    LLM_PROTOCOL_PROFILE_DEEPSEEK_RESPONSES: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_DEEPSEEK_RESPONSES,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        recommended_identity="openai_sdk",
        forbidden_request_fields=frozenset(
            {
                "conversation",
                "include",
                "previous_response_id",
                "store",
            }
        ),
        supports_store=False,
        supports_previous_response_id=False,
        reasoning_transport="responses_item",
        # DeepSeek 官方根地址与显式 /v1 配置在不同部署中均可见。
        models_endpoint_suffixes=("/models", "/v1/models"),
    ),
    LLM_PROTOCOL_PROFILE_CODEX_RESPONSES: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_CODEX_RESPONSES,
        api_formats=frozenset({LLM_API_FORMAT_RESPONSES}),
        recommended_identity="codex_tui",
        request_defaults=(("store", False),),
        supports_store=False,
        supports_previous_response_id=True,
        include_reasoning_encrypted=True,
        reasoning_transport="encrypted_reasoning_item",
    ),
    LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY: ProviderProtocolProfile(
        name=LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY,
        api_formats=frozenset({LLM_API_FORMAT_ANTHROPIC_MESSAGES}),
        recommended_identity="claude_code",
        reasoning_transport="anthropic_thinking",
    ),
}


_DEEPSEEK_RESPONSES_VISION_MODELS = frozenset(
    {
        "deepseek-v4-flash-vision-exp",
    }
)


def protocol_model_capability_overrides(
    profile_name: str,
    model: str,
) -> dict[str, Any]:
    """Return provider-dialect model facts not exposed by ``GET /models``.

    DeepSeek's model-list response currently provides identifiers only.  The
    Responses endpoint accepts ``input_image`` for every V4 model, but only the
    documented vision model actually processes the image; text-only models
    replace it with placeholder text.
    """

    if profile_name != LLM_PROTOCOL_PROFILE_DEEPSEEK_RESPONSES:
        return {}
    is_vision = str(model or "").strip().lower() in _DEEPSEEK_RESPONSES_VISION_MODELS
    return {
        "images": is_vision,
        "input_modalities": frozenset({"text", "image"} if is_vision else {"text"}),
    }


def infer_protocol_profile(
    api_format: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> str:
    """Suggest a dialect from strong endpoint/model evidence.

    Existing ``standard`` configurations are never rewritten.  Callers may opt
    into this inference for strong official-host compatibility at runtime.
    """

    if api_format != LLM_API_FORMAT_RESPONSES:
        return LLM_PROTOCOL_PROFILE_STANDARD
    host = (urlsplit(base_url or "").hostname or "").lower()
    if host == "api.deepseek.com":
        return LLM_PROTOCOL_PROFILE_DEEPSEEK_RESPONSES
    if host in {"api.openai.com", "chatgpt.com"}:
        return LLM_PROTOCOL_PROFILE_OPENAI_RESPONSES
    return LLM_PROTOCOL_PROFILE_STANDARD


def resolve_protocol_profile(
    api_format: str,
    configured_profile: str | None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    infer_when_standard: bool = False,
) -> ProviderProtocolProfile:
    """Resolve a configured profile, optionally inferring from strong evidence."""

    if api_format not in _STANDARD_BY_FORMAT:
        raise ValueError(f"未知 api_format: {api_format}")
    normalized = normalize_protocol_profile(api_format, configured_profile)
    if infer_when_standard and normalized == LLM_PROTOCOL_PROFILE_STANDARD:
        normalized = infer_protocol_profile(api_format, base_url=base_url, model=model)
    if normalized == LLM_PROTOCOL_PROFILE_STANDARD:
        return _STANDARD_BY_FORMAT[api_format]
    profile = _PROFILES.get(normalized)
    if profile is None or api_format not in profile.api_formats:
        return _STANDARD_BY_FORMAT[api_format]
    return profile


def protocol_profile_names_for_format(api_format: str) -> tuple[str, ...]:
    """Return stable UI/API choices for an API format."""

    names = [LLM_PROTOCOL_PROFILE_STANDARD]
    names.extend(
        profile.name
        for profile in _PROFILES.values()
        if api_format in profile.api_formats
    )
    return tuple(names)


__all__ = [
    "ProviderProtocolProfile",
    "infer_protocol_profile",
    "protocol_model_capability_overrides",
    "protocol_profile_names_for_format",
    "resolve_protocol_profile",
]
