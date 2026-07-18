from __future__ import annotations

from types import SimpleNamespace

from app.services.system_agent.memory import (
    clear_session_memory,
    memory_context,
    update_session_memory,
)


def test_successful_turn_updates_summary_and_working_memory() -> None:
    session = SimpleNamespace(
        memory_summary="",
        memory_state={},
        account_id=7,
    )

    update_session_memory(
        session,
        user_text="帮我看今天收入",
        assistant_text="今天收入 100 元",
        domains=["ledger"],
        tool_events=[
            {
                "type": "tool_finished",
                "tool_name": "ledger.summary",
                "is_error": False,
                "result_summary": {"income": 100},
            }
        ],
    )

    assert session.memory_summary == ""
    assert session.memory_state["last_domains"] == ["ledger"]
    assert session.memory_state["account_id"] == 7
    assert session.memory_state["recent_tools"][0]["name"] == "ledger.summary"
    assert "服务端维护的会话记忆" in memory_context(session)
    assert "recent_turns" not in memory_context(session)

    for index in range(4):
        update_session_memory(
            session,
            user_text=f"后续目标 {index}",
            assistant_text=f"后续结果 {index}",
            domains=["ledger"],
            tool_events=[],
        )

    assert "帮我看今天收入" in session.memory_summary
    assert len(session.memory_state["recent_turns"]) == 4


def test_clear_session_memory_resets_summary_and_state() -> None:
    session = SimpleNamespace(
        memory_summary="old",
        memory_state={"last_domains": ["logs"]},
    )

    clear_session_memory(session)

    assert session.memory_summary == ""
    assert session.memory_state == {}
    assert memory_context(session) == ""
