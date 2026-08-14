"""插件平台能力声明的静态读取兼容测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.plugin_capability_requirements import (
    MISSING_DECLARATION_WARNING,
    SOURCE_MISSING_WARNING,
    read_plugin_capability_requirement,
)


def _write_manifest(path: Path, declaration: str = "requires_platform_capabilities=[]") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.py").write_text(
        "from app.worker.plugins.manifest import Manifest\n"
        f"MANIFEST = Manifest(key={path.name!r}, display_name='Demo', {declaration})\n",
        encoding="utf-8",
    )


def test_explicit_empty_declaration_is_valid_and_participates(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "tool_leaf"
    _write_manifest(plugin_dir)

    result = read_plugin_capability_requirement(
        "tool_leaf", plugin_dir, source="local"
    )

    assert result.declared is True
    assert result.requires == ()
    assert result.warnings == ()
    assert result.participates_in_demand is True


def test_legacy_missing_declaration_only_warns(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "legacy_leaf"
    _write_manifest(plugin_dir, declaration="version='1.0.0'")

    result = read_plugin_capability_requirement(
        "legacy_leaf", plugin_dir, source="local"
    )

    assert result.declared is False
    assert result.warnings == (MISSING_DECLARATION_WARNING,)
    assert result.participates_in_demand is False


def test_metadata_declaration_mismatch_warns_and_does_not_demand(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "mismatch_leaf"
    _write_manifest(plugin_dir, declaration="requires_platform_capabilities=['ai']")
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"requires_platform_capabilities": ["ledger"]}),
        encoding="utf-8",
    )

    result = read_plugin_capability_requirement(
        "mismatch_leaf", plugin_dir, source="repo"
    )

    assert result.declared is True
    assert result.warnings == (
        "plugin.json 与 manifest.py 的 requires_platform_capabilities 声明不一致。",
    )
    assert result.participates_in_demand is False


def test_codex_image_pycache_residue_is_source_missing(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "codex_image"
    (plugin_dir / "__pycache__").mkdir(parents=True)
    (plugin_dir / "__pycache__" / "plugin.cpython-313.pyc").write_bytes(b"residue")

    result = read_plugin_capability_requirement(
        "codex_image", plugin_dir, source="repo"
    )

    assert result.source_missing is True
    assert result.requires == ()
    assert result.warnings == (SOURCE_MISSING_WARNING,)
    assert result.participates_in_demand is False
