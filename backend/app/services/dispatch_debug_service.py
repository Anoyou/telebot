"""Worker 命中模拟的 IPC 服务。"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import HTTPException

from ..redis_client import get_redis
from ..worker.ipc import (
    CMD_DISPATCH_SIMULATE,
    EVT_ACK,
    IPCMessage,
    ack_channel,
    cmd_channel,
    make_cmd,
)


async def simulate_dispatch(
    *,
    account_id: int,
    chat_type: str = "group",
    chat_id: int | None = None,
    sender_id: int | None = None,
    text: str = "",
    via: str = "userbot",
) -> dict[str, Any] | None:
    """向账号 Worker 发送一次命中模拟并返回 ACK trace。"""

    redis = get_redis()
    cmd_id = str(uuid.uuid4())
    reply_to = ack_channel(account_id, cmd_id)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_to)
        subscribers = await redis.publish(
            cmd_channel(account_id),
            make_cmd(
                CMD_DISPATCH_SIMULATE,
                cmd_id=cmd_id,
                reply_to=reply_to,
                account_id=account_id,
                chat_type=chat_type,
                chat_id=chat_id,
                sender_id=sender_id,
                text=text,
                via=via,
            ),
        )
        if int(subscribers or 0) <= 0:
            return None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            msg = await asyncio.wait_for(
                pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=remaining
                ),
                timeout=remaining + 0.1,
            )
            if not msg:
                continue
            ack = IPCMessage.decode(msg["data"])
            if ack.type != EVT_ACK or ack.payload.get("cmd_id") != cmd_id:
                continue
            if not ack.payload.get("ok", False):
                error = str(ack.payload.get("error") or "worker 命中模拟失败")
                raise HTTPException(
                    status_code=502,
                    detail={"code": "DISPATCH_SIMULATE_FAILED", "message": error},
                )
            trace = ack.payload.get("trace")
            return trace if isinstance(trace, dict) else None
        return None
    except TimeoutError:
        return None
    finally:
        try:
            await pubsub.unsubscribe(reply_to)
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


__all__ = ["simulate_dispatch"]
