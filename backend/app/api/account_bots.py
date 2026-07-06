"""账号绑定普通 Bot 联动 API。"""

from __future__ import annotations

from typing import Any

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

_KEYWORD_RULE_REQUIRES_INTERACTION_BOT_MESSAGE = (
    "关键词触发依赖交互 Bot，请先配置交互 Bot Token 或改用命令触发"
)


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


async def _with_polling_dlq_count(aid: int, data: dict) -> dict:
    try:
        count = await account_bot_runtime._count_polling_dead_letters(aid)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        count = 0
    return {
        **data,
        "polling_dlq_count": count,
    }


def _enabled_keyword_rules_require_interaction_bot(data: dict[str, Any]) -> bool:
    if not data.get("enabled"):
        return False
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict) or not bool(rule.get("enabled", True)):
            continue
        trigger_mode = str(rule.get("trigger_mode") or "payment").strip()
        if trigger_mode in {"keyword", "both"}:
            return True
        if rule.get("module_start_keywords"):
            return True
    return False


async def _ensure_keyword_rules_have_interaction_bot_token(
    db: DBSession,
    aid: int,
    payload_data: dict[str, Any],
) -> None:
    current = await interaction_bot_service.get_interaction_bot_config(db, aid)
    incoming = dict(payload_data or {})
    candidate = interaction_bot_service.normalize_transfer_notice_config({**current, **incoming})
    current_has_token = bool(current.get("has_interaction_bot_token"))
    incoming_token = str(incoming.get("interaction_bot_token") or "").strip()
    has_token_after_save = bool(incoming_token) or (
        current_has_token and not bool(incoming.get("clear_interaction_bot_token"))
    )
    if _enabled_keyword_rules_require_interaction_bot(candidate) and not has_token_after_save:
        raise HTTPException(
            400,
            detail={
                "code": "INTERACTION_BOT_TOKEN_REQUIRED_FOR_KEYWORD_RULES",
                "message": _KEYWORD_RULE_REQUIRES_INTERACTION_BOT_MESSAGE,
            },
        )


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
    data = await _with_polling_dlq_count(aid, data)
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

    payload_data = payload.model_dump()
    await _ensure_keyword_rules_have_interaction_bot_token(db, aid, payload_data)
    data = await interaction_bot_service.update_interaction_bot_config(
        db,
        aid,
        payload_data,
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
            "trusted_bot_ids": data.get("trusted_bot_ids"),
            "amount": data.get("amount"),
        },
    )
    await db.commit()
    await feature_service._notify_reload(aid)  # noqa: SLF001 - 交互规则可能同步启用插件，需要 worker 即时加载。
    await interaction_bot_runtime.restart_interaction_bot(aid)
    data = _with_interaction_runtime_state(aid, data)
    data = await _with_polling_dlq_count(aid, data)
    return AccountBotInteractionConfig(**data)


def _polling_dlq_id_or_400(loop: str, update_id: int) -> str:
    try:
        return account_bot_runtime._polling_dlq_id(loop, update_id)  # noqa: SLF001
    except ValueError as exc:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_POLLING_DLQ_LOOP",
                "message": "未知 polling DLQ 类型",
            },
        ) from exc


@router.get("/{aid}/bot/polling-dlq", response_model=dict)
async def list_account_bot_polling_dlq(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
    limit: int = 100,
) -> dict[str, Any]:
    await account_bot_service.ensure_account(db, aid)
    items = await account_bot_runtime._list_polling_dead_letters(aid, limit=limit)  # noqa: SLF001
    return {
        "ok": True,
        "count": await account_bot_runtime._count_polling_dead_letters(aid),  # noqa: SLF001
        "items": items,
    }


@router.post("/{aid}/bot/polling-dlq/{loop}/{update_id}/replay", response_model=dict)
async def replay_account_bot_polling_dlq(
    aid: int,
    loop: str,
    update_id: int,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, Any]:
    await account_bot_service.ensure_account(db, aid)
    dlq_id = _polling_dlq_id_or_400(loop, update_id)
    result = await account_bot_runtime._replay_polling_dead_letter(aid, dlq_id)  # noqa: SLF001
    if not result.get("ok") and str(result.get("error") or "") == "DLQ 条目不存在":
        raise HTTPException(
            404,
            detail={
                "code": "POLLING_DLQ_NOT_FOUND",
                "message": "DLQ 条目不存在",
            },
        )
    await audit.write(
        db,
        user.id,
        "account_bot.polling_dlq_replay",
        target=f"account:{aid}/bot/polling_dlq:{dlq_id}",
        detail={"ok": bool(result.get("ok")), "error": result.get("error")},
    )
    await db.commit()
    return {"dlq_id": dlq_id, **result}


@router.delete("/{aid}/bot/polling-dlq/{loop}/{update_id}", response_model=dict)
async def discard_account_bot_polling_dlq(
    aid: int,
    loop: str,
    update_id: int,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, Any]:
    await account_bot_service.ensure_account(db, aid)
    dlq_id = _polling_dlq_id_or_400(loop, update_id)
    deleted = await account_bot_runtime._discard_polling_dead_letter(aid, dlq_id)  # noqa: SLF001
    if not deleted:
        raise HTTPException(
            404,
            detail={
                "code": "POLLING_DLQ_NOT_FOUND",
                "message": "DLQ 条目不存在",
            },
        )
    await audit.write(
        db,
        user.id,
        "account_bot.polling_dlq_discard",
        target=f"account:{aid}/bot/polling_dlq:{dlq_id}",
    )
    await db.commit()
    return {"ok": True, "dlq_id": dlq_id, "deleted": True}


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
