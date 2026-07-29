"""网络环境探测 API。

提供：
  - ``GET /api/system/network``  返回当前后端进程出口 IP + 国家/地区
  - ``GET /api/system/network/refresh``  强制刷新（绕过缓存）

结果缓存 5 分钟（避免每次请求都打 ipinfo.io）。前端 TopBar 用此显示当前环境。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import CurrentUser
from ..services.network_service import NetworkInfo, get_network_info

router = APIRouter(prefix="/api/system", tags=["system"])


async def _get_or_fetch(force: bool = False) -> NetworkInfo:
    return await get_network_info(force=force)


@router.get("/network", response_model=NetworkInfo)
async def get_network(_user: CurrentUser) -> NetworkInfo:
    return await _get_or_fetch(force=False)


@router.post("/network/refresh", response_model=NetworkInfo)
async def refresh_network(_user: CurrentUser) -> NetworkInfo:
    return await _get_or_fetch(force=True)
