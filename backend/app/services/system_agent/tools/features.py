"""功能/插件启停与账号级、全局配置工作流。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from ....db.models.account import Account
from ....db.models.feature import AccountFeature, Feature
from ....db.models.plugin import InstalledPlugin
from ....services import feature_service
from ....services.plugin_config_action_jobs import (
    get_plugin_config_action_job,
    list_plugin_config_action_jobs,
)
from ....services.plugin_config_actions import declared_config_actions
from ....services.plugin_config_secrets import (
    decrypt_config_secrets,
    mask_config_secrets,
    preserve_masked_config_secrets,
)
from ....services.redactor import redact_value
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter


def _public_config(
    value: dict[str, Any] | None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """配置只返回遮罩值；加密信封和敏感键都不能进入会话。"""

    masked = mask_config_secrets(dict(value or {}), schema=schema)
    redacted = redact_value(masked)
    return redacted if isinstance(redacted, dict) else {}


def _parse_config_json(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("config_json")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("需要 config_json（JSON 对象字符串）")
    if len(raw) > 65_536:
        raise ValueError("config_json 不能超过 64 KiB")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("config_json 必须是合法 JSON 对象字符串") from exc
    if not isinstance(value, dict):
        raise ValueError("config_json 顶层必须是 JSON 对象")
    return value


def _merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """递归合并对象；显式 null 会覆盖旧值。"""

    out = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_patch(dict(out[key]), value)
        else:
            out[key] = deepcopy(value)
    return out


def _declares_direct_passthrough(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    raw = capabilities.get("telegram_direct_passthrough")
    return raw is True or (isinstance(raw, dict) and raw.get("enabled") is True)


def _scope_schema(feature: Feature, scope: str) -> dict[str, Any] | None:
    manifest = feature.manifest if isinstance(feature.manifest, dict) else {}
    raw = manifest.get("config_schema")
    if not isinstance(raw, dict):
        if scope == "account" and _declares_direct_passthrough(manifest):
            return {
                "type": "object",
                "properties": {
                    "direct_passthrough": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "enabled": {"type": "boolean", "default": False},
                            "priority": {"type": "integer", "minimum": 0, "default": 1000},
                        },
                    }
                },
            }
        return None
    schema = feature_service.config_schema_for_scope(raw, scope)
    if scope == "account" and _declares_direct_passthrough(manifest):
        properties = schema.setdefault("properties", {})
        if isinstance(properties, dict):
            properties.setdefault(
                "direct_passthrough",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "enabled": {"type": "boolean", "default": False},
                        "priority": {"type": "integer", "minimum": 0, "default": 1000},
                    },
                },
            )
    return schema


def _validate_patch_scope(feature: Feature, patch: dict[str, Any], scope: str) -> None:
    manifest = feature.manifest if isinstance(feature.manifest, dict) else {}
    raw = manifest.get("config_schema")
    properties = raw.get("properties") if isinstance(raw, dict) else None
    if scope == "global" and not isinstance(properties, dict):
        raise ValueError(f"功能 {feature.key} 没有可写的全局配置字段")
    if isinstance(properties, dict):
        for key in patch:
            field = properties.get(key)
            if not isinstance(field, dict):
                continue
            is_global = field.get("level") == "global"
            if scope == "global" and not is_global:
                raise ValueError(f"{key} 是账号级字段，不能写入全局配置")
            if scope == "account" and is_global:
                raise ValueError(f"{key} 是全局字段，不能写入账号配置")
            if field.get("readOnly") is True:
                raise ValueError(f"{key} 是服务端只读字段，不能修改")
    if (
        scope == "account"
        and "direct_passthrough" in patch
        and not _declares_direct_passthrough(manifest)
    ):
        raise ValueError("该插件未声明 telegram_direct_passthrough，不能设置 direct_passthrough")


def _validate_config(config: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    if schema is None:
        return config
    normalized = feature_service.apply_required_config_defaults(config, schema)
    validation = feature_service.validate_config_against_schema(normalized, schema)
    if not validation.valid:
        detail = "; ".join(f"{e.field}: {e.message}" for e in validation.errors)
        raise ValueError(f"配置验证失败：{detail}")
    return normalized


async def _feature(ctx: ToolContext, key: str) -> Feature:
    await feature_service.seed_builtin_features(ctx.db)
    row = await ctx.db.get(Feature, key)
    if row is None:
        raise ValueError(f"未注册的功能/插件：{key}")
    return row


async def get_account_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        return {"error": "account_id_required", "message": "请提供 account_id"}

    fr = await ctx.db.execute(select(Feature))
    features = {f.key: f for f in fr.scalars().all()}
    ar = await ctx.db.execute(
        select(AccountFeature).where(AccountFeature.account_id == account_id)
    )
    account_features = list(ar.scalars().all())
    af_map = {af.feature_key: af for af in account_features}

    ir = await ctx.db.execute(select(InstalledPlugin))
    installed_by_key = {p.key: p for p in ir.scalars().all()}

    items: list[dict[str, Any]] = []
    for key, feature in sorted(features.items(), key=lambda x: x[0]):
        af = af_map.get(key)
        plugin = installed_by_key.get(key)
        is_plugin = plugin is not None or not bool(feature.is_builtin)
        items.append(
            {
                "feature_key": key,
                "name": feature.display_name or key,
                "enabled": bool(af.enabled) if af is not None else False,
                "kind": "plugin" if is_plugin else "builtin",
                "last_error": af.last_error if af is not None else None,
                "installed_package_enabled": bool(plugin.enabled) if plugin is not None else None,
            }
        )
    for key, af in af_map.items():
        if key in features:
            continue
        plugin = installed_by_key.get(key)
        items.append(
            {
                "feature_key": key,
                "name": key,
                "enabled": bool(af.enabled),
                "kind": "plugin" if plugin is not None else "unknown",
                "last_error": af.last_error,
                "installed_package_enabled": bool(plugin.enabled) if plugin is not None else None,
            }
        )

    enabled = [i for i in items if i["enabled"]]
    return {
        "account_id": account_id,
        "enabled_count": len(enabled),
        "total": len(items),
        "features": items,
        "note": "账号级启停统一在 AccountFeature；InstalledPlugin.enabled 是安装包全局状态，由 plugins 工具维护。",
    }


async def set_enabled_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    feature_key = str(args.get("feature_key") or args.get("key") or "").strip()
    enabled = bool(args.get("enabled"))
    if account_id is None or not feature_key:
        raise ValueError("需要 account_id 与 feature_key")
    feature = await ctx.db.get(Feature, feature_key)
    ar = await ctx.db.execute(
        select(AccountFeature).where(
            AccountFeature.account_id == account_id,
            AccountFeature.feature_key == feature_key,
        )
    )
    af = ar.scalar_one_or_none()
    return {
        "summary": f"{'启用' if enabled else '禁用'}账号 #{account_id} 的功能 {feature_key}",
        "account_id": account_id,
        "feature_key": feature_key,
        "feature_name": feature.display_name if feature else feature_key,
        "current_enabled": bool(af.enabled) if af is not None else False,
        "target_enabled": enabled,
        "note": "账号级启停；不会自动恢复。",
    }


async def set_enabled_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    feature_key = str(args.get("feature_key") or args.get("key") or "").strip()
    enabled = bool(args.get("enabled"))
    if account_id is None or not feature_key:
        raise ValueError("需要 account_id 与 feature_key")
    af = await feature_service.set_account_feature(
        ctx.db,
        account_id,
        feature_key,
        enabled,
        config=None,
        notify=False,
        commit=False,
    )
    return {
        "account_id": account_id,
        "feature_key": feature_key,
        "enabled": bool(af.enabled),
        "business_changed": True,
    }


async def _validate_direct_passthrough_order(
    ctx: ToolContext,
    account_id: int,
    order: list[str],
) -> list[tuple[Feature, AccountFeature]]:
    if not order or len(order) != len(set(order)):
        raise ValueError("order 不能为空且不能包含重复插件 key")
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 {account_id} 不存在")
    rows: list[tuple[Feature, AccountFeature]] = []
    for key in order:
        feature = await _feature(ctx, key)
        if not _declares_direct_passthrough(feature.manifest):
            raise ValueError(f"插件 {key} 未声明 telegram_direct_passthrough")
        account_feature = await ctx.db.get(AccountFeature, (account_id, key))
        if account_feature is None or not bool(account_feature.enabled):
            raise ValueError(f"插件 {key} 未在账号 #{account_id} 启用")
        block = (
            account_feature.config.get("direct_passthrough")
            if isinstance(account_feature.config, dict)
            else None
        )
        if not (isinstance(block, dict) and block.get("enabled") is True):
            raise ValueError(f"插件 {key} 未开启账号级裸直通")
        rows.append((feature, account_feature))
    return rows


async def reorder_direct_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("需要 account_id")
    order = [str(value).strip() for value in (args.get("order") or []) if str(value).strip()]
    rows = await _validate_direct_passthrough_order(ctx, account_id, order)
    return {
        "summary": f"重排账号 #{account_id} 的 {len(order)} 个裸直通插件优先级",
        "account_id": account_id,
        "order": order,
        "current": [
            {
                "feature_key": feature.key,
                "priority": (account_feature.config or {})
                .get("direct_passthrough", {})
                .get("priority"),
            }
            for feature, account_feature in rows
        ],
        "target": [
            {"feature_key": key, "priority": index * 10}
            for index, key in enumerate(order)
        ],
    }


async def reorder_direct_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    if account_id is None:
        raise ValueError("需要 account_id")
    order = [str(value).strip() for value in (args.get("order") or []) if str(value).strip()]
    rows = await _validate_direct_passthrough_order(ctx, account_id, order)
    updated: list[dict[str, Any]] = []
    for index, (feature, account_feature) in enumerate(rows):
        config = deepcopy(dict(account_feature.config or {}))
        block = (
            dict(config.get("direct_passthrough") or {})
            if isinstance(config.get("direct_passthrough"), dict)
            else {}
        )
        block["priority"] = index * 10
        config["direct_passthrough"] = block
        row = await feature_service.set_account_feature(
            ctx.db,
            account_id,
            feature.key,
            enabled=True,
            config=config,
            commit=False,
            notify=False,
        )
        updated.append(
            {
                "feature_key": row.feature_key,
                "priority": index * 10,
            }
        )
    return {
        "account_id": account_id,
        "order": order,
        "updated": updated,
        "business_changed": True,
    }


async def get_config(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    feature_key = str(args.get("feature_key") or args.get("key") or "").strip()
    if not feature_key:
        raise ValueError("需要 feature_key")
    feature = await _feature(ctx, feature_key)
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    global_config = await feature_service.get_plugin_global_config(ctx.db, feature_key)
    account_config: dict[str, Any] | None = None
    effective: dict[str, Any] | None = None
    enabled: bool | None = None
    if account_id is not None:
        if await ctx.db.get(Account, account_id) is None:
            raise ValueError(f"账号 #{account_id} 不存在")
        af = await ctx.db.get(AccountFeature, (account_id, feature_key))
        account_config = dict(af.config or {}) if af is not None else {}
        enabled = bool(af.enabled) if af is not None else False
        effective = await feature_service.get_effective_plugin_config(
            ctx.db, account_id, feature_key
        )
    manifest = feature.manifest if isinstance(feature.manifest, dict) else {}
    raw_schema = manifest.get("config_schema") if isinstance(manifest.get("config_schema"), dict) else None
    return {
        "feature_key": feature_key,
        "feature_name": feature.display_name or feature_key,
        "account_id": account_id,
        "enabled": enabled,
        "global_config": _public_config(global_config, raw_schema),
        "account_config": _public_config(account_config, raw_schema),
        "effective_config": _public_config(effective, raw_schema),
        "config_schema": redact_value(manifest.get("config_schema")),
        "note": "敏感字段只显示掩码；保存采用 JSON 对象补丁，未提供字段保持不变。",
    }


async def _prepare_account_config(
    ctx: ToolContext, args: dict[str, Any]
) -> tuple[int, Feature, dict[str, Any], dict[str, Any]]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    feature_key = str(args.get("feature_key") or args.get("key") or "").strip()
    if account_id is None or not feature_key:
        raise ValueError("需要 account_id 与 feature_key")
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    feature = await _feature(ctx, feature_key)
    patch = _parse_config_json(args)
    _validate_patch_scope(feature, patch, "account")
    existing = await ctx.db.get(AccountFeature, (account_id, feature_key))
    stored = dict(existing.config or {}) if existing is not None else {}
    raw_schema = (
        feature.manifest.get("config_schema")
        if isinstance(feature.manifest, dict)
        else None
    )
    current = decrypt_config_secrets(
        stored,
        schema=raw_schema if isinstance(raw_schema, dict) else None,
        strict=True,
    )
    patch = preserve_masked_config_secrets(current, patch, schema=raw_schema)
    merged = _merge_patch(current, patch)
    if feature_key == "codex_image" and merged.get("model") == "gpt-5.4":
        merged["model"] = "gpt-5.5"
    merged = _validate_config(merged, _scope_schema(feature, "account"))
    return account_id, feature, patch, merged


async def save_account_config_preview(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    account_id, feature, patch, _merged = await _prepare_account_config(ctx, args)
    current = await get_config(
        ctx, {"account_id": account_id, "feature_key": feature.key}
    )
    return {
        "summary": f"更新账号 #{account_id} 的 {feature.key} 配置",
        "account_id": account_id,
        "feature_key": feature.key,
        "changed_keys": sorted(str(key) for key in patch),
        "current_config": current["account_config"],
        "warning": "确认后覆盖这些字段并热加载 Worker；敏感值不会显示在预览中。",
    }


async def save_account_config_execute(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    account_id, feature, patch, merged = await _prepare_account_config(ctx, args)
    existing = await ctx.db.get(AccountFeature, (account_id, feature.key))
    af = await feature_service.set_account_feature(
        ctx.db,
        account_id,
        feature.key,
        enabled=bool(existing.enabled) if existing is not None else False,
        config=merged,
        notify=False,
        commit=False,
    )
    return {
        "account_id": account_id,
        "feature_key": feature.key,
        "enabled": bool(af.enabled),
        "changed_keys": sorted(str(key) for key in patch),
        "business_changed": True,
    }


async def _prepare_global_config(
    ctx: ToolContext, args: dict[str, Any]
) -> tuple[Feature, dict[str, Any], dict[str, Any]]:
    feature_key = str(args.get("feature_key") or args.get("key") or "").strip()
    if not feature_key:
        raise ValueError("需要 feature_key")
    feature = await _feature(ctx, feature_key)
    patch = _parse_config_json(args)
    _validate_patch_scope(feature, patch, "global")
    stored = await feature_service.get_plugin_global_config(ctx.db, feature_key)
    raw_schema = (
        feature.manifest.get("config_schema")
        if isinstance(feature.manifest, dict)
        else None
    )
    current = decrypt_config_secrets(
        stored,
        schema=raw_schema if isinstance(raw_schema, dict) else None,
        strict=True,
    )
    patch = preserve_masked_config_secrets(current, patch, schema=raw_schema)
    merged = _merge_patch(current, patch)
    merged = _validate_config(merged, _scope_schema(feature, "global"))
    return feature, patch, merged


async def save_global_config_preview(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    feature, patch, _merged = await _prepare_global_config(ctx, args)
    current = await feature_service.get_plugin_global_config(ctx.db, feature.key)
    raw_schema = (
        feature.manifest.get("config_schema")
        if isinstance(feature.manifest, dict)
        and isinstance(feature.manifest.get("config_schema"), dict)
        else None
    )
    return {
        "summary": f"更新插件 {feature.key} 的全局配置",
        "feature_key": feature.key,
        "changed_keys": sorted(str(key) for key in patch),
        "current_config": _public_config(current, raw_schema),
        "warning": "确认后影响所有使用该插件的账号；敏感值不会显示在预览中。",
    }


async def save_global_config_execute(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    feature, patch, merged = await _prepare_global_config(ctx, args)
    stored = await feature_service.set_plugin_global_config(
        ctx.db,
        feature.key,
        merged,
        notify=False,
        commit=False,
    )
    return {
        "feature_key": feature.key,
        "changed_keys": sorted(str(key) for key in patch),
        "stored_keys": sorted(str(key) for key in stored),
        "business_changed": True,
    }


async def list_config_actions(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    feature_key = str(args.get("feature_key") or "").strip()
    if account_id is None or not feature_key:
        raise ValueError("需要 account_id 与 feature_key")
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    feature = await _feature(ctx, feature_key)
    installed = await ctx.db.get(InstalledPlugin, feature_key)
    actions = declared_config_actions(feature, installed_plugin=installed)
    jobs = await list_plugin_config_action_jobs(
        ctx.db,
        account_id=account_id,
        plugin_key=feature_key,
        limit=max(1, min(int(args.get("limit") or 10), 50)),
    )
    return {
        "account_id": account_id,
        "feature_key": feature_key,
        "actions": redact_value(actions),
        "recent_jobs": [redact_value(job.model_dump(mode="json")) for job in jobs],
    }


async def get_config_action_job(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("需要 job_id")
    job = await get_plugin_config_action_job(ctx.db, job_id)
    if job is None:
        raise ValueError(f"插件配置动作任务 {job_id} 不存在")
    data = job.model_dump(mode="json")
    if ctx.channel == "bot" and (
        ctx.account_id is None or int(data.get("account_id") or 0) != int(ctx.account_id)
    ):
        raise ValueError("Bot 渠道只能查看当前绑定账号的配置动作任务")
    return redact_value(data)


async def _prepare_config_action(
    ctx: ToolContext, args: dict[str, Any]
) -> tuple[int, Feature, dict[str, Any], dict[str, Any]]:
    account_id = account_scope_filter(
        args.get("account_id"),
        context_account_id=ctx.account_id,
        channel=ctx.channel,
    )
    feature_key = str(args.get("feature_key") or "").strip()
    action_key = str(args.get("action_key") or "").strip()
    if account_id is None or not feature_key or not action_key:
        raise ValueError("需要 account_id、feature_key 与 action_key")
    if await ctx.db.get(Account, account_id) is None:
        raise ValueError(f"账号 #{account_id} 不存在")
    feature = await _feature(ctx, feature_key)
    installed = await ctx.db.get(InstalledPlugin, feature_key)
    actions = declared_config_actions(feature, installed_plugin=installed)
    action = next(
        (item for item in actions if str(item.get("key") or "").strip() == action_key),
        None,
    )
    if action is None:
        raise ValueError(f"插件 {feature_key} 未声明配置动作 {action_key}")
    raw = args.get("payload_json")
    if not isinstance(raw, str) or not raw.strip():
        payload: dict[str, Any] = {}
    else:
        if len(raw) > 65_536:
            raise ValueError("payload_json 不能超过 64 KiB")
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("payload_json 必须是合法 JSON 对象字符串") from exc
        if not isinstance(parsed, dict):
            raise ValueError("payload_json 顶层必须是 JSON 对象")
        payload = parsed
    for key in ("config", "input"):
        if key in payload and not isinstance(payload[key], dict):
            raise ValueError(f"payload_json.{key} 必须是对象")
    return account_id, feature, action, payload


async def run_config_action_preview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id, feature, action, payload = await _prepare_config_action(ctx, args)
    background = bool(args.get("background", False))
    return {
        "summary": (
            f"启动插件 {feature.key} 的后台配置动作 {action['key']}"
            if background
            else f"运行插件 {feature.key} 的配置动作 {action['key']}"
        ),
        "account_id": account_id,
        "feature_key": feature.key,
        "action_key": action["key"],
        "action_label": action.get("label") or action.get("title"),
        "background": background,
        "input_keys": sorted(str(key) for key in (payload.get("input") or {})),
        "config_override_keys": sorted(str(key) for key in (payload.get("config") or {})),
        "warning": "确认后会执行插件代码；它只能使用插件已声明的 HTTP/AI 权限。",
    }


async def run_config_action_execute(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id, feature, action, _payload = await _prepare_config_action(ctx, args)
    return {
        "account_id": account_id,
        "feature_key": feature.key,
        "action_key": action["key"],
        "background": bool(args.get("background", False)),
        "runtime_sync_required": True,
        "business_changed": True,
    }


async def control_config_action_preview(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    job_id = str(args.get("job_id") or "").strip()
    action = str(args.get("action") or "").strip().lower()
    if not job_id or action not in {"pause", "cancel"}:
        raise ValueError("需要 job_id，action 仅支持 pause 或 cancel")
    job = await get_plugin_config_action_job(ctx.db, job_id)
    if job is None:
        raise ValueError(f"插件配置动作任务 {job_id} 不存在")
    data = job.model_dump(mode="json")
    if ctx.channel == "bot" and (
        ctx.account_id is None or int(data.get("account_id") or 0) != int(ctx.account_id)
    ):
        raise ValueError("Bot 渠道只能控制当前绑定账号的配置动作任务")
    return {
        "summary": f"{'中断' if action == 'pause' else '终止'}插件配置动作任务 {job_id}",
        "job_id": job_id,
        "action": action,
        "current_status": data.get("status"),
        "plugin_key": data.get("plugin_key"),
        "account_id": data.get("account_id"),
        "warning": "任务运行中的外部请求可能已经发生；控制操作只停止后续步骤。",
    }


async def control_config_action_execute(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    preview = await control_config_action_preview(ctx, args)
    return {
        "job_id": preview["job_id"],
        "action": preview["action"],
        "runtime_sync_required": True,
        "business_changed": True,
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="features.reorder_direct_passthrough",
            description="按列表顺序重排账号已开启的裸直通插件优先级（0、10、20…）。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "order": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["order"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=reorder_direct_preview,
            execute_handler=reorder_direct_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="features.get_account_status",
            description="获取账号功能/插件启停矩阵，通过元数据区分内置功能与插件。",
            input_schema={
                "type": "object",
                "properties": {"account_id": {"type": "integer"}},
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_account_status,
        )
    )
    registry.register(
        ToolSpec(
            name="features.set_enabled",
            description="启停账号级功能或插件（AccountFeature）。禁用不会自动恢复。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "feature_key": {"type": "string"},
                    "key": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["enabled"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="operator",
            risk="normal",
            preview_handler=set_enabled_preview,
            execute_handler=set_enabled_execute,
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="features.get_config",
            description=(
                "读取功能/插件的配置 Schema、遮罩后的全局配置，以及指定账号的账号级和最终生效配置。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "feature_key": {"type": "string"},
                    "key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_config,
        )
    )
    config_schema = {
        "type": "object",
        "properties": {
            "account_id": {"type": "integer"},
            "feature_key": {"type": "string"},
            "key": {"type": "string"},
            "config_json": {
                "type": "string",
                "description": "要合并保存的 JSON 对象字符串；可能含密钥，因此整体加密存入待确认 Action。",
            },
        },
        "required": ["feature_key", "config_json"],
        "additionalProperties": False,
    }
    registry.register(
        ToolSpec(
            name="features.save_account_config",
            description="以补丁方式保存账号级功能/插件配置；未提供字段保持不变。",
            input_schema=config_schema,
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=save_account_config_preview,
            execute_handler=save_account_config_execute,
            secret_argument_names=("config_json",),
            runtime_effects=("reload_config",),
        )
    )
    registry.register(
        ToolSpec(
            name="features.save_global_config",
            description="以补丁方式保存插件全局配置；仅接受 Schema 中 level=global 的字段。",
            input_schema={
                "type": "object",
                "properties": {
                    "feature_key": {"type": "string"},
                    "key": {"type": "string"},
                    "config_json": config_schema["properties"]["config_json"],
                },
                "required": ["feature_key", "config_json"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            channels=("web",),
            risk="dangerous",
            preview_handler=save_global_config_preview,
            execute_handler=save_global_config_execute,
            secret_argument_names=("config_json",),
            runtime_effects=("reload_feature_accounts",),
        )
    )
    registry.register(
        ToolSpec(
            name="features.list_config_actions",
            description="列出插件声明的配置动作及该账号最近的后台动作任务。",
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "feature_key": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["account_id", "feature_key"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=list_config_actions,
        )
    )
    registry.register(
        ToolSpec(
            name="features.get_config_action_job",
            description="查询插件配置动作后台任务状态与脱敏过程日志。",
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_config_action_job,
        )
    )
    registry.register(
        ToolSpec(
            name="features.run_config_action",
            description=(
                "运行插件声明的配置动作；background=true 时创建可查询和控制的后台任务。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "feature_key": {"type": "string"},
                    "action_key": {"type": "string"},
                    "background": {"type": "boolean"},
                    "payload_json": {
                        "type": "string",
                        "description": "可选 {config,input} JSON 对象字符串，整体加密存入 Action。",
                    },
                },
                "required": ["account_id", "feature_key", "action_key"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=run_config_action_preview,
            execute_handler=run_config_action_execute,
            secret_argument_names=("payload_json",),
            runtime_effects=("plugin_config_action",),
            runtime_retryable=False,
        )
    )
    registry.register(
        ToolSpec(
            name="features.control_config_action_job",
            description="中断（pause）或终止（cancel）插件配置动作后台任务。",
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["pause", "cancel"]},
                },
                "required": ["job_id", "action"],
                "additionalProperties": False,
            },
            read_only=False,
            min_role="admin",
            risk="dangerous",
            preview_handler=control_config_action_preview,
            execute_handler=control_config_action_execute,
            runtime_effects=("plugin_config_action_control",),
        )
    )
