"""插件聚合只读视图 schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PluginCenterTraceRef(BaseModel):
    trace_id: str
    account_id: int | None = None
    status: str | None = None
    event_type: str | None = None
    source_channel: str | None = None
    started_at: datetime | None = None


class PluginCenterLoadError(BaseModel):
    source: str
    account_id: int | None = None
    load_status: str | None = None
    message: str
    updated_at: datetime | None = None


class PluginCenterAccountItem(BaseModel):
    account_id: int
    account_name: str
    enabled: bool
    state: str
    last_error: str | None = None
    load_status: str | None = None
    last_load_error: str | None = None
    last_trace: PluginCenterTraceRef | None = None


class PluginCenterUpdateStatus(BaseModel):
    update_available: bool = False
    latest_version: str | None = None
    last_update_check_at: datetime | None = None
    last_update_check_error: str | None = None


class PluginCenterItem(BaseModel):
    key: str
    display_name: str
    source: str
    source_url: str | None = None
    source_label: str | None = None
    version: str | None = None
    global_enabled: bool
    signature_ok: bool | None = None
    trust_tier: str | None = None
    lint_warnings: list[str] = Field(default_factory=list)
    update: PluginCenterUpdateStatus = Field(default_factory=PluginCenterUpdateStatus)
    accounts: list[PluginCenterAccountItem] = Field(default_factory=list)
    recent_load_error: PluginCenterLoadError | None = None
    recent_trace: PluginCenterTraceRef | None = None


__all__ = [
    "PluginCenterAccountItem",
    "PluginCenterItem",
    "PluginCenterLoadError",
    "PluginCenterTraceRef",
    "PluginCenterUpdateStatus",
]
