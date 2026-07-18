from __future__ import annotations

import re
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm_client import AnthropicClient, OpenAIClient, ResponsesClient
from app.services.llm_protocol import (
    MessageRole,
    ModelMessage,
    ModelRequest,
    NamedToolChoice,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    wire_tool_name,
)


class _Response:
    status_code = 200
    text = ""
    headers: dict[str, str] = {}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def _request(tool_name: str = "lookup") -> ModelRequest:
    return ModelRequest(
        model="model",
        messages=(
            ModelMessage.text(MessageRole.SYSTEM, "system"),
            ModelMessage.text(MessageRole.USER, "question"),
            ModelMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=(ToolCall("call-1", tool_name, {"id": 1}),),
            ),
            ModelMessage(
                role=MessageRole.TOOL,
                tool_results=(ToolResult("call-1", tool_name, {"value": "one"}),),
            ),
        ),
        tools=(
            ToolSpec(
                tool_name,
                "Lookup",
                {"type": "object", "properties": {"id": {"type": "integer"}}},
            ),
        ),
        tool_choice=NamedToolChoice(tool_name),
    )


@pytest.mark.asyncio
async def test_chat_adapter_round_trips_tool_calls() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(
        return_value=_Response(
            {
                "model": "model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "function": {"name": wire_name, "arguments": '{"id":2}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        response = await OpenAIClient("sk", "https://api.example/v1/chat/completions", "model").invoke(
            _request(internal_name)
        )

    body = fake.post.await_args.kwargs["json"]
    assert fake.post.await_args.args[0] == "https://api.example/v1/chat/completions"
    assert body["messages"][-1]["role"] == "tool"
    assert body["tools"][0]["function"]["name"] == wire_name
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", wire_name)
    assert body["tool_choice"]["function"]["name"] == wire_name
    assert body["messages"][-2]["tool_calls"][0]["function"]["name"] == wire_name
    assert body["messages"][-1]["name"] == wire_name
    assert response.tool_calls[0].name == internal_name
    assert response.tool_calls[0].arguments == {"id": 2}
    assert response.stop_reason is StopReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_chat_legacy_complete_accepts_array_content() -> None:
    """OpenAI 兼容端返回数组 content 时，legacy complete 也应拼接文本。"""
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(
        return_value=_Response(
            {
                "model": "model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": [
                                {"type": "text", "text": "first"},
                                {"type": "output_text", "text": "second"},
                            ]
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        result = await OpenAIClient("sk", "https://api.example/v1", "model").complete(
            "system", "question"
        )

    assert result.text == "first\nsecond"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_profile", ["standard", "claude_code_proxy"])
async def test_anthropic_profiles_map_reasoning_effort(protocol_profile: str) -> None:
    """两种 Anthropic 协议档都必须把 effort 写入 output_config。"""
    sent_bodies: list[dict] = []

    class _StreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            lines = (
                'event: message_start',
                'data: {"message":{"model":"claude","usage":{"input_tokens":1}}}',
                '',
                'event: content_block_delta',
                'data: {"delta":{"type":"text_delta","text":"ok"}}',
                '',
                'event: message_delta',
                'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
            )
            yield ("\n".join(lines) + "\n").encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            sent_bodies.append(kwargs["json"])
            return _StreamResponse()

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        result = await AnthropicClient(
            "sk",
            "https://api.anthropic.com/v1",
            "claude-sonnet-4-6",
            protocol_profile=protocol_profile,
        ).complete("system", "question", reasoning_effort="high")

    assert result.text == "ok"
    assert sent_bodies[0]["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_anthropic_complete_preserves_explicit_empty_refusal() -> None:
    class _StreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            lines = (
                "event: message_start",
                'data: {"message":{"model":"claude","usage":{"input_tokens":1}}}',
                "",
                "event: message_delta",
                'data: {"delta":{"stop_reason":"refusal"},"usage":{"output_tokens":0}}',
            )
            yield ("\n".join(lines) + "\n").encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            return _StreamResponse()

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        result = await AnthropicClient(
            "sk", "https://api.anthropic.com/v1", "claude"
        ).complete("system", "question")

    assert result.text == ""
    assert result.stop_reason is StopReason.REFUSAL


@pytest.mark.asyncio
async def test_responses_adapter_round_trips_function_call_output() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(
        return_value=_Response(
            {
                "model": "model",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-2",
                        "name": wire_name,
                        "arguments": '{"id":2}',
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        response = await ResponsesClient("sk", "https://api.example/v1/responses", "model").invoke(
            _request(internal_name)
        )

    body = fake.post.await_args.kwargs["json"]
    assert any(item.get("type") == "function_call_output" for item in body["input"])
    assert body["tools"][0]["name"] == wire_name
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", wire_name)
    assert body["tool_choice"]["name"] == wire_name
    assert any(
        item.get("type") == "function_call" and item.get("name") == wire_name
        for item in body["input"]
    )
    assert body["store"] is False
    assert response.tool_calls[0].id == "call-2"
    assert response.tool_calls[0].name == internal_name


@pytest.mark.asyncio
async def test_responses_adapter_keeps_dotted_history_safe_on_no_tools_turn() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(
        return_value=_Response(
            {
                "model": "model",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        )
    )
    request = replace(_request(internal_name), tools=())

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        response = await ResponsesClient("sk", "https://api.example/v1/responses", "model").invoke(
            request
        )

    body = fake.post.await_args.kwargs["json"]
    assert "tools" not in body
    assert any(
        item.get("type") == "function_call" and item.get("name") == wire_name
        for item in body["input"]
    )
    assert response.text == "done"


@pytest.mark.asyncio
async def test_responses_adapter_preserves_explicit_refusal_as_legal_empty() -> None:
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(
        return_value=_Response(
            {
                "model": "model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "policy"}],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 0},
            }
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        response = await ResponsesClient(
            "sk", "https://api.example/v1/responses", "model"
        ).invoke(_request())

    assert response.text == ""
    assert response.stop_reason is StopReason.REFUSAL


@pytest.mark.asyncio
async def test_anthropic_adapter_uses_standard_headers_and_tool_blocks() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    fake.post = AsyncMock(
        return_value=_Response(
            {
                "model": "claude",
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "call-2", "name": wire_name, "input": {"id": 2}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=fake):
        response = await AnthropicClient("sk", "https://api.anthropic.com/v1/messages", "claude").invoke(
            _request(internal_name)
        )

    body = fake.post.await_args.kwargs["json"]
    headers = fake.post.await_args.kwargs["headers"]
    assert "anthropic-beta" not in headers
    assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    assert body["tools"][0]["name"] == wire_name
    assert body["tool_choice"] == {"type": "tool", "name": wire_name}
    assert body["messages"][-2]["content"][0]["name"] == wire_name
    assert response.tool_calls[0].name == internal_name
    assert response.text == "checking"
    assert response.stop_reason is StopReason.TOOL_CALLS
