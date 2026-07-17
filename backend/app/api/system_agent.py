"""System Agent HTTP API：配置、会话、NDJSON 消息流。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..db.models.system_agent import CHANNEL_WEB, SESSION_STATUS_ACTIVE
from ..deps import CurrentUser, DBSession
from ..schemas.system_agent import (
    SystemAgentActionConfirmOut,
    SystemAgentActionOut,
    SystemAgentCapabilitiesOut,
    SystemAgentConfigOut,
    SystemAgentConfigPatch,
    SystemAgentMessageCreate,
    SystemAgentMessageOut,
    SystemAgentSecretInput,
    SystemAgentSecretInputOut,
    SystemAgentSessionCreate,
    SystemAgentSessionOut,
    SystemAgentSessionUpdate,
)
from ..services.system_agent import get_system_agent_service
from ..services.system_agent.actions import (
    action_to_dict,
    decrypt_secret_payload,
    encrypt_secret_payload,
    get_action,
    list_actions,
    lock_action,
    mark_expired_if_needed,
    reject_action,
    web_owns_action,
)
from ..services.system_agent.executor import get_action_executor
from ..services.system_agent.registry import get_registry

router = APIRouter(prefix="/api/system-agent", tags=["system-agent"])


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _session_out(row: Any) -> SystemAgentSessionOut:
    return SystemAgentSessionOut.model_validate(row)


# ── 配置 ─────────────────────────────────────────────────────────
@router.get("/config", response_model=SystemAgentConfigOut)
async def get_config(db: DBSession, _user: CurrentUser) -> SystemAgentConfigOut:
    svc = get_system_agent_service()
    cfg = await svc.get_config(db)
    return SystemAgentConfigOut(**cfg)


@router.patch("/config", response_model=SystemAgentConfigOut)
async def patch_config(
    payload: SystemAgentConfigPatch,
    db: DBSession,
    _user: CurrentUser,
) -> SystemAgentConfigOut:
    svc = get_system_agent_service()
    patch = payload.model_dump(exclude_unset=True)
    cfg = await svc.update_config(db, patch)
    await db.commit()
    return SystemAgentConfigOut(**cfg)


@router.get("/capabilities", response_model=SystemAgentCapabilitiesOut)
async def get_capabilities(db: DBSession, _user: CurrentUser) -> SystemAgentCapabilitiesOut:
    svc = get_system_agent_service()
    data = await svc.get_capabilities(db, channel=CHANNEL_WEB, role="admin")
    return SystemAgentCapabilitiesOut(**data)


# ── 会话 ─────────────────────────────────────────────────────────
@router.post("/sessions", response_model=SystemAgentSessionOut)
async def create_session(
    payload: SystemAgentSessionCreate,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentSessionOut:
    svc = get_system_agent_service()
    session = await svc.create_session(
        db,
        channel=CHANNEL_WEB,
        web_user_id=user.id,
        account_id=payload.account_id,
        title=payload.title,
    )
    await db.commit()
    await db.refresh(session)
    return _session_out(session)


@router.get("/sessions", response_model=list[SystemAgentSessionOut])
async def list_sessions(
    db: DBSession,
    user: CurrentUser,
    status: str | None = Query(default=SESSION_STATUS_ACTIVE),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SystemAgentSessionOut]:
    svc = get_system_agent_service()
    rows = await svc.list_sessions(db, web_user_id=user.id, status=status, limit=limit)
    return [_session_out(r) for r in rows]


@router.get("/sessions/{session_id}", response_model=SystemAgentSessionOut)
async def get_session(
    session_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentSessionOut:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    return _session_out(session)


@router.patch("/sessions/{session_id}", response_model=SystemAgentSessionOut)
async def update_session(
    session_id: str,
    payload: SystemAgentSessionUpdate,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentSessionOut:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    try:
        await svc.update_session(
            db,
            session,
            title=payload.title,
            status=payload.status,
            account_id=payload.account_id if "account_id" in payload.model_fields_set else ...,
        )
    except ValueError as exc:
        raise _err("INVALID_SESSION", str(exc)) from None
    await db.commit()
    await db.refresh(session)
    return _session_out(session)


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, Any]:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    await svc.delete_session(db, session)
    await db.commit()
    return {"ok": True, "deleted": session_id}


@router.delete("/sessions")
async def delete_all_sessions(db: DBSession, user: CurrentUser) -> dict[str, Any]:
    svc = get_system_agent_service()
    count = await svc.delete_all_sessions(db, web_user_id=user.id)
    await db.commit()
    return {"ok": True, "deleted": count}


# ── 消息 ─────────────────────────────────────────────────────────
@router.get("/sessions/{session_id}/messages", response_model=list[SystemAgentMessageOut])
async def list_messages(
    session_id: str,
    db: DBSession,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None),
) -> list[SystemAgentMessageOut]:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    rows = await svc.list_messages(db, session_id, limit=limit, before_id=before_id)
    return [SystemAgentMessageOut.model_validate(r) for r in rows]


@router.post("/sessions/{session_id}/messages/stream")
async def stream_message(
    session_id: str,
    payload: SystemAgentMessageCreate,
    db: DBSession,
    user: CurrentUser,
) -> StreamingResponse:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)

    # 可选更新账号上下文
    if payload.account_id is not None and session.account_id != payload.account_id:
        await svc.update_session(db, session, account_id=payload.account_id)

    async def event_source():
        try:
            async for event in svc.stream_message(
                db,
                session=session,
                text=payload.content,
                role="admin",
                channel=CHANNEL_WEB,
                web_user_id=user.id,
            ):
                yield json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            err = {
                "type": "error",
                "code": "STREAM_FAILED",
                "message": str(exc)[:500],
                "session_id": session_id,
            }
            yield json.dumps(err, ensure_ascii=False, separators=(",", ":")) + "\n"
            done = {"type": "done", "ok": False, "session_id": session_id}
            yield json.dumps(done, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        event_source(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ── Action（阶段 2）──────────────────────────────────────────────
@router.get("/actions", response_model=list[SystemAgentActionOut])
async def list_system_agent_actions(
    db: DBSession,
    user: CurrentUser,
    session_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SystemAgentActionOut]:
    rows = await list_actions(
        db,
        session_id=session_id,
        web_user_id=user.id,
        status=status,
        limit=limit,
    )
    return [SystemAgentActionOut(**action_to_dict(r)) for r in rows]


@router.get("/actions/{action_id}", response_model=SystemAgentActionOut)
async def get_system_agent_action(
    action_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentActionOut:
    row = await get_action(db, action_id)
    if row is None or not web_owns_action(row, user.id):
        raise _err("ACTION_NOT_FOUND", "操作不存在", 404)
    return SystemAgentActionOut(**action_to_dict(row))


@router.post("/actions/{action_id}/confirm", response_model=SystemAgentActionConfirmOut)
async def confirm_system_agent_action(
    action_id: str,
    user: CurrentUser,
) -> SystemAgentActionConfirmOut:
    result = await get_action_executor().confirm(
        action_id=action_id,
        role="admin",
        channel=CHANNEL_WEB,
        web_user_id=user.id,
    )
    action = result.get("action")
    return SystemAgentActionConfirmOut(
        ok=bool(result.get("ok")),
        already_final=bool(result.get("already_final")),
        keep_pending=bool(result.get("keep_pending")),
        error_code=result.get("error_code"),
        error_message=result.get("error_message"),
        business_changed=result.get("business_changed"),
        action=SystemAgentActionOut(**action) if isinstance(action, dict) else None,
    )


@router.post("/actions/{action_id}/reject", response_model=SystemAgentActionOut)
async def reject_system_agent_action(
    action_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentActionOut:
    row = await lock_action(db, action_id)
    if row is None or not web_owns_action(row, user.id):
        raise _err("ACTION_NOT_FOUND", "操作不存在", 404)
    row = await reject_action(db, row)
    await db.commit()
    await db.refresh(row)
    return SystemAgentActionOut(**action_to_dict(row))


@router.post("/actions/{action_id}/retry-runtime-sync", response_model=SystemAgentActionConfirmOut)
async def retry_runtime_sync_action(
    action_id: str,
    user: CurrentUser,
) -> SystemAgentActionConfirmOut:
    # 先校验所有权
    from ..db.base import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = await get_action(db, action_id)
        if row is None or not web_owns_action(row, user.id):
            raise _err("ACTION_NOT_FOUND", "操作不存在", 404)
    result = await get_action_executor().retry_runtime_sync(action_id)
    action = result.get("action")
    return SystemAgentActionConfirmOut(
        ok=bool(result.get("ok")),
        error_code=result.get("error_code"),
        error_message=result.get("error_message"),
        action=SystemAgentActionOut(**action) if isinstance(action, dict) else None,
    )


@router.post("/actions/{action_id}/secret-input", response_model=SystemAgentSecretInputOut)
async def secret_input_action(
    action_id: str,
    payload: SystemAgentSecretInput,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentSecretInputOut:
    """Web 内联卡片补填密钥；只接受工具注册表声明字段，响应不回显明文。"""

    row = await lock_action(db, action_id)
    if row is None or not web_owns_action(row, user.id):
        raise _err("ACTION_NOT_FOUND", "操作不存在", 404)
    row = await mark_expired_if_needed(db, row)
    if row.status != "pending":
        raise _err("INVALID_STATUS", "仅待确认操作可补填密钥", 400)

    spec = get_registry().get(row.tool_name)
    allowed = set(spec.secret_argument_names) if spec else set()
    if not allowed:
        raise _err("NO_SECRET_FIELDS", "该操作不接受密钥补填", 400)

    incoming = payload.fields or {}
    secrets: dict[str, Any] = decrypt_secret_payload(row.secret_payload_enc)
    accepted: list[str] = []
    for name, value in incoming.items():
        if name not in allowed:
            raise _err("FIELD_NOT_ALLOWED", f"字段 {name} 未在工具声明中", 400)
        text = str(value or "").strip()
        if not text:
            continue
        secrets[name] = text
        accepted.append(name)

    if not accepted:
        raise _err("EMPTY_SECRET", "未提供有效密钥", 400)

    row.secret_payload_enc = encrypt_secret_payload(secrets)
    existing_fields = list(row.secret_fields or [])
    for name in accepted:
        if name not in existing_fields:
            existing_fields.append(name)
    row.secret_fields = existing_fields
    # 普通 arguments 只标记 has_*，不写明文
    args = dict(row.arguments or {})
    for name in accepted:
        args.pop(name, None)
        args[f"has_{name}"] = True
    row.arguments = args
    # 与 Bot 密钥写回一致：清除旧预检错误，避免卡片仍显示红字
    row.error_code = None
    row.error_message = None
    await db.commit()
    await db.refresh(row)
    return SystemAgentSecretInputOut(
        action_id=row.id,
        has_secret=True,
        secret_fields=list(row.secret_fields or []),
    )
