"""部署源码只读检索工具。

只暴露经过白名单筛选的文本源码，不提供 shell、执行或写文件能力。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from ....services.redactor import redact_text
from ....settings import PROJECT_ROOT
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import clamp_limit, mark_external_text

_ALLOWED_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_ALLOWED_FILENAMES = frozenset({"Dockerfile", "Makefile"})
_DENIED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "_data",
        "__pycache__",
        "build",
        "data",
        "dist",
        "logs",
        "node_modules",
        "sessions",
        "venv",
    }
)
_DENIED_ROOT_PARTS: dict[str, frozenset[str]] = {
    # 生产镜像的 backend 根是 /app；frontend 与 plugins 必须只通过各自标签读取。
    "backend": frozenset({"plugins", "source"}),
}
_MAX_FILE_BYTES = 256 * 1024
_MAX_READ_LINES = 400
_MAX_SEARCH_FILES = 4_000
_MAX_MATCH_TEXT = 600


def _source_roots() -> dict[str, Path]:
    """返回当前运行包可读的源码根目录。

    本地开发直接读取仓库；生产镜像中的前端源码由 Dockerfile 复制到
    ``/app/source/frontend``，已安装插件则来自只读查询目标目录。
    """

    roots: dict[str, Path] = {}
    backend = PROJECT_ROOT.resolve()
    if (backend / "app").is_dir():
        roots["backend"] = backend

    frontend_candidates = (
        PROJECT_ROOT / "source" / "frontend",
        PROJECT_ROOT.parent / "frontend",
    )
    frontend = next((path.resolve() for path in frontend_candidates if path.is_dir()), None)
    if frontend is not None:
        roots["frontend"] = frontend

    plugin_candidates = (
        PROJECT_ROOT / "plugins" / "installed",
        PROJECT_ROOT.parent / "plugins" / "installed",
    )
    plugins = next((path.resolve() for path in plugin_candidates if path.is_dir()), None)
    if plugins is not None:
        roots["plugins"] = plugins
    return roots


def _is_allowed_file(path: Path) -> bool:
    return path.name in _ALLOWED_FILENAMES or path.suffix.lower() in _ALLOWED_SUFFIXES


def _is_safe_relative(path: Path, *, label: str | None = None) -> bool:
    if label and path.parts and path.parts[0].lower() in _DENIED_ROOT_PARTS.get(label, ()):
        return False
    for part in path.parts:
        lowered = part.lower()
        if lowered in _DENIED_PARTS or part.startswith("."):
            return False
    return True


def _public_path(label: str, root: Path, path: Path) -> str:
    return f"{label}/{path.relative_to(root).as_posix()}"


def _resolve_scope(raw_path: Any, *, require_file: bool) -> tuple[str, Path, Path] | str:
    value = str(raw_path or "").strip().replace("\\", "/")
    if not value:
        return "path_required"
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "\x00" in value:
        return "path_forbidden"

    roots = _source_roots()
    label = pure.parts[0] if pure.parts else ""
    root = roots.get(label)
    if root is None:
        return "root_not_allowed"
    relative = Path(*pure.parts[1:]) if len(pure.parts) > 1 else Path()
    if relative.parts and not _is_safe_relative(relative, label=label):
        return "path_forbidden"

    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return "path_forbidden"
    if not target.exists():
        return "not_found"
    if require_file and not target.is_file():
        return "file_required"
    if target.is_file() and not _is_allowed_file(target):
        return "file_type_forbidden"
    return label, root, target


def _iter_source_files(label: str, root: Path, scope: Path) -> Iterator[tuple[str, Path]]:
    if scope.is_file():
        if _is_allowed_file(scope) and _is_safe_relative(scope.relative_to(root), label=label):
            yield _public_path(label, root, scope), scope
        return

    scanned = 0
    for current, dirs, files in os.walk(scope):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for name in dirs:
            if name.lower() in _DENIED_PARTS or name.startswith("."):
                continue
            try:
                relative_dir = (current_path / name).resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if _is_safe_relative(relative_dir, label=label):
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(files):
            path = current_path / name
            scanned += 1
            if scanned > _MAX_SEARCH_FILES:
                return
            if not _is_allowed_file(path):
                continue
            try:
                relative = path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if not _is_safe_relative(relative, label=label):
                continue
            yield _public_path(label, root, path.resolve()), path.resolve()


def _scope_error(code: str) -> dict[str, Any]:
    messages = {
        "path_required": "需要源码路径，例如 backend/app/main.py。",
        "path_forbidden": "路径越界或位于敏感目录，拒绝读取。",
        "root_not_allowed": "只允许 backend、frontend 或 plugins 源码根目录。",
        "not_found": "源码路径不存在。",
        "file_required": "该操作需要具体文件路径。",
        "file_type_forbidden": "该文件类型不在源码只读白名单内。",
    }
    return {"error": code, "message": messages.get(code, "源码路径不可用。")}


async def search_source(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ctx.require_role("admin")
    query = str(args.get("query") or "").strip()
    if len(query) < 2:
        return {"error": "query_too_short", "message": "搜索词至少 2 个字符。"}
    if len(query) > 160:
        return {"error": "query_too_long", "message": "搜索词不能超过 160 个字符。"}

    limit = clamp_limit(args.get("limit"), default=30, maximum=100)
    case_sensitive = args.get("case_sensitive") is True
    raw_scope = str(args.get("path") or "").strip()
    roots = _source_roots()
    scopes: list[tuple[str, Path, Path]] = []
    if raw_scope:
        resolved = _resolve_scope(raw_scope, require_file=False)
        if isinstance(resolved, str):
            return _scope_error(resolved)
        scopes.append(resolved)
    else:
        scopes.extend((label, root, root) for label, root in roots.items())

    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    searched_files = 0
    skipped_large_files = 0
    for label, root, scope in scopes:
        for public_path, path in _iter_source_files(label, root, scope):
            searched_files += 1
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    skipped_large_files += 1
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                snippet = redact_text(line.strip())[:_MAX_MATCH_TEXT]
                matches.append(
                    {
                        "path": public_path,
                        "line": line_number,
                        "text": mark_external_text(snippet),
                    }
                )
                if len(matches) >= limit:
                    return {
                        "query": query,
                        "count": len(matches),
                        "limit": limit,
                        "truncated": True,
                        "searched_files": searched_files,
                        "skipped_large_files": skipped_large_files,
                        "matches": matches,
                    }
    return {
        "query": query,
        "count": len(matches),
        "limit": limit,
        "truncated": False,
        "searched_files": searched_files,
        "skipped_large_files": skipped_large_files,
        "matches": matches,
    }


async def read_source(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ctx.require_role("admin")
    resolved = _resolve_scope(args.get("path"), require_file=True)
    if isinstance(resolved, str):
        return _scope_error(resolved)
    label, root, path = resolved
    try:
        size = path.stat().st_size
    except OSError:
        return _scope_error("not_found")
    if size > _MAX_FILE_BYTES:
        return {
            "error": "file_too_large",
            "message": f"文件超过 {_MAX_FILE_BYTES // 1024} KiB，只允许先搜索后读取更小文件。",
        }

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"error": "read_failed", "message": str(exc)[:200]}
    total_lines = len(lines)
    try:
        start_line = max(1, int(args.get("start_line") or 1))
    except (TypeError, ValueError):
        start_line = 1
    if total_lines:
        start_line = min(start_line, total_lines)
    try:
        requested_end = int(args.get("end_line") or (start_line + 199))
    except (TypeError, ValueError):
        requested_end = start_line + 199
    end_line = min(total_lines, max(start_line, requested_end), start_line + _MAX_READ_LINES - 1)
    selected = lines[start_line - 1 : end_line]
    numbered = "\n".join(
        f"{line_number}: {redact_text(line)}"
        for line_number, line in enumerate(selected, start=start_line)
    )
    return {
        "path": _public_path(label, root, path),
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": end_line < total_lines,
        "content": mark_external_text(numbered),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="source.search",
            description=(
                "只读搜索当前部署的 backend、frontend 与已安装插件源码。"
                "用于从日志堆栈定位实现，不执行命令、不读取敏感目录。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "普通文本搜索词，不是正则表达式。"},
                    "path": {"type": "string", "description": "可选目录，如 backend/app/services。"},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer", "description": "默认 30，最多 100。"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="admin",
            read_handler=search_source,
        )
    )
    registry.register(
        ToolSpec(
            name="source.read",
            description=(
                "按行只读当前部署的源码文件。只允许源码白名单路径，单次最多 400 行；"
                "不提供修改、执行或写入能力。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "如 backend/app/main.py。"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="admin",
            read_handler=read_source,
        )
    )


__all__ = ["read_source", "register", "search_source"]
