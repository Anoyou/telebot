"""System Agent 轮次指代与失败任务状态。"""

from __future__ import annotations

import re
from typing import Any

from ...db.models.system_agent import SystemAgentSession

FAILED_TURN_STATE_KEY = "failed_turn"
MAX_FAILED_GOAL_CHARS = 500

_RETRY_REFERENCES = frozenset(
    {
        "重试",
        "重试一下",
        "再试",
        "再试一次",
        "继续",
        "继续刚才的",
        "继续刚才那个",
        "重新试",
        "重新试试",
        "重新尝试",
        "再来一次",
        "重跑",
        "再执行一次",
        "重试刚才的",
        "retry",
        "tryagain",
        "continue",
    }
)


def is_retry_reference(text: str) -> bool:
    """仅识别可确定映射到失败轮次的短指代，避免误伤普通请求。"""

    normalized = re.sub(r"[\s，。！？!?、]+", "", str(text or "").lower())
    return normalized in _RETRY_REFERENCES


def failed_turn_state(session: SystemAgentSession) -> dict[str, Any] | None:
    state = session.memory_state if isinstance(session.memory_state, dict) else {}
    failed = state.get(FAILED_TURN_STATE_KEY)
    if not isinstance(failed, dict):
        return None
    try:
        message_id = int(failed.get("message_id"))
    except (TypeError, ValueError):
        return None
    if message_id <= 0:
        return None
    return {
        "message_id": message_id,
        "user_goal": str(failed.get("user_goal") or "")[:MAX_FAILED_GOAL_CHARS],
        "error_code": str(failed.get("error_code") or "AGENT_RUN_FAILED")[:64],
    }


def remember_failed_turn(
    session: SystemAgentSession,
    *,
    message_id: int,
    user_goal: str,
    error_code: str,
) -> None:
    """保存打码后的失败目标；后续失败会自然覆盖旧锚点。"""

    state = dict(session.memory_state) if isinstance(session.memory_state, dict) else {}
    state[FAILED_TURN_STATE_KEY] = {
        "message_id": int(message_id),
        "user_goal": " ".join(str(user_goal or "").split())[:MAX_FAILED_GOAL_CHARS],
        "error_code": str(error_code or "AGENT_RUN_FAILED")[:64],
    }
    session.memory_state = state


def clear_failed_turn(
    session: SystemAgentSession,
    *,
    message_id: int | None = None,
) -> None:
    """清除失败锚点；指定 ID 时只清理对应任务，避免并发覆盖新失败。"""

    state = dict(session.memory_state) if isinstance(session.memory_state, dict) else {}
    failed = state.get(FAILED_TURN_STATE_KEY)
    if message_id is not None and isinstance(failed, dict):
        try:
            anchored_id = int(failed.get("message_id"))
        except (TypeError, ValueError):
            anchored_id = None
        if anchored_id != int(message_id):
            return
    if FAILED_TURN_STATE_KEY not in state:
        return
    state.pop(FAILED_TURN_STATE_KEY, None)
    session.memory_state = state


__all__ = [
    "FAILED_TURN_STATE_KEY",
    "clear_failed_turn",
    "failed_turn_state",
    "is_retry_reference",
    "remember_failed_turn",
]
