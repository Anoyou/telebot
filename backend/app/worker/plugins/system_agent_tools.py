"""Worker 侧：插件暴露给 System Agent 的只读工具 handler 注册表。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

# (plugin_key, tool_name) -> async handler(ctx, arguments) -> Any
SystemAgentToolHandler = Callable[[Any, dict[str, Any]], Awaitable[Any]]

_HANDLERS: dict[tuple[str, str], SystemAgentToolHandler] = {}
# plugin_key -> live Plugin instance（on_startup 时登记，供无独立 handler 时委托）
_PLUGIN_INSTANCES: dict[str, Any] = {}


def register_system_agent_tool_handler(
    plugin_key: str,
    tool_name: str,
    handler: SystemAgentToolHandler,
) -> None:
    key = (str(plugin_key), str(tool_name))
    _HANDLERS[key] = handler
    log.debug("registered system_agent tool handler %s.%s", plugin_key, tool_name)


def unregister_plugin_system_agent_tools(plugin_key: str) -> None:
    prefix = str(plugin_key)
    for key in list(_HANDLERS):
        if key[0] == prefix:
            _HANDLERS.pop(key, None)
    _PLUGIN_INSTANCES.pop(prefix, None)


def bind_plugin_instance(plugin_key: str, instance: Any) -> None:
    _PLUGIN_INSTANCES[str(plugin_key)] = instance


def unbind_plugin_instance(plugin_key: str) -> None:
    _PLUGIN_INSTANCES.pop(str(plugin_key), None)


async def invoke_system_agent_tool(
    *,
    plugin_key: str,
    tool_name: str,
    arguments: dict[str, Any],
    plugin_context: Any | None = None,
) -> Any:
    """执行已注册 handler；若插件实例实现 system_agent_tool(name, args) 也可委托。"""

    key = (str(plugin_key), str(tool_name))
    handler = _HANDLERS.get(key)
    if handler is not None:
        return await handler(plugin_context, dict(arguments or {}))

    inst = _PLUGIN_INSTANCES.get(str(plugin_key))
    if inst is not None:
        method = getattr(inst, "system_agent_tool", None)
        if callable(method):
            return await method(tool_name, dict(arguments or {}), plugin_context)
        named = getattr(inst, f"system_agent_{tool_name}", None)
        if callable(named):
            return await named(dict(arguments or {}), plugin_context)

    raise LookupError(f"未注册 system_agent 工具 {plugin_key}.{tool_name}")


__all__ = [
    "bind_plugin_instance",
    "invoke_system_agent_tool",
    "register_system_agent_tool_handler",
    "unbind_plugin_instance",
    "unregister_plugin_system_agent_tools",
]
