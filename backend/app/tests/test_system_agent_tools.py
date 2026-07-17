"""System Agent 只读工具：时区、范围、交互规则隔离。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.system_agent.context import ToolContext
from app.services.system_agent.tools import ledger as ledger_tools
from app.services.system_agent.tools import rules as rules_tools
from app.services.system_agent.tools import system as system_tools
from app.services.system_agent.tools._helpers import (
    account_scope_filter,
    clamp_limit,
    local_day_bounds_utc,
)


def test_clamp_limit() -> None:
    assert clamp_limit(None) == 20
    assert clamp_limit("x", default=10) == 10
    assert clamp_limit(0) == 1
    assert clamp_limit(9999, maximum=500) == 500


def test_account_scope_filter_bot_forces_context() -> None:
    assert account_scope_filter(99, context_account_id=7, channel="bot") == 7
    assert account_scope_filter(None, context_account_id=7, channel="bot") == 7
    assert account_scope_filter(99, context_account_id=7, channel="web") == 99
    assert account_scope_filter(None, context_account_id=7, channel="web") == 7
    assert account_scope_filter(None, context_account_id=None, channel="web") is None


def test_local_day_bounds_utc_shanghai() -> None:
    # 2026-07-18 03:00 UTC = 2026-07-18 11:00 Asia/Shanghai → 本地日 7/18
    day = datetime(2026, 7, 18, 3, 0, tzinfo=UTC)
    start, end = local_day_bounds_utc("Asia/Shanghai", day=day)
    assert start == datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 18, 16, 0, tzinfo=UTC)


def test_local_day_bounds_utc_new_york_summer() -> None:
    # EDT (UTC-4): 本地 7/18 对应 UTC 7/18 04:00 → 7/19 04:00
    day = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    start, end = local_day_bounds_utc("America/New_York", day=day)
    assert start == datetime(2026, 7, 18, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 19, 4, 0, tzinfo=UTC)


def test_local_day_bounds_utc_invalid_timezone_falls_back() -> None:
    day = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    start, end = local_day_bounds_utc("Not/AZone", day=day)
    assert start.tzinfo is UTC
    assert (end - start).total_seconds() == 86400


@pytest.mark.asyncio
async def test_rules_list_rejects_interaction_feature() -> None:
    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin", account_id=1)
    result = await rules_tools.list_rules(ctx, {"feature_key": "interaction"})
    assert result["error"] == "wrong_tool"
    assert "interaction.list_rules" in result["message"]


@pytest.mark.asyncio
async def test_ledger_summary_today_uses_timezone(monkeypatch) -> None:
    captured: dict = {}

    async def fake_tz(_db):
        return "Asia/Shanghai"

    async def fake_summarize(db, filters):  # noqa: ANN001
        captured["filters"] = filters
        return SimpleNamespace(
            income="10",
            payout="2",
            net="8",
            count=3,
            by_day=[],
            by_chat=[],
        )

    monkeypatch.setattr(ledger_tools, "get_timezone_name", fake_tz)
    monkeypatch.setattr(ledger_tools.ledger_service, "summarize_ledger", fake_summarize)

    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin", account_id=5)
    result = await ledger_tools.summary(ctx, {"day": "今日", "account_id": 5})
    assert result["day"] == "today"
    assert result["timezone"] == "Asia/Shanghai"
    assert result["income"] == "10"
    assert captured["filters"].account_id == 5
    assert captured["filters"].since is not None
    assert captured["filters"].until is not None
    assert captured["filters"].until > captured["filters"].since


@pytest.mark.asyncio
async def test_system_get_context_shape(monkeypatch) -> None:
    async def fake_flags(_db):
        return {
            "timezone": "UTC",
            "command_prefix": "/",
            "ai_enabled": True,
            "agent_config": {
                "enabled": True,
                "provider_id": 1,
                "model": "m",
                "max_steps": 8,
                "max_tool_calls": 24,
                "session_token_limit": 16000,
            },
        }

    monkeypatch.setattr(system_tools, "load_system_context_flags", fake_flags)
    ctx = ToolContext(db=AsyncMock(), channel="web", role="admin", account_id=None)
    result = await system_tools.get_context(ctx, {})
    assert result["timezone"] == "UTC"
    assert result["system_agent"]["enabled"] is True
    assert "version" in result
