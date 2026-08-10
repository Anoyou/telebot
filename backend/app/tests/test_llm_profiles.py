from __future__ import annotations

from app.db.models.command import normalize_protocol_profile
from app.services.llm_codecs.responses import plan_responses_body
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_profiles import infer_protocol_profile, resolve_protocol_profile
from app.services.llm_protocol import provider_models_endpoints


def _provider(**overrides) -> LLMProviderDTO:
    values = {
        "id": 1,
        "name": "provider",
        "provider": "openai",
        "api_format": "responses",
        "protocol_profile": "standard",
        "base_url": "https://api.example.com/v1",
        "default_model": "model",
    }
    values.update(overrides)
    return LLMProviderDTO(**values)


def test_protocol_profile_normalization_is_scoped_by_api_format() -> None:
    assert normalize_protocol_profile("responses", "deepseek_responses") == "deepseek_responses"
    assert normalize_protocol_profile("responses", "claude_code_proxy") == "standard"
    assert (
        normalize_protocol_profile("anthropic_messages", "claude_code_proxy")
        == "claude_code_proxy"
    )


def test_deepseek_profile_hard_limits_override_model_metadata() -> None:
    provider = _provider(
        protocol_profile="deepseek_responses",
        models=[
            {
                "id": "deepseek-v4-flash",
                "supports_images": True,
                "supports_web_search": True,
                "supports_parallel_tool_calls": True,
            }
        ],
    )

    capabilities = provider.capabilities_for_model("deepseek-v4-flash")

    assert capabilities.images is False
    assert capabilities.web_search is False
    assert capabilities.parallel_tool_calls is True
    assert capabilities.reasoning_transport == "responses_item"


def test_model_metadata_exposes_extended_capability_facts() -> None:
    provider = _provider(
        models=[
            {
                "id": "model",
                "context_window": 131_072,
                "max_output_tokens": 8192,
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
                "supported_api_formats": ["responses"],
                "reasoning_transport": "responses_item",
            }
        ]
    )

    capabilities = provider.capabilities_for_model("model")

    assert capabilities.context_window == 131_072
    assert capabilities.max_output_tokens == 8192
    assert capabilities.input_modalities == frozenset({"text", "image"})
    assert capabilities.protocol_compatible is True


def test_profile_inference_and_model_endpoint_candidates() -> None:
    assert (
        infer_protocol_profile(
            "responses",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
        )
        == "deepseek_responses"
    )
    profile = resolve_protocol_profile(
        "responses",
        "deepseek_responses",
        base_url="https://api.deepseek.com",
    )
    assert profile.recommended_identity == "openai_sdk"
    assert provider_models_endpoints(
        "https://api.deepseek.com/responses",
        "responses",
        protocol_profile=profile.name,
    ) == (
        "https://api.deepseek.com/models",
        "https://api.deepseek.com/v1/models",
    )


def test_responses_codec_applies_deepseek_and_codex_profile_rules() -> None:
    deepseek = resolve_protocol_profile("responses", "deepseek_responses")
    codex = resolve_protocol_profile("responses", "codex_responses")
    body = {
        "model": "model",
        "store": False,
        "previous_response_id": "response-1",
        "include": ["web_search_call.action.sources"],
        "reasoning": {"effort": "high"},
    }

    deepseek_body = plan_responses_body(body, deepseek)
    codex_body = plan_responses_body(body, codex)

    assert "store" not in deepseek_body
    assert "previous_response_id" not in deepseek_body
    assert "include" not in deepseek_body
    assert "reasoning.encrypted_content" in codex_body["include"]
