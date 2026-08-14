"""账号 API：CRUD + 暂停 / 恢复 + 复制配置 + Telethon 登录绑定向导。

绑定向导按 plan 设计：``/login/start``、``/login/code``、``/login/2fa`` 都不带 aid，
因为新建账号在 finalize 之前还没有 aid；老账号重登可以在 ``start`` 入参里带 ``account_id``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..deps import CurrentUser, DBSession
from ..schemas.account import (
    AccountCloneConfigRequest,
    AccountConfirm2FARequest,
    AccountConfirmCodeRequest,
    AccountConfirmResponse,
    AccountDetail,
    AccountStartLoginRequest,
    AccountStartLoginResponse,
    AccountSummary,
    AccountUpdateRequest,
)
from ..services import account_service, audit, login_service, platform_capabilities

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ── 列表 / 详情 / 修改 / 删除 ─────────────────────────────────────
@router.get("", response_model=list[AccountSummary])
async def list_accounts(db: DBSession, user: CurrentUser) -> list[AccountSummary]:
    """列出全部账号。"""
    return await account_service.list_accounts(db)


@router.get("/{aid}", response_model=AccountDetail)
async def get_account(aid: int, db: DBSession, user: CurrentUser) -> AccountDetail:
    """读取账号详情。"""
    return await account_service.get_account(db, aid)


@router.patch("/{aid}", response_model=AccountDetail)
async def update_account(
    aid: int,
    payload: AccountUpdateRequest,
    db: DBSession,
    user: CurrentUser,
) -> AccountDetail:
    """修改账号字段。"""
    detail = await account_service.update_account(db, aid, payload)
    await audit.write(
        db,
        user.id,
        "account.update",
        target=f"account:{aid}",
        detail=payload.model_dump(exclude_unset=True),
    )
    await db.commit()
    return detail


@router.delete("/{aid}")
async def delete_account(aid: int, db: DBSession, user: CurrentUser) -> dict[str, bool]:
    """删除账号（撤销 session + 清理本地数据）。"""
    await account_service.delete_account(db, aid)
    await audit.write(db, user.id, "account.delete", target=f"account:{aid}")
    await db.commit()
    return {"ok": True}


# ── 头像 ───────────────────────────────────────────────────────────
@router.get("/{aid}/avatar")
async def get_avatar(aid: int, db: DBSession, user: CurrentUser):
    """返回账号头像 PNG/JPEG，本地缓存 24h。

    - 文件不存在（worker 离线 / 账号无头像 / 首次访问）→ 404，前端走首字母 fallback。
    - 文件存在 → 用浏览器私有缓存 1h；超过 24h 后台会触发 worker 重拉。
    """
    path = await account_service.ensure_avatar(db, aid)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail={"code": "no_avatar", "message": "暂无头像"})
    return FileResponse(
        str(path),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ── 绑定向导 ──────────────────────────────────────────────────────
@router.post("/login/start", response_model=AccountStartLoginResponse)
async def login_start(
    req: AccountStartLoginRequest,
    db: DBSession,
    user: CurrentUser,
) -> AccountStartLoginResponse:
    """绑定向导第 1 步：建立 Telethon client，发送验证码，返回 login_token。"""
    token = await login_service.start_login(
        db,
        api_id=req.api_id,
        api_hash=req.api_hash,
        phone=req.phone,
        account_id=req.account_id,
        proxy_id=req.proxy_id,
        device_profile_id=req.device_profile_id,
    )
    await audit.write(
        db,
        user.id,
        "account.login.start",
        target=f"account:{req.account_id}" if req.account_id else f"phone:{req.phone}",
        detail={
            "account_id": req.account_id,
            "phone": req.phone,
            "proxy_id": req.proxy_id,
            "device_profile_id": req.device_profile_id,
        },
    )
    await db.commit()
    # phone_code_hash 不必返给前端（state 已在主进程内存里）
    return AccountStartLoginResponse(login_token=token, phone_code_hash=None)


@router.post("/login/code", response_model=AccountConfirmResponse)
async def login_code(
    req: AccountConfirmCodeRequest,
    db: DBSession,
    user: CurrentUser,
) -> AccountConfirmResponse:
    """绑定向导第 2 步：提交短信/Telegram 验证码。

    若账号未启用 2FA，本步同时完成 finalize；否则等待第 3 步。
    """
    require_2fa, pending = await login_service.confirm_code(req.login_token, req.code)
    if require_2fa:
        # 卡在两步验证；account_id 此时还没产生
        return AccountConfirmResponse(account_id=0, require_2fa=True, display_name=None)

    aid = await login_service.finalize(db, req.login_token, pending)
    await audit.write(db, user.id, "account.login.finalize", target=f"account:{aid}")
    await db.commit()

    detail = await account_service.get_account(db, aid)
    return AccountConfirmResponse(
        account_id=aid, require_2fa=False, display_name=detail.display_name
    )


@router.post("/login/2fa", response_model=AccountConfirmResponse)
async def login_2fa(
    req: AccountConfirm2FARequest,
    db: DBSession,
    user: CurrentUser,
) -> AccountConfirmResponse:
    """绑定向导第 3 步：提交两步验证密码，并完成 finalize。"""
    pending = await login_service.confirm_2fa(req.login_token, req.password)
    aid = await login_service.finalize(db, req.login_token, pending)
    await audit.write(db, user.id, "account.login.finalize2fa", target=f"account:{aid}")
    await db.commit()

    detail = await account_service.get_account(db, aid)
    return AccountConfirmResponse(
        account_id=aid, require_2fa=False, display_name=detail.display_name
    )


# ── 暂停 / 恢复 ───────────────────────────────────────────────────
@router.post("/{aid}/pause")
async def pause_account(aid: int, db: DBSession, user: CurrentUser) -> dict[str, bool]:
    """暂停账号。"""
    await account_service.pause(db, aid)
    await audit.write(db, user.id, "account.pause", target=f"account:{aid}")
    await db.commit()
    return {"ok": True}


@router.post("/{aid}/resume")
async def resume_account(aid: int, db: DBSession, user: CurrentUser) -> dict[str, bool]:
    """恢复账号。"""
    await account_service.resume(db, aid)
    await audit.write(db, user.id, "account.resume", target=f"account:{aid}")
    await db.commit()
    return {"ok": True}


# ── 复制配置 ──────────────────────────────────────────────────────
@router.post("/{aid}/clone-config")
async def clone_config(
    aid: int,
    req: AccountCloneConfigRequest,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, int | bool]:
    """从 ``req.from_account_id`` 复制 features+rules 到 aid。"""
    try:
        stats = await account_service.clone_config(
            db,
            src_aid=req.from_account_id,
            dst_aid=aid,
            features=req.features or None,
            web_user_id=int(user.id),
        )
    except platform_capabilities.PluginCapabilityBlocked as exc:
        raise HTTPException(
            409,
            detail={"code": exc.error_code, "message": str(exc)},
        ) from exc
    await audit.write(
        db,
        user.id,
        "account.clone_config",
        target=f"account:{aid}",
        detail={"from": req.from_account_id, "features": req.features, **stats},
    )
    await db.commit()
    return {"ok": True, **stats}
