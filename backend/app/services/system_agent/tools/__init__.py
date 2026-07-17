"""注册全部 System Agent 工具。

阶段 1 仅注册只读工具；写工具从阶段 2 起加入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..registry import ToolRegistry


def register_all_tools(registry: ToolRegistry) -> None:
    from . import accounts, commands, features, interaction, ledger, logs, providers, rules, scheduler, system

    system.register(registry)
    accounts.register(registry)
    interaction.register(registry)
    rules.register(registry)
    scheduler.register(registry)
    providers.register(registry)
    commands.register(registry)
    features.register(registry)
    logs.register(registry)
    ledger.register(registry)


__all__ = ["register_all_tools"]
