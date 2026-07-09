"""tp_plugin 脚手架 CLI 测试。

覆盖：
- ``new`` 生成的三种 profile 骨架均能通过 ``check``；
- session_game 骨架开局动作序列含 ``start_session`` 且先于 ``update_session``；
- ``check`` 对故意写错的事件报 ``unknown_events``；
- ``register`` 成功登记进台账，重复登记友好报错。

DB 沿用本仓测试惯例：内存 ``_FakeDB``（无真 sqlite fixture）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.db.models.account import Account
from app.db.models.feature import Feature
from app.db.models.plugin import InstalledPlugin
from app.services import plugin_repo_service as repo

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TP_PLUGIN_PATH = BACKEND_ROOT / "scripts" / "tp_plugin.py"


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("telepilot_tp_plugin_cli", TP_PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {TP_PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tp = _load_cli()


def _load_plugin_class(plugin_dir: Path, plugin_name: str):
    """把生成的 plugin.py 作为独立模块加载并返回 PLUGIN_CLASS。"""
    path = plugin_dir / "plugin.py"
    module_name = f"telepilot_scaffold_{plugin_name}_plugin"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.PLUGIN_CLASS


# ─────────────────────────────────────────────────────
# new + check
# ─────────────────────────────────────────────────────
@pytest.mark.parametrize("profile", ["session_game", "command", "passthrough"])
def test_new_scaffold_passes_check(profile: str, tmp_path: Path) -> None:
    name = f"demo_{profile}"
    plugin_dir = tmp_path / name
    tp.scaffold_plugin(name, profile, plugin_dir)

    for filename in ("plugin.json", "manifest.py", "plugin.py", "__init__.py"):
        assert (plugin_dir / filename).is_file(), f"{profile} 应生成 {filename}"

    report = tp.check_plugin(plugin_dir)
    assert report.ok, f"{profile} 骨架 check 应通过，errors={report.errors}"
    assert not report.unknown_events
    assert not report.unknown_filter_keys


async def test_session_game_scaffold_starts_session_before_update(tmp_path: Path) -> None:
    name = "demo_session"
    plugin_dir = tmp_path / name
    tp.scaffold_plugin(name, "session_game", plugin_dir)
    plugin = _load_plugin_class(plugin_dir, name)()

    actions = await plugin.on_interaction(
        None,
        f"start_{name}",
        {
            "source": {"type": "command", "chat_id": -100123, "message_id": 7},
            "trigger": {"type": "command", "command": name, "args": []},
            "session": {"scope": "chat", "channel": "userbot", "data": {}},
            "answer": "888",
            "prize": 100,
        },
    )
    types = [a["type"] for a in actions]
    assert "start_session" in types
    assert types[0] == "start_session"
    assert types.index("start_session") < types.index("update_session")
    assert actions[0]["chat_id"] == -100123
    assert actions[0]["entry_key"] == f"start_{name}"


async def test_session_game_scaffold_pays_out_on_win(tmp_path: Path) -> None:
    name = "demo_win"
    plugin_dir = tmp_path / name
    tp.scaffold_plugin(name, "session_game", plugin_dir)
    plugin = _load_plugin_class(plugin_dir, name)()

    actions = await plugin.on_interaction(
        None,
        f"start_{name}",
        {
            "source": {"type": "message", "chat_id": -100123, "message_id": 9, "text": "888"},
            "actor": {"user_id": 111, "display_name": "AAA"},
            "session": {"scope": "chat", "data": {"active": True, "answer": "888", "prize": 100}},
        },
    )
    types = [a["type"] for a in actions]
    assert "payout" in types
    assert types[-1] == "end_session"


def test_check_flags_unknown_events(tmp_path: Path) -> None:
    name = "demo_bad_events"
    plugin_dir = tmp_path / name
    tp.scaffold_plugin(name, "session_game", plugin_dir)

    pj_path = plugin_dir / "plugin.json"
    data = json.loads(pj_path.read_text(encoding="utf-8"))
    data["interaction_entries"][0]["events"] = ["command", "not_an_event", "message"]
    data["event_subscriptions"] = [
        {"events": ["message", "bogus_evt"], "source": ["userbot"], "filters": {"weird_key": 1}}
    ]
    pj_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    report = tp.check_plugin(plugin_dir)
    assert "not_an_event" in report.unknown_events
    assert "bogus_evt" in report.unknown_events
    assert "weird_key" in report.unknown_filter_keys


# ─────────────────────────────────────────────────────
# register（内存 FakeDB）
# ─────────────────────────────────────────────────────
class _FakeResult:
    def __init__(self, items: list) -> None:
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _FakeDB:
    """支持 install_local_plugin(default_enabled=False) 所需的最小行为。"""

    def __init__(self) -> None:
        self.installed: dict[str, InstalledPlugin] = {}
        self.features: dict[str, Feature] = {}

    async def get(self, model, pk):  # noqa: ANN001
        if model is InstalledPlugin:
            return self.installed.get(pk)
        if model is Feature:
            return self.features.get(pk)
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        if isinstance(obj, InstalledPlugin):
            self.installed[obj.key] = obj
        elif isinstance(obj, Feature):
            self.features[obj.key] = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, stmt):  # noqa: ANN001
        model = stmt.column_descriptions[0].get("entity")
        if model is Feature:
            return _FakeResult(list(self.features.values()))
        if model is InstalledPlugin:
            return _FakeResult(list(self.installed.values()))
        if model is Account:
            return _FakeResult([])
        return _FakeResult([])


@pytest.fixture()
def _redirect_plugin_paths(tmp_path: Path, monkeypatch):
    local_root = tmp_path / "local_imports"
    installed_root = tmp_path / "installed"
    local_root.mkdir(parents=True, exist_ok=True)
    installed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(repo, "_local_import_root", lambda: local_root)
    monkeypatch.setattr(repo.settings, "plugins_installed_dir", str(installed_root))
    return local_root, installed_root


async def test_register_success_then_duplicate(tmp_path: Path, _redirect_plugin_paths) -> None:
    name = "demo_register"
    source_dir = tmp_path / "src" / name
    tp.scaffold_plugin(name, "session_game", source_dir)

    db = _FakeDB()
    view = await tp.register_plugin(db, source_dir)

    assert getattr(view, "name", None) == name or getattr(view, "key", None) == name
    assert name in db.installed, "登记后 installed_plugin 应有台账行"
    local_root, installed_root = _redirect_plugin_paths
    assert (local_root / name / "plugin.json").is_file(), "源目录应被拷入 local_imports"
    assert (installed_root / name).is_dir(), "应落盘到 installed 目录"

    with pytest.raises(repo.DuplicatePluginName):
        await tp.register_plugin(db, source_dir)


async def test_register_via_session_reports_duplicate_friendly(tmp_path: Path, _redirect_plugin_paths, monkeypatch) -> None:
    name = "demo_register_msg"
    source_dir = tmp_path / "src" / name
    tp.scaffold_plugin(name, "session_game", source_dir)

    db = _FakeDB()

    class _Factory:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.db.base.AsyncSessionLocal", lambda: _Factory())

    code, message = await tp._register_via_session(source_dir, default_enabled=False)
    assert code == 0 and name in message

    code2, message2 = await tp._register_via_session(source_dir, default_enabled=False)
    assert code2 == 1
    assert "已登记" in message2 or "重复" in message2
