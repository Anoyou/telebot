"""System Agent API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SystemAgentConfigOut(BaseModel):
    enabled: bool = False
    provider_id: int | None = None
    model: str | None = None
    fallback_provider_ids: list[int] = Field(default_factory=list)
    require_tool_approval: bool = False
    max_steps: int = 8
    max_tool_calls: int = 24
    session_token_limit: int = 16_384


class SystemAgentUserMemoryOut(BaseModel):
    id: int
    scope_type: str
    scope_id: int
    content: str
    source: str
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemAgentUserMemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class SystemAgentUserMemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None


class SystemAgentConfigPatch(BaseModel):
    enabled: bool | None = None
    provider_id: int | None = None
    model: str | None = None
    fallback_provider_ids: list[int] | None = None
    require_tool_approval: bool | None = None
    max_steps: int | None = Field(default=None, ge=1, le=16)
    max_tool_calls: int | None = Field(default=None, ge=1, le=64)
    session_token_limit: int | None = Field(default=None, ge=0)


class SystemAgentCapabilitiesOut(BaseModel):
    enabled: bool
    provider_id: int | None = None
    model: str | None = None
    provider_name: str | None = None
    resolved_model: str | None = None
    ai_enabled: bool = True
    timezone: str = "UTC"
    tools: list[dict[str, Any]] = Field(default_factory=list)
    stage: int = 1
    write_tools_available: bool = False
    model_matrix: list[dict[str, Any]] = Field(default_factory=list)


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
    origin: str = "interactive"
    status: str
    memory_summary: str = ""
    memory_state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemAgentMessageOut(BaseModel):
    id: int
    session_id: str
    role: str
    content: dict[str, Any]
    usage: dict[str, Any] | None = None
    run_status: str = "completed"
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemAgentModelSelection(BaseModel):
    """本轮模型选择：auto 用全局配置；pinned 固定 provider+model，失败不静默换模型。"""

    mode: str = Field(default="auto", pattern="^(auto|pinned)$")
    provider_id: int | None = Field(default=None, ge=1)
    model: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _pinned_requires_target(self) -> SystemAgentModelSelection:
        if self.mode == "pinned" and (self.provider_id is None or not str(self.model or "").strip()):
            raise ValueError("pinned 模式必须同时提供 provider_id 与 model")
        return self


class SystemAgentMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)
    account_id: int | None = None
    model_selection: SystemAgentModelSelection | None = None


class SystemAgentMessageRetry(BaseModel):
    account_id: int | None = None
    fallback_provider_id: int | None = Field(default=None, ge=1)
    approved_tools: list[str] = Field(default_factory=list, max_length=64)
    # 缺省：重试保留原始请求 selection（由调用方传入）；未传则 auto
    model_selection: SystemAgentModelSelection | None = None


class SystemAgentRunCreate(SystemAgentMessageCreate):
    client_request_id: str = Field(min_length=8, max_length=64)


class SystemAgentRetryRunCreate(SystemAgentMessageRetry):
    client_request_id: str = Field(min_length=8, max_length=64)


class SystemAgentRegenerateRunCreate(SystemAgentMessageRetry):
    client_request_id: str = Field(min_length=8, max_length=64)
    assistant_message_id: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=32_000)


class SystemAgentRunOut(BaseModel):
    id: str
    run_id: str = Field(validation_alias="id")
    session_id: str
    web_user_id: int | None = None
    user_message_id: int | None = None
    client_request_id: str
    kind: str
    status: str
    last_seq: int = 0
    cancel_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemAgentRunEventOut(BaseModel):
    run_id: str
    seq: int
    event: dict[str, Any]
    created_at: datetime | None = None


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
    runtime_retryable: bool = True
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    executed_at: datetime | None = None
    # 收件箱展示：来源会话
    session_title: str | None = None
    session_origin: str | None = None


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
