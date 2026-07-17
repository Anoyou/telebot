"""管理 Bot `/agent` 与助手模式桥接。"""

from __future__ import annotations

import logging
from typing import Any

from ...db.base import AsyncSessionLocal
from ...db.models.system_agent import CHANNEL_BOT, SESSION_STATUS_ACTIVE
from ...redis_client import get_redis
from .service import get_system_agent_service

log = logging.getLogger(__name__)

AGENT_MODE_TTL_SECONDS = 30 * 60
AGENT_MODE_KEY = "system_agent:bot_mode:{account_id}:{tg_user_id}"


def _mode_key(account_id: int, tg_user_id: int) -> str:
    return AGENT_MODE_KEY.format(account_id=int(account_id), tg_user_id=int(tg_user_id))


async def is_agent_mode(account_id: int, tg_user_id: int) -> bool:
    try:
        redis = get_redis()
        return bool(await redis.get(_mode_key(account_id, tg_user_id)))
    except Exception:  # noqa: BLE001
        return False


async def enter_agent_mode(account_id: int, tg_user_id: int) -> bool:
    try:
        redis = get_redis()
        await redis.set(_mode_key(account_id, tg_user_id), "1", ex=AGENT_MODE_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001
        log.warning("enter agent mode failed account=%s user=%s", account_id, tg_user_id, exc_info=True)
        return False


async def refresh_agent_mode(account_id: int, tg_user_id: int) -> None:
    try:
        redis = get_redis()
        key = _mode_key(account_id, tg_user_id)
        if await redis.get(key):
            await redis.expire(key, AGENT_MODE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        pass


async def exit_agent_mode(account_id: int, tg_user_id: int) -> None:
    try:
        redis = get_redis()
        await redis.delete(_mode_key(account_id, tg_user_id))
    except Exception:  # noqa: BLE001
        pass


async def handle_agent_command(
    *,
    account_id: int,
    tg_user_id: int,
    role: str,
    text: str,
    send: Any,
    edit: bool = False,
) -> None:
    """处理 `/agent` 及其子命令与自然语言任务。"""

    raw = (text or "").strip()
    # 去掉命令本体
    parts = raw.split(maxsplit=1)
    head = (parts[0] if parts else "").lower()
    # 支持 /agent@botname
    if head.startswith("/agent"):
        tail = parts[1].strip() if len(parts) > 1 else ""
    else:
        tail = raw

    if not tail or tail.lower() in {"help", "status", "?"}:
        active = await is_agent_mode(account_id, tg_user_id)
        ok = await enter_agent_mode(account_id, tg_user_id)
        msg = (
            "🤖 <b>系统助手</b>\n"
            f"账号：<code>{account_id}</code>\n"
            f"助手模式：{'已开启' if ok or active else 'Redis 不可用，仅 /agent 单次任务可用'}\n\n"
            "用法：\n"
            "/agent &lt;问题&gt; — 直接提问\n"
            "/agent new — 新建会话\n"
            "/agent clear — 删除当前会话\n"
            "/agent exit — 退出助手模式\n\n"
            "助手模式下可直接发送自然语言（既有斜杠命令仍优先）。\n"
            "当前阶段为只读查询；写操作将在后续版本开放。"
        )
        await send(msg, edit=edit)
        return

    sub = tail.split(maxsplit=1)
    verb = sub[0].lower()

    if verb == "exit":
        await exit_agent_mode(account_id, tg_user_id)
        await send("已退出系统助手模式。历史会话仍保留。", edit=edit)
        return

    if verb == "new":
        await enter_agent_mode(account_id, tg_user_id)
        async with AsyncSessionLocal() as db:
            svc = get_system_agent_service()
            # 归档旧会话
            sessions = await svc.list_sessions(
                db,
                bot_tg_user_id=tg_user_id,
                account_id=account_id,
                status=SESSION_STATUS_ACTIVE,
                limit=20,
            )
            for s in sessions:
                await svc.update_session(db, s, status="archived")
            session = await svc.create_session(
                db,
                channel=CHANNEL_BOT,
                bot_tg_user_id=tg_user_id,
                account_id=account_id,
                title="新对话",
            )
            await db.commit()
        await send(f"已新建助手会话 <code>{session.id[:8]}…</code>，直接发问题即可。", edit=edit)
        return

    if verb == "clear":
        await enter_agent_mode(account_id, tg_user_id)
        async with AsyncSessionLocal() as db:
            svc = get_system_agent_service()
            sessions = await svc.list_sessions(
                db,
                bot_tg_user_id=tg_user_id,
                account_id=account_id,
                status=SESSION_STATUS_ACTIVE,
                limit=1,
            )
            if not sessions:
                await send("当前没有活跃助手会话。", edit=edit)
                return
            await svc.delete_session(db, sessions[0])
            await db.commit()
        await send("已删除当前助手会话。", edit=edit)
        return

    # 自然语言任务
    await enter_agent_mode(account_id, tg_user_id)
    question = tail
    await run_agent_query(
        account_id=account_id,
        tg_user_id=tg_user_id,
        role=role,
        text=question,
        send=send,
        edit=edit,
    )


async def run_agent_query(
    *,
    account_id: int,
    tg_user_id: int,
    role: str,
    text: str,
    send: Any,
    edit: bool = False,
) -> None:
    """执行一轮助手查询，尽量编辑原消息减少噪音。"""

    await refresh_agent_mode(account_id, tg_user_id)
    await send("⏳ 系统助手处理中…", edit=edit)

    assistant_text = ""
    error_text = ""
    async with AsyncSessionLocal() as db:
        svc = get_system_agent_service()
        session = await svc.get_or_create_active_session(
            db,
            channel=CHANNEL_BOT,
            bot_tg_user_id=tg_user_id,
            account_id=account_id,
        )
        await db.commit()
        # 重新取会话避免 expire
        session = await svc.get_session(db, session.id, bot_tg_user_id=tg_user_id)
        assert session is not None
        try:
            async for event in svc.stream_message(
                db,
                session=session,
                text=text,
                role=role or "viewer",
                channel=CHANNEL_BOT,
                bot_tg_user_id=tg_user_id,
            ):
                et = event.get("type")
                if et == "assistant_message":
                    assistant_text = str(event.get("content") or "")
                elif et == "error":
                    error_text = str(event.get("message") or "助手运行失败")
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            log.exception("bot agent query failed")
            error_text = str(exc)[:400]

    if error_text and not assistant_text:
        await send(f"❌ {error_text}", edit=True)
        return
    body = assistant_text or "（无文本回复）"
    # Telegram 消息长度限制
    if len(body) > 3500:
        body = body[:3400] + "\n\n…（已截断，完整内容请到 Web /assistant 查看）"
    # 简单 HTML 转义尖括号外的内容：_send 使用 HTML parse_mode
    safe = (
        body.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    await send(safe, edit=True)


__all__ = [
    "enter_agent_mode",
    "exit_agent_mode",
    "handle_agent_command",
    "is_agent_mode",
    "refresh_agent_mode",
    "run_agent_query",
]
