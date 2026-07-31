"""Pure Anthropic Messages wire normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..llm_protocol import ModelUsage


def usage_from_anthropic(data: Mapping[str, Any] | None) -> ModelUsage:
    usage = data if isinstance(data, Mapping) else {}
    return ModelUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
    )


__all__ = ["usage_from_anthropic"]
