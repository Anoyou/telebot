from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.db.models.system import SystemSetting
from app.services import platform_capabilities as caps
from app.services import runtime_profile_service as profiles


class _FakeSettingsDB:
    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.rows = {
            key: SystemSetting(key=key, value=value)
            for key, value in (initial or {}).items()
        }
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, model, key):  # noqa: ANN001
        assert model is SystemSetting
        return self.rows.get(key)

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, SystemSetting):
            self.rows[row.key] = row

    async def delete(self, row: SystemSetting) -> None:
        self.rows.pop(row.key, None)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    caps._reset_for_tests()
    profiles._reset_for_tests()
    caps._CACHE_READY = True
    for key in caps.ALL_MODULE_KEYS:
        caps._DESIRED[key] = True
        caps._RUNTIME[key] = "ready"
    yield
    profiles._reset_for_tests()
    caps._reset_for_tests()


def _module_rows(values: dict[str, bool] | None = None) -> dict[str, Any]:
    values = values or {key: True for key in caps.ALL_MODULE_KEYS}
    return {
        caps.module_setting_key(key): {"enabled": enabled, "generation": 0}
        for key, enabled in values.items()
    }


def _install_module_fakes(monkeypatch, db: _FakeSettingsDB) -> list[tuple[str, bool]]:
    changes: list[tuple[str, bool]] = []

    async def read_module_desired(_db, key: str):  # noqa: ANN001
        row = db.rows.get(caps.module_setting_key(key))
        value = caps.normalize_capability_setting(row.value if row else None)
        return bool(value["enabled"]), int(value["generation"])

    async def set_module_enabled(_db, key: str, enabled: bool, **_kwargs):  # noqa: ANN001
        setting_key = caps.module_setting_key(key)
        row = db.rows.get(setting_key)
        generation = int((row.value if row else {}).get("generation", 0)) + 1
        db.rows[setting_key] = SystemSetting(
            key=setting_key,
            value={"enabled": enabled, "generation": generation},
        )
        caps._DESIRED[key] = enabled
        caps._RUNTIME[key] = "ready" if enabled else "stopped"
        changes.append((key, enabled))
        return {"runtime_state": caps._RUNTIME[key], "changed": True}

    monkeypatch.setattr(caps, "read_module_desired", read_module_desired)
    monkeypatch.setattr(caps, "set_module_enabled", set_module_enabled)
    return changes


@pytest.mark.asyncio
async def test_apply_persists_snapshot_registers_t3_deny_and_audits_blind_spot(
    monkeypatch,
) -> None:
    db = _FakeSettingsDB(_module_rows())
    changes = _install_module_fakes(monkeypatch, db)
    converge = AsyncMock(return_value={"total": 2, "acked": 2, "failed": []})
    audit = AsyncMock()
    monkeypatch.setattr(profiles, "_converge_workers", converge)
    monkeypatch.setattr(profiles.audit_svc, "write", audit)

    status = await profiles.apply(db, "safe_watch", operator_id=17)  # type: ignore[arg-type]

    assert db.rows[profiles.PROFILE_ROLLBACK_SNAPSHOT_KEY].value["modules"] == {
        key: True for key in caps.ALL_MODULE_KEYS
    }
    assert ("interaction_bot", False) in changes
    assert status["active_profile"] == "safe_watch"
    assert status["status"] == "active"
    assert caps.ledger_action_deny_registrations() == ((
        profiles.SAFE_WATCH_LEDGER_DENY_OWNER,
        profiles.SAFE_WATCH_LEDGER_DENY_REASON,
    ),)
    assert caps.ledger_actions_enabled() is False
    with pytest.raises(caps.LedgerActionsFailedClosed) as exc_info:
        caps.require_ledger_actions_enabled()
    assert exc_info.value.error_code == caps.LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE
    converge.assert_awaited_once()
    audit.assert_awaited_once()
    detail = audit.await_args.kwargs["detail"]
    assert detail["operator_id"] == 17
    assert detail["interaction_bot"] == "interaction_bot 采集已停"
    assert "实时观测盲区" in detail["blind_spot"]


@pytest.mark.asyncio
async def test_apply_convergence_timeout_is_explicit_failed_and_keeps_deny(
    monkeypatch,
) -> None:
    db = _FakeSettingsDB(_module_rows())
    _install_module_fakes(monkeypatch, db)
    monkeypatch.setattr(
        profiles,
        "_converge_workers",
        AsyncMock(return_value={"total": 1, "acked": 0, "failed": [9]}),
    )

    with pytest.raises(profiles.ProfileConvergenceFailed) as exc_info:
        await profiles.apply(db, "safe_watch", operator_id=8)  # type: ignore[arg-type]

    assert exc_info.value.error_code == "PROFILE_CONVERGENCE_FAILED"
    state = db.rows[profiles.RUNTIME_PROFILE_STATE_KEY].value
    assert state["active_profile"] == "safe_watch"
    assert state["status"] == "failed"
    assert profiles.SAFE_WATCH_LEDGER_DENY_REASON in caps.ledger_action_deny_reasons()


@pytest.mark.asyncio
async def test_apply_module_failure_reapplies_full_safe_watch_and_stays_failed(
    monkeypatch,
) -> None:
    db = _FakeSettingsDB(_module_rows())
    changes = _install_module_fakes(monkeypatch, db)
    original_set = caps.set_module_enabled
    failed_once = False

    async def fail_ai_once(_db, key: str, enabled: bool, **kwargs):  # noqa: ANN001
        nonlocal failed_once
        if key == "ai" and not enabled and not failed_once:
            failed_once = True
            return {"runtime_state": "failed", "last_error": "ai stop failed"}
        return await original_set(_db, key, enabled, **kwargs)

    stop_manager = AsyncMock()
    converge = AsyncMock(return_value={"total": 1, "acked": 1, "failed": []})
    monkeypatch.setattr(caps, "set_module_enabled", fail_ai_once)
    monkeypatch.setattr(caps, "stop_local_module", stop_manager)
    monkeypatch.setattr(profiles, "_converge_workers", converge)

    with pytest.raises(profiles.RuntimeProfileError, match="ai stop failed"):
        await profiles.apply(db, "safe_watch", operator_id=8)  # type: ignore[arg-type]

    state = db.rows[profiles.RUNTIME_PROFILE_STATE_KEY].value
    assert state["active_profile"] == "safe_watch"
    assert state["status"] == "failed"
    assert ("ai", False) in changes
    assert ("interaction_bot", False) in changes
    stop_manager.assert_awaited_once_with("interaction_bot")
    converge.assert_awaited_once_with("pause")
    assert profiles.SAFE_WATCH_LEDGER_DENY_REASON in caps.ledger_action_deny_reasons()


@pytest.mark.asyncio
async def test_startup_restore_rehydrates_process_local_t3_deny(monkeypatch) -> None:
    db = _FakeSettingsDB(
        {
            profiles.RUNTIME_PROFILE_STATE_KEY: {
                "active_profile": "safe_watch",
                "status": "active",
            }
        }
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(profiles, "AsyncSessionLocal", lambda: _SessionContext())

    await profiles.startup_restore()

    assert caps.ledger_action_deny_registrations() == ((
        profiles.SAFE_WATCH_LEDGER_DENY_OWNER,
        profiles.SAFE_WATCH_LEDGER_DENY_REASON,
    ),)
    with pytest.raises(caps.LedgerActionsFailedClosed) as exc_info:
        caps.require_ledger_actions_enabled()
    assert exc_info.value.error_code == caps.LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["main_process_restart", "worker_crash_restart", "new_account_login"],
)
async def test_restart_and_new_worker_paths_remain_paused_and_t3_denied(
    monkeypatch, scenario: str,
) -> None:
    from app.worker import runtime as worker_runtime

    db = _FakeSettingsDB(
        {
            profiles.RUNTIME_PROFILE_STATE_KEY: {
                "active_profile": "safe_watch",
                "status": "active",
            }
        }
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(profiles, "AsyncSessionLocal", lambda: _SessionContext())

    if scenario == "main_process_restart":
        await profiles.startup_restore()

    # 主进程重启后的 worker、supervisor 崩溃重启与登录后的 start_worker，
    # 最终都必须走同一持久 profile 冷启动闭锁。
    gate = await worker_runtime._initialize_leaf_delivery_gate()
    assert gate.is_set() is False
    assert gate.has_reason("safe_watch") is True
    async with gate.delivery() as entered:
        assert entered is False
    with pytest.raises(caps.LedgerActionsFailedClosed) as exc_info:
        caps.require_ledger_actions_enabled()
    assert exc_info.value.error_code == caps.LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE


@pytest.mark.asyncio
async def test_worker_cold_start_fails_closed_on_missing_or_corrupt_profile(
    monkeypatch,
) -> None:
    class _BrokenSessionContext:
        async def __aenter__(self):
            raise RuntimeError("db unavailable")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(profiles, "AsyncSessionLocal", lambda: _BrokenSessionContext())

    assert await profiles.read_worker_pause_state() is True
    assert profiles.is_safe_watch_active_cached() is True


@pytest.mark.asyncio
async def test_restore_waits_for_worker_then_manager_and_disposes_deny(monkeypatch) -> None:
    initial = _module_rows()
    original = {
        "ai": True,
        "interaction_bot": True,
        "webhooks": False,
        "ledger": True,
        "dispatch_debug": False,
    }
    initial[profiles.RUNTIME_PROFILE_STATE_KEY] = {
        "active_profile": "safe_watch",
        "status": "active",
    }
    initial[profiles.PROFILE_ROLLBACK_SNAPSHOT_KEY] = {"modules": original}
    db = _FakeSettingsDB(initial)
    changes = _install_module_fakes(monkeypatch, db)
    order: list[str] = []

    async def converge(command: str, **_payload):
        order.append(command)
        return {"total": 1, "acked": 1, "failed": []}

    async def reconcile(module: str) -> None:
        order.append(f"manager:{module}")

    audit = AsyncMock()
    monkeypatch.setattr(profiles, "_converge_workers", converge)
    monkeypatch.setattr(caps, "reconcile_local_module", reconcile)
    monkeypatch.setattr(profiles.audit_svc, "write", audit)
    profiles._ensure_deny_registered()

    status = await profiles.restore(db, operator_id=22)  # type: ignore[arg-type]

    assert changes[:5] == list(original.items())
    assert order == ["resume", "manager:interaction_bot"]
    assert status["active_profile"] is None
    assert status["status"] == "idle"
    assert profiles.PROFILE_ROLLBACK_SNAPSHOT_KEY not in db.rows
    assert caps.ledger_action_deny_registrations() == ()
    audit.assert_awaited_once()
    assert audit.await_args.args[2] == "runtime_profile.exit"
    assert audit.await_args.kwargs["detail"]["operator_id"] == 22


@pytest.mark.asyncio
async def test_restore_timeout_reapplies_safe_watch_and_keeps_deny(monkeypatch) -> None:
    initial = _module_rows()
    initial[profiles.RUNTIME_PROFILE_STATE_KEY] = {
        "active_profile": "safe_watch",
        "status": "active",
    }
    initial[profiles.PROFILE_ROLLBACK_SNAPSHOT_KEY] = {
        "modules": {key: True for key in caps.ALL_MODULE_KEYS}
    }
    db = _FakeSettingsDB(initial)
    changes = _install_module_fakes(monkeypatch, db)
    calls = 0

    async def converge(command: str, **_payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"total": 1, "acked": 0, "failed": [3]}
        return {"total": 1, "acked": 1, "failed": []}

    monkeypatch.setattr(profiles, "_converge_workers", converge)
    monkeypatch.setattr(caps, "stop_local_module", AsyncMock())
    profiles._ensure_deny_registered()

    with pytest.raises(profiles.ProfileConvergenceFailed):
        await profiles.restore(db, operator_id=22)  # type: ignore[arg-type]

    assert changes[-4:] == [
        ("ai", False),
        ("interaction_bot", False),
        ("webhooks", False),
        ("dispatch_debug", True),
    ]
    state = db.rows[profiles.RUNTIME_PROFILE_STATE_KEY].value
    assert state["active_profile"] == "safe_watch"
    assert state["status"] == "failed"
    assert profiles.SAFE_WATCH_LEDGER_DENY_REASON in caps.ledger_action_deny_reasons()
