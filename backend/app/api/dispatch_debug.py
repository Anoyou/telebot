"""命中调试 API 空桩（WP4）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import CurrentUser

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


@router.get("/debug")
async def dispatch_debug_placeholder(_user: CurrentUser) -> dict[str, str]:
    return {"status": "not_implemented"}
