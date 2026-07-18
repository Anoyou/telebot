from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.services import action_tap
from app.worker import replay as replay_mod
from app.worker.plugins import base as plugin_base_mod
from app.worker.plugins import loader as loader_mod
from app.worker.plugins.base import Plugin
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


def test_replay_userbot_event_preserves_rich_message_metadata() -> None:
    envelope = _fake_envelope()
    envelope["message"].update(
        {
            "text": "富文本 fallback",
            "text_source": "rich_message_fallback",
            "rich_message": {"blocks": [{"type": "paragraph", "text": "富文本 fallback"}]},
        }
    )

    event = replay_mod._userbot_event_from_envelope(envelope)  # noqa: SLF001

    assert event.raw_text == "富文本 fallback"
    assert event.message.text_source == "rich_message_fallback"
    assert event.message.rich_message == envelope["message"]["rich_message"]


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeDB:
    def __init__(self, *, account_id: int, plugin_key: str) -> None:
        self.account_id = account_id
        self.plugin_key = plugin_key

    async def __aenter__(self) -> _FakeDB:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def get(self, model: Any, key: Any) -> Any:
        name = getattr(model, "__name__", "")
        if name == "Account":
            return SimpleNamespace(id=key, tg_user_id=42, cold_start_until=None)
        if name in {"HumanizeConfig", "PluginGlobalConfig", "SystemSetting"}:
            return None
        return None

    async def execute(self, statement: Any) -> _FakeExecuteResult:
        text = str(statement)
        if "account_feature" in text and "SELECT" in text.upper():
            return _FakeExecuteResult(
                [
                    SimpleNamespace(
                        account_id=self.account_id,
                        feature_key=self.plugin_key,
                        enabled=True,
                        config={},
                    )
                ]
            )
        return _FakeExecuteResult([])

    async def commit(self) -> None:
        return None


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


@pytest.mark.asyncio
async def test_default_replay_dispatcher_loads_plugins_in_dry_run(monkeypatch: pytest.MonkeyPatch, recording_root: Path) -> None:
    plugin_key = "replay_echo_test"
    envelope = _fake_envelope()

    class ReplayEchoPlugin(Plugin):
        key = plugin_key
        display_name = "Replay Echo Test"
        owner_only = False

        async def on_message(self, ctx: Any, event: Any) -> None:
            await ctx.client.send_message(event.chat_id, f"echo:{event.raw_text}")

    monkeypatch.setitem(plugin_base_mod._REGISTRY, plugin_key, ReplayEchoPlugin)  # noqa: SLF001
    monkeypatch.setattr(
        loader_mod,
        "AsyncSessionLocal",
        lambda: _FakeDB(account_id=7, plugin_key=plugin_key),
    )

    async def _false() -> bool:
        return False

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _not_consumed(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(loader_mod, "_load_log_incoming_messages_setting", _false)
    monkeypatch.setattr(loader_mod, "_load_ignored_peers", _noop)
    monkeypatch.setattr(loader_mod, "_refresh_interaction_text_guard_cache", _noop)
    monkeypatch.setattr(loader_mod, "_refresh_userbot_session_chat_cache", _noop)
    monkeypatch.setattr(loader_mod, "_dispatch_userbot_direct_passthrough", _not_consumed)
    monkeypatch.setattr(loader_mod, "_dispatch_userbot_session_message", _not_consumed)

    created: list[replay_mod._DefaultReplayDispatcher] = []  # noqa: SLF001
    original_dispatcher = replay_mod._DefaultReplayDispatcher  # noqa: SLF001

    class CapturingDispatcher(original_dispatcher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(replay_mod, "_DefaultReplayDispatcher", CapturingDispatcher)

    path = recording_root / "7" / "replay.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False) + "\n", encoding="utf-8")

    result = await replay_recording(path)

    assert result.envelope_count == 1
    assert result.account_id == 7
    assert created
    assert all(item.get("dry_run") is True for item in created[0].client.sent)
    assert created[0].redis.published == []
    assert result.action_events == [
        {
            "account_id": 7,
            "channel": "userbot_reply",
            "session_key": None,
            "plugin_key": plugin_key,
            "entry_key": None,
            "action_type": "send_message",
            "params_summary": {
                "chat_id": -100123,
                "send_via": "userbot_reply",
                "text": "echo:ping",
                "type": "send_message",
                "result": {"chat_id": -100123},
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
