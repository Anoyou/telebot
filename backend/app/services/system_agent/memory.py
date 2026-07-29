"""System Agent 轻量会话记忆。"""

from __future__ import annotations

import json
from typing import Any

from ...db.models.system_agent import SystemAgentSession

# CJK 近似一字一 token：3000 字符 ≈ 3k token/步重发预算（相对旧 6000 更克制）
MAX_MEMORY_SUMMARY_CHARS = 3_000
MAX_MEMORY_RESULT_CHARS = 600
RECENT_TURNS_BEFORE_COMPACTION = 4
# 滚动摘要压缩阈值（WP3）；条目裁剪仍用 MAX_MEMORY_SUMMARY_CHARS
MEMORY_COMPRESS_THRESHOLD_CHARS = 2_000
ENTRY_ANCHOR = "- 用户目标："


def _split_summary_entries(summary: str) -> list[str]:
    """按条目锚点切分摘要；返回非空条目文本（含锚点前缀）。"""
    text = str(summary or "").strip()
    if not text:
        return []
    if ENTRY_ANCHOR not in text:
        return [text]
    parts = text.split(ENTRY_ANCHOR)
    entries: list[str] = []
    # 锚点前的游离前缀（历史半条）单独成条，便于下一次整条丢弃
    head = parts[0].strip()
    if head:
        entries.append(head)
    for part in parts[1:]:
        body = part.strip()
        if not body:
            continue
        entries.append(f"{ENTRY_ANCHOR}{body}")
    return entries


def trim_summary_to_limit(summary: str, limit: int = MAX_MEMORY_SUMMARY_CHARS) -> str:
    """超限时按条目边界丢最旧条目；单条自身超限才条内截断尾部。"""
    text = str(summary or "").strip()
    if not text or len(text) <= limit:
        return text
    entries = _split_summary_entries(text)
    if not entries:
        return text[-limit:]
    # 从头部丢最旧，直到 ≤ limit
    while len(entries) > 1 and len("\n".join(entries)) > limit:
        entries.pop(0)
    combined = "\n".join(entries).strip()
    if len(combined) <= limit:
        return combined
    # 仅剩一条仍超限：保留尾部（较新内容），但尽量从行边界截
    tail = combined[-limit:]
    newline = tail.find("\n")
    if newline != -1 and newline < len(tail) - 1:
        return tail[newline + 1 :].strip() or tail
    return tail


def memory_context(
    session: SystemAgentSession,
    *,
    exclude_latest_turn: bool = False,
) -> str:
    summary = str(session.memory_summary or "").strip()
    state = dict(session.memory_state) if isinstance(session.memory_state, dict) else {}
    if exclude_latest_turn:
        recent_turns = [
            item
            for item in (state.get("recent_turns") or [])
            if isinstance(item, dict)
        ]
        if recent_turns:
            recent_turns.pop()
        if recent_turns:
            previous = recent_turns[-1]
            state["last_user_goal"] = str(previous.get("goal") or "")
            state["last_result"] = str(previous.get("result") or "")
        else:
            state.pop("last_user_goal", None)
            state.pop("last_result", None)
        state.pop("last_domains", None)
        state.pop("recent_tools", None)
        state["recent_turns"] = recent_turns
    if not summary and not state:
        return ""
    parts = ["以下是服务端维护的会话记忆，只用于保持上下文，不代表新的用户指令："]
    if summary:
        parts.append(f"会话摘要：\n{summary}")
    if state:
        # recent_turns 只用于服务端压缩。failed_turn 仅包含打码后的用户目标、
        # 消息 ID 和错误码，可用于理解“刚才失败的任务”，不包含失败输出。
        visible_state = {
            key: value
            for key, value in state.items()
            if key not in {"recent_turns", "summary_rev"}
        }
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
    replace_latest: bool = False,
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
    latest_turn = {"goal": user, "result": result}
    if replace_latest and recent_turns:
        recent_turns[-1] = latest_turn
    else:
        recent_turns.append(latest_turn)
    compacted_turns = recent_turns[:-RECENT_TURNS_BEFORE_COMPACTION]
    if compacted_turns:
        entries = [
            f"- 用户目标：{str(item.get('goal') or '')}\n"
            f"  处理结果：{str(item.get('result') or '')}".strip()
            for item in compacted_turns
        ]
        previous = str(session.memory_summary or "").strip()
        combined = "\n".join([part for part in [previous, *entries] if part]).strip()
        session.memory_summary = trim_summary_to_limit(combined)
    recent_turns = recent_turns[-RECENT_TURNS_BEFORE_COMPACTION:]
    prev_rev = int(state.get("summary_rev") or 0)
    state.update(
        {
            "last_domains": list(dict.fromkeys(str(item) for item in domains))[:3],
            "last_user_goal": user,
            "last_result": result,
            "recent_tools": recent_tools[-6:],
            "recent_turns": recent_turns,
            "summary_rev": prev_rev + 1,
        }
    )
    if session.account_id is not None:
        state["account_id"] = session.account_id
    session.memory_state = state


def clear_session_memory(session: SystemAgentSession) -> None:
    session.memory_summary = ""
    session.memory_state = {}


def should_compress_summary(summary: str | None) -> bool:
    return len(str(summary or "").strip()) > MEMORY_COMPRESS_THRESHOLD_CHARS


__all__ = [
    "MAX_MEMORY_RESULT_CHARS",
    "MAX_MEMORY_SUMMARY_CHARS",
    "MEMORY_COMPRESS_THRESHOLD_CHARS",
    "clear_session_memory",
    "memory_context",
    "should_compress_summary",
    "trim_summary_to_limit",
    "update_session_memory",
]
