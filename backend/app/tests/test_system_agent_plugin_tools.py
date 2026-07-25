"""插件工具插槽：manifest 解析与只读硬约束。"""

from __future__ import annotations

from app.services.system_agent.plugin_tools import (
    apply_exposed_tools_to_registry,
    exposed_tool_name,
    parse_exposed_tools,
)
from app.services.system_agent.registry import ToolRegistry, ToolSpec


def test_parse_exposed_tools_read_only_and_limit() -> None:
    warnings: list[str] = []
    tools = parse_exposed_tools(
        "lottery_plus",
        {
            "description": "彩票",
            "capabilities": {"agent_tools": {"enabled": True}},
            "agent_keywords": ["彩票", "开奖"],
            "agent_tools": [
                {
                    "name": "list_recent_rounds",
                    "description": "近期开奖",
                    "read_only": True,
                    "expose": ["system_agent"],
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "name": "place_bet",
                    "description": "下注",
                    "read_only": False,
                    "expose": ["system_agent"],
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "name": "no_expose",
                    "description": "不暴露",
                    "read_only": True,
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        },
        warnings=warnings,
    )
    assert len(tools) == 1
    assert tools[0]["full_name"] == "plugin_lottery_plus.list_recent_rounds"
    assert any("写语义" in w for w in warnings)


def test_apply_exposed_registers_dynamic_domain() -> None:
    reg = ToolRegistry()
    # seed a builtin so registry is valid
    async def _noop(_ctx, _args):  # noqa: ANN001
        return {}

    reg.register(
        ToolSpec(
            name="logs.list",
            description="logs",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            read_handler=_noop,
        )
    )
    apply_exposed_tools_to_registry(
        reg,
        [
            {
                "plugin_key": "demo_game",
                "tool_name": "list_recent_rounds",
                "full_name": exposed_tool_name("demo_game", "list_recent_rounds"),
                "description": "近期开奖",
                "parameters": {"type": "object", "properties": {}},
                "plugin_description": "演示插件",
                "agent_keywords": ["demo_game"],
            }
        ],
    )
    spec = reg.get("plugin_demo_game.list_recent_rounds")
    assert spec is not None
    assert spec.read_only is True
    caps = reg.capabilities(channel="web", role="admin")
    plugin_caps = [c for c in caps if c.get("source") == "plugin"]
    assert plugin_caps
    assert plugin_caps[0]["plugin_key"] == "demo_game"
