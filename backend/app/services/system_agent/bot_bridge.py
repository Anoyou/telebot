"""管理 Bot `/agent` 与助手模式桥接（含写操作 Inline 确认）。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from typing import Any

from ...db.base import AsyncSessionLocal
from ...db.models.system_agent import (
    ACTION_STATUS_PENDING,
    CHANNEL_BOT,
    SESSION_STATUS_ACTIVE,
)
from ...redis_client import get_redis
from .actions import (
    bot_owns_action,
    decrypt_secret_payload,
    encrypt_secret_payload,
    list_actions,
    lock_action,
    mark_expired_if_needed,
    reject_action,
)
from .executor import get_action_executor
from .registry import get_registry
from .secrets import extract_plaintext_secrets
from .service import get_system_agent_service

log = logging.getLogger(__name__)

AGENT_MODE_TTL_SECONDS = 30 * 60
AGENT_MODE_KEY = "system_agent:bot_mode:{account_id}:{tg_user_id}"
AGENT_CONFIRM_PREFIX = "system_agent:bot_confirm:"
AGENT_CONFIRM_TTL_SECONDS = 10 * 60  # 与 Action 默认 TTL 对齐
_PURE_SECRET_RE = re.compile(r"^[A-Za-z0-9._+/=:-]{12,512}$")


def _mode_key(account_id: int, tg_user_id: int) -> str:
    return AGENT_MODE_KEY.format(account_id=int(account_id), tg_user_id=int(tg_user_id))


def _confirm_redis_key(nonce: str) -> str:
    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:32]
    return AGENT_CONFIRM_PREFIX + digest


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


def _html_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _agent_button(text: str, action: str, aid: int, nonce: str) -> dict[str, str]:
    """复用 account_bot 的 ab: 回调协议：ab:{aid}:{action}:agent:{nonce}。"""

    data = f"ab:{int(aid)}:{action}:agent:{nonce}"
    return {"text": text, "callback_data": data[:64]}


def _agent_confirm_keyboard(aid: int, nonce: str, *, dangerous: bool = False) -> dict[str, Any]:
    confirm_label = "⚠️ 确认执行" if dangerous else "✅ 确认执行"
    return {
        "inline_keyboard": [
            [
                _agent_button(confirm_label, "confirm", aid, nonce),
                _agent_button("❌ 取消", "cancel", aid, nonce),
            ]
        ]
    }


async def store_agent_confirm_nonce(
    *,
    account_id: int,
    tg_user_id: int,
    action_id: str,
) -> str | None:
    """写入 Redis 确认票据，返回 nonce；Redis 不可用时返回 None。"""

    nonce = secrets.token_urlsafe(8)
    payload = {
        "account_id": int(account_id),
        "tg_user_id": int(tg_user_id),
        "action": "agent",
        "action_id": action_id,
    }
    try:
        redis = get_redis()
        await redis.setex(
            _confirm_redis_key(nonce),
            AGENT_CONFIRM_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
        return nonce
    except Exception:  # noqa: BLE001
        log.warning("store agent confirm nonce failed", exc_info=True)
        return None


async def consume_agent_confirm_payload(nonce: str) -> dict[str, Any] | None:
    try:
        redis = get_redis()
        key = _confirm_redis_key(nonce)
        raw = await redis.get(key)
        if not raw:
            return None
        await redis.delete(key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        log.warning("consume agent confirm failed", exc_info=True)
        return None


async def read_agent_confirm_payload(nonce: str) -> dict[str, Any] | None:
    try:
        redis = get_redis()
        raw = await redis.get(_confirm_redis_key(nonce))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def handle_agent_confirm_callback(
    *,
    account_id: int,
    tg_user_id: int,
    role: str,
    nonce: str | None,
    decide: str,
    answer: Any,
    send: Any,
) -> None:
    """处理 Bot Inline 确认/取消（decide=confirm|cancel）。"""

    if not nonce:
        await answer("确认已过期", show_alert=True)
        return

    # 先读票据 → 校验身份 → 再消费（避免非本人点取消烧掉 nonce）
    payload = await read_agent_confirm_payload(nonce)
    if not payload:
        await answer("确认已过期", show_alert=True)
        return
    if payload.get("account_id") != account_id or payload.get("tg_user_id") != tg_user_id:
        await answer("只能由原用户操作", show_alert=True)
        return
    if payload.get("action") != "agent":
        await answer("确认资源不匹配", show_alert=True)
        return

    action_id = str(payload.get("action_id") or "")
    if not action_id:
        await answer("缺少操作 ID", show_alert=True)
        return

    if decide == "cancel":
        consumed = await consume_agent_confirm_payload(nonce)
        if not consumed:
            await answer("已取消或过期", show_alert=False)
            await send("已取消操作。", edit=True)
            return
        async with AsyncSessionLocal() as db:
            action = await lock_action(db, action_id)
            if action is not None and bot_owns_action(action, tg_user_id):
                await reject_action(db, action)
                await db.commit()
        await answer("已取消")
        await send("已取消该助手操作，业务未变更。", edit=True)
        return

    # confirm：先消费防双击，但 keep_pending 时重新发票据
    consumed = await consume_agent_confirm_payload(nonce)
    if not consumed:
        await answer("确认已过期", show_alert=True)
        return

    await answer("处理中…")
    await send("⏳ 正在执行助手操作…", edit=True)

    result = await get_action_executor().confirm(
        action_id=action_id,
        role=role or "viewer",
        channel=CHANNEL_BOT,
        bot_tg_user_id=tg_user_id,
    )
    action = result.get("action") if isinstance(result.get("action"), dict) else None
    if result.get("ok"):
        summary = (action or {}).get("summary") or action_id
        sync = (action or {}).get("runtime_sync_status") or ""
        extra = ""
        if sync == "failed":
            extra = "\n⚠️ 配置已保存，运行时同步失败；请稍后重新发起，或到 Web 查看运行日志。"
        already = "（已执行，未重复）" if result.get("already_final") else ""
        await send(
            f"✅ 已确认并执行{already}\n{_html_escape(str(summary))}{extra}",
            edit=True,
        )
        return

    # 验证失败等 keep_pending：重发 Inline 确认，允许重新贴 Key 后再确认
    if result.get("keep_pending"):
        err = result.get("error_message") or result.get("error_code") or "预检失败"
        danger = str((action or {}).get("risk") or "") == "dangerous"
        new_nonce = await store_agent_confirm_nonce(
            account_id=account_id,
            tg_user_id=tg_user_id,
            action_id=action_id,
        )
        summary = _html_escape(str((action or {}).get("summary") or action_id))
        body = (
            f"❌ {_html_escape(str(err)[:400])}\n"
            f"业务未变更，操作仍待确认：{summary}\n"
            "若需要密钥：请在本对话直接粘贴 API Key（单独发送即可），"
            "系统会写入该操作后再点确认；不要依赖掩码。"
        )
        markup = (
            _agent_confirm_keyboard(account_id, new_nonce, dangerous=danger) if new_nonce else None
        )
        if not new_nonce:
            body += "\n（Redis 不可用，请稍后重试，或到 Web /assistant 重新发起）"
        await send(body, edit=True, reply_markup=markup)
        return

    err = result.get("error_message") or result.get("error_code") or "执行失败"
    await send(f"❌ 执行失败：{_html_escape(str(err)[:400])}\n业务是否变化：否（失败路径）", edit=True)


def _text_is_mostly_secrets(text: str, secrets: list[str]) -> bool:
    """判断消息是否主要是密钥重发（去掉密钥后几乎无内容）。"""

    if not secrets:
        return False
    remainder = str(text or "")
    for secret in secrets:
        remainder = remainder.replace(secret, " ")
    # 去掉常见提示词
    for token in ("api_key", "api-key", "key", "token", "密钥", "是", ":", "=", "："):
        remainder = remainder.replace(token, " ")
        remainder = remainder.replace(token.upper(), " ")
    cleaned = " ".join(remainder.split())
    return len(cleaned) <= 8


async def try_attach_secrets_to_pending_action(
    *,
    account_id: int,
    tg_user_id: int,
    text: str,
    send: Any,
) -> bool:
    """若存在需要密钥的 pending Action，把聊天中的 Key 加密写回并重发确认键盘。

    返回 True 表示已处理完毕、调用方不应再跑完整 Agent 轮次。
    """

    raw_secret = str(text or "").strip()
    secrets = extract_plaintext_secrets(text)
    pure_secret_candidate = (
        not secrets
        and bool(_PURE_SECRET_RE.fullmatch(raw_secret))
        and not any(ch.isspace() for ch in raw_secret)
    )
    if not secrets and not pure_secret_candidate:
        return False

    async with AsyncSessionLocal() as db:
        rows = await list_actions(
            db,
            bot_tg_user_id=tg_user_id,
            status=ACTION_STATUS_PENDING,
            limit=20,
        )
        # 优先本账号上下文（Bot 会话绑定 account）
        candidates = [
            r for r in rows if r.account_id is None or int(r.account_id) == int(account_id)
        ]
        registry = get_registry()
        target = None
        secret_names: tuple[str, ...] = ()
        for row in candidates:
            row = await mark_expired_if_needed(db, row)
            if row.status != ACTION_STATUS_PENDING:
                continue
            spec = registry.get(row.tool_name)
            if spec is None or not spec.secret_argument_names:
                continue
            # 优先：已报缺 Key / 验证失败，或当前无密文
            if (
                not row.secret_payload_enc
                or (row.error_code or "").upper()
                in {
                    "API_KEY_REQUIRED",
                    "PROVIDER_VERIFY_FAILED",
                    "API_KEY_DECRYPT_FAILED",
                    "PRECHECK_FAILED",
                }
                or str(row.error_message or "").find("密钥") >= 0
                or str(row.error_message or "").find("Key") >= 0
                or str(row.error_message or "").find("api_key") >= 0
            ):
                target = row
                secret_names = tuple(spec.secret_argument_names)
                break
            if target is None:
                target = row
                secret_names = tuple(spec.secret_argument_names)
        if target is None or not secret_names:
            await db.commit()  # 可能有过期标记
            return False
        if not secrets and pure_secret_candidate:
            secrets = [raw_secret]

        locked = await lock_action(db, target.id)
        if locked is None or not bot_owns_action(locked, tg_user_id):
            await db.rollback()
            return False
        locked = await mark_expired_if_needed(db, locked)
        if locked.status != ACTION_STATUS_PENDING:
            await db.commit()
            return False
        target = locked

        secret_map = decrypt_secret_payload(target.secret_payload_enc)
        for i, name in enumerate(secret_names):
            if i < len(secrets):
                secret_map[name] = secrets[i]
        if not any(secret_map.get(n) for n in secret_names):
            await db.rollback()
            return False

        target.secret_payload_enc = encrypt_secret_payload(secret_map)
        target.secret_fields = [n for n in secret_names if secret_map.get(n)]
        args = dict(target.arguments or {})
        for name in secret_names:
            if secret_map.get(name):
                args.pop(name, None)
                args[f"has_{name}"] = True
        target.arguments = args
        target.error_code = None
        target.error_message = None
        await db.commit()
        action_id = target.id
        summary = target.summary
        danger = str(target.risk or "") == "dangerous"

    # 仅当消息几乎全是密钥时短路；否则仍继续 Agent（用户可能边说边贴 Key）
    only_secret = _text_is_mostly_secrets(text, secrets)
    if not only_secret:
        return False

    nonce = await store_agent_confirm_nonce(
        account_id=account_id,
        tg_user_id=tg_user_id,
        action_id=action_id,
    )
    safe_summary = _html_escape(str(summary or action_id))
    body = (
        f"🔑 已把密钥写入待确认操作，本地只保存加密版本：\n"
        f"{safe_summary}\n"
        "请再次点击确认执行（不会在回复中复述密钥）。"
    )
    markup = _agent_confirm_keyboard(account_id, nonce, dangerous=danger) if nonce else None
    if not nonce:
        body += "\n（Redis 不可用，请稍后重试，或到 Web /assistant 重新发起）"
    await send(body, edit=True, reply_markup=markup)
    return True


async def handle_agent_command(
    *,
    account_id: int,
    tg_user_id: int,
    role: str,
    text: str,
    send: Any,
    draft: Any | None = None,
    edit: bool = False,
) -> None:
    """处理 `/agent` 及其子命令与自然语言任务。"""

    raw = (text or "").strip()
    parts = raw.split(maxsplit=1)
    head = (parts[0] if parts else "").lower()
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
            "写操作会弹出 Inline 确认按钮，确认后才会落库。"
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

    await enter_agent_mode(account_id, tg_user_id)
    await run_agent_query(
        account_id=account_id,
        tg_user_id=tg_user_id,
        role=role,
        text=tail,
        send=send,
        draft=draft,
        edit=edit,
    )


async def run_agent_query(
    *,
    account_id: int,
    tg_user_id: int,
    role: str,
    text: str,
    send: Any,
    draft: Any | None = None,
    edit: bool = False,
) -> None:
    """执行一轮助手查询；写工具附带 Inline 确认按钮。"""

    await refresh_agent_mode(account_id, tg_user_id)

    # keep_pending 后用户重发 Key：写回现有 Action，不新开一轮
    try:
        if await try_attach_secrets_to_pending_action(
            account_id=account_id,
            tg_user_id=tg_user_id,
            text=text,
            send=send,
        ):
            return
    except Exception:  # noqa: BLE001
        log.warning("attach secrets to pending action failed", exc_info=True)

    draft_active = False
    if draft is not None:
        try:
            await draft("")
            draft_active = True
        except Exception:  # noqa: BLE001
            log.debug("system agent bot draft unavailable; using persistent placeholder", exc_info=True)
    if not draft_active:
        await send("⏳ 系统助手处理中…", edit=edit)

    assistant_text = ""
    error_text = ""
    proposed_actions: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        svc = get_system_agent_service()
        session = await svc.get_or_create_active_session(
            db,
            channel=CHANNEL_BOT,
            bot_tg_user_id=tg_user_id,
            account_id=account_id,
        )
        await db.commit()
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
                elif et == "action_proposed":
                    action = event.get("action")
                    if isinstance(action, dict):
                        proposed_actions.append(action)
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            log.exception("bot agent query failed")
            error_text = str(exc)[:400]

    if error_text and not assistant_text and not proposed_actions:
        await send(f"❌ {_html_escape(error_text)}", edit=not draft_active)
        return

    body = assistant_text or ""
    if proposed_actions and not body:
        body = "已生成待确认操作，请点击下方按钮确认或取消。"
    elif proposed_actions:
        body = body.rstrip() + "\n\n——\n已生成待确认操作，请点击下方按钮。"

    if len(body) > 3500:
        body = body[:3400] + "\n\n…（已截断，完整内容请到 Web /assistant 查看）"

    safe = _html_escape(body) if body else "（无文本回复）"

    # 单条 Action：主消息带 Inline 按钮；多条：逐条追加
    if len(proposed_actions) == 1:
        action = proposed_actions[0]
        nonce = await store_agent_confirm_nonce(
            account_id=account_id,
            tg_user_id=tg_user_id,
            action_id=str(action.get("id") or ""),
        )
        danger = str(action.get("risk") or "") == "dangerous"
        summary = _html_escape(str(action.get("summary") or action.get("tool_name") or "操作"))
        warning = ""
        preview = action.get("preview") if isinstance(action.get("preview"), dict) else {}
        if preview.get("warning"):
            warning = f"\n⚠️ {_html_escape(str(preview.get('warning')))}"
        elif preview.get("note"):
            warning = f"\nℹ️ {_html_escape(str(preview.get('note')))}"
        card = f"\n\n🧾 <b>待确认</b>\n{summary}{warning}"
        markup = _agent_confirm_keyboard(account_id, nonce, dangerous=danger) if nonce else None
        if not nonce:
            card += "\n（Redis 不可用，请稍后重试，或到 Web /assistant 重新发起）"
        await send(safe + card, edit=not draft_active, reply_markup=markup)
        return

    await send(safe, edit=not draft_active, reply_markup=None)
    for action in proposed_actions:
        nonce = await store_agent_confirm_nonce(
            account_id=account_id,
            tg_user_id=tg_user_id,
            action_id=str(action.get("id") or ""),
        )
        danger = str(action.get("risk") or "") == "dangerous"
        summary = _html_escape(str(action.get("summary") or action.get("tool_name") or "操作"))
        markup = _agent_confirm_keyboard(account_id, nonce, dangerous=danger) if nonce else None
        extra = "" if nonce else "\n（Redis 不可用，请稍后重试，或到 Web 重新发起）"
        await send(
            f"🧾 <b>待确认</b>\n{summary}{extra}",
            edit=False,
            reply_markup=markup,
        )


__all__ = [
    "consume_agent_confirm_payload",
    "enter_agent_mode",
    "exit_agent_mode",
    "handle_agent_command",
    "handle_agent_confirm_callback",
    "is_agent_mode",
    "read_agent_confirm_payload",
    "refresh_agent_mode",
    "run_agent_query",
    "store_agent_confirm_nonce",
    "try_attach_secrets_to_pending_action",
]
