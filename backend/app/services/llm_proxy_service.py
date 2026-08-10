"""LLM Provider 出口代理解析（Web API / System Agent 共用）。"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt_str
from ..db.models.account import Proxy
from ..util.proxy import ProxyConfigError, parse_proxy_url


def _decrypt_proxy_password(proxy: Proxy) -> str:
    try:
        return decrypt_str(proxy.password_enc) if proxy.password_enc else ""
    except Exception as exc:  # noqa: BLE001 - 坏凭据不能被降级为空密码继续发网
        raise ProxyConfigError("LLM Provider 代理凭据无法解密，请重新保存或更换代理") from exc


async def resolve_proxy_url(db: AsyncSession, proxy_id: int | None) -> str | None:
    if proxy_id is None:
        return None
    # Gateway 候选快照可能复用先前读过 Proxy 的 Session；强制覆盖 identity map，
    # 保证拿到当前事务可见版本，避免把并发轮换前的代理凭据重新同步回 Gateway。
    proxy = await db.get(Proxy, int(proxy_id), populate_existing=True)
    if proxy is None:
        raise ProxyConfigError(f"代理 #{proxy_id} 不存在，拒绝回落直连")
    proxy_type = str(proxy.type or "").lower()
    if proxy_type not in {"socks5", "http", "https"}:
        raise ProxyConfigError(f"代理类型 {proxy.type!r} 不能用于 LLM，拒绝回落直连")
    if "://" in proxy.host:
        parsed = parse_proxy_url(proxy.host)
        if parsed is not None:
            ptype, host, port, _rdns, parsed_user, parsed_password = parsed
            if ptype not in {"socks5", "http"}:
                raise ProxyConfigError(f"代理类型 {ptype!r} 不能用于 LLM，拒绝回落直连")
            username = proxy.username or parsed_user
            password = _decrypt_proxy_password(proxy) or (parsed_password or "")
            return _build_url(ptype, host, int(port), username, password)
    password = _decrypt_proxy_password(proxy)
    return _build_url(
        "socks5" if proxy_type == "socks5" else "http",
        proxy.host,
        int(proxy.port),
        proxy.username,
        password,
    )


def _build_url(
    scheme: str,
    host: str,
    port: int,
    username: str | None,
    password: str,
) -> str:
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth = f"{auth}:{quote(password, safe='')}"
        auth = f"{auth}@"
    return f"{scheme}://{auth}{host}:{int(port)}"


__all__ = ["resolve_proxy_url"]
