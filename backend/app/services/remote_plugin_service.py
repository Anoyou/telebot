"""远程 Git 仓库插件管理服务（阶段 D：tpm-style 远程插件）。

职责：
- ``install``：``git clone`` 到 ``plugins/installed/<name>/`` → 解析 ``plugin.json`` → 写 ``installed_plugin`` 表 → 触发 worker 热加载（``reload_config``）
- ``uninstall``：删 installed_plugin 行 + 删插件目录
- ``enable`` / ``disable``：翻转 ``enabled`` 标志，并向 worker 广播热加载
- ``update``：``git pull`` → 重读 plugin.json → 写新版本号 → 触发热加载

安全设计（阶段 E 修复）：
- 安装阶段**绝对禁止执行任何 Python 代码**（manifest.py 在安装时不被解析/执行）
- 只允许静态解析 ``plugin.json``
- source_url 只允许 https:// 和 git+ssh://
- git clone 强制 timeout

设计要点：
- 与现有 ``loader.py`` 集成靠两条路：
  1. 直接 ``import`` 现有 ``reload_account_config`` 函数（同进程调用）
  2. 通过 Redis IPC ``CMD_RELOAD_CONFIG`` 广播到所有 worker 进程
- 不依赖 GitPython，统一走 ``asyncio`` 子进程跑 ``git``，只需要环境里有 ``git`` 即可
- 名字（``name``）三重身份：DB 唯一键 / 文件目录名 / loader 注册的 plugin key
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import AsyncSessionLocal
from ..db.models.account import Account
from ..db.models.feature import FEATURE_STATE_DISABLED, AccountFeature, Feature
from ..db.models.plugin import (
    PLUGIN_SOURCE_GIT,
    PLUGIN_SOURCE_LOCAL,
    PLUGIN_SOURCE_OFFICIAL,
    PLUGIN_SOURCE_REPO,
    PLUGIN_TRUST_COMMUNITY,
    InstalledPlugin,
)
from ..db.models.system import SystemSetting
from ..settings import settings
from ..worker.ipc import CMD_RELOAD_CONFIG, publish_cmd_with_ack

# 直接复用现有 loader 的配置热更新路径；installed 插件在 loader 里按 DB 双开关按需加载
from ..worker.plugins.loader import reload_account_config
from ..worker.plugins.update_barrier import (
    begin_plugin_update,
    clear_plugin_update,
    plugin_update_active,
)
from .event_bus import SUPPORTED_FILTER_KEYS, VALID_EVENT_SCOPES, VALID_EVENT_SOURCES, VALID_EVENT_TYPES
from .interaction.contracts import send_via_selector_options, unsupported_send_via_values
from .redactor import redact_text

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 安全常量：source_url 允许的 scheme
# ─────────────────────────────────────────────────────
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"https", "git+ssh"})
_REMOTE_UPDATE_DEFAULT_INTERVAL_MINUTES = 360
_COMMAND_PLACEHOLDER_RE = (
    r"\{(?:command|cmd|name|help_command|cancel_command|undo_command|"
    r"force_stop_command|admin_command|edit_command|example)\}"
)
# 只检查英文逗号命令示例，避免把中文正文标点、CSV 配置值误报为硬编码前缀。
_HARDCODED_PREFIX_RE = re.compile(
    r"(?:^|(?<=[\s:：(<\[【\"'`>]))"
    r"(,(?:"
    + _COMMAND_PLACEHOLDER_RE
    + r"|[A-Za-z_][A-Za-z0-9_-]{0,31}"
    + r"|[\u4e00-\u9fff]{1,12}(?=$|[\s<。！？!?、，,；;：:）)\]}\"'`>])"
    + r"))"
)
_REMOTE_INSTALL_SOURCES = (PLUGIN_SOURCE_GIT, PLUGIN_SOURCE_REPO, PLUGIN_SOURCE_LOCAL)
_REMOTE_INFO_KEY = "_telepilot_remote"


# ─────────────────────────────────────────────────────
# 错误类型
# ─────────────────────────────────────────────────────
class RemotePluginError(Exception):
    """远程插件操作的基类异常。``code`` 用于 API 层映射 HTTP 状态。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RemotePluginNotFound(RemotePluginError):
    """根据 name 查不到 ``installed_plugin`` 行。"""


class DuplicatePluginName(RemotePluginError):
    """安装时同名插件已存在。"""


class GitOperationFailed(RemotePluginError):
    """``git clone`` / ``git pull`` 等子命令非 0 退出。"""


class InvalidPluginMetadata(RemotePluginError):
    """``plugin.json`` 缺失或解析失败。"""


class InvalidSourceUrl(RemotePluginError):
    """source_url 不符合安全要求。"""


@dataclass(frozen=True, slots=True)
class GitSourceUrl:
    """规范化后的 Git 拉取目标。

    ``original_url`` 仍然可以原样存库；真正执行 git 时使用 ``clone_url``，
    GitHub ``tree/<branch>`` 页面链接则额外带上 ``ref``。
    """

    clone_url: str
    ref: str | None = None


def normalize_git_source_url(url: str) -> GitSourceUrl:
    """把用户可读的仓库 URL 规范化为可执行的 git clone 目标。

    支持 GitHub 分支页：
    ``https://github.com/user/repo/tree/feature/foo`` →
    ``clone_url=https://github.com/user/repo.git`` + ``ref=feature/foo``。
    """
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() == "https" and parsed.hostname in {"github.com", "www.github.com"}:
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 3 and parts[2] == "tree":
            if len(parts) < 4:
                raise InvalidSourceUrl(
                    "BAD_SOURCE_URL",
                    "GitHub tree/<branch> 插件仓库链接缺少分支名。",
                )
            owner = parts[0].strip()
            repo = parts[1].strip()
            if repo.endswith(".git"):
                repo = repo[:-4]
            ref = "/".join(part.strip() for part in parts[3:] if part.strip())
            if not owner or not repo or not ref:
                raise InvalidSourceUrl(
                    "BAD_SOURCE_URL",
                    "GitHub tree/<branch> 插件仓库链接格式不正确。",
                )
            return GitSourceUrl(
                clone_url=f"https://github.com/{owner}/{repo}.git",
                ref=ref,
            )
    return GitSourceUrl(clone_url=raw)


# ─────────────────────────────────────────────────────
# 元数据模型（用于校验 plugin.json）
# ─────────────────────────────────────────────────────
class PluginMetadataSchema(BaseModel):
    """plugin.json 的 Pydantic 校验模型。

    只允许静态解析，不执行任何 Python 代码。
    所有字段在通过校验后才返回 PluginMetadata。
    """

    # 允许 name/key 任一字段，优先用 name
    name: str | None = None
    key: str | None = None

    display_name: str = ""
    description: str = ""
    usage: str | None = None
    author: str = ""
    version: str = "0.0.0"
    # entry 是可选的，默认为 plugin.py
    entry: str = "plugin.py"
    # permissions 和 config_schema 是可选扩展字段
    permissions: list[str] = Field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    config_actions: list[dict[str, Any]] = Field(default_factory=list)
    agent_tools: list[dict[str, Any]] = Field(default_factory=list)
    category: str | None = None
    interaction_profile: str | None = None
    interaction_entries: list[dict[str, Any]] = Field(default_factory=list)
    event_subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] | None = None
    min_telepilot_version: str | None = None
    # 0.15 rename 前的旧字段，继续作为兼容别名解析。
    min_telebot_version: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("name", "key")
    @classmethod
    def _validate_key(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = str(v).strip()
        if not v:
            return None
        # 路径穿越防御：禁止 . / \ 以及不可见字符
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$", v):
            raise ValueError(
                f"插件名仅允许字母/数字/_/-，得到 {v!r}"
            )
        return v

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        # 简单校验：必须是类似 x.y.z 的格式
        v = str(v).strip()
        if not re.match(r"^\d+\.\d+\.\d+", v):
            raise ValueError(f"版本号格式不正确: {v!r}")
        return v

    @field_validator("author")
    @classmethod
    def _validate_author(cls, v: str) -> str:
        # 防止注入
        v = str(v).strip()
        if len(v) > 255:
            raise ValueError("author 字段过长（最大 255 字符）")
        return v

    @field_validator("category")
    @classmethod
    def _validate_category(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = str(v).strip()
        if not value:
            return None
        if value not in {"interactive", "automation", "utility"}:
            raise ValueError("category 只能是 interactive / automation / utility")
        return value

    @field_validator("agent_tools")
    @classmethod
    def _validate_agent_tools(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        for index, raw in enumerate(value):
            name = str(raw.get("name") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError(f"agent_tools[{index}].name 格式无效")
            if name in seen:
                raise ValueError(f"agent_tools 工具名重复: {name}")
            seen.add(name)
            parameters = raw.get("parameters")
            if not isinstance(parameters, dict) or parameters.get("type") != "object":
                raise ValueError(f"agent_tools[{index}].parameters 必须是 type=object 的 JSON Schema")
            if "read_only" in raw and not isinstance(raw.get("read_only"), bool):
                raise ValueError(f"agent_tools[{index}].read_only 必须是布尔值")
            if "strict" in raw and not isinstance(raw.get("strict"), bool):
                raise ValueError(f"agent_tools[{index}].strict 必须是布尔值")
        return value

    @model_validator(mode="after")
    def _fill_name_from_key(self) -> PluginMetadataSchema:
        if self.name is None and self.key is not None:
            self.name = self.key
        if self.agent_tools:
            agent_capability = (self.capabilities or {}).get("agent_tools")
            enabled = agent_capability is True or (
                isinstance(agent_capability, dict) and agent_capability.get("enabled") is True
            )
            if not enabled:
                raise ValueError("声明 agent_tools 时必须启用 capabilities.agent_tools")
            if "ai_agent" not in self.permissions:
                raise ValueError("声明 agent_tools 时必须声明 permissions=['ai_agent']")
        return self


@dataclass
class PluginMetadata:
    """从 ``plugin.json`` 解析出来的统一形态。"""

    name: str
    display_name: str = ""
    description: str = ""
    usage: str | None = None
    author: str = ""
    version: str = "0.0.0"
    entry: str = "plugin.py"
    permissions: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    config_actions: list[dict[str, Any]] = field(default_factory=list)
    agent_tools: list[dict[str, Any]] = field(default_factory=list)
    category: str | None = None
    interaction_profile: str | None = None
    interaction_entries: list[dict[str, Any]] = field(default_factory=list)
    event_subscriptions: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] | None = None
    min_telepilot_version: str | None = None
    min_telebot_version: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RemotePluginUpdateCheckSummary:
    total: int = 0
    checked: int = 0
    update_available: int = 0
    failed: int = 0


@dataclass(slots=True)
class RemotePluginView:
    """由 installed_plugin 派生的远程插件 API 响应视图。"""

    id: int
    name: str
    display_name: str
    description: str
    usage: str | None
    author: str
    source_url: str
    version: str
    latest_version: str | None
    update_available: bool
    last_update_check_at: datetime | None
    last_update_check_error: str | None
    lint_warnings: list[str]
    event_subscriptions: list[dict[str, Any]]
    capabilities: dict[str, Any]
    permissions: list[str]
    enabled: bool
    default_enabled: bool
    installed_at: datetime | None


def _remote_info_from_manifest(manifest_json: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manifest_json, dict):
        return {}
    raw = manifest_json.get(_REMOTE_INFO_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _remote_info_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _with_remote_info(
    manifest_json: dict[str, Any],
    *,
    default_enabled: bool,
    latest_version: str | None,
    update_available: bool,
    last_update_check_at: datetime | None = None,
    last_update_check_error: str | None = None,
) -> dict[str, Any]:
    data = dict(manifest_json)
    data[_REMOTE_INFO_KEY] = {
        "default_enabled": bool(default_enabled),
        "latest_version": latest_version,
        "update_available": bool(update_available),
        "last_update_check_at": last_update_check_at.isoformat() if last_update_check_at else None,
        "last_update_check_error": last_update_check_error,
    }
    return data


def _set_remote_update_info(
    row: InstalledPlugin,
    *,
    latest_version: str | None,
    update_available: bool,
    last_update_check_error: str | None,
    default_enabled: bool | None = None,
) -> None:
    current = dict(row.manifest_json or {})
    info = _remote_info_from_manifest(current)
    if default_enabled is None:
        default_enabled = bool(info.get("default_enabled", False))
    row.manifest_json = _with_remote_info(
        current,
        default_enabled=default_enabled,
        latest_version=latest_version,
        update_available=update_available,
        last_update_check_at=datetime.now(UTC),
        last_update_check_error=last_update_check_error,
    )


def _source_label_for_installed(row: InstalledPlugin) -> str:
    if row.source_label:
        return row.source_label
    if row.source == PLUGIN_SOURCE_OFFICIAL:
        return "Plugin Repo"
    if row.source == PLUGIN_SOURCE_REPO:
        return "Plugin Repo"
    if row.source == PLUGIN_SOURCE_LOCAL:
        return "Local"
    return "Git"


def _installed_remote_query():
    return select(InstalledPlugin).where(InstalledPlugin.source.in_(_REMOTE_INSTALL_SOURCES))


def remote_plugin_view_from_installed(row: InstalledPlugin) -> RemotePluginView:
    """把 installed_plugin 行转换为远程插件 API 的兼容响应形状。"""

    manifest = dict(row.manifest_json or {})
    info = _remote_info_from_manifest(manifest)
    raw_permissions = manifest.get("permissions")
    permissions = [
        str(item)
        for item in raw_permissions
        if isinstance(item, str) and item.strip()
    ] if isinstance(raw_permissions, list) else []
    return RemotePluginView(
        id=0,
        name=row.key,
        display_name=str(manifest.get("display_name") or row.key),
        description=str(manifest.get("description") or ""),
        usage=str(manifest.get("usage") or "").strip() or None,
        author=str(manifest.get("author") or ""),
        source_url=str(row.source_url or ""),
        version=row.version,
        latest_version=info.get("latest_version") or row.version,
        update_available=bool(info.get("update_available", False)),
        last_update_check_at=_remote_info_datetime(info.get("last_update_check_at")),
        last_update_check_error=info.get("last_update_check_error"),
        lint_warnings=[item for item in (row.lint_warnings or []) if isinstance(item, str)],
        event_subscriptions=[
            item for item in manifest.get("event_subscriptions", []) if isinstance(item, dict)
        ] if isinstance(manifest.get("event_subscriptions"), list) else [],
        capabilities=dict(manifest.get("capabilities")) if isinstance(manifest.get("capabilities"), dict) else {},
        permissions=permissions,
        enabled=bool(row.enabled),
        default_enabled=bool(info.get("default_enabled", False)),
        installed_at=row.installed_at,
    )


def _feature_manifest_from_meta(meta: PluginMetadata) -> dict[str, Any] | None:
    manifest: dict[str, Any] = {}
    if meta.config_schema:
        manifest["config_schema"] = meta.config_schema
    if meta.config_actions:
        manifest["config_actions"] = [item for item in meta.config_actions if isinstance(item, dict)]
    if meta.agent_tools:
        manifest["agent_tools"] = [item for item in meta.agent_tools if isinstance(item, dict)]
    if meta.usage:
        manifest["usage"] = meta.usage
    if meta.category:
        manifest["category"] = meta.category
    if meta.interaction_profile:
        manifest["interaction_profile"] = meta.interaction_profile
    if meta.interaction_entries:
        manifest["interaction_entries"] = [item for item in meta.interaction_entries if isinstance(item, dict)]
    if meta.event_subscriptions:
        manifest["event_subscriptions"] = [item for item in meta.event_subscriptions if isinstance(item, dict)]
    if isinstance(meta.capabilities, dict) and meta.capabilities:
        manifest["capabilities"] = dict(meta.capabilities)
    if meta.permissions:
        manifest["permissions"] = list(meta.permissions)
    if meta.min_telepilot_version:
        manifest["min_telepilot_version"] = meta.min_telepilot_version
    if meta.min_telebot_version:
        manifest["min_telebot_version"] = meta.min_telebot_version
    return manifest or None


def _merge_feature_manifest_preserving_global_config(
    current: dict[str, Any] | None,
    meta: PluginMetadata,
) -> dict[str, Any] | None:
    """Build feature manifest from plugin metadata while preserving legacy global config.

    Remote plugin install/update refreshes manifest metadata from plugin.json. The
    active config source is plugin_global_config, but the old manifest key remains
    as a compatibility fallback for one rollout.
    """
    next_manifest = _feature_manifest_from_meta(meta)
    if current and "global_config" in current:
        next_manifest = dict(next_manifest or {})
        next_manifest["global_config"] = current["global_config"]
    return next_manifest


# ─────────────────────────────────────────────────────
# 内部工具：路径与 Git
# ─────────────────────────────────────────────────────
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")  # no dots to prevent .. traversal


def _installed_root() -> Path:
    """安装根目录（与 worker loader 的 ``_installed_dir`` 同源）。"""
    return settings.plugins_installed_path


def _plugin_dir(name: str) -> Path:
    """计算单个插件的安装目录，含路径穿越防御。"""
    root = _installed_root()
    target = (root / name).resolve()
    # 名字必须能作合法目录名，且最终路径必须落在 installed_root 之下
    if not _NAME_RE.match(name):
        raise RemotePluginError(
            "BAD_PLUGIN_NAME",
            f"插件名仅允许字母/数字/._- 字符，得到 {name!r}",
        )
    if root != target and root not in target.parents:
        raise RemotePluginError(
            "BAD_PLUGIN_NAME",
            f"插件名派生路径越界: {name!r}",
        )
    return target


def _plugin_data_dir(name: str) -> Path:
    """Return the update-safe persistent file directory for one plugin."""

    _plugin_dir(name)  # Reuse the plugin-name and confinement validation.
    data_root = (_installed_root() / "_data").resolve()
    candidate = data_root / name
    if candidate.is_symlink():
        raise RemotePluginError("PLUGIN_DATA_CONFLICT", f"插件数据目录不得是符号链接: {name!r}")
    target = candidate.resolve()
    if target == data_root or data_root not in target.parents:
        raise RemotePluginError("BAD_PLUGIN_NAME", f"插件数据目录越界: {name!r}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _replace_with_symlink(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}.persist-link-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _relocate_legacy_plugin_sqlite(install_path: Path, plugin_name: str) -> list[str]:
    """Atomically redirect legacy SQLite paths without changing the live inode."""

    if not install_path.is_dir():
        return []
    data_dir = _plugin_data_dir(plugin_name)
    names: list[str] = []
    for source in sorted(install_path.glob("*.sqlite3")):
        target = data_dir / source.name
        source_is_persistent_link = False
        if source.is_symlink():
            if source.resolve(strict=False) == target.resolve(strict=False) and target.is_file():
                names.append(source.name)
                source_is_persistent_link = True
            else:
                raise RemotePluginError(
                    "PLUGIN_DATA_CONFLICT",
                    f"插件 {plugin_name} 的旧数据库链接目标异常: {source.name}",
                )
        else:
            if not source.is_file():
                continue
            names.append(source.name)
        with sqlite3.connect(source, timeout=10.0, isolation_level=None) as migration_guard:
            migration_guard.execute("PRAGMA busy_timeout = 10000")
            migration_guard.execute("BEGIN IMMEDIATE")
            try:
                if not source_is_persistent_link:
                    if target.exists():
                        if not target.is_file() or not os.path.samefile(source, target):
                            raise RemotePluginError(
                                "PLUGIN_DATA_CONFLICT",
                                f"插件 {plugin_name} 的持久化数据库与旧数据库冲突: {source.name}",
                            )
                    else:
                        os.link(source, target)
                for suffix in ("-wal", "-shm"):
                    legacy_sidecar = install_path / f"{source.name}{suffix}"
                    persistent_sidecar = data_dir / f"{source.name}{suffix}"
                    if legacy_sidecar.is_symlink():
                        if legacy_sidecar.resolve(strict=False) != persistent_sidecar.resolve(strict=False):
                            raise RemotePluginError(
                                "PLUGIN_DATA_CONFLICT",
                                f"插件 {plugin_name} 的 SQLite 辅助文件链接异常: {legacy_sidecar.name}",
                            )
                        continue
                    if legacy_sidecar.exists():
                        if persistent_sidecar.exists():
                            if not os.path.samefile(legacy_sidecar, persistent_sidecar):
                                raise RemotePluginError(
                                    "PLUGIN_DATA_CONFLICT",
                                    f"插件 {plugin_name} 的 SQLite 辅助文件冲突: {legacy_sidecar.name}",
                                )
                        else:
                            os.link(legacy_sidecar, persistent_sidecar)
                    _replace_with_symlink(legacy_sidecar, persistent_sidecar)
                if not source_is_persistent_link:
                    _replace_with_symlink(source, target)
                migration_guard.commit()
            except Exception:
                migration_guard.rollback()
                raise
    return names


def _attach_legacy_plugin_sqlite_links(
    install_path: Path,
    plugin_name: str,
    sqlite_names: list[str],
) -> None:
    """Keep legacy plugin code pointed at the migrated DB until worker reload completes."""

    data_dir = _plugin_data_dir(plugin_name)
    for name in sqlite_names:
        target = data_dir / name
        if not target.is_file():
            continue
        for suffix in ("", "-wal", "-shm"):
            link = install_path / f"{name}{suffix}"
            target_link = data_dir / f"{name}{suffix}"
            if link.is_symlink() and link.resolve(strict=False) == target_link.resolve(strict=False):
                continue
            _replace_with_symlink(link, target_link)


def _relocate_legacy_plugin_json(install_path: Path, plugin_name: str) -> list[str]:
    """Move top-level runtime JSON files into the update-safe plugin data dir."""

    if not install_path.is_dir():
        return []
    data_dir = _plugin_data_dir(plugin_name)
    names: list[str] = []
    for source in sorted(install_path.glob("*.json")):
        if source.name == "plugin.json":
            continue
        target = data_dir / source.name
        if source.is_symlink():
            if source.resolve(strict=False) != target.resolve(strict=False) or not target.is_file():
                raise RemotePluginError(
                    "PLUGIN_DATA_CONFLICT",
                    f"插件 {plugin_name} 的旧 JSON 链接目标异常: {source.name}",
                )
            names.append(source.name)
            continue
        if not source.is_file():
            continue
        names.append(source.name)
        if target.exists():
            if not target.is_file():
                raise RemotePluginError(
                    "PLUGIN_DATA_CONFLICT",
                    f"插件 {plugin_name} 的持久化 JSON 路径不是文件: {source.name}",
                )
            continue
        temporary = target.with_name(f".{target.name}.migrating-{uuid.uuid4().hex}")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return names


def _attach_legacy_plugin_json_links(
    install_path: Path,
    plugin_name: str,
    json_names: list[str],
) -> None:
    """Keep old and new plugin code pointed at migrated runtime JSON files."""

    data_dir = _plugin_data_dir(plugin_name)
    for name in json_names:
        target = data_dir / name
        if target.is_file():
            _replace_with_symlink(install_path / name, target)


def _legacy_plugin_dir(name: str) -> Path:
    """旧版本在 backend/ 工作目录下运行时可能写到 backend/plugins/installed。"""
    backend_root = Path(__file__).resolve().parents[2]
    return (backend_root / "plugins" / "installed" / name).resolve()


def _existing_plugin_dir(name: str) -> Path:
    """返回当前插件实际目录；兼容已安装到旧 backend/plugins/installed 的插件。"""
    target = _plugin_dir(name)
    if target.exists():
        return target
    legacy = _legacy_plugin_dir(name)
    if legacy.exists():
        return legacy
    return target


def _validate_source_url(url: str) -> None:
    """校验 source_url 只允许 https:// 或 git+ssh://，防止本地文件/恶意 URL 攻击。

    Args:
        url: 待校验的 source_url

    Raises:
        InvalidSourceUrl: scheme 不在白名单中
    """
    if not url or not url.strip():
        raise InvalidSourceUrl("BAD_SOURCE_URL", "source_url 不能为空")

    url = normalize_git_source_url(url).clone_url

    # 解析 scheme
    if url.startswith("git+ssh://"):
        scheme = "git+ssh"
    elif "://" in url:
        scheme = url.split("://", 1)[0].lower()
    elif ":" in url and "@" in url:
        # scp-like 格式 git@github.com:foo/bar
        scheme = "ssh"
    else:
        raise InvalidSourceUrl(
            "BAD_SOURCE_URL",
            f"source_url 缺少合法 scheme: {url!r}",
        )

    # 特殊处理 scp-like SSH URL
    if scheme == "ssh" and url.startswith("git@"):
        # git@github.com:foo/bar.git 格式是允许的
        allowed_ssh_pattern = re.compile(r"^git@[a-zA-Z0-9.\-]+:[^:]+$")
        if not allowed_ssh_pattern.match(url):
            raise InvalidSourceUrl(
                "BAD_SOURCE_URL",
                f"SSH URL 格式不正确: {url!r}",
            )
        return

    if scheme not in _ALLOWED_URL_SCHEMES:
        raise InvalidSourceUrl(
            "BAD_SOURCE_URL",
            f"source_url 只允许 https:// 或 git+ssh:// scheme，得到 {scheme!r}",
        )


def _derive_name_from_url(url: str) -> str:
    """从 ``source_url`` 的最后一段推导插件名：
    - ``https://github.com/foo/bar.git`` → ``bar``
    - ``https://github.com/foo/bar/tree/dev`` → ``bar``
    - ``git@github.com:foo/bar`` → ``bar``
    - ``./local/path`` → ``path``
    """
    cleaned = normalize_git_source_url(url).clone_url.rstrip("/").strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    # 同时支持 ``/`` 与 ``:`` 作分隔（scp-like git URL）
    last = re.split(r"[/:]", cleaned)[-1] if cleaned else ""
    if not last:
        raise RemotePluginError(
            "BAD_SOURCE_URL", f"无法从 source_url 推导插件名: {url!r}"
        )
    return last


async def _run_git(
    *args: str,
    cwd: str | Path | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> str:
    """以子进程跑 ``git <args>``；失败抛 ``GitOperationFailed``。返回 stdout（已解码）。

    Args:
        timeout: git 操作超时秒数，默认 120s。clone 超时会导致半目录被清理。
    """
    if shutil.which("git") is None:
        raise GitOperationFailed(
            "GIT_NOT_FOUND",
            "服务器运行环境缺少 git，无法拉取远程模块库；请更新生产镜像或在运行环境安装 git。",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **env} if env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitOperationFailed(
            "GIT_NOT_FOUND",
            "服务器运行环境缺少 git，无法拉取远程模块库；请更新生产镜像或在运行环境安装 git。",
        ) from exc
    except PermissionError as exc:
        raise GitOperationFailed(
            "GIT_NOT_EXECUTABLE",
            "服务器运行环境中的 git 无法执行，请检查容器镜像或文件权限。",
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitOperationFailed(
            "GIT_TIMEOUT",
            f"git {redact_text(' '.join(args))} 超时（{timeout}s）",
        ) from None
    if proc.returncode != 0:
        msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise GitOperationFailed(
            "GIT_FAILED",
            f"git {redact_text(' '.join(args))} 失败 (rc={proc.returncode}): {redact_text(msg)}",
        )
    return (stdout or b"").decode("utf-8", errors="replace")


async def _clone_git_source(
    source_url: str,
    target: Path,
    *,
    depth: int | None = 1,
    timeout: float = 180.0,
    env: dict[str, str] | None = None,
) -> None:
    """Clone 插件源；GitHub tree/<branch> 页面链接会转为仓库 URL + 分支。"""
    _validate_source_url(source_url)
    source = normalize_git_source_url(source_url)
    args = ["clone"]
    if depth is not None:
        args.extend(["--depth", str(depth)])
    if source.ref:
        args.extend(["--branch", source.ref, "--single-branch"])
    args.extend([source.clone_url, str(target)])
    await _run_git(*args, timeout=timeout, env=env)


# ─────────────────────────────────────────────────────
# 元数据读取：只支持 plugin.json，禁止执行 manifest.py
# ─────────────────────────────────────────────────────
def _read_plugin_metadata(plugin_dir: Path, *, fallback_name: str) -> PluginMetadata:
    """从插件目录读元数据。**安全设计：只解析 plugin.json，绝对不执行 manifest.py**。

    plugin.json 字段约定：
        {
          "name" / "key": str (必填其一，name 优先),
          "display_name": str,
          "description": str,
          "author": str,
          "version": str,
          "entry": str (可选，默认 plugin.py),
          "min_telepilot_version": str (推荐，旧 min_telebot_version 仍兼容),
          "permissions": list[str] (可选),
          "config_schema": dict (可选)
        }

    Args:
        plugin_dir: 插件目录路径
        fallback_name: plugin.json 解析失败时的回退名称

    Returns:
        PluginMetadata 实例

    Raises:
        InvalidPluginMetadata: plugin.json 不存在或解析失败
    """
    # ── 只允许 plugin.json ──
    pj = plugin_dir / "plugin.json"
    if not pj.is_file():
        raise InvalidPluginMetadata(
            "PLUGIN_JSON_NOT_FOUND",
            f"插件目录 {plugin_dir} 必须包含 plugin.json（manifest.py 在安装阶段禁止执行）",
        )

    try:
        raw_data = json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidPluginMetadata(
            "BAD_PLUGIN_JSON", f"plugin.json 解析失败: {exc}"
        ) from exc

    # Pydantic 校验
    try:
        validated = PluginMetadataSchema(**raw_data)
    except Exception as exc:
        raise InvalidPluginMetadata(
            "BAD_PLUGIN_JSON",
            f"plugin.json 字段校验失败: {exc}",
        ) from exc

    # 提取 name（优先 name，key 作备选）
    name = validated.name or validated.key or fallback_name

    # 再次路径穿越防御（防御 plugin.json 中可能的恶意 name）
    if not _NAME_RE.match(name):
        raise InvalidPluginMetadata(
            "BAD_PLUGIN_NAME",
            f"plugin.json 中的 name/key 非法: {name!r}",
        )

    return PluginMetadata(
        name=name,
        display_name=str(validated.display_name or ""),
        description=str(validated.description or ""),
        usage=str(validated.usage or "").strip() or None,
        author=str(validated.author or ""),
        version=str(validated.version or "0.0.0"),
        entry=str(validated.entry or "plugin.py"),
        permissions=list(validated.permissions or []),
        config_schema=validated.config_schema,
        config_actions=[item for item in validated.config_actions if isinstance(item, dict)],
        agent_tools=[item for item in validated.agent_tools if isinstance(item, dict)],
        category=validated.category,
        interaction_profile=validated.interaction_profile,
        interaction_entries=[item for item in validated.interaction_entries if isinstance(item, dict)],
        event_subscriptions=[item for item in validated.event_subscriptions if isinstance(item, dict)],
        capabilities=dict(validated.capabilities) if isinstance(validated.capabilities, dict) else None,
        min_telepilot_version=validated.min_telepilot_version,
        min_telebot_version=validated.min_telebot_version,
        tags=list(validated.tags or []),
    )


def _validate_runtime_plugin_shape(plugin_dir: Path, meta: PluginMetadata) -> None:
    """校验远程插件运行期结构，避免安装后 worker 找不到实现。

    安装阶段仍不执行 Python，只检查必要文件存在。远程插件必须按新版文档
    提供完整包结构：plugin.json + manifest.py + plugin.py + __init__.py。
    """
    missing: list[str] = []
    for filename in ("manifest.py", "plugin.py", "__init__.py"):
        if not (plugin_dir / filename).is_file():
            missing.append(filename)

    entry = str(meta.entry or "plugin.py")
    if "/" in entry or "\\" in entry or not entry.endswith(".py"):
        raise InvalidPluginMetadata(
            "BAD_PLUGIN_ENTRY",
            f"plugin.json entry 必须是当前插件目录下的 .py 文件，得到 {entry!r}。请按 docs/PLUGIN-REMOTE.md 更新插件结构。",
        )
    if not (plugin_dir / entry).is_file():
        missing.append(entry)

    if missing:
        unique = sorted(set(missing))
        raise InvalidPluginMetadata(
            "PLUGIN_RUNTIME_FILES_MISSING",
            "远程插件缺少运行期文件："
            + ", ".join(unique)
            + "。新版远程插件必须包含 plugin.json、manifest.py、plugin.py、__init__.py；请按 docs/PLUGIN-REMOTE.md 更新插件后再安装。",
        )


def _version_tuple(raw: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", str(raw or ""))[:3])


def _has_newer_version(latest: str | None, current: str | None) -> bool:
    latest_tuple = _version_tuple(latest)
    current_tuple = _version_tuple(current)
    return bool(latest_tuple and current_tuple and latest_tuple > current_tuple)


def _iter_json_strings(value: Any, path: str = "$"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_json_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from _iter_json_strings(item, f"{path}[{idx}]")


def _iter_manifest_string_literals(manifest_text: str):
    try:
        tree = ast.parse(manifest_text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield getattr(node, "lineno", 0), node.value


def _is_forbidden_internal_import(name: str) -> bool:
    return name == "app.db" or name.startswith("app.db.") or name == "app.services" or name.startswith("app.services.")


def _lint_python_source_file(path: Path, plugin_dir: Path) -> list[str]:
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except Exception:  # noqa: BLE001
        return warnings

    rel = path.relative_to(plugin_dir)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_internal_import(alias.name):
                    warnings.append(f"{rel}:line {node.lineno} 禁止直接 import 内部模块 {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden_internal_import(module):
                warnings.append(f"{rel}:line {node.lineno} 禁止直接 import 内部模块 {module!r}")
        elif isinstance(node, ast.Call):
            func = node.func
            call_name: str | None = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                call_name = f"{func.value.id}.{func.attr}"
            if call_name and call_name.split(".", 1)[0] in {"httpx", "requests"}:
                has_timeout = any(keyword.arg == "timeout" for keyword in node.keywords)
                if not has_timeout:
                    warnings.append(f"{rel}:line {node.lineno} {call_name} 调用缺少 timeout 参数")
    return warnings


def _lint_metadata_text_file(path: Path, plugin_dir: Path) -> list[str]:
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return warnings
    rel = path.relative_to(plugin_dir)
    for needle in ("import app.db.", "from app.db.", "import app.services.", "from app.services."):
        if needle in text:
            warnings.append(f"{rel} 元数据疑似引用内部模块 {needle.strip()!r}")
    return warnings


def _warn_hardcoded_prefix(source: str, location: str, text: str) -> str | None:
    if "{prefix}" in text:
        return None
    match = _HARDCODED_PREFIX_RE.search(text)
    if not match:
        return None
    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 100:
        snippet = snippet[:97] + "..."
    return f"{source} {location} 疑似硬编码命令前缀 {match.group(0)!r}：{snippet}"


def lint_plugin_metadata_files(plugin_dir: Path) -> list[str]:
    """静态 lint 插件源码/metadata，只给 warning，不阻断安装。

    manifest.py 只做 AST 解析，不执行任何代码。
    """
    warnings: list[str] = []
    pj = plugin_dir / "plugin.json"
    if pj.is_file():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(data, dict) and not _plugin_declares_usage_guide(data):
                warnings.append(
                    "高级规范警告：插件未声明详细使用说明。请在 plugin.json 中提供 usage，或在 config_schema 中提供 usage_preview、x-usage-guide 或 x-usage-steps。"
                )
            for path, text in _iter_json_strings(data):
                warning = _warn_hardcoded_prefix("plugin.json", path, text)
                if warning:
                    warnings.append(warning)
        except Exception:  # noqa: BLE001
            pass

    manifest = plugin_dir / "manifest.py"
    if manifest.is_file():
        try:
            text = manifest.read_text(encoding="utf-8")
            for lineno, literal in _iter_manifest_string_literals(text):
                warning = _warn_hardcoded_prefix("manifest.py", f"line {lineno}", literal)
                if warning:
                    warnings.append(warning)
        except Exception:  # noqa: BLE001
            pass

    for metadata_file in (plugin_dir / "plugin.json", plugin_dir / "manifest.py"):
        if metadata_file.is_file():
            warnings.extend(_lint_metadata_text_file(metadata_file, plugin_dir))

    for path in plugin_dir.rglob("*.py"):
        if any(part in {".git", "__pycache__"} for part in path.relative_to(plugin_dir).parts):
            continue
        warnings.extend(_lint_python_source_file(path, plugin_dir))

    plugin_json_path = plugin_dir / "plugin.json"
    if plugin_json_path.is_file():
        try:
            data = json.loads(plugin_json_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            entries = data.get("interaction_entries")
            if isinstance(entries, list) and entries:
                for idx, raw_entry in enumerate(entries, start=1):
                    if not isinstance(raw_entry, dict):
                        continue
                    key = str(raw_entry.get("key") or "").strip()
                    if not key:
                        warnings.append(f"plugin.json interaction_entries[{idx}] 缺少 key")
                    if raw_entry.get("preserve_command_trigger", True) is not True:
                        warnings.append(f"plugin.json interaction_entries[{idx}] 建议显式声明 preserve_command_trigger=true")
                    session_scope = str(raw_entry.get("session_scope") or "").strip()
                    if session_scope not in {"chat", "user", "none"}:
                        warnings.append(f"plugin.json interaction_entries[{idx}] session_scope 必须是 chat / user / none")
                    raw_events = raw_entry.get("events")
                    if not isinstance(raw_events, list) or not any(str(item or "").strip() for item in raw_events):
                        warnings.append(f"plugin.json interaction_entries[{idx}] 建议显式声明 events")
                    result_contract = raw_entry.get("result_contract")
                    if isinstance(result_contract, dict):
                        send_via = result_contract.get("send_via")
                        send_via_items = send_via if isinstance(send_via, list) else [send_via]
                        if send_via is not None and (
                            any(not send_via_selector_options(item) for item in send_via_items)
                            or any(unsupported_send_via_values(item) for item in send_via_items)
                        ):
                            warnings.append(f"plugin.json interaction_entries[{idx}] result_contract.send_via 含有未支持值")
                    raw_dispatch_modes = raw_entry.get("dispatch_modes")
                    if isinstance(raw_dispatch_modes, list) and raw_dispatch_modes:
                        unsupported = [
                            str(item).strip()
                            for item in raw_dispatch_modes
                            if str(item).strip() not in {"admin_command", "public_keyword"}
                        ]
                        if unsupported:
                            warnings.append(f"plugin.json interaction_entries[{idx}] dispatch_modes 含有未支持值")
                    if not raw_entry.get("interaction_profile"):
                        warnings.append(f"plugin.json interaction_entries[{idx}] 建议声明 interaction_profile")
            subscriptions = data.get("event_subscriptions")
            if isinstance(subscriptions, list) and subscriptions:
                for idx, raw_subscription in enumerate(subscriptions, start=1):
                    if not isinstance(raw_subscription, dict):
                        warnings.append(f"plugin.json event_subscriptions[{idx}] 必须是对象")
                        continue
                    raw_events = raw_subscription.get("events")
                    if not isinstance(raw_events, list) or not any(str(item or "").strip() in VALID_EVENT_TYPES for item in raw_events):
                        warnings.append(f"plugin.json event_subscriptions[{idx}] events 必须声明有效事件")
                    if isinstance(raw_events, list):
                        unknown_events = sorted(
                            {
                                str(item or "").strip()
                                for item in raw_events
                                if str(item or "").strip() and str(item or "").strip() not in VALID_EVENT_TYPES
                            }
                        )
                        if unknown_events:
                            warnings.append(
                                f"plugin.json event_subscriptions[{idx}] events 含未知类型: {', '.join(unknown_events)}，这些事件类型不会匹配任何当前支持的事件"
                            )
                    raw_sources = raw_subscription.get("source")
                    if raw_sources is not None:
                        source_items = raw_sources if isinstance(raw_sources, list) else [raw_sources]
                        if any(str(item or "").strip() not in VALID_EVENT_SOURCES for item in source_items):
                            warnings.append(f"plugin.json event_subscriptions[{idx}] source 含有未支持值")
                    scope = str(raw_subscription.get("scope") or "all_allowed_chats").strip()
                    if scope not in VALID_EVENT_SCOPES:
                        warnings.append(f"plugin.json event_subscriptions[{idx}] scope 必须是 all_allowed_chats / owner_only / known_users / inline_all / rule_bound")
                    filters = raw_subscription.get("filters")
                    if isinstance(filters, dict):
                        unknown = sorted(
                            {
                                str(key).strip()
                                for key in filters
                                if str(key).strip() and str(key).strip() not in SUPPORTED_FILTER_KEYS
                            }
                        )
                        if unknown:
                            warnings.append(
                                f"plugin.json event_subscriptions[{idx}] filters 含未知 key: {', '.join(unknown)}，该过滤条件不会生效，订阅可能匹配到预期外的事件"
                            )
            capabilities = data.get("capabilities")
            if isinstance(capabilities, dict):
                native_raw = capabilities.get("telegram_native_raw")
                if native_raw is not None:
                    if not isinstance(native_raw, dict):
                        warnings.append("plugin.json capabilities.telegram_native_raw 必须是对象")
                    else:
                        if native_raw.get("enabled") is True and not str(native_raw.get("reason") or "").strip():
                            warnings.append("plugin.json capabilities.telegram_native_raw.enabled=true 时必须说明 reason")
                        sources = native_raw.get("sources")
                        if sources is not None and not isinstance(sources, list):
                            warnings.append("plugin.json capabilities.telegram_native_raw.sources 必须是数组")

    # 去重并限制数量，避免一个坏模板刷屏。
    unique: list[str] = []
    for item in warnings:
        if item not in unique:
            unique.append(item)
    return unique[:10]


def _plugin_declares_usage_guide(manifest: dict[str, Any]) -> bool:
    if _has_usage_content(manifest.get("usage")):
        return True
    schema = manifest.get("config_schema")
    if not isinstance(schema, dict):
        return False
    for key in ("x-usage-guide", "x-usage-instructions", "x-usage-steps", "x-help"):
        if _has_usage_content(schema.get(key)):
            return True
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    for key in ("usage_preview", "usage_guide", "usage_instructions", "ai_usage_guide"):
        field = properties.get(key)
        if isinstance(field, dict) and (
            _has_usage_content(field.get("default")) or _has_usage_content(field.get("description"))
        ):
            return True
    return False


def _has_usage_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_usage_content(item) for item in value)
    if isinstance(value, dict):
        return bool(value)
    return False


def _manifest_json_from_remote_meta(meta: PluginMetadata) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": meta.name,
        "display_name": meta.display_name,
        "description": meta.description,
        "author": meta.author,
        "version": meta.version,
        "entry": meta.entry,
        "permissions": list(meta.permissions),
    }
    if meta.usage:
        data["usage"] = meta.usage
    if meta.config_schema is not None:
        data["config_schema"] = meta.config_schema
    if meta.config_actions:
        data["config_actions"] = [item for item in meta.config_actions if isinstance(item, dict)]
    if meta.agent_tools:
        data["agent_tools"] = [item for item in meta.agent_tools if isinstance(item, dict)]
    if meta.category:
        data["category"] = meta.category
    if meta.interaction_profile:
        data["interaction_profile"] = meta.interaction_profile
    if meta.interaction_entries:
        data["interaction_entries"] = [item for item in meta.interaction_entries if isinstance(item, dict)]
    if meta.event_subscriptions:
        data["event_subscriptions"] = [item for item in meta.event_subscriptions if isinstance(item, dict)]
    if isinstance(meta.capabilities, dict) and meta.capabilities:
        data["capabilities"] = dict(meta.capabilities)
    if meta.min_telepilot_version:
        data["min_telepilot_version"] = meta.min_telepilot_version
    if meta.min_telebot_version:
        data["min_telebot_version"] = meta.min_telebot_version
    if meta.tags:
        data["tags"] = list(meta.tags)
    return data


async def upsert_installed_plugin(
    db: AsyncSession,
    *,
    key: str,
    source: str,
    manifest_json: dict[str, Any] | None,
    installed_path: str | None,
    source_url: str | None = None,
    version: str = "0.0.0",
    enabled: bool = False,
    signature_ok: bool | None = None,
    trust_tier: str = PLUGIN_TRUST_COMMUNITY,
    source_label: str | None = None,
    last_install_error: str | None = None,
    lint_warnings: list[str] | None = None,
) -> InstalledPlugin:
    """写入统一安装记录表。"""
    row = await db.get(InstalledPlugin, key)
    if row is None:
        row = InstalledPlugin(key=key, source=source)
        db.add(row)
    row.source = source
    row.source_url = source_url
    row.installed_path = installed_path
    row.version = version or "0.0.0"
    row.manifest_json = manifest_json
    row.enabled = bool(enabled)
    row.signature_ok = signature_ok
    row.trust_tier = trust_tier
    row.source_label = source_label
    row.last_install_error = last_install_error
    row.lint_warnings = list(lint_warnings or [])
    return row


async def set_installed_plugin_enabled(
    db: AsyncSession,
    key: str,
    enabled: bool,
) -> None:
    """直接同步 installed_plugin.enabled；保留给邻近服务复用。"""

    row = await db.get(InstalledPlugin, key)
    if row is None:
        return
    row.enabled = bool(enabled)
    await db.flush()


async def delete_installed_plugin_record(db: AsyncSession, key: str) -> None:
    """删除统一安装记录；保留给邻近服务复用。"""

    row = await db.get(InstalledPlugin, key)
    if row is None:
        return
    await db.delete(row)
    await db.flush()


def _find_plugin_metadata_in_repo(repo_dir: Path, name: str) -> tuple[PluginMetadata, Path]:
    candidates = [repo_dir]
    candidates.extend([p for p in repo_dir.iterdir() if p.is_dir() and not p.name.startswith(".")])
    for candidate in candidates:
        if not (candidate / "plugin.json").is_file():
            continue
        try:
            meta = _read_plugin_metadata(candidate, fallback_name=candidate.name)
        except InvalidPluginMetadata:
            continue
        if meta.name == name:
            return meta, candidate
    raise RemotePluginError(
        "PLUGIN_NOT_IN_REPO",
        f"仓库内未找到插件 {name!r}",
    )


async def _git_env_for_installed_source(
    db: AsyncSession,
    source_url: str,
) -> dict[str, str] | None:
    """复用插件仓库保存的私有仓库凭证。"""
    if not source_url:
        return None
    from . import plugin_repo_service

    return await plugin_repo_service.git_env_for_source_url(db, source_url)


async def _copy_plugin_from_source_url(
    *,
    name: str,
    source_url: str,
    target: Path,
    replace_existing: bool,
    git_env: dict[str, str] | None = None,
) -> None:
    """Clone ``source_url`` and copy plugin ``name`` into ``target``.

    Used by update for multi-plugin repos and as a self-healing path for legacy
    rows whose installed directory was missing after the InstalledPlugin cutover.
    """

    if not source_url:
        raise RemotePluginError("SOURCE_URL_MISSING", f"插件 {name} 缺少 source_url，无法更新")
    if source_url.startswith("local://"):
        raise RemotePluginError(
            "DIR_MISSING",
            f"插件目录已丢失: {target}；本地导入插件无法从 {source_url!r} 自动恢复，请重新导入。",
        )
    _validate_source_url(source_url)

    staging = target.with_name(f"{target.name}.installing")
    backup = target.with_name(f"{target.name}.bak-update")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    swapped = False
    legacy_sqlite_names: list[str] = []
    legacy_json_names: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="telepilot-plugin-update-") as tmp:
            repo_dir = Path(tmp) / "repo"
            await _clone_git_source(source_url, repo_dir, timeout=180.0, env=git_env)
            _, source_dir = _find_plugin_metadata_in_repo(repo_dir, name)
            shutil.copytree(
                source_dir,
                staging,
                ignore=shutil.ignore_patterns(".git", ".gitignore", "__pycache__"),
            )
            staged_meta = _read_plugin_metadata(staging, fallback_name=name)
            if staged_meta.name != name:
                raise RemotePluginError(
                    "PLUGIN_NAME_MISMATCH",
                    f"仓库 {source_url!r} 中匹配到的插件名为 {staged_meta.name!r}，预期 {name!r}",
                )
            _validate_runtime_plugin_shape(staging, staged_meta)

        legacy_sqlite_names = _relocate_legacy_plugin_sqlite(target, name)
        legacy_json_names = _relocate_legacy_plugin_json(target, name)
        if replace_existing:
            begin_plugin_update(target.parent, name, target_version=staged_meta.version)
        if replace_existing and target.exists():
            target.rename(backup)
        staging.rename(target)
        swapped = True
        _attach_legacy_plugin_sqlite_links(target, name, legacy_sqlite_names)
        _attach_legacy_plugin_json_links(target, name, legacy_json_names)
    except Exception:
        if replace_existing:
            clear_plugin_update(target.parent, name)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if replace_existing:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                backup.rename(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            if swapped:
                shutil.rmtree(backup, ignore_errors=True)
            elif replace_existing and not target.exists():
                backup.rename(target)


async def check_remote_plugin_update(db: AsyncSession, row: InstalledPlugin) -> InstalledPlugin:
    """检查单个远程模块是否有更新，并把状态写回 installed_plugin 行。"""
    try:
        if str(row.source_url or "").startswith("local://"):
            target = Path(row.installed_path or _existing_plugin_dir(row.key))
            row.lint_warnings = lint_plugin_metadata_files(target) if target.exists() else []
            _set_remote_update_info(
                row,
                latest_version=row.version,
                update_available=False,
                last_update_check_error=None,
            )
            await db.flush()
            return row

        with tempfile.TemporaryDirectory(prefix="telepilot-plugin-check-") as tmp:
            repo_dir = Path(tmp) / "repo"
            source_url = str(row.source_url or "")
            if not source_url:
                raise RemotePluginError("SOURCE_URL_MISSING", f"插件 {row.key} 缺少 source_url，无法检查更新")
            git_env = await _git_env_for_installed_source(db, source_url)
            await _clone_git_source(source_url, repo_dir, timeout=180.0, env=git_env)
            meta, plugin_dir = _find_plugin_metadata_in_repo(repo_dir, row.key)
            _set_remote_update_info(
                row,
                latest_version=meta.version,
                update_available=_has_newer_version(meta.version, row.version),
                last_update_check_error=None,
            )
            row.lint_warnings = lint_plugin_metadata_files(plugin_dir)
    except Exception as exc:  # noqa: BLE001
        _set_remote_update_info(
            row,
            latest_version=row.version,
            update_available=False,
            last_update_check_error=redact_text(f"{type(exc).__name__}: {exc}"),
        )
    await db.flush()
    return row


async def check_updates(
    db: AsyncSession,
    *,
    name: str | None = None,
) -> RemotePluginUpdateCheckSummary:
    """检查已安装远程模块更新状态；只更新 installed_plugin 标记，不自动安装新版本。"""
    stmt = _installed_remote_query().order_by(InstalledPlugin.key)
    if name:
        stmt = stmt.where(InstalledPlugin.key == name)
    rows = (await db.execute(stmt)).scalars().all()
    summary = RemotePluginUpdateCheckSummary(total=len(rows))
    for row in rows:
        await check_remote_plugin_update(db, row)
        info = _remote_info_from_manifest(row.manifest_json)
        summary.checked += 1
        if info.get("update_available"):
            summary.update_available += 1
        if info.get("last_update_check_error"):
            summary.failed += 1
    return summary


async def _load_update_check_setting() -> tuple[bool, int]:
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "remote_plugin_update_check")
            value = row.value if row is not None else {}
    except Exception:  # noqa: BLE001
        return True, _REMOTE_UPDATE_DEFAULT_INTERVAL_MINUTES
    if not isinstance(value, dict):
        value = {}
    enabled = bool(value.get("enabled", True))
    try:
        interval = int(value.get("interval_minutes") or _REMOTE_UPDATE_DEFAULT_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        interval = _REMOTE_UPDATE_DEFAULT_INTERVAL_MINUTES
    interval = max(30, min(interval, 10080))
    return enabled, interval


async def auto_update_check_loop() -> None:
    """后台自动检查远程模块是否有可更新版本。"""
    await asyncio.sleep(15)
    last_run_at: datetime | None = None
    while True:
        enabled, interval = await _load_update_check_setting()
        now = datetime.now(UTC)
        due = last_run_at is None or (now - last_run_at).total_seconds() >= interval * 60
        if enabled and due:
            try:
                async with AsyncSessionLocal() as db:
                    await check_updates(db)
                    await db.commit()
                last_run_at = datetime.now(UTC)
            except Exception:  # noqa: BLE001
                last_run_at = datetime.now(UTC)
                log.warning("远程模块自动检查更新失败", exc_info=True)
        await asyncio.sleep(60)


# ─────────────────────────────────────────────────────
# 触发 worker 热加载
# ─────────────────────────────────────────────────────
async def _trigger_reload(db: AsyncSession, name: str) -> list[int]:
    """通知 worker 重新加载该插件。

    两条路径并行：
    - 通过 Redis IPC ``CMD_RELOAD_CONFIG`` 广播到所有账号 worker
    - 直接调本进程的 ``reload_account_config``（在 worker 进程内才会有效，主进程为 no-op）

    返回未确认的账号；调用方仅在磁盘覆盖更新已经建立屏障时严格报错。
    """
    try:
        rows = (
            await db.execute(
                select(AccountFeature.account_id)
                .where(AccountFeature.feature_key == name)
                .distinct()
            )
        ).scalars().all()
        aids = [int(a) for a in rows]
    except Exception as exc:
        log.exception("拉取插件重载目标账号失败 plugin=%s", name)
        raise RemotePluginError(
            "PLUGIN_RELOAD_TARGETS_FAILED",
            f"插件 {name} 已写入磁盘，但无法确认需要重载的 worker；旧实例已被更新屏障阻止调用",
        ) from exc

    # 1) Redis IPC 广播
    unconfirmed: list[int] = []
    try:
        from ..redis_client import get_redis  # 延迟 import 防循环

        redis = get_redis()
        for aid in aids:
            try:
                ok = await publish_cmd_with_ack(
                    redis,
                    aid,
                    CMD_RELOAD_CONFIG,
                    timeout=15.0,
                    plugin_key=name,
                )
                if not ok:
                    unconfirmed.append(aid)
                    log.warning("worker reload_config 未确认 aid=%s plugin=%s", aid, name)
            except Exception:  # noqa: BLE001
                unconfirmed.append(aid)
                log.warning("redis 广播 reload_config 失败 aid=%s", aid, exc_info=True)
    except Exception:  # noqa: BLE001
        unconfirmed = list(aids)
        log.warning("redis 不可用，无法确认插件重载", exc_info=True)

    # 2) 进程内直接调用（worker 进程才有效；主进程内 _STATES 为空，函数会早返回）
    for aid in aids:
        try:
            await reload_account_config(aid, {"plugin_key": name})
        except Exception:  # noqa: BLE001
            log.debug("inproc reload_config 失败 aid=%s name=%s", aid, name, exc_info=True)
    return sorted(set(unconfirmed))


async def trigger_reload(db: AsyncSession, name: str) -> None:
    """提交事务后通知 worker；更新屏障只在所有在线目标确认后解除。"""

    root = _installed_root()
    barrier_active = plugin_update_active(root, name)
    try:
        unconfirmed = await _trigger_reload(db, name)
    except RemotePluginError:
        if barrier_active:
            raise
        log.warning("插件重载目标查询失败 plugin=%s，将由周期 reconcile 收敛", name)
        return
    if unconfirmed:
        if barrier_active:
            raise RemotePluginError(
                "PLUGIN_RELOAD_UNCONFIRMED",
                f"插件 {name} 已写入磁盘，但账号 {', '.join(str(item) for item in unconfirmed)} 的 worker 未确认重载；旧实例已被更新屏障阻止调用",
            )
        log.warning("插件重载未获全部 worker 确认 plugin=%s accounts=%s", name, unconfirmed)
        return
    clear_plugin_update(root, name)


async def _enable_for_all_accounts_if_unclaimed(db: AsyncSession, name: str) -> int:
    """首次全局启用远程插件时，为现有账号创建账号级启用行。

    已经存在任何 account_feature 行时保留用户的账号级选择；这避免后续全局开关
    反复开关时，把用户手动关闭的账号重新打开。
    """
    existing = (
        await db.execute(select(AccountFeature).where(AccountFeature.feature_key == name))
    ).scalars().all()
    if existing:
        return 0

    aids = (await db.execute(select(Account.id))).scalars().all()
    for aid in aids:
        db.add(
            AccountFeature(
                account_id=int(aid),
                feature_key=name,
                enabled=True,
                state=FEATURE_STATE_DISABLED,
            )
        )
    await db.flush()
    return len(aids)


async def enable_for_all_accounts(db: AsyncSession, name: str) -> int:
    """为所有现有账号启用远程插件的账号级开关。

    远程插件实际加载需要 InstalledPlugin.enabled 和 AccountFeature.enabled 同时为真。
    管理页的“启用”是用户可见的显式动作，因此应让它收敛到“当前账号都能实际运行”，
    避免只打开全局开关却留下账号级 disabled 的半启用状态。
    """
    aids = (await db.execute(select(Account.id))).scalars().all()
    changed = 0
    for aid in aids:
        account_id = int(aid)
        af = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == account_id,
                    AccountFeature.feature_key == name,
                )
            )
        ).scalar_one_or_none()
        if af is None:
            db.add(
                AccountFeature(
                    account_id=account_id,
                    feature_key=name,
                    enabled=True,
                    state=FEATURE_STATE_DISABLED,
                )
            )
            changed += 1
        elif not af.enabled:
            af.enabled = True
            af.state = FEATURE_STATE_DISABLED
            af.last_error = None
            changed += 1
    await db.flush()
    return changed


# ─────────────────────────────────────────────────────
# 核心动作
# ─────────────────────────────────────────────────────
async def install(
    db: AsyncSession,
    source_url: str,
    *,
    name: str | None = None,
    enable: bool = False,
    default_enabled: bool = False,
) -> RemotePluginView:
    """从 Git 仓库克隆并安装一个远程插件。

    **安全要求**：
    - source_url 必须通过 ``_validate_source_url``（只允许 https:// 或 git+ssh://）
    - 安装阶段绝对不执行任何 Python 代码（只解析 plugin.json）

    步骤：
      1. 推导 / 校验 ``name``
      2. 校验 ``source_url`` scheme
      3. 拒绝重名：DB 已有同名行或目录已存在 → ``DuplicatePluginName``
      4. ``git clone <source_url> plugins/installed/<name>`` (带 timeout)
      5. 读 ``plugin.json``（不执行 manifest.py）
      6. 写 ``installed_plugin`` 行
      7. 注册到 ``feature`` 表（is_builtin=False），使功能矩阵可见
      8. 若 ``default_enabled=True``，为所有已有账号创建 ``AccountFeature`` 行
      9. 触发 ``reload_config`` 广播

    任何中间步骤失败：已克隆的目录会被清理；DB 不会留下脏行（由调用方
    在事务里 commit / rollback 即可，本函数只 ``flush``）。
    """
    # 1. 安全校验 source_url
    _validate_source_url(source_url)

    final_name = name or _derive_name_from_url(source_url)
    target = _plugin_dir(final_name)
    staging = target.parent / f"{target.name}.installing"

    # 2. 重名拦截：先查 DB，再查目录
    existing = await db.get(InstalledPlugin, final_name)
    if existing is not None:
        raise DuplicatePluginName(
            "PLUGIN_EXISTS", f"插件 {final_name!r} 已安装"
        )
    legacy_target = _legacy_plugin_dir(final_name)
    if target.exists() or legacy_target.exists():
        raise DuplicatePluginName(
            "DIR_EXISTS", f"目录已存在但 DB 无记录: {target}（请先手动清理）"
        )
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    # 3. 确保父目录存在
    target.parent.mkdir(parents=True, exist_ok=True)

    renamed = False
    # 4. git clone 到 staging（带 timeout，防止挂起）
    try:
        await _clone_git_source(source_url, staging, timeout=180.0)
    except GitOperationFailed:
        # 失败时清理可能产生的部分目录
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        meta = _read_plugin_metadata(staging, fallback_name=final_name)
        _validate_runtime_plugin_shape(staging, meta)
        lint_warnings = lint_plugin_metadata_files(staging)
        staging.rename(target)
        renamed = True
        final_enabled = bool(enable or default_enabled)

        # 注册到 feature 表（使功能矩阵可见）
        feat = (
            await db.execute(select(Feature).where(Feature.key == final_name))
        ).scalar_one_or_none()
        if feat is None:
            db.add(Feature(
                key=final_name,
                display_name=meta.display_name or final_name,
                is_builtin=False,
                version=meta.version,
                manifest=_feature_manifest_from_meta(meta),
            ))
        else:
            # 已存在则校正 display_name 和 version
            feat.display_name = meta.display_name or final_name
            feat.version = meta.version
            feat.is_builtin = False
            feat.manifest = _merge_feature_manifest_preserving_global_config(feat.manifest, meta)

        manifest_json = _with_remote_info(
            _manifest_json_from_remote_meta(meta),
            default_enabled=default_enabled,
            latest_version=meta.version,
            update_available=False,
        )
        row = await upsert_installed_plugin(
            db,
            key=final_name,
            source=PLUGIN_SOURCE_GIT,
            source_url=source_url,
            installed_path=str(target),
            version=meta.version,
            manifest_json=manifest_json,
            enabled=final_enabled,
            signature_ok=None,
            trust_tier=PLUGIN_TRUST_COMMUNITY,
            source_label="Git",
            last_install_error=None,
            lint_warnings=lint_warnings,
        )
        await db.flush()

        # 如果 default_enabled=True，为所有已有账号启用
        if default_enabled:
            aids = (await db.execute(select(Account.id))).scalars().all()
            for aid in aids:
                af = (
                    await db.execute(
                        select(AccountFeature).where(
                            AccountFeature.account_id == int(aid),
                            AccountFeature.feature_key == final_name,
                        )
                    )
                ).scalar_one_or_none()
                if af is None:
                    db.add(AccountFeature(
                        account_id=int(aid),
                        feature_key=final_name,
                        enabled=True,
                        state=FEATURE_STATE_DISABLED,
                    ))
            await db.flush()

    except Exception:
        # 元数据/写库失败 → 回滚文件系统目录
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if renamed and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise

    return remote_plugin_view_from_installed(row)


async def uninstall(db: AsyncSession, name: str, *, remove_files: bool = True) -> bool:
    """卸载远程插件：删 installed_plugin 行 + 清理 Feature/AccountFeature 行。

    返回 ``True`` 表示真删了一行。``name`` 不存在时返回 ``False``，不抛异常。

    ``remove_files=False`` 时只改库、不删目录，供事务型调用方在 **commit 之后**
    再做文件系统清理，避免 DB 回滚后磁盘已空。
    """
    row = await db.get(InstalledPlugin, name)
    if row is None or row.source not in _REMOTE_INSTALL_SOURCES:
        return False

    # 清理 AccountFeature 行
    afs = (
        await db.execute(
            select(AccountFeature).where(AccountFeature.feature_key == name)
        )
    ).scalars().all()
    for af in afs:
        await db.delete(af)

    # 清理 Feature 行
    feat = (
        await db.execute(select(Feature).where(Feature.key == name))
    ).scalar_one_or_none()
    if feat is not None:
        await db.delete(feat)

    await db.delete(row)
    await db.flush()

    # 文件系统清理：默认立即删除；事务调用方可推迟到 commit 后
    if remove_files:
        try:
            target = _existing_plugin_dir(name)
            if target.exists():
                shutil.rmtree(target)
        except Exception:  # noqa: BLE001
            log.exception("卸载 %s 时删除目录失败", name)

    return True


async def set_enabled(
    db: AsyncSession, name: str, *, enabled: bool, bootstrap_accounts: bool = False
) -> RemotePluginView:
    """翻转 ``enabled`` 标志。``name`` 不存在抛 ``RemotePluginNotFound``。"""
    row = await db.get(InstalledPlugin, name)
    if row is None or row.source not in _REMOTE_INSTALL_SOURCES:
        raise RemotePluginNotFound("PLUGIN_NOT_FOUND", f"插件不存在: {name}")
    row.enabled = bool(enabled)
    if row.enabled and bootstrap_accounts:
        await _enable_for_all_accounts_if_unclaimed(db, name)
    await db.flush()
    return remote_plugin_view_from_installed(row)


async def enable(
    db: AsyncSession, name: str, *, bootstrap_accounts: bool = False
) -> RemotePluginView:
    """启用插件 = ``set_enabled(..., enabled=True)``。"""
    return await set_enabled(db, name, enabled=True, bootstrap_accounts=bootstrap_accounts)


async def disable(db: AsyncSession, name: str) -> RemotePluginView:
    """禁用插件 = ``set_enabled(..., enabled=False)``。"""
    return await set_enabled(db, name, enabled=False)


async def update(db: AsyncSession, name: str) -> RemotePluginView:
    """从远程仓库拉取最新版本（``git pull``）+ 重读 plugin.json + 写新版本号。

    注意：manifest.py 不会被执行，只解析 plugin.json。
    """
    row = await db.get(InstalledPlugin, name)
    if row is None or row.source not in _REMOTE_INSTALL_SOURCES:
        raise RemotePluginNotFound("PLUGIN_NOT_FOUND", f"插件不存在: {name}")

    target = Path(row.installed_path or _existing_plugin_dir(name))
    source_url = str(row.source_url or "")
    git_env = await _git_env_for_installed_source(db, source_url) if source_url else None
    restored_from_source = False
    if not target.exists():
        await _copy_plugin_from_source_url(
            name=name,
            source_url=source_url,
            target=target,
            replace_existing=False,
            git_env=git_env,
        )
        restored_from_source = True

    # git pull（带 timeout）。如果插件是从多插件仓库子目录复制安装的，
    # 安装目录没有 .git，此时临时 clone source_url 后按 plugin.json.name 定位子目录覆盖。
    if (target / ".git").exists():
        begin_plugin_update(target.parent, name)
        try:
            await _run_git("pull", "--ff-only", cwd=target, timeout=60.0, env=git_env)
        except Exception:
            clear_plugin_update(target.parent, name)
            raise
    elif not restored_from_source:
        await _copy_plugin_from_source_url(
            name=name,
            source_url=source_url,
            target=target,
            replace_existing=True,
            git_env=git_env,
        )

    meta = _read_plugin_metadata(target, fallback_name=name)
    _validate_runtime_plugin_shape(target, meta)
    lint_warnings = lint_plugin_metadata_files(target)
    old_source = row.source
    old_source_url = row.source_url
    old_enabled = row.enabled
    old_signature_ok = row.signature_ok
    old_trust_tier = row.trust_tier
    old_source_label = _source_label_for_installed(row)
    old_default_enabled = remote_plugin_view_from_installed(row).default_enabled
    feat = (
        await db.execute(select(Feature).where(Feature.key == name))
    ).scalar_one_or_none()
    if feat is not None:
        feat.display_name = meta.display_name or name
        feat.version = meta.version or feat.version
        feat.is_builtin = False
        feat.manifest = _merge_feature_manifest_preserving_global_config(feat.manifest, meta)
    updated_version = meta.version or row.version
    manifest_json = _with_remote_info(
        _manifest_json_from_remote_meta(meta),
        default_enabled=old_default_enabled,
        latest_version=updated_version,
        update_available=False,
        last_update_check_at=datetime.now(UTC),
        last_update_check_error=None,
    )
    row = await upsert_installed_plugin(
        db,
        key=name,
        source=old_source,
        source_url=old_source_url,
        installed_path=str(target),
        version=updated_version,
        manifest_json=manifest_json,
        enabled=old_enabled,
        signature_ok=old_signature_ok,
        trust_tier=old_trust_tier,
        source_label=old_source_label,
        last_install_error=None,
        lint_warnings=lint_warnings,
    )
    await db.flush()

    return remote_plugin_view_from_installed(row)


async def list_installed(db: AsyncSession) -> list[RemotePluginView]:
    """按 name 字典序列出所有远程插件。"""
    rows = (
        await db.execute(_installed_remote_query().order_by(InstalledPlugin.key))
    ).scalars().all()
    return [remote_plugin_view_from_installed(row) for row in rows]


async def get_by_name(db: AsyncSession, name: str) -> RemotePluginView | None:
    """按 name 查单个插件；不存在返回 None（不抛异常，调用方自决）。"""
    row = await db.get(InstalledPlugin, name)
    if row is None or row.source not in _REMOTE_INSTALL_SOURCES:
        return None
    return remote_plugin_view_from_installed(row)


__all__ = [
    "DuplicatePluginName",
    "GitOperationFailed",
    "InvalidPluginMetadata",
    "InvalidSourceUrl",
    "PluginMetadata",
    "PluginMetadataSchema",
    "RemotePluginError",
    "RemotePluginNotFound",
    "RemotePluginUpdateCheckSummary",
    "RemotePluginView",
    "auto_update_check_loop",
    "check_remote_plugin_update",
    "check_updates",
    "delete_installed_plugin_record",
    "disable",
    "enable",
    "get_by_name",
    "install",
    "list_installed",
    "remote_plugin_view_from_installed",
    "set_enabled",
    "set_installed_plugin_enabled",
    "trigger_reload",
    "uninstall",
    "update",
    "upsert_installed_plugin",
]
