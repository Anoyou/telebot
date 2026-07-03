"""账号绑定普通 Bot 联动 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..db.models.account_bot import ACCOUNT_BOT_STATUS_DISABLED, AccountBot
from ..deps import CurrentUser, DBSession
from ..schemas.account_bot import (
    AccountBotConfigResponse,
    AccountBotConfigUpdate,
    AccountBotInteractionConfig,
    AccountBotRemotePluginPolicy,
    AccountBotRuntimeResponse,
    AccountBotTestRequest,
    AccountBotTestResponse,
    AccountBotTransferNoticeConfig,
    AccountBotUserCreate,
    AccountBotUserResponse,
    AccountBotUserUpdate,
)
from ..services import (
    account_bot_runtime,
    account_bot_service,
    audit,
    feature_service,
    interaction_bot_runtime,
    interaction_bot_service,
)

router = APIRouter(prefix="/api/accounts", tags=["account-bots"])


def _with_interaction_runtime_state(aid: int, data: dict) -> dict:
    running = interaction_bot_runtime.is_interaction_bot_running(aid)
    if not data.get("enabled") or not data.get("has_interaction_bot_token"):
        running = False
        data = {**data, "interaction_last_error": None}
    return {
        **data,
        "interaction_running": running,
        "interaction_runtime_status": "running" if running else "stopped",
    }


@router.get("/{aid}/bot", response_model=AccountBotConfigResponse)
async def get_account_bot(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
) -> AccountBotConfigResponse:
    """读取该账号 Bot 配置；不返回 token 明文。"""

    await account_bot_service.ensure_account(db, aid)
    row = (
        await db.execute(select(AccountBot).where(AccountBot.account_id == aid))
    ).scalar_one_or_none()
    if row is None:
        return AccountBotConfigResponse(
            account_id=aid,
            enabled=False,
            status=ACCOUNT_BOT_STATUS_DISABLED,
            has_token=False,
            remote_plugin_policy=AccountBotRemotePluginPolicy(),
        )
    return account_bot_service.config_to_response(row)


@router.put("/{aid}/bot", response_model=AccountBotConfigResponse)
async def update_account_bot(
    aid: int,
    payload: AccountBotConfigUpdate,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotConfigResponse:
    """保存该账号 Bot token/启停配置，并同步 polling runtime。"""

    row = await account_bot_service.update_bot_config(db, aid, payload)
    await audit.write(
        db,
        user.id,
        "account_bot.update",
        target=f"account:{aid}/bot",
        detail={
            "enabled": payload.enabled,
            "token_changed": bool(payload.bot_token or payload.clear_token),
            "remote_plugin_policy_changed": payload.remote_plugin_policy is not None,
        },
    )
    await db.commit()
    await db.refresh(row)
    await account_bot_runtime.sync_account_bot(aid)
    return account_bot_service.config_to_response(row)


@router.post("/{aid}/bot/test", response_model=AccountBotTestResponse)
async def test_account_bot(
    aid: int,
    payload: AccountBotTestRequest,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotTestResponse:
    """发送测试消息。

    - 默认发给已授权且有 last_chat_id 的通知用户
    - 传 ``chat_id`` 时改为直发指定会话
    - 传 ``bot_token_override`` 时改用一次性临时 token（不落库）
    """

    token: str
    if payload.bot_token_override:
        token = payload.bot_token_override
    else:
        row = await account_bot_service.get_bot_config(db, aid, create=False)
        token = account_bot_service.decrypt_bot_token(row)

    text = payload.text or "✅ TelePilot 账号 Bot 测试消息发送成功。"
    sent = 0
    last_error = None

    if payload.chat_id is not None:
        try:
            await account_bot_service.send_message(token, int(payload.chat_id), text)
            sent = 1
        except Exception as exc:  # noqa: BLE001
            last_error = account_bot_service.sanitize_bot_error(exc, token=token)
    else:
        users = await account_bot_service.list_bot_users(db, aid)
        targets = [
            u for u in users
            if u.enabled and u.notify_enabled and u.last_chat_id is not None
        ]
        if not targets:
            raise HTTPException(
                400,
                detail={
                    "code": "ACCOUNT_BOT_NO_TARGET",
                    "message": "没有可发送的授权用户。请先让授权用户给这个 Bot 发送 /start。",
                },
            )
        for target in targets:
            try:
                await account_bot_service.send_message(token, int(target.last_chat_id), text)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                last_error = account_bot_service.sanitize_bot_error(exc, token=token)

    await audit.write(
        db,
        user.id,
        "account_bot.test",
        target=f"account:{aid}/bot",
        detail={
            "sent": sent,
            "chat_id": payload.chat_id,
            "token_override": bool(payload.bot_token_override),
        },
    )
    await db.commit()
    if sent <= 0:
        raise HTTPException(
            502,
            detail={
                "code": "ACCOUNT_BOT_TEST_FAILED",
                "message": last_error or "测试发送失败",
            },
        )
    return AccountBotTestResponse(
        ok=True,
        sent=sent,
        message=(
            "测试消息已发送到指定会话，Bbot 将通过 polling 自然接收并触发联动。"
            if payload.chat_id is not None
            else "测试消息已发送，Bbot 将通过 polling 自然接收并触发联动。"
        ),
    )


@router.get("/{aid}/interaction-bot", response_model=AccountBotInteractionConfig)
@router.get("/{aid}/bot/interaction", response_model=AccountBotInteractionConfig)
async def get_account_bot_interaction(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
) -> AccountBotInteractionConfig:
    """读取交互 Bot / 转账联动测试配置。"""

    data = await interaction_bot_service.get_interaction_bot_config(db, aid)
    data = _with_interaction_runtime_state(aid, data)
    data["interaction_debug"] = await account_bot_runtime.get_interaction_debug_snapshot(aid)
    return AccountBotInteractionConfig(**data)


@router.put("/{aid}/interaction-bot", response_model=AccountBotInteractionConfig)
@router.put("/{aid}/bot/interaction", response_model=AccountBotInteractionConfig)
async def update_account_bot_interaction(
    aid: int,
    payload: AccountBotInteractionConfig,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotInteractionConfig:
    """保存交互 Bot / 转账联动测试配置。"""

    data = await interaction_bot_service.update_interaction_bot_config(
        db,
        aid,
        payload.model_dump(),
    )
    await audit.write(
        db,
        user.id,
        "account_bot.transfer_notice_update",
        target=f"account:{aid}/bot/transfer_notice",
        detail={
            "enabled": data.get("enabled"),
            "chat_id": data.get("chat_id"),
            "interaction_bot_id": data.get("interaction_bot_id"),
            "interaction_bot_username": data.get("interaction_bot_username"),
            "trusted_bot_id": data.get("trusted_bot_id"),
            "amount": data.get("amount"),
        },
    )
    await db.commit()
    await feature_service._notify_reload(aid)  # noqa: SLF001 - 交互规则可能同步启用插件，需要 worker 即时加载。
    await interaction_bot_runtime.restart_interaction_bot(aid)
    data = _with_interaction_runtime_state(aid, data)
    return AccountBotInteractionConfig(**data)


@router.get("/{aid}/bot/transfer-notice", response_model=AccountBotTransferNoticeConfig)
async def get_account_bot_transfer_notice(
    aid: int,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotTransferNoticeConfig:
    """兼容旧前端入口；新代码请使用 ``/interaction-bot``。"""

    return await get_account_bot_interaction(aid, db, user)


@router.put("/{aid}/bot/transfer-notice", response_model=AccountBotTransferNoticeConfig)
async def update_account_bot_transfer_notice(
    aid: int,
    payload: AccountBotTransferNoticeConfig,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotTransferNoticeConfig:
    """兼容旧前端入口；新代码请使用 ``/interaction-bot``。"""

    return await update_account_bot_interaction(aid, payload, db, user)


@router.post("/{aid}/bot/restart-runtime", response_model=AccountBotRuntimeResponse)
async def restart_account_bot_runtime(
    aid: int,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotRuntimeResponse:
    """重启该账号 Bot polling task。"""

    await account_bot_service.ensure_account(db, aid)
    await audit.write(
        db,
        user.id,
        "account_bot.restart_runtime",
        target=f"account:{aid}/bot",
    )
    await db.commit()
    await account_bot_runtime.restart_account_bot(aid)
    return AccountBotRuntimeResponse(ok=True, message="已重启 Bot polling runtime")


@router.get("/{aid}/bot/users", response_model=list[AccountBotUserResponse])
async def list_account_bot_users(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
) -> list[AccountBotUserResponse]:
    rows = await account_bot_service.list_bot_users(db, aid)
    return [AccountBotUserResponse.model_validate(r) for r in rows]


@router.post("/{aid}/bot/users", response_model=AccountBotUserResponse, status_code=201)
async def create_account_bot_user(
    aid: int,
    payload: AccountBotUserCreate,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotUserResponse:
    row = await account_bot_service.create_bot_user(db, aid, payload)
    await audit.write(
        db,
        user.id,
        "account_bot_user.create",
        target=f"account:{aid}/bot_user:{row.tg_user_id}",
        detail={"role": row.role, "notify_enabled": row.notify_enabled},
    )
    await db.commit()
    await db.refresh(row)
    return AccountBotUserResponse.model_validate(row)


@router.patch("/{aid}/bot/users/{uid}", response_model=AccountBotUserResponse)
async def update_account_bot_user(
    aid: int,
    uid: int,
    payload: AccountBotUserUpdate,
    db: DBSession,
    user: CurrentUser,
) -> AccountBotUserResponse:
    row = await account_bot_service.update_bot_user(db, aid, uid, payload)
    await audit.write(
        db,
        user.id,
        "account_bot_user.update",
        target=f"account:{aid}/bot_user:{row.tg_user_id}",
        detail=payload.model_dump(exclude_unset=True),
    )
    await db.commit()
    await db.refresh(row)
    return AccountBotUserResponse.model_validate(row)


@router.delete("/{aid}/bot/users/{uid}", response_model=dict)
async def delete_account_bot_user(
    aid: int,
    uid: int,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, bool]:
    row = await account_bot_service.get_bot_user(db, aid, uid)
    tg_user_id = row.tg_user_id
    await account_bot_service.delete_bot_user(db, aid, uid)
    await audit.write(
        db,
        user.id,
        "account_bot_user.delete",
        target=f"account:{aid}/bot_user:{tg_user_id}",
    )
    await db.commit()
    return {"ok": True}
