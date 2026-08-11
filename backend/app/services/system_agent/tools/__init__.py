"""注册全部 System Agent 工具。

阶段 1 仅注册只读工具；写工具从阶段 2 起加入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..registry import ToolRegistry


def register_all_tools(registry: ToolRegistry) -> None:
    from . import (
        access,
        account_bots,
        accounts,
        capabilities,
        commands,
        config_bundles,
        connectivity,
        dispatch,
        features,
        interaction,
        ledger,
        logs,
        memory,
        message_templates,
        notifications,
        plugin_data,
        plugin_repos,
        plugins,
        product,
        providers,
        routing,
        rules,
        safety,
        scheduler,
        source,
        system,
        system_ops,
        system_settings,
        usage,
        web,
        webhooks,
    )

    system.register(registry)
    system_ops.register(registry)
    usage.register(registry)
    access.register(registry)
    account_bots.register(registry)
    accounts.register(registry)
    capabilities.register(registry)
    interaction.register(registry)
    rules.register(registry)
    safety.register(registry)
    scheduler.register(registry)
    system_settings.register(registry)
    providers.register(registry)
    commands.register(registry)
    config_bundles.register(registry)
    connectivity.register(registry)
    dispatch.register(registry)
    routing.register(registry)
    features.register(registry)
    plugin_data.register(registry)
    plugins.register(registry)
    product.register(registry)
    plugin_repos.register(registry)
    logs.register(registry)
    source.register(registry)
    web.register(registry)
    webhooks.register(registry)
    ledger.register(registry)
    memory.register(registry)
    message_templates.register(registry)
    notifications.register(registry)
    # 插件工具插槽：从磁盘扫描已安装插件的 expose:system_agent 声明
    try:
        from ..plugin_tools import register_plugin_tools_from_disk

        register_plugin_tools_from_disk(registry)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).debug("register plugin tools from disk failed", exc_info=True)


__all__ = ["register_all_tools"]
