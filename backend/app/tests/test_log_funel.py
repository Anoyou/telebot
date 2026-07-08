from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.log_funel import build_message_funel, reason_display


def _trace(**overrides):
    data = {
        "trace_id": "evt_test",
        "status": "ok",
        "ended_at": datetime(2026, 7, 8, tzinfo=UTC),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _span(**overrides):
    data = {
        "span_id": "sp_test",
        "trace_id": "evt_test",
        "phase": "route",
        "component": "event_bus",
        "plugin_key": None,
        "entry_key": None,
        "status": "ok",
        "reason_code": "matched",
        "message": None,
        "ended_at": datetime(2026, 7, 8, tzinfo=UTC),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _action(**overrides):
    data = {
        "action_id": "act_test",
        "trace_id": "evt_test",
        "plugin_key": "math10",
        "action_type": "send_message",
        "status": "ok",
        "error_code": None,
        "error_message": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_reason_display_includes_label_and_code() -> None:
    assert reason_display("subscription_not_matched") == "触发入口未命中 (subscription_not_matched)"


def test_message_funel_responded_with_successful_action() -> None:
    funel = build_message_funel(
        _trace(),
        [
            _span(phase="receive", component="interaction_bot"),
            _span(phase="route", component="interaction_rule"),
            _span(phase="plugin_invoke", plugin_key="math10", component="interaction_module"),
        ],
        [_action()],
    )

    assert funel.verdict == "responded"
    assert funel.received == "pass"
    assert funel.routed == "pass"
    assert funel.ran == "pass"
    assert funel.sent == "pass"
    assert funel.stuck_at is None
    assert "插件 math10" in funel.reason_text


def test_message_funel_no_response_normal_for_subscription_skip() -> None:
    funel = build_message_funel(
        _trace(status="skipped"),
        [
            _span(phase="receive", component="interaction_bot"),
            _span(
                phase="route",
                component="interaction_bot",
                status="skipped",
                reason_code="subscription_not_matched",
            ),
        ],
        [],
    )

    assert funel.verdict == "no_response_normal"
    assert funel.routed == "skip"
    assert funel.ran == "skip"
    assert funel.sent == "none"
    assert funel.stuck_at is None
    assert "这不是故障" in funel.reason_text


def test_message_funel_stuck_after_plugin_invoke_without_completion() -> None:
    funel = build_message_funel(
        _trace(status="running", ended_at=None),
        [
            _span(phase="receive", component="interaction_bot"),
            _span(phase="route", component="interaction_rule"),
            _span(
                phase="plugin_invoke",
                plugin_key="promo",
                component="interaction_module",
                status="ok",
                ended_at=None,
            ),
        ],
        [],
    )

    assert funel.verdict == "stuck"
    assert funel.ran == "stuck"
    assert funel.stuck_at == "ran"
    assert funel.sent == "none"


def test_message_funel_failed_action_points_to_sent_stage() -> None:
    funel = build_message_funel(
        _trace(),
        [
            _span(phase="route", component="interaction_rule"),
            _span(phase="plugin_invoke", plugin_key="promo"),
        ],
        [
            _action(
                status="failed",
                error_code="telegram_api_error",
                error_message="message to edit not found",
            )
        ],
    )

    assert funel.verdict == "failed"
    assert funel.sent == "fail"
    assert funel.stuck_at == "sent"
    assert funel.reason_code == "telegram_api_error"
    assert "message to edit not found" in funel.reason_text


def test_message_funel_direct_passthrough_trace_is_responded() -> None:
    funel = build_message_funel(
        _trace(status="ok"),
        [
            _span(phase="receive", component="userbot_direct_passthrough"),
            _span(
                phase="route",
                component="userbot_direct_passthrough",
                plugin_key="low_latency",
                reason_code="matched",
            ),
            _span(
                phase="plugin_invoke",
                component="userbot_direct_passthrough",
                plugin_key="low_latency",
            ),
        ],
        [],
    )

    assert funel.verdict == "responded"
    assert funel.routed == "pass"
    assert funel.ran == "pass"
    assert funel.sent == "none"
    assert "插件 low_latency" in funel.reason_text
