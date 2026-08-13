"""值守运行预设 REST API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..deps import CurrentUser, DBSession
from ..schemas.runtime_profiles import (
    RuntimeProfileApplyIn,
    RuntimeProfileDryRunIn,
    RuntimeProfileDryRunOut,
    RuntimeProfileStatusOut,
)
from ..services import runtime_profile_service as profiles

router = APIRouter(prefix="/api/platform/profile", tags=["runtime-profile"])


def _transition_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "error_code", "PROFILE_TRANSITION_FAILED")
    return HTTPException(
        status_code=409,
        detail={"code": code, "message": str(exc) or "运行预设切换失败"},
    )


@router.get("", response_model=RuntimeProfileStatusOut)
async def get_runtime_profile(
    db: DBSession,
    _user: CurrentUser,
) -> RuntimeProfileStatusOut:
    return RuntimeProfileStatusOut.model_validate(await profiles.get_status(db))


@router.post("/dry-run", response_model=RuntimeProfileDryRunOut)
async def dry_run_runtime_profile(
    body: RuntimeProfileDryRunIn,
    db: DBSession,
    _user: CurrentUser,
) -> RuntimeProfileDryRunOut:
    return RuntimeProfileDryRunOut.model_validate(await profiles.dry_run(db, body.preset))


@router.post("/apply", response_model=RuntimeProfileStatusOut)
async def apply_runtime_profile(
    body: RuntimeProfileApplyIn,
    db: DBSession,
    user: CurrentUser,
) -> RuntimeProfileStatusOut:
    try:
        payload = await profiles.apply(db, body.preset, operator_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise _transition_error(exc) from exc
    return RuntimeProfileStatusOut.model_validate(payload)


@router.post("/restore", response_model=RuntimeProfileStatusOut)
async def restore_runtime_profile(
    db: DBSession,
    user: CurrentUser,
) -> RuntimeProfileStatusOut:
    try:
        payload = await profiles.restore(db, operator_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise _transition_error(exc) from exc
    return RuntimeProfileStatusOut.model_validate(payload)


__all__ = ["router"]
