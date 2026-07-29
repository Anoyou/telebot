"""LLM Provider 出口代理解析（Web API / System Agent 共用）。"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt_str
from ..db.models.account import Proxy
from ..util.proxy import parse_proxy_url


async def resolve_proxy_url(db: AsyncSession, proxy_id: int | None) -> str | None:
    if proxy_id is None:
        return None
    proxy = await db.get(Proxy, int(proxy_id))
    if proxy is None:
        return None
    if "://" in proxy.host:
        parsed = parse_proxy_url(proxy.host)
        if parsed is not None:
            ptype, host, port, _rdns, parsed_user, parsed_password = parsed
            if ptype not in {"socks5", "http"}:
                return None
            username = proxy.username or parsed_user
            password = (
                decrypt_str(proxy.password_enc)
                if proxy.password_enc
                else (parsed_password or "")
            )
            return _build_url(ptype, host, int(port), username, password)
    proxy_type = str(proxy.type or "").lower()
    if proxy_type not in {"socks5", "http", "https"}:
        return None
    password = decrypt_str(proxy.password_enc) if proxy.password_enc else ""
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
