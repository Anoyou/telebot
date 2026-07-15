from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0041_notify_bot_account_source.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0041", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0041_chains_from_0040() -> None:
    migration = _load_migration_module()

    assert migration.revision == "0041"
    assert migration.down_revision == "0040"
