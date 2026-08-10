"""代理 URL 解析工具。

把 ``.env`` 里的 ``TG_DEFAULT_PROXY`` 字符串（或任何 URL 风格的代理串）
转成 Telethon 接受的 PySocks 元组：

    (proxy_type, host, port, rdns, username, password)

支持的格式：
    socks5://user:pass@host:port
    socks5://host:port
    socks4://host:port
    http://host:port

调用：
    >>> parse_proxy_url("socks5://user:pass@127.0.0.1:1080")
    ("socks5", "127.0.0.1", 1080, True, "user", "pass")
    >>> parse_proxy_url("")     # 空字符串视为不配置
    None
"""
from __future__ import annotations

from urllib.parse import unquote, urlparse

# Telethon / PySocks 接受的 proxy_type 枚举
_VALID_TYPES: dict[str, str] = {
    "socks5": "socks5",
    "socks4": "socks4",
    "http": "http",
    "https": "http",        # https 走 HTTP CONNECT
}


ProxyTuple = tuple[str, str, int, bool, str | None, str | None]


class ProxyConfigError(ValueError):
    """显式代理配置无效；调用方必须 fail-closed，不能把它解释成直连。"""


def parse_proxy_url(url: str | None) -> ProxyTuple | None:
    """解析代理 URL；仅空值表示未配置，非空无效值统一抛错。"""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None

    # 兼容用户写 "127.0.0.1:1080" 不带 scheme：默认按 socks5 处理
    if "://" not in url:
        url = "socks5://" + url

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ProxyConfigError(f"代理 URL 无法解析：{url!r}") from exc

    scheme = (parsed.scheme or "").lower()

    if scheme == "mtproxy":
        # 返回 None 会被调用方解释为“直连”，从而在用户明确配置代理时泄漏真实出口。
        # 在实现 Telethon 专用 connection class 与 secret 校验前必须 fail-closed。
        raise ProxyConfigError("MTProxy 当前不受支持，请使用 SOCKS5、SOCKS4 或 HTTP 代理")

    proxy_type = _VALID_TYPES.get(scheme)
    if not proxy_type:
        raise ProxyConfigError(
            f"未知代理 scheme {scheme!r}；仅支持 socks5、socks4、http 或 https"
        )

    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigError(f"代理端口无效：{url!r}") from exc
    if not host or not port:
        raise ProxyConfigError(f"代理 URL 缺少 host 或 port：{url!r}")

    user = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None

    # rdns=True：让代理服务器做 DNS 解析，避免本地 DNS 泄漏（连 TG 时很关键）
    return (proxy_type, host, int(port), True, user, password)


def get_default_proxy_tuple() -> ProxyTuple | None:
    """读 settings.tg_default_proxy 并解析；用作所有未指定 proxy_id 的账号的兜底代理。"""
    # 延迟 import 避免循环
    from ..settings import settings
    return parse_proxy_url(settings.tg_default_proxy)
