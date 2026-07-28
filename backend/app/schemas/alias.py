"""命令别名管理 Schemas。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CommandAliasCreate(BaseModel):
    """创建命令别名请求。"""
    alias: str
    target: str
    account_id: int | None = None


class CommandAliasUpdate(BaseModel):
    """更新命令别名请求。"""
    target: str
    account_id: int | None = None


class CommandAliasResponse(BaseModel):
    """命令别名响应。"""
    id: int
    alias: str
    target: str
    account_id: int | None = None
    created_at: str

    model_config = ConfigDict(from_attributes=True)
