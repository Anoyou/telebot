from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0051_llm_provider_codex_tui_identity.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0051", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0051_chains_from_0050_and_matches_runtime_identities() -> None:
    migration = _load_migration_module()

    assert migration.revision == "0051"
    assert migration.down_revision == "0050"
    assert "codex_tui" in migration._ALLOWED_IDENTITIES
    assert "codex_cli" not in migration._ALLOWED_IDENTITIES
    assert "codex_exec" not in migration._ALLOWED_IDENTITIES


def test_upgrade_and_downgrade_convert_data_before_recreating_constraint(monkeypatch) -> None:
    migration = _load_migration_module()
    operations: list[tuple[str, str]] = []

    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, type_: operations.append(("drop", f"{name}:{table}:{type_}")),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: operations.append(("execute", str(statement))),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, expression: operations.append(("create", expression)),
    )

    migration.upgrade()
    migration.downgrade()

    assert [kind for kind, _value in operations] == [
        "drop",
        "execute",
        "create",
        "drop",
        "execute",
        "create",
    ]
    assert "codex_cli" in operations[1][1]
    assert "codex_exec" in operations[1][1]
    assert "codex_tui" in operations[2][1]
    assert "codex_cli" not in operations[2][1]
    assert "codex_tui" in operations[4][1]
    assert "codex_cli" in operations[5][1]
    assert "codex_tui" not in operations[5][1]
