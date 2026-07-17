"""NDJSON 事件构造。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_event(
    event_type: str,
    *,
    run_id: str,
    session_id: str,
    seq: int,
    **payload: Any,
) -> dict[str, Any]:
    """构造带公共字段的 NDJSON 事件。"""

    body: dict[str, Any] = {
        "type": event_type,
        "run_id": run_id,
        "session_id": session_id,
        "seq": seq,
        "ts": _now_iso(),
    }
    body.update(payload)
    return body


__all__ = ["make_event"]
