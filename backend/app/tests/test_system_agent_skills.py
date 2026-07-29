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
from app.services.system_agent.tool_routing import (
    ToolRoute,
    route_locally,
    select_tool_specs,
    tool_domain,
)


async def _read_handler(_ctx, _args):  # noqa: ANN001
    return {"ok": True}


async def _write_handler(_ctx, _args):  # noqa: ANN001
    return {"ok": True}


def _spec(
    name: str,
    *,
    read_only: bool = True,
    diagnostic_safe: bool = False,
) -> ToolSpec:
    if not read_only:
        return ToolSpec(
            name=name,
            description=name,
            input_schema={"type": "object", "properties": {}},
            read_only=False,
            diagnostic_safe=diagnostic_safe,
            preview_handler=_write_handler,
            execute_handler=_write_handler,
        )
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
    ("text", "available", "expected_skills"),
    [
        ("交互里有哪些规则", {"interaction", "rules"}, ("interaction",)),
        ("列出所有账号", {"accounts", "logs"}, ("accounts",)),
        ("给账号启用这个插件", {"accounts", "features", "plugins"}, ("features",)),
        ("列出通用 Rule", {"rules", "interaction"}, ("rules",)),
        ("看看今晚的定时任务", {"scheduler", "logs"}, ("scheduler",)),
        ("今天收入多少", {"ledger", "logs"}, ("ledger",)),
        ("你记住了我什么", {"memory", "system"}, ("memory",)),
        (
            "列出 Provider 和自定义指令",
            {"providers", "commands"},
            ("commands", "ai-config"),
        ),
        ("检查已安装插件更新", {"plugins", "plugin_repos"}, ("plugins",)),
        ("浏览官方插件仓库", {"plugins", "plugin_repos"}, ("plugin-catalog",)),
        ("看看最近错误日志", {"logs", "system"}, ("diagnostics",)),
        ("联网查一下官方文档", {"web", "logs"}, ("web-research",)),
    ],
)
def test_builtin_skill_selected_for_domain_request(
    text: str,
    available: set[str],
    expected_skills: tuple[str, ...],
) -> None:
    route = route_locally(text, available=available)

    assert route is not None
    assert tuple(skill.name for skill in _skill_registry().select(route)) == expected_skills


def test_builtin_skill_metadata_and_tools_stay_concise() -> None:
    registry = _skill_registry()

    assert {skill.name for skill in registry.list_all()} >= {
        "interaction",
        "accounts",
        "account-bots",
        "platform-capabilities",
        "system-settings",
        "access-control",
        "connectivity",
        "device-profiles",
        "features",
        "safety-controls",
        "rules",
        "scheduler",
        "ledger",
        "memory",
        "message-templates",
        "notifications",
        "ai-config",
        "llm-usage",
        "commands",
        "config-bundles",
        "routing",
        "dispatch-debug",
        "plugins",
        "plugin-catalog",
        "system-operations",
        "diagnostics",
        "web-research",
        "webhooks",
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


def test_plugin_install_and_account_enable_starts_with_install_only() -> None:
    route = route_locally(
        "从官方库安装这个插件并给账号启用",
        available={"accounts", "features", "plugins", "plugin_repos"},
    )

    assert route is not None
    assert route.domains[0] == "plugin_repos"
    assert [skill.name for skill in _skill_registry().select(route)] == ["plugin-catalog"]

    specs = get_registry().list_all()
    narrowed = _skill_registry().narrow_tools(select_tool_specs(specs, route), _skill_registry().select(route))
    names = {spec.name for spec in narrowed}
    assert "features.set_enabled" not in names
    assert "plugin_repos.install_plugin" in names


def test_saved_plugin_repo_install_and_account_enable_keeps_both_workflows() -> None:
    route = route_locally(
        "从插件仓库安装这个插件并给账号启用",
        available={"accounts", "features", "plugins", "plugin_repos"},
    )

    assert route is not None
    assert route.domains == ("plugin_repos",)
    assert [skill.name for skill in _skill_registry().select(route)] == ["plugin-catalog"]


def test_plugin_debug_gets_state_logs_and_source_but_no_write_tools() -> None:
    route = route_locally(
        "Debug payment-helper 插件为什么报错，并报告修复方式",
        available={"plugins", "logs", "source"},
    )

    assert route == ToolRoute(("plugins", "logs", "source"), "local", "keyword_match")
    registry = _skill_registry()
    selected = registry.select(route)
    assert [skill.name for skill in selected] == ["plugins", "diagnostics"]

    narrowed = registry.narrow_tools(
        select_tool_specs(get_registry().list_all(), route),
        selected,
    )
    names = {spec.name for spec in narrowed}
    assert {
        "plugins.list_installed",
        "plugins.get",
        "logs.system_console",
        "logs.recent",
        "logs.search_errors",
        "source.search",
        "source.read",
    }.issubset(names)
    assert not {"plugins.install", "plugins.update", "plugins.uninstall"}.intersection(names)


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


def test_skill_tools_never_exceed_route_permission_or_sixteen() -> None:
    registry = _skill_registry()
    all_specs = [
        _spec(name)
        for name in (
            "providers.list",
            "providers.probe_and_add",
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

    assert len(narrowed) == 9
    assert {spec.name for spec in narrowed}.issubset({spec.name for spec in routed})
    assert "plugins.install" not in {spec.name for spec in narrowed}
    assert "providers.list" in {spec.name for spec in narrowed}
    assert "providers.probe_and_add" in {spec.name for spec in narrowed}
    assert "providers.save" in {spec.name for spec in narrowed}
    assert "providers.delete" in {spec.name for spec in narrowed}
    assert "commands.list" in {spec.name for spec in narrowed}
    assert "commands.save" in {spec.name for spec in narrowed}
    assert "commands.delete" in {spec.name for spec in narrowed}


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
            "logs.system_console",
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

    assert len(names) == 12
    assert names[:4] == [
        "scheduler.list",
        "logs.system_console",
        "scheduler.save",
        "logs.recent",
    ]


@pytest.mark.parametrize(
    ("text", "expected_tool"),
    [
        ("临时调严账号 1 的风控", "rate_limits.set_strict"),
        ("重启账号1的管理bot", "account_bots.restart"),
        ("运行插件配置动作", "features.run_config_action"),
        ("添加忽略用户123", "ignored.add"),
        ("忽略账号1里的用户123", "ignored.add"),
        ("检查系统更新", "system.check_update"),
        ("重启系统", "system.restart"),
    ],
)
def test_advertised_workflow_keeps_target_tool_reachable(
    text: str,
    expected_tool: str,
) -> None:
    specs = get_registry().list_all()
    available = {tool_domain(spec) for spec in specs}
    route = route_locally(text, available=available)

    assert route is not None
    selected = _skill_registry().select(route)
    narrowed = _skill_registry().narrow_tools(
        select_tool_specs(specs, route), selected
    )
    assert expected_tool in {spec.name for spec in narrowed}


def test_provider_500_diagnostics_keeps_console_source_and_provider_tools() -> None:
    registry = _skill_registry()
    specs = [
        _spec(name)
        for name in (
            "logs.system_console",
            "logs.recent",
            "source.search",
            "source.read",
            "system.get_health",
            "providers.list",
            "providers.verify",
            "providers.save",
        )
    ]
    specs[-2] = _spec("providers.verify", read_only=False, diagnostic_safe=True)
    specs[-1] = _spec("providers.save", read_only=False)
    route = route_locally(
        "排查 Provider 保存时报服务器内部错误",
        available={"logs", "source", "system", "providers"},
    )

    assert route is not None
    selected = registry.select(route)
    narrowed = registry.narrow_tools(select_tool_specs(specs, route), selected)
    names = {spec.name for spec in narrowed}

    assert {
        "logs.system_console",
        "source.search",
        "source.read",
        "providers.list",
        "providers.verify",
    } <= names
    assert "providers.save" not in names
    assert "providers.delete" not in names


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
