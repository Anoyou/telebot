"""注册全部 System Agent 工具。

阶段 1 仅注册只读工具；写工具从阶段 2 起加入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..registry import ToolRegistry


def register_all_tools(registry: ToolRegistry) -> None:
    from . import (
        accounts,
        commands,
        features,
        interaction,
        ledger,
        logs,
        memory,
        plugin_repos,
        plugins,
        providers,
        routing,
        rules,
        scheduler,
        system,
        system_ops,
    )

    system.register(registry)
    system_ops.register(registry)
    accounts.register(registry)
    interaction.register(registry)
    rules.register(registry)
    scheduler.register(registry)
    providers.register(registry)
    commands.register(registry)
    routing.register(registry)
    features.register(registry)
    plugins.register(registry)
    plugin_repos.register(registry)
    logs.register(registry)
    ledger.register(registry)
    memory.register(registry)


__all__ = ["register_all_tools"]
