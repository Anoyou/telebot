"""插件配置敏感字段加密信封测试。"""

from __future__ import annotations

import pytest

from app.crypto import generate_master_key
from app.services import plugin_config_secrets as secrets
from app.settings import settings


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setattr(settings, "master_key", generate_master_key())
    # reset fernet singleton
    import app.crypto as crypto

    monkeypatch.setattr(crypto, "_fernet", None)
    yield
    monkeypatch.setattr(crypto, "_fernet", None)


def test_encrypt_decrypt_roundtrip_by_key_name() -> None:
    raw = {"api_key": "sk-test-123456", "name": "demo", "nested": {"bot_token": "123:ABC"}}
    enc = secrets.encrypt_config_secrets(raw)
    assert secrets.is_secret_envelope(enc["api_key"])
    assert enc["name"] == "demo"
    assert secrets.is_secret_envelope(enc["nested"]["bot_token"])
    assert "sk-test" not in enc["api_key"]

    plain = secrets.decrypt_config_secrets(enc)
    assert plain["api_key"] == "sk-test-123456"
    assert plain["nested"]["bot_token"] == "123:ABC"


def test_mask_never_returns_plaintext() -> None:
    enc = secrets.encrypt_config_secrets({"password": "hunter2", "title": "x"})
    masked = secrets.mask_config_secrets(enc)
    assert masked["password"] == "***"
    assert masked["title"] == "x"


def test_encrypt_is_idempotent_for_envelopes() -> None:
    once = secrets.encrypt_config_secrets({"token": "abc"})
    twice = secrets.encrypt_config_secrets(once)
    assert once["token"] == twice["token"]


def test_schema_x_sensitive_and_password_format() -> None:
    schema = {
        "type": "object",
        "properties": {
            "custom_secret": {"type": "string", "x-sensitive": True},
            "login_pwd": {"type": "string", "format": "password"},
            "public": {"type": "string"},
        },
    }
    enc = secrets.encrypt_config_secrets(
        {"custom_secret": "s1", "login_pwd": "s2", "public": "ok"},
        schema=schema,
    )
    assert secrets.is_secret_envelope(enc["custom_secret"])
    assert secrets.is_secret_envelope(enc["login_pwd"])
    assert enc["public"] == "ok"


def test_count_encryptable_secrets() -> None:
    config = {
        "api_key": "plain",
        "bot_token": secrets.wrap_secret("tok"),
        "name": "n",
    }
    counts = secrets.count_encryptable_secrets(config)
    assert counts["plain"] == 1
    assert counts["envelope"] == 1


def test_array_items_x_sensitive_fields_are_encrypted() -> None:
    """profiles[].access 仅由 schema x-sensitive 标记时也必须加密。"""

    schema = {
        "type": "object",
        "properties": {
            "profiles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "access": {"type": "string", "x-sensitive": True},
                        "password": {"type": "string", "format": "password"},
                    },
                },
            }
        },
    }
    raw = {
        "profiles": [
            {"name": "a", "access": "should-encrypt", "password": "p1"},
            {"name": "b", "access": "also-secret", "password": "p2"},
        ]
    }
    enc = secrets.encrypt_config_secrets(raw, schema=schema)
    assert enc["profiles"][0]["name"] == "a"
    assert secrets.is_secret_envelope(enc["profiles"][0]["access"])
    assert secrets.is_secret_envelope(enc["profiles"][0]["password"])
    assert secrets.is_secret_envelope(enc["profiles"][1]["access"])
    assert "should-encrypt" not in enc["profiles"][0]["access"]

    plain = secrets.decrypt_config_secrets(enc, schema=schema)
    assert plain["profiles"][0]["access"] == "should-encrypt"
    assert plain["profiles"][1]["password"] == "p2"

    counts = secrets.count_encryptable_secrets(raw, schema=schema)
    assert counts["plain"] == 4
    assert counts["envelope"] == 0
    counts_enc = secrets.count_encryptable_secrets(enc, schema=schema)
    assert counts_enc["plain"] == 0
    assert counts_enc["envelope"] == 4
