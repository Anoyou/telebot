from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import JSON, Column, Integer, LargeBinary, MetaData, String, Table, create_engine, select

from app.scripts.rekey import rekey_database


def _key() -> str:
    return Fernet.generate_key().decode()


def _encrypt_text(key: str, plain: str) -> str:
    return Fernet(key.encode()).encrypt(plain.encode()).decode()


def _encrypt_bytes(key: str, plain: bytes) -> bytes:
    return Fernet(key.encode()).encrypt(plain)


def _decrypt_text(key: str, token: str) -> str:
    return Fernet(key.encode()).decrypt(token.encode()).decode()


def _decrypt_bytes(key: str, token: bytes) -> bytes:
    return Fernet(key.encode()).decrypt(token)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'rekey.db'}"


def _create_rekey_tables(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    metadata = MetaData()
    Table("proxy", metadata, Column("id", Integer, primary_key=True), Column("password_enc", String))
    Table(
        "account",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("api_id_enc", String),
        Column("api_hash_enc", String),
        Column("session_enc", LargeBinary),
    )
    Table("llm_provider", metadata, Column("id", Integer, primary_key=True), Column("api_key_enc", String))
    Table("notify_bot", metadata, Column("id", Integer, primary_key=True), Column("bot_token_enc", String))
    Table("web_user", metadata, Column("id", Integer, primary_key=True), Column("totp_secret_enc", String))
    Table("account_bot", metadata, Column("id", Integer, primary_key=True), Column("bot_token_enc", String))
    Table("plugin_repo", metadata, Column("id", Integer, primary_key=True), Column("credential_enc", String))
    Table("system_setting", metadata, Column("key", String, primary_key=True), Column("value", JSON))
    metadata.create_all(engine)
    engine.dispose()


def test_rekey_database_dry_run_then_rotate(tmp_path: Path) -> None:
    old_key = _key()
    new_key = _key()
    database_url = _database_url(tmp_path)
    _create_rekey_tables(database_url)

    engine = create_engine(database_url, future=True)
    with engine.begin() as conn:
        conn.execute(
            Table("proxy", MetaData(), autoload_with=conn).insert(),
            {"id": 1, "password_enc": _encrypt_text(old_key, "proxy-pass")},
        )
        conn.execute(
            Table("account", MetaData(), autoload_with=conn).insert(),
            {
                "id": 1,
                "api_id_enc": _encrypt_text(old_key, "12345"),
                "api_hash_enc": _encrypt_text(old_key, "api-hash"),
                "session_enc": _encrypt_bytes(old_key, b"session"),
            },
        )
        conn.execute(
            Table("llm_provider", MetaData(), autoload_with=conn).insert(),
            {"id": 1, "api_key_enc": _encrypt_text(old_key, "llm-key")},
        )
        conn.execute(
            Table("notify_bot", MetaData(), autoload_with=conn).insert(),
            {"id": 1, "bot_token_enc": _encrypt_text(old_key, "notify-token")},
        )
        conn.execute(
            Table("web_user", MetaData(), autoload_with=conn).insert(),
            {"id": 1, "totp_secret_enc": _encrypt_text(old_key, "totp-secret")},
        )
        conn.execute(
            Table("account_bot", MetaData(), autoload_with=conn).insert(),
            {"id": 1, "bot_token_enc": _encrypt_text(old_key, "account-bot-token")},
        )
        conn.execute(
            Table("plugin_repo", MetaData(), autoload_with=conn).insert(),
            {"id": 1, "credential_enc": _encrypt_text(old_key, "github-token")},
        )
        conn.execute(
            Table("system_setting", MetaData(), autoload_with=conn).insert(),
            {
                "key": "account_bot_transfer_notice:1",
                "value": {
                    "interaction_bot_token_enc": _encrypt_text(old_key, "interaction-token"),
                    "transfer_bot_token_enc": _encrypt_text(old_key, "transfer-token"),
                    "enabled": True,
                },
            },
        )
        conn.execute(
            Table("system_setting", MetaData(), autoload_with=conn).insert(),
            {
                "key": "account_webhooks:1",
                "value": {
                    "token": "stale-legacy-webhook-token",
                    "token_enc": _encrypt_text(old_key, "webhook-token"),
                    "hooks": [{"key": "default", "enabled": True}],
                },
            },
        )

    dry_run = rekey_database(old_key=old_key, new_key=new_key, database_url=database_url, dry_run=True)
    assert dry_run.scanned == 12
    assert dry_run.changed == 12

    with engine.connect() as conn:
        proxy = Table("proxy", MetaData(), autoload_with=conn)
        token = conn.execute(select(proxy.c.password_enc)).scalar_one()
    assert _decrypt_text(old_key, token) == "proxy-pass"

    result = rekey_database(old_key=old_key, new_key=new_key, database_url=database_url)
    assert result.scanned == 12
    assert result.changed == 12

    with engine.connect() as conn:
        account = Table("account", MetaData(), autoload_with=conn)
        row = conn.execute(select(account)).mappings().one()
        assert _decrypt_text(new_key, row["api_id_enc"]) == "12345"
        assert _decrypt_text(new_key, row["api_hash_enc"]) == "api-hash"
        assert _decrypt_bytes(new_key, row["session_enc"]) == b"session"

        settings = Table("system_setting", MetaData(), autoload_with=conn)
        value = conn.execute(
            select(settings.c.value).where(settings.c.key == "account_bot_transfer_notice:1")
        ).scalar_one()
        assert _decrypt_text(new_key, value["interaction_bot_token_enc"]) == "interaction-token"
        assert _decrypt_text(new_key, value["transfer_bot_token_enc"]) == "transfer-token"
        webhook_value = conn.execute(
            select(settings.c.value).where(settings.c.key == "account_webhooks:1")
        ).scalar_one()
        assert "token" not in webhook_value
        assert _decrypt_text(new_key, webhook_value["token_enc"]) == "webhook-token"

        plugin_repo = Table("plugin_repo", MetaData(), autoload_with=conn)
        repo_token = conn.execute(select(plugin_repo.c.credential_enc)).scalar_one()
        assert _decrypt_text(new_key, repo_token) == "github-token"

    engine.dispose()


def test_rekey_migrates_legacy_plaintext_webhook_token(tmp_path: Path) -> None:
    old_key = _key()
    new_key = _key()
    database_url = _database_url(tmp_path)
    _create_rekey_tables(database_url)

    engine = create_engine(database_url, future=True)
    with engine.begin() as conn:
        settings = Table("system_setting", MetaData(), autoload_with=conn)
        conn.execute(
            settings.insert(),
            {
                "key": "account_webhooks:7",
                "value": {"token": "legacy-webhook-token", "hooks": []},
            },
        )

    result = rekey_database(old_key=old_key, new_key=new_key, database_url=database_url)
    assert result.scanned == 1
    assert result.changed == 1

    with engine.connect() as conn:
        settings = Table("system_setting", MetaData(), autoload_with=conn)
        value = conn.execute(select(settings.c.value)).scalar_one()
    assert "token" not in value
    assert _decrypt_text(new_key, value["token_enc"]) == "legacy-webhook-token"
    engine.dispose()


def test_rekey_database_rolls_back_when_any_token_fails(tmp_path: Path) -> None:
    old_key = _key()
    new_key = _key()
    database_url = _database_url(tmp_path)
    _create_rekey_tables(database_url)

    engine = create_engine(database_url, future=True)
    with engine.begin() as conn:
        proxy = Table("proxy", MetaData(), autoload_with=conn)
        llm_provider = Table("llm_provider", MetaData(), autoload_with=conn)
        conn.execute(proxy.insert(), {"id": 1, "password_enc": _encrypt_text(old_key, "proxy-pass")})
        conn.execute(llm_provider.insert(), {"id": 1, "api_key_enc": "not-a-fernet-token"})

    with pytest.raises(RuntimeError):
        rekey_database(old_key=old_key, new_key=new_key, database_url=database_url)

    with engine.connect() as conn:
        proxy = Table("proxy", MetaData(), autoload_with=conn)
        token = conn.execute(select(proxy.c.password_enc)).scalar_one()
    assert _decrypt_text(old_key, token) == "proxy-pass"
    with pytest.raises(InvalidToken):
        Fernet(new_key.encode()).decrypt(token.encode())

    engine.dispose()
