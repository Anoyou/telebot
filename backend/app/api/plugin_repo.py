"""插件仓库 API 路由：保存仓库 + 浏览仓库内插件 + 选择性安装。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import CurrentUser, DBSession
from ..schemas.plugin_repo import (
    PluginRepoBulkUpdateResult,
    PluginRepoCreate,
    PluginRepoCredentialUpdate,
    PluginRepoOut,
    PluginRepoPlugin,
)
from ..schemas.remote_plugin import RemotePluginOut
from ..services import plugin_repo_service as svc
from ..services.plugin_repo_service import (
    DuplicatePluginRepo,
    PluginNotInRepo,
    PluginRepoError,
    PluginRepoNotFound,
)
from ..services.remote_plugin_service import (
    DuplicatePluginName,
    GitOperationFailed,
    InvalidPluginMetadata,
    InvalidSourceUrl,
    RemotePluginError,
    trigger_reload,
)

router = APIRouter(prefix="/api/plugin-repos", tags=["plugin-repos"])


@router.get("", response_model=list[PluginRepoOut])
async def list_plugin_repos(db: DBSession, _user: CurrentUser):
    """列出所有保存的插件仓库。"""
    return await svc.list_repos(db)


@router.post("", response_model=PluginRepoOut, status_code=201)
async def create_plugin_repo(body: PluginRepoCreate, db: DBSession, _user: CurrentUser):
    """保存一个新仓库（仅写库；浏览插件请单独调 ``/{id}/plugins``）。"""
    try:
        credential = body.credential
        row = await svc.create_repo(
            db,
            body.url,
            name=body.name,
            description=body.description,
            auth_type=credential.auth_type if credential else None,
            credential=credential.token if credential else None,
        )
        await db.commit()
        await db.refresh(row)
        return row
    except DuplicatePluginRepo as e:
        raise HTTPException(409, detail={"code": e.code, "message": e.message}) from e
    except InvalidSourceUrl as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except PluginRepoError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except RemotePluginError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.put("/{repo_id}/credential", response_model=PluginRepoOut)
async def update_plugin_repo_credential(
    repo_id: int,
    body: PluginRepoCredentialUpdate,
    db: DBSession,
    _user: CurrentUser,
):
    """更新或清除插件仓库凭证。token 不会在响应中回显。"""
    try:
        row = await svc.update_repo_credential(
            db,
            repo_id,
            auth_type=body.auth_type,
            token=body.token,
        )
        await db.commit()
        await db.refresh(row)
        return row
    except PluginRepoNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except PluginRepoError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.delete("/{repo_id}")
async def delete_plugin_repo(repo_id: int, db: DBSession, _user: CurrentUser):
    """删除仓库（**不**联动卸载已安装的插件，只是从“目录索引”里摘掉）。"""
    try:
        row = await svc.get_repo(db, repo_id)
    except PluginRepoNotFound as e:
        raise HTTPException(
            404,
            detail={"code": e.code, "message": e.message},
        ) from e
    repo_url = row.url
    found = await svc.delete_repo(db, repo_id, remove_cache=False)
    if not found:
        raise HTTPException(
            404,
            detail={"code": "REPO_NOT_FOUND", "message": f"仓库不存在: id={repo_id}"},
        )
    await db.commit()
    try:
        await svc.cleanup_repo_cache(repo_url)
    except Exception:  # noqa: BLE001
        # 缓存可在后续刷新时重建/清理，不把已提交的删除误报成失败。
        import logging

        logging.getLogger(__name__).warning(
            "清理已删除插件仓库缓存失败 repo_id=%s",
            repo_id,
            exc_info=True,
        )
    return {"ok": True, "id": repo_id}


class InstallFromRepoBody(BaseModel):
    """``POST /{id}/plugins/{name}/install`` 的可选 body。"""

    default_enabled: bool = False


@router.get("/local/plugins", response_model=list[PluginRepoPlugin])
async def list_local_plugins(_user: CurrentUser):
    """列出 ``plugins/local_imports`` 下可导入的本地插件。"""
    return svc.list_local_import_candidates()


@router.post(
    "/local/plugins/{plugin_name}/install",
    response_model=RemotePluginOut,
    status_code=201,
)
async def install_local_plugin(
    plugin_name: str,
    db: DBSession,
    _user: CurrentUser,
    body: InstallFromRepoBody | None = None,
):
    """从 ``plugins/local_imports`` 导入本地插件。"""
    default_enabled = bool(body.default_enabled) if body else False
    try:
        row = await svc.install_local_plugin(
            db, plugin_name, default_enabled=default_enabled,
        )
        await db.commit()
        await trigger_reload(db, row.name)
        return row
    except PluginNotInRepo as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except DuplicatePluginName as e:
        raise HTTPException(409, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.get("/official/plugins", response_model=list[PluginRepoPlugin])
async def list_official_plugins(db: DBSession, _user: CurrentUser):
    """列出推荐插件库中的插件。"""
    try:
        return await svc.list_official_plugins(db)
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.post(
    "/official/plugins/{plugin_name}/install",
    response_model=RemotePluginOut,
    status_code=201,
)
async def install_official_plugin(
    plugin_name: str,
    db: DBSession,
    _user: CurrentUser,
    body: InstallFromRepoBody | None = None,
):
    """从推荐插件库快捷安装插件。"""
    default_enabled = bool(body.default_enabled) if body else False
    try:
        row = await svc.install_official_plugin(
            db, plugin_name, default_enabled=default_enabled,
        )
        await db.commit()
        await trigger_reload(db, row.name)
        return row
    except PluginNotInRepo as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except DuplicatePluginName as e:
        raise HTTPException(409, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.get("/{repo_id}/plugins", response_model=list[PluginRepoPlugin])
async def list_repo_plugins(repo_id: int, db: DBSession, _user: CurrentUser):
    """列出指定仓库内所有可装插件。

    首次访问会触发 ``git clone``；后续访问只做 ``git fetch + reset``，比较快。
    任一插件目录的 plugin.json 损坏会被静默跳过（已记 warning）。
    """
    try:
        return await svc.list_plugins_in_repo(db, repo_id)
    except PluginRepoNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.post("/{repo_id}/refresh", response_model=list[PluginRepoPlugin])
async def refresh_repo_plugins(repo_id: int, db: DBSession, _user: CurrentUser):
    """强制刷新仓库缓存并返回最新插件列表。"""
    try:
        return await svc.list_plugins_in_repo(db, repo_id, force_refresh=True)
    except PluginRepoNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.post("/{repo_id}/update-installed", response_model=PluginRepoBulkUpdateResult)
async def update_installed_plugins_from_repo(
    repo_id: int,
    db: DBSession,
    _user: CurrentUser,
):
    """更新该仓库里所有已安装且版本低于仓库版本的插件。"""
    try:
        result = await svc.update_installed_plugins_from_repo(db, repo_id)
        await db.commit()
        reload_errors: list[RemotePluginError] = []
        for item in result.items:
            if item.status == "updated":
                try:
                    await trigger_reload(db, item.name)
                except RemotePluginError as exc:
                    reload_errors.append(exc)
        if reload_errors:
            affected = ", ".join(
                item.name for item in result.items if item.status == "updated"
            )
            raise RemotePluginError(
                "PLUGIN_BULK_RELOAD_UNCONFIRMED",
                f"插件 {affected} 已写入磁盘，但部分 worker 未确认重载；相应旧实例已被更新屏障阻止调用",
            )
        return result
    except PluginRepoNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e


@router.post(
    "/{repo_id}/plugins/{plugin_name}/install",
    response_model=RemotePluginOut,
    status_code=201,
)
async def install_plugin_from_repo(
    repo_id: int,
    plugin_name: str,
    db: DBSession,
    _user: CurrentUser,
    body: InstallFromRepoBody | None = None,
):
    """安装仓库中指定名字的插件。

    安装完成后插件全局默认禁用，要让某账号生效需在“账号插件管理”里勾选；
    若想一次性给所有账号启用，传 ``default_enabled=true``。
    """
    default_enabled = bool(body.default_enabled) if body else False
    try:
        row = await svc.install_plugin_from_repo(
            db, repo_id, plugin_name, default_enabled=default_enabled,
        )
        await db.commit()
        await trigger_reload(db, row.name)
        return row
    except PluginRepoNotFound as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except PluginNotInRepo as e:
        raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
    except DuplicatePluginName as e:
        raise HTTPException(409, detail={"code": e.code, "message": e.message}) from e
    except (GitOperationFailed, InvalidPluginMetadata) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
    except (PluginRepoError, RemotePluginError) as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message}) from e
