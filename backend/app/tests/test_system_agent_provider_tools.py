from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.system_agent import provider_verify
from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import ActionKeepPendingError, PreparedAction, ToolRegistry
from app.services.system_agent.tools.providers import (
    list_providers,
    probe_and_add_preview,
    register,
    save_execute,
    save_precheck,
    save_preview,
    verify_execute,
    verify_precheck,
    verify_preview,
)


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


def test_probe_and_add_tool_is_a_confirmed_write_with_encrypted_secret() -> None:
    registry = ToolRegistry()
    register(registry)

    spec = next(item for item in registry.list_all() if item.name == "providers.probe_and_add")
    assert spec.read_only is False
    assert spec.secret_argument_names == ("api_key", "request_headers")
    assert spec.allow_secret_input is False
    assert spec.preview_handler is probe_and_add_preview
    assert spec.execute_handler is not None
    assert "测活成功" in spec.description
    assert spec.input_schema["required"] == ["base_url"]
    assert "request_headers" not in spec.input_schema["properties"]


@pytest.mark.parametrize(
    "handler",
    (
        probe_and_add_preview,
        save_preview,
        save_precheck,
        save_execute,
        verify_preview,
        verify_precheck,
        verify_execute,
    ),
)
@pytest.mark.asyncio
async def test_provider_tools_reject_undeclared_request_headers_at_runtime(handler) -> None:
    with pytest.raises(ValueError, match="不能通过 System Agent"):
        await handler(
            None,
            {
                "request_headers": [
                    {
                        "name": "X-Tenant-ID",
                        "value": "opaque-header-secret",
                        "scopes": ["inference"],
                    }
                ]
            },
        )


@pytest.mark.asyncio
async def test_probe_and_add_discovers_model_and_prepares_confirmation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_verify(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {
            "ok": True,
            "model": "upstream-model-alias",
            "requested_model": "chat-model",
            "latency_ms": 123,
            "api_format": "chat_completions",
            "base_url": "https://api.example/v1",
            "provider": "openai",
            "suggested_name": "api.example",
            "response_preview": "sk-secret-value",
        }

    monkeypatch.setattr(provider_verify, "run_quick_verify", fake_verify)
    args = {
        "base_url": "https://api.example/v1",
        "api_key": "sk-secret-value",
    }

    prepared = await probe_and_add_preview(
        ToolContext(db=AsyncMock(), channel="web", role="admin"),
        args,
    )

    assert captured["api_key"] == "sk-secret-value"
    assert captured["default_model"] is None
    assert args == {
        "base_url": "https://api.example/v1",
        "api_key": "sk-secret-value",
    }
    assert isinstance(prepared, PreparedAction)
    assert prepared.arguments == {
        "base_url": "https://api.example/v1",
        "api_key": "sk-secret-value",
        "name": "api.example",
        "provider": "openai",
        "default_model": "chat-model",
            "api_format": "chat_completions",
            "models": [{"id": "chat-model", "enabled": True, "custom": False}],
        }
    preview = prepared.preview
    assert preview["mode"] == "verified_create"
    assert preview["provider"]["default_model"] == "chat-model"
    assert preview["liveness"]["ok"] is True
    assert "尚未保存" in preview["note"]
    assert "sk-secret-value" not in str(preview)


@pytest.mark.asyncio
async def test_probe_and_add_failure_does_not_prepare_confirmation(monkeypatch) -> None:
    async def fake_verify(**_kwargs):  # noqa: ANN003
        raise ActionKeepPendingError(
            "Provider 验证失败：上游不可用。临时密钥仍安全暂存。",
            code="PROVIDER_VERIFY_FAILED",
        )

    monkeypatch.setattr(provider_verify, "run_quick_verify", fake_verify)
    args = {
        "base_url": "https://api.example/v1",
        "api_key": "sk-secret-value",
    }

    with pytest.raises(ActionKeepPendingError):
        await probe_and_add_preview(
            ToolContext(db=AsyncMock(), channel="web", role="admin"),
            args,
        )

    assert "name" not in args
    assert "default_model" not in args


@pytest.mark.asyncio
async def test_probe_and_add_rejects_upstream_model_field_that_echoes_key(monkeypatch) -> None:
    key = "AbCdEfGhIjKlMnOpQrStUvWxYz123456"

    async def fake_verify(**_kwargs):  # noqa: ANN003
        return {
            "ok": True,
            "model": key,
            "requested_model": key,
            "latency_ms": 10,
            "api_format": "chat_completions",
            "base_url": "https://api.example/v1",
            "provider": "openai",
            "suggested_name": "api.example",
            "response_preview": key,
        }

    monkeypatch.setattr(provider_verify, "run_quick_verify", fake_verify)

    with pytest.raises(ValueError, match="公开配置字段包含当前凭据"):
        await probe_and_add_preview(
            ToolContext(db=AsyncMock(), channel="web", role="admin"),
            {"base_url": "https://api.example/v1", "api_key": key},
        )


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
    if expected_code == "API_KEY_REJECTED":
        assert ei.value.clear_secret_names == ("api_key",)
    else:
        assert ei.value.clear_secret_names == ()
    assert expected_message in ei.value.message
    assert "已保存的 Provider 配置未修改" in ei.value.message
