"""Scheduler Telegram 目标的校验与规范化。"""

from __future__ import annotations

import re
from typing import Any

_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")


class SchedulerTargetError(ValueError):
    """Scheduler 目标聊天配置无效。"""


def normalize_scheduler_target(value: Any, *, required: bool = True) -> int | str | None:
    """把数字 ID 或 ``@username`` 规范化为 Worker 可使用的值。"""
    if isinstance(value, bool):
        raise SchedulerTargetError("目标聊天必须是非零数字 ID 或 @username")

    if isinstance(value, int):
        if value == 0:
            if required:
                raise SchedulerTargetError("目标聊天必填，请填写非零数字 ID 或 @username")
            return None
        return value

    if value is None:
        if required:
            raise SchedulerTargetError("目标聊天必填，请填写非零数字 ID 或 @username")
        return None

    raw = str(value).strip()
    if not raw or raw == "0":
        if required:
            raise SchedulerTargetError("目标聊天必填，请填写非零数字 ID 或 @username")
        return None

    if _INTEGER_RE.fullmatch(raw):
        target_id = int(raw)
        if target_id == 0:
            if required:
                raise SchedulerTargetError("目标聊天必填，请填写非零数字 ID 或 @username")
            return None
        return target_id

    if _USERNAME_RE.fullmatch(raw):
        return raw

    raise SchedulerTargetError(
        "目标聊天格式无效，请填写非零数字 ID 或标准 @username（不支持裸用户名或 t.me 链接）"
    )


def normalize_scheduler_action_target(config: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化 scheduler action 的目标聊天字段。"""
    cfg = dict(config or {})
    action_raw = cfg.get("action")
    if not isinstance(action_raw, dict):
        return cfg

    action = dict(action_raw)
    action_type = str(action.get("type") or "").strip()
    if action_type in {"send_message", "call_llm"}:
        action["target_chat_id"] = normalize_scheduler_target(
            action.get("target_chat_id"),
            required=True,
        )
    elif action_type == "run_command":
        target = normalize_scheduler_target(
            action.get("target_chat_id"),
            required=False,
        )
        if target is None:
            action.pop("target_chat_id", None)
        else:
            action["target_chat_id"] = target

    cfg["action"] = action
    return cfg
