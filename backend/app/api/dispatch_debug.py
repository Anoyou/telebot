"""全链路命中调试 API。"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..services import account_bot_runtime, platform_capabilities
from ..worker.ipc import (
    CMD_DISPATCH_SIMULATE,
    EVT_ACK,
    IPCMessage,
    ack_channel,
    cmd_channel,
    make_cmd,
)

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


class DispatchSimulateRequest(BaseModel):
    account_id: int = Field(gt=0)
    chat_type: str = Field(default="group", min_length=1, max_length=32)
    chat_id: int | None = None
    sender_id: int | None = None
    text: str = Field(default="", max_length=20000)
    via: str = Field(default="userbot", min_length=1, max_length=64)


class RouterDebugTraceRequest(BaseModel):
    account_id: int = Field(gt=0)
    plugin_key: str | None = Field(default=None, min_length=1, max_length=128)
    chat_id: int | None = None
    ttl_seconds: int = Field(default=300, ge=1, le=3600)


async def _publish_dispatch_simulation(redis: Any, payload: DispatchSimulateRequest) -> dict[str, Any] | None:
    """Send dispatch simulation to a worker and return the ACK trace."""

    cmd_id = str(uuid.uuid4())
    reply_to = ack_channel(payload.account_id, cmd_id)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_to)
        subscribers = await redis.publish(
            cmd_channel(payload.account_id),
            make_cmd(
                CMD_DISPATCH_SIMULATE,
                cmd_id=cmd_id,
                reply_to=reply_to,
                account_id=payload.account_id,
                chat_type=payload.chat_type,
                chat_id=payload.chat_id,
                sender_id=payload.sender_id,
                text=payload.text,
                via=payload.via,
            ),
        )
        if int(subscribers or 0) <= 0:
            return None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            msg = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
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
                ret = close()
                if hasattr(ret, "__await__"):
                    await ret


async def _require_dispatch_debug(db: DBSession) -> None:
    await platform_capabilities.require_module_enabled(db, "dispatch_debug")


@router.post("/simulate")
async def simulate_dispatch(
    payload: DispatchSimulateRequest,
    db: DBSession,
    _user: CurrentUser,
) -> dict[str, Any]:
    await _require_dispatch_debug(db)
    redis = get_redis()
    trace = await _publish_dispatch_simulation(redis, payload)
    if trace is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "WORKER_OFFLINE",
                "message": f"账号 {payload.account_id} 的 worker 未在线或未返回命中调试结果。",
            },
        )
    return trace


@router.post("/router-debug-trace")
async def enable_router_debug_trace(
    payload: RouterDebugTraceRequest,
    db: DBSession,
    _user: CurrentUser,
) -> dict[str, Any]:
    await _require_dispatch_debug(db)
    return await account_bot_runtime.set_router_debug_trace(
        payload.account_id,
        plugin_key=payload.plugin_key,
        chat_id=payload.chat_id,
        ttl_seconds=payload.ttl_seconds,
    )


@router.get("/router-delivery-stats")
async def get_router_delivery_stats(
    _user: CurrentUser,
    db: DBSession,
    account_id: int | None = None,
    channel: str | None = None,
    plugin_key: str | None = None,
    chat_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    await _require_dispatch_debug(db)
    return account_bot_runtime.get_router_delivery_stats_summary(
        account_id=account_id,
        channel=channel,
        plugin_key=plugin_key,
        chat_id=chat_id,
        limit=limit,
    )
