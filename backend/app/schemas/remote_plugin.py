"""远程插件 Pydantic schemas。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RemotePluginCreate(BaseModel):
    source_url: str
    default_enabled: bool = False


class RemotePluginOut(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    usage: str | None = None
    author: str
    source_url: str
    version: str
    latest_version: str | None = None
    update_available: bool = False
    last_update_check_at: datetime | None = None
    last_update_check_error: str | None = None
    lint_warnings: list[str] = Field(default_factory=list)
    event_subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    enabled: bool
    default_enabled: bool = False
    installed_at: datetime | None = None

    class Config:
        from_attributes = True


class RegistryPluginOut(BaseModel):
    name: str
    display_name: str
    description: str
    author: str
    source_url: str
    version: str
    installed: bool
