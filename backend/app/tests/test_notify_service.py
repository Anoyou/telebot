from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from app.db.models.notify import NotifyBot
from app.schemas.notify import NotifyBotCreate
from app.services import notify_service


@dataclass
class _ScalarResult:
    value: object | None

    def scalar_one_or_none(self) -> object | None:
        return self.value

    def scalars(self) -> _ScalarResult:
        return self

    def first(self) -> object | None:
        return self.value


class _SequenceDB:
    def __init__(self, values: list[object | None]) -> None:
        self._values = iter(values)

    async def __aenter__(self) -> _SequenceDB:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(next(self._values))


def _notify_bot(*, name: str = "default", token: str | None = "encrypted") -> NotifyBot:
    return NotifyBot(
        id=1,
        name=name,
        bot_token_enc=token,
        source_account_id=None,
        default_chat_id=1682400007,
        enabled=True,
    )


def test_notify_bot_create_requires_exactly_one_credential_source() -> None:
    direct = NotifyBotCreate(
        name="default",
        bot_token="123:token",
        default_chat_id=1682400007,
    )
    referenced = NotifyBotCreate(
        name="alert",
        source_account_id=7,
        default_chat_id=-1001682400007,
    )

    assert direct.bot_token == "123:token"
    assert referenced.source_account_id == 7

    with pytest.raises(ValidationError):
        NotifyBotCreate(name="default", default_chat_id=1682400007)
    with pytest.raises(ValidationError):
        NotifyBotCreate(
            name="default",
            bot_token="123:token",
            source_account_id=7,
            default_chat_id=1682400007,
        )


@pytest.mark.asyncio
async def test_alert_route_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    default = _notify_bot()
    monkeypatch.setattr(
        notify_service,
        "AsyncSessionLocal",
        lambda: _SequenceDB([None, default]),
    )

    selected = await notify_service._select_bot("alert")

    assert selected is default


@pytest.mark.asyncio
async def test_alert_route_falls_back_to_first_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    first_enabled = _notify_bot(name="operations")
    monkeypatch.setattr(
        notify_service,
        "AsyncSessionLocal",
        lambda: _SequenceDB([None, None, first_enabled]),
    )

    selected = await notify_service._select_bot("alert")

    assert selected is first_enabled


@pytest.mark.asyncio
async def test_resolve_token_uses_referenced_management_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _notify_bot(token=None)
    route.source_account_id = 7
    account_bot = type("AccountBotStub", (), {"bot_token_enc": "account-encrypted"})()
    monkeypatch.setattr(
        notify_service,
        "AsyncSessionLocal",
        lambda: _SequenceDB([account_bot]),
    )
    monkeypatch.setattr(notify_service, "decrypt_str", lambda value: f"clear:{value}")

    assert await notify_service._resolve_token(route) == "clear:account-encrypted"


@pytest.mark.asyncio
async def test_resolve_token_keeps_direct_token_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _notify_bot(token="direct-encrypted")
    monkeypatch.setattr(notify_service, "decrypt_str", lambda value: f"clear:{value}")

    assert await notify_service._resolve_token(route) == "clear:direct-encrypted"


@pytest.mark.asyncio
async def test_resolve_token_returns_none_when_referenced_bot_has_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _notify_bot(token=None)
    route.source_account_id = 7
    account_bot = type("AccountBotStub", (), {"bot_token_enc": None})()
    monkeypatch.setattr(
        notify_service,
        "AsyncSessionLocal",
        lambda: _SequenceDB([account_bot]),
    )

    assert await notify_service._resolve_token(route) is None
