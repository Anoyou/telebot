"""项目通知发送服务（Sprint4 #2D）。"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from ..crypto import decrypt_str
from ..db.base import AsyncSessionLocal
from ..db.models.account_bot import AccountBot
from ..db.models.notify import NotifyBot

log = logging.getLogger(__name__)


async def _select_bot(channel_name: str | None) -> NotifyBot | None:
    async with AsyncSessionLocal() as db:
        if channel_name:
            row = (
                await db.execute(
                    select(NotifyBot).where(
                        NotifyBot.enabled.is_(True),
                        NotifyBot.name == channel_name,
                    )
                )
            ).scalar_one_or_none()
            if row is not None or channel_name != "alert":
                return row

        default_row = (
            await db.execute(
                select(NotifyBot).where(
                    NotifyBot.enabled.is_(True),
                    NotifyBot.name == "default",
                )
            )
        ).scalar_one_or_none()
        if default_row is not None:
            return default_row

        first_enabled = (
            await db.execute(
                select(NotifyBot)
                .where(NotifyBot.enabled.is_(True))
                .order_by(NotifyBot.id.asc())
            )
        ).scalars().first()
        return first_enabled


async def _resolve_token(bot: NotifyBot) -> str | None:
    encrypted = bot.bot_token_enc
    source_account_id = getattr(bot, "source_account_id", None)
    if source_account_id is not None:
        async with AsyncSessionLocal() as db:
            account_bot = (
                await db.execute(
                    select(AccountBot).where(AccountBot.account_id == int(source_account_id))
                )
            ).scalar_one_or_none()
        encrypted = account_bot.bot_token_enc if account_bot is not None else None
    if not encrypted:
        return None
    try:
        return decrypt_str(encrypted)
    except Exception:
        log.exception(
            "notify bot token 解密失败: name=%s source_account_id=%s",
            bot.name,
            source_account_id,
        )
        return None


async def send(channel_name: str | None, text: str, *, parse_mode: str = "HTML") -> bool:
    """发到指定 NotifyBot；channel_name=None 时优先 default。"""
    bot = await _select_bot(channel_name)
    if bot is None:
        return False
    token = await _resolve_token(bot)
    if token is None:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": bot.default_chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            resp = await cli.post(url, json=payload)
        if not resp.is_success:
            log.warning(
                "notify send 失败: name=%s status=%s body=%s",
                bot.name,
                resp.status_code,
                resp.text[:300],
            )
        return resp.is_success
    except Exception:
        log.exception("notify send 异常: name=%s", bot.name)
        return False
