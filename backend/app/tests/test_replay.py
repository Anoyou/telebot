from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.services import action_tap
from app.worker.replay import replay_recording


@pytest.fixture
def recording_root(tmp_path: Path) -> Path:
    return tmp_path / "recordings"


def _fake_envelope(account_id: int = 7) -> dict:
    return {
        "source": {
            "type": "message",
            "channel": "userbot",
            "driver": "telethon",
            "account_id": account_id,
            "update_id": 1001,
        },
        "event_type": "message",
        "message": {
            "chat_id": -100123,
            "message_id": 55,
            "text": "ping",
        },
        "chat": {"id": -100123, "type": "supergroup"},
        "sender": {"user_id": 42, "display_name": "Alice", "username": "alice"},
        "actor": {"user_id": 42, "display_name": "Alice", "username": "alice"},
        "raw": {"message_id": 55, "text": "ping", "event_type": "message"},
        "native_raw_meta": {"enabled": False, "reason_code": "native_raw_not_allowed"},
        "native_raw": None,
    }


@pytest.mark.asyncio
async def test_recording_jsonl_replays_stable_action_sequence(recording_root: Path) -> None:
    envelope = _fake_envelope()

    off = await action_tap.emit_inbound_event(
        account_id=8,
        envelope=envelope,
        account_config={"dev_mode": {"recording": False}},
        recordings_dir=recording_root,
    )
    assert off is None
    assert not (recording_root / "8").exists()

    path = await action_tap.emit_inbound_event(
        account_id=7,
        envelope=envelope,
        account_config={"dev_mode": {"recording": True}},
        recordings_dir=recording_root,
    )
    assert path is not None
    assert path.is_file()

    seen_dry_run: list[bool] = []

    async def _dispatch(recorded: dict) -> None:
        seen_dry_run.append(bool((recorded.get("context") or {}).get("dev_mode", {}).get("dry_run")))
        await action_tap.emit_action_event(
            account_id=7,
            action={
                "type": "send_message",
                "send_via": "userbot_reply",
                "chat_id": (recorded.get("message") or {}).get("chat_id"),
                "text": "pong",
                "context": {"plugin_key": "demo", "entry_key": "main", "session_key": "sess"},
            },
            status=action_tap.ACTION_EVENT_STATUS_DRY_RUN,
            channel="userbot_reply",
            result={"dry_run": True, "message_id": 99, "chat_id": -100123},
        )

    first = await replay_recording(path, dispatch=_dispatch)
    second = await replay_recording(path, dispatch=_dispatch)

    assert first.envelope_count == 1
    assert first.action_events == second.action_events
    assert seen_dry_run == [True, True]
    assert first.action_events == [
        {
            "account_id": 7,
            "channel": "userbot_reply",
            "session_key": "sess",
            "plugin_key": "demo",
            "entry_key": "main",
            "action_type": "send_message",
            "params_summary": {
                "chat_id": -100123,
                "send_via": "userbot_reply",
                "text": "pong",
                "type": "send_message",
                "result": {"message_id": 99, "chat_id": -100123},
            },
            "status": action_tap.ACTION_EVENT_STATUS_DRY_RUN,
            "error_code": None,
            "error_summary": None,
        }
    ]


def _load_tp_replay_cli() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "tp_replay.py"
    spec = importlib.util.spec_from_file_location("telepilot_tp_replay_cli", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tp_replay_cli_parser_accepts_run_command() -> None:
    cli = _load_tp_replay_cli()

    args = cli.build_parser().parse_args(["run", "data/recordings/7/2026-07-09.jsonl", "--account-id", "7", "--compact"])

    assert args.command == "run"
    assert args.account_id == 7
    assert args.compact is True
