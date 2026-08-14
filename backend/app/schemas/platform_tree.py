"""平台树视图响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .platform_capabilities import ModuleKey, RuntimeState


class PlatformTreeWorker(BaseModel):
    account_id: int
    pid: int | None = None
    alive: bool = False
    desired: str = "running"
    fail_count: int = 0
    queued: bool = False
    starting: bool = False


class PlatformTreeUserbot(BaseModel):
    workers: list[PlatformTreeWorker] = Field(default_factory=list)
    total: int = 0
    alive: int = 0


class PlatformTreeTrunk(BaseModel):
    userbot: PlatformTreeUserbot
    kill_switch: bool = False
    current_profile: Literal["production", "safe_watch", "custom"] = "production"


class PlatformTreeBranch(BaseModel):
    state: RuntimeState
    desired: bool
    forced_off: bool = False
    demanded_by: list[str] = Field(default_factory=list)
    can_turn_off: bool = False


class PlatformTreeLeaf(BaseModel):
    key: str
    attachment: Literal["直通", "命令", "交互"]
    enabled: bool = False
    requires: list[ModuleKey] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_missing: bool = False


class PlatformTreeOut(BaseModel):
    trunk: PlatformTreeTrunk
    branches: dict[ModuleKey, PlatformTreeBranch]
    leaves: list[PlatformTreeLeaf] = Field(default_factory=list)


__all__ = ["PlatformTreeOut"]
