"""ToolSpec 注册表与角色/渠道过滤。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .context import ToolContext

ReadHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class PreparedAction:
    """Preview 可显式返回规范化后的 Action 参数，避免依赖原地修改入参。"""

    arguments: dict[str, Any]
    preview: dict[str, Any]


PreviewHandler = Callable[
    [ToolContext, dict[str, Any]], Awaitable[dict[str, Any] | PreparedAction]
]
ExecuteHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]
# 事务外预检（如 Provider 上游验证）；失败可保持 Action pending
PrecheckHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


class ActionKeepPendingError(Exception):
    """预检失败：Action 保持 pending；只按明确声明清除已判无效的密文。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "PRECHECK_FAILED",
        clear_secret_names: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.clear_secret_names = tuple(clear_secret_names)

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
    precheck_clear_secret_argument_names: tuple[str, ...] | None = None
    allow_secret_input: bool = True
    runtime_effects: tuple[str, ...] = ()
    # 仅当所有提交后副作用都可安全重复时才允许用户手动重试。
    # 真实消息发送、规则立即执行、插件动作等非幂等操作必须关闭，
    # 避免“副作用已成功、结果落库失败”时再次执行。
    runtime_retryable: bool = True
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
    _generation: int = 0
    _list_cache: dict[tuple[str, str, bool, int], tuple[ToolSpec, ...]] = field(
        default_factory=dict
    )

    @property
    def generation(self) -> int:
        return self._generation

    def _invalidate(self) -> None:
        self._generation += 1
        self._list_cache.clear()

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name required")
        if spec.read_only and spec.read_handler is None:
            raise ValueError(f"read-only tool {spec.name} requires read_handler")
        if not spec.read_only:
            if spec.preview_handler is None or spec.execute_handler is None:
                raise ValueError(f"write tool {spec.name} requires preview and execute handlers")
        self._tools[spec.name] = spec
        self._invalidate()

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def unregister(self, name: str) -> bool:
        """移除动态工具；不存在时返回 False。"""

        removed = self._tools.pop(str(name), None) is not None
        if removed:
            self._invalidate()
        return removed

    def list_all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def list_for(
        self,
        *,
        channel: str,
        role: str,
        read_only_only: bool = False,
    ) -> list[ToolSpec]:
        cache_key = (channel, role, bool(read_only_only), self._generation)
        cached = self._list_cache.get(cache_key)
        if cached is not None:
            return list(cached)
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
        result = tuple(sorted(out, key=lambda s: s.name))
        self._list_cache[cache_key] = result
        return list(result)

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
            if (
                spec.name.startswith("plugin_")
                and not spec.name.startswith("plugin_repos.")
                and "." in spec.name
            ):
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
    "PreparedAction",
    "ToolRegistry",
    "ToolSpec",
    "get_registry",
    "reset_registry_for_tests",
    "role_at_least",
]
