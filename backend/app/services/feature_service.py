"""功能（feature/plugin）业务层：feature 表 seed、account_feature 启停 + 矩阵查询。

API 层调本服务而不直接读写 ORM，便于以后引入更复杂的状态机或缓存。
所有需要"通知 worker 热重载"的写操作都会发一条 IPC ``CMD_RELOAD_CONFIG``，
异常吞掉避免影响 DB 事务结果。

热重载设计：
- ``seed_builtin_features`` 每次调用时强制刷新 ``BUILTIN_FEATURES``（动态扫描 builtin 目录），
  保证新增 builtin 插件目录后，主进程调用任意 API 都能立即把新行写入 ``feature`` 表。
- worker 端 ``reload_account_config`` 也会刷新注册表后重激活，两侧相互独立。

Global Config 设计：
- global config 存储在 ``plugin_global_config`` 表中，为所有账号共享。
- 兼容期内，如果新表缺行，会回退读取 ``Feature.manifest["global_config"]``。
- 配置合并顺序：schema defaults < global config < account config。
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft7Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..db.models.account import Account
from ..db.models.feature import (
    BUILTIN_FEATURES,
    FEATURE_STATE_ACTIVE,
    FEATURE_STATE_DISABLED,
    AccountFeature,
    Feature,
)
from ..db.models.plugin import InstalledPlugin
from ..db.models.plugin_global_config import PluginGlobalConfig
from ..db.models.rule import Rule
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..schemas.feature import (
    ConfigValidationError,
    ConfigValidationResponse,
    FeatureInfo,
)
from ..worker.ipc import CMD_RELOAD_CONFIG, publish_cmd_with_ack
from .account_bot_service import normalize_interaction_entry_manifest

log = logging.getLogger(__name__)
_WARNED_ORPHAN_PLUGIN_KEYS: set[str] = set()
_OPTIONAL_OFFICIAL_PLUGIN_KEYS: frozenset[str] = frozenset(
    {
        "auto_reply",
        "autorepeat",
        "chatgpt_image",
        "codex_image",
        "game24",
        "math10",
    }
)


def _feature_manifest_from_installed_plugin(
    installed_plugin: InstalledPlugin,
) -> dict[str, Any] | None:
    """从已安装插件记录生成 feature 索引 manifest。

    ``installed_plugin`` 是插件安装与更新后的权威元数据源；``feature`` 表只是
    账号矩阵和旧配置页使用的索引，不能反过来覆盖已安装插件的版本或契约。
    """

    manifest_json = installed_plugin.manifest_json if isinstance(installed_plugin.manifest_json, dict) else {}
    manifest: dict[str, Any] = {}
    cfg_schema = manifest_json.get("config_schema")
    if isinstance(cfg_schema, dict):
        manifest["config_schema"] = cfg_schema
    config_actions = manifest_json.get("config_actions")
    if isinstance(config_actions, list):
        manifest["config_actions"] = [item for item in config_actions if isinstance(item, dict)]
    agent_tools = manifest_json.get("agent_tools")
    if isinstance(agent_tools, list):
        manifest["agent_tools"] = [item for item in agent_tools if isinstance(item, dict)]
    usage = str(manifest_json.get("usage") or "").strip()
    if usage:
        manifest["usage"] = usage
    category = str(manifest_json.get("category") or "").strip()
    if category:
        manifest["category"] = category
    interaction_profile = str(manifest_json.get("interaction_profile") or "").strip()
    if interaction_profile:
        manifest["interaction_profile"] = interaction_profile
    entries = manifest_json.get("interaction_entries")
    if isinstance(entries, list):
        manifest["interaction_entries"] = [item for item in entries if isinstance(item, dict)]
    subscriptions = manifest_json.get("event_subscriptions")
    if isinstance(subscriptions, list):
        manifest["event_subscriptions"] = [item for item in subscriptions if isinstance(item, dict)]
    capabilities = manifest_json.get("capabilities")
    if isinstance(capabilities, dict):
        manifest["capabilities"] = dict(capabilities)
    permissions = manifest_json.get("permissions")
    if isinstance(permissions, list):
        manifest["permissions"] = list(permissions)
    if manifest_json.get("x-experimental") or manifest_json.get("experimental"):
        manifest["x-experimental"] = True
    for version_key in ("min_telepilot_version", "min_telebot_version"):
        version_value = str(manifest_json.get(version_key) or "").strip()
        if version_value:
            manifest[version_key] = version_value
    source_label = getattr(installed_plugin, "source_label", None) or getattr(installed_plugin, "source", None)
    if source_label:
        manifest["source_label"] = str(source_label)
    return manifest or None


def _merge_feature_manifest_preserving_global_config(
    current: dict[str, Any] | None,
    next_manifest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """刷新索引 manifest，同时保留一轮旧版 global_config 兼容字段。"""

    if current and "global_config" in current:
        next_manifest = dict(next_manifest or {})
        next_manifest["global_config"] = current["global_config"]
    return next_manifest


# ─────────────────────────────────────────────────────
# Feature 表 seed
# ─────────────────────────────────────────────────────
async def seed_builtin_features(db: AsyncSession) -> int:
    """确保内置和本地 installed feature 行存在；返回新增条数。

    每次调用都先强制刷新 ``BUILTIN_FEATURES`` 字典（重新扫描 builtin 目录），
    并扫描 ``plugins/installed`` 里的 ``plugin.json``。这样本地已安装的远程模块
    不需要再次走安装流程，也能在模块中心出现。

    幂等：已存在的行只校正 display_name / is_builtin，不会触发额外 INSERT；
    新行才增加计数并 commit。
    """
    # 强制刷新动态字典，确保扫描到最新文件系统状态
    BUILTIN_FEATURES.refresh()

    rows = (await db.execute(select(Feature))).scalars().all()
    existing: dict[str, Feature] = {f.key: f for f in rows}
    migrated_count, migrated_changed = await _migrate_optional_builtin_features(db, existing)
    added = 0
    changed_existing = migrated_changed
    for key, name in BUILTIN_FEATURES.items():
        # 尝试从 manifest 读取 config_schema 和 version
        cfg_schema = None
        ver = None
        experimental = False
        usage = None
        category = "utility"
        interaction_profile = None
        interaction_entries: list[dict[str, Any]] = []
        event_subscriptions: list[dict[str, Any]] = []
        capabilities: dict[str, Any] | None = None
        m = BUILTIN_FEATURES.manifest_for(key)
        if m is not None:
            cfg_schema = getattr(m, "config_schema", None)
            ver = getattr(m, "version", None)
            experimental = bool(getattr(m, "experimental", False))
            usage = str(getattr(m, "usage", None) or "").strip() or None
            category = str(getattr(m, "category", None) or "utility")
            interaction_profile = str(getattr(m, "interaction_profile", None) or "").strip() or None
            raw_subscriptions = getattr(m, "event_subscriptions", None) or []
            if isinstance(raw_subscriptions, list):
                event_subscriptions = [item for item in raw_subscriptions if isinstance(item, dict)]
            raw_capabilities = getattr(m, "capabilities", None)
            capabilities = dict(raw_capabilities) if isinstance(raw_capabilities, dict) else None
            raw_entries = getattr(m, "interaction_entries", None) or []
            if isinstance(raw_entries, list):
                interaction_entries = [
                    entry
                    for item in raw_entries
                    if (entry := normalize_interaction_entry_manifest(item)) is not None
                ]
        if category not in {"interactive", "automation", "utility"}:
            category = "utility"

        if key in existing:
            f = existing[key]
            changed = False
            if f.display_name != name:
                f.display_name = name
                changed = True
            if not f.is_builtin:
                f.is_builtin = True
                changed = True
            if ver and f.version != ver:
                f.version = ver
                changed = True
            if (
                cfg_schema
                or experimental
                or usage
                or category
                or interaction_entries
                or event_subscriptions
                or capabilities
                or interaction_profile
            ):
                manifest = dict(f.manifest or {})
                if manifest.get("config_schema") != cfg_schema:
                    manifest["config_schema"] = cfg_schema
                    changed = True
                if manifest.get("usage") != usage:
                    if usage:
                        manifest["usage"] = usage
                    else:
                        manifest.pop("usage", None)
                    changed = True
                if manifest.get("category") != category:
                    manifest["category"] = category
                    changed = True
                if manifest.get("interaction_profile") != interaction_profile:
                    manifest["interaction_profile"] = interaction_profile
                    changed = True
                if manifest.get("interaction_entries") != interaction_entries:
                    manifest["interaction_entries"] = interaction_entries
                    changed = True
                if manifest.get("event_subscriptions") != event_subscriptions:
                    manifest["event_subscriptions"] = event_subscriptions
                    changed = True
                if manifest.get("capabilities") != capabilities:
                    manifest["capabilities"] = capabilities
                    changed = True
                if manifest.get("x-experimental") != experimental:
                    manifest["x-experimental"] = experimental
                    changed = True
                f.manifest = manifest
            if changed:
                changed_existing = True
                await db.flush()
            continue
        manifest_data: dict[str, Any] | None = None
        if cfg_schema or experimental or usage or category or interaction_entries or event_subscriptions or capabilities or interaction_profile:
            manifest_data = {}
            if cfg_schema:
                manifest_data["config_schema"] = cfg_schema
            if usage:
                manifest_data["usage"] = usage
            manifest_data["category"] = category
            if interaction_profile:
                manifest_data["interaction_profile"] = interaction_profile
            if interaction_entries:
                manifest_data["interaction_entries"] = interaction_entries
            if event_subscriptions:
                manifest_data["event_subscriptions"] = event_subscriptions
            if capabilities:
                manifest_data["capabilities"] = capabilities
            manifest_data["x-experimental"] = experimental
        db.add(Feature(key=key, display_name=name, is_builtin=True, version=ver, manifest=manifest_data))
        added += 1
    installed_added, installed_changed = await _seed_local_installed_features(db, existing)
    added += migrated_count + installed_added
    changed_existing = changed_existing or installed_changed
    if added:
        await db.commit()
    elif changed_existing:
        await db.commit()
    return added


async def _migrate_optional_builtin_features(
    db: AsyncSession,
    existing: dict[str, Feature],
) -> tuple[int, bool]:
    """把历史 builtin 可选插件收敛成插件库 installed 插件。

    0.35 起这些插件不再作为 builtin 自动出现。若旧数据库里已经有账号开关、
    规则引用、全局配置或交互规则引用，则自动登记为插件库已安装包，保证旧配置
    继续可用；若从未使用，则删除旧 feature 行，避免插件中心继续展示。
    """

    keys = sorted(key for key in _OPTIONAL_OFFICIAL_PLUGIN_KEYS if key in existing)
    if not keys:
        return 0, False

    try:
        from ..db.models.plugin import (
            PLUGIN_SOURCE_REPO,
            PLUGIN_TRUST_COMMUNITY,
        )
        from . import plugin_repo_service, remote_plugin_service
        from .remote_plugin_service import upsert_installed_plugin
    except Exception:  # noqa: BLE001
        log.warning("加载官方插件迁移工具失败，跳过本轮收敛", exc_info=True)
        return 0, False

    account_feature_rows = (
        await db.execute(
            select(AccountFeature).where(AccountFeature.feature_key.in_(keys))
        )
    ).scalars().all()
    rule_rows = (
        await db.execute(select(Rule).where(Rule.feature_key.in_(keys)))
    ).scalars().all()
    global_config_rows = (
        await db.execute(
            select(PluginGlobalConfig).where(PluginGlobalConfig.plugin_key.in_(keys))
        )
    ).scalars().all()
    interaction_settings = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.key.like("account_bot_transfer_notice:%"))
        )
    ).scalars().all()
    used_keys = {row.feature_key for row in account_feature_rows}
    used_keys.update(row.feature_key for row in rule_rows)
    used_keys.update(row.plugin_key for row in global_config_rows)
    for row in rule_rows:
        cfg = row.config if isinstance(row.config, dict) else {}
        module_key = str(cfg.get("module_key") or "").strip()
        if module_key in _OPTIONAL_OFFICIAL_PLUGIN_KEYS:
            used_keys.add(module_key)
    for setting in interaction_settings:
        value = setting.value if isinstance(setting.value, dict) else {}
        module_key = str(value.get("module_key") or "").strip()
        if module_key in _OPTIONAL_OFFICIAL_PLUGIN_KEYS:
            used_keys.add(module_key)
        raw_rules = value.get("rules")
        if isinstance(raw_rules, list):
            for item in raw_rules:
                if not isinstance(item, dict):
                    continue
                module_key = str(item.get("module_key") or "").strip()
                if module_key in _OPTIONAL_OFFICIAL_PLUGIN_KEYS:
                    used_keys.add(module_key)

    added = 0
    changed = False
    for key in keys:
        feature = existing[key]
        if key not in used_keys:
            if bool(feature.is_builtin):
                await db.delete(feature)
                existing.pop(key, None)
                changed = True
            continue
        try:
            official_source = await plugin_repo_service._find_official_plugin_source(key)
        except Exception:  # noqa: BLE001
            log.warning("历史 builtin 插件 %s 已被使用，但读取推荐插件源失败", key, exc_info=True)
            continue
        if official_source is None:
            log.warning(
                "历史 builtin 插件 %s 已被使用，但 Core 已不再随包携带源码；请在安装插件页从推荐插件源安装该插件",
                key,
            )
            continue
        official_source_url = official_source.source_url

        target = plugin_repo_service._plugin_dir(key)
        installed = await db.get(InstalledPlugin, key)
        if not target.exists():
            try:
                staging = target.parent / f"{target.name}.installing"
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                official_source_url = await plugin_repo_service.copy_official_plugin_source(
                    key,
                    staging,
                )
                staging.rename(target)
            except Exception:  # noqa: BLE001
                log.warning("复制插件库插件 %s 到 installed 失败", key, exc_info=True)
                continue

        try:
            meta = plugin_repo_service._read_plugin_metadata(target, fallback_name=key)
            base_manifest = plugin_repo_service._manifest_json_from_remote_meta(meta)
            base_manifest["source_url"] = official_source_url
            current_info = remote_plugin_service._remote_info_from_manifest(
                installed.manifest_json if installed is not None else None
            )
            current_revision = remote_plugin_service._remote_info_datetime(
                current_info.get("runtime_revision_at")
            )
            manifest_json = remote_plugin_service._with_remote_info(
                base_manifest,
                default_enabled=bool(current_info.get("default_enabled", False)),
                latest_version=official_source.meta.version,
                update_available=(
                    plugin_repo_service._version_tuple(official_source.meta.version)
                    > plugin_repo_service._version_tuple(meta.version)
                ),
                last_update_check_at=remote_plugin_service._remote_info_datetime(
                    current_info.get("last_update_check_at")
                ),
                last_update_check_error=current_info.get("last_update_check_error"),
                runtime_revision_at=current_revision or datetime.now(UTC),
            )
            feature_manifest = plugin_repo_service._feature_manifest_from_manifest_json(manifest_json)
            lint_warnings = plugin_repo_service.lint_plugin_metadata_files(target)
        except Exception:  # noqa: BLE001
            log.warning("读取已安装插件 %s 元数据失败", key, exc_info=True)
            continue

        if feature.is_builtin:
            feature.is_builtin = False
            changed = True
        display_name = str(manifest_json.get("display_name") or meta.display_name or key)
        version = str(manifest_json.get("version") or meta.version)
        if feature.display_name != display_name:
            feature.display_name = display_name
            changed = True
        if feature.version != version:
            feature.version = version
            changed = True
        if (feature.manifest or {}) != (feature_manifest or {}):
            feature.manifest = feature_manifest
            changed = True

        was_enabled = bool(getattr(installed, "enabled", False)) if installed is not None else bool(
            any(row.feature_key == key and row.enabled for row in account_feature_rows)
        )
        await upsert_installed_plugin(
            db,
            key=key,
            source=PLUGIN_SOURCE_REPO,
            source_url=official_source.source_url,
            installed_path=str(target),
            version=version,
            manifest_json=manifest_json,
            enabled=was_enabled,
            signature_ok=None,
            trust_tier=PLUGIN_TRUST_COMMUNITY,
            source_label="Plugin Repo",
            last_install_error=None,
            lint_warnings=lint_warnings,
        )
        added += 1 if installed is None else 0
        await db.flush()
    return added, changed


async def _seed_local_installed_features(
    db: AsyncSession,
    existing: dict[str, Feature],
) -> tuple[int, bool]:
    """把本地 ``plugins/installed`` 目录中已有 plugin.json 同步到 feature 表。

    这里只登记元数据，不自动启用插件；运行期是否加载仍由账号 feature 开关和
    loader 的安全策略决定。
    """

    installed_plugin_rows = (await db.execute(select(InstalledPlugin))).scalars().all()
    installed_plugin_by_key = {str(row.key): row for row in installed_plugin_rows}

    added = 0
    changed = False
    for key, installed_plugin in sorted(installed_plugin_by_key.items()):
        if not key or "/" in key or "\\" in key:
            continue
        installed_source = str(getattr(installed_plugin, "source", "") or "")
        if installed_source == "builtin":
            continue
        manifest_json = installed_plugin.manifest_json if isinstance(installed_plugin.manifest_json, dict) else {}
        display_name = str(manifest_json.get("display_name") or key)
        version = str(manifest_json.get("version") or getattr(installed_plugin, "version", "") or "") or None
        manifest_data = _feature_manifest_from_installed_plugin(installed_plugin)

        if key in existing:
            row = existing[key]
            row_changed = False
            if row.is_builtin:
                row.is_builtin = False
                row_changed = True
            if row.display_name != display_name:
                row.display_name = display_name
                row_changed = True
            if version and row.version != version:
                row.version = version
                row_changed = True
            next_manifest = _merge_feature_manifest_preserving_global_config(row.manifest, manifest_data)
            if (row.manifest or None) != (next_manifest or None):
                row.manifest = next_manifest
                flag_modified(row, "manifest")
                row_changed = True
            if row_changed:
                await db.flush()
                changed = True
            continue

        row = Feature(
            key=key,
            display_name=display_name,
            is_builtin=False,
            version=version,
            manifest=manifest_data,
        )
        db.add(row)
        existing[key] = row
        added += 1

    try:
        from ..settings import settings

        root = settings.plugins_installed_path
    except Exception:  # noqa: BLE001
        return added, changed
    if not root.exists():
        return added, changed

    for plugin_json in sorted(root.glob("*/plugin.json")):
        try:
            meta = json.loads(plugin_json.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.warning("读取本地 installed 模块元数据失败: %s", plugin_json, exc_info=True)
            continue
        key = str(meta.get("name") or plugin_json.parent.name).strip()
        if not key or "/" in key or "\\" in key:
            continue
        installed_plugin = installed_plugin_by_key.get(key)
        is_orphan = installed_plugin is None
        if installed_plugin is not None and isinstance(installed_plugin.manifest_json, dict):
            meta = dict(installed_plugin.manifest_json)
        display_name = str(meta.get("display_name") or key)
        version = str(meta.get("version") or "") or None
        cfg_schema = meta.get("config_schema")
        schema_meta = cfg_schema if isinstance(cfg_schema, dict) else {}
        category = str(meta.get("category") or schema_meta.get("x-category") or "utility")
        if category not in {"interactive", "automation", "utility"}:
            category = "utility"
        interaction_profile = str(meta.get("interaction_profile") or "").strip() or None
        raw_entries = meta.get("interaction_entries")
        if raw_entries is None and isinstance(cfg_schema, dict):
            raw_entries = cfg_schema.get("x-interaction-entries")
        interaction_entries = [
            entry
            for item in raw_entries
            if (entry := normalize_interaction_entry_manifest(item)) is not None
        ] if isinstance(raw_entries, list) else []
        raw_subscriptions = meta.get("event_subscriptions")
        event_subscriptions = [item for item in raw_subscriptions if isinstance(item, dict)] if isinstance(raw_subscriptions, list) else []
        raw_capabilities = meta.get("capabilities")
        capabilities = dict(raw_capabilities) if isinstance(raw_capabilities, dict) else None
        tags = meta.get("tags") or []
        experimental = bool(meta.get("experimental")) or "experimental" in tags
        if is_orphan:
            row = existing.get(key)
            if row is not None and not row.is_builtin:
                try:
                    await db.delete(row)
                    existing.pop(key, None)
                    changed = True
                except Exception:  # noqa: BLE001
                    log.warning("清理孤立 installed 模块 feature 行失败: %s", key, exc_info=True)
            if key not in _WARNED_ORPHAN_PLUGIN_KEYS:
                _WARNED_ORPHAN_PLUGIN_KEYS.add(key)
                log.warning(
                    "跳过孤立 installed 模块 %s：磁盘存在 plugin.json，但没有安装记录；"
                    "该目录不会被加载，请在确认无用后手动清理或重新安装插件",
                    key,
                )
            else:
                log.debug("继续跳过孤立 installed 模块 %s", key)
            continue
        manifest: dict[str, Any] = {}
        if cfg_schema:
            manifest["config_schema"] = cfg_schema
        manifest["category"] = category
        if interaction_profile:
            manifest["interaction_profile"] = interaction_profile
        if interaction_entries:
            manifest["interaction_entries"] = interaction_entries
        if event_subscriptions:
            manifest["event_subscriptions"] = event_subscriptions
        if capabilities:
            manifest["capabilities"] = capabilities
        if experimental:
            manifest["x-experimental"] = True
        if meta.get("permissions"):
            manifest["permissions"] = list(meta.get("permissions") or [])
        source_label = getattr(installed_plugin, "source_label", None) or getattr(installed_plugin, "source", None)
        if source_label:
            manifest["source_label"] = str(source_label)
        manifest_data = manifest or None

        if key in existing:
            row = existing[key]
            if row.is_builtin:
                # 本地 installed 目录不能覆盖 builtin 元数据；同名目录交给安装流程拒绝/处理。
                continue
            row_changed = False
            if row.display_name != display_name:
                row.display_name = display_name
                row_changed = True
            if version and row.version != version:
                row.version = version
                row_changed = True
            current_manifest = dict(row.manifest or {})
            if manifest_data:
                next_manifest = dict(manifest_data)
                if "global_config" in current_manifest:
                    next_manifest["global_config"] = current_manifest["global_config"]
                if current_manifest != next_manifest:
                    row.manifest = next_manifest
                    flag_modified(row, "manifest")
                    row_changed = True
            if row_changed:
                await db.flush()
                changed = True
            continue

        row = Feature(
            key=key,
            display_name=display_name,
            is_builtin=False,
            version=version,
            manifest=manifest_data,
        )
        db.add(row)
        existing[key] = row
        added += 1
    return added, changed


# ─────────────────────────────────────────────────────
# 列表查询
# ─────────────────────────────────────────────────────
async def list_features(db: AsyncSession) -> list[Feature]:
    """列出所有已登记的 feature；首次调用时会自动 seed 内置行。"""
    await seed_builtin_features(db)
    rows = (
        await db.execute(select(Feature).order_by(Feature.is_builtin.desc(), Feature.key.asc()))
    ).scalars().all()
    return list(rows)


async def get_account_features(db: AsyncSession, aid: int) -> list[AccountFeature]:
    """返回某账号已登记的 [account_feature] 行（未启用过的不在结果里）。"""
    rows = (
        await db.execute(
            select(AccountFeature)
            .where(AccountFeature.account_id == aid)
            .order_by(AccountFeature.feature_key.asc())
        )
    ).scalars().all()
    return list(rows)


# ─────────────────────────────────────────────────────
# upsert：启停 feature + 修改 config
# ─────────────────────────────────────────────────────
async def set_account_feature(
    db: AsyncSession,
    aid: int,
    key: str,
    enabled: bool,
    config: dict[str, Any] | None = None,
    *,
    notify: bool = True,
    commit: bool = True,
) -> AccountFeature:
    """对 [账号 × feature] 做 upsert。

    - 若该 account_feature 不存在 → 新建；state 默认 ``disabled``。
    - 若 enabled 由 False→True：state 仍记为 disabled，等 worker 实际激活后再改成 active。
    - 若 enabled 由 True→False：直接把 state 改成 ``disabled``。
    - ``config`` 不为 None 时整体覆盖（便于前端"保存即覆盖"语义）。

    完成后视 ``notify`` 决定是否发 IPC 通知 worker reload。
    ``commit=False`` 时仅 flush，由调用方负责事务边界（复合保存用）。
    """
    af = (
        await db.execute(
            select(AccountFeature).where(
                AccountFeature.account_id == aid,
                AccountFeature.feature_key == key,
            )
        )
    ).scalar_one_or_none()
    from .plugin_config_secrets import encrypt_config_secrets

    feature_row = await db.get(Feature, key)
    schema = None
    if feature_row is not None and isinstance(feature_row.manifest, dict):
        raw_schema = feature_row.manifest.get("config_schema")
        schema = raw_schema if isinstance(raw_schema, dict) else None

    if af is None:
        af = AccountFeature(
            account_id=aid,
            feature_key=key,
            enabled=enabled,
            config=encrypt_config_secrets(dict(config or {}), schema=schema),
            state=FEATURE_STATE_DISABLED,
        )
        db.add(af)
    else:
        af.enabled = enabled
        if config is not None:
            af.config = encrypt_config_secrets(dict(config), schema=schema)
        if not enabled:
            # 立刻把状态置 disabled；激活由 worker 反向写
            af.state = FEATURE_STATE_DISABLED
            af.last_error = None
    if commit:
        await db.commit()
        await db.refresh(af)
    else:
        await db.flush()
        await db.refresh(af)
    if notify:
        await _notify_reload(aid)
    return af


# ─────────────────────────────────────────────────────
# 矩阵：行=账号、列=feature
# ─────────────────────────────────────────────────────
async def feature_matrix(db: AsyncSession) -> dict[str, Any]:
    """构造功能矩阵数据：

    返回结构::

        {
          "features": [{key, display_name, is_builtin, version}, ...],
          "accounts": [
            {"id": 1, "name": "...", "features": {"auto_reply": "active", "forward": "disabled", ...}}
          ]
        }
    """
    # 1) 保证内置 feature 行齐全
    features = await list_features(db)
    installed_plugin_rows = (await db.execute(select(InstalledPlugin))).scalars().all()
    installed_plugin_by_key = {row.key: row for row in installed_plugin_rows}

    # 2) 拿全部账号 + 全部 account_feature
    accounts = (
        await db.execute(select(Account).order_by(Account.id.asc()))
    ).scalars().all()
    afs = (await db.execute(select(AccountFeature))).scalars().all()
    by_aid: dict[int, dict[str, str]] = {}
    enabled_by_aid: dict[int, dict[str, bool]] = {}
    for af in afs:
        enabled = bool(af.enabled)
        # 默认按 state 显示；若 enabled=False 则强制 disabled，避免脏状态混淆
        if not enabled:
            cell = FEATURE_STATE_DISABLED
        else:
            cell = af.state or FEATURE_STATE_ACTIVE
        by_aid.setdefault(af.account_id, {})[af.feature_key] = cell
        enabled_by_aid.setdefault(af.account_id, {})[af.feature_key] = enabled

    rows: list[dict[str, Any]] = []
    for acc in accounts:
        cells: dict[str, str] = {}
        enabled_cells: dict[str, bool] = {}
        existing = by_aid.get(acc.id, {})
        existing_enabled = enabled_by_aid.get(acc.id, {})
        for f in features:
            cells[f.key] = existing.get(f.key, FEATURE_STATE_DISABLED)
            enabled_cells[f.key] = existing_enabled.get(f.key, False)
        rows.append(
            {
                "id": acc.id,
                "name": acc.display_name or acc.phone,
                "features": cells,
                "feature_enabled": enabled_cells,
            }
        )

    return {
        "features": [
            FeatureInfo.from_feature(
                f,
                None,
                None,
                installed_plugin_by_key.get(f.key),
            ).model_dump()
            for f in features
        ],
        "accounts": rows,
    }


# ─────────────────────────────────────────────────────
# 批量启停（plugins/install / uninstall 用）
# ─────────────────────────────────────────────────────
async def bulk_set_enabled(
    db: AsyncSession,
    aids: Iterable[int],
    key: str,
    enabled: bool,
) -> int:
    """对一组账号统一启 / 停某 feature。返回受影响条数。"""
    n = 0
    for aid in aids:
        await set_account_feature(db, aid, key, enabled, config=None, notify=True)
        n += 1
    return n


# ─────────────────────────────────────────────────────
# IPC：通知 worker reload
# ─────────────────────────────────────────────────────
async def _notify_reload(account_id: int) -> None:
    """对指定 worker 发 ``CMD_RELOAD_CONFIG``；redis 不可用时静默。"""
    try:
        redis = get_redis()
        ok = await publish_cmd_with_ack(redis, account_id, CMD_RELOAD_CONFIG)
        if not ok:
            log.debug("worker reload_config 未确认 account=%s，将由周期 reconcile 收敛", account_id)
    except Exception:  # noqa: BLE001
        log.debug("通知 worker reload 失败 account=%s", account_id, exc_info=True)


# ─────────────────────────────────────────────────────
# Global Config
# ─────────────────────────────────────────────────────
async def _read_plugin_global_config(
    db: AsyncSession,
    plugin_key: str,
    feature: Feature | None = None,
) -> dict[str, Any]:
    """读取插件全局配置；新表缺行时回退旧 manifest 字段。"""

    row = await db.get(PluginGlobalConfig, plugin_key)
    if row is not None:
        return dict(row.config or {})

    if feature is None:
        feature = await db.get(Feature, plugin_key)
    if feature is None:
        return {}
    manifest = feature.manifest or {}
    legacy_config = manifest.get("global_config", {})
    return dict(legacy_config) if isinstance(legacy_config, dict) else {}


async def get_plugin_global_config(db: AsyncSession, plugin_key: str) -> dict[str, Any]:
    """获取插件的 global config。

    优先读取 plugin_global_config；兼容期内新表缺行时回退旧 manifest 字段。
    """
    await seed_builtin_features(db)
    feature = await db.get(Feature, plugin_key)
    if feature is None:
        return {}
    return await _read_plugin_global_config(db, plugin_key, feature)


async def set_plugin_global_config(
    db: AsyncSession,
    plugin_key: str,
    config: dict[str, Any],
    *,
    notify: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """设置插件的 global config。

    - 验证 config 是否符合 config_schema（如果有）。
    - 保存到 plugin_global_config。
    - 清理 Feature.manifest["global_config"] 旧字段，避免后续数据漂移。
    - 通知所有已启用该插件的账号的 worker reload。

    返回验证后的 config。
    """
    await seed_builtin_features(db)
    feature = await db.get(Feature, plugin_key)
    if feature is None:
        raise ValueError(f"Plugin not found: {plugin_key}")

    # 验证 config_schema
    config_schema = (feature.manifest or {}).get("config_schema")
    if config_schema:
        validation = validate_config_against_schema(
            config,
            config_schema_for_scope(config_schema, "global"),
        )
        if not validation.valid:
            error_msgs = [f"{e.field}: {e.message}" for e in validation.errors]
            raise ValueError(f"Config validation failed: {'; '.join(error_msgs)}")

    from .plugin_config_secrets import encrypt_config_secrets

    # 提取 global 字段（level === "global" 的字段）
    schema = config_schema if isinstance(config_schema, dict) else None
    if config_schema and "properties" in config_schema:
        global_fields = {
            k for k, v in config_schema["properties"].items()
            if isinstance(v, dict) and v.get("level") == "global"
        }
        global_config = {k: v for k, v in config.items() if k in global_fields}
    else:
        # 如果没有 level 标记，全部视为 account config
        global_config = {}
    global_config = encrypt_config_secrets(global_config, schema=schema)

    # 写入新表
    row = await db.get(PluginGlobalConfig, plugin_key)
    if row is None:
        db.add(PluginGlobalConfig(plugin_key=plugin_key, config=global_config))
    else:
        row.config = global_config

    # 清理旧 manifest 字段
    manifest = dict(feature.manifest or {})
    if "global_config" in manifest:
        manifest.pop("global_config", None)
        feature.manifest = manifest
        flag_modified(feature, "manifest")
    if commit:
        await db.commit()
    else:
        await db.flush()

    # 通知所有使用该插件的账号的 worker reload
    if notify:
        await _notify_all_accounts_using_feature(db, plugin_key)

    return global_config


async def _notify_all_accounts_using_feature(db: AsyncSession, plugin_key: str) -> None:
    """通知所有已启用指定插件的账号的 worker reload。"""
    rows = (
        await db.execute(
            select(AccountFeature).where(
                AccountFeature.feature_key == plugin_key,
                AccountFeature.enabled == True,  # noqa: E712
            )
        )
    ).scalars().all()
    for af in rows:
        await _notify_reload(af.account_id)


async def get_effective_plugin_config(
    db: AsyncSession,
    aid: int,
    plugin_key: str,
) -> dict[str, Any]:
    """获取某账号某插件的最终生效配置。

    合并顺序：schema defaults < global config < account config

    - 如果 Feature 没有 config_schema，返回 AccountFeature.config。
    - 如果 AccountFeature 不存在，返回 global config 或 schema defaults。
    """
    await seed_builtin_features(db)
    feature = await db.get(Feature, plugin_key)
    if feature is None:
        return {}

    config_schema = (feature.manifest or {}).get("config_schema")
    global_config = await _read_plugin_global_config(db, plugin_key, feature)

    # 获取账号配置
    af = (
        await db.execute(
            select(AccountFeature).where(
                AccountFeature.account_id == aid,
                AccountFeature.feature_key == plugin_key,
            )
        )
    ).scalar_one_or_none()
    account_config = dict(af.config) if af else {}

    # 提取 schema defaults
    defaults: dict[str, Any] = {}
    if config_schema and "properties" in config_schema:
        for prop_name, prop_def in config_schema["properties"].items():
            if isinstance(prop_def, dict) and "default" in prop_def:
                defaults[prop_name] = prop_def["default"]

    # 合并：defaults < global < account
    result = {**defaults}
    result.update(global_config)
    result.update(account_config)
    return result


def validate_config_against_schema(
    config: dict[str, Any],
    config_schema: dict[str, Any],
) -> ConfigValidationResponse:
    """验证配置是否符合 JSON Schema。

    返回验证结果，包含错误列表。
    """
    try:
        validator = Draft7Validator(config_schema)
        errors: list[ConfigValidationError] = []
        for error in validator.iter_errors(config):
            path = ".".join(str(p) for p in error.path) if error.path else "root"
            if error.validator == "required" and error.validator_value:
                missing = [k for k in error.validator_value if isinstance(k, str) and k not in config]
                if missing:
                    path = missing[0]
            errors.append(ConfigValidationError(
                field=path,
                message=error.message,
            ))
        return ConfigValidationResponse(
            valid=len(errors) == 0,
            errors=errors,
        )
    except Exception as e:
        return ConfigValidationResponse(
            valid=False,
            errors=[ConfigValidationError(field="schema", message=str(e))],
        )


def apply_required_config_defaults(
    config: dict[str, Any],
    config_schema: dict[str, Any],
) -> dict[str, Any]:
    """补齐当前配置 scope 内带默认值的必填字段。

    插件配置页会用 schema defaults 初始化表单；交互规则页只同步入口额外参数时，
    可能会提交一个局部 config。这里在服务端补齐 ``command`` 这类有默认值的
    required 字段，避免把局部保存误判成配置缺失；真正没有 default 的必填项
    仍然交给 JSON Schema 正常报错。
    """

    if not isinstance(config_schema, dict):
        return dict(config)
    properties = config_schema.get("properties")
    required = config_schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return dict(config)

    out = dict(config)
    for key in required:
        if not isinstance(key, str) or key in out:
            continue
        field = properties.get(key)
        if isinstance(field, dict) and "default" in field:
            out[key] = deepcopy(field["default"])
    return out


def config_schema_for_scope(
    config_schema: dict[str, Any],
    scope: str,
) -> dict[str, Any]:
    """Return a schema whose required fields match one config storage scope.

    A plugin config can be split between plugin-global storage and account
    storage via ``properties[*].level == "global"``. The complete schema still
    describes the merged runtime object, so validating either half with the
    complete ``required`` list rejects valid partial saves.
    """

    if not isinstance(config_schema, dict):
        return {}
    scoped = deepcopy(config_schema)
    properties = scoped.get("properties")
    if not isinstance(properties, dict):
        scoped.pop("required", None)
        return scoped

    if scope == "global":
        scoped_keys = {
            key
            for key, field in properties.items()
            if isinstance(field, dict) and field.get("level") == "global"
        }
    elif scope == "account":
        scoped_keys = {
            key
            for key, field in properties.items()
            if not (isinstance(field, dict) and field.get("level") == "global")
        }
    else:
        scoped_keys = set(properties)

    required = scoped.get("required")
    if isinstance(required, list):
        scoped["required"] = [
            key for key in required if isinstance(key, str) and key in scoped_keys
        ]
    return scoped


__all__ = [
    "apply_required_config_defaults",
    "bulk_set_enabled",
    "config_schema_for_scope",
    "feature_matrix",
    "get_account_features",
    "get_effective_plugin_config",
    "get_plugin_global_config",
    "list_features",
    "seed_builtin_features",
    "set_account_feature",
    "set_plugin_global_config",
    "validate_config_against_schema",
]
