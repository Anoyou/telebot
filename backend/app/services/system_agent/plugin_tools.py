"""插件工具插槽：manifest.agent_tools → System Agent 动态注册 + worker IPC 执行。

第一期硬约束：仅暴露只读工具（read_only=True）；写语义条目拒绝暴露。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.feature import AccountFeature
from ...db.models.plugin import InstalledPlugin
from ...redis_client import get_redis
from ...worker.ipc import CMD_AGENT_PLUGIN_TOOL, IPCMessage, cmd_channel, make_cmd
from .context import ToolContext
from .registry import ToolRegistry, ToolSpec, get_registry
from .tool_routing import register_dynamic_domain, unregister_dynamic_domains
from .tools._helpers import mark_external_text

log = logging.getLogger(__name__)

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
_PLUGIN_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_MAX_TOOLS_PER_PLUGIN = 5
_IPC_TIMEOUT_SECONDS = 10.0
_DYNAMIC_PREFIX = "plugin_"

# 进程内记录当前动态工具名，便于卸载时清理
_DYNAMIC_TOOL_NAMES: set[str] = set()
_DYNAMIC_DOMAINS: set[str] = set()


def exposed_tool_name(plugin_key: str, tool_name: str) -> str:
    return f"{_DYNAMIC_PREFIX}{plugin_key}.{tool_name}"


def parse_exposed_tools(
    plugin_key: str,
    manifest: dict[str, Any] | None,
    *,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """从 manifest 提取应暴露给 system_agent 的只读工具声明。"""

    warn = warnings if warnings is not None else []
    if not isinstance(manifest, dict):
        return []
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        return []
    agent_cap = capabilities.get("agent_tools")
    if agent_cap is not True and not (
        isinstance(agent_cap, dict) and agent_cap.get("enabled") is True
    ):
        return []

    raw_tools = manifest.get("agent_tools")
    if not isinstance(raw_tools, list):
        return []

    if not _PLUGIN_KEY_RE.fullmatch(str(plugin_key or "")):
        warn.append(f"plugin_key={plugin_key!r} 不合规，跳过 system_agent 暴露")
        return []

    description = str(manifest.get("description") or manifest.get("display_name") or plugin_key)
    keywords_raw = manifest.get("agent_keywords")
    keywords: list[str] = []
    if isinstance(keywords_raw, list):
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()][:6]

    out: list[dict[str, Any]] = []
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        expose = raw.get("expose")
        if not isinstance(expose, list) or "system_agent" not in expose:
            continue
        name = str(raw.get("name") or "").strip()
        if not _TOOL_NAME_RE.fullmatch(name):
            warn.append(f"{plugin_key}.{name or '?'}: 工具名不合规，静默跳过")
            continue
        parameters = raw.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            warn.append(f"{plugin_key}.{name}: parameters 必须是 type=object，静默跳过")
            continue
        read_only = bool(raw.get("read_only", True))
        if not read_only:
            warn.append(
                f"{plugin_key}.{name}: 声明写语义，第一期拒绝暴露给 system_agent"
            )
            continue
        if len(out) >= _MAX_TOOLS_PER_PLUGIN:
            warn.append(f"{plugin_key}: 超过每插件暴露上限 {_MAX_TOOLS_PER_PLUGIN}，其余跳过")
            break
        out.append(
            {
                "plugin_key": plugin_key,
                "tool_name": name,
                "full_name": exposed_tool_name(plugin_key, name),
                "description": str(raw.get("description") or name).strip()[:500],
                "parameters": dict(parameters),
                "plugin_description": description[:200],
                "agent_keywords": keywords,
            }
        )
    return out


async def list_installed_exposed_tools(db: AsyncSession) -> tuple[list[dict[str, Any]], list[str]]:
    """扫描已安装且启用的插件，返回暴露工具声明与安装警告。"""

    result = await db.execute(
        select(InstalledPlugin).where(InstalledPlugin.enabled.is_(True))
    )
    rows = list(result.scalars().all())
    tools: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in rows:
        tools.extend(
            parse_exposed_tools(
                str(row.key),
                row.manifest_json if isinstance(row.manifest_json, dict) else None,
                warnings=warnings,
            )
        )
    return tools, warnings


def _make_read_handler(plugin_key: str, tool_name: str):
    async def _handler(ctx: ToolContext, args: dict[str, Any]) -> Any:
        account_id = ctx.account_id
        if account_id is None:
            try:
                account_id = int(args.get("account_id")) if args.get("account_id") is not None else None
            except (TypeError, ValueError):
                account_id = None
        if account_id is None:
            return {
                "error": "account_id_required",
                "message": "调用插件工具需要 account_id",
                "business_changed": False,
            }
        # AccountFeature 必须已启用该插件
        q = await ctx.db.execute(
            select(AccountFeature).where(
                AccountFeature.account_id == int(account_id),
                AccountFeature.feature_key == plugin_key,
                AccountFeature.enabled.is_(True),
            )
        )
        feat = q.scalar_one_or_none()
        if feat is None:
            return {
                "error": "plugin_not_enabled",
                "message": f"账号 #{account_id} 未启用插件 {plugin_key}",
                "business_changed": False,
            }

        result = await invoke_plugin_tool_via_ipc(
            account_id=int(account_id),
            plugin_key=plugin_key,
            tool_name=tool_name,
            arguments=dict(args or {}),
        )
        return _sanitize_plugin_result(result)

    return _handler


def _sanitize_plugin_result(value: Any) -> Any:
    """结果过 mark_external_text，防注入。"""

    if isinstance(value, str):
        return mark_external_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, str):
                out[str(k)] = mark_external_text(v)
            elif isinstance(v, (dict, list)):
                out[str(k)] = _sanitize_plugin_result(v)
            else:
                out[str(k)] = v
        out.setdefault("business_changed", False)
        return out
    if isinstance(value, list):
        return [_sanitize_plugin_result(item) for item in value]
    return value


async def invoke_plugin_tool_via_ipc(
    *,
    account_id: int,
    plugin_key: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = _IPC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """主进程 → worker IPC 执行插件工具；超时/失败返回结构化错误。"""

    try:
        redis = get_redis()
    except Exception as exc:  # noqa: BLE001
        return {
            "error": "no_redis",
            "message": f"Redis 不可用：{type(exc).__name__}",
            "business_changed": False,
        }

    reply_channel = (
        f"worker_reply:{int(account_id)}:agent_plugin_tool:{secrets.token_hex(8)}"
    )
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_channel)
        subscribers = await redis.publish(
            cmd_channel(int(account_id)),
            make_cmd(
                CMD_AGENT_PLUGIN_TOOL,
                plugin_key=plugin_key,
                tool_name=tool_name,
                arguments=arguments,
                reply_to=reply_channel,
            ),
        )
        if int(subscribers or 0) <= 0:
            return {
                "error": "worker_offline",
                "message": f"账号 #{account_id} Worker 未在线",
                "business_changed": False,
            }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(timeout))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {
                    "error": "timeout",
                    "message": f"插件工具超时（{timeout:.0f}s）",
                    "business_changed": False,
                }
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                    timeout=remaining + 0.1,
                )
            except TimeoutError:
                return {
                    "error": "timeout",
                    "message": f"插件工具超时（{timeout:.0f}s）",
                    "business_changed": False,
                }
            if not msg or msg.get("type") != "message":
                continue
            payload = IPCMessage.decode(msg["data"]).payload
            if not bool(payload.get("ok")):
                return {
                    "error": str(payload.get("error") or "plugin_tool_failed"),
                    "message": str(payload.get("message") or "插件工具执行失败")[:500],
                    "business_changed": False,
                }
            result = payload.get("result")
            if isinstance(result, dict):
                return result
            return {"result": result, "business_changed": False}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "invoke plugin tool failed plugin=%s tool=%s",
            plugin_key,
            tool_name,
            exc_info=True,
        )
        return {
            "error": type(exc).__name__,
            "message": str(exc)[:500],
            "business_changed": False,
        }
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close is not None:
                ret = close()
                if hasattr(ret, "__await__"):
                    await ret


def clear_dynamic_plugin_tools(registry: ToolRegistry | None = None) -> None:
    """移除全部动态插件工具与域。"""

    global _DYNAMIC_TOOL_NAMES, _DYNAMIC_DOMAINS
    reg = registry or get_registry()
    for name in list(_DYNAMIC_TOOL_NAMES):
        reg.unregister(name)
    unregister_dynamic_domains(_DYNAMIC_DOMAINS)
    _DYNAMIC_TOOL_NAMES.clear()
    _DYNAMIC_DOMAINS.clear()


def apply_exposed_tools_to_registry(
    registry: ToolRegistry,
    tools: list[dict[str, Any]],
) -> None:
    """把暴露工具写入注册表与 DOMAIN_CATALOG 动态域。"""

    clear_dynamic_plugin_tools(registry)
    by_plugin: dict[str, list[dict[str, Any]]] = {}
    for item in tools:
        by_plugin.setdefault(str(item["plugin_key"]), []).append(item)

    for plugin_key, items in by_plugin.items():
        domain = f"{_DYNAMIC_PREFIX}{plugin_key}"
        desc = str(items[0].get("plugin_description") or plugin_key)
        keywords = tuple(items[0].get("agent_keywords") or ()) or (plugin_key,)
        register_dynamic_domain(domain, desc, keywords)
        _DYNAMIC_DOMAINS.add(domain)
        for item in items:
            full_name = str(item["full_name"])
            tool_name = str(item["tool_name"])
            registry.register(
                ToolSpec(
                    name=full_name,
                    description=f"[插件 {plugin_key}] {item['description']}",
                    input_schema=dict(item["parameters"]),
                    read_only=True,
                    min_role="viewer",
                    channels=("web", "bot"),
                    read_handler=_make_read_handler(plugin_key, tool_name),
                )
            )
            _DYNAMIC_TOOL_NAMES.add(full_name)


def register_plugin_tools_from_disk(registry: ToolRegistry | None = None) -> list[str]:
    """从 plugins/installed 扫描 manifest（无 DB 时的启动兜底）。"""

    from ...settings import settings

    reg = registry or get_registry()
    root = getattr(settings, "plugins_installed_dir", None) or "./plugins/installed"
    from pathlib import Path

    base = Path(root)
    tools: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not base.is_dir():
        apply_exposed_tools_to_registry(reg, [])
        return warnings
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        manifest: dict[str, Any] | None = None
        for name in ("plugin.json", "manifest.json"):
            path = child / name
            if path.is_file():
                try:
                    import json

                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        manifest = data
                        break
                except Exception:  # noqa: BLE001
                    warnings.append(f"{child.name}: 读取 {name} 失败")
        if manifest is None:
            # 尝试 manifest.py 不在此处执行；仅 JSON
            continue
        key = str(manifest.get("key") or child.name)
        tools.extend(parse_exposed_tools(key, manifest, warnings=warnings))
    apply_exposed_tools_to_registry(reg, tools)
    return warnings


async def refresh_plugin_system_agent_tools(db: AsyncSession) -> list[str]:
    """从 DB 重建动态插件工具段；返回警告列表。"""

    tools, warnings = await list_installed_exposed_tools(db)
    apply_exposed_tools_to_registry(get_registry(), tools)
    for w in warnings:
        log.warning("plugin system_agent expose: %s", w)
    return warnings


__all__ = [
    "apply_exposed_tools_to_registry",
    "clear_dynamic_plugin_tools",
    "exposed_tool_name",
    "invoke_plugin_tool_via_ipc",
    "list_installed_exposed_tools",
    "parse_exposed_tools",
    "refresh_plugin_system_agent_tools",
]
