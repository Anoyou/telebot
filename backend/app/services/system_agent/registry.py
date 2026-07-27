"""ToolSpec 注册表与角色/渠道过滤。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .context import ToolContext

ReadHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]
PreviewHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]
ExecuteHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]
# 事务外预检（如 Provider 上游验证）；失败可保持 Action pending
PrecheckHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


class ActionKeepPendingError(Exception):
    """预检失败：清除无效密文、Action 保持 pending，允许重新输入。"""

    def __init__(self, message: str, *, code: str = "PRECHECK_FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.message = message

ROLE_ORDER = {"viewer": 0, "operator": 1, "admin": 2}


def role_at_least(current: str, minimum: str) -> bool:
    return ROLE_ORDER.get(str(current or "").lower(), -1) >= ROLE_ORDER.get(
        str(minimum or "").lower(), 99
    )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = True
    min_role: str = "viewer"
    risk: str = "normal"  # normal | dangerous
    channels: tuple[str, ...] = ("web", "bot")
    read_handler: ReadHandler | None = None
    preview_handler: PreviewHandler | None = None
    execute_handler: ExecuteHandler | None = None
    precheck_handler: PrecheckHandler | None = None
    secret_argument_names: tuple[str, ...] = ()
    runtime_effects: tuple[str, ...] = ()
    diagnostic_safe: bool = False
    available: bool = True
    unavailable_reason: str | None = None

    def for_llm(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }


@dataclass
class ToolRegistry:
    """内存工具注册表。"""

    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name required")
        if spec.read_only and spec.read_handler is None:
            raise ValueError(f"read-only tool {spec.name} requires read_handler")
        if not spec.read_only:
            if spec.preview_handler is None or spec.execute_handler is None:
                raise ValueError(f"write tool {spec.name} requires preview and execute handlers")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def unregister(self, name: str) -> bool:
        """移除动态工具；不存在时返回 False。"""

        return self._tools.pop(str(name), None) is not None

    def list_all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def list_for(
        self,
        *,
        channel: str,
        role: str,
        read_only_only: bool = False,
    ) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for spec in self._tools.values():
            if not spec.available:
                continue
            if channel not in spec.channels:
                continue
            if not role_at_least(role, spec.min_role):
                continue
            if read_only_only and not spec.read_only:
                continue
            out.append(spec)
        return sorted(out, key=lambda s: s.name)

    def capabilities(
        self,
        *,
        channel: str,
        role: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for spec in sorted(self._tools.values(), key=lambda s: s.name):
            allowed = (
                channel in spec.channels
                and role_at_least(role, spec.min_role)
                and spec.available
            )
            source = "builtin"
            plugin_key: str | None = None
            if spec.name.startswith("plugin_") and "." in spec.name:
                source = "plugin"
                # plugin_{key}.{tool}
                rest = spec.name[len("plugin_") :]
                plugin_key = rest.split(".", 1)[0]
            items.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "read_only": spec.read_only,
                    "min_role": spec.min_role,
                    "risk": spec.risk,
                    "channels": list(spec.channels),
                    "available": allowed,
                    "source": source,
                    "plugin_key": plugin_key,
                    "unavailable_reason": None
                    if allowed
                    else (
                        spec.unavailable_reason
                        or (
                            "角色或渠道不足"
                            if channel in spec.channels
                            else "当前渠道不可用"
                        )
                    ),
                }
            )
        return items


_REGISTRY: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        from .tools import register_all_tools

        reg = ToolRegistry()
        register_all_tools(reg)
        _REGISTRY = reg
    return _REGISTRY


def reset_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "ActionKeepPendingError",
    "ToolRegistry",
    "ToolSpec",
    "get_registry",
    "reset_registry_for_tests",
    "role_at_least",
]
