"""第三方插件安装管理 API（本地已安装列表 + 上传/启停/卸载）。"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..db.models.account import Account
from ..db.models.plugin import InstalledPlugin, PluginInstallHistory
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..schemas.plugin_center import PluginCenterItem
from ..services import audit
from ..services import plugin_center_service as pcs
from ..services import plugin_install_service as pis
from ..services.remote_plugin_service import RemotePluginError, trigger_reload
from ..settings import settings
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


class PluginChangelogOut(BaseModel):
    key: str
    available: bool
    content: str = ""
    truncated: bool = False
    message: str | None = None


class PluginBatchStateIn(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=100)
    enabled: bool


class PluginBatchStateItem(BaseModel):
    key: str
    ok: bool
    code: str | None = None
    message: str | None = None
    plugin: PluginInstallOut | None = None


class PluginBatchStateOut(BaseModel):
    enabled: bool
    succeeded: int
    failed: int
    reloaded_accounts: int
    items: list[PluginBatchStateItem]


class PluginInstallHistoryOut(BaseModel):
    id: int
    plugin_key: str
    event_type: str
    version: str | None = None
    previous_version: str | None = None
    source: str | None = None
    source_label: str | None = None
    enabled: bool | None = None
    signature_ok: bool | None = None
    detail: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


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


@router.get("/api/plugins/install/{key}/changelog", response_model=PluginChangelogOut)
async def get_installed_plugin_changelog(
    key: str, db: DBSession, _user: CurrentUser
) -> PluginChangelogOut:
    row = await db.get(InstalledPlugin, key)
    if row is None:
        raise _bad("PLUGIN_NOT_FOUND", f"插件不存在: {key}", 404)

    installed_root = settings.plugins_installed_path.resolve()
    plugin_root = Path(row.installed_path or installed_root / key).resolve()
    if not plugin_root.is_relative_to(installed_root):
        raise _bad("PLUGIN_PATH_INVALID", "插件安装路径不在受管目录内", 409)

    changelog_path = (plugin_root / "CHANGELOG.md").resolve()
    if not changelog_path.is_relative_to(plugin_root):
        raise _bad("PLUGIN_CHANGELOG_PATH_INVALID", "插件更新日志路径越界", 409)
    if not changelog_path.is_file():
        return PluginChangelogOut(
            key=key,
            available=False,
            message="该插件未提供 CHANGELOG.md。",
        )

    max_bytes = 256 * 1024
    with changelog_path.open("rb") as changelog_file:
        raw = changelog_file.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    content = raw[:max_bytes].decode("utf-8", errors="replace")
    return PluginChangelogOut(
        key=key,
        available=True,
        content=content,
        truncated=truncated,
        message="更新日志过长，仅显示前 256 KiB。" if truncated else None,
    )


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
    try:
        await trigger_reload(db, row.key)
    except RemotePluginError as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": exc.message}) from exc
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


@router.post("/api/plugins/install/batch-state", response_model=PluginBatchStateOut)
async def batch_install_state(
    body: PluginBatchStateIn,
    db: DBSession,
    user: CurrentUser,
) -> PluginBatchStateOut:
    keys = list(dict.fromkeys(key.strip() for key in body.keys if key.strip()))
    if not keys:
        raise _bad("PLUGIN_KEYS_REQUIRED", "至少选择一个插件")

    items: list[PluginBatchStateItem] = []
    for key in keys:
        try:
            row = await pis.set_enabled(db, key, body.enabled)
        except pis.PluginInstallError as exc:
            items.append(
                PluginBatchStateItem(
                    key=key,
                    ok=False,
                    code=exc.code,
                    message=exc.message,
                )
            )
            continue
        items.append(PluginBatchStateItem(key=key, ok=True, plugin=_to_out(row)))

    succeeded = sum(1 for item in items if item.ok)
    await audit.write(
        db,
        user.id,
        "plugin.install_batch_enable" if body.enabled else "plugin.install_batch_disable",
        target="plugins:batch",
        detail={
            "keys": keys,
            "requested": len(keys),
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
        },
    )
    await db.commit()
    reloaded = await _broadcast_reload_config(db) if succeeded else 0
    return PluginBatchStateOut(
        enabled=body.enabled,
        succeeded=succeeded,
        failed=len(items) - succeeded,
        reloaded_accounts=reloaded,
        items=items,
    )


@router.get(
    "/api/plugins/install/{key}/history",
    response_model=list[PluginInstallHistoryOut],
)
async def get_installed_plugin_history(
    key: str,
    db: DBSession,
    _user: CurrentUser,
    limit: int = 50,
) -> list[PluginInstallHistoryOut]:
    safe_limit = max(1, min(int(limit), 200))
    rows = (
        await db.execute(
            select(PluginInstallHistory)
            .where(PluginInstallHistory.plugin_key == key)
            .order_by(PluginInstallHistory.created_at.desc(), PluginInstallHistory.id.desc())
            .limit(safe_limit)
        )
    ).scalars().all()
    return [PluginInstallHistoryOut.model_validate(row) for row in rows]


@router.delete("/api/plugins/install/{key}", status_code=204)
async def delete_install(key: str, db: DBSession, user: CurrentUser) -> None:
    deleted = await pis.uninstall(db, key)
    if not deleted:
        raise _bad("PLUGIN_NOT_FOUND", f"插件不存在: {key}", 404)
    await audit.write(db, user.id, "plugin.install_uninstall", target=f"plugin:{key}")
    await db.commit()
    await _broadcast_reload_config(db)


__all__ = ["router"]
