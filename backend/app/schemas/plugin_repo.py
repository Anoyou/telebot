"""插件仓库 Pydantic schemas。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PluginRepoCredentialUpdate(BaseModel):
    """更新仓库凭证请求体。空 token 表示清除凭证。"""

    auth_type: str | None = Field(default="github_token", description="凭证类型；当前支持 github_token 或 none")
    token: str | None = Field(default=None, description="GitHub fine-grained / classic token；不会回显")

    @field_validator("auth_type")
    @classmethod
    def _normalize_auth_type(cls, value: str | None) -> str:
        raw = str(value or "github_token").strip().lower()
        if raw in {"", "none", "public"}:
            return "none"
        if raw in {"github", "github_token", "token", "pat"}:
            return "github_token"
        raise ValueError("auth_type 仅支持 github_token 或 none")


class PluginRepoCreate(BaseModel):
    """新增仓库请求体。

    ``url`` 必填；``name``/``description`` 可选，缺省时服务端会从 URL 派生显示名。
    """

    url: str = Field(
        ...,
        description="Git URL，如 https://github.com/foo/bar.git；也支持 GitHub tree/<branch> 分支页链接",
    )
    name: str | None = Field(default=None, description="展示名；为空时从 URL 派生")
    description: str | None = Field(default=None, description="可选备注")
    credential: PluginRepoCredentialUpdate | None = Field(
        default=None,
        description="可选私有 GitHub 仓库凭证；token 加密保存且不会回显",
    )


class PluginRepoOut(BaseModel):
    """仓库 DB 行的对外形态。"""

    id: int
    name: str
    url: str
    description: str
    auth_type: str = "none"
    has_credentials: bool = False
    added_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class PluginRepoPlugin(BaseModel):
    """仓库内单个插件的描述（仅来自 plugin.json 静态解析，无执行）。

    ``installed`` 由服务层根据 ``remote_plugin.name`` 是否已存在来填，便于
    前端给出“已安装/可安装”按钮状态。
    """

    name: str
    display_name: str = ""
    description: str = ""
    usage: str | None = None
    author: str = ""
    version: str = "0.0.0"
    installed: bool = False
    installed_version: str | None = None
    update_available: bool = False
    event_subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # 该插件在仓库内的相对子目录（用于安装时定位）；若插件位于仓库根目录则为 ""
    subdir: str = ""


class PluginRepoBulkUpdateItem(BaseModel):
    """仓库批量更新中单个插件的处理结果。"""

    name: str
    display_name: str = ""
    from_version: str | None = None
    to_version: str | None = None
    status: str = Field(description="updated / skipped / failed")
    message: str = ""


class PluginRepoBulkUpdateResult(BaseModel):
    """按仓库批量更新已安装插件的汇总结果。"""

    repo_id: int
    repo_name: str
    checked: int = 0
    update_available: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[PluginRepoBulkUpdateItem] = Field(default_factory=list)
