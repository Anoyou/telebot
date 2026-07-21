"""Global AI capability switch helpers.

AI 开关继续使用 ``ai_enabled`` SystemSetting，并委托统一平台能力服务。
旧调用点 ``is_ai_enabled`` / ``normalize_ai_enabled`` 保持兼容。
"""

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
    """读取 AI 是否开启。

    优先走平台能力进程内缓存（已 bootstrap 时）；否则回落 DB。
    """

    try:
        from . import platform_capabilities as caps

        snap = caps.get_snapshot()
        if snap.cache_ready:
            return caps.is_ai_enabled_cached(fail_closed=False)
    except Exception:  # noqa: BLE001
        pass

    if db is not None:
        row = await db.get(SystemSetting, AI_ENABLED_SETTING_KEY)
        return normalize_ai_enabled(row.value if row is not None else None, default=default)

    async with AsyncSessionLocal() as session:
        row = await session.get(SystemSetting, AI_ENABLED_SETTING_KEY)
        return normalize_ai_enabled(row.value if row is not None else None, default=default)


__all__ = ["AI_ENABLED_SETTING_KEY", "is_ai_enabled", "normalize_ai_enabled"]
