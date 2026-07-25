from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0050_llm_provider_request_headers.py"
    spec = importlib.util.spec_from_file_location("telepilot_migration_0050", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0050_adds_encrypted_request_headers_column(monkeypatch) -> None:
    migration = _load_migration_module()
    added = []
    dropped = []
    monkeypatch.setattr(migration.op, "add_column", lambda table, column: added.append((table, column)))
    monkeypatch.setattr(migration.op, "drop_column", lambda table, column: dropped.append((table, column)))

    migration.upgrade()
    migration.downgrade()

    assert migration.revision == "0050"
    assert migration.down_revision == "0049"
    assert added[0][0] == "llm_provider"
    assert added[0][1].name == "request_headers_enc"
    assert dropped == [("llm_provider", "request_headers_enc")]
