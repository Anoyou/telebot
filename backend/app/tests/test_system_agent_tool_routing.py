from __future__ import annotations

from app.services.system_agent.registry import ToolSpec
from app.services.system_agent.tool_routing import (
    ToolRoute,
    parse_model_route,
    route_locally,
    select_tool_specs,
)


def _spec(name: str) -> ToolSpec:
    async def read_handler(_ctx, _args):  # noqa: ANN001
        return {}

    return ToolSpec(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        read_handler=read_handler,
    )


def test_general_question_uses_no_tools() -> None:
    route = route_locally(
        "你能做什么？",
        available={"scheduler", "logs"},
    )

    assert route == ToolRoute((), "local", "general_help")


def test_changelog_request_uses_product_domain() -> None:
    route = route_locally(
        "看看更新日志",
        available={"logs", "system", "product"},
    )

    assert route == ToolRoute(("product",), "local", "product_changelog")


def test_scheduler_request_only_exposes_scheduler_tools() -> None:
    specs = [_spec("scheduler.list"), _spec("logs.recent"), _spec("rules.list")]
    route = route_locally(
        "帮我看看今晚的定时任务",
        available={"scheduler", "logs", "rules"},
    )

    assert route is not None
    assert route.domains == ("scheduler",)
    assert [item.name for item in select_tool_specs(specs, route)] == ["scheduler.list"]


def test_reference_reuses_last_memory_domain() -> None:
    route = route_locally(
        "把它停掉",
        available={"interaction", "scheduler"},
        memory_state={"last_domains": ["interaction"]},
    )

    assert route == ToolRoute(
        ("interaction",),
        "memory",
        "reference_to_previous_domain",
    )


def test_model_route_parses_json_and_limits_to_three_domains() -> None:
    route = parse_model_route(
        '说明文字 {"needs_tools":true,"domains":["logs","ledger","system","rules","logs"],"reason":"query"}',
        available={"logs", "ledger", "system", "rules"},
    )

    assert route is not None
    assert route.domains == ("logs", "ledger", "system")
    assert route.source == "model"


def test_model_route_rejects_unknown_only_selection() -> None:
    route = parse_model_route(
        '{"needs_tools":true,"domains":["unknown"]}',
        available={"logs"},
    )

    assert route is None


def test_provider_and_command_request_keeps_both_routed_domains() -> None:
    route = route_locally(
        "列出 Provider 和自定义指令",
        available={"providers", "commands", "routing", "logs"},
    )

    assert route is not None
    assert route.domains == ("commands", "providers")


def test_log_diagnostics_does_not_expose_unrelated_tools() -> None:
    specs = [
        _spec("logs.recent"),
        _spec("system.get_health"),
        _spec("providers.list"),
    ]
    route = route_locally(
        "看看最近错误日志",
        available={"logs", "system", "providers"},
    )

    assert route is not None
    assert [item.name for item in select_tool_specs(specs, route)] == ["logs.recent"]
