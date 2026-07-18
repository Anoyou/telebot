"""Provider-neutral request and response types for LLM protocols."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ApiFormat(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    COMPLETED = "completed"
    MAX_TOKENS = "max_tokens"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TextContent:
    text: str


@dataclass(frozen=True)
class ImageContent:
    data: bytes | None = None
    mime_type: str | None = None
    url: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.url is None):
            raise ValueError("image content requires exactly one of data or url")
        if self.data is not None and not self.mime_type:
            raise ValueError("inline image content requires mime_type")


ContentBlock = TextContent | ImageContent


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class ModelMessage:
    role: MessageRole
    content: tuple[ContentBlock, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

    @classmethod
    def text(cls, role: MessageRole, value: str) -> ModelMessage:
        return cls(role=role, content=(TextContent(value),) if value else ())

    def text_content(self) -> str:
        return "\n".join(
            block.text for block in self.content if isinstance(block, TextContent)
        ).strip()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True


# This conservative subset is accepted by all native tool protocols we support.
# Internal registries can keep dotted names without inheriting wire constraints.
_WIRE_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_WIRE_TOOL_PREFIX = "telepilot_"


def wire_tool_name(name: str) -> str:
    """Return a deterministic protocol-safe alias for an internal tool name."""

    normalized = str(name).strip()
    if _WIRE_TOOL_NAME_RE.fullmatch(normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"{_WIRE_TOOL_PREFIX}{digest}"


def wire_tool_name_map(names: Iterable[str]) -> dict[str, str]:
    """Build an internal-to-wire map and reject the practically impossible collision."""

    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for name in names:
        internal = str(name).strip()
        if not internal or internal in mapping:
            continue
        wire = wire_tool_name(internal)
        previous = used.get(wire)
        if previous is not None and previous != internal:
            raise ValueError(f"tool wire name collision: {previous!r} and {internal!r}")
        mapping[internal] = wire
        used[wire] = internal
    return mapping


def to_wire_tool_name(name: str, mapping: Mapping[str, str]) -> str:
    return mapping.get(name, name)


def from_wire_tool_name(name: str, mapping: Mapping[str, str]) -> str:
    for internal, wire in mapping.items():
        if wire == name:
            return internal
    return name


class ToolChoiceMode(StrEnum):
    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True)
class NamedToolChoice:
    name: str


ToolChoice = ToolChoiceMode | NamedToolChoice


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice = ToolChoiceMode.AUTO
    max_output_tokens: int = 512
    temperature: float | None = None
    reasoning_effort: str | None = None
    stream: bool = False
    web_search: bool = False
    web_search_context_size: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not self.messages:
            raise ValueError("messages must not be empty")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.temperature is not None and self.temperature < 0:
            raise ValueError("temperature must not be negative")


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("token usage values must not be negative")
        if self.total_tokens == 0 and (self.input_tokens or self.output_tokens):
            object.__setattr__(self, "total_tokens", self.input_tokens + self.output_tokens)


@dataclass(frozen=True)
class ModelResponse:
    model: str
    content: tuple[ContentBlock, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    stop_reason: StopReason = StopReason.UNKNOWN
    provider_status: str | None = None
    sources: tuple[dict[str, str], ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(
            block.text for block in self.content if isinstance(block, TextContent)
        ).strip()


@dataclass(frozen=True)
class ProviderCapabilities:
    multi_turn: bool = True
    images: bool = False
    tools: bool = False
    streaming: bool = False
    web_search: bool = False
    temperature: bool = True
    reasoning: bool = False
    reasoning_efforts: frozenset[str] = frozenset()

    def validation_errors(self, request: ModelRequest) -> list[str]:
        errors: list[str] = []
        if len(request.messages) > 2 and not self.multi_turn:
            errors.append("provider 不支持多轮消息")
        if any(isinstance(block, ImageContent) for msg in request.messages for block in msg.content):
            if not self.images:
                errors.append("provider 不支持图片输入")
        if request.tools and not self.tools:
            errors.append("provider 不支持原生工具调用")
        if request.stream and not self.streaming:
            errors.append("provider 不支持流式输出")
        if request.web_search and not self.web_search:
            errors.append("provider 不支持原生联网搜索")
        if request.temperature is not None and not self.temperature:
            errors.append("provider 不支持 temperature")
        if request.reasoning_effort:
            if not self.reasoning:
                errors.append("provider 不支持 reasoning")
            elif self.reasoning_efforts and request.reasoning_effort not in self.reasoning_efforts:
                errors.append(
                    f"provider 不支持 reasoning_effort={request.reasoning_effort}"
                )
        return errors

    def validate(self, request: ModelRequest, api_format: ApiFormat | str) -> None:
        errors = self.validation_errors(request)
        if errors:
            raise UnsupportedCapabilityError(ApiFormat(api_format), tuple(errors))

    def with_overrides(self, **overrides: Any) -> ProviderCapabilities:
        values = {
            "multi_turn": self.multi_turn,
            "images": self.images,
            "tools": self.tools,
            "streaming": self.streaming,
            "web_search": self.web_search,
            "temperature": self.temperature,
            "reasoning": self.reasoning,
            "reasoning_efforts": self.reasoning_efforts,
        }
        unknown = set(overrides) - values.keys()
        if unknown:
            raise ValueError(f"unknown capability overrides: {', '.join(sorted(unknown))}")
        values.update(overrides)
        return ProviderCapabilities(**values)


class UnsupportedCapabilityError(ValueError):
    def __init__(self, api_format: ApiFormat, errors: tuple[str, ...]) -> None:
        self.api_format = api_format
        self.errors = errors
        super().__init__(f"{api_format.value}: {'; '.join(errors)}")


CAPABILITIES_BY_API_FORMAT: dict[str, ProviderCapabilities] = {
    "chat_completions": ProviderCapabilities(
        images=True,
        tools=True,
        streaming=True,
        reasoning=True,
    ),
    "responses": ProviderCapabilities(
        images=True,
        tools=True,
        streaming=True,
        web_search=True,
        reasoning=True,
    ),
    "anthropic_messages": ProviderCapabilities(
        images=True,
        tools=True,
        streaming=True,
        reasoning=True,
        reasoning_efforts=frozenset({"low", "medium", "high", "max"}),
    ),
}


def capabilities_for_api_format(api_format: str) -> ProviderCapabilities:
    normalized = ApiFormat(api_format).value
    return CAPABILITIES_BY_API_FORMAT[normalized]


def capabilities_for(api_format: ApiFormat | str) -> ProviderCapabilities:
    return capabilities_for_api_format(ApiFormat(api_format).value)


_KNOWN_ENDPOINTS = (
    re.compile(r"/models/[^/]+:(?:generateContent|streamGenerateContent)$", re.I),
    re.compile(r"/(?:chat/completions|completions|responses|messages)$", re.I),
)


def normalize_base_url(value: str) -> str:
    """Strip query, fragment, trailing slash, and a known full endpoint suffix."""

    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    path = parsed.path.rstrip("/")
    changed = True
    while changed:
        changed = False
        for pattern in _KNOWN_ENDPOINTS:
            next_path = pattern.sub("", path).rstrip("/")
            if next_path != path:
                path = next_path
                changed = True
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


_VERSION_SEGMENT = re.compile(r"^v\d+(?:beta|alpha)?$", re.I)


def _with_default_version(base_url: str, api_format: str) -> str:
    path = urlsplit(base_url).path.rstrip("/")
    last_segment = path.rsplit("/", 1)[-1]
    if _VERSION_SEGMENT.fullmatch(last_segment):
        return base_url
    # A non-empty custom path is part of the provider contract.  Inserting
    # /v1 into e.g. /openai or /gateway/anthropic silently breaks existing
    # OpenAI-compatible reverse proxies.  Only bare origins get a default.
    if path:
        return base_url
    return f"{base_url}/v1"


def provider_endpoint(base_url: str, api_format: ApiFormat | str, *, model: str | None = None) -> str:
    api_format = ApiFormat(api_format).value
    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("base_url 不能为空")
    base = _with_default_version(base, api_format)
    if api_format == "chat_completions":
        return f"{base}/chat/completions"
    if api_format == "responses":
        return f"{base}/responses"
    if api_format == "anthropic_messages":
        return f"{base}/messages"
    raise AssertionError("unreachable")


def build_endpoint(
    base_url: str,
    api_format: ApiFormat | str,
    *,
    model: str | None = None,
) -> str:
    return provider_endpoint(base_url, api_format, model=model)


def provider_models_endpoint(base_url: str, api_format: ApiFormat | str) -> str:
    normalized_format = ApiFormat(api_format).value
    base = _with_default_version(normalize_base_url(base_url), normalized_format)
    return f"{base}/models"


def stop_reason_from_provider(value: object) -> StopReason:
    normalized = str(value or "").strip().lower()
    if normalized in {"stop", "end_turn", "completed", "complete"}:
        return StopReason.COMPLETED
    if normalized in {"length", "max_tokens", "max_output_tokens"}:
        return StopReason.MAX_TOKENS
    if normalized in {"tool_calls", "tool_use", "function_call"}:
        return StopReason.TOOL_CALLS
    if normalized in {"content_filter", "safety"}:
        return StopReason.CONTENT_FILTER
    if normalized in {"refusal", "refused"}:
        return StopReason.REFUSAL
    if normalized in {"cancelled", "canceled"}:
        return StopReason.CANCELLED
    if normalized in {"failed", "error", "incomplete"}:
        return StopReason.FAILED
    return StopReason.UNKNOWN


__all__ = [
    "ApiFormat",
    "CAPABILITIES_BY_API_FORMAT",
    "ContentBlock",
    "ImageContent",
    "MessageRole",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "ProviderCapabilities",
    "StopReason",
    "TextContent",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "ToolChoiceMode",
    "NamedToolChoice",
    "UnsupportedCapabilityError",
    "Usage",
    "Message",
    "build_endpoint",
    "capabilities_for",
    "capabilities_for_api_format",
    "normalize_base_url",
    "provider_endpoint",
    "provider_models_endpoint",
    "stop_reason_from_provider",
    "from_wire_tool_name",
    "to_wire_tool_name",
    "wire_tool_name",
    "wire_tool_name_map",
]


Message = ModelMessage
Usage = ModelUsage
