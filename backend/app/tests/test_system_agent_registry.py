"""System Agent 工具注册表与角色/渠道过滤。"""

from __future__ import annotations

import pytest

from app.services.system_agent.registry import (
    ToolRegistry,
    ToolSpec,
    get_registry,
    reset_registry_for_tests,
    role_at_least,
)


def test_role_at_least_ordering() -> None:
    assert role_at_least("admin", "viewer")
    assert role_at_least("operator", "viewer")
    assert role_at_least("viewer", "viewer")
    assert not role_at_least("viewer", "operator")
    assert not role_at_least("operator", "admin")
    assert not role_at_least("", "viewer")


def test_registry_requires_handlers() -> None:
    reg = ToolRegistry()
    with pytest.raises(ValueError, match="read_handler"):
        reg.register(
            ToolSpec(
                name="bad.read",
                description="x",
                input_schema={"type": "object"},
                read_only=True,
            )
        )
    with pytest.raises(ValueError, match="preview and execute"):
        reg.register(
            ToolSpec(
                name="bad.write",
                description="x",
                input_schema={"type": "object"},
                read_only=False,
            )
        )


@pytest.mark.asyncio
async def test_registry_includes_read_and_write_tools() -> None:
    reset_registry_for_tests()
    reg = get_registry()
    tools = reg.list_all()
    assert len(tools) >= 25
    names = {t.name for t in tools}
    expected_read = {
        "system.get_context",
        "system.get_health",
        "accounts.list",
        "accounts.get",
        "interaction.list_rules",
        "rules.list",
        "scheduler.list",
        "ledger.summary",
        "source.search",
        "source.read",
        "web.search",
        "web.read",
    }
    expected_write = {
        "accounts.set_paused",
        "accounts.restart_worker",
        "rules.save",
        "rules.set_enabled",
        "rules.delete",
        "interaction.save_rule",
        "interaction.set_enabled",
        "interaction.delete_rule",
        "scheduler.save",
        "scheduler.set_enabled",
        "scheduler.delete",
        "scheduler.execute_now",
        "features.set_enabled",
        "providers.save",
        "providers.delete",
        "providers.verify",
        "commands.save",
        "commands.delete",
        "commands.set_enabled_for_accounts",
    }
    assert expected_read.issubset(names)
    assert expected_write.issubset(names)
    write_tools = [t for t in tools if not t.read_only]
    assert write_tools
    assert all(t.preview_handler and t.execute_handler for t in write_tools)


def test_list_for_filters_role_and_channel() -> None:
    async def _handler(_ctx, _args):  # noqa: ANN001
        return {}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="web.only",
            description="web",
            input_schema={"type": "object"},
            read_only=True,
            min_role="admin",
            channels=("web",),
            read_handler=_handler,
        )
    )
    reg.register(
        ToolSpec(
            name="bot.viewer",
            description="bot",
            input_schema={"type": "object"},
            read_only=True,
            min_role="viewer",
            channels=("bot", "web"),
            read_handler=_handler,
        )
    )
    web_admin = {t.name for t in reg.list_for(channel="web", role="admin")}
    bot_viewer = {t.name for t in reg.list_for(channel="bot", role="viewer")}
    web_viewer = {t.name for t in reg.list_for(channel="web", role="viewer")}
    assert web_admin == {"web.only", "bot.viewer"}
    assert bot_viewer == {"bot.viewer"}
    assert web_viewer == {"bot.viewer"}


def test_capabilities_marks_unavailable() -> None:
    async def _handler(_ctx, _args):  # noqa: ANN001
        return {}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="admin.tool",
            description="admin",
            input_schema={"type": "object"},
            read_only=True,
            min_role="admin",
            channels=("web",),
            read_handler=_handler,
        )
    )
    caps = reg.capabilities(channel="bot", role="viewer")
    assert len(caps) == 1
    assert caps[0]["available"] is False
    assert caps[0]["unavailable_reason"]
