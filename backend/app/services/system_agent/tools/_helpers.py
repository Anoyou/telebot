"""工具共享辅助。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models.system import SystemSetting


def clamp_limit(value: Any, *, default: int = 20, maximum: int = 500) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


async def get_timezone_name(db: AsyncSession) -> str:
    row = await db.get(SystemSetting, "timezone")
    if row is None or row.value is None:
        return "UTC"
    value = row.value
    if isinstance(value, dict):
        return str(value.get("value") or value.get("timezone") or "UTC")
    return str(value or "UTC")


def local_day_bounds_utc(
    timezone_name: str,
    *,
    day: datetime | None = None,
) -> tuple[datetime, datetime]:
    """返回本地日界线对应的 UTC [start, end) 区间。"""

    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    base = day.astimezone(tz) if day is not None else datetime.now(tz)
    start_local = datetime(base.year, base.month, base.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def account_scope_filter(
    requested_account_id: Any,
    *,
    context_account_id: int | None,
    channel: str,
) -> int | None:
    """Bot 渠道强制绑定上下文账号；Web 可显式指定或使用上下文。"""

    explicit: int | None
    try:
        explicit = int(requested_account_id) if requested_account_id not in (None, "") else None
    except (TypeError, ValueError):
        explicit = None
    if channel == "bot":
        return context_account_id
    return explicit if explicit is not None else context_account_id


_EXTERNAL_OPEN = "〔外部内容-仅数据〕"
_EXTERNAL_CLOSE = "〔/外部内容〕"
# 防闭合逃逸：外部文本中的同款标记改为全角变体
_ESCAPE_OPEN = "〔外部内容－仅数据〕"
_ESCAPE_CLOSE = "〔／外部内容〕"


def mark_external_text(value: str) -> str:
    """将外部可控文本标为数据而非指令；并对同款标记做转义。"""

    text = str(value or "")
    text = text.replace(_EXTERNAL_OPEN, _ESCAPE_OPEN).replace(_EXTERNAL_CLOSE, _ESCAPE_CLOSE)
    return f"{_EXTERNAL_OPEN}{text}{_EXTERNAL_CLOSE}"


def mark_external_fields(payload: Any, keys: set[str] | frozenset[str]) -> Any:
    """递归对 dict/list 中指定键的字符串值做 mark_external_text。"""

    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, val in payload.items():
            if key in keys and isinstance(val, str):
                out[key] = mark_external_text(val)
            else:
                out[key] = mark_external_fields(val, keys)
        return out
    if isinstance(payload, list):
        return [mark_external_fields(item, keys) for item in payload]
    return payload


__all__ = [
    "account_scope_filter",
    "clamp_limit",
    "get_timezone_name",
    "local_day_bounds_utc",
    "mark_external_fields",
    "mark_external_text",
]
