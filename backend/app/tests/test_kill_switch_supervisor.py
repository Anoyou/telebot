"""紧急停用接口与 supervisor 的联动测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import rate_limit
from app.schemas.rate_limit import KillSwitchRequest
from app.services import account_bot_runtime, interaction_bot_runtime
from app.worker import supervisor


@pytest.mark.asyncio
async def test_kill_switch_enabled_stops_running_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """开启紧急停用时，API 必须直接停止当前 supervisor 托管的 worker。"""
    set_setting = AsyncMock()
    audit = AsyncMock()
    stop_running_workers = AsyncMock()
    start_active_workers = AsyncMock()
    publish = AsyncMock()
    stop_interaction = AsyncMock()
    start_interaction = AsyncMock()
    stop_account_bot = AsyncMock()
    start_account_bot = AsyncMock()
    monkeypatch.setattr(rate_limit, "_set_setting", set_setting)
    monkeypatch.setattr(rate_limit, "_audit", audit)
    monkeypatch.setattr(supervisor, "stop_running_workers", stop_running_workers)
    monkeypatch.setattr(supervisor, "start_active_workers", start_active_workers)
    monkeypatch.setattr(interaction_bot_runtime, "stop_interaction_bot_manager", stop_interaction)
    monkeypatch.setattr(interaction_bot_runtime, "start_interaction_bot_manager", start_interaction)
    monkeypatch.setattr(account_bot_runtime, "stop_account_bot_manager", stop_account_bot)
    monkeypatch.setattr(account_bot_runtime, "start_account_bot_manager", start_account_bot)
    monkeypatch.setattr(rate_limit, "get_redis", lambda: SimpleNamespace(publish=publish))

    result = await rate_limit.post_kill_switch(
        KillSwitchRequest(enabled=True),
        db=AsyncMock(),
        user=SimpleNamespace(id=7),
    )

    assert result == {"enabled": True}
    set_setting.assert_awaited_once()
    audit.assert_awaited_once()
    stop_running_workers.assert_awaited_once()
    start_active_workers.assert_not_awaited()
    publish.assert_awaited_once()
    stop_interaction.assert_awaited_once()
    start_interaction.assert_not_awaited()
    stop_account_bot.assert_awaited_once()
    start_account_bot.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_disabled_starts_active_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """解除紧急停用时，API 必须恢复 DB 中 active 账号的 worker。"""
    set_setting = AsyncMock()
    audit = AsyncMock()
    stop_running_workers = AsyncMock()
    start_active_workers = AsyncMock()
    publish = AsyncMock()
    stop_interaction = AsyncMock()
    start_interaction = AsyncMock(return_value=0)
    stop_account_bot = AsyncMock()
    start_account_bot = AsyncMock()
    monkeypatch.setattr(rate_limit, "_set_setting", set_setting)
    monkeypatch.setattr(rate_limit, "_audit", audit)
    monkeypatch.setattr(supervisor, "stop_running_workers", stop_running_workers)
    monkeypatch.setattr(supervisor, "start_active_workers", start_active_workers)
    monkeypatch.setattr(interaction_bot_runtime, "stop_interaction_bot_manager", stop_interaction)
    monkeypatch.setattr(interaction_bot_runtime, "start_interaction_bot_manager", start_interaction)
    monkeypatch.setattr(account_bot_runtime, "stop_account_bot_manager", stop_account_bot)
    monkeypatch.setattr(account_bot_runtime, "start_account_bot_manager", start_account_bot)
    monkeypatch.setattr(rate_limit, "get_redis", lambda: SimpleNamespace(publish=publish))

    result = await rate_limit.post_kill_switch(
        KillSwitchRequest(enabled=False),
        db=AsyncMock(),
        user=SimpleNamespace(id=7),
    )

    assert result == {"enabled": False}
    set_setting.assert_awaited_once()
    audit.assert_awaited_once()
    stop_running_workers.assert_not_awaited()
    start_active_workers.assert_awaited_once()
    publish.assert_awaited_once()
    stop_interaction.assert_not_awaited()
    start_interaction.assert_awaited_once()
    stop_account_bot.assert_not_awaited()
    start_account_bot.assert_awaited_once()


@pytest.mark.asyncio
async def test_kill_switch_reports_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "_set_setting", AsyncMock())
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())
    monkeypatch.setattr(
        supervisor,
        "stop_running_workers",
        AsyncMock(side_effect=RuntimeError("worker still alive")),
    )
    monkeypatch.setattr(interaction_bot_runtime, "stop_interaction_bot_manager", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "stop_account_bot_manager", AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: SimpleNamespace(publish=publish))

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.post_kill_switch(
            KillSwitchRequest(enabled=True),
            db=AsyncMock(),
            user=SimpleNamespace(id=7),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "KILL_SWITCH_PARTIAL_FAILURE"
    publish.assert_awaited_once()
