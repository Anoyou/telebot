from __future__ import annotations

import pytest

from app.services import rule_service
from app.services.rule_service import RuleServiceError
from app.services.scheduler_target import (
    SchedulerTargetError,
    normalize_scheduler_action_target,
    normalize_scheduler_target,
)


class _DB:
    async def get(self, *_args):
        return None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (8395686237, 8395686237),
        ("8395686237", 8395686237),
        (" -1001234567890 ", -1001234567890),
        (" @qingbaobu ", "@qingbaobu"),
    ],
)
def test_normalize_scheduler_target_accepts_ids_and_username(raw, expected) -> None:
    assert normalize_scheduler_target(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        0,
        "qingbaobu",
        "https://t.me/qingbaobu",
        "@bad name",
        "@abc",
        9_223_372_036_854_775_808,
        "9223372036854775808",
        "9" * 4301,
    ],
)
def test_normalize_scheduler_target_rejects_invalid_values(raw) -> None:
    with pytest.raises(SchedulerTargetError):
        normalize_scheduler_target(raw)


@pytest.mark.parametrize("target", [0, "0", "+0", "-0"])
def test_normalize_scheduler_action_target_keeps_run_command_self_default(target) -> None:
    cfg = normalize_scheduler_action_target(
        {"action": {"type": "run_command", "target_chat_id": target, "command": ",help"}}
    )

    assert "target_chat_id" not in cfg["action"]


def test_normalize_scheduler_action_target_normalizes_required_target() -> None:
    cfg = normalize_scheduler_action_target(
        {"action": {"type": "send_message", "target_chat_id": " @qingbaobu "}}
    )

    assert cfg["action"]["target_chat_id"] == "@qingbaobu"


def test_normalize_scheduler_action_target_keeps_matching_resolved_target() -> None:
    cfg = normalize_scheduler_action_target(
        {
            "action": {
                "type": "send_message",
                "target_chat_id": "@qingbaobu",
                "target_chat_id_resolved": 8395686237,
                "target_chat_resolved_ref": "@qingbaobu",
            }
        }
    )

    assert cfg["action"]["target_chat_id_resolved"] == 8395686237


def test_normalize_scheduler_action_target_drops_stale_resolved_target() -> None:
    cfg = normalize_scheduler_action_target(
        {
            "_target_retry_at": "2026-07-24T00:00:00+00:00",
            "action": {
                "type": "send_message",
                "target_chat_id": "@newtarget",
                "target_chat_id_resolved": 8395686237,
                "target_chat_resolved_ref": "@oldtarget",
            },
        }
    )

    assert "target_chat_id_resolved" not in cfg["action"]
    assert "target_chat_resolved_ref" not in cfg["action"]
    assert "_target_retry_at" not in cfg


@pytest.mark.asyncio
async def test_rule_service_normalizes_scheduler_username() -> None:
    cfg = await rule_service.normalize_scheduler_config(
        _DB(),
        {
            "kind": "interval",
            "interval_sec": 60,
            "action": {
                "type": "send_message",
                "target_chat_id": "  @qingbaobu  ",
                "text": "📅 签到",
            },
        },
    )

    assert cfg["action"]["target_chat_id"] == "@qingbaobu"


@pytest.mark.asyncio
async def test_rule_service_rejects_invalid_scheduler_target() -> None:
    with pytest.raises(RuleServiceError) as exc_info:
        await rule_service.normalize_scheduler_config(
            _DB(),
            {
                "kind": "interval",
                "interval_sec": 60,
                "action": {
                    "type": "send_message",
                    "target_chat_id": "qingbaobu",
                    "text": "📅 签到",
                },
            },
        )

    assert exc_info.value.code == "INVALID_SCHEDULER_TARGET"
