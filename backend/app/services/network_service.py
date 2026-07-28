"""后端出口网络环境探测与五分钟缓存。"""

from __future__ import annotations

import asyncio
import time

import httpx
from pydantic import BaseModel


class NetworkInfo(BaseModel):
    ip: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    org: str | None = None
    cached_at: float = 0.0
    fresh: bool = True
    error: str | None = None


_TTL_SECONDS = 5 * 60
_CACHE: dict[str, NetworkInfo] = {}
_LOCK = asyncio.Lock()


async def fetch_network_info() -> NetworkInfo:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get("http://ip-api.com/json/")
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                return NetworkInfo(
                    ip=data.get("query"),
                    country=data.get("countryCode"),
                    region=data.get("regionName") or data.get("region"),
                    city=data.get("city"),
                    org=data.get("isp") or data.get("org"),
                    cached_at=time.time(),
                )
    except Exception:  # noqa: BLE001
        pass
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get("https://ipinfo.io/json")
            response.raise_for_status()
            data = response.json()
            return NetworkInfo(
                ip=data.get("ip"),
                country=data.get("country"),
                region=data.get("region"),
                city=data.get("city"),
                org=data.get("org"),
                cached_at=time.time(),
            )
    except Exception as exc:  # noqa: BLE001
        return NetworkInfo(
            cached_at=time.time(),
            error=f"{type(exc).__name__}: {exc}",
        )


async def get_network_info(*, force: bool = False) -> NetworkInfo:
    async with _LOCK:
        cached = _CACHE.get("v1")
        now = time.time()
        if not force and cached and (now - cached.cached_at) < _TTL_SECONDS:
            return cached.model_copy(update={"fresh": False})
        info = await fetch_network_info()
        _CACHE["v1"] = info
        return info


__all__ = ["NetworkInfo", "fetch_network_info", "get_network_info"]
