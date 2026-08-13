"""运行预设服务：本轮只实现 production 与 safe_watch。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..db.base import AsyncSessionLocal
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..worker.ipc import CMD_PAUSE, CMD_RESUME, publish_cmd_with_ack
from . import audit as audit_svc
from . import platform_capabilities

log = logging.getLogger(__name__)

RUNTIME_PROFILE_STATE_KEY = "runtime_profile_state"
PROFILE_ROLLBACK_SNAPSHOT_KEY = "profile_rollback_snapshot"
SAFE_WATCH_LEDGER_DENY_OWNER = "runtime_profile:safe_watch"
SAFE_WATCH_LEDGER_DENY_REASON = "safe_watch_active"
SAFE_WATCH_BLIND_SPOT = "interaction_bot 采集已停；值守期间该通道存在实时观测盲区，未取更新将在恢复后续取。"
PROFILE_CONVERGENCE_TIMEOUT_SECONDS = 5.0

ProfileStatus = Literal["idle", "applying", "active", "restoring", "failed"]


class RuntimeProfileError(RuntimeError):
    error_code = "PROFILE_TRANSITION_FAILED"


class ProfileConvergenceFailed(RuntimeProfileError):
    error_code = "PROFILE_CONVERGENCE_FAILED"


class ProfileSnapshotInvalid(RuntimeProfileError):
    error_code = "PROFILE_SNAPSHOT_INVALID"


PRESETS: dict[str, dict[str, bool | None]] = {
    "production": {key: True for key in platform_capabilities.ALL_MODULE_KEYS},
    "safe_watch": {
        "ai": False,
        "interaction_bot": False,
        "webhooks": False,
        "ledger": None,
        "dispatch_debug": True,
    },
}

_STATE_CACHE: dict[str, Any] = {
    "active_profile": None,
    "status": "idle",
    "last_error": None,
    "operator_id": None,
    "updated_at": None,
    "resume_nonce": None,
}
_CACHE_READY = False
_PROFILE_LOCK = asyncio.Lock()
_DENY_HANDLE: platform_capabilities.LedgerActionDenyRegistration | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalized_state(value: Any) -> dict[str, Any]:
    if value is None:
        return {"active_profile": None, "status": "idle", "last_error": None, "operator_id": None, "updated_at": None, "resume_nonce": None}
    if not isinstance(value, dict):
        raise ValueError("runtime profile state must be an object")
    profile = value.get("active_profile")
    status = value.get("status")
    if profile not in {None, "safe_watch"} or status not in {"idle", "applying", "active", "restoring", "failed"}:
        raise ValueError("invalid runtime profile state")
    return {
        "active_profile": profile,
        "status": status,
        "last_error": str(value.get("last_error") or "")[:500] or None,
        "operator_id": value.get("operator_id"),
        "updated_at": value.get("updated_at"),
        "resume_nonce": str(value.get("resume_nonce") or "").strip() or None,
    }


def _cache_state(state: dict[str, Any]) -> dict[str, Any]:
    global _CACHE_READY
    _STATE_CACHE.clear()
    _STATE_CACHE.update(state)
    _CACHE_READY = True
    return dict(_STATE_CACHE)


def is_safe_watch_active_cached(*, fail_closed: bool = True) -> bool:
    if not _CACHE_READY:
        return bool(fail_closed)
    return _STATE_CACHE.get("active_profile") == "safe_watch"


async def refresh_state_from_db(db: AsyncSession | None = None) -> dict[str, Any]:
    try:
        if db is not None:
            row = await db.get(SystemSetting, RUNTIME_PROFILE_STATE_KEY)
            state = _cache_state(_normalized_state(row.value if row else None))
            _sync_process_local_deny(state)
            return state
        async with AsyncSessionLocal() as session:
            row = await session.get(SystemSetting, RUNTIME_PROFILE_STATE_KEY)
            state = _cache_state(_normalized_state(row.value if row else None))
            _sync_process_local_deny(state)
            return state
    except Exception:
        # 数据损坏或读取失败时按值守处理，防止 worker 冷启动逃逸。
        log.exception("读取运行预设失败，按 safe_watch fail-closed")
        state = _cache_state(
            {
                "active_profile": "safe_watch",
                "status": "failed",
                "last_error": "profile_state_unavailable",
                "operator_id": None,
                "updated_at": _now(),
                "resume_nonce": None,
            }
        )
        _sync_process_local_deny(state)
        return state


async def read_worker_pause_state() -> bool:
    state = await refresh_state_from_db()
    return state.get("active_profile") == "safe_watch"


def _ensure_deny_registered() -> None:
    global _DENY_HANDLE
    if _DENY_HANDLE is None:
        _DENY_HANDLE = platform_capabilities.register_ledger_action_deny(
            SAFE_WATCH_LEDGER_DENY_REASON, owner=SAFE_WATCH_LEDGER_DENY_OWNER
        )


def _dispose_deny() -> None:
    global _DENY_HANDLE
    if _DENY_HANDLE is not None:
        _DENY_HANDLE.dispose()
        _DENY_HANDLE = None


def _sync_process_local_deny(state: dict[str, Any]) -> None:
    """让每个主进程/worker 的进程内 T3 注册与持久 profile 自愈一致。"""

    if state.get("active_profile") == "safe_watch":
        _ensure_deny_registered()
    else:
        _dispose_deny()


async def startup_restore() -> dict[str, Any]:
    return await refresh_state_from_db()


async def shutdown() -> None:
    # 只清理本进程持有的注册；持久 profile 不变，下次启动会自行恢复。
    _dispose_deny()


async def _write_setting(db: AsyncSession, key: str, value: Any) -> None:
    row = await db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
        flag_modified(row, "value")


async def _write_state(db: AsyncSession, **updates: Any) -> dict[str, Any]:
    state = dict(_STATE_CACHE) if _CACHE_READY else _normalized_state(None)
    state.update(updates)
    state["updated_at"] = _now()
    await _write_setting(db, RUNTIME_PROFILE_STATE_KEY, state)
    await db.commit()
    return _cache_state(state)


async def _module_snapshot(db: AsyncSession) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for key in platform_capabilities.ALL_MODULE_KEYS:
        enabled, _generation = await platform_capabilities.read_module_desired(db, key)
        result[key] = enabled
    return result


async def _set_module_checked(
    db: AsyncSession,
    key: str,
    enabled: bool,
    *,
    operator_id: int | None,
    apply_local: bool = True,
) -> None:
    result = await platform_capabilities.set_module_enabled(
        db,
        key,
        enabled,
        user_id=operator_id,
        apply_local=apply_local,
        write_audit=False,
    )
    if result.get("runtime_state") == "failed":
        raise RuntimeProfileError(
            str(result.get("last_error") or f"{key} runtime convergence failed")
        )


async def _reapply_safe_watch_fail_closed(
    db: AsyncSession,
    *,
    operator_id: int | None,
) -> None:
    """转换失败后尽力重施完整值守态；任何单点失败都不能阻断后续闭锁。"""

    _ensure_deny_registered()
    for key in platform_capabilities.ALL_MODULE_KEYS:
        target = PRESETS["safe_watch"][key]
        if target is None:
            continue
        try:
            await _set_module_checked(
                db,
                key,
                target,
                operator_id=operator_id,
                apply_local=key != "interaction_bot",
            )
        except Exception:  # noqa: BLE001
            log.exception("转换失败后重新应用值守模块状态失败: %s", key)
    try:
        await platform_capabilities.stop_local_module("interaction_bot")
    except Exception:  # noqa: BLE001
        log.exception("转换失败后停止 interaction_bot manager 失败")
    try:
        await _converge_workers(CMD_PAUSE)
    except Exception:  # noqa: BLE001
        log.exception("转换失败后重新闭锁 worker 失败")


async def dry_run(db: AsyncSession, preset_key: str) -> dict[str, Any]:
    if preset_key not in PRESETS:
        raise KeyError(preset_key)
    current = await _module_snapshot(db)
    diff = []
    for key, target in PRESETS[preset_key].items():
        if target is None:
            target = current[key]
        if current[key] != target:
            diff.append({"key": key, "from_enabled": current[key], "to_enabled": target})
    return {"preset": preset_key, "diff": diff, "blind_spot": SAFE_WATCH_BLIND_SPOT if preset_key == "safe_watch" else None}


async def validate_worker_resume_nonce(nonce: str) -> bool:
    state = await refresh_state_from_db()
    return bool(
        nonce
        and state.get("active_profile") == "safe_watch"
        and state.get("status") == "restoring"
        and secrets.compare_digest(str(state.get("resume_nonce") or ""), str(nonce))
    )


async def _converge_workers(command: str, **payload: Any) -> dict[str, Any]:
    # 只等待当前真实存活的 worker。未启动/启动中的账号会在冷启动时读取
    # SystemSetting 并自行进入 safe_watch，不能把不存在的 ACK 计作收敛失败。
    from ..worker import supervisor

    account_ids = sorted(
        {
            int(row["account_id"])
            for row in supervisor.get_worker_runtime_snapshot()
            if row.get("alive") and row.get("desired") == "running"
        }
    )
    if not account_ids:
        return {"total": 0, "acked": 0, "failed": []}
    redis = get_redis()
    results = await asyncio.gather(
        *(
            publish_cmd_with_ack(
                redis,
                aid,
                command,
                timeout=PROFILE_CONVERGENCE_TIMEOUT_SECONDS,
                source="runtime_profile",
                **payload,
            )
            for aid in account_ids
        ),
        return_exceptions=True,
    )
    failed = [aid for aid, result in zip(account_ids, results, strict=True) if result is not True]
    return {"total": len(account_ids), "acked": len(account_ids) - len(failed), "failed": failed}


async def apply(db: AsyncSession, preset_key: str, *, operator_id: int | None) -> dict[str, Any]:
    if preset_key != "safe_watch":
        raise ValueError("本轮只允许显式进入 safe_watch；production 请使用 restore")
    async with _PROFILE_LOCK:
        state = await refresh_state_from_db(db)
        if state.get("active_profile") == "safe_watch" and state.get("status") == "active":
            return await get_status(db)
        snapshot = await _module_snapshot(db)
        await _write_setting(db, PROFILE_ROLLBACK_SNAPSHOT_KEY, {"modules": snapshot, "created_at": _now()})
        await db.commit()
        await _write_state(db, active_profile="safe_watch", status="applying", last_error=None, operator_id=operator_id, resume_nonce=None)
        _ensure_deny_registered()
        try:
            targets = PRESETS["safe_watch"]
            for key in platform_capabilities.ALL_MODULE_KEYS:
                target = targets[key]
                if target is None:
                    continue
                await _set_module_checked(
                    db, key, target, operator_id=operator_id
                )
            convergence = await _converge_workers(CMD_PAUSE)
            if convergence["failed"]:
                raise ProfileConvergenceFailed(
                    f"worker pause convergence timeout: {convergence['failed']}"
                )
            await _write_state(db, status="active", last_error=None)
            await audit_svc.write(db, operator_id, "runtime_profile.enter", target="safe_watch", detail={"operator_id": operator_id, "worker_convergence": convergence, "interaction_bot": "interaction_bot 采集已停", "blind_spot": SAFE_WATCH_BLIND_SPOT})
            await db.commit()
        except Exception as exc:
            await _reapply_safe_watch_fail_closed(db, operator_id=operator_id)
            await _write_state(db, status="failed", last_error=f"{type(exc).__name__}: {exc}")
            raise
        return await get_status(db)


async def restore(db: AsyncSession, *, operator_id: int | None) -> dict[str, Any]:
    async with _PROFILE_LOCK:
        await refresh_state_from_db(db)
        snap_row = await db.get(SystemSetting, PROFILE_ROLLBACK_SNAPSHOT_KEY)
        snapshot = snap_row.value if snap_row and isinstance(snap_row.value, dict) else None
        modules = snapshot.get("modules") if isinstance(snapshot, dict) else None
        if not isinstance(modules, dict):
            raise ProfileSnapshotInvalid("值守恢复快照不存在或已损坏")
        nonce = secrets.token_urlsafe(24)
        await _write_state(
            db,
            active_profile="safe_watch",
            status="restoring",
            last_error=None,
            operator_id=operator_id,
            resume_nonce=nonce,
        )
        interaction_should_run = False
        try:
            for key in platform_capabilities.ALL_MODULE_KEYS:
                if key not in modules or not isinstance(modules[key], bool):
                    raise ProfileSnapshotInvalid(f"值守恢复快照缺少模块 {key}")
                if key == "interaction_bot":
                    interaction_should_run = modules[key]
                await _set_module_checked(
                    db,
                    key,
                    modules[key],
                    operator_id=operator_id,
                    apply_local=key != "interaction_bot",
                )
            # worker 仅接受与 DB 中 restoring 状态匹配的一次性凭据；普通 resume
            # 永远不能解除 safe_watch reason。资金 deny 保留到完整恢复完成。
            convergence = await _converge_workers(CMD_RESUME, resume_nonce=nonce)
            if convergence["failed"]:
                raise ProfileConvergenceFailed(
                    f"worker resume convergence timeout: {convergence['failed']}"
                )
            # manager 在 profile 仍为 restoring 时启动；模拟入口的纵深闸仍会
            # 观测并跳过更新。只有 manager 收敛成功后才真正清 profile 与 deny。
            if interaction_should_run:
                await platform_capabilities.reconcile_local_module("interaction_bot")
            if snap_row is not None:
                await db.delete(snap_row)
            await _write_state(
                db, active_profile=None, status="idle", last_error=None, resume_nonce=None
            )
            _dispose_deny()
            await audit_svc.write(db, operator_id, "runtime_profile.exit", target="safe_watch", detail={"operator_id": operator_id, "worker_convergence": convergence, "restored_modules": modules})
            await db.commit()
        except Exception as exc:
            # 恢复失败重新闭锁；不得留下名义恢复、实际半开的状态。
            await _reapply_safe_watch_fail_closed(db, operator_id=operator_id)
            await _write_state(db, active_profile="safe_watch", status="failed", last_error=f"{type(exc).__name__}: {exc}", resume_nonce=None)
            raise
        return await get_status(db)


async def get_status(db: AsyncSession) -> dict[str, Any]:
    state = await refresh_state_from_db(db)
    modules = await _module_snapshot(db)
    matched = "custom"
    if state.get("active_profile") == "safe_watch":
        matched = "safe_watch"
    elif all(modules.get(key) is True for key in platform_capabilities.ALL_MODULE_KEYS):
        matched = "production"
    public_state = {key: value for key, value in state.items() if key != "resume_nonce"}
    return {**public_state, "current_profile": matched, "modules": modules, "blind_spot": SAFE_WATCH_BLIND_SPOT if state.get("active_profile") == "safe_watch" else None}


def _reset_for_tests() -> None:
    global _CACHE_READY, _DENY_HANDLE
    if _DENY_HANDLE is not None:
        _DENY_HANDLE.dispose()
    _DENY_HANDLE = None
    _CACHE_READY = False
    _STATE_CACHE.clear()
    _STATE_CACHE.update(_normalized_state(None))


__all__ = [
    "PRESETS",
    "PROFILE_ROLLBACK_SNAPSHOT_KEY",
    "RUNTIME_PROFILE_STATE_KEY",
    "SAFE_WATCH_BLIND_SPOT",
    "SAFE_WATCH_LEDGER_DENY_OWNER",
    "SAFE_WATCH_LEDGER_DENY_REASON",
    "ProfileConvergenceFailed",
    "ProfileSnapshotInvalid",
    "RuntimeProfileError",
    "apply",
    "dry_run",
    "get_status",
    "is_safe_watch_active_cached",
    "read_worker_pause_state",
    "refresh_state_from_db",
    "restore",
    "shutdown",
    "startup_restore",
    "_reset_for_tests",
]
