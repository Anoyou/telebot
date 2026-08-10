from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app.services.gateway_runtime import (
    TEMPORARY_GATEWAY_PROVIDER_ID,
    GatewayRuntimeManager,
    GatewayRuntimeStatus,
    acquire_gateway_configuration_db_lock,
    restore_committed_gateway_snapshot,
    rollback_and_restore_gateway,
    temporary_gateway_provider,
)
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_proxy_service import resolve_proxy_url


def _provider(*, backend: str = "codex_gateway", api_format: str = "responses") -> LLMProviderDTO:
    return LLMProviderDTO(
        id=7,
        name="gateway",
        provider="openai",
        execution_backend=backend,
        api_format=api_format,
        base_url="https://upstream.example/v1",
        api_key_enc="encrypted",
        default_model="gpt-x",
    )


@pytest.mark.asyncio
async def test_direct_only_does_not_start_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = GatewayRuntimeManager(binary="/missing")
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_start_locked", start)
    status = await manager.reconcile([_provider(backend="direct")])
    assert status.state == "not_required"
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_binary_is_degraded_not_global_failure() -> None:
    manager = GatewayRuntimeManager(binary="/definitely/missing")
    status = await manager.reconcile([_provider()])
    assert status.state == "degraded"
    assert status.required is True
    assert "不存在" in (status.error or "")
    assert manager._db_recovery_task is not None  # noqa: SLF001
    await manager.shutdown()


@pytest.mark.asyncio
async def test_snapshot_sync_is_revisioned_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = GatewayRuntimeManager(binary="/fake")
    manager._process = type("Process", (), {"returncode": None})()  # type: ignore[assignment]
    monkeypatch.setattr("app.services.gateway_runtime.decrypt_str", lambda _: "plain-key")
    request = AsyncMock(side_effect=[{"revision": 1}])
    monkeypatch.setattr(manager, "_request_json", request)
    first = await manager.reconcile([_provider()])
    second = await manager.reconcile([_provider()])
    assert first.state == second.state == "ready"
    assert first.revision == second.revision == 1
    assert request.await_count == 1
    snapshot = request.await_args.kwargs["json_body"]
    assert snapshot["gateway_protocol_version"] == "2"
    assert snapshot["codex_client_version"]
    provider = snapshot["providers"][0]
    assert provider["base_url"] == "https://upstream.example/v1"
    assert provider["liveness_compatibility_headers"] == {}
    assert provider["models_endpoints"] == [
        "https://upstream.example/v1/models",
        "https://upstream.example/models",
    ]


@pytest.mark.asyncio
async def test_codex_version_change_triggers_new_gateway_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = GatewayRuntimeManager(binary="/fake")
    manager._process = type("Process", (), {"returncode": None})()  # type: ignore[assignment]
    monkeypatch.setattr("app.services.gateway_runtime.decrypt_str", lambda _: "plain-key")
    version = {"value": "0.198.0"}
    monkeypatch.setattr(
        "app.services.gateway_runtime.llm_identity.current_client_versions",
        lambda: {"codex_tui": version["value"]},
    )
    monkeypatch.setattr(
        "app.services.gateway_runtime.llm_identity.default_client_versions",
        lambda: {"codex_tui": "0.145.0"},
    )
    request = AsyncMock(side_effect=[{"revision": 1}, {"revision": 2}])
    monkeypatch.setattr(manager, "_request_json", request)

    first = await manager.reconcile([_provider()])
    version["value"] = "0.199.0"
    second = await manager.reconcile([_provider()])

    assert first.revision == 1
    assert second.revision == 2
    assert request.await_count == 2
    assert request.await_args_list[0].kwargs["json_body"]["codex_client_version"] == "0.198.0"
    assert request.await_args_list[1].kwargs["json_body"]["codex_client_version"] == "0.199.0"


@pytest.mark.asyncio
async def test_non_responses_provider_is_rejected_during_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = GatewayRuntimeManager(binary="/fake")
    process = SimpleNamespace(
        returncode=None,
        terminate=lambda: setattr(process, "returncode", 0),
        wait=AsyncMock(return_value=0),
    )
    manager._process = process  # type: ignore[assignment]
    status = await manager.reconcile([_provider(api_format="chat_completions")])
    assert status.state == "degraded"
    assert "仅支持 Responses" in (status.error or "")
    assert manager._process is None  # noqa: SLF001
    assert manager._db_recovery_task is not None  # noqa: SLF001
    await manager.shutdown()


@pytest.mark.asyncio
async def test_preflight_does_not_keep_previous_config_error_sticky() -> None:
    manager = GatewayRuntimeManager(binary="/fake")
    manager._process = type("Process", (), {"returncode": None})()  # type: ignore[assignment]
    manager._version = "test"
    manager._state = "degraded"
    manager._error = "previous snapshot rejected"

    status = await manager.preflight()

    assert status.state == "ready"
    assert status.error is None


@pytest.mark.asyncio
async def test_provider_dtos_include_resolved_provider_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.models.command import LLMProvider
    from app.services import llm_proxy_service

    resolve = AsyncMock(return_value="socks5://user:pass@proxy.example:1080")
    monkeypatch.setattr(llm_proxy_service, "resolve_proxy_url", resolve)
    row = LLMProvider(
        id=12,
        name="proxied",
        provider="openai",
        api_key_enc="encrypted",
        base_url="https://api.example/v1",
        default_model="gpt-x",
        api_format="responses",
        execution_backend="codex_gateway",
        proxy_id=8,
    )

    providers = await GatewayRuntimeManager()._provider_dtos(object(), [row])

    assert providers[0].proxy_url == "socks5://user:pass@proxy.example:1080"
    resolve.assert_awaited_once_with(ANY, 8)


@pytest.mark.asyncio
async def test_postgres_configuration_lock_uses_transaction_advisory_lock() -> None:
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=AsyncMock(),
    )

    await acquire_gateway_configuration_db_lock(db)

    statement, parameters = db.execute.await_args.args
    assert "pg_advisory_xact_lock" in str(statement)
    assert isinstance(parameters["lock_key"], int)


@pytest.mark.asyncio
async def test_proxy_resolution_refreshes_session_identity_map() -> None:
    proxy = SimpleNamespace(
        type="http",
        host="proxy.example",
        port=8080,
        username=None,
        password_enc=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=proxy))

    assert await resolve_proxy_url(db, 8) == "http://proxy.example:8080"
    db.get.assert_awaited_once_with(ANY, 8, populate_existing=True)


@pytest.mark.asyncio
async def test_proxy_resolution_rejects_legacy_mtproxy_instead_of_direct() -> None:
    proxy = SimpleNamespace(
        type="mtproxy",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=proxy))

    with pytest.raises(ValueError, match="拒绝回落直连"):
        await resolve_proxy_url(db, 8)


@pytest.mark.asyncio
async def test_proxy_resolution_rejects_legacy_type_even_with_supported_url_host() -> None:
    proxy = SimpleNamespace(
        type="mtproxy",
        host="http://proxy.example:8080",
        port=443,
        username=None,
        password_enc=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=proxy))

    with pytest.raises(ValueError, match="拒绝回落直连"):
        await resolve_proxy_url(db, 8)


@pytest.mark.asyncio
async def test_compensation_reacquires_db_lock_before_reading_committed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime
    from app.services.gateway_runtime import GatewayRuntimeStatus, gateway_runtime_manager

    events: list[str] = []
    restore_db = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
        rollback=AsyncMock(),
    )

    class _SessionContext:
        async def __aenter__(self):
            return restore_db

        async def __aexit__(self, *_args):
            return False

    async def acquire(_db) -> None:  # noqa: ANN001
        events.append("lock")

    async def reconcile(_db, **_kwargs):  # noqa: ANN001
        events.append("reconcile")
        return GatewayRuntimeStatus("ready", True, 3, 1, version="test")

    monkeypatch.setattr(gateway_runtime, "AsyncSessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(gateway_runtime, "acquire_gateway_configuration_db_lock", acquire)
    monkeypatch.setattr(gateway_runtime_manager, "reconcile_from_session", reconcile)

    status = await restore_committed_gateway_snapshot()

    assert status.state == "ready"
    assert events == ["lock", "reconcile", "commit"]


@pytest.mark.asyncio
async def test_compensation_restores_snapshot_even_when_transaction_rollback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    db = SimpleNamespace(rollback=AsyncMock(side_effect=RuntimeError("rollback failed")))
    restore = AsyncMock(
        return_value=GatewayRuntimeStatus("ready", True, 3, 1, version="test")
    )
    monkeypatch.setattr(gateway_runtime, "restore_committed_gateway_snapshot", restore)

    restored = await rollback_and_restore_gateway(db)

    assert restored is False
    restore.assert_awaited_once()


@pytest.mark.asyncio
async def test_compensation_finishes_before_propagating_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    restore_started = asyncio.Event()
    allow_restore = asyncio.Event()
    restored = False

    async def restore() -> GatewayRuntimeStatus:
        nonlocal restored
        restore_started.set()
        await allow_restore.wait()
        restored = True
        return GatewayRuntimeStatus("ready", True, 3, 1, version="test")

    monkeypatch.setattr(gateway_runtime, "restore_committed_gateway_snapshot", restore)
    task = asyncio.create_task(
        rollback_and_restore_gateway(SimpleNamespace(rollback=AsyncMock()))
    )
    await restore_started.wait()

    task.cancel()
    allow_restore.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored is True


@pytest.mark.asyncio
async def test_compensation_task_cancellation_marks_gateway_degraded_without_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    async def restore() -> GatewayRuntimeStatus:
        raise asyncio.CancelledError

    degraded = AsyncMock()
    monkeypatch.setattr(gateway_runtime, "restore_committed_gateway_snapshot", restore)
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "mark_control_plane_degraded",
        degraded,
    )

    restored = await asyncio.wait_for(
        rollback_and_restore_gateway(SimpleNamespace(rollback=AsyncMock())),
        timeout=1,
    )

    assert restored is False
    degraded.assert_awaited_once()
    assert "补偿任务被取消" in str(degraded.await_args.args[0])


@pytest.mark.asyncio
async def test_compensation_degraded_marker_failure_does_not_mask_restore_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    monkeypatch.setattr(
        gateway_runtime,
        "restore_committed_gateway_snapshot",
        AsyncMock(side_effect=RuntimeError("restore failed")),
    )
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "mark_control_plane_degraded",
        AsyncMock(side_effect=RuntimeError("degraded marker failed")),
    )

    restored = await rollback_and_restore_gateway(
        SimpleNamespace(rollback=AsyncMock())
    )

    assert restored is False


@pytest.mark.asyncio
async def test_temporary_gateway_provider_restores_committed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    committed = _provider()
    draft = _provider()
    rows_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(id=7)]),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=rows_result),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "_provider_dtos",
        AsyncMock(return_value=[committed]),
    )
    reconcile = AsyncMock(
        side_effect=[
            GatewayRuntimeStatus("ready", True, 2, 2, version="test"),
            GatewayRuntimeStatus("ready", True, 3, 1, version="test"),
        ],
    )
    monkeypatch.setattr(gateway_runtime.gateway_runtime_manager, "reconcile", reconcile)

    async with temporary_gateway_provider(db, draft) as temporary:
        assert temporary.id == TEMPORARY_GATEWAY_PROVIDER_ID
        assert temporary.execution_backend == "codex_gateway"

    assert reconcile.await_count == 2
    first_snapshot = reconcile.await_args_list[0].args[0]
    assert [provider.id for provider in first_snapshot] == [7, TEMPORARY_GATEWAY_PROVIDER_ID]
    assert reconcile.await_args_list[1].args[0] == [committed]


@pytest.mark.asyncio
async def test_temporary_gateway_provider_waits_for_restore_before_propagating_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    committed = _provider()
    draft = _provider()
    rows_result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(id=7)]),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=rows_result),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "_provider_dtos",
        AsyncMock(return_value=[committed]),
    )
    restore_started = asyncio.Event()
    allow_restore = asyncio.Event()
    calls = 0

    async def reconcile(_providers, **_kwargs):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            return GatewayRuntimeStatus("ready", True, 2, 2, version="test")
        restore_started.set()
        await allow_restore.wait()
        return GatewayRuntimeStatus("ready", True, 3, 1, version="test")

    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "reconcile",
        reconcile,
    )
    entered = asyncio.Event()
    hold_request = asyncio.Event()

    async def use_temporary_provider() -> None:
        async with temporary_gateway_provider(db, draft):
            entered.set()
            await hold_request.wait()

    task = asyncio.create_task(use_temporary_provider())
    await entered.wait()
    task.cancel()
    await restore_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert gateway_runtime.gateway_provider_transaction_lock.locked()

    allow_restore.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == 2
    assert not gateway_runtime.gateway_provider_transaction_lock.locked()


@pytest.mark.asyncio
async def test_temporary_gateway_provider_marks_degraded_when_restore_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    rows_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=rows_result),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "_provider_dtos",
        AsyncMock(return_value=[]),
    )
    reconcile = AsyncMock(
        side_effect=[
            GatewayRuntimeStatus("ready", True, 1, 1, version="test"),
            asyncio.CancelledError(),
        ],
    )
    degraded = AsyncMock()
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "reconcile",
        reconcile,
    )
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "mark_control_plane_degraded",
        degraded,
    )

    with pytest.raises(RuntimeError, match="补偿任务被取消"):
        async with temporary_gateway_provider(db, _provider()):
            pass

    degraded.assert_awaited_once()
    assert not gateway_runtime.gateway_provider_transaction_lock.locked()


@pytest.mark.asyncio
async def test_unsaved_temporary_gateway_provider_uses_reserved_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import gateway_runtime

    draft = _provider()
    draft.id = 0
    rows_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=rows_result),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )
    monkeypatch.setattr(
        gateway_runtime.gateway_runtime_manager,
        "_provider_dtos",
        AsyncMock(return_value=[]),
    )
    reconcile = AsyncMock(
        side_effect=[
            GatewayRuntimeStatus("ready", True, 1, 1, version="test"),
            GatewayRuntimeStatus("not_required", False, 1, 0, version="test"),
        ],
    )
    monkeypatch.setattr(gateway_runtime.gateway_runtime_manager, "reconcile", reconcile)

    async with temporary_gateway_provider(db, draft) as temporary:
        assert temporary.id == TEMPORARY_GATEWAY_PROVIDER_ID

    assert reconcile.await_args_list[0].args[0][0].id == TEMPORARY_GATEWAY_PROVIDER_ID
