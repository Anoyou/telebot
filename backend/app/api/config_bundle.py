"""账号 Config Bundle 导出 / dry-run API。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from ..db.models.feature import Feature
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..schemas.config_bundle import (
    ConfigBundleConfirmResponse,
    ConfigBundleDryRunResponse,
    ConfigBundleExport,
)
from ..services import audit, platform_capabilities
from ..services.config_bundle_service import (
    BundleConfirmError,
    BundleTooLarge,
    apply_bundle_confirm,
    assert_bundle_size,
    available_command_templates,
    build_preview_context_digest,
    build_preview_signature,
    compare_bundles,
    load_config_bundle,
)
from ..worker.ipc import (
    CMD_RELOAD_COMMANDS,
    CMD_RELOAD_CONFIG,
    CMD_RELOAD_IGNORED,
    publish_cmd_with_ack,
)

router = APIRouter(tags=["config-bundle"])
log = logging.getLogger(__name__)


def _bad(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _load_bundle(db, aid: int) -> ConfigBundleExport:
    try:
        return await load_config_bundle(db, aid)
    except BundleConfirmError as exc:
        raise _bad(exc.code, exc.message, 404) from exc


async def _available_feature_map(db) -> dict[str, str]:
    rows = (await db.execute(select(Feature))).scalars().all()
    return {row.key: row.display_name for row in rows}


async def _available_command_templates(db) -> dict[str, dict[str, object]]:
    return await available_command_templates(db)


@router.get("/api/accounts/{aid}/config-bundle/export")
async def export_config_bundle(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
) -> Response:
    bundle = await _load_bundle(db, aid)
    try:
        body = assert_bundle_size(bundle)
    except BundleTooLarge as exc:
        raise _bad("BUNDLE_TOO_LARGE", "bundle 超过 1MB，请拆分后再导出", 413) from exc

    filename = f"telepilot-config-bundle-{aid}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/api/accounts/{aid}/config-bundle/dry-run",
    response_model=ConfigBundleDryRunResponse,
)
async def dry_run_config_bundle(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
    request: Request,
    file: UploadFile = File(...),
    apply_conflicts: bool = Form(False),
    confirm_chat_id_conflicts: bool = Form(False),
) -> ConfigBundleDryRunResponse:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > 1_048_576:
                raise _bad("BUNDLE_TOO_LARGE", "bundle 超过 1MB，请拆分后再导入", 413)
        except ValueError:
            pass

    content = await file.read()
    if len(content) > 1_048_576:
        raise _bad("BUNDLE_TOO_LARGE", "bundle 超过 1MB，请拆分后再导入", 413)
    try:
        payload = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        raise _bad("BUNDLE_INVALID_JSON", "bundle 不是合法 JSON") from exc

    try:
        source_bundle = ConfigBundleExport.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise _bad("BUNDLE_INVALID", "bundle 结构不符合规范") from exc

    target_bundle = await _load_bundle(db, aid)
    available_features = await _available_feature_map(db)
    available_templates = await _available_command_templates(db)
    report = compare_bundles(
        source_bundle,
        target_bundle,
        available_features=available_features,
        available_command_templates=available_templates,
    )
    report.preview_signature = build_preview_signature(
        account_id=aid,
        file_content=content,
        apply_conflicts=apply_conflicts,
        confirm_chat_id_conflicts=confirm_chat_id_conflicts,
        preview_context_digest=build_preview_context_digest(
            target=target_bundle,
            available_features=available_features,
            available_command_templates=available_templates,
        ),
    )
    return report


async def _notify_worker_reload(account_id: int) -> None:
    try:
        redis = get_redis()
        for cmd in (CMD_RELOAD_CONFIG, CMD_RELOAD_COMMANDS, CMD_RELOAD_IGNORED):
            ok = await publish_cmd_with_ack(redis, account_id, cmd)
            if not ok:
                log.debug("worker %s 未确认 aid=%s，将由周期 reconcile 收敛", cmd, account_id)
    except Exception:  # noqa: BLE001
        log.debug("通知 worker reload 失败 aid=%s", account_id, exc_info=True)


@router.post(
    "/api/accounts/{aid}/config-bundle/confirm",
    response_model=ConfigBundleConfirmResponse,
)
async def confirm_config_bundle(
    aid: int,
    db: DBSession,
    user: CurrentUser,
    request: Request,
    file: UploadFile = File(...),
    apply_conflicts: bool = Form(False),
    confirm_chat_id_conflicts: bool = Form(False),
    preview_signature: str = Form(""),
) -> ConfigBundleConfirmResponse:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > 1_048_576:
                raise _bad("BUNDLE_TOO_LARGE", "bundle 超过 1MB，请拆分后再导入", 413)
        except ValueError:
            pass

    content = await file.read()
    if len(content) > 1_048_576:
        raise _bad("BUNDLE_TOO_LARGE", "bundle 超过 1MB，请拆分后再导入", 413)
    try:
        payload = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        raise _bad("BUNDLE_INVALID_JSON", "bundle 不是合法 JSON") from exc

    try:
        source_bundle = ConfigBundleExport.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise _bad("BUNDLE_INVALID", "bundle 结构不符合规范") from exc

    target_bundle = await _load_bundle(db, aid)
    available_features = await _available_feature_map(db)
    available_templates = await _available_command_templates(db)
    expected_signature = build_preview_signature(
        account_id=aid,
        file_content=content,
        apply_conflicts=apply_conflicts,
        confirm_chat_id_conflicts=confirm_chat_id_conflicts,
        preview_context_digest=build_preview_context_digest(
            target=target_bundle,
            available_features=available_features,
            available_command_templates=available_templates,
        ),
    )
    if preview_signature != expected_signature:
        raise _bad(
            "PREVIEW_SIGNATURE_MISMATCH",
            "预览与确认参数不一致或目标配置已变化，请重新 dry-run 后再确认写入",
        )

    dry_run = compare_bundles(
        source_bundle,
        target_bundle,
        available_features=available_features,
        available_command_templates=available_templates,
    )
    try:
        imported, skipped, conflicts, warnings = await apply_bundle_confirm(
            db,
            account_id=aid,
            source=source_bundle,
            dry_run=dry_run,
            available_command_templates=available_templates,
            apply_conflicts=apply_conflicts,
            confirm_chat_id_conflicts=confirm_chat_id_conflicts,
            web_user_id=int(user.id),
        )
    except BundleConfirmError as exc:
        raise _bad(exc.code, exc.message) from exc
    except platform_capabilities.PluginCapabilityBlocked as exc:
        raise _bad(exc.error_code, str(exc), 409) from exc

    await audit.write(
        db,
        user.id,
        "account.config_bundle.confirm",
        target=f"account:{aid}",
        detail={
            "source_account_id": source_bundle.source_account.id,
            "apply_conflicts": apply_conflicts,
            "confirm_chat_id_conflicts": confirm_chat_id_conflicts,
            "imported": imported,
            "skipped": skipped,
            "conflicts": conflicts,
        },
    )
    await db.commit()
    if imported > 0:
        await _notify_worker_reload(aid)

    return ConfigBundleConfirmResponse(
        source_account=source_bundle.source_account,
        target_account=target_bundle.source_account,
        imported=imported,
        skipped=skipped,
        conflicts=conflicts,
        warnings=warnings,
        preview_signature=expected_signature,
    )
