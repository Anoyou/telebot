from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.llm_agent import AgentResult
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_protocol import ModelUsage, StopReason
from app.services.system_agent import runtime as runtime_module
from app.services.system_agent.config import ResolvedAgentProviders
from app.services.system_agent.registry import ToolRegistry, ToolSpec, get_registry
from app.services.system_agent.runtime import SystemAgentRuntime
from app.services.system_agent.skills import BUILTIN_SKILLS, SkillRegistry
from app.services.system_agent.tool_routing import ToolRoute, route_locally, select_tool_specs


async def _read_handler(_ctx, _args):  # noqa: ANN001
    return {"ok": True}


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        read_handler=_read_handler,
    )


def _skill_registry() -> SkillRegistry:
    return SkillRegistry(BUILTIN_SKILLS)


async def _accept_provider_capabilities(_db, resolved, **_kwargs):  # noqa: ANN001
    return resolved


@pytest.mark.parametrize(
    ("text", "available", "expected_skill"),
    [
        ("交互里有哪些规则", {"interaction", "rules"}, "interaction"),
        ("看看今晚的定时任务", {"scheduler", "logs"}, "scheduler"),
        ("列出 Provider 和自定义指令", {"providers", "commands"}, "ai-config"),
        ("检查已安装插件更新", {"plugins", "plugin_repos"}, "plugins"),
        ("看看最近错误日志", {"logs", "system"}, "diagnostics"),
        ("联网查一下官方文档", {"web", "logs"}, "web-research"),
    ],
)
def test_builtin_skill_selected_for_domain_request(
    text: str,
    available: set[str],
    expected_skill: str,
) -> None:
    route = route_locally(text, available=available)

    assert route is not None
    assert [skill.name for skill in _skill_registry().select(route)] == [expected_skill]


def test_builtin_skill_metadata_and_tools_stay_concise() -> None:
    registry = _skill_registry()

    assert {skill.name for skill in registry.list_all()} >= {
        "interaction",
        "scheduler",
        "ai-config",
        "plugins",
        "diagnostics",
        "web-research",
    }
    for skill in registry.list_all():
        assert skill.description
        assert skill.domains
        assert skill.allowed_tools
        assert skill.instructions
        assert skill.examples
        assert skill.required_context
        assert "properties" not in skill.render_prompt()


def test_builtin_skills_only_reference_registered_tools() -> None:
    registered = {spec.name for spec in get_registry().list_all()}

    for skill in BUILTIN_SKILLS:
        assert set(skill.allowed_tools).issubset(registered)


def test_composite_route_loads_at_most_two_skills() -> None:
    selected = _skill_registry().select(
        ToolRoute(("scheduler", "logs", "providers"), "model", "compound")
    )

    assert [skill.name for skill in selected] == ["scheduler", "diagnostics"]
    assert len(selected) == 2


def test_general_question_loads_no_skill_and_no_tool() -> None:
    route = route_locally("你能做什么？", available={"scheduler", "logs"})
    registry = _skill_registry()

    assert route == ToolRoute((), "local", "general_help")
    assert registry.select(route) == ()
    assert registry.narrow_tools([], ()) == []
    assert registry.render_prompt(()) == ""


def test_skill_tools_never_exceed_route_permission_or_eight() -> None:
    registry = _skill_registry()
    all_specs = [
        _spec(name)
        for name in (
            "providers.list",
            "providers.save",
            "providers.verify",
            "providers.delete",
            "commands.list",
            "commands.save",
            "commands.set_enabled_for_accounts",
            "commands.delete",
            "routing.list_ai_commands",
            "routing.set_command_mode",
            "routing.preview",
            "plugins.install",
        )
    ]
    route = ToolRoute(("providers", "commands", "routing"), "model", "ai config")
    routed = select_tool_specs(all_specs, route)
    selected = registry.select(route)
    narrowed = registry.narrow_tools(routed, selected)

    assert len(narrowed) == 8
    assert {spec.name for spec in narrowed}.issubset({spec.name for spec in routed})
    assert "plugins.install" not in {spec.name for spec in narrowed}
    assert "providers.list" in {spec.name for spec in narrowed}
    assert "providers.save" in {spec.name for spec in narrowed}
    assert "commands.list" in {spec.name for spec in narrowed}
    assert "commands.save" in {spec.name for spec in narrowed}


def test_two_skills_receive_stable_key_tools() -> None:
    registry = _skill_registry()
    specs = [
        _spec(name)
        for name in (
            "scheduler.list",
            "scheduler.save",
            "scheduler.get",
            "scheduler.set_enabled",
            "scheduler.execute_now",
            "scheduler.delete",
            "logs.recent",
            "logs.search_errors",
            "logs.get_event_detail",
            "system.get_health",
            "system.get_context",
        )
    ]
    route = ToolRoute(("scheduler", "logs", "system"), "model", "compound")
    selected = registry.select(route)
    narrowed = registry.narrow_tools(select_tool_specs(specs, route), selected)
    names = [spec.name for spec in narrowed]

    assert len(names) == 8
    assert names[:4] == [
        "scheduler.list",
        "logs.recent",
        "scheduler.save",
        "logs.search_errors",
    ]


@pytest.mark.asyncio
async def test_runtime_emits_skill_event_and_injects_skill_prompt(monkeypatch) -> None:
    provider = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
        api_key_enc="encrypted",
    )

    async def load_flags(_db):  # noqa: ANN001
        return {
            "timezone": "UTC",
            "command_prefix": "/",
            "ai_enabled": True,
            "agent_config": {
                "enabled": True,
                "max_steps": 8,
                "max_tool_calls": 24,
                "session_token_limit": 16_384,
                "require_tool_approval": False,
            },
        }

    async def resolve(_db, _cfg):  # noqa: ANN001
        return ResolvedAgentProviders(
            primary=provider,
            model=provider.default_model,
            providers={provider.id: provider},
        )

    async def run(_model_call, request, tools, **_kwargs):  # noqa: ANN001
        prompt = request.messages[0].text_content()
        assert "## 当前按需领域技能" in prompt
        assert "### scheduler" in prompt
        assert "### diagnostics" not in prompt
        assert len(request.tools) <= 8
        assert list(tools) == ["scheduler.list", "scheduler.save"]
        return AgentResult(
            text="ok",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "load_system_context_flags", load_flags)
    monkeypatch.setattr(runtime_module, "resolve_agent_providers", resolve)
    monkeypatch.setattr(
        runtime_module,
        "verify_resolved_agent_providers",
        _accept_provider_capabilities,
    )
    monkeypatch.setattr(runtime_module, "run_agent", run)

    registry = ToolRegistry()
    registry.register(_spec("scheduler.list"))
    registry.register(_spec("scheduler.save"))
    session = SimpleNamespace(
        id="session-skills",
        account_id=None,
        memory_summary="",
        memory_state={},
    )
    events = [
        event
        async for event in SystemAgentRuntime(
            registry,
            _skill_registry(),
        ).stream_turn(
            None,  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
            user_text="看看今晚的定时任务",
            role="admin",
            channel="web",
        )
    ]

    selected = next(event for event in events if event["type"] == "skill_selected")
    assert selected["skills"] == ["scheduler"]
    assert selected["skill_names"] == ["scheduler"]
    assert "定时任务" in selected["understanding_summary"]
    assert selected["tool_count"] == 2


@pytest.mark.asyncio
async def test_runtime_general_question_does_not_emit_skill_event(monkeypatch) -> None:
    provider = LLMProviderDTO(
        id=1,
        name="primary",
        provider="openai",
        api_format="responses",
        default_model="primary-model",
        api_key_enc="encrypted",
    )

    async def load_flags(_db):  # noqa: ANN001
        return {
            "timezone": "UTC",
            "command_prefix": "/",
            "ai_enabled": True,
            "agent_config": {
                "enabled": True,
                "max_steps": 8,
                "max_tool_calls": 24,
                "session_token_limit": 16_384,
                "require_tool_approval": False,
            },
        }

    async def resolve(_db, _cfg):  # noqa: ANN001
        return ResolvedAgentProviders(
            primary=provider,
            model=provider.default_model,
            providers={provider.id: provider},
        )

    async def run(_model_call, request, tools, **_kwargs):  # noqa: ANN001
        assert "## 当前按需领域技能" not in request.messages[0].text_content()
        assert request.tools == ()
        assert tools == {}
        return AgentResult(
            text="ok",
            model=request.model,
            messages=request.messages,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            steps=1,
            tool_calls=0,
            stop_reason=StopReason.COMPLETED,
        )

    monkeypatch.setattr(runtime_module, "load_system_context_flags", load_flags)
    monkeypatch.setattr(runtime_module, "resolve_agent_providers", resolve)
    monkeypatch.setattr(
        runtime_module,
        "verify_resolved_agent_providers",
        _accept_provider_capabilities,
    )
    monkeypatch.setattr(runtime_module, "run_agent", run)

    registry = ToolRegistry()
    registry.register(_spec("scheduler.list"))
    session = SimpleNamespace(
        id="session-general",
        account_id=None,
        memory_summary="",
        memory_state={},
    )
    events = [
        event
        async for event in SystemAgentRuntime(
            registry,
            _skill_registry(),
        ).stream_turn(
            None,  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
            user_text="你能做什么？",
            role="admin",
            channel="web",
        )
    ]

    assert not any(event["type"] == "skill_selected" for event in events)
