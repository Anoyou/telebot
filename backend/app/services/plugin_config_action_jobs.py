"""Background jobs for generic plugin configuration actions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.base import AsyncSessionLocal
from ..db.models.account import Account
from ..db.models.feature import AccountFeature, Feature
from ..db.models.log import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARN,
    PluginConfigActionJob,
    RuntimeLog,
)
from ..db.models.plugin import InstalledPlugin
from ..db.models.plugin_global_config import PluginGlobalConfig
from ..schemas.feature import PluginConfigActionJobLogItem, PluginConfigActionJobResponse
from ..services.redactor import redact_text, redact_value
from ..worker.plugins.ai_facade import AIQuotaError, AIUnavailableError
from ..worker.plugins.http_facade import PluginHTTPError
from . import feature_service
from .plugin_config_actions import (
    PluginConfigActionError,
    PluginConfigActionNotFound,
    PluginConfigActionUnavailable,
    declared_config_actions,
    run_plugin_config_action,
)
from .plugin_config_secrets import (
    config_secret_values,
    decrypt_config_secrets,
    mask_config_secrets,
    preserve_masked_config_secrets,
    redact_exact_secrets,
)

log = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_PAUSED = "paused"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_PAUSED, STATUS_CANCELLED})
INTERRUPTED_ERROR_CODE = "CONFIG_ACTION_INTERRUPTED"
_ACTIVE_TASKS: set[asyncio.Task[Any]] = set()
_ACTIVE_TASKS_BY_JOB_ID: dict[str, asyncio.Task[Any]] = {}
_CREATE_LOCKS_BY_ACTION: dict[tuple[int, str, str], asyncio.Lock] = {}
_MASKED_PATCH_MARKER = "__telepilot_schema_masked_v1__"


def _string_candidates(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_candidates(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _string_candidates(child)]
    if isinstance(value, str) and len(value) >= 8:
        return [value]
    return []


async def startup_plugin_config_action_jobs() -> int:
    """Fail stale in-process jobs left behind by a previous web process."""

    return await _converge_interrupted_jobs("服务重启，未完成的配置动作已终止，请重新执行。")


async def shutdown_plugin_config_action_jobs() -> int:
    """Cancel owned tasks, await them, then converge any non-terminal rows."""

    tasks = list(_ACTIVE_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return await _converge_interrupted_jobs("服务关闭，未完成的配置动作已终止，请重新执行。")


async def _converge_interrupted_jobs(message: str) -> int:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(PluginConfigActionJob).where(
                    PluginConfigActionJob.status.in_((STATUS_QUEUED, STATUS_RUNNING))
                )
            )
        ).scalars().all()
        now = _utcnow()
        for job in rows:
            job.status = STATUS_FAILED
            job.message = message
            job.error_code = INTERRUPTED_ERROR_CODE
            job.error_message = message
            job.ended_at = now
            job.updated_at = now
            await _write_runtime_log(
                db,
                job,
                LEVEL_ERROR,
                message,
                step="interrupted",
                error_code=INTERRUPTED_ERROR_CODE,
            )
        if rows:
            await db.commit()
        return len(rows)


def _start_job_task(job_id: str, coro: Any) -> None:
    task = asyncio.create_task(coro)
    if not isinstance(task, asyncio.Task):
        return
    _ACTIVE_TASKS.add(task)
    _ACTIVE_TASKS_BY_JOB_ID[job_id] = task

    def _done(completed: asyncio.Task[Any]) -> None:
        _ACTIVE_TASKS.discard(completed)
        if _ACTIVE_TASKS_BY_JOB_ID.get(job_id) is completed:
            _ACTIVE_TASKS_BY_JOB_ID.pop(job_id, None)
        if completed.cancelled():
            return
        try:
            error = completed.exception()
        except Exception:  # noqa: BLE001
            log.exception("读取插件配置任务结果失败")
            return
        if error is not None:
            log.error(
                "插件配置后台任务未捕获异常: %s: %s",
                type(error).__name__,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_done)


async def create_plugin_config_action_job(
    db: AsyncSession,
    *,
    account: Account,
    feature: Feature,
    action_key: str,
    effective_config: Mapping[str, Any],
    current_config: Mapping[str, Any] | None = None,
    action_input: Mapping[str, Any] | None = None,
    installed_plugin: InstalledPlugin | Mapping[str, Any] | None = None,
) -> PluginConfigActionJob:
    """Create and start a background config action job."""

    key = str(action_key or "").strip()
    if not any(str(action.get("key") or "").strip() == key for action in declared_config_actions(feature, installed_plugin)):
        raise PluginConfigActionNotFound(f"插件 {feature.key} 未声明配置动作 {key}")
    lock_key = (int(account.id), str(feature.key), key)
    lock = _CREATE_LOCKS_BY_ACTION.setdefault(lock_key, asyncio.Lock())
    async with lock:
        return await _create_plugin_config_action_job_locked(
            db,
            account=account,
            feature=feature,
            action_key=key,
            effective_config=effective_config,
            current_config=current_config,
            action_input=action_input,
        )


async def _create_plugin_config_action_job_locked(
    db: AsyncSession,
    *,
    account: Account,
    feature: Feature,
    action_key: str,
    effective_config: Mapping[str, Any],
    current_config: Mapping[str, Any] | None,
    action_input: Mapping[str, Any] | None,
) -> PluginConfigActionJob:
    """Check and create one job while the process-local action key is locked."""

    key = action_key
    active_result = await db.execute(
        select(PluginConfigActionJob).where(
            PluginConfigActionJob.account_id == account.id,
            PluginConfigActionJob.plugin_key == feature.key,
            PluginConfigActionJob.action_key == key,
            PluginConfigActionJob.status.in_((STATUS_QUEUED, STATUS_RUNNING)),
        )
    )
    active_job = active_result.scalars().first()
    if active_job is not None:
        raise PluginConfigActionUnavailable(
            f"配置动作正在执行（任务 {active_job.job_id}），请先中断或终止后再重新开始"
        )

    job = PluginConfigActionJob(
        job_id=f"pcaj_{uuid.uuid4().hex}",
        account_id=account.id,
        plugin_key=feature.key,
        action_key=key,
        status=STATUS_QUEUED,
        message="配置动作已排队",
        input_preview=redact_value(dict(action_input or {})),
        result={},
        config_patch={},
    )
    db.add(job)
    await db.flush()
    await _write_runtime_log(
        db,
        job,
        LEVEL_INFO,
        "配置动作已排队",
        step="queued",
    )
    await db.commit()
    await db.refresh(job)

    _start_job_task(
        job.job_id,
        _run_plugin_config_action_job(
            job.job_id,
            effective_config=dict(effective_config or {}),
            current_config=dict(current_config or {}),
            action_input=dict(action_input or {}),
        )
    )
    return job


async def control_plugin_config_action_job(
    db: AsyncSession,
    job_id: str,
    *,
    action: str,
) -> PluginConfigActionJobResponse | None:
    """Pause or cancel one in-process background config action."""

    mode = str(action or "").strip().lower()
    if mode not in {"pause", "cancel"}:
        raise ValueError("配置动作控制只支持 pause 或 cancel")
    job = await _load_job(db, job_id)
    if job is None:
        return None
    if job.status in TERMINAL_STATUSES:
        return job_response(job, logs=await _load_job_logs(db, job.job_id))

    task = _ACTIVE_TASKS_BY_JOB_ID.get(job.job_id)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await db.refresh(job)
    if job.status in TERMINAL_STATUSES:
        return job_response(job, logs=await _load_job_logs(db, job.job_id))

    now = _utcnow()
    paused = mode == "pause"
    job.status = STATUS_PAUSED if paused else STATUS_CANCELLED
    job.message = "配置动作已中断，可调整配置后继续执行。" if paused else "配置动作已终止。"
    job.error_code = "CONFIG_ACTION_PAUSED" if paused else "CONFIG_ACTION_CANCELLED"
    job.error_message = None
    job.ended_at = now
    job.updated_at = now
    await _write_runtime_log(
        db,
        job,
        LEVEL_WARN,
        job.message,
        step="paused" if paused else "cancelled",
        error_code=job.error_code,
    )
    await db.commit()
    return job_response(job, logs=await _load_job_logs(db, job.job_id))


async def get_plugin_config_action_job(
    db: AsyncSession,
    job_id: str,
    *,
    include_logs: bool = True,
) -> PluginConfigActionJobResponse | None:
    """Return job status with process logs."""

    job = await _load_job(db, job_id)
    if job is None:
        return None
    logs = await _load_job_logs(db, job.job_id) if include_logs else []
    return job_response(job, logs=logs)


async def list_plugin_config_action_jobs(
    db: AsyncSession,
    *,
    account_id: int,
    plugin_key: str,
    limit: int = 10,
) -> list[PluginConfigActionJobResponse]:
    """Return recent config action jobs for one account/plugin pair."""

    safe_limit = min(max(int(limit or 10), 1), 50)
    rows = (
        await db.execute(
            select(PluginConfigActionJob)
            .where(
                PluginConfigActionJob.account_id == account_id,
                PluginConfigActionJob.plugin_key == plugin_key,
            )
            .order_by(desc(PluginConfigActionJob.created_at), desc(PluginConfigActionJob.id))
            .limit(safe_limit)
        )
    ).scalars().all()
    return [job_response(row, logs=[]) for row in rows]


def job_response(
    job: PluginConfigActionJob,
    *,
    logs: list[RuntimeLog] | None = None,
) -> PluginConfigActionJobResponse:
    """Convert a job row to API response."""

    stored_patch = dict(job.config_patch or {})
    schema_masked = stored_patch.pop(_MASKED_PATCH_MARKER, False) is True
    public_patch = (
        redact_value(stored_patch)
        if schema_masked
        else {str(key): "***" for key in stored_patch}
    )
    return PluginConfigActionJobResponse(
        job_id=job.job_id,
        account_id=job.account_id,
        plugin_key=job.plugin_key,
        action_key=job.action_key,
        status=job.status,
        message=redact_text(job.message or "") or None,
        error_code=job.error_code,
        error_message=redact_text(job.error_message or "") or None,
        result=redact_value(job.result or {}),
        # 新行落库前已按 schema 遮罩；旧行无法证明安全，只返回键。
        config_patch=public_patch,
        created_at=job.created_at,
        started_at=job.started_at,
        ended_at=job.ended_at,
        updated_at=job.updated_at,
        logs=[_log_item(row) for row in logs or []],
    )


async def _run_plugin_config_action_job(
    job_id: str,
    *,
    effective_config: dict[str, Any],
    current_config: dict[str, Any],
    action_input: dict[str, Any],
) -> None:
    async with AsyncSessionLocal() as db:
        job = await _load_job(db, job_id)
        if job is None:
            log.warning("plugin config action job disappeared job_id=%s", job_id)
            return
        now = _utcnow()
        job.status = STATUS_RUNNING
        job.started_at = now
        job.updated_at = now
        job.message = "开始执行配置动作"
        await _write_runtime_log(db, job, LEVEL_INFO, "开始执行配置动作", step="start")
        await db.commit()

        account = await db.get(Account, job.account_id)
        feature = await db.get(Feature, job.plugin_key)
        installed_plugin = await db.get(InstalledPlugin, job.plugin_key)
        if account is None or feature is None:
            await _fail_job(
                db,
                job,
                code="CONFIG_ACTION_TARGET_MISSING",
                message="账号或插件不存在，无法执行配置动作",
            )
            return
        raw_schema = (
            feature.manifest.get("config_schema")
            if isinstance(feature.manifest, dict)
            and isinstance(feature.manifest.get("config_schema"), dict)
            else None
        )
        known_secrets = list(
            dict.fromkeys(
                [
                    *config_secret_values(effective_config, schema=raw_schema),
                    *config_secret_values(current_config, schema=raw_schema),
                    # action_input 整体来自加密 Action payload；其插件级 input schema
                    # 不一定声明 sensitive，保守按值脱敏长字符串。
                    *_string_candidates(action_input),
                ]
            )
        )

        async def write_progress(level: str = LEVEL_INFO, message: str = "", **detail: Any) -> None:
            normalized = _normalize_level(level)
            safe_message = str(redact_exact_secrets(str(message or ""), known_secrets) or "")
            safe_detail = redact_exact_secrets(detail, known_secrets)
            await _write_runtime_log(
                db,
                job,
                normalized,
                safe_message,
                **(safe_detail if isinstance(safe_detail, dict) else {}),
            )
            job.message = safe_message[:1000] or job.message
            job.updated_at = _utcnow()
            await db.commit()

        try:
            result = await run_plugin_config_action(
                db,
                account=account,
                feature=feature,
                action_key=job.action_key,
                effective_config=effective_config,
                current_config=current_config,
                action_input=action_input,
                installed_plugin=installed_plugin,
                log=write_progress,
            )
        except Exception as exc:  # noqa: BLE001 - map plugin/runtime failures to job state
            code, message, status_level = _exception_detail(exc)
            await _fail_job(db, job, code=code, message=message, log_level=status_level)
            return

        patch = result.get("config_patch") if isinstance(result.get("config_patch"), Mapping) else {}
        applied_patch_keys: list[str] = []
        if patch:
            try:
                applied_patch_keys = await _apply_config_patch(db, job, feature, patch)
            except Exception as exc:  # noqa: BLE001 - persist schema/apply failures as job failures
                code, message, status_level = _exception_detail(exc)
                await _fail_job(db, job, code=code, message=message, log_level=status_level)
                return
        now = _utcnow()
        job.status = STATUS_SUCCEEDED
        success_text = redact_exact_secrets(
            str(result.get("toast") or result.get("message") or "配置动作已完成"),
            known_secrets,
        )
        job.message = _success_message(
            str(success_text),
            auto_saved=bool(applied_patch_keys),
        )
        job.error_code = None
        job.error_message = None
        safe_result = redact_exact_secrets(result.get("result") or {}, known_secrets)
        job.result = redact_value(safe_result) if isinstance(safe_result, dict) else {}
        raw_schema = (
            feature.manifest.get("config_schema")
            if isinstance(feature.manifest, dict)
            and isinstance(feature.manifest.get("config_schema"), dict)
            else None
        )
        job.config_patch = {
            _MASKED_PATCH_MARKER: True,
            **mask_config_secrets(dict(patch or {}), schema=raw_schema),
        }
        job.ended_at = now
        job.updated_at = now
        await _write_runtime_log(
            db,
            job,
            LEVEL_INFO,
            job.message or "配置动作已完成",
            step="finish",
            config_patch_keys=sorted(str(key) for key in patch),
            applied_config_keys=applied_patch_keys,
        )
        await db.commit()


async def _fail_job(
    db: AsyncSession,
    job: PluginConfigActionJob,
    *,
    code: str,
    message: str,
    log_level: str = LEVEL_ERROR,
) -> None:
    now = _utcnow()
    safe_message = str(message or "配置动作失败")[:2000]
    job.status = STATUS_FAILED
    job.message = safe_message
    job.error_code = code
    job.error_message = safe_message
    job.ended_at = now
    job.updated_at = now
    await _write_runtime_log(db, job, log_level, safe_message, step="failed", error_code=code)
    await db.commit()


async def _load_job(db: AsyncSession, job_id: str) -> PluginConfigActionJob | None:
    value = str(job_id or "").strip()
    if not value:
        return None
    return (
        await db.execute(select(PluginConfigActionJob).where(PluginConfigActionJob.job_id == value))
    ).scalar_one_or_none()


async def _load_job_logs(db: AsyncSession, job_id: str) -> list[RuntimeLog]:
    rows = (
        await db.execute(
            select(RuntimeLog)
            .where(RuntimeLog.detail["config_action_job_id"].as_string() == job_id)
            .order_by(RuntimeLog.ts.asc(), RuntimeLog.id.asc())
            .limit(500)
        )
    ).scalars().all()
    return list(rows)


async def _apply_config_patch(
    db: AsyncSession,
    job: PluginConfigActionJob,
    feature: Feature,
    patch: Mapping[str, Any],
) -> list[str]:
    account_patch, global_patch = _split_config_patch(feature, patch)
    raw_schema = (
        feature.manifest.get("config_schema")
        if isinstance(feature.manifest, dict)
        and isinstance(feature.manifest.get("config_schema"), dict)
        else None
    )
    applied_keys: list[str] = []
    if account_patch:
        _validate_config_patch(feature, account_patch, "account")
        existing = (
            await db.execute(
                select(AccountFeature).where(
                    AccountFeature.account_id == job.account_id,
                    AccountFeature.feature_key == job.plugin_key,
                )
            )
        ).scalar_one_or_none()
        current = decrypt_config_secrets(
            dict(existing.config or {}) if existing is not None else {},
            schema=raw_schema,
            strict=True,
        )
        safe_patch = preserve_masked_config_secrets(
            current,
            account_patch,
            schema=raw_schema,
        )
        merged = {**current, **safe_patch}
        await feature_service.set_account_feature(
            db,
            job.account_id,
            job.plugin_key,
            enabled=bool(existing.enabled) if existing is not None else True,
            config=merged,
            notify=False,
            commit=False,
        )
        applied_keys.extend(sorted(account_patch.keys()))

    if global_patch:
        _validate_config_patch(feature, global_patch, "global")
        row = await db.get(PluginGlobalConfig, job.plugin_key)
        current = decrypt_config_secrets(
            dict(row.config or {}) if row is not None else {},
            schema=raw_schema,
            strict=True,
        )
        safe_patch = preserve_masked_config_secrets(
            current,
            global_patch,
            schema=raw_schema,
        )
        merged = {**current, **safe_patch}
        await feature_service.set_plugin_global_config(
            db,
            job.plugin_key,
            merged,
            notify=False,
            commit=False,
        )
        applied_keys.extend(sorted(global_patch.keys()))

    if applied_keys:
        await _write_runtime_log(
            db,
            job,
            LEVEL_INFO,
            "配置补丁已自动保存",
            step="apply_config_patch",
            config_patch_keys=applied_keys,
        )
        await db.commit()
        if account_patch:
            await feature_service._notify_reload(job.account_id)  # noqa: SLF001 - shared service boundary
        if global_patch:
            await feature_service._notify_all_accounts_using_feature(db, job.plugin_key)  # noqa: SLF001
    return applied_keys


def _split_config_patch(
    feature: Feature,
    patch: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    config_schema = (feature.manifest or {}).get("config_schema")
    properties = config_schema.get("properties") if isinstance(config_schema, dict) else None
    account_patch: dict[str, Any] = {}
    global_patch: dict[str, Any] = {}
    for key, value in patch.items():
        item_key = str(key)
        prop = properties.get(item_key) if isinstance(properties, dict) else None
        if isinstance(prop, dict) and prop.get("level") == "global":
            global_patch[item_key] = value
        else:
            account_patch[item_key] = value
    return account_patch, global_patch


def _validate_config_patch(
    feature: Feature,
    patch: Mapping[str, Any],
    scope: str,
) -> None:
    config_schema = (feature.manifest or {}).get("config_schema")
    if not isinstance(config_schema, dict) or not patch:
        return
    patch_schema = _config_patch_schema(config_schema, patch.keys(), scope)
    validation = feature_service.validate_config_against_schema(dict(patch), patch_schema)
    if not validation.valid:
        detail = "; ".join(f"{e.field}: {e.message}" for e in validation.errors)
        raise PluginConfigActionError(f"配置动作生成的配置补丁不符合插件 schema：{detail}")


def _config_patch_schema(
    config_schema: dict[str, Any],
    keys: Any,
    scope: str,
) -> dict[str, Any]:
    scoped = feature_service.config_schema_for_scope(config_schema, scope)
    patch_keys = {str(key) for key in keys}
    schema = deepcopy(scoped)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["properties"] = {key: value for key, value in properties.items() if key in patch_keys}
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [key for key in required if isinstance(key, str) and key in patch_keys]
    return schema


def _success_message(message: str, *, auto_saved: bool) -> str:
    text = (message or "配置动作已完成").strip()
    if not auto_saved:
        return text
    for suffix in ("，请保存配置后生效。", "，请保存配置后生效", "请保存配置后生效。", "请保存配置后生效"):
        text = text.replace(suffix, "").strip()
    if text.endswith("。"):
        text = text[:-1]
    return f"{text}，已自动保存并通知插件热加载。"


async def _write_runtime_log(
    db: AsyncSession,
    job: PluginConfigActionJob,
    level: str,
    message: str,
    **detail: Any,
) -> None:
    db.add(
        RuntimeLog(
            account_id=job.account_id,
            level=_normalize_level(level),
            source="plugin",
            message=redact_text(str(message or "")) or "",
            detail=redact_value(
                {
                    **detail,
                    "plugin_key": job.plugin_key,
                    "action_key": job.action_key,
                    "config_action_job_id": job.job_id,
                    "component": "plugin_config_action",
                }
            ),
        )
    )


def _exception_detail(exc: Exception) -> tuple[str, str, str]:
    if isinstance(exc, PluginConfigActionNotFound):
        return "CONFIG_ACTION_NOT_FOUND", "插件配置动作不存在", LEVEL_WARN
    if isinstance(exc, PluginConfigActionUnavailable):
        return "CONFIG_ACTION_UNAVAILABLE", "插件配置动作当前不可用", LEVEL_WARN
    if isinstance(exc, PluginHTTPError):
        return "CONFIG_ACTION_HTTP_REJECTED", "插件配置动作的 HTTP 请求失败（详情已脱敏）", LEVEL_WARN
    if isinstance(exc, AIQuotaError):
        return "CONFIG_ACTION_AI_QUOTA", "插件配置动作的 AI 配额不足", LEVEL_WARN
    if isinstance(exc, AIUnavailableError):
        return "CONFIG_ACTION_AI_UNAVAILABLE", "插件配置动作的 AI 服务不可用", LEVEL_ERROR
    if isinstance(exc, PluginConfigActionError):
        return "CONFIG_ACTION_FAILED", "插件配置动作执行失败（详情已脱敏）", LEVEL_WARN
    return (
        "CONFIG_ACTION_FAILED",
        f"插件配置动作执行异常（{type(exc).__name__}，详情已脱敏）",
        LEVEL_ERROR,
    )


def _log_item(row: RuntimeLog) -> PluginConfigActionJobLogItem:
    return PluginConfigActionJobLogItem(
        id=row.id,
        ts=row.ts,
        level=row.level,
        message=redact_text(row.message or "") or "",
        detail=redact_value(row.detail) if row.detail is not None else None,
    )


def _normalize_level(level: str) -> str:
    value = str(level or LEVEL_INFO).lower()
    if value == "warning":
        return LEVEL_WARN
    if value in {LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR, "debug"}:
        return value
    return LEVEL_INFO


def _utcnow() -> datetime:
    return datetime.now(UTC)
