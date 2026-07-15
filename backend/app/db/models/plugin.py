"""插件安装记录模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

PLUGIN_SOURCE_BUILTIN = "builtin"
PLUGIN_SOURCE_ZIP = "zip"
PLUGIN_SOURCE_REPO = "repo"
PLUGIN_SOURCE_GIT = "git"
PLUGIN_SOURCE_LOCAL = "local"
PLUGIN_SOURCE_OFFICIAL = "official"

PLUGIN_TRUST_CORE = "core"
PLUGIN_TRUST_VERIFIED = "verified"
PLUGIN_TRUST_COMMUNITY = "community"
PLUGIN_TRUST_LOCAL = "local"
PLUGIN_TRUST_OFFICIAL = "official"
PLUGIN_TRUST_ORPHAN = "orphan"


class PluginInstall(Base):
    """旧版 zip 安装表。

    Deprecated: 新写路径已切到 ``InstalledPlugin``；本模型仅保留给升级兼容和只读排查。
    """

    __tablename__ = "plugin_install"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False, default="0.0.0")
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    signature_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    installed_path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InstalledPlugin(Base):
    """统一安装记录表；插件安装、启停、卸载的权威数据源。"""

    __tablename__ = "installed_plugin"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    installed_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="0.0.0")
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signature_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    trust_tier: Mapped[str] = mapped_column(String(32), nullable=False, default=PLUGIN_TRUST_COMMUNITY)
    source_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_install_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_load_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lint_warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "InstalledPlugin",
    "PLUGIN_SOURCE_BUILTIN",
    "PLUGIN_SOURCE_GIT",
    "PLUGIN_SOURCE_LOCAL",
    "PLUGIN_SOURCE_OFFICIAL",
    "PLUGIN_SOURCE_REPO",
    "PLUGIN_SOURCE_ZIP",
    "PLUGIN_TRUST_COMMUNITY",
    "PLUGIN_TRUST_CORE",
    "PLUGIN_TRUST_LOCAL",
    "PLUGIN_TRUST_OFFICIAL",
    "PLUGIN_TRUST_ORPHAN",
    "PLUGIN_TRUST_VERIFIED",
    "PluginInstall",
]
