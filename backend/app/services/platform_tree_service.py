"""聚合树干、平台枝与插件叶的只读运行视图。"""

from __future__ import annotations

import ast
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.feature import AccountFeature
from ..db.models.plugin import InstalledPlugin
from ..worker.supervisor import get_worker_runtime_snapshot
from . import kill_switch_service, platform_capabilities, runtime_profile_service
from .plugin_capability_requirements import (
    PluginCapabilityRequirement,
    list_builtin_capability_requirements,
    list_installed_capability_requirements,
)


def _attachment_from_metadata(record: PluginCapabilityRequirement) -> str:
    plugin_json = record.path / "plugin.json"
    if plugin_json.is_file():
        try:
            raw = json.loads(plugin_json.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                capabilities = raw.get("capabilities")
                passthrough = (
                    capabilities.get("telegram_direct_passthrough")
                    if isinstance(capabilities, dict)
                    else None
                )
                if isinstance(passthrough, dict) and passthrough.get("enabled") is True:
                    return "直通"
                if raw.get("interaction_entries") or raw.get("interaction_profile"):
                    return "交互"
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if "interaction_bot" in record.requires:
        return "交互"
    manifest_py = record.path / "manifest.py"
    if manifest_py.is_file():
        try:
            tree = ast.parse(manifest_py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.keyword) or node.arg != "capabilities":
                    continue
                value: Any = ast.literal_eval(node.value)
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("telegram_direct_passthrough"), dict)
                    and value["telegram_direct_passthrough"].get("enabled") is True
                ):
                    return "直通"
        except (OSError, SyntaxError, ValueError):
            pass
    return "命令"


async def build_platform_tree(db: AsyncSession) -> dict[str, Any]:
    if not platform_capabilities.get_snapshot().cache_ready:
        await platform_capabilities.refresh_cache_from_db(db)
    profile = await runtime_profile_service.get_status(db)
    demand = await platform_capabilities.compute_demand(db)
    snapshot = platform_capabilities.get_snapshot()
    workers = get_worker_runtime_snapshot()

    enabled_leaf_keys = set(
        (
            await db.execute(
                select(AccountFeature.feature_key).where(AccountFeature.enabled.is_(True))
            )
        ).scalars()
    )
    installed_rows = (await db.execute(select(InstalledPlugin))).scalars().all()
    installed_enabled = {row.key: bool(row.enabled) for row in installed_rows}

    records = list_builtin_capability_requirements()
    records.extend(await list_installed_capability_requirements(db))
    leaves = []
    for record in records:
        enabled = record.key in enabled_leaf_keys
        if record.source != "builtin":
            enabled = enabled and installed_enabled.get(record.key, False)
        leaves.append(
            {
                "key": record.key,
                "attachment": _attachment_from_metadata(record),
                "enabled": enabled,
                "requires": list(record.requires),
                "warnings": list(record.warnings),
                "source_missing": record.source_missing,
            }
        )

    return {
        "trunk": {
            "userbot": {
                "workers": workers,
                "total": len(workers),
                "alive": sum(1 for row in workers if row.get("alive") is True),
            },
            "kill_switch": await kill_switch_service.get_enabled(db),
            "current_profile": profile.get("current_profile", "production"),
        },
        "branches": {
            key: {
                "state": snapshot.runtime.get(key, "stopped"),
                "desired": bool(snapshot.desired.get(key, False)),
                "forced_off": bool(snapshot.forced_off.get(key, False)),
                "demanded_by": demand[key],
                "can_turn_off": bool(snapshot.desired.get(key, False) and not demand[key]),
            }
            for key in platform_capabilities.ALL_MODULE_KEYS
        },
        "leaves": sorted(leaves, key=lambda item: item["key"]),
    }


__all__ = ["build_platform_tree"]
