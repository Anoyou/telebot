from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.worker.plugins.http_facade import (
    PluginHTTP,
    PluginHTTPPolicyError,
    PluginHTTPResponseTooLarge,
    host_matches_allowed,
)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


async def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_host_matches_exact_single_wildcard_and_deep_wildcard() -> None:
    assert host_matches_allowed("api.example.com", ["api.example.com"])
    assert not host_matches_allowed("x.api.example.com", ["api.example.com"])

    assert host_matches_allowed("api.example.com", ["*.example.com"])
    assert not host_matches_allowed("example.com", ["*.example.com"])
    assert not host_matches_allowed("x.api.example.com", ["*.example.com"])

    assert host_matches_allowed("example.com", ["**.example.com"])
    assert host_matches_allowed("api.example.com", ["**.example.com"])
    assert host_matches_allowed("x.api.example.com", ["**.example.com"])
    assert not host_matches_allowed("badexample.com", ["**.example.com"])


@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("http://localhost/status", ["93.184.216.34"]),
        ("http://127.0.0.1/status", ["93.184.216.34"]),
        ("http://[::1]/status", ["93.184.216.34"]),
        ("http://api.example.com/status", ["10.0.0.8"]),
        ("http://api.example.com/status", ["172.16.0.8"]),
        ("http://api.example.com/status", ["192.168.1.8"]),
        ("http://api.example.com/status", ["169.254.1.8"]),
        ("http://api.example.com/status", ["::ffff:127.0.0.1"]),
        ("http://api.example.com/status", ["fc00::1"]),
    ],
)
@pytest.mark.asyncio
async def test_blocks_localhost_private_link_local_and_ipv4_mapped_ipv6(
    url: str,
    answers: list[str],
) -> None:
    called = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called += 1
        return httpx.Response(200, content=b"should not connect")

    async def _resolver(_host: str, _port: int) -> list[str]:
        return answers

    http = PluginHTTP(
        allowed_hosts=["localhost", "127.0.0.1", "::1", "**.example.com"],
        resolver=_resolver,
        transport=httpx.MockTransport(_handler),
    )

    with pytest.raises(PluginHTTPPolicyError):
        await http.get(url)

    assert called == 0


@pytest.mark.asyncio
async def test_dns_preflight_happens_before_transport_connect() -> None:
    called = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called += 1
        return httpx.Response(200, content=b"should not connect")

    async def _private_resolver(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1"]

    http = PluginHTTP(
        allowed_hosts=["api.example.com"],
        resolver=_private_resolver,
        transport=httpx.MockTransport(_handler),
    )

    with pytest.raises(PluginHTTPPolicyError):
        await http.get("https://api.example.com/v1")

    assert called == 0


@pytest.mark.asyncio
async def test_account_proxy_mode_allows_proxy_fake_ip_dns_answer() -> None:
    async def _resolver(_host: str, _port: int) -> list[str]:
        return ["198.18.5.204"]

    http = PluginHTTP(
        allowed_hosts=["www.qingwapt.com"],
        account_proxy_url="http://10.10.8.33:6152",
        network_mode="account_proxy",
        resolver=_resolver,
    )

    await http._validate_url(httpx.URL("https://www.qingwapt.com/details.php?id=1"))


@pytest.mark.asyncio
async def test_direct_mode_still_blocks_proxy_fake_ip_dns_answer() -> None:
    async def _resolver(_host: str, _port: int) -> list[str]:
        return ["198.18.5.204"]

    http = PluginHTTP(
        allowed_hosts=["www.qingwapt.com"],
        network_mode="direct",
        resolver=_resolver,
    )

    with pytest.raises(PluginHTTPPolicyError):
        await http._validate_url(httpx.URL("https://www.qingwapt.com/details.php?id=1"))


@pytest.mark.asyncio
async def test_streaming_response_size_cap_rejects_before_full_body() -> None:
    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkStream([b"abc", b"def"]))

    http = PluginHTTP(
        allowed_hosts=["api.example.com"],
        max_response_bytes=5,
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )

    with pytest.raises(PluginHTTPResponseTooLarge):
        await http.get("https://api.example.com/v1")


@pytest.mark.asyncio
async def test_streaming_response_allows_body_at_exact_cap() -> None:
    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers={"content-type": "application/json"},
            stream=_ChunkStream([b'{"ok"', b":true}"]),
        )

    http = PluginHTTP(
        allowed_hosts=["api.example.com"],
        max_response_bytes=11,
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )

    response = await http.post("https://api.example.com/v1", json={"x": 1})

    assert response.status_code == 201
    assert response.content == b'{"ok":true}'
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_rejects_follow_redirects_and_unsupported_request_kwargs() -> None:
    called = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called += 1
        return httpx.Response(200, content=b"should not connect")

    http = PluginHTTP(
        allowed_hosts=["api.example.com"],
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )

    with pytest.raises(PluginHTTPPolicyError, match="重定向"):
        await http.get("https://api.example.com/v1", follow_redirects=True)

    with pytest.raises(PluginHTTPPolicyError, match="timeout"):
        await http.get("https://api.example.com/v1", timeout=120)

    with pytest.raises(PluginHTTPPolicyError, match="不支持请求参数"):
        await http.get("https://api.example.com/v1", extensions={"trace": "x"})

    assert called == 0


def test_uses_account_proxy_by_default_and_direct_mode_omits_proxy() -> None:
    proxied = PluginHTTP(
        allowed_hosts=["api.example.com"],
        account_proxy_url="socks5://127.0.0.1:1080",
    )
    assert proxied._client_kwargs()["proxy"] == "socks5://127.0.0.1:1080"

    direct = PluginHTTP(
        allowed_hosts=["api.example.com"],
        account_proxy_url="socks5://127.0.0.1:1080",
        network_mode="direct",
    )
    assert "proxy" not in direct._client_kwargs()


def test_from_context_direct_mode_requires_manifest_opt_in() -> None:
    ctx = SimpleNamespace(
        feature_key="http_demo",
        account_proxy_url="socks5://127.0.0.1:1080",
        config={"http": {"network_mode": "direct"}},
    )

    with pytest.raises(PluginHTTPPolicyError, match="http_demo"):
        PluginHTTP.from_context(ctx, allowed_hosts=["api.example.com"])

    http = PluginHTTP.from_context(
        ctx,
        allowed_hosts=["api.example.com"],
        manifest_http={"allow_direct": True},
    )
    assert http.network_mode == "direct"
    assert http.plugin_key == "http_demo"
    assert "proxy" not in http._client_kwargs()


@pytest.mark.asyncio
async def test_policy_errors_include_plugin_key() -> None:
    http = PluginHTTP(
        plugin_key="demo_http",
        allowed_hosts=["api.example.com"],
        resolver=_public_resolver,
    )

    with pytest.raises(PluginHTTPPolicyError, match="demo_http"):
        await http.get("https://evil.example.com/v1")


@pytest.mark.asyncio
async def test_policy_error_for_blocked_literal_includes_plugin_key() -> None:
    http = PluginHTTP(
        allowed_hosts=["example.com"],
        plugin_key="demo_plugin",
        resolver=_public_resolver,
    )

    with pytest.raises(PluginHTTPPolicyError) as exc:
        await http.get("http://127.0.0.1/")

    assert "demo_plugin" in str(exc.value)


@pytest.mark.asyncio
async def test_unavailable_account_proxy_fails_before_dns_or_transport() -> None:
    resolved = 0
    requested = 0

    async def _resolver(_host: str, _port: int) -> list[str]:
        nonlocal resolved
        resolved += 1
        return ["93.184.216.34"]

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested += 1
        return httpx.Response(200)

    http = PluginHTTP(
        allowed_hosts=["api.example.com"],
        account_proxy_error="账号使用 SOCKS4 Telegram 代理",
        resolver=_resolver,
        transport=httpx.MockTransport(_handler),
    )

    with pytest.raises(PluginHTTPPolicyError, match="拒绝回落直连"):
        await http.get("https://api.example.com/v1")

    assert resolved == 0
    assert requested == 0


@pytest.mark.asyncio
async def test_manifest_opted_direct_mode_can_ignore_account_proxy_http_limitation() -> None:
    requested = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested += 1
        return httpx.Response(200, content=b"ok")

    ctx = SimpleNamespace(
        feature_key="direct_demo",
        account_proxy_url=None,
        account_proxy_error="账号使用 SOCKS4 Telegram 代理",
        config={"http": {"network_mode": "direct"}},
    )
    http = PluginHTTP.from_context(
        ctx,
        allowed_hosts=["api.example.com"],
        manifest_http={"allow_direct": True},
        resolver=_public_resolver,
        transport=httpx.MockTransport(_handler),
    )

    response = await http.get("https://api.example.com/v1")

    assert response.status_code == 200
    assert requested == 1
