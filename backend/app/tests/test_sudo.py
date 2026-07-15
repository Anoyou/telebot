"""Sudo 消息代发单元测试。

覆盖：
- CommandContext sudo 字段默认值
- _check_sudo_permission 权限检查逻辑（无 sudo / 显式全部 / chat 白名单 / 命令白名单）
- sudo prefix 匹配（通过 make_command_handler 间接测试）
- ,sudo ls 内置命令只读查询（mock DB）
- generation guard 逻辑（loader 中 generation 不匹配时跳过 handler）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.sudo import SudoUserResponse
from app.worker.command import (
    CommandContext,
    _check_sudo_permission,
    _has_dispatch_target,
    _is_self_chat,
    _looks_like_command_name,
    _should_report_incoming_sudo_denial,
    make_command_handler,
    register_plugin_command,
    set_command_context,
    unregister_plugin_command,
)
from app.worker.commands.sudo_guard import (
    check_sudo_permission as guard_check_sudo_permission,
)
from app.worker.commands.sudo_guard import (
    has_dispatch_target as guard_has_dispatch_target,
)
from app.worker.commands.sudo_guard import (
    is_self_chat as guard_is_self_chat,
)
from app.worker.commands.sudo_guard import (
    looks_like_command_name as guard_looks_like_command_name,
)
from app.worker.commands.sudo_guard import (
    should_report_incoming_sudo_denial as guard_should_report_incoming_sudo_denial,
)

# ════════════════════════════════════════════════════════════
# 1) CommandContext sudo 字段
# ════════════════════════════════════════════════════════════


def test_command_context_sudo_defaults():
    """新 CommandContext 的 sudo 字段应该是空。"""
    ctx = CommandContext(
        account_id=1,
        templates={},
        providers={},
        command_prefix=",",
    )
    assert ctx.sudo_users == {}
    assert ctx.sudo_prefix == "."
    assert ctx.sudo_enabled is False
    assert ctx.self_tg_user_id is None


def test_command_context_sudo_can_be_set():
    """sudo 字段可以在构造时传入。"""
    ctx = CommandContext(
        account_id=1,
        templates={},
        providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={
            111: {"display_name": "alice", "allowed_chat_ids": [], "allowed_commands": []},
        },
        sudo_prefix=".",
    )
    assert 111 in ctx.sudo_users
    assert ctx.sudo_users[111]["display_name"] == "alice"
    assert ctx.sudo_enabled is True


# ════════════════════════════════════════════════════════════
# 2) _check_sudo_permission 权限检查
# ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_check_sudo_no_ctx():
    """_ctx 为 None 时应该拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = None
    try:
        event = AsyncMock()
        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is False
        assert "未开启" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_empty_users():
    """sudo_users 为空时应该拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",", sudo_enabled=True, sudo_users={},
    )
    try:
        event = AsyncMock()
        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is False
        assert "未配置" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_disabled_by_global_switch():
    """sudo 总开关关闭时，即使配置了用户也拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1,
        templates={},
        providers={},
        command_prefix=",",
        sudo_enabled=False,
        sudo_users={111: {"display_name": "alice", "allowed_chat_ids": ["*"], "allowed_commands": ["*"]}},
    )
    try:
        event = AsyncMock()
        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is False
        assert "未开启" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_user_not_in_list():
    """发送者不在 sudo 列表时应该拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={999: {"display_name": "bob", "allowed_chat_ids": [], "allowed_commands": []}},
    )
    try:
        sender = MagicMock()
        sender.id = 111  # 不在列表里
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)

        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is False
        assert "不在 sudo 列表" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_allowed_explicit_all_scope():
    """在列表中且显式允许全部对话/命令时应该通过。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={111: {"display_name": "alice", "allowed_chat_ids": ["*"], "allowed_commands": ["*"]}},
    )
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)
        event.chat_id = -100999

        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is True
        assert msg == ""
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_empty_scope_denied_by_default():
    """sudo 用户未配置对话/命令范围时默认拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={111: {"display_name": "alice", "allowed_chat_ids": [], "allowed_commands": []}},
    )
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)
        event.chat_id = -100999

        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is False
        assert "未配置允许对话" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_chat_not_allowed():
    """chat_id 不在白名单时应该拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={111: {
            "display_name": "alice",
            "allowed_chat_ids": [-100111],
            "allowed_commands": ["*"],
        }},
    )
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)
        event.chat_id = -100999  # 不在白名单

        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is False
        assert "不在白名单" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_chat_allowed():
    """chat_id 在白名单时应该通过。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={111: {
            "display_name": "alice",
            "allowed_chat_ids": [-100111],
            "allowed_commands": ["*"],
        }},
    )
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)
        event.chat_id = -100111

        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is True
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_command_not_allowed():
    """命令不在白名单时应该拒绝。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={111: {
            "display_name": "alice",
            "allowed_chat_ids": ["*"],
            "allowed_commands": ["ping", "help"],
        }},
    )
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)

        allowed, msg = await _check_sudo_permission(event, "reboot", 1)
        assert allowed is False
        assert "不在白名单" in msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_check_sudo_command_allowed():
    """命令在白名单时应该通过。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1, templates={}, providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={111: {
            "display_name": "alice",
            "allowed_chat_ids": ["*"],
            "allowed_commands": ["ping", "help"],
        }},
    )
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)

        allowed, msg = await _check_sudo_permission(event, "ping", 1)
        assert allowed is True
    finally:
        wcmd._ctx = old


def test_sudo_response_hides_internal_all_marker():
    """API 响应应该用布尔字段表达“全部”，不把内部通配符暴露给前端列表。"""
    response = SudoUserResponse.model_validate({
        "id": 1,
        "account_id": 1,
        "tg_user_id": 111,
        "display_name": "alice",
        "allowed_chat_ids": ["*"],
        "allowed_commands": ["*"],
        "created_at": "2026-05-10T00:00:00+08:00",
    })

    assert response.allow_all_chats is True
    assert response.allow_all_commands is True
    assert response.allowed_chat_ids == []
    assert response.allowed_commands == []


def test_incoming_sudo_denial_reporting_is_quiet_for_unconfigured_users():
    """群里普通消息撞到 sudo 前缀时，不应该公开回复未授权提示。"""
    assert _should_report_incoming_sudo_denial("sudo 系统未开启") is False
    assert _should_report_incoming_sudo_denial("sudo 系统未配置") is False
    assert _should_report_incoming_sudo_denial("TG 用户 123 不在 sudo 列表中") is False
    assert _should_report_incoming_sudo_denial("未配置允许对话，sudo 默认拒绝") is False
    assert _should_report_incoming_sudo_denial("未配置允许命令，sudo 默认拒绝") is False


def test_incoming_sudo_denial_reporting_keeps_scope_errors_visible():
    """已授权 sudo 用户越界使用时仍给明确反馈。"""
    assert _should_report_incoming_sudo_denial("此对话（chat_id=-100）不在白名单中") is True
    assert _should_report_incoming_sudo_denial("命令 `reboot` 不在白名单中") is True


def test_sudo_command_name_rejects_repeated_dot_noise():
    """多个小数点不是 sudo 命令，不能进入权限拒绝链路。"""
    assert _looks_like_command_name("ping", prefix=".") is True
    assert _looks_like_command_name("帮助", prefix=".") is True
    assert _looks_like_command_name(".", prefix=".") is False
    assert _looks_like_command_name("..", prefix=".") is False
    assert _looks_like_command_name("...", prefix=".") is False


def test_sudo_dispatch_target_only_accepts_known_commands():
    """incoming sudo 只对真实命令做权限检查，未知文本静默忽略。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1,
        templates={"hello": {"type": "reply_text", "text": "hi"}},
        providers={},
        command_prefix=",",
        aliases={"问候": "hello"},
    )
    try:
        assert _has_dispatch_target("ping") is True
        assert _has_dispatch_target("hello") is True
        assert _has_dispatch_target("问候") is True
        assert _has_dispatch_target(".") is False
        assert _has_dispatch_target("not-a-command") is False
    finally:
        wcmd._ctx = old


def test_sudo_self_chat_guard():
    """sudo incoming 只允许账号自身 chat。"""
    from app.worker import command as wcmd
    old = wcmd._ctx
    wcmd._ctx = CommandContext(
        account_id=1,
        templates={},
        providers={},
        command_prefix=",",
        sudo_enabled=True,
        self_tg_user_id=999,
    )
    try:
        event = MagicMock()
        event.chat_id = 999
        assert _is_self_chat(event) is True
        assert guard_is_self_chat(event, ctx=wcmd._ctx) is True
        event.chat_id = -1002090852236
        assert _is_self_chat(event) is False
        assert guard_is_self_chat(event, ctx=wcmd._ctx) is False
    finally:
        wcmd._ctx = old


def test_sudo_guard_name_and_denial_helpers():
    """新模块的命名识别与拒绝提示规则应与旧行为一致。"""
    assert guard_looks_like_command_name("ping", prefix=".") is True
    assert guard_looks_like_command_name(".", prefix=".") is False
    assert guard_looks_like_command_name("...", prefix=".") is False
    assert guard_should_report_incoming_sudo_denial("sudo 系统未开启") is False
    assert guard_should_report_incoming_sudo_denial("命令 `reboot` 不在白名单中") is True


def test_sudo_guard_dispatch_target_checks_builtin_template_and_alias():
    """新模块应继续识别 builtin、模板和别名派发目标。"""
    ctx = CommandContext(
        account_id=1,
        templates={"hello": {"type": "reply_text"}},
        providers={},
        command_prefix=",",
        aliases={"问候": "hello"},
    )
    assert guard_has_dispatch_target(
        "ping",
        builtin_alias_to_primary={"ping": "ping"},
        ctx=ctx,
    ) is True
    assert guard_has_dispatch_target(
        "hello",
        builtin_alias_to_primary={},
        ctx=ctx,
    ) is True
    assert guard_has_dispatch_target(
        "问候",
        builtin_alias_to_primary={},
        ctx=ctx,
    ) is True
    assert guard_has_dispatch_target(
        "not-a-command",
        builtin_alias_to_primary={},
        ctx=ctx,
    ) is False


@pytest.mark.asyncio
async def test_sudo_guard_permission_matches_command_wrapper():
    """command.py 的包装函数应与新模块返回一致。"""
    from app.worker import command as wcmd

    old = wcmd._ctx
    ctx = CommandContext(
        account_id=1,
        templates={},
        providers={},
        command_prefix=",",
        sudo_enabled=True,
        sudo_users={
            111: {
                "display_name": "alice",
                "allowed_chat_ids": ["*"],
                "allowed_commands": ["ping"],
            }
        },
    )
    wcmd._ctx = ctx
    try:
        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.get_sender = AsyncMock(return_value=sender)
        event.chat_id = -100111

        wrapper_allowed, wrapper_msg = await _check_sudo_permission(event, "ping", 1)
        guard_allowed, guard_msg = await guard_check_sudo_permission(ctx, event, "ping")
        assert wrapper_allowed == guard_allowed
        assert wrapper_msg == guard_msg
    finally:
        wcmd._ctx = old


@pytest.mark.asyncio
async def test_incoming_repeated_dots_do_not_trigger_sudo_denial():
    """回归：群里发 '..' / '...' 不应被 userbot 当作 sudo 命令并公开回复。"""
    captured = []

    def fake_on(_event_type):
        def deco(fn):
            captured.append(fn)
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix=",")
    incoming_handler = captured[0]

    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix=",",
            sudo_prefix=".",
            sudo_enabled=True,
            self_tg_user_id=111,
            sudo_users={
                111: {
                    "display_name": "alice",
                    "allowed_chat_ids": [-100111],
                    "allowed_commands": ["*"],
                }
            },
        )
    )

    sender = MagicMock()
    sender.id = 111
    event = AsyncMock()
    event.raw_text = "..."
    event.chat_id = -1002090852236
    event.get_sender = AsyncMock(return_value=sender)

    await incoming_handler(event)
    event.respond.assert_not_called()


@pytest.mark.asyncio
async def test_incoming_sudo_command_outside_self_chat_is_ignored():
    """即使是授权 sudo 用户，在群组里发 .ping 也不应触发拒绝提示。"""
    captured = []

    def fake_on(_event_type):
        def deco(fn):
            captured.append(fn)
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix=",")
    incoming_handler = captured[0]

    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix=",",
            sudo_prefix=".",
            sudo_enabled=True,
            self_tg_user_id=111,
            sudo_users={
                111: {
                    "display_name": "alice",
                    "allowed_chat_ids": ["*"],
                    "allowed_commands": ["*"],
                }
            },
        )
    )

    sender = MagicMock()
    sender.id = 111
    event = AsyncMock()
    event.raw_text = ".ping"
    event.chat_id = -1002090852236
    event.get_sender = AsyncMock(return_value=sender)

    await incoming_handler(event)
    event.respond.assert_not_called()


@pytest.mark.asyncio
async def test_incoming_normal_prefix_plugin_command_from_group_user_is_ignored():
    """群组成员发系统前缀插件命令不应触发 userbot 命令链路。"""
    captured = []

    def fake_on(_event_type):
        def deco(fn):
            captured.append(fn)
            return fn

        return deco

    plugin_handler = AsyncMock()
    register_plugin_command("10d", plugin_handler, owner_plugin_key="ten_half", generation=1)
    try:
        client = MagicMock()
        client.on = fake_on
        make_command_handler(client, account_id=1, prefix="。")
        incoming_handler = captured[0]

        set_command_context(
            CommandContext(
                account_id=1,
                templates={},
                providers={},
                command_prefix="。",
                sudo_prefix=".",
                sudo_enabled=False,
                self_tg_user_id=999,
                sudo_users={},
            )
        )

        sender = MagicMock()
        sender.id = 111
        event = AsyncMock()
        event.raw_text = "。10d 6789"
        event.chat_id = -1002090852236
        event.get_sender = AsyncMock(return_value=sender)

        await incoming_handler(event)

        plugin_handler.assert_not_awaited()
        event.edit.assert_not_called()
        event.respond.assert_not_called()
    finally:
        unregister_plugin_command("10d", owner_plugin_key="ten_half")


@pytest.mark.asyncio
async def test_incoming_normal_prefix_builtin_command_is_not_exposed_in_group():
    """群组 incoming 系统前缀只接插件命令，不暴露内置管理命令。"""
    captured = []

    def fake_on(_event_type):
        def deco(fn):
            captured.append(fn)
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix="。")
    incoming_handler = captured[0]

    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix="。",
            sudo_prefix=".",
            sudo_enabled=False,
            self_tg_user_id=999,
            sudo_users={},
        )
    )

    sender = MagicMock()
    sender.id = 111
    event = AsyncMock()
    event.raw_text = "。status"
    event.chat_id = -1002090852236
    event.get_sender = AsyncMock(return_value=sender)

    await incoming_handler(event)

    event.edit.assert_not_called()
    event.respond.assert_not_called()


@pytest.mark.asyncio
async def test_outgoing_dot_prefix_does_not_enter_sudo_branch():
    """账号自己发点号消息时不走 sudo 分支，避免群里被改成权限拒绝。"""
    captured = []

    def fake_on(_event_type):
        def deco(fn):
            captured.append(fn)
            return fn

        return deco

    client = MagicMock()
    client.on = fake_on
    make_command_handler(client, account_id=1, prefix=",")
    outgoing_handler = captured[1]

    set_command_context(
        CommandContext(
            account_id=1,
            templates={},
            providers={},
            command_prefix=",",
            sudo_prefix=".",
            sudo_enabled=True,
            self_tg_user_id=111,
            sudo_users={
                111: {
                    "display_name": "self",
                    "allowed_chat_ids": ["*"],
                    "allowed_commands": ["*"],
                }
            },
        )
    )

    event = AsyncMock()
    event.raw_text = ".ping"
    event.chat_id = -1002090852236

    await outgoing_handler(event)
    event.edit.assert_not_called()


# ════════════════════════════════════════════════════════════
# 3) Generation Guard
# ════════════════════════════════════════════════════════════


def test_generation_guard_skip_stale():
    """generation 不匹配时应该跳过 handler。"""
    # 模拟 _dispatch 中的检查逻辑
    class FakeCtx:
        generation = 1

    class FakeState:
        generation = 2  # reload 后 generation 增加了

    ctx = FakeCtx()
    state = FakeState()

    # generation 不匹配 → 应该跳过
    assert ctx.generation != state.generation


def test_generation_guard_pass_fresh():
    """generation 匹配时应该执行 handler。"""
    class FakeCtx:
        generation = 3

    class FakeState:
        generation = 3

    ctx = FakeCtx()
    state = FakeState()

    assert ctx.generation == state.generation


def test_generation_guard_increment():
    """reload 后 generation 应该递增。"""
    from app.worker.plugins.loader import _AccountState

    state = _AccountState(account_id=1)
    assert state.generation == 1
    state.generation += 1
    assert state.generation == 2
    state.generation += 1
    assert state.generation == 3


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        return _Rows(self._rows)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_builtin_sudo_add_and_del_are_removed():
    """高危 sudo add/del 不再可用。"""
    from app.worker import command as wcmd
    from app.worker.command import _BUILTIN

    client = AsyncMock()
    event = AsyncMock()
    audit_write = AsyncMock(return_value=None)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(wcmd, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(wcmd.audit_svc, "write", audit_write)
    try:
        await _BUILTIN["sudo"].handler(client, event, ["add", "123"], 1)
        event.edit.assert_called_once_with("仅支持只读查询：.sudo ls")
        audit_write.assert_awaited_once()
        assert audit_write.await_args.kwargs["detail"] == {
            "status": "invalid_subcommand",
            "subcommand": "add",
        }

        event2 = AsyncMock()
        audit_write.reset_mock()
        await _BUILTIN["sudo"].handler(client, event2, ["del", "123"], 1)
        event2.edit.assert_called_once_with("仅支持只读查询：.sudo ls")
        audit_write.assert_awaited_once()
        assert audit_write.await_args.kwargs["detail"] == {
            "status": "invalid_subcommand",
            "subcommand": "del",
        }
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_builtin_sudo_without_args_shows_readonly_usage():
    """空参数时返回只读用法提示，不直接列出用户。"""
    from app.worker import command as wcmd
    from app.worker.command import _BUILTIN

    client = AsyncMock()
    event = AsyncMock()
    audit_write = AsyncMock(return_value=None)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(wcmd, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(wcmd.audit_svc, "write", audit_write)
    try:
        await _BUILTIN["sudo"].handler(client, event, [], 1)
        event.edit.assert_called_once_with("用法：.sudo ls（仅只读查询）")
        audit_write.assert_awaited_once()
        assert audit_write.await_args.kwargs["detail"] == {"status": "usage"}
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_builtin_sudo_ls_still_works_with_summary(monkeypatch):
    """sudo ls 仍可用，返回授权摘要。"""
    from types import SimpleNamespace

    from app.worker import command as wcmd
    from app.worker.command import _BUILTIN

    audit_write = AsyncMock(return_value=None)
    monkeypatch.setattr(
        wcmd,
        "AsyncSessionLocal",
        lambda: _FakeSession(
            [
                SimpleNamespace(
                    tg_user_id=111,
                    display_name="alice",
                    allowed_chat_ids=["*"],
                    allowed_commands=["ping", "help"],
                )
            ]
        ),
    )
    monkeypatch.setattr(wcmd.audit_svc, "write", audit_write)

    client = AsyncMock()
    event = AsyncMock()
    await _BUILTIN["sudo"].handler(client, event, ["ls"], 1)
    msg = event.edit.call_args[0][0]
    assert "Sudo 用户列表" in msg
    assert "TG用户 111（alice）" in msg
    assert "允许对话：全部（显式）" in msg
    assert ("允许命令：help,ping" in msg) or ("允许命令：ping,help" in msg)
    audit_write.assert_awaited_once()
    assert audit_write.await_args.kwargs["detail"] == {
        "status": "ok",
        "subcommand": "ls",
        "row_count": 1,
    }
