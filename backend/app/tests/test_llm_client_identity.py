"""阶段 A：客户端身份基础层测试。

覆盖：
- 三协议 auto 身份映射（chat→openai_sdk / responses→codex_cli / anthropic→claude_code）。
- 身份依据"本次实际协议"解析：api_format_override / 联网搜索切协议后身份重算。
- minimal 不附加任何产品模拟头。
- 全局断言所有 LLM 请求不再发送 TelePilot 产品 UA。
- 固定身份与协议兼容性校验；未验证 Desktop 档案不可选、解析时回落。
- 敏感信息隔离：身份摘要不含 api_key / base_url。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
    LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY,
    normalize_client_identity_profile,
)
from app.services import llm_identity
from app.services.llm_client import (
    AnthropicClient,
    OpenAIClient,
    ResponsesClient,
    build_client_from_dto,
)
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_identity import (
    CLIENT_IDENTITY_CLAUDE_CODE,
    CLIENT_IDENTITY_CODEX_CLI,
    CLIENT_IDENTITY_GROK_CLI,
    CLIENT_IDENTITY_MINIMAL,
    CLIENT_IDENTITY_OPENAI_SDK,
    default_identity_for_format,
    is_identity_compatible,
    resolve_identity,
    selectable_identities,
)


class _Response:
    status_code = 200
    text = ""
    headers: dict[str, str] = {}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def _chat_response() -> _Response:
    return _Response(
        {
            "model": "model",
            "choices": [
                {"finish_reason": "stop", "message": {"content": "ok"}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def _responses_response() -> _Response:
    return _Response(
        {
            "model": "model",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


# ── 身份解析表驱动 ────────────────────────────────────────────


def test_auto_identity_maps_by_effective_format() -> None:
    assert default_identity_for_format(LLM_API_FORMAT_CHAT_COMPLETIONS) == CLIENT_IDENTITY_OPENAI_SDK
    assert default_identity_for_format(LLM_API_FORMAT_RESPONSES) == CLIENT_IDENTITY_CODEX_CLI
    assert default_identity_for_format(LLM_API_FORMAT_ANTHROPIC_MESSAGES) == CLIENT_IDENTITY_CLAUDE_CODE


def test_resolve_auto_uses_effective_format_not_provider_default() -> None:
    # Provider 默认 chat_completions，但本次实际协议是 responses（联网搜索切协议）。
    identity = resolve_identity("auto", LLM_API_FORMAT_RESPONSES)
    assert identity.profile == CLIENT_IDENTITY_CODEX_CLI


def test_fixed_identity_incompatible_falls_back_to_auto_for_format() -> None:
    # 固定 openai_sdk（只支持 chat/responses），本次协议是 anthropic → 回落 claude_code。
    identity = resolve_identity(CLIENT_IDENTITY_OPENAI_SDK, LLM_API_FORMAT_ANTHROPIC_MESSAGES)
    assert identity.profile == CLIENT_IDENTITY_CLAUDE_CODE


def test_minimal_identity_has_no_product_headers() -> None:
    identity = resolve_identity(CLIENT_IDENTITY_MINIMAL, LLM_API_FORMAT_CHAT_COMPLETIONS)
    assert identity.profile == CLIENT_IDENTITY_MINIMAL
    assert identity.headers() == {}


def test_unverified_desktop_profile_not_selectable_and_falls_back() -> None:
    items = {item["profile"]: item for item in selectable_identities()}
    # codex_desktop 已有真实抓包证据 → 可选；claude_desktop 仍无证据 → 不可选。
    assert items["codex_desktop"]["selectable"] is True
    assert items["claude_desktop"]["selectable"] is False
    # 未验证档案解析时回落到该协议 auto 默认身份，绝不发送未验证头。
    identity = resolve_identity("claude_desktop", LLM_API_FORMAT_ANTHROPIC_MESSAGES)
    assert identity.profile == CLIENT_IDENTITY_CLAUDE_CODE
    assert identity.verified is True


def test_identity_compat_matrix() -> None:
    assert is_identity_compatible("auto", LLM_API_FORMAT_RESPONSES) is True
    assert is_identity_compatible("minimal", LLM_API_FORMAT_ANTHROPIC_MESSAGES) is True
    assert is_identity_compatible("codex_cli", LLM_API_FORMAT_RESPONSES) is True
    assert is_identity_compatible("codex_cli", LLM_API_FORMAT_CHAT_COMPLETIONS) is False
    assert is_identity_compatible("claude_code", LLM_API_FORMAT_ANTHROPIC_MESSAGES) is True
    assert is_identity_compatible("claude_code", LLM_API_FORMAT_RESPONSES) is False
    assert is_identity_compatible("grok_cli", LLM_API_FORMAT_RESPONSES) is True
    assert is_identity_compatible("grok_cli", LLM_API_FORMAT_CHAT_COMPLETIONS) is False


def test_normalize_unknown_identity_degrades_to_auto() -> None:
    assert normalize_client_identity_profile("bogus") == "auto"
    assert normalize_client_identity_profile(None) == "auto"
    assert normalize_client_identity_profile("codex_cli") == "codex_cli"


def test_identity_summary_has_no_secrets() -> None:
    for item in selectable_identities():
        keys = set(item.keys())
        assert "api_key" not in keys
        assert "base_url" not in keys
        assert "api_key_enc" not in keys


# ── 三协议 Client 请求头 fixture ──────────────────────────────


def _dto(**kw: Any) -> LLMProviderDTO:
    base = {
        "id": 1,
        "name": "p",
        "provider": "openai",
        "api_format": LLM_API_FORMAT_CHAT_COMPLETIONS,
        "default_model": "model",
        "api_key_enc": None,
    }
    base.update(kw)
    return LLMProviderDTO.from_dict(base)


@pytest.mark.asyncio
async def test_chat_client_sends_openai_sdk_ua_not_telepilot() -> None:
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_chat_response())
    identity = resolve_identity("auto", LLM_API_FORMAT_CHAT_COMPLETIONS)
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        await OpenAIClient("sk", "https://api.example/v1", "model", identity=identity).complete(
            "system", "hello"
        )
    headers = fake.post.await_args.kwargs["headers"]
    ua = headers.get("User-Agent", "")
    assert "TelePilot" not in ua
    assert ua.startswith("OpenAI/Python")


@pytest.mark.asyncio
async def test_responses_client_sends_codex_identity() -> None:
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_responses_response())
    identity = resolve_identity("auto", LLM_API_FORMAT_RESPONSES)
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        await ResponsesClient("sk", "https://api.example/v1", "model", identity=identity).complete(
            "system", "hello"
        )
    headers = fake.post.await_args.kwargs["headers"]
    assert "TelePilot" not in headers.get("User-Agent", "")
    assert headers.get("User-Agent", "").startswith("codex_cli_rs/")
    assert headers.get("originator") == "codex_cli_rs"


@pytest.mark.asyncio
async def test_responses_client_sends_minimal_grok_cli_identity() -> None:
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(return_value=_responses_response())
    identity = resolve_identity(CLIENT_IDENTITY_GROK_CLI, LLM_API_FORMAT_RESPONSES)
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        await ResponsesClient("sk", "https://api.example/v1", "model", identity=identity).complete(
            "system", "hello"
        )
    headers = fake.post.await_args.kwargs["headers"]
    assert headers.get("User-Agent") == "grok-cli/0.2.93"
    assert headers.get("x-grok-client-version") == "0.2.93"
    for forbidden in ("authorization", "x-xai-token-auth", "x-grok-conv-id"):
        assert forbidden not in {key.lower() for key in identity.extra_headers}


@pytest.mark.asyncio
async def test_anthropic_client_sends_claude_code_identity_and_no_telepilot() -> None:
    sent: dict[str, Any] = {}

    class _Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            lines = (
                "event: message_start",
                'data: {"message":{"model":"claude","usage":{"input_tokens":1}}}',
                "",
                "event: content_block_delta",
                'data: {"delta":{"type":"text_delta","text":"ok"}}',
                "",
                "event: message_delta",
                'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
            )
            yield ("\n".join(lines) + "\n").encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):  # noqa: A002
            sent["headers"] = headers
            return _Stream()

    identity = resolve_identity("auto", LLM_API_FORMAT_ANTHROPIC_MESSAGES)
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        await AnthropicClient(
            "sk", "https://api.anthropic.com/v1", "claude", identity=identity
        ).complete("system", "hello")
    headers = sent["headers"]
    assert "TelePilot" not in headers.get("User-Agent", "")
    assert headers.get("User-Agent", "").startswith("claude-cli/")
    assert headers.get("x-app") == "cli"
    # 协议必需头仍在。
    assert headers.get("anthropic-version") == "2023-06-01"
    # 身份不触发 beta（protocol_profile=standard）。
    assert "anthropic-beta" not in headers


@pytest.mark.asyncio
async def test_anthropic_claude_code_proxy_keeps_beta_independent_of_identity() -> None:
    sent: dict[str, Any] = {}

    class _Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            lines = (
                "event: message_start",
                'data: {"message":{"model":"claude","usage":{"input_tokens":1}}}',
                "",
                "event: content_block_delta",
                'data: {"delta":{"type":"text_delta","text":"ok"}}',
                "",
                "event: message_delta",
                'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
            )
            yield ("\n".join(lines) + "\n").encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):  # noqa: A002
            sent["headers"] = headers
            return _Stream()

    identity = resolve_identity("auto", LLM_API_FORMAT_ANTHROPIC_MESSAGES)
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        await AnthropicClient(
            "sk",
            "https://proxy.example/v1",
            "claude",
            protocol_profile=LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY,
            identity=identity,
        ).complete("system", "hello")
    headers = sent["headers"]
    # protocol_profile 控制 beta，与身份独立。
    assert "anthropic-beta" in headers


# ── build_client_from_dto 依据 override 重算身份 ──────────────


def test_build_client_recomputes_identity_after_api_format_override() -> None:
    # Provider 默认 chat_completions（→openai_sdk），但本次 override 到 responses。
    dto = _dto(client_identity_profile="auto")
    client = build_client_from_dto(dto, api_format_override=LLM_API_FORMAT_RESPONSES)
    assert isinstance(client, ResponsesClient)
    assert client._identity.profile == CLIENT_IDENTITY_CODEX_CLI


def test_build_client_uses_provider_default_format_identity() -> None:
    dto = _dto(client_identity_profile="auto")
    client = build_client_from_dto(dto)
    assert isinstance(client, OpenAIClient)
    assert client._identity.profile == CLIENT_IDENTITY_OPENAI_SDK


def test_dto_normalizes_unknown_identity() -> None:
    dto = _dto(client_identity_profile="bogus")
    assert dto.client_identity_profile == "auto"


def test_catalog_source_documents_evidence() -> None:
    codex = llm_identity.get_identity(CLIENT_IDENTITY_CODEX_CLI)
    assert codex is not None
    assert codex.verified is True
    assert "codex" in codex.source.lower()
    assert codex.user_agent and codex.user_agent.startswith("codex_cli_rs/")


def test_no_telepilot_user_agent_constant_exists() -> None:
    # 全局断言：llm_client 模块不再定义 TelePilot 产品 UA 常量，
    # 且源码中不再出现 TelePilot 产品 UA 字面量。
    import app.services.llm_client as mod

    assert not hasattr(mod, "_LLM_USER_AGENT")
    src_path = Path(mod.__file__)
    text = src_path.read_text(encoding="utf-8")
    assert "TelePilot/" not in text
