"""Action 创建、查询、拒绝、过期与密文清理。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...crypto import decrypt_str, encrypt_str
from ...db.models.system_agent import (
    ACTION_STATUS_EXPIRED,
    ACTION_STATUS_PENDING,
    ACTION_STATUS_REJECTED,
    RISK_NORMAL,
    RUNTIME_SYNC_NOT_REQUIRED,
    SystemAgentAction,
)
from .context import ToolContext
from .redactor import redact_content
from .registry import ToolSpec, role_at_least

log = logging.getLogger(__name__)

DEFAULT_ACTION_TTL = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


def action_to_dict(row: SystemAgentAction) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "account_id": row.account_id,
        "actor_user_id": row.actor_user_id,
        "actor_bot_user_id": row.actor_bot_user_id,
        "channel": row.channel,
        "tool_name": row.tool_name,
        "arguments": row.arguments or {},
        "secret_fields": list(row.secret_fields or []),
        "has_secret": bool(row.secret_payload_enc),
        "summary": row.summary,
        "preview": row.preview or {},
        "risk": row.risk,
        "status": row.status,
        "result": row.result,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "runtime_sync_status": row.runtime_sync_status,
        "runtime_sync_error": row.runtime_sync_error,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
    }


def split_secret_arguments(
    arguments: dict[str, Any],
    secret_names: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """把敏感字段移出普通 arguments。返回 (public_args, secrets, secret_field_names)。"""

    public = dict(arguments or {})
    secrets: dict[str, Any] = {}
    fields: list[str] = []
    for name in secret_names:
        if name not in public:
            continue
        value = public.pop(name)
        if value in (None, ""):
            continue
        secrets[name] = value
        fields.append(name)
        public[f"has_{name}"] = True
    return public, secrets, fields


def encrypt_secret_payload(secrets: dict[str, Any]) -> str | None:
    if not secrets:
        return None
    return encrypt_str(json.dumps(secrets, ensure_ascii=False, default=str))


def decrypt_secret_payload(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        raw = decrypt_str(token)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        log.warning("decrypt action secret payload failed", exc_info=True)
        return {}


async def create_pending_action(
    db: AsyncSession,
    *,
    ctx: ToolContext,
    spec: ToolSpec,
    arguments: dict[str, Any],
    preview: dict[str, Any],
    summary: str | None = None,
) -> SystemAgentAction:
    if not role_at_least(ctx.role, spec.min_role):
        raise PermissionError(f"需要角色 {spec.min_role} 或更高")
    if ctx.channel not in spec.channels:
        raise PermissionError(f"工具 {spec.name} 在渠道 {ctx.channel} 不可用")

    public_args, secrets, secret_fields = split_secret_arguments(
        arguments, spec.secret_argument_names
    )
    safe_preview = redact_content(preview if isinstance(preview, dict) else {"value": preview})
    if not isinstance(safe_preview, dict):
        safe_preview = {"value": safe_preview}

    summary_text = (summary or str(safe_preview.get("summary") or spec.description))[:512]
    account_id = (
        public_args.get("account_id")
        if public_args.get("account_id") is not None
        else ctx.account_id
    )
    try:
        account_id_int = int(account_id) if account_id is not None else None
    except (TypeError, ValueError):
        account_id_int = ctx.account_id

    action = SystemAgentAction(
        id=str(uuid.uuid4()),
        session_id=ctx.session.id if ctx.session is not None else None,
        account_id=account_id_int,
        actor_user_id=ctx.web_user_id,
        actor_bot_user_id=ctx.bot_tg_user_id,
        channel=ctx.channel,
        tool_name=spec.name,
        arguments=redact_content(public_args) if isinstance(redact_content(public_args), dict) else public_args,
        secret_fields=secret_fields or None,
        secret_payload_enc=encrypt_secret_payload(secrets),
        summary=summary_text,
        preview=safe_preview,
        risk=spec.risk or RISK_NORMAL,
        status=ACTION_STATUS_PENDING,
        # 执行成功前不标 pending，避免未执行就显示「待同步」
        runtime_sync_status=RUNTIME_SYNC_NOT_REQUIRED,
        expires_at=_now() + DEFAULT_ACTION_TTL,
    )
    db.add(action)
    await db.flush()
    return action


async def get_action(db: AsyncSession, action_id: str) -> SystemAgentAction | None:
    return await db.get(SystemAgentAction, action_id)


async def lock_action(db: AsyncSession, action_id: str) -> SystemAgentAction | None:
    """锁定 Action 行，供确认、拒绝和密钥补填共享同一状态机边界。"""

    q = select(SystemAgentAction).where(SystemAgentAction.id == action_id)
    try:
        q = q.with_for_update()
    except Exception:  # noqa: BLE001 - SQLite 测试环境不支持时退化为普通查询
        pass
    result = await db.execute(q)
    return result.scalar_one_or_none()


def web_owns_action(action: SystemAgentAction, web_user_id: int | None) -> bool:
    """Web 渠道：必须精确匹配 actor_user_id（禁止 None 共享）。"""

    if web_user_id is None:
        return False
    return action.actor_user_id is not None and int(action.actor_user_id) == int(web_user_id)


def bot_owns_action(action: SystemAgentAction, bot_tg_user_id: int | None) -> bool:
    """Bot 渠道：必须精确匹配 actor_bot_user_id。"""

    if bot_tg_user_id is None:
        return False
    return (
        action.actor_bot_user_id is not None
        and int(action.actor_bot_user_id) == int(bot_tg_user_id)
    )


def clear_action_secrets(action: SystemAgentAction, secret_names: tuple[str, ...] = ()) -> None:
    """清除密文与 has_* 标记（验证失败 / 过期 / 拒绝共用）。"""

    action.secret_payload_enc = None
    names = tuple(secret_names or ()) or tuple(action.secret_fields or ())
    action.secret_fields = None
    args = dict(action.arguments or {})
    for name in names:
        args.pop(name, None)
        args.pop(f"has_{name}", None)
    for key in list(args.keys()):
        if str(key).startswith("has_"):
            args.pop(key, None)
    action.arguments = args


async def list_actions(
    db: AsyncSession,
    *,
    session_id: str | None = None,
    web_user_id: int | None = None,
    bot_tg_user_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[SystemAgentAction]:
    q = select(SystemAgentAction).order_by(SystemAgentAction.created_at.desc()).limit(
        max(1, min(limit, 200))
    )
    if session_id:
        q = q.where(SystemAgentAction.session_id == session_id)
    if web_user_id is not None:
        q = q.where(SystemAgentAction.actor_user_id == web_user_id)
    if bot_tg_user_id is not None:
        q = q.where(SystemAgentAction.actor_bot_user_id == bot_tg_user_id)
    if status:
        q = q.where(SystemAgentAction.status == status)
    result = await db.execute(q)
    return list(result.scalars().all())


async def mark_expired_if_needed(db: AsyncSession, action: SystemAgentAction) -> SystemAgentAction:
    if action.status != ACTION_STATUS_PENDING:
        return action
    expires = action.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires is not None and expires <= _now():
        action.status = ACTION_STATUS_EXPIRED
        clear_action_secrets(action)
        action.error_code = "EXPIRED"
        action.error_message = "操作已过期，请重新发起"
        action.updated_at = _now()
        await db.flush()
    return action


async def reject_action(db: AsyncSession, action: SystemAgentAction) -> SystemAgentAction:
    action = await mark_expired_if_needed(db, action)
    if action.status != ACTION_STATUS_PENDING:
        return action
    action.status = ACTION_STATUS_REJECTED
    clear_action_secrets(action)
    action.updated_at = _now()
    await db.flush()
    return action


async def clear_expired_secrets(db: AsyncSession, *, limit: int = 100) -> int:
    """顺带清理已过期 pending Action 的密文并标记 expired。"""

    now = _now()
    result = await db.execute(
        select(SystemAgentAction)
        .where(
            SystemAgentAction.status == ACTION_STATUS_PENDING,
            SystemAgentAction.expires_at <= now,
        )
        .limit(limit)
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = ACTION_STATUS_EXPIRED
        clear_action_secrets(row)
        row.error_code = "EXPIRED"
        row.error_message = "操作已过期"
        row.updated_at = now
    if rows:
        await db.flush()
    return len(rows)


__all__ = [
    "DEFAULT_ACTION_TTL",
    "action_to_dict",
    "bot_owns_action",
    "clear_action_secrets",
    "clear_expired_secrets",
    "create_pending_action",
    "decrypt_secret_payload",
    "encrypt_secret_payload",
    "get_action",
    "lock_action",
    "list_actions",
    "mark_expired_if_needed",
    "reject_action",
    "split_secret_arguments",
    "web_owns_action",
]
