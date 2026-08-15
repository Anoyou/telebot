"""已安装互动插件与交互示例的契约测试。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
import types
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from app.worker.plugins.base import PluginContext
from app.worker.plugins.message_ops import BufferedMessageOps

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INSTALLED_ROOT = PROJECT_ROOT / "plugins" / "installed"
VALIDATOR_PATH = PROJECT_ROOT / "scripts" / "validate-installed-interaction-plugins.py"
REQUIRED_INSTALLED_PLUGIN_KEYS = (
    "dice_grid_hunt",
    "guess_number",
    "poetry_blank",
    "pt_promote",
    "redpack-byRBQ",
)


def _installed_plugins_available() -> bool:
    return INSTALLED_ROOT.is_dir() and all((INSTALLED_ROOT / key / "plugin.py").is_file() for key in REQUIRED_INSTALLED_PLUGIN_KEYS)


requires_installed_plugins = pytest.mark.skipif(
    not _installed_plugins_available(),
    reason="本机未安装交互插件样本；CI 干净 checkout 不包含 plugins/installed 运行时目录",
)


def _load_validator_module() -> ModuleType:
    module_name = "telepilot_validate_installed_interaction_plugins"
    spec = importlib.util.spec_from_file_location(module_name, VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_installed_module(plugin_key: str, filename: str) -> ModuleType:
    package_root = "plugins.installed"
    if package_root not in sys.modules:
        pkg = types.ModuleType(package_root)
        pkg.__path__ = [str(INSTALLED_ROOT)]  # type: ignore[attr-defined]
        sys.modules[package_root] = pkg

    package_name = f"{package_root}.{plugin_key}"
    plugin_dir = INSTALLED_ROOT / plugin_key
    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        sys.modules[package_name] = pkg

    path = plugin_dir / filename
    module_name = f"{package_name}.{filename[:-3]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _ReplyRecorder:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


@requires_installed_plugins
def test_installed_interaction_validator_discovers_only_nonempty_interaction_entries() -> None:
    validate_installed_interactions = _load_validator_module()

    assert validate_installed_interactions._installed_plugin_keys() == list(REQUIRED_INSTALLED_PLUGIN_KEYS)


@pytest.mark.asyncio
async def test_example_with_interaction_preserves_original_command_trigger() -> None:
    plugin_module = importlib.import_module("examples.plugins.with_interaction")
    plugin = plugin_module.PLUGIN_CLASS()
    ctx = PluginContext(account_id=1, feature_key="with_interaction", log=AsyncMock())

    event = _ReplyRecorder()
    handled = await plugin.on_command(ctx, "with_interaction", [], event)

    assert handled is True
    assert event.replies == []
    ctx.log.assert_awaited()

    ignored = await plugin.on_command(ctx, "other_command", [], event)
    assert ignored is False


@pytest.mark.asyncio
async def test_example_with_interaction_uses_message_ops_for_visible_reply() -> None:
    plugin_module = importlib.import_module("examples.plugins.with_interaction")
    plugin = plugin_module.PLUGIN_CLASS()
    messages = BufferedMessageOps()
    ctx = PluginContext(account_id=1, feature_key="with_interaction", log=AsyncMock(), messages=messages)

    actions = await plugin.on_interaction(
        ctx,
            "start_with_interaction",
            {
                "message": {"chat_id": -100123, "text": "框架消息"},
                "response_text": "你好，交互 Bot",
                "source": {"type": "message", "chat_id": -100123},
                "actor": {"user_id": 111, "display_name": "AAA"},
            },
        )

    assert messages.actions == [
        {
                "type": "send_message",
                "chat_id": -100123,
                "text": "你好，交互 Bot\n收到：框架消息\n触发人：AAA",
                "parse_mode": "plain",
                "reply_to_message_id": None,
            }
        ]
    assert actions == [
        {
            "type": "result",
            "success": True,
            "result": {"status": "ok", "actor_user_id": 111, "entry_key": "start_with_interaction"},
            "settlement": {"mode": "announce_only", "winner_user_id": 111, "winner_name": "AAA"},
        },
        {"type": "end_session"},
    ]


@pytest.mark.asyncio
@requires_installed_plugins
async def test_installed_interaction_plugins_keep_original_command_handlers() -> None:
    class _ReplyMsg:
        def __init__(self) -> None:
            self.id = 101

    class _CmdEvent:
        def __init__(self, chat_id: int = -100123) -> None:
            self.chat_id = chat_id
            self.replies: list[str] = []

        async def reply(self, text: str, **_kwargs) -> _ReplyMsg:
            self.replies.append(text)
            return _ReplyMsg()

        async def edit(self, text: str, **_kwargs) -> _ReplyMsg:
            self.replies.append(text)
            return _ReplyMsg()

    poetry_module = _load_installed_module("poetry_blank", "plugin.py")
    poetry_plugin = poetry_module.PoetryBlankPlugin()
    poetry_event = _CmdEvent()
    poetry_ctx = PluginContext(account_id=1, feature_key="poetry_blank", log=AsyncMock())
    await poetry_plugin.on_startup(poetry_ctx)

    await poetry_plugin._cmd_handler(None, poetry_event, ["poetry", "123"], 1, poetry_ctx)
    assert poetry_event.replies, "诗词填空原命令仍应能回复"

    dice_module = _load_installed_module("dice_grid_hunt", "plugin.py")
    dice_plugin = dice_module.DiceGridHuntPlugin()
    dice_event = _CmdEvent()
    dice_ctx = PluginContext(account_id=1, feature_key="dice_grid_hunt", log=AsyncMock())
    await dice_plugin.on_startup(dice_ctx)

    await dice_plugin._cmd_handler(None, dice_event, ["100"], 1, dice_ctx)
    assert dice_event.replies, "九宫格原命令仍应能回复"

    pt_module = _load_installed_module("pt_promote", "plugin.py")
    pt_plugin = pt_module.PTPromotePlugin()
    pt_event = _CmdEvent()
    pt_ctx = PluginContext(account_id=1, feature_key="pt_promote", log=AsyncMock())
    await pt_plugin.on_startup(pt_ctx)

    await pt_plugin._handle_promote(None, pt_event, [], 1, pt_ctx)
    assert pt_event.replies, "PT 促销原命令仍应能回复"


@pytest.mark.asyncio
@requires_installed_plugins
async def test_redpack_original_command_preserves_legacy_core_path(monkeypatch) -> None:
    redpack_module = _load_installed_module("redpack-byRBQ", "plugin.py")
    redpack_plugin = redpack_module.RedpackByRBQPlugin()
    redpack_ctx = PluginContext(account_id=1, feature_key="redpack-byRBQ", log=AsyncMock())
    await redpack_plugin.on_startup(redpack_ctx)

    command_client = types.SimpleNamespace(get_me=AsyncMock(return_value=types.SimpleNamespace(id=1)))
    event = types.SimpleNamespace(chat_id=-100123, raw_text=",redpack help")
    run_legacy = AsyncMock()

    monkeypatch.setattr(redpack_module, "_is_account_command_event", AsyncMock(return_value=True))
    monkeypatch.setattr(redpack_module.redpack_core, "redpack_command", run_legacy)

    await redpack_plugin._cmd_redpack(command_client, event, ["help"], 1, redpack_ctx)

    assert "redpack" in redpack_plugin.commands
    run_legacy.assert_awaited_once()
    message, bot = run_legacy.await_args.args
    assert message.arguments == "help"
    assert getattr(message.chat, "id", None) == -100123
    assert isinstance(bot, redpack_module._NativeClientAdapter)


@requires_installed_plugins
def test_guess_number_manifest_declares_interaction_contract() -> None:
    manifest_module = _load_installed_module("guess_number", "manifest.py")
    manifest = manifest_module.MANIFEST

    assert manifest.category == "interactive"
    entry = manifest.interaction_entries[0]
    assert entry["key"] == "start_guess_number"
    assert entry["interaction_profile"] == "session_game"
    assert entry["launch_mode"] == "hybrid"
    assert entry["session_scope"] == "chat"
    assert entry["events"] == ["command", "keyword", "payment_confirmed", "message", "callback_query", "session_expired", "session_close"]
    assert entry["triggers"]["command"] == "guess"
    assert entry["preserve_command_trigger"] is True
    assert entry["command_fallback"]["command"] == "guess"
    assert entry["result_contract"]["actions"] == [
        "send_message",
        "answer_callback",
        "update_session",
        "payout",
        "end_session",
        "result",
        "settlement",
    ]
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply"]
    assert "valid_seconds" in entry["input_schema"]["properties"]


@requires_installed_plugins
def test_poetry_blank_manifest_declares_interaction_contract() -> None:
    manifest_module = _load_installed_module("poetry_blank", "manifest.py")
    manifest = manifest_module.MANIFEST

    assert manifest.category == "interactive"
    entry = manifest.interaction_entries[0]
    assert entry["key"] == "start_poetry_blank"
    assert entry["interaction_profile"] == "session_game"
    assert entry["launch_mode"] == "hybrid"
    assert entry["session_scope"] == "chat"
    assert entry["events"] == ["command", "payment_confirmed", "keyword", "message", "session_expired", "session_close"]
    assert entry["triggers"]["command"] == "poetry"
    assert entry["preserve_command_trigger"] is True
    assert entry["command_fallback"]["command"] == "poetry"
    assert entry["result_contract"]["actions"] == ["send_message", "update_session", "payout", "end_session", "result", "settlement"]
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply"]
    assert "valid_seconds" in entry["input_schema"]["properties"]


@requires_installed_plugins
def test_dice_grid_hunt_manifest_declares_interaction_contract() -> None:
    manifest_module = _load_installed_module("dice_grid_hunt", "manifest.py")
    manifest = manifest_module.MANIFEST

    assert manifest.category == "interactive"
    entry = manifest.interaction_entries[0]
    assert entry["key"] == "start_dice_grid_hunt"
    assert entry["interaction_profile"] == "session_game"
    assert entry["launch_mode"] == "hybrid"
    assert entry["session_scope"] == "chat"
    assert entry["events"] == ["command", "payment_confirmed", "keyword", "message", "callback_query", "session_expired", "session_close"]
    assert entry["triggers"]["command"] == "dicegrid"
    assert entry["preserve_command_trigger"] is True
    assert entry["command_fallback"]["command"] == "dicegrid"
    assert entry["result_contract"]["actions"] == [
        "send_message",
        "send_photo",
        "update_session",
        "payout",
        "end_session",
        "result",
        "settlement",
    ]
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply"]
    assert "valid_seconds" in entry["input_schema"]["properties"]


@requires_installed_plugins
def test_redpack_manifest_declares_interaction_contract() -> None:
    manifest_module = _load_installed_module("redpack-byRBQ", "manifest.py")
    manifest = manifest_module.MANIFEST

    assert manifest.category == "interactive"
    entry = manifest.interaction_entries[0]
    assert entry["key"] == "start_redpack"
    assert entry["interaction_profile"] == "reward_pool"
    assert entry["launch_mode"] == "hybrid"
    assert entry["session_scope"] == "chat"
    assert entry["events"] == ["keyword", "payment_confirmed", "message", "session_close"]
    assert entry["preserve_command_trigger"] is True
    assert entry["command_fallback"]["command"] == "redpack"
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply"]
    assert "total_amount" in entry["input_schema"]["properties"]
    assert "count" in entry["input_schema"]["properties"]


@requires_installed_plugins
def test_pt_promote_manifest_declares_interaction_contract() -> None:
    manifest_module = _load_installed_module("pt_promote", "manifest.py")
    manifest = manifest_module.MANIFEST

    assert manifest.category == "utility"
    entry = manifest.interaction_entries[0]
    assert entry["key"] == "promote_torrent"
    assert entry["interaction_profile"] == "utility_trigger"
    assert entry["launch_mode"] == "hybrid"
    assert entry["session_scope"] == "user"
    assert entry["events"] == ["keyword", "payment_confirmed", "message"]
    assert entry["triggers"]["command"] == "pt"
    assert entry["preserve_command_trigger"] is True
    assert entry["command_fallback"]["command"] == "pt"
    assert entry["result_contract"]["send_via"] == ["interaction_bot", "userbot_reply"]
    assert "id" in entry["input_schema"]["properties"]
    assert "default_options" in entry["input_schema"]["properties"]


@requires_installed_plugins
def test_installed_interaction_plugin_json_matches_manifest_contracts() -> None:
    for plugin_key in (
        "guess_number",
        "poetry_blank",
        "dice_grid_hunt",
        "redpack-byRBQ",
        "pt_promote",
    ):
        manifest_module = _load_installed_module(plugin_key, "manifest.py")
        plugin_json = INSTALLED_ROOT / plugin_key / "plugin.json"
        metadata = importlib.import_module("json").loads(plugin_json.read_text(encoding="utf-8"))

        assert metadata.get("category") == manifest_module.MANIFEST.category
        assert metadata.get("interaction_profile") == manifest_module.MANIFEST.interaction_profile
        assert metadata.get("interaction_entries") == manifest_module.MANIFEST.interaction_entries


@pytest.mark.asyncio
@requires_installed_plugins
async def test_guess_number_on_interaction_uses_session_data_and_payout() -> None:
    plugin_module = _load_installed_module("guess_number", "plugin.py")
    plugin = plugin_module.GuessNumberPlugin()
    ctx = PluginContext(account_id=1, feature_key="guess_number", log=AsyncMock())

    start_actions = await plugin.on_interaction(
        ctx,
        "start_guess_number",
        {
            "source": {"type": "command", "chat_id": -100123, "message_id": 70},
            "trigger": {"type": "command", "command": "guess", "args": ["321", "1", "100", "5"]},
            "session": {"scope": "chat", "channel": "userbot", "ttl_seconds": 120, "data": {}},
            "valid_seconds": 120,
        },
    )
    assert start_actions is not None
    assert start_actions[0]["type"] == "send_message"
    assert start_actions[0]["chat_id"] == -100123
    assert start_actions[0]["parse_mode"] == "plain"
    assert start_actions[0]["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == "guess:50"
    assert start_actions[1]["type"] == "update_session"
    state = dict(start_actions[1]["data"])
    state["target"] = 42

    answer_actions = await plugin.on_interaction(
        ctx,
        "start_guess_number",
        {
            "source": {"type": "message", "chat_id": -100123, "message_id": 99, "text": "42"},
            "actor": {"user_id": 111, "display_name": "AAA"},
            "session": {"scope": "chat", "channel": "userbot", "data": state},
        },
    )

    assert answer_actions == [
        {
            "type": "send_message",
            "chat_id": -100123,
            "text": "答对了：AAA\n题目：猜数字 1 ~ 100\n答案：42\n奖金：321",
            "parse_mode": "plain",
            "reply_to_message_id": 99,
        },
        {
            "type": "payout",
            "chat_id": -100123,
            "amount": 321,
            "text": "+321",
            "parse_mode": "plain",
            "reply_to_message_id": 99,
        },
        {
            "type": "result",
            "success": True,
            "result": {
                "status": "winner",
                "winner_user_id": 111,
                "winner_name": "AAA",
                "winner_message_id": 99,
                "target": 42,
                "guess": 42,
                "range": [1, 100],
                "attempts": 1,
                "prize": 321,
            },
            "settlement": {
                "mode": "auto",
                "amount": 321,
                "winner_user_id": 111,
                "winner_name": "AAA",
                "status": "payout_requested",
            },
        },
        {"type": "end_session"},
    ]


@pytest.mark.asyncio
@requires_installed_plugins
async def test_guess_number_handles_callback_and_session_expired() -> None:
    plugin_module = _load_installed_module("guess_number", "plugin.py")
    plugin = plugin_module.GuessNumberPlugin()
    ctx = PluginContext(account_id=1, feature_key="guess_number", log=AsyncMock())
    state = {
        "active": True,
        "target": 50,
        "low": 1,
        "high": 100,
        "prize": 123,
        "attempts": 0,
        "max_attempts": 0,
        "timeout": 120,
        "history": [],
    }

    callback_actions = await plugin.on_interaction(
        ctx,
        "start_guess_number",
        {
            "source": {
                "type": "callback_query",
                "chat_id": -100123,
                "message_id": 70,
                "callback_query_id": "cb-1",
                "callback_data": "guess:25",
            },
            "actor": {"user_id": 111, "display_name": "AAA"},
            "session": {"scope": "chat", "channel": "interaction_bot", "data": state},
        },
    )
    assert callback_actions is not None
    assert callback_actions[0] == {"type": "answer_callback", "callback_query_id": "cb-1", "text": ""}
    assert callback_actions[1]["type"] == "send_message"
    assert callback_actions[1]["parse_mode"] == "plain"
    assert callback_actions[2]["type"] == "update_session"
    assert callback_actions[2]["data"]["attempts"] == 1

    expired_actions = await plugin.on_interaction(
        ctx,
        "start_guess_number",
        {
            "source": {"type": "session_expired", "chat_id": -100123},
            "session": {"scope": "chat", "channel": "interaction_bot", "data": state},
        },
    )
    assert expired_actions == [
        {
            "type": "send_message",
            "chat_id": -100123,
            "text": "猜数字已超时，答案是 50。",
            "parse_mode": "plain",
        },
        {"type": "end_session"},
    ]


@pytest.mark.asyncio
@requires_installed_plugins
async def test_poetry_blank_on_interaction_returns_result_from_standard_envelope() -> None:
    plugin_module = _load_installed_module("poetry_blank", "plugin.py")
    plugin = plugin_module.PoetryBlankPlugin()
    ctx = PluginContext(account_id=1, feature_key="poetry_blank", log=AsyncMock())

    plugin._pick_poem = lambda _chat_id: ("床前明月光", "李白", "静夜思", "床前__光", ["明", "月"])  # type: ignore[method-assign]

    start_actions = await plugin.on_interaction(
        ctx,
        "start_poetry_blank",
        {
            "source": {"type": "keyword", "chat_id": -100123, "message_id": 70, "text": "开诗词"},
            "session": {"scope": "chat", "ttl_seconds": 120},
            "prize": 456,
            "valid_seconds": 120,
        },
    )
    assert start_actions[0] == {
        "type": "send_message",
        "text": "诗词填空开始\n奖金：+456\n\n床前__光\n\n提示：李白 · 《静夜思》\n请直接发送答案抢答。",
        "parse_mode": "plain",
    }
    assert start_actions[1]["type"] == "update_session"
    assert start_actions[1]["data"] == {
        "active": True,
        "full_line": "床前明月光",
        "author": "李白",
        "title": "静夜思",
        "blanked": "床前__光",
        "answer": ["明", "月"],
        "prize": 456,
        "started_at": start_actions[1]["data"]["started_at"],
        "timeout": 120,
    }
    state = dict(start_actions[1]["data"])
    assert start_actions == [
        {
            "type": "send_message",
            "text": "诗词填空开始\n奖金：+456\n\n床前__光\n\n提示：李白 · 《静夜思》\n请直接发送答案抢答。",
            "parse_mode": "plain",
        },
        {
            "type": "update_session",
            "data": {
                "active": True,
                "full_line": "床前明月光",
                "author": "李白",
                "title": "静夜思",
                "blanked": "床前__光",
                "answer": ["明", "月"],
                "prize": 456,
                "started_at": start_actions[1]["data"]["started_at"],
                "timeout": 120,
            },
        },
    ]

    answer_actions = await plugin.on_interaction(
        ctx,
        "start_poetry_blank",
        {
            "source": {"type": "message", "chat_id": -100123, "message_id": 99, "text": "明月"},
            "actor": {"user_id": 111, "display_name": "AAA"},
            "session": {"scope": "chat", "channel": "userbot", "data": state},
        },
    )

    assert answer_actions == [
        {
            "type": "send_message",
            "chat_id": -100123,
            "text": "答对了：AAA\n题目：床前__光\n原句：床前明月光\n出处：李白 · 《静夜思》\n奖金：456",
            "parse_mode": "plain",
            "reply_to_message_id": 99,
        },
        {
            "type": "payout",
            "chat_id": -100123,
            "amount": 456,
            "text": "+456",
            "parse_mode": "plain",
            "reply_to_message_id": 99,
        },
        {
            "type": "result",
            "success": True,
            "result": {
                "status": "winner",
                "winner_user_id": 111,
                "winner_name": "AAA",
                "winner_message_id": 99,
                "full_line": "床前明月光",
                "author": "李白",
                "title": "静夜思",
                "answer": "明月",
                "prize": 456,
            },
            "settlement": {
                "mode": "auto",
                "amount": 456,
                "winner_user_id": 111,
                "winner_name": "AAA",
                "status": "payout_requested",
            },
        },
        {"type": "end_session"},
    ]


@pytest.mark.asyncio
@requires_installed_plugins
async def test_dice_grid_hunt_on_interaction_returns_result_from_standard_envelope() -> None:
    plugin_module = _load_installed_module("dice_grid_hunt", "plugin.py")
    plugin = plugin_module.DiceGridHuntPlugin()
    ctx = PluginContext(account_id=1, feature_key="dice_grid_hunt", log=AsyncMock())

    plugin_module._render_grid_png = lambda _rd: b"png-bytes"  # type: ignore[assignment]
    plugin._new_round = lambda prize, timeout=None: plugin_module.RoundState(  # type: ignore[method-assign]
        rolls=[[1, 1, 1, 1, 1, 1]] * 9,
        sums=[6] * 9,
        answer_index=6,
        target_sum=17,
        prize=prize,
        started_at=time.monotonic(),
        timeout=timeout or 90,
        last_guess_at={},
    )

    start_actions = await plugin.on_interaction(
        ctx,
        "start_dice_grid_hunt",
        {
            "source": {"type": "payment_confirmed", "chat_id": -100123, "message_id": 70},
            "session": {"scope": "chat", "ttl_seconds": 90},
            "prize": 777,
            "valid_seconds": 90,
        },
    )
    assert start_actions[0]["type"] == "send_photo"
    assert start_actions[0]["filename"] == "dice_grid_hunt.png"
    assert start_actions[0]["parse_mode"] == "html"
    assert start_actions[0]["reply_to_message_id"] == 70
    assert start_actions[1]["type"] == "update_session"
    state = dict(start_actions[1]["data"])

    first_guess_actions = await plugin.on_interaction(
        ctx,
        "start_dice_grid_hunt",
        {
            "source": {"type": "message", "chat_id": -100123, "message_id": 98, "text": "5"},
            "actor": {"user_id": 111, "display_name": "AAA"},
            "session": {"scope": "chat", "channel": "userbot", "data": state},
        },
    )
    assert first_guess_actions is not None
    assert first_guess_actions[0]["type"] == "update_session"
    state = dict(first_guess_actions[0]["data"])

    throttled_actions = await plugin.on_interaction(
        ctx,
        "start_dice_grid_hunt",
        {
            "source": {"type": "message", "chat_id": -100123, "message_id": 99, "text": "6"},
            "actor": {"user_id": 111, "display_name": "AAA"},
            "session": {"scope": "chat", "channel": "userbot", "data": state},
        },
    )
    assert throttled_actions == []

    answer_actions = await plugin.on_interaction(
        ctx,
        "start_dice_grid_hunt",
        {
            "source": {"type": "message", "chat_id": -100123, "message_id": 100, "text": "6"},
            "actor": {"user_id": 222, "display_name": "BBB"},
            "session": {"scope": "chat", "channel": "userbot", "data": state},
        },
    )
    assert answer_actions[0]["type"] == "send_message"
    assert "答对了：BBB" in answer_actions[0]["text"]
    assert answer_actions[0]["parse_mode"] == "plain"
    assert answer_actions[0]["reply_to_message_id"] == 100
    assert answer_actions[1] == {
        "type": "payout",
        "chat_id": -100123,
        "amount": 777,
        "text": "+777",
        "parse_mode": "plain",
        "reply_to_message_id": 100,
    }
    assert answer_actions[2] == {
        "type": "result",
        "success": True,
        "result": {
            "status": "winner",
            "winner_user_id": 222,
            "winner_name": "BBB",
            "winner_message_id": 100,
            "target_sum": 17,
            "answer_index": 6,
            "prize": 777,
        },
        "settlement": {
            "mode": "auto",
            "amount": 777,
            "winner_user_id": 222,
            "winner_name": "BBB",
            "status": "payout_requested",
        },
    }
    assert answer_actions[3] == {"type": "end_session"}
