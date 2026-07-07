"""第三方插件安装管理 API（本地已安装列表 + 上传/启停/卸载）。"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models.account import Account
from ..db.models.plugin import InstalledPlugin
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..schemas.plugin_center import PluginCenterItem
from ..services import audit
from ..services import plugin_center_service as pcs
from ..services import plugin_install_service as pis
from ..worker.ipc import CMD_RELOAD_CONFIG, cmd_channel, make_cmd

log = logging.getLogger(__name__)
router = APIRouter(tags=["plugins"])


class PluginInstallOut(BaseModel):
    key: str
    source: str
    source_url: str | None = None
    source_label: str | None = None
    version: str
    enabled: bool
    signature_ok: bool | None
    installed_path: str
    manifest: dict[str, Any] | None = None
    installed_at: datetime | None = None
    updated_at: datetime | None = None


def _to_out(row: InstalledPlugin) -> PluginInstallOut:
    return PluginInstallOut(
        key=row.key,
        source=row.source,
        source_url=row.source_url,
        source_label=row.source_label,
        version=row.version,
        enabled=bool(row.enabled),
        signature_ok=row.signature_ok,
        installed_path=row.installed_path or "",
        manifest=row.manifest_json,
        installed_at=row.installed_at,
        updated_at=row.updated_at,
    )


def _bad(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _map_install_error(exc: pis.PluginInstallError) -> HTTPException:
    status_map = {
        "PLUGIN_NOT_FOUND": 404,
    }
    return _bad(exc.code, exc.message, status_map.get(exc.code, 400))


async def _read_signature(
    signature_file: UploadFile | None,
    signature: str | None,
) -> bytes | None:
    if signature_file is not None:
        data = await signature_file.read()
        return data or None
    raw = (signature or "").strip()
    if not raw:
        return None
    if len(raw) % 2 == 0 and all(ch in "0123456789abcdefABCDEF" for ch in raw):
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        pass
    return raw.encode("utf-8")


async def _broadcast_reload_config(db) -> int:
    aids = (await db.execute(select(Account.id))).scalars().all()
    try:
        redis = get_redis()
    except Exception:  # noqa: BLE001
        log.debug("get_redis 失败，跳过广播", exc_info=True)
        return 0

    n = 0
    for aid in aids:
        try:
            await redis.publish(cmd_channel(int(aid)), make_cmd(CMD_RELOAD_CONFIG))
            n += 1
        except Exception:  # noqa: BLE001
            log.debug("publish reload_config 失败 aid=%s", aid, exc_info=True)
    return n


@router.get("/api/plugins/installed-packages", response_model=list[PluginInstallOut])
async def list_installed_packages(
    db: DBSession, _user: CurrentUser
) -> list[PluginInstallOut]:
    rows = await pis.list_installed(db)
    return [_to_out(r) for r in rows]


@router.get("/api/plugins/installed-overview", response_model=list[PluginCenterItem])
async def list_installed_overview(
    db: DBSession, _user: CurrentUser
) -> list[PluginCenterItem]:
    return await pcs.list_installed_plugins_overview(db)


@router.post("/api/plugins/install/upload", response_model=PluginInstallOut)
async def upload_plugin_package(
    db: DBSession,
    user: CurrentUser,
    file: UploadFile = File(...),
    signature_file: UploadFile | None = File(None),
    signature: str | None = Form(None),
) -> PluginInstallOut:
    zip_bytes = await file.read()
    sig_bytes = await _read_signature(signature_file, signature)
    try:
        row = await pis.install_zip(db, zip_bytes=zip_bytes, signature=sig_bytes)
    except pis.PluginInstallError as exc:
        raise _map_install_error(exc) from exc
    await audit.write(db, user.id, "plugin.install_upload", target=f"plugin:{row.key}")
    await db.commit()
    await _broadcast_reload_config(db)
    return _to_out(row)


@router.post("/api/plugins/install/{key}/enable", response_model=PluginInstallOut)
async def enable_install(
    key: str, db: DBSession, user: CurrentUser
) -> PluginInstallOut:
    try:
        row = await pis.set_enabled(db, key, True)
    except pis.PluginInstallError as exc:
        raise _map_install_error(exc) from exc
    await audit.write(db, user.id, "plugin.install_enable", target=f"plugin:{key}")
    await db.commit()
    await _broadcast_reload_config(db)
    return _to_out(row)


@router.post("/api/plugins/install/{key}/disable", response_model=PluginInstallOut)
async def disable_install(
    key: str, db: DBSession, user: CurrentUser
) -> PluginInstallOut:
    try:
        row = await pis.set_enabled(db, key, False)
    except pis.PluginInstallError as exc:
        raise _map_install_error(exc) from exc
    await audit.write(db, user.id, "plugin.install_disable", target=f"plugin:{key}")
    await db.commit()
    await _broadcast_reload_config(db)
    return _to_out(row)


@router.delete("/api/plugins/install/{key}", status_code=204)
async def delete_install(key: str, db: DBSession, user: CurrentUser) -> None:
    deleted = await pis.uninstall(db, key)
    if not deleted:
        raise _bad("PLUGIN_NOT_FOUND", f"插件不存在: {key}", 404)
    await audit.write(db, user.id, "plugin.install_uninstall", target=f"plugin:{key}")
    await db.commit()
    await _broadcast_reload_config(db)


__all__ = ["router"]
