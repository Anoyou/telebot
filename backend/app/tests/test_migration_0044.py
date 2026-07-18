from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0044_system_agent_memory.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0044", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0044_chains_from_0043() -> None:
    migration = _load_migration_module()

    assert migration.revision == "0044"
    assert migration.down_revision == "0043"
