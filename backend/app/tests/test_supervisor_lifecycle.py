"""Worker Supervisor 生命周期、退避和故障关停回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models.account import ACCOUNT_STATUS_ACTIVE
from app.worker import supervisor


class _FakeProcess:
    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.pid = 12345
        self.terminated = False
        self.killed = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        del timeout


@pytest.fixture(autouse=True)
def _clear_supervisor_state() -> None:
    supervisor._WORKERS.clear()
    supervisor._WORKER_LOCKS.clear()


async def _run_one_monitor_iteration(monkeypatch: pytest.MonkeyPatch, *, now: float) -> None:
    calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(supervisor.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now)
    await supervisor._monitor_loop()


@pytest.mark.asyncio
async def test_stop_worker_terminates_locally_when_redis_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(alive=True)
    supervisor._WORKERS[7] = supervisor._WorkerHandle(account_id=7, process=process)
    redis = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError("redis down")))
    monkeypatch.setattr(supervisor, "get_redis", lambda: redis)
    monkeypatch.setattr(supervisor.asyncio, "sleep", AsyncMock())

    await supervisor.stop_worker(7)

    assert process.terminated is True
    assert supervisor._WORKERS[7].process is None
    assert supervisor._WORKERS[7].desired == "stopped"


def test_worker_entry_strips_updater_control_plane_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEPILOT_UPDATER_TOKEN", "web-secret")
    monkeypatch.setenv("UPDATER_TOKEN", "updater-secret")

    def _entry(account_id: int) -> None:
        assert account_id == 7
        assert "TELEPILOT_UPDATER_TOKEN" not in supervisor.os.environ
        assert "UPDATER_TOKEN" not in supervisor.os.environ

    monkeypatch.setattr(supervisor, "worker_entry", _entry)

    supervisor._worker_entry_without_control_plane_secrets(7)


@pytest.mark.asyncio
async def test_monitor_schedules_backoff_without_immediate_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(alive=False)
    handle = supervisor._WorkerHandle(account_id=8, process=process, started_at=99.0)
    supervisor._WORKERS[8] = handle
    monkeypatch.setattr(supervisor, "_account_status", AsyncMock(return_value=ACCOUNT_STATUS_ACTIVE))
    start_worker = AsyncMock()
    monkeypatch.setattr(supervisor, "start_worker", start_worker)

    await _run_one_monitor_iteration(monkeypatch, now=100.0)

    assert handle.fail_count == 1
    assert handle.next_retry_at == 100.0 + supervisor._BACKOFF[0]
    start_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_restarts_only_after_backoff_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = supervisor._WorkerHandle(
        account_id=9,
        process=None,
        fail_count=2,
        next_retry_at=50.0,
    )
    supervisor._WORKERS[9] = handle
    monkeypatch.setattr(supervisor, "_account_status", AsyncMock(return_value=ACCOUNT_STATUS_ACTIVE))
    start_worker = AsyncMock()
    monkeypatch.setattr(supervisor, "start_worker", start_worker)

    await _run_one_monitor_iteration(monkeypatch, now=50.0)

    start_worker.assert_awaited_once_with(9)
    assert handle.fail_count == 2


@pytest.mark.asyncio
async def test_monitor_survives_spawn_failure_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = supervisor._WorkerHandle(
        account_id=11,
        process=None,
        fail_count=1,
        next_retry_at=25.0,
    )
    supervisor._WORKERS[11] = handle
    monkeypatch.setattr(supervisor, "_account_status", AsyncMock(return_value=ACCOUNT_STATUS_ACTIVE))
    monkeypatch.setattr(
        supervisor,
        "start_worker",
        AsyncMock(side_effect=RuntimeError("spawn failed")),
    )

    await _run_one_monitor_iteration(monkeypatch, now=25.0)

    assert handle.next_retry_at == 0.0
    assert handle.desired == "running"


@pytest.mark.asyncio
async def test_monitor_resets_failure_count_only_after_stable_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = supervisor._WorkerHandle(
        account_id=10,
        process=_FakeProcess(alive=True),
        fail_count=3,
        started_at=10.0,
    )
    supervisor._WORKERS[10] = handle

    await _run_one_monitor_iteration(
        monkeypatch,
        now=10.0 + supervisor._STABLE_WINDOW_SECONDS,
    )

    assert handle.fail_count == 0
