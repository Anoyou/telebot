"""代理 URL 归一化。

用户经常从 Surge/Clash 里直接复制完整代理 URL；后端应在入库前拆开，
避免把 ``http://host:port`` 当成 DNS 主机名。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import proxies as proxies_api
from app.api.proxies import ProxyUpdate, _parse_proxy_url, patch_proxy
from app.services import gateway_runtime, llm_proxy_service, login_service
from app.services.gateway_runtime import GatewayRuntimeStatus
from app.services.llm_proxy_service import resolve_proxy_url
from app.services.system_agent.context import ToolContext
from app.services.system_agent.tools import connectivity
from app.services.system_agent.tools.connectivity import save_proxy_execute
from app.util.proxy import ProxyConfigError, parse_proxy_url
from app.worker.tg_client import build_proxy_tuple


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


def test_proxy_api_rejects_mtproxy_url() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _parse_proxy_url("mtproxy://proxy.example:443?secret=deadbeef")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_PROXY_TYPE"


def test_default_mtproxy_fails_closed_instead_of_becoming_direct() -> None:
    with pytest.raises(ProxyConfigError, match="MTProxy 当前不受支持"):
        parse_proxy_url("mtproxy://proxy.example:443?secret=deadbeef")


@pytest.mark.parametrize(
    "value",
    ["ftp://proxy.example:21", "socks5://proxy.example"],
)
def test_nonempty_invalid_default_proxy_never_becomes_direct(value: str) -> None:
    with pytest.raises(ProxyConfigError):
        parse_proxy_url(value)


def test_worker_rejects_legacy_mtproxy_row() -> None:
    proxy = SimpleNamespace(
        type="mtproxy",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )

    with pytest.raises(ValueError, match="不支持的 Telegram 代理类型"):
        build_proxy_tuple(proxy)


def test_worker_normalizes_historical_proxy_type_case_and_https() -> None:
    proxy = SimpleNamespace(
        type="HTTPS",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )

    assert build_proxy_tuple(proxy) == (
        "http",
        "proxy.example",
        443,
        True,
        None,
        None,
    )


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
async def test_web_legacy_proxy_migration_clears_old_credentials_by_default() -> None:
    proxy = SimpleNamespace(
        id=6,
        type="mtproxy",
        host="legacy.local",
        port=443,
        username="legacy-user",
        password_enc=b"legacy-secret",
    )
    db = AsyncMock()
    db.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(return_value=_ScalarResult(None))

    with (
        patch("app.api.proxies.audit.write", AsyncMock()),
        patch.object(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock()),
    ):
        await patch_proxy(
            6,
            ProxyUpdate(type="socks5", host="new.local", port=1080),
            db,
            SimpleNamespace(id=1),
        )

    assert proxy.type == "socks5"
    assert proxy.username is None
    assert proxy.password_enc is None


@pytest.mark.asyncio
async def test_web_legacy_proxy_migration_applies_explicit_new_credentials(
    monkeypatch,
) -> None:
    proxy = SimpleNamespace(
        id=7,
        type="mtproxy",
        host="legacy.local",
        port=443,
        username="legacy-user",
        password_enc=b"legacy-secret",
    )
    db = AsyncMock()
    db.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(return_value=_ScalarResult(None))
    monkeypatch.setattr(proxies_api, "encrypt_str", lambda value: f"enc:{value}")
    monkeypatch.setattr(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock())
    monkeypatch.setattr("app.api.proxies.audit.write", AsyncMock())

    await patch_proxy(
        7,
        ProxyUpdate(
            host="http://new-user:new-pass@new.local:8080",
        ),
        db,
        SimpleNamespace(id=1),
    )

    assert proxy.type == "http"
    assert proxy.username == "new-user"
    assert proxy.password_enc == "enc:new-pass"


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
async def test_web_rejects_unsupported_mtproxy_type(monkeypatch) -> None:  # noqa: ANN001
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

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_PROXY_TYPE"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_normalizes_historical_proxy_type_case_on_update(monkeypatch) -> None:  # noqa: ANN001
    proxy = SimpleNamespace(
        id=6,
        type="HTTPS",
        host="old.local",
        port=443,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get_bind = lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(return_value=_ScalarResult(None))
    monkeypatch.setattr(gateway_runtime, "gateway_provider_transaction_lock", asyncio.Lock())
    monkeypatch.setattr("app.api.proxies.audit.write", AsyncMock())

    await patch_proxy(
        6,
        ProxyUpdate(host="new.local"),
        db,
        SimpleNamespace(id=1),
    )

    assert proxy.type == "https"
    assert proxy.host == "new.local"
    db.commit.assert_awaited_once()


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
async def test_system_agent_normalizes_historical_proxy_type_case_on_update() -> None:
    proxy = SimpleNamespace(
        id=7,
        type="SOCKS5",
        host="old.local",
        port=1080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(side_effect=[_RowsResult([]), _RowsResult([])])

    await save_proxy_execute(
        ToolContext(db=db, channel="web", role="admin"),
        {"id": 7, "host": "new.local"},
    )

    assert proxy.type == "socks5"
    assert proxy.host == "new.local"


@pytest.mark.asyncio
async def test_system_agent_legacy_proxy_migration_clears_old_credentials() -> None:
    proxy = SimpleNamespace(
        id=8,
        type="mtproxy",
        host="legacy.local",
        port=443,
        username="legacy-user",
        password_enc=b"legacy-secret",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(side_effect=[_RowsResult([]), _RowsResult([])])

    await save_proxy_execute(
        ToolContext(db=db, channel="web", role="admin"),
        {"id": 8, "type": "http", "host": "new.local", "port": 8080},
    )

    assert proxy.type == "http"
    assert proxy.username is None
    assert proxy.password_enc is None


@pytest.mark.asyncio
async def test_system_agent_legacy_proxy_migration_applies_new_credentials(
    monkeypatch,
) -> None:
    proxy = SimpleNamespace(
        id=9,
        type="mtproxy",
        host="legacy.local",
        port=443,
        username="legacy-user",
        password_enc=b"legacy-secret",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    db.execute = AsyncMock(side_effect=[_RowsResult([]), _RowsResult([])])
    monkeypatch.setattr(connectivity, "encrypt_str", lambda value: f"enc:{value}")

    await save_proxy_execute(
        ToolContext(db=db, channel="web", role="admin"),
        {
            "id": 9,
            "type": "socks5",
            "host": "new.local",
            "port": 1080,
            "username": "new-user",
            "password": "new-pass",
        },
    )

    assert proxy.type == "socks5"
    assert proxy.username == "new-user"
    assert proxy.password_enc == "enc:new-pass"


@pytest.mark.asyncio
async def test_system_agent_rejects_unsupported_mtproxy_type() -> None:
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

    with pytest.raises(ValueError, match="代理类型必须是 socks5、http 或 https"):
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


@pytest.mark.asyncio
async def test_login_proxy_tuple_rejects_invalid_legacy_full_url_as_422() -> None:
    proxy = SimpleNamespace(
        type="socks5",
        host="ftp://proxy.example:21",
        port=1080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)

    with pytest.raises(HTTPException) as exc_info:
        await login_service._build_proxy_tuple(db, 1)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "PROXY_CONFIG_INVALID"


@pytest.mark.asyncio
async def test_login_proxy_tuple_normalizes_historical_proxy_type_case_and_https() -> None:
    proxy = SimpleNamespace(
        type="HTTPS",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)

    assert await login_service._build_proxy_tuple(db, 1) == (
        "http",
        "proxy.example",
        443,
        True,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_login_rejects_legacy_mtproxy_row() -> None:
    proxy = SimpleNamespace(
        type="mtproxy",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)

    with pytest.raises(HTTPException) as exc_info:
        await login_service._build_proxy_tuple(db, 1)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "PROXY_TYPE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_login_rejects_broken_proxy_credentials(monkeypatch) -> None:
    proxy = SimpleNamespace(
        type="socks5",
        host="proxy.example",
        port=1080,
        username="alice",
        password_enc=b"broken",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    monkeypatch.setattr(
        login_service,
        "decrypt_str",
        lambda _value: (_ for _ in ()).throw(ValueError("bad master key")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await login_service._build_proxy_tuple(db, 1)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "PROXY_CONFIG_INVALID"


@pytest.mark.asyncio
async def test_login_rejects_missing_explicit_proxy_instead_of_falling_back() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await login_service._build_proxy_tuple(db, 999)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "PROXY_NOT_FOUND"


@pytest.mark.asyncio
async def test_login_missing_proxy_never_constructs_telegram_client(monkeypatch) -> None:  # noqa: ANN001
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    constructed = False

    def _telegram_client(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal constructed
        constructed = True
        raise AssertionError("失效代理 ID 不得进入 Telegram 客户端构造")

    monkeypatch.setattr(login_service, "TelegramClient", _telegram_client)

    with pytest.raises(HTTPException) as exc_info:
        await login_service.start_login(
            db,
            api_id=12345,
            api_hash="hash",
            phone="+8613800000000",
            proxy_id=999,
        )

    assert exc_info.value.detail["code"] == "PROXY_NOT_FOUND"
    assert constructed is False


@pytest.mark.asyncio
async def test_llm_proxy_resolution_rejects_missing_explicit_proxy() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(ProxyConfigError, match="代理 #999 不存在"):
        await resolve_proxy_url(db, 999)


@pytest.mark.asyncio
async def test_llm_proxy_resolution_rejects_broken_proxy_credentials(
    monkeypatch,
) -> None:
    proxy = SimpleNamespace(
        type="socks5",
        host="proxy.example",
        port=1080,
        username="alice",
        password_enc=b"broken",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    monkeypatch.setattr(
        llm_proxy_service,
        "decrypt_str",
        lambda _value: (_ for _ in ()).throw(ValueError("bad master key")),
    )

    with pytest.raises(ProxyConfigError, match="凭据无法解密"):
        await resolve_proxy_url(db, 1)


@pytest.mark.asyncio
async def test_system_agent_rejects_legacy_proxy_before_endpoint_probe(monkeypatch) -> None:  # noqa: ANN001
    proxy = SimpleNamespace(
        id=4,
        type="mtproxy",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(connectivity, "_probe_endpoint", probe)

    result = await connectivity.test_proxy(
        ToolContext(db=db, channel="web", role="admin"),
        {"id": 4},
    )

    assert result == {
        "ok": False,
        "proxy_id": 4,
        "error": "不支持的代理类型，请先迁移",
    }
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_agent_accepts_historical_proxy_type_case(monkeypatch) -> None:  # noqa: ANN001
    proxy = SimpleNamespace(
        id=4,
        type="SOCKS5",
        host="proxy.example",
        port=1080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    probe = AsyncMock(return_value="case-normalized")
    monkeypatch.setattr(connectivity, "_probe_endpoint", probe)

    result = await connectivity.test_proxy(
        ToolContext(db=db, channel="web", role="admin"),
        {"id": 4},
    )

    assert result == {
        "ok": False,
        "proxy_id": 4,
        "error": "case-normalized",
    }
    probe.assert_awaited_once_with("proxy.example", 1080)


@pytest.mark.asyncio
async def test_system_agent_rejects_broken_credentials_before_endpoint_probe(
    monkeypatch,
) -> None:
    proxy = SimpleNamespace(
        id=4,
        type="socks5",
        host="proxy.example",
        port=1080,
        username="alice",
        password_enc=b"broken",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(connectivity, "_probe_endpoint", probe)
    monkeypatch.setattr(
        connectivity,
        "decrypt_str",
        lambda _value: (_ for _ in ()).throw(ValueError("bad master key")),
    )

    result = await connectivity.test_proxy(
        ToolContext(db=db, channel="web", role="admin"),
        {"id": 4},
    )

    assert result == {
        "ok": False,
        "proxy_id": 4,
        "error": "代理凭据无法解密，请重新保存或更换代理",
    }
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_proxy_test_rejects_broken_credentials_before_endpoint_probe(
    monkeypatch,
) -> None:
    proxy = SimpleNamespace(
        id=5,
        type="http",
        host="proxy.example",
        port=8080,
        username="alice",
        password_enc=b"broken",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(proxies_api, "_probe_proxy_endpoint", probe)
    monkeypatch.setattr(
        proxies_api,
        "decrypt_str",
        lambda _value: (_ for _ in ()).throw(ValueError("bad master key")),
    )

    result = await proxies_api.test_proxy(5, db, SimpleNamespace(id=1))

    assert result.ok is False
    assert result.error == "代理凭据无法解密，请重新保存或更换代理"
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_proxy_test_accepts_historical_proxy_type_case(monkeypatch) -> None:  # noqa: ANN001
    proxy = SimpleNamespace(
        id=5,
        type="HTTP",
        host="proxy.example",
        port=8080,
        username=None,
        password_enc=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=proxy)
    probe = AsyncMock(return_value="case-normalized")
    monkeypatch.setattr(proxies_api, "_probe_proxy_endpoint", probe)

    result = await proxies_api.test_proxy(5, db, SimpleNamespace(id=1))

    assert result.ok is False
    assert result.error == "case-normalized"
    probe.assert_awaited_once_with("proxy.example", 8080)
