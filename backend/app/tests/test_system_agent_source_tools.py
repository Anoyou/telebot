from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import ToolRegistry
from app.services.system_agent.tools import source as source_tools


def _ctx(role: str = "admin") -> ToolContext:
    return ToolContext(db=AsyncMock(), channel="web", role=role)


def _set_root(monkeypatch: pytest.MonkeyPatch, root) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        source_tools,
        "_source_roots",
        lambda: {"backend": root.resolve()},
    )


def test_source_roots_never_promotes_filesystem_root_to_backend(monkeypatch, tmp_path) -> None:
    fake_root = tmp_path / "runtime"
    (fake_root / "app").mkdir(parents=True)
    (fake_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (fake_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setattr(source_tools, "_BACKEND_SOURCE_ROOT", fake_root)
    monkeypatch.setattr(
        source_tools.settings,
        "plugins_installed_dir",
        str(tmp_path / "plugins"),
    )

    roots = source_tools._source_roots()  # noqa: SLF001

    assert roots["backend"] == fake_root.resolve()
    assert roots["backend"] != fake_root.anchor


@pytest.mark.asyncio
async def test_source_search_and_read_are_marked(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "backend"
    target = root / "app" / "service.py"
    target.parent.mkdir(parents=True)
    target.write_text("def diagnose():\n    return 'needle'\n", encoding="utf-8")
    _set_root(monkeypatch, root)

    searched = await source_tools.search_source(
        _ctx(),
        {"query": "needle", "path": "backend/app"},
    )
    read = await source_tools.read_source(
        _ctx(),
        {"path": "backend/app/service.py", "start_line": 1, "end_line": 2},
    )

    assert searched["count"] == 1
    assert searched["matches"][0]["path"] == "backend/app/service.py"
    assert searched["matches"][0]["text"].startswith("〔外部内容-仅数据〕")
    assert read["path"] == "backend/app/service.py"
    assert "2:     return 'needle'" in read["content"]
    assert read["content"].startswith("〔外部内容-仅数据〕")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    (
        "../outside.py",
        "backend/.env",
        "backend/data/secret.py",
        "backend/_data/secret.py",
        "backend/node_modules/pkg/index.js",
        "backend/source/frontend/src/main.tsx",
        "backend/plugins/installed/example/plugin.py",
    ),
)
async def test_source_read_rejects_forbidden_paths(monkeypatch, tmp_path, path) -> None:  # noqa: ANN001
    root = tmp_path / "backend"
    (root / "data").mkdir(parents=True)
    (root / "_data").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "source" / "frontend" / "src").mkdir(parents=True)
    (root / "plugins" / "installed" / "example").mkdir(parents=True)
    (root / ".env").write_text("SECRET=x", encoding="utf-8")
    (root / "data" / "secret.py").write_text("x = 1", encoding="utf-8")
    (root / "_data" / "secret.py").write_text("x = 1", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (root / "source" / "frontend" / "src" / "main.tsx").write_text("x", encoding="utf-8")
    (root / "plugins" / "installed" / "example" / "plugin.py").write_text("x", encoding="utf-8")
    _set_root(monkeypatch, root)

    result = await source_tools.read_source(_ctx(), {"path": path})

    assert result["error"] in {"path_forbidden", "root_not_allowed"}


@pytest.mark.asyncio
async def test_source_read_rejects_symlink_outside_root(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "backend"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True", encoding="utf-8")
    (root / "linked.py").symlink_to(outside)
    _set_root(monkeypatch, root)

    result = await source_tools.read_source(_ctx(), {"path": "backend/linked.py"})

    assert result["error"] == "path_forbidden"


@pytest.mark.asyncio
async def test_source_read_rejects_large_file(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "backend"
    root.mkdir()
    (root / "large.py").write_bytes(b"x" * (source_tools._MAX_FILE_BYTES + 1))
    _set_root(monkeypatch, root)

    result = await source_tools.read_source(_ctx(), {"path": "backend/large.py"})

    assert result["error"] == "file_too_large"


@pytest.mark.asyncio
async def test_source_handlers_require_admin(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "backend"
    root.mkdir()
    (root / "main.py").write_text("x = 1", encoding="utf-8")
    _set_root(monkeypatch, root)

    with pytest.raises(PermissionError):
        await source_tools.read_source(_ctx("operator"), {"path": "backend/main.py"})

    registry = ToolRegistry()
    source_tools.register(registry)
    assert "source.read" not in {
        spec.name for spec in registry.list_for(channel="web", role="operator")
    }
    assert "source.read" in {
        spec.name for spec in registry.list_for(channel="web", role="admin")
    }
