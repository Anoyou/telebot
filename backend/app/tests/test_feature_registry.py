from __future__ import annotations

from pathlib import Path

from app import feature_registry
from app.feature_registry import BUILTIN_FEATURES, LazyBuiltinFeatures, scan_builtin_manifest_objects


def _write_manifest(dir_path: Path, content: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "manifest.py").write_text(content, encoding="utf-8")


def test_scan_builtin_manifest_objects_returns_empty_when_dir_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(feature_registry, "_BUILTIN_PLUGIN_DIR", tmp_path / "missing")
    result = scan_builtin_manifest_objects()
    assert result == {}


def test_lazy_registry_loads_display_name_and_manifest(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "builtin"
    _write_manifest(
        plugin_dir / "demo",
        "class _M:\n"
        "    pass\n"
        "MANIFEST = _M()\n"
        "MANIFEST.key = 'demo_key'\n"
        "MANIFEST.display_name = 'Demo Display'\n",
    )
    monkeypatch.setattr(feature_registry, "_BUILTIN_PLUGIN_DIR", plugin_dir)

    registry = LazyBuiltinFeatures()
    assert "demo_key" in registry
    assert registry["demo_key"] == "Demo Display"
    manifest = registry.manifest_for("demo_key")
    assert manifest is not None
    assert manifest.key == "demo_key"


def test_lazy_registry_refresh_picks_latest_manifest(monkeypatch, tmp_path) -> None:
    plugin_dir = tmp_path / "builtin"
    _write_manifest(
        plugin_dir / "alpha",
        "class _M:\n"
        "    pass\n"
        "MANIFEST = _M()\n"
        "MANIFEST.key = 'alpha'\n"
        "MANIFEST.display_name = 'Alpha V1'\n",
    )
    monkeypatch.setattr(feature_registry, "_BUILTIN_PLUGIN_DIR", plugin_dir)

    registry = LazyBuiltinFeatures()
    assert registry["alpha"] == "Alpha V1"

    _write_manifest(
        plugin_dir / "alpha",
        "class _M:\n"
        "    pass\n"
        "MANIFEST = _M()\n"
        "MANIFEST.key = 'alpha'\n"
        "MANIFEST.display_name = 'Alpha V2'\n",
    )
    registry.refresh()
    assert registry["alpha"] == "Alpha V2"


def test_builtin_registry_excludes_legacy_optional_plugins() -> None:
    BUILTIN_FEATURES.refresh()
    keys = set(BUILTIN_FEATURES.keys())
    assert "codex_image" not in keys
    assert "chatgpt_image" not in keys
    assert "game24" not in keys
    assert "math10" not in keys
    assert "auto_reply" not in keys
    assert "autorepeat" not in keys


def test_optional_plugins_are_not_bundled_in_core() -> None:
    builtin_root = Path(__file__).resolve().parents[1] / "worker" / "plugins" / "builtin"
    for key in ("auto_reply", "autorepeat", "game24", "math10"):
        assert not (builtin_root / key / "plugin.py").exists()
        assert not (builtin_root / key / "manifest.py").exists()


def test_builtin_registry_excludes_legacy_feature_keys() -> None:
    BUILTIN_FEATURES.refresh()
    keys = set(BUILTIN_FEATURES.keys())
    assert "group_admin" not in keys
    assert "monitor" not in keys
