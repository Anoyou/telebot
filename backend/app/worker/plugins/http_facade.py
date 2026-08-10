"""Safe HTTP facade exposed to plugins as ``ctx.http``.

The loader attaches ``PluginHTTP.from_context(ctx, ...)`` only when a plugin
declares ``external_http`` and a non-empty ``allowed_hosts`` list.

Security model:
- only ``http``/``https`` URLs are accepted;
- every request host must match the plugin's allow-list;
- local/private/non-global IPs are blocked for both literals and DNS answers;
- response bodies are capped while streaming, not after full buffering.

Known MVP gap:
``httpx`` does not expose the connected peer address in a portable way here, so
we validate DNS results before connecting but do not yet re-check the final
socket peer after the connection is established. A hostile DNS server could
rebind between preflight resolution and connect. Keep this facade behind
explicit host allow-lists until a custom transport or resolver-pinning layer is
added.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

import httpx

NetworkMode = Literal["account_proxy", "direct"]
Resolver = Callable[[str, int], Awaitable[Sequence[str]] | Sequence[str]]

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_REQUEST_BLOCK_REASON: ContextVar[str | None] = ContextVar(
    "plugin_http_request_block_reason",
    default=None,
)
_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
_REQUEST_KWARG_ALLOWLIST = frozenset(
    {
        "auth",
        "content",
        "cookies",
        "data",
        "files",
        "headers",
        "json",
        "params",
    }
)


class PluginHTTPError(RuntimeError):
    """Base class for safe plugin HTTP failures."""


class PluginHTTPPolicyError(PluginHTTPError):
    """Raised when a request violates plugin HTTP policy."""


class PluginHTTPResponseTooLarge(PluginHTTPError):
    """Raised when a response exceeds the configured streaming byte cap."""


@dataclass(frozen=True)
class PluginHTTPResponse:
    """Small response object returned by ``PluginHTTP.get/post``."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode(_charset_from_headers(self.headers), errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


def _charset_from_headers(headers: Mapping[str, str]) -> str:
    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip('"')
    return "utf-8"


def normalize_hostname(host: str, *, plugin_key: str = "?") -> str:
    """Normalize a URL hostname for policy checks."""

    clean = str(host or "").strip().rstrip(".").lower()
    if not clean:
        raise PluginHTTPPolicyError(_policy_message(plugin_key, "HTTP 请求缺少 host"))
    try:
        return clean.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PluginHTTPPolicyError(_policy_message(plugin_key, f"HTTP host 非法: {host}")) from exc


def host_matches_allowed(host: str, allowed_hosts: Sequence[str], *, plugin_key: str = "?") -> bool:
    """Return whether ``host`` matches exact, ``*.domain`` or ``**.domain`` rules.

    Semantics:
    - ``example.com`` matches exactly ``example.com``;
    - ``*.example.com`` matches one subdomain label, e.g. ``api.example.com``;
    - ``**.example.com`` matches ``example.com`` and any nested subdomain.
    """

    candidate = normalize_hostname(host, plugin_key=plugin_key)
    for raw_pattern in allowed_hosts:
        pattern = normalize_hostname(str(raw_pattern), plugin_key=plugin_key)
        if pattern.startswith("**."):
            suffix = pattern[3:]
            if candidate == suffix or candidate.endswith(f".{suffix}"):
                return True
            continue
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if candidate.endswith(f".{suffix}"):
                prefix = candidate[: -(len(suffix) + 1)]
                if prefix and "." not in prefix:
                    return True
            continue
        if candidate == pattern:
            return True
    return False


def _normalize_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Map IPv4-mapped IPv6 addresses before classification."""

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_ip(ip)
    return (
        normalized.is_loopback
        or normalized.is_link_local
        or normalized.is_private
        or normalized.is_unspecified
        or normalized.is_multicast
        or normalized.is_reserved
        or not normalized.is_global
    )


def _assert_public_ip(value: str, *, plugin_key: str = "?") -> None:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise PluginHTTPPolicyError(_policy_message(plugin_key, f"DNS 返回了非法 IP: {value}")) from exc
    if _is_blocked_ip(ip):
        raise PluginHTTPPolicyError(_policy_message(plugin_key, f"HTTP 目标解析到受保护地址: {value}"))


def _is_proxy_fake_ip(value: str) -> bool:
    """Return True for local proxy fake-ip DNS answers.

    Clash/Surge-style TUN fake-ip commonly uses 198.18.0.0/15. Direct egress
    must still reject that reserved range. When a plugin request is routed
    through the account proxy, the proxy resolves the original hostname, so the
    local fake-ip answer should not block a host that already passed
    ``allowed_hosts``.
    """

    try:
        ip = _normalize_ip(ipaddress.ip_address(value))
    except ValueError:
        return False
    if not isinstance(ip, ipaddress.IPv4Address):
        return False
    return any(ip in network for network in _FAKE_IP_NETWORKS)


async def _default_resolver(host: str, port: int) -> Sequence[str]:
    def _resolve() -> list[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return sorted({str(item[4][0]) for item in infos})

    return await asyncio.to_thread(_resolve)


async def _resolve_with(resolver: Resolver, host: str, port: int) -> Sequence[str]:
    result = resolver(host, port)
    if inspect.isawaitable(result):
        return await result
    return result


def _network_mode_from_hooks(
    *,
    config: Mapping[str, Any] | None,
    manifest_http: Mapping[str, Any] | None,
    plugin_key: str = "?",
) -> NetworkMode:
    """Resolve the manifest/config hook for direct HTTP.

    Default is ``account_proxy``. A manifest may opt into direct egress with
    ``{"allow_direct": true}``, while account config can request it with
    ``{"http": {"network_mode": "direct"}}`` or ``{"http_network_mode":
    "direct"}``. Without the manifest opt-in, direct mode is rejected.
    """

    config = config or {}
    manifest_http = manifest_http or {}
    nested_config = config.get("http") if isinstance(config.get("http"), Mapping) else {}
    requested = (
        nested_config.get("network_mode")
        or nested_config.get("network")
        or config.get("http_network_mode")
        or manifest_http.get("network_mode")
        or manifest_http.get("network")
        or "account_proxy"
    )
    if requested not in ("account_proxy", "direct"):
        raise PluginHTTPPolicyError(_policy_message(plugin_key, f"不支持的 HTTP 出口模式: {requested}"))
    if requested == "direct" and not bool(manifest_http.get("allow_direct", False)):
        raise PluginHTTPPolicyError(_policy_message(plugin_key, "插件 manifest 未声明 allow_direct，不能绕过账号代理"))
    return requested


class PluginHTTP:
    """Safe async HTTP facade exposed as ``ctx.http``."""

    def __init__(
        self,
        *,
        allowed_hosts: Sequence[str],
        plugin_key: str = "?",
        account_proxy_url: str | None = None,
        account_proxy_error: str | None = None,
        network_mode: NetworkMode = "account_proxy",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not allowed_hosts:
            raise PluginHTTPPolicyError(_policy_message(plugin_key, "插件 HTTP 必须声明 allowed_hosts"))
        if network_mode not in ("account_proxy", "direct"):
            raise PluginHTTPPolicyError(_policy_message(plugin_key, f"不支持的 HTTP 出口模式: {network_mode}"))
        if max_response_bytes <= 0:
            raise PluginHTTPPolicyError(_policy_message(plugin_key, "max_response_bytes 必须大于 0"))
        if timeout_seconds <= 0:
            raise PluginHTTPPolicyError(_policy_message(plugin_key, "timeout_seconds 必须大于 0"))

        self._plugin_key = str(plugin_key or "?")
        self.allowed_hosts = tuple(allowed_hosts)
        self.account_proxy_url = account_proxy_url
        self.account_proxy_error = str(account_proxy_error or "").strip() or None
        self.network_mode = network_mode
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._resolver = resolver or _default_resolver
        self._transport = transport

    @classmethod
    def from_context(
        cls,
        ctx: Any,
        *,
        allowed_hosts: Sequence[str],
        manifest_http: Mapping[str, Any] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> PluginHTTP:
        """Build the facade from a ``PluginContext``-like object."""

        plugin_key = str(getattr(ctx, "feature_key", "") or "?")
        try:
            network_mode = _network_mode_from_hooks(
                config=getattr(ctx, "config", None),
                manifest_http=manifest_http,
                plugin_key=plugin_key,
            )
        except PluginHTTPPolicyError as exc:
            raise PluginHTTPPolicyError(_policy_message(plugin_key, str(exc))) from exc
        return cls(
            allowed_hosts=allowed_hosts,
            plugin_key=plugin_key,
            account_proxy_url=getattr(ctx, "account_proxy_url", None),
            account_proxy_error=getattr(ctx, "account_proxy_error", None),
            network_mode=network_mode,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            resolver=resolver,
            transport=transport,
        )

    async def get(self, url: str, **kwargs: Any) -> PluginHTTPResponse:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> PluginHTTPResponse:
        return await self._request("POST", url, **kwargs)

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout_seconds),
            "follow_redirects": False,
            "trust_env": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        if self.network_mode == "account_proxy" and self.account_proxy_url:
            kwargs["proxy"] = self.account_proxy_url
        return kwargs

    @property
    def plugin_key(self) -> str:
        return self._plugin_key

    async def _request(self, method: str, url: str, **kwargs: Any) -> PluginHTTPResponse:
        blocked_reason = _REQUEST_BLOCK_REASON.get()
        if blocked_reason:
            raise self._policy_error(blocked_reason)
        if self.network_mode == "account_proxy" and self.account_proxy_error:
            raise self._policy_error(
                f"账号代理不能用于插件 HTTP，拒绝回落直连：{self.account_proxy_error}"
            )
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise self._policy_error(f"HTTP URL 非法: {url}") from exc
        try:
            await self._validate_url(parsed)
            request_kwargs = self._sanitize_request_kwargs(kwargs)
        except PluginHTTPPolicyError as exc:
            raise self._policy_error(str(exc)) from exc

        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            async with client.stream(method, parsed, **request_kwargs) as response:
                content = await self._read_capped(response)
                return PluginHTTPResponse(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    content=content,
                    url=str(response.url),
                )

    def _sanitize_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key == "follow_redirects":
                if value:
                    raise self._policy_error("ctx.http 不允许自动跟随重定向")
                continue
            if key == "timeout":
                raise self._policy_error("ctx.http timeout 由平台统一控制")
            if key not in _REQUEST_KWARG_ALLOWLIST:
                raise self._policy_error(f"ctx.http 不支持请求参数: {key}")
            sanitized[key] = value
        return sanitized

    def _policy_error(self, message: str) -> PluginHTTPPolicyError:
        return PluginHTTPPolicyError(_policy_message(self._plugin_key, message))

    async def _validate_url(self, url: httpx.URL) -> None:
        if url.scheme not in ("http", "https"):
            raise self._policy_error("插件 HTTP 仅允许 http/https URL")
        if not url.host:
            raise self._policy_error("HTTP 请求缺少 host")

        host = normalize_hostname(url.host, plugin_key=self._plugin_key)
        if host == "localhost" or host.endswith(".localhost"):
            raise self._policy_error("插件 HTTP 禁止访问 localhost")
        if not host_matches_allowed(host, self.allowed_hosts, plugin_key=self._plugin_key):
            raise self._policy_error(f"HTTP host 不在 allowed_hosts 内: {host}")

        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            answers = await _resolve_with(self._resolver, host, port)
            if not answers:
                raise self._policy_error(f"HTTP host 无 DNS 解析结果: {host}") from None
            for answer in answers:
                if (
                    self.network_mode == "account_proxy"
                    and self.account_proxy_url
                    and _is_proxy_fake_ip(str(answer))
                ):
                    continue
                _assert_public_ip(str(answer), plugin_key=self._plugin_key)
        else:
            if _is_blocked_ip(literal):
                raise self._policy_error(f"HTTP 目标是受保护地址: {host}")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_response_bytes:
                raise PluginHTTPResponseTooLarge(
                    _policy_message(
                        self._plugin_key,
                        f"HTTP 响应超过上限: {total}>{self.max_response_bytes} bytes",
                    )
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _policy_message(plugin_key: str, message: str) -> str:
    plugin = str(plugin_key or "?")
    prefix = f"插件 {plugin!r}: "
    return message if message.startswith(prefix) else f"{prefix}{message}"


SafeHTTP = PluginHTTP


@contextmanager
def block_plugin_http_requests(reason: str):
    """只在当前异步任务上下文中禁止插件 HTTP，并由子任务继承。"""

    token: Token[str | None] = _REQUEST_BLOCK_REASON.set(str(reason))
    try:
        yield
    finally:
        _REQUEST_BLOCK_REASON.reset(token)


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "block_plugin_http_requests",
    "PluginHTTP",
    "PluginHTTPError",
    "PluginHTTPPolicyError",
    "PluginHTTPResponse",
    "PluginHTTPResponseTooLarge",
    "SafeHTTP",
    "host_matches_allowed",
]
