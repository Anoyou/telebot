"""平台能力热插拔状态模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RuntimeState = Literal["starting", "ready", "quiescing", "stopped", "failed"]
ModuleKey = Literal["ai", "interaction_bot", "webhooks", "ledger", "dispatch_debug"]
ChannelKey = Literal["userbot", "interaction_bot", "webhook"]
BlockedReasonCode = Literal[
    "platform_module_disabled",
    "channel_disabled",
    "channel_not_configured",
    "capability_unavailable",
    "platform_module_transitioning",
]


class CapabilityModuleState(BaseModel):
    key: ModuleKey
    label: str
    desired_enabled: bool = True
    forced_off: bool = False
    generation: int = 0
    runtime_state: RuntimeState = "starting"
    last_error: str | None = None
    last_transition_at: datetime | None = None
    resource_summary: dict[str, Any] = Field(default_factory=dict)


class CapabilityChannelState(BaseModel):
    key: ChannelKey
    label: str
    fixed: bool = True
    managed_by: ModuleKey | None = None
    available: bool = True
    reason_code: BlockedReasonCode | None = None
    reason_text: str | None = None


class CapabilityWorkerConvergence(BaseModel):
    total_accounts: int = 0
    notified: int = 0
    acked: int = 0
    pending: int = 0
    offline_or_timeout: int = 0
    last_broadcast_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class PlatformCapabilitiesOut(BaseModel):
    modules: list[CapabilityModuleState]
    channels: list[CapabilityChannelState]
    worker_convergence: CapabilityWorkerConvergence = Field(
        default_factory=CapabilityWorkerConvergence
    )
    cache_ready: bool = False
    updated_at: datetime | None = None


class CapabilityModulePatch(BaseModel):
    enabled: bool


class CapabilityModulePatchOut(BaseModel):
    module: CapabilityModuleState
    worker_convergence: CapabilityWorkerConvergence
    ok: bool = True
    message: str | None = None


__all__ = [
    "BlockedReasonCode",
    "CapabilityChannelState",
    "CapabilityModulePatch",
    "CapabilityModulePatchOut",
    "CapabilityModuleState",
    "CapabilityWorkerConvergence",
    "ChannelKey",
    "ModuleKey",
    "PlatformCapabilitiesOut",
    "RuntimeState",
]
