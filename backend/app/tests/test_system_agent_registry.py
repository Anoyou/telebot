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
async def test_stage1_registers_read_only_tools_only() -> None:
    reset_registry_for_tests()
    reg = get_registry()
    tools = reg.list_all()
    assert len(tools) >= 15
    assert all(t.read_only for t in tools)
    names = {t.name for t in tools}
    expected = {
        "system.get_context",
        "system.get_health",
        "accounts.list",
        "accounts.get",
        "interaction.list_rules",
        "interaction.get_rule",
        "interaction.list_active_sessions",
        "rules.list",
        "rules.get",
        "scheduler.list",
        "scheduler.get",
        "providers.list",
        "commands.list",
        "features.get_account_status",
        "logs.recent",
        "logs.search_errors",
        "logs.get_event_detail",
        "ledger.summary",
        "ledger.list",
    }
    assert expected.issubset(names)
    # 阶段 1 不得注册写工具
    assert not any(n.endswith(".save") or n.endswith(".delete") for n in names)


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
