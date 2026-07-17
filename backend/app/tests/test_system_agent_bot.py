"""管理 Bot /agent 桥接。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.system_agent import bot_bridge


class _SendCapture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, msg: str, *, edit: bool = False) -> None:
        self.calls.append((msg, edit))


@pytest.mark.asyncio
async def test_agent_help_enters_mode_and_shows_usage() -> None:
    send = _SendCapture()
    with (
        patch.object(bot_bridge, "enter_agent_mode", AsyncMock(return_value=True)),
        patch.object(bot_bridge, "is_agent_mode", AsyncMock(return_value=False)),
    ):
        await bot_bridge.handle_agent_command(
            account_id=1,
            tg_user_id=42,
            role="admin",
            text="/agent",
            send=send,
        )
    assert send.calls
    body = send.calls[0][0]
    assert "系统助手" in body
    assert "/agent new" in body
    assert "只读" in body


@pytest.mark.asyncio
async def test_agent_exit() -> None:
    send = _SendCapture()
    exit_mock = AsyncMock()
    with patch.object(bot_bridge, "exit_agent_mode", exit_mock):
        await bot_bridge.handle_agent_command(
            account_id=1,
            tg_user_id=42,
            role="admin",
            text="/agent exit",
            send=send,
        )
    exit_mock.assert_awaited_once_with(1, 42)
    assert "退出" in send.calls[0][0]


@pytest.mark.asyncio
async def test_agent_natural_language_runs_query() -> None:
    send = _SendCapture()
    run_mock = AsyncMock()
    with (
        patch.object(bot_bridge, "enter_agent_mode", AsyncMock(return_value=True)),
        patch.object(bot_bridge, "run_agent_query", run_mock),
    ):
        await bot_bridge.handle_agent_command(
            account_id=3,
            tg_user_id=9,
            role="operator",
            text="/agent 今天收入多少",
            send=send,
        )
    run_mock.assert_awaited_once()
    kwargs = run_mock.await_args.kwargs
    assert kwargs["account_id"] == 3
    assert kwargs["tg_user_id"] == 9
    assert kwargs["text"] == "今天收入多少"
    assert kwargs["role"] == "operator"


@pytest.mark.asyncio
async def test_run_agent_query_edits_final_message(monkeypatch) -> None:
    send = _SendCapture()

    class _Svc:
        async def get_or_create_active_session(self, *a, **k):  # noqa: ANN001
            return SimpleSession("sess-1")

        async def get_session(self, *a, **k):  # noqa: ANN001
            return SimpleSession("sess-1")

        async def stream_message(self, *a, **k):  # noqa: ANN001
            yield {"type": "assistant_message", "content": "今日净收入 12.5"}
            yield {"type": "done", "ok": True}

    class SimpleSession:
        def __init__(self, sid: str) -> None:
            self.id = sid

    class _DB:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(bot_bridge, "AsyncSessionLocal", lambda: _DB())
    monkeypatch.setattr(bot_bridge, "get_system_agent_service", lambda: _Svc())
    monkeypatch.setattr(bot_bridge, "refresh_agent_mode", AsyncMock())

    await bot_bridge.run_agent_query(
        account_id=1,
        tg_user_id=2,
        role="admin",
        text="今日收入",
        send=send,
    )
    assert len(send.calls) >= 2
    assert "处理中" in send.calls[0][0]
    assert "12.5" in send.calls[-1][0]
    assert send.calls[-1][1] is True  # edit final
