"""Worker session 失效状态收敛回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models.account import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_LOGIN_REQUIRED,
    ACCOUNT_STATUS_PAUSED,
)
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


@pytest.mark.asyncio
async def test_proxy_configuration_error_pauses_account_and_publishes_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = SimpleNamespace(status=ACCOUNT_STATUS_ACTIVE)
    session = _SessionContext(account)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: session)
    redis = SimpleNamespace(
        publish=AsyncMock(),
        rpush=AsyncMock(),
        ltrim=AsyncMock(),
    )

    await runtime._handle_proxy_configuration_error(
        redis,
        14,
        ValueError("MTProxy 当前不受支持"),
    )

    assert account.status == ACCOUNT_STATUS_PAUSED
    session.commit.assert_awaited_once()
    redis.rpush.assert_awaited_once()
    redis.publish.assert_awaited_once()
    published = redis.publish.await_args.args[1]
    assert '"status":"paused"' in published
    assert '"reason":"proxy_configuration_error"' in published


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy",
    [
        SimpleNamespace(
            id=8,
            type="mtproxy",
            host="proxy.example",
            port=443,
            username=None,
            password_enc=None,
        ),
        None,
    ],
    ids=["legacy-type", "missing-row"],
)
async def test_worker_invalid_proxy_is_handled_before_supervisor_crash_sequence(
    monkeypatch: pytest.MonkeyPatch,
    proxy,
) -> None:
    account = SimpleNamespace(id=15, proxy_id=8, status=ACCOUNT_STATUS_ACTIVE)

    class _Result:
        def scalar_one_or_none(self):  # noqa: ANN201
            return account

    class _WorkerSession:
        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN002
            return None

        async def execute(self, _query):  # noqa: ANN001, ANN201
            return _Result()

        async def get(self, model, _key):  # noqa: ANN001, ANN201
            return proxy if getattr(model, "__name__", "") == "Proxy" else None

    redis = SimpleNamespace()
    handled = AsyncMock()
    bootstrap = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", _WorkerSession)
    monkeypatch.setattr(runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(runtime, "_handle_proxy_configuration_error", handled)
    monkeypatch.setattr(runtime, "_bootstrap_platform_capabilities", bootstrap)
    from app.services import llm_usage_service

    monkeypatch.setattr(
        llm_usage_service,
        "ensure_llm_usage_callback_registered",
        lambda: None,
    )

    await runtime.run_worker(15)

    handled.assert_awaited_once()
    assert handled.await_args.args[1] == 15
    bootstrap.assert_not_awaited()
