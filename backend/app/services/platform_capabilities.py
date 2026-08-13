"""平台能力热插拔统一服务。

负责模块定义、SystemSetting 读写、进程内缓存、主进程启停编排，
以及复用现有 ``CMD_RELOAD_CONFIG`` 通知 worker 收敛。

关闭语义：
- 只暂停可选模块的入口与运行时资源，不删除配置、Token、规则或资金数据。
- userbot、插件加载器、Action/审计/结算/补偿属于平台内核，不受普通模块关闭影响。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..db.base import AsyncSessionLocal
from ..db.models.account import Account
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..worker.ipc import CMD_RELOAD_CONFIG, publish_cmd_with_ack

log = logging.getLogger(__name__)

ModuleKey = Literal["ai", "interaction_bot", "webhooks", "ledger", "dispatch_debug"]
RuntimeState = Literal["starting", "ready", "quiescing", "stopped", "failed"]
BlockedReasonCode = Literal[
    "platform_module_disabled",
    "channel_disabled",
    "channel_not_configured",
    "capability_unavailable",
    "platform_module_transitioning",
]

MODULE_DEFS: dict[ModuleKey, dict[str, str]] = {
    "ai": {
        "setting_key": "ai_enabled",
        "label": "AI",
        "description": "模型 Provider、插件 ctx.ai 与 AI 命令",
    },
    "interaction_bot": {
        "setting_key": "interaction_bot_enabled",
        "label": "Interaction Bot",
        "description": "交互 Bot / 测试 Bot 与 interaction_bot 通道",
    },
    "webhooks": {
        "setting_key": "webhooks_enabled",
        "label": "入站 Webhook",
        "description": "公开入站 Webhook 投递通道",
    },
    "ledger": {
        "setting_key": "ledger_enabled",
        "label": "资金台账",
        "description": "台账查询、统计、导出与人工操作面",
    },
    "dispatch_debug": {
        "setting_key": "dispatch_debug_enabled",
        "label": "命中调试",
        "description": "dispatch 模拟与 router debug trace",
    },
}

SETTING_KEY_TO_MODULE: dict[str, ModuleKey] = {
    meta["setting_key"]: key for key, meta in MODULE_DEFS.items()
}

ALL_MODULE_KEYS: tuple[ModuleKey, ...] = tuple(MODULE_DEFS.keys())
DEFAULT_ENABLED = True
LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE = "ledger_actions_failed_closed"
LEDGER_ACTIONS_FAILED_CLOSED_AUDIT_STATUS = "FAILED_CLOSED"

# 进程内只读快照：公开入口（如 webhook）只读缓存，失败时 fail-closed。
_CACHE_READY = False
_CACHE_LOCK = asyncio.Lock()
_DESIRED: dict[ModuleKey, bool] = {key: DEFAULT_ENABLED for key in ALL_MODULE_KEYS}
_GENERATIONS: dict[ModuleKey, int] = {key: 0 for key in ALL_MODULE_KEYS}
_RUNTIME: dict[ModuleKey, RuntimeState] = {key: "starting" for key in ALL_MODULE_KEYS}
_LAST_ERROR: dict[ModuleKey, str | None] = {key: None for key in ALL_MODULE_KEYS}
_LAST_TRANSITION_AT: dict[ModuleKey, datetime | None] = {key: None for key in ALL_MODULE_KEYS}
_SWITCH_LOCKS: dict[ModuleKey, asyncio.Lock] = {key: asyncio.Lock() for key in ALL_MODULE_KEYS}
_LAST_WORKER_CONVERGENCE: dict[str, Any] = {
    "total_accounts": 0,
    "notified": 0,
    "acked": 0,
    "pending": 0,
    "offline_or_timeout": 0,
    "last_broadcast_at": None,
    "notes": [],
}
_LEDGER_DENY_LOCK = threading.RLock()
_LEDGER_DENY_GENERATION = 0
_LEDGER_DENY_NEXT_TOKEN = 1
_LEDGER_DENY_REGISTRATIONS: dict[int, tuple[str, str]] = {}


@dataclass(slots=True)
class LedgerActionDenyRegistration:
    """一个 owner 持有的资金动作拒绝注册；``dispose`` 可重复调用。"""

    owner: str
    reason: str
    generation: int
    _token: int
    _disposed: bool = False

    def dispose(self) -> None:
        """仅清理本句柄登记的条目，不影响其他 owner 或同 owner 的其他注册。"""

        global _LEDGER_DENY_GENERATION
        if self._disposed:
            return
        with _LEDGER_DENY_LOCK:
            if self._disposed:
                return
            removed = _LEDGER_DENY_REGISTRATIONS.pop(self._token, None)
            self._disposed = True
            if removed is not None:
                _LEDGER_DENY_GENERATION += 1

    def __enter__(self) -> LedgerActionDenyRegistration:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.dispose()


class LedgerActionsFailedClosed(RuntimeError):
    """资金动作保险丝断开时抛出的稳定异常。"""

    error_code = LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE
    audit_status = LEDGER_ACTIONS_FAILED_CLOSED_AUDIT_STATUS

    def __init__(self, reasons: tuple[str, ...] | None = None) -> None:
        self.reasons = reasons if reasons is not None else ledger_action_block_reasons()
        reason_text = ", ".join(self.reasons) if self.reasons else "unknown"
        super().__init__(f"ledger actions denied (fail-closed): {reason_text}")


def register_ledger_action_deny(reason: str, *, owner: str) -> LedgerActionDenyRegistration:
    """注册一个进程内资金动作拒绝原因，并把清理所有权交给调用方。"""

    global _LEDGER_DENY_GENERATION, _LEDGER_DENY_NEXT_TOKEN
    normalized_owner = str(owner or "").strip()
    normalized_reason = str(reason or "").strip()
    if not normalized_owner:
        raise ValueError("ledger action deny owner must not be empty")
    if not normalized_reason:
        raise ValueError("ledger action deny reason must not be empty")
    with _LEDGER_DENY_LOCK:
        token = _LEDGER_DENY_NEXT_TOKEN
        _LEDGER_DENY_NEXT_TOKEN += 1
        _LEDGER_DENY_REGISTRATIONS[token] = (normalized_owner, normalized_reason)
        _LEDGER_DENY_GENERATION += 1
        generation = _LEDGER_DENY_GENERATION
    return LedgerActionDenyRegistration(
        owner=normalized_owner,
        reason=normalized_reason,
        generation=generation,
        _token=token,
    )


def ledger_action_deny_reasons() -> tuple[str, ...]:
    """返回稳定排序的拒绝原因快照，不暴露可变注册表。"""

    with _LEDGER_DENY_LOCK:
        return tuple(sorted({reason for _owner, reason in _LEDGER_DENY_REGISTRATIONS.values()}))


def ledger_action_deny_registrations() -> tuple[tuple[str, str], ...]:
    """返回 ``(owner, reason)`` 快照，供诊断和所有权核查。"""

    with _LEDGER_DENY_LOCK:
        return tuple(sorted(_LEDGER_DENY_REGISTRATIONS.values()))


def get_ledger_action_deny_generation() -> int:
    with _LEDGER_DENY_LOCK:
        return _LEDGER_DENY_GENERATION


def ledger_action_block_reasons() -> tuple[str, ...]:
    """返回当前资金动作不可执行的原因；任何读取异常都按断闸处理。"""

    reasons: list[str] = []
    try:
        snap = get_snapshot()
        if not snap.cache_ready:
            reasons.append("capability_cache_not_ready")
        elif snap.desired.get("ledger") is not True:
            reasons.append("ledger_not_desired")
        elif snap.runtime.get("ledger") != "ready":
            runtime_state = snap.runtime.get("ledger")
            reasons.append(f"ledger_runtime_{runtime_state or 'unknown'}")
    except Exception:  # noqa: BLE001
        reasons.append("capability_state_unavailable")
    reasons.extend(ledger_action_deny_reasons())
    return tuple(dict.fromkeys(reasons))


def ledger_actions_enabled() -> bool:
    """仅在 ledger ready 且无 deny 注册时放行资金动作。"""

    return not ledger_action_block_reasons()


def require_ledger_actions_enabled() -> None:
    reasons = ledger_action_block_reasons()
    if reasons:
        raise LedgerActionsFailedClosed(reasons)


@dataclass(frozen=True)
class CapabilitySnapshot:
    """进程内能力快照（不可变）。"""

    desired: dict[ModuleKey, bool]
    generations: dict[ModuleKey, int]
    runtime: dict[ModuleKey, RuntimeState]
    cache_ready: bool
    last_error: dict[ModuleKey, str | None] = field(default_factory=dict)
    last_transition_at: dict[ModuleKey, datetime | None] = field(default_factory=dict)

    def is_enabled(self, module_key: ModuleKey) -> bool:
        if not self.cache_ready:
            return False
        return bool(self.desired.get(module_key, DEFAULT_ENABLED))

    def generation(self, module_key: ModuleKey) -> int:
        return int(self.generations.get(module_key, 0) or 0)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_enabled(value: Any, *, default: bool = DEFAULT_ENABLED) -> bool:
    if isinstance(value, dict):
        return bool(value.get("enabled", default))
    if value is None:
        return default
    return bool(value)


def _normalize_generation(value: Any, *, default: int = 0) -> int:
    if isinstance(value, dict):
        raw = value.get("generation", default)
    else:
        raw = default
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return default


def normalize_capability_setting(
    value: Any,
    *,
    default_enabled: bool = DEFAULT_ENABLED,
) -> dict[str, Any]:
    """把 SystemSetting 值规范化为 ``{enabled, generation}``。"""

    return {
        "enabled": _normalize_enabled(value, default=default_enabled),
        "generation": _normalize_generation(value, default=0),
    }


def module_setting_key(module_key: ModuleKey) -> str:
    return MODULE_DEFS[module_key]["setting_key"]


def module_key_for_setting(setting_key: str) -> ModuleKey | None:
    return SETTING_KEY_TO_MODULE.get(setting_key)


def get_snapshot() -> CapabilitySnapshot:
    """返回当前进程内快照。缓存未初始化时 ``cache_ready=False``。"""

    return CapabilitySnapshot(
        desired=dict(_DESIRED),
        generations=dict(_GENERATIONS),
        runtime=dict(_RUNTIME),
        cache_ready=_CACHE_READY,
        last_error=dict(_LAST_ERROR),
        last_transition_at=dict(_LAST_TRANSITION_AT),
    )


def is_module_enabled_cached(module_key: ModuleKey, *, fail_closed: bool = True) -> bool:
    """只读缓存判断。fail_closed 时缓存未就绪视为关闭。"""

    if not _CACHE_READY:
        return False if fail_closed else DEFAULT_ENABLED
    return bool(_DESIRED.get(module_key, DEFAULT_ENABLED))


def is_ai_enabled_cached(*, fail_closed: bool = False) -> bool:
    """兼容 AI 门禁：AI 历史默认开启；缓存未就绪时默认 true，避免旧路径误杀。"""

    if not _CACHE_READY:
        return True if not fail_closed else False
    return bool(_DESIRED.get("ai", DEFAULT_ENABLED))


def get_module_generation_cached(module_key: ModuleKey) -> int:
    return int(_GENERATIONS.get(module_key, 0) or 0)


async def bootstrap_from_db(db: AsyncSession | None = None) -> CapabilitySnapshot:
    """启动时从 DB 预加载并初始化 runtime。"""

    global _CACHE_READY
    async with _CACHE_LOCK:
        # 读取期间先撤销 ready，避免上一次快照或半读取快照在冷启动失败时
        # 被受控入口继续当作可用状态消费。
        _CACHE_READY = False
        if db is not None:
            desired, generations = await _load_desired_from_db(db)
        else:
            async with AsyncSessionLocal() as session:
                desired, generations = await _load_desired_from_db(session)
        _DESIRED.update(desired)
        _GENERATIONS.update(generations)
        _apply_startup_runtime()
        _CACHE_READY = True
        return get_snapshot()


async def refresh_cache_from_db(db: AsyncSession | None = None) -> CapabilitySnapshot:
    """刷新 desired/generation 缓存，不重置 runtime。"""

    global _CACHE_READY
    async with _CACHE_LOCK:
        _CACHE_READY = False
        if db is not None:
            desired, generations = await _load_desired_from_db(db)
        else:
            async with AsyncSessionLocal() as session:
                desired, generations = await _load_desired_from_db(session)
        _DESIRED.update(desired)
        _GENERATIONS.update(generations)
        _CACHE_READY = True
        return get_snapshot()


async def _load_desired_from_db(
    db: AsyncSession,
) -> tuple[dict[ModuleKey, bool], dict[ModuleKey, int]]:
    desired: dict[ModuleKey, bool] = {}
    generations: dict[ModuleKey, int] = {}
    for module_key, meta in MODULE_DEFS.items():
        row = await db.get(SystemSetting, meta["setting_key"])
        normalized = normalize_capability_setting(row.value if row is not None else None)
        desired[module_key] = bool(normalized["enabled"])
        generations[module_key] = int(normalized["generation"])
    return desired, generations


def _apply_startup_runtime() -> None:
    """服务进程重启后 runtime 必须从 starting 重新收敛，不信任旧 ready。"""

    now = _utcnow()
    for module_key in ALL_MODULE_KEYS:
        desired = bool(_DESIRED.get(module_key, DEFAULT_ENABLED))
        _RUNTIME[module_key] = "starting" if desired else "stopped"
        _LAST_ERROR[module_key] = None
        _LAST_TRANSITION_AT[module_key] = now


def _set_runtime(
    module_key: ModuleKey,
    state: RuntimeState,
    *,
    error: str | None = None,
) -> None:
    _RUNTIME[module_key] = state
    _LAST_ERROR[module_key] = error
    _LAST_TRANSITION_AT[module_key] = _utcnow()


async def read_module_desired(
    db: AsyncSession,
    module_key: ModuleKey,
) -> tuple[bool, int]:
    """从 DB 读取模块 desired/generation；缺失时默认 enabled=true, generation=0。"""

    row = await db.get(SystemSetting, module_setting_key(module_key))
    normalized = normalize_capability_setting(row.value if row is not None else None)
    return bool(normalized["enabled"]), int(normalized["generation"])


async def is_module_enabled(db: AsyncSession | None, module_key: ModuleKey) -> bool:
    """带 DB 的启用判断；db=None 时开 session。"""

    if db is not None:
        enabled, _ = await read_module_desired(db, module_key)
        return enabled
    async with AsyncSessionLocal() as session:
        enabled, _ = await read_module_desired(session, module_key)
        return enabled


async def require_module_enabled(
    db: AsyncSession,
    module_key: ModuleKey,
    *,
    http_status: int = 409,
    not_found_when_disabled: bool = False,
) -> None:
    """API 门禁：模块关闭时抛 HTTPException。"""

    from fastapi import HTTPException

    enabled = await is_module_enabled(db, module_key)
    if enabled:
        return
    label = MODULE_DEFS[module_key]["label"]
    status = 404 if not_found_when_disabled else http_status
    raise HTTPException(
        status_code=status,
        detail={
            "code": "PLATFORM_MODULE_DISABLED",
            "message": f"{label} 模块已暂停，请在系统设置的平台能力中重新启用。",
            "module": module_key,
            "reason_code": "platform_module_disabled",
        },
    )


async def set_module_enabled(
    db: AsyncSession,
    module_key: ModuleKey,
    enabled: bool,
    *,
    user_id: int | None = None,
    notify_workers: bool = True,
    apply_local: bool = True,
) -> dict[str, Any]:
    """写入目标状态并执行热切换。

    返回模块状态与 worker 收敛摘要。并发切换串行化，避免重复启停。
    """

    global _CACHE_READY
    if module_key not in MODULE_DEFS:
        raise KeyError(f"unknown module: {module_key}")

    async with _SWITCH_LOCKS[module_key]:
        current_enabled, current_gen = await read_module_desired(db, module_key)
        next_enabled = bool(enabled)
        # 幂等：状态相同仍返回当前快照，但 generation 不前进。
        if current_enabled == next_enabled and _CACHE_READY and _DESIRED[module_key] == next_enabled:
            snap = get_snapshot()
            return {
                "module_key": module_key,
                "desired_enabled": next_enabled,
                "generation": current_gen,
                "runtime_state": snap.runtime.get(module_key, "ready" if next_enabled else "stopped"),
                "worker_convergence": dict(_LAST_WORKER_CONVERGENCE),
                "changed": False,
            }

        next_gen = current_gen + 1
        setting_key = module_setting_key(module_key)
        payload = {"enabled": next_enabled, "generation": next_gen}
        row = await db.get(SystemSetting, setting_key)
        if row is None:
            db.add(SystemSetting(key=setting_key, value=payload))
        else:
            row.value = payload
            flag_modified(row, "value")
        await db.commit()

        async with _CACHE_LOCK:
            _DESIRED[module_key] = next_enabled
            _GENERATIONS[module_key] = next_gen
            _CACHE_READY = True

        if apply_local:
            await _apply_local_transition(module_key, next_enabled)

        worker_conv = (
            await _broadcast_reload_config(
                source="platform_capabilities",
                module_key=module_key,
                generation=next_gen,
                enabled=next_enabled,
            )
            if notify_workers
            else dict(_LAST_WORKER_CONVERGENCE)
        )

        try:
            from . import audit as audit_svc

            await audit_svc.write(
                db,
                user_id,
                "set_platform_capability",
                target=module_key,
                detail={
                    "module": module_key,
                    "enabled": next_enabled,
                    "generation": next_gen,
                    "worker_convergence": {
                        "acked": worker_conv.get("acked"),
                        "offline_or_timeout": worker_conv.get("offline_or_timeout"),
                        "total_accounts": worker_conv.get("total_accounts"),
                    },
                },
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("写入平台能力审计失败 module=%s", module_key)

        return {
            "module_key": module_key,
            "desired_enabled": next_enabled,
            "generation": next_gen,
            "runtime_state": _RUNTIME.get(module_key),
            "last_error": _LAST_ERROR.get(module_key),
            "worker_convergence": worker_conv,
            "changed": True,
        }


async def set_ai_enabled_compat(
    db: AsyncSession,
    enabled: bool,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """供 ``/api/system/settings`` 的 ``ai_enabled`` 兼容委托。"""

    return await set_module_enabled(
        db,
        "ai",
        enabled,
        user_id=user_id,
        notify_workers=True,
        apply_local=True,
    )


async def _apply_local_transition(module_key: ModuleKey, enabled: bool) -> None:
    """主进程本地启停。失败写入 runtime=failed，不回滚 DB 目标状态。"""

    try:
        if enabled:
            _set_runtime(module_key, "starting")
            await _start_module(module_key)
            _set_runtime(module_key, "ready")
        else:
            _set_runtime(module_key, "quiescing")
            await _stop_module(module_key)
            _set_runtime(module_key, "stopped")
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("平台能力本地切换失败 module=%s enabled=%s", module_key, enabled)
        _set_runtime(module_key, "failed", error=msg)


async def _start_module(module_key: ModuleKey) -> None:
    if module_key == "interaction_bot":
        from . import interaction_bot_runtime

        await interaction_bot_runtime.start_interaction_bot_manager()
        return
    if module_key == "ai":
        # AI 无主进程常驻 manager；worker reload 会卸载/加载 Provider。
        return
    if module_key == "webhooks":
        # 入站 Webhook 由进程内缓存门禁；无独立 manager。
        return
    if module_key == "ledger":
        return
    if module_key == "dispatch_debug":
        return


async def _stop_module(module_key: ModuleKey) -> None:
    if module_key == "interaction_bot":
        from . import interaction_bot_runtime

        await interaction_bot_runtime.stop_interaction_bot_manager()
        return
    if module_key == "dispatch_debug":
        await _clear_router_debug_trace_keys()
        return
    if module_key in {"ai", "webhooks", "ledger"}:
        return


async def _clear_router_debug_trace_keys() -> None:
    """关闭命中调试时清理 Redis 临时 router debug trace keys。"""

    prefix = "account_bot:router_debug_trace:"
    try:
        redis = get_redis()
    except Exception:  # noqa: BLE001
        log.debug("清理 router debug trace 时 Redis 不可用", exc_info=True)
        return

    deleted = 0
    try:
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=f"{prefix}*", count=100)
            if keys:
                deleted += int(await redis.delete(*keys))
            if int(cursor) == 0:
                break
    except Exception:  # noqa: BLE001
        log.exception("SCAN/删除 router debug trace keys 失败")
        return
    if deleted:
        log.info("已清理 %d 个 router debug trace 临时 key", deleted)


async def _broadcast_reload_config(
    *,
    source: str,
    module_key: ModuleKey,
    generation: int,
    enabled: bool,
) -> dict[str, Any]:
    """向所有账号 worker 发送 ``CMD_RELOAD_CONFIG``，复用现有 ACK 机制。"""

    notes: list[str] = []
    total = 0
    notified = 0
    acked = 0
    offline = 0
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Account.id).order_by(Account.id.asc()))).all()
            account_ids = [int(r[0]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        notes.append(f"list_accounts_failed: {type(exc).__name__}")
        account_ids = []

    total = len(account_ids)
    if total == 0:
        conv = {
            "total_accounts": 0,
            "notified": 0,
            "acked": 0,
            "pending": 0,
            "offline_or_timeout": 0,
            "last_broadcast_at": _utcnow().isoformat(),
            "notes": notes or ["no_accounts"],
        }
        _LAST_WORKER_CONVERGENCE.clear()
        _LAST_WORKER_CONVERGENCE.update(conv)
        return conv

    try:
        redis = get_redis()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"redis_unavailable: {type(exc).__name__}")
        conv = {
            "total_accounts": total,
            "notified": 0,
            "acked": 0,
            "pending": total,
            "offline_or_timeout": total,
            "last_broadcast_at": _utcnow().isoformat(),
            "notes": notes,
        }
        _LAST_WORKER_CONVERGENCE.clear()
        _LAST_WORKER_CONVERGENCE.update(conv)
        return conv

    payload = {
        "source": source,
        "module_key": module_key,
        "generation": generation,
        "enabled": enabled,
        "platform_capabilities": True,
    }

    def ack_matches_generation(ack_payload: dict[str, Any]) -> bool:
        loaded_generation = ack_payload.get("loaded_generation")
        return (
            ack_payload.get("module_key") == module_key
            and isinstance(loaded_generation, int)
            and not isinstance(loaded_generation, bool)
            and loaded_generation >= generation
            and ack_payload.get("loaded_enabled") is enabled
        )

    for aid in account_ids:
        notified += 1
        try:
            ok = await publish_cmd_with_ack(
                redis,
                aid,
                CMD_RELOAD_CONFIG,
                timeout=3.0,
                ack_validator=ack_matches_generation,
                **payload,
            )
            if ok:
                acked += 1
            else:
                offline += 1
        except Exception as exc:  # noqa: BLE001
            offline += 1
            notes.append(f"account_{aid}:{type(exc).__name__}")

    pending = max(0, total - acked)
    if offline:
        notes.append("offline_or_timeout_will_converge_via_periodic_reconcile")
    conv = {
        "total_accounts": total,
        "notified": notified,
        "acked": acked,
        "pending": pending,
        "offline_or_timeout": offline,
        "last_broadcast_at": _utcnow().isoformat(),
        "notes": notes[:20],
    }
    _LAST_WORKER_CONVERGENCE.clear()
    _LAST_WORKER_CONVERGENCE.update(conv)
    return conv


async def mark_runtime_ready_if_starting(module_key: ModuleKey | None = None) -> None:
    """启动组件成功后，把 starting 收敛为 ready。"""

    keys = [module_key] if module_key else list(ALL_MODULE_KEYS)
    for key in keys:
        if key is None:
            continue
        if not _DESIRED.get(key, DEFAULT_ENABLED):
            if _RUNTIME.get(key) != "stopped":
                _set_runtime(key, "stopped")
            continue
        if _RUNTIME.get(key) in {"starting", "failed"}:
            _set_runtime(key, "ready")


async def reconcile_runtime_after_startup() -> None:
    """主进程启动完成后，按 desired 收敛 runtime。

    interaction_bot 的 manager 是否真正启动由 main lifespan 与本服务启停路径共同决定。
    """

    snap = get_snapshot()
    if not snap.cache_ready:
        return
    for key in ALL_MODULE_KEYS:
        desired = snap.is_enabled(key)
        if not desired:
            _set_runtime(key, "stopped")
            continue
        if key == "interaction_bot":
            # 若 manager 启动失败，main 的 retry 会继续；此处保持 starting 或升为 ready。
            try:
                from . import account_bot_runtime

                # 有任意交互 bot task 或 manager 已初始化即视为 ready；否则 starting。
                running = False
                checker = getattr(account_bot_runtime, "is_interaction_bot_manager_running", None)
                if callable(checker):
                    running = bool(checker())
                else:
                    # 回退：只要 desired 开启且无失败，标记 ready（manager 由 lifespan 负责）。
                    running = True
                _set_runtime(key, "ready" if running else "starting")
            except Exception as exc:  # noqa: BLE001
                _set_runtime(key, "failed", error=f"{type(exc).__name__}: {exc}"[:300])
        else:
            _set_runtime(key, "ready")


def build_status_payload() -> dict[str, Any]:
    """聚合 GET /api/system/capabilities 响应内容。"""

    snap = get_snapshot()
    modules = []
    for key in ALL_MODULE_KEYS:
        meta = MODULE_DEFS[key]
        desired = bool(snap.desired.get(key, DEFAULT_ENABLED)) if snap.cache_ready else DEFAULT_ENABLED
        runtime = snap.runtime.get(key, "starting")
        modules.append(
            {
                "key": key,
                "label": meta["label"],
                "desired_enabled": desired,
                "generation": snap.generation(key) if snap.cache_ready else 0,
                "runtime_state": runtime if snap.cache_ready else "starting",
                "last_error": snap.last_error.get(key),
                "last_transition_at": snap.last_transition_at.get(key),
                "resource_summary": _resource_summary(key, desired=desired, runtime=runtime),
            }
        )

    ai_on = is_module_enabled_cached("ai", fail_closed=True) if snap.cache_ready else False
    interaction_on = is_module_enabled_cached("interaction_bot", fail_closed=True) if snap.cache_ready else False
    webhooks_on = is_module_enabled_cached("webhooks", fail_closed=True) if snap.cache_ready else False
    if not snap.cache_ready:
        webhooks_on = False

    channels = [
        {
            "key": "userbot",
            "label": "Userbot",
            "fixed": True,
            "managed_by": None,
            "available": True,
            "reason_code": None,
            "reason_text": None,
        },
        {
            "key": "interaction_bot",
            "label": "Interaction Bot",
            "fixed": True,
            "managed_by": "interaction_bot",
            "available": bool(interaction_on),
            "reason_code": None if interaction_on else "channel_disabled",
            "reason_text": None if interaction_on else "Interaction Bot 模块已暂停",
        },
        {
            "key": "webhook",
            "label": "Webhook",
            "fixed": True,
            "managed_by": "webhooks",
            "available": bool(webhooks_on),
            "reason_code": None if webhooks_on else "capability_unavailable" if not snap.cache_ready else "channel_disabled",
            "reason_text": None
            if webhooks_on
            else "入站 Webhook 模块已暂停",
        },
    ]

    # 补充 AI 对展示无直接影响 channel，但资源摘要会用
    _ = ai_on

    return {
        "modules": modules,
        "channels": channels,
        "worker_convergence": dict(_LAST_WORKER_CONVERGENCE),
        "cache_ready": snap.cache_ready,
        "updated_at": _utcnow(),
    }


def _resource_summary(module_key: ModuleKey, *, desired: bool, runtime: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "desired_enabled": desired,
        "runtime_state": runtime,
    }
    if module_key == "interaction_bot":
        try:
            from . import account_bot_runtime

            count_fn = getattr(account_bot_runtime, "count_interaction_bot_tasks", None)
            if callable(count_fn):
                summary["polling_tasks"] = int(count_fn())
        except Exception:  # noqa: BLE001
            pass
    return summary


def _normalize_capability_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip()
        if key in MODULE_DEFS and key not in out:
            out.append(key)
    return out


def channel_module_key(channel: str | None) -> ModuleKey | None:
    """固定通道 → 管理模块。userbot 无管理模块。"""

    ch = str(channel or "").strip()
    if ch == "interaction_bot":
        return "interaction_bot"
    if ch == "webhook":
        return "webhooks"
    return None


def subscription_blocked_reason(
    raw: dict[str, Any] | Any,
    *,
    fail_closed_for_public: bool = True,
) -> str | None:
    """Event subscription 是否因平台能力关闭而不可投递。

    返回原因码，None 表示可投递。
    """

    data = raw if isinstance(raw, dict) else getattr(raw, "raw", None)
    data = data if isinstance(data, dict) else {}
    sources = data.get("source") or data.get("sources")
    if sources is None and hasattr(raw, "sources"):
        sources = getattr(raw, "sources", None)
    if isinstance(sources, str):
        source_list = [sources]
    elif isinstance(sources, list):
        source_list = [str(s).strip() for s in sources if str(s).strip()]
    else:
        source_list = []

    requires = _normalize_capability_names(
        data.get("requires_platform_capabilities")
        if isinstance(data, dict)
        else None
    )
    raw_payload = raw.raw if hasattr(raw, "raw") else None
    if not requires and isinstance(raw_payload, dict):
        requires = _normalize_capability_names(
            raw_payload.get("requires_platform_capabilities")
        )

    for req in requires:
        # 公开类模块 fail-closed；AI 等展示用默认开
        fail_closed = fail_closed_for_public and req in {"webhooks"}
        if not is_module_enabled_cached(req, fail_closed=fail_closed):  # type: ignore[arg-type]
            return "platform_module_disabled"

    # 通道源自动依赖
    for source in source_list:
        mod = channel_module_key(source)
        if mod is None:
            continue
        fail_closed = fail_closed_for_public and mod == "webhooks"
        if not is_module_enabled_cached(mod, fail_closed=fail_closed):
            return "channel_disabled"
    return None


def filter_runtime_event_subscriptions(subscriptions: list[Any]) -> list[Any]:
    """按当前平台能力过滤 Event Bus 订阅（运行时投递用）。"""

    kept: list[Any] = []
    for sub in subscriptions:
        reason = subscription_blocked_reason(sub)
        if reason is None:
            kept.append(sub)
    return kept


def is_event_source_delivery_enabled(source: str) -> bool:
    """事件来源是否允许投递。"""

    mod = channel_module_key(source)
    if mod is None:
        return True
    fail_closed = mod == "webhooks"
    return is_module_enabled_cached(mod, fail_closed=fail_closed)


def evaluate_plugin_runtime_availability(
    *,
    permissions: list[str] | None = None,
    capabilities: dict[str, Any] | None = None,
    interaction_entries: list[dict[str, Any]] | None = None,
    event_subscriptions: list[dict[str, Any]] | None = None,
    requires_platform_capabilities: list[str] | None = None,
    preserve_command_trigger: bool = True,
) -> dict[str, Any]:
    """根据当前能力快照计算插件 runtime 可用性（feature matrix 用）。"""

    snap = get_snapshot()
    # feature matrix 展示用：缓存未就绪时按全开推断，避免前端全红。
    def _on(key: ModuleKey) -> bool:
        if not snap.cache_ready:
            return True
        return bool(snap.desired.get(key, DEFAULT_ENABLED))

    perms = {str(p) for p in (permissions or []) if str(p).strip()}
    caps = capabilities if isinstance(capabilities, dict) else {}
    entries = [e for e in (interaction_entries or []) if isinstance(e, dict)]
    subs = [s for s in (event_subscriptions or []) if isinstance(s, dict)]
    plugin_requires = {
        str(x).strip()
        for x in (requires_platform_capabilities or [])
        if str(x).strip() in MODULE_DEFS
    }

    blocked_entries: list[dict[str, Any]] = []
    available_channels: list[str] = []
    blocked_reason_codes: set[str] = set()

    # userbot 始终是固定通道
    available_channels.append("userbot")

    interaction_channel_ok = _on("interaction_bot")
    webhook_channel_ok = _on("webhooks")
    ai_ok = _on("ai")

    if interaction_channel_ok:
        available_channels.append("interaction_bot")
    if webhook_channel_ok:
        available_channels.append("webhook")

    # 插件级硬依赖
    for req in sorted(plugin_requires):
        if not _on(req):  # type: ignore[arg-type]
            blocked_reason_codes.add("platform_module_disabled")
            blocked_entries.append(
                {
                    "scope": "plugin",
                    "key": req,
                    "reason_code": "platform_module_disabled",
                    "reason_text": f"依赖的平台模块 {MODULE_DEFS[req]['label']} 已暂停",  # type: ignore[index]
                }
            )

    # AI 权限/能力依赖（旧插件未声明 requires 时仍通过 runtime 标记）
    needs_ai = bool(perms & {"ai_text", "ai_agent"}) or bool(
        isinstance(caps.get("ai"), dict) or caps.get("ai") is True
    )
    if needs_ai and not ai_ok and "ai" not in plugin_requires:
        blocked_reason_codes.add("capability_unavailable")
        blocked_entries.append(
            {
                "scope": "capability",
                "key": "ai",
                "reason_code": "capability_unavailable",
                "reason_text": "AI 模块已暂停，ctx.ai 与 AI 入口不可用",
            }
        )

    # 入口级裁剪
    for idx, entry in enumerate(entries):
        entry_key = str(entry.get("key") or entry.get("id") or f"entry_{idx}")
        entry_requires = {
            str(x).strip()
            for x in (entry.get("requires_platform_capabilities") or [])
            if str(x).strip() in MODULE_DEFS
        }
        for req in sorted(entry_requires):
            if not _on(req):  # type: ignore[arg-type]
                blocked_reason_codes.add("platform_module_disabled")
                blocked_entries.append(
                    {
                        "scope": "interaction_entry",
                        "key": entry_key,
                        "module": req,
                        "reason_code": "platform_module_disabled",
                        "reason_text": f"入口依赖 {MODULE_DEFS[req]['label']} 已暂停",  # type: ignore[index]
                    }
                )
        send_via = entry.get("send_via") or entry.get("interaction_send_via") or []
        if isinstance(send_via, str):
            send_via = [send_via]
        if not isinstance(send_via, list):
            send_via = []
        via_set = {str(v) for v in send_via if str(v).strip()}
        if via_set and via_set <= {"interaction_bot"} and not interaction_channel_ok:
            blocked_reason_codes.add("channel_disabled")
            blocked_entries.append(
                {
                    "scope": "interaction_entry",
                    "key": entry_key,
                    "reason_code": "channel_disabled",
                    "reason_text": "Interaction Bot 通道已暂停",
                }
            )

    for idx, sub in enumerate(subs):
        sub_key = str(sub.get("key") or sub.get("topic") or f"sub_{idx}")
        source = str(sub.get("source") or "").strip()
        sub_requires = {
            str(x).strip()
            for x in (sub.get("requires_platform_capabilities") or [])
            if str(x).strip() in MODULE_DEFS
        }
        for req in sorted(sub_requires):
            if not _on(req):  # type: ignore[arg-type]
                blocked_reason_codes.add("platform_module_disabled")
                blocked_entries.append(
                    {
                        "scope": "event_subscription",
                        "key": sub_key,
                        "module": req,
                        "reason_code": "platform_module_disabled",
                        "reason_text": f"订阅依赖 {MODULE_DEFS[req]['label']} 已暂停",  # type: ignore[index]
                    }
                )
        if source == "interaction_bot" and not interaction_channel_ok:
            blocked_reason_codes.add("channel_disabled")
            blocked_entries.append(
                {
                    "scope": "event_subscription",
                    "key": sub_key,
                    "reason_code": "channel_disabled",
                    "reason_text": "Interaction Bot 来源事件暂停投递",
                }
            )
        if source == "webhook" and not webhook_channel_ok:
            blocked_reason_codes.add("channel_disabled")
            blocked_entries.append(
                {
                    "scope": "event_subscription",
                    "key": sub_key,
                    "reason_code": "channel_disabled",
                    "reason_text": "Webhook 来源事件暂停投递",
                }
            )

    # 整体可用性
    plugin_hard_blocked = any(
        item.get("scope") == "plugin" for item in blocked_entries
    )
    has_userbot_path = bool(preserve_command_trigger) or any(
        str(e.get("channel") or e.get("source") or "") in {"", "userbot"}
        or "userbot" in str(e.get("send_via") or e.get("interaction_send_via") or "")
        for e in entries
    ) or not entries

    if plugin_hard_blocked:
        runtime_availability = "paused"
    elif blocked_entries and has_userbot_path:
        runtime_availability = "partial"
    elif blocked_entries and not has_userbot_path:
        runtime_availability = "paused"
    elif any(
        snap.runtime.get(k) in {"starting", "quiescing"}
        for k in ALL_MODULE_KEYS
        if snap.cache_ready
    ) and blocked_reason_codes:
        runtime_availability = "transitioning"
    else:
        runtime_availability = "ready"

    primary_reason = next(iter(sorted(blocked_reason_codes)), None)
    return {
        "runtime_availability": runtime_availability,
        "available_channels": available_channels,
        "blocked_entries": blocked_entries,
        "blocked_reason_code": primary_reason,
    }


# 测试辅助：重置进程内状态
def _reset_for_tests() -> None:
    global _CACHE_READY, _LEDGER_DENY_GENERATION, _LEDGER_DENY_NEXT_TOKEN
    _CACHE_READY = False
    for key in ALL_MODULE_KEYS:
        _DESIRED[key] = DEFAULT_ENABLED
        _GENERATIONS[key] = 0
        _RUNTIME[key] = "starting"
        _LAST_ERROR[key] = None
        _LAST_TRANSITION_AT[key] = None
    _LAST_WORKER_CONVERGENCE.clear()
    _LAST_WORKER_CONVERGENCE.update(
        {
            "total_accounts": 0,
            "notified": 0,
            "acked": 0,
            "pending": 0,
            "offline_or_timeout": 0,
            "last_broadcast_at": None,
            "notes": [],
        }
    )
    with _LEDGER_DENY_LOCK:
        _LEDGER_DENY_REGISTRATIONS.clear()
        _LEDGER_DENY_GENERATION = 0
        _LEDGER_DENY_NEXT_TOKEN = 1


__all__ = [
    "ALL_MODULE_KEYS",
    "MODULE_DEFS",
    "CapabilitySnapshot",
    "LEDGER_ACTIONS_FAILED_CLOSED_AUDIT_STATUS",
    "LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE",
    "LedgerActionDenyRegistration",
    "LedgerActionsFailedClosed",
    "bootstrap_from_db",
    "build_status_payload",
    "channel_module_key",
    "evaluate_plugin_runtime_availability",
    "filter_runtime_event_subscriptions",
    "get_module_generation_cached",
    "get_ledger_action_deny_generation",
    "get_snapshot",
    "is_ai_enabled_cached",
    "is_event_source_delivery_enabled",
    "is_module_enabled",
    "is_module_enabled_cached",
    "ledger_action_block_reasons",
    "ledger_action_deny_reasons",
    "ledger_action_deny_registrations",
    "ledger_actions_enabled",
    "mark_runtime_ready_if_starting",
    "module_key_for_setting",
    "module_setting_key",
    "normalize_capability_setting",
    "read_module_desired",
    "register_ledger_action_deny",
    "reconcile_runtime_after_startup",
    "refresh_cache_from_db",
    "require_module_enabled",
    "require_ledger_actions_enabled",
    "set_ai_enabled_compat",
    "set_module_enabled",
    "subscription_blocked_reason",
    "_reset_for_tests",
]
