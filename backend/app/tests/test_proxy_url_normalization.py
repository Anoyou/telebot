"""代理 URL 归一化。

用户经常从 Surge/Clash 里直接复制完整代理 URL；后端应在入库前拆开，
避免把 ``http://host:port`` 当成 DNS 主机名。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.proxies import ProxyUpdate, _parse_proxy_url, patch_proxy
from app.services import gateway_runtime, login_service
from app.services.gateway_runtime import GatewayRuntimeStatus
from app.services.system_agent.context import ToolContext
from app.services.system_agent.tools.connectivity import save_proxy_execute


class _ScalarResult:
    def __init__(self, value):  # noqa: ANN001
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):  # noqa: ANN001
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self


def test_parse_proxy_url_splits_http_url() -> None:
    parsed = _parse_proxy_url("http://10.10.8.33:6152")

    assert parsed == {
        "type": "http",
        "host": "10.10.8.33",
        "port": 6152,
    }


def test_parse_proxy_url_splits_auth_and_socks_url() -> None:
    parsed = _parse_proxy_url("socks5://user%40mail:pa%23ss@127.0.0.1:6153")

    assert parsed == {
        "type": "socks5",
        "host": "127.0.0.1",
        "port": 6153,
        "username": "user@mail",
        "password": "pa#ss",
    }


def test_parse_proxy_url_ignores_plain_host() -> None:
    assert _parse_proxy_url("10.10.8.33") is None


def test_parse_proxy_url_rejects_unknown_scheme() -> None:
    with pytest.raises(Exception) as exc_info:
        _parse_proxy_url("ftp://10.10.8.33:6152")

    assert exc_info.value.detail["code"] == "INVALID_PROXY_TYPE"


@pytest.mark.asyncio
async def test_patch_proxy_prefers_pasted_url_over_stale_form_fields() -> None:
    proxy = SimpleNamespace(
        id=1,
        type="socks5",
        host="old.local",
        port=1080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(return_value=_ScalarResult(None))

    with (
        patch("app.api.proxies.audit.write", AsyncMock()),
        patch.object(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock()),
    ):
        out = await patch_proxy(
            1,
            ProxyUpdate(
                type="socks5",
                host="http://10.10.8.33:6152",
                port=1080,
            ),
            db,
            SimpleNamespace(id=1),
        )

    assert proxy.type == "http"
    assert proxy.host == "10.10.8.33"
    assert proxy.port == 6152
    assert out.type == "http"


@pytest.mark.asyncio
async def test_patch_referenced_gateway_proxy_syncs_before_commit(monkeypatch) -> None:  # noqa: ANN001
    proxy = SimpleNamespace(
        id=2,
        type="http",
        host="old.local",
        port=8080,
        username="old-user",
        password_enc=None,
    )
    events: list[str] = []
    db = AsyncMock()
    db.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(return_value=_ScalarResult(91))
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    sync = AsyncMock(
        side_effect=lambda _db: (
            events.append("sync")
            or GatewayRuntimeStatus("ready", True, 2, 1, version="test")
        )
    )
    monkeypatch.setattr(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock())
    monkeypatch.setattr(gateway_runtime, "reconcile_gateway_runtime_from_session", sync)
    monkeypatch.setattr("app.api.proxies.audit.write", AsyncMock())

    await patch_proxy(
        2,
        ProxyUpdate(host="new.local", password="rotated-secret"),
        db,
        SimpleNamespace(id=1),
    )

    assert events == ["sync", "commit"]
    assert proxy.host == "new.local"
    sync.assert_awaited_once_with(db)


@pytest.mark.asyncio
async def test_web_rejects_mtproxy_for_referenced_llm_proxy(monkeypatch) -> None:  # noqa: ANN001
    proxy = SimpleNamespace(
        id=5,
        type="http",
        host="old.local",
        port=8080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(return_value=_ScalarResult(93))
    monkeypatch.setattr(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock())
    monkeypatch.setattr("app.api.proxies.audit.write", AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await patch_proxy(
            5,
            ProxyUpdate(type="mtproxy"),
            db,
            SimpleNamespace(id=1),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LLM_PROXY_TYPE_INVALID"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_agent_proxy_update_marks_gateway_candidate_sync() -> None:
    proxy = SimpleNamespace(
        id=3,
        type="http",
        host="old.local",
        port=8080,
        username=None,
        password_enc=None,
    )
    action = SimpleNamespace(arguments={})
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(
        side_effect=[
            _RowsResult([]),
            _RowsResult([(91, "codex_gateway")]),
        ]
    )

    ctx = ToolContext(db=db, channel="web", role="admin", action=action)
    await save_proxy_execute(
        ctx,
        {"id": 3, "host": "new.local"},
    )

    assert ctx.gateway_candidate_sync is True
    assert proxy.host == "new.local"


@pytest.mark.asyncio
async def test_system_agent_rejects_mtproxy_for_referenced_llm_proxy() -> None:
    proxy = SimpleNamespace(
        id=4,
        type="http",
        host="old.local",
        port=8080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(
        side_effect=[
            _RowsResult([]),
            _RowsResult([(92, "direct")]),
        ]
    )

    with pytest.raises(ValueError, match="不能改为 MTProxy"):
        await save_proxy_execute(
            ToolContext(db=db, channel="web", role="admin"),
            {"id": 4, "type": "mtproxy"},
        )


@pytest.mark.asyncio
async def test_login_proxy_tuple_accepts_legacy_full_url_host() -> None:
    proxy = SimpleNamespace(
        type="socks5",
        host="http://10.10.8.33:6152",
        port=1080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)

    assert await login_service._build_proxy_tuple(db, 1) == (
        "http",
        "10.10.8.33",
        6152,
        True,
        None,
        None,
    )
