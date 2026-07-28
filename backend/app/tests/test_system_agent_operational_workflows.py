"""System Agent 扩展运维工作流的安全契约与注册覆盖。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.db.models.account import Account
from app.db.models.feature import AccountFeature, Feature
from app.db.models.system import SystemSetting
from app.services.system_agent.actions import (
    create_pending_action,
    decrypt_secret_payload,
)
from app.services.system_agent.context import ToolContext
from app.services.system_agent.registry import get_registry
from app.services.system_agent.tool_routing import route_locally
from app.services.system_agent.tools import access as access_tools
from app.services.system_agent.tools import account_bots as account_bot_tools
from app.services.system_agent.tools import accounts as account_tools
from app.services.system_agent.tools import config_bundles as bundle_tools
from app.services.system_agent.tools import dispatch as dispatch_tools
from app.services.system_agent.tools import features as feature_tools
from app.services.system_agent.tools import interaction as interaction_tools
from app.services.system_agent.tools import message_templates as message_template_tools
from app.services.system_agent.tools import safety as safety_tools
from app.services.system_agent.tools import system_settings as settings_tools
from app.services.system_agent.tools import usage as usage_tools
from app.services.system_agent.tools import webhooks as webhook_tools


def test_operational_workflows_are_registered_with_expected_safety_contracts() -> None:
    registry = get_registry()
    specs = {spec.name: spec for spec in registry.list_all()}

    expected = {
        "accounts.update",
        "accounts.clone_config",
        "features.get_config",
        "features.save_account_config",
        "features.save_global_config",
        "features.list_config_actions",
        "features.get_config_action_job",
        "features.run_config_action",
        "features.control_config_action_job",
        "interaction.get_config",
        "interaction.save_config",
        "account_bots.list_polling_dlq",
        "account_bots.replay_polling_dlq",
        "account_bots.discard_polling_dlq",
        "rate_limits.get_usage",
        "rate_limits.get_events",
        "rate_limits.estimate",
        "rate_limits.list_templates",
        "rate_limits.save_template",
        "rate_limits.delete_template",
        "rate_limits.save_template_rule",
        "rate_limits.set_strict",
        "rate_limits.drop_override",
        "rules.dry_run",
        "rules.copy",
        "plugin_repos.refresh",
        "plugin_repos.update_credential",
        "plugin_repos.update_installed",
    }
    assert expected.issubset(specs)
    assert specs["features.save_account_config"].secret_argument_names == ("config_json",)
    assert specs["features.save_global_config"].secret_argument_names == ("config_json",)
    assert specs["interaction.save_config"].secret_argument_names == ("config_json",)
    assert specs["features.save_global_config"].runtime_effects == (
        "reload_feature_accounts",
    )
    assert specs["features.run_config_action"].secret_argument_names == (
        "payload_json",
    )
    assert specs["interaction.save_config"].runtime_effects == (
        "reload_config",
        "interaction_bot_restart",
    )
    assert specs["rate_limits.set_strict"].risk == "dangerous"
    assert specs["accounts.clone_config"].risk == "dangerous"
    assert specs["account_bots.replay_polling_dlq"].runtime_effects == (
        "account_bot_dlq_replay",
    )
    assert specs["settings.save"].runtime_effects == ("reload_global_settings",)
    assert specs["account_bots.test"].runtime_effects == ("account_bot_test_send",)
    assert specs["notifications.test"].runtime_effects == ("notification_test_send",)
    assert specs["message_templates.test_send"].runtime_effects == (
        "message_template_test_send",
    )
    assert specs["dispatch.enable_trace"].runtime_effects == (
        "dispatch_enable_trace",
    )
    assert specs["plugin_repos.update_installed"].runtime_effects == (
        "plugin_repo_bulk_update",
    )
    assert specs["plugin_repos.update_installed"].runtime_retryable is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("修改账号备注并复制账号配置", "accounts"),
        ("修改这个插件的账号级配置", "features"),
        ("配置交互 Bot 的可信 Bot 和通知模板", "interaction"),
        ("查看限流用量后临时调严两小时", "rate_limits"),
    ],
)
def test_operational_workflows_route_to_their_domain(text: str, expected: str) -> None:
    available = {"accounts", "features", "interaction", "rate_limits", "message_templates"}
    route = route_locally(text, available=available)

    assert route is not None
    assert expected in route.domains


def test_bot_channel_forces_current_account_across_operational_tools() -> None:
    ctx = ToolContext(
        db=SimpleDB(),  # type: ignore[arg-type]
        channel="bot",
        role="admin",
        account_id=7,
    )
    malicious = {"account_id": 99}

    assert account_bot_tools._account_id(ctx, malicious) == 7  # noqa: SLF001
    assert access_tools._account_id(ctx, malicious) == 7  # noqa: SLF001
    assert access_tools._optional_account_id(ctx, malicious) == 7  # noqa: SLF001
    assert bundle_tools._account_id(ctx, malicious) == 7  # noqa: SLF001
    assert dispatch_tools._account_id(ctx, malicious) == 7  # noqa: SLF001
    assert message_template_tools._account_id(ctx, malicious) == 7  # noqa: SLF001
    assert safety_tools._account_id(ctx, malicious) == 7  # noqa: SLF001
    assert usage_tools._account_scope(ctx, malicious) == 7  # noqa: SLF001
    assert webhook_tools._account_id(ctx, malicious) == 7  # noqa: SLF001


def test_bot_channel_rejects_cross_account_access_row() -> None:
    ctx = ToolContext(
        db=SimpleDB(),  # type: ignore[arg-type]
        channel="bot",
        role="admin",
        account_id=7,
    )
    with pytest.raises(ValueError, match="当前绑定账号"):
        access_tools._require_bot_row_scope(  # noqa: SLF001
            ctx,
            type("Row", (), {"account_id": 99})(),
        )


def test_feature_config_parser_and_scope_guards() -> None:
    assert feature_tools._parse_config_json(  # noqa: SLF001
        {"config_json": json.dumps({"api_key": "secret", "nested": {"enabled": True}})}
    ) == {"api_key": "secret", "nested": {"enabled": True}}

    feature = Feature(
        key="demo",
        display_name="Demo",
        is_builtin=False,
        manifest={
            "config_schema": {
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "level": "global", "x-sensitive": True},
                    "prompt": {"type": "string"},
                    "server_value": {"type": "string", "readOnly": True},
                },
            }
        },
    )
    feature_tools._validate_patch_scope(feature, {"prompt": "hello"}, "account")  # noqa: SLF001
    feature_tools._validate_patch_scope(feature, {"api_key": "secret"}, "global")  # noqa: SLF001
    with pytest.raises(ValueError, match="全局字段"):
        feature_tools._validate_patch_scope(feature, {"api_key": "secret"}, "account")  # noqa: SLF001
    with pytest.raises(ValueError, match="账号级字段"):
        feature_tools._validate_patch_scope(feature, {"prompt": "hello"}, "global")  # noqa: SLF001
    with pytest.raises(ValueError, match="只读字段"):
        feature_tools._validate_patch_scope(  # noqa: SLF001
            feature, {"server_value": "tamper"}, "account"
        )


def test_direct_passthrough_schema_is_only_added_for_declaring_plugin() -> None:
    declared = Feature(
        key="direct",
        display_name="Direct",
        is_builtin=False,
        manifest={
            "capabilities": {"telegram_direct_passthrough": {"enabled": True}}
        },
    )
    schema = feature_tools._scope_schema(declared, "account")  # noqa: SLF001
    assert schema is not None
    assert "direct_passthrough" in schema["properties"]

    undeclared = Feature(
        key="plain", display_name="Plain", is_builtin=False, manifest={}
    )
    with pytest.raises(ValueError, match="未声明"):
        feature_tools._validate_patch_scope(  # noqa: SLF001
            undeclared,
            {"direct_passthrough": {"enabled": True}},
            "account",
        )


def test_interaction_config_patch_rejects_rules_and_server_state() -> None:
    assert interaction_tools._parse_config_json(  # noqa: SLF001
        {"config_json": '{"trusted_bot_ids":[123],"enabled":true}'}
    ) == {"trusted_bot_ids": [123], "enabled": True}
    with pytest.raises(ValueError, match="专用工具"):
        interaction_tools._parse_config_json(  # noqa: SLF001
            {"config_json": '{"rules":[]}'}
        )


@pytest.mark.asyncio
async def test_management_bot_token_is_verified_before_action(monkeypatch) -> None:
    monkeypatch.setattr(
        account_bot_tools,
        "get_config",
        AsyncMock(return_value={"configured": False, "has_token": False}),
    )
    get_me = AsyncMock(return_value={"id": 123, "username": "demo_bot"})
    monkeypatch.setattr(account_bot_tools.account_bot_service, "get_me", get_me)
    ctx = ToolContext(
        db=SimpleDB(),  # type: ignore[arg-type]
        channel="web",
        role="admin",
    )

    prepared = await account_bot_tools.save_preview(
        ctx,
        {"account_id": 1, "bot_token": "123456:secret-token", "enabled": True},
    )

    assert prepared.arguments["_token_preverified"] is True
    assert prepared.arguments["_verified_username"] == "demo_bot"
    assert "secret-token" not in json.dumps(prepared.preview)
    get_me.assert_awaited_once_with("123456:secret-token")


@pytest.mark.parametrize(
    "changes",
    [
        {"command_echo_guard_previous_messages": 51},
        {"login_security": {"notify_otp_ttl_seconds": 30}},
        {"login_security": {"totp_mode": "sometimes"}},
        {"log_retention": {"runtime_log_max_message_chars": 100}},
        {"log_retention": {"runtime_log_min_level": "fatal"}},
        {"ui_preferences": {"sidebar_order": ["/unknown"]}},
        {"ui_preferences": {"provider_order": [1, -2]}},
        {"llm_limits": {"unknown": 1}},
    ],
)
def test_agent_system_settings_enforce_api_bounds(changes: dict) -> None:
    with pytest.raises(ValueError):
        settings_tools._validate_changes(changes)  # noqa: SLF001


@pytest.mark.asyncio
async def test_agent_login_otp_threshold_zero_forces_notification_off() -> None:
    row = SystemSetting(
        key="login_security",
        value={
            "notify_otp_enabled": True,
            "notify_otp_failed_attempt_threshold": 5,
        },
    )

    async def fake_get(model, key):  # noqa: ANN001
        return row if model is SystemSetting and key == "login_security" else None

    await settings_tools.save_execute(
        ToolContext(
            db=SimpleDB(get=fake_get),  # type: ignore[arg-type]
            channel="web",
            role="admin",
        ),
        {
            "login_security": {
                "notify_otp_enabled": True,
                "notify_otp_failed_attempt_threshold": 0,
            }
        },
    )

    assert row.value["notify_otp_enabled"] is False
    assert row.value["notify_otp_failed_attempt_threshold"] == 0


@pytest.mark.asyncio
async def test_agent_resume_rejects_undecryptable_account(monkeypatch) -> None:
    account = Account(id=1, phone="+100", status="paused")

    async def fake_get(model, key):  # noqa: ANN001
        return account if model is Account and key == 1 else None

    monkeypatch.setattr(
        "app.services.account_service.ensure_account_secrets_decryptable",
        lambda _row: (_ for _ in ()).throw(ValueError("bad master key")),
    )

    with pytest.raises(ValueError, match="MASTER_KEY"):
        await account_tools.set_paused_execute(
            ToolContext(
                db=SimpleDB(get=fake_get),  # type: ignore[arg-type]
                channel="web",
                role="admin",
            ),
            {"account_id": 1, "paused": False},
        )

    assert account.status == "paused"


@pytest.mark.asyncio
async def test_account_feature_config_save_merges_patch_without_committing(
    monkeypatch,
) -> None:
    account = Account(id=1, phone="+100", display_name="A")
    feature = Feature(
        key="demo",
        display_name="Demo",
        is_builtin=False,
        manifest={
            "config_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "prompt": {"type": "string"},
                    "api_key": {"type": "string", "level": "global"},
                },
            }
        },
    )
    existing = AccountFeature(
        account_id=1,
        feature_key="demo",
        enabled=True,
        config={"prompt": "old"},
    )

    async def fake_get(model, key):  # noqa: ANN001
        if model is Account:
            return account
        if model is Feature:
            return feature
        if model is AccountFeature:
            return existing
        return None

    db = SimpleDB(get=fake_get)
    save = AsyncMock(return_value=existing)
    monkeypatch.setattr(feature_tools.feature_service, "seed_builtin_features", AsyncMock())
    monkeypatch.setattr(feature_tools.feature_service, "set_account_feature", save)
    ctx = ToolContext(db=db, channel="web", role="admin")  # type: ignore[arg-type]

    result = await feature_tools.save_account_config_execute(
        ctx,
        {
            "account_id": 1,
            "feature_key": "demo",
            "config_json": '{"prompt":"new"}',
        },
    )

    assert result["changed_keys"] == ["prompt"]
    call = save.await_args
    assert call.kwargs["config"] == {"prompt": "new"}
    assert call.kwargs["commit"] is False
    assert call.kwargs["notify"] is False


@pytest.mark.asyncio
async def test_global_feature_config_save_only_passes_global_patch(monkeypatch) -> None:
    feature = Feature(
        key="demo",
        display_name="Demo",
        is_builtin=False,
        manifest={
            "config_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "api_key": {"type": "string", "level": "global"},
                },
            }
        },
    )

    async def fake_get(model, _key):  # noqa: ANN001
        return feature if model is Feature else None

    db = SimpleDB(get=fake_get)
    monkeypatch.setattr(feature_tools.feature_service, "seed_builtin_features", AsyncMock())
    monkeypatch.setattr(
        feature_tools.feature_service,
        "get_plugin_global_config",
        AsyncMock(return_value={"api_key": "old"}),
    )
    save = AsyncMock(return_value={"api_key": "new"})
    monkeypatch.setattr(feature_tools.feature_service, "set_plugin_global_config", save)
    ctx = ToolContext(db=db, channel="web", role="admin")  # type: ignore[arg-type]

    result = await feature_tools.save_global_config_execute(
        ctx,
        {"feature_key": "demo", "config_json": '{"api_key":"new"}'},
    )

    assert result["changed_keys"] == ["api_key"]
    assert save.await_args.kwargs == {"notify": False, "commit": False}
    assert save.await_args.args[2] == {"api_key": "new"}


@pytest.mark.asyncio
async def test_interaction_config_save_preserves_existing_rules(monkeypatch) -> None:
    account = Account(id=1, phone="+100", display_name="A")

    async def fake_get(model, _key):  # noqa: ANN001
        return account if model is Account else None

    current = {
        "enabled": False,
        "has_interaction_bot_token": False,
        "has_transfer_bot_token": False,
        "rules": [
            {
                "id": "existing",
                "name": "Existing",
                "enabled": True,
                "trigger_mode": "payment",
                "trigger_texts": ["转账成功"],
            }
        ],
    }
    monkeypatch.setattr(
        interaction_tools.account_bot_service,
        "get_interaction_bot_config",
        AsyncMock(return_value=current),
    )
    update = AsyncMock(return_value={**current, "trusted_bot_ids": [123]})
    monkeypatch.setattr(
        interaction_tools.account_bot_service,
        "update_interaction_bot_config",
        update,
    )
    ctx = ToolContext(
        db=SimpleDB(get=fake_get),  # type: ignore[arg-type]
        channel="web",
        role="admin",
    )

    await interaction_tools.save_config_execute(
        ctx,
        {"account_id": 1, "config_json": '{"trusted_bot_ids":[123]}'},
    )

    saved_patch = update.await_args.args[2]
    assert saved_patch["trusted_bot_ids"] == [123]
    assert saved_patch["rules"][0]["id"] == "existing"
    with pytest.raises(ValueError, match="服务端状态字段"):
        interaction_tools._parse_config_json(  # noqa: SLF001
            {"config_json": '{"interaction_running":true}'}
        )


@pytest.mark.asyncio
async def test_arbitrary_config_json_is_encrypted_out_of_action_arguments() -> None:
    spec = get_registry().get("features.save_account_config")
    assert spec is not None

    db = SimpleDB()
    ctx = ToolContext(
        db=db,  # type: ignore[arg-type]
        channel="web",
        role="admin",
        web_user_id=7,
    )
    raw = '{"api_key":"sk-secret-value","prompt":"hello"}'
    action = await create_pending_action(
        db,  # type: ignore[arg-type]
        ctx=ctx,
        spec=spec,
        arguments={"account_id": 1, "feature_key": "demo", "config_json": raw},
        preview={"summary": "save", "changed_keys": ["api_key", "prompt"]},
    )

    assert "config_json" not in action.arguments
    assert action.arguments["has_config_json"] is True
    assert "sk-secret-value" not in json.dumps(action.arguments)
    assert "sk-secret-value" not in json.dumps(action.preview)
    assert decrypt_secret_payload(action.secret_payload_enc) == {"config_json": raw}


def test_action_handlers_keep_the_executor_transaction_boundary() -> None:
    tools_dir = Path(inspect.getfile(feature_tools)).parent
    names = {
        "access.py",
        "account_bots.py",
        "accounts.py",
        "capabilities.py",
        "config_bundles.py",
        "connectivity.py",
        "dispatch.py",
        "features.py",
        "interaction.py",
        "message_templates.py",
        "notifications.py",
        "safety.py",
        "system_settings.py",
        "usage.py",
        "webhooks.py",
    }
    for name in names:
        source = (tools_dir / name).read_text(encoding="utf-8")
        assert "AsyncSessionLocal" not in source, name
        assert ".commit(" not in source, name
        assert ".rollback(" not in source, name


def test_shared_services_offer_non_committing_agent_mode() -> None:
    from app.services import account_service, alias_service, feature_service, sudo_service

    clone_params = inspect.signature(account_service.clone_config).parameters
    global_params = inspect.signature(feature_service.set_plugin_global_config).parameters
    assert clone_params["commit"].default is True
    assert clone_params["notify"].default is True
    assert global_params["commit"].default is True
    assert global_params["notify"].default is True
    assert inspect.signature(alias_service.create_alias).parameters["commit"].default is True
    assert inspect.signature(sudo_service.create_sudo_user).parameters["commit"].default is True


def test_ignored_peer_conflict_uses_savepoint_not_outer_rollback() -> None:
    from app.services import ignored_peer_service

    source = inspect.getsource(ignored_peer_service.add_ignored)
    assert "begin_nested" in source
    assert ".rollback(" not in source


class SimpleDB:
    """只实现工具单测所需的 AsyncSession 最小表面。"""

    def __init__(self, *, get=None) -> None:  # noqa: ANN001
        self.added = None
        self._get = get

    def add(self, value) -> None:  # noqa: ANN001
        self.added = value

    async def flush(self) -> None:
        return None

    async def get(self, model, key):  # noqa: ANN001
        if self._get is None:
            return None
        return await self._get(model, key)
