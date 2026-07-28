"""System Agent 密钥抽取与打码。"""

from __future__ import annotations

import pytest

from app.services.system_agent.secrets import (
    extract_plaintext_secrets,
    looks_like_provider_credential_paste,
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


@pytest.mark.parametrize(
    "key",
    (
        "Abcdefghijklmnopqrstuvwxyz",
        "AbcdEFGHijklmnop+/=_-1234",
    ),
)
def test_extract_json_labeled_provider_key(key: str) -> None:
    text = f'{{"baseurl":"https://models.example/v1","apikey":"{key}"}}'

    secrets = extract_plaintext_secrets(text)

    assert key in secrets
    assert looks_like_provider_credential_paste(text) is True
    assert key not in redact_known_secrets(text, secrets)


@pytest.mark.parametrize(
    "key",
    (
        "AbCdEfGhIjKlMnOpQrStUvWxYz123456",
        "abcdefghijklmnop.qrstuvwxyz123456",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
    ),
)
def test_extract_provider_paste_keys_that_router_accepts(key: str) -> None:
    text = f"https://api.example/v1 {key}"

    secrets = extract_plaintext_secrets(text)
    redacted = redact_known_secrets(text, secrets)

    assert secrets == [key]
    assert key not in redacted
    assert "[REDACTED]" in redacted


def test_redact_known_secrets() -> None:
    key = "sk-abcdefghijklmnopqrstuvwxyz123456"
    text = f"这是密钥 {key} 请保存"
    out = redact_known_secrets(text, [key])
    assert key not in out
    assert "REDACTED" in out or "***" in out or "sk-" not in out


def test_proxy_url_password_is_extracted_and_redacted() -> None:
    text = "添加 socks5://proxy-user:VerySecret123@example.com:1080 这个代理"

    secrets = extract_plaintext_secrets(text)

    assert secrets == ["VerySecret123"]
    assert "VerySecret123" not in redact_known_secrets(text, secrets)


def test_telegram_bot_token_is_extracted_and_redacted() -> None:
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    text = f"用这个 Bot Token 配置通知：{token}"

    secrets = extract_plaintext_secrets(text)

    assert secrets == [token]
    assert token not in redact_known_secrets(text, secrets)


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


def test_chat_secret_replaces_model_redacted_placeholder() -> None:
    public, secrets, fields = merge_secret_into_arguments(
        {"base_url": "https://api.example/v1", "api_key": "[REDACTED]"},
        secret_names=("api_key",),
        chat_secrets=["sk-abcdefghijklmnopqrstuvwxyz123456"],
    )

    assert public["has_api_key"] is True
    assert "api_key" not in public
    assert secrets == {"api_key": "sk-abcdefghijklmnopqrstuvwxyz123456"}
    assert fields == ["api_key"]
