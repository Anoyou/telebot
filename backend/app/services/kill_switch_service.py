"""全局总闸的持久化与运行时收敛。

DB 状态由调用方事务提交；Worker/Bot 收敛只在提交后执行，避免 Agent
通过伪造 WebUser 调 API，或在审计外键失败前留下半完成状态。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..worker.ipc import GCMD_KILL_SWITCH, GLOBAL_CHANNEL, make_cmd
from . import account_bot_runtime, interaction_bot_runtime
from . import platform_capabilities as platform_caps
from .redactor import redact_text


class KillSwitchConvergenceError(RuntimeError):
    def __init__(self, enabled: bool, errors: list[str]) -> None:
        self.enabled = bool(enabled)
        self.errors = list(errors)
        super().__init__("总闸目标状态已保存，但运行时未完全收敛")


async def get_enabled(db: AsyncSession) -> bool:
    row = await db.get(SystemSetting, "kill_switch")
    if row is None:
        return False
    value = row.value
    return bool(value.get("enabled", False)) if isinstance(value, dict) else bool(value)


async def set_enabled(db: AsyncSession, enabled: bool) -> None:
    row = await db.get(SystemSetting, "kill_switch")
    value = {"enabled": bool(enabled)}
    if row is None:
        db.add(SystemSetting(key="kill_switch", value=value))
    else:
        row.value = value
    await db.flush()


async def converge_runtime(db: AsyncSession, enabled: bool) -> None:
    """把本进程和多进程监听者收敛到已提交的总闸目标状态。"""

    from ..worker import supervisor

    if enabled:
        operations = (
            supervisor.stop_running_workers(),
            account_bot_runtime.stop_account_bot_manager(),
            interaction_bot_runtime.stop_interaction_bot_manager(),
        )
    else:
        interaction_ops: list[Any] = []
        try:
            if not platform_caps.get_snapshot().cache_ready:
                await platform_caps.refresh_cache_from_db(db)
            if platform_caps.is_module_enabled_cached("interaction_bot", fail_closed=True):
                interaction_ops.append(
                    interaction_bot_runtime.start_interaction_bot_manager()
                )
        except Exception:  # noqa: BLE001
            # 能力状态未知时保持 fail-closed，等待下一次显式恢复/重试。
            pass
        operations = (
            supervisor.start_active_workers(),
            account_bot_runtime.start_account_bot_manager(),
            *interaction_ops,
        )

    results = await asyncio.gather(*operations, return_exceptions=True)
    failures = [
        f"{type(result).__name__}: {redact_text(str(result))}"
        for result in results
        if isinstance(result, BaseException)
    ]
    try:
        await get_redis().publish(
            GLOBAL_CHANNEL,
            make_cmd(GCMD_KILL_SWITCH, enabled=bool(enabled)),
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"Redis broadcast {type(exc).__name__}: {redact_text(str(exc))}"
        )
    if failures:
        raise KillSwitchConvergenceError(bool(enabled), failures)


__all__ = [
    "KillSwitchConvergenceError",
    "converge_runtime",
    "get_enabled",
    "set_enabled",
]
