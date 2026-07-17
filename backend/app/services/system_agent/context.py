"""ToolContext：工具 handler 统一上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models.system_agent import SystemAgentSession


@dataclass
class ToolContext:
    """工具执行上下文。

    硬规则：
    - handler 禁止创建 ``AsyncSessionLocal``
    - handler 禁止 ``commit()`` / ``rollback()``；允许 ``flush()``
    - handler 禁止调用本项目 HTTP API
    """

    db: AsyncSession
    channel: str
    role: str
    session: SystemAgentSession | None = None
    account_id: int | None = None
    web_user_id: int | None = None
    bot_tg_user_id: int | None = None
    action: Any | None = None

    def require_role(self, min_role: str) -> None:
        from .registry import role_at_least

        if not role_at_least(self.role, min_role):
            raise PermissionError(f"需要角色 {min_role} 或更高，当前为 {self.role}")
