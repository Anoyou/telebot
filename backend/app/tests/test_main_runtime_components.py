"""关键运行组件的启动自愈与 readiness 回归测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import main


@pytest.fixture(autouse=True)
def _restore_runtime_component_state():
    components = dict(main._RUNTIME_COMPONENTS)
    errors = dict(main._RUNTIME_COMPONENT_ERRORS)
    yield
    main._RUNTIME_COMPONENTS.clear()
    main._RUNTIME_COMPONENTS.update(components)
    main._RUNTIME_COMPONENT_ERRORS.clear()
    main._RUNTIME_COMPONENT_ERRORS.update(errors)


@pytest.mark.asyncio
async def test_start_runtime_component_records_failure_then_recovers() -> None:
    failing = AsyncMock(side_effect=RuntimeError("boom"))
    healthy = AsyncMock(return_value=None)

    assert await main._start_runtime_component("worker_supervisor", failing) is False
    assert main._RUNTIME_COMPONENTS["worker_supervisor"] is False
    assert "RuntimeError: boom" in main._RUNTIME_COMPONENT_ERRORS["worker_supervisor"]

    assert await main._start_runtime_component("worker_supervisor", healthy) is True
    assert main._RUNTIME_COMPONENTS["worker_supervisor"] is True
    assert "worker_supervisor" not in main._RUNTIME_COMPONENT_ERRORS


@pytest.mark.asyncio
async def test_retry_runtime_component_retries_until_success(monkeypatch) -> None:
    attempts = 0

    async def _starter() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")

    sleep = AsyncMock(return_value=None)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    main._RUNTIME_COMPONENTS["account_bot_manager"] = False

    await main._retry_runtime_component("account_bot_manager", _starter)

    assert attempts == 2
    assert sleep.await_count == 2
    assert main._RUNTIME_COMPONENTS["account_bot_manager"] is True
