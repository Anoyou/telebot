from __future__ import annotations

from types import SimpleNamespace

from app.services.system_agent.memory import (
    ENTRY_ANCHOR,
    MAX_MEMORY_SUMMARY_CHARS,
    clear_session_memory,
    memory_context,
    trim_summary_to_limit,
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


def test_regenerate_context_excludes_the_latest_turn() -> None:
    session = SimpleNamespace(
        memory_summary="",
        memory_state={
            "last_domains": ["logs"],
            "last_user_goal": "最新问题",
            "last_result": "旧回答",
            "recent_tools": [{"name": "logs.list"}],
            "recent_turns": [
                {"goal": "较早问题", "result": "较早回答"},
                {"goal": "最新问题", "result": "旧回答"},
            ],
        },
    )

    context = memory_context(session, exclude_latest_turn=True)

    assert "最新问题" not in context
    assert "旧回答" not in context
    assert "较早问题" in context
    assert "较早回答" in context
    assert "logs.list" not in context


def test_trim_summary_drops_oldest_entries_not_half_lines() -> None:
    pad = "详" * 200
    entries = [
        f"{ENTRY_ANCHOR}旧目标 {i} {pad}\n  处理结果：旧结果 {i} {pad}"
        for i in range(12)
    ]
    bloated = "\n".join(entries)
    assert len(bloated) > MAX_MEMORY_SUMMARY_CHARS
    trimmed = trim_summary_to_limit(bloated)
    assert len(trimmed) <= MAX_MEMORY_SUMMARY_CHARS
    assert "旧目标 0" not in trimmed
    assert "旧目标 11" in trimmed
    # 所有条目起点都是完整锚点
    for chunk in trimmed.split(ENTRY_ANCHOR)[1:]:
        assert chunk.strip()
        assert "处理结果" in chunk


def test_trim_summary_heals_legacy_half_prefix_on_update() -> None:
    """历史硬砍产生的半条前缀会在下次更新时作为独立条目被优先丢掉。"""
    pad = "字" * 100
    session = SimpleNamespace(
        memory_summary="半截残留文本没有锚点 " + pad + "\n"
        + "\n".join(
            f"{ENTRY_ANCHOR}目标{i} {pad}\n  处理结果：结果{i} {pad}" for i in range(20)
        ),
        memory_state={"recent_turns": [{"goal": f"g{i}", "result": f"r{i}"} for i in range(4)]},
        account_id=None,
    )
    # 再压一轮以触发 compact
    for index in range(2):
        update_session_memory(
            session,
            user_text=f"新目标 {index}",
            assistant_text=f"新结果 {index}",
            domains=["logs"],
            tool_events=[],
        )
    assert "半截残留" not in (session.memory_summary or "")
    assert len(session.memory_summary or "") <= MAX_MEMORY_SUMMARY_CHARS
    assert int(session.memory_state.get("summary_rev") or 0) >= 1
