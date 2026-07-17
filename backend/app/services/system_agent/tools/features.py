"""功能与账号级插件启停只读工具。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ....db.models.feature import AccountFeature, Feature
from ....db.models.plugin import InstalledPlugin
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import account_scope_filter


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
        "note": "账号级启停统一在 AccountFeature；InstalledPlugin.enabled 是安装包全局状态，首发不接入。",
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
    from ....services import feature_service

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


def register(registry: ToolRegistry) -> None:
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
