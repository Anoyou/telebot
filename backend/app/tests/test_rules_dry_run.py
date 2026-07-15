from __future__ import annotations

import json

import pytest

from app.api import rules
from app.db.models.plugin import InstalledPlugin
from app.settings import settings
from app.worker.plugins.loader import _clear_installed_module_cache


class _DryRunDB:
    def __init__(self, row: InstalledPlugin | None) -> None:
        self.row = row

    async def get(self, model, key):  # noqa: ANN001
        if model is InstalledPlugin and self.row is not None and key == self.row.key:
            return self.row
        return None


def _write_installed_plugin(plugin_dir, *, key: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": key, "version": "1.0.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from .manifest import MANIFEST\n"
        "from .plugin import DemoPlugin, _dry_run_match\n"
        "PLUGIN_CLASS = DemoPlugin\n",
        encoding="utf-8",
    )
    (plugin_dir / "manifest.py").write_text(
        "from app.worker.plugins.manifest import Manifest\n"
        f"MANIFEST = Manifest(key={key!r}, display_name={key!r}, version='1.0.0')\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "from app.worker.plugins.base import Plugin\n\n"
        "class DemoPlugin(Plugin):\n"
        f"    key = {key!r}\n\n"
        "def _dry_run_match(cfg, text, *_args):\n"
        "    return text == cfg.get('keyword'), cfg.get('reply')\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_installed_plugin_dry_run_loads_repo_plugin(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "plugins_installed_dir", str(tmp_path / "installed"))
    _write_installed_plugin(tmp_path / "installed" / "auto_reply", key="auto_reply")
    db = _DryRunDB(
        InstalledPlugin(
            key="auto_reply",
            source="repo",
            source_url="https://github.com/Anoyou/telebot-plugins",
            enabled=True,
            trust_tier="community",
            signature_ok=True,
        )
    )

    try:
        matched, output = await rules._installed_plugin_dry_run_match(
            db,
            "auto_reply",
            {"keyword": "hi", "reply": "ok"},
            "hi",
            "private",
            123,
        )
    finally:
        _clear_installed_module_cache("auto_reply")

    assert matched is True
    assert output == "ok"


@pytest.mark.asyncio
async def test_installed_plugin_dry_run_requires_installed_record() -> None:
    matched, output = await rules._installed_plugin_dry_run_match(
        _DryRunDB(None),
        "auto_reply",
        {},
        "hi",
    )

    assert matched is False
    assert "未安装" in str(output)
