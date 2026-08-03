from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0054_llm_usage_gateway_metadata.py"
    spec = importlib.util.spec_from_file_location("telepilot_migration_0054", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0054_upgrade_and_downgrade(monkeypatch) -> None:
    migration = _load_migration_module()
    added: list[tuple[str, str, bool]] = []
    dropped: list[tuple[str, str]] = []
    constraints: list[tuple[str, str, str]] = []
    dropped_constraints: list[str] = []

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added.append((table, column.name, column.nullable)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: constraints.append((name, table, condition)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, _table, **_kwargs: dropped_constraints.append(name),
    )

    migration.upgrade()
    migration.downgrade()

    assert migration.revision == "0054"
    assert migration.down_revision == "0053"
    assert added == [
        ("llm_usage", "execution_backend", True),
        ("llm_usage", "gateway_version", True),
        ("llm_usage", "gateway_request_id", True),
        ("llm_usage", "gateway_stage", True),
    ]
    assert constraints == [
        (
            "ck_llm_usage_execution_backend",
            "llm_usage",
            "execution_backend IS NULL OR execution_backend IN ('direct', 'codex_gateway')",
        )
    ]
    assert dropped_constraints == ["ck_llm_usage_execution_backend"]
    assert dropped == [
        ("llm_usage", "gateway_stage"),
        ("llm_usage", "gateway_request_id"),
        ("llm_usage", "gateway_version"),
        ("llm_usage", "execution_backend"),
    ]
