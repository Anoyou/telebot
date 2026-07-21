"""平台能力热插拔服务与 API 测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import platform_capabilities as caps_api
from app.api import rate_limit
from app.db.models.system import SystemSetting
from app.services import platform_capabilities as caps


class _FakeSettingsDB:
    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.rows: dict[str, SystemSetting] = {
            key: SystemSetting(key=key, value=value)
            for key, value in (initial or {}).items()
        }
        self.commits = 0

    async def get(self, model, key):  # noqa: ANN001
        assert model is SystemSetting
        return self.rows.get(key)

    def add(self, row: SystemSetting) -> None:
        self.rows[row.key] = row

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def _reset_caps():
    caps._reset_for_tests()
    yield
    caps._reset_for_tests()


@pytest.mark.asyncio
async def test_normalize_missing_generation_defaults_to_zero() -> None:
    assert caps.normalize_capability_setting({"enabled": True}) == {
        "enabled": True,
        "generation": 0,
    }
    assert caps.normalize_capability_setting(None)["enabled"] is True


@pytest.mark.asyncio
async def test_bootstrap_defaults_all_enabled() -> None:
    db = _FakeSettingsDB()
    snap = await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    assert snap.cache_ready is True
    for key in caps.ALL_MODULE_KEYS:
        assert snap.is_enabled(key) is True
        assert snap.generation(key) == 0


@pytest.mark.asyncio
async def test_set_module_enabled_persists_generation_and_cache(monkeypatch) -> None:
    db = _FakeSettingsDB()
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    monkeypatch.setattr(caps, "_broadcast_reload_config", AsyncMock(return_value={
        "total_accounts": 0,
        "notified": 0,
        "acked": 0,
        "pending": 0,
        "offline_or_timeout": 0,
        "last_broadcast_at": None,
        "notes": [],
    }))
    monkeypatch.setattr(caps, "_apply_local_transition", AsyncMock())
    monkeypatch.setattr(
        "app.services.audit.write",
        AsyncMock(),
    )

    result = await caps.set_module_enabled(
        db,  # type: ignore[arg-type]
        "webhooks",
        False,
        user_id=1,
    )
    assert result["desired_enabled"] is False
    assert result["generation"] == 1
    assert db.rows["webhooks_enabled"].value == {"enabled": False, "generation": 1}
    assert caps.is_module_enabled_cached("webhooks", fail_closed=True) is False


@pytest.mark.asyncio
async def test_idempotent_toggle_does_not_bump_generation(monkeypatch) -> None:
    db = _FakeSettingsDB({"ai_enabled": {"enabled": True, "generation": 3}})
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    broadcast = AsyncMock()
    monkeypatch.setattr(caps, "_broadcast_reload_config", broadcast)
    monkeypatch.setattr(caps, "_apply_local_transition", AsyncMock())

    result = await caps.set_module_enabled(db, "ai", True, user_id=1)  # type: ignore[arg-type]
    assert result["changed"] is False
    assert result["generation"] == 3
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_cache_fail_closed_when_not_bootstrapped() -> None:
    assert caps.is_module_enabled_cached("webhooks", fail_closed=True) is False


@pytest.mark.asyncio
async def test_ai_cache_defaults_open_when_not_bootstrapped() -> None:
    assert caps.is_ai_enabled_cached(fail_closed=False) is True


@pytest.mark.asyncio
async def test_settings_ai_enabled_delegates_to_platform_service(monkeypatch) -> None:
    db = _FakeSettingsDB()
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())
    monkeypatch.setattr(rate_limit, "_broadcast_reload", AsyncMock())
    set_ai = AsyncMock(return_value={
        "module_key": "ai",
        "desired_enabled": False,
        "generation": 1,
        "runtime_state": "stopped",
        "worker_convergence": {},
        "changed": True,
    })
    monkeypatch.setattr(rate_limit.platform_caps, "set_ai_enabled_compat", set_ai)
    # 模拟委托成功后缓存已关闭
    monkeypatch.setattr(
        rate_limit.platform_caps,
        "get_snapshot",
        lambda: SimpleNamespace(cache_ready=True),
    )
    monkeypatch.setattr(
        rate_limit.platform_caps,
        "is_ai_enabled_cached",
        lambda *args, **kwargs: False,
    )

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(ai_enabled=False),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )
    set_ai.assert_awaited_once()
    assert set_ai.await_args.args[1] is False
    assert result["ai_enabled"] is False


@pytest.mark.asyncio
async def test_capabilities_api_patch_unknown_module() -> None:
    from app.schemas.platform_capabilities import CapabilityModulePatch

    with pytest.raises(HTTPException) as exc:
        await caps_api.patch_platform_capability(
            "not_a_module",
            CapabilityModulePatch(enabled=False),
            _FakeSettingsDB(),  # type: ignore[arg-type]
            SimpleNamespace(id=1),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_capabilities_api_roundtrip(monkeypatch) -> None:
    db = _FakeSettingsDB()
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    monkeypatch.setattr(caps, "_broadcast_reload_config", AsyncMock(return_value={
        "total_accounts": 1,
        "notified": 1,
        "acked": 1,
        "pending": 0,
        "offline_or_timeout": 0,
        "last_broadcast_at": None,
        "notes": [],
    }))
    monkeypatch.setattr(caps, "_apply_local_transition", AsyncMock())
    monkeypatch.setattr("app.services.audit.write", AsyncMock())

    from app.schemas.platform_capabilities import CapabilityModulePatch

    out = await caps_api.patch_platform_capability(
        "ledger",
        CapabilityModulePatch(enabled=False),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=9),
    )
    assert out.module.key == "ledger"
    assert out.module.desired_enabled is False
    assert out.module.generation == 1

    status = await caps_api.get_platform_capabilities(db, SimpleNamespace(id=9))  # type: ignore[arg-type]
    ledger = next(m for m in status.modules if m.key == "ledger")
    assert ledger.desired_enabled is False


@pytest.mark.asyncio
async def test_plugin_runtime_partial_when_interaction_disabled() -> None:
    db = _FakeSettingsDB({"interaction_bot_enabled": {"enabled": False, "generation": 1}})
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    meta = caps.evaluate_plugin_runtime_availability(
        permissions=[],
        interaction_entries=[
            {"key": "play", "send_via": ["interaction_bot"]},
            {"key": "cmd", "send_via": ["userbot"]},
        ],
        event_subscriptions=[{"source": "userbot"}],
        preserve_command_trigger=True,
    )
    assert meta["runtime_availability"] in {"partial", "paused"}
    assert "userbot" in meta["available_channels"]
    assert "interaction_bot" not in meta["available_channels"]


@pytest.mark.asyncio
async def test_require_module_enabled_raises() -> None:
    db = _FakeSettingsDB({"dispatch_debug_enabled": {"enabled": False, "generation": 2}})
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as exc:
        await caps.require_module_enabled(db, "dispatch_debug")  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PLATFORM_MODULE_DISABLED"


@pytest.mark.asyncio
async def test_filter_runtime_subscriptions_by_channel_and_requires() -> None:
    db = _FakeSettingsDB(
        {
            "interaction_bot_enabled": {"enabled": False, "generation": 1},
            "webhooks_enabled": {"enabled": True, "generation": 0},
        }
    )
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]

    from app.services.event_bus import normalize_event_subscription

    subs = [
        normalize_event_subscription(
            {"source": ["userbot"], "events": ["message"]},
            plugin_key="demo",
        ),
        normalize_event_subscription(
            {"source": ["interaction_bot"], "events": ["message"]},
            plugin_key="demo",
        ),
        normalize_event_subscription(
            {
                "source": ["userbot"],
                "events": ["message"],
                "requires_platform_capabilities": ["ai"],
            },
            plugin_key="demo",
        ),
        normalize_event_subscription(
            {
                "source": ["webhook"],
                "events": ["webhook"],
                "requires_platform_capabilities": ["webhooks"],
            },
            plugin_key="demo",
        ),
    ]
    kept = caps.filter_runtime_event_subscriptions(subs)
    sources = []
    for item in kept:
        sources.extend(item.sources)
    assert "userbot" in sources
    assert "interaction_bot" not in sources
    assert "webhook" in sources


@pytest.mark.asyncio
async def test_concurrent_set_module_serializes_generation(monkeypatch) -> None:
    import asyncio

    db = _FakeSettingsDB()
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    monkeypatch.setattr(
        caps,
        "_broadcast_reload_config",
        AsyncMock(
            return_value={
                "total_accounts": 0,
                "notified": 0,
                "acked": 0,
                "pending": 0,
                "offline_or_timeout": 0,
                "last_broadcast_at": None,
                "notes": [],
            }
        ),
    )
    monkeypatch.setattr(caps, "_apply_local_transition", AsyncMock())
    monkeypatch.setattr("app.services.audit.write", AsyncMock())

    async def _toggle(n: int) -> dict:
        return await caps.set_module_enabled(
            db,  # type: ignore[arg-type]
            "ledger",
            n % 2 == 0,
            user_id=1,
        )

    results = await asyncio.gather(*[_toggle(i) for i in range(6)])
    gens = [r["generation"] for r in results if r.get("changed")]
    # 串行锁下 generation 应单调不重复
    assert gens == sorted(gens)
    assert len(gens) == len(set(gens))
    assert caps.get_module_generation_cached("ledger") == max(gens)


@pytest.mark.asyncio
async def test_local_transition_failure_sets_failed_runtime(monkeypatch) -> None:
    db = _FakeSettingsDB()
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    monkeypatch.setattr(
        caps,
        "_broadcast_reload_config",
        AsyncMock(
            return_value={
                "total_accounts": 0,
                "notified": 0,
                "acked": 0,
                "pending": 0,
                "offline_or_timeout": 0,
                "last_broadcast_at": None,
                "notes": [],
            }
        ),
    )
    monkeypatch.setattr("app.services.audit.write", AsyncMock())
    monkeypatch.setattr(caps, "_stop_module", AsyncMock())

    # 先关掉，再开启时注入启动失败
    await caps.set_module_enabled(db, "interaction_bot", False, user_id=1)  # type: ignore[arg-type]

    async def _start_fail(module_key):  # noqa: ANN001
        if module_key == "interaction_bot":
            raise RuntimeError("manager down")

    monkeypatch.setattr(caps, "_start_module", _start_fail)

    result = await caps.set_module_enabled(
        db,  # type: ignore[arg-type]
        "interaction_bot",
        True,
        user_id=1,
    )
    # desired 已写入；本地失败 → failed
    assert result["desired_enabled"] is True
    assert caps.get_snapshot().runtime["interaction_bot"] == "failed"
    assert caps.get_snapshot().last_error["interaction_bot"]


@pytest.mark.asyncio
async def test_stop_interaction_keeps_session_expire_tasks() -> None:
    """关闭交互只停 polling/测试 Bot，保留会话过期 drain。"""

    import asyncio

    from app.services import account_bot_runtime as abr

    async def _hang() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    poll = asyncio.create_task(_hang())
    test = asyncio.create_task(_hang())
    expire = asyncio.create_task(_hang())
    abr._INTERACTION_TASKS[1] = poll
    abr._TRANSFER_TEST_TASKS[1] = test
    abr._INTERACTION_SESSION_EXPIRE_TASKS[1] = expire

    try:
        await abr.stop_interaction_bot_manager()
        assert poll.cancelled() or poll.done()
        assert test.cancelled() or test.done()
        assert not expire.cancelled()
        assert 1 in abr._INTERACTION_SESSION_EXPIRE_TASKS
        assert abr._INTERACTION_TASKS == {}
        assert abr._TRANSFER_TEST_TASKS == {}
    finally:
        for t in (poll, test, expire):
            t.cancel()
        await asyncio.gather(poll, test, expire, return_exceptions=True)
        abr._INTERACTION_TASKS.clear()
        abr._TRANSFER_TEST_TASKS.clear()
        abr._INTERACTION_SESSION_EXPIRE_TASKS.clear()


@pytest.mark.asyncio
async def test_is_event_source_delivery_respects_modules() -> None:
    db = _FakeSettingsDB(
        {
            "interaction_bot_enabled": {"enabled": False, "generation": 1},
            "webhooks_enabled": {"enabled": False, "generation": 2},
        }
    )
    await caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    assert caps.is_event_source_delivery_enabled("userbot") is True
    assert caps.is_event_source_delivery_enabled("interaction_bot") is False
    assert caps.is_event_source_delivery_enabled("webhook") is False


@pytest.mark.asyncio
async def test_dispatch_webhook_event_rejects_when_module_disabled(monkeypatch) -> None:
    from app.worker.plugins import loader as loader_mod

    caps._reset_for_tests()
    caps._DESIRED["webhooks"] = False
    caps._CACHE_READY = True

    with pytest.raises(RuntimeError, match="webhooks module disabled"):
        await loader_mod.dispatch_webhook_event(1, {"hook_key": "default", "body": {}})

    caps._reset_for_tests()


@pytest.mark.asyncio
async def test_reload_config_payload_source_platform_capabilities_refresh(monkeypatch) -> None:
    """reload_account_config 应刷新能力缓存（platform_capabilities 热切换复用路径）。"""

    from app.worker.plugins import loader as loader_mod

    state = SimpleNamespace(
        generation=1,
        redis=None,
        instances={},
        contexts={},
        account_id=1,
        owner_tg_user_id=None,
        sudo_users={},
        log_incoming_messages=False,
        ignored_peers=set(),
        scheduler=None,
    )
    loader_mod._STATES[1] = state  # type: ignore[assignment]
    refresh = AsyncMock(return_value=caps.get_snapshot())
    monkeypatch.setattr(caps, "refresh_cache_from_db", refresh)
    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", AsyncMock(return_value=False))
    monkeypatch.setattr(loader_mod, "_refresh_interaction_text_guard_cache", AsyncMock())
    monkeypatch.setattr(loader_mod, "_refresh_userbot_session_chat_cache", AsyncMock())
    monkeypatch.setattr(loader_mod, "_log", AsyncMock())

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return None

        async def execute(self, *args, **kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _Sess())

    try:
        await loader_mod.reload_account_config(
            1,
            {
                "source": "platform_capabilities",
                "module_key": "ai",
                "generation": 3,
                "enabled": False,
            },
        )
        refresh.assert_awaited()
    finally:
        loader_mod._STATES.pop(1, None)
