"""System Agent HTTP API：配置、会话、NDJSON 消息流。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..db.models.system_agent import (
    CHANNEL_WEB,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MESSAGE_RUN_FAILED,
    SESSION_STATUS_ACTIVE,
    SystemAgentRun,
    SystemAgentSession,
)
from ..deps import CurrentUser, DBSession
from ..schemas.system_agent import (
    SystemAgentActionConfirmOut,
    SystemAgentActionOut,
    SystemAgentCapabilitiesOut,
    SystemAgentConfigOut,
    SystemAgentConfigPatch,
    SystemAgentMessageCreate,
    SystemAgentMessageOut,
    SystemAgentMessageRetry,
    SystemAgentRegenerateRunCreate,
    SystemAgentRetryRunCreate,
    SystemAgentRunCreate,
    SystemAgentRunEventOut,
    SystemAgentRunOut,
    SystemAgentSecretInput,
    SystemAgentSecretInputOut,
    SystemAgentSessionCreate,
    SystemAgentSessionOut,
    SystemAgentSessionUpdate,
    SystemAgentUserMemoryCreate,
    SystemAgentUserMemoryOut,
    SystemAgentUserMemoryPatch,
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
from ..services.system_agent.run_manager import (
    RunConflictError,
    RunNotFoundError,
    get_system_agent_run_manager,
)

router = APIRouter(prefix="/api/system-agent", tags=["system-agent"])
log = logging.getLogger(__name__)


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _session_out(row: Any) -> SystemAgentSessionOut:
    return SystemAgentSessionOut.model_validate(row)


def _run_out(row: Any) -> SystemAgentRunOut:
    return SystemAgentRunOut.model_validate(row)


async def _owned_run(db: DBSession, run_id: str, web_user_id: int) -> SystemAgentRun:
    result = await db.execute(
        select(SystemAgentRun).where(
            SystemAgentRun.id == run_id,
            SystemAgentRun.web_user_id == web_user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise _err("RUN_NOT_FOUND", "助手运行不存在", 404)
    return row


def _run_stream_response(run_id: str, *, after_seq: int = 0) -> StreamingResponse:
    manager = get_system_agent_run_manager()

    async def event_source():
        try:
            async for event in manager.stream_events(run_id, after_seq=after_seq):
                yield json.dumps(
                    event,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                ) + "\n"
        except Exception as exc:  # noqa: BLE001
            # 订阅失败不能取消仍在执行的后台 run；客户端未收到 done 时会按游标重连。
            log.exception("system agent run subscription failed run=%s", run_id)
            err = {
                "type": "error",
                "code": "RUN_STREAM_FAILED",
                "message": f"助手进度连接失败（{type(exc).__name__}），正在等待重连。",
                "run_id": run_id,
            }
            yield json.dumps(err, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        event_source(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


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
    # 刷新插件工具插槽（安装/启停后能力矩阵立即可见）
    try:
        from ..services.system_agent.plugin_tools import refresh_plugin_system_agent_tools

        await refresh_plugin_system_agent_tools(db)
    except Exception:  # noqa: BLE001
        log.debug("refresh plugin system_agent tools failed", exc_info=True)
    svc = get_system_agent_service()
    data = await svc.get_capabilities(db, channel=CHANNEL_WEB, role="admin")
    return SystemAgentCapabilitiesOut(**data)


# ── 长期记忆 ─────────────────────────────────────────────────────
@router.get("/memory", response_model=list[SystemAgentUserMemoryOut])
async def list_user_memory(db: DBSession, user: CurrentUser) -> list[SystemAgentUserMemoryOut]:
    from ..services.system_agent.user_memory import list_memories, memory_to_dict

    rows = await list_memories(db, scope_type="web_user", scope_id=int(user.id))
    return [SystemAgentUserMemoryOut(**memory_to_dict(r)) for r in rows]


@router.post("/memory", response_model=SystemAgentUserMemoryOut)
async def create_user_memory(
    payload: SystemAgentUserMemoryCreate,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentUserMemoryOut:
    from ..services.system_agent.user_memory import create_memory, memory_to_dict

    try:
        row = await create_memory(
            db,
            scope_type="web_user",
            scope_id=int(user.id),
            content=payload.content,
            source="user_set",
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise _err("MEMORY_INVALID", str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return SystemAgentUserMemoryOut(**memory_to_dict(row))


@router.patch("/memory/{memory_id}", response_model=SystemAgentUserMemoryOut)
async def patch_user_memory(
    memory_id: int,
    payload: SystemAgentUserMemoryPatch,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentUserMemoryOut:
    from ..services.system_agent.user_memory import memory_to_dict, update_memory

    try:
        row = await update_memory(
            db,
            memory_id=memory_id,
            scope_type="web_user",
            scope_id=int(user.id),
            content=payload.content,
            enabled=payload.enabled,
        )
    except LookupError as exc:
        raise _err("MEMORY_NOT_FOUND", str(exc), status=404) from exc
    except ValueError as exc:
        raise _err("MEMORY_INVALID", str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return SystemAgentUserMemoryOut(**memory_to_dict(row))


@router.delete("/memory/{memory_id}")
async def delete_user_memory(
    memory_id: int,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, Any]:
    from ..services.system_agent.user_memory import delete_memory

    try:
        await delete_memory(
            db,
            memory_id=memory_id,
            scope_type="web_user",
            scope_id=int(user.id),
        )
    except LookupError as exc:
        raise _err("MEMORY_NOT_FOUND", str(exc), status=404) from exc
    await db.commit()
    return {"ok": True, "id": memory_id}


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
    origin: str | None = Query(default=None, description="interactive | scheduled；缺省返回全部"),
    include_bot: bool = Query(default=False, description="管理员会话列表是否包含 Telegram Bot 会话"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SystemAgentSessionOut]:
    svc = get_system_agent_service()
    rows = await svc.list_sessions(
        db,
        web_user_id=user.id,
        status=status,
        origin=origin,
        include_bot_sessions=include_bot,
        limit=limit,
    )
    return [_session_out(r) for r in rows]


@router.get("/sessions/{session_id}", response_model=SystemAgentSessionOut)
async def get_session(
    session_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentSessionOut:
    svc = get_system_agent_service()
    session = await svc.get_session(
        db,
        session_id,
        web_user_id=user.id,
        allow_bot_session=True,
    )
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
    session = await svc.get_session(
        db,
        session_id,
        web_user_id=user.id,
        allow_bot_session=True,
    )
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    if await svc.reconcile_stale_messages(db, session_id):
        await db.commit()
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

    await db.commit()
    try:
        run = await get_system_agent_run_manager().start_run(
            session_id=session_id,
            web_user_id=user.id,
            client_request_id=str(uuid.uuid4()),
            text=payload.content,
            model_selection=(
                payload.model_selection.model_dump() if payload.model_selection else None
            ),
        )
    except RunConflictError as exc:
        raise _err("RUN_CONFLICT", str(exc), 409) from None
    return _run_stream_response(run.id)


@router.post("/sessions/{session_id}/messages/{message_id}/retry/stream")
async def retry_message(
    session_id: str,
    message_id: int,
    payload: SystemAgentMessageRetry,
    db: DBSession,
    user: CurrentUser,
) -> StreamingResponse:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    message = await svc.get_message(db, message_id, session_id=session_id)
    if message is None or message.role != MESSAGE_ROLE_USER:
        raise _err("MESSAGE_NOT_FOUND", "可重试消息不存在", 404)
    if message.run_status != MESSAGE_RUN_FAILED:
        raise _err("MESSAGE_NOT_RETRYABLE", "只有失败消息可以重试", 409)
    if payload.account_id is not None and session.account_id != payload.account_id:
        await svc.update_session(db, session, account_id=payload.account_id)

    await db.commit()
    try:
        run = await get_system_agent_run_manager().start_run(
            session_id=session_id,
            web_user_id=user.id,
            client_request_id=str(uuid.uuid4()),
            text="",
            retry_message_id=message.id,
            fallback_provider_id=payload.fallback_provider_id,
            approved_tools=payload.approved_tools,
            model_selection=(
                payload.model_selection.model_dump() if payload.model_selection else None
            ),
        )
    except RunConflictError as exc:
        raise _err("RUN_CONFLICT", str(exc), 409) from None
    return _run_stream_response(run.id)


# ── Durable Run ──────────────────────────────────────────────────
@router.get("/runs", response_model=list[SystemAgentRunOut])
async def list_system_agent_runs(
    user: CurrentUser,
    status: str | None = Query(
        default=None,
        pattern="^(queued|running|succeeded|failed|cancelled)$",
    ),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[SystemAgentRunOut]:
    rows = await get_system_agent_run_manager().list_runs(
        web_user_id=user.id,
        status=status,
        since=since,
        until=until,
        limit=limit,
    )
    return [_run_out(row) for row in rows]


@router.post(
    "/sessions/{session_id}/runs",
    response_model=SystemAgentRunOut,
    status_code=202,
)
async def start_system_agent_run(
    session_id: str,
    payload: SystemAgentRunCreate,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentRunOut:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    if payload.account_id is not None and session.account_id != payload.account_id:
        await svc.update_session(db, session, account_id=payload.account_id)
    await db.commit()
    try:
        row = await get_system_agent_run_manager().start_run(
            session_id=session_id,
            web_user_id=user.id,
            client_request_id=payload.client_request_id,
            text=payload.content,
            model_selection=(
                payload.model_selection.model_dump() if payload.model_selection else None
            ),
        )
    except RunConflictError as exc:
        raise _err("RUN_CONFLICT", str(exc), 409) from None
    return _run_out(row)


@router.post(
    "/sessions/{session_id}/messages/{message_id}/retry/runs",
    response_model=SystemAgentRunOut,
    status_code=202,
)
async def start_system_agent_retry_run(
    session_id: str,
    message_id: int,
    payload: SystemAgentRetryRunCreate,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentRunOut:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    message = await svc.get_message(db, message_id, session_id=session_id)
    if message is None or message.role != MESSAGE_ROLE_USER:
        raise _err("MESSAGE_NOT_FOUND", "可重试消息不存在", 404)
    if message.run_status != MESSAGE_RUN_FAILED:
        raise _err("MESSAGE_NOT_RETRYABLE", "只有失败消息可以重试", 409)
    if payload.account_id is not None and session.account_id != payload.account_id:
        await svc.update_session(db, session, account_id=payload.account_id)
    await db.commit()
    try:
        row = await get_system_agent_run_manager().start_run(
            session_id=session_id,
            web_user_id=user.id,
            client_request_id=payload.client_request_id,
            text="",
            retry_message_id=message.id,
            fallback_provider_id=payload.fallback_provider_id,
            approved_tools=payload.approved_tools,
            model_selection=(
                payload.model_selection.model_dump() if payload.model_selection else None
            ),
        )
    except RunConflictError as exc:
        raise _err("RUN_CONFLICT", str(exc), 409) from None
    return _run_out(row)


@router.post(
    "/sessions/{session_id}/messages/{message_id}/regenerate/runs",
    response_model=SystemAgentRunOut,
    status_code=202,
)
async def start_system_agent_regenerate_run(
    session_id: str,
    message_id: int,
    payload: SystemAgentRegenerateRunCreate,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentRunOut:
    svc = get_system_agent_service()
    session = await svc.get_session(db, session_id, web_user_id=user.id)
    if session is None:
        raise _err("SESSION_NOT_FOUND", "会话不存在", 404)
    message = await svc.get_message(db, message_id, session_id=session_id)
    assistant_message = await svc.get_message(
        db,
        payload.assistant_message_id,
        session_id=session_id,
    )
    if message is None or message.role != MESSAGE_ROLE_USER:
        raise _err("MESSAGE_NOT_FOUND", "用户消息不存在", 404)
    if assistant_message is None or assistant_message.role != MESSAGE_ROLE_ASSISTANT:
        raise _err("MESSAGE_NOT_FOUND", "助手回答不存在", 404)
    if not await svc.is_latest_completed_pair(
        db,
        session_id=session_id,
        user_message_id=message.id,
        assistant_message_id=assistant_message.id,
    ):
        raise _err(
            "MESSAGE_NOT_REGENERATABLE",
            "只能编辑或重新生成当前会话最新完成的一轮",
            409,
        )
    edited_content = payload.content.strip() if payload.content is not None else None
    if payload.content is not None and not edited_content:
        raise _err("EMPTY_MESSAGE", "消息不能为空", 422)
    await db.commit()
    try:
        row = await get_system_agent_run_manager().start_run(
            session_id=session_id,
            web_user_id=user.id,
            client_request_id=payload.client_request_id,
            text=edited_content or "",
            account_id=payload.account_id,
            regenerate_message_id=message.id,
            regenerate_assistant_message_id=assistant_message.id,
            fallback_provider_id=payload.fallback_provider_id,
            approved_tools=payload.approved_tools,
            model_selection=(
                payload.model_selection.model_dump() if payload.model_selection else None
            ),
        )
    except RunConflictError as exc:
        raise _err("MESSAGE_NOT_REGENERATABLE", str(exc), 409) from None
    return _run_out(row)


@router.get("/runs/{run_id}", response_model=SystemAgentRunOut)
async def get_system_agent_run(
    run_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentRunOut:
    await _owned_run(db, run_id, user.id)
    try:
        row = await get_system_agent_run_manager().get_run(run_id)
    except RunNotFoundError:
        raise _err("RUN_NOT_FOUND", "助手运行不存在", 404) from None
    return _run_out(row)


@router.get("/runs/{run_id}/events", response_model=list[SystemAgentRunEventOut])
async def list_system_agent_run_events(
    run_id: str,
    db: DBSession,
    user: CurrentUser,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[SystemAgentRunEventOut]:
    await _owned_run(db, run_id, user.id)
    rows = await get_system_agent_run_manager().list_events(
        run_id,
        after_seq=after_seq,
        limit=limit,
    )
    return [
        SystemAgentRunEventOut(
            run_id=row.run_id,
            seq=row.seq,
            event=dict(row.event or {}),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/runs/{run_id}/stream")
async def stream_system_agent_run(
    run_id: str,
    db: DBSession,
    user: CurrentUser,
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    await _owned_run(db, run_id, user.id)
    return _run_stream_response(run_id, after_seq=after_seq)


@router.post("/runs/{run_id}/cancel", response_model=SystemAgentRunOut)
async def cancel_system_agent_run(
    run_id: str,
    db: DBSession,
    user: CurrentUser,
) -> SystemAgentRunOut:
    await _owned_run(db, run_id, user.id)
    try:
        row = await get_system_agent_run_manager().cancel_run(run_id)
    except RunNotFoundError:
        raise _err("RUN_NOT_FOUND", "助手运行不存在", 404) from None
    return _run_out(row)


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
    session_ids = {r.session_id for r in rows if r.session_id}
    session_meta: dict[str, Any] = {}
    if session_ids:
        result = await db.execute(
            select(SystemAgentSession).where(SystemAgentSession.id.in_(session_ids))
        )
        for sess in result.scalars().all():
            session_meta[sess.id] = {
                "session_title": sess.title,
                "session_origin": getattr(sess, "origin", None) or "interactive",
            }
    out: list[SystemAgentActionOut] = []
    for row in rows:
        payload = action_to_dict(row)
        meta = session_meta.get(str(row.session_id or ""), {})
        payload.update(meta)
        out.append(SystemAgentActionOut(**payload))
    return out


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
    if spec is not None and not getattr(spec, "allow_secret_input", True):
        raise _err(
            "SECRET_INPUT_LOCKED",
            "该操作已绑定测活时使用的临时密钥；如需更换，请拒绝后重新发起测活",
            409,
        )

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
