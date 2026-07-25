"""管理 Bot `/agent` 与助手模式桥接（含写操作 Inline 确认）。"""

from __future__ import annotations

import hashlib
import html
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
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
# 用户文本命中写意图时，若角色过滤掉了写工具且本轮无 proposed_actions，追加有声提示
_WRITE_INTENT_RE = re.compile(
    r"(停用|启用|暂停|恢复|删除|移除|创建|新建|修改|更新|设置|保存|取消|添加|绑定|解绑|"
    r"配置|写入|执行|确认|驳回|拒绝|通过|审批|重启|关闭|打开|切换|迁移|导入|导出|"
    r"pause|resume|delete|create|update|set|save|disable|enable|bind|reject|approve)",
    re.IGNORECASE,
)


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


def _markdown_inline_to_telegram_html(text: str) -> str:
    """Convert the small inline Markdown subset supported by Agent replies."""

    tokens: list[str] = []

    def store(value: str) -> str:
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    escaped = html.escape(str(text or ""), quote=False)
    escaped = re.sub(
        r"`([^`\n]+)`",
        lambda match: store(f"<code>{match.group(1)}</code>"),
        escaped,
    )
    escaped = _MARKDOWN_LINK_RE.sub(
        lambda match: store(
            f'<a href="{html.escape(html.unescape(match.group(2)), quote=True)}">'
            f"{_markdown_inline_to_telegram_html(html.unescape(match.group(1)))}</a>"
        ),
        escaped,
    )
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    for index, value in enumerate(tokens):
        escaped = escaped.replace(f"\x00{index}\x00", value)
    return escaped


def _markdown_table_cells(line: str) -> list[str]:
    value = str(line or "").strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _markdown_table_to_pre(lines: list[str]) -> str:
    rows = [_markdown_table_cells(line) for line in lines]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    plain_rows = [
        [
            re.sub(r"(?:\*\*|__|~~|`)", "", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cell))
            for cell in row
        ]
        for row in normalized
    ]
    widths = [min(30, max(2, *(len(row[index]) for row in plain_rows))) for index in range(width)]

    def render(row: list[str]) -> str:
        return "  ".join(value[: widths[index]].ljust(widths[index]) for index, value in enumerate(row)).rstrip()

    output = [render(plain_rows[0]), "  ".join("-" * item for item in widths)]
    output.extend(render(row) for row in plain_rows[1:])
    return f"<pre>{html.escape(chr(10).join(output), quote=False)}</pre>"


def _markdown_to_telegram_html(markdown: str) -> str:
    """Build a safe Bot API HTML fallback for a GFM-style Agent response."""

    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            language = stripped[3:].strip()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            language_attr = ""
            if re.fullmatch(r"[A-Za-z0-9_+-]{1,32}", language):
                language_attr = f' class="language-{language}"'
            output.append(
                f"<pre><code{language_attr}>{html.escape(chr(10).join(code), quote=False)}</code></pre>"
            )
        elif index + 1 < len(lines) and "|" in line and _MARKDOWN_TABLE_SEPARATOR_RE.fullmatch(lines[index + 1]):
            table_lines = [line]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table_lines.append(lines[index])
                index += 1
            output.append(_markdown_table_to_pre(table_lines))
            continue
        elif not stripped:
            output.append("")
        elif re.fullmatch(r"[-*_]{3,}", stripped):
            output.append("────────")
        elif match := re.match(r"^#{1,6}\s+(.+)$", stripped):
            output.append(f"<b>{_markdown_inline_to_telegram_html(match.group(1))}</b>")
        elif match := re.match(r"^\s*[-+*]\s+(.+)$", line):
            output.append(f"• {_markdown_inline_to_telegram_html(match.group(1))}")
        elif match := re.match(r"^\s*(\d+)[.)]\s+(.+)$", line):
            output.append(f"{match.group(1)}. {_markdown_inline_to_telegram_html(match.group(2))}")
        elif match := re.match(r"^\s*>\s?(.*)$", line):
            output.append(f"<blockquote>{_markdown_inline_to_telegram_html(match.group(1))}</blockquote>")
        else:
            output.append(_markdown_inline_to_telegram_html(line))
        index += 1
    return "\n".join(output).strip()


def _agent_progress_text(event: dict[str, Any]) -> str:
    """Translate durable Agent events into a compact Telegram draft status."""

    event_type = str(event.get("type") or "")
    provider = _html_escape(str(event.get("provider_name") or "当前提供商"))
    model = _html_escape(str(event.get("model") or "默认模型"))
    target = f"{provider} / <code>{model}</code>"
    if event_type == "model_capability_check":
        return f"🔎 正在检查 {target} 的工具调用能力…"
    if event_type == "provider_selected":
        reason = str(event.get("reason") or "")
        if reason == "model_fallback":
            return f"🔄 已切换到同一提供商的备用模型 {target}…"
        if reason == "provider_fallback":
            return f"🔁 正在切换到备用提供商 {target}…"
        return f"🤖 已选择 {target}，正在理解你的需求…"
    if event_type == "skill_selected":
        summary = str(event.get("understanding_summary") or "").strip()
        if summary:
            return f"🧭 已理解需求：{_html_escape(summary[:180])}"
        return "🧭 已理解需求，正在准备所需能力…"
    if event_type == "model_attempt":
        attempt = max(1, int(event.get("attempt") or 1))
        max_retries = max(0, int(event.get("max_retries") or 0))
        if attempt > 1:
            return f"🔄 正在重试 {target}（第 {attempt - 1}/{max_retries} 次）…"
        return f"🤖 {target} 正在思考并规划…"
    if event_type == "retry_scheduled":
        retry_number = max(1, int(event.get("retry_number") or 1))
        max_retries = max(retry_number, int(event.get("max_retries") or retry_number))
        delay = max(0.0, float(event.get("delay_seconds") or 0))
        delay_text = str(int(delay)) if delay.is_integer() else f"{delay:.1f}"
        return f"⏱️ {target} 暂时失败，{delay_text} 秒后进行第 {retry_number}/{max_retries} 次重试…"
    if event_type == "model_exhausted":
        return f"⚠️ {target} 重试已用尽，正在寻找可用的备用模型…"
    if event_type in {"tool_started", "tool_finished"}:
        description = str(event.get("tool_description") or event.get("tool_name") or "系统工具")
        description = _html_escape(description[:180])
        if event_type == "tool_started":
            return f"🛠️ 正在调用：{description}…"
        if event.get("is_error"):
            return f"⚠️ 调用失败：{description}，正在判断下一步…"
        return f"✅ 已完成：{description}，正在整理结果…"
    return ""


def _tool_visibility(*, channel: str, role: str) -> dict[str, Any]:
    """比对角色过滤前后的工具可见性（bot_bridge 侧有声提示 / 状态输出用）。"""

    registry = get_registry()
    visible = registry.list_for(channel=channel, role=role or "viewer")
    full = registry.list_for(channel=channel, role="admin")
    write_visible = [t for t in visible if not t.read_only]
    write_full = [t for t in full if not t.read_only]
    read_visible = [t for t in visible if t.read_only]
    write_hidden = [t for t in write_full if t.name not in {s.name for s in write_visible}]
    return {
        "role": str(role or "viewer"),
        "read_count": len(read_visible),
        "write_count": len(write_visible),
        "write_tools_visible": len(write_visible) > 0,
        "write_tools_hidden_by_role": len(write_hidden) > 0,
        "total_visible": len(visible),
    }


def _write_intent_hint(*, role: str, text: str, proposed_actions: list[dict[str, Any]]) -> str:
    """角色过滤导致写工具不可见时，把无声失败改成有声提示（纯文本，交由 markdown→HTML 统一转义）。"""

    if proposed_actions:
        return ""
    if not _WRITE_INTENT_RE.search(str(text or "")):
        return ""
    vis = _tool_visibility(channel=CHANNEL_BOT, role=role)
    if not vis["write_tools_hidden_by_role"]:
        return ""
    role_label = str(vis["role"])
    return (
        f"\n\nℹ️ 当前角色 {role_label} 无权发起写操作（需 operator 及以上），"
        "请在管理 Bot 用户绑定中调整。"
    )


async def _redis_nonce_available() -> bool:
    """探测 Redis 是否可用于 Inline 确认 nonce。"""

    try:
        redis = get_redis()
        probe_key = AGENT_CONFIRM_PREFIX + "probe"
        await redis.set(probe_key, "1", ex=5)
        await redis.delete(probe_key)
        return True
    except Exception:  # noqa: BLE001
        return False


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
            body += "\n（Redis 不可用，请稍后重试，或到 Web 悬浮助手重新发起）"
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
        body += "\n（Redis 不可用，请稍后重试，或到 Web 悬浮助手重新发起）"
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
        vis = _tool_visibility(channel=CHANNEL_BOT, role=role)
        redis_ok = await _redis_nonce_available()
        role_label = _html_escape(str(vis["role"]))
        msg = (
            "🤖 <b>系统助手</b>\n"
            f"账号：<code>{account_id}</code>\n"
            f"当前角色：<code>{role_label}</code>\n"
            f"可用工具：读 {vis['read_count']} / 写 {vis['write_count']}"
            f"（合计 {vis['total_visible']}）\n"
            f"Redis 确认票据：{'可用' if redis_ok else '不可用（写操作无 Inline 按钮）'}\n"
            f"助手模式：{'已开启' if ok or active else 'Redis 不可用，仅 /agent 单次任务可用'}\n\n"
            "用法：\n"
            "/agent &lt;问题&gt; — 直接提问\n"
            "/agent new — 新建会话\n"
            "/agent clear — 删除当前会话\n"
            "/agent exit — 退出助手模式\n\n"
            "助手模式下可直接发送自然语言（既有斜杠命令仍优先）。\n"
            "写操作会弹出 Inline 确认按钮，确认后才会落库。"
        )
        if not vis["write_tools_visible"]:
            msg += (
                f"\n\nℹ️ 当前角色 {role_label} 看不到写工具；"
                "需要 operator 及以上才能发起写操作确认。"
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
    last_draft_text = ""

    async def update_draft(value: str) -> None:
        """进度/流式片段更新 draft；空串被守卫拦住，清空请走 clear_draft。"""
        nonlocal last_draft_text
        if not draft_active or draft is None or not value or value == last_draft_text:
            return
        try:
            await draft(value)
            last_draft_text = value
        except Exception:  # noqa: BLE001
            log.debug("system agent bot draft update failed", exc_info=True)

    async def clear_draft() -> None:
        """最终消息发出前清空 ephemeral draft，避免与真实消息并存。"""
        nonlocal last_draft_text
        if not draft_active or draft is None:
            return
        try:
            await draft("")
            last_draft_text = ""
        except Exception:  # noqa: BLE001
            log.debug("system agent bot draft clear failed", exc_info=True)

    if draft is not None:
        try:
            last_draft_text = "⏳ 系统助手正在理解你的需求…"
            await draft(last_draft_text)
            draft_active = True
        except Exception:  # noqa: BLE001
            log.debug("system agent bot draft unavailable; using persistent placeholder", exc_info=True)
    placeholder_message_id: int | None = None
    if not draft_active:
        placeholder = await send("⏳ 系统助手正在理解你的需求…", edit=edit)
        if isinstance(placeholder, dict) and placeholder.get("message_id") is not None:
            placeholder_message_id = int(placeholder["message_id"])

    assistant_text = ""
    streamed_assistant_text = ""
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
                if et == "assistant_delta_reset":
                    streamed_assistant_text = ""
                elif et == "assistant_delta":
                    # 流式过程只推进 draft；最终正文只走真实消息，避免 draft 与 final 双气泡
                    streamed_assistant_text += str(event.get("delta") or "")
                    await update_draft(_markdown_to_telegram_html(streamed_assistant_text))
                elif et == "assistant_message":
                    # 完整正文不进 draft：随后会 send 真实消息，若再 update_draft 会与 final 并存
                    assistant_text = str(event.get("content") or "")
                elif et == "error":
                    error_text = str(event.get("message") or "助手运行失败")
                elif et == "action_proposed":
                    action = event.get("action")
                    if isinstance(action, dict):
                        proposed_actions.append(action)
                else:
                    await update_draft(_agent_progress_text(event))
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            log.exception("bot agent query failed")
            error_text = str(exc)[:400]

    if error_text and not assistant_text and not proposed_actions:
        await clear_draft()
        await send(
            f"❌ {_html_escape(error_text)}",
            edit=not draft_active and placeholder_message_id is not None,
            edit_message_id=placeholder_message_id,
        )
        return

    vis = _tool_visibility(channel=CHANNEL_BOT, role=role)
    log.info(
        "bot agent query done account=%s tg_user=%s role=%s write_tools_visible=%s proposed_actions=%s",
        account_id,
        tg_user_id,
        vis["role"],
        vis["write_tools_visible"],
        len(proposed_actions),
    )

    body = assistant_text or ""
    if proposed_actions and not body:
        body = "已生成待确认操作，请点击下方按钮确认或取消。"
    elif proposed_actions:
        body = body.rstrip() + "\n\n——\n已生成待确认操作，请点击下方按钮。"

    role_hint = _write_intent_hint(role=role, text=text, proposed_actions=proposed_actions)
    if role_hint:
        body = (body.rstrip() if body else "") + role_hint

    if len(body) > 3500:
        body = body[:3400] + "\n\n…（已截断，完整内容请到 Web 悬浮助手查看）"

    safe = _markdown_to_telegram_html(body) if body else "（无文本回复）"

    # 单条 Action：主消息带 Inline 按钮；多条：逐条追加
    if len(proposed_actions) == 1:
        action = proposed_actions[0]
        nonce = await store_agent_confirm_nonce(
            account_id=account_id,
            tg_user_id=tg_user_id,
            action_id=str(action.get("id") or ""),
        )
        danger = str(action.get("risk") or "") == "dangerous"
        raw_summary = str(action.get("summary") or action.get("tool_name") or "操作")
        summary = _html_escape(raw_summary)
        warning = ""
        rich_warning = ""
        preview = action.get("preview") if isinstance(action.get("preview"), dict) else {}
        if preview.get("warning"):
            warning = f"\n⚠️ {_html_escape(str(preview.get('warning')))}"
            rich_warning = f"\n⚠️ {preview.get('warning')}"
        elif preview.get("note"):
            warning = f"\nℹ️ {_html_escape(str(preview.get('note')))}"
            rich_warning = f"\nℹ️ {preview.get('note')}"
        card = f"\n\n🧾 <b>待确认</b>\n{summary}{warning}"
        rich_card = f"\n\n🧾 **待确认**\n{raw_summary}{rich_warning}"
        markup = _agent_confirm_keyboard(account_id, nonce, dangerous=danger) if nonce else None
        if not nonce:
            card += "\n（Redis 不可用，请稍后重试，或到 Web 悬浮助手重新发起）"
            rich_card += "\n（Redis 不可用，请稍后重试，或到 Web 悬浮助手重新发起）"
        await clear_draft()
        await send(
            safe + card,
            edit=not draft_active and placeholder_message_id is not None,
            edit_message_id=placeholder_message_id,
            reply_markup=markup,
            rich_markdown=body + rich_card,
        )
        return

    await clear_draft()
    await send(
        safe,
        edit=not draft_active and placeholder_message_id is not None,
        edit_message_id=placeholder_message_id,
        reply_markup=None,
        rich_markdown=body or None,
    )
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
