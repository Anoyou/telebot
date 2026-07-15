"""插件中心只读聚合服务。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.feature import AccountFeature
from ..db.models.log import EventTrace, PluginRuntimeStatus
from ..db.models.plugin import InstalledPlugin
from ..schemas.plugin_center import (
    PluginCenterAccountItem,
    PluginCenterItem,
    PluginCenterLoadError,
    PluginCenterTraceRef,
    PluginCenterUpdateStatus,
)
from . import feature_service
from .redactor import redact_text


def _normalize_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _redact_text(value: Any) -> str | None:
    text = _normalize_text(value)
    if text is None:
        return None
    redacted = redact_text(text)
    return redacted or None


def _trace_ref(
    trace_id: str | None,
    account_id: int | None,
    trace_by_id: dict[str, EventTrace],
) -> PluginCenterTraceRef | None:
    trace_key = _normalize_text(trace_id)
    if trace_key is None:
        return None
    trace = trace_by_id.get(trace_key)
    return PluginCenterTraceRef(
        trace_id=trace_key,
        account_id=account_id if trace is None else trace.account_id,
        status=None if trace is None else trace.status,
        event_type=None if trace is None else trace.event_type,
        source_channel=None if trace is None else trace.source_channel,
        started_at=None if trace is None else trace.started_at,
    )


def _pick_recent_load_error(
    installed: InstalledPlugin,
    runtime_rows: list[PluginRuntimeStatus],
) -> PluginCenterLoadError | None:
    for row in runtime_rows:
        message = _redact_text(row.last_load_error)
        if message is None:
            continue
        return PluginCenterLoadError(
            source="plugin_runtime_status",
            account_id=row.account_id,
            load_status=_normalize_text(row.load_status),
            message=message,
            updated_at=row.updated_at,
        )

    installed_error = _redact_text(installed.last_load_error)
    if installed_error is None:
        return None
    return PluginCenterLoadError(
        source="installed_plugin",
        message=installed_error,
        updated_at=installed.updated_at,
    )


def _pick_recent_trace(
    runtime_rows: list[PluginRuntimeStatus],
    trace_by_id: dict[str, EventTrace],
) -> PluginCenterTraceRef | None:
    for row in runtime_rows:
        ref = _trace_ref(row.last_trace_id, row.account_id, trace_by_id)
        if ref is not None:
            return ref
    return None


async def list_installed_plugins_overview(db: AsyncSession) -> list[PluginCenterItem]:
    installed_rows = (
        await db.execute(select(InstalledPlugin).order_by(InstalledPlugin.key.asc()))
    ).scalars().all()
    if not installed_rows:
        return []

    plugin_keys = [str(row.key) for row in installed_rows if str(row.key or "").strip()]
    matrix = await feature_service.feature_matrix(db)
    feature_info_by_key = {
        str(item.get("key")): item
        for item in matrix.get("features", [])
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }
    matrix_accounts = [
        item
        for item in matrix.get("accounts", [])
        if isinstance(item, dict)
    ]

    account_feature_rows = (
        await db.execute(
            select(AccountFeature).where(AccountFeature.feature_key.in_(plugin_keys))
        )
    ).scalars().all()
    account_feature_by_key = {
        (int(row.account_id), str(row.feature_key)): row for row in account_feature_rows
    }

    runtime_rows = (
        await db.execute(
            select(PluginRuntimeStatus)
            .where(PluginRuntimeStatus.plugin_key.in_(plugin_keys))
            .order_by(desc(PluginRuntimeStatus.updated_at), desc(PluginRuntimeStatus.id))
        )
    ).scalars().all()

    runtime_by_plugin: dict[str, list[PluginRuntimeStatus]] = defaultdict(list)
    runtime_by_plugin_account: dict[tuple[str, int], PluginRuntimeStatus] = {}
    trace_ids: list[str] = []
    seen_trace_ids: set[str] = set()

    for row in runtime_rows:
        plugin_key = str(row.plugin_key or "").strip()
        if not plugin_key:
            continue
        runtime_by_plugin[plugin_key].append(row)
        if row.account_id is not None:
            runtime_by_plugin_account.setdefault((plugin_key, int(row.account_id)), row)
        trace_id = _normalize_text(row.last_trace_id)
        if trace_id is not None and trace_id not in seen_trace_ids:
            seen_trace_ids.add(trace_id)
            trace_ids.append(trace_id)

    trace_rows: list[EventTrace] = []
    if trace_ids:
        trace_rows = (
            await db.execute(
                select(EventTrace)
                .where(EventTrace.trace_id.in_(trace_ids))
                .order_by(desc(EventTrace.started_at), desc(EventTrace.id))
            )
        ).scalars().all()
    trace_by_id = {str(row.trace_id): row for row in trace_rows}

    items: list[PluginCenterItem] = []
    for installed in installed_rows:
        key = str(installed.key or "").strip()
        feature_info = feature_info_by_key.get(key, {})
        runtime_group = runtime_by_plugin.get(key, [])
        display_name = _normalize_text(feature_info.get("display_name"))
        if display_name is None and isinstance(installed.manifest_json, dict):
            display_name = _normalize_text(installed.manifest_json.get("display_name"))
        display_name = display_name or key

        accounts: list[PluginCenterAccountItem] = []
        for account in matrix_accounts:
            account_id = account.get("id")
            if not isinstance(account_id, int):
                continue
            feature_states = account.get("features")
            feature_enabled = account.get("feature_enabled")
            feature_states = feature_states if isinstance(feature_states, dict) else {}
            feature_enabled = feature_enabled if isinstance(feature_enabled, dict) else {}
            account_feature = account_feature_by_key.get((account_id, key))
            runtime_row = runtime_by_plugin_account.get((key, account_id))
            accounts.append(
                PluginCenterAccountItem(
                    account_id=account_id,
                    account_name=str(account.get("name") or account_id),
                    enabled=bool(
                        feature_enabled.get(
                            key,
                            getattr(account_feature, "enabled", False),
                        )
                    ),
                    state=str(
                        feature_states.get(
                            key,
                            getattr(account_feature, "state", "disabled"),
                        )
                        or "disabled"
                    ),
                    last_error=_normalize_text(getattr(account_feature, "last_error", None)),
                    load_status=_normalize_text(
                        None if runtime_row is None else runtime_row.load_status
                    ),
                    last_load_error=_redact_text(
                        None if runtime_row is None else runtime_row.last_load_error
                    ),
                    last_trace=(
                        None
                        if runtime_row is None
                        else _trace_ref(runtime_row.last_trace_id, runtime_row.account_id, trace_by_id)
                    ),
                )
            )

        items.append(
            PluginCenterItem(
                key=key,
                display_name=display_name,
                source=str(installed.source or ""),
                source_url=_normalize_text(installed.source_url),
                source_label=_normalize_text(
                    feature_info.get("source_label") or installed.source_label or installed.source
                ),
                version=_normalize_text(feature_info.get("version") or installed.version),
                global_enabled=bool(installed.enabled),
                signature_ok=installed.signature_ok,
                trust_tier=_normalize_text(installed.trust_tier),
                lint_warnings=[
                    item for item in (installed.lint_warnings or []) if isinstance(item, str)
                ],
                update=PluginCenterUpdateStatus(
                    update_available=bool(feature_info.get("update_available", False)),
                    latest_version=_normalize_text(feature_info.get("latest_version")),
                    last_update_check_at=feature_info.get("last_update_check_at"),
                    last_update_check_error=_redact_text(
                        feature_info.get("last_update_check_error")
                    ),
                ),
                accounts=accounts,
                recent_load_error=_pick_recent_load_error(installed, runtime_group),
                recent_trace=_pick_recent_trace(runtime_group, trace_by_id),
            )
        )

    return items


__all__ = ["list_installed_plugins_overview"]
