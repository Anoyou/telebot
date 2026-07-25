"""scheduler.save：agent_prompt 便捷字段与防套娃。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models.system_agent import SESSION_ORIGIN_INTERACTIVE, SESSION_ORIGIN_SCHEDULED
from app.services.system_agent.context import ToolContext
from app.services.system_agent.tools import scheduler as scheduler_tools


def test_normalize_agent_prompt_convenience_fields() -> None:
    out = scheduler_tools._normalize_scheduler_save_args(
        {
            "account_id": 1,
            "name": "晨检",
            "action_type": "agent_prompt",
            "prompt": "帮我盘一遍异常日志",
            "cron": "0 9 * * *",
            "report_channel": -100123,
        }
    )
    config = out["config"]
    assert config["kind"] == "cron"
    assert config["cron"] == "0 9 * * *"
    assert config["action"]["type"] == "agent_prompt"
    assert config["action"]["prompt"] == "帮我盘一遍异常日志"
    assert config["action"]["target_chat_id"] == -100123


def test_schedule_label_daily_nine() -> None:
    assert (
        scheduler_tools._agent_prompt_schedule_label({"kind": "cron", "cron": "0 9 * * *"})
        == "每天 09:00"
    )
    assert (
        scheduler_tools._agent_prompt_schedule_label({"kind": "cron", "cron": "0 0 9 * * *"})
        == "每天 09:00"
    )


def test_agent_prompt_preview_summary() -> None:
    summary = scheduler_tools._agent_prompt_preview_summary(
        name="晨检",
        config={
            "kind": "cron",
            "cron": "0 9 * * *",
            "action": {"type": "agent_prompt", "prompt": "巡检异常日志", "target_chat_id": 42},
        },
        mode="create",
    )
    assert "将创建定时 Agent 任务" in summary
    assert "每天 09:00" in summary
    assert "巡检异常日志" in summary


@pytest.mark.asyncio
async def test_save_preview_rejects_nested_agent_prompt() -> None:
    session = SimpleNamespace(origin=SESSION_ORIGIN_SCHEDULED, id="sched-1")
    ctx = ToolContext(
        db=AsyncMock(),
        channel="web",
        role="admin",
        session=session,  # type: ignore[arg-type]
        account_id=1,
        web_user_id=1,
    )
    with pytest.raises(ValueError, match="防套娃"):
        await scheduler_tools.save_preview(
            ctx,
            {
                "account_id": 1,
                "action_type": "agent_prompt",
                "prompt": "再套一层",
                "cron": "0 9 * * *",
                "report_channel": 1,
            },
        )


@pytest.mark.asyncio
async def test_save_preview_agent_prompt_ok_on_interactive() -> None:
    session = SimpleNamespace(origin=SESSION_ORIGIN_INTERACTIVE, id="chat-1")
    ctx = ToolContext(
        db=AsyncMock(),
        channel="web",
        role="admin",
        session=session,  # type: ignore[arg-type]
        account_id=3,
        web_user_id=1,
    )
    # get_timezone_name 走 db；打桩
    preview = await scheduler_tools.save_preview(
        ctx,
        {
            "account_id": 3,
            "action_type": "agent_prompt",
            "prompt": "帮我盘一遍异常日志",
            "cron": "0 9 * * *",
            "report_channel": 99,
            "name": "晨间巡检",
        },
    )
    assert preview["mode"] == "create"
    assert "将创建定时 Agent 任务" in preview["summary"]
    assert "每天 09:00" in preview["summary"]
    assert preview["config"]["action"]["type"] == "agent_prompt"
