"""插件安装生命周期历史的统一写入入口。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.plugin import InstalledPlugin, PluginInstallHistory


def record_plugin_install_history(
    db: AsyncSession,
    *,
    row: InstalledPlugin,
    event_type: str,
    previous_version: str | None = None,
    detail: str | None = None,
) -> PluginInstallHistory:
    """只保存状态摘要，不复制 manifest、配置或任何可复用凭据。"""

    event = PluginInstallHistory(
        plugin_key=row.key,
        event_type=event_type,
        version=row.version,
        previous_version=previous_version,
        source=row.source,
        source_label=row.source_label,
        enabled=bool(row.enabled),
        signature_ok=row.signature_ok,
        detail=(detail or "")[:1000] or None,
    )
    db.add(event)
    return event


__all__ = ["record_plugin_install_history"]
