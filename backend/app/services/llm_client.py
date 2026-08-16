"""LLM provider 抽象 —— OpenAI / Anthropic / (占位) Ollama。

设计要点：
- 每个 provider 实现 ``LLMClient`` 接口；``complete`` 返 ``LLMResult``
- ``build_client`` 根据 ``LLMProvider`` ORM 行解密 api_key 并装配具体实现
- **安全红线**：解密后的 api_key 仅留在 client 实例内；不打 log，不 audit；
  错误路径用 ``_safe_error_message`` 兜底剥离任何含 sk-/secret-/Bearer 字样
- 视觉支持：``complete(images=[...])`` 接 PNG/JPEG 等字节，由各实现按各自厂商
  vision 协议封装到 multipart content。``images`` 留空 = 纯文本（向后兼容）

调用入口在 worker 进程 (``worker/command.py:_run_ai``)，所以这里 httpx 调用是 async。

V1 仅实现 openai/anthropic 两类常用接口；ollama 走 OpenAI-compatible 端点（``/v1/chat/completions``）由 OpenAIClient 复用。
"""

from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

import httpx

from ..crypto import decrypt_str
from ..db.models.command import (
    LLM_API_FORMAT_ANTHROPIC_MESSAGES,
    LLM_API_FORMAT_CHAT_COMPLETIONS,
    LLM_API_FORMAT_RESPONSES,
    LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
    LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY,
    LLM_PROTOCOL_PROFILE_STANDARD,
    LLM_PROVIDER_OLLAMA,
    LLMProvider,
    default_api_format_for,
)
from . import llm_diagnostics as llm_diag
from .llm_call_context import ClientRuntimeContext
from .llm_codecs.anthropic import usage_from_anthropic
from .llm_codecs.chat_completions import usage_from_chat
from .llm_codecs.responses import plan_responses_body, usage_from_responses
from .llm_codecs.sse import iter_sse_events, parse_sse_text
from .llm_dto import LLMProviderDTO
from .llm_identity import (
    ClientIdentity,
    resolve_identity,
)
from .llm_profiles import ProviderProtocolProfile, resolve_protocol_profile
from .llm_protocol import (
    ImageContent,
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    NamedToolChoice,
    StopReason,
    TextContent,
    ToolCall,
    ToolChoiceMode,
    ToolResult,
    ToolSpec,
    capabilities_for_api_format,
    from_wire_tool_name,
    normalize_base_url,
    provider_endpoint,
    stop_reason_from_provider,
    to_wire_tool_name,
    wire_tool_name_map,
)
from .llm_request_headers import (
    REQUEST_SCOPE_INFERENCE,
    plan_request_headers,
    request_headers_for_scope,
)

# 默认调用超时；prompt 较长 / TG 端用户体验角度都不宜过长
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# 本地桥接（如 grok-bridge）需要等待浏览器 JS 执行 + LLM 生成，超时更长
_LOCAL_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
# 流式上游属于不可信输入：限制单行与整条响应，避免 ``aiter_lines`` 在缺少
# 换行时无限缓存。8000 output tokens 的正常文本通常远低于这些上限。
_STREAM_SSE_LINE_LIMIT_BYTES = 1_048_576
_STREAM_SSE_TOTAL_LIMIT_BYTES = 8 * 1_048_576
_RESPONSES_ALLOWED_INCOMPLETE_REASONS = frozenset(
    {"max_output_tokens", "max_tokens", "content_filter", "safety"}
)
_TOOL_MEDIA_MARKER = "[TelePilot：工具结果媒体已转为后续原生媒体块]"
_ERROR_SECRET_VALUES: ContextVar[tuple[str, ...]] = ContextVar(
    "llm_error_secret_values",
    default=(),
)


def _activate_error_secrets(values: Iterable[str]) -> None:
    """把本次异步调用的请求头密钥放入错误脱敏上下文，不跨任务共享。"""

    _ERROR_SECRET_VALUES.set(tuple(str(value) for value in values if str(value)))


def _llm_headers(
    *,
    identity: ClientIdentity | None = None,
    content_type: str | None = "application/json",
    accept: str | None = None,
    compatibility_headers: Mapping[str, str] | None = None,
    runtime_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造 LLM 请求头。

    0.57.0 起不再发送 TelePilot 产品 UA：身份头（含 UA）由集中身份目录解析后的
    ``identity`` 提供。``identity=None`` 或 ``minimal`` 档案时不注入任何身份 UA，
    仅保留协议必需头（Content-Type / Accept 与调用方自行装配的 Authorization）。
    """
    _activate_error_secrets((compatibility_headers or {}).values())
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if accept:
        headers["Accept"] = accept
    return plan_request_headers(
        system_headers=headers,
        identity_headers=identity.headers() if identity is not None else None,
        runtime_headers=runtime_headers,
        compatibility_headers=compatibility_headers,
    )


def _timeout_for_call(base_url: str, timeout_seconds: int | None) -> httpx.Timeout:
    if timeout_seconds and timeout_seconds > 0:
        seconds = float(max(1, timeout_seconds))
        return httpx.Timeout(
            seconds,
            connect=min(5.0, seconds),
            pool=min(5.0, seconds),
            write=min(15.0, seconds),
            read=seconds,
        )
    if "127.0.0.1" in base_url or "localhost" in base_url:
        return _LOCAL_TIMEOUT
    return _HTTP_TIMEOUT


def _normalize_temperature(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(2.0, float(value)))


def _normalize_reasoning_effort(value: str | None) -> str | None:
    effort = (value or "").strip().lower()
    return effort if effort in {"minimal", "low", "medium", "high", "xhigh", "max"} else None


@dataclass
class LLMResult:
    """LLM 调用的统一结果。"""

    text: str  # 模型回答正文
    model: str  # 实际使用的模型名（便于 TG 内回显）
    input_tokens: int  # 入 tokens；若供应商不返就给 0
    output_tokens: int  # 出 tokens；若供应商不返就给 0
    image_urls: list = field(default_factory=list)  # LLM 生成的图片 URL（如 Grok 文生图）
    image_data: list = field(default_factory=list)  # LLM 生成的图片 base64 data URI（如 Grok 文生图）
    sources: list = field(default_factory=list)  # 联网搜索来源：[{url,title?}, ...]
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.UNKNOWN
    provider_status: str | None = None
    execution_backend: str = "direct"
    gateway_version: str | None = None
    gateway_request_id: str | None = None
    gateway_stage: str | None = None


@dataclass(frozen=True)
class LLMStreamChunk:
    """Incremental text chunk from a provider streaming response."""

    delta: str = ""
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    done: bool = False
    # Upstream accepted the streaming request but returned one completed JSON
    # response.  Callers surface this as an honest non-incremental fallback and
    # must not issue a second request or split the text into pretend deltas.
    stream_fallback: bool = False
    execution_backend: str = "direct"
    gateway_version: str | None = None
    gateway_request_id: str | None = None
    gateway_stage: str | None = None


def _responses_event_error(prefix: str, error: Any, api_key: str | None) -> LLMError:
    """Preserve structured facts from HTTP-200 Responses error events."""

    payload = error if isinstance(error, Mapping) else {"message": str(error or "")}
    fact = llm_diag.diagnose_http_error(400, payload, api_key=api_key)
    return LLMError(
        _safe_error_message(
            f"{prefix}：{llm_diag.format_diagnostic_error(fact)}",
            api_key,
        ),
        retryable=fact.retryable,
        scope=fact.scope,
        status_code=fact.status_code,
        category=fact.category,
        upstream_status_code=fact.upstream_status_code,
        upstream_error_code=fact.upstream_error_code,
        upstream_error_message=fact.upstream_error_message,
        upstream_error_detail=fact.upstream_error_detail,
        upstream_request_id=fact.upstream_request_id,
        client_request_id=fact.client_request_id,
        upstream_summary=fact.upstream_summary,
    )


def _completed_json_as_stream_result(
    data: object,
    *,
    api_format: str,
    default_model: str,
    api_key: str | None = None,
) -> LLMResult:
    """解析忽略 ``stream=true`` 而返回的普通 JSON，避免再次请求上游。"""

    if not isinstance(data, dict):
        raise LLMError("上游流式请求返回的 JSON 不是对象")
    status = str(data.get("status") or "").lower()
    incomplete_reason: str | None = None
    if api_format == LLM_API_FORMAT_RESPONSES:
        incomplete = data.get("incomplete_details") or {}
        incomplete_reason = str(incomplete.get("reason") or "") if isinstance(incomplete, dict) else None
        if status in {"failed", "cancelled"} or (
            status == "incomplete" and incomplete_reason not in _RESPONSES_ALLOWED_INCOMPLETE_REASONS
        ):
            detail = data.get("error") or data.get("incomplete_details") or status
            raise _responses_event_error(
                f"Responses 返回状态异常: {status}",
                detail,
                api_key,
            )
    if data.get("error"):
        raise _responses_event_error("上游流式请求返回错误", data["error"], api_key)
    text = ""
    stop_reason = StopReason.UNKNOWN
    provider_status: str | None = None
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    try:
        if api_format == LLM_API_FORMAT_CHAT_COMPLETIONS:
            choices = data.get("choices") or []
            choice = choices[0] if isinstance(choices, list) and choices else {}
            message = choice.get("message") if isinstance(choice, dict) else {}
            text = _openai_message_visible_text(message) if isinstance(message, dict) else ""
            refusal = message.get("refusal") if isinstance(message, dict) else None
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
            stop_reason = (
                StopReason.REFUSAL
                if isinstance(refusal, str) and refusal.strip()
                else stop_reason_from_provider(finish_reason)
            )
            provider_status = (
                "refusal" if stop_reason is StopReason.REFUSAL else str(finish_reason or "") or None
            )
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        elif api_format == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
            text = "".join(_anthropic_content_text_pieces(data.get("content")))
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            provider_status = str(data.get("stop_reason") or "") or None
            stop_reason = stop_reason_from_provider(provider_status)
        else:
            if isinstance(data.get("output_text"), str):
                text = str(data["output_text"])
            if not text:
                text = "".join(
                    str(content.get("text") or "")
                    for item in data.get("output") or []
                    if isinstance(item, dict)
                    for content in item.get("content") or []
                    if isinstance(content, dict) and isinstance(content.get("text"), str)
                )
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            provider_status = str(incomplete_reason or status or "") or None
            stop_reason = (
                StopReason.CONTENT_FILTER
                if incomplete_reason in {"content_filter", "safety"}
                else StopReason.MAX_TOKENS
                if incomplete_reason in {"max_output_tokens", "max_tokens"}
                else stop_reason_from_provider(provider_status)
            )
    except (TypeError, ValueError):
        raise LLMError("上游流式请求返回的 usage 字段格式无效") from None
    return LLMResult(
        text=text,
        model=str(data.get("model") or default_model),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
        provider_status=provider_status,
    )


async def _read_limited_stream_json(response: Any, *, limit_bytes: int = 1_048_576) -> object:
    """读取忽略流式参数的 JSON 响应，并限制传输层累计大小。"""

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit_bytes:
            raise LLMError("上游流式请求返回的 JSON 超过 1 MiB 限制")
    try:
        return json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LLMError("上游 streaming 返回了无法解析的 JSON") from None


async def _iter_limited_sse_lines(
    response: Any,
    *,
    line_limit_bytes: int = _STREAM_SSE_LINE_LIMIT_BYTES,
    total_limit_bytes: int = _STREAM_SSE_TOTAL_LIMIT_BYTES,
) -> AsyncIterator[str]:
    """按完整 SSE block 解析，再投影成兼容旧调用方的 event/data/空行。"""

    try:
        async for event in iter_sse_events(
            response,
            event_limit_bytes=line_limit_bytes,
            total_limit_bytes=total_limit_bytes,
        ):
            yield f"event: {event.event}"
            yield f"data: {event.data}"
            yield ""
    except ValueError as exc:
        raise LLMError(f"上游 streaming SSE 结构异常: {exc}") from None


def _model_response_from_result(result: LLMResult) -> ModelResponse:
    content = (TextContent(result.text),) if result.text else ()
    return ModelResponse(
        model=result.model,
        content=content,
        tool_calls=tuple(result.tool_calls),
        usage=ModelUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        ),
        stop_reason=result.stop_reason,
        provider_status=result.provider_status,
        sources=tuple(dict(item) for item in result.sources if isinstance(item, dict)),
    )


def _parse_tool_arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"_raw": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": value}


@dataclass(frozen=True)
class _ToolResultMediaPlan:
    text: str
    images: tuple[ImageContent, ...] = ()


def _image_from_data_url(value: object) -> ImageContent | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    match = re.fullmatch(r"data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", candidate, re.I)
    if not match:
        return None
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if not data:
        return None
    return ImageContent(data=data, mime_type=match.group(1).lower())


def _extract_tool_media(value: object, images: list[ImageContent], depth: int = 0) -> object:
    """Extract only explicit image blocks/data URLs from tool output.

    Generic long strings are intentionally untouched: a base64-looking value is
    not media unless it is a complete, typed image data URL or a known block.
    """

    if depth > 32:
        return value
    if isinstance(value, str):
        image = _image_from_data_url(value)
        if image is not None:
            images.append(image)
            return _TOOL_MEDIA_MARKER
        return value
    if isinstance(value, ImageContent):
        images.append(value)
        return _TOOL_MEDIA_MARKER
    if isinstance(value, list):
        return [_extract_tool_media(item, images, depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_extract_tool_media(item, images, depth + 1) for item in value]
    if isinstance(value, dict):
        item_type = str(value.get("type") or "").lower()
        if item_type in {"image", "input_image", "image_url"}:
            image_value = value.get("url") or value.get("image_url")
            image_detail = value.get("detail")
            if isinstance(image_value, dict):
                image_detail = image_value.get("detail") or image_detail
                image_value = image_value.get("url")
            image = _image_from_data_url(image_value)
            if image is not None:
                images.append(image)
                return _TOOL_MEDIA_MARKER
            if (
                isinstance(image_value, str)
                and image_value.strip().lower().startswith(("http://", "https://"))
            ):
                images.append(ImageContent(url=image_value.strip(), detail=image_detail))
                return _TOOL_MEDIA_MARKER
            source = value.get("source")
            if isinstance(source, dict) and str(source.get("type") or "") == "base64":
                raw = source.get("data")
                mime = source.get("media_type")
                if isinstance(raw, str) and isinstance(mime, str) and mime.startswith("image/"):
                    try:
                        data = base64.b64decode(raw, validate=True)
                    except (ValueError, base64.binascii.Error):
                        data = b""
                    if data:
                        images.append(ImageContent(data=data, mime_type=mime))
                        return _TOOL_MEDIA_MARKER
        return {
            str(key): _extract_tool_media(child, images, depth + 1)
            for key, child in value.items()
        }
    return value


def _tool_result_media_plan(result: ToolResult) -> _ToolResultMediaPlan:
    images: list[ImageContent] = []
    sanitized = _extract_tool_media(result.content, images)
    if isinstance(result.content, str) and sanitized == _TOOL_MEDIA_MARKER:
        text = _TOOL_MEDIA_MARKER
    elif isinstance(sanitized, str):
        text = sanitized
    else:
        text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    return _ToolResultMediaPlan(text=text, images=tuple(images))


def _tool_result_text(result: ToolResult) -> str:
    return _tool_result_media_plan(result).text


def _tool_specs_openai(
    tools: tuple[ToolSpec, ...],
    tool_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Build OpenAI-compatible tool specs.

    Only emit ``strict`` when True. Many OpenAI-compatible providers (DeepSeek,
    relays) reject or mishandle ``strict: true`` / unknown fields.
    """

    payload: list[dict[str, Any]] = []
    for tool in tools:
        function: dict[str, Any] = {
            "name": to_wire_tool_name(tool.name, tool_names),
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if tool.strict:
            function["strict"] = True
        payload.append({"type": "function", "function": function})
    return payload


def _openai_tool_choice(
    choice: ToolChoiceMode | NamedToolChoice,
    tool_names: Mapping[str, str],
) -> object:
    if isinstance(choice, NamedToolChoice):
        return {
            "type": "function",
            "function": {"name": to_wire_tool_name(choice.name, tool_names)},
        }
    return choice.value


def _responses_tool_choice(
    choice: ToolChoiceMode | NamedToolChoice,
    tool_names: Mapping[str, str],
) -> object:
    if isinstance(choice, NamedToolChoice):
        return {"type": "function", "name": to_wire_tool_name(choice.name, tool_names)}
    return choice.value


def _anthropic_tool_choice(
    choice: ToolChoiceMode | NamedToolChoice,
    tool_names: Mapping[str, str],
) -> object:
    if isinstance(choice, NamedToolChoice):
        return {"type": "tool", "name": to_wire_tool_name(choice.name, tool_names)}
    if choice is ToolChoiceMode.REQUIRED:
        return {"type": "any"}
    return {"type": choice.value}


def _openai_content_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in value
        if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
    ).strip()


def _chat_messages(
    messages: tuple[ModelMessage, ...],
    tool_names: Mapping[str, str],
    reasoning_transport: str = "native",
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.TOOL:
            pending_media: list[ImageContent] = []
            for result in message.tool_results:
                plan = _tool_result_media_plan(result)
                output.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "name": to_wire_tool_name(result.name, tool_names),
                        "content": plan.text,
                    }
                )
                pending_media.extend(plan.images)
            if pending_media:
                output.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _TOOL_MEDIA_MARKER},
                            *[
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": image.url
                                        if image.url
                                        else _to_data_url(image.data or b""),
                                        **({"detail": image.detail} if image.detail else {}),
                                    },
                                }
                                for image in pending_media
                            ],
                        ],
                    }
                )
            continue
        text = message.text_content()
        image_blocks = [block for block in message.content if isinstance(block, ImageContent)]
        if image_blocks and message.role is MessageRole.USER:
            content: object = ([{"type": "text", "text": text}] if text else []) + [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": block.url if block.url else _to_data_url(block.data or b""),
                        **({"detail": block.detail} if block.detail else {}),
                    },
                }
                for block in image_blocks
            ]
        else:
            content = text or None
        item: dict[str, Any] = {"role": message.role.value, "content": content}
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": to_wire_tool_name(call.name, tool_names),
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        # DeepSeek 思考+工具：有 tool_calls 的 assistant 轮必须回传 reasoning_content
        if (
            reasoning_transport == "reasoning_content"
            and message.role is MessageRole.ASSISTANT
            and isinstance(message.reasoning_content, str)
            and message.reasoning_content
        ):
            item["reasoning_content"] = message.reasoning_content
        output.append(item)
    return output


def _responses_input(
    messages: tuple[ModelMessage, ...],
    tool_names: Mapping[str, str],
    reasoning_transport: str = "native",
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.SYSTEM:
            continue
        if message.role is MessageRole.TOOL:
            pending_media: list[ImageContent] = []
            for result in message.tool_results:
                plan = _tool_result_media_plan(result)
                output.append({
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": plan.text,
                })
                pending_media.extend(plan.images)
            if pending_media:
                output.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _TOOL_MEDIA_MARKER},
                            *[
                                {
                                    "type": "input_image",
                                    "image_url": image.url
                                    if image.url
                                    else _to_data_url(image.data or b""),
                                }
                                for image in pending_media
                            ],
                        ],
                    }
                )
            continue
        text = message.text_content()
        # Responses reasoning item 必须与同一轮后续的 assistant message 或
        # function_call 成对出现。推理被截断且没有生成结果时不能回放，否则
        # 下一次请求会被上游以 "without its required following item" 拒绝。
        if (
            reasoning_transport == "responses_item"
            and message.role is MessageRole.ASSISTANT
            and isinstance(message.reasoning_content, str)
            and message.reasoning_content
            and (bool(text) or bool(message.tool_calls))
        ):
            output.append(
                {
                    "type": "reasoning",
                    "content": [
                        {
                            "type": "reasoning_text",
                            "text": message.reasoning_content,
                        }
                    ],
                }
            )
        content: list[dict[str, Any]] = []
        if text:
            content.append(
                {
                    "type": "output_text" if message.role is MessageRole.ASSISTANT else "input_text",
                    "text": text,
                }
            )
        if message.role is MessageRole.USER:
            content.extend(
                {
                    "type": "input_image",
                    "image_url": block.url if block.url else _to_data_url(block.data or b""),
                    **({"detail": block.detail} if block.detail else {}),
                }
                for block in message.content
                if isinstance(block, ImageContent)
            )
        if content:
            output.append({"type": "message", "role": message.role.value, "content": content})
        output.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": to_wire_tool_name(call.name, tool_names),
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }
            for call in message.tool_calls
        )
    return output


def _system_instructions(messages: tuple[ModelMessage, ...]) -> str:
    return "\n\n".join(
        message.text_content()
        for message in messages
        if message.role is MessageRole.SYSTEM and message.text_content()
    )


def _anthropic_image_block(image: ImageContent) -> dict[str, Any]:
    if image.url is not None:
        return {
            "type": "image",
            "source": {"type": "url", "url": image.url},
        }
    data = image.data or b""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.mime_type or _sniff_image_mime(data),
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _anthropic_messages(
    messages: tuple[ModelMessage, ...],
    tool_names: Mapping[str, str],
    reasoning_transport: str = "native",
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        if message.role is MessageRole.SYSTEM:
            continue
        if message.role is MessageRole.TOOL:
            results: list[dict[str, Any]] = []
            for result in message.tool_results:
                plan = _tool_result_media_plan(result)
                content: object = plan.text
                if plan.images:
                    content = [
                        {"type": "text", "text": plan.text},
                        *[_anthropic_image_block(image) for image in plan.images],
                    ]
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": content,
                        "is_error": result.is_error,
                    }
                )
            output.append(
                {
                    "role": "user",
                    "content": results,
                }
            )
            continue
        content: list[dict[str, Any]] = []
        # DeepSeek Anthropic：工具轮次需回传 thinking 块，否则后续请求 400
        if (
            reasoning_transport == "anthropic_thinking"
            and message.role is MessageRole.ASSISTANT
            and isinstance(message.reasoning_content, str)
            and message.reasoning_content
        ):
            content.append({"type": "thinking", "thinking": message.reasoning_content})
        for block in message.content:
            if isinstance(block, TextContent) and block.text:
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                content.append(_anthropic_image_block(block))
        if message.role is MessageRole.ASSISTANT:
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": to_wire_tool_name(call.name, tool_names),
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
        output.append(
            {
                "role": "assistant" if message.role is MessageRole.ASSISTANT else "user",
                "content": content or "",
            }
        )
    return output


def _request_tool_name_map(request: ModelRequest) -> dict[str, str]:
    """Keep historical calls wire-compatible even on the no-tools final turn."""

    names = [tool.name for tool in request.tools]
    if isinstance(request.tool_choice, NamedToolChoice):
        names.append(request.tool_choice.name)
    for message in request.messages:
        names.extend(call.name for call in message.tool_calls)
        names.extend(result.name for result in message.tool_results)
    return wire_tool_name_map(names)


def _stream_openai_text(value: object) -> str:
    """Read an OpenAI-compatible delta without stripping meaningful spaces."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(item.get("text") or "")
        for item in value
        if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
    )


def _openai_reasoning_text(value: object) -> str:
    """Extract reasoning/thinking text from OpenAI-compatible message fields.

    Kimi K3 / 智谱 GLM 等在 Chat Completions 中用 ``reasoning_content`` 承载思考过程，
    最终答案在 ``content``。仅读 content 时，若答案尚未落盘会误判为空。
    """

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(item.get("text") or item.get("thinking") or "")
        for item in value
        if isinstance(item, dict) and item.get("type") in {"text", "output_text", "thinking", "reasoning"}
    )


def _openai_message_reasoning_text(message: Mapping[str, Any] | dict[str, Any]) -> str:
    """Extract provider-native chain-of-thought fields (DeepSeek / Kimi / 智谱)."""

    for key in ("reasoning_content", "reasoning", "thinking"):
        piece = _openai_reasoning_text(message.get(key)).strip()
        if piece:
            return piece
    return ""


def _openai_message_visible_text(message: Mapping[str, Any] | dict[str, Any]) -> str:
    """Prefer final ``content``; fall back to ``reasoning_content`` when content is empty.

    当同时存在 tool_calls 时不把 reasoning 折叠进正文——工具轮需要单独回传
    ``reasoning_content`` 字段，折叠会丢失协议语义。
    """

    content = _openai_content_text(message.get("content")).strip()
    if content:
        return content
    has_tool_calls = bool(message.get("tool_calls"))
    if has_tool_calls:
        return ""
    return _openai_message_reasoning_text(message)


def _request_thinking_mode(request: ModelRequest) -> str | None:
    """Optional DeepSeek-style thinking switch from request metadata.

    Values: ``enabled`` / ``disabled``. Unknown providers that reject the field
    should not receive it — callers only set this for compatible hosts.
    """

    raw = (request.metadata or {}).get("thinking")
    if raw in {"enabled", "disabled"}:
        return str(raw)
    return None


def _apply_thinking_control(body: dict[str, Any], request: ModelRequest) -> None:
    mode = _request_thinking_mode(request)
    if mode is not None:
        body["thinking"] = {"type": mode}


def _openai_structured_response(
    data: dict[str, Any],
    *,
    request: ModelRequest,
    tool_names: Mapping[str, str],
    stream_fallback: bool = False,
) -> ModelResponse:
    """Normalize a Chat Completions payload for both JSON and SSE terminals."""

    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMError("OpenAI 返回结构异常: 缺少 choices[0]")
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise LLMError("OpenAI 返回结构异常: message 不是对象")
    raw_tool_calls = message.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        raise LLMError("OpenAI 返回结构异常: tool_calls 不是数组", category="upstream_tool_call_dropped")
    finish_reason = choice.get("finish_reason")
    malformed_tool_call = False
    normalized_tool_calls: list[ToolCall] = []
    for item in raw_tool_calls:
        if not isinstance(item, dict):
            malformed_tool_call = True
            continue
        function = item.get("function")
        if not isinstance(function, dict) or not str(function.get("name") or "").strip():
            malformed_tool_call = True
            continue
        normalized_tool_calls.append(
            ToolCall(
                id=str(item.get("id") or ""),
                name=from_wire_tool_name(str(function.get("name") or ""), tool_names),
                arguments=_parse_tool_arguments(function.get("arguments")),
            )
        )
    if (malformed_tool_call or (finish_reason == "tool_calls" and not normalized_tool_calls)) and finish_reason != "length":
        raise LLMError(
            "上游工具调用缺少函数名，无法安全执行",
            category="upstream_tool_call_dropped",
        )
    tool_calls = tuple(normalized_tool_calls)
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    refusal = message.get("refusal")
    normalized_finish_reason = stop_reason_from_provider(finish_reason)
    if normalized_finish_reason in {StopReason.FAILED, StopReason.CANCELLED}:
        raise LLMError(f"OpenAI 返回结束状态异常: {str(finish_reason)[:200]}")
    reasoning = _openai_message_reasoning_text(message) or None
    visible = _openai_message_visible_text(message)
    return ModelResponse(
        model=str(data.get("model") or request.model),
        content=(TextContent(visible),) if visible else (),
        tool_calls=tool_calls,
        usage=usage_from_chat(usage),
        stop_reason=(
            StopReason.REFUSAL
            if isinstance(refusal, str) and refusal.strip()
            else StopReason.TOOL_CALLS
            if tool_calls
            else normalized_finish_reason
        ),
        provider_status=(
            "refusal"
            if isinstance(refusal, str) and refusal.strip()
            else str(finish_reason)
            if finish_reason
            else None
        ),
        stream_fallback=stream_fallback,
        reasoning_content=reasoning,
    )


def _anthropic_delta_text_piece(delta: Any) -> str | None:
    """Extract visible text from an Anthropic content_block_delta payload.

    DeepSeek V4 / Claude extended-thinking 兼容流常只推 ``thinking_delta``，
    没有 ``text_delta``。测活与闲聊若只认 text_delta 会误报空内容。
    """

    if not isinstance(delta, dict):
        return None
    delta_type = str(delta.get("type") or "")
    if delta_type == "text_delta":
        text = delta.get("text")
        return text if isinstance(text, str) and text else None
    if delta_type == "thinking_delta":
        # Anthropic: {"type":"thinking_delta","thinking":"..."}
        # 部分兼容站也用 text 字段
        for key in ("thinking", "text"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _anthropic_content_text_pieces(items: Any) -> list[str]:
    """Collect assistant-visible text from Anthropic message content blocks."""

    parts: list[str] = []
    if not isinstance(items, list):
        return parts
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "text" and isinstance(item.get("text"), str) and item["text"]:
            parts.append(str(item["text"]))
        elif item_type == "thinking":
            thinking = item.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                parts.append(thinking)
            elif isinstance(item.get("text"), str) and item["text"].strip():
                parts.append(str(item["text"]))
    return parts


def _anthropic_structured_response(
    data: dict[str, Any],
    *,
    request: ModelRequest,
    tool_names: Mapping[str, str],
    stream_fallback: bool = False,
) -> ModelResponse:
    """Normalize an Anthropic Messages payload for both JSON and SSE terminals."""

    content: list[TextContent] = []
    tool_calls: list[ToolCall] = []
    thinking_parts: list[str] = []
    for item in data.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            if item["text"]:
                content.append(TextContent(item["text"]))
        elif item.get("type") == "thinking":
            thinking = item.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                thinking_parts.append(thinking)
            elif isinstance(item.get("text"), str) and item["text"].strip():
                thinking_parts.append(str(item["text"]))
        elif item.get("type") == "tool_use":
            name = str(item.get("name") or "").strip()
            if name:
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("id") or ""),
                        name=from_wire_tool_name(name, tool_names),
                        arguments=_parse_tool_arguments(item.get("input")),
                    )
                )
    reasoning = "".join(thinking_parts) if thinking_parts else None
    # 仅有 thinking、没有 text 且无 tool 时：用 thinking 兜底为正文（闲聊/测活）
    # 有 tool_use 时保留 reasoning 独立字段，供后续轮次回传
    if not content and thinking_parts and not tool_calls:
        content.append(TextContent("".join(thinking_parts)))
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    stop_reason = data.get("stop_reason")
    normalized_stop_reason = stop_reason_from_provider(stop_reason)
    if normalized_stop_reason in {StopReason.FAILED, StopReason.CANCELLED}:
        raise LLMError(f"Anthropic 返回结束状态异常: {str(stop_reason)[:200]}")
    return ModelResponse(
        model=str(data.get("model") or request.model),
        content=tuple(content),
        tool_calls=tuple(tool_calls),
        usage=usage_from_anthropic(usage),
        stop_reason=(StopReason.TOOL_CALLS if tool_calls else normalized_stop_reason),
        provider_status=str(stop_reason) if stop_reason else None,
        stream_fallback=stream_fallback,
        reasoning_content=reasoning,
    )


def _coerce_chat_completions_to_responses(data: dict[str, Any]) -> dict[str, Any]:
    """If a gateway returns Chat Completions JSON for a Responses request, reshape it.

    智谱 / Kimi / 部分中转站官方主推 ``/chat/completions``；若用户误选
    ``api_format=responses`` 或网关混用形态，尽量从 ``choices`` 救回正文与工具调用。
    """

    if not isinstance(data, dict):
        return data
    if isinstance(data.get("output"), list) or str(data.get("object") or "") == "response":
        return data
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return data
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    visible = _openai_message_visible_text(message)
    reasoning = ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        reasoning = _openai_reasoning_text(message.get(key)).strip()
        if reasoning:
            break
    output: list[dict[str, Any]] = []
    if reasoning and not visible:
        output.append(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    if visible or reasoning:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": visible or reasoning,
                    }
                ],
            }
        )
    for item in message.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        args = function.get("arguments")
        output.append(
            {
                "type": "function_call",
                "id": str(item.get("id") or ""),
                "call_id": str(item.get("id") or ""),
                "name": name,
                "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
            }
        )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "id": data.get("id"),
        "object": "response",
        "status": "completed",
        "model": data.get("model"),
        "output": output,
        "output_text": visible or reasoning or "",
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            "output_tokens_details": usage.get("completion_tokens_details")
            if isinstance(usage.get("completion_tokens_details"), dict)
            else {},
        },
    }


def _responses_reasoning_text_from_item(item: Mapping[str, Any]) -> str:
    """Extract reasoning/summary text from a Responses output item."""

    parts: list[str] = []
    item_type = str(item.get("type") or "")
    if item_type == "reasoning":
        for summary in item.get("summary") or []:
            if isinstance(summary, dict):
                text = summary.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if str(content.get("type") or "") in {
                "summary_text",
                "reasoning_text",
                "text",
                "output_text",
            }:
                text = content.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    # 部分中转把思考塞进 message.content 的非 output_text 类型
    if item_type in {"message", "output_message"}:
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if str(content.get("type") or "") in {"reasoning", "thinking", "reasoning_text"}:
                text = content.get("text") or content.get("thinking")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts)


def _responses_structured_response(
    data: dict[str, Any],
    *,
    request: ModelRequest,
    tool_names: Mapping[str, str],
    stream_fallback: bool = False,
    api_key: str | None = None,
) -> ModelResponse:
    """Normalize a Responses payload for both JSON and SSE terminals."""

    data = _coerce_chat_completions_to_responses(data if isinstance(data, dict) else {})
    status = str(data.get("status") or "").lower()
    incomplete = data.get("incomplete_details") or {}
    incomplete_reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
    if status in {"failed", "cancelled"} or (
        status == "incomplete" and incomplete_reason not in _RESPONSES_ALLOWED_INCOMPLETE_REASONS
    ):
        detail = data.get("error") or data.get("incomplete_details") or status
        raise LLMError(
            _safe_error_message(
                f"Responses 返回状态异常: {status}: {str(detail)[:200]}",
                api_key,
            )
        )
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    has_refusal = False
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            name = str(item.get("name") or "").strip()
            if name:
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=from_wire_tool_name(name, tool_names),
                        arguments=_parse_tool_arguments(item.get("arguments")),
                    )
                )
            continue
        if item.get("type") == "reasoning":
            piece = _responses_reasoning_text_from_item(item)
            if piece:
                reasoning_parts.append(piece)
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            content_type = str(content.get("type") or "")
            if content_type == "refusal" and content.get("refusal"):
                has_refusal = True
            if content_type in {"output_text", "text"} and isinstance(content.get("text"), str):
                if content["text"]:
                    text_parts.append(content["text"])
            elif content_type in {"reasoning", "thinking", "reasoning_text", "summary_text"}:
                text = content.get("text") or content.get("thinking")
                if isinstance(text, str) and text:
                    reasoning_parts.append(text)
            elif isinstance(content.get("text"), str) and content["text"]:
                # 未知 type 但带 text：保守收下
                text_parts.append(content["text"])
    if not text_parts and isinstance(data.get("output_text"), str) and data["output_text"]:
        text_parts.append(str(data["output_text"]))
    # 仅有 reasoning、没有工具调用时才兜底为正文。工具轮必须保持
    # reasoning 独立，供下一轮按 Responses input item 原样回传。
    if not text_parts and reasoning_parts and not tool_calls:
        text_parts.extend(reasoning_parts)
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    provider_reason = incomplete_reason or status
    return ModelResponse(
        model=str(data.get("model") or request.model),
        content=(TextContent("".join(text_parts)),) if text_parts else (),
        tool_calls=tuple(tool_calls),
        usage=usage_from_responses(usage),
        stop_reason=(
            StopReason.REFUSAL
            if has_refusal
            else StopReason.MAX_TOKENS
            if incomplete_reason in {"max_output_tokens", "max_tokens"}
            else StopReason.TOOL_CALLS
            if tool_calls
            else stop_reason_from_provider(provider_reason)
        ),
        provider_status=str(provider_reason) if provider_reason else None,
        sources=tuple(_extract_response_sources(data)),
        stream_fallback=stream_fallback,
        reasoning_content="".join(reasoning_parts) or None,
    )


def _sniff_image_mime(data: bytes) -> str:
    """根据 magic bytes 判断图片 MIME 类型。

    支持 JPEG / PNG / WebP / GIF；其它一律返回 ``image/jpeg``（绝大多数 vision 模型
    会按 jpeg 兜底解码，比报错稳）。

    与 OpenAI/Anthropic 对 ``image/...`` 的接受集对齐。
    """
    if len(data) < 12:
        return "image/jpeg"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _to_data_url(data: bytes) -> str:
    """把图片字节编码为 ``data:image/...;base64,...`` data URL。

    OpenAI Chat Completions Vision / mimo / GLM-4V 等都接受这种 inline 形式，
    省去托管图床的麻烦。"""
    mime = _sniff_image_mime(data)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _normalize_image_data_uri(value: str, default_mime: str = "image/png") -> str:
    """把裸 base64 或 data URI 统一成 data URI，便于 worker 发送图片。"""
    raw = str(value or "").strip()
    if raw.startswith("data:") and ";base64," in raw:
        return raw
    return f"data:{default_mime};base64,{raw}"


def _extract_response_image_outputs(data: Any) -> tuple[list[str], list[str], str]:
    """从 Responses / 兼容返回体中提取生图结果。

    返回 ``(image_data, image_urls, output_text)``：
    - ``image_data`` 始终是 data URI；
    - ``image_urls`` 是可下载 URL；
    - ``output_text`` 用作图片 caption 或失败时的错误提示上下文。
    """
    image_data: list[str] = []
    image_urls: list[str] = []
    text_parts: list[str] = []

    def add_text(value: Any) -> None:
        if isinstance(value, str) and value:
            text_parts.append(value)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = str(node.get("type") or "")
            if "image_generation" in node_type or node_type in {"image", "output_image"}:
                for key in ("result", "b64_json", "image_base64", "partial_image_b64"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        image_data.append(_normalize_image_data_uri(value.strip()))
                for key in ("url", "image_url"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        image_urls.append(value.strip())
            if node_type in {"output_text", "text"}:
                add_text(node.get("text"))
            if (
                isinstance(node.get("text"), str)
                and "image_generation" not in node_type
                and node_type not in {"output_text", "text"}
            ):
                add_text(node.get("text"))
            for key in ("output", "content", "response", "data", "result", "message"):
                if key in node:
                    walk(node.get(key))
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(data, dict):
        add_text(data.get("output_text"))
    walk(data)

    # 去重并保持顺序
    def unique(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    return unique(image_data), unique(image_urls), "".join(text_parts).strip()


def _extract_response_sources(data: Any) -> list[dict[str, str]]:
    """从 Responses API 返回体里提取联网搜索来源。

    OpenAI Responses 的来源可能出现在两类位置：
    - ``output[].content[].annotations[]`` 的 ``url_citation``；
    - ``web_search_call.action.sources``（当请求 include 了 sources）。
    兼容反代时字段名可能略有差异，所以递归扫描常见 key。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url: Any, title: Any = None) -> None:
        if not isinstance(url, str):
            return
        u = url.strip()
        if not u or u in seen:
            return
        seen.add(u)
        item = {"url": u}
        if isinstance(title, str) and title.strip():
            item["title"] = title.strip()
        out.append(item)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            typ = str(node.get("type") or "")
            if typ in {"url_citation", "citation"}:
                add(node.get("url"), node.get("title"))
            if isinstance(node.get("url"), str) and ("title" in node or "source" in typ or "citation" in typ):
                add(node.get("url"), node.get("title"))
            web = node.get("web")
            if isinstance(web, dict):
                add(web.get("uri") or web.get("url"), web.get("title"))
            for key in (
                "sources",
                "annotations",
                "grounding_chunks",
                "groundingChunks",
                "output",
                "content",
                "action",
            ):
                value = node.get(key)
                if value is not None:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return out[:12]


def _response_text(resp: Any) -> str:
    return str(getattr(resp, "text", "") or "")


def _response_content_type(resp: Any) -> str:
    headers = getattr(resp, "headers", {}) or {}
    try:
        return str(headers.get("content-type") or headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return ""


def _parse_responses_sse(text: str, api_key: str | None = None) -> dict[str, Any]:
    """把 Responses API 的 SSE 成功流折叠为普通 Responses JSON。

    部分 Codex/CLIProxyAPI 反代即使请求里带了 ``stream: false``，仍会返回
    ``text/event-stream``。这里优先使用 ``response.completed`` 里的完整响应；
    如果反代只给了文本增量，则退化为顶层 ``output_text``。
    """
    delta_parts: list[str] = []
    done_text = ""
    error_payload: Any = None

    def text_from_stream() -> str:
        return (done_text or "".join(delta_parts)).strip()

    def with_stream_text(response: dict[str, Any]) -> dict[str, Any]:
        stream_text = text_from_stream()
        if not stream_text:
            return response
        image_data, image_urls, output_text = _extract_response_image_outputs(response)
        if output_text or image_data or image_urls:
            return response
        response = dict(response)
        response["output_text"] = stream_text
        return response

    for event in parse_sse_text(text):
        event_name = event.event
        raw_data = event.data
        if not raw_data or raw_data == "[DONE]":
            continue
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        payload_type = str(payload.get("type") or event_name or "")
        if payload_type in {"error", "response.error"}:
            error_payload = payload.get("error") or payload
            continue

        response = payload.get("response")
        if isinstance(response, dict):
            # Responses 的状态字段描述资源状态，不能替代协议定义的终态事件。
            # response.completed / response.incomplete 都是官方定义的正常终态。
            if payload_type in {"response.completed", "response.incomplete"}:
                return with_stream_text(response)
            if payload_type == "response.failed":
                error_payload = payload
                continue

        if payload_type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
            delta_parts.append(payload["delta"])
        elif payload_type == "response.output_text.done" and isinstance(payload.get("text"), str):
            done_text = payload["text"]
        elif payload_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        } and isinstance(payload.get("delta"), str):
            # 仅在尚无正文增量时用 reasoning 兜底拼流
            if not delta_parts and not done_text:
                delta_parts.append(payload["delta"])

    if error_payload is not None:
        raise _responses_event_error("Responses SSE 返回错误事件", error_payload, api_key)
    raise ValueError("缺少 response.completed / response.incomplete 终态")


def _decode_responses_payload(prefix: str, resp: Any, api_key: str | None) -> dict[str, Any]:
    content_type = _response_content_type(resp).lower()
    text = _response_text(resp)
    if "text/event-stream" in content_type or text.lstrip().startswith(("event:", "data:")):
        try:
            return _parse_responses_sse(text, api_key)
        except LLMError:
            raise
        except ValueError as exc:
            raise LLMError(_safe_error_message(f"{prefix} SSE 返回结构异常: {exc}", api_key)) from None
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise _non_json_error(prefix, resp, exc, api_key) from None
    if not isinstance(data, dict):
        raise LLMError(f"{prefix} 返回结构异常: 顶层不是对象")
    return data


_RESPONSES_REMOVABLE_PARAMETERS = {
    "temperature": "temperature",
    "reasoning": "reasoning",
    "reasoning.effort": "reasoning",
    "stream": "stream",
}


def _unsupported_parameter_name(resp: Any) -> str | None:
    if int(getattr(resp, "status_code", 0) or 0) < 400:
        return None
    lowered = _response_text(resp).lower()
    if not (
        "unsupported parameter" in lowered
        or "unknown parameter" in lowered
        or "unrecognized parameter" in lowered
        or "invalid parameter" in lowered
    ):
        return None
    match = re.search(
        r"(?:unsupported|unknown|unrecognized|invalid)\s+parameter(?:s)?\s*[:=]?\s*[`'\"]?([a-z0-9_.-]+)",
        lowered,
    )
    if match:
        return match.group(1).strip("`'\" ")
    for parameter in _RESPONSES_REMOVABLE_PARAMETERS:
        if parameter in lowered:
            return parameter
    return None


def _remove_unsupported_parameter(body: dict[str, Any], parameter: str) -> str | None:
    parameter = parameter.strip().lower()
    key = _RESPONSES_REMOVABLE_PARAMETERS.get(parameter)
    if key is None and "." in parameter:
        key = _RESPONSES_REMOVABLE_PARAMETERS.get(parameter.split(".", 1)[0])
    if key is None or key not in body:
        return None
    body.pop(key, None)
    return key


async def _post_responses_compatible(
    cli: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    current_body = dict(body)
    removed: set[str] = set()
    while True:
        resp = await cli.post(url, headers=headers, json=dict(current_body))
        parameter = _unsupported_parameter_name(resp)
        if not parameter:
            return resp
        removed_key = _remove_unsupported_parameter(current_body, parameter)
        if not removed_key or removed_key in removed:
            return resp
        removed.add(removed_key)


def _responses_capabilities_for_profile(
    profile: ProviderProtocolProfile,
):
    overrides = {
        name: False
        for name in profile.hard_disabled_capabilities
        if name in {"images", "tools", "parallel_tool_calls", "web_search", "temperature"}
    }
    return capabilities_for_api_format(LLM_API_FORMAT_RESPONSES).with_overrides(
        reasoning_transport=profile.reasoning_transport,
        **overrides,
    )


def _non_json_error(prefix: str, resp: Any, exc: json.JSONDecodeError, api_key: str | None) -> LLMError:
    content_type = _response_content_type(resp)
    status_code = int(getattr(resp, "status_code", 0) or 0)
    body = _response_text(resp).replace("\n", "\\n")[:200]
    if not body:
        body = "<empty>"
    return LLMError(
        _safe_error_message(
            f"{prefix} 返回非 JSON: status={status_code} content-type={content_type or 'unknown'} body={body} parse_error={exc}",
            api_key,
        )
    )


class LLMClient(ABC):
    """provider-agnostic 调用接口。"""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """以 system + user 拼 prompt（可附图），返回回答与 token 统计。

        ``images`` 留空 = 纯文本路径（向后兼容老调用）；
        非空时各实现按自己的 vision 协议把图片塞进 user message 的 content 块里。
        """
        raise NotImplementedError

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        """Compatibility bridge for clients without a native structured adapter."""

        if request.tools:
            raise NotImplementedError("当前 provider 尚未接入原生工具调用")
        system = "\n\n".join(
            message.text_content()
            for message in request.messages
            if message.role is MessageRole.SYSTEM and message.text_content()
        )
        user_messages = [message for message in request.messages if message.role is MessageRole.USER]
        if not user_messages:
            raise ValueError("ModelRequest 至少需要一条 user message")
        user_message = user_messages[-1]
        images = [
            block.data
            for block in user_message.content
            if isinstance(block, ImageContent) and block.data is not None
        ]
        result = await self.complete(
            system,
            user_message.text_content(),
            max_tokens=request.max_output_tokens,
            images=images or None,
            web_search=request.web_search,
            web_search_context_size=request.web_search_context_size,
            temperature=request.temperature,
            reasoning_effort=request.reasoning_effort,
        )
        return _model_response_from_result(result)

    async def stream_invoke(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Stream a structured request without fabricating text deltas.

        Concrete protocol clients override this to expose text received from the
        upstream transport.  The base implementation deliberately returns one
        terminal fallback event instead of splitting a completed response into
        pretend tokens.
        """

        response = await self.invoke(replace(request, stream=False))
        yield ModelStreamEvent(
            response=replace(response, stream_fallback=True),
        )

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Yield provider-native text deltas.

        Providers that have not wired a real streaming protocol must leave this
        explicit instead of emulating streaming from a completed response.
        """

        if False:
            yield LLMStreamChunk()
        raise NotImplementedError("当前 provider 协议尚未接入原生 streaming")

    async def transcribe(self, audio: bytes, model: str) -> str:
        """语音转写：把音频字节喂给 ``/audio/transcriptions`` 之类的 STT 端点。

        默认抛 ``NotImplementedError``——需要每个具体 client 自己实现（Anthropic 暂无）。

        ``model`` 由调用方指定（一般是 ``whisper-1``）；不复用 ``self._model``
        因为聊天模型与 STT 模型几乎总是不同的（gpt-4o-mini vs whisper-1）。
        """
        raise NotImplementedError(
            "本 provider 不支持语音转写（仅 OpenAI 兼容 /audio/transcriptions 端点支持）"
        )

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """原生图片生成入口。

        默认不支持；具体协议实现可返回 ``LLMResult.image_data`` 或
        ``LLMResult.image_urls``。参数列表刻意与 ``complete`` 对齐，方便
        fallback 调用层复用同一套 provider / retry / usage 管线。
        """
        raise NotImplementedError("当前 provider 协议尚未接入原生图片生成")


# ────────────────────────────────────────────────────────────
# OpenAI / OpenAI 兼容（含 Ollama）
# ────────────────────────────────────────────────────────────


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions 兼容协议。

    用 ``/v1/chat/completions`` 端点；Ollama (``/v1/chat/completions`` since 0.1.20+) 也走这里。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        proxy_url: str | None = None,
        identity: ClientIdentity | None = None,
        compatibility_headers: Mapping[str, str] | None = None,
        reasoning_transport: str = "native",
    ):
        self._api_key = api_key
        self._base_url = normalize_base_url(base_url or "https://api.openai.com/v1")
        self._model = model
        self._proxy_url = proxy_url
        self._identity = identity
        self._compatibility_headers = dict(compatibility_headers or {})
        self._reasoning_transport = reasoning_transport

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        if web_search:
            raise LLMError(
                "联网搜索需要使用 OpenAI Responses API（api_format=responses）",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_CHAT_COMPLETIONS)
        # Ollama 部署可能不需要 api_key；为空时不下发 Authorization 头
        headers = _llm_headers(identity=self._identity, compatibility_headers=self._compatibility_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 视觉路径：content 改成数组，先 text 再 image_url（OpenAI / mimo / GLM-4V 均如此）
        if images:
            user_content: object = [
                {"type": "text", "text": user},
                *[{"type": "image_url", "image_url": {"url": _to_data_url(img)}} for img in images],
            ]
        else:
            user_content = user
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning_effort"] = normalized_effort
        # httpx 0.28+ 用 proxy=<str> 单参数；socks5 需要 httpx[socks] 安装的 socksio
        # 当 proxy_url 为空时，显式 trust_env=False 避免 httpx 读取环境变量中的
        # HTTP_PROXY / NO_PROXY（NO_PROXY 含 ::1 会导致 httpx InvalidURL 崩溃）
        # 本地桥接（grok-bridge 等 localhost 服务）需要更长超时：浏览器 JS 执行 +
        # LLM 生成 + 图片 XHR 下载，整个过程可能超过 30 秒
        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            # 很多 httpx 异常 str() 是空（典型 SSL 握手 / ConnectError("")）；
            # 把异常类名 + 目标 host 也透出来，否则用户只看到 "网络异常: " 没法排查
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            # 不要把 api_key 回显到错误里；构造前先剥离
            raise LLMError(
                _safe_error_message(
                    f"OpenAI 接口返回 {resp.status_code}: {resp.text[:200]}{_diagnostic_hint(resp.status_code, resp.text)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, resp.text),
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI 返回非 JSON: {exc}") from None

        # 标准 OpenAI 形态：choices[0].message.content（Kimi/智谱可带 reasoning_content）
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            text = _openai_message_visible_text(message)
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"OpenAI 返回结构异常: {exc}") from None

        usage = data.get("usage") or {}
        raw_images = data.get("images") or []
        if not isinstance(raw_images, list):
            raw_images = []
        # 兼容两种格式：
        #   旧: ["url1", "url2"]（纯 URL 列表）
        #   新: [{"url": "url1", "data": "base64..."}, ...]（带 base64 数据）
        image_urls = []
        image_data = []
        for item in raw_images:
            if isinstance(item, dict):
                if item.get("url"):
                    image_urls.append(item["url"])
                if item.get("data"):
                    image_data.append(item["data"])
            elif isinstance(item, str):
                image_urls.append(item)
        choice = data.get("choices", [{}])[0] if isinstance(data.get("choices"), list) else {}
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        tool_calls = [
            ToolCall(
                id=str(item.get("id") or ""),
                name=str((item.get("function") or {}).get("name") or ""),
                arguments=_parse_tool_arguments((item.get("function") or {}).get("arguments")),
            )
            for item in message.get("tool_calls") or []
            if isinstance(item, dict) and str((item.get("function") or {}).get("name") or "")
        ]
        refusal = message.get("refusal")
        resolved_stop_reason = (
            StopReason.REFUSAL
            if isinstance(refusal, str) and refusal.strip()
            else StopReason.TOOL_CALLS
            if tool_calls
            else stop_reason_from_provider(finish_reason)
        )
        return LLMResult(
            text=text,
            model=str(data.get("model", self._model)),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            image_urls=image_urls,
            image_data=image_data,
            tool_calls=tool_calls,
            stop_reason=resolved_stop_reason,
            provider_status=(
                "refusal"
                if resolved_stop_reason is StopReason.REFUSAL
                else str(finish_reason)
                if finish_reason
                else None
            ),
        )

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """通过 Chat Completions SSE 逐段返回文本。

        不主动发送 ``stream_options``，以兼容只实现了基础 ``stream=true`` 的
        OpenAI-compatible 上游；如果上游自带 usage chunk，仍会正常采集。
        """
        if web_search:
            raise LLMError(
                "联网搜索需要使用 OpenAI Responses API（api_format=responses）",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_CHAT_COMPLETIONS)
        headers = _llm_headers(
            identity=self._identity,
            accept="text/event-stream",
            compatibility_headers=self._compatibility_headers,
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if images:
            user_content: object = [
                {"type": "text", "text": user},
                *[{"type": "image_url", "image_url": {"url": _to_data_url(img)}} for img in images],
            ]
        else:
            user_content = user
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning_effort"] = normalized_effort

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        model_name = self._model
        input_tokens = 0
        output_tokens = 0
        final_sent = False
        finish_received = False
        saw_content = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"OpenAI streaming 接口返回 {resp.status_code}: "
                                f"{error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                        )

                    response_headers = getattr(resp, "headers", {})
                    content_type = str(response_headers.get("content-type") or "")
                    if "json" in content_type.lower():
                        payload = await _read_limited_stream_json(resp)
                        result = _completed_json_as_stream_result(
                            payload,
                            api_format=LLM_API_FORMAT_CHAT_COMPLETIONS,
                            default_model=self._model,
                            api_key=self._api_key,
                        )
                        if result.text:
                            yield LLMStreamChunk(
                                delta=result.text,
                                model=result.model,
                                stream_fallback=True,
                            )
                        yield LLMStreamChunk(
                            model=result.model,
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            done=True,
                            stream_fallback=True,
                        )
                        return

                    async for line in _iter_limited_sse_lines(resp):
                        line = line.strip()
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        if raw == "[DONE]":
                            final_sent = True
                            yield LLMStreamChunk(
                                model=model_name,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                done=True,
                            )
                            return
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if payload.get("error"):
                            raise LLMError(
                                _safe_error_message(
                                    f"OpenAI streaming 返回错误事件: {str(payload['error'])[:200]}",
                                    self._api_key,
                                )
                            )
                        model_name = str(payload.get("model") or model_name)
                        usage = payload.get("usage") or {}
                        if isinstance(usage, dict):
                            input_tokens = int(usage.get("prompt_tokens") or input_tokens or 0)
                            output_tokens = int(usage.get("completion_tokens") or output_tokens or 0)
                        choices = payload.get("choices") or []
                        if not isinstance(choices, list) or not choices:
                            continue
                        choice = choices[0]
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        if isinstance(delta, dict):
                            text = _stream_openai_text(delta.get("content"))
                            if text:
                                saw_content = True
                                yield LLMStreamChunk(delta=text, model=model_name)
                            reasoning = _openai_reasoning_text(
                                delta.get("reasoning_content")
                                if delta.get("reasoning_content") is not None
                                else delta.get("reasoning")
                            )
                            if reasoning and not saw_content:
                                yield LLMStreamChunk(delta=reasoning, model=model_name)
                        if choice.get("finish_reason") is not None:
                            finish_received = True
                            # usage 可能位于 finish chunk 或其后的独立 chunk；继续读到
                            # [DONE]，若反代不发送 [DONE] 则在流结束后统一收尾。
                            continue
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if not final_sent and finish_received:
            yield LLMStreamChunk(
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                done=True,
            )
            return
        if not final_sent:
            raise LLMError(
                "OpenAI streaming 响应提前结束，缺少 finish_reason 或 [DONE] 终态",
                retryable=True,
            )

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        capabilities_for_api_format(LLM_API_FORMAT_CHAT_COMPLETIONS).validate(
            request,
            LLM_API_FORMAT_CHAT_COMPLETIONS,
        )
        tool_names = _request_tool_name_map(request)
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_CHAT_COMPLETIONS)
        headers = _llm_headers(identity=self._identity, compatibility_headers=self._compatibility_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": _chat_messages(request.messages, tool_names, self._reasoning_transport),
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            body["tools"] = _tool_specs_openai(request.tools, tool_names)
            body["tool_choice"] = _openai_tool_choice(request.tool_choice, tool_names)
        if request.temperature is not None:
            body["temperature"] = _normalize_temperature(request.temperature)
        if request.reasoning_effort:
            body["reasoning_effort"] = _normalize_reasoning_effort(request.reasoning_effort)
        _apply_thinking_control(body, request)

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, None)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(_describe_http_error(exc, self._base_url), self._api_key),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"OpenAI 接口返回 {resp.status_code}: {resp.text[:200]}{_diagnostic_hint(resp.status_code, resp.text)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, resp.text),
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI 返回非 JSON: {exc}") from None
        return _openai_structured_response(data, request=request, tool_names=tool_names)

    async def stream_invoke(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Expose real Chat Completions deltas while preserving native tools."""

        capabilities_for_api_format(LLM_API_FORMAT_CHAT_COMPLETIONS).validate(
            replace(request, stream=True),
            LLM_API_FORMAT_CHAT_COMPLETIONS,
        )
        tool_names = _request_tool_name_map(request)
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_CHAT_COMPLETIONS)
        headers = _llm_headers(
            identity=self._identity,
            accept="text/event-stream",
            compatibility_headers=self._compatibility_headers,
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": _chat_messages(request.messages, tool_names, self._reasoning_transport),
            "max_tokens": request.max_output_tokens,
            "stream": True,
        }
        if request.tools:
            body["tools"] = _tool_specs_openai(request.tools, tool_names)
            body["tool_choice"] = _openai_tool_choice(request.tool_choice, tool_names)
        if request.temperature is not None:
            body["temperature"] = _normalize_temperature(request.temperature)
        if request.reasoning_effort:
            body["reasoning_effort"] = _normalize_reasoning_effort(request.reasoning_effort)
        _apply_thinking_control(body, request)

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, None)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        model_name = request.model or self._model
        input_tokens = 0
        output_tokens = 0
        finish_reason: object = None
        finish_received = False
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_parts: dict[str, dict[str, Any]] = {}
        anonymous_tool_counter = 0
        # Some OpenAI-compatible gateways omit either ``index`` or ``id`` (or
        # switch between them across frames). Keep an ordinal alias table so
        # partial arguments remain attached to the same call without changing
        # insertion order when a later frame reveals an id.
        ordinal_tool_keys: list[str] = []
        terminal_sent = False

        def terminal_response(*, stream_fallback: bool = False) -> ModelResponse:
            calls: list[dict[str, Any]] = []
            for item in tool_parts.values():
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                calls.append(
                    {
                        "id": item.get("id") or "",
                        "function": {
                            "name": function.get("name") or "",
                            "arguments": function.get("arguments") or "{}",
                        },
                    }
                )
            content_text = "".join(text_parts)
            reasoning_text = "".join(reasoning_parts)
            return _openai_structured_response(
                {
                    "model": model_name,
                    "choices": [
                        {
                            "finish_reason": finish_reason,
                            "message": {
                                "content": content_text,
                                "reasoning_content": reasoning_text or None,
                                "refusal": "".join(refusal_parts) or None,
                                "tool_calls": calls,
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                    },
                },
                request=request,
                tool_names=tool_names,
                stream_fallback=stream_fallback,
            )

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"OpenAI streaming 接口返回 {resp.status_code}: "
                                f"{error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                        )
                    content_type = str(getattr(resp, "headers", {}).get("content-type") or "")
                    if "json" in content_type.lower():
                        payload = await _read_limited_stream_json(resp)
                        if not isinstance(payload, dict):
                            raise LLMError("OpenAI streaming 返回的 JSON 不是对象")
                        yield ModelStreamEvent(
                            response=_openai_structured_response(
                                payload,
                                request=request,
                                tool_names=tool_names,
                                stream_fallback=True,
                            )
                        )
                        return

                    async for line in _iter_limited_sse_lines(resp):
                        line = line.strip()
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        if raw == "[DONE]":
                            terminal_sent = True
                            yield ModelStreamEvent(response=terminal_response())
                            return
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if payload.get("error"):
                            raise LLMError(
                                _safe_error_message(
                                    f"OpenAI streaming 返回错误事件: {str(payload['error'])[:200]}",
                                    self._api_key,
                                )
                            )
                        model_name = str(payload.get("model") or model_name)
                        usage = payload.get("usage") or {}
                        if isinstance(usage, dict):
                            input_tokens = int(usage.get("prompt_tokens") or input_tokens or 0)
                            output_tokens = int(usage.get("completion_tokens") or output_tokens or 0)
                        choices = payload.get("choices") or []
                        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        if isinstance(delta, dict):
                            text = _stream_openai_text(delta.get("content"))
                            if text:
                                text_parts.append(text)
                                yield ModelStreamEvent(delta=text)
                            # Kimi / 智谱 / DeepSeek reasoner：推理增量
                            reasoning = _openai_reasoning_text(
                                delta.get("reasoning_content")
                                if delta.get("reasoning_content") is not None
                                else delta.get("reasoning")
                            )
                            if reasoning:
                                reasoning_parts.append(reasoning)
                                yield ModelStreamEvent(reasoning_delta=reasoning)
                            refusal = delta.get("refusal")
                            if isinstance(refusal, str) and refusal:
                                refusal_parts.append(refusal)
                            raw_calls = delta.get("tool_calls") or []
                            call_ordinal = 0
                            for raw_call in raw_calls if isinstance(raw_calls, list) else []:
                                if not isinstance(raw_call, dict):
                                    continue
                                call_id = str(raw_call.get("id") or "").strip()
                                raw_index = raw_call.get("index")
                                key: str | None = None
                                if call_id:
                                    # Prefer an already established id even if
                                    # this frame also changes the index shape.
                                    for candidate_key, candidate in tool_parts.items():
                                        if candidate.get("id") == call_id:
                                            key = candidate_key
                                            break
                                if raw_index is not None:
                                    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                                        raise LLMError("OpenAI streaming tool_call index 格式无效")
                                    indexed_key = f"idx:{raw_index}"
                                    indexed = tool_parts.get(indexed_key)
                                    if indexed is not None:
                                        key = indexed_key
                                    elif key is None:
                                        # A provider may reveal an index only
                                        # after beginning the call anonymously.
                                        # Reuse that anonymous ordinal instead
                                        # of creating a duplicate indexed call.
                                        if call_ordinal < len(ordinal_tool_keys):
                                            candidate_key = ordinal_tool_keys[call_ordinal]
                                            candidate = tool_parts.get(candidate_key)
                                            if (
                                                candidate_key.startswith("anon:")
                                                and candidate is not None
                                                and not candidate.get("id")
                                            ):
                                                key = candidate_key
                                        if key is None:
                                            key = indexed_key
                                if key is None and call_ordinal < len(ordinal_tool_keys):
                                    candidate_key = ordinal_tool_keys[call_ordinal]
                                    candidate = tool_parts.get(candidate_key)
                                    if candidate is not None and (not call_id or not candidate.get("id")):
                                        key = candidate_key
                                if key is None:
                                    if call_id:
                                        key = f"id:{call_id}"
                                    else:
                                        anonymous_tool_counter += 1
                                        key = f"anon:{anonymous_tool_counter}"
                                if call_ordinal >= len(ordinal_tool_keys):
                                    ordinal_tool_keys.extend("" for _ in range(call_ordinal + 1 - len(ordinal_tool_keys)))
                                ordinal_tool_keys[call_ordinal] = key
                                call_ordinal += 1
                                current = tool_parts.setdefault(key, {"function": {}})
                                if call_id:
                                    current["id"] = call_id
                                function = raw_call.get("function") or {}
                                if isinstance(function, dict):
                                    current_function = current.setdefault("function", {})
                                    if function.get("name"):
                                        current_function["name"] = str(function["name"])
                                    if isinstance(function.get("arguments"), str):
                                        current_function["arguments"] = (
                                            str(current_function.get("arguments") or "")
                                            + function["arguments"]
                                        )
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice.get("finish_reason")
                            finish_received = True
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(_describe_http_error(exc, self._base_url), self._api_key),
                retryable=True,
            ) from None

        if not terminal_sent:
            if not finish_received:
                raise LLMError(
                    "OpenAI streaming 响应提前结束，缺少 finish_reason 或 [DONE] 终态",
                    retryable=True,
                )
            yield ModelStreamEvent(response=terminal_response())

    async def transcribe(self, audio: bytes, model: str) -> str:
        """OpenAI / 兼容厂商的 ``POST /audio/transcriptions``（Whisper 协议）。

        multipart/form-data 上传：``file=<bytes>``、``model=<id>``、可选 ``response_format=json``。
        返回 JSON ``{"text": "..."}``。
        """
        if not audio:
            raise LLMError("音频字节为空")
        if not model:
            raise LLMError("transcribe() 必须指定 model（如 'whisper-1'）")
        url = f"{self._base_url}/audio/transcriptions"
        headers = _llm_headers(
            identity=self._identity, content_type=None, compatibility_headers=self._compatibility_headers
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 文件名给个通用后缀，让上游按二进制 audio 流处理
        files = {
            "file": ("audio.ogg", audio, "audio/ogg"),
        }
        data = {"model": model, "response_format": "json"}
        _is_local = "127.0.0.1" in self._base_url or "localhost" in self._base_url
        client_kwargs: dict[str, object] = {"timeout": _LOCAL_TIMEOUT if _is_local else _HTTP_TIMEOUT}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, files=files, data=data)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"STT 接口返回 {resp.status_code}: {resp.text[:200]}{_diagnostic_hint(resp.status_code, resp.text)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, resp.text),
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"STT 返回非 JSON: {exc}") from None
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise LLMError(f"STT 返回缺少 text 字段：{str(payload)[:200]}")
        return text.strip()

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """OpenAI-compatible Images API: ``POST /images/generations``.

        这条路径适合把模板模型直接设为 ``gpt-image-*`` / ``dall-e-*`` 的
        Provider。若要用普通主模型配 ``image_generation`` 工具，请使用
        ``api_format=responses``，由 ``ResponsesClient.generate_image`` 处理。
        """
        if web_search:
            raise LLMError("图片生成不支持联网搜索，请关闭 web_search")
        if images:
            raise LLMError(
                "当前 /images/generations 路径暂不支持参考图；请改用 api_format=responses 的 Provider"
            )

        url = f"{self._base_url}/images/generations"
        headers = _llm_headers(identity=self._identity, compatibility_headers=self._compatibility_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        prompt = user.strip()
        if system.strip():
            prompt = f"{system.strip()}\n\n用户需求：{prompt}"
        body = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
        }

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"Images 接口返回 {resp.status_code}: {resp.text[:200]}{_diagnostic_hint(resp.status_code, resp.text)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, resp.text),
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"Images 返回非 JSON: {exc}") from None

        image_data: list[str] = []
        image_urls: list[str] = []
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            b64 = item.get("b64_json") or item.get("base64") or item.get("data")
            if isinstance(b64, str) and b64.strip():
                image_data.append(_normalize_image_data_uri(b64.strip()))
            url_value = item.get("url")
            if isinstance(url_value, str) and url_value.strip():
                image_urls.append(url_value.strip())
        if not image_data and not image_urls:
            raise LLMError(f"Images 返回中没有图片数据：{str(data)[:200]}")

        usage = data.get("usage") or {}
        return LLMResult(
            text="",
            model=str(data.get("model") or self._model),
            input_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            image_urls=image_urls,
            image_data=image_data,
        )


# ────────────────────────────────────────────────────────────
# Anthropic Messages API
# ────────────────────────────────────────────────────────────


class AnthropicClient(LLMClient):
    """Anthropic ``/v1/messages`` 协议（Claude 系列）。"""

    # 文档要求的版本头；新版本兼容旧调用
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        proxy_url: str | None = None,
        protocol_profile: str = LLM_PROTOCOL_PROFILE_STANDARD,
        identity: ClientIdentity | None = None,
        compatibility_headers: Mapping[str, str] | None = None,
        provider_scope: str | None = None,
        reasoning_transport: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = normalize_base_url(base_url or "https://api.anthropic.com/v1")
        self._model = model
        self._proxy_url = proxy_url
        self._protocol_profile = protocol_profile
        self._identity = identity
        self._provider_scope = provider_scope or f"{self._base_url}|{protocol_profile}"
        self._compatibility_headers = dict(compatibility_headers or {})
        self._reasoning_transport = reasoning_transport or resolve_protocol_profile(
            LLM_API_FORMAT_ANTHROPIC_MESSAGES,
            protocol_profile,
            base_url=self._base_url,
            model=model,
            infer_when_standard=True,
        ).reasoning_transport

    def _headers(self, request: ModelRequest | None = None) -> dict[str, str]:
        _activate_error_secrets(self._compatibility_headers.values())
        # 协议必需头：x-api-key / anthropic-version / Content-Type。
        # 身份头（UA、x-app 等）由集中身份目录提供，不再发送 TelePilot 产品 UA；
        # minimal 身份不注入任何产品模拟头。
        system_headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        # protocol_profile 只控制协议语义 / beta 头，与身份相互独立：
        # 切换身份不会打开 beta，配置 claude_code_proxy 才发送 beta 头。
        if self._protocol_profile == LLM_PROTOCOL_PROFILE_CLAUDE_CODE_PROXY:
            # claude_code_proxy 历史上就绑定这组兼容头（含 x-app: cli），
            # 反代依赖它分发；保留以兼容既有 Provider，与身份档案是否提供 x-app 无关。
            system_headers.update(
                {
                    "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,effort-2025-11-24",
                    "anthropic-dangerous-direct-browser-access": "true",
                    "x-app": "cli",
                }
            )
        runtime_headers: dict[str, str] = {}
        if self._identity is not None:
            context = ClientRuntimeContext.from_metadata(
                request.metadata if request is not None else None,
                provider_scope=self._provider_scope,
            )
            runtime_headers = context.headers_for_identity(
                self._identity.profile,
                model=request.model if request is not None else self._model,
            )
        return plan_request_headers(
            system_headers=system_headers,
            identity_headers=self._identity.headers() if self._identity is not None else None,
            runtime_headers=runtime_headers,
            compatibility_headers=self._compatibility_headers,
        )

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        if web_search:
            raise LLMError(
                "当前 Anthropic 调用路径尚未接入联网搜索；请使用 OpenAI Responses provider",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_ANTHROPIC_MESSAGES)
        headers = self._headers()
        # 视觉路径：Anthropic 用 {"type":"image","source":{"type":"base64",...}}
        # 与 OpenAI 的 image_url 协议**不一样**，要分别构造
        if images:
            user_content: object = [
                *[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _sniff_image_mime(img),
                            "data": base64.b64encode(img).decode("ascii"),
                        },
                    }
                    for img in images
                ],
                {"type": "text", "text": user},
            ]
        else:
            user_content = user
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            # 使用流式（SSE）模式；Anyrouter 等 Claude Code 反代依赖流式协议分发
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = min(1.0, normalized_temperature)
        self._apply_reasoning_effort(body, reasoning_effort)
        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        # ── SSE 流式响应解析 ──────────────────────────────
        # 事件流生命周期：
        #   message_start → content_block_start → content_block_delta(×N)
        #   → content_block_stop → message_delta → message_stop
        #
        # 我们只需要：
        #   - message_start.message.model / .usage  → 模型名 + input_tokens
        #   - content_block_delta.delta.text         → 文本增量
        #   - message_delta.usage.output_tokens      → output_tokens
        text_parts: list[str] = []
        model_name = self._model
        input_tokens = 0
        output_tokens = 0
        provider_stop_reason: str | None = None
        message_stop_received = False

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        # 流式模式下，错误仍然可能作为普通 JSON 返回
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Anthropic 接口返回 {resp.status_code}: {error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                        )
                    # 逐行解析 SSE 事件
                    current_event = ""
                    async for line in _iter_limited_sse_lines(resp):
                        line = line.rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line.removeprefix("event:").strip()
                            continue
                        if line.startswith("data:"):
                            raw = line.removeprefix("data:").strip()
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            event_type = str(payload.get("type") or current_event or "")
                            if event_type == "message_start":
                                msg = payload.get("message") or {}
                                model_name = str(msg.get("model", self._model))
                                usage = msg.get("usage") or {}
                                input_tokens = int(usage.get("input_tokens") or 0)
                            elif event_type == "content_block_start":
                                # 部分兼容站在 start 块里带初始 thinking/text
                                block = payload.get("content_block") or {}
                                if isinstance(block, dict):
                                    for key in ("text", "thinking"):
                                        value = block.get(key)
                                        if isinstance(value, str) and value:
                                            text_parts.append(value)
                                            break
                            elif event_type == "content_block_delta":
                                delta = payload.get("delta") or {}
                                piece = _anthropic_delta_text_piece(delta)
                                if piece:
                                    text_parts.append(piece)
                            elif event_type == "message_delta":
                                delta = payload.get("delta") or {}
                                if isinstance(delta, dict) and delta.get("stop_reason"):
                                    provider_stop_reason = str(delta["stop_reason"])
                                usage = payload.get("usage") or {}
                                output_tokens = int(usage.get("output_tokens") or 0)
                            elif event_type == "error":
                                error = payload.get("error") or payload
                                raise LLMError(
                                    _safe_error_message(
                                        f"Anthropic streaming 返回错误事件: {str(error)[:200]}",
                                        self._api_key,
                                    )
                                )
                            elif event_type == "message_stop":
                                message_stop_received = True
                                break
                            # content_block_stop → 忽略
                            continue
                        # 空行 = 事件分隔符（SSE 规范）
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        text = "".join(text_parts).strip()
        resolved_stop_reason = stop_reason_from_provider(provider_stop_reason)
        if not message_stop_received and text:
            provider_stop_reason = provider_stop_reason or "missing_message_stop"
            resolved_stop_reason = stop_reason_from_provider(provider_stop_reason)
            return LLMResult(
                text=text,
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=resolved_stop_reason,
                provider_status=provider_stop_reason,
            )
        if not message_stop_received:
            raise LLMError(
                "Anthropic streaming 响应提前结束，缺少 message_stop 终态",
                retryable=True,
            )
        if not text and resolved_stop_reason not in {
            StopReason.REFUSAL,
            StopReason.CONTENT_FILTER,
        }:
            raise LLMError(
                "Anthropic 返回空内容（SSE 流中未收到 text_delta/thinking_delta 事件）",
                scope=LLMErrorScope.PROVIDER_LOCAL,
            )

        return LLMResult(
            text=text,
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=resolved_stop_reason,
            provider_status=provider_stop_reason,
        )

    def _apply_reasoning_effort(
        self,
        body: dict[str, Any],
        reasoning_effort: str | None,
    ) -> None:
        normalized = _normalize_reasoning_effort(reasoning_effort)
        if reasoning_effort and normalized is None:
            raise LLMError(
                f"Anthropic 不支持 reasoning_effort={reasoning_effort}",
                scope=LLMErrorScope.REQUEST_INVALID,
            )
        if normalized is None:
            return
        if normalized not in {"low", "medium", "high", "max"}:
            raise LLMError(
                f"Anthropic 不支持 reasoning_effort={normalized}",
                scope=LLMErrorScope.REQUEST_INVALID,
            )
        if normalized == "max" and any(family in self._model.lower() for family in ("sonnet", "haiku")):
            raise LLMError(
                "Anthropic max 推理强度仅适用于 Opus 系列模型",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        body["output_config"] = {"effort": normalized}

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        capabilities_for_api_format(LLM_API_FORMAT_ANTHROPIC_MESSAGES).validate(
            request,
            LLM_API_FORMAT_ANTHROPIC_MESSAGES,
        )
        tool_names = _request_tool_name_map(request)
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_ANTHROPIC_MESSAGES)
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "max_tokens": request.max_output_tokens,
            "system": _system_instructions(request.messages),
            "messages": _anthropic_messages(request.messages, tool_names, self._reasoning_transport),
            "stream": False,
        }
        if request.tools:
            body["tools"] = [
                {
                    "name": to_wire_tool_name(tool.name, tool_names),
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            body["tool_choice"] = _anthropic_tool_choice(request.tool_choice, tool_names)
        if request.temperature is not None:
            body["temperature"] = min(1.0, _normalize_temperature(request.temperature) or 0.0)
        self._apply_reasoning_effort(body, request.reasoning_effort)
        _apply_thinking_control(body, request)

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, None)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await cli.post(url, headers=self._headers(request), json=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(_describe_http_error(exc, self._base_url), self._api_key),
                retryable=True,
            ) from None
        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"Anthropic 接口返回 {resp.status_code}: {resp.text[:200]}{_diagnostic_hint(resp.status_code, resp.text)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, resp.text),
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"Anthropic 返回非 JSON: {exc}") from None
        return _anthropic_structured_response(data, request=request, tool_names=tool_names)

    async def stream_invoke(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Expose real Anthropic Messages deltas while preserving tool blocks."""

        capabilities_for_api_format(LLM_API_FORMAT_ANTHROPIC_MESSAGES).validate(
            replace(request, stream=True),
            LLM_API_FORMAT_ANTHROPIC_MESSAGES,
        )
        tool_names = _request_tool_name_map(request)
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_ANTHROPIC_MESSAGES)
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "max_tokens": request.max_output_tokens,
            "system": _system_instructions(request.messages),
            "messages": _anthropic_messages(request.messages, tool_names, self._reasoning_transport),
            "stream": True,
        }
        if request.tools:
            body["tools"] = [
                {
                    "name": to_wire_tool_name(tool.name, tool_names),
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            body["tool_choice"] = _anthropic_tool_choice(request.tool_choice, tool_names)
        if request.temperature is not None:
            body["temperature"] = min(1.0, _normalize_temperature(request.temperature) or 0.0)
        self._apply_reasoning_effort(body, request.reasoning_effort)
        _apply_thinking_control(body, request)

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, None)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        model_name = request.model or self._model
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        provider_stop_reason: str | None = None
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        # index → "text" | "thinking" | "tool_use"，用于 delta 分流
        block_kinds: dict[int, str] = {}
        content_blocks: dict[int, dict[str, Any]] = {}
        terminal_sent = False

        def terminal_response(*, stream_fallback: bool = False) -> ModelResponse:
            content: list[dict[str, Any]] = []
            if thinking_parts:
                content.append({"type": "thinking", "thinking": "".join(thinking_parts)})
            if text_parts:
                content.append({"type": "text", "text": "".join(text_parts)})
            for index in sorted(content_blocks):
                block = content_blocks[index]
                if block.get("type") == "tool_use":
                    raw_input = str(block.get("input_json") or "")
                    try:
                        parsed_input = json.loads(raw_input) if raw_input else {}
                    except json.JSONDecodeError:
                        parsed_input = {"_raw": raw_input}
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "input": parsed_input,
                        }
                    )
            return _anthropic_structured_response(
                {
                    "model": model_name,
                    "content": content,
                    "stop_reason": provider_stop_reason,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": cache_read_tokens,
                        "cache_creation_input_tokens": cache_write_tokens,
                    },
                },
                request=request,
                tool_names=tool_names,
                stream_fallback=stream_fallback,
            )

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream(
                    "POST",
                    url,
                    headers=self._headers(request),
                    json=body,
                ) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Anthropic streaming 接口返回 {resp.status_code}: "
                                f"{error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                        )
                    content_type = str(getattr(resp, "headers", {}).get("content-type") or "")
                    if "json" in content_type.lower():
                        payload = await _read_limited_stream_json(resp)
                        if not isinstance(payload, dict):
                            raise LLMError("Anthropic streaming 返回的 JSON 不是对象")
                        yield ModelStreamEvent(
                            response=_anthropic_structured_response(
                                payload,
                                request=request,
                                tool_names=tool_names,
                                stream_fallback=True,
                            )
                        )
                        return

                    current_event = ""
                    async for line in _iter_limited_sse_lines(resp):
                        line = line.rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line.removeprefix("event:").strip()
                            continue
                        if line.startswith("data:"):
                            raw = line.removeprefix("data:").strip()
                            if not raw:
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            event_type = str(payload.get("type") or current_event or "")
                            if event_type == "error":
                                error = payload.get("error") or payload
                                raise LLMError(
                                    _safe_error_message(
                                        f"Anthropic streaming 返回错误事件: {str(error)[:200]}",
                                        self._api_key,
                                    )
                                )
                            if event_type == "message_start":
                                message = payload.get("message") or {}
                                if isinstance(message, dict):
                                    model_name = str(message.get("model") or model_name)
                                    usage = message.get("usage") or {}
                                    if isinstance(usage, dict):
                                        input_tokens = int(usage.get("input_tokens") or 0)
                                        cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
                                        cache_write_tokens = int(
                                            usage.get("cache_creation_input_tokens") or 0
                                        )
                            elif event_type == "content_block_start":
                                try:
                                    index = int(payload.get("index") or 0)
                                except (TypeError, ValueError):
                                    raise LLMError(
                                        "Anthropic streaming content block index 格式无效"
                                    ) from None
                                block = payload.get("content_block") or {}
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    block_kinds[index] = "tool_use"
                                    content_blocks[index] = {
                                        "type": "tool_use",
                                        "id": str(block.get("id") or ""),
                                        "name": str(block.get("name") or ""),
                                        # Anthropic 的 start.input 通常是空对象，后续
                                        # input_json_delta 才给完整 JSON；空对象不能先
                                        # 写成 "{}"，否则会与真实分片拼成非法 JSON。
                                        "input_json": (
                                            json.dumps(block.get("input"), ensure_ascii=False)
                                            if block.get("input")
                                            else ""
                                        ),
                                    }
                                elif isinstance(block, dict) and block.get("type") == "thinking":
                                    block_kinds[index] = "thinking"
                                    value = block.get("thinking")
                                    if not isinstance(value, str) or not value:
                                        value = (
                                            block.get("text") if isinstance(block.get("text"), str) else ""
                                        )
                                    if value:
                                        thinking_parts.append(value)
                                        yield ModelStreamEvent(reasoning_delta=value)
                                elif isinstance(block, dict) and block.get("type") == "text":
                                    block_kinds[index] = "text"
                                    value = block.get("text")
                                    if isinstance(value, str) and value:
                                        text_parts.append(value)
                                        yield ModelStreamEvent(delta=value)
                            elif event_type == "content_block_delta":
                                try:
                                    index = int(payload.get("index") or 0)
                                except (TypeError, ValueError):
                                    raise LLMError(
                                        "Anthropic streaming content block index 格式无效"
                                    ) from None
                                delta = payload.get("delta") or {}
                                if not isinstance(delta, dict):
                                    continue
                                delta_type = str(delta.get("type") or "")
                                if delta_type == "input_json_delta":
                                    block = content_blocks.setdefault(
                                        index,
                                        {"type": "tool_use", "id": "", "name": "", "input_json": ""},
                                    )
                                    partial_json = delta.get("partial_json")
                                    if isinstance(partial_json, str):
                                        block["input_json"] = (
                                            str(block.get("input_json") or "") + partial_json
                                        )
                                elif delta_type == "thinking_delta" or block_kinds.get(index) == "thinking":
                                    piece = None
                                    for key in ("thinking", "text"):
                                        value = delta.get(key)
                                        if isinstance(value, str) and value:
                                            piece = value
                                            break
                                    if piece is None:
                                        piece = _anthropic_delta_text_piece(delta)
                                    if piece:
                                        thinking_parts.append(piece)
                                        yield ModelStreamEvent(reasoning_delta=piece)
                                else:
                                    piece = _anthropic_delta_text_piece(delta)
                                    if piece:
                                        text_parts.append(piece)
                                        yield ModelStreamEvent(delta=piece)
                            elif event_type == "message_delta":
                                delta = payload.get("delta") or {}
                                if isinstance(delta, dict) and delta.get("stop_reason"):
                                    provider_stop_reason = str(delta["stop_reason"])
                                usage = payload.get("usage") or {}
                                if isinstance(usage, dict):
                                    output_tokens = int(usage.get("output_tokens") or output_tokens or 0)
                            elif event_type == "message_stop":
                                terminal_sent = True
                                yield ModelStreamEvent(response=terminal_response())
                                return
                            continue
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(_describe_http_error(exc, self._base_url), self._api_key),
                retryable=True,
            ) from None

        if not terminal_sent and (text_parts or thinking_parts) and not content_blocks:
            provider_stop_reason = provider_stop_reason or "missing_message_stop"
            yield ModelStreamEvent(response=terminal_response(stream_fallback=True))
            return
        if not terminal_sent:
            raise LLMError(
                "Anthropic streaming 响应提前结束，缺少 message_stop 终态",
                retryable=True,
            )

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        if web_search:
            raise LLMError(
                "当前 Anthropic streaming 尚未接入联网搜索；请使用 OpenAI Responses provider",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_ANTHROPIC_MESSAGES)
        headers = self._headers()
        if images:
            user_content: object = [
                *[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _sniff_image_mime(img),
                            "data": base64.b64encode(img).decode("ascii"),
                        },
                    }
                    for img in images
                ],
                {"type": "text", "text": user},
            ]
        else:
            user_content = user
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = min(1.0, normalized_temperature)
        self._apply_reasoning_effort(body, reasoning_effort)

        client_kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            client_kwargs["proxy"] = self._proxy_url
        else:
            client_kwargs["trust_env"] = False

        model_name = self._model
        input_tokens = 0
        output_tokens = 0
        final_sent = False
        text_parts: list[str] = []

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Anthropic streaming 接口返回 {resp.status_code}: {error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                        )

                    response_headers = getattr(resp, "headers", {})
                    content_type = str(response_headers.get("content-type") or "")
                    if "json" in content_type.lower():
                        payload = await _read_limited_stream_json(resp)
                        result = _completed_json_as_stream_result(
                            payload,
                            api_format=LLM_API_FORMAT_ANTHROPIC_MESSAGES,
                            default_model=self._model,
                            api_key=self._api_key,
                        )
                        if result.text:
                            yield LLMStreamChunk(
                                delta=result.text,
                                model=result.model,
                                stream_fallback=True,
                            )
                        yield LLMStreamChunk(
                            model=result.model,
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            done=True,
                            stream_fallback=True,
                        )
                        return

                    current_event = ""
                    async for line in _iter_limited_sse_lines(resp):
                        line = line.rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line.removeprefix("event:").strip()
                            continue
                        if line.startswith("data:"):
                            raw = line.removeprefix("data:").strip()
                            if raw == "[DONE]":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            event_type = str(payload.get("type") or current_event or "")
                            if event_type == "message_start":
                                msg = payload.get("message") or {}
                                model_name = str(msg.get("model", self._model))
                                usage = msg.get("usage") or {}
                                input_tokens = int(usage.get("input_tokens") or 0)
                            elif event_type == "content_block_start":
                                block = payload.get("content_block") or {}
                                if isinstance(block, dict):
                                    for key in ("text", "thinking"):
                                        value = block.get(key)
                                        if isinstance(value, str) and value:
                                            text_parts.append(value)
                                            yield LLMStreamChunk(delta=value, model=model_name)
                                            break
                            elif event_type == "content_block_delta":
                                delta = payload.get("delta") or {}
                                piece = _anthropic_delta_text_piece(delta)
                                if piece:
                                    text_parts.append(piece)
                                    yield LLMStreamChunk(delta=piece, model=model_name)
                            elif event_type == "message_delta":
                                usage = payload.get("usage") or {}
                                output_tokens = int(usage.get("output_tokens") or 0)
                            elif event_type == "error":
                                error = payload.get("error") or payload
                                raise LLMError(
                                    _safe_error_message(
                                        f"Anthropic streaming 返回错误事件: {str(error)[:200]}",
                                        self._api_key,
                                    )
                                )
                            elif event_type == "message_stop":
                                final_sent = True
                                yield LLMStreamChunk(
                                    model=model_name,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    done=True,
                                )
                                return
                            continue
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if not final_sent and text_parts:
            yield LLMStreamChunk(
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                done=True,
                stream_fallback=True,
            )
            return
        if not final_sent:
            raise LLMError(
                "Anthropic streaming 响应提前结束，缺少 message_stop 终态",
                retryable=True,
            )


# ────────────────────────────────────────────────────────────
# OpenAI Responses API（POST /responses，2024 出的新协议）
# ────────────────────────────────────────────────────────────


class ResponsesClient(LLMClient):
    """OpenAI Responses API（POST ``/responses``）。

    与 chat/completions 的差异：
    - 入参 ``input=[{role, content}]`` + ``instructions`` + ``model`` + ``max_output_tokens``
    - 出参 ``output=[{type:"message", content:[{type:"output_text", text:"..."}]}]``
      也可能直接给 ``output_text`` 顶层字符串（不同实现略有差异，都做兼容）
    - usage 字段是 ``input_tokens`` / ``output_tokens``（不是 prompt_tokens / completion_tokens）

    很多国内 OpenAI 兼容反代（如 anyrouter）只接 ``/responses`` 不接 ``/chat/completions``，
    所以这条 client 是必须的。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        proxy_url: str | None = None,
        identity: ClientIdentity | None = None,
        compatibility_headers: Mapping[str, str] | None = None,
        protocol_profile: str = LLM_PROTOCOL_PROFILE_STANDARD,
        provider_scope: str | None = None,
        reasoning_transport: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = normalize_base_url(base_url or "https://api.openai.com/v1")
        self._model = model
        self._proxy_url = proxy_url
        self._identity = identity
        self._protocol_profile = resolve_protocol_profile(
            LLM_API_FORMAT_RESPONSES,
            protocol_profile,
            base_url=self._base_url,
            model=model,
            infer_when_standard=True,
        )
        self._provider_scope = provider_scope or (
            f"{self._base_url}|{self._protocol_profile.name}"
        )
        self._compatibility_headers = dict(compatibility_headers or {})
        self._reasoning_transport = reasoning_transport or self._protocol_profile.reasoning_transport

    def _client_kwargs(self, timeout_seconds: int | None) -> dict[str, object]:
        kwargs: dict[str, object] = {"timeout": _timeout_for_call(self._base_url, timeout_seconds)}
        if self._proxy_url:
            kwargs["proxy"] = self._proxy_url
        else:
            kwargs["trust_env"] = False
        return kwargs

    def _runtime_headers(self, request: ModelRequest | None = None) -> dict[str, str]:
        if self._identity is None:
            return {}
        context = ClientRuntimeContext.from_metadata(
            request.metadata if request is not None else None,
            provider_scope=self._provider_scope,
        )
        return context.headers_for_identity(
            self._identity.profile,
            model=request.model if request is not None else self._model,
        )

    def _capture_response_metadata(self, response: httpx.Response) -> None:
        """Transport-specific clients may retain bounded diagnostic headers."""

    def _http_error_diagnostic_kwargs(
        self,
        response: httpx.Response,
        body: str,
    ) -> dict[str, Any]:
        self._capture_response_metadata(response)
        fact = llm_diag.diagnose_http_error(
            response.status_code,
            body,
            api_key=self._api_key,
            base_url=self._base_url,
        )
        return {
            "category": fact.category,
            "upstream_status_code": fact.upstream_status_code,
            "upstream_error_code": fact.upstream_error_code,
            "upstream_error_message": fact.upstream_error_message,
            "upstream_error_detail": fact.upstream_error_detail,
            "upstream_request_id": fact.upstream_request_id,
            "client_request_id": fact.client_request_id,
            "upstream_summary": fact.upstream_summary,
        }

    def _network_error(self, exc: httpx.HTTPError) -> LLMError:
        return LLMError(
            _safe_error_message(
                _describe_http_error(exc, self._base_url),
                self._api_key,
            ),
            retryable=True,
        )

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        if images and "images" in self._protocol_profile.hard_disabled_capabilities:
            raise LLMError(
                f"{self._protocol_profile.name} 不支持图片输入",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        if web_search and "web_search" in self._protocol_profile.hard_disabled_capabilities:
            raise LLMError(
                f"{self._protocol_profile.name} 不支持原生联网搜索",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_RESPONSES)
        headers = _llm_headers(
            identity=self._identity,
            accept="application/json",
            compatibility_headers=self._compatibility_headers,
            runtime_headers=self._runtime_headers(),
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # 视觉路径：Responses API 的 content 是 [{"type":"input_text"}, {"type":"input_image"}]
        # （注意：不是 chat/completions 的 image_url 名字；OpenAI 把这两套协议命名拆开了）
        if images:
            input_content: object = [
                {"type": "input_text", "text": user},
                *[{"type": "input_image", "image_url": _to_data_url(img)} for img in images],
            ]
        else:
            input_content = user
        body = {
            "model": self._model,
            # 用 instructions 字段传 system；input 列表按 role/content 给 user 输入
            "instructions": system,
            "input": [
                {"role": "user", "content": input_content},
            ],
            # Responses API 用 max_output_tokens（不是 max_tokens）
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning"] = {"effort": normalized_effort}
        if web_search:
            size = (web_search_context_size or "medium").lower()
            if size not in {"low", "medium", "high"}:
                size = "medium"
            body["tools"] = [{"type": "web_search", "search_context_size": size}]
            body["include"] = ["web_search_call.action.sources"]
        body = plan_responses_body(body, self._protocol_profile)

        client_kwargs = self._client_kwargs(timeout_seconds)
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await _post_responses_compatible(cli, url, headers=headers, body=body)
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from None

        self._capture_response_metadata(resp)

        if resp.status_code >= 400:
            response_body = _response_text(resp)
            raise LLMError(
                _safe_error_message(
                    f"Responses 接口返回 {resp.status_code}: {response_body[:200]}{_diagnostic_hint(resp.status_code, response_body)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, response_body),
                status_code=resp.status_code,
                **self._http_error_diagnostic_kwargs(resp, response_body),
            )

        data = _decode_responses_payload("Responses", resp, self._api_key)
        # 统一走 structured 归一化：支持 reasoning 兜底、Chat Completions 误回包整形
        request = ModelRequest(
            model=self._model,
            messages=(
                ModelMessage.text(MessageRole.SYSTEM, system),
                ModelMessage.text(MessageRole.USER, user),
            ),
            max_output_tokens=max_tokens,
        )
        normalized = _responses_structured_response(
            data if isinstance(data, dict) else {},
            request=request,
            tool_names={},
            api_key=self._api_key,
        )
        return LLMResult(
            text=normalized.text,
            model=str(normalized.model or self._model),
            input_tokens=int(normalized.usage.input_tokens or 0),
            output_tokens=int(normalized.usage.output_tokens or 0),
            sources=list(normalized.sources or ()),
            tool_calls=list(normalized.tool_calls or ()),
            stop_reason=normalized.stop_reason,
            provider_status=normalized.provider_status,
        )


    async def invoke(self, request: ModelRequest) -> ModelResponse:
        _responses_capabilities_for_profile(self._protocol_profile).validate(
            request,
            LLM_API_FORMAT_RESPONSES,
        )
        tool_names = _request_tool_name_map(request)
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_RESPONSES)
        headers = _llm_headers(
            identity=self._identity,
            accept="application/json",
            compatibility_headers=self._compatibility_headers,
            runtime_headers=self._runtime_headers(request),
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "instructions": _system_instructions(request.messages),
            "input": _responses_input(request.messages, tool_names, self._reasoning_transport),
            "max_output_tokens": request.max_output_tokens,
            "stream": False,
            "store": False,
        }
        if request.tools:
            body["tools"] = []
            for tool in request.tools:
                item: dict[str, Any] = {
                    "type": "function",
                    "name": to_wire_tool_name(tool.name, tool_names),
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                if tool.strict:
                    item["strict"] = True
                body["tools"].append(item)
            body["tool_choice"] = _responses_tool_choice(request.tool_choice, tool_names)
        if request.temperature is not None:
            body["temperature"] = _normalize_temperature(request.temperature)
        if request.reasoning_effort:
            body["reasoning"] = {"effort": _normalize_reasoning_effort(request.reasoning_effort)}
        if request.web_search:
            size = (request.web_search_context_size or "medium").lower()
            if size not in {"low", "medium", "high"}:
                size = "medium"
            body.setdefault("tools", []).append({"type": "web_search", "search_context_size": size})
            body["include"] = ["web_search_call.action.sources"]
        body = plan_responses_body(body, self._protocol_profile)

        client_kwargs = self._client_kwargs(None)
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await _post_responses_compatible(cli, url, headers=headers, body=body)
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from None
        self._capture_response_metadata(resp)
        if resp.status_code >= 400:
            response_body = _response_text(resp)
            raise LLMError(
                _safe_error_message(
                    f"Responses 接口返回 {resp.status_code}: {response_body[:200]}{_diagnostic_hint(resp.status_code, response_body)}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, response_body),
                status_code=resp.status_code,
                **self._http_error_diagnostic_kwargs(resp, response_body),
            )
        data = _decode_responses_payload("Responses", resp, self._api_key)
        return _responses_structured_response(
            data, request=request, tool_names=tool_names, api_key=self._api_key
        )

    async def stream_invoke(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Expose real Responses API deltas while preserving function calls."""

        _responses_capabilities_for_profile(self._protocol_profile).validate(
            replace(request, stream=True),
            LLM_API_FORMAT_RESPONSES,
        )
        tool_names = _request_tool_name_map(request)
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_RESPONSES)
        headers = _llm_headers(
            identity=self._identity,
            accept="text/event-stream",
            compatibility_headers=self._compatibility_headers,
            runtime_headers=self._runtime_headers(request),
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {
            "model": request.model or self._model,
            "instructions": _system_instructions(request.messages),
            "input": _responses_input(request.messages, tool_names, self._reasoning_transport),
            "max_output_tokens": request.max_output_tokens,
            "stream": True,
            "store": False,
        }
        if request.tools:
            body["tools"] = []
            for tool in request.tools:
                item: dict[str, Any] = {
                    "type": "function",
                    "name": to_wire_tool_name(tool.name, tool_names),
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                if tool.strict:
                    item["strict"] = True
                body["tools"].append(item)
            body["tool_choice"] = _responses_tool_choice(request.tool_choice, tool_names)
        if request.temperature is not None:
            body["temperature"] = _normalize_temperature(request.temperature)
        if request.reasoning_effort:
            body["reasoning"] = {"effort": _normalize_reasoning_effort(request.reasoning_effort)}
        if request.web_search:
            size = (request.web_search_context_size or "medium").lower()
            if size not in {"low", "medium", "high"}:
                size = "medium"
            body.setdefault("tools", []).append(
                {"type": "web_search", "search_context_size": size}
            )
            body["include"] = ["web_search_call.action.sources"]
        body = plan_responses_body(body, self._protocol_profile)

        client_kwargs = self._client_kwargs(None)

        model_name = request.model or self._model
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        function_calls: dict[str, dict[str, Any]] = {}
        function_call_aliases: dict[str, str] = {}
        indexed_output_items: dict[int, dict[str, Any]] = {}
        unindexed_output_items: list[dict[str, Any]] = []
        last_response: dict[str, Any] | None = None
        terminal_sent = False

        def recorded_output_items() -> list[dict[str, Any]]:
            return [
                *(
                    dict(indexed_output_items[index])
                    for index in sorted(indexed_output_items)
                ),
                *(dict(item) for item in unindexed_output_items),
            ]

        def terminal_response(*, stream_fallback: bool = False) -> ModelResponse:
            if last_response is not None:
                response = dict(last_response)
                visible = "".join(text_parts)
                if visible and not response.get("output_text"):
                    response["output_text"] = visible
                existing_output = response.get("output")
                merged_output = (
                    list(existing_output)
                    if isinstance(existing_output, list) and existing_output
                    else recorded_output_items()
                )
                reasoning_text = "".join(reasoning_parts)
                if reasoning_text:
                    reasoning_item = next(
                        (
                            item
                            for item in merged_output
                            if isinstance(item, dict) and item.get("type") == "reasoning"
                        ),
                        None,
                    )
                    if reasoning_item is None:
                        merged_output.insert(
                            0,
                            {
                                "type": "reasoning",
                                "content": [
                                    {
                                        "type": "reasoning_text",
                                        "text": reasoning_text,
                                    }
                                ],
                            },
                        )
                    elif not _responses_reasoning_text_from_item(reasoning_item):
                        reasoning_item["content"] = [
                            {
                                "type": "reasoning_text",
                                "text": reasoning_text,
                            }
                        ]
                if function_calls:
                    seen_keys: set[str] = set()
                    for item in merged_output:
                        if not isinstance(item, dict) or item.get("type") != "function_call":
                            continue
                        aliases = (
                            str(item.get("id") or ""),
                            str(item.get("call_id") or ""),
                        )
                        key = next(
                            (function_call_aliases.get(alias, alias) for alias in aliases if alias),
                            "",
                        )
                        current = function_calls.get(key)
                        if current is not None:
                            if not item.get("arguments"):
                                item["arguments"] = current.get("arguments") or ""
                            if not item.get("name"):
                                item["name"] = current.get("name") or ""
                            seen_keys.add(key)
                    merged_output.extend(item for key, item in function_calls.items() if key not in seen_keys)
                if merged_output:
                    response["output"] = merged_output
            else:
                output = recorded_output_items()
                reasoning_text = "".join(reasoning_parts)
                if reasoning_text and not any(
                    item.get("type") == "reasoning" for item in output
                ):
                    output.insert(
                        0,
                        {
                            "type": "reasoning",
                            "content": [
                                {
                                    "type": "reasoning_text",
                                    "text": reasoning_text,
                                }
                            ],
                        },
                    )
                output.extend(
                    item for item in function_calls.values() if item not in output
                )
                response = {
                    "model": model_name,
                    "status": "completed",
                    "output_text": "".join(text_parts),
                    "output": output,
                }
            return _responses_structured_response(
                response,
                request=request,
                tool_names=tool_names,
                stream_fallback=stream_fallback,
                api_key=self._api_key,
            )

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    self._capture_response_metadata(resp)
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Responses streaming 接口返回 {resp.status_code}: "
                                f"{error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                            **self._http_error_diagnostic_kwargs(resp, error_body),
                        )
                    content_type = str(getattr(resp, "headers", {}).get("content-type") or "")
                    if "json" in content_type.lower():
                        payload = await _read_limited_stream_json(resp)
                        if not isinstance(payload, dict):
                            raise LLMError("Responses streaming 返回的 JSON 不是对象")
                        # 中转若回 Chat Completions 形态，先整形再解析
                        payload = _coerce_chat_completions_to_responses(payload)
                        yield ModelStreamEvent(
                            response=_responses_structured_response(
                                payload,
                                request=request,
                                tool_names=tool_names,
                                stream_fallback=True,
                                api_key=self._api_key,
                            )
                        )
                        return

                    current_event = ""
                    async for line in _iter_limited_sse_lines(resp):
                        line = line.rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line.removeprefix("event:").strip()
                            continue
                        if line.startswith("data:"):
                            raw = line.removeprefix("data:").strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            # 极少数网关把 chat SSE 塞进 responses 流
                            if payload.get("choices") and not payload.get("type"):
                                coerced = _coerce_chat_completions_to_responses(payload)
                                visible = str(coerced.get("output_text") or "")
                                if visible and not text_parts:
                                    text_parts.append(visible)
                                    yield ModelStreamEvent(delta=visible)
                                last_response = coerced
                                continue
                            event_type = str(payload.get("type") or current_event or "")
                            if event_type in {"error", "response.error"}:
                                error = payload.get("error") or payload
                                raise _responses_event_error(
                                    "Responses streaming 返回错误事件",
                                    error,
                                    self._api_key,
                                )
                            response = payload.get("response")
                            if isinstance(response, dict):
                                last_response = response
                                model_name = str(response.get("model") or model_name)
                                status = str(response.get("status") or "").lower()
                                incomplete = response.get("incomplete_details") or {}
                                incomplete_reason = (
                                    str(incomplete.get("reason") or "")
                                    if isinstance(incomplete, dict)
                                    else ""
                                )
                                if status in {"failed", "cancelled"} or (
                                    status == "incomplete"
                                    and incomplete_reason not in _RESPONSES_ALLOWED_INCOMPLETE_REASONS
                                ):
                                    raise _responses_event_error(
                                        f"Responses streaming 结束状态异常: {status}",
                                        payload,
                                        self._api_key,
                                    )
                                if event_type in {"response.completed", "response.incomplete"}:
                                    terminal_sent = True
                                    yield ModelStreamEvent(response=terminal_response())
                                    return
                                if event_type == "response.failed":
                                    raise _responses_event_error(
                                        "Responses streaming 返回失败事件",
                                        payload,
                                        self._api_key,
                                    )
                            if event_type == "response.output_text.delta":
                                delta = payload.get("delta")
                                if isinstance(delta, str) and delta:
                                    text_parts.append(delta)
                                    yield ModelStreamEvent(delta=delta)
                            elif event_type == "response.output_text.done":
                                text = payload.get("text")
                                if isinstance(text, str) and text and not text_parts:
                                    text_parts.append(text)
                                    yield ModelStreamEvent(delta=text)
                            elif event_type in {
                                "response.reasoning_summary_text.delta",
                                "response.reasoning_text.delta",
                            }:
                                delta = payload.get("delta")
                                if isinstance(delta, str) and delta:
                                    reasoning_parts.append(delta)
                                    yield ModelStreamEvent(reasoning_delta=delta)
                            elif event_type in {
                                "response.reasoning_summary_text.done",
                                "response.reasoning_text.done",
                            }:
                                text = payload.get("text")
                                if isinstance(text, str) and text and not reasoning_parts:
                                    reasoning_parts.append(text)
                                    yield ModelStreamEvent(reasoning_delta=text)
                            elif event_type in {
                                "response.output_item.added",
                                "response.output_item.done",
                            }:
                                item = payload.get("item")
                                if (
                                    event_type == "response.output_item.done"
                                    and isinstance(item, dict)
                                    and item.get("type")
                                ):
                                    output_index = payload.get("output_index")
                                    if isinstance(output_index, int):
                                        indexed_output_items[output_index] = dict(item)
                                    else:
                                        unindexed_output_items.append(dict(item))
                                if isinstance(item, dict) and item.get("type") == "function_call":
                                    item_id = str(item.get("id") or "")
                                    call_id = str(item.get("call_id") or "")
                                    key = item_id or call_id
                                    if key:
                                        if item_id:
                                            function_call_aliases[item_id] = key
                                        if call_id:
                                            function_call_aliases[call_id] = key
                                        current = function_calls.get(key)
                                        next_item = dict(item)
                                        # done 事件有的实现会带完整 arguments，有的会
                                        # 省略；仅在省略时保留前面真实收到的分片。
                                        if current is not None and not isinstance(
                                            next_item.get("arguments"), str
                                        ):
                                            next_item["arguments"] = str(current.get("arguments") or "")
                                        if current is not None and not next_item.get("name"):
                                            next_item["name"] = current.get("name") or ""
                                        function_calls[key] = next_item
                            elif event_type == "response.function_call_arguments.delta":
                                raw_key = str(payload.get("item_id") or payload.get("call_id") or "")
                                key = function_call_aliases.get(raw_key, raw_key)
                                if key:
                                    if payload.get("item_id"):
                                        function_call_aliases[str(payload["item_id"])] = key
                                    if payload.get("call_id"):
                                        function_call_aliases[str(payload["call_id"])] = key
                                    item = function_calls.setdefault(
                                        key,
                                        {
                                            "type": "function_call",
                                            "call_id": str(payload.get("call_id") or ""),
                                            "name": str(payload.get("name") or ""),
                                            "arguments": "",
                                        },
                                    )
                                    delta = payload.get("delta")
                                    if isinstance(delta, str):
                                        item["arguments"] = str(item.get("arguments") or "") + delta
                            continue
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from None

        if not terminal_sent:
            raise LLMError(
                "Responses streaming 响应提前结束，缺少 response.completed / "
                "response.incomplete 终态",
                retryable=True,
            )

    async def stream_complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        if images and "images" in self._protocol_profile.hard_disabled_capabilities:
            raise LLMError(
                f"{self._protocol_profile.name} 不支持图片输入",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        if web_search and "web_search" in self._protocol_profile.hard_disabled_capabilities:
            raise LLMError(
                f"{self._protocol_profile.name} 不支持原生联网搜索",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_RESPONSES)
        headers = _llm_headers(
            identity=self._identity,
            accept="text/event-stream",
            compatibility_headers=self._compatibility_headers,
            runtime_headers=self._runtime_headers(),
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if images:
            input_content: object = [
                {"type": "input_text", "text": user},
                *[{"type": "input_image", "image_url": _to_data_url(img)} for img in images],
            ]
        else:
            input_content = user
        body = {
            "model": self._model,
            "instructions": system,
            "input": [
                {"role": "user", "content": input_content},
            ],
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning"] = {"effort": normalized_effort}
        if web_search:
            size = (web_search_context_size or "medium").lower()
            if size not in {"low", "medium", "high"}:
                size = "medium"
            body["tools"] = [{"type": "web_search", "search_context_size": size}]
            body["include"] = ["web_search_call.action.sources"]
        body = plan_responses_body(body, self._protocol_profile)

        client_kwargs = self._client_kwargs(timeout_seconds)

        model_name = self._model
        input_tokens = 0
        output_tokens = 0
        final_sent = False
        saw_output_text = False

        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                async with cli.stream("POST", url, headers=headers, json=body) as resp:
                    self._capture_response_metadata(resp)
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_text():
                            error_body += chunk
                            if len(error_body) > 500:
                                break
                        raise LLMError(
                            _safe_error_message(
                                f"Responses streaming 接口返回 {resp.status_code}: {error_body[:200]}{_diagnostic_hint(resp.status_code, error_body)}",
                                self._api_key,
                            ),
                            retryable=_is_retryable_status(resp.status_code),
                            scope=_error_scope_for_http(resp.status_code, error_body),
                            status_code=resp.status_code,
                            **self._http_error_diagnostic_kwargs(resp, error_body),
                        )

                    response_headers = getattr(resp, "headers", {})
                    content_type = str(response_headers.get("content-type") or "")
                    if "json" in content_type.lower():
                        payload = await _read_limited_stream_json(resp)
                        result = _completed_json_as_stream_result(
                            payload,
                            api_format=LLM_API_FORMAT_RESPONSES,
                            default_model=self._model,
                            api_key=self._api_key,
                        )
                        if result.text:
                            yield LLMStreamChunk(
                                delta=result.text,
                                model=result.model,
                                stream_fallback=True,
                            )
                        yield LLMStreamChunk(
                            model=result.model,
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            done=True,
                            stream_fallback=True,
                        )
                        return

                    current_event = ""
                    async for line in _iter_limited_sse_lines(resp):
                        line = line.rstrip("\r\n")
                        if line.startswith("event:"):
                            current_event = line.removeprefix("event:").strip()
                            continue
                        if line.startswith("data:"):
                            raw = line.removeprefix("data:").strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            payload_type = str(payload.get("type") or current_event or "")
                            if payload_type in {"error", "response.error"}:
                                error = payload.get("error") or payload
                                raise _responses_event_error(
                                    "Responses streaming 返回错误事件",
                                    error,
                                    self._api_key,
                                )
                            response = payload.get("response")
                            if isinstance(response, dict):
                                model_name = str(response.get("model") or model_name)
                                usage = response.get("usage") or {}
                                input_tokens = int(usage.get("input_tokens") or input_tokens or 0)
                                output_tokens = int(usage.get("output_tokens") or output_tokens or 0)
                                status = str(response.get("status") or "")
                                if status in {"failed", "cancelled"}:
                                    raise _responses_event_error(
                                        f"Responses streaming 结束状态异常: {status}",
                                        payload,
                                        self._api_key,
                                    )
                                if payload_type in {"response.completed", "response.incomplete"}:
                                    final_sent = True
                                    yield LLMStreamChunk(
                                        model=model_name,
                                        input_tokens=input_tokens,
                                        output_tokens=output_tokens,
                                        done=True,
                                    )
                                    return
                                if payload_type == "response.failed":
                                    raise _responses_event_error(
                                        "Responses streaming 返回失败事件",
                                        payload,
                                        self._api_key,
                                    )
                            if payload_type == "response.output_text.delta":
                                delta = payload.get("delta")
                                if isinstance(delta, str) and delta:
                                    saw_output_text = True
                                    yield LLMStreamChunk(delta=delta, model=model_name)
                            elif payload_type == "response.output_text.done":
                                text = payload.get("text")
                                if (
                                    isinstance(text, str)
                                    and text
                                    and not saw_output_text
                                    and not final_sent
                                ):
                                    saw_output_text = True
                                    yield LLMStreamChunk(
                                        delta=text,
                                        model=model_name,
                                        stream_fallback=True,
                                    )
                            continue
                        if not line:
                            current_event = ""
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from None

        if not final_sent:
            raise LLMError(
                "Responses streaming 响应提前结束，缺少 response.completed / "
                "response.incomplete 终态",
                retryable=True,
            )

    async def transcribe(self, audio: bytes, model: str) -> str:
        """OpenAI Responses 协议厂商一般也在同一个 base_url 下挂 ``/audio/transcriptions``——
        直接复用 Whisper 协议（与 ``OpenAIClient.transcribe`` 同实现）。"""
        # 复用 OpenAIClient 的 transcribe；二者只差 chat/responses 那条主 endpoint
        return await OpenAIClient.transcribe(self, audio, model)  # type: ignore[arg-type]

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        """Responses API image_generation tool: ``POST /responses``.

        这条路径适合普通主模型（例如 gpt-5.x）调用原生图片生成工具。无参考图
        时显式 ``action=generate``，有参考图时交给 ``auto``，让上游决定生成或编辑。
        """
        if web_search:
            raise LLMError("图片生成不支持联网搜索，请关闭 web_search")
        if "images" in self._protocol_profile.hard_disabled_capabilities:
            raise LLMError(
                f"{self._protocol_profile.name} 不支持图片生成",
                scope=LLMErrorScope.CAPABILITY_MISMATCH,
            )
        url = provider_endpoint(self._base_url, LLM_API_FORMAT_RESPONSES)
        headers = _llm_headers(
            identity=self._identity,
            accept="application/json",
            compatibility_headers=self._compatibility_headers,
            runtime_headers=self._runtime_headers(),
        )
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        if images:
            input_content: object = [
                {"type": "input_text", "text": user},
                *[{"type": "input_image", "image_url": _to_data_url(img)} for img in images],
            ]
        else:
            input_content = user

        image_tool: dict[str, Any] = {"type": "image_generation"}
        if images:
            image_tool["action"] = "auto"
        else:
            image_tool["action"] = "generate"

        body = {
            "model": self._model,
            "instructions": system,
            "input": [
                {"role": "user", "content": input_content},
            ],
            "tools": [image_tool],
            "tool_choice": {"type": "image_generation"},
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        normalized_temperature = _normalize_temperature(temperature)
        if normalized_temperature is not None:
            body["temperature"] = normalized_temperature
        normalized_effort = _normalize_reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            body["reasoning"] = {"effort": normalized_effort}
        body = plan_responses_body(body, self._protocol_profile)

        client_kwargs = self._client_kwargs(timeout_seconds)
        try:
            async with httpx.AsyncClient(**client_kwargs) as cli:
                resp = await _post_responses_compatible(cli, url, headers=headers, body=body)
        except httpx.HTTPError as exc:
            raise LLMError(
                _safe_error_message(
                    _describe_http_error(exc, self._base_url),
                    self._api_key,
                ),
                retryable=True,
            ) from None

        if resp.status_code >= 400:
            raise LLMError(
                _safe_error_message(
                    f"Responses 生图接口返回 {resp.status_code}: {_response_text(resp)[:200]}{_diagnostic_hint(resp.status_code, _response_text(resp))}",
                    self._api_key,
                ),
                retryable=_is_retryable_status(resp.status_code),
                scope=_error_scope_for_http(resp.status_code, _response_text(resp)),
                status_code=resp.status_code,
            )
        data = _decode_responses_payload("Responses 生图", resp, self._api_key)

        image_data, image_urls, output_text = _extract_response_image_outputs(data)
        if not image_data and not image_urls:
            hint = output_text or str(data)[:200]
            raise LLMError(f"Responses 生图返回中没有图片数据：{hint}")
        usage = data.get("usage") or {}
        return LLMResult(
            text=output_text,
            model=str(data.get("model") or self._model),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            image_urls=image_urls,
            image_data=image_data,
            sources=_extract_response_sources(data),
        )


class GatewayResponsesClient(ResponsesClient):
    """复用 Responses codec，仅把 transport 与内部路由元数据切到 Unix Socket。"""

    def __init__(
        self,
        *,
        provider_id: int,
        model: str,
        socket_path: str,
        request_scope: str = REQUEST_SCOPE_INFERENCE,
    ) -> None:
        super().__init__(
            api_key="",
            base_url="http://gateway/v1",
            model=model,
            protocol_profile="codex_responses",
            provider_scope=f"gateway-provider:{provider_id}",
            identity=None,
            compatibility_headers=None,
        )
        self._gateway_provider_id = int(provider_id)
        self._gateway_socket_path = socket_path
        self._gateway_request_scope = request_scope
        self._last_gateway_version: str | None = None
        self._last_gateway_request_id: str | None = None
        self._last_gateway_stage: str | None = None
        self._last_outbound_request_id: str | None = None
        self._gateway_response_seen = False

    def _reset_gateway_diagnostics(self) -> None:
        self._last_gateway_request_id = None
        self._last_gateway_stage = None
        self._last_outbound_request_id = None
        self._gateway_response_seen = False
        try:
            from .gateway_runtime import gateway_runtime_manager

            self._last_gateway_version = gateway_runtime_manager.status().version
        except Exception:  # noqa: BLE001 - diagnostics must never block a call
            self._last_gateway_version = None

    def _runtime_headers(self, request: ModelRequest | None = None) -> dict[str, str]:
        context = ClientRuntimeContext.from_metadata(
            request.metadata if request is not None else None,
            provider_scope=self._provider_scope,
        )
        self._last_outbound_request_id = context.request_id
        return {
            "X-TelePilot-Provider-ID": str(self._gateway_provider_id),
            "X-TelePilot-Request-Scope": self._gateway_request_scope,
            "X-TelePilot-Request-ID": context.request_id,
            "X-TelePilot-Session-ID": context.session_id,
            "X-TelePilot-Run-ID": context.run_id,
            "X-TelePilot-Turn-ID": context.turn_id,
            "X-TelePilot-Turn-Index": str(context.turn_index),
        }

    def _client_kwargs(self, timeout_seconds: int | None) -> dict[str, object]:
        return {
            "timeout": _timeout_for_call(self._base_url, timeout_seconds),
            "transport": httpx.AsyncHTTPTransport(uds=self._gateway_socket_path),
            "trust_env": False,
        }

    @property
    def execution_backend(self) -> str:
        return LLM_EXECUTION_BACKEND_CODEX_GATEWAY

    @property
    def gateway_version(self) -> str | None:
        return self._last_gateway_version

    @property
    def gateway_request_id(self) -> str | None:
        return self._last_gateway_request_id or self._last_outbound_request_id

    @property
    def gateway_stage(self) -> str | None:
        return self._last_gateway_stage

    def _capture_response_metadata(self, response: httpx.Response) -> None:
        self._gateway_response_seen = True
        headers = response.headers
        version = str(headers.get("X-TelePilot-Gateway-Version") or "").strip()
        request_id = str(headers.get("X-TelePilot-Gateway-Request-ID") or "").strip()
        stage = str(headers.get("X-TelePilot-Gateway-Stage") or "").strip()
        if version:
            self._last_gateway_version = version[:64]
        if request_id:
            self._last_gateway_request_id = request_id[:128]
        if stage:
            self._last_gateway_stage = stage[:64]

    def _http_error_diagnostic_kwargs(
        self,
        response: httpx.Response,
        body: str,
    ) -> dict[str, Any]:
        self._capture_response_metadata(response)
        payload: str | Mapping[str, Any] = body
        try:
            decoded = json.loads(body)
            if isinstance(decoded, Mapping):
                payload = decoded
                error = decoded.get("error")
                source = error if isinstance(error, Mapping) else decoded
                request_id = str(source.get("request_id") or "").strip()
                stage = str(source.get("gateway_stage") or "").strip()
                if request_id:
                    self._last_gateway_request_id = request_id[:128]
                if stage:
                    self._last_gateway_stage = stage[:64]
        except (TypeError, ValueError):
            pass
        fact = llm_diag.diagnose_http_error(
            response.status_code,
            payload,
            request_id=self._last_gateway_request_id or self._last_outbound_request_id,
            gateway_stage=self._last_gateway_stage,
        )
        return {
            "category": fact.category,
            "upstream_status_code": fact.upstream_status_code,
            "upstream_error_code": fact.upstream_error_code,
            "upstream_error_message": fact.upstream_error_message,
            "upstream_error_detail": fact.upstream_error_detail,
            "upstream_request_id": fact.upstream_request_id,
            "client_request_id": fact.client_request_id,
            "request_id": fact.request_id,
            "gateway_stage": fact.gateway_stage,
            "gateway_version": self._last_gateway_version,
            "execution_backend": LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
            "upstream_summary": fact.upstream_summary,
        }

    def _network_error(self, exc: httpx.HTTPError) -> LLMError:
        return LLMError(
            _safe_error_message(_describe_http_error(exc, self._base_url), None),
            retryable=True,
            scope=LLMErrorScope.TRANSIENT,
            category=llm_diag.DIAG_GATEWAY_UNAVAILABLE,
            request_id=self._last_outbound_request_id,
            gateway_stage="transport",
            gateway_version=self._last_gateway_version,
            execution_backend=LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
        )

    def _enrich_gateway_error(self, exc: LLMError) -> LLMError:
        exc.request_id = exc.request_id or self._last_gateway_request_id or self._last_outbound_request_id
        exc.gateway_stage = (
            exc.gateway_stage
            or self._last_gateway_stage
            or ("upstream" if self._gateway_response_seen else "transport")
        )
        exc.gateway_version = exc.gateway_version or self._last_gateway_version
        exc.execution_backend = LLM_EXECUTION_BACKEND_CODEX_GATEWAY
        if not self._gateway_response_seen and exc.category == llm_diag.DIAG_NETWORK_ERROR:
            exc.category = llm_diag.DIAG_GATEWAY_UNAVAILABLE
            exc.scope = LLMErrorScope.TRANSIENT
            exc.retryable = True
        return exc

    def _decorate_result(self, result: LLMResult) -> LLMResult:
        result.execution_backend = LLM_EXECUTION_BACKEND_CODEX_GATEWAY
        result.gateway_version = self._last_gateway_version
        result.gateway_request_id = self._last_gateway_request_id or self._last_outbound_request_id
        result.gateway_stage = None
        return result

    def _decorate_response(self, response: ModelResponse) -> ModelResponse:
        return replace(
            response,
            execution_backend=LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
            gateway_version=self._last_gateway_version,
            gateway_request_id=self._last_gateway_request_id or self._last_outbound_request_id,
            gateway_stage=None,
        )

    async def transcribe(self, audio: bytes, model: str) -> str:
        del audio, model
        raise NotImplementedError("Codex 客户端兼容模式（Gateway）暂不支持语音转写")

    async def generate_image(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        images: list[bytes] | None = None,
        web_search: bool = False,
        web_search_context_size: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
    ) -> LLMResult:
        del (
            system,
            user,
            max_tokens,
            images,
            web_search,
            web_search_context_size,
            temperature,
            reasoning_effort,
            timeout_seconds,
        )
        raise NotImplementedError("Codex 客户端兼容模式（Gateway）暂不支持图片生成")

    async def complete(self, *args: Any, **kwargs: Any) -> LLMResult:
        self._reset_gateway_diagnostics()
        try:
            return self._decorate_result(await super().complete(*args, **kwargs))
        except LLMError as exc:
            raise self._enrich_gateway_error(exc) from None

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        self._reset_gateway_diagnostics()
        try:
            return self._decorate_response(await super().invoke(request))
        except LLMError as exc:
            raise self._enrich_gateway_error(exc) from None

    async def stream_invoke(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        self._reset_gateway_diagnostics()
        try:
            async for event in super().stream_invoke(request):
                if event.response is not None:
                    event = replace(event, response=self._decorate_response(event.response))
                yield event
        except LLMError as exc:
            raise self._enrich_gateway_error(exc) from None

    async def stream_complete(self, *args: Any, **kwargs: Any) -> AsyncIterator[LLMStreamChunk]:
        self._reset_gateway_diagnostics()
        try:
            async for chunk in super().stream_complete(*args, **kwargs):
                if chunk.done:
                    chunk = replace(
                        chunk,
                        execution_backend=LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
                        gateway_version=self._last_gateway_version,
                        gateway_request_id=(self._last_gateway_request_id or self._last_outbound_request_id),
                        gateway_stage=None,
                    )
                yield chunk
        except LLMError as exc:
            raise self._enrich_gateway_error(exc) from None

    async def list_models(self) -> list[str]:
        """List upstream models through the same Provider-bound Unix Socket."""

        self._reset_gateway_diagnostics()
        headers = _llm_headers(
            accept="application/json",
            runtime_headers=self._runtime_headers(),
        )
        try:
            async with httpx.AsyncClient(**self._client_kwargs(15)) as client:
                response = await client.get(f"{self._base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from None
        self._capture_response_metadata(response)
        if response.status_code >= 400:
            body = _response_text(response)
            raise LLMError(
                _safe_error_message(
                    f"Gateway 模型列表返回 {response.status_code}: {body[:200]}",
                    None,
                ),
                retryable=_is_retryable_status(response.status_code),
                scope=_error_scope_for_http(response.status_code, body),
                status_code=response.status_code,
                **self._http_error_diagnostic_kwargs(response, body),
            )
        try:
            payload = response.json()
        except ValueError:
            raise LLMError(
                "Gateway 模型列表响应不是合法 JSON",
                category=llm_diag.DIAG_INVALID_RESPONSE,
                request_id=self._last_gateway_request_id or self._last_outbound_request_id,
                gateway_stage="response",
                gateway_version=self._last_gateway_version,
                execution_backend=LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
            ) from None
        items = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            raise LLMError(
                "Gateway 模型列表响应缺少 data 数组",
                category=llm_diag.DIAG_INVALID_RESPONSE,
                request_id=self._last_gateway_request_id or self._last_outbound_request_id,
                gateway_stage="response",
                gateway_version=self._last_gateway_version,
                execution_backend=LLM_EXECUTION_BACKEND_CODEX_GATEWAY,
            )
        seen: set[str] = set()
        models: list[str] = []
        for item in items:
            model = str(item.get("id") or "").strip() if isinstance(item, Mapping) else ""
            if model and len(model) <= 128 and model not in seen:
                seen.add(model)
                models.append(model)
            if len(models) >= 200:
                break
        return models


# ────────────────────────────────────────────────────────────
# 工厂 & 安全工具
# ────────────────────────────────────────────────────────────


class LLMErrorScope(StrEnum):
    """Decides whether an error belongs to this provider or the whole request."""

    TRANSIENT = "transient"
    PROVIDER_LOCAL = "provider_local"
    CAPABILITY_MISMATCH = "capability_mismatch"
    REQUEST_INVALID = "request_invalid"
    ACCOUNT_POLICY = "account_policy"
    # Premium-provider daily budget is provider-local for fallback purposes:
    # the request may continue on a cheaper provider, while account-wide
    # request/token budgets remain terminal.
    PREMIUM_DAILY = "premium_daily"
    UNKNOWN = "unknown"


def _diagnostic_body_for_error(
    message: str,
    *,
    upstream_status_code: int | None,
    upstream_error_code: str | None,
    upstream_error_message: str | None,
    upstream_error_detail: str | None,
    upstream_request_id: str | None,
    client_request_id: str | None,
) -> str | Mapping[str, Any]:
    structured_values = (
        upstream_status_code,
        upstream_error_code,
        upstream_error_message,
        upstream_error_detail,
        upstream_request_id,
        client_request_id,
    )
    if not any(value is not None for value in structured_values):
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            return payload
    return {
        "error": {
            "message": message,
            "upstream_status_code": upstream_status_code,
            "upstream_error_code": upstream_error_code,
            "upstream_error_message": upstream_error_message,
            "upstream_error_detail": upstream_error_detail,
            "upstream_request_id": upstream_request_id,
            "client_request_id": client_request_id,
        }
    }


class LLMError(Exception):
    """LLM 调用层统一异常；message 已脱敏。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        scope: LLMErrorScope | str | None = None,
        status_code: int | None = None,
        category: str | None = None,
        upstream_status_code: int | None = None,
        upstream_error_code: str | None = None,
        upstream_error_message: str | None = None,
        upstream_error_detail: str | None = None,
        upstream_request_id: str | None = None,
        client_request_id: str | None = None,
        request_id: str | None = None,
        gateway_stage: str | None = None,
        gateway_version: str | None = None,
        execution_backend: str | None = None,
        upstream_summary: str | None = None,
    ):
        super().__init__(message)
        diagnostic_body = _diagnostic_body_for_error(
            message,
            upstream_status_code=upstream_status_code,
            upstream_error_code=upstream_error_code,
            upstream_error_message=upstream_error_message,
            upstream_error_detail=upstream_error_detail,
            upstream_request_id=upstream_request_id,
            client_request_id=client_request_id,
        )
        fact = (
            llm_diag.diagnose_http_error(
                status_code,
                diagnostic_body,
                request_id=request_id,
                gateway_stage=gateway_stage,
            )
            if status_code is not None
            else None
        )
        inferred_category = (
            fact.category
            if fact is not None
            else llm_diag.classify_message(message, retryable=retryable)
        )
        self.category = category or inferred_category
        self.retryable = fact.retryable if fact is not None else retryable
        self.scope = LLMErrorScope(
            (fact.scope if fact else None)
            or scope
            or (LLMErrorScope.TRANSIENT if self.retryable else LLMErrorScope.UNKNOWN)
        )
        self.status_code = status_code
        self.upstream_status_code = upstream_status_code or (
            fact.upstream_status_code if fact else None
        )
        self.upstream_error_code = upstream_error_code or (fact.upstream_error_code if fact else None)
        self.upstream_error_message = upstream_error_message or (
            fact.upstream_error_message if fact else None
        )
        self.upstream_error_detail = upstream_error_detail or (
            fact.upstream_error_detail if fact else None
        )
        self.upstream_request_id = upstream_request_id or (
            fact.upstream_request_id if fact else None
        )
        self.client_request_id = client_request_id or (
            fact.client_request_id if fact else None
        )
        self.request_id = request_id or (fact.request_id if fact else None)
        self.gateway_stage = gateway_stage or (fact.gateway_stage if fact else None)
        self.gateway_version = (gateway_version or "").strip()[:64] or None
        self.execution_backend = (execution_backend or "").strip()[:32] or None
        self.upstream_summary = upstream_summary or (fact.upstream_summary if fact else None)


class LLMCallFailed(Exception):
    """LLM 调用失败（捕获后用于 fallback 决策）。"""

    def __init__(
        self,
        message: str,
        provider_id: int | None = None,
        provider_name: str | None = None,
        error_type: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        scope: LLMErrorScope | str | None = None,
        category: str | None = None,
        upstream_status_code: int | None = None,
        upstream_error_code: str | None = None,
        upstream_error_message: str | None = None,
        upstream_error_detail: str | None = None,
        upstream_request_id: str | None = None,
        client_request_id: str | None = None,
        request_id: str | None = None,
        gateway_stage: str | None = None,
        gateway_version: str | None = None,
        execution_backend: str | None = None,
        upstream_summary: str | None = None,
    ):
        super().__init__(message)
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.error_type = error_type  # "timeout" / "network" / "rate_limit" / "auth" / "server_error"
        self.status_code = status_code
        diagnostic_body = _diagnostic_body_for_error(
            message,
            upstream_status_code=upstream_status_code,
            upstream_error_code=upstream_error_code,
            upstream_error_message=upstream_error_message,
            upstream_error_detail=upstream_error_detail,
            upstream_request_id=upstream_request_id,
            client_request_id=client_request_id,
        )
        fact = (
            llm_diag.diagnose_http_error(
                status_code,
                diagnostic_body,
                request_id=request_id,
                gateway_stage=gateway_stage,
            )
            if status_code is not None
            else None
        )
        self.category = category or (fact.category if fact else error_type) or llm_diag.DIAG_INVALID_RESPONSE
        self.retryable = fact.retryable if fact is not None else retryable
        self.scope = LLMErrorScope((fact.scope if fact else None) or scope or LLMErrorScope.UNKNOWN)
        self.upstream_status_code = upstream_status_code or (
            fact.upstream_status_code if fact else None
        )
        self.upstream_error_code = upstream_error_code or (fact.upstream_error_code if fact else None)
        self.upstream_error_message = upstream_error_message or (
            fact.upstream_error_message if fact else None
        )
        self.upstream_error_detail = upstream_error_detail or (
            fact.upstream_error_detail if fact else None
        )
        self.upstream_request_id = upstream_request_id or (
            fact.upstream_request_id if fact else None
        )
        self.client_request_id = client_request_id or (
            fact.client_request_id if fact else None
        )
        self.request_id = request_id or (fact.request_id if fact else None)
        self.gateway_stage = gateway_stage or (fact.gateway_stage if fact else None)
        self.gateway_version = (gateway_version or "").strip()[:64] or None
        self.execution_backend = (execution_backend or "").strip()[:32] or None
        self.upstream_summary = upstream_summary or (fact.upstream_summary if fact else None)


def _is_retryable_status(status_code: int) -> bool:
    """HTTP 状态码是否值得重试：429 限流与 5xx 服务端错误可重试，4xx 认证/配置类不可重试。"""
    return status_code == 429 or 500 <= status_code < 600


def _error_scope_for_http(status_code: int, body: str = "") -> LLMErrorScope:
    """Classify HTTP failures without treating every 4xx as interchangeable."""

    if _is_retryable_status(status_code):
        return LLMErrorScope.TRANSIENT
    normalized = body.lower()
    if status_code in {401, 404}:
        return LLMErrorScope.PROVIDER_LOCAL
    if status_code == 403:
        if any(word in normalized for word in ("policy", "safety", "moderation", "blocked")):
            return LLMErrorScope.ACCOUNT_POLICY
        return LLMErrorScope.PROVIDER_LOCAL
    if status_code == 400:
        if any(
            word in normalized
            for word in (
                "model_not_found",
                "model not found",
                "unknown model",
                "deployment not found",
            )
        ):
            return LLMErrorScope.PROVIDER_LOCAL
        if any(word in normalized for word in ("unsupported", "does not support", "not supported")):
            return LLMErrorScope.CAPABILITY_MISMATCH
        return LLMErrorScope.REQUEST_INVALID
    if status_code in {409, 422}:
        return LLMErrorScope.REQUEST_INVALID
    return LLMErrorScope.UNKNOWN


def _safe_error_message(
    msg: str,
    api_key: str | None,
    additional_secrets: Iterable[str] | None = None,
) -> str:
    """把可能含敏感信息的错误文本脱敏。

    - 若 api_key 出现在 msg 中，整段替换为 ``<redacted>``
    - 兜底过滤 ``sk-...`` / ``Bearer ...`` 形态
    - 过滤其他常见 token 格式
    """
    import re

    if not msg:
        return ""
    out = msg
    exact_secrets = [api_key] if api_key else []
    exact_secrets.extend(_ERROR_SECRET_VALUES.get())
    exact_secrets.extend(str(value) for value in (additional_secrets or ()) if str(value))
    for secret in sorted(set(exact_secrets), key=len, reverse=True):
        out = out.replace(secret, "<redacted>")
    # 统一截断，避免长串敏感数据透出
    if len(out) > 400:
        out = out[:400] + "..."
    # 正则过滤常见 token 格式（独立于 api_key 变量）
    # sk- 开头的 key
    out = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "<sk>", out)
    # Bearer token
    out = re.sub(r"Bearer\s+[A-Za-z0-9_.\-]{8,}", "Bearer <token>", out)
    # 常见的其他 key 格式
    out = re.sub(
        r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9_.\-]{8,}['\"]?", r"\1=<redacted>", out
    )
    return out


def _diagnostic_hint(status: int, body: str) -> str:
    """只从统一诊断事实生成提示，禁止各 Client 自行解释状态码。"""

    category = llm_diag.classify_status_code(status, body)
    suggestion = llm_diag.suggestion_for(category)
    return f"  ↳ [{category}] {suggestion}" if suggestion else f"  ↳ [{category}]"


def _reasoning_transport_for_model(
    profile: ProviderProtocolProfile,
    models: Iterable[Mapping[str, Any]],
    model: str,
) -> str:
    """Only replay reasoning history when the provider explicitly supports it."""

    if (
        profile.name != LLM_PROTOCOL_PROFILE_STANDARD
        or LLM_API_FORMAT_CHAT_COMPLETIONS not in profile.api_formats
    ):
        return profile.reasoning_transport
    for metadata in models:
        if str(metadata.get("id") or "").strip() != model:
            continue
        value = metadata.get("reasoning_transport")
        if isinstance(value, str) and value in {
            "none",
            "reasoning_content",
            "responses_item",
            "encrypted_reasoning_item",
            "anthropic_thinking",
        }:
            return value
    return "native"


def _describe_http_error(exc: BaseException, base_url: str | None) -> str:
    """把 httpx 异常翻译成"用户能看懂的报错"。

    httpx 很多异常的 ``str(exc)`` 是空字符串（``ConnectError("")`` / SSL 握手错），
    单纯透 ``f"网络异常: {exc}"`` 会变成 "网络异常: " 难以排查。这里：

    - 总带上异常类名：``ConnectError`` / ``ReadTimeout`` / ``ProxyError`` / ``SSLError`` 等
    - 总带上目标 host（不带路径）：让用户一眼看出是 anthropic.com 还是 openai.com 不通
    - 细节为空时给一个建议性提示（"可能是 SSL/DNS/代理"）
    """
    name = type(exc).__name__
    detail = str(exc).strip()
    host = ""
    if base_url:
        try:
            from urllib.parse import urlparse

            host = urlparse(base_url).netloc or base_url
        except Exception:  # noqa: BLE001
            host = base_url

    parts = [f"网络异常 {name}"]
    if host:
        parts.append(f"→ {host}")
    if detail:
        parts.append(f": {detail}")
    else:
        parts.append("（无详情；常见原因：连不到目标域名 / SSL 握手失败 / 代理未生效）")
    return " ".join(parts)


def build_client(
    provider_row: LLMProvider,
    override_model: str | None = None,
    proxy_url: str | None = None,
    api_format_override: str | None = None,
    identity_override: str | None = None,
    request_scope: str = REQUEST_SCOPE_INFERENCE,
) -> LLMClient:
    """根据 ORM 行装配具体 LLMClient。

    协议路由（以 ``api_format`` 为准；老数据没这字段时按 ``provider`` 厂商兜底）：
    - ``chat_completions``     → ``OpenAIClient``        ``POST /chat/completions``
    - ``responses``            → ``ResponsesClient``     ``POST /responses``
    - ``anthropic_messages``   → ``AnthropicClient``     ``POST /messages``

    - 解密 api_key（若该 provider 行没有 key 字段则 client 拿空串）
    - ``override_model`` 优先于 provider.default_model
    - ``proxy_url`` 给 None 表示直连；socks5/http/https 都接受 httpx URL
    - 客户端身份依据"本次实际协议"（``fmt``，即 override 生效后的协议）解析；
      ``identity_override`` 供协议检测按顺序显式指定身份使用，正式业务调用留空
      即用 Provider 配置的 ``client_identity_profile``。
    """
    model = (override_model or provider_row.default_model or "").strip()
    if not model:
        raise ValueError("LLM provider 没配 default_model，且当次调用也未提供 model 覆盖")

    # api_format_override 用于联网搜索等单次调用协议覆盖；否则按 provider 配置。
    fmt = (
        api_format_override
        or getattr(provider_row, "api_format", None)
        or default_api_format_for(provider_row.provider)
    )
    execution_backend = getattr(provider_row, "execution_backend", "direct") or "direct"
    if execution_backend == LLM_EXECUTION_BACKEND_CODEX_GATEWAY:
        if fmt != LLM_API_FORMAT_RESPONSES:
            raise ValueError("Codex 客户端兼容模式（Gateway）仅支持 Responses")
        from .gateway_runtime import DEFAULT_GATEWAY_SOCKET

        return GatewayResponsesClient(
            provider_id=int(provider_row.id),
            model=model,
            socket_path=os.getenv("TELEPILOT_GATEWAY_SOCKET", DEFAULT_GATEWAY_SOCKET),
            request_scope=request_scope,
        )
    api_key = ""
    if provider_row.api_key_enc:
        api_key = decrypt_str(provider_row.api_key_enc)
    protocol_profile = resolve_protocol_profile(
        fmt,
        getattr(provider_row, "protocol_profile", LLM_PROTOCOL_PROFILE_STANDARD),
        base_url=getattr(provider_row, "base_url", None),
        model=model,
        infer_when_standard=True,
    )
    configured_identity = identity_override or getattr(provider_row, "client_identity_profile", None)
    identity = resolve_identity(
        configured_identity,
        fmt,
        recommended_profile=protocol_profile.recommended_identity,
    )
    compatibility_headers = request_headers_for_scope(
        getattr(provider_row, "request_headers_enc", None),
        request_scope,
    )
    reasoning_transport = _reasoning_transport_for_model(
        protocol_profile, getattr(provider_row, "models", None) or [], model
    )

    if fmt == LLM_API_FORMAT_CHAT_COMPLETIONS:
        # ollama 兜底 base_url（chat_completions 也兼容）
        base = provider_row.base_url
        if not base and provider_row.provider == LLM_PROVIDER_OLLAMA:
            base = "http://localhost:11434/v1"
        return OpenAIClient(
            api_key="" if provider_row.provider == LLM_PROVIDER_OLLAMA else api_key,
            base_url=base,
            model=model,
            proxy_url=proxy_url,
            identity=identity,
            compatibility_headers=compatibility_headers,
            reasoning_transport=reasoning_transport,
        )
    if fmt == LLM_API_FORMAT_RESPONSES:
        return ResponsesClient(
            api_key=api_key,
            base_url=provider_row.base_url,
            model=model,
            proxy_url=proxy_url,
            protocol_profile=protocol_profile.name,
            provider_scope=f"provider:{provider_row.id}|{protocol_profile.name}",
            identity=identity,
            compatibility_headers=compatibility_headers,
            reasoning_transport=reasoning_transport,
        )
    if fmt == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        return AnthropicClient(
            api_key=api_key,
            base_url=provider_row.base_url,
            model=model,
            proxy_url=proxy_url,
            protocol_profile=protocol_profile.name,
            provider_scope=f"provider:{provider_row.id}|{protocol_profile.name}",
            identity=identity,
            compatibility_headers=compatibility_headers,
            reasoning_transport=reasoning_transport,
        )
    raise ValueError(f"未知 api_format: {fmt}")


def build_client_from_dto(
    dto: LLMProviderDTO,
    override_model: str | None = None,
    proxy_url: str | None = None,
    api_format_override: str | None = None,
    identity_override: str | None = None,
    request_scope: str = REQUEST_SCOPE_INFERENCE,
) -> LLMClient:
    """根据 LLMProviderDTO 装配具体 LLMClient。

    与 build_client 等效，但输入是 DTO 而非 ORM 行。
    proxy_url 以参数传入优先，其次用 dto.proxy_url。

    Args:
        dto: LLMProviderDTO 对象
        override_model: 覆盖模型名（优先于 dto.default_model）
        proxy_url: 代理 URL（优先于 dto.proxy_url）
        identity_override: 显式身份档案（协议检测用）；留空用 dto 配置。
    """
    model = (override_model or dto.default_model or "").strip()
    if not model:
        raise ValueError("LLM provider 没配 default_model，且当次调用也未提供 model 覆盖")

    # api_format_override 用于联网搜索等单次调用协议覆盖；否则按 provider 配置。
    fmt = api_format_override or dto.api_format or default_api_format_for(dto.provider)
    if dto.execution_backend == LLM_EXECUTION_BACKEND_CODEX_GATEWAY:
        if fmt != LLM_API_FORMAT_RESPONSES:
            raise ValueError("Codex 客户端兼容模式（Gateway）仅支持 Responses")
        from .gateway_runtime import DEFAULT_GATEWAY_SOCKET

        return GatewayResponsesClient(
            provider_id=dto.id,
            model=model,
            socket_path=os.getenv("TELEPILOT_GATEWAY_SOCKET", DEFAULT_GATEWAY_SOCKET),
            request_scope=request_scope,
        )
    api_key = ""
    if dto.api_key_enc:
        api_key = decrypt_str(dto.api_key_enc)
    protocol_profile = resolve_protocol_profile(
        fmt,
        dto.protocol_profile,
        base_url=dto.base_url,
        model=model,
        infer_when_standard=True,
    )

    # proxy 合并：参数传入 > dto 内置
    final_proxy = proxy_url if proxy_url else dto.proxy_url
    # 身份依据本次实际协议解析（override 生效后）。
    configured_identity = identity_override or dto.client_identity_profile
    identity = resolve_identity(
        configured_identity,
        fmt,
        recommended_profile=protocol_profile.recommended_identity,
    )
    compatibility_headers = request_headers_for_scope(
        dto.request_headers_enc,
        request_scope,
    )
    reasoning_transport = _reasoning_transport_for_model(protocol_profile, dto.models, model)

    if fmt == LLM_API_FORMAT_CHAT_COMPLETIONS:
        base = dto.base_url
        if not base and dto.is_ollama:
            base = "http://localhost:11434/v1"
        return OpenAIClient(
            api_key="" if dto.is_ollama else api_key,
            base_url=base,
            model=model,
            proxy_url=final_proxy,
            identity=identity,
            compatibility_headers=compatibility_headers,
            reasoning_transport=reasoning_transport,
        )
    if fmt == LLM_API_FORMAT_RESPONSES:
        return ResponsesClient(
            api_key=api_key,
            base_url=dto.base_url,
            model=model,
            proxy_url=final_proxy,
            protocol_profile=protocol_profile.name,
            provider_scope=f"provider:{dto.id}|{protocol_profile.name}",
            identity=identity,
            compatibility_headers=compatibility_headers,
            reasoning_transport=reasoning_transport,
        )
    if fmt == LLM_API_FORMAT_ANTHROPIC_MESSAGES:
        return AnthropicClient(
            api_key=api_key,
            base_url=dto.base_url,
            model=model,
            proxy_url=final_proxy,
            protocol_profile=protocol_profile.name,
            provider_scope=f"provider:{dto.id}|{protocol_profile.name}",
            identity=identity,
            compatibility_headers=compatibility_headers,
            reasoning_transport=reasoning_transport,
        )
    raise ValueError(f"未知 api_format: {fmt}")


__all__ = [
    "AnthropicClient",
    "GatewayResponsesClient",
    "LLMCallFailed",
    "LLMClient",
    "LLMError",
    "LLMErrorScope",
    "LLMResult",
    "LLMStreamChunk",
    "OpenAIClient",
    "ResponsesClient",
    "build_client",
    "build_client_from_dto",
]
