"""Global AI capability switch helpers."""

from __future__ import annotations

from typing import Any

from ..db.base import AsyncSessionLocal
from ..db.models.system import SystemSetting

AI_ENABLED_SETTING_KEY = "ai_enabled"


def normalize_ai_enabled(value: Any, *, default: bool = True) -> bool:
    if isinstance(value, dict):
        return bool(value.get("enabled", default))
    if value is None:
        return default
    return bool(value)


async def is_ai_enabled(db: Any | None = None, *, default: bool = True) -> bool:
    if db is not None:
        row = await db.get(SystemSetting, AI_ENABLED_SETTING_KEY)
        return normalize_ai_enabled(row.value if row is not None else None, default=default)

    async with AsyncSessionLocal() as session:
        row = await session.get(SystemSetting, AI_ENABLED_SETTING_KEY)
        return normalize_ai_enabled(row.value if row is not None else None, default=default)


__all__ = ["AI_ENABLED_SETTING_KEY", "is_ai_enabled", "normalize_ai_enabled"]
