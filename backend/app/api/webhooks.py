"""入站 Webhook API 空桩（WP7）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import CurrentUser

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.get("")
async def webhooks_placeholder(_user: CurrentUser) -> dict[str, str]:
    return {"status": "not_implemented"}
