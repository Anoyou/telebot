"""Pure Chat Completions wire normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..llm_protocol import ModelUsage


def usage_from_chat(data: Mapping[str, Any] | None) -> ModelUsage:
    usage = data if isinstance(data, Mapping) else {}
    details = usage.get("completion_tokens_details")
    if not isinstance(details, Mapping):
        details = {}
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    return ModelUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read_tokens=int(prompt_details.get("cached_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


__all__ = ["usage_from_chat"]
