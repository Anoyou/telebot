"""WP-T4 按叶点枝与看树视图验收测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import ledger as ledger_api
from app.db.models.feature import AccountFeature
from app.db.models.log import AuditLog
from app.db.models.plugin import InstalledPlugin
from app.db.models.system import SystemSetting
from app.services import (
    platform_capabilities as caps,
)
from app.services import (
    platform_tree_service as tree_service,
)
from app.services import (
    plugin_install_service,
    runtime_profile_service,
)
from app.services.plugin_capability_requirements import (
    SOURCE_MISSING_WARNING,
)


class _Scalars:
    def __init__(self, rows):  # noqa: ANN001
        self.rows = list(rows)

    def all(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _Result:
    def __init__(self, rows):  # noqa: ANN001
        self.rows = list(rows)

    def scalars(self):
        return _Scalars(self.rows)


class _TreeDB:
    def __init__(
        self,
        *,
        installed: list[InstalledPlugin] | None = None,
        account_features: list[AccountFeature] | None = None,
        settings: dict[str, object] | None = None,
    ) -> None:
        self.installed = {row.key: row for row in (installed or [])}
        self.account_features = list(account_features or [])
        self.settings = {
            key: SystemSetting(key=key, value=value)
            for key, value in (settings or {}).items()
        }
        self.commits = 0
        self.info: dict[str, object] = {}

    async def get(self, model, key):  # noqa: ANN001
        if model is InstalledPlugin:
            return self.installed.get(key)
        if model is SystemSetting:
            return self.settings.get(key)
        return None

    def add(self, row) -> None:  # noqa: ANN001
        if isinstance(row, SystemSetting):
            self.settings[row.key] = row

    async def execute(self, stmt):  # noqa: ANN001
        entity = stmt.column_descriptions[0].get("entity")
        if entity is InstalledPlugin:
            return _Result(self.installed.values())
        if entity is AccountFeature:
            if len(stmt.column_descriptions) == 1 and stmt.column_descriptions[0].get("name") == "feature_key":
                return _Result(
                    row.feature_key for row in self.account_features if row.enabled
                )
            return _Result(self.account_features)
        return _Result([])

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None


def _write_plugin(path: Path, requires: list[str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    rendered = repr(requires)
    (path / "manifest.py").write_text(
        "from app.worker.plugins.manifest import Manifest\n"
        f"MANIFEST = Manifest(key={path.name!r}, display_name='Demo', "
        f"requires_platform_capabilities={rendered})\n",
        encoding="utf-8",
    )
    (path / "plugin.json").write_text(
        '{"requires_platform_capabilities": ' + rendered.replace("'", '"') + "}",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    caps._reset_for_tests()
    runtime_profile_service._reset_for_tests()
    yield
    runtime_profile_service._reset_for_tests()
    caps._reset_for_tests()


@pytest.mark.asyncio
async def test_enabling_lottery_plus_auto_enables_two_branches_and_audits(
    tmp_path: Path, monkeypatch
) -> None:
    plugin_dir = tmp_path / "lottery_plus"
    _write_plugin(plugin_dir, ["interaction_bot", "ledger"])
    row = InstalledPlugin(
        key="lottery_plus",
        source="zip",
        installed_path=str(plugin_dir),
        version="1.0.0",
        enabled=False,
        signature_ok=True,
        trust_tier="verified",
    )
    db = _TreeDB(
        installed=[row],
        settings={
            "interaction_bot_enabled": {"enabled": False, "generation": 0},
            "ledger_enabled": {"enabled": False, "generation": 0},
        },
    )
    audit_write = AsyncMock()
    monkeypatch.setattr(caps, "_apply_local_transition", AsyncMock())
    monkeypatch.setattr(
        caps,
        "_broadcast_reload_config",
        AsyncMock(
            return_value={
                "total_accounts": 0,
                "notified": 0,
                "acked": 0,
                "pending": 0,
                "offline_or_timeout": 0,
                "last_broadcast_at": None,
                "notes": [],
            }
        ),
    )
    monkeypatch.setattr("app.services.audit.write", audit_write)

    await plugin_install_service.set_enabled(
        db, "lottery_plus", True, triggered_by_user_id=42  # type: ignore[arg-type]
    )

    assert row.enabled is True
    assert db.settings["interaction_bot_enabled"].value["enabled"] is True
    assert db.settings["ledger_enabled"].value["enabled"] is True
    assert [call.args[2] for call in audit_write.await_args_list] == [
        "platform_capability.auto_enable",
        "platform_capability.auto_enable",
    ]
    assert [call.kwargs["detail"]["message"] for call in audit_write.await_args_list] == [
        "因插件 lottery_plus 需要，自动启用模块 interaction_bot",
        "因插件 lottery_plus 需要，自动启用模块 ledger",
    ]
    assert all(
        call.kwargs["detail"]["triggered_by_user_id"] == 42
        for call in audit_write.await_args_list
    )
    caps._broadcast_reload_config.assert_not_awaited()
    caps._apply_local_transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_branch_auto_enable_and_audit_commit_atomically(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SystemSetting.__table__.create)
        await conn.execute(
            text(
                "CREATE TABLE audit_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "user_id BIGINT NULL, action VARCHAR NOT NULL, "
                "target VARCHAR NULL, detail JSON NULL"
                ")"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    broadcast = AsyncMock(return_value={})
    apply_local = AsyncMock()
    monkeypatch.setattr(caps, "_broadcast_reload_config", broadcast)
    monkeypatch.setattr(caps, "_apply_local_transition", apply_local)
    monkeypatch.setattr(
        "app.services.plugin_capability_requirements.get_plugin_capability_requirement",
        AsyncMock(
            return_value=SimpleNamespace(
                participates_in_demand=True,
                requires=("interaction_bot", "ledger"),
            )
        ),
    )

    try:
        async with factory() as db:
            db.add_all(
                [
                    SystemSetting(
                        key="interaction_bot_enabled",
                        value={"enabled": False, "generation": 0},
                    ),
                    SystemSetting(
                        key="ledger_enabled",
                        value={"enabled": False, "generation": 0},
                    ),
                ]
            )
            await db.commit()
            await caps.bootstrap_from_db(db)

            opened = await caps.ensure_plugin_capabilities(
                db, "lottery_plus", triggered_by_user_id=42
            )

            assert opened == ["interaction_bot", "ledger"]
            assert caps.is_module_enabled_cached(
                "interaction_bot", fail_closed=True
            ) is False
            assert caps.is_module_enabled_cached("ledger", fail_closed=True) is False
            broadcast.assert_not_awaited()
            apply_local.assert_not_awaited()

            profile_lock_acquired = asyncio.Event()

            async def wait_for_profile_lock() -> None:
                async with runtime_profile_service._PROFILE_LOCK:
                    profile_lock_acquired.set()

            profile_waiter = asyncio.create_task(wait_for_profile_lock())
            await asyncio.sleep(0)
            assert profile_lock_acquired.is_set() is False
            await db.commit()
            await asyncio.wait_for(profile_lock_acquired.wait(), timeout=1)
            await profile_waiter

        while caps._CAPABILITY_FINALIZER_TASKS:
            await asyncio.gather(*tuple(caps._CAPABILITY_FINALIZER_TASKS))

        async with factory() as observer:
            settings = {
                row.key: row.value
                for row in (await observer.scalars(select(SystemSetting))).all()
            }
            audits = (
                await observer.scalars(
                    select(AuditLog).order_by(AuditLog.id.asc())
                )
            ).all()

        assert settings["interaction_bot_enabled"]["enabled"] is True
        assert settings["ledger_enabled"]["enabled"] is True
        assert [row.detail["message"] for row in audits] == [
            "因插件 lottery_plus 需要，自动启用模块 interaction_bot",
            "因插件 lottery_plus 需要，自动启用模块 ledger",
        ]
        assert all(row.user_id == 42 for row in audits)
        assert caps.is_module_enabled_cached(
            "interaction_bot", fail_closed=True
        ) is True
        assert caps.is_module_enabled_cached("ledger", fail_closed=True) is True
        assert broadcast.await_count == 2
        assert apply_local.await_count == 2
    finally:
        while caps._CAPABILITY_FINALIZER_TASKS:
            await asyncio.gather(*tuple(caps._CAPABILITY_FINALIZER_TASKS))
        await engine.dispose()


@pytest.mark.asyncio
async def test_safe_watch_rejects_enabling_game_plugin(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "lottery_plus"
    _write_plugin(plugin_dir, ["interaction_bot", "ledger"])
    row = InstalledPlugin(
        key="lottery_plus",
        source="zip",
        installed_path=str(plugin_dir),
        version="1.0.0",
        enabled=False,
        signature_ok=True,
        trust_tier="verified",
    )
    db = _TreeDB(
        installed=[row],
        settings={
            runtime_profile_service.RUNTIME_PROFILE_STATE_KEY: {
                "active_profile": "safe_watch",
                "status": "active",
            }
        },
    )

    with pytest.raises(caps.PluginCapabilityBlocked) as exc:
        await plugin_install_service.set_enabled(db, "lottery_plus", True)  # type: ignore[arg-type]

    assert exc.value.error_code == "PLUGIN_CAPABILITY_FORCED_OFF"
    assert exc.value.reason == "值守预设"
    assert "Interaction Bot" in str(exc.value)
    assert row.enabled is False


@pytest.mark.asyncio
async def test_forced_off_preflight_blocks_without_partially_opening_other_branch(
    tmp_path: Path, monkeypatch
) -> None:
    plugin_dir = tmp_path / "lottery_plus"
    _write_plugin(plugin_dir, ["interaction_bot", "ledger"])
    row = InstalledPlugin(
        key="lottery_plus",
        source="zip",
        installed_path=str(plugin_dir),
        version="1.0.0",
        enabled=False,
        signature_ok=True,
        trust_tier="verified",
    )
    db = _TreeDB(
        installed=[row],
        settings={
            "interaction_bot_enabled": {"enabled": False, "generation": 0},
            "ledger_enabled": {
                "enabled": False,
                "generation": 3,
                "forced_off": True,
            },
        },
    )
    set_enabled = AsyncMock()
    monkeypatch.setattr(caps, "set_module_enabled", set_enabled)

    with pytest.raises(caps.PluginCapabilityBlocked) as exc:
        await plugin_install_service.set_enabled(db, "lottery_plus", True)  # type: ignore[arg-type]

    assert exc.value.reason == "管理员强制"
    set_enabled.assert_not_awaited()
    assert row.enabled is False


@pytest.mark.asyncio
async def test_forced_off_rechecked_inside_switch_lock_blocks_racing_auto_enable(
    tmp_path: Path, monkeypatch
) -> None:
    plugin_dir = tmp_path / "game"
    _write_plugin(plugin_dir, ["interaction_bot"])
    row = InstalledPlugin(
        key="game",
        source="zip",
        installed_path=str(plugin_dir),
        version="1.0.0",
        enabled=False,
    )
    db = _TreeDB(
        installed=[row],
        settings={
            "interaction_bot_enabled": {
                "enabled": False,
                "generation": 0,
                "forced_off": False,
            }
        },
    )
    read_control = AsyncMock(
        side_effect=[
            (False, 0, False),  # 完整预检时尚未被管理员关闭
            (False, 1, True),  # 进入模块切换锁后观察到最新修枝剪
        ]
    )
    audit_write = AsyncMock()
    monkeypatch.setattr(caps, "read_module_control", read_control)
    monkeypatch.setattr("app.services.audit.write", audit_write)

    with pytest.raises(caps.PluginCapabilityBlocked) as exc:
        await plugin_install_service.set_enabled(
            db, "game", True, triggered_by_user_id=42  # type: ignore[arg-type]
        )

    assert exc.value.reason == "管理员强制"
    assert row.enabled is False
    assert db.settings["interaction_bot_enabled"].value["enabled"] is False
    audit_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_only_tree_keeps_ledger_off_rejects_api_and_marks_source_missing(
    tmp_path: Path, monkeypatch
) -> None:
    sum_dir = tmp_path / "sum"
    _write_plugin(sum_dir, ["ai"])
    codex_dir = tmp_path / "codex_image"
    (codex_dir / "__pycache__").mkdir(parents=True)
    sum_row = InstalledPlugin(
        key="sum", source="local", installed_path=str(sum_dir), version="1", enabled=True
    )
    codex_row = InstalledPlugin(
        key="codex_image",
        source="repo",
        installed_path=str(codex_dir),
        version="1",
        enabled=True,
    )
    db = _TreeDB(
        installed=[sum_row, codex_row],
        account_features=[
            AccountFeature(account_id=1, feature_key="sum", enabled=True),
            AccountFeature(account_id=1, feature_key="codex_image", enabled=True),
        ],
        settings={
            "ai_enabled": {"enabled": True, "generation": 0},
            "interaction_bot_enabled": {"enabled": False, "generation": 1},
            "webhooks_enabled": {"enabled": True, "generation": 0},
            "ledger_enabled": {"enabled": False, "generation": 1},
            "dispatch_debug_enabled": {"enabled": True, "generation": 0},
        },
    )
    monkeypatch.setattr(tree_service, "list_builtin_capability_requirements", lambda: [])
    monkeypatch.setattr(tree_service, "get_worker_runtime_snapshot", lambda: [])
    monkeypatch.setattr(
        tree_service.runtime_profile_service,
        "get_status",
        AsyncMock(return_value={"current_profile": "safe_watch"}),
    )
    monkeypatch.setattr(
        tree_service.kill_switch_service, "get_enabled", AsyncMock(return_value=False)
    )

    tree = await tree_service.build_platform_tree(db)  # type: ignore[arg-type]

    assert tree["trunk"]["current_profile"] == "safe_watch"
    assert tree["branches"]["ledger"]["desired"] is False
    assert tree["branches"]["ledger"]["demanded_by"] == []
    assert tree["branches"]["webhooks"]["can_turn_off"] is True
    assert db.settings["webhooks_enabled"].value["enabled"] is True
    leaf_by_key = {leaf["key"]: leaf for leaf in tree["leaves"]}
    assert leaf_by_key["sum"]["requires"] == ["ai"]
    assert leaf_by_key["codex_image"]["source_missing"] is True
    assert leaf_by_key["codex_image"]["warnings"] == [SOURCE_MISSING_WARNING]
    assert "codex_image" not in {
        key
        for branch in tree["branches"].values()
        for key in branch["demanded_by"]
    }

    with pytest.raises(HTTPException) as exc:
        await ledger_api._require_ledger_module(db)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PLATFORM_MODULE_DISABLED"
