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
from .actions import (
    action_to_dict,
    bot_owns_action,
    clear_action_secrets,
    decrypt_secret_payload,
    mark_expired_if_needed,
    web_owns_action,
)
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
        # ── 阶段 1：无锁读取 + 所有权 ───────────────────────────
        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return {
                    "ok": False,
                    "error_code": "ACTION_NOT_FOUND",
                    "error_message": "操作不存在",
                }
            if web_user_id is not None and not web_owns_action(action, web_user_id):
                return {
                    "ok": False,
                    "error_code": "FORBIDDEN",
                    "error_message": "无权确认此操作",
                    "action": action_to_dict(action),
                }
            if bot_tg_user_id is not None and not bot_owns_action(action, bot_tg_user_id):
                return {
                    "ok": False,
                    "error_code": "FORBIDDEN",
                    "error_message": "无权确认此操作",
                    "action": action_to_dict(action),
                }
            action = await mark_expired_if_needed(db, action)
            if action.status != ACTION_STATUS_PENDING:
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
                clear_action_secrets(action)
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
            session_id = action.session_id
            account_id = action.account_id
            channel_eff = channel or action.channel
            actor_web = web_user_id if web_user_id is not None else action.actor_user_id
            actor_bot = (
                bot_tg_user_id if bot_tg_user_id is not None else action.actor_bot_user_id
            )
            secret_names = tuple(spec.secret_argument_names or ())
            tool_name = action.tool_name
            summary = action.summary

        # ── 阶段 2：无锁预检（可含上游网络 I/O）────────────────
        if spec.precheck_handler is not None:
            async with AsyncSessionLocal() as pre_db:
                session = await pre_db.get(SystemAgentSession, session_id) if session_id else None
                pre_ctx = ToolContext(
                    db=pre_db,
                    channel=channel_eff,
                    role=role,
                    session=session,
                    account_id=account_id,
                    web_user_id=actor_web,
                    bot_tg_user_id=actor_bot,
                    action=None,
                )
                try:
                    await spec.precheck_handler(pre_ctx, arguments)
                except ActionKeepPendingError as exc:
                    async with AsyncSessionLocal() as db2:
                        row = await db2.get(SystemAgentAction, action_id)
                        if row is not None and row.status == ACTION_STATUS_PENDING:
                            clear_action_secrets(row, secret_names)
                            row.error_code = exc.code
                            row.error_message = exc.message[:1000]
                            row.updated_at = _now()
                            await db2.commit()
                            return {
                                "ok": False,
                                "keep_pending": True,
                                "error_code": exc.code,
                                "error_message": exc.message,
                                "business_changed": False,
                                "action": action_to_dict(row),
                            }
                    return {
                        "ok": False,
                        "keep_pending": True,
                        "error_code": exc.code,
                        "error_message": exc.message,
                        "business_changed": False,
                    }
                except Exception as exc:  # noqa: BLE001
                    log.exception("action precheck failed id=%s", action_id)
                    async with AsyncSessionLocal() as db2:
                        row = await db2.get(SystemAgentAction, action_id)
                        if row is not None and row.status == ACTION_STATUS_PENDING:
                            clear_action_secrets(row, secret_names)
                            row.error_code = type(exc).__name__
                            row.error_message = str(exc)[:500]
                            row.updated_at = _now()
                            await db2.commit()
                            return {
                                "ok": False,
                                "keep_pending": True,
                                "error_code": type(exc).__name__,
                                "error_message": str(exc)[:500],
                                "business_changed": False,
                                "action": action_to_dict(row),
                            }
                    return {
                        "ok": False,
                        "keep_pending": True,
                        "error_code": type(exc).__name__,
                        "error_message": str(exc)[:500],
                        "business_changed": False,
                    }

        # ── 阶段 3：行锁 + 业务执行 ─────────────────────────────
        async with AsyncSessionLocal() as db:
            try:
                action = await self._lock_action(db, action_id)
                if action is None:
                    return {
                        "ok": False,
                        "error_code": "ACTION_NOT_FOUND",
                        "error_message": "操作不存在",
                    }
                if web_user_id is not None and not web_owns_action(action, web_user_id):
                    return {
                        "ok": False,
                        "error_code": "FORBIDDEN",
                        "error_message": "无权确认此操作",
                        "action": action_to_dict(action),
                    }
                if bot_tg_user_id is not None and not bot_owns_action(action, bot_tg_user_id):
                    return {
                        "ok": False,
                        "error_code": "FORBIDDEN",
                        "error_message": "无权确认此操作",
                        "action": action_to_dict(action),
                    }
                action = await mark_expired_if_needed(db, action)
                if action.status != ACTION_STATUS_PENDING:
                    await db.commit()
                    return {
                        "ok": action.status == ACTION_STATUS_EXECUTED,
                        "already_final": True,
                        "action": action_to_dict(action),
                    }

                # 重新解密（预检期间用户可能 secret-input）
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
                        f"system_agent.{tool_name}",
                        target=f"action:{action.id}",
                        detail={
                            "tool_name": tool_name,
                            "account_id": action.account_id,
                            "summary": summary,
                            "result": safe_result,
                        },
                    )

                    action.status = ACTION_STATUS_EXECUTED
                    action.result = safe_result
                    action.error_code = None
                    action.error_message = None
                    clear_action_secrets(action, secret_names)
                    action.executed_at = _now()
                    action.updated_at = _now()
                    if spec.runtime_effects:
                        action.runtime_sync_status = RUNTIME_SYNC_PENDING
                    else:
                        action.runtime_sync_status = RUNTIME_SYNC_NOT_REQUIRED
                    # 把 execute 可能写入的 arguments 同步回去（plugin_name 等）
                    if action.arguments:
                        arguments = {**arguments, **dict(action.arguments)}
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    log.exception("action execute failed id=%s tool=%s", action.id, tool_name)
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

                if spec.runtime_effects:
                    await self._run_runtime_sync(
                        action_id, list(spec.runtime_effects), arguments=arguments
                    )

                async with AsyncSessionLocal() as db3:
                    final = await db3.get(SystemAgentAction, action_id)
                    return {
                        "ok": True,
                        "action": action_to_dict(final) if final else None,
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
            args = dict(action.arguments or {})
            if not effects:
                action.runtime_sync_status = RUNTIME_SYNC_NOT_REQUIRED
                await db.commit()
                return {"ok": True, "action": action_to_dict(action)}
            action.runtime_sync_status = RUNTIME_SYNC_PENDING
            action.runtime_sync_error = None
            await db.commit()
        await self._run_runtime_sync(action_id, effects, arguments=args)
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
            clear_action_secrets(action)
            action.updated_at = _now()
            await db.commit()

    async def _run_runtime_sync(
        self,
        action_id: str,
        effects: list[str],
        *,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        errors: list[str] = []
        account_id: int | None = None
        args = dict(arguments or {})
        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return
            account_id = action.account_id
            # 合并 DB 中最新 arguments
            args = {**dict(action.arguments or {}), **args}

        for effect in effects:
            try:
                await self._apply_effect(
                    effect,
                    account_id=account_id,
                    action_id=action_id,
                    arguments=args,
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
                await redis.publish(
                    GLOBAL_CHANNEL, make_cmd("start_worker", account_id=int(account_id))
                )
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

        if effect == "plugin_reload":
            plugin_name = str(
                args.get("plugin_name") or args.get("name") or args.get("plugin_key") or ""
            ).strip()
            if not plugin_name:
                return
            from ...services import remote_plugin_service as rps

            async with AsyncSessionLocal() as db:
                await rps.trigger_reload(db, plugin_name)
            return

        if effect == "plugin_fs_cleanup":
            plugin_name = str(
                args.get("plugin_name") or args.get("name") or args.get("plugin_key") or ""
            ).strip()
            if not plugin_name:
                return
            from ...services.remote_plugin_service import _existing_plugin_dir

            try:
                import shutil

                target = _existing_plugin_dir(plugin_name)
                if target.exists():
                    shutil.rmtree(target)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"plugin_fs_cleanup failed: {exc}") from exc
            return

        log.debug("unknown runtime effect %s action=%s", effect, action_id)


_EXECUTOR: ActionExecutor | None = None


def get_action_executor() -> ActionExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ActionExecutor()
    return _EXECUTOR


__all__ = ["ActionExecutor", "get_action_executor"]
