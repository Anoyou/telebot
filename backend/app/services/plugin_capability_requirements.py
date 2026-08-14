"""静态读取插件对平台能力模块的声明。

该模块不执行第三方 ``manifest.py``。存量插件允许缺少声明，但缺声明、
声明损坏或源码缺失的叶不会参与 demand 计算。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.plugin import InstalledPlugin
from ..feature_registry import (
    _BUILTIN_PLUGIN_DIR,
    _NON_CORE_BUILTIN_COMPAT_KEYS,
)
from ..settings import settings

PLATFORM_CAPABILITY_KEYS: tuple[str, ...] = (
    "ai",
    "interaction_bot",
    "webhooks",
    "ledger",
    "dispatch_debug",
)
_CAPABILITY_SET = frozenset(PLATFORM_CAPABILITY_KEYS)
MISSING_DECLARATION_WARNING = (
    "插件未声明 requires_platform_capabilities；存量继续运行，升级时将强制校验。"
)
SOURCE_MISSING_WARNING = "插件源缺失；不参与平台能力需求推导。"


@dataclass(frozen=True, slots=True)
class PluginCapabilityRequirement:
    key: str
    source: str
    path: Path
    declared: bool
    requires: tuple[str, ...] = ()
    source_missing: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def participates_in_demand(self) -> bool:
        return self.declared and not self.source_missing and not self.warnings


def _normalize_declared(value: Any, *, origin: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{origin} 的 requires_platform_capabilities 必须是数组")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in _CAPABILITY_SET:
            raise ValueError(f"{origin} 声明了未知平台能力: {item!r}")
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _read_plugin_json(path: Path) -> tuple[bool, tuple[str, ...]] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("plugin.json 顶层必须是对象")
    if "requires_platform_capabilities" not in raw:
        return False, ()
    return True, _normalize_declared(
        raw["requires_platform_capabilities"], origin="plugin.json"
    )


def _manifest_call(tree: ast.Module) -> ast.Call | None:
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MANIFEST"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MANIFEST"
        ):
            value = node.value
        if isinstance(value, ast.Call):
            return value
    return None


def _read_manifest_py(path: Path) -> tuple[bool, tuple[str, ...]] | None:
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    call = _manifest_call(tree)
    if call is None:
        raise ValueError("manifest.py 未找到顶层 MANIFEST = Manifest(...)")
    for keyword in call.keywords:
        if keyword.arg == "requires_platform_capabilities":
            value = ast.literal_eval(keyword.value)
            return True, _normalize_declared(value, origin="manifest.py")
    return False, ()


def read_plugin_capability_requirement(
    key: str,
    path: Path,
    *,
    source: str,
) -> PluginCapabilityRequirement:
    path = path.resolve()
    metadata_files = (path / "plugin.json", path / "manifest.py")
    if not path.is_dir() or not any(item.is_file() for item in metadata_files):
        return PluginCapabilityRequirement(
            key=key,
            source=source,
            path=path,
            declared=False,
            source_missing=True,
            warnings=(SOURCE_MISSING_WARNING,),
        )

    warnings: list[str] = []
    readings: list[tuple[str, bool, tuple[str, ...]]] = []
    for label, reader, metadata_path in (
        ("plugin.json", _read_plugin_json, metadata_files[0]),
        ("manifest.py", _read_manifest_py, metadata_files[1]),
    ):
        try:
            result = reader(metadata_path)
        except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"{label} 平台能力声明不可用：{exc}")
            continue
        if result is not None:
            readings.append((label, result[0], result[1]))

    declared_values = [(label, value) for label, declared, value in readings if declared]
    if len(declared_values) > 1 and len({value for _label, value in declared_values}) > 1:
        warnings.append("plugin.json 与 manifest.py 的 requires_platform_capabilities 声明不一致。")
    if not declared_values:
        warnings.append(MISSING_DECLARATION_WARNING)
        return PluginCapabilityRequirement(
            key=key,
            source=source,
            path=path,
            declared=False,
            warnings=tuple(warnings),
        )

    requires = declared_values[0][1]
    return PluginCapabilityRequirement(
        key=key,
        source=source,
        path=path,
        declared=True,
        requires=requires,
        warnings=tuple(warnings),
    )


def list_builtin_capability_requirements() -> list[PluginCapabilityRequirement]:
    records: list[PluginCapabilityRequirement] = []
    if not _BUILTIN_PLUGIN_DIR.is_dir():
        return records
    for child in sorted(_BUILTIN_PLUGIN_DIR.iterdir(), key=lambda item: item.name):
        if (
            not child.is_dir()
            or child.name.startswith("_")
            or child.name in _NON_CORE_BUILTIN_COMPAT_KEYS
            or not (child / "manifest.py").is_file()
        ):
            continue
        records.append(
            read_plugin_capability_requirement(child.name, child, source="builtin")
        )
    return records


async def list_installed_capability_requirements(
    db: AsyncSession,
) -> list[PluginCapabilityRequirement]:
    rows = (
        await db.execute(select(InstalledPlugin).order_by(InstalledPlugin.key.asc()))
    ).scalars().all()
    records: list[PluginCapabilityRequirement] = []
    for row in rows:
        path = Path(row.installed_path) if row.installed_path else settings.plugins_installed_path / row.key
        records.append(
            read_plugin_capability_requirement(row.key, path, source=str(row.source))
        )
    return records


async def get_plugin_capability_requirement(
    db: AsyncSession, key: str
) -> PluginCapabilityRequirement | None:
    row = await db.get(InstalledPlugin, key)
    if row is not None:
        path = Path(row.installed_path) if row.installed_path else settings.plugins_installed_path / key
        return read_plugin_capability_requirement(key, path, source=str(row.source))
    builtin_path = _BUILTIN_PLUGIN_DIR / key
    if key in _NON_CORE_BUILTIN_COMPAT_KEYS or not (builtin_path / "manifest.py").is_file():
        return None
    return read_plugin_capability_requirement(key, builtin_path, source="builtin")


__all__ = [
    "MISSING_DECLARATION_WARNING",
    "PLATFORM_CAPABILITY_KEYS",
    "PluginCapabilityRequirement",
    "SOURCE_MISSING_WARNING",
    "get_plugin_capability_requirement",
    "list_builtin_capability_requirements",
    "list_installed_capability_requirements",
    "read_plugin_capability_requirement",
]
