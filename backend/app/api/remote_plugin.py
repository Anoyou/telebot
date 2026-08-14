"""远程插件管理 API 路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import CurrentUser, DBSession
from ..schemas.remote_plugin import RemotePluginCreate, RemotePluginOut
from ..services import feature_service
from ..services import remote_plugin_service as svc
from ..services.platform_capabilities import PluginCapabilityBlocked
from ..services.remote_plugin_service import (
    DuplicatePluginName,
    GitOperationFailed,
    InvalidPluginMetadata,
    InvalidSourceUrl,
    RemotePluginError,
    RemotePluginNotFound,
)

router = APIRouter(prefix="/api/remote-plugins", tags=["remote-plugins"])


@router.get("", response_model=list[RemotePluginOut])
async def list_remote_plugins(db: DBSession, _user: CurrentUser):
    """列出所有已安装远程插件。"""
    rows = await svc.list_installed(db)
    return rows


@router.post("/install", response_model=RemotePluginOut, status_code=201)
async def api_install_plugin(
    body: RemotePluginCreate, db: DBSession, user: CurrentUser
):
    """从 Git URL 克隆并安装远程插件。

    ``default_enabled=True`` 时，安装后自动为所有已有账号启用该插件
    （写入 AccountFeature 行），功能矩阵 Tab 会自动展示。
    """
    try:
        row = await svc.install(
            db,
            body.source_url,
            default_enabled=body.default_enabled,
            triggered_by_user_id=user.id,
        )
        await db.commit()
        await svc.trigger_reload(db, row.name)
        return row
    except DuplicatePluginName as e:
        raise HTTPException(409, detail={"code": e.code, "message": e.message}) from e
    except InvalidSourceUrl as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except PluginCapabilityBlocked as e:
        raise HTTPException(409, detail={"code": e.error_code, "message": str(e)}) from e
    except RemotePluginError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except Exception as e:
        raise HTTPException(500, detail={"code": "INTERNAL", "message": f"安装失败: {e}"}) from e


@router.post("/{name}/enable")
async def api_enable(name: str, db: DBSession, user: CurrentUser):
    """启用指定远程插件，并为所有现有账号打开账号级开关。"""
    try:
        row = await svc.enable(
            db,
            name,
            bootstrap_accounts=False,
            triggered_by_user_id=user.id,
        )
        applied = await svc.enable_for_all_accounts(db, name)
        plugin_name = row.name
        await db.commit()
        await svc.trigger_reload(db, plugin_name)
        return {"ok": True, "name": plugin_name, "enabled": True, "applied": applied}
    except RemotePluginNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except PluginCapabilityBlocked as e:
        raise HTTPException(409, detail={"code": e.error_code, "message": str(e)}) from e


@router.post("/{name}/disable")
async def api_disable(name: str, db: DBSession, _user: CurrentUser):
    """禁用指定远程插件（全局开关）。"""
    try:
        row = await svc.disable(db, name)
        plugin_name = row.name
        await db.commit()
        await svc.trigger_reload(db, plugin_name)
        return {"ok": True, "name": plugin_name, "enabled": False}
    except RemotePluginNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e


class AccountPluginAction(BaseModel):
    account_ids: list[int]


class RemotePluginUpdateCheckResponse(BaseModel):
    total: int
    checked: int
    update_available: int
    failed: int


@router.post("/{name}/enable-accounts")
async def api_enable_accounts(name: str, body: AccountPluginAction, db: DBSession, user: CurrentUser):
    """按账号启用远程插件（写入 AccountFeature 行，功能矩阵可见）。"""
    # 先确认远程插件存在
    rp = await svc.get_by_name(db, name)
    if rp is None:
        raise HTTPException(404, detail={"code": "PLUGIN_NOT_FOUND", "message": f"插件 {name} 不存在"})
    try:
        n = await feature_service.bulk_set_enabled(
            db,
            body.account_ids,
            name,
            enabled=True,
            triggered_by_user_id=user.id,
        )
    except PluginCapabilityBlocked as e:
        raise HTTPException(409, detail={"code": e.error_code, "message": str(e)}) from e
    await db.commit()
    return {"ok": True, "name": name, "applied": n}


@router.post("/{name}/disable-accounts")
async def api_disable_accounts(name: str, body: AccountPluginAction, db: DBSession, user: CurrentUser):
    """按账号禁用远程插件。"""
    rp = await svc.get_by_name(db, name)
    if rp is None:
        raise HTTPException(404, detail={"code": "PLUGIN_NOT_FOUND", "message": f"插件 {name} 不存在"})
    n = await feature_service.bulk_set_enabled(
        db,
        body.account_ids,
        name,
        enabled=False,
        triggered_by_user_id=user.id,
    )
    await db.commit()
    return {"ok": True, "name": name, "applied": n}


@router.post("/{name}/update", response_model=RemotePluginOut)
async def api_update(name: str, db: DBSession, user: CurrentUser):
    """从远程仓库 git pull 并更新插件元数据。"""
    try:
        row = await svc.update(db, name, triggered_by_user_id=user.id)
        await db.commit()
        await svc.trigger_reload(db, row.name)
        return row
    except RemotePluginNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except PluginCapabilityBlocked as e:
        raise HTTPException(409, detail={"code": e.error_code, "message": str(e)}) from e
    except RemotePluginError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.post("/check-updates", response_model=RemotePluginUpdateCheckResponse)
async def api_check_updates(db: DBSession, _user: CurrentUser):
    """手动检查所有远程模块是否有更新；只更新提示状态，不自动安装。"""
    summary = await svc.check_updates(db)
    await db.commit()
    return RemotePluginUpdateCheckResponse(
        total=summary.total,
        checked=summary.checked,
        update_available=summary.update_available,
        failed=summary.failed,
    )


@router.delete("/{name}")
async def api_uninstall(name: str, db: DBSession, _user: CurrentUser):
    """卸载并删除指定远程插件（同时清理 Feature/AccountFeature 行）。"""
    found = await svc.uninstall(db, name)
    await db.commit()
    if found:
        await svc.trigger_reload(db, name)
    if not found:
        raise HTTPException(
            404, detail={"code": "PLUGIN_NOT_FOUND", "message": f"插件 {name} 不存在"}
        )
    return {"ok": True, "name": name}
