#!/usr/bin/env python3
"""Validate installed plugin metadata stays in sync with MANIFEST.

The installed directory can contain user-maintained plugins that are not part of
this docs/examples migration. This script fails on contract drift and deprecated
runtime risks, and prints warnings for plugins that still need final Event Bus
metadata.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
INSTALLED_ROOT = ROOT / "plugins" / "installed"

for import_root in (ROOT, BACKEND_ROOT):
    path = str(import_root)
    if path not in sys.path:
        sys.path.insert(0, path)

from app.worker.plugins.manifest import Manifest  # noqa: E402
from app.services.event_bus import SUPPORTED_FILTER_KEYS  # noqa: E402

REQUIRED_FILES = {"plugin.json", "manifest.py", "plugin.py", "__init__.py"}
DEPRECATED_RISK_TOKENS = ("bbot_notice", "notice_bot", "raw_event")
DEPRECATED_SEND_VIA = {"notice", "bbot_notice", "notice_bot"}


def _load_installed_module(plugin_key: str, filename: str) -> types.ModuleType:
    package_root = "plugins.installed"
    if package_root not in sys.modules:
        pkg = types.ModuleType(package_root)
        pkg.__path__ = [str(INSTALLED_ROOT)]  # type: ignore[attr-defined]
        sys.modules[package_root] = pkg

    package_name = f"{package_root}.{plugin_key}"
    plugin_dir = INSTALLED_ROOT / plugin_key
    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        sys.modules[package_name] = pkg

    path = plugin_dir / filename
    module_name = f"{package_name}.{filename[:-3]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{plugin_key}: 无法加载模块 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_plugin_json(plugin_dir: Path) -> dict[str, Any]:
    path = plugin_dir / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{path}: plugin.json 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"{path}: plugin.json 顶层必须是 object")
    return data


def _has_event_subscription(metadata: dict[str, Any], manifest: Manifest | None = None) -> bool:
    raw = metadata.get("event_subscriptions")
    if isinstance(raw, list) and any(isinstance(item, dict) for item in raw):
        return True
    if isinstance(manifest, Manifest):
        return any(isinstance(item, dict) for item in manifest.event_subscriptions)
    return False


def _installed_plugin_keys() -> list[str]:
    keys: set[str] = set()
    for plugin_dir in sorted(path for path in INSTALLED_ROOT.iterdir() if path.is_dir()):
        plugin_json = plugin_dir / "plugin.json"
        manifest_py = plugin_dir / "manifest.py"
        has_interaction_entries = False
        has_event_subscriptions = False
        if plugin_json.is_file():
            metadata = _load_plugin_json(plugin_dir)
            entries = metadata.get("interaction_entries")
            has_interaction_entries = isinstance(entries, list) and any(isinstance(item, dict) for item in entries)
            has_event_subscriptions = _has_event_subscription(metadata)
        if not has_interaction_entries and manifest_py.is_file():
            manifest_module = _load_installed_module(plugin_dir.name, "manifest.py")
            manifest = getattr(manifest_module, "MANIFEST", None)
            has_interaction_entries = isinstance(manifest, Manifest) and any(
                isinstance(item, dict) for item in manifest.interaction_entries
            )
            has_event_subscriptions = has_event_subscriptions or (
                isinstance(manifest, Manifest) and _has_event_subscription({}, manifest)
            )
        if has_interaction_entries or has_event_subscriptions:
            keys.add(plugin_dir.name)
    return sorted(keys)


def _manifest_capabilities(manifest: Manifest) -> dict[str, Any]:
    capabilities = getattr(manifest, "capabilities", None)
    return dict(capabilities) if isinstance(capabilities, dict) else {}


def _validate_high_risk_capabilities(plugin_key: str, capabilities: dict[str, Any]) -> None:
    direct_capability = capabilities.get("telegram_direct_passthrough")
    if isinstance(direct_capability, dict) and direct_capability.get("enabled") is True:
        if not str(direct_capability.get("reason") or "").strip():
            raise AssertionError(f"{plugin_key}: telegram_direct_passthrough.enabled=true 时必须说明 reason")


def _has_usage(metadata: dict[str, Any], manifest: Manifest) -> bool:
    if str(metadata.get("usage") or "").strip():
        return True
    schema = metadata.get("config_schema")
    if not isinstance(schema, dict):
        schema = manifest.config_schema if isinstance(manifest.config_schema, dict) else {}
    if not isinstance(schema, dict):
        return False
    for key in ("x-usage-guide", "x-usage-instructions", "x-usage-steps", "usage_preview", "usage_guide", "usage_instructions"):
        if str(schema.get(key) or "").strip():
            return True
    return False


def _validate_deprecated_risks(plugin_key: str, plugin_dir: Path, metadata: dict[str, Any]) -> None:
    for path in sorted(plugin_dir.glob("*")):
        if path.suffix not in {".py", ".json", ".md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in DEPRECATED_RISK_TOKENS:
            if token in text:
                raise AssertionError(f"{plugin_key}: {path.name} 仍包含旧风险字段 {token}")

    raw_entries = metadata.get("interaction_entries")
    if isinstance(raw_entries, list):
        for index, entry in enumerate(raw_entries, start=1):
            if not isinstance(entry, dict):
                continue
            contract = entry.get("result_contract") if isinstance(entry.get("result_contract"), dict) else {}
            send_via = contract.get("send_via")
            if isinstance(send_via, list) and any(str(item) in DEPRECATED_SEND_VIA for item in send_via):
                raise AssertionError(f"{plugin_key}: interaction_entries[{index}] result_contract.send_via 包含旧 notice 通道")


def _validate_plugin(plugin_key: str) -> None:
    plugin_dir = INSTALLED_ROOT / plugin_key
    missing = sorted(file for file in REQUIRED_FILES if not (plugin_dir / file).is_file())
    if missing:
        raise AssertionError(f"{plugin_key}: 缺少必要文件: {', '.join(missing)}")

    metadata = _load_plugin_json(plugin_dir)
    manifest_module = _load_installed_module(plugin_key, "manifest.py")
    manifest = getattr(manifest_module, "MANIFEST", None)
    if not isinstance(manifest, Manifest):
        raise AssertionError(f"{plugin_key}: MANIFEST 必须是 Manifest 实例")

    plugin_json_key = metadata.get("name") or metadata.get("key")
    if plugin_json_key != manifest.key:
        raise AssertionError(f"{plugin_key}: plugin.json key 与 MANIFEST.key 不一致")
    if metadata.get("version") != manifest.version:
        raise AssertionError(f"{plugin_key}: plugin.json.version 与 MANIFEST.version 不一致")
    if metadata.get("category") != manifest.category:
        raise AssertionError(f"{plugin_key}: plugin.json.category 与 MANIFEST.category 不一致")
    if metadata.get("interaction_profile") != manifest.interaction_profile:
        raise AssertionError(f"{plugin_key}: plugin.json.interaction_profile 与 MANIFEST.interaction_profile 不一致")
    if list(metadata.get("interaction_entries") or []) != list(manifest.interaction_entries):
        raise AssertionError(f"{plugin_key}: plugin.json.interaction_entries 与 MANIFEST.interaction_entries 不一致")

    metadata_subscriptions = metadata.get("event_subscriptions")
    if metadata_subscriptions is None:
        metadata_subscriptions = []
    if not isinstance(metadata_subscriptions, list):
        raise AssertionError(f"{plugin_key}: plugin.json.event_subscriptions 必须是数组")
    metadata_subscriptions = [item for item in metadata_subscriptions if isinstance(item, dict)]
    manifest_subscriptions = list(manifest.event_subscriptions or [])
    if metadata_subscriptions and metadata_subscriptions != manifest_subscriptions:
        raise AssertionError(f"{plugin_key}: plugin.json.event_subscriptions 与 MANIFEST.event_subscriptions 不一致")

    metadata_capabilities = metadata.get("capabilities")
    if metadata_capabilities is None:
        metadata_capabilities = {}
    if not isinstance(metadata_capabilities, dict):
        raise AssertionError(f"{plugin_key}: plugin.json.capabilities 必须是对象")
    if metadata_capabilities and dict(metadata_capabilities) != _manifest_capabilities(manifest):
        raise AssertionError(f"{plugin_key}: plugin.json.capabilities 与 MANIFEST.capabilities 不一致")
    _validate_high_risk_capabilities(plugin_key, metadata_capabilities)

    _validate_deprecated_risks(plugin_key, plugin_dir, metadata)
    warnings: list[str] = []
    warnings.extend(_unknown_filter_key_warnings(metadata_subscriptions))
    if not _has_usage(metadata, manifest):
        warnings.append("缺少 usage 或 config_schema 使用说明")
    if list(metadata.get("interaction_entries") or []) and not metadata_subscriptions:
        warnings.append("仍是旧 interaction_entries 主声明，建议补 event_subscriptions")
    if metadata_capabilities == {} and _has_event_subscription(metadata, manifest):
        warnings.append("缺少 capabilities；如不需要高风险能力也建议显式写 {}")
    for warning in warnings:
        print(f"warn: {plugin_key} - {warning}")
    print(f"ok: {plugin_key}")


def _unknown_filter_key_warnings(subscriptions: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for index, subscription in enumerate(subscriptions, start=1):
        filters = subscription.get("filters")
        if not isinstance(filters, dict):
            continue
        unknown = sorted(
            {
                str(key).strip()
                for key in filters
                if str(key).strip() and str(key).strip() not in SUPPORTED_FILTER_KEYS
            }
        )
        if unknown:
            warnings.append(f"event_subscriptions[{index}] filters 含未知 key: {', '.join(unknown)}")
    return warnings


def main() -> int:
    if not INSTALLED_ROOT.is_dir():
        raise AssertionError(f"已安装插件目录不存在: {INSTALLED_ROOT}")

    plugin_keys = _installed_plugin_keys()
    if not plugin_keys:
        raise AssertionError("未发现声明 interaction_entries 或 event_subscriptions 的已安装插件")

    for plugin_key in plugin_keys:
        _validate_plugin(plugin_key)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"installed interaction plugin validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
