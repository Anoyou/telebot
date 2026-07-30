from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Integer


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0052_system_agent_queue_runtime.py"
    )
    spec = importlib.util.spec_from_file_location("telepilot_migration_0052", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BatchRecorder:
    def __init__(self, operations: list[tuple[str, str]]) -> None:
        self.operations = operations

    def __enter__(self) -> _BatchRecorder:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def add_column(self, column) -> None:
        self.operations.append(("add_column", column.name))

    def create_foreign_key(self, name, *_args, **_kwargs) -> None:
        self.operations.append(("create_foreign_key", name))

    def drop_constraint(self, name, **_kwargs) -> None:
        self.operations.append(("drop_constraint", name))

    def drop_column(self, name) -> None:
        self.operations.append(("drop_column", name))


def test_migration_0052_upgrade_and_downgrade(monkeypatch) -> None:
    migration = _load_migration_module()
    created_tables: dict[str, tuple[Any, ...]] = {}
    created_indexes: list[str] = []
    dropped_tables: list[str] = []
    dropped_indexes: list[str] = []
    batch_operations: list[tuple[str, str]] = []
    executed_sql: list[str] = []

    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *columns, **_kwargs: created_tables.setdefault(name, columns),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *_args, **_kwargs: created_indexes.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda name, **_kwargs: dropped_tables.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, **_kwargs: dropped_indexes.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "batch_alter_table",
        lambda *_args, **_kwargs: _BatchRecorder(batch_operations),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: executed_sql.append(str(statement)),
    )

    migration.upgrade()
    migration.downgrade()

    assert migration.revision == "0052"
    assert migration.down_revision == "0051"
    assert list(created_tables) == [
        "system_agent_pending_turn",
        "system_agent_run_input",
    ]
    assert created_indexes == [
        "ix_system_agent_pending_turn_session_status_position",
        "ix_system_agent_pending_turn_owner",
        "ix_system_agent_run_bot_status",
        "ix_system_agent_run_lease",
        "ix_system_agent_run_pending_turn",
        "ix_system_agent_run_input_pending",
    ]
    assert dropped_tables == [
        "system_agent_run_input",
        "system_agent_pending_turn",
    ]
    assert dropped_indexes == list(reversed(created_indexes))
    assert executed_sql == ["UPDATE system_agent_run SET phase = status"]

    input_id = next(
        column
        for column in created_tables["system_agent_run_input"]
        if getattr(column, "name", None) == "id"
    )
    assert isinstance(input_id.type, BigInteger)
    assert isinstance(input_id.type._variant_mapping["sqlite"], Integer)
    assert input_id.autoincrement is True

    added_columns = [
        name for operation, name in batch_operations if operation == "add_column"
    ]
    dropped_columns = [
        name for operation, name in batch_operations if operation == "drop_column"
    ]
    assert added_columns == [
        "bot_tg_user_id",
        "channel",
        "pending_turn_id",
        "phase",
        "paused_reason",
        "claimed_by",
        "lease_expires_at",
        "heartbeat_at",
        "usage",
        "elapsed_ms",
    ]
    assert dropped_columns == list(reversed(added_columns))
