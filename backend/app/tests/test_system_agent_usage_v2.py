"""usage schema_version=2 与 model_selection 归一化。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.llm_protocol import ModelUsage
from app.services.system_agent.runtime import (
    _normalize_model_selection,
    _usage_payload,
)


def _provider(pid: int = 1, name: str = "p1", fmt: str = "responses") -> SimpleNamespace:
    return SimpleNamespace(
        id=pid,
        name=name,
        api_format=fmt,
        has_api_key=True,
        default_model="m1",
    )


def test_usage_payload_schema_v2_splits_tool_counts() -> None:
    usage = ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14)
    provider = _provider()
    payload = _usage_payload(
        usage,
        provider,  # type: ignore[arg-type]
        "m1",
        requested_provider=_provider(2, "p2"),  # type: ignore[arg-type]
        requested_model="m-req",
        selection_mode="pinned",
        tool_calls=3,
        available_tools=12,
        used_fallback=True,
        stream_fallback=False,
        route_domains=["logs"],
        stage_timings={
            "verify_ms": 12,
            "route_ms": 3,
            "first_token_ms": 840,
            "total_ms": 2100,
            "noise": "drop",
        },
    )
    assert payload["schema_version"] == 2
    assert payload["tool_calls"] == 3
    assert payload["available_tools"] == 12
    assert payload["tool_count"] == 12  # 兼容旧键=暴露数
    assert payload["selection_mode"] == "pinned"
    assert payload["requested_provider_id"] == 2
    assert payload["requested_model"] == "m-req"
    assert payload["provider_id"] == 1
    assert payload["api_format"] == "responses"
    assert payload["used_fallback"] is True
    assert payload["route_domains"] == ["logs"]
    assert payload["stage_timings"] == {
        "verify_ms": 12,
        "route_ms": 3,
        "first_token_ms": 840,
        "total_ms": 2100,
    }
    assert payload["elapsed_ms"] == 2100


def test_usage_payload_stage_timings_nulls_and_elapsed_override() -> None:
    usage = ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    payload = _usage_payload(
        usage,
        _provider(),  # type: ignore[arg-type]
        "m1",
        stage_timings={"verify_ms": 0, "route_ms": None, "first_token_ms": "bad", "total_ms": 9},
        elapsed_ms=99,
    )
    assert payload["stage_timings"] == {
        "verify_ms": 0,
        "route_ms": None,
        "first_token_ms": None,
        "total_ms": 9,
    }
    assert payload["elapsed_ms"] == 99


def test_normalize_model_selection() -> None:
    assert _normalize_model_selection(None) == {"mode": "auto"}
    assert _normalize_model_selection({"mode": "auto"}) == {"mode": "auto"}
    assert _normalize_model_selection({"mode": "pinned", "provider_id": 3, "model": "x"}) == {
        "mode": "pinned",
        "provider_id": 3,
        "model": "x",
    }
    assert _normalize_model_selection({"mode": "pinned", "provider_id": "bad"}) == {"mode": "auto"}
    assert _normalize_model_selection({"mode": "pinned", "provider_id": 1, "model": ""}) == {
        "mode": "auto"
    }
