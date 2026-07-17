"""Action 统一事务执行器：行锁、状态机、幂等与运行时同步。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.base import AsyncSessionLocal
from ...db.models.system_agent import (
    ACTION_STATUS_EXECUTED,
    ACTION_STATUS_EXECUTING,
    ACTION_STATUS_FAILED,
    ACTION_STATUS_PENDING,
    RUNTIME_SYNC_FAILED,
    RUNTIME_SYNC_NOT_REQUIRED,
    RUNTIME_SYNC_PENDING,
    RUNTIME_SYNC_SUCCEEDED,
    SystemAgentAction,
    SystemAgentSession,
)
from ...services import audit
from .actions import action_to_dict, decrypt_secret_payload, mark_expired_if_needed
from .context import ToolContext
from .redactor import summarize_tool_result
from .registry import ActionKeepPendingError, get_registry, role_at_least

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class ActionExecutor:
    """确认执行 pending Action；重复确认返回现有状态。"""

    async def confirm(
        self,
        *,
        action_id: str,
        role: str = "admin",
        channel: str | None = None,
        web_user_id: int | None = None,
        bot_tg_user_id: int | None = None,
    ) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            try:
                action = await self._lock_action(db, action_id)
                if action is None:
                    return {
                        "ok": False,
                        "error_code": "ACTION_NOT_FOUND",
                        "error_message": "操作不存在",
                    }

                # 所有权
                if web_user_id is not None and action.actor_user_id not in (None, web_user_id):
                    return {
                        "ok": False,
                        "error_code": "FORBIDDEN",
                        "error_message": "无权确认此操作",
                        "action": action_to_dict(action),
                    }
                if bot_tg_user_id is not None and action.actor_bot_user_id not in (
                    None,
                    bot_tg_user_id,
                ):
                    return {
                        "ok": False,
                        "error_code": "FORBIDDEN",
                        "error_message": "无权确认此操作",
                        "action": action_to_dict(action),
                    }

                action = await mark_expired_if_needed(db, action)
                if action.status != ACTION_STATUS_PENDING:
                    # 重复确认 / 已终态：直接返回现有状态，不再执行
                    await db.commit()
                    return {
                        "ok": action.status == ACTION_STATUS_EXECUTED,
                        "already_final": True,
                        "action": action_to_dict(action),
                    }

                registry = get_registry()
                spec = registry.get(action.tool_name)
                if spec is None or spec.execute_handler is None:
                    action.status = ACTION_STATUS_FAILED
                    action.error_code = "TOOL_MISSING"
                    action.error_message = f"工具 {action.tool_name} 不可用"
                    action.secret_payload_enc = None
                    action.updated_at = _now()
                    await db.commit()
                    return {
                        "ok": False,
                        "error_code": action.error_code,
                        "error_message": action.error_message,
                        "action": action_to_dict(action),
                    }

                if not role_at_least(role, spec.min_role):
                    return {
                        "ok": False,
                        "error_code": "PERMISSION_DENIED",
                        "error_message": f"需要角色 {spec.min_role} 或更高",
                        "action": action_to_dict(action),
                    }

                secrets = decrypt_secret_payload(action.secret_payload_enc)
                arguments = dict(action.arguments or {})
                arguments.update(secrets)

                session = None
                if action.session_id:
                    session = await db.get(SystemAgentSession, action.session_id)

                ctx = ToolContext(
                    db=db,
                    channel=channel or action.channel,
                    role=role,
                    session=session,
                    account_id=action.account_id,
                    web_user_id=web_user_id if web_user_id is not None else action.actor_user_id,
                    bot_tg_user_id=(
                        bot_tg_user_id
                        if bot_tg_user_id is not None
                        else action.actor_bot_user_id
                    ),
                    action=action,
                )

                # 事务外预检（Provider 上游验证等）：失败保持 pending、清除无效密钥
                if spec.precheck_handler is not None:
                    try:
                        await spec.precheck_handler(ctx, arguments)
                    except ActionKeepPendingError as exc:
                        action.status = ACTION_STATUS_PENDING
                        action.secret_payload_enc = None
                        # 普通 arguments 去掉 has_* 误导（可选）
                        args_pub = dict(action.arguments or {})
                        for name in spec.secret_argument_names or ():
                            args_pub.pop(name, None)
                            args_pub.pop(f"has_{name}", None)
                        action.arguments = args_pub
                        action.secret_fields = None
                        action.error_code = exc.code
                        action.error_message = exc.message[:1000]
                        action.updated_at = _now()
                        await db.commit()
                        return {
                            "ok": False,
                            "keep_pending": True,
                            "error_code": exc.code,
                            "error_message": exc.message,
                            "business_changed": False,
                            "action": action_to_dict(action),
                        }
                    except Exception as exc:  # noqa: BLE001
                        log.exception("action precheck failed id=%s", action.id)
                        action.status = ACTION_STATUS_PENDING
                        action.secret_payload_enc = None
                        action.error_code = type(exc).__name__
                        action.error_message = str(exc)[:500]
                        action.updated_at = _now()
                        await db.commit()
                        return {
                            "ok": False,
                            "keep_pending": True,
                            "error_code": type(exc).__name__,
                            "error_message": str(exc)[:500],
                            "business_changed": False,
                            "action": action_to_dict(action),
                        }

                action.status = ACTION_STATUS_EXECUTING
                action.updated_at = _now()
                action.error_code = None
                action.error_message = None
                await db.flush()

                try:
                    result = await spec.execute_handler(ctx, arguments)
                    safe_result = summarize_tool_result(result, max_chars=4000)
                    if not isinstance(safe_result, dict):
                        safe_result = {"value": safe_result}

                    await audit.write(
                        db,
                        action.actor_user_id,
                        f"system_agent.{action.tool_name}",
                        target=f"action:{action.id}",
                        detail={
                            "tool_name": action.tool_name,
                            "account_id": action.account_id,
                            "summary": action.summary,
                            "result": safe_result,
                        },
                    )

                    action.status = ACTION_STATUS_EXECUTED
                    action.result = safe_result
                    action.error_code = None
                    action.error_message = None
                    action.secret_payload_enc = None
                    action.executed_at = _now()
                    action.updated_at = _now()
                    if spec.runtime_effects:
                        action.runtime_sync_status = RUNTIME_SYNC_PENDING
                    else:
                        action.runtime_sync_status = RUNTIME_SYNC_NOT_REQUIRED
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    log.exception("action execute failed id=%s tool=%s", action.id, action.tool_name)
                    await db.rollback()
                    await self._mark_failed(
                        action_id,
                        error_code=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )
                    async with AsyncSessionLocal() as db2:
                        failed = await db2.get(SystemAgentAction, action_id)
                        return {
                            "ok": False,
                            "error_code": type(exc).__name__,
                            "error_message": str(exc)[:500],
                            "business_changed": False,
                            "action": action_to_dict(failed) if failed else None,
                        }

                # commit 后运行时同步
                if spec.runtime_effects:
                    await self._run_runtime_sync(action_id, list(spec.runtime_effects))

                async with AsyncSessionLocal() as db3:
                    final = await db3.get(SystemAgentAction, action_id)
                    return {
                        "ok": True,
                        "action": action_to_dict(final) if final else action_to_dict(action),
                    }
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                log.exception("action confirm outer failure id=%s", action_id)
                return {
                    "ok": False,
                    "error_code": "CONFIRM_FAILED",
                    "error_message": str(exc)[:500],
                }

    async def retry_runtime_sync(self, action_id: str) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return {"ok": False, "error_code": "ACTION_NOT_FOUND", "error_message": "操作不存在"}
            if action.status != ACTION_STATUS_EXECUTED:
                return {
                    "ok": False,
                    "error_code": "INVALID_STATUS",
                    "error_message": "仅已执行的操作可重新同步",
                    "action": action_to_dict(action),
                }
            registry = get_registry()
            spec = registry.get(action.tool_name)
            effects = list(spec.runtime_effects) if spec else []
            if not effects:
                action.runtime_sync_status = RUNTIME_SYNC_NOT_REQUIRED
                await db.commit()
                return {"ok": True, "action": action_to_dict(action)}
            action.runtime_sync_status = RUNTIME_SYNC_PENDING
            action.runtime_sync_error = None
            await db.commit()
        await self._run_runtime_sync(action_id, effects)
        async with AsyncSessionLocal() as db2:
            final = await db2.get(SystemAgentAction, action_id)
            return {"ok": True, "action": action_to_dict(final) if final else None}

    async def _lock_action(self, db: AsyncSession, action_id: str) -> SystemAgentAction | None:
        q = select(SystemAgentAction).where(SystemAgentAction.id == action_id)
        try:
            q = q.with_for_update()
        except Exception:  # noqa: BLE001
            pass
        result = await db.execute(q)
        return result.scalar_one_or_none()

    async def _mark_failed(
        self,
        action_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return
            action.status = ACTION_STATUS_FAILED
            action.error_code = error_code
            action.error_message = error_message
            action.secret_payload_enc = None
            action.updated_at = _now()
            await db.commit()

    async def _run_runtime_sync(self, action_id: str, effects: list[str]) -> None:
        errors: list[str] = []
        account_id: int | None = None
        arguments: dict[str, Any] = {}
        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return
            account_id = action.account_id
            arguments = dict(action.arguments or {})

        for effect in effects:
            try:
                await self._apply_effect(
                    effect,
                    account_id=account_id,
                    action_id=action_id,
                    arguments=arguments,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "runtime sync failed action=%s effect=%s: %s",
                    action_id,
                    effect,
                    exc,
                    exc_info=True,
                )
                errors.append(f"{effect}: {str(exc)[:200]}")

        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return
            if errors:
                action.runtime_sync_status = RUNTIME_SYNC_FAILED
                action.runtime_sync_error = "; ".join(errors)[:1000]
            else:
                action.runtime_sync_status = RUNTIME_SYNC_SUCCEEDED
                action.runtime_sync_error = None
            action.updated_at = _now()
            await db.commit()

    async def _apply_effect(
        self,
        effect: str,
        *,
        account_id: int | None,
        action_id: str,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        args = arguments or {}
        if effect == "pause_or_resume_worker":
            effect = "pause_worker" if bool(args.get("paused")) else "resume_worker"

        if effect in {"reload_worker", "worker_reload", "reload_config"}:
            if account_id is None:
                return
            from ...redis_client import get_redis
            from ...worker.ipc import CMD_RELOAD_CONFIG, publish_cmd_with_ack

            redis = get_redis()
            await publish_cmd_with_ack(redis, int(account_id), CMD_RELOAD_CONFIG)
            return

        if effect in {"pause_worker", "stop_worker"}:
            if account_id is None:
                return
            try:
                from ...worker import supervisor

                await supervisor.stop_worker(int(account_id))
            except Exception:  # noqa: BLE001
                from ...redis_client import get_redis
                from ...worker.ipc import CMD_PAUSE, cmd_channel, make_cmd

                redis = get_redis()
                await redis.publish(cmd_channel(int(account_id)), make_cmd(CMD_PAUSE))
            return

        if effect in {"resume_worker", "start_worker"}:
            if account_id is None:
                return
            try:
                from ...worker import supervisor

                await supervisor.start_worker(int(account_id))
            except Exception:  # noqa: BLE001
                from ...redis_client import get_redis
                from ...worker.ipc import CMD_RESUME, GLOBAL_CHANNEL, cmd_channel, make_cmd

                redis = get_redis()
                await redis.publish(cmd_channel(int(account_id)), make_cmd(CMD_RESUME))
                await redis.publish(GLOBAL_CHANNEL, make_cmd("start_worker", account_id=int(account_id)))
            return

        if effect == "restart_worker":
            if account_id is None:
                return
            try:
                from ...worker import supervisor

                await supervisor.stop_worker(int(account_id))
                await supervisor.start_worker(int(account_id))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"restart_worker failed: {exc}") from exc
            return

        log.debug("unknown runtime effect %s action=%s", effect, action_id)


_EXECUTOR: ActionExecutor | None = None


def get_action_executor() -> ActionExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ActionExecutor()
    return _EXECUTOR


__all__ = ["ActionExecutor", "get_action_executor"]
