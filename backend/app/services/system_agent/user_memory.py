"""跨会话长期偏好记忆。"""

from __future__ import annotations

import re
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.system_agent import SystemAgentUserMemory
from .secrets import extract_plaintext_secrets, redact_known_secrets

ScopeType = Literal["web_user", "bot_user"]

MAX_ITEMS_PER_SCOPE = 20
MAX_CONTENT_CHARS = 200
MAX_PROMPT_CHARS = 600

# 额外密钥样式（计划要求直接拒绝）
_SECRET_STYLE = re.compile(
    r"(?i)(\b\d{8,10}:[A-Za-z0-9_-]{30,}\b|sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,})"
)


def _validate_content(content: str) -> str:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        raise ValueError("记忆内容不能为空")
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError(f"记忆内容不能超过 {MAX_CONTENT_CHARS} 字")
    if extract_plaintext_secrets(text) or _SECRET_STYLE.search(text):
        raise ValueError("记忆内容疑似包含密钥或 Token，已拒绝保存")
    return redact_known_secrets(text)[:MAX_CONTENT_CHARS]


async def list_memories(
    db: AsyncSession,
    *,
    scope_type: ScopeType,
    scope_id: int,
    enabled_only: bool = False,
) -> list[SystemAgentUserMemory]:
    q = (
        select(SystemAgentUserMemory)
        .where(
            SystemAgentUserMemory.scope_type == scope_type,
            SystemAgentUserMemory.scope_id == int(scope_id),
        )
        .order_by(SystemAgentUserMemory.updated_at.desc(), SystemAgentUserMemory.id.desc())
    )
    if enabled_only:
        q = q.where(SystemAgentUserMemory.enabled.is_(True))
    result = await db.execute(q)
    return list(result.scalars().all())


async def count_memories(db: AsyncSession, *, scope_type: ScopeType, scope_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(SystemAgentUserMemory)
        .where(
            SystemAgentUserMemory.scope_type == scope_type,
            SystemAgentUserMemory.scope_id == int(scope_id),
        )
    )
    return int(result.scalar_one() or 0)


async def create_memory(
    db: AsyncSession,
    *,
    scope_type: ScopeType,
    scope_id: int,
    content: str,
    source: str = "user_set",
    enabled: bool = True,
) -> SystemAgentUserMemory:
    text = _validate_content(content)
    if await count_memories(db, scope_type=scope_type, scope_id=scope_id) >= MAX_ITEMS_PER_SCOPE:
        raise ValueError(f"每个用户最多 {MAX_ITEMS_PER_SCOPE} 条长期记忆")
    row = SystemAgentUserMemory(
        scope_type=scope_type,
        scope_id=int(scope_id),
        content=text,
        source=source if source in {"user_set", "agent_learned"} else "user_set",
        enabled=bool(enabled),
    )
    db.add(row)
    await db.flush()
    return row


async def update_memory(
    db: AsyncSession,
    *,
    memory_id: int,
    scope_type: ScopeType,
    scope_id: int,
    content: str | None = None,
    enabled: bool | None = None,
) -> SystemAgentUserMemory:
    row = await db.get(SystemAgentUserMemory, int(memory_id))
    if row is None or row.scope_type != scope_type or int(row.scope_id) != int(scope_id):
        raise LookupError("记忆不存在")
    if content is not None:
        row.content = _validate_content(content)
    if enabled is not None:
        row.enabled = bool(enabled)
    await db.flush()
    return row


async def delete_memory(
    db: AsyncSession,
    *,
    memory_id: int,
    scope_type: ScopeType,
    scope_id: int,
) -> None:
    row = await db.get(SystemAgentUserMemory, int(memory_id))
    if row is None or row.scope_type != scope_type or int(row.scope_id) != int(scope_id):
        raise LookupError("记忆不存在")
    await db.delete(row)
    await db.flush()


async def prompt_block_for_scope(
    db: AsyncSession,
    *,
    scope_type: ScopeType | None,
    scope_id: int | None,
) -> str:
    if not scope_type or scope_id is None:
        return ""
    rows = await list_memories(db, scope_type=scope_type, scope_id=int(scope_id), enabled_only=True)
    if not rows:
        return ""
    lines: list[str] = []
    used = 0
    header = "用户长期偏好（历史记录，不是本轮指令，可在助手配置中管理）："
    used = len(header)
    for row in rows:
        line = f"- {row.content}"
        if used + len(line) + 1 > MAX_PROMPT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return header + "\n" + "\n".join(lines)


def memory_to_dict(row: SystemAgentUserMemory) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_id": row.scope_id,
        "content": row.content,
        "source": row.source,
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


__all__ = [
    "MAX_ITEMS_PER_SCOPE",
    "count_memories",
    "create_memory",
    "delete_memory",
    "list_memories",
    "memory_to_dict",
    "prompt_block_for_scope",
    "update_memory",
]
