from __future__ import annotations

import json
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


class _StreamingResponse:
    status_code = 200
    text = ""

    def __init__(self, chunks: list[bytes], *, content_type: str = "text/event-stream") -> None:
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _StreamingClient:
    def __init__(self, response: _StreamingResponse) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return self.response


def _sse_chunks(*events: dict | str) -> list[bytes]:
    raw = "".join(
        f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n"
        for event in events
    ).encode()
    # 刻意打散到 JSON、UTF-8 和行边界之外，验证底层按字节安全重组。
    return [raw[:11], raw[11:37], raw[37:73], raw[73:]]


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
                    '',
                    'event: message_stop',
                    'data: {"type":"message_stop"}',
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
async def test_responses_accepts_reasoning_summary_without_output_text() -> None:
    """Responses 仅有 reasoning 项时，应兜底为可见正文。"""

    class _Response:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json"}

        def json(self) -> dict:
            return {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "proxy-reasoner",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "先分析再回答：答案是 7。"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 5},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        result = await ResponsesClient(
            "sk",
            "https://example.invalid/v1",
            "proxy-reasoner",
        ).complete("system", "3+4?")

    assert "7" in result.text


@pytest.mark.asyncio
async def test_responses_coerces_chat_completions_payload() -> None:
    """中转误回 Chat Completions 形态时，Responses 客户端仍应解出正文。"""

    class _Response:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json"}

        def json(self) -> dict:
            return {
                "model": "kimi-k3",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "因为 2+2=4。",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 4},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        result = await ResponsesClient(
            "sk",
            "https://api.moonshot.cn/v1",
            "kimi-k3",
        ).complete("system", "2+2?")

    assert "4" in result.text


@pytest.mark.asyncio
async def test_openai_complete_uses_reasoning_content_when_content_empty() -> None:
    """Kimi K3 / 智谱等：正文可能在 reasoning_content，content 为空。"""

    class _Response:
        status_code = 200
        text = ""
        headers: dict[str, str] = {}

        def json(self) -> dict:
            return {
                "model": "kimi-k3",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "先算一下：1+1=2。",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 8},
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        result = await OpenAIClient(
            "sk",
            "https://api.moonshot.cn/v1",
            "kimi-k3",
        ).complete("system", "1+1?")

    assert "1+1=2" in result.text


@pytest.mark.asyncio
async def test_openai_stream_invoke_yields_reasoning_before_content() -> None:
    class _StreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            lines = (
                'data: {"model":"glm-5","choices":[{"delta":{"reasoning_content":"思考中…"}}]}',
                "",
                'data: {"model":"glm-5","choices":[{"delta":{"content":"你好"}}]}',
                "",
                'data: {"model":"glm-5","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":3}}',
                "",
                "data: [DONE]",
                "",
            )
            yield ("\n".join(lines) + "\n").encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            return _StreamResponse()

    deltas: list[str] = []
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        async for event in OpenAIClient(
            "sk",
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-5",
        ).stream_invoke(
            ModelRequest(
                model="glm-5",
                messages=(ModelMessage.text(MessageRole.USER, "hi"),),
            )
        ):
            if event.delta:
                deltas.append(event.delta)

    assert deltas == ["思考中…", "你好"]


@pytest.mark.asyncio
async def test_anthropic_complete_accepts_thinking_delta_without_text_delta() -> None:
    """DeepSeek V4 等推理模型在 Anthropic 兼容流上可能只推 thinking_delta。"""

    class _StreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            lines = (
                "event: message_start",
                'data: {"type":"message_start","message":{"model":"deepseek-v4-pro","usage":{"input_tokens":3}}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"先想一下："}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"答案是 42"}}',
                "",
                "event: message_delta",
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8}}',
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
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
            "sk",
            "https://api.deepseek.com/anthropic",
            "deepseek-v4-pro",
        ).complete("system", "1+1?")

    assert "42" in result.text
    assert result.model == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_anthropic_stream_complete_yields_thinking_deltas() -> None:
    class _StreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            lines = (
                "event: message_start",
                'data: {"type":"message_start","message":{"model":"deepseek-v4-pro","usage":{"input_tokens":1}}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"你好"}}',
                "",
                "event: message_delta",
                'data: {"type":"message_delta","usage":{"output_tokens":2}}',
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
            )
            yield ("\n".join(lines) + "\n").encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            return _StreamResponse()

    chunks: list[str] = []
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_Client()):
        async for chunk in AnthropicClient(
            "sk",
            "https://api.deepseek.com/anthropic",
            "deepseek-v4-pro",
        ).stream_complete("system", "hi"):
            if chunk.delta:
                chunks.append(chunk.delta)

    assert "".join(chunks) == "你好"


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
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
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


@pytest.mark.asyncio
async def test_chat_stream_invoke_emits_real_deltas_and_joins_tool_arguments() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    response = _StreamingResponse(
        _sse_chunks(
            {
                "model": "model-live",
                "choices": [{"delta": {"content": "你"}, "finish_reason": None}],
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": "好",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-live",
                                    "function": {"name": wire_name, "arguments": '{"id":'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "2}"}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
            "[DONE]",
        )
    )
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        events = [
            event
            async for event in OpenAIClient(
                "sk", "https://api.example/v1", "model"
            ).stream_invoke(_request(internal_name))
        ]

    assert [event.delta for event in events if event.delta] == ["你", "好"]
    terminal = events[-1].response
    assert terminal is not None
    assert terminal.text == "你好"
    assert terminal.model == "model-live"
    assert terminal.usage.total_tokens == 7
    assert terminal.tool_calls == (
        ToolCall("call-live", internal_name, {"id": 2}),
    )
    assert terminal.stop_reason is StopReason.TOOL_CALLS
    assert terminal.stream_fallback is False


@pytest.mark.asyncio
async def test_anthropic_stream_invoke_emits_real_deltas_and_joins_tool_input() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    events_payload = [
        {"type": "message_start", "message": {"model": "claude-live", "usage": {"input_tokens": 4}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "call-live", "name": wire_name},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"id":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "2}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    response = _StreamingResponse(_sse_chunks(*events_payload))
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        events = [
            event
            async for event in AnthropicClient(
                "sk", "https://api.anthropic.com/v1", "claude"
            ).stream_invoke(_request(internal_name))
        ]

    assert [event.delta for event in events if event.delta] == ["你"]
    terminal = events[-1].response
    assert terminal is not None
    assert terminal.text == "你"
    assert terminal.model == "claude-live"
    assert terminal.usage.total_tokens == 7
    assert terminal.tool_calls == (
        ToolCall("call-live", internal_name, {"id": 2}),
    )
    assert terminal.stop_reason is StopReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_responses_stream_preserves_split_function_arguments_on_done() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    response = _StreamingResponse(
        _sse_chunks(
            {"type": "response.created", "response": {"model": "responses-live", "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "你"},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": "call-live", "name": wire_name},
            },
            {"type": "response.function_call_arguments.delta", "call_id": "call-live", "delta": '{"id":'},
            {"type": "response.function_call_arguments.delta", "call_id": "call-live", "delta": "2}"},
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "call_id": "call-live", "name": wire_name},
            },
            {
                "type": "response.completed",
                "response": {
                    "model": "responses-live",
                    "status": "completed",
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                },
            },
        )
    )
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        events = [
            event
            async for event in ResponsesClient(
                "sk", "https://api.example/v1", "model"
            ).stream_invoke(_request(internal_name))
        ]

    assert [event.delta for event in events if event.delta] == ["你"]
    terminal = events[-1].response
    assert terminal is not None
    assert terminal.text == "你"
    assert terminal.usage.total_tokens == 7
    assert terminal.tool_calls == (
        ToolCall("call-live", internal_name, {"id": 2}),
    )
    assert terminal.stop_reason is StopReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_responses_stream_maps_item_id_arguments_to_call_id() -> None:
    internal_name = "interaction.list_rules"
    wire_name = wire_tool_name(internal_name)
    response = _StreamingResponse(
        _sse_chunks(
            {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": wire_name}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"id": 2}'},
            {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": wire_name}},
            {"type": "response.completed", "response": {"model": "model", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}}},
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        events = [event async for event in ResponsesClient("sk", "https://api.example/v1", "model").stream_invoke(_request(internal_name))]

    terminal = events[-1].response
    assert terminal is not None
    assert terminal.tool_calls == (ToolCall("call_1", internal_name, {"id": 2}),)


@pytest.mark.asyncio
async def test_chat_stream_preserves_refusal_as_terminal_reason() -> None:
    response = _StreamingResponse(
        _sse_chunks(
            {"choices": [{"delta": {"refusal": "不能回答"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            "[DONE]",
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        events = [event async for event in OpenAIClient("sk", "https://api.example/v1", "model").stream_invoke(_request())]

    terminal = events[-1].response
    assert terminal is not None
    assert terminal.stop_reason is StopReason.REFUSAL


@pytest.mark.asyncio
async def test_chat_structured_stream_without_usage_still_finishes() -> None:
    response = _StreamingResponse(
        _sse_chunks(
            {"choices": [{"delta": {"content": "ok"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            "[DONE]",
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        events = [event async for event in OpenAIClient("sk", "https://api.example/v1", "model").stream_invoke(_request())]

    terminal = events[-1].response
    assert terminal is not None
    assert terminal.text == "ok"
    assert terminal.stop_reason is StopReason.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "payload"),
    [
        (
            OpenAIClient("sk", "https://api.example/v1", "model"),
            {
                "model": "model",
                "choices": [{"finish_reason": "stop", "message": {"content": "完整"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        ),
        (
            AnthropicClient("sk", "https://api.anthropic.com/v1", "model"),
            {
                "model": "model",
                "content": [{"type": "text", "text": "完整"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        (
            ResponsesClient("sk", "https://api.example/v1", "model"),
            {
                "model": "model",
                "status": "completed",
                "output_text": "完整",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ],
)
async def test_structured_stream_json_fallback_does_not_fabricate_delta(
    client,
    payload: dict,
) -> None:
    response = _StreamingResponse(
        [json.dumps(payload, ensure_ascii=False).encode()],
        content_type="application/json",
    )
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        events = [event async for event in client.stream_invoke(_request())]

    assert [event.delta for event in events] == [""]
    assert events[0].response is not None
    assert events[0].response.text == "完整"
    assert events[0].response.stream_fallback is True


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_responses_json_stream_fallback_rejects_failed_status(
    structured: bool,
) -> None:
    """忽略 stream=true 的 Responses 上游仍不能把失败 JSON 当成功结果。"""

    payload = {
        "model": "model",
        "status": "failed",
        "error": {"message": "upstream failed"},
        "output_text": "不应展示",
    }
    response = _StreamingResponse(
        [json.dumps(payload, ensure_ascii=False).encode()],
        content_type="application/json",
    )
    client = ResponsesClient("sk", "https://api.example/v1", "model")
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        with pytest.raises(Exception, match="状态异常"):
            if structured:
                _ = [event async for event in client.stream_invoke(_request())]
            else:
                _ = [chunk async for chunk in client.stream_complete("sys", "user")]


@pytest.mark.asyncio
async def test_json_stream_fallback_redacts_provider_error_secret() -> None:
    secret = "sk-sensitive-json-error"
    payload = {"error": {"message": f"invalid credential {secret}"}}
    response = _StreamingResponse(
        [json.dumps(payload).encode()],
        content_type="application/json",
    )
    client = OpenAIClient(secret, "https://api.example/v1", "model")
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        with pytest.raises(Exception) as caught:
            _ = [chunk async for chunk in client.stream_complete("sys", "user")]

    message = str(caught.value)
    assert secret not in message
    assert "<redacted>" in message


@pytest.mark.asyncio
async def test_responses_json_stream_fallback_rejects_non_token_incomplete() -> None:
    payload = {
        "model": "model",
        "status": "incomplete",
        "incomplete_details": {"reason": "server_error"},
        "output_text": "不应展示",
    }
    response = _StreamingResponse(
        [json.dumps(payload).encode()],
        content_type="application/json",
    )
    client = ResponsesClient("sk", "https://api.example/v1", "model")
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        with pytest.raises(Exception, match="状态异常"):
            _ = [chunk async for chunk in client.stream_complete("sys", "user")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "payload", "message"),
    [
        (
            OpenAIClient("sk", "https://api.example/v1", "model"),
            {
                "model": "model",
                "choices": [
                    {"finish_reason": "failed", "message": {"content": "不应展示"}}
                ],
            },
            "OpenAI 返回结束状态异常",
        ),
        (
            AnthropicClient("sk", "https://api.anthropic.com/v1", "model"),
            {
                "model": "model",
                "content": [{"type": "text", "text": "不应展示"}],
                "stop_reason": "cancelled",
            },
            "Anthropic 返回结束状态异常",
        ),
    ],
)
async def test_json_stream_fallback_rejects_protocol_failure_terminal(
    client,
    payload: dict,
    message: str,
) -> None:
    response = _StreamingResponse(
        [json.dumps(payload, ensure_ascii=False).encode()],
        content_type="application/json",
    )
    with patch(
        "app.services.llm_client.httpx.AsyncClient",
        return_value=_StreamingClient(response),
    ):
        with pytest.raises(Exception, match=message):
            _ = [event async for event in client.stream_invoke(_request())]


@pytest.mark.asyncio
async def test_chat_stream_complete_accepts_done_without_finish_reason() -> None:
    """Chat Completions 的 [DONE] 本身就是合法终态。"""
    response = _StreamingResponse(
        _sse_chunks(
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            "[DONE]",
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        chunks = [
            chunk
            async for chunk in OpenAIClient("sk", "https://api.example/v1", "model").stream_complete(
                "sys", "user"
            )
        ]
    assert [chunk.delta for chunk in chunks if chunk.delta] == ["ok"]
    assert chunks[-1].done is True


@pytest.mark.asyncio
async def test_chat_stream_complete_rejects_natural_eof_without_terminal() -> None:
    response = _StreamingResponse(
        _sse_chunks({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        with pytest.raises(Exception, match="缺少 finish_reason"):
            _ = [
                chunk
                async for chunk in OpenAIClient("sk", "https://api.example/v1", "model").stream_complete(
                    "sys", "user"
                )
            ]


@pytest.mark.asyncio
async def test_anthropic_stream_complete_accepts_text_without_message_stop_as_fallback() -> None:
    """部分 Anthropic 兼容网关会在正文后直接 EOF，应保留回复并标记降级。"""
    response = _StreamingResponse(
        _sse_chunks(
            {"type": "message_start", "message": {"model": "claude"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        chunks = [
            chunk
            async for chunk in AnthropicClient(
                "sk", "https://api.anthropic.com/v1", "model"
            ).stream_complete("sys", "user")
        ]

    assert [chunk.delta for chunk in chunks if chunk.delta] == ["partial"]
    assert chunks[-1].done is True
    assert chunks[-1].stream_fallback is True


@pytest.mark.asyncio
async def test_responses_stream_complete_rejects_natural_eof_without_completed() -> None:
    response = _StreamingResponse(
        _sse_chunks(
            {"type": "response.created", "response": {"model": "model", "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "partial"},
        )
    )
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        with pytest.raises(Exception, match="缺少 response.completed"):
            _ = [
                chunk
                async for chunk in ResponsesClient(
                    "sk", "https://api.example/v1", "model"
                ).stream_complete("sys", "user")
            ]


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_responses_stream_rejects_completed_status_on_nonterminal_event(
    structured: bool,
) -> None:
    """只有 response.completed 事件能宣告终态，普通事件里的 status 不能代替。"""

    response = _StreamingResponse(
        _sse_chunks(
            {
                "type": "response.created",
                "response": {
                    "model": "model",
                    "status": "completed",
                    "output_text": "尚未收到协议终态",
                },
            },
        )
    )
    client = ResponsesClient("sk", "https://api.example/v1", "model")
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        with pytest.raises(Exception, match="缺少 response.completed"):
            if structured:
                _ = [event async for event in client.stream_invoke(_request())]
            else:
                _ = [chunk async for chunk in client.stream_complete("sys", "user")]


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_responses_completed_event_rejects_failed_response_status(
    structured: bool,
) -> None:
    response = _StreamingResponse(
        _sse_chunks(
            {
                "type": "response.completed",
                "response": {
                    "model": "model",
                    "status": "failed",
                    "error": {"message": "upstream failed"},
                },
            },
        )
    )
    client = ResponsesClient("sk", "https://api.example/v1", "model")
    with patch("app.services.llm_client.httpx.AsyncClient", return_value=_StreamingClient(response)):
        with pytest.raises(Exception, match="状态异常"):
            if structured:
                _ = [event async for event in client.stream_invoke(_request())]
            else:
                _ = [chunk async for chunk in client.stream_complete("sys", "user")]
