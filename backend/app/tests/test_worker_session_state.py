"""Worker session 失效状态收敛回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models.account import ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_LOGIN_REQUIRED
from app.worker import runtime


class _SessionContext:
    def __init__(self, account: SimpleNamespace) -> None:
        self.account = account
        self.commit = AsyncMock()

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_args) -> None:  # noqa: ANN002
        return None

    async def get(self, _model, _account_id: int):  # noqa: ANN001, ANN202
        return self.account


@pytest.mark.asyncio
async def test_login_required_is_persisted_even_when_redis_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = SimpleNamespace(status=ACCOUNT_STATUS_ACTIVE)
    session = _SessionContext(account)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: session)
    redis = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError("redis down")))

    await runtime._handle_login_required(redis, 12, message="session invalid")

    assert account.status == ACCOUNT_STATUS_LOGIN_REQUIRED
    session.commit.assert_awaited_once()
    redis.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_required_event_is_published_when_database_update_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenSession:
        async def __aenter__(self):  # noqa: ANN204
            raise ConnectionError("db down")

        async def __aexit__(self, *_args) -> None:  # noqa: ANN002
            return None

    monkeypatch.setattr(runtime, "AsyncSessionLocal", _BrokenSession)
    redis = SimpleNamespace(publish=AsyncMock())

    await runtime._handle_login_required(redis, 13, reason="SessionRevokedError")

    redis.publish.assert_awaited_once()
