"""NotifyBot CRUD + 测试发送（Sprint4 #2D）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select

from .. import __version__
from ..crypto import encrypt_str
from ..db.models.account_bot import AccountBot
from ..db.models.notify import NotifyBot
from ..deps import CurrentUser, DBSession
from ..schemas.notify import (
    NotifyBotCreate,
    NotifyBotOut,
    NotifyBotTestRequest,
    NotifyBotTestResponse,
    NotifyBotUpdate,
)
from ..services import audit, notify_service

router = APIRouter(prefix="/api/notify-bots", tags=["notify-bots"])



async def _source_account_bot(db: DBSession, account_id: int | None) -> AccountBot | None:
    if account_id is None:
        return None
    return (
        await db.execute(select(AccountBot).where(AccountBot.account_id == int(account_id)))
    ).scalar_one_or_none()


async def _to_out(db: DBSession, row: NotifyBot) -> NotifyBotOut:
    source = await _source_account_bot(db, row.source_account_id)
    return NotifyBotOut(
        id=row.id,
        name=row.name,
        default_chat_id=row.default_chat_id,
        enabled=row.enabled,
        has_token=bool(source.bot_token_enc) if source is not None else bool(row.bot_token_enc),
        credential_source="account_bot" if row.source_account_id is not None else "direct",
        source_account_id=row.source_account_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[NotifyBotOut])
async def list_notify_bots(db: DBSession, _user: CurrentUser) -> list[NotifyBotOut]:
    rows = (
        await db.execute(select(NotifyBot).order_by(NotifyBot.id.asc()))
    ).scalars().all()
    return [await _to_out(db, r) for r in rows]


@router.post("", response_model=NotifyBotOut, status_code=status.HTTP_201_CREATED)
async def create_notify_bot(
    payload: NotifyBotCreate,
    db: DBSession,
    user: CurrentUser,
) -> NotifyBotOut:
    exists = (
        await db.execute(select(NotifyBot.id).where(NotifyBot.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONFLICT", "message": "name 已存在"},
        )

    source = await _source_account_bot(db, payload.source_account_id)
    if payload.source_account_id is not None and (source is None or not source.bot_token_enc):
        raise HTTPException(
            status_code=422,
            detail={"code": "ACCOUNT_BOT_TOKEN_REQUIRED", "message": "所选账号尚未配置管理 Bot Token"},
        )

    row = NotifyBot(
        name=payload.name,
        bot_token_enc=encrypt_str(payload.bot_token) if payload.bot_token else None,
        source_account_id=payload.source_account_id,
        default_chat_id=payload.default_chat_id,
        enabled=payload.enabled,
    )
    db.add(row)
    await db.flush()

    await audit.write(
        db,
        user.id,
        "notify_bot.create",
        target=f"notify_bot:{row.id}",
        detail={
            "name": row.name,
            "enabled": row.enabled,
            "credential_source": "account_bot" if row.source_account_id is not None else "direct",
            "source_account_id": row.source_account_id,
        },
    )
    await db.commit()
    await db.refresh(row)
    return await _to_out(db, row)


@router.patch("/{bot_id}", response_model=NotifyBotOut)
async def update_notify_bot(
    bot_id: int,
    payload: NotifyBotUpdate,
    db: DBSession,
    user: CurrentUser,
) -> NotifyBotOut:
    row = (
        await db.execute(select(NotifyBot).where(NotifyBot.id == bot_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "NotifyBot 不存在"})

    if payload.name is not None and payload.name != row.name:
        dup = (
            await db.execute(
                select(NotifyBot.id).where(NotifyBot.name == payload.name, NotifyBot.id != bot_id)
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "CONFLICT", "message": "name 已存在"},
            )
        row.name = payload.name

    if payload.default_chat_id is not None:
        row.default_chat_id = payload.default_chat_id
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.clear_token:
        row.bot_token_enc = None
        row.source_account_id = None
    if payload.bot_token is not None and "source_account_id" in payload.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail={"code": "CREDENTIAL_SOURCE_CONFLICT", "message": "不能同时填写独立 Token 和引用管理 Bot"},
        )
    if "source_account_id" in payload.model_fields_set:
        source = await _source_account_bot(db, payload.source_account_id)
        if payload.source_account_id is not None and (source is None or not source.bot_token_enc):
            raise HTTPException(
                status_code=422,
                detail={"code": "ACCOUNT_BOT_TOKEN_REQUIRED", "message": "所选账号尚未配置管理 Bot Token"},
            )
        row.source_account_id = payload.source_account_id
        if payload.source_account_id is not None:
            row.bot_token_enc = None
    if payload.bot_token is not None:
        row.bot_token_enc = encrypt_str(payload.bot_token)
        row.source_account_id = None

    await audit.write(
        db,
        user.id,
        "notify_bot.update",
        target=f"notify_bot:{row.id}",
        detail={
            **payload.model_dump(exclude_unset=True, exclude={"bot_token"}),
            **({"bot_token_changed": True} if "bot_token" in payload.model_dump(exclude_unset=True) else {}),
        },
    )
    await db.commit()
    await db.refresh(row)
    return await _to_out(db, row)


@router.delete("/{bot_id}")
async def delete_notify_bot(bot_id: int, db: DBSession, user: CurrentUser) -> dict[str, bool]:
    row = (
        await db.execute(select(NotifyBot).where(NotifyBot.id == bot_id))
    ).scalar_one_or_none()
    if row is None:
        return {"ok": True}

    await db.execute(delete(NotifyBot).where(NotifyBot.id == bot_id))
    await audit.write(
        db,
        user.id,
        "notify_bot.delete",
        target=f"notify_bot:{bot_id}",
        detail={"name": row.name},
    )
    await db.commit()
    return {"ok": True}


@router.post("/{bot_id}/test", response_model=NotifyBotTestResponse)
async def test_notify_bot(
    bot_id: int,
    payload: NotifyBotTestRequest,
    db: DBSession,
    _user: CurrentUser,
) -> NotifyBotTestResponse:
    row = (
        await db.execute(select(NotifyBot).where(NotifyBot.id == bot_id, NotifyBot.enabled.is_(True)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "NotifyBot 不存在或未启用"})

    text = payload.text or f"test from telepilot v{__version__}"
    ok = await notify_service.send(row.name, text)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": "SEND_FAILED", "message": "发送失败，请检查 bot_token 与 chat_id"})
    return NotifyBotTestResponse(ok=True)
