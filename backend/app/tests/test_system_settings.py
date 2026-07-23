from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import rate_limit
from app.db.models.system import SystemSetting


class _FakeSettingsDB:
    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.rows: dict[str, SystemSetting] = {
            key: SystemSetting(key=key, value=value)
            for key, value in (initial or {}).items()
        }
        self.commits = 0

    async def get(self, model, key):  # noqa: ANN001
        assert model is SystemSetting
        return self.rows.get(key)

    def add(self, row: SystemSetting) -> None:
        self.rows[row.key] = row

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_system_settings_log_retention_switches_roundtrip(monkeypatch) -> None:
    db = _FakeSettingsDB({
        "log_retention": {
            "trace_enabled": True,
            "event_bus_delivery_enabled": True,
            "inline_updates_enabled": True,
            "runtime_log_retention_days": 30,
            "runtime_log_max_message_chars": 2000,
            "runtime_log_max_detail_chars": 8000,
            "runtime_log_min_level": "info",
            "trace_retention_days": 30,
            "trace_payload_snapshot_retention_days": 7,
            "native_raw_persist_enabled": False,
            "native_raw_retention_days": 1,
        }
    })
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())
    monkeypatch.setattr(rate_limit, "_broadcast_reload", AsyncMock())
    monkeypatch.setattr("app.worker.supervisor.invalidate_log_retention_cache", lambda: None)

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(
            log_retention=rate_limit._LogRetentionPatch(
                trace_enabled=False,
                event_bus_delivery_enabled=False,
                inline_updates_enabled=False,
                native_raw_persist_enabled=True,
                native_raw_retention_days=2,
            )
        ),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )

    stored = db.rows["log_retention"].value
    assert stored["trace_enabled"] is False
    assert stored["event_bus_delivery_enabled"] is False
    assert stored["inline_updates_enabled"] is False
    assert stored["native_raw_persist_enabled"] is True
    assert stored["native_raw_retention_days"] == 2
    assert result["log_retention"]["trace_enabled"] is False
    assert result["log_retention"]["event_bus_delivery_enabled"] is False
    assert result["log_retention"]["inline_updates_enabled"] is False


@pytest.mark.asyncio
async def test_system_settings_ai_enabled_hotplug_roundtrip(monkeypatch) -> None:
    from app.services import platform_capabilities as platform_caps

    platform_caps._reset_for_tests()
    db = _FakeSettingsDB()
    await platform_caps.bootstrap_from_db(db)  # type: ignore[arg-type]
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())
    monkeypatch.setattr(rate_limit, "_broadcast_reload", AsyncMock())
    monkeypatch.setattr(
        platform_caps,
        "_broadcast_reload_config",
        AsyncMock(
            return_value={
                "total_accounts": 0,
                "notified": 0,
                "acked": 0,
                "pending": 0,
                "offline_or_timeout": 0,
                "last_broadcast_at": None,
                "notes": [],
            }
        ),
    )
    monkeypatch.setattr(platform_caps, "_apply_local_transition", AsyncMock())
    monkeypatch.setattr("app.services.audit.write", AsyncMock())

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(ai_enabled=False),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )

    assert db.rows["ai_enabled"].value == {"enabled": False, "generation": 1}
    assert result["ai_enabled"] is False
    assert platform_caps.is_ai_enabled_cached(fail_closed=False) is False
    platform_caps._reset_for_tests()


@pytest.mark.asyncio
async def test_system_settings_command_prefix_required_roundtrip(monkeypatch) -> None:
    db = _FakeSettingsDB()
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())
    broadcast = AsyncMock()
    monkeypatch.setattr(rate_limit, "_broadcast_reload", broadcast)

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(command_prefix_required=False),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )

    assert db.rows["command_prefix_required"].value == {"enabled": False}
    assert result["command_prefix_required"] is False
    broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_system_settings_ui_preferences_roundtrip(monkeypatch) -> None:
    db = _FakeSettingsDB()
    audit = AsyncMock()
    monkeypatch.setattr(rate_limit, "_audit", audit)

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(
            ui_preferences=rate_limit._UIPreferencesPatch(
                sidebar_order=["/ai", "/plugins", "/settings"],
                mobile_nav_order=["/ai", "/plugins", "/overview", "/interaction"],
                provider_order=[9, 3, 12],
            )
        ),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )

    assert result["ui_preferences"] == {
        "sidebar_order": ["/ai", "/plugins", "/settings"],
        "mobile_nav_order": ["/ai", "/plugins", "/overview", "/interaction"],
        "provider_order": [9, 3, 12],
    }
    assert db.rows["ui_preferences"].value == result["ui_preferences"]
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_system_settings_ui_preferences_rejects_unknown_or_duplicate_entries(monkeypatch) -> None:
    db = _FakeSettingsDB()
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.patch_system_settings(
            rate_limit._SettingsPatch(
                ui_preferences=rate_limit._UIPreferencesPatch(
                    sidebar_order=["/plugins", "/not-a-page"],
                )
            ),
            db,  # type: ignore[arg-type]
            SimpleNamespace(id=1),
        )
    assert exc_info.value.status_code == 400

    with pytest.raises(ValidationError):
        rate_limit._UIPreferencesPatch(provider_order=list(range(1, 2050)))

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.patch_system_settings(
            rate_limit._SettingsPatch(
                ui_preferences=rate_limit._UIPreferencesPatch(provider_order=[4, 4]),
            ),
            db,  # type: ignore[arg-type]
            SimpleNamespace(id=1),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_system_settings_app_update_target_roundtrip(monkeypatch) -> None:
    db = _FakeSettingsDB()
    audit = AsyncMock()
    monkeypatch.setattr(rate_limit, "_audit", audit)

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(
            app_update_target=rate_limit._AppUpdateTargetPatch(
                remote="origin",
                branch="codex/0.33-interaction-framework",
            )
        ),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )

    expected = {"remote": "origin", "branch": "codex/0.33-interaction-framework"}
    assert db.rows["app_update_target"].value == expected
    assert result["app_update_target"] == expected
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_system_settings_app_update_target_rejects_invalid_branch(monkeypatch) -> None:
    db = _FakeSettingsDB()
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.patch_system_settings(
            rate_limit._SettingsPatch(
                app_update_target=rate_limit._AppUpdateTargetPatch(branch="../main")
            ),
            db,  # type: ignore[arg-type]
            SimpleNamespace(id=1),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_system_settings_login_security_defaults_off_and_roundtrip(monkeypatch) -> None:
    db = _FakeSettingsDB()
    monkeypatch.setattr(rate_limit, "_audit", AsyncMock())
    monkeypatch.setattr(rate_limit, "_broadcast_reload", AsyncMock())

    initial = await rate_limit.get_system_settings(db, SimpleNamespace(id=1))  # type: ignore[arg-type]
    assert initial["login_security"]["notify_otp_enabled"] is False
    assert initial["login_security"]["totp_enabled"] is False
    assert initial["login_security"]["totp_mode"] == "after_failures"

    result = await rate_limit.patch_system_settings(
        rate_limit._SettingsPatch(
            login_security=rate_limit._LoginSecurityPatch(
                notify_otp_enabled=True,
                notify_otp_failed_attempt_threshold=5,
                notify_otp_fail_window_seconds=900,
                notify_otp_ttl_seconds=300,
                notify_otp_max_attempts=3,
                totp_enabled=True,
                totp_mode="after_failures",
                totp_failed_attempt_threshold=4,
                recovery_code_ttl_seconds=1200,
            )
        ),
        db,  # type: ignore[arg-type]
        SimpleNamespace(id=1),
    )

    stored = db.rows["login_security"].value
    assert stored["notify_otp_enabled"] is True
    assert stored["notify_otp_failed_attempt_threshold"] == 5
    assert stored["totp_enabled"] is True
    assert stored["totp_mode"] == "after_failures"
    assert stored["totp_failed_attempt_threshold"] == 4
    assert stored["recovery_code_ttl_seconds"] == 1200
    assert result["login_security"]["totp_enabled"] is True
    assert result["login_security"]["totp_failed_attempt_threshold"] == 4
