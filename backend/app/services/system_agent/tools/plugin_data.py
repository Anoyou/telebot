"""已安装插件私有 SQLite 数据的受限只读查询工具。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ....db.models.plugin import InstalledPlugin
from ....settings import settings
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec

_SQLITE_HEADER = b"SQLite format 3\x00"
_MAX_DISCOVERED_FILES = 20
_MAX_SCANNED_ENTRIES = 2_000
_MAX_SCHEMA_OBJECTS = 100
_MAX_SCHEMA_COLUMNS = 1_000
_MAX_COLUMNS_PER_OBJECT = 200
_DEFAULT_ROW_LIMIT = 100
_MAX_ROW_LIMIT = 500
_MAX_SQL_LENGTH = 16_384
_MAX_PARAMETERS_LENGTH = 65_536
_MAX_CELL_TEXT = 4_000
_MAX_RESULT_TEXT = 128 * 1024
_QUERY_TIMEOUT_SECONDS = 2.0
_PROGRESS_CALLBACK_LIMIT = 2_000
_LEADING_SQL = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.S)
_READ_STATEMENT = re.compile(r"(?:select|with)\b", re.I)
_PLUGIN_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SENSITIVE_COLUMN_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session_string",
)


def _plugin_data_root() -> Path:
    return (settings.plugins_installed_path / "_data").resolve()


def _plugin_data_dir(plugin_key: str) -> Path:
    if not _PLUGIN_KEY.fullmatch(plugin_key):
        raise ValueError("插件 key 无效，无法解析私有数据目录")
    root = _plugin_data_root()
    target = (root / plugin_key).resolve()
    if target == root or root not in target.parents:
        raise ValueError("插件 key 无效，无法解析私有数据目录")
    return target


async def _installed_plugin(ctx: ToolContext, plugin_key: str) -> InstalledPlugin:
    key = str(plugin_key or "").strip()
    if not key:
        raise ValueError("需要 feature_key")
    plugin = await ctx.db.get(InstalledPlugin, key)
    if plugin is None:
        raise ValueError(f"未找到已安装插件：{key}")
    return plugin


def _is_sqlite_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _discover_databases(data_dir: Path) -> tuple[list[Path], bool]:
    if not data_dir.is_dir() or data_dir.is_symlink():
        return [], False
    found: list[Path] = []
    scanned = 0
    for directory, dirnames, filenames in os.walk(data_dir, followlinks=False):
        current = Path(directory)
        dirnames[:] = [
            name for name in dirnames if not (current / name).is_symlink()
        ]
        for name in filenames:
            scanned += 1
            if scanned > _MAX_SCANNED_ENTRIES:
                return sorted(found)[:_MAX_DISCOVERED_FILES], True
            candidate = current / name
            if _is_sqlite_file(candidate):
                found.append(candidate)
                if len(found) > _MAX_DISCOVERED_FILES:
                    return sorted(found)[:_MAX_DISCOVERED_FILES], True
    return sorted(found), False


def _database_path(data_dir: Path, relative_name: str) -> Path:
    raw = str(relative_name or "").strip()
    if not raw:
        raise ValueError("需要 database（list_plugin_databases 返回的相对路径）")
    relative = Path(raw)
    if relative.is_absolute():
        raise ValueError("database 必须是插件私有数据目录内的相对路径")
    candidate = data_dir / relative
    if candidate.is_symlink():
        raise ValueError("不允许读取符号链接数据库")
    resolved = candidate.resolve()
    if resolved == data_dir or data_dir not in resolved.parents:
        raise ValueError("database 路径越界")
    current = candidate.parent
    while current != data_dir:
        if current.is_symlink():
            raise ValueError("不允许通过符号链接目录读取数据库")
        current = current.parent
    if not _is_sqlite_file(resolved):
        raise ValueError("目标不是可读取的 SQLite 数据库")
    return resolved


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(False)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _database_metadata(
    path: Path,
    *,
    relative_name: str,
    budget: dict[str, int],
) -> dict[str, Any]:
    object_limit = min(_MAX_SCHEMA_OBJECTS, max(0, budget["objects"]))
    schema_truncated = object_limit == 0
    with _connect_read_only(path) as connection:
        objects = connection.execute(
            "SELECT name, type FROM sqlite_schema "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name LIMIT ?",
            (object_limit + 1,),
        ).fetchall()
        if len(objects) > object_limit:
            schema_truncated = True
        tables: list[dict[str, Any]] = []
        for item in objects[:object_limit]:
            if budget["columns"] <= 0:
                schema_truncated = True
                break
            name = str(item["name"])
            column_limit = min(_MAX_COLUMNS_PER_OBJECT, budget["columns"])
            columns = connection.execute(
                'SELECT name, type, "notnull", pk FROM pragma_table_info(?) LIMIT ?',
                (name, column_limit + 1),
            ).fetchall()
            if len(columns) > column_limit:
                schema_truncated = True
            visible_columns = columns[:column_limit]
            tables.append(
                {
                    "name": name,
                    "type": str(item["type"]),
                    "columns": [
                        {
                            "name": str(column["name"]),
                            "type": str(column["type"] or ""),
                            "not_null": bool(column["notnull"]),
                            "primary_key": bool(column["pk"]),
                        }
                        for column in visible_columns
                    ],
                    "columns_truncated": len(columns) > column_limit,
                }
            )
            budget["objects"] -= 1
            budget["columns"] -= len(visible_columns)
    return {
        "database": relative_name,
        "size_bytes": path.stat().st_size,
        "tables": tables,
        "schema_truncated": schema_truncated,
    }


def _list_database_metadata(data_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    databases, databases_truncated = _discover_databases(data_dir)
    budget = {"objects": _MAX_SCHEMA_OBJECTS, "columns": _MAX_SCHEMA_COLUMNS}
    metadata = [
        _database_metadata(
            path,
            relative_name=path.relative_to(data_dir).as_posix(),
            budget=budget,
        )
        for path in databases
    ]
    return metadata, databases_truncated


def _parse_parameters(raw: Any) -> list[Any] | dict[str, Any]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, str) or len(raw) > _MAX_PARAMETERS_LENGTH:
        raise ValueError("parameters_json 必须是长度不超过 64 KiB 的 JSON")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("parameters_json 必须是合法 JSON") from exc
    if not isinstance(value, (list, dict)):
        raise ValueError("parameters_json 顶层必须是数组或对象")
    return value


def _validate_read_query(sql: str) -> str:
    query = str(sql or "")
    if not query.strip():
        raise ValueError("需要 sql")
    if len(query) > _MAX_SQL_LENGTH:
        raise ValueError("sql 不能超过 16 KiB")
    remainder = query[_LEADING_SQL.match(query).end() :]
    if not _READ_STATEMENT.match(remainder):
        raise ValueError("只允许单条 SELECT 或 WITH 查询")
    return query


def _authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    _database: str | None,
    _trigger: str | None,
) -> int:
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        getattr(sqlite3, "SQLITE_RECURSIVE", -1),
    }
    if action == sqlite3.SQLITE_READ and _is_sensitive_column(str(arg2 or "")):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg2 or arg1 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _is_sensitive_column(column: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_")
    return any(part in normalized for part in _SENSITIVE_COLUMN_PARTS)


def _public_cell(column: str, value: Any, budget: list[int]) -> Any:
    if _is_sensitive_column(column):
        return "***"
    if isinstance(value, bytes):
        return {"type": "blob", "bytes": len(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) > _MAX_CELL_TEXT:
        text = f"{text[:_MAX_CELL_TEXT]}…"
    remaining = max(0, _MAX_RESULT_TEXT - budget[0])
    if len(text) > remaining:
        text = f"{text[:remaining]}…" if remaining else ""
    budget[0] += len(text)
    return text


def _run_query(
    path: Path,
    *,
    sql: str,
    parameters: list[Any] | dict[str, Any],
    row_limit: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    progress_calls = 0

    def progress() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(
            progress_calls > _PROGRESS_CALLBACK_LIMIT or time.monotonic() > deadline
        )

    with _connect_read_only(path) as connection:
        connection.set_authorizer(_authorizer)
        connection.set_progress_handler(progress, 100)
        try:
            cursor = connection.execute(sql, parameters)
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows = cursor.fetchmany(row_limit + 1)
        except sqlite3.DatabaseError as exc:
            message = str(exc).lower()
            if any(
                marker in message
                for marker in ("not authorized", "authorization denied", "is prohibited")
            ):
                raise ValueError("查询包含未授权的 SQLite 操作") from exc
            if "one statement at a time" in message:
                raise ValueError("只允许执行单条查询") from exc
            if "interrupted" in message:
                raise ValueError("查询超过计算或时间限制") from exc
            raise ValueError(f"SQLite 查询失败：{exc}") from exc
        finally:
            connection.set_progress_handler(None, 0)
            connection.set_authorizer(None)

    truncated = len(rows) > row_limit
    budget = [0]
    public_rows = [
        {
            column: _public_cell(column, row[index], budget)
            for index, column in enumerate(columns)
        }
        for row in rows[:row_limit]
    ]
    return {
        "columns": columns,
        "rows": public_rows,
        "row_count": len(public_rows),
        "truncated": truncated or budget[0] >= _MAX_RESULT_TEXT,
        "limits": {
            "row_limit": row_limit,
            "query_timeout_seconds": _QUERY_TIMEOUT_SECONDS,
            "text_bytes": _MAX_RESULT_TEXT,
        },
    }


async def list_plugin_databases(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    plugin = await _installed_plugin(ctx, str(args.get("feature_key") or args.get("key") or ""))
    data_dir = _plugin_data_dir(plugin.key)
    metadata, databases_truncated = await asyncio.to_thread(
        _list_database_metadata, data_dir
    )
    return {
        "feature_key": plugin.key,
        "database_count": len(metadata),
        "databases": list(metadata),
        "databases_truncated": databases_truncated,
        "limits": {
            "databases": _MAX_DISCOVERED_FILES,
            "schema_objects": _MAX_SCHEMA_OBJECTS,
            "schema_columns": _MAX_SCHEMA_COLUMNS,
        },
        "read_only": True,
    }


async def query_plugin_database(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    plugin = await _installed_plugin(ctx, str(args.get("feature_key") or args.get("key") or ""))
    data_dir = _plugin_data_dir(plugin.key)
    path = _database_path(data_dir, str(args.get("database") or ""))
    sql = _validate_read_query(str(args.get("sql") or ""))
    parameters = _parse_parameters(args.get("parameters_json"))
    try:
        row_limit = int(args.get("row_limit") or _DEFAULT_ROW_LIMIT)
    except (TypeError, ValueError) as exc:
        raise ValueError("row_limit 必须是整数") from exc
    if row_limit < 1 or row_limit > _MAX_ROW_LIMIT:
        raise ValueError(f"row_limit 必须在 1 到 {_MAX_ROW_LIMIT} 之间")
    result = await asyncio.to_thread(
        _run_query,
        path,
        sql=sql,
        parameters=parameters,
        row_limit=row_limit,
    )
    return {
        "feature_key": plugin.key,
        "database": path.relative_to(data_dir).as_posix(),
        "read_only": True,
        **result,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="features.list_plugin_databases",
            description="列出已安装插件私有数据目录内的 SQLite 数据库、表和字段结构。",
            input_schema={
                "type": "object",
                "properties": {
                    "feature_key": {"type": "string"},
                    "key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="admin",
            read_handler=list_plugin_databases,
        )
    )
    registry.register(
        ToolSpec(
            name="features.query_plugin_database",
            description=(
                "对已安装插件私有 SQLite 数据库执行一条参数化 SELECT/WITH 只读查询；"
                "禁止读取常见密钥列，并禁止写入、DDL、ATTACH、PRAGMA、扩展加载和路径越界。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "feature_key": {"type": "string"},
                    "key": {"type": "string"},
                    "database": {"type": "string"},
                    "sql": {"type": "string"},
                    "parameters_json": {
                        "type": "string",
                        "description": "SQL 参数的 JSON 数组或对象；不要把参数值拼进 SQL。",
                    },
                    "row_limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["feature_key", "database", "sql"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="admin",
            read_handler=query_plugin_database,
            secret_argument_names=("parameters_json",),
        )
    )


__all__ = [
    "list_plugin_databases",
    "query_plugin_database",
    "register",
]
