"""Shared Redis pub/sub broker for main→worker interaction RPC.

Instead of opening a new ``pubsub()`` connection per request (which can exhaust
the main process pool under concurrent payouts), one long-lived subscriber
demultiplexes replies by channel name onto asyncio Futures.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from ...redis_client import get_redis
from ...worker.ipc import IPCMessage

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_INFLIGHT = 256


class InteractionRpcBroker:
    """Process-local reply demultiplexer for interaction worker RPC."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pubsub: Any | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._redis: Any | None = None
        self._started = False

    @property
    def inflight(self) -> int:
        return len(self._pending)

    async def start(self, redis: Any | None = None) -> None:
        async with self._lock:
            if self._started:
                return
            client = redis or get_redis()
            self._redis = client
            self._pubsub = client.pubsub()
            # 订阅一个永不使用的占位频道，使连接进入 pubsub 模式；真实 reply 频道按需 subscribe。
            await self._pubsub.subscribe("__telepilot:interaction_rpc:keepalive__")
            self._listener_task = asyncio.create_task(self._listen_loop(), name="interaction-rpc-broker")
            self._started = True

    async def stop(self) -> None:
        async with self._lock:
            task = self._listener_task
            self._listener_task = None
            self._started = False
            pending = list(self._pending.items())
            self._pending.clear()
            pubsub = self._pubsub
            self._pubsub = None
        for _channel, fut in pending:
            if not fut.done():
                fut.set_exception(RuntimeError("interaction rpc broker stopped"))
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.debug("interaction rpc broker listener stop failed", exc_info=True)
        if pubsub is not None:
            try:
                close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                if close is not None:
                    ret = close()
                    if hasattr(ret, "__await__"):
                        await ret
            except Exception:  # noqa: BLE001
                log.debug("interaction rpc broker pubsub close failed", exc_info=True)

    async def request(
        self,
        *,
        cmd_channel: str,
        command: str,
        reply_channel: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        online_wait_seconds: float = 2.0,
        redis: Any | None = None,
    ) -> tuple[bool, int, dict[str, Any] | None, str | None]:
        """Publish ``command`` and wait for a reply on ``reply_channel``.

        Returns ``(worker_online, publish_attempts, payload_or_none, error_or_none)``.
        """

        await self.start(redis=redis)
        client = redis or self._redis or get_redis()
        if len(self._pending) >= _MAX_INFLIGHT:
            return True, 0, None, "interaction rpc broker overloaded"

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._lock:
            if reply_channel in self._pending:
                return True, 0, None, "duplicate reply channel"
            self._pending[reply_channel] = fut
            pubsub = self._pubsub
        if pubsub is None:
            async with self._lock:
                self._pending.pop(reply_channel, None)
            return False, 0, None, "interaction rpc broker not ready"

        try:
            await pubsub.subscribe(reply_channel)
        except Exception as exc:  # noqa: BLE001
            async with self._lock:
                self._pending.pop(reply_channel, None)
            return False, 0, None, f"{type(exc).__name__}: {exc}"

        publish_attempts = 0
        subscriber_count = 0
        deadline = time.time() + max(0.1, float(timeout_seconds))
        online_deadline = time.time() + min(float(timeout_seconds), max(0.0, float(online_wait_seconds)))
        try:
            while True:
                publish_attempts += 1
                subscriber_count = int(await client.publish(cmd_channel, command) or 0)
                if subscriber_count > 0:
                    break
                now = time.time()
                if now >= online_deadline:
                    break
                await asyncio.sleep(min(0.25, max(0.01, online_deadline - now)))

            if subscriber_count <= 0:
                return False, publish_attempts, None, "账号 worker 不在线"

            remaining = max(0.01, deadline - time.time())
            payload = await asyncio.wait_for(asyncio.shield(fut), timeout=remaining)
            return True, publish_attempts, payload, None
        except TimeoutError:
            return True, publish_attempts, None, "worker 调用超时"
        except Exception as exc:  # noqa: BLE001
            return True, publish_attempts, None, f"{type(exc).__name__}: {exc}"
        finally:
            async with self._lock:
                self._pending.pop(reply_channel, None)
            try:
                if pubsub is not None:
                    await pubsub.unsubscribe(reply_channel)
            except Exception:  # noqa: BLE001
                log.debug("interaction rpc unsubscribe failed channel=%s", reply_channel, exc_info=True)
            if not fut.done():
                fut.cancel()

    async def _listen_loop(self) -> None:
        assert self._pubsub is not None
        pubsub = self._pubsub
        try:
            while True:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.debug("interaction rpc broker get_message failed", exc_info=True)
                    await asyncio.sleep(0.2)
                    continue
                if not msg:
                    await asyncio.sleep(0.01)
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "message":
                    continue
                channel = msg.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8", errors="ignore")
                channel = str(channel or "")
                raw = msg.get("data")
                # 兼容仅返回 data 的旧测试假 pubsub。
                if not channel:
                    async with self._lock:
                        pending_items = list(self._pending.items())
                    if len(pending_items) == 1:
                        channel, fut = pending_items[0]
                    else:
                        continue
                else:
                    async with self._lock:
                        fut = self._pending.get(channel)
                if fut is None or fut.done():
                    continue
                try:
                    payload = IPCMessage.decode(raw).payload
                    if not isinstance(payload, dict):
                        payload = {}
                    fut.set_result(payload)
                except Exception as exc:  # noqa: BLE001
                    if not fut.done():
                        fut.set_exception(exc)
        except asyncio.CancelledError:
            return


_BROKER: InteractionRpcBroker | None = None
_BROKER_LOCK = asyncio.Lock()


async def get_interaction_rpc_broker() -> InteractionRpcBroker:
    global _BROKER
    async with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = InteractionRpcBroker()
        return _BROKER


async def reset_interaction_rpc_broker_for_tests() -> None:
    """Test helper: stop and drop the process-local broker singleton."""

    global _BROKER
    async with _BROKER_LOCK:
        broker = _BROKER
        _BROKER = None
    if broker is not None:
        await broker.stop()


def new_reply_channel(kind: str, account_id: int) -> str:
    return f"account_bot:interaction_{kind}:{int(account_id)}:{secrets.token_hex(8)}"


__all__ = [
    "InteractionRpcBroker",
    "get_interaction_rpc_broker",
    "new_reply_channel",
    "reset_interaction_rpc_broker_for_tests",
]
