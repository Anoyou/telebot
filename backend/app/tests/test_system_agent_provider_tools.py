from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.system_agent import provider_verify
from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import ActionKeepPendingError, ToolRegistry
from app.services.system_agent.tools.providers import list_providers, register


def _empty_result() -> SimpleNamespace:
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


@pytest.mark.asyncio
async def test_list_providers_always_lists_all_without_filters() -> None:
    db = AsyncMock()
    db.execute.return_value = _empty_result()

    await list_providers(
        ToolContext(db=db, channel="web", role="viewer"),
        {"id": 23, "name": "metapi-gpt", "limit": 100},
    )

    statement = db.execute.await_args.args[0]
    assert len(statement._where_criteria) == 0


def test_provider_list_schema_only_accepts_optional_limit() -> None:
    registry = ToolRegistry()
    register(registry)

    spec = next(item for item in registry.list_all() if item.name == "providers.list")
    assert spec.input_schema["properties"] == {"limit": {"type": "integer"}}
    assert "required" not in spec.input_schema
    assert "全部模型提供商" in spec.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_error", "expected_code", "expected_message"),
    (
        (
            'Responses streaming 接口返回 503: {"error":{"code":"model_not_found"}}',
            "PROVIDER_VERIFY_FAILED",
            "无需重新输入 API Key",
        ),
        (
            "Responses streaming 接口返回 401: invalid api key",
            "API_KEY_REJECTED",
            "如需更换密钥，请重新输入",
        ),
    ),
)
async def test_saved_provider_verify_distinguishes_auth_from_upstream_failure(
    monkeypatch,
    upstream_error: str,
    expected_code: str,
    expected_message: str,
) -> None:
    async def fake_events(**kwargs):  # noqa: ANN003
        yield {"type": "error", "ok": False, "error": upstream_error}

    monkeypatch.setattr(provider_verify.llm_quick_verify, "quick_verify_events", fake_events)
    monkeypatch.setattr(
        provider_verify.llm_quick_verify,
        "normalize_quick_verify_base_url",
        lambda value: value,
    )

    with pytest.raises(ActionKeepPendingError) as ei:
        await provider_verify.run_quick_verify(
            base_url="https://api.example/v1",
            api_key="sk-secret",
            api_format="responses",
            default_model="deepseek-chat",
            provider="openai",
            using_saved_key=True,
        )

    assert ei.value.code == expected_code
    assert expected_message in ei.value.message
    assert "已保存的 Provider 配置未修改" in ei.value.message
