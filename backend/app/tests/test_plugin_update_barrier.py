from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import remote_plugin_service
from app.worker.plugins import loader
from app.worker.plugins.base import PluginContext
from app.worker.plugins.update_barrier import (
    acknowledge_plugin_update,
    begin_plugin_update,
    clear_plugin_update,
    plugin_update_in_progress,
)


def test_update_barrier_is_scoped_per_account_and_clears_atomically(tmp_path) -> None:
    update_id = begin_plugin_update(tmp_path, "ai_redpacket", target_version="0.1.19")

    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 1) is True
    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 2) is True

    acknowledge_plugin_update(tmp_path, "ai_redpacket", 1, update_id)

    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 1) is False
    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 2) is True

    clear_plugin_update(tmp_path, "ai_redpacket")

    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 1) is False
    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 2) is False
    assert list(tmp_path.glob(".ai_redpacket.update*")) == []


def test_stale_worker_ack_cannot_confirm_a_newer_update(tmp_path) -> None:
    first_update_id = begin_plugin_update(tmp_path, "ai_redpacket", target_version="0.1.19")
    second_update_id = begin_plugin_update(tmp_path, "ai_redpacket", target_version="0.1.20")

    acknowledge_plugin_update(tmp_path, "ai_redpacket", 1, first_update_id)
    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 1) is True

    acknowledge_plugin_update(tmp_path, "ai_redpacket", 1, second_update_id)
    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 1) is False


def test_update_barrier_accepts_platform_test_and_internal_plugin_keys(tmp_path) -> None:
    update_id = begin_plugin_update(tmp_path, "_test_plugin")

    assert plugin_update_in_progress(tmp_path, "_test_plugin", 1) is True
    acknowledge_plugin_update(tmp_path, "_test_plugin", 1, update_id)
    assert plugin_update_in_progress(tmp_path, "_test_plugin", 1) is False


async def test_update_barrier_blocks_command_and_interaction_factories(tmp_path, monkeypatch) -> None:
    begin_plugin_update(tmp_path, "ai_redpacket")
    monkeypatch.setattr(loader, "_installed_dir", lambda: tmp_path)
    calls: list[str] = []
    ctx = PluginContext(account_id=2, feature_key="ai_redpacket")

    async def command_handler(*_args) -> None:
        calls.append("command")

    wrapped = loader._wrap_cmd(command_handler, ctx)
    with pytest.raises(RuntimeError, match="PLUGIN_UPDATE_IN_PROGRESS"):
        await wrapped(None, None, "", 2)

    state = loader._AccountState(2)

    async def interaction_factory() -> None:
        calls.append("interaction")

    with pytest.raises(RuntimeError, match="PLUGIN_UPDATE_IN_PROGRESS"):
        await loader._invoke_plugin_with_resilience(
            state,
            "ai_redpacket",
            interaction_factory,
        )

    assert calls == []


async def test_trigger_reload_keeps_barrier_until_all_workers_confirm(tmp_path, monkeypatch) -> None:
    update_id = begin_plugin_update(tmp_path, "ai_redpacket")
    monkeypatch.setattr(remote_plugin_service, "_installed_root", lambda: tmp_path)

    async def unconfirmed(_db, _name: str) -> list[int]:
        return [2]

    monkeypatch.setattr(remote_plugin_service, "_trigger_reload", unconfirmed)
    with pytest.raises(remote_plugin_service.RemotePluginError) as exc_info:
        await remote_plugin_service.trigger_reload(SimpleNamespace(), "ai_redpacket")

    assert exc_info.value.code == "PLUGIN_RELOAD_UNCONFIRMED"
    assert plugin_update_in_progress(tmp_path, "ai_redpacket", 2) is True

    async def confirmed(_db, _name: str) -> list[int]:
        acknowledge_plugin_update(tmp_path, "ai_redpacket", 2, update_id)
        return []

    monkeypatch.setattr(remote_plugin_service, "_trigger_reload", confirmed)
    await remote_plugin_service.trigger_reload(SimpleNamespace(), "ai_redpacket")

    assert list(tmp_path.glob(".ai_redpacket.update*")) == []


async def test_trigger_reload_without_replacement_barrier_keeps_legacy_best_effort(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(remote_plugin_service, "_installed_root", lambda: tmp_path)

    async def unconfirmed(_db, _name: str) -> list[int]:
        return [2]

    monkeypatch.setattr(remote_plugin_service, "_trigger_reload", unconfirmed)

    await remote_plugin_service.trigger_reload(SimpleNamespace(), "ai_redpacket")
