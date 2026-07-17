"""System Agent 密钥抽取与打码。"""

from __future__ import annotations

from app.services.system_agent.secrets import (
    extract_plaintext_secrets,
    merge_secret_into_arguments,
    redact_known_secrets,
)


def test_extract_openai_style_key() -> None:
    text = "帮我加一个 provider，key 是 sk-abcdefghijklmnopqrstuvwxyz123456"
    secrets = extract_plaintext_secrets(text)
    assert secrets
    assert secrets[0].startswith("sk-")


def test_extract_labeled_key() -> None:
    text = "api_key: my-super-secret-token-value"
    secrets = extract_plaintext_secrets(text)
    assert any("my-super-secret-token-value" in s for s in secrets)


def test_redact_known_secrets() -> None:
    key = "sk-abcdefghijklmnopqrstuvwxyz123456"
    text = f"这是密钥 {key} 请保存"
    out = redact_known_secrets(text, [key])
    assert key not in out
    assert "REDACTED" in out or "***" in out or "sk-" not in out


def test_merge_secret_into_arguments_from_chat() -> None:
    public, secrets, fields = merge_secret_into_arguments(
        {"name": "p1", "default_model": "m"},
        secret_names=("api_key",),
        chat_secrets=["sk-abcdefghijklmnopqrstuvwxyz123456"],
    )
    assert public.get("has_api_key") is True
    assert "api_key" not in public
    assert secrets["api_key"].startswith("sk-")
    assert fields == ["api_key"]
