"""NotifyBot API schema（Sprint4 #2D）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NotifyBotBase(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    default_chat_id: int
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        out = v.strip()
        if not out:
            raise ValueError("name 不能为空")
        return out


class NotifyBotCreate(NotifyBotBase):
    bot_token: str | None = Field(default=None, max_length=512)
    source_account_id: int | None = Field(default=None, gt=0)

    @field_validator("bot_token")
    @classmethod
    def _strip_token(cls, v: str | None) -> str | None:
        if v is None:
            return None
        out = v.strip()
        if not out:
            raise ValueError("bot_token 不能为空")
        return out

    @model_validator(mode="after")
    def _credential_source(self) -> NotifyBotCreate:
        if bool(self.bot_token) == bool(self.source_account_id):
            raise ValueError("必须且只能选择独立 Bot Token 或引用管理 Bot")
        return self


class NotifyBotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    default_chat_id: int | None = None
    enabled: bool | None = None
    bot_token: str | None = Field(default=None, max_length=512)
    clear_token: bool = False
    source_account_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        out = v.strip()
        if not out:
            raise ValueError("name 不能为空")
        return out

    @field_validator("bot_token")
    @classmethod
    def _strip_token(cls, v: str | None) -> str | None:
        if v is None:
            return None
        out = v.strip()
        if not out:
            raise ValueError("bot_token 不能为空")
        return out


class NotifyBotOut(BaseModel):
    id: int
    name: str
    default_chat_id: int
    enabled: bool
    has_token: bool
    credential_source: str
    source_account_id: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotifyBotTestRequest(BaseModel):
    text: str | None = Field(default=None, max_length=4096)


class NotifyBotTestResponse(BaseModel):
    ok: bool
