"""运行预设 API 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .platform_capabilities import ModuleKey


class RuntimeProfileApplyIn(BaseModel):
    preset: Literal["safe_watch"]


class RuntimeProfileDryRunIn(BaseModel):
    preset: Literal["production", "safe_watch"]


class RuntimeProfileDiffItem(BaseModel):
    key: ModuleKey
    from_enabled: bool
    to_enabled: bool


class RuntimeProfileDryRunOut(BaseModel):
    preset: Literal["production", "safe_watch"]
    diff: list[RuntimeProfileDiffItem] = Field(default_factory=list)
    blind_spot: str | None = None


class RuntimeProfileStatusOut(BaseModel):
    active_profile: Literal["safe_watch"] | None = None
    current_profile: Literal["production", "safe_watch", "custom"]
    status: Literal["idle", "applying", "active", "restoring", "failed"]
    last_error: str | None = None
    operator_id: int | None = None
    updated_at: datetime | None = None
    modules: dict[ModuleKey, bool]
    blind_spot: str | None = None


__all__ = [
    "RuntimeProfileApplyIn",
    "RuntimeProfileDiffItem",
    "RuntimeProfileDryRunIn",
    "RuntimeProfileDryRunOut",
    "RuntimeProfileStatusOut",
]
