from __future__ import annotations

import pytest

from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import (
    ApiFormat,
    ImageContent,
    Message,
    MessageRole,
    ModelMessage,
    ModelRequest,
    StopReason,
    TextContent,
    ToolSpec,
    UnsupportedCapabilityError,
    Usage,
    build_endpoint,
    capabilities_for,
    capabilities_for_api_format,
    normalize_base_url,
    provider_endpoint,
    stop_reason_from_provider,
)


def test_normalize_base_url_strips_known_endpoint_query_and_fragment() -> None:
    assert normalize_base_url(
        "https://api.example/v1/chat/completions?x=1#fragment"
    ) == "https://api.example/v1"


def test_normalize_base_url_rejects_non_absolute_urls() -> None:
    for value in ("api.example/v1", "ftp://api.example/v1"):
        try:
            normalize_base_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {value}")
    assert normalize_base_url(
        "https://api.example/v1/responses/responses"
    ) == "https://api.example/v1"


def test_provider_endpoint_preserves_custom_version_path() -> None:
    assert provider_endpoint(
        "https://api.example/custom/v2/messages",
        "anthropic_messages",
    ) == "https://api.example/custom/v2/messages"
    assert provider_endpoint(
        "https://api.example/v1/responses",
        "chat_completions",
    ) == "https://api.example/v1/chat/completions"


def test_provider_endpoint_adds_protocol_default_version() -> None:
    assert build_endpoint(
        "https://api.example",
        ApiFormat.CHAT_COMPLETIONS,
    ) == "https://api.example/v1/chat/completions"


def test_provider_endpoint_does_not_invent_version_inside_custom_path() -> None:
    assert provider_endpoint(
        "https://gateway.example/openai",
        ApiFormat.CHAT_COMPLETIONS,
    ) == "https://gateway.example/openai/chat/completions"


def test_protocol_capabilities_reject_unsupported_features() -> None:
    request = ModelRequest(
        model="claude",
        messages=(
            ModelMessage(
                role=MessageRole.USER,
                content=(ImageContent(b"image/png", mime_type="image/png"),),
            ),
        ),
        tools=(ToolSpec("lookup", "Lookup", {"type": "object"}),),
        reasoning_effort="high",
        web_search=True,
    )

    errors = capabilities_for_api_format("anthropic_messages").validation_errors(request)

    assert errors == ["provider 不支持原生联网搜索"]


def test_capability_validation_can_raise_structured_error() -> None:
    request = ModelRequest(
        model="model",
        messages=(Message.text(MessageRole.USER, "hello"),),
        web_search=True,
    )
    try:
        capabilities_for(ApiFormat.CHAT_COMPLETIONS).validate(request, ApiFormat.CHAT_COMPLETIONS)
    except UnsupportedCapabilityError as exc:
        assert exc.api_format is ApiFormat.CHAT_COMPLETIONS
        assert exc.errors == ("provider 不支持原生联网搜索",)
    else:
        raise AssertionError("expected UnsupportedCapabilityError")


def test_request_usage_and_image_validation() -> None:
    request = ModelRequest(
        model="model",
        messages=(Message.text(MessageRole.USER, "hello"),),
        temperature=0.5,
    )
    assert isinstance(request.messages[0].content[0], TextContent)
    assert Usage(input_tokens=3, output_tokens=2).total_tokens == 5

    for factory in (
        lambda: ImageContent(),
        lambda: ImageContent(data=b"image"),
        lambda: ModelRequest(model="model", messages=(), temperature=0.5),
        lambda: ModelRequest(model="model", messages=(Message.text(MessageRole.USER, "x"),), temperature=-0.1),
    ):
        try:
            factory()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_stop_reason_normalization() -> None:
    assert stop_reason_from_provider("end_turn") is StopReason.COMPLETED
    assert stop_reason_from_provider("length") is StopReason.MAX_TOKENS
    assert stop_reason_from_provider("tool_use") is StopReason.TOOL_CALLS
    assert stop_reason_from_provider("incomplete") is StopReason.FAILED


def test_model_metadata_narrows_protocol_capabilities() -> None:
    provider = LLMProviderDTO(
        id=1,
        name="provider",
        provider="openai",
        api_format="responses",
        default_model="model",
        models=[
            {
                "id": "model",
                "supports_tools": False,
                "supports_temperature": False,
                "reasoning_efforts": ["low", "high"],
            }
        ],
    )
    request = ModelRequest(
        model="model",
        messages=(Message.text(MessageRole.USER, "hello"),),
        tools=(ToolSpec("lookup", "Lookup", {"type": "object"}),),
        temperature=0.5,
        reasoning_effort="xhigh",
    )

    assert provider.capabilities_for_model("model").validation_errors(request) == [
        "provider 不支持原生工具调用",
        "provider 不支持 temperature",
        "provider 不支持 reasoning_effort=xhigh",
    ]


@pytest.mark.parametrize("protocol_profile", ["standard", "claude_code_proxy"])
def test_anthropic_profiles_declare_reasoning_effort_capability(
    protocol_profile: str,
) -> None:
    provider = LLMProviderDTO(
        id=1,
        name="Anthropic provider",
        provider="anthropic",
        api_format="anthropic_messages",
        protocol_profile=protocol_profile,
        default_model="claude-sonnet-4-6",
    )
    request = ModelRequest(
        model="claude-sonnet-4-6",
        messages=(Message.text(MessageRole.USER, "question"),),
        reasoning_effort="high",
    )

    assert provider.capabilities_for_model("claude-sonnet-4-6").validation_errors(request) == []


def test_anthropic_max_effort_is_only_inferred_for_opus() -> None:
    provider = LLMProviderDTO(
        id=1,
        name="Anthropic provider",
        provider="anthropic",
        api_format="anthropic_messages",
        default_model="claude-opus-4-6",
    )

    assert "max" in provider.capabilities_for_model("claude-opus-4-6").reasoning_efforts
    assert "max" not in provider.capabilities_for_model("claude-sonnet-4-6").reasoning_efforts
