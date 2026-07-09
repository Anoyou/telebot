"""资金台账 API 空桩（WP5）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import CurrentUser

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


@router.get("")
async def ledger_placeholder(_user: CurrentUser) -> dict[str, str]:
    return {"status": "not_implemented"}
