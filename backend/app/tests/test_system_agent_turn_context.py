from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.system_agent.memory import memory_context
from app.services.system_agent.turn_context import (
    clear_failed_turn,
    failed_turn_state,
    is_retry_reference,
    remember_failed_turn,
)


@pytest.mark.parametrize(
    "text",
    ["重试", "再试一次", "继续刚才的", "重新试试", "  重试！  "],
)
def test_short_retry_references_are_deterministic(text: str) -> None:
    assert is_retry_reference(text) is True


@pytest.mark.parametrize(
    "text",
    ["重试定时任务", "继续查看日志", "重新试试另一个账号", "交互里有哪些规则？"],
)
def test_normal_requests_do_not_match_retry_reference(text: str) -> None:
    assert is_retry_reference(text) is False


def test_failed_turn_state_is_replaced_and_cleared_by_message_id() -> None:
    session = SimpleNamespace(memory_state={"last_domains": ["interaction"]})

    remember_failed_turn(
        session,
        message_id=10,
        user_goal="交互里有哪些规则？",
        error_code="UPSTREAM_503",
    )
    assert failed_turn_state(session) == {
        "message_id": 10,
        "user_goal": "交互里有哪些规则？",
        "error_code": "UPSTREAM_503",
    }

    remember_failed_turn(
        session,
        message_id=11,
        user_goal="查看日志",
        error_code="TIMEOUT",
    )
    clear_failed_turn(session, message_id=10)
    assert failed_turn_state(session)["message_id"] == 11

    clear_failed_turn(session, message_id=11)
    assert failed_turn_state(session) is None
    assert session.memory_state == {"last_domains": ["interaction"]}


def test_memory_context_exposes_only_safe_failed_task_state() -> None:
    session = SimpleNamespace(memory_summary="", memory_state={})
    remember_failed_turn(
        session,
        message_id=12,
        user_goal="交互里有哪些规则？",
        error_code="UPSTREAM_503",
    )

    context = memory_context(session)

    assert "交互里有哪些规则？" in context
    assert "UPSTREAM_503" in context
    assert "message_id" in context
    assert "失败的模型答案" not in context
    assert "失败的工具输出" not in context
