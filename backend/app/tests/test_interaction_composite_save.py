"""交互 Bot 复合保存：规则 + 插件配置同一事务。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import account_bots
from app.schemas.account_bot import (
    AccountBotInteractionCompositePluginConfig,
    AccountBotInteractionCompositeSaveRequest,
    AccountBotInteractionConfig,
)


class _FakeDB:
    def __init__(self) -> None:
        self.committed = False
        self.features: dict[str, SimpleNamespace] = {
            "game": SimpleNamespace(manifest={"config_schema": {"type": "object", "properties": {}}}),
        }
        self.account_features: dict[tuple[int, str], SimpleNamespace] = {
            (7, "game"): SimpleNamespace(enabled=True, config={"old": 1}),
        }

    async def get(self, model, ident):  # noqa: ANN001
        name = getattr(model, "__name__", str(model))
        if name == "Feature":
            return self.features.get(ident)
        if name == "AccountFeature":
            return self.account_features.get(ident)
        return None

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_composite_save_writes_plugins_and_interaction_then_commits(monkeypatch) -> None:
    db = _FakeDB()
    user = SimpleNamespace(id=1)

    monkeypatch.setattr(account_bots.account_bot_service, "ensure_account", AsyncMock())
    monkeypatch.setattr(account_bots.feature_service, "seed_builtin_features", AsyncMock())
    monkeypatch.setattr(
        account_bots,
        "_ensure_keyword_rules_have_interaction_bot_token",
        AsyncMock(),
    )
    set_feature = AsyncMock(
        return_value=SimpleNamespace(feature_key="game", enabled=True, config={"prize": 10}),
    )
    monkeypatch.setattr(account_bots.feature_service, "set_account_feature", set_feature)
    monkeypatch.setattr(
        account_bots.features_api,
        "_preserve_existing_sensitive_values",
        lambda existing, incoming, key: dict(incoming),
    )
    monkeypatch.setattr(
        account_bots.features_api,
        "_normalize_feature_config",
        lambda key, config: dict(config),
    )
    monkeypatch.setattr(
        account_bots.features_api,
        "_account_config_schema",
        lambda manifest, config: None,
    )
    update_interaction = AsyncMock(
        return_value={
            "enabled": True,
            "rules": [{"id": "r1"}],
            "interaction_bot_id": 99,
            "trusted_bot_ids": [1],
            "has_interaction_bot_token": True,
        },
    )
    monkeypatch.setattr(
        account_bots.interaction_bot_service,
        "update_interaction_bot_config",
        update_interaction,
    )
    monkeypatch.setattr(account_bots.audit, "write", AsyncMock())
    monkeypatch.setattr(account_bots.feature_service, "_notify_reload", AsyncMock())
    monkeypatch.setattr(
        account_bots.interaction_bot_runtime,
        "restart_interaction_bot",
        AsyncMock(),
    )
    monkeypatch.setattr(
        account_bots,
        "_with_interaction_runtime_state",
        lambda aid, data: data,
    )
    monkeypatch.setattr(
        account_bots,
        "_with_polling_dlq_count",
        AsyncMock(side_effect=lambda aid, data: data),
    )

    payload = AccountBotInteractionCompositeSaveRequest(
        interaction=AccountBotInteractionConfig(enabled=True),
        plugin_configs=[
            AccountBotInteractionCompositePluginConfig(
                plugin_key="game",
                config={"prize": 10},
            ),
        ],
    )
    result = await account_bots.save_account_bot_interaction_composite(7, payload, db, user)

    set_feature.assert_awaited_once()
    assert set_feature.await_args.kwargs["commit"] is False
    assert set_feature.await_args.kwargs["notify"] is False
    update_interaction.assert_awaited_once()
    assert db.committed is True
    assert result.plugins[0].plugin_key == "game"
    assert result.plugins[0].config_keys == ["prize"]
    assert result.interaction.enabled is True


@pytest.mark.asyncio
async def test_composite_save_rejects_unknown_plugin_before_writes(monkeypatch) -> None:
    db = _FakeDB()
    user = SimpleNamespace(id=1)
    monkeypatch.setattr(account_bots.account_bot_service, "ensure_account", AsyncMock())
    monkeypatch.setattr(account_bots.feature_service, "seed_builtin_features", AsyncMock())
    monkeypatch.setattr(
        account_bots,
        "_ensure_keyword_rules_have_interaction_bot_token",
        AsyncMock(),
    )
    set_feature = AsyncMock()
    update_interaction = AsyncMock()
    monkeypatch.setattr(account_bots.feature_service, "set_account_feature", set_feature)
    monkeypatch.setattr(
        account_bots.interaction_bot_service,
        "update_interaction_bot_config",
        update_interaction,
    )

    payload = AccountBotInteractionCompositeSaveRequest(
        interaction=AccountBotInteractionConfig(enabled=True),
        plugin_configs=[
            AccountBotInteractionCompositePluginConfig(
                plugin_key="missing_plugin",
                config={"x": 1},
            ),
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        await account_bots.save_account_bot_interaction_composite(7, payload, db, user)

    assert exc_info.value.status_code == 404
    set_feature.assert_not_awaited()
    update_interaction.assert_not_awaited()
    assert db.committed is False
