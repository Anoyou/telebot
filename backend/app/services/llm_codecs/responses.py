"""Pure Responses API request planning and usage normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..llm_profiles import ProviderProtocolProfile
from ..llm_protocol import ModelUsage


def plan_responses_body(
    body: Mapping[str, Any],
    profile: ProviderProtocolProfile,
) -> dict[str, Any]:
    planned = dict(body)
    for key, value in profile.defaults().items():
        planned.setdefault(key, value)
    for key in profile.forbidden_request_fields:
        planned.pop(key, None)
    if profile.include_reasoning_encrypted and "reasoning" in planned:
        include = [
            str(item)
            for item in (planned.get("include") or [])
            if isinstance(item, str)
        ]
        if "reasoning.encrypted_content" not in include:
            include.append("reasoning.encrypted_content")
        planned["include"] = include
    return planned


def usage_from_responses(data: Mapping[str, Any] | None) -> ModelUsage:
    usage = data if isinstance(data, Mapping) else {}
    output_details = (
        usage.get("output_tokens_details")
        or usage.get("completion_tokens_details")
        or {}
    )
    if not isinstance(output_details, Mapping):
        output_details = {}
    input_details = (
        usage.get("input_tokens_details")
        or usage.get("prompt_tokens_details")
        or {}
    )
    if not isinstance(input_details, Mapping):
        input_details = {}
    return ModelUsage(
        input_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        output_tokens=int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        ),
        cache_read_tokens=int(
            usage.get("cache_read_input_tokens")
            or input_details.get("cached_tokens")
            or 0
        ),
        cache_write_tokens=int(
            usage.get("cache_creation_input_tokens")
            or input_details.get("cache_write_tokens")
            or usage.get("cache_write_tokens")
            or 0
        ),
        reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
    )


__all__ = ["plan_responses_body", "usage_from_responses"]
