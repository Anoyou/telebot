from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.crypto as crypto
from app.crypto import generate_master_key
from app.scripts import migrate_plugin_config_secrets as migration
from app.services.plugin_config_secrets import is_secret_envelope, unwrap_secret
from app.settings import settings


class _Scalars:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Session:
    def __init__(self, features, account_rows, global_rows) -> None:
        self._results = iter((features, account_rows, global_rows))
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _Result(next(self._results))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_existing_pt_cookie_migration_is_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "master_key", generate_master_key())
    monkeypatch.setattr(crypto, "_fernet", None)
    schema = {
        "type": "object",
        "properties": {
            "cookie": {
                "type": "string",
                "x-sensitive": True,
                "level": "global",
            }
        },
    }
    feature = SimpleNamespace(
        key="pt_promote",
        manifest={"config_schema": schema},
    )
    account_row = SimpleNamespace(
        feature_key="pt_promote",
        config={"cookie": "uid=legacy-account"},
    )
    global_row = SimpleNamespace(
        plugin_key="pt_promote",
        config={"cookie": "uid=legacy-global"},
    )
    sessions = []

    def session_factory():
        session = _Session([feature], [account_row], [global_row])
        sessions.append(session)
        return session

    monkeypatch.setattr(migration, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(migration, "flag_modified", lambda *_args: None)

    first = await migration.migrate_plugin_config_secrets(dry_run=False)
    second = await migration.migrate_plugin_config_secrets(dry_run=False)

    assert first.account_rows_changed == 1
    assert first.global_rows_changed == 1
    assert second.account_rows_changed == 0
    assert second.global_rows_changed == 0
    assert sessions[0].commits == 1
    assert is_secret_envelope(account_row.config["cookie"])
    assert is_secret_envelope(global_row.config["cookie"])
    assert unwrap_secret(account_row.config["cookie"]) == "uid=legacy-account"
    assert unwrap_secret(global_row.config["cookie"]) == "uid=legacy-global"
