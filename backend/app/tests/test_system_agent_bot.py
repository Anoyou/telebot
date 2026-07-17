"""管理 Bot /agent 桥接与 Inline 确认。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services.system_agent import bot_bridge


class _SendCapture:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        msg: str,
        *,
        edit: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append({"msg": msg, "edit": edit, "reply_markup": reply_markup})


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
    body = send.calls[0]["msg"]
    assert "系统助手" in body
    assert "/agent new" in body
    assert "Inline 确认" in body


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
    assert "退出" in send.calls[0]["msg"]


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
    assert "处理中" in send.calls[0]["msg"]
    assert "12.5" in send.calls[-1]["msg"]
    assert send.calls[-1]["edit"] is True


@pytest.mark.asyncio
async def test_run_agent_query_attaches_inline_confirm(monkeypatch) -> None:
    send = _SendCapture()

    class _Svc:
        async def get_or_create_active_session(self, *a, **k):  # noqa: ANN001
            return SimpleSession("sess-1")

        async def get_session(self, *a, **k):  # noqa: ANN001
            return SimpleSession("sess-1")

        async def stream_message(self, *a, **k):  # noqa: ANN001
            yield {
                "type": "action_proposed",
                "action": {
                    "id": "act-1",
                    "summary": "暂停账号 #1",
                    "risk": "normal",
                    "tool_name": "accounts.set_paused",
                    "preview": {"note": "不会自动恢复"},
                },
            }
            yield {"type": "assistant_message", "content": "请确认暂停操作"}
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
    monkeypatch.setattr(
        bot_bridge,
        "store_agent_confirm_nonce",
        AsyncMock(return_value="nonce-xyz"),
    )

    await bot_bridge.run_agent_query(
        account_id=7,
        tg_user_id=99,
        role="admin",
        text="暂停账号",
        send=send,
    )
    final = send.calls[-1]
    assert final["edit"] is True
    assert "待确认" in final["msg"]
    assert "暂停账号" in final["msg"]
    markup = final["reply_markup"]
    assert markup is not None
    buttons = markup["inline_keyboard"][0]
    assert any("confirm" in b["callback_data"] for b in buttons)
    assert any("cancel" in b["callback_data"] for b in buttons)
    assert all(b["callback_data"].startswith("ab:7:") for b in buttons)


@pytest.mark.asyncio
async def test_handle_agent_confirm_callback_success() -> None:
    answers: list[tuple[str, bool]] = []
    send = _SendCapture()

    async def answer(text: str, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    with (
        patch.object(
            bot_bridge,
            "read_agent_confirm_payload",
            AsyncMock(
                return_value={
                    "account_id": 1,
                    "tg_user_id": 2,
                    "action": "agent",
                    "action_id": "act-9",
                }
            ),
        ),
        patch.object(
            bot_bridge,
            "consume_agent_confirm_payload",
            AsyncMock(
                return_value={
                    "account_id": 1,
                    "tg_user_id": 2,
                    "action": "agent",
                    "action_id": "act-9",
                }
            ),
        ),
        patch.object(
            bot_bridge,
            "get_action_executor",
            lambda: type(
                "E",
                (),
                {
                    "confirm": AsyncMock(
                        return_value={
                            "ok": True,
                            "action": {"summary": "已暂停账号", "runtime_sync_status": "succeeded"},
                        }
                    )
                },
            )(),
        ),
    ):
        await bot_bridge.handle_agent_confirm_callback(
            account_id=1,
            tg_user_id=2,
            role="admin",
            nonce="n1",
            decide="confirm",
            answer=answer,
            send=send,
        )
    assert any("处理中" in a[0] for a in answers)
    assert any("已确认并执行" in c["msg"] for c in send.calls)


@pytest.mark.asyncio
async def test_handle_agent_cancel_rejects_action() -> None:
    send = _SendCapture()
    answers: list[str] = []

    async def answer(text: str, show_alert: bool = False) -> None:
        answers.append(text)

    reject_mock = AsyncMock()
    get_action_mock = AsyncMock(return_value=object())
    consume_mock = AsyncMock(
        return_value={
            "account_id": 1,
            "tg_user_id": 2,
            "action": "agent",
            "action_id": "act-x",
        }
    )

    class _DB:
        def __init__(self) -> None:
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with (
        patch.object(
            bot_bridge,
            "read_agent_confirm_payload",
            AsyncMock(
                return_value={
                    "account_id": 1,
                    "tg_user_id": 2,
                    "action": "agent",
                    "action_id": "act-x",
                }
            ),
        ),
        patch.object(bot_bridge, "consume_agent_confirm_payload", consume_mock),
        patch.object(bot_bridge, "AsyncSessionLocal", lambda: _DB()),
        patch.object(bot_bridge, "get_action", get_action_mock),
        patch.object(bot_bridge, "reject_action", reject_mock),
    ):
        await bot_bridge.handle_agent_confirm_callback(
            account_id=1,
            tg_user_id=2,
            role="operator",
            nonce="n2",
            decide="cancel",
            answer=answer,
            send=send,
        )
    consume_mock.assert_awaited()
    reject_mock.assert_awaited()
    assert any("取消" in c["msg"] for c in send.calls)


@pytest.mark.asyncio
async def test_cancel_by_non_owner_does_not_consume_nonce() -> None:
    send = _SendCapture()
    answers: list[tuple[str, bool]] = []

    async def answer(text: str, show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    consume_mock = AsyncMock()
    with (
        patch.object(
            bot_bridge,
            "read_agent_confirm_payload",
            AsyncMock(
                return_value={
                    "account_id": 1,
                    "tg_user_id": 2,
                    "action": "agent",
                    "action_id": "act-x",
                }
            ),
        ),
        patch.object(bot_bridge, "consume_agent_confirm_payload", consume_mock),
    ):
        await bot_bridge.handle_agent_confirm_callback(
            account_id=1,
            tg_user_id=999,  # 非本人
            role="admin",
            nonce="n3",
            decide="cancel",
            answer=answer,
            send=send,
        )
    consume_mock.assert_not_awaited()
    assert any("原用户" in a[0] for a in answers)


@pytest.mark.asyncio
async def test_attach_secrets_to_pending_action_short_circuits() -> None:
    send = _SendCapture()
    key = "sk-abcdefghijklmnopqrstuvwxyz123456"

    class _Action:
        id = "act-secret"
        account_id = 1
        tool_name = "providers.save"
        secret_payload_enc = None
        secret_fields = None
        arguments = {"name": "p1"}
        summary = "创建 Provider"
        risk = "normal"
        error_code = "PROVIDER_VERIFY_FAILED"
        error_message = "验证失败"

    action = _Action()

    class _DB:
        def __init__(self) -> None:
            self.commit = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def fake_list(*a, **k):  # noqa: ANN001
        return [action]

    class _Spec:
        secret_argument_names = ("api_key",)

    class _Reg:
        def get(self, _name):  # noqa: ANN001
            return _Spec()

    with (
        patch.object(bot_bridge, "AsyncSessionLocal", lambda: _DB()),
        patch.object(bot_bridge, "list_actions", fake_list),
        patch.object(bot_bridge, "get_registry", lambda: _Reg()),
        patch.object(bot_bridge, "decrypt_secret_payload", lambda _t: {}),
        patch.object(bot_bridge, "encrypt_secret_payload", lambda d: "enc"),
        patch.object(bot_bridge, "store_agent_confirm_nonce", AsyncMock(return_value="n-re")),
    ):
        handled = await bot_bridge.try_attach_secrets_to_pending_action(
            account_id=1,
            tg_user_id=2,
            text=key,
            send=send,
        )
    assert handled is True
    assert action.secret_payload_enc == "enc"
    assert action.arguments.get("has_api_key") is True
    assert send.calls
    assert send.calls[-1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_keep_pending_reissues_inline_keyboard() -> None:
    send = _SendCapture()

    async def answer(text: str, show_alert: bool = False) -> None:
        return None

    with (
        patch.object(
            bot_bridge,
            "read_agent_confirm_payload",
            AsyncMock(
                return_value={
                    "account_id": 1,
                    "tg_user_id": 2,
                    "action": "agent",
                    "action_id": "act-k",
                }
            ),
        ),
        patch.object(
            bot_bridge,
            "consume_agent_confirm_payload",
            AsyncMock(
                return_value={
                    "account_id": 1,
                    "tg_user_id": 2,
                    "action": "agent",
                    "action_id": "act-k",
                }
            ),
        ),
        patch.object(
            bot_bridge,
            "get_action_executor",
            lambda: type(
                "E",
                (),
                {
                    "confirm": AsyncMock(
                        return_value={
                            "ok": False,
                            "keep_pending": True,
                            "error_message": "验证失败：401",
                            "action": {"id": "act-k", "summary": "创建 Provider", "risk": "normal"},
                        }
                    )
                },
            )(),
        ),
        patch.object(bot_bridge, "store_agent_confirm_nonce", AsyncMock(return_value="nonce-new")),
    ):
        await bot_bridge.handle_agent_confirm_callback(
            account_id=1,
            tg_user_id=2,
            role="admin",
            nonce="n-old",
            decide="confirm",
            answer=answer,
            send=send,
        )
    final = send.calls[-1]
    assert "验证失败" in final["msg"] or "仍待确认" in final["msg"]
    assert final["reply_markup"] is not None
    assert any("confirm" in b["callback_data"] for b in final["reply_markup"]["inline_keyboard"][0])
