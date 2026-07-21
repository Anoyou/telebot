"""平台能力热插拔 REST API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..deps import CurrentUser, DBSession
from ..schemas.platform_capabilities import (
    CapabilityModulePatch,
    CapabilityModulePatchOut,
    CapabilityModuleState,
    CapabilityWorkerConvergence,
    PlatformCapabilitiesOut,
)
from ..services import platform_capabilities as caps

router = APIRouter(prefix="/api/system/capabilities", tags=["platform-capabilities"])


@router.get("", response_model=PlatformCapabilitiesOut)
async def get_platform_capabilities(
    db: DBSession,
    _user: CurrentUser,
) -> PlatformCapabilitiesOut:
    """返回模块目标状态、运行时状态、固定通道与 worker 收敛摘要。"""

    # 确保缓存已初始化（测试或热路径漏 bootstrap 时兜底）
    if not caps.get_snapshot().cache_ready:
        try:
            await caps.bootstrap_from_db(db)
        except Exception:  # noqa: BLE001
            await caps.refresh_cache_from_db(db)
    payload = caps.build_status_payload()
    return PlatformCapabilitiesOut.model_validate(payload)


@router.patch("/{module_key}", response_model=CapabilityModulePatchOut)
async def patch_platform_capability(
    module_key: str,
    body: CapabilityModulePatch,
    db: DBSession,
    user: CurrentUser,
) -> CapabilityModulePatchOut:
    if module_key not in caps.MODULE_DEFS:
        raise HTTPException(
            status_code=404,
            detail={"code": "UNKNOWN_MODULE", "message": f"未知平台模块：{module_key}"},
        )
    result = await caps.set_module_enabled(
        db,
        module_key,  # type: ignore[arg-type]
        bool(body.enabled),
        user_id=user.id,
        notify_workers=True,
        apply_local=True,
    )
    # 重新聚合模块展示字段
    status = caps.build_status_payload()
    module_map = {m["key"]: m for m in status["modules"]}
    module = module_map.get(module_key) or {
        "key": module_key,
        "label": caps.MODULE_DEFS[module_key]["label"],  # type: ignore[index]
        "desired_enabled": result["desired_enabled"],
        "generation": result["generation"],
        "runtime_state": result.get("runtime_state") or "ready",
        "last_error": result.get("last_error"),
        "last_transition_at": None,
        "resource_summary": {},
    }
    worker = result.get("worker_convergence") or status.get("worker_convergence") or {}
    offline = int(worker.get("offline_or_timeout") or 0)
    message = None
    ok = True
    if offline > 0:
        ok = True  # 目标状态已保存；部分 worker 将由周期 reconcile 收敛
        message = (
            f"目标状态已保存（generation={result['generation']}），"
            f"{offline} 个 worker 未即时确认，将由周期 reconcile 或重启收敛。"
        )
    elif result.get("runtime_state") == "failed":
        ok = False
        message = result.get("last_error") or "本地运行时切换失败"
    return CapabilityModulePatchOut(
        module=CapabilityModuleState.model_validate(module),
        worker_convergence=CapabilityWorkerConvergence.model_validate(worker),
        ok=ok,
        message=message,
    )


__all__ = ["router"]
