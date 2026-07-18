from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0045_llm_usage_client_identity.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0045", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0045_chains_from_0044() -> None:
    migration = _load_migration_module()

    assert migration.revision == "0045"
    assert migration.down_revision == "0044"


def test_migration_0045_adds_nullable_client_identity(monkeypatch) -> None:
    migration = _load_migration_module()
    added: list[tuple[str, object]] = []
    dropped: list[tuple[str, str]] = []

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )

    migration.upgrade()
    migration.downgrade()

    assert len(added) == 1
    table, column = added[0]
    assert table == "llm_usage"
    assert column.name == "client_identity_profile"
    assert column.type.length == 32
    assert column.nullable is True
    assert dropped == [("llm_usage", "client_identity_profile")]
