from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import plugins_install as plugins_install_api
from app.db.models.feature import AccountFeature
from app.db.models.log import EventTrace, PluginRuntimeStatus
from app.db.models.plugin import InstalledPlugin
from app.deps import get_current_user, get_db
from app.main import app
from app.schemas.plugin_center import (
    PluginCenterAccountItem,
    PluginCenterItem,
    PluginCenterLoadError,
    PluginCenterTraceRef,
    PluginCenterUpdateStatus,
)
from app.services import plugin_center_service as pcs
from app.services.plugin_capability_requirements import (
    MISSING_DECLARATION_WARNING,
    PluginCapabilityRequirement,
)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


def _csrf_headers() -> dict[str, str]:
    return {
        "X-Requested-With": "telepilot-ui",
        "X-CSRF-Token": "test-token",
        "Cookie": "csrf_token=test-token",
    }


@pytest.mark.asyncio
async def test_list_installed_plugins_overview_aggregates_matrix_runtime_and_trace(monkeypatch) -> None:
    checked_at = datetime(2026, 7, 7, 9, 30, tzinfo=UTC)
    runtime_ok_at = datetime(2026, 7, 7, 9, 35, tzinfo=UTC)
    runtime_fail_at = datetime(2026, 7, 7, 9, 40, tzinfo=UTC)
    trace_ok_at = datetime(2026, 7, 7, 9, 34, tzinfo=UTC)
    trace_fail_at = datetime(2026, 7, 7, 9, 39, tzinfo=UTC)

    installed = InstalledPlugin(
        key="demo_repo",
        source="repo",
        source_url="https://github.com/example/demo_repo",
        source_label="仓库",
        version="1.2.3",
        enabled=True,
        signature_ok=True,
        trust_tier="verified",
        lint_warnings=["plugin.py:1: 示例告警"],
    )

    account_feature_active = AccountFeature(
        account_id=101,
        feature_key="demo_repo",
        enabled=True,
        state="active",
        last_error=None,
    )
    account_feature_failed = AccountFeature(
        account_id=102,
        feature_key="demo_repo",
        enabled=False,
        state="disabled",
        last_error="账号级配置缺失",
    )

    runtime_ok = PluginRuntimeStatus(
        id=11,
        plugin_key="demo_repo",
        account_id=101,
        enabled=True,
        installed_version="1.2.3",
        load_status="active",
        last_trace_id="evt_demo_ok",
        updated_at=runtime_ok_at,
    )
    runtime_failed = PluginRuntimeStatus(
        id=12,
        plugin_key="demo_repo",
        account_id=102,
        enabled=False,
        installed_version="1.2.3",
        load_status="failed",
        last_load_error="ImportError: token=secret",
        last_trace_id="evt_demo_failed",
        updated_at=runtime_fail_at,
    )

    trace_ok = EventTrace(
        id=21,
        trace_id="evt_demo_ok",
        account_id=101,
        source_channel="interaction_bot",
        event_type="message",
        status="ok",
        started_at=trace_ok_at,
    )
    trace_failed = EventTrace(
        id=22,
        trace_id="evt_demo_failed",
        account_id=102,
        source_channel="userbot",
        event_type="command",
        status="failed",
        started_at=trace_fail_at,
    )

    monkeypatch.setattr(
        "app.services.plugin_center_service.feature_service.feature_matrix",
        AsyncMock(
            return_value={
                "features": [
                    {
                        "key": "demo_repo",
                        "display_name": "示例仓库插件",
                        "version": "1.2.3",
                        "source_label": "仓库",
                        "update_available": True,
                        "latest_version": "1.3.0",
                        "last_update_check_at": checked_at,
                        "last_update_check_error": None,
                    }
                ],
                "accounts": [
                    {
                        "id": 101,
                        "name": "账号 A",
                        "features": {"demo_repo": "active"},
                        "feature_enabled": {"demo_repo": True},
                    },
                    {
                        "id": 102,
                        "name": "账号 B",
                        "features": {"demo_repo": "disabled"},
                        "feature_enabled": {"demo_repo": False},
                    },
                ],
            }
        ),
    )
    monkeypatch.setattr(
        pcs,
        "list_installed_capability_requirements",
        AsyncMock(
            return_value=[
                PluginCapabilityRequirement(
                    key="demo_repo",
                    source="repo",
                    path=Path("/tmp/demo_repo"),
                    declared=True,
                )
            ]
        ),
    )

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result([installed]),
            _Result([account_feature_active, account_feature_failed]),
            _Result([runtime_failed, runtime_ok]),
            _Result([trace_failed, trace_ok]),
        ]
    )

    rows = await pcs.list_installed_plugins_overview(db)

    assert len(rows) == 1
    row = rows[0]
    assert row.key == "demo_repo"
    assert row.display_name == "示例仓库插件"
    assert row.source == "repo"
    assert row.version == "1.2.3"
    assert row.global_enabled is True
    assert row.update.update_available is True
    assert row.update.latest_version == "1.3.0"
    assert [item.account_id for item in row.accounts] == [101, 102]
    assert row.accounts[0].enabled is True
    assert row.accounts[0].state == "active"
    assert row.accounts[0].last_error is None
    assert row.accounts[1].enabled is False
    assert row.accounts[1].state == "disabled"
    assert row.accounts[1].last_error == "账号级配置缺失"
    assert row.accounts[1].last_load_error == "ImportError: token=***"
    assert row.recent_load_error is not None
    assert row.recent_load_error.account_id == 102
    assert row.recent_load_error.message == "ImportError: token=***"
    assert row.recent_trace is not None
    assert row.recent_trace.trace_id == "evt_demo_failed"
    assert row.recent_trace.account_id == 102
    assert row.recent_trace.status == "failed"


@pytest.mark.asyncio
async def test_installed_legacy_missing_declaration_is_exposed_as_warning_badge(
    monkeypatch,
) -> None:
    installed = InstalledPlugin(
        key="legacy_leaf",
        source="local",
        version="1.0.0",
        enabled=True,
        trust_tier="local",
        lint_warnings=[],
    )
    monkeypatch.setattr(
        pcs.feature_service,
        "feature_matrix",
        AsyncMock(return_value={"features": [], "accounts": []}),
    )
    monkeypatch.setattr(
        pcs,
        "list_installed_capability_requirements",
        AsyncMock(
            return_value=[
                PluginCapabilityRequirement(
                    key="legacy_leaf",
                    source="local",
                    path=Path("/tmp/legacy_leaf"),
                    declared=False,
                    warnings=(MISSING_DECLARATION_WARNING,),
                )
            ]
        ),
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_Result([installed]), _Result([]), _Result([])]
    )

    rows = await pcs.list_installed_plugins_overview(db)

    assert rows[0].lint_warnings == [MISSING_DECLARATION_WARNING]


@pytest.mark.asyncio
async def test_plugins_installed_overview_api_returns_aggregated_payload(monkeypatch) -> None:
    previous_overrides = dict(app.dependency_overrides)

    async def _override_db():
        yield SimpleNamespace()

    async def _override_user():
        return SimpleNamespace(id=7)

    monkeypatch.setattr(
        plugins_install_api.pcs,
        "list_installed_plugins_overview",
        AsyncMock(
            return_value=[
                PluginCenterItem(
                    key="zip_demo",
                    display_name="ZIP 示例",
                    source="zip",
                    source_url=None,
                    source_label="ZIP",
                    version="0.1.0",
                    global_enabled=False,
                    signature_ok=None,
                    trust_tier="community",
                    lint_warnings=[],
                    update=PluginCenterUpdateStatus(update_available=False),
                    accounts=[
                        PluginCenterAccountItem(
                            account_id=1,
                            account_name="主账号",
                            enabled=False,
                            state="disabled",
                        )
                    ],
                    recent_load_error=PluginCenterLoadError(
                        source="plugin_runtime_status",
                        account_id=1,
                        load_status="failed",
                        message="最近一次失败",
                        updated_at=datetime(2026, 7, 7, 10, 0, tzinfo=UTC),
                    ),
                    recent_trace=PluginCenterTraceRef(
                        trace_id="evt_zip_demo",
                        account_id=1,
                        status="failed",
                        event_type="message",
                        source_channel="interaction_bot",
                        started_at=datetime(2026, 7, 7, 10, 1, tzinfo=UTC),
                    ),
                )
            ]
        ),
    )

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/plugins/installed-overview",
                headers=_csrf_headers(),
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["key"] == "zip_demo"
    assert body[0]["global_enabled"] is False
    assert body[0]["update"]["update_available"] is False
    assert body[0]["accounts"][0]["account_name"] == "主账号"
    assert body[0]["recent_trace"]["trace_id"] == "evt_zip_demo"
