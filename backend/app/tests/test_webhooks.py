from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import webhooks as webhooks_api
from app.db.models.account import Account
from app.db.models.system import SystemSetting
from app.deps import get_current_user, get_db
from app.main import app
from app.worker.ipc import CMD_WEBHOOK_DELIVER
from app.worker.plugins import loader as loader_mod
from app.worker.plugins.base import Plugin, PluginContext
from app.worker.plugins.manifest import Manifest


CSRF_HEADERS = {
    "X-Requested-With": "telepilot-ui",
    "X-CSRF-Token": "test-token",
    "Cookie": "csrf_token=test-token",
}


class _FakeDB:
    def __init__(self, *, token: str = "secret", hook_key: str = "default") -> None:
        self.accounts = {1: SimpleNamespace(id=1, template_id=None)}
        self.settings: dict[str, SystemSetting] = {
            webhooks_api._setting_key(1): SystemSetting(
                key=webhooks_api._setting_key(1),
                value={
                    "token": token,
                    "hooks": [{"key": hook_key, "label": hook_key, "enabled": True}],
                    "created_at": "2026-07-10T00:00:00+00:00",
                    "updated_at": "2026-07-10T00:00:00+00:00",
                },
            )
        }
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, model: Any, key: Any) -> Any:
        table = getattr(model, "__tablename__", "")
        if model is Account or table == "account":
            return self.accounts.get(int(key))
        if model is SystemSetting or table == "system_setting":
            return self.settings.get(str(key))
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, SystemSetting):
            self.settings[row.key] = row

    async def commit(self) -> None:
        self.commits += 1


class _FakeRedis:
    pass


@asynccontextmanager
async def _client(db: _FakeDB):
    previous = dict(app.dependency_overrides)

    async def _override_db():
        yield db

    async def _override_user():
        return SimpleNamespace(id=7, username="tester")

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def _headers(token: str) -> dict[str, str]:
    return {
        **CSRF_HEADERS,
        webhooks_api.TOKEN_HEADER: token,
        "Content-Type": "application/json",
        "User-Agent": "pytest-webhook",
    }


@pytest.mark.asyncio
async def test_deliver_webhook_rejects_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB(token="good-token")
    monkeypatch.setattr(webhooks_api, "publish_cmd_with_ack", AsyncMock())

    async with _client(db) as client:
        response = await client.post(
            "/api/webhooks/1/default",
            headers=_headers("bad-token"),
            json={"event": "demo"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_TOKEN_INVALID"
    webhooks_api.publish_cmd_with_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_webhook_exempt_from_csrf_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """外部系统无法携带 X-Requested-With/CSRF cookie，webhook 路径必须豁免 CSRF 中间件，
    请求应穿过中间件到达端点做 token 校验（而非被 403 CSRF_HEADER_REQUIRED 拦截）。"""
    db = _FakeDB(token="good-token")
    monkeypatch.setattr(webhooks_api, "publish_cmd_with_ack", AsyncMock())

    async with _client(db) as client:
        response = await client.post(
            "/api/webhooks/1/default",
            headers={  # 故意不带 CSRF_HEADERS，模拟第三方外部系统
                webhooks_api.TOKEN_HEADER: "bad-token",
                "Content-Type": "application/json",
                "User-Agent": "external-system",
            },
            json={"event": "demo"},
        )

    # 关键断言：没有被 CSRF 中间件 403 拦截，而是到达端点返回 token 校验结果
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "WEBHOOK_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_deliver_webhook_rejects_unknown_hook_key(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB(token="good-token", hook_key="default")
    monkeypatch.setattr(webhooks_api, "publish_cmd_with_ack", AsyncMock())

    async with _client(db) as client:
        response = await client.post(
            "/api/webhooks/1/missing",
            headers=_headers("good-token"),
            json={"event": "demo"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEBHOOK_HOOK_NOT_FOUND"
    webhooks_api.publish_cmd_with_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_webhook_returns_429_when_bucket_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB(token="good-token", hook_key="default")
    monkeypatch.setattr(webhooks_api, "get_redis", lambda: _FakeRedis())

    async def _limited(*_args: Any, **_kwargs: Any) -> None:
        raise HTTPException(
            status_code=429,
            detail={"code": "WEBHOOK_RATE_LIMITED", "message": "limited"},
            headers={"Retry-After": "3"},
        )

    monkeypatch.setattr(webhooks_api, "_enforce_webhook_rate_limit", _limited)
    monkeypatch.setattr(webhooks_api, "publish_cmd_with_ack", AsyncMock())

    async with _client(db) as client:
        response = await client.post(
            "/api/webhooks/1/default",
            headers=_headers("good-token"),
            json={"event": "demo"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "WEBHOOK_RATE_LIMITED"
    webhooks_api.publish_cmd_with_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_webhook_mock_ipc_reaches_subscribed_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB(token="good-token", hook_key="orders")
    received: list[dict[str, Any]] = []

    class _WebhookPlugin(Plugin):
        key = "_test_webhook_receiver"
        display_name = "Webhook Receiver"

        async def on_event(self, ctx: PluginContext, payload: dict[str, Any]) -> list[dict[str, Any]]:
            received.append(payload)
            return []

    _WebhookPlugin._manifest = Manifest(
        key="_test_webhook_receiver",
        display_name="Webhook Receiver",
        event_subscriptions=[
            {
                "source": ["webhook"],
                "events": ["webhook"],
                "scope": "all_allowed_chats",
                "triggers": {"webhook": "orders"},
                "entry_key": "main",
            }
        ],
    )

    state = loader_mod._AccountState(1)
    state.redis = _FakeRedis()
    state.instances[_WebhookPlugin.key] = _WebhookPlugin()
    state.contexts[_WebhookPlugin.key] = PluginContext(
        account_id=1,
        feature_key=_WebhookPlugin.key,
        redis=state.redis,
        generation=state.generation,
    )
    loader_mod._STATES[1] = state

    async def _publish(redis: Any, account_id: int, cmd_type: str, **payload: Any) -> bool:
        assert redis is state.redis or isinstance(redis, _FakeRedis)
        assert account_id == 1
        assert cmd_type == CMD_WEBHOOK_DELIVER
        payload.pop("timeout", None)
        result = await loader_mod.dispatch_webhook_event(account_id, payload, redis=state.redis)
        return bool(result["ok"])

    monkeypatch.setattr(webhooks_api, "get_redis", lambda: state.redis)
    monkeypatch.setattr(webhooks_api, "_enforce_webhook_rate_limit", AsyncMock(return_value={}))
    monkeypatch.setattr(webhooks_api, "publish_cmd_with_ack", _publish)
    monkeypatch.setattr(
        loader_mod,
        "_load_event_framework_flags",
        AsyncMock(return_value={"trace_enabled": False, "event_bus_delivery_enabled": True}),
    )
    monkeypatch.setattr(loader_mod, "update_plugin_runtime_status", AsyncMock())

    try:
        async with _client(db) as client:
            response = await client.post(
                "/api/webhooks/1/orders",
                headers=_headers("good-token"),
                json={"order_id": "A-1", "status": "paid"},
            )
    finally:
        loader_mod._STATES.pop(1, None)

    assert response.status_code == 202
    assert response.json()["delivered"] is True
    assert len(received) == 1
    payload = received[0]
    assert payload["event_type"] == "webhook"
    assert payload["source"]["channel"] == "webhook"
    assert payload["trigger"]["hook_key"] == "orders"
    assert payload["webhook"]["body"] == {"order_id": "A-1", "status": "paid"}
    assert payload["webhook"]["headers"]["content-type"] == "application/json"
    assert "x-telepilot-webhook-token" not in payload["webhook"]["headers"]
