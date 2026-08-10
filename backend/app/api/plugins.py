"""插件管理 REST API（内置插件账号矩阵）。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models.account import Account
from ..db.models.feature import BUILTIN_FEATURES, AccountFeature, Feature
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..services import audit, feature_service
from ..worker.ipc import CMD_RELOAD_PLUGIN, cmd_channel, make_cmd

log = logging.getLogger(__name__)
router = APIRouter(tags=["plugins"])


class PluginInstallRequest(BaseModel):
    plugin_key: str
    account_ids: list[int]


class PluginAccountActionRequest(BaseModel):
    account_ids: list[int]


def _bad(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _is_builtin(plugin_key: str) -> bool:
    # BUILTIN_FEATURES 是惰性字典，首次访问时自动扫描；此后可通过 refresh() 强制刷新。
    # 这里不主动 refresh，避免每次 API 请求都扫文件系统；seed_builtin_features 负责刷新。
    return plugin_key in BUILTIN_FEATURES


@router.get("/api/plugins/installed")
async def list_installed(
    db: DBSession,
    _user: CurrentUser,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    q = select(AccountFeature, Feature).join(Feature, AccountFeature.feature_key == Feature.key)
    if account_id is not None:
        q = q.where(AccountFeature.account_id == account_id)
    rows = (await db.execute(q)).all()
    out: list[dict[str, Any]] = []
    for af, feat in rows:
        out.append(
            {
                "account_id": af.account_id,
                "feature_key": af.feature_key,
                "display_name": feat.display_name,
                "is_builtin": feat.is_builtin,
                "enabled": af.enabled,
                "state": af.state,
                "last_error": af.last_error,
                "version": feat.version,
            }
        )
    return out


def _ensure_builtin_or_501(plugin_key: str) -> None:
    if not _is_builtin(plugin_key):
        raise HTTPException(
            status_code=501,
            detail={
                "code": "NOT_IMPLEMENTED",
                "message": f"该旧入口仅支持内置插件: {plugin_key}",
            },
        )


@router.post("/api/plugins/install")
async def install_plugin(
    payload: PluginInstallRequest, db: DBSession, user: CurrentUser
) -> dict[str, Any]:
    _ensure_builtin_or_501(payload.plugin_key)
    await feature_service.seed_builtin_features(db)
    await _ensure_accounts_exist(db, payload.account_ids)
    n = await feature_service.bulk_set_enabled(
        db, payload.account_ids, payload.plugin_key, enabled=True
    )
    await audit.write(
        db,
        user.id,
        "plugin.install",
        target=f"plugin:{payload.plugin_key}",
        detail={"account_ids": payload.account_ids},
    )
    await db.commit()
    return {"applied": n}


@router.post("/api/plugins/uninstall")
async def uninstall_plugin(
    payload: PluginInstallRequest, db: DBSession, user: CurrentUser
) -> dict[str, Any]:
    _ensure_builtin_or_501(payload.plugin_key)
    await _ensure_accounts_exist(db, payload.account_ids)
    n = await feature_service.bulk_set_enabled(
        db, payload.account_ids, payload.plugin_key, enabled=False
    )
    await audit.write(
        db,
        user.id,
        "plugin.uninstall",
        target=f"plugin:{payload.plugin_key}",
        detail={"account_ids": payload.account_ids},
    )
    await db.commit()
    return {"applied": n}


@router.post("/api/plugins/{key}/enable")
async def enable_plugin(
    key: str, payload: PluginAccountActionRequest, db: DBSession, user: CurrentUser
) -> dict[str, Any]:
    _ensure_builtin_or_501(key)
    await feature_service.seed_builtin_features(db)
    await _ensure_accounts_exist(db, payload.account_ids)
    n = await feature_service.bulk_set_enabled(db, payload.account_ids, key, enabled=True)
    await audit.write(
        db,
        user.id,
        "plugin.enable",
        target=f"plugin:{key}",
        detail={"account_ids": payload.account_ids},
    )
    await db.commit()
    return {"applied": n}


@router.post("/api/plugins/{key}/disable")
async def disable_plugin(
    key: str, payload: PluginAccountActionRequest, db: DBSession, user: CurrentUser
) -> dict[str, Any]:
    _ensure_builtin_or_501(key)
    await _ensure_accounts_exist(db, payload.account_ids)
    n = await feature_service.bulk_set_enabled(db, payload.account_ids, key, enabled=False)
    await audit.write(
        db,
        user.id,
        "plugin.disable",
        target=f"plugin:{key}",
        detail={"account_ids": payload.account_ids},
    )
    await db.commit()
    return {"applied": n}


@router.post("/api/plugins/{key}/reload")
async def reload_plugin_endpoint(
    key: str, payload: PluginAccountActionRequest, db: DBSession, user: CurrentUser
) -> dict[str, Any]:
    _ensure_builtin_or_501(key)
    await _ensure_accounts_exist(db, payload.account_ids)
    redis = None
    try:
        redis = get_redis()
    except Exception:  # noqa: BLE001
        redis = None
    n = 0
    for aid in payload.account_ids:
        if redis is not None:
            try:
                await redis.publish(
                    cmd_channel(aid),
                    make_cmd(CMD_RELOAD_PLUGIN, plugin_key=key),
                )
                n += 1
            except Exception:  # noqa: BLE001
                log.debug("reload plugin 广播失败 aid=%s", aid, exc_info=True)
    await audit.write(
        db,
        user.id,
        "plugin.reload",
        target=f"plugin:{key}",
        detail={"account_ids": payload.account_ids},
    )
    await db.commit()
    return {"sent": n}


async def _ensure_accounts_exist(db, aids: list[int]) -> None:
    for aid in aids:
        if await db.get(Account, aid) is None:
            raise _bad("ACCOUNT_NOT_FOUND", f"账号不存在: {aid}", 404)


__all__ = ["router"]
