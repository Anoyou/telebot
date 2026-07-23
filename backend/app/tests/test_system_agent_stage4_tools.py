"""阶段 4 扩展工具：注册表与关键 handler 形状。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import get_registry, reset_registry_for_tests
from app.services.system_agent.tools import plugins, routing, scheduler


@pytest.fixture(autouse=True)
def _reset_reg():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def test_stage4_tools_registered() -> None:
    reg = get_registry()
    names = {t.name for t in reg.list_all()}
    expected = {
        "plugins.list_installed",
        "plugins.install",
        "plugins.update",
        "plugins.uninstall",
        "plugins.set_package_enabled",
        "plugin_repos.list",
        "plugin_repos.create",
        "plugin_repos.install_plugin",
        "system.check_update",
        "system.apply_update",
        "system.restart",
        "product.get_changelog",
        "routing.list_ai_commands",
        "routing.preview",
        "routing.set_command_mode",
    }
    assert expected.issubset(names)
    # 危险写工具必须有 preview/execute
    for name in (
        "plugins.install",
        "plugins.uninstall",
        "system.apply_update",
        "system.restart",
        "plugin_repos.install_plugin",
    ):
        spec = reg.get(name)
        assert spec is not None
        assert not spec.read_only
        assert spec.risk == "dangerous"
        assert spec.preview_handler and spec.execute_handler
    assert reg.get("system.apply_update").runtime_effects == ("system_apply_update",)
    assert reg.get("system.restart").runtime_effects == ("system_restart",)
    assert reg.get("plugins.update").runtime_effects == ("plugin_update",)
    assert reg.get("scheduler.execute_now").runtime_effects == ("scheduler_execute_now",)


@pytest.mark.asyncio
async def test_plugins_list_uses_service(monkeypatch) -> None:
    class _Row:
        name = "demo"
        display_name = "Demo"
        version = "1.0.0"
        enabled = True
        source_url = "https://example.com/a.git"
        update_available = False
        latest_version = "1.0.0"
        description = "d"

    async def fake_list(db):  # noqa: ANN001
        return [_Row()]

    monkeypatch.setattr(
        "app.services.remote_plugin_service.list_installed",
        fake_list,
    )
    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin")
    out = await plugins.list_installed(ctx, {})
    assert out["count"] == 1
    assert out["plugins"][0]["name"] == "demo"


@pytest.mark.asyncio
async def test_plugin_update_defers_filesystem_change_until_runtime_sync(monkeypatch) -> None:
    row = type("Plugin", (), {"name": "demo", "version": "1.0.0"})()
    get_mock = AsyncMock(return_value=row)
    update_mock = AsyncMock()
    monkeypatch.setattr("app.services.remote_plugin_service.get_by_name", get_mock)
    monkeypatch.setattr("app.services.remote_plugin_service.update", update_mock)
    action = type("Action", (), {"arguments": {}})()
    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin", action=action)

    out = await plugins.update_execute(ctx, {"name": "demo"})

    assert out["requested"] is True
    assert out["business_changed"] is False
    update_mock.assert_not_awaited()
    assert action.arguments["plugin_name"] == "demo"


@pytest.mark.asyncio
async def test_scheduler_execute_now_does_not_write_unused_config_marker() -> None:
    row = type(
        "Rule",
        (),
        {"id": 9, "feature_key": "scheduler", "account_id": 7, "config": {"cron": "0 1 * * *"}},
    )()
    db = AsyncMock()
    db.get = AsyncMock(return_value=row)
    ctx = ToolContext(db=db, channel="web", role="admin", account_id=7)

    out = await scheduler.execute_now_execute(ctx, {"rule_id": 9})

    assert out["requested"] is True
    assert row.config == {"cron": "0 1 * * *"}


@pytest.mark.asyncio
async def test_uninstall_execute_raises_when_not_deleted(monkeypatch) -> None:
    async def fake_uninstall(db, name, *, remove_files=True):  # noqa: ANN001
        return False

    monkeypatch.setattr(
        "app.services.remote_plugin_service.uninstall",
        fake_uninstall,
    )
    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin")
    with pytest.raises(ValueError, match="不可卸载"):
        await plugins.uninstall_execute(ctx, {"name": "missing-plugin"})


@pytest.mark.asyncio
async def test_delete_repo_raises_when_not_found(monkeypatch) -> None:
    from app.services.system_agent.tools import plugin_repos

    async def fake_delete(db, repo_id):  # noqa: ANN001
        return False

    monkeypatch.setattr(
        "app.services.plugin_repo_service.delete_repo",
        fake_delete,
    )
    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin")
    with pytest.raises(ValueError, match="不存在或已删除"):
        await plugin_repos.delete_repo_execute(ctx, {"repo_id": 99})


@pytest.mark.asyncio
async def test_routing_set_mode_preview_requires_ai(monkeypatch) -> None:
    class _Tpl:
        id = 3
        name = "sum"
        type = "reply_text"
        config = {}

    db = AsyncMock()
    db.get = AsyncMock(return_value=_Tpl())
    ctx = ToolContext(db=db, channel="web", role="admin")
    with pytest.raises(ValueError, match="type=ai"):
        await routing.set_routing_mode_preview(
            ctx, {"template_id": 3, "routing_mode": "auto"}
        )
