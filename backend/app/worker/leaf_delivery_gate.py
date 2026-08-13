"""Worker 内叶子投递闸。

``is_set()`` 保持与历史 ``asyncio.Event`` 相同的极性：True 放行，False 暂停。
额外跟踪已经进入的投递，以便 CMD_PAUSE 在 ACK 前等待它们收敛。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class LeafDeliveryGate:
    def __init__(self, *, enabled: bool = True) -> None:
        self._event = asyncio.Event()
        self._reasons: set[str] = set()
        if not enabled:
            self._reasons.add("runtime_control")
        self._sync_event()
        self._active = 0
        self._tracked_tasks: set[asyncio.Task[object]] = set()
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        """兼容 ``asyncio.Event.set``：只解除普通运行控制暂停。"""
        self.resume("runtime_control")

    def clear(self) -> None:
        """兼容 ``asyncio.Event.clear``：只施加普通运行控制暂停。"""
        self.pause("runtime_control")

    def _sync_event(self) -> None:
        if self._reasons:
            self._event.clear()
        else:
            self._event.set()

    def pause(self, reason: str) -> None:
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("pause reason is required")
        self._reasons.add(reason)
        self._sync_event()

    def resume(self, reason: str) -> None:
        self._reasons.discard(str(reason or "").strip())
        self._sync_event()

    def has_reason(self, reason: str) -> bool:
        return str(reason or "").strip() in self._reasons

    async def wait(self) -> bool:
        return await self._event.wait()

    def track_current_task(self) -> bool:
        """原子检查闸门并把当前事件任务计入 in-flight。

        Telethon 为每个 update 创建独立任务；用任务完成回调收口，可以在不改变
        loader 巨大派发函数控制流的前提下，让 ``CMD_PAUSE`` 等到该叶真正退出。
        """

        if not self._event.is_set():
            return False
        task = asyncio.current_task()
        if task is None:
            return False
        tracked = task  # 缩窄类型只为下面的集合与回调。
        if tracked in self._tracked_tasks:
            return True
        self._tracked_tasks.add(tracked)
        self._idle.clear()

        def _finished(done: asyncio.Task[object]) -> None:
            self._tracked_tasks.discard(done)
            if self._active == 0 and not self._tracked_tasks:
                self._idle.set()

        tracked.add_done_callback(_finished)
        return True

    @asynccontextmanager
    async def delivery(self) -> AsyncIterator[bool]:
        entered = False
        async with self._lock:
            if self._event.is_set():
                self._active += 1
                self._idle.clear()
                entered = True
        try:
            yield entered
        finally:
            if entered:
                async with self._lock:
                    self._active -= 1
                    if self._active == 0 and not self._tracked_tasks:
                        self._idle.set()

    async def pause_and_wait(self, reason: str = "runtime_control", *, timeout: float) -> bool:
        self.pause(reason)
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=max(0.1, timeout))
        except TimeoutError:
            return False
        return True

    @property
    def active_deliveries(self) -> int:
        return self._active + len(self._tracked_tasks)

    @property
    def pause_reasons(self) -> frozenset[str]:
        return frozenset(self._reasons)


__all__ = ["LeafDeliveryGate"]
