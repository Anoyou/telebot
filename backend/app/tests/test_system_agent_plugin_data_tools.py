from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import ToolRegistry
from app.services.system_agent.tools import plugin_data


class _DatabaseSession:
    async def get(self, _model, key):  # noqa: ANN001
        if key == "demo":
            return SimpleNamespace(key="demo")
        return None


def _context() -> ToolContext:
    return ToolContext(  # type: ignore[arg-type]
        db=_DatabaseSession(),
        channel="web",
        role="admin",
    )


def _create_database(installed_root: Path) -> Path:
    target = installed_root / "_data" / "demo" / "runtime.sqlite3"
    target.parent.mkdir(parents=True)
    with sqlite3.connect(target) as connection:
        connection.execute(
            "CREATE TABLE rounds (id INTEGER PRIMARY KEY, winner TEXT, prize INTEGER, api_token TEXT)"
        )
        connection.executemany(
            "INSERT INTO rounds (winner, prize, api_token) VALUES (?, ?, ?)",
            [("Alice", 2333, "token-a"), ("Bob", 1111, "token-b")],
        )
    return target


@pytest.mark.asyncio
async def test_lists_plugin_sqlite_schema_without_exposing_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_root = tmp_path / "installed"
    _create_database(installed_root)
    monkeypatch.setattr(
        plugin_data,
        "settings",
        SimpleNamespace(plugins_installed_path=installed_root),
    )

    result = await plugin_data.list_plugin_databases(_context(), {"feature_key": "demo"})

    assert result["database_count"] == 1
    assert result["databases"][0]["database"] == "runtime.sqlite3"
    table = result["databases"][0]["tables"][0]
    assert table["name"] == "rounds"
    assert [column["name"] for column in table["columns"]] == [
        "id",
        "winner",
        "prize",
        "api_token",
    ]
    assert all("default" not in column for column in table["columns"])
    assert result["databases_truncated"] is False


@pytest.mark.asyncio
async def test_database_schema_listing_enforces_global_output_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_root = tmp_path / "installed"
    _create_database(installed_root)
    monkeypatch.setattr(
        plugin_data,
        "settings",
        SimpleNamespace(plugins_installed_path=installed_root),
    )
    monkeypatch.setattr(plugin_data, "_MAX_SCHEMA_COLUMNS", 2)

    result = await plugin_data.list_plugin_databases(_context(), {"feature_key": "demo"})

    database = result["databases"][0]
    assert database["schema_truncated"] is True
    assert len(database["tables"][0]["columns"]) == 2
    assert database["tables"][0]["columns_truncated"] is True


@pytest.mark.asyncio
async def test_parameterized_query_is_read_only_and_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_root = tmp_path / "installed"
    database = _create_database(installed_root)
    monkeypatch.setattr(
        plugin_data,
        "settings",
        SimpleNamespace(plugins_installed_path=installed_root),
    )

    result = await plugin_data.query_plugin_database(
        _context(),
        {
            "feature_key": "demo",
            "database": "runtime.sqlite3",
            "sql": "SELECT winner, prize FROM rounds WHERE prize >= ? ORDER BY id",
            "parameters_json": "[1000]",
            "row_limit": 1,
        },
    )

    assert result["rows"] == [{"winner": "Alice", "prize": 2333}]
    assert result["truncated"] is True
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM rounds").fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    (
        "SELECT api_token FROM rounds",
        "SELECT api_token AS harmless_name FROM rounds",
        "SELECT * FROM rounds",
    ),
)
async def test_rejects_sensitive_columns_even_when_aliased(
    sql: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_root = tmp_path / "installed"
    _create_database(installed_root)
    monkeypatch.setattr(
        plugin_data,
        "settings",
        SimpleNamespace(plugins_installed_path=installed_root),
    )

    with pytest.raises(ValueError, match="未授权"):
        await plugin_data.query_plugin_database(
            _context(),
            {
                "feature_key": "demo",
                "database": "runtime.sqlite3",
                "sql": sql,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    (
        "UPDATE rounds SET prize = 1",
        "PRAGMA table_info(rounds)",
        "WITH target AS (SELECT id FROM rounds) DELETE FROM rounds WHERE id IN target",
        "SELECT 1; SELECT 2",
    ),
)
async def test_rejects_sqlite_mutations_and_multiple_statements(
    sql: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_root = tmp_path / "installed"
    _create_database(installed_root)
    monkeypatch.setattr(
        plugin_data,
        "settings",
        SimpleNamespace(plugins_installed_path=installed_root),
    )

    with pytest.raises(ValueError):
        await plugin_data.query_plugin_database(
            _context(),
            {
                "feature_key": "demo",
                "database": "runtime.sqlite3",
                "sql": sql,
            },
        )


@pytest.mark.asyncio
async def test_rejects_database_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_root = tmp_path / "installed"
    outside = _create_database(tmp_path / "outside")
    data_dir = installed_root / "_data" / "demo"
    data_dir.mkdir(parents=True)
    (data_dir / "escape.sqlite3").symlink_to(outside)
    monkeypatch.setattr(
        plugin_data,
        "settings",
        SimpleNamespace(plugins_installed_path=installed_root),
    )

    with pytest.raises(ValueError, match="符号链接"):
        await plugin_data.query_plugin_database(
            _context(),
            {
                "feature_key": "demo",
                "database": "escape.sqlite3",
                "sql": "SELECT 1",
            },
        )


def test_plugin_database_tools_are_admin_only_and_read_only() -> None:
    registry = ToolRegistry()
    plugin_data.register(registry)

    assert {spec.name for spec in registry.list_for(channel="web", role="viewer")} == set()
    specs = registry.list_for(channel="web", role="admin")
    assert {spec.name for spec in specs} == {
        "features.list_plugin_databases",
        "features.query_plugin_database",
    }
    assert all(spec.read_only for spec in specs)
