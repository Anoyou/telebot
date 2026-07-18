"""plugin loader 测试：mock DB（AsyncSessionLocal）+ Redis + Telethon client。

覆盖：
  - 注册表：核心平台 plugin 能被找到
  - 加载流程：enabled feature 会调到对应 plugin 的 on_startup（用 spy）
  - 配置热重载：reload_account_config 能刷新 ctx.rules / ctx.config，已禁用的会 shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.feature import (
    FEATURE_FORWARD,
    FEATURE_SCHEDULER,
)
from app.services.interaction.dedupe import interaction_message_claim_key
from app.worker.plugins import loader as loader_mod
from app.worker.plugins.base import Plugin, PluginContext
from app.worker.plugins.events import TelePilotEvent
from app.worker.plugins.loader import (
    _BUILTIN_MODULES,
    _clear_installed_module_cache,
    _import_builtins,
    _load_dir,
    _manifest_compatible,
    _missing_plugin_error,
    load_plugins_for_account,
    reload_account_config,
)
from app.worker.plugins.manifest import Manifest
from app.worker.ratelimit.humanize import HumanizeOpts


def _write_installed_plugin_json(plugin_dir, plugin_key: str, **extra) -> None:
    metadata = {
        "name": plugin_key,
        "key": plugin_key,
        "version": "0.1.0",
        **extra,
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────
# 极简 fake redis（loader 仅用 rpush）
# ─────────────────────────────────────────────────────
class _FakeRedis:
    def __init__(self) -> None:
        self.list_pushes: list[tuple[str, str]] = []
        self.sets: list[tuple[str, str, dict[str, Any]]] = []
        self.values: dict[str, str] = {}

    async def rpush(self, key: str, val: str) -> int:
        self.list_pushes.append((key, val))
        return len(self.list_pushes)

    async def publish(self, *_a, **_kw) -> int:
        return 0

    async def get(self, key, *_a, **_kw):
        return self.values.get(str(key))

    async def set(self, key: str, value: str, **kwargs):
        if kwargs.get("nx") and str(key) in self.values:
            return False
        self.sets.append((key, value, dict(kwargs)))
        self.values[str(key)] = value
        return True

    async def delete(self, *keys: str):
        removed = 0
        for key in keys:
            if str(key) in self.values:
                removed += 1
                self.values.pop(str(key), None)
        return removed

    async def keys(self, pattern: str):
        import fnmatch

        return [key for key in self.values if fnmatch.fnmatch(key, pattern)]

    async def scan_iter(self, match: str):
        import fnmatch

        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key

    async def script_load(self, *_a, **_kw):
        return "fake-sha"

    async def evalsha(self, *_a, **_kw):
        return [1, 0, 0]


def _mock_payout_delivery(monkeypatch):
    claim = AsyncMock(
        return_value=loader_mod.payout_compensation.PayoutDeliveryClaim(
            status="acquired",
            row_id=1,
            claim_token="test-token",
        )
    )
    complete = AsyncMock(return_value=True)
    release = AsyncMock()
    monkeypatch.setattr(loader_mod.payout_compensation, "claim_payout_delivery", claim)
    monkeypatch.setattr(loader_mod.payout_compensation, "complete_payout_delivery", complete)
    monkeypatch.setattr(loader_mod.payout_compensation, "release_payout_delivery_claim", release)
    return claim, complete, release


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", False),
        ("false", False),
        (1, False),
        (None, False),
    ],
)
def test_direct_passthrough_account_opt_in_requires_strict_true(value: object, expected: bool) -> None:
    ctx = PluginContext(
        account_id=1,
        feature_key="strict_direct_opt_in",
        account_config={"direct_passthrough": {"enabled": value}},
        generation=1,
    )

    assert loader_mod._plugin_direct_passthrough_enabled(ctx) is expected  # noqa: SLF001


# ─────────────────────────────────────────────────────
# Fake ORM 行（避免连真 PG）
# ─────────────────────────────────────────────────────
@dataclass
class _FakeAcc:
    id: int = 1
    cold_start_until: Any = None


@dataclass
class _FakeAF:
    account_id: int
    feature_key: str
    enabled: bool = True
    config: dict | None = None
    state: str = "disabled"
    last_error: str | None = None


@dataclass
class _FakeRule:
    id: int
    account_id: int
    feature_key: str
    enabled: bool = True
    priority: int = 100
    config: dict | None = None


@dataclass
class _FakeFeature:
    key: str
    manifest: dict | None = None


@dataclass
class _FakeInstalledPlugin:
    key: str
    enabled: bool = True
    version: str = "0.0.0"
    signature_ok: bool | None = True
    trust_tier: str = "community"
    last_install_error: str | None = None


@dataclass
class _FakePluginGlobalConfig:
    plugin_key: str
    config: dict[str, Any]


# ─────────────────────────────────────────────────────
# Fake AsyncSession：拦截 db.get / db.execute / db.commit
# ─────────────────────────────────────────────────────
class _FakeDB:
    """一个超薄 fake DB：以"按表归类的 rows"驱动 db.get / select 行为。"""

    def __init__(
        self,
        accounts: dict[int, _FakeAcc],
        humanize: dict[int, Any],
        afs: list[_FakeAF],
        rules: list[_FakeRule],
        features: dict[str, Any] | None = None,
        installed_plugins: dict[str, Any] | None = None,
        plugin_global_configs: dict[str, Any] | None = None,
    ) -> None:
        self.accounts = accounts
        self.humanize = humanize
        self.afs = afs
        self.rules = rules
        self.features = features or {}
        self.installed_plugins = installed_plugins or {}
        self.plugin_global_configs = plugin_global_configs or {}
        # 记录 update 调用，便于断言 state 改动
        self.update_calls: list[Any] = []

    async def get(self, model, pk):
        # 按 model.__tablename__ 区分
        name = getattr(model, "__tablename__", None) or getattr(
            getattr(model, "__table__", None), "name", None
        )
        if name == "account":
            return self.accounts.get(pk)
        if name == "humanize_config":
            return self.humanize.get(pk)
        if name == "feature":
            return self.features.get(pk)
        if name == "installed_plugin":
            return self.installed_plugins.get(pk)
        if name == "plugin_global_config":
            return self.plugin_global_configs.get(pk)
        return None

    async def execute(self, stmt):
        text = str(stmt).lower()
        # update -> 记录并返回空 result
        if text.startswith("update"):
            self.update_calls.append(stmt)
            values = {
                getattr(col, "key", ""): getattr(bind, "value", None)
                for col, bind in getattr(stmt, "_values", {}).items()
            }
            where_values = {
                getattr(getattr(expr, "left", None), "key", ""): getattr(
                    getattr(expr, "right", None),
                    "value",
                    None,
                )
                for expr in getattr(stmt, "_where_criteria", ())
            }
            if "account_feature" in text:
                for af in self.afs:
                    if where_values.get("account_id") not in {None, af.account_id}:
                        continue
                    if where_values.get("feature_key") not in {None, af.feature_key}:
                        continue
                    for key, value in values.items():
                        setattr(af, key, value)
            return _FakeResult([])
        # select account_feature where account_id = X
        if "account_feature" in text:
            return _FakeResult([(af,) for af in self.afs])
        if "rule" in text:
            return _FakeResult([(r,) for r in self.rules])
        return _FakeResult([])

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None

    def scalars(self):
        return _FakeScalars([r[0] for r in self._rows])


class _FakeScalars:
    def __init__(self, items: list[Any]):
        self._items = items

    def all(self):
        return list(self._items)


@asynccontextmanager
async def _fake_session_factory(db: _FakeDB):
    yield db


# ─────────────────────────────────────────────────────
# 用例 1：核心内置 plugin 都能被注册
# ─────────────────────────────────────────────────────
def test_import_builtins_registers_all_three() -> None:
    _import_builtins()
    from app.worker.plugins.base import all_plugins

    reg = all_plugins()
    for key in (
        FEATURE_FORWARD,
        FEATURE_SCHEDULER,
    ):
        assert key in reg, f"plugin {key} 未注册"


def test_builtin_modules_constant_is_complete() -> None:
    """_BUILTIN_MODULES 应当覆盖核心内置模块。"""
    assert {
        "forward",
        "scheduler",
    } <= set(_BUILTIN_MODULES)
    assert "codex_image" not in set(_BUILTIN_MODULES)


def test_builtin_rule_and_platform_manifests_are_explicit() -> None:
    """规则/平台类内置 manifest 应声明封闭 schema，避免配置页和校验语义漂移。"""
    from app.worker.plugins.builtin.forward.manifest import MANIFEST as FORWARD_MANIFEST
    from app.worker.plugins.builtin.scheduler.manifest import MANIFEST as SCHEDULER_MANIFEST

    for manifest in (FORWARD_MANIFEST, SCHEDULER_MANIFEST):
        schema = manifest.config_schema or {}
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False


def test_clear_installed_module_cache_drops_registered_class() -> None:
    """installed 插件更新时不能只清 sys.modules，还要丢掉注册表里的旧 class。"""
    from app.worker.plugins.base import _REGISTRY, register

    @register
    class _TempInstalledPlugin(Plugin):
        key = "_test_installed_reload"
        display_name = "installed reload"

    _TempInstalledPlugin._source = "installed"
    try:
        assert _REGISTRY["_test_installed_reload"] is _TempInstalledPlugin
        _clear_installed_module_cache("_test_installed_reload")
        assert "_test_installed_reload" not in _REGISTRY
    finally:
        _REGISTRY.pop("_test_installed_reload", None)


def test_clear_installed_module_cache_prunes_tracked_installed_modules(monkeypatch) -> None:
    """installed 模块缓存应只清理目标前缀，并同步维护模块名清单。"""
    import sys
    from types import ModuleType

    target_key = "_test_installed_cache_a"
    other_key = "_test_installed_cache_b"
    target_mod = loader_mod._installed_module_name(target_key)
    target_child_mod = f"{target_mod}.plugin"
    other_mod = loader_mod._installed_module_name(other_key)

    monkeypatch.setattr(
        loader_mod,
        "_INSTALLED_MODULE_NAMES",
        {target_mod, target_child_mod, other_mod},
    )
    sys.modules[target_mod] = ModuleType(target_mod)
    sys.modules[target_child_mod] = ModuleType(target_child_mod)
    sys.modules[other_mod] = ModuleType(other_mod)

    try:
        _clear_installed_module_cache(target_key)

        assert loader_mod._INSTALLED_MODULE_NAMES == {other_mod}
        assert target_mod not in sys.modules
        assert target_child_mod not in sys.modules
        assert other_mod in sys.modules
    finally:
        sys.modules.pop(target_mod, None)
        sys.modules.pop(target_child_mod, None)
        sys.modules.pop(other_mod, None)


def test_load_dir_tracks_installed_child_modules(tmp_path, monkeypatch) -> None:
    """installed 插件相对 import 出来的子模块也要进入清理清单。"""
    import sys

    from app.worker.plugins.base import _REGISTRY

    plugin_key = "_test_installed_tracking"
    plugin_dir = tmp_path / plugin_key
    plugin_dir.mkdir()
    _write_installed_plugin_json(plugin_dir, plugin_key)
    (plugin_dir / "__init__.py").write_text(
        "from .plugin import PLUGIN_CLASS, MANIFEST\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "\n".join(
            [
                "from app.worker.plugins.base import Plugin, register",
                "from app.worker.plugins.manifest import Manifest",
                "",
                "@register",
                "class TrackingPlugin(Plugin):",
                f"    key = {plugin_key!r}",
                "    display_name = 'tracking'",
                "",
                "PLUGIN_CLASS = TrackingPlugin",
                f"MANIFEST = Manifest(key={plugin_key!r}, display_name='tracking')",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_mod, "_INSTALLED_MODULE_NAMES", set())
    mod_name = loader_mod._installed_module_name(plugin_key)
    child_mod = f"{mod_name}.plugin"

    try:
        loaded = _load_dir(plugin_dir, source="installed")

        assert plugin_key in loaded
        assert {mod_name, child_mod} <= loader_mod._INSTALLED_MODULE_NAMES
        assert mod_name in sys.modules
        assert child_mod in sys.modules

        _clear_installed_module_cache(plugin_key)

        assert mod_name not in sys.modules
        assert child_mod not in sys.modules
        assert mod_name not in loader_mod._INSTALLED_MODULE_NAMES
        assert child_mod not in loader_mod._INSTALLED_MODULE_NAMES
    finally:
        sys.modules.pop(mod_name, None)
        sys.modules.pop(child_mod, None)
        _REGISTRY.pop(plugin_key, None)


def test_load_dir_warns_manifest_event_subscription_lint_once(tmp_path, monkeypatch, caplog) -> None:
    """Python Manifest 的 event_subscriptions 也要在加载期暴露订阅风险。"""
    import sys

    from app.worker.plugins.base import _REGISTRY

    plugin_key = "_test_installed_subscription_lint"
    plugin_dir = tmp_path / plugin_key
    plugin_dir.mkdir()
    _write_installed_plugin_json(plugin_dir, plugin_key)
    (plugin_dir / "__init__.py").write_text(
        "from .plugin import PLUGIN_CLASS, MANIFEST\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "\n".join(
            [
                "from app.worker.plugins.base import Plugin, register",
                "from app.worker.plugins.manifest import Manifest",
                "",
                "@register",
                "class SubscriptionLintPlugin(Plugin):",
                f"    key = {plugin_key!r}",
                "    display_name = 'subscription lint'",
                "",
                "PLUGIN_CLASS = SubscriptionLintPlugin",
                "MANIFEST = Manifest(",
                f"    key={plugin_key!r},",
                "    display_name='subscription lint',",
                "    event_subscriptions=[{",
                "        'events': ['message', 'ghost_event'],",
                "        'filters': {'mystery': True},",
                "    }],",
                ")",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_mod, "_INSTALLED_MODULE_NAMES", set())
    caplog.set_level(logging.WARNING, logger=loader_mod.log.name)
    mod_name = loader_mod._installed_module_name(plugin_key)
    child_mod = f"{mod_name}.plugin"

    try:
        loaded = _load_dir(plugin_dir, source="installed")

        assert plugin_key in loaded
        messages = [record.getMessage() for record in caplog.records]
        assert any(plugin_key in item and "不会生效" in item for item in messages)
        assert any("ghost_event" in item and "不会匹配任何当前支持的事件" in item for item in messages)
    finally:
        sys.modules.pop(mod_name, None)
        sys.modules.pop(child_mod, None)
        _REGISTRY.pop(plugin_key, None)


@pytest.mark.asyncio
async def test_load_dir_builds_simple_mode_command_plugin_and_dispatches(tmp_path, monkeypatch) -> None:
    """无显式 manifest 的 @plugin.command 单函数插件应能加载并走现有命令分发。"""
    import sys

    from app.worker.command import dispatch_plugin_command, unregister_all_plugin_commands
    from app.worker.plugins.base import _REGISTRY

    plugin_key = "_test_simple_ping"
    plugin_dir = tmp_path / plugin_key
    plugin_dir.mkdir()
    _write_installed_plugin_json(plugin_dir, plugin_key)
    (plugin_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from telepilot import plugin",
                "",
                "@plugin.command('sdkping')",
                "async def sdkping(ctx):",
                "    await ctx.reply('pong')",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_mod, "_INSTALLED_MODULE_NAMES", set())
    mod_name = loader_mod._installed_module_name(plugin_key)

    try:
        loaded = _load_dir(plugin_dir, source="installed")

        assert plugin_key in loaded
        cls = loaded[plugin_key]
        manifest = cls._manifest
        assert manifest.key == plugin_key
        assert manifest.permissions == ["read_event", "send_message"]
        assert sorted(cls.commands) == ["sdkping"]

        ctx = PluginContext(account_id=1, feature_key=plugin_key)
        for command, handler in cls.commands.items():
            loader_mod.register_plugin_command(
                command,
                loader_mod._wrap_cmd(handler, ctx),
                owner_plugin_key=plugin_key,
                generation=1,
            )

        event = SimpleNamespace(
            chat_id=-100123,
            raw_text=",sdkping",
            trace_id="trace-simple-ping",
            reply=AsyncMock(),
        )
        dispatched = await dispatch_plugin_command(
            None,
            event,
            [],
            1,
            plugin_key=plugin_key,
            method="sdkping",
        )

        assert dispatched is True
        event.reply.assert_awaited_once_with("pong")
    finally:
        unregister_all_plugin_commands(owner_plugin_key=plugin_key)
        sys.modules.pop(mod_name, None)
        _REGISTRY.pop(plugin_key, None)
        _clear_installed_module_cache(plugin_key)


def test_simple_mode_plugin_coexists_with_explicit_manifest_plugin(tmp_path, monkeypatch) -> None:
    """隐式 manifest 与显式 manifest 插件应能同时由 loader 加载。"""
    import sys

    from app.worker.plugins.base import _REGISTRY

    simple_key = "_test_simple_coexist"
    explicit_key = "_test_explicit_coexist"
    simple_dir = tmp_path / simple_key
    explicit_dir = tmp_path / explicit_key
    simple_dir.mkdir()
    explicit_dir.mkdir()
    _write_installed_plugin_json(simple_dir, simple_key)
    _write_installed_plugin_json(explicit_dir, explicit_key)
    (simple_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from telepilot import plugin",
                "",
                "@plugin.command('hello')",
                "async def hello(ctx):",
                "    await ctx.reply('hi')",
            ]
        ),
        encoding="utf-8",
    )
    (explicit_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from app.worker.plugins.base import Plugin, register",
                "from app.worker.plugins.manifest import Manifest",
                "",
                "@register",
                "class ExplicitPlugin(Plugin):",
                f"    key = {explicit_key!r}",
                "    display_name = 'explicit coexist'",
                "",
                "PLUGIN_CLASS = ExplicitPlugin",
                f"MANIFEST = Manifest(key={explicit_key!r}, display_name='explicit coexist')",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_mod, "_INSTALLED_MODULE_NAMES", set())
    simple_mod = loader_mod._installed_module_name(simple_key)
    explicit_mod = loader_mod._installed_module_name(explicit_key)

    try:
        simple_loaded = _load_dir(simple_dir, source="installed")
        explicit_loaded = _load_dir(explicit_dir, source="installed")

        assert simple_key in simple_loaded
        assert explicit_key in explicit_loaded
        assert _REGISTRY[simple_key] is simple_loaded[simple_key]
        assert _REGISTRY[explicit_key] is explicit_loaded[explicit_key]
        assert simple_loaded[simple_key]._manifest.permissions == ["read_event", "send_message"]
        assert explicit_loaded[explicit_key]._manifest.display_name == "explicit coexist"
    finally:
        for name in (simple_mod, explicit_mod):
            sys.modules.pop(name, None)
        _REGISTRY.pop(simple_key, None)
        _REGISTRY.pop(explicit_key, None)
        _clear_installed_module_cache(simple_key)
        _clear_installed_module_cache(explicit_key)


def test_clear_installed_module_cache_prunes_lazy_and_origin_modules(tmp_path, monkeypatch) -> None:
    """运行期懒加载的子模块和插件目录来源模块也不能残留旧代码。"""
    import importlib
    import sys
    from types import ModuleType

    from app.worker.plugins.base import _REGISTRY

    plugin_key = "_test_installed_lazy_tracking"
    plugin_dir = tmp_path / plugin_key
    plugin_dir.mkdir()
    _write_installed_plugin_json(plugin_dir, plugin_key)
    (plugin_dir / "__init__.py").write_text(
        "from .plugin import PLUGIN_CLASS, MANIFEST\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "\n".join(
            [
                "from app.worker.plugins.base import Plugin, register",
                "from app.worker.plugins.manifest import Manifest",
                "",
                "@register",
                "class LazyTrackingPlugin(Plugin):",
                f"    key = {plugin_key!r}",
                "    display_name = 'lazy tracking'",
                "",
                "PLUGIN_CLASS = LazyTrackingPlugin",
                f"MANIFEST = Manifest(key={plugin_key!r}, display_name='lazy tracking')",
            ]
        ),
        encoding="utf-8",
    )
    (plugin_dir / "late.py").write_text("VALUE = 'new runtime module'\n", encoding="utf-8")
    (plugin_dir / "legacy_helper.py").write_text("VALUE = 'legacy helper'\n", encoding="utf-8")

    monkeypatch.setattr(loader_mod, "_INSTALLED_MODULE_NAMES", set())
    monkeypatch.setattr(loader_mod, "_installed_dir", lambda: tmp_path)
    mod_name = loader_mod._installed_module_name(plugin_key)
    late_mod_name = f"{mod_name}.late"
    legacy_mod_name = "_test_installed_lazy_legacy_helper"

    try:
        loaded = _load_dir(plugin_dir, source="installed")
        assert plugin_key in loaded

        importlib.invalidate_caches()
        importlib.import_module(late_mod_name)
        assert late_mod_name in sys.modules
        assert late_mod_name not in loader_mod._INSTALLED_MODULE_NAMES

        legacy_mod = ModuleType(legacy_mod_name)
        legacy_mod.__file__ = str(plugin_dir / "legacy_helper.py")
        sys.modules[legacy_mod_name] = legacy_mod

        _clear_installed_module_cache(plugin_key)

        assert mod_name not in sys.modules
        assert late_mod_name not in sys.modules
        assert legacy_mod_name not in sys.modules
        assert mod_name not in loader_mod._INSTALLED_MODULE_NAMES
    finally:
        for name in (mod_name, f"{mod_name}.plugin", late_mod_name, legacy_mod_name):
            sys.modules.pop(name, None)
        _REGISTRY.pop(plugin_key, None)


def test_installed_plugin_identity_mismatch_does_not_pollute_registry(tmp_path) -> None:
    """已授权目录不能通过 MANIFEST.key/Plugin.key 冒充其它插件。"""
    from app.worker.plugins.base import _REGISTRY

    class _ExistingAutoReply(Plugin):
        key = "auto_reply"
        display_name = "existing"

    _REGISTRY["auto_reply"] = _ExistingAutoReply

    plugin_dir = tmp_path / "evil"
    plugin_dir.mkdir()
    _write_installed_plugin_json(plugin_dir, "evil")
    (plugin_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from app.worker.plugins.base import Plugin, register",
                "from app.worker.plugins.manifest import Manifest",
                "",
                "@register",
                "class EvilPlugin(Plugin):",
                "    key = 'auto_reply'",
                "    display_name = 'evil'",
                "",
                "PLUGIN_CLASS = EvilPlugin",
                "MANIFEST = Manifest(key='auto_reply', display_name='evil')",
            ]
        ),
        encoding="utf-8",
    )

    try:
        loaded = _load_dir(plugin_dir, source="installed")
        assert loaded == {}
        assert _REGISTRY.get("auto_reply") is _ExistingAutoReply
        assert "evil" not in _REGISTRY
    finally:
        _REGISTRY.pop("auto_reply", None)
        _clear_installed_module_cache("evil")


def test_installed_plugin_import_failure_rolls_back_registry(tmp_path) -> None:
    """插件 import 中途失败时，已发生的 @register 副作用也要回滚。"""
    from app.worker.plugins.base import _REGISTRY

    class _ExistingAutoReply(Plugin):
        key = "auto_reply"
        display_name = "existing"

    _REGISTRY["auto_reply"] = _ExistingAutoReply

    plugin_dir = tmp_path / "boom"
    plugin_dir.mkdir()
    _write_installed_plugin_json(plugin_dir, "boom")
    (plugin_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from app.worker.plugins.base import Plugin, register",
                "",
                "@register",
                "class BoomPlugin(Plugin):",
                "    key = 'auto_reply'",
                "    display_name = 'boom'",
                "",
                "raise RuntimeError('boom after register')",
            ]
        ),
        encoding="utf-8",
    )

    try:
        loaded = _load_dir(plugin_dir, source="installed")
        assert loaded == {}
        assert _REGISTRY.get("auto_reply") is _ExistingAutoReply
        assert "boom" not in _REGISTRY
    finally:
        _REGISTRY.pop("auto_reply", None)
        _clear_installed_module_cache("boom")


def test_incompatible_installed_plugin_is_rejected_before_python_exec(tmp_path) -> None:
    plugin_key = "future_plugin"
    plugin_dir = tmp_path / plugin_key
    plugin_dir.mkdir()
    sentinel = tmp_path / "imported.txt"
    _write_installed_plugin_json(
        plugin_dir,
        plugin_key,
        min_telepilot_version="999.0.0",
    )
    (plugin_dir / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )

    try:
        loaded = _load_dir(plugin_dir, source="installed")

        assert loaded == {}
        assert not sentinel.exists()
        assert "当前 TelePilot 版本太旧" in loader_mod._PLUGIN_LOAD_ERRORS[plugin_key]
        assert "插件至少需要 999.0.0" in loader_mod._PLUGIN_LOAD_ERRORS[plugin_key]
        assert "请先更新 TelePilot，再重新启用插件" in loader_mod._PLUGIN_LOAD_ERRORS[plugin_key]
    finally:
        loader_mod._PLUGIN_LOAD_ERRORS.pop(plugin_key, None)


def test_clear_installed_module_cache_removes_pycache(monkeypatch, tmp_path) -> None:
    """git pull 后旧 __pycache__ 也要清掉，避免重新 import 仍读旧字节码。"""

    plugin_dir = tmp_path / "installed" / "_test_installed_reload"
    cache_dir = plugin_dir / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin.cpython-312.pyc").write_bytes(b"stale")
    monkeypatch.setattr(loader_mod, "_installed_dir", lambda: tmp_path / "installed")

    _clear_installed_module_cache("_test_installed_reload")

    assert not cache_dir.exists()


@pytest.mark.asyncio
async def test_authorize_installed_plugin_rejects_orphan_directory() -> None:
    """磁盘/Feature 中有 installed 插件但没有 installed_plugin 记录时，必须拒绝。"""

    db = _FakeDB(accounts={}, humanize={}, afs=[], rules=[])

    auth = await loader_mod._authorize_installed_plugin(db, "orphan_demo")

    assert auth.allowed is False
    assert auth.state == "failed"
    assert "installed_plugin missing" in (auth.last_error or "")


@pytest.mark.asyncio
async def test_authorize_installed_plugin_honors_installed_plugin_enabled() -> None:
    """installed_plugin.enabled=false 必须成为运行期硬门禁。"""

    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            "zip_demo": _FakeInstalledPlugin(
                key="zip_demo",
                enabled=False,
                signature_ok=True,
                trust_tier="community",
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, "zip_demo")

    assert auth.allowed is False
    assert auth.state == "disabled"
    assert "installed_plugin.enabled=False" in (auth.last_error or "")


@pytest.mark.asyncio
async def test_authorize_installed_plugin_rejects_failed_signature() -> None:
    """签名失败的 zip 插件即使 enabled=true 也不能被 worker 加载。"""

    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            "bad_sig": _FakeInstalledPlugin(
                key="bad_sig",
                enabled=True,
                signature_ok=False,
                trust_tier="community",
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, "bad_sig")

    assert auth.allowed is False
    assert auth.state == "failed"
    assert "PLUGIN_SIGNATURE_FAILED" in (auth.last_error or "")


@pytest.mark.asyncio
async def test_authorize_installed_plugin_allows_legacy_unsigned_when_enabled(monkeypatch) -> None:
    """历史 signature_ok=NULL 插件在兼容开关开启时继续可加载，避免升级后突然失效。"""

    monkeypatch.setattr(loader_mod.app_settings, "plugin_allow_legacy_unsigned_plugins", True)
    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            "legacy_unsigned": _FakeInstalledPlugin(
                key="legacy_unsigned",
                enabled=True,
                signature_ok=None,
                trust_tier="community",
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, "legacy_unsigned")

    assert auth.allowed is True


@pytest.mark.asyncio
async def test_authorize_installed_plugin_rejects_legacy_unsigned_when_disabled(monkeypatch) -> None:
    """管理员关闭兼容开关后，signature_ok=NULL 的历史插件必须被拒绝。"""

    monkeypatch.setattr(loader_mod.app_settings, "plugin_allow_legacy_unsigned_plugins", False)
    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            "legacy_unsigned": _FakeInstalledPlugin(
                key="legacy_unsigned",
                enabled=True,
                signature_ok=None,
                trust_tier="community",
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, "legacy_unsigned")

    assert auth.allowed is False
    assert auth.state == "failed"
    assert "PLUGIN_SIGNATURE_UNKNOWN" in (auth.last_error or "")


@pytest.mark.asyncio
async def test_authorize_installed_plugin_rejects_last_install_error() -> None:
    """installed_plugin.last_install_error 非空时不能加载。"""

    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            "remote_demo": _FakeInstalledPlugin(
                key="remote_demo",
                enabled=True,
                signature_ok=True,
                trust_tier="community",
                last_install_error="clone failed",
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, "remote_demo")

    assert auth.allowed is False
    assert auth.state == "failed"
    assert "PLUGIN_INSTALL_FAILED" in (auth.last_error or "")


@pytest.mark.asyncio
async def test_authorize_installed_plugin_rejects_orphan_trust_tier() -> None:
    """trust_tier=orphan 的 installed_plugin 记录仍不能被 worker 加载。"""

    plugin_key = "orphan_tier"
    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            plugin_key: _FakeInstalledPlugin(
                key=plugin_key,
                enabled=True,
                signature_ok=True,
                trust_tier="orphan",
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, plugin_key)

    assert auth.allowed is False
    assert auth.state == "failed"
    assert "PLUGIN_LOAD_ORPHAN" in (auth.last_error or "")


@pytest.mark.asyncio
async def test_authorize_installed_plugin_allows_installed_plugin_when_valid() -> None:
    """installed_plugin 记录完整且可用时允许加载。"""

    plugin_key = "zip_consistent"
    db = _FakeDB(
        accounts={},
        humanize={},
        afs=[],
        rules=[],
        installed_plugins={
            plugin_key: _FakeInstalledPlugin(
                key=plugin_key,
                enabled=True,
                signature_ok=True,
                trust_tier="community",
                last_install_error=None,
            )
        },
    )

    auth = await loader_mod._authorize_installed_plugin(db, plugin_key)

    assert auth.allowed is True
    assert auth.state == "active"


@pytest.mark.asyncio
async def test_activate_marks_orphan_installed_plugin_failed(monkeypatch, tmp_path) -> None:
    """启动/reload 遇到孤儿 installed 目录时，要写回结构化 failed 状态。"""

    plugin_key = "_test_orphan_installed"
    plugin_dir = tmp_path / "installed" / plugin_key
    plugin_dir.mkdir(parents=True)
    monkeypatch.setattr(loader_mod, "_installed_dir", lambda: tmp_path / "installed")

    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
    )
    state = loader_mod._AccountState(account_id=1)
    state.client = MagicMock()
    redis = _FakeRedis()

    await loader_mod._activate(db, state, af, redis)

    assert plugin_key not in state.instances
    assert af.state == "failed"
    assert af.last_error is not None
    assert "PLUGIN_LOAD_ORPHAN" in af.last_error
    assert any("缺少 installed_plugin" in payload for _, payload in redis.list_pushes)


@pytest.mark.asyncio
async def test_write_account_feature_load_state_updates_plugin_runtime_status(monkeypatch) -> None:
    af = _FakeAF(account_id=1, feature_key="demo_plugin", enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
    )
    update_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", update_status)

    await loader_mod._write_account_feature_load_state(
        db,
        1,
        "demo_plugin",
        state="failed",
        last_error="boom",
    )

    update_status.assert_awaited_once_with(
        account_id=1,
        plugin_key="demo_plugin",
        enabled=False,
        load_status="failed",
        last_load_error="boom",
    )


@pytest.mark.asyncio
async def test_activate_installed_plugin_import_failure_updates_runtime_status(tmp_path, monkeypatch) -> None:
    plugin_key = "_test_import_failed"
    plugin_dir = tmp_path / "installed" / plugin_key
    plugin_dir.mkdir(parents=True)
    _write_installed_plugin_json(plugin_dir, plugin_key)
    (plugin_dir / "__init__.py").write_text("raise RuntimeError('broken import')\n", encoding="utf-8")
    monkeypatch.setattr(loader_mod, "_installed_dir", lambda: tmp_path / "installed")
    update_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", update_status)

    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        installed_plugins={plugin_key: _FakeInstalledPlugin(plugin_key)},
    )
    state = loader_mod._AccountState(account_id=1)
    state.client = MagicMock()
    redis = _FakeRedis()

    await loader_mod._activate(db, state, af, redis)

    assert plugin_key not in state.instances
    assert af.state == "failed"
    assert af.last_error == "插件加载失败。请检查插件文件是否完整、版本是否兼容，然后重试。"
    update_status.assert_awaited_with(
        account_id=1,
        plugin_key=plugin_key,
        enabled=False,
        load_status="failed",
        last_load_error="插件加载失败。请检查插件文件是否完整、版本是否兼容，然后重试。",
    )


@pytest.mark.asyncio
async def test_activate_incompatible_plugin_exposes_plain_language_version_error(tmp_path, monkeypatch) -> None:
    plugin_key = "_test_version_too_old"
    plugin_dir = tmp_path / "installed" / plugin_key
    plugin_dir.mkdir(parents=True)
    _write_installed_plugin_json(plugin_dir, plugin_key, min_telepilot_version="999.0.0")
    (plugin_dir / "__init__.py").write_text("raise AssertionError('must not import')\n", encoding="utf-8")
    monkeypatch.setattr(loader_mod, "_installed_dir", lambda: tmp_path / "installed")
    update_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", update_status)

    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        installed_plugins={plugin_key: _FakeInstalledPlugin(plugin_key)},
    )
    state = loader_mod._AccountState(account_id=1)
    state.client = MagicMock()

    try:
        await loader_mod._activate(db, state, af, _FakeRedis())

        assert af.state == "failed"
        assert "当前 TelePilot 版本太旧" in af.last_error
        assert "插件至少需要 999.0.0" in af.last_error
        assert "请先更新 TelePilot，再重新启用插件" in af.last_error
        update_status.assert_awaited_with(
            account_id=1,
            plugin_key=plugin_key,
            enabled=False,
            load_status="failed",
            last_load_error=af.last_error,
        )
    finally:
        loader_mod._PLUGIN_LOAD_ERRORS.pop(plugin_key, None)


@pytest.mark.asyncio
async def test_reload_account_config_unloads_installed_plugin_when_authorization_denied(monkeypatch) -> None:
    """已加载插件若全局开关被关闭，reload 时要立即卸载并写回 disabled。"""

    from app.worker.plugins.base import _REGISTRY, register

    shutdown_spy = AsyncMock()

    @register
    class _TempInstalledRuntimePlugin(Plugin):
        key = "_test_runtime_remote_disabled"
        display_name = "运行期禁用测试"

        async def on_shutdown(self, ctx: PluginContext) -> None:  # noqa: D401
            await shutdown_spy(ctx)

    _TempInstalledRuntimePlugin._source = "installed"
    plugin_key = _TempInstalledRuntimePlugin.key
    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        installed_plugins={
            plugin_key: _FakeInstalledPlugin(
                key=plugin_key,
                enabled=False,
                signature_ok=True,
                trust_tier="community",
            )
        },
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(db))

    state = loader_mod._AccountState(account_id=1)
    state.redis = _FakeRedis()
    inst = _TempInstalledRuntimePlugin()
    ctx = PluginContext(account_id=1, feature_key=plugin_key, client=MagicMock())
    state.instances[plugin_key] = inst
    state.contexts[plugin_key] = ctx
    loader_mod._STATES[1] = state

    try:
        await reload_account_config(account_id=1)
    finally:
        loader_mod._STATES.pop(1, None)
        _REGISTRY.pop(plugin_key, None)

    shutdown_spy.assert_awaited_once_with(ctx)
    assert plugin_key not in state.instances
    assert af.state == "disabled"
    assert af.last_error == "PLUGIN_DISABLED: installed_plugin.enabled=False"


@pytest.mark.asyncio
async def test_reload_account_config_force_reload_clears_installed_module_cache(monkeypatch) -> None:
    """远程更新触发 reload_config(plugin_key) 时，要清掉 installed 模块缓存再重载。"""

    from app.worker.plugins.base import _REGISTRY, register

    shutdown_spy = AsyncMock()
    cleared: list[str] = []

    @register
    class _TempInstalledForceReloadPlugin(Plugin):
        key = "_test_force_reload_installed"
        display_name = "强制重载测试"

        async def on_shutdown(self, ctx: PluginContext) -> None:  # noqa: D401
            await shutdown_spy(ctx)

    _TempInstalledForceReloadPlugin._source = "installed"
    plugin_key = _TempInstalledForceReloadPlugin.key
    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        installed_plugins={
            plugin_key: _FakeInstalledPlugin(
                key=plugin_key,
                enabled=True,
                signature_ok=True,
                trust_tier="community",
            )
        },
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(db))
    monkeypatch.setattr(loader_mod, "_clear_installed_module_cache", lambda key: cleared.append(key))

    state = loader_mod._AccountState(account_id=1)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    inst = _TempInstalledForceReloadPlugin()
    ctx = PluginContext(account_id=1, feature_key=plugin_key, client=MagicMock())
    state.instances[plugin_key] = inst
    state.contexts[plugin_key] = ctx
    loader_mod._STATES[1] = state

    try:
        await reload_account_config(account_id=1, payload={"plugin_key": plugin_key})
    finally:
        loader_mod._STATES.pop(1, None)
        _REGISTRY.pop(plugin_key, None)

    shutdown_spy.assert_awaited_once_with(ctx)
    assert cleared == [plugin_key]
    assert plugin_key in state.instances


@pytest.mark.asyncio
async def test_reload_account_config_reconciles_installed_version_drift(monkeypatch) -> None:
    """周期 reconcile 兜底时，DB 已更新但内存插件版本旧，也必须强制重载。"""

    from app.worker.plugins.base import _REGISTRY, register

    shutdown_spy = AsyncMock()
    cleared: list[str] = []
    activated: list[str] = []

    @register
    class _TempInstalledVersionDriftPlugin(Plugin):
        key = "_test_version_drift_installed"
        display_name = "版本漂移重载测试"

        async def on_shutdown(self, ctx: PluginContext) -> None:  # noqa: D401
            await shutdown_spy(ctx)

    _TempInstalledVersionDriftPlugin._source = "installed"
    _TempInstalledVersionDriftPlugin._manifest = Manifest(
        key=_TempInstalledVersionDriftPlugin.key,
        display_name="版本漂移重载测试",
        version="1.0.0",
    )
    plugin_key = _TempInstalledVersionDriftPlugin.key
    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        installed_plugins={
            plugin_key: _FakeInstalledPlugin(
                key=plugin_key,
                enabled=True,
                version="1.1.0",
                signature_ok=True,
                trust_tier="community",
            )
        },
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(db))
    monkeypatch.setattr(loader_mod, "_clear_installed_module_cache", lambda key: cleared.append(key))

    async def _activate_spy(_db, _state, _af, _redis):  # noqa: ANN001
        activated.append(_af.feature_key)

    monkeypatch.setattr(loader_mod, "_activate", _activate_spy)

    state = loader_mod._AccountState(account_id=1)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    inst = _TempInstalledVersionDriftPlugin()
    ctx = PluginContext(account_id=1, feature_key=plugin_key, client=MagicMock())
    state.instances[plugin_key] = inst
    state.contexts[plugin_key] = ctx
    loader_mod._STATES[1] = state

    try:
        await reload_account_config(account_id=1, payload={"source": "periodic_reconcile"})
    finally:
        loader_mod._STATES.pop(1, None)
        _REGISTRY.pop(plugin_key, None)

    shutdown_spy.assert_awaited_once_with(ctx)
    assert cleared == [plugin_key]
    assert activated == [plugin_key]


@pytest.mark.asyncio
async def test_reload_account_config_force_reload_unregisters_stale_commands(monkeypatch) -> None:
    """reload_config(plugin_key) 清 registry 后也必须注销该插件遗留命令。"""

    from app.worker.command import (
        _PLUGIN_COMMANDS,
        register_plugin_command,
        unregister_all_plugin_commands,
    )
    from app.worker.plugins.base import _REGISTRY, register

    shutdown_spy = AsyncMock()

    @register
    class _TempInstalledCommandCleanupPlugin(Plugin):
        key = "_test_force_reload_command_cleanup"
        display_name = "命令清理测试"

        async def on_shutdown(self, ctx: PluginContext) -> None:  # noqa: D401
            await shutdown_spy(ctx)

    _TempInstalledCommandCleanupPlugin._source = "installed"
    plugin_key = _TempInstalledCommandCleanupPlugin.key

    async def _old_handler(*_a, **_kw):
        return None

    register_plugin_command("old_hot_reload_cmd", _old_handler, owner_plugin_key=plugin_key)
    assert _PLUGIN_COMMANDS["old_hot_reload_cmd"].owner_plugin_key == plugin_key

    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=False, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(db))

    state = loader_mod._AccountState(account_id=1)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    inst = _TempInstalledCommandCleanupPlugin()
    ctx = PluginContext(account_id=1, feature_key=plugin_key, client=MagicMock())
    state.instances[plugin_key] = inst
    state.contexts[plugin_key] = ctx
    loader_mod._STATES[1] = state

    try:
        await reload_account_config(account_id=1, payload={"plugin_key": plugin_key})
    finally:
        loader_mod._STATES.pop(1, None)
        _REGISTRY.pop(plugin_key, None)
        unregister_all_plugin_commands(owner_plugin_key=plugin_key)

    shutdown_spy.assert_awaited_once_with(ctx)
    assert plugin_key not in state.instances
    assert "old_hot_reload_cmd" not in _PLUGIN_COMMANDS


def test_missing_plugin_error_uses_codex_image_repo_plugin_hint() -> None:
    err, message = _missing_plugin_error("codex_image")
    assert "codex_image" in err
    assert "插件库插件" in message
    assert "plugins/installed/codex_image" in message


def test_installed_plugin_runtime_drift_detects_same_version_disk_update() -> None:
    class _LoadedPlugin(Plugin):
        key = "_test_same_version_drift"
        display_name = "同版本覆盖测试"

    _LoadedPlugin._manifest = Manifest(
        key=_LoadedPlugin.key,
        display_name="同版本覆盖测试",
        version="1.0.0",
    )
    _LoadedPlugin._loaded_at = 1000.0

    drift, reason = loader_mod._installed_plugin_runtime_drift(
        _LoadedPlugin,
        None,
        SimpleNamespace(
            version="1.0.0",
            updated_at=datetime.fromtimestamp(1005.0, UTC),
            manifest_json={
                "_telepilot_remote": {
                    "runtime_revision_at": "1970-01-01T00:16:40.500000+00:00"
                }
            },
        ),
    )

    assert drift is True
    assert reason and "runtime_revision_at" in reason


def test_installed_plugin_runtime_drift_ignores_update_check_timestamp() -> None:
    class _LoadedPlugin(Plugin):
        key = "_test_update_check_only"
        display_name = "更新检查时间测试"

    _LoadedPlugin._manifest = Manifest(
        key=_LoadedPlugin.key,
        display_name="更新检查时间测试",
        version="1.0.0",
    )
    _LoadedPlugin._loaded_at = 1000.0

    drift, reason = loader_mod._installed_plugin_runtime_drift(
        _LoadedPlugin,
        None,
        SimpleNamespace(
            version="1.0.0",
            updated_at=datetime.fromtimestamp(1005.0, UTC),
            manifest_json={
                "_telepilot_remote": {
                    "last_update_check_at": "1970-01-01T00:16:45+00:00"
                }
            },
        ),
    )

    assert drift is False
    assert reason is None


def test_manifest_min_telepilot_version_is_preferred() -> None:
    manifest = Manifest(
        key="_test_version",
        display_name="版本测试",
        min_telepilot_version="999.0.0",
        min_telebot_version="0.1.0",
    )

    ok, reason = _manifest_compatible(manifest)

    assert ok is False
    assert reason is not None
    assert "当前 TelePilot 版本太旧" in reason
    assert "插件至少需要 999.0.0" in reason
    assert "请先更新 TelePilot，再重新启用插件" in reason


def test_manifest_min_telebot_version_kept_as_legacy_alias() -> None:
    manifest = Manifest(
        key="_test_legacy_version",
        display_name="旧字段版本测试",
        min_telebot_version="999.0.0",
    )

    ok, reason = _manifest_compatible(manifest)

    assert ok is False
    assert reason is not None
    assert "当前 TelePilot 版本太旧" in reason
    assert "插件至少需要 999.0.0" in reason
    assert "请先更新 TelePilot，再重新启用插件" in reason


@pytest.mark.asyncio
async def test_owner_only_false_incoming_command_text_does_not_dispatch_command(monkeypatch) -> None:
    from app.worker.command import unregister_all_plugin_commands
    from app.worker.plugins.base import _REGISTRY, register

    command_calls: list[tuple[list[str], int]] = []
    message_calls: list[str] = []

    async def handler(client, event, args, account_id, ctx):  # noqa: ANN001
        command_calls.append((args, account_id))

    @register
    class _PublicCommandPlugin(Plugin):
        key = "_test_public_command"
        display_name = "公开命令测试"
        message_channels = {"incoming"}
        owner_only = False
        commands = {"cy": handler}

    class _Event:
        raw_text = "。cy 100"
        chat_id = -1001
        sender_id = 42
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    async def _on_message(self, ctx: PluginContext, event: Any) -> None:
        message_calls.append(str(getattr(event, "raw_text", "")))

    monkeypatch.setattr(_PublicCommandPlugin, "on_message", _on_message)
    fake_db = _FakeDB(
        accounts={7: _FakeAcc(id=7)},
        humanize={7: None},
        afs=[_FakeAF(account_id=7, feature_key="_test_public_command", enabled=True, config={})],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=7, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert command_calls == []
        assert message_calls == ["。cy 100"]
    finally:
        loader_mod._STATES.pop(7, None)
        _REGISTRY.pop("_test_public_command", None)
        unregister_all_plugin_commands(owner_plugin_key="_test_public_command")


@pytest.mark.asyncio
async def test_userbot_event_bus_dispatch_invokes_on_event_and_records_action(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    event_calls: list[str] = []
    legacy_calls: list[str] = []

    @register
    class _TracePlugin(Plugin):
        key = "_test_trace_dispatch"
        display_name = "Trace 分发测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            event_calls.append(str((payload.get("message") or {}).get("text") or ""))
            await ctx.messages.send(channel="userbot_reply", text="event ok")
            return []

    _TracePlugin._manifest = Manifest(
        key="_test_trace_dispatch",
        display_name="Trace 分发测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "main",
            }
        ],
    )

    class _Event:
        raw_text = "hello trace"
        text = "hello trace"
        chat_id = -1001
        sender_id = 42
        id = 88
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={9: _FakeAcc(id=9)},
        humanize={9: None},
        afs=[_FakeAF(account_id=9, feature_key="_test_trace_dispatch", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_loader_trace")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    record_action = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=901))
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=9, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert event_calls == ["hello trace"]
        assert legacy_calls == []
        phases = [call.args[1] for call in record_span.await_args_list]
        assert "receive" in phases
        assert "subscription_match" in phases
        assert "plugin_invoke" in phases
        assert "plugin_return" in phases
        record_action.assert_awaited()
        assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"
        finish_trace.assert_awaited_once()
        assert finish_trace.await_args.args[:2] == (trace, loader_mod.TRACE_STATUS_OK)
    finally:
        loader_mod._STATES.pop(9, None)
        _REGISTRY.pop("_test_trace_dispatch", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_subscription_without_entry_key_invokes_on_event(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    event_calls: list[str] = []

    @register
    class _TraceNoEntryPlugin(Plugin):
        key = "_test_trace_no_entry_dispatch"
        display_name = "无入口事件分发测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            event_calls.append(str((payload.get("message") or {}).get("text") or ""))
            await ctx.messages.send(channel="userbot_reply", text="event ok")
            return []

    _TraceNoEntryPlugin._manifest = Manifest(
        key="_test_trace_no_entry_dispatch",
        display_name="无入口事件分发测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
            }
        ],
    )

    class _Event:
        raw_text = "hello no entry"
        text = "hello no entry"
        chat_id = -1001
        sender_id = 42
        id = 90
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={11: _FakeAcc(id=11)},
        humanize={11: None},
        afs=[_FakeAF(account_id=11, feature_key="_test_trace_no_entry_dispatch", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_loader_no_entry_trace")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    runtime_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", runtime_status)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=903))
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=11, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert event_calls == ["hello no entry"]
        assert not any(
            call.args[1] == "plugin_invoke"
            and call.args[2] == loader_mod.TRACE_STATUS_FAILED
            and call.kwargs.get("reason_code") == "entry_key_missing"
            for call in record_span.await_args_list
        )
        record_action.assert_awaited()
        assert any(
            call.kwargs.get("plugin_key") == "_test_trace_no_entry_dispatch"
            and call.kwargs.get("last_invocation_status") == loader_mod.TRACE_STATUS_OK
            for call in runtime_status.await_args_list
        )
    finally:
        loader_mod._STATES.pop(11, None)
        _REGISTRY.pop("_test_trace_no_entry_dispatch", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_missing_entry_key_keeps_legacy_on_message(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    legacy_calls: list[str] = []

    @register
    class _LegacyNoEntryPlugin(Plugin):
        key = "_test_legacy_no_entry_dispatch"
        display_name = "无入口 legacy 分发测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

    _LegacyNoEntryPlugin._manifest = Manifest(
        key="_test_legacy_no_entry_dispatch",
        display_name="无入口 legacy 分发测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
            }
        ],
    )

    class _Event:
        raw_text = "hello legacy no entry"
        text = "hello legacy no entry"
        chat_id = -1001
        sender_id = 42
        id = 91
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={12: _FakeAcc(id=12)},
        humanize={12: None},
        afs=[_FakeAF(account_id=12, feature_key="_test_legacy_no_entry_dispatch", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_loader_legacy_no_entry_trace")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    runtime_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", runtime_status)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=12, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert legacy_calls == ["hello legacy no entry"]
        assert any(
            call.args[1] == "plugin_invoke"
            and call.args[2] == loader_mod.TRACE_STATUS_SKIPPED
            and call.kwargs.get("reason_code") == "entry_key_missing"
            for call in record_span.await_args_list
        )
        assert not any(
            call.kwargs.get("plugin_key") == "_test_legacy_no_entry_dispatch"
            and call.kwargs.get("last_invocation_status") == loader_mod.TRACE_STATUS_FAILED
            for call in runtime_status.await_args_list
        )
        assert any(
            call.kwargs.get("plugin_key") == "_test_legacy_no_entry_dispatch"
            and call.kwargs.get("last_invocation_status") == loader_mod.TRACE_STATUS_OK
            for call in runtime_status.await_args_list
        )
    finally:
        loader_mod._STATES.pop(12, None)
        _REGISTRY.pop("_test_legacy_no_entry_dispatch", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_ctx_client_send_message_records_action(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    @register
    class _ClientTracePlugin(Plugin):
        key = "_test_client_trace_dispatch"
        display_name = "Client Trace 分发测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            await ctx.client.send_message(  # type: ignore[union-attr]
                chat_id=(payload.get("message") or {}).get("chat_id"),
                message="client ok",
            )
            return []

    _ClientTracePlugin._manifest = Manifest(
        key="_test_client_trace_dispatch",
        display_name="Client Trace 分发测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "main",
            }
        ],
    )

    class _Event:
        raw_text = "hello client trace"
        text = "hello client trace"
        chat_id = -1001
        sender_id = 42
        id = 89
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={10: _FakeAcc(id=10)},
        humanize={10: None},
        afs=[_FakeAF(account_id=10, feature_key="_test_client_trace_dispatch", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_loader_client_trace")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=902, chat_id=-1001))
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=10, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        client.send_message.assert_awaited_once_with(-1001, "client ok")
        record_action.assert_awaited_once()
        assert record_action.await_args.args[1]["type"] == "send_message"
        assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_OK
        assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"
    finally:
        loader_mod._STATES.pop(10, None)
        _REGISTRY.pop("_test_client_trace_dispatch", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_unmatched_subscription_keeps_legacy_on_message(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    legacy_calls: list[str] = []

    @register
    class _UnmatchedSubscriptionPlugin(Plugin):
        key = "_test_unmatched_subscription_legacy"
        display_name = "订阅未命中兼容测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

    _UnmatchedSubscriptionPlugin._manifest = Manifest(
        key="_test_unmatched_subscription_legacy",
        display_name="订阅未命中兼容测试",
        event_subscriptions=[
            {
                "source": ["interaction_bot"],
                "events": ["message"],
                "scope": "rule_bound",
                "entry_key": "main",
            }
        ],
    )

    class _Event:
        raw_text = "9"
        text = "9"
        chat_id = -1001
        sender_id = 42
        id = 92
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={13: _FakeAcc(id=13)},
        humanize={13: None},
        afs=[_FakeAF(account_id=13, feature_key="_test_unmatched_subscription_legacy", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_loader_unmatched_legacy")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=13, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert legacy_calls == ["9"]
        assert any(
            call.args[1] == "route"
            and call.kwargs.get("component") == "event_bus"
            and call.kwargs.get("reason_code") == "subscription_not_matched"
            for call in record_span.await_args_list
        )
    finally:
        loader_mod._STATES.pop(13, None)
        _REGISTRY.pop("_test_unmatched_subscription_legacy", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_deprecated_send_via_log_context_does_not_duplicate_plugin_key(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=14)
    redis = _FakeRedis()
    trace = "evt_deprecated_send_via"
    event = SimpleNamespace(chat_id=-1001, sender_id=42, raw_text="hello")
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    failed = await loader_mod._apply_userbot_event_bus_actions(
        state,
        trace,
        event,
        plugin_key="dice_grid_hunt",
        entry_key="start_dice_grid_hunt",
        actions=[{"type": "send_message", "send_via": "notice", "text": "旧通道"}],
        redis=redis,
    )

    assert failed is True
    assert redis.list_pushes
    payload = json.loads(redis.list_pushes[-1][1])
    assert payload["detail"]["trace_id"] == "evt_deprecated_send_via"
    assert payload["detail"]["plugin_key"] == "dice_grid_hunt"
    assert payload["detail"]["entry_key"] == "start_dice_grid_hunt"
    assert record_action.await_args.args[0]["trace_id"] == "evt_deprecated_send_via"


@pytest.mark.asyncio
async def test_userbot_event_bus_action_limit_records_dropped_actions(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=16)
    redis = _FakeRedis()
    trace = "evt_action_limit"
    event = SimpleNamespace(chat_id=-1001, sender_id=42, raw_text="hello")
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    actions = [{"type": "result"} for _ in range(10)] + [{"type": "send_message", "text": "too much"}]
    failed = await loader_mod._apply_userbot_event_bus_actions(
        state,
        trace,
        event,
        plugin_key="dice_grid_hunt",
        entry_key="start_dice_grid_hunt",
        actions=actions,
        redis=redis,
    )

    assert failed is False
    payload = json.loads(redis.list_pushes[-1][1])
    assert payload["detail"]["reason_code"] == "action_limit_exceeded"
    assert payload["detail"]["dropped_count"] == 1
    assert any(call.kwargs.get("error_code") == "action_limit_exceeded" for call in record_action.await_args_list)


@pytest.mark.asyncio
async def test_save_action_message_id_uses_account_namespace() -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=42)
    state.redis = redis

    await loader_mod._save_action_message_id(
        state,
        {"save_message_id_key": "game:notice:-100"},
        {"message_id": 99},
    )

    assert redis.sets == [("tp:msgid:42:game:notice:-100", "99", {"ex": 7200})]


@pytest.mark.asyncio
async def test_read_action_message_id_uses_account_namespace() -> None:
    redis = _FakeRedis()
    redis.values["tp:msgid:42:game:notice:-100"] = "99"
    state = loader_mod._AccountState(account_id=42)
    state.redis = redis

    assert await loader_mod._read_action_message_id(state, "game:notice:-100") == 99


@pytest.mark.asyncio
async def test_userbot_send_message_action_uses_rate_limit_and_preserves_parse_mode(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=43)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    state.client.send_message = AsyncMock(return_value=SimpleNamespace(id=501))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "<b>ok</b>",
            "parse_mode": "html",
            "context": {"trace_id": "evt_rate_send"},
        },
    )

    assert ok is True
    state.engine.acquire.assert_awaited_once_with(43, "send_message_group", peer_id=-100123)
    state.client.send_message.assert_awaited_once_with(
        -100123,
        "<b>ok</b>",
        reply_to=None,
        parse_mode="html",
    )
    assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_OK
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"


@pytest.mark.asyncio
async def test_userbot_session_send_message_humanize_runs_before_send_when_enabled(monkeypatch) -> None:
    order: list[str] = []

    async def fake_send_message(chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        order.append("send")
        return SimpleNamespace(id=502)

    async def fake_read(client, peer, opts):  # noqa: ANN001, ANN003
        order.append("read")
        assert peer == -100123
        assert opts.read_before_reply is True
        assert client is state.client

    async def fake_typing(client, peer, opts):  # noqa: ANN001, ANN003
        order.append("typing")
        assert peer == -100123
        assert opts.typing_simulate is True
        assert client is state.client

    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(side_effect=fake_send_message))
    state.engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    session = {
        "account_id": 44,
        "chat_id": -100123,
        "channel": "userbot",
        "module_key": "demo",
        "entry_key": "main",
        "data": {},
    }
    simulate_read = AsyncMock(side_effect=fake_read)
    simulate_typing = AsyncMock(side_effect=fake_typing)
    monkeypatch.setattr(loader_mod, "simulate_read", simulate_read)
    monkeypatch.setattr(loader_mod, "simulate_typing", simulate_typing)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "session humanized",
        },
        redis=state.redis,
        session_key="account_bot:interaction_session:44:demo:-100123",
        session=session,
    )

    assert ok is True
    assert order == ["read", "typing", "send"]
    simulate_read.assert_awaited_once()
    simulate_typing.assert_awaited_once()
    state.client.send_message.assert_awaited_once_with(
        -100123,
        "session humanized",
        reply_to=None,
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_userbot_session_send_message_humanize_skips_when_switches_disabled(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=503)))
    state.engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=False, typing_simulate=False),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(loader_mod, "simulate_read", simulate_read)
    monkeypatch.setattr(loader_mod, "simulate_typing", simulate_typing)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "session no humanize",
        },
        redis=state.redis,
        session_key="account_bot:interaction_session:44:demo:-100123",
        session={"channel": "userbot", "data": {}},
    )

    assert ok is True
    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    state.client.send_message.assert_awaited_once_with(
        -100123,
        "session no humanize",
        reply_to=None,
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_userbot_non_session_send_message_humanize_skips(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=504)))
    state.engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(loader_mod, "simulate_read", simulate_read)
    monkeypatch.setattr(loader_mod, "simulate_typing", simulate_typing)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "event bus direct action",
        },
        redis=state.redis,
    )

    assert ok is True
    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    state.client.send_message.assert_awaited_once_with(
        -100123,
        "event bus direct action",
        reply_to=None,
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_userbot_session_settlement_send_message_humanize_skips(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=505)))
    state.engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(loader_mod, "simulate_read", simulate_read)
    monkeypatch.setattr(loader_mod, "simulate_typing", simulate_typing)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "结算公告",
            "settlement": {"amount": 8, "winner_user_id": 111},
        },
        redis=state.redis,
        session_key="account_bot:interaction_session:44:demo:-100123",
        session={"channel": "userbot", "data": {}},
    )

    assert ok is True
    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    state.client.send_message.assert_awaited_once_with(
        -100123,
        "结算公告",
        reply_to=None,
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_userbot_payout_action_rejected_when_over_limit(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=71)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    state.client.send_message = AsyncMock(return_value=SimpleNamespace(id=1))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(
        loader_mod.payout_limit,
        "check_and_consume",
        AsyncMock(return_value=(False, "payout 单笔上限超限：本笔 500，单笔上限 100。")),
    )

    ok = await loader_mod._apply_userbot_payout_action(
        state,
        SimpleNamespace(chat_id=-100777),
        {"type": "payout", "amount": 500, "chat_id": -100777, "context": {"trace_id": "evt_payout_limit"}},
    )

    assert ok is False
    # 超限必须在发送前拦截：userbot 不应真的发出 "+金额"
    state.client.send_message.assert_not_awaited()
    assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["error_code"] == "payout_limit_exceeded"
    assert "单笔" in record_action.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_userbot_payout_action_skips_humanize_when_sent(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=71)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=510)))
    state.engine = SimpleNamespace(
        humanize=HumanizeOpts(read_before_reply=True, typing_simulate=True),
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(loader_mod, "simulate_read", simulate_read)
    monkeypatch.setattr(loader_mod, "simulate_typing", simulate_typing)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(True, None)))
    _mock_payout_delivery(monkeypatch)

    ok = await loader_mod._apply_userbot_payout_action(
        state,
        SimpleNamespace(chat_id=-100777),
        {"type": "payout", "amount": 8, "chat_id": -100777, "context": {"trace_id": "evt_payout_humanize_skip"}},
    )

    assert ok is True
    simulate_read.assert_not_awaited()
    simulate_typing.assert_not_awaited()
    state.client.send_message.assert_awaited_once_with(-100777, "+8", reply_to=None, parse_mode=None)


@pytest.mark.asyncio
async def test_userbot_payout_saves_reply_target_before_completing_ledger(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=71)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=511)))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    order: list[str] = []
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(True, None)))
    claim, complete, _release = _mock_payout_delivery(monkeypatch)
    complete.side_effect = lambda *args, **kwargs: order.append("complete") or True
    save_target = AsyncMock(side_effect=lambda *args, **kwargs: order.append("save"))
    monkeypatch.setattr(loader_mod, "_save_userbot_reply_target", save_target)

    ok = await loader_mod._apply_userbot_payout_action(
        state,
        SimpleNamespace(chat_id=-100777),
        {
            "type": "payout",
            "amount": 8,
            "chat_id": -100777,
            "reply_to_message_id": 41,
            "reply_to_user_id": 77,
            "reply_to_display_name": "公开名称",
        },
    )

    assert ok is True
    assert order == ["save", "complete"]
    claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_userbot_payout_completes_ledger_when_reply_target_save_fails(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=71)
    state.redis = _FakeRedis()
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=512)))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
    )
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(True, None)))
    claim, complete, _release = _mock_payout_delivery(monkeypatch)
    monkeypatch.setattr(
        loader_mod,
        "_save_userbot_reply_target",
        AsyncMock(side_effect=RuntimeError("reply target save failed")),
    )

    ok = await loader_mod._apply_userbot_payout_action(
        state,
        SimpleNamespace(chat_id=-100777),
        {
            "type": "payout",
            "amount": 8,
            "chat_id": -100777,
            "reply_to_message_id": 41,
            "reply_to_user_id": 77,
            "reply_to_display_name": "公开名称",
        },
    )

    assert ok is True
    claim.assert_awaited_once()
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_userbot_payout_action_floodwait_feeds_engine(monkeypatch) -> None:
    from telethon.errors import FloodWaitError

    flood = FloodWaitError(42)
    flood.seconds = 42  # telethon 构造不吃秒数，显式赋值

    state = loader_mod._AccountState(account_id=72)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    state.client.send_message = AsyncMock(side_effect=flood)
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok")),
        on_flood_wait=AsyncMock(),
        on_peer_flood=AsyncMock(),
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    # 放行 payout 限额，隔离 FloodWait 行为
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(True, None)))
    _mock_payout_delivery(monkeypatch)

    ok = await loader_mod._apply_userbot_payout_action(
        state,
        SimpleNamespace(chat_id=-100888),
        {"type": "payout", "amount": 5, "chat_id": -100888, "context": {"trace_id": "evt_payout_flood"}},
    )

    assert ok is False
    state.engine.on_flood_wait.assert_awaited_once()
    assert state.engine.on_flood_wait.await_args.args[0] == "send_message_group"
    state.engine.on_peer_flood.assert_not_awaited()
    # 失败 detail 带上 wait 秒数
    assert record_action.await_args.kwargs["result"].get("flood_wait_seconds") == 42


@pytest.mark.asyncio
async def test_scan_expired_session_skips_plugin_when_event_not_declared(monkeypatch) -> None:
    account_id = 73
    redis = _FakeRedis()
    key = f"{loader_mod._USERBOT_SESSION_KEY_PREFIX}{account_id}:sess-1"
    redis.values[key] = json.dumps(
        {
            "channel": "userbot",
            "expires_at": time.time() - 10,  # 已过期
            "module_key": "guess_number",
            "entry_key": "main",
            "chat_id": -100999,
        }
    )
    state = loader_mod._AccountState(account_id=account_id)
    state.redis = redis

    # 入口只声明 message，未声明 session_expired → 应跳过派发
    monkeypatch.setattr(
        loader_mod.account_bot_service,
        "declared_module_entry_events",
        lambda *_a: ["message"],
    )
    invoke = AsyncMock()
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    start_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "_start_userbot_session_trace", start_trace)
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)

    loader_mod._STATES[account_id] = state
    try:
        processed = await loader_mod.scan_userbot_expired_sessions_once(account_id)
    finally:
        loader_mod._STATES.pop(account_id, None)

    # 插件不被调用、也不开 trace，但会话 key 仍被清理
    invoke.assert_not_awaited()
    start_trace.assert_not_awaited()
    assert any(
        call.args[1:3] == ("subscription_match", loader_mod.TRACE_STATUS_SKIPPED)
        and call.kwargs.get("reason_code") == "event_type_not_subscribed"
        for call in record_span.await_args_list
    )
    assert key not in redis.values
    assert processed == 1


@pytest.mark.asyncio
async def test_userbot_edit_caption_action_uses_saved_key_and_rate_limit(monkeypatch) -> None:
    redis = _FakeRedis()
    redis.values["tp:msgid:43:dice_grid:round:1"] = "66"
    state = loader_mod._AccountState(account_id=43)
    state.redis = redis
    state.client = MagicMock()
    state.client.edit_message = AsyncMock(return_value=SimpleNamespace(id=66))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_edit_caption_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "edit_caption",
            "send_via": "userbot_reply",
            "message_id_key": "dice_grid:round:1",
            "caption": "<b>答对</b>",
            "parse_mode": "html",
            "context": {"trace_id": "evt_caption"},
        },
    )

    assert ok is True
    state.engine.acquire.assert_awaited_once_with(43, "edit_message", peer_id=-100123)
    state.client.edit_message.assert_awaited_once_with(
        -100123,
        66,
        "<b>答对</b>",
        parse_mode="html",
    )
    assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_OK
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"


@pytest.mark.asyncio
async def test_userbot_send_media_action_saves_message_id(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=43)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    state.client.send_file = AsyncMock(return_value=SimpleNamespace(id=701))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())

    ok = await loader_mod._apply_userbot_send_media_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_photo",
            "send_via": "userbot_reply",
            "photo_base64": "aW1n",
            "filename": "grid.png",
            "save_message_id_key": "dice_grid:round:1",
        },
    )

    assert ok is True
    assert state.redis.sets == [("tp:msgid:43:dice_grid:round:1", "701", {"ex": 7200})]


@pytest.mark.asyncio
async def test_userbot_send_file_action_uses_interaction_bot_document_api(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=43)
    state.redis = _FakeRedis()
    send_document = AsyncMock(return_value={"message_id": 702})
    monkeypatch.setattr(loader_mod.account_bot_service, "send_document_bytes", send_document)
    monkeypatch.setattr(loader_mod, "_interaction_bot_token_for_account", AsyncMock(return_value="123:bot"))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    reply_markup = {"inline_keyboard": [[{"text": "打开", "url": "https://example.com"}]]}

    ok = await loader_mod._apply_userbot_send_media_action(
        state,
        SimpleNamespace(chat_id=-100123),
        {
            "type": "send_file",
            "send_via": "interaction_bot",
            "file_base64": "ZG9j",
            "filename": "round.txt",
            "caption": "文件题面",
            "reply_markup": reply_markup,
            "save_message_id_key": "file:round:1",
        },
    )

    assert ok is True
    send_document.assert_awaited_once_with(
        "123:bot",
        -100123,
        b"doc",
        filename="round.txt",
        caption="文件题面",
        reply_to_message_id=None,
        reply_markup=reply_markup,
        parse_mode="plain",
    )
    assert state.redis.sets == [("tp:msgid:43:file:round:1", "702", {"ex": 7200})]


@pytest.mark.asyncio
async def test_userbot_payout_action_uses_userbot_rate_limit_and_default_text(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = MagicMock()
    state.client.send_message = AsyncMock(return_value=SimpleNamespace(id=777))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    check_and_consume = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", check_and_consume)
    _claim, complete_delivery, _release = _mock_payout_delivery(monkeypatch)

    failed = await loader_mod._apply_userbot_event_bus_actions(
        state,
        "evt_payout_loader",
        SimpleNamespace(chat_id=-100456),
        plugin_key="game",
        entry_key="main",
        actions=[
            {
                "type": "payout",
                "amount": 12,
                "reply_to_message_id": 34,
                "parse_mode": "plain",
            }
        ],
        redis=state.redis,
    )

    assert failed is False
    state.engine.acquire.assert_awaited_once_with(44, "send_message_group", peer_id=-100456)
    check_and_consume.assert_awaited_once()
    payout_key = check_and_consume.await_args.kwargs["idempotency_key"]
    assert payout_key.startswith("pay_")
    state.client.send_message.assert_awaited_once_with(
        -100456,
        "+12",
        reply_to=34,
        parse_mode=None,
    )
    complete_delivery.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == "payout"
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"
    assert record_action.await_args.kwargs["result"]["message_id"] == 777


@pytest.mark.asyncio
async def test_userbot_payout_action_resolves_reply_to_user_recent_message(monkeypatch) -> None:
    class _Client:
        def __init__(self) -> None:
            self.send_message = AsyncMock(return_value=SimpleNamespace(id=778))

        def iter_messages(self, chat_id, **kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if chat_id == -100456 and kwargs.get("from_user") == 12345:
                    yield SimpleNamespace(id=66, sender_id=12345)

            return _gen()

    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = _Client()
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(loader_mod.payout_limit, "check_and_consume", AsyncMock(return_value=(True, None)))
    _mock_payout_delivery(monkeypatch)

    failed = await loader_mod._apply_userbot_event_bus_actions(
        state,
        "evt_payout_loader_reply_to_user",
        SimpleNamespace(chat_id=-100456),
        plugin_key="game",
        entry_key="main",
        actions=[
            {
                "type": "payout",
                "amount": 12,
                "reply_to_user_id": 12345,
                "reply_to_search_limit": 20,
            }
        ],
        redis=state.redis,
    )

    assert failed is False
    state.client.send_message.assert_awaited_once_with(
        -100456,
        "+12",
        reply_to=66,
        parse_mode=None,
    )
    assert record_action.await_args.kwargs["result"]["reply_to_message_id"] == 66
    assert record_action.await_args.kwargs["result"]["reply_to_user_id"] == 12345


@pytest.mark.asyncio
async def test_userbot_payout_action_sends_notice_when_reply_anchor_missing(monkeypatch) -> None:
    class _Client:
        def __init__(self) -> None:
            self.send_message = AsyncMock(return_value=SimpleNamespace(id=780))

        def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if False:
                    yield None

            return _gen()

    state = loader_mod._AccountState(account_id=44)
    state.redis = _FakeRedis()
    state.client = _Client()
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    failed = await loader_mod._apply_userbot_event_bus_actions(
        state,
        "evt_payout_loader_reply_anchor_missing",
        SimpleNamespace(chat_id=-100456),
        plugin_key="game",
        entry_key="main",
        actions=[
            {
                "type": "payout",
                "amount": 12,
                "parse_mode": "html",
                "reply_to_user_id": 12345,
                "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言。<code>/airp list</code>",
            }
        ],
        redis=state.redis,
    )

    assert failed is True
    state.client.send_message.assert_awaited_once_with(
        -100456,
        "没有找到 12345 的近期发言。<code>/airp list</code>",
        reply_to=None,
        parse_mode="html",
    )
    assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["error_code"] == "reply_anchor_missing"
    assert record_action.await_args.kwargs["result"]["chat_id"] == -100456
    assert record_action.await_args.kwargs["result"]["amount"] == 12
    assert record_action.await_args.kwargs["result"]["reply_to_user_id"] == 12345
    assert record_action.await_args.kwargs["result"]["reply_to_search_limit"] == 5000
    assert record_action.await_args.kwargs["result"]["reply_anchor_missing"] is True
    log_payload = json.loads(state.redis.list_pushes[-1][1])
    assert log_payload["message"] == "userbot payout action failed"
    assert log_payload["detail"]["chat_id"] == -100456
    assert log_payload["detail"]["amount"] == 12
    assert log_payload["detail"]["reply_to_user_id"] == 12345
    assert log_payload["detail"]["reply_to_search_limit"] == 5000
    assert log_payload["detail"]["error_code"] == "reply_anchor_missing"
    assert log_payload["detail"]["reply_anchor_missing"] is True


@pytest.mark.asyncio
async def test_userbot_send_message_action_resolves_reply_to_user_recent_message(monkeypatch) -> None:
    class _Client:
        def __init__(self) -> None:
            self.send_message = AsyncMock(return_value=SimpleNamespace(id=779))

        def iter_messages(self, chat_id, **kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if chat_id == -100789 and kwargs.get("from_user") == 222:
                    yield SimpleNamespace(id=70, sender_id=222)

            return _gen()

    state = loader_mod._AccountState(account_id=45)
    state.redis = _FakeRedis()
    state.client = _Client()
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100789),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "+88",
            "reply_to_user_id": 222,
            "reply_to_search_limit": 20,
            "context": {"trace_id": "evt_send_reply_to_user"},
        },
    )

    assert ok is True
    state.client.send_message.assert_awaited_once_with(
        -100789,
        "+88",
        reply_to=70,
        parse_mode=None,
    )
    assert record_action.await_args.kwargs["result"]["reply_to_message_id"] == 70
    assert record_action.await_args.kwargs["result"]["reply_to_user_id"] == 222


@pytest.mark.asyncio
async def test_userbot_send_message_action_records_reply_anchor_failure_details(monkeypatch) -> None:
    class _Client:
        def __init__(self) -> None:
            self.send_message = AsyncMock(return_value=SimpleNamespace(id=781))

        def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if False:
                    yield None

            return _gen()

    state = loader_mod._AccountState(account_id=46)
    state.redis = _FakeRedis()
    state.client = _Client()
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100789),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "+88",
            "reply_to_user_id": 222,
            "reply_to_search_limit": 20,
            "reply_anchor_missing_text": "没有找到 {user_id} 的近期发言，无法发奖。",
            "context": {"trace_id": "evt_send_reply_anchor_missing"},
        },
    )

    assert ok is False
    state.client.send_message.assert_awaited_once_with(
        -100789,
        "没有找到 222 的近期发言，无法发奖。",
        reply_to=None,
        parse_mode=None,
    )
    assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["error_code"] == "reply_anchor_missing"
    result = record_action.await_args.kwargs["result"]
    assert result["chat_id"] == -100789
    assert result["amount"] is None
    assert result["reply_to_message_id"] is None
    assert result["reply_to_user_id"] == 222
    assert result["reply_to_search_limit"] == 20
    assert result["worker_offline"] is False
    assert result["reply_anchor_missing"] is True
    log_payload = json.loads(state.redis.list_pushes[-1][1])
    assert log_payload["message"] == "userbot send_message action failed"
    assert log_payload["detail"]["chat_id"] == -100789
    assert log_payload["detail"]["reply_to_user_id"] == 222
    assert log_payload["detail"]["reply_to_search_limit"] == 20
    assert log_payload["detail"]["error_code"] == "reply_anchor_missing"
    assert log_payload["detail"]["reply_anchor_missing"] is True


@pytest.mark.asyncio
async def test_userbot_send_message_action_suppresses_reply_anchor_missing_notice(monkeypatch) -> None:
    class _Client:
        def __init__(self) -> None:
            self.send_message = AsyncMock(return_value=SimpleNamespace(id=782))

        def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
            async def _gen():
                if False:
                    yield None

            return _gen()

    state = loader_mod._AccountState(account_id=47)
    state.redis = _FakeRedis()
    state.client = _Client()
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100789),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "text": "-100",
            "reply_to_user_id": 222,
            "reply_to_search_limit": 20,
            "reply_anchor_missing_text": "无法扣款，加入失败。",
            "suppress_reply_anchor_missing_notice": True,
            "context": {"trace_id": "evt_send_reply_anchor_missing_suppressed"},
        },
    )

    assert ok is False
    state.client.send_message.assert_not_awaited()
    assert record_action.await_args.args[2] == loader_mod.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["error_code"] == "reply_anchor_missing"
    result = record_action.await_args.kwargs["result"]
    assert result["reply_to_user_id"] == 222
    assert result["reply_anchor_missing"] is True


@pytest.mark.asyncio
async def test_invoke_interaction_entry_ctx_log_does_not_duplicate_plugin_key() -> None:
    class _InteractionLogPlugin(Plugin):
        key = "_test_interaction_log"
        display_name = "交互入口日志测试"

        async def on_interaction(self, ctx: PluginContext, entry_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
            assert entry_key == "start"
            assert payload["trace_id"] == "evt_interaction_log"
            assert isinstance(payload["tp_event"], TelePilotEvent)
            assert "tp_event" not in payload["tp_event"].raw
            if ctx.log is not None:
                await ctx.log("info", "interaction log ok")
            return [{"type": "send_message", "text": "ok"}]

    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=15)
    state.instances["_test_interaction_log"] = _InteractionLogPlugin()
    state.contexts["_test_interaction_log"] = PluginContext(
        account_id=15,
        feature_key="_test_interaction_log",
        log=loader_mod._make_logger(redis, 15, "_test_interaction_log"),
    )
    loader_mod._STATES[15] = state
    try:
        actions = await loader_mod.invoke_interaction_entry(
            15,
            plugin_key="_test_interaction_log",
            entry_key="start",
            payload={"trace_id": "evt_interaction_log"},
        )

        assert actions == [{"type": "send_message", "text": "ok", "send_via": "interaction_bot"}]
        assert redis.list_pushes
        payload = json.loads(redis.list_pushes[-1][1])
        assert payload["message"] == "interaction log ok"
        assert payload["detail"]["trace_id"] == "evt_interaction_log"
        assert payload["detail"]["plugin_key"] == "_test_interaction_log"
        assert payload["detail"]["entry_key"] == "start"
    finally:
        loader_mod._STATES.pop(15, None)


@pytest.mark.asyncio
async def test_invoke_interaction_entry_inherits_default_send_via_for_plain_message_ops() -> None:
    class _InteractionDefaultChannelPlugin(Plugin):
        key = "_test_interaction_default_channel"
        display_name = "交互入口默认通道测试"

        async def on_interaction(self, ctx: PluginContext, entry_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
            assert entry_key == "start"
            assert payload["session"]["channel"] == "userbot"
            await ctx.messages.send(text="buffered")
            return [
                {"type": "send_message", "text": "returned"},
                {"type": "send_message", "text": "explicit", "send_via": "interaction_bot"},
            ]

    state = loader_mod._AccountState(account_id=151)
    state.instances["_test_interaction_default_channel"] = _InteractionDefaultChannelPlugin()
    state.contexts["_test_interaction_default_channel"] = PluginContext(
        account_id=151,
        feature_key="_test_interaction_default_channel",
        client=MagicMock(),
    )
    loader_mod._STATES[151] = state
    try:
        actions = await loader_mod.invoke_interaction_entry(
            151,
            plugin_key="_test_interaction_default_channel",
            entry_key="start",
            payload={"session": {"channel": "userbot"}},
            default_send_via=["userbot_reply"],
        )

        assert actions == [
            {
                "type": "send_message",
                "chat_id": None,
                "text": "buffered",
                "parse_mode": "plain",
                "reply_to_message_id": None,
                "send_via": "userbot_reply",
            },
            {"type": "send_message", "text": "returned", "send_via": "userbot_reply"},
            {"type": "send_message", "text": "explicit", "send_via": "interaction_bot"},
        ]
    finally:
        loader_mod._STATES.pop(151, None)


@pytest.mark.asyncio
async def test_interaction_entry_messages_apply_executes_with_logical_default_channel(monkeypatch) -> None:
    class _InteractionBackgroundActionsPlugin(Plugin):
        key = "_test_interaction_background_actions"
        display_name = "交互入口后台动作测试"

        async def on_interaction(self, ctx: PluginContext, entry_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
            await ctx.messages.apply(
                [{"type": "send_message", "chat_id": -100123, "text": "后台刷新"}],
                entry_key=entry_key,
            )
            await ctx.messages.send(chat_id=-100123, text="当前回复")
            return []

    state = loader_mod._AccountState(account_id=152)
    state.redis = _FakeRedis()
    state.instances["_test_interaction_background_actions"] = _InteractionBackgroundActionsPlugin()
    state.contexts["_test_interaction_background_actions"] = PluginContext(
        account_id=152,
        feature_key="_test_interaction_background_actions",
        client=MagicMock(),
    )
    apply_actions = AsyncMock(return_value=False)
    monkeypatch.setattr(loader_mod, "_apply_userbot_event_bus_actions", apply_actions)
    loader_mod._STATES[152] = state
    try:
        actions = await loader_mod.invoke_interaction_entry(
            152,
            plugin_key="_test_interaction_background_actions",
            entry_key="start",
            payload={"trace_id": "evt_background_actions"},
            default_send_via=["interaction_bot"],
        )

        assert actions == [
            {
                "type": "send_message",
                "chat_id": -100123,
                "text": "当前回复",
                "parse_mode": "plain",
                "reply_to_message_id": None,
                "send_via": "interaction_bot",
            }
        ]
        apply_actions.assert_awaited_once()
        applied = apply_actions.await_args.kwargs["actions"]
        assert applied[0]["text"] == "后台刷新"
        assert applied[0]["send_via"] == "interaction_bot"
        assert applied[0]["context"] == {
            "trace_id": "evt_background_actions",
            "plugin_key": "_test_interaction_background_actions",
            "entry_key": "start",
        }
    finally:
        loader_mod._STATES.pop(152, None)


@pytest.mark.asyncio
async def test_invoke_interaction_entry_uses_call_scoped_contexts() -> None:
    seen: list[tuple[str, bool, bool]] = []
    ready: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    class _ScopedInteractionPlugin(Plugin):
        key = "_test_interaction_scoped_ctx"
        display_name = "交互入口上下文隔离测试"

        async def on_interaction(self, ctx: PluginContext, entry_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
            seen.append((entry_key, ctx is base_ctx, ctx.messages is base_ctx.messages))
            await ready.put(None)
            await release.wait()
            await ctx.messages.send(text=f"buffered {entry_key}")
            return [{"type": "send_message", "text": f"returned {entry_key}"}]

    state = loader_mod._AccountState(account_id=16)
    base_ctx = PluginContext(
        account_id=16,
        feature_key="_test_interaction_scoped_ctx",
        client=MagicMock(),
        messages=SimpleNamespace(kind="base_messages"),
    )
    state.instances["_test_interaction_scoped_ctx"] = _ScopedInteractionPlugin()
    state.contexts["_test_interaction_scoped_ctx"] = base_ctx
    loader_mod._STATES[16] = state

    try:
        first = asyncio.create_task(
            loader_mod.invoke_interaction_entry(
                16,
                plugin_key="_test_interaction_scoped_ctx",
                entry_key="first",
                payload={"trace_id": "evt_first"},
            )
        )
        second = asyncio.create_task(
            loader_mod.invoke_interaction_entry(
                16,
                plugin_key="_test_interaction_scoped_ctx",
                entry_key="second",
                payload={"trace_id": "evt_second"},
            )
        )
        await ready.get()
        await ready.get()
        release.set()

        first_actions, second_actions = await asyncio.gather(first, second)

        assert seen == [("first", False, False), ("second", False, False)]
        assert [item["text"] for item in first_actions] == ["buffered first", "returned first"]
        assert [item["text"] for item in second_actions] == ["buffered second", "returned second"]
        assert state.contexts["_test_interaction_scoped_ctx"] is base_ctx
        assert base_ctx.messages == SimpleNamespace(kind="base_messages")
    finally:
        loader_mod._STATES.pop(16, None)


@pytest.mark.asyncio
async def test_userbot_event_bus_entry_uses_call_scoped_contexts() -> None:
    seen: list[tuple[str, bool, bool]] = []
    ready: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    class _ScopedEventBusPlugin(Plugin):
        key = "_test_event_bus_scoped_ctx"
        display_name = "Event Bus 上下文隔离测试"

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            label = str(payload["label"])
            assert isinstance(payload["tp_event"], TelePilotEvent)
            assert "tp_event" not in payload["tp_event"].raw
            seen.append((label, ctx is base_ctx, ctx.messages is base_ctx.messages))
            await ready.put(None)
            await release.wait()
            await ctx.messages.send(text=f"buffered {label}")
            return [{"type": "send_message", "text": f"returned {label}"}]

    inst = _ScopedEventBusPlugin()
    base_ctx = PluginContext(
        account_id=17,
        feature_key="_test_event_bus_scoped_ctx",
        client=MagicMock(),
        messages=SimpleNamespace(kind="base_messages"),
    )

    first = asyncio.create_task(
        loader_mod._invoke_userbot_event_bus_entry(
            inst,
            base_ctx,
            plugin_key="_test_event_bus_scoped_ctx",
            entry_key="main",
            payload={"trace_id": "evt_bus_first", "label": "first"},
        )
    )
    second = asyncio.create_task(
        loader_mod._invoke_userbot_event_bus_entry(
            inst,
            base_ctx,
            plugin_key="_test_event_bus_scoped_ctx",
            entry_key="main",
            payload={"trace_id": "evt_bus_second", "label": "second"},
        )
    )
    await ready.get()
    await ready.get()
    release.set()

    first_actions, second_actions = await asyncio.gather(first, second)

    assert seen == [("first", False, False), ("second", False, False)]
    assert [item["text"] for item in first_actions] == ["buffered first", "returned first"]
    assert [item["text"] for item in second_actions] == ["buffered second", "returned second"]
    assert [item["send_via"] for item in first_actions] == ["userbot_reply", "userbot_reply"]
    assert [item["send_via"] for item in second_actions] == ["userbot_reply", "userbot_reply"]
    assert base_ctx.messages == SimpleNamespace(kind="base_messages")


@pytest.mark.asyncio
async def test_legacy_dispatcher_uses_call_scoped_context(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    seen: list[tuple[bool, bool, bool]] = []

    @register
    class _LegacyScopedPlugin(Plugin):
        key = "_test_legacy_scoped_ctx"
        display_name = "legacy 上下文隔离测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            base = loader_mod._STATES[18].contexts[self.key]
            seen.append((ctx is base, ctx.client is base.client, ctx.messages is base.messages))

    class _Event:
        raw_text = "hello scoped legacy"
        text = "hello scoped legacy"
        chat_id = -1001
        sender_id = 42
        id = 93
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={18: _FakeAcc(id=18)},
        humanize={18: None},
        afs=[_FakeAF(account_id=18, feature_key="_test_legacy_scoped_ctx", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_legacy_scoped")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client._is_sandboxed = False
    client.is_sandbox_client = False
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=18, paused=paused, redis=_FakeRedis())
        await captured[-1](_Event())

        assert seen == [(False, False, False)]
        assert loader_mod._STATES[18].contexts["_test_legacy_scoped_ctx"].client is client
    finally:
        loader_mod._STATES.pop(18, None)
        _REGISTRY.pop("_test_legacy_scoped_ctx", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_trace_switch_disables_trace(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    event_calls: list[str] = []
    legacy_calls: list[str] = []

    @register
    class _TraceOffPlugin(Plugin):
        key = "_test_trace_off_dispatch"
        display_name = "Trace 关闭测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            event_calls.append(str((payload.get("message") or {}).get("text") or ""))
            return []

    _TraceOffPlugin._manifest = Manifest(
        key="_test_trace_off_dispatch",
        display_name="Trace 关闭测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "main",
            }
        ],
    )

    class _Event:
        raw_text = "hello legacy"
        text = "hello legacy"
        chat_id = -1001
        sender_id = 42
        id = 90
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={11: _FakeAcc(id=11)},
        humanize={11: None},
        afs=[_FakeAF(account_id=11, feature_key="_test_trace_off_dispatch", enabled=True, config={})],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=11, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert event_calls == ["hello legacy"]
        assert legacy_calls == []
        loader_mod.start_trace.assert_not_awaited()
        assert all(call.args[0] is None for call in loader_mod.record_span.await_args_list)
        loader_mod.finish_trace.assert_awaited_once()
        assert loader_mod.finish_trace.await_args.args[0] is None
    finally:
        loader_mod._STATES.pop(11, None)
        _REGISTRY.pop("_test_trace_off_dispatch", None)


@pytest.mark.asyncio
async def test_userbot_event_bus_delivery_switch_records_disabled_and_uses_legacy(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    event_calls: list[str] = []
    legacy_calls: list[str] = []

    @register
    class _DeliveryOffPlugin(Plugin):
        key = "_test_delivery_off_dispatch"
        display_name = "Event Bus 关闭测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            event_calls.append(str((payload.get("message") or {}).get("text") or ""))
            return []

    _DeliveryOffPlugin._manifest = Manifest(
        key="_test_delivery_off_dispatch",
        display_name="Event Bus 关闭测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
                "entry_key": "main",
            }
        ],
    )

    class _Event:
        raw_text = "hello fallback"
        text = "hello fallback"
        chat_id = -1001
        sender_id = 42
        id = 91
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={12: _FakeAcc(id=12)},
        humanize={12: None},
        afs=[_FakeAF(account_id=12, feature_key="_test_delivery_off_dispatch", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_loader_delivery_disabled")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": False,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=12, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert event_calls == []
        assert legacy_calls == ["hello fallback"]
        assert any(
            call.kwargs.get("reason_code") == "event_bus_delivery_disabled"
            for call in record_span.await_args_list
        )
        loader_mod.finish_trace.assert_awaited_once()
    finally:
        loader_mod._STATES.pop(12, None)
        _REGISTRY.pop("_test_delivery_off_dispatch", None)


@pytest.mark.asyncio
async def test_direct_passthrough_requires_account_config_opt_in(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    direct_calls: list[str] = []
    legacy_calls: list[str] = []

    @register
    class _DirectNeedsConfigPlugin(Plugin):
        key = "_test_direct_needs_config"
        display_name = "直通二次确认测试"
        owner_only = False

        async def on_direct_message(self, ctx: PluginContext, event: Any) -> None:
            direct_calls.append(str(getattr(event, "raw_text", "")))

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

    _DirectNeedsConfigPlugin._manifest = Manifest(
        key="_test_direct_needs_config",
        display_name="直通二次确认测试",
        capabilities={
            "telegram_direct_passthrough": {
                "enabled": True,
                "reason": "测试二次确认开关",
                "sources": ["userbot"],
                "directions": ["incoming"],
            }
        },
    )

    class _Event:
        raw_text = "hello direct disabled"
        text = "hello direct disabled"
        chat_id = -1001
        sender_id = 42
        id = 188
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={13: _FakeAcc(id=13)},
        humanize={13: None},
        afs=[_FakeAF(account_id=13, feature_key="_test_direct_needs_config", enabled=True, config={})],
        rules=[],
        features={
            "_test_direct_needs_config": _FakeFeature(
                key="_test_direct_needs_config",
                manifest={
                    "config_schema": {
                        "type": "object",
                        "properties": {
                            "direct_passthrough": {
                                "type": "object",
                                "default": {"enabled": True},
                            }
                        },
                    }
                },
            )
        },
        plugin_global_configs={
            "_test_direct_needs_config": _FakePluginGlobalConfig(
                plugin_key="_test_direct_needs_config",
                config={"direct_passthrough": {"enabled": True}},
            )
        },
    )
    trace = SimpleNamespace(trace_id="evt_direct_disabled_trace")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    start_trace = AsyncMock(return_value=trace)
    monkeypatch.setattr(loader_mod, "start_trace", start_trace)
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=13, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert direct_calls == []
        assert legacy_calls == ["hello direct disabled"]
        start_trace.assert_awaited_once()
    finally:
        loader_mod._STATES.pop(13, None)
        _REGISTRY.pop("_test_direct_needs_config", None)


@pytest.mark.asyncio
async def test_direct_passthrough_consumes_raw_event_before_event_bus(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    direct_events: list[Any] = []
    legacy_calls: list[str] = []

    @register
    class _DirectEnabledPlugin(Plugin):
        key = "_test_direct_enabled"
        display_name = "直通启用测试"
        owner_only = False

        async def on_direct_message(self, ctx: PluginContext, event: Any) -> None:
            direct_events.append(event)
            await ctx.client.send_message(event.chat_id, "direct reply")

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            legacy_calls.append("event_bus")
            return []

    _DirectEnabledPlugin._manifest = Manifest(
        key="_test_direct_enabled",
        display_name="直通启用测试",
        event_subscriptions=[
            {
                "source": ["userbot"],
                "events": ["message"],
                "scope": "all_allowed_chats",
            }
        ],
        capabilities={
            "telegram_direct_passthrough": {
                "enabled": True,
                "reason": "测试低延时直通",
                "sources": ["userbot"],
                "directions": ["incoming"],
            }
        },
    )

    class _Event:
        raw_text = "hello direct enabled"
        text = "hello direct enabled"
        chat_id = -1001
        sender_id = 42
        id = 189
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={14: _FakeAcc(id=14)},
        humanize={14: None},
        afs=[
            _FakeAF(
                account_id=14,
                feature_key="_test_direct_enabled",
                enabled=True,
                config={"direct_passthrough": {"enabled": True}},
            )
        ],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    trace = SimpleNamespace(trace_id="evt_direct_enabled_trace")
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    start_trace = AsyncMock(return_value=trace)
    monkeypatch.setattr(loader_mod, "start_trace", start_trace)
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)
    runtime_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", runtime_status)
    simulate_read = AsyncMock()
    simulate_typing = AsyncMock()
    monkeypatch.setattr(loader_mod, "simulate_read", simulate_read)
    monkeypatch.setattr(loader_mod, "simulate_typing", simulate_typing)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=991))
    paused = asyncio.Event()
    paused.set()
    event = _Event()

    try:
        await load_plugins_for_account(client, account_id=14, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(event)

        assert direct_events == [event]
        assert legacy_calls == []
        client.send_message.assert_awaited_once_with(-1001, "direct reply")
        simulate_read.assert_not_awaited()
        simulate_typing.assert_not_awaited()
        start_trace.assert_awaited_once()
        phases = [call.args[1] for call in record_span.await_args_list]
        assert "receive" in phases
        assert "route" in phases
        assert "plugin_invoke" in phases
        finish_trace.assert_awaited_once()
        assert finish_trace.await_args.args[:2] == (trace, loader_mod.TRACE_STATUS_OK)
        assert finish_trace.await_args.kwargs["consumed"] is True
        assert any(
            call.kwargs.get("plugin_key") == "_test_direct_enabled"
            and call.kwargs.get("last_invocation_status") == loader_mod.TRACE_STATUS_OK
            and call.kwargs.get("last_trace_id") == "evt_direct_enabled_trace"
            for call in runtime_status.await_args_list
        )
    finally:
        loader_mod._STATES.pop(14, None)
        _REGISTRY.pop("_test_direct_enabled", None)


@pytest.mark.asyncio
async def test_direct_passthrough_broadcasts_before_incoming_guards(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    calls: list[tuple[str, str]] = []

    @register
    class _DirectBroadcastA(Plugin):
        key = "_test_direct_broadcast_a"
        display_name = "直通广播 A"
        owner_only = False

        async def on_direct_message(self, ctx: PluginContext, event: Any) -> None:
            calls.append((self.key, str(getattr(event, "raw_text", ""))))

    @register
    class _DirectBroadcastB(Plugin):
        key = "_test_direct_broadcast_b"
        display_name = "直通广播 B"
        owner_only = False

        async def on_direct_message(self, ctx: PluginContext, event: Any) -> None:
            calls.append((self.key, str(getattr(event, "raw_text", ""))))

    for cls in (_DirectBroadcastA, _DirectBroadcastB):
        cls._manifest = Manifest(
            key=cls.key,
            display_name=cls.display_name,
            capabilities={
                "telegram_direct_passthrough": {
                    "enabled": True,
                    "reason": "测试直通广播顺序",
                    "sources": ["userbot"],
                    "directions": ["incoming"],
                }
            },
        )

    class _Event:
        raw_text = "keyword owned by interaction bot"
        text = "keyword owned by interaction bot"
        chat_id = -2002
        sender_id = 42
        id = 190
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={15: _FakeAcc(id=15)},
        humanize={15: None},
        afs=[
            _FakeAF(
                account_id=15,
                feature_key="_test_direct_broadcast_a",
                enabled=True,
                config={"direct_passthrough": {"enabled": True}},
            ),
            _FakeAF(
                account_id=15,
                feature_key="_test_direct_broadcast_b",
                enabled=True,
                config={"direct_passthrough": {"enabled": True}},
            ),
        ],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_record_recent_peer", AsyncMock())
    interaction_owned = AsyncMock(return_value=True)
    monkeypatch.setattr(loader_mod, "_interaction_bot_owns_incoming_text", interaction_owned)
    monkeypatch.setattr(loader_mod, "_record_interaction_text_guard_skip", AsyncMock())
    trace = SimpleNamespace(trace_id="evt_direct_broadcast_trace")
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=15, paused=paused, redis=_FakeRedis())
        state = loader_mod._STATES[15]
        state.ignored_peers = {-1001}
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert calls == [
            ("_test_direct_broadcast_a", "keyword owned by interaction bot"),
            ("_test_direct_broadcast_b", "keyword owned by interaction bot"),
        ]
        loader_mod._record_recent_peer.assert_not_awaited()
        interaction_owned.assert_not_awaited()
        loader_mod.start_trace.assert_awaited_once()
        assert sum(1 for call in record_span.await_args_list if call.args[1] == "route") == 2
        assert sum(1 for call in record_span.await_args_list if call.args[1] == "plugin_invoke") == 2
        loader_mod.finish_trace.assert_awaited_once()
        assert loader_mod.finish_trace.await_args.args[:2] == (trace, loader_mod.TRACE_STATUS_OK)
        assert loader_mod.finish_trace.await_args.kwargs["invoked_count"] == 2
    finally:
        loader_mod._STATES.pop(15, None)
        _REGISTRY.pop("_test_direct_broadcast_a", None)
        _REGISTRY.pop("_test_direct_broadcast_b", None)


@pytest.mark.asyncio
async def test_direct_passthrough_records_failed_trace(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    @register
    class _DirectFailingPlugin(Plugin):
        key = "_test_direct_failing"
        display_name = "直通失败 trace 测试"
        owner_only = False

        async def on_direct_message(self, ctx: PluginContext, event: Any) -> None:
            raise RuntimeError("boom")

    _DirectFailingPlugin._manifest = Manifest(
        key="_test_direct_failing",
        display_name="直通失败 trace 测试",
        capabilities={
            "telegram_direct_passthrough": {
                "enabled": True,
                "reason": "测试直通异常 trace",
                "sources": ["userbot"],
                "directions": ["incoming"],
            }
        },
    )

    class _Event:
        raw_text = "hello direct failure"
        text = "hello direct failure"
        chat_id = -3003
        sender_id = 42
        id = 191
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={16: _FakeAcc(id=16)},
        humanize={16: None},
        afs=[
            _FakeAF(
                account_id=16,
                feature_key="_test_direct_failing",
                enabled=True,
                config={"direct_passthrough": {"enabled": True}},
            )
        ],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_direct_failed_trace")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)
    runtime_status = AsyncMock()
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", runtime_status)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=16, paused=paused, redis=_FakeRedis())
        incoming_dispatch = captured[-1]
        await incoming_dispatch(_Event())

        assert any(
            call.args[1] == "plugin_invoke"
            and call.args[2] == loader_mod.TRACE_STATUS_FAILED
            and call.kwargs.get("reason_code") == "plugin_runtime_error"
            for call in record_span.await_args_list
        )
        finish_trace.assert_awaited_once()
        assert finish_trace.await_args.args[:2] == (trace, loader_mod.TRACE_STATUS_FAILED)
        assert finish_trace.await_args.kwargs["consumed"] is True
        assert any(
            call.kwargs.get("plugin_key") == "_test_direct_failing"
            and call.kwargs.get("last_invocation_status") == loader_mod.TRACE_STATUS_FAILED
            and call.kwargs.get("last_trace_id") == "evt_direct_failed_trace"
            for call in runtime_status.await_args_list
        )
    finally:
        loader_mod._STATES.pop(16, None)
        _REGISTRY.pop("_test_direct_failing", None)


def test_userbot_native_raw_boolean_true_is_not_explicit_capability() -> None:
    class _Plugin(Plugin):
        key = "_test_native_raw_bool"

    _Plugin._manifest = Manifest(
        key="_test_native_raw_bool",
        display_name="Native Raw Bool",
        capabilities={"telegram_native_raw": True},
    )

    assert loader_mod._plugin_declares_native_raw(_Plugin(), source="userbot") is False


def test_userbot_native_raw_requires_enabled_object_and_source() -> None:
    class _Plugin(Plugin):
        key = "_test_native_raw_object"

    _Plugin._manifest = Manifest(
        key="_test_native_raw_object",
        display_name="Native Raw Object",
        capabilities={"telegram_native_raw": {"enabled": True, "sources": ["interaction_bot"]}},
    )

    assert loader_mod._plugin_declares_native_raw(_Plugin(), source="userbot") is False
    assert loader_mod._plugin_declares_native_raw(_Plugin(), source="interaction_bot") is True


@pytest.mark.asyncio
async def test_plugin_command_ctx_client_send_message_records_action(monkeypatch) -> None:
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    raw_client = MagicMock()
    raw_client.send_message = AsyncMock(return_value=SimpleNamespace(id=903, chat_id=-1002))
    ctx = PluginContext(account_id=11, feature_key="_test_command_trace", client=raw_client)

    async def _handler(client, event, args, account_id, ctx):  # noqa: ANN001
        await client.send_message(12345, "command client ok")

    wrapped = loader_mod._wrap_cmd(_handler, ctx)
    event = SimpleNamespace(trace_id="evt_command_client_trace", chat_id=12345, message=SimpleNamespace(id=7))

    await wrapped(raw_client, event, [], 11)

    raw_client.send_message.assert_awaited_once_with(12345, "command client ok")
    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["type"] == "send_message"
    assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"


@pytest.mark.asyncio
async def test_plugin_command_ctx_messages_apply_records_trace(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    rule = {
        "id": "ten-half-paid",
        "name": "十点半",
        "action": "module",
        "module_key": "ten_half",
        "module_action": "start_ten_half",
        "module_session_scope": "chat",
        "participant_policy": "paid_pool",
        "chat_ids": [-100123],
        "valid_seconds": 600,
    }
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(
        loader_mod.account_bot_service,
        "get_transfer_notice_config",
        AsyncMock(return_value={"enabled": True, "rules": [rule]}),
    )
    state = loader_mod._AccountState(1)
    state.redis = _FakeRedis()
    ctx = PluginContext(
        account_id=1,
        feature_key="ten_half",
        client=MagicMock(),
        messages=loader_mod._LiveMessageOps(state, plugin_key="ten_half"),
    )

    async def _handler(client, event, args, account_id, ctx):  # noqa: ANN001
        await ctx.messages.apply(
            [
                {
                    "type": "start_session",
                    "chat_id": -100123,
                    "entry_key": "start_ten_half",
                    "started_by_user_id": 999,
                }
            ],
            entry_key="start_ten_half",
        )

    wrapped = loader_mod._wrap_cmd(_handler, ctx)
    event = SimpleNamespace(trace_id="evt_command_session_trace", chat_id=-100123, message=SimpleNamespace(id=7))

    await wrapped(ctx.client, event, ["100"], 1)

    record_action.assert_awaited_once()
    assert record_action.await_args.args[0]["trace_id"] == "evt_command_session_trace"
    assert record_action.await_args.args[1]["type"] == "start_session"
    assert record_action.await_args.args[1]["context"]["entry_key"] == "start_ten_half"
    assert record_action.await_args.kwargs["actual_send_via"] == "interaction_session"
    session_payload = json.loads(state.redis.sets[-1][1])
    assert session_payload["channel"] == "userbot"
    assert session_payload["expires_at"] > session_payload["created_at"]
    assert state.redis.sets[-1][2]["ex"] == 690
    assert -100123 in state.userbot_session_chats


@pytest.mark.asyncio
async def test_live_message_ops_defaults_to_userbot_reply(monkeypatch) -> None:
    state = loader_mod._AccountState(18)
    state.redis = _FakeRedis()
    apply_actions = AsyncMock(return_value=False)
    monkeypatch.setattr(loader_mod, "_apply_userbot_event_bus_actions", apply_actions)

    messages = loader_mod._LiveMessageOps(state, plugin_key="_test_live_messages")

    await messages.send(chat_id=-100123, text="命令回复")
    await messages.payout(chat_id=-100123, amount=88, reply_to_user_id=12345)

    # 归一化结果通过下游 _apply_userbot_event_bus_actions 收到的 actions 断言，
    # 而不是读 messages.actions —— 后者是 facade 字段，不再跨 apply 累积。
    assert apply_actions.await_count == 2
    assert apply_actions.await_args_list[0].kwargs["actions"] == [
        {
            "type": "send_message",
            "chat_id": -100123,
            "text": "命令回复",
            "parse_mode": "plain",
            "reply_to_message_id": None,
            "send_via": "userbot_reply",
            "context": {
                "plugin_key": "_test_live_messages",
            },
        },
    ]
    assert apply_actions.await_args_list[1].kwargs["actions"] == [
        {
            "type": "payout",
            "chat_id": -100123,
            "amount": 88,
            "parse_mode": "plain",
            "reply_to_message_id": None,
            "reply_to_user_id": 12345,
            "context": {
                "plugin_key": "_test_live_messages",
            },
        },
    ]
    # 泄漏回归锁：apply 执行后 actions 不累积，恒为空。
    assert messages.actions == []


@pytest.mark.asyncio
async def test_plugin_command_uses_call_scoped_context() -> None:
    state = loader_mod._AccountState(19)
    state.redis = _FakeRedis()
    base_client = MagicMock()
    base_client._is_sandboxed = False
    base_client.is_sandbox_client = False
    base_ctx = PluginContext(
        account_id=19,
        feature_key="_test_command_scoped_ctx",
        client=base_client,
        messages=loader_mod._LiveMessageOps(state, plugin_key="_test_command_scoped_ctx"),
    )
    seen: list[tuple[bool, bool, bool]] = []
    ready: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    async def _handler(client, event, args, account_id, ctx):  # noqa: ANN001
        seen.append((ctx is base_ctx, ctx.client is base_ctx.client, ctx.messages is base_ctx.messages))
        await ready.put(None)
        await release.wait()

    wrapped = loader_mod._wrap_cmd(_handler, base_ctx)
    first = asyncio.create_task(
        wrapped(base_client, SimpleNamespace(trace_id="evt_cmd_first", chat_id=1), [], 19)
    )
    second = asyncio.create_task(
        wrapped(base_client, SimpleNamespace(trace_id="evt_cmd_second", chat_id=1), [], 19)
    )
    await ready.get()
    await ready.get()
    release.set()
    await asyncio.gather(first, second)

    assert seen == [(False, False, False), (False, False, False)]
    assert base_ctx.client is base_client
    assert isinstance(base_ctx.messages, loader_mod._LiveMessageOps)


@pytest.mark.asyncio
async def test_interaction_text_guard_uses_cached_rules_without_db(monkeypatch) -> None:
    state = loader_mod._AccountState(20)
    state.interaction_text_guard_rules = (
        loader_mod._InteractionTextGuardRule(
            chat_ids=frozenset({-100123}),
            texts=frozenset({"我要猜骰", "开启游戏", "关闭游戏"}),
        ),
    )

    def _forbidden_session():
        raise AssertionError("ordinary guard lookup should use _AccountState cache, not DB")

    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", _forbidden_session)

    assert await loader_mod._interaction_bot_owns_incoming_text(
        state,
        SimpleNamespace(chat_id=-100123, raw_text="我要猜骰"),
    ) is True
    assert await loader_mod._interaction_bot_owns_incoming_text(
        state,
        SimpleNamespace(chat_id=-100999, raw_text="我要猜骰"),
    ) is False


@pytest.mark.asyncio
async def test_interaction_text_guard_skip_records_trace(monkeypatch) -> None:
    fake_db = _FakeDB(
        accounts={21: _FakeAcc(id=21)},
        humanize={21: None},
        afs=[],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_interaction_guard")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    record_span = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=21, paused=paused, redis=_FakeRedis())
        state = loader_mod._STATES[21]
        state.interaction_text_guard_rules = (
            loader_mod._InteractionTextGuardRule(
                chat_ids=frozenset({-100123}),
                texts=frozenset({"我要猜骰"}),
            ),
        )
        await captured[-1](SimpleNamespace(
            chat_id=-100123,
            sender_id=42,
            raw_text="我要猜骰",
            text="我要猜骰",
            id=101,
            is_private=False,
            is_group=True,
            is_channel=False,
            get_chat=AsyncMock(return_value=None),
        ))

        assert any(
            call.args[1] == "route"
            and call.args[2] == loader_mod.TRACE_STATUS_SKIPPED
            and call.kwargs.get("reason_code") == loader_mod._INTERACTION_RULE_OWNED_REASON_CODE
            for call in record_span.await_args_list
        )
        finish_trace.assert_awaited_once()
        assert finish_trace.await_args.args[:2] == (trace, loader_mod.TRACE_STATUS_SKIPPED)
    finally:
        loader_mod._STATES.pop(21, None)


@pytest.mark.asyncio
async def test_userbot_session_dispatch_invokes_interaction_entry_and_skips_legacy(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    interaction_payloads: list[dict[str, Any]] = []
    legacy_calls: list[str] = []

    @register
    class _SessionPlugin(Plugin):
        key = "_test_userbot_session"
        display_name = "UserBot 会话测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            legacy_calls.append(str(getattr(event, "raw_text", "")))

        async def on_interaction(self, ctx: PluginContext, entry_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
            interaction_payloads.append(payload)
            return [{"type": "send_message", "text": "session ok"}]

    redis = _FakeRedis()
    redis.values["account_bot:interaction_session:45:rule-session:-100789"] = json.dumps(
        {
            "account_id": 45,
            "chat_id": -100789,
            "rule_id": "rule-session",
            "module_key": "_test_userbot_session",
            "entry_key": "main",
            "channel": "userbot",
            "data": {"round": 1},
            "created_at": 1,
            "updated_at": 2,
            "expires_at": 4_000_000_000,
        }
    )
    fake_db = _FakeDB(
        accounts={45: _FakeAcc(id=45)},
        humanize={45: None},
        afs=[_FakeAF(account_id=45, feature_key="_test_userbot_session", enabled=True, config={})],
        rules=[],
    )
    trace = SimpleNamespace(trace_id="evt_userbot_session")
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": True,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=778))
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=45, paused=paused, redis=redis)
        state = loader_mod._STATES[45]
        state.engine = SimpleNamespace(
            acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
        )
        incoming_dispatch = captured[-1]
        await incoming_dispatch(SimpleNamespace(
            chat_id=-100789,
            sender_id=99,
            raw_text="answer",
            text="answer",
            id=600,
            is_private=False,
            is_group=True,
            is_channel=False,
            get_chat=AsyncMock(return_value=None),
        ))

        assert legacy_calls == []
        assert len(interaction_payloads) == 1
        payload = interaction_payloads[0]
        assert payload["source"]["channel"] == "userbot"
        assert payload["session"]["channel"] == "userbot"
        assert payload["session"]["data"] == {"round": 1}
        assert payload["trigger"]["type"] == "session_message"
        assert payload["trigger"]["channel"] == "userbot"
        state.engine.acquire.assert_awaited_once_with(45, "send_message_group", peer_id=-100789)
        client.send_message.assert_awaited_once_with(-100789, "session ok", reply_to=None, parse_mode=None)
        assert record_action.await_args.args[1]["send_via"] == "userbot_reply"
        finish_trace.assert_awaited_once()
        assert finish_trace.await_args.args[:2] == (trace, loader_mod.TRACE_STATUS_OK)
        assert finish_trace.await_args.kwargs["consumed"] is True
    finally:
        loader_mod._STATES.pop(45, None)
        _REGISTRY.pop("_test_userbot_session", None)


@pytest.mark.asyncio
async def test_userbot_observed_interaction_session_keeps_logical_interaction_channel(monkeypatch) -> None:
    _mock_payout_delivery(monkeypatch)
    redis = _FakeRedis()
    state = loader_mod._AccountState(82)
    state.redis = redis
    state.interaction_bot_sender_ids = frozenset({9000, 9001})
    state.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=702)))
    state.engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0, outcome="ok"))
    )
    session_key = "account_bot:interaction_session:82:rule-math:-10082"
    redis.values[session_key] = json.dumps(
        {
            "account_id": 82,
            "chat_id": -10082,
            "rule_id": "rule-math",
            "module_key": "math10",
            "entry_key": "start_math_game",
            "channel": "interaction_bot",
            "data": {"answer": 10},
            "expires_at": 4_000_000_000,
        }
    )
    captured: dict[str, Any] = {}

    async def fake_invoke(account_id, *, plugin_key, entry_key, payload, default_send_via=None):
        captured.update(
            {
                "account_id": account_id,
                "plugin_key": plugin_key,
                "entry_key": entry_key,
                "payload": payload,
                "default_send_via": default_send_via,
            }
        )
        return loader_mod._normalize_interaction_actions(
            [
                {"type": "send_message", "text": "答对"},
                {"type": "payout", "amount": 666},
                {"type": "end_session"},
            ],
            default_send_via=default_send_via,
        )

    interaction_send = AsyncMock(return_value={"message_id": 701})
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", fake_invoke)
    monkeypatch.setattr(loader_mod, "_interaction_bot_token_for_account", AsyncMock(return_value="interaction-token"))
    monkeypatch.setattr(loader_mod.account_bot_service, "send_message", interaction_send)
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(
        loader_mod.payout_limit,
        "check_and_consume",
        AsyncMock(return_value=(True, None)),
    )

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10082, sender_id=111, raw_text="10", text="10"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    assert consumed is True
    assert captured["account_id"] == 82
    assert captured["plugin_key"] == "math10"
    assert captured["entry_key"] == "start_math_game"
    assert captured["default_send_via"] == ["interaction_bot"]
    payload = captured["payload"]
    assert payload["source"]["channel"] == "interaction_bot"
    assert payload["source"]["observed_channel"] == "userbot"
    assert payload["session"]["channel"] == "interaction_bot"
    assert payload["trigger"]["channel"] == "interaction_bot"
    assert payload["message_text"] == "10"
    interaction_send.assert_awaited_once_with(
        "interaction-token",
        -10082,
        "答对",
        reply_to_message_id=None,
        reply_markup=None,
        parse_mode="plain",
    )
    state.client.send_message.assert_awaited_once_with(-10082, "+666", reply_to=None, parse_mode=None)
    assert session_key not in redis.values


@pytest.mark.asyncio
async def test_userbot_observed_interaction_session_does_not_consume_without_actions(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(85)
    state.redis = redis
    session_key = "account_bot:interaction_session:85:rule-dice:-10085"
    redis.values[session_key] = json.dumps(
        {
            "account_id": 85,
            "chat_id": -10085,
            "rule_id": "rule-dice",
            "module_key": "dice_grid_hunt",
            "entry_key": "start_dice_grid_hunt",
            "channel": "interaction_bot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)
    message_id = 8501
    claim_key = interaction_message_claim_key(85, -10085, message_id, "rule-dice")

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10085, sender_id=111, id=message_id, raw_text="2", text="2"),
        direction="outgoing",
        edited=False,
        event_label="outgoing",
        redis=redis,
    )

    assert consumed is False
    invoke.assert_awaited_once()
    assert invoke.await_args.kwargs["default_send_via"] == ["interaction_bot"]
    assert claim_key not in redis.values
    assert await loader_mod.claim_interaction_message(
        account_id=85,
        chat_id=-10085,
        message_id=message_id,
        rule_id="rule-dice",
        redis=redis,
    ) is True
    synthetic_payload = loader_mod._userbot_session_event_payload(
        {"source": {"channel": "userbot"}, "trigger": {}, "message": {"text": "2"}},
        session_key=session_key,
        session=json.loads(redis.values[session_key]),
        event_label="incoming",
        callback_data="pick:2",
    )
    assert synthetic_payload["message_text"] == "2"
    finish_trace.assert_awaited_once()
    assert finish_trace.await_args.kwargs["consumed"] is False


@pytest.mark.asyncio
async def test_userbot_observed_interaction_session_consumes_when_actions_returned(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(85)
    state.redis = redis
    redis.values["account_bot:interaction_session:85:rule-dice:-10085"] = json.dumps(
        {
            "account_id": 85,
            "chat_id": -10085,
            "rule_id": "rule-dice",
            "module_key": "dice_grid_hunt",
            "entry_key": "start_dice_grid_hunt",
            "channel": "interaction_bot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[{"type": "send_message", "text": "handled"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod.account_bot_service, "send_message", AsyncMock())
    monkeypatch.setattr(loader_mod, "_interaction_bot_token_for_account", AsyncMock(return_value="interaction-token"))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)
    message_id = 8502
    claim_key = interaction_message_claim_key(85, -10085, message_id, "rule-dice")

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10085, sender_id=111, id=message_id, raw_text="2", text="2"),
        direction="outgoing",
        edited=False,
        event_label="outgoing",
        redis=redis,
    )

    assert consumed is True
    invoke.assert_awaited_once()
    assert invoke.await_args.kwargs["default_send_via"] == ["interaction_bot"]
    assert claim_key in redis.values
    finish_trace.assert_awaited_once()
    assert finish_trace.await_args.kwargs["consumed"] is True


@pytest.mark.asyncio
async def test_unrelated_interaction_session_does_not_mute_other_userbot_plugins(monkeypatch) -> None:
    redis = _FakeRedis()
    redis.values["account_bot:interaction_session:88:rule-dice:-10088"] = json.dumps(
        {
            "account_id": 88,
            "chat_id": -10088,
            "rule_id": "rule-dice",
            "module_key": "dice_grid_hunt",
            "entry_key": "start_dice_grid_hunt",
            "channel": "interaction_bot",
            "expires_at": 4_000_000_000,
        }
    )
    fake_db = _FakeDB(
        accounts={88: _FakeAcc(id=88)},
        humanize={88: None},
        afs=[],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        loader_mod,
        "_start_userbot_message_trace",
        AsyncMock(return_value=loader_mod._UserbotEventBusDispatch(trace=None, event_payload={})),
    )
    dispatch_event_bus = AsyncMock(return_value=(0, 0, frozenset()))
    monkeypatch.setattr(loader_mod, "_dispatch_userbot_event_bus_matches", dispatch_event_bus)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=88, paused=paused, redis=redis)
        incoming_dispatch = captured[-1]
        await incoming_dispatch(SimpleNamespace(
            chat_id=-10088,
            sender_id=111,
            raw_text="unrelated",
            text="unrelated",
            id=888,
            is_private=False,
            is_group=True,
            is_channel=False,
            get_chat=AsyncMock(return_value=None),
        ))

        loader_mod.invoke_interaction_entry.assert_awaited_once()
        assert finish_trace.await_args_list[0].kwargs["consumed"] is False
        dispatch_event_bus.assert_awaited_once()
    finally:
        loader_mod._STATES.pop(88, None)


@pytest.mark.asyncio
async def test_userbot_observed_interaction_session_skips_platform_bot_sender(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(83)
    state.redis = redis
    state.userbot_session_chats.add(-10083)
    state.interaction_bot_sender_ids = frozenset({9000})
    redis.values["account_bot:interaction_session:83:rule-math:-10083"] = json.dumps(
        {
            "account_id": 83,
            "chat_id": -10083,
            "rule_id": "rule-math",
            "module_key": "math10",
            "entry_key": "start_math_game",
            "channel": "interaction_bot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[{"type": "send_message", "text": "nope"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10083, sender_id=9000, raw_text="算数题测试开始", text="算数题测试开始"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    assert consumed is False
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_userbot_session_command_prefix_message_skips_session_feed(monkeypatch) -> None:
    state = loader_mod._AccountState(46)
    state.redis = _FakeRedis()
    state.userbot_session_chats.add(-100111)
    state.redis.values["account_bot:interaction_session:46:rule:-100111"] = json.dumps(
        {
            "account_id": 46,
            "chat_id": -100111,
            "rule_id": "rule",
            "module_key": "demo",
            "entry_key": "main",
            "channel": "userbot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[{"type": "send_message", "text": "nope"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-100111, sender_id=9, raw_text=",guess 100", text=",guess 100"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=state.redis,
    )

    assert consumed is False
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_userbot_session_shortcircuits_scan_when_cache_ready_and_chat_absent(monkeypatch) -> None:
    """F3：缓存已就绪（ready）且 chat 不在集合时，直接短路、不触发 Redis SCAN。"""
    redis = _FakeRedis()
    scan_calls: list[str] = []
    original_scan = redis.scan_iter

    def _spy_scan(match: str):
        scan_calls.append(match)
        return original_scan(match=match)

    monkeypatch.setattr(redis, "scan_iter", _spy_scan)

    state = loader_mod._AccountState(84)
    state.redis = redis
    state.userbot_session_chats_ready = True
    # chat -10084 不在集合里
    invoke = AsyncMock(return_value=[{"type": "send_message", "text": "nope"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10084, sender_id=5, raw_text="随便说点什么", text="随便说点什么"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    assert consumed is False
    invoke.assert_not_awaited()
    # 关键：短路后完全没有触发 SCAN。
    assert scan_calls == []


@pytest.mark.asyncio
async def test_userbot_session_does_not_shortcircuit_when_cache_not_ready(monkeypatch) -> None:
    """F3 防呆：缓存未就绪（ready=False）时不得短路，照常 SCAN，避免全量假阴性漏会话。"""
    redis = _FakeRedis()
    scan_calls: list[str] = []
    original_scan = redis.scan_iter

    def _spy_scan(match: str):
        scan_calls.append(match)
        return original_scan(match=match)

    monkeypatch.setattr(redis, "scan_iter", _spy_scan)

    state = loader_mod._AccountState(85)
    state.redis = redis
    # ready 保持默认 False，chat 不在集合里
    assert state.userbot_session_chats_ready is False
    invoke = AsyncMock(return_value=[{"type": "send_message", "text": "nope"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10085, sender_id=5, raw_text="随便说点什么", text="随便说点什么"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    # 无会话所以最终仍不消费，但必须是走完 SCAN 后得出的结论，而不是被短路挡掉。
    assert consumed is False
    assert scan_calls, "ready=False 时必须照常 SCAN，不能短路"


@pytest.mark.asyncio
async def test_userbot_session_feeds_when_cache_ready_and_chat_present(monkeypatch) -> None:
    """F3 假阴性防护：ready + chat 在集合 + 存在真实会话时，正常派发（短路不误伤）。"""
    redis = _FakeRedis()
    state = loader_mod._AccountState(86)
    state.redis = redis
    state.userbot_session_chats_ready = True
    state.userbot_session_chats.add(-10086)
    redis.values["account_bot:interaction_session:86:rule:-10086"] = json.dumps(
        {
            "account_id": 86,
            "chat_id": -10086,
            "rule_id": "rule",
            "module_key": "demo",
            "entry_key": "main",
            "channel": "userbot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[{"type": "end_session"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10086, sender_id=9, raw_text="继续", text="继续"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    assert consumed is True
    invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_userbot_channel_session_still_consumes_without_actions(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(87)
    state.redis = redis
    redis.values["account_bot:interaction_session:87:rule:-10087"] = json.dumps(
        {
            "account_id": 87,
            "chat_id": -10087,
            "rule_id": "rule",
            "module_key": "demo",
            "entry_key": "main",
            "channel": "userbot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "record_span", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10087, sender_id=9, raw_text="继续", text="继续"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    assert consumed is True
    invoke.assert_awaited_once()
    assert invoke.await_args.kwargs["default_send_via"] == ["userbot_reply"]
    finish_trace.assert_awaited_once()
    assert finish_trace.await_args.kwargs["consumed"] is True


@pytest.mark.asyncio
async def test_userbot_session_message_skips_cross_channel_duplicate(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(89)
    state.redis = redis
    redis.values["account_bot:interaction_session:89:rule-dupe:-10089"] = json.dumps(
        {
            "account_id": 89,
            "chat_id": -10089,
            "rule_id": "rule-dupe",
            "module_key": "demo",
            "entry_key": "main",
            "channel": "userbot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[{"type": "send_message", "text": "nope"}])
    claim = AsyncMock(return_value=False)
    record_span = AsyncMock()
    finish_trace = AsyncMock()
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "claim_interaction_message", claim)
    monkeypatch.setattr(loader_mod, "_start_userbot_session_trace", AsyncMock(return_value=None))
    monkeypatch.setattr(loader_mod, "record_span", record_span)
    monkeypatch.setattr(loader_mod, "finish_trace", finish_trace)

    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        SimpleNamespace(chat_id=-10089, sender_id=9, id=8901, raw_text="继续", text="继续"),
        direction="incoming",
        edited=False,
        event_label="incoming",
        redis=redis,
    )

    assert consumed is False
    claim.assert_awaited_once_with(
        account_id=89,
        chat_id=-10089,
        message_id=8901,
        rule_id="rule-dupe",
        redis=redis,
        fail_open=False,
    )
    invoke.assert_not_awaited()
    assert any(
        call.args[1:3] == ("route", loader_mod.TRACE_STATUS_SKIPPED)
        and call.kwargs.get("reason_code") == "cross_channel_duplicate"
        for call in record_span.await_args_list
    )
    finish_trace.assert_awaited_once()
    assert finish_trace.await_args.args[1] == loader_mod.TRACE_STATUS_SKIPPED


@pytest.mark.asyncio
async def test_userbot_session_outgoing_requires_entry_include_outgoing(monkeypatch) -> None:
    state = loader_mod._AccountState(47)
    state.redis = _FakeRedis()
    state.userbot_session_chats.add(-100222)
    state.redis.values["account_bot:interaction_session:47:rule:-100222"] = json.dumps(
        {
            "account_id": 47,
            "chat_id": -100222,
            "rule_id": "rule",
            "module_key": "demo",
            "entry_key": "main",
            "channel": "userbot",
            "expires_at": 4_000_000_000,
        }
    )
    invoke = AsyncMock(return_value=[{"type": "end_session"}])
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "current_command_prefix", lambda *, fallback=None: ",")
    monkeypatch.setattr(loader_mod, "_load_event_framework_flags", AsyncMock(return_value={
        "trace_enabled": False,
        "event_bus_delivery_enabled": True,
    }))
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    monkeypatch.setattr(loader_mod.account_bot_service, "declared_module_entry_manifest", lambda *_args: {"include_outgoing": False})

    event = SimpleNamespace(chat_id=-100222, sender_id=9, raw_text="host reply", text="host reply")
    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        event,
        direction="outgoing",
        edited=False,
        event_label="outgoing",
        redis=state.redis,
    )

    assert consumed is False
    invoke.assert_not_awaited()

    monkeypatch.setattr(loader_mod.account_bot_service, "declared_module_entry_manifest", lambda *_args: {"include_outgoing": True})
    consumed = await loader_mod._dispatch_userbot_session_message(
        state,
        event,
        direction="outgoing",
        edited=False,
        event_label="outgoing",
        redis=state.redis,
    )

    assert consumed is True
    invoke.assert_awaited_once()
    assert invoke.await_args.kwargs["default_send_via"] == ["userbot_reply"]


@pytest.mark.asyncio
async def test_message_edited_dispatches_dedicated_hook(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    message_calls: list[str] = []
    edited_calls: list[str] = []

    @register
    class _EditedPlugin(Plugin):
        key = "_test_edited_message"
        display_name = "编辑消息测试"
        message_channels = {"incoming"}
        owner_only = False

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            message_calls.append(str(getattr(event, "raw_text", "")))

        async def on_message_edited(self, ctx: PluginContext, event: Any) -> None:
            edited_calls.append(str(getattr(event, "raw_text", "")))

    class _Event:
        raw_text = "edited text"
        chat_id = -1001
        sender_id = 42
        is_private = False
        is_group = True
        is_channel = False

        async def get_chat(self):
            return None

    fake_db = _FakeDB(
        accounts={8: _FakeAcc(id=8)},
        humanize={8: None},
        afs=[_FakeAF(account_id=8, feature_key="_test_edited_message", enabled=True, config={})],
        rules=[],
    )
    interaction_owned = AsyncMock(return_value=True)
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_interaction_bot_owns_incoming_text", interaction_owned)

    captured: list[Any] = []

    def _on(_filter):
        def _wrap(fn):
            captured.append(fn)
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=8, paused=paused, redis=_FakeRedis())
        incoming_edited_dispatch = captured[1]
        await incoming_edited_dispatch(_Event())

        assert message_calls == []
        assert edited_calls == ["edited text"]
        interaction_owned.assert_not_awaited()
    finally:
        loader_mod._STATES.pop(8, None)
        _REGISTRY.pop("_test_edited_message", None)


# ─────────────────────────────────────────────────────
# 用例 2：load_plugins_for_account 调到 on_startup
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_load_calls_on_startup(monkeypatch) -> None:
    """模拟一个 account_feature 行（核心平台插件 enabled），验证 on_startup 被调一次。"""
    # 1) mock db 数据
    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[_FakeAF(account_id=1, feature_key=FEATURE_FORWARD, enabled=True, config={})],
        rules=[],
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    # 2) 替换 ForwardPlugin.on_startup 为 spy
    on_startup_spy = AsyncMock()
    monkeypatch.setattr(
        "app.worker.plugins.builtin.forward.ForwardPlugin.on_startup",
        on_startup_spy,
    )

    # 3) mock telethon client（client.on 装饰器返回原函数即可）
    client = MagicMock()

    def _on(_filter):
        def _wrap(fn):
            return fn

        return _wrap

    client.on = _on

    redis = _FakeRedis()
    paused = asyncio.Event()
    paused.set()

    await load_plugins_for_account(client, account_id=1, paused=paused, redis=redis)

    on_startup_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_loader_injects_namespaced_plugin_storage(monkeypatch) -> None:
    """loader 应把同一个 Redis 后端包装为 ctx.storage，供插件按需持久化状态。"""
    from app.worker.plugins.base import _REGISTRY, register
    from app.worker.plugins.storage import PluginStorage

    captured: dict[str, PluginContext] = {}

    @register
    class _TempStoragePlugin(Plugin):
        key = "_test_storage_injected"
        display_name = "storage 注入测试"

        async def on_startup(self, ctx: PluginContext) -> None:  # noqa: D401
            captured["ctx"] = ctx

    fake_db = _FakeDB(
        accounts={31: _FakeAcc(id=31)},
        humanize={31: None},
        afs=[_FakeAF(account_id=31, feature_key=_TempStoragePlugin.key, enabled=True, config={})],
        rules=[],
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    client = MagicMock()
    client.on = lambda f: (lambda fn: fn)
    redis = _FakeRedis()
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=31, paused=paused, redis=redis)

        ctx = captured["ctx"]
        assert ctx.redis is redis
        assert isinstance(ctx.storage, PluginStorage)
        await ctx.storage.set("round", {"status": "open"})
        assert await ctx.storage.get("round") == {"status": "open"}
        assert redis.values["plugin_store:31:_test_storage_injected:round"] == '{"status":"open"}'
    finally:
        loader_mod._STATES.pop(31, None)
        _REGISTRY.pop(_TempStoragePlugin.key, None)


@pytest.mark.asyncio
async def test_ai_facade_requires_ai_text_or_ai_agent_permission(monkeypatch) -> None:
    """ctx.ai 只应给声明 ai_text/ai_agent 的插件，Agent 权限独立保留。"""
    from app.worker.plugins.ai_facade import PluginAI
    from app.worker.plugins.base import _REGISTRY, PluginIdentityFacade, register

    @register
    class _TempAIPlugin(Plugin):
        key = "_test_ai_allowed"
        display_name = "AI 权限测试"

    @register
    class _TempNoAIPlugin(Plugin):
        key = "_test_ai_denied"
        display_name = "无 AI 权限测试"

    @register
    class _TempAgentPlugin(Plugin):
        key = "_test_ai_agent"
        display_name = "Agent 权限测试"

    _TempAIPlugin._manifest = Manifest(
        key="_test_ai_allowed",
        display_name="AI 权限测试",
        permissions=["ai_text"],
    )
    _TempNoAIPlugin._manifest = Manifest(
        key="_test_ai_denied",
        display_name="无 AI 权限测试",
        permissions=[],
    )
    _TempAgentPlugin._manifest = Manifest(
        key="_test_ai_agent",
        display_name="Agent 权限测试",
        permissions=["ai_agent"],
        capabilities={"agent_tools": {"enabled": True}},
        agent_tools=[
            {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )

    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[
            _FakeAF(account_id=1, feature_key="_test_ai_allowed", enabled=True, config={}),
            _FakeAF(account_id=1, feature_key="_test_ai_denied", enabled=True, config={}),
            _FakeAF(account_id=1, feature_key="_test_ai_agent", enabled=True, config={}),
        ],
        rules=[],
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    client = MagicMock()
    client.on = lambda f: (lambda fn: fn)
    paused = asyncio.Event()
    paused.set()

    try:
        await load_plugins_for_account(client, account_id=1, paused=paused, redis=_FakeRedis())
        state = loader_mod._STATES[1]

        assert isinstance(state.contexts["_test_ai_allowed"].ai, PluginAI)
        assert state.contexts["_test_ai_denied"].ai is None
        assert isinstance(state.contexts["_test_ai_agent"].ai, PluginAI)
        assert state.contexts["_test_ai_agent"].ai._allow_agent is True
        assert all(
            isinstance(state.contexts[key].identities, PluginIdentityFacade)
            for key in ("_test_ai_allowed", "_test_ai_denied", "_test_ai_agent")
        )
    finally:
        loader_mod._STATES.pop(1, None)
        _REGISTRY.pop("_test_ai_allowed", None)
        _REGISTRY.pop("_test_ai_denied", None)
        _REGISTRY.pop("_test_ai_agent", None)


@pytest.mark.asyncio
async def test_activate_logs_reserved_unsupported_facade_permission() -> None:
    """声明预留 facade 权限时要写 warning，避免插件作者误以为权限已生效。"""
    from app.worker.plugins.base import _REGISTRY, register

    @register
    class _TempReservedFacadePlugin(Plugin):
        key = "_test_reserved_facade_permission"
        display_name = "预留 facade 权限测试"

        async def on_startup(self, ctx: PluginContext) -> None:  # noqa: D401
            return None

    plugin_key = _TempReservedFacadePlugin.key
    _TempReservedFacadePlugin._source = "installed"
    _TempReservedFacadePlugin._manifest = Manifest(
        key=plugin_key,
        display_name="预留 facade 权限测试",
        permissions=["ai_vision"],
    )

    af = _FakeAF(account_id=1, feature_key=plugin_key, enabled=True, config={})
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        installed_plugins={
            plugin_key: _FakeInstalledPlugin(
                key=plugin_key,
                enabled=True,
                signature_ok=True,
                trust_tier="community",
            )
        },
    )
    state = loader_mod._AccountState(account_id=1)
    state.client = MagicMock()
    redis = _FakeRedis()

    try:
        await loader_mod._activate(db, state, af, redis)
    finally:
        _REGISTRY.pop(plugin_key, None)

    decoded_logs = [json.loads(payload) for _, payload in redis.list_pushes]
    assert any(
        log["source"] == "system"
        and log["level"] == "warn"
        and "ai_vision" in log["message"]
        and log["detail"]["plugin_key"] == plugin_key
        for log in decoded_logs
    )


@pytest.mark.asyncio
async def test_activate_decrypts_schema_only_account_secret(monkeypatch) -> None:
    import app.crypto as crypto
    from app.crypto import generate_master_key
    from app.services.plugin_config_secrets import wrap_secret
    from app.settings import settings
    from app.worker.plugins.base import _REGISTRY, register

    captured = {}

    @register
    class _SchemaSecretPlugin(Plugin):
        key = "_test_schema_account_secret"
        display_name = "schema secret"

        async def on_startup(self, ctx: PluginContext) -> None:
            captured.update(ctx.account_config)

    schema = {
        "type": "object",
        "properties": {
            "credential": {"type": "string", "x-sensitive": True},
        },
    }
    plugin_key = _SchemaSecretPlugin.key
    _SchemaSecretPlugin._source = "installed"
    _SchemaSecretPlugin._manifest = Manifest(
        key=plugin_key,
        display_name="schema secret",
        config_schema=schema,
    )
    monkeypatch.setattr(settings, "master_key", generate_master_key())
    monkeypatch.setattr(crypto, "_fernet", None)
    af = _FakeAF(
        account_id=1,
        feature_key=plugin_key,
        enabled=True,
        config={"credential": wrap_secret("schema-only-value")},
    )
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        features={plugin_key: _FakeFeature(key=plugin_key, manifest={"config_schema": schema})},
        installed_plugins={plugin_key: _FakeInstalledPlugin(plugin_key)},
    )
    state = loader_mod._AccountState(account_id=1)
    state.client = MagicMock()

    try:
        await loader_mod._activate(db, state, af, _FakeRedis())
        assert captured["credential"] == "schema-only-value"
    finally:
        _REGISTRY.pop(plugin_key, None)


@pytest.mark.asyncio
async def test_activate_isolates_corrupt_plugin_secret(monkeypatch) -> None:
    from app.worker.plugins.base import _REGISTRY, register

    startup = AsyncMock()

    @register
    class _CorruptSecretPlugin(Plugin):
        key = "_test_corrupt_secret"
        display_name = "corrupt secret"
        on_startup = startup

    plugin_key = _CorruptSecretPlugin.key
    _CorruptSecretPlugin._source = "installed"
    _CorruptSecretPlugin._manifest = Manifest(
        key=plugin_key,
        display_name="corrupt secret",
    )
    envelope = "secret:v1:corrupt-ciphertext"
    af = _FakeAF(
        account_id=1,
        feature_key=plugin_key,
        enabled=True,
        config={"cookie": envelope},
    )
    db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[af],
        rules=[],
        features={plugin_key: _FakeFeature(key=plugin_key, manifest={})},
        installed_plugins={plugin_key: _FakeInstalledPlugin(plugin_key)},
    )
    state = loader_mod._AccountState(account_id=1)
    state.client = MagicMock()

    try:
        await loader_mod._activate(db, state, af, _FakeRedis())
        startup.assert_not_awaited()
        assert plugin_key not in state.instances
        assert af.state == "failed"
        assert "PLUGIN_CONFIG_DECRYPT_FAILED" in (af.last_error or "")
        assert "cookie" in (af.last_error or "")
        assert envelope not in (af.last_error or "")
    finally:
        _REGISTRY.pop(plugin_key, None)


# ─────────────────────────────────────────────────────
# 用例 3：reload_account_config 在 plugin 已禁用时应触发 shutdown
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reload_account_config_shutdown_disabled(monkeypatch) -> None:
    """先正常加载一个 plugin，然后把它在 DB 里改成 enabled=False，触发热重载应调 on_shutdown。"""

    # 注册一个临时 plugin，以便我们独占断言
    from app.worker.plugins.base import register

    @register
    class _TempPlugin(Plugin):
        key = "_test_temp"
        display_name = "测试占位"

        async def on_startup(self, ctx: PluginContext) -> None:  # noqa: D401
            return None

        async def on_shutdown(self, ctx: PluginContext) -> None:  # noqa: D401
            return None

    # 在 feature 表里登记，避免 _activate 因 plugin 未注册而走 failed 分支
    fake_db_init = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[_FakeAF(account_id=1, feature_key="_test_temp", enabled=True, config={})],
        rules=[],
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db_init)
    )

    client = MagicMock()
    client.on = lambda f: (lambda fn: fn)
    paused = asyncio.Event()
    paused.set()
    redis = _FakeRedis()

    # spy on_shutdown
    shutdown_spy = AsyncMock()
    monkeypatch.setattr(_TempPlugin, "on_shutdown", shutdown_spy)

    await load_plugins_for_account(client, account_id=1, paused=paused, redis=redis)

    # 把 fake_db 的 enabled 改成 False，再触发热重载
    fake_db_init.afs[0].enabled = False
    await reload_account_config(account_id=1)

    shutdown_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_account_config_keeps_merged_defaults_stable(monkeypatch) -> None:
    """首次激活和后续热更新应使用同一套合并配置，避免每次 reload 都误重启插件。"""
    from app.worker.plugins.base import _REGISTRY, register

    startup_configs: list[dict[str, Any]] = []
    shutdown_spy = AsyncMock()

    @register
    class _TempConfigPlugin(Plugin):
        key = "_test_config_stable"
        display_name = "配置稳定性测试"
        command_config_keys = {"command", "timeout"}

        async def on_startup(self, ctx: PluginContext) -> None:  # noqa: D401
            startup_configs.append(dict(ctx.config))

        async def on_shutdown(self, ctx: PluginContext) -> None:  # noqa: D401
            await shutdown_spy(ctx)

    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[
            _FakeAF(
                account_id=1,
                feature_key="_test_config_stable",
                enabled=True,
                config={"command": "ct"},
            )
        ],
        rules=[],
        features={
            "_test_config_stable": _FakeFeature(
                key="_test_config_stable",
                manifest={
                    "config_schema": {
                        "properties": {
                            "command": {"default": "dicegrid"},
                            "timeout": {"default": 90, "level": "global"},
                        }
                    }
                },
            )
        },
        plugin_global_configs={
            "_test_config_stable": _FakePluginGlobalConfig(
                plugin_key="_test_config_stable",
                config={"timeout": 120},
            )
        },
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    client = MagicMock()
    client.on = lambda f: (lambda fn: fn)
    paused = asyncio.Event()
    paused.set()
    redis = _FakeRedis()

    try:
        await load_plugins_for_account(client, account_id=1, paused=paused, redis=redis)
        state = loader_mod._STATES[1]
        before_generation = state.generation
        await reload_account_config(account_id=1)

        assert startup_configs == [{"command": "ct", "timeout": 120}]
        assert state.generation == before_generation + 1
        assert state.contexts["_test_config_stable"].generation == state.generation
        shutdown_spy.assert_not_awaited()
    finally:
        _REGISTRY.pop("_test_config_stable", None)


@pytest.mark.asyncio
async def test_periodic_reload_account_config_success_is_silent(monkeypatch) -> None:
    """周期性配置收敛成功不写热更新日志，避免默认运行事件被心跳刷屏。"""
    from app.worker.plugins.base import _REGISTRY, register

    @register
    class _TempPeriodicPlugin(Plugin):
        key = "_test_periodic_reload"
        display_name = "周期热更新测试"

    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[_FakeAF(account_id=1, feature_key="_test_periodic_reload", enabled=True, config={})],
        rules=[],
    )
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))

    client = MagicMock()
    client.on = lambda f: (lambda fn: fn)
    paused = asyncio.Event()
    paused.set()
    redis = _FakeRedis()

    try:
        await load_plugins_for_account(client, account_id=1, paused=paused, redis=redis)
        redis.list_pushes.clear()

        await reload_account_config(account_id=1, payload={"source": "periodic_reconcile"})

        payloads = [json.loads(value) for _key, value in redis.list_pushes]
        hot_reload_logs = [item for item in payloads if item["message"] == "插件配置已热更新"]
        assert hot_reload_logs == []
    finally:
        loader_mod._STATES.pop(1, None)
        _REGISTRY.pop("_test_periodic_reload", None)


@pytest.mark.asyncio
async def test_merge_plugin_config_uses_legacy_account_global_field_when_global_empty() -> None:
    """字段迁移到 global 后，旧账号级值应继续作为运行时兼容回退。"""
    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[],
        rules=[],
        features={
            "pt_promote": _FakeFeature(
                key="pt_promote",
                manifest={
                    "config_schema": {
                        "properties": {
                            "command": {"default": "pt"},
                            "cookie": {"default": "", "level": "global"},
                            "torrent_cooldown_seconds": {"default": "12h"},
                        }
                    }
                },
            )
        },
        plugin_global_configs={},
    )

    merged = await loader_mod._merge_plugin_config(
        fake_db,
        1,
        "pt_promote",
        {"command": "pt", "cookie": "sid=legacy", "torrent_cooldown_seconds": "12h"},
    )

    assert merged["cookie"] == "sid=legacy"
    assert merged["command"] == "pt"


@pytest.mark.asyncio
async def test_merge_plugin_config_decrypts_legacy_account_global_secret_envelope(monkeypatch) -> None:
    """兼容回退路径必须解密 secret:v1，避免插件拿到信封明文。"""
    import app.crypto as crypto
    from app.crypto import generate_master_key
    from app.services import plugin_config_secrets as secrets
    from app.settings import settings

    monkeypatch.setattr(settings, "master_key", generate_master_key())
    monkeypatch.setattr(crypto, "_fernet", None)

    envelope = secrets.wrap_secret("sid=secret-legacy")
    assert secrets.is_secret_envelope(envelope)

    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[],
        rules=[],
        features={
            "pt_promote": _FakeFeature(
                key="pt_promote",
                manifest={
                    "config_schema": {
                        "properties": {
                            "command": {"default": "pt"},
                            "cookie": {"default": "", "level": "global"},
                        }
                    }
                },
            )
        },
        plugin_global_configs={},
    )

    merged = await loader_mod._merge_plugin_config(
        fake_db,
        1,
        "pt_promote",
        {"command": "pt", "cookie": envelope},
    )

    assert merged["cookie"] == "sid=secret-legacy"
    assert not secrets.is_secret_envelope(merged["cookie"])


@pytest.mark.asyncio
async def test_live_message_ops_reads_and_deletes_platform_saved_message_id() -> None:
    state = loader_mod._AccountState(account_id=77)
    redis = _FakeRedis()
    state.redis = redis
    redis.values["tp:msgid:77:question:round-1"] = "901"
    messages = loader_mod._LiveMessageOps(state, plugin_key="math10")

    assert await messages.read_saved_message_id("question:round-1") == 901
    assert await messages.delete_saved_message_id("question:round-1") is True
    assert "tp:msgid:77:question:round-1" not in redis.values


@pytest.mark.asyncio
async def test_merge_plugin_config_prefers_saved_global_over_legacy_account_global_field() -> None:
    """全局配置保存成功后，应以 plugin_global_config 为准。"""
    fake_db = _FakeDB(
        accounts={1: _FakeAcc(id=1)},
        humanize={1: None},
        afs=[],
        rules=[],
        features={
            "pt_promote": _FakeFeature(
                key="pt_promote",
                manifest={
                    "config_schema": {
                        "properties": {
                            "command": {"default": "pt"},
                            "cookie": {"default": "", "level": "global"},
                            "torrent_cooldown_seconds": {"default": "12h"},
                        }
                    }
                },
            )
        },
        plugin_global_configs={
            "pt_promote": _FakePluginGlobalConfig(
                plugin_key="pt_promote",
                config={"cookie": "sid=global"},
            )
        },
    )

    merged = await loader_mod._merge_plugin_config(
        fake_db,
        1,
        "pt_promote",
        {"command": "pt", "cookie": "sid=legacy", "torrent_cooldown_seconds": "12h"},
    )

    assert merged["cookie"] == "sid=global"
    assert merged["command"] == "pt"


@pytest.mark.asyncio
async def test_manifest_command_trigger_registers_and_invokes_interaction_entry(monkeypatch) -> None:
    """声明 triggers.command 的入口应自动注册为 userbot 命令并创建标准会话 payload。"""
    state = loader_mod._AccountState(account_id=77)
    redis = _FakeRedis()
    state.redis = redis
    captured: dict[str, Any] = {}

    def fake_register(name, fn, **kwargs):
        captured[name] = {"fn": fn, "kwargs": kwargs}

    invoked: dict[str, Any] = {}

    async def fake_invoke(account_id, *, plugin_key, entry_key, payload, default_send_via=None):
        invoked.update(
            {
                "account_id": account_id,
                "plugin_key": plugin_key,
                "entry_key": entry_key,
                "payload": payload,
                "default_send_via": default_send_via,
            }
        )
        return []

    monkeypatch.setattr(loader_mod, "register_plugin_command", fake_register)
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", fake_invoke)

    ctx = PluginContext(account_id=77, feature_key="demo_game")
    manifest = Manifest(
        key="demo_game",
        display_name="demo",
        interaction_entries=[
            {
                "key": "start",
                "triggers": {"command": "guess"},
                "session_scope": "chat",
                "ttl_seconds": 120,
            }
        ],
    )

    loader_mod._register_manifest_interaction_commands(state, ctx, manifest, {})

    assert "guess" in captured
    assert captured["guess"]["kwargs"] == {"owner_plugin_key": "demo_game", "generation": 1}

    event = SimpleNamespace(
        chat_id=-100123,
        sender_id=456,
        id=88,
        raw_text=",guess 100",
        trace_id="trace-command",
        message=SimpleNamespace(chat_id=-100123, sender_id=456, id=88, text=",guess 100"),
    )
    await captured["guess"]["fn"](AsyncMock(), event, ["100"], 77)

    assert invoked["account_id"] == 77
    assert invoked["plugin_key"] == "demo_game"
    assert invoked["entry_key"] == "start"
    assert invoked["default_send_via"] == ["userbot_reply"]
    assert invoked["payload"]["source"]["type"] == "command"
    assert invoked["payload"]["trigger"]["args"] == ["100"]
    assert invoked["payload"]["session"]["channel"] == "userbot"
    assert state.userbot_session_chats == {-100123}
    stored = next(json.loads(raw) for raw in redis.values.values())
    assert stored["channel"] == "userbot"
    assert stored["data"]["args"] == ["100"]


def test_manifest_command_trigger_honors_keyword_only(monkeypatch) -> None:
    state = loader_mod._AccountState(account_id=78)
    calls: list[str] = []
    monkeypatch.setattr(loader_mod, "register_plugin_command", lambda name, *_a, **_kw: calls.append(name))
    ctx = PluginContext(account_id=78, feature_key="demo_game")

    loader_mod._register_manifest_interaction_commands(
        state,
        ctx,
        Manifest(
            key="demo_game",
            display_name="demo",
            interaction_entries=[{"key": "start", "triggers": {"command": "guess"}}],
        ),
        {"interaction_trigger_modes": "keyword_only"},
    )
    loader_mod._register_manifest_interaction_commands(
        state,
        ctx,
        Manifest(
            key="demo_game",
            display_name="demo",
            interaction_entries=[
                {
                    "key": "start_buttons",
                    "triggers": {"command": "buttons"},
                    "default_trigger_modes": "keyword_only",
                }
            ],
        ),
        {},
    )

    assert calls == []


@pytest.mark.asyncio
async def test_userbot_update_session_merges_data_without_resetting_expiry(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=79)
    key = "account_bot:interaction_session:79:manifest_command:demo:start:-100"
    expires_at = time.time() + 120
    session = {
        "account_id": 79,
        "chat_id": -100,
        "channel": "userbot",
        "module_key": "demo",
        "entry_key": "start",
        "expires_at": expires_at,
        "data": {"round": 1},
    }
    redis.values[key] = json.dumps(session)
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_update_session_action(
        state,
        {"type": "update_session", "data": {"score": 3}},
        redis=redis,
        session_key=key,
        session=session,
    )

    assert ok is True
    stored = json.loads(redis.values[key])
    assert stored["data"] == {"round": 1, "score": 3}
    assert stored["expires_at"] == expires_at
    assert redis.sets[-1][2]["ex"] >= 1
    record_action.assert_awaited()


@pytest.mark.asyncio
async def test_userbot_update_session_accepts_observed_interaction_session(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=84)
    key = "account_bot:interaction_session:84:rule-game:-10084"
    expires_at = time.time() + 120
    session = {
        "account_id": 84,
        "chat_id": -10084,
        "channel": "interaction_bot",
        "module_key": "ten_half",
        "entry_key": "start_ten_half",
        "expires_at": expires_at,
        "data": {"players": [10]},
    }
    redis.values[key] = json.dumps(session)
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_update_session_action(
        state,
        {"type": "update_session", "data": {"phase": "betting"}, "extend_seconds": 30},
        redis=redis,
        session_key=key,
        session=session,
    )

    assert ok is True
    stored = json.loads(redis.values[key])
    assert stored["channel"] == "interaction_bot"
    assert stored["data"] == {"players": [10], "phase": "betting"}
    assert stored["expires_at"] > expires_at
    assert session["data"] == stored["data"]
    assert -10084 in state.userbot_session_chats
    record_action.assert_awaited()
    assert record_action.await_args.kwargs["result"]["channel"] == "interaction_bot"


@pytest.mark.asyncio
async def test_userbot_send_message_degrades_buttons_and_synthetic_callback_is_skipped(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=80)
    state.redis = redis
    state.client = AsyncMock()
    state.client.send_message = AsyncMock(return_value=SimpleNamespace(id=9001))
    session_key = "account_bot:interaction_session:80:manifest_command:quiz:start:-100"
    session = {
        "account_id": 80,
        "chat_id": -100,
        "channel": "userbot",
        "module_key": "quiz",
        "entry_key": "start",
        "expires_at": time.time() + 600,
        "data": {},
    }
    redis.values[session_key] = json.dumps(session)
    record_action = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", record_action)

    ok = await loader_mod._apply_userbot_send_message_action(
        state,
        SimpleNamespace(chat_id=-100),
        {
            "type": "send_message",
            "send_via": "userbot_reply",
            "chat_id": -100,
            "text": "请选择",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "A", "callback_data": "pick:a"}],
                    [{"text": "B", "callback_data": "pick:b"}],
                    [{"text": "说明", "url": "https://example.test/help"}],
                ]
            },
        },
        redis=redis,
        session_key=session_key,
        session=session,
    )

    assert ok is True
    sent_text = state.client.send_message.await_args.args[1]
    assert "请回复序号选择" in sent_text
    assert "1. A" in sent_text
    assert "3. 说明: https://example.test/help" in sent_text
    stored = json.loads(redis.values[session_key])
    button_map = stored["data"]["_tp_button_map"]["map"]
    assert button_map["1"] == "pick:a"
    assert button_map["B"] == "pick:b"
    assert "说明" not in button_map

    assert loader_mod._userbot_text_button_callback_data(stored, "2") == "pick:b"
    payload = loader_mod._userbot_session_event_payload(
        {"source": {"channel": "userbot"}, "trigger": {}, "message": {"text": "2"}},
        session_key=session_key,
        session=stored,
        event_label="incoming",
        callback_data="pick:b",
    )
    assert payload["source"]["type"] == "callback_query"
    assert payload["source"]["synthetic"] == "text_button"
    assert payload["callback_data"] == "pick:b"

    answer_ok = await loader_mod._apply_userbot_answer_callback_action(
        state,
        {"type": "answer_callback", "_tp_synthetic_callback": True},
    )
    assert answer_ok is True
    assert record_action.await_args_list[-1].args[2] == loader_mod.TRACE_STATUS_SKIPPED
    assert record_action.await_args_list[-1].kwargs["error_code"] == "synthetic_callback"


@pytest.mark.asyncio
async def test_scan_userbot_expired_sessions_invokes_entry_and_deletes(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=81)
    state.redis = redis
    state.userbot_session_chats = {-10081}
    key = "account_bot:interaction_session:81:manifest_command:quiz:start:-10081"
    redis.values[key] = json.dumps(
        {
            "account_id": 81,
            "chat_id": -10081,
            "channel": "userbot",
            "module_key": "quiz",
            "entry_key": "start",
            "started_by_user_id": 42,
            "expires_at": time.time() - 1,
            "data": {"round": 2},
        }
    )
    invoked: dict[str, Any] = {}

    async def fake_invoke(account_id, *, plugin_key, entry_key, payload, default_send_via=None):
        invoked.update(
            {
                "account_id": account_id,
                "plugin_key": plugin_key,
                "entry_key": entry_key,
                "payload": payload,
                "default_send_via": default_send_via,
            }
        )
        return [{"type": "send_message", "text": "timeout"}]

    monkeypatch.setitem(loader_mod._STATES, 81, state)
    monkeypatch.setattr(loader_mod, "_start_userbot_session_trace", AsyncMock(return_value=None))
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", fake_invoke)
    monkeypatch.setattr(loader_mod, "_apply_userbot_event_bus_actions", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    try:
        processed = await loader_mod.scan_userbot_expired_sessions_once(81)
    finally:
        loader_mod._STATES.pop(81, None)

    assert processed == 1
    assert key not in redis.values
    assert invoked["account_id"] == 81
    assert invoked["plugin_key"] == "quiz"
    assert invoked["entry_key"] == "start"
    assert invoked["payload"]["event_type"] == "session_expired"
    assert invoked["payload"]["session"]["data"] == {"round": 2}
    assert invoked["default_send_via"] == ["userbot_reply"]


@pytest.mark.asyncio
async def test_userbot_expiry_failure_releases_lease_and_retries(monkeypatch) -> None:
    redis = _FakeRedis()
    state = loader_mod._AccountState(account_id=82)
    state.redis = redis
    key = "account_bot:interaction_session:82:manifest_command:quiz:start:-10082"
    redis.values[key] = json.dumps(
        {
            "account_id": 82,
            "chat_id": -10082,
            "channel": "userbot",
            "module_key": "quiz",
            "entry_key": "start",
            "revision": 7,
            "expires_at": time.time() - 1,
            "data": {"round": 2},
        }
    )
    invoke = AsyncMock(side_effect=RuntimeError("temporary failure"))
    monkeypatch.setitem(loader_mod._STATES, 82, state)
    monkeypatch.setattr(loader_mod, "_entry_declares_session_expired", lambda *_args: True)
    monkeypatch.setattr(loader_mod, "_start_userbot_session_trace", AsyncMock(return_value=None))
    monkeypatch.setattr(loader_mod, "invoke_interaction_entry", invoke)
    monkeypatch.setattr(loader_mod, "_apply_userbot_event_bus_actions", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())
    monkeypatch.setattr(loader_mod, "finish_trace", AsyncMock())
    monkeypatch.setattr(loader_mod, "_log", AsyncMock())
    try:
        first = await loader_mod.scan_userbot_expired_sessions_once(82)
        assert first == 0
        retained = json.loads(redis.values[key])
        assert retained["revision"] == 7
        assert "_expiry_claim" not in retained

        invoke.side_effect = None
        invoke.return_value = []
        second = await loader_mod.scan_userbot_expired_sessions_once(82)
    finally:
        loader_mod._STATES.pop(82, None)

    assert second == 1
    assert invoke.await_count == 2
    assert key not in redis.values


@pytest.mark.asyncio
async def test_plugin_invoke_deadline_and_circuit_are_scoped_per_plugin(monkeypatch) -> None:
    slow_key = "_test_slow_plugin"
    fast_key = "_test_fast_plugin"
    slow_calls = 0

    class _SlowPlugin(Plugin):
        key = slow_key
        display_name = "slow"

        async def on_interaction(self, _ctx, _entry_key, _payload):  # noqa: ANN001
            nonlocal slow_calls
            slow_calls += 1
            await asyncio.sleep(60)
            return []

    class _FastPlugin(Plugin):
        key = fast_key
        display_name = "fast"

        async def on_interaction(self, _ctx, _entry_key, _payload):  # noqa: ANN001
            return [{"type": "send_message", "text": "ok"}]

    monkeypatch.setattr(loader_mod.app_settings, "plugin_invoke_timeout_seconds", 0.01)
    monkeypatch.setattr(loader_mod.app_settings, "plugin_circuit_failure_threshold", 2)
    monkeypatch.setattr(loader_mod.app_settings, "plugin_circuit_cooldown_seconds", 60.0)
    state = loader_mod._AccountState(account_id=183)
    for key, instance in ((slow_key, _SlowPlugin()), (fast_key, _FastPlugin())):
        state.instances[key] = instance
        state.contexts[key] = PluginContext(account_id=183, feature_key=key, client=MagicMock())
    loader_mod._STATES[183] = state

    try:
        with pytest.raises(TimeoutError):
            await loader_mod.invoke_interaction_entry(
                183,
                plugin_key=slow_key,
                entry_key="main",
                payload={},
            )
        with pytest.raises(TimeoutError):
            await loader_mod.invoke_interaction_entry(
                183,
                plugin_key=slow_key,
                entry_key="main",
                payload={},
            )
        with pytest.raises(RuntimeError, match="PLUGIN_CIRCUIT_OPEN"):
            await loader_mod.invoke_interaction_entry(
                183,
                plugin_key=slow_key,
                entry_key="main",
                payload={},
            )
        assert slow_calls == 2

        actions = await loader_mod.invoke_interaction_entry(
            183,
            plugin_key=fast_key,
            entry_key="main",
            payload={},
        )
        assert actions[0]["text"] == "ok"
    finally:
        loader_mod._STATES.pop(183, None)
