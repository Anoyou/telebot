"""System Agent API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SystemAgentConfigOut(BaseModel):
    enabled: bool = False
    provider_id: int | None = None
    model: str | None = None
    max_steps: int = 8
    max_tool_calls: int = 24
    session_token_limit: int = 16_384


class SystemAgentConfigPatch(BaseModel):
    enabled: bool | None = None
    provider_id: int | None = None
    model: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=16)
    max_tool_calls: int | None = Field(default=None, ge=1, le=64)
    session_token_limit: int | None = Field(default=None, ge=1024, le=100_000)


class SystemAgentCapabilitiesOut(BaseModel):
    enabled: bool
    provider_id: int | None = None
    model: str | None = None
    ai_enabled: bool = True
    timezone: str = "UTC"
    tools: list[dict[str, Any]] = Field(default_factory=list)
    stage: int = 1
    write_tools_available: bool = False


class SystemAgentSessionCreate(BaseModel):
    account_id: int | None = None
    title: str | None = Field(default=None, max_length=64)


class SystemAgentSessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=64)
    status: str | None = None
    account_id: int | None = None


class SystemAgentSessionOut(BaseModel):
    id: str
    web_user_id: int | None = None
    bot_tg_user_id: int | None = None
    account_id: int | None = None
    channel: str
    title: str | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemAgentMessageOut(BaseModel):
    id: int
    session_id: str
    role: str
    content: dict[str, Any]
    usage: dict[str, Any] | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemAgentMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)
    account_id: int | None = None


class SystemAgentActionOut(BaseModel):
    id: str
    session_id: str | None = None
    account_id: int | None = None
    actor_user_id: int | None = None
    actor_bot_user_id: int | None = None
    channel: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    secret_fields: list[str] | None = None
    has_secret: bool = False
    summary: str = ""
    preview: dict[str, Any] = Field(default_factory=dict)
    risk: str = "normal"
    status: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    runtime_sync_status: str = "not_required"
    runtime_sync_error: str | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    executed_at: datetime | None = None


class SystemAgentActionConfirmOut(BaseModel):
    ok: bool
    already_final: bool = False
    keep_pending: bool = False
    error_code: str | None = None
    error_message: str | None = None
    business_changed: bool | None = None
    action: SystemAgentActionOut | None = None


class SystemAgentSecretInput(BaseModel):
    """Web Action 卡片可选密钥补填。"""

    fields: dict[str, str] = Field(default_factory=dict)


class SystemAgentSecretInputOut(BaseModel):
    action_id: str
    has_secret: bool = True
    secret_fields: list[str] = Field(default_factory=list)
