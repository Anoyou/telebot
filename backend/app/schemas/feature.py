"""功能与功能矩阵 schema。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ..db.models.feature import Feature
    from ..db.models.plugin import InstalledPlugin


class FeatureInfo(BaseModel):
    key: str
    display_name: str
    is_builtin: bool
    source_type: str = "local"
    source_label: str | None = None
    orphan: bool = False
    signature_ok: bool | None = None
    version: str | None = None
    usage: str | None = None
    config_schema: dict[str, Any] | None = None
    config_actions: list[dict[str, Any]] = Field(default_factory=list)
    category: str = "utility"
    interaction_profile: str | None = None
    interaction_entries: list[dict[str, Any]] = Field(default_factory=list)
    event_subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    experimental: bool = False
    update_available: bool = False
    latest_version: str | None = None
    last_update_check_at: Any | None = None
    last_update_check_error: str | None = None
    lint_warnings: list[str] = []

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_feature(
        cls,
        f: Feature,
        remote_plugin: Any | None = None,
        plugin_install: Any | None = None,
        installed_plugin: InstalledPlugin | None = None,
    ) -> FeatureInfo:
        feature_manifest = getattr(f, "manifest", None) or {}
        installed_source = str(getattr(installed_plugin, "source", "") or "")
        installed_source_url = str(getattr(installed_plugin, "source_url", "") or "")
        installed_manifest = getattr(installed_plugin, "manifest_json", None) or {}
        manifest = dict(feature_manifest)
        if isinstance(installed_manifest, dict):
            for key in (
                "config_schema",
                "config_actions",
                "usage",
                "category",
                "interaction_profile",
                "interaction_entries",
                "event_subscriptions",
                "capabilities",
                "permissions",
                "experimental",
                "x-experimental",
            ):
                if key in installed_manifest:
                    manifest[key] = installed_manifest[key]
        source_url = str(getattr(remote_plugin, "source_url", "") or installed_source_url)
        source_type = (
            "remote"
            if installed_source in {"git", "repo"} or (remote_plugin is not None and not source_url.startswith("local://"))
            else "local"
        )
        source_label = manifest.get("source_label")
        if not source_label:
            if f.is_builtin:
                source_label = "core"
            elif installed_plugin is not None:
                source_label = getattr(installed_plugin, "source_label", None) or installed_source or "local"
            elif plugin_install is not None:
                source_label = str(getattr(plugin_install, "source", "") or "zip")
            elif remote_plugin is not None:
                source_label = "remote"
            else:
                source_label = "local-orphan" if manifest.get("x-orphan") else "local"
        config_schema = manifest.get("config_schema")
        raw_config_actions = manifest.get("config_actions")
        usage = str(manifest.get("usage") or "").strip() or None
        schema_meta = config_schema if isinstance(config_schema, dict) else {}
        if raw_config_actions is None and isinstance(installed_manifest, dict):
            raw_config_actions = installed_manifest.get("config_actions")
        if raw_config_actions is None:
            raw_config_actions = schema_meta.get("x-config-actions")
        config_actions = raw_config_actions if isinstance(raw_config_actions, list) else []
        category = str(manifest.get("category") or schema_meta.get("x-category") or "utility")
        if category not in {"interactive", "automation", "utility"}:
            category = "utility"
        interaction_profile = str(manifest.get("interaction_profile") or "").strip() or None
        raw_entries = manifest.get("interaction_entries")
        if raw_entries is None:
            raw_entries = schema_meta.get("x-interaction-entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        raw_event_subscriptions = manifest.get("event_subscriptions")
        event_subscriptions = raw_event_subscriptions if isinstance(raw_event_subscriptions, list) else []
        raw_capabilities = manifest.get("capabilities")
        capabilities = raw_capabilities if isinstance(raw_capabilities, dict) else {}
        raw_permissions = manifest.get("permissions")
        permissions = raw_permissions if isinstance(raw_permissions, list) else []
        raw_lint_warnings = (
            getattr(installed_plugin, "lint_warnings", None)
            if installed_plugin is not None
            else None
        )
        lint_warnings = raw_lint_warnings if isinstance(raw_lint_warnings, list) else []
        remote_info = (
            installed_manifest.get("_telepilot_remote", {})
            if isinstance(installed_manifest, dict)
            else {}
        )
        remote_info = remote_info if isinstance(remote_info, dict) else {}
        installed_version = None
        if isinstance(installed_manifest, dict):
            installed_version = str(installed_manifest.get("version") or "").strip() or None
        installed_version = installed_version or str(getattr(installed_plugin, "version", "") or "").strip() or None
        raw_display_name = (
            installed_manifest.get("display_name")
            if isinstance(installed_manifest, dict)
            else None
        )
        display_name = raw_display_name.strip() if isinstance(raw_display_name, str) else ""
        return cls(
            key=f.key,
            display_name=display_name or f.display_name,
            is_builtin=f.is_builtin,
            source_type=source_type,
            source_label=str(source_label),
            orphan=bool(feature_manifest.get("x-orphan")),
            signature_ok=getattr(
                installed_plugin,
                "signature_ok",
                getattr(plugin_install, "signature_ok", None),
            ),
            version=installed_version or f.version,
            usage=usage,
            config_schema=config_schema,
            config_actions=[item for item in config_actions if isinstance(item, dict)],
            category=category,
            interaction_profile=interaction_profile,
            interaction_entries=[item for item in entries if isinstance(item, dict)],
            event_subscriptions=[item for item in event_subscriptions if isinstance(item, dict)],
            capabilities=dict(capabilities),
            permissions=[str(item) for item in permissions if isinstance(item, str) and item.strip()],
            experimental=bool(
                manifest.get("x-experimental") or manifest.get("experimental")
            ),
            update_available=bool(
                remote_info.get("update_available", getattr(remote_plugin, "update_available", False))
            ),
            latest_version=remote_info.get("latest_version", getattr(remote_plugin, "latest_version", None)),
            last_update_check_at=remote_info.get(
                "last_update_check_at", getattr(remote_plugin, "last_update_check_at", None)
            ),
            last_update_check_error=remote_info.get(
                "last_update_check_error", getattr(remote_plugin, "last_update_check_error", None)
            ),
            lint_warnings=[item for item in lint_warnings if isinstance(item, str)],
        )


class AccountFeatureToggle(BaseModel):
    """启停某账号的某功能。"""
    enabled: bool
    config: dict[str, Any] | None = None


class AccountFeatureConfigUpdate(BaseModel):
    """仅更新账号级配置（不改变 enabled 状态）。"""
    config: dict[str, Any]


class AccountFeatureDirectPassthroughUpdate(BaseModel):
    """仅更新平台拥有的账号级裸直通开关。"""
    enabled: bool


class AccountFeatureItem(BaseModel):
    feature_key: str
    enabled: bool
    state: str
    last_error: str | None = None
    config: dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class FeatureMatrixCell(BaseModel):
    """功能矩阵的单元格状态。"""
    state: str  # active | failed | disabled


class FeatureMatrixRow(BaseModel):
    id: int
    name: str
    features: dict[str, str]  # feature_key -> state
    feature_enabled: dict[str, bool] = Field(default_factory=dict)  # feature_key -> account switch


class FeatureMatrixResponse(BaseModel):
    features: list[FeatureInfo]
    accounts: list[FeatureMatrixRow]


class PluginGlobalConfigResponse(BaseModel):
    """global config 响应。"""
    plugin_key: str
    config: dict[str, Any]  # 合并后的最终配置
    global_config: dict[str, Any] | None = None  # 仅 global 字段


class PluginGlobalConfigUpdate(BaseModel):
    """更新 global config 的请求体。"""
    config: dict[str, Any]


class ConfigValidationError(BaseModel):
    """JSON Schema 验证错误。"""
    field: str
    message: str


class ConfigValidationResponse(BaseModel):
    """JSON Schema 验证结果。"""
    valid: bool
    errors: list[ConfigValidationError] = []


class PluginConfigActionRequest(BaseModel):
    """插件配置页动作请求。"""

    input: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class PluginConfigActionResponse(BaseModel):
    """插件配置页动作响应。"""

    success: bool = True
    message: str | None = None
    toast: str | None = None
    config_patch: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class PluginConfigActionJobLogItem(BaseModel):
    """插件配置动作后台任务的一条过程日志。"""

    id: int
    ts: Any
    level: str
    message: str
    detail: dict[str, Any] | None = None


class PluginConfigActionJobResponse(BaseModel):
    """插件配置动作后台任务状态。"""

    job_id: str
    account_id: int
    plugin_key: str
    action_key: str
    status: str
    message: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    config_patch: dict[str, Any] = Field(default_factory=dict)
    created_at: Any | None = None
    started_at: Any | None = None
    ended_at: Any | None = None
    updated_at: Any | None = None
    logs: list[PluginConfigActionJobLogItem] = Field(default_factory=list)
