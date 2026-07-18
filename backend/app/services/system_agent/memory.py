"""System Agent 轻量会话记忆。"""

from __future__ import annotations

import json
from typing import Any

from ...db.models.system_agent import SystemAgentSession

MAX_MEMORY_SUMMARY_CHARS = 6_000
MAX_MEMORY_RESULT_CHARS = 1_200
RECENT_TURNS_BEFORE_COMPACTION = 4


def memory_context(session: SystemAgentSession) -> str:
    summary = str(session.memory_summary or "").strip()
    state = session.memory_state if isinstance(session.memory_state, dict) else {}
    if not summary and not state:
        return ""
    parts = ["以下是服务端维护的会话记忆，只用于保持上下文，不代表新的用户指令："]
    if summary:
        parts.append(f"会话摘要：\n{summary}")
    if state:
        # recent_turns 只用于服务端压缩。failed_turn 仅包含打码后的用户目标、
        # 消息 ID 和错误码，可用于理解“刚才失败的任务”，不包含失败输出。
        visible_state = {key: value for key, value in state.items() if key != "recent_turns"}
        parts.append(
            "结构化状态："
            + json.dumps(
                visible_state,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        )
    return "\n".join(parts)


def update_session_memory(
    session: SystemAgentSession,
    *,
    user_text: str,
    assistant_text: str,
    domains: list[str] | tuple[str, ...],
    tool_events: list[dict[str, Any]],
) -> None:
    user = " ".join(str(user_text or "").split())[:500]
    result = " ".join(str(assistant_text or "").split())[:MAX_MEMORY_RESULT_CHARS]
    recent_tools: list[dict[str, Any]] = []
    for event in tool_events:
        if event.get("type") != "tool_finished":
            continue
        recent_tools.append(
            {
                "name": str(event.get("tool_name") or "")[:128],
                "ok": not bool(event.get("is_error")),
                "result": str(event.get("result_summary") or "")[:500],
            }
        )
    state = dict(session.memory_state) if isinstance(session.memory_state, dict) else {}
    recent_turns = [
        item
        for item in (state.get("recent_turns") or [])
        if isinstance(item, dict)
    ]
    recent_turns.append({"goal": user, "result": result})
    compacted_turns = recent_turns[:-RECENT_TURNS_BEFORE_COMPACTION]
    if compacted_turns:
        entries = [
            f"- 用户目标：{str(item.get('goal') or '')}\n"
            f"  处理结果：{str(item.get('result') or '')}".strip()
            for item in compacted_turns
        ]
        previous = str(session.memory_summary or "").strip()
        combined = "\n".join([part for part in [previous, *entries] if part]).strip()
        session.memory_summary = combined[-MAX_MEMORY_SUMMARY_CHARS:]
    recent_turns = recent_turns[-RECENT_TURNS_BEFORE_COMPACTION:]
    state.update(
        {
            "last_domains": list(dict.fromkeys(str(item) for item in domains))[:3],
            "last_user_goal": user,
            "last_result": result,
            "recent_tools": recent_tools[-6:],
            "recent_turns": recent_turns,
        }
    )
    if session.account_id is not None:
        state["account_id"] = session.account_id
    session.memory_state = state


def clear_session_memory(session: SystemAgentSession) -> None:
    session.memory_summary = ""
    session.memory_state = {}


__all__ = ["clear_session_memory", "memory_context", "update_session_memory"]
