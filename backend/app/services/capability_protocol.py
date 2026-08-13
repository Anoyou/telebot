"""新增能力注册面的静态接线协议；本模块不提供任何运行时机制。"""

from __future__ import annotations

from typing import Literal, Protocol

LifecycleState = Literal[
    "starting",
    "ready",
    "quiescing",
    "stopped",
    "failed",
]


class Disposable(Protocol):
    """由注册 owner 持有的清理句柄。"""

    @property
    def owner(self) -> str:
        """返回负责清理该注册项的稳定 owner key。"""
        ...

    @property
    def generation(self) -> int:
        """返回该句柄所属的注册代际。"""
        ...

    async def dispose(self) -> None:
        """幂等地移除登记项并释放由 owner 创建的资源。"""
        ...


class Registrable(Protocol):
    """支持所有权追踪和代际失效的注册面结构。"""

    @property
    def generation(self) -> int:
        """返回当前可见注册集合的代际。"""
        ...

    def register(
        self,
        entry: object,
        *,
        key: str,
        owner: str,
    ) -> Disposable:
        """登记一个稳定 key，并把对应清理句柄交还 owner。"""
        ...


class CapabilityModule(Protocol):
    """具有稳定 key、五态生命周期和 generation 的能力模块结构。"""

    @property
    def module_key(self) -> str:
        """返回稳定的小写 snake_case module key。"""
        ...

    @property
    def lifecycle_state(self) -> LifecycleState:
        """返回当前生命周期状态；只有 ready 可接收新工作。"""
        ...

    @property
    def generation(self) -> int:
        """返回当前能力语义的代际。"""
        ...

    async def start(self) -> None:
        """启动能力并完成从 starting 到 ready 的收敛。"""
        ...

    async def stop(self) -> None:
        """停止接收新工作并完成 quiescing 到 stopped 的收敛。"""
        ...
