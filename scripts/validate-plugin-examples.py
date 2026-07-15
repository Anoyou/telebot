#!/usr/bin/env python3
"""Validate maintained plugin examples without network or private LLM access."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = ROOT / "examples" / "plugins"
BACKEND_ROOT = ROOT / "backend"

for import_root in (ROOT, BACKEND_ROOT):
    path = str(import_root)
    if path not in sys.path:
        sys.path.insert(0, path)

from app.services.event_bus import SUPPORTED_FILTER_KEYS  # noqa: E402
from app.worker.plugins.base import Plugin  # noqa: E402
from app.worker.plugins.manifest import Manifest  # noqa: E402

INCLUDED_EXAMPLES = {
    "event_bus_demo",
    "hello_ping",
    "with_ai",
    "with_ai_components",
    "with_http",
    "with_interaction",
    "webhook_receiver",
}
SKIPPED_EXAMPLES = {
    "translate": "历史示例仍依赖后端私有 LLM 链路，迁移到 ctx.ai 前不纳入稳定 API gate。",
}
REQUIRED_FILES = {"plugin.json", "manifest.py", "plugin.py", "__init__.py"}
REQUIRED_PERMISSIONS = {
    "with_ai": {"ai_text"},
    "with_ai_components": {"ai_text"},
    "with_http": {"external_http"},
    "webhook_receiver": {"send_message"},
}
EVENT_EXAMPLES = {"event_bus_demo", "hello_ping", "with_interaction", "webhook_receiver"}
NATIVE_RAW_EXAMPLES = {"event_bus_demo", "with_interaction"}
REQUIRED_EVENT_TYPES = {
    "event_bus_demo": {
        "message",
        "command",
        "callback_query",
        "inline_query",
        "chosen_inline_result",
        "payment_confirmed",
    },
    "hello_ping": {"message"},
    "with_interaction": {"message", "callback_query", "payment_confirmed"},
    "webhook_receiver": {"webhook"},
}
DEPRECATED_RISK_TOKENS = ("notice", "bbot_notice", "notice_bot", "raw_event")
DEPRECATED_RISK_PATTERN = re.compile(
    r"""(?P<quote>["'])(?:notice|bbot_notice|notice_bot|raw_event)(?P=quote)"""
)
DEPRECATED_SEND_VIA = {"notice", "bbot_notice", "notice_bot"}
EVENT_FIXTURES = {
    "message.json",
    "command.json",
    "callback_query.json",
    "inline_query.json",
    "chosen_inline_result.json",
    "payment_confirmed.json",
    "native_raw_telethon_message.json",
    "deprecated_notice_action.json",
}
EXPECTED_EVENT_ACTIONS = {
    "message.json": {"send_message"},
    "command.json": {"send_message"},
    "callback_query.json": {"answer_callback"},
    "inline_query.json": {"answer_inline_query"},
    "chosen_inline_result.json": {"result"},
    "payment_confirmed.json": {"settlement", "send_message"},
    "native_raw_telethon_message.json": {"send_message"},
}


def _load_plugin_json(plugin_dir: Path) -> dict[str, Any]:
    path = plugin_dir / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{path}: plugin.json 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"{path}: plugin.json 顶层必须是 object")
    return data


def _manifest_capabilities(manifest: Manifest) -> dict[str, Any]:
    capabilities = getattr(manifest, "capabilities", None)
    return dict(capabilities) if isinstance(capabilities, dict) else {}


def _declared_events(subscriptions: list[dict[str, Any]]) -> set[str]:
    events: set[str] = set()
    for subscription in subscriptions:
        raw_events = subscription.get("events")
        if isinstance(raw_events, list):
            events.update(str(item).strip() for item in raw_events if str(item).strip())
    return events


def _validate_usage(name: str, metadata: dict[str, Any]) -> None:
    usage = str(metadata.get("usage") or "").strip()
    if not usage:
        raise AssertionError(f"{name}: plugin.json 必须声明 usage，不能只依赖旧配置说明")


def _validate_event_contract(name: str, metadata: dict[str, Any], manifest: Manifest) -> None:
    metadata_subscriptions = metadata.get("event_subscriptions")
    if metadata_subscriptions is None:
        metadata_subscriptions = []
    if not isinstance(metadata_subscriptions, list):
        raise AssertionError(f"{name}: plugin.json.event_subscriptions 必须是数组")
    metadata_subscriptions = [item for item in metadata_subscriptions if isinstance(item, dict)]
    manifest_subscriptions = list(getattr(manifest, "event_subscriptions", []) or [])
    if metadata_subscriptions != manifest_subscriptions:
        raise AssertionError(f"{name}: plugin.json.event_subscriptions 与 MANIFEST.event_subscriptions 不一致")

    metadata_capabilities = metadata.get("capabilities")
    if metadata_capabilities is None:
        metadata_capabilities = {}
    if not isinstance(metadata_capabilities, dict):
        raise AssertionError(f"{name}: plugin.json.capabilities 必须是对象")
    if dict(metadata_capabilities) != _manifest_capabilities(manifest):
        raise AssertionError(f"{name}: plugin.json.capabilities 与 MANIFEST.capabilities 不一致")
    _warn_unknown_filter_keys(name, metadata_subscriptions)

    if name in EVENT_EXAMPLES:
        if not metadata_subscriptions:
            raise AssertionError(f"{name}: Event Bus 示例必须声明 event_subscriptions")
        missing_events = sorted(REQUIRED_EVENT_TYPES.get(name, set()) - _declared_events(metadata_subscriptions))
        if missing_events:
            raise AssertionError(f"{name}: event_subscriptions 缺少事件: {', '.join(missing_events)}")

        raw_capability = metadata_capabilities.get("telegram_native_raw")
        if name in NATIVE_RAW_EXAMPLES:
            if not isinstance(raw_capability, dict):
                raise AssertionError(f"{name}: capabilities.telegram_native_raw 必须显式声明为对象")
            if raw_capability.get("enabled") is True and not str(raw_capability.get("reason") or "").strip():
                raise AssertionError(f"{name}: telegram_native_raw.enabled=true 时必须说明 reason")
        direct_capability = metadata_capabilities.get("telegram_direct_passthrough")
        if isinstance(direct_capability, dict) and direct_capability.get("enabled") is True:
            if not str(direct_capability.get("reason") or "").strip():
                raise AssertionError(f"{name}: telegram_direct_passthrough.enabled=true 时必须说明 reason")


def _warn_unknown_filter_keys(name: str, subscriptions: list[dict[str, Any]]) -> None:
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
            print(f"warn: {name} - event_subscriptions[{index}] filters 含未知 key: {', '.join(unknown)}")


def _validate_deprecated_risks(name: str, plugin_dir: Path, metadata: dict[str, Any]) -> None:
    for path in sorted(plugin_dir.glob("*")):
        if path.suffix not in {".py", ".json", ".md"}:
            continue
        if path.name == "deprecated_notice_action.json":
            continue
        text = path.read_text(encoding="utf-8")
        match = DEPRECATED_RISK_PATTERN.search(text)
        if match:
            raise AssertionError(f"{name}: {path.name} 仍包含旧风险字段 {match.group(0)}")

    raw_entries = metadata.get("interaction_entries")
    if isinstance(raw_entries, list):
        for index, entry in enumerate(raw_entries, start=1):
            if not isinstance(entry, dict):
                continue
            contract = entry.get("result_contract") if isinstance(entry.get("result_contract"), dict) else {}
            send_via = contract.get("send_via")
            if isinstance(send_via, list) and any(str(item) in DEPRECATED_SEND_VIA for item in send_via):
                raise AssertionError(f"{name}: interaction_entries[{index}] result_contract.send_via 包含旧 notice 通道")


def _validate_event_fixtures(name: str, plugin_dir: Path) -> None:
    if name != "event_bus_demo":
        return
    fixtures_dir = plugin_dir / "fixtures"
    missing = sorted(file for file in EVENT_FIXTURES if not (fixtures_dir / file).is_file())
    if missing:
        raise AssertionError(f"{name}: fixtures 缺少 {', '.join(missing)}")
    for fixture in sorted(fixtures_dir.glob("*.json")):
        data = json.loads(fixture.read_text(encoding="utf-8"))
        if not isinstance(data.get("source"), dict):
            raise AssertionError(f"{name}: {fixture.name} 缺少 source 信封")
        if "native_raw_meta" not in data:
            raise AssertionError(f"{name}: {fixture.name} 缺少 native_raw_meta")
        if fixture.name == "native_raw_telethon_message.json":
            if not data["native_raw_meta"].get("enabled"):
                raise AssertionError(f"{name}: {fixture.name} 必须演示已声明 native_raw")
            if not isinstance(data.get("native_raw"), dict):
                raise AssertionError(f"{name}: {fixture.name} 必须包含 JSON 兼容 native_raw dict")
        if fixture.name == "deprecated_notice_action.json":
            expected = data.get("expected_action")
            if not isinstance(expected, dict):
                raise AssertionError(f"{name}: {fixture.name} 缺少 expected_action")
            if expected.get("send_via") not in DEPRECATED_SEND_VIA:
                raise AssertionError(f"{name}: {fixture.name} 必须使用废弃 send_via 作为探针")
            if expected.get("reason_code") != "send_channel_deprecated":
                raise AssertionError(f"{name}: {fixture.name} 必须声明 send_channel_deprecated 期望")


async def _run_on_event(plugin: Plugin, payload: dict[str, Any]) -> list[dict[str, Any]]:
    handler = getattr(plugin, "on_event", None)
    if not callable(handler):
        raise AssertionError(f"{plugin.key}: 缺少 on_event 示例入口")
    ctx = SimpleNamespace(messages=None, log=None)
    result = await handler(ctx, payload)
    if not isinstance(result, list):
        raise AssertionError(f"{plugin.key}: on_event 必须返回 action list")
    actions = [item for item in result if isinstance(item, dict)]
    if len(actions) != len(result):
        raise AssertionError(f"{plugin.key}: on_event 返回值必须全部是 action dict")
    return actions


def _validate_event_demo_runtime(name: str, plugin_dir: Path, plugin: Plugin) -> None:
    if name != "event_bus_demo":
        return
    fixtures_dir = plugin_dir / "fixtures"
    for fixture_name, expected_actions in EXPECTED_EVENT_ACTIONS.items():
        payload = json.loads((fixtures_dir / fixture_name).read_text(encoding="utf-8"))
        actions = asyncio.run(_run_on_event(plugin, payload))
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        synthetic_source = str(source.get("synthetic") or "").strip()
        action_types = {str(action.get("type") or "").strip() for action in actions}
        if not expected_actions <= action_types:
            raise AssertionError(
                f"{name}: {fixture_name} action 类型不足，期望 {sorted(expected_actions)}，实际 {sorted(action_types)}"
            )
        for action in actions:
            action_type = str(action.get("type") or "").strip()
            if action_type in {"send_message", "send_rich_message", "send_photo", "send_file"}:
                send_via = action.get("send_via")
                send_via_values = send_via if isinstance(send_via, list) else [send_via]
                if any(str(item) in DEPRECATED_SEND_VIA for item in send_via_values):
                    raise AssertionError(f"{name}: {fixture_name} 返回了旧 notice 发送通道")
            if action_type == "answer_inline_query" and not str(action.get("inline_query_id") or "").strip():
                raise AssertionError(f"{name}: {fixture_name} 缺少 inline_query_id")
            if action_type == "answer_callback" and not str(action.get("callback_query_id") or "").strip():
                if synthetic_source:
                    continue
                raise AssertionError(f"{name}: {fixture_name} 缺少 callback_query_id")

    deprecated_payload = json.loads((fixtures_dir / "deprecated_notice_action.json").read_text(encoding="utf-8"))
    expected = deprecated_payload.get("expected_action") or {}
    if expected.get("send_via") not in DEPRECATED_SEND_VIA:
        raise AssertionError(f"{name}: deprecated_notice_action.json 未覆盖旧 notice 通道")
    if expected.get("reason_code") != "send_channel_deprecated":
        raise AssertionError(f"{name}: deprecated_notice_action.json 未覆盖 send_channel_deprecated")


def _validate_hello_ping_runtime(name: str, plugin: Plugin) -> None:
    if name != "hello_ping":
        return
    ping_payload = {
        "source": {
            "type": "message",
            "channel": "interaction_bot",
            "account_id": 1,
            "chat_id": -100123,
            "message_id": 456,
        },
        "message": {
            "chat_id": -100123,
            "message_id": 456,
            "text": "ping",
        },
        "chat": {"id": -100123, "type": "supergroup"},
        "sender": {"user_id": 789, "display_name": "Alice"},
    }
    miss_payload = {
        **ping_payload,
        "message": {**ping_payload["message"], "text": "hello"},
    }
    actions = asyncio.run(_run_on_event(plugin, ping_payload))
    if len(actions) != 1:
        raise AssertionError(f"{name}: ping 必须只返回一条 action，实际 {len(actions)} 条")
    action = actions[0]
    if action.get("type") != "send_message" or action.get("text") != "pong":
        raise AssertionError(f"{name}: ping 必须返回 send_message/pong，实际 {action}")
    if action.get("chat_id") != -100123 or action.get("reply_to_message_id") != 456:
        raise AssertionError(f"{name}: ping 回复必须引用原会话和原消息")
    miss_actions = asyncio.run(_run_on_event(plugin, miss_payload))
    if miss_actions:
        raise AssertionError(f"{name}: 非 ping 文本不能返回 action")


def _validate_webhook_receiver_runtime(name: str, plugin_dir: Path, plugin: Plugin) -> None:
    if name != "webhook_receiver":
        return
    payload = json.loads(
        (plugin_dir / "fixtures/orders_paid.json").read_text(encoding="utf-8")
    )
    ctx = SimpleNamespace(
        config={"target_chat_id": -100123, "title": "订单事件"},
        log=None,
    )
    actions = asyncio.run(plugin.on_event(ctx, payload))
    if not isinstance(actions, list) or len(actions) != 1:
        raise AssertionError(f"{name}: default Webhook 必须返回一条消息动作")
    action = actions[0]
    if action.get("type") != "send_message" or action.get("chat_id") != -100123:
        raise AssertionError(f"{name}: Webhook 消息动作或目标 Chat ID 不正确: {action}")
    text = str(action.get("text") or "")
    if "default" not in text or "A-1001" not in text or "paid" not in text:
        raise AssertionError(f"{name}: Webhook 消息缺少 Hook 或订单摘要: {text}")


def _validate_example(name: str) -> None:
    plugin_dir = EXAMPLES_ROOT / name
    missing = sorted(file for file in REQUIRED_FILES if not (plugin_dir / file).is_file())
    if missing:
        raise AssertionError(f"{plugin_dir}: 缺少必要文件: {', '.join(missing)}")

    metadata = _load_plugin_json(plugin_dir)
    module = importlib.import_module(f"examples.plugins.{name}")

    manifest = getattr(module, "MANIFEST", None)
    plugin_cls = getattr(module, "PLUGIN_CLASS", None)
    if not isinstance(manifest, Manifest):
        raise AssertionError(f"{name}: MANIFEST 必须是 Manifest 实例")
    if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, Plugin):
        raise AssertionError(f"{name}: PLUGIN_CLASS 必须是 Plugin 子类")

    instance = plugin_cls()
    if not isinstance(instance, Plugin):
        raise AssertionError(f"{name}: PLUGIN_CLASS 无法实例化为 Plugin")

    plugin_json_key = metadata.get("name") or metadata.get("key")
    if plugin_json_key != manifest.key or plugin_cls.key != manifest.key:
        raise AssertionError(
            f"{name}: key 不一致: plugin.json={plugin_json_key!r}, "
            f"MANIFEST={manifest.key!r}, PLUGIN_CLASS={plugin_cls.key!r}"
        )
    if metadata.get("version") != manifest.version:
        raise AssertionError(f"{name}: plugin.json.version 与 MANIFEST.version 不一致")
    if metadata.get("category") != manifest.category:
        raise AssertionError(f"{name}: plugin.json.category 与 MANIFEST.category 不一致")
    if metadata.get("interaction_profile") != manifest.interaction_profile:
        raise AssertionError(
            f"{name}: plugin.json.interaction_profile 与 MANIFEST.interaction_profile 不一致"
        )
    if list(metadata.get("interaction_entries") or []) != list(manifest.interaction_entries):
        raise AssertionError(f"{name}: plugin.json.interaction_entries 与 MANIFEST.interaction_entries 不一致")
    if name == "webhook_receiver":
        metadata_schema = metadata.get("config_schema")
        if not isinstance(metadata_schema, dict) or metadata_schema != manifest.config_schema:
            raise AssertionError(
                f"{name}: plugin.json.config_schema 与 MANIFEST.config_schema 不一致"
            )
    _validate_usage(name, metadata)
    _validate_event_contract(name, metadata, manifest)
    _validate_deprecated_risks(name, plugin_dir, metadata)
    _validate_event_fixtures(name, plugin_dir)
    _validate_event_demo_runtime(name, plugin_dir, instance)
    _validate_hello_ping_runtime(name, instance)
    _validate_webhook_receiver_runtime(name, plugin_dir, instance)

    for field in ("permissions", "allowed_hosts"):
        expected = list(metadata.get(field) or [])
        actual = list(getattr(manifest, field))
        if expected != actual:
            raise AssertionError(f"{name}: plugin.json.{field} 与 MANIFEST.{field} 不一致")

    missing_permissions = sorted(REQUIRED_PERMISSIONS.get(name, set()) - set(manifest.permissions))
    if missing_permissions:
        raise AssertionError(f"{name}: 缺少必要权限: {', '.join(missing_permissions)}")

    print(f"ok: {name}")


def main() -> int:
    if not EXAMPLES_ROOT.is_dir():
        raise AssertionError(f"示例目录不存在: {EXAMPLES_ROOT}")

    present = {path.name for path in EXAMPLES_ROOT.iterdir() if path.is_dir()}
    unexpected = sorted(present - INCLUDED_EXAMPLES - set(SKIPPED_EXAMPLES))
    if unexpected:
        raise AssertionError(
            "发现未分类的插件示例，请加入 INCLUDED_EXAMPLES 或 SKIPPED_EXAMPLES: "
            + ", ".join(unexpected)
        )

    for name in sorted(INCLUDED_EXAMPLES):
        if name not in present:
            raise AssertionError(f"INCLUDED_EXAMPLES 中的示例不存在: {name}")
        _validate_example(name)

    for name in sorted(SKIPPED_EXAMPLES):
        if name in present:
            print(f"skip: {name} - {SKIPPED_EXAMPLES[name]}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"plugin example validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
