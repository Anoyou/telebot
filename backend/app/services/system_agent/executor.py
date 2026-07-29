"""Action 统一事务执行器：行锁、状态机、幂等与运行时同步。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

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
    lock_action,
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
        bot_account_id: int | None = None,
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
            if bot_account_id is not None and (
                action.channel != "bot"
                or action.account_id is None
                or int(action.account_id) != int(bot_account_id)
            ):
                return {
                    "ok": False,
                    "error_code": "FORBIDDEN",
                    "error_message": "操作不属于当前 Bot 账号",
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
                action = await lock_action(db, action_id)
                if action is None:
                    return {
                        "ok": False,
                        "error_code": "ACTION_NOT_FOUND",
                        "error_message": "操作不存在",
                    }
                if action.status != ACTION_STATUS_PENDING:
                    await db.commit()
                    return {
                        "ok": action.status == ACTION_STATUS_EXECUTED,
                        "already_final": True,
                        "action": action_to_dict(action),
                    }
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
            actor_bot = bot_tg_user_id if bot_tg_user_id is not None else action.actor_bot_user_id
            secret_names = tuple(spec.secret_argument_names or ())
            precheck_clear_names = tuple(
                spec.precheck_clear_secret_argument_names
                if spec.precheck_clear_secret_argument_names is not None
                else secret_names
            )
            precheck_secret_token = action.secret_payload_enc
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
                        row = await lock_action(db2, action_id)
                        if row is not None and row.status == ACTION_STATUS_PENDING:
                            if row.secret_payload_enc != precheck_secret_token:
                                row.error_code = "PRECHECK_STALE"
                                row.error_message = "密钥已在验证期间更新，请再次确认"
                            else:
                                clear_names = tuple(
                                    name
                                    for name in exc.clear_secret_names
                                    if name in precheck_clear_names
                                )
                                if clear_names:
                                    clear_action_secrets(row, clear_names)
                                row.error_code = exc.code
                                row.error_message = exc.message[:1000]
                            row.updated_at = _now()
                            await db2.commit()
                            return {
                                "ok": False,
                                "keep_pending": True,
                                "error_code": row.error_code,
                                "error_message": row.error_message,
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
                    log.warning(
                        "action precheck failed id=%s error_type=%s",
                        action_id,
                        type(exc).__name__,
                    )
                    async with AsyncSessionLocal() as db2:
                        row = await lock_action(db2, action_id)
                        if row is not None and row.status == ACTION_STATUS_PENDING:
                            if row.secret_payload_enc != precheck_secret_token:
                                row.error_code = "PRECHECK_STALE"
                                row.error_message = "密钥已在验证期间更新，请再次确认"
                            else:
                                row.error_code = "PRECHECK_INTERNAL_ERROR"
                                row.error_message = "预检服务异常，密钥仍安全暂存，请稍后重试"
                            row.updated_at = _now()
                            await db2.commit()
                            return {
                                "ok": False,
                                "keep_pending": True,
                                "error_code": row.error_code,
                                "error_message": row.error_message,
                                "business_changed": False,
                                "action": action_to_dict(row),
                            }
                    return {
                        "ok": False,
                        "keep_pending": True,
                        "error_code": "PRECHECK_INTERNAL_ERROR",
                        "error_message": "预检服务异常，密钥仍安全暂存，请稍后重试",
                        "business_changed": False,
                    }

        # ── 阶段 3：行锁 + 业务执行 ─────────────────────────────
        async with AsyncSessionLocal() as db:
            try:
                action = await lock_action(db, action_id)
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
                if bot_account_id is not None and (
                    action.channel != "bot"
                    or action.account_id is None
                    or int(action.account_id) != int(bot_account_id)
                ):
                    return {
                        "ok": False,
                        "error_code": "FORBIDDEN",
                        "error_message": "操作不属于当前 Bot 账号",
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
                if spec.precheck_handler is not None and action.secret_payload_enc != precheck_secret_token:
                    action.error_code = "PRECHECK_STALE"
                    action.error_message = "密钥已在验证期间更新，请再次确认"
                    action.updated_at = _now()
                    await db.commit()
                    return {
                        "ok": False,
                        "keep_pending": True,
                        "error_code": action.error_code,
                        "error_message": action.error_message,
                        "business_changed": False,
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
                        bot_tg_user_id if bot_tg_user_id is not None else action.actor_bot_user_id
                    ),
                    action=action,
                )

                action.status = ACTION_STATUS_EXECUTING
                action.updated_at = _now()
                action.error_code = None
                action.error_message = None
                await db.flush()

                result: Any = None
                result_for_comp: dict[str, Any] = {}
                try:
                    result = await spec.execute_handler(ctx, arguments)
                    # 立刻缓存，供后续任意失败路径做 FS 补偿（含 audit 失败）
                    result_for_comp = result if isinstance(result, dict) else {}
                    if action.arguments:
                        arguments = {**arguments, **dict(action.arguments)}
                    if result_for_comp.get("plugin_name"):
                        arguments["plugin_name"] = result_for_comp["plugin_name"]

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
                    try:
                        await db.commit()
                    except Exception as commit_exc:  # noqa: BLE001
                        log.exception("action commit failed id=%s", action_id)
                        await db.rollback()
                        compensation_error = await self._compensate_plugin_fs_after_failed_commit(
                            tool_name, arguments, result_for_comp
                        )
                        error_code = (
                            "COMMIT_FAILED_COMPENSATION_FAILED" if compensation_error else "COMMIT_FAILED"
                        )
                        error_message = str(commit_exc)[:500]
                        if compensation_error:
                            error_message = (f"{error_message}; 文件补偿失败: {compensation_error}")[:500]
                        await self._mark_failed(
                            action_id,
                            error_code=error_code,
                            error_message=error_message,
                        )
                        async with AsyncSessionLocal() as db_fail:
                            failed = await db_fail.get(SystemAgentAction, action_id)
                            return {
                                "ok": False,
                                "error_code": error_code,
                                "error_message": error_message,
                                "business_changed": None if compensation_error else False,
                                "action": action_to_dict(failed) if failed else None,
                            }
                except Exception as exc:  # noqa: BLE001
                    log.exception("action execute failed id=%s tool=%s", action.id, tool_name)
                    # execute 可能已改磁盘；合并 action.arguments 再补偿
                    try:
                        if action.arguments:
                            arguments = {**arguments, **dict(action.arguments)}
                    except Exception:  # noqa: BLE001
                        pass
                    await db.rollback()
                    compensation_error = await self._compensate_plugin_fs_after_failed_commit(
                        tool_name, arguments, result_for_comp
                    )
                    error_code = type(exc).__name__
                    error_message = str(exc)[:500]
                    if compensation_error:
                        error_code = "EXECUTE_FAILED_COMPENSATION_FAILED"
                        error_message = (f"{error_message}; 文件补偿失败: {compensation_error}")[:500]
                    await self._mark_failed(
                        action_id,
                        error_code=error_code,
                        error_message=error_message,
                    )
                    async with AsyncSessionLocal() as db2:
                        failed = await db2.get(SystemAgentAction, action_id)
                        return {
                            "ok": False,
                            "error_code": error_code,
                            "error_message": error_message,
                            "business_changed": None if compensation_error else False,
                            "action": action_to_dict(failed) if failed else None,
                        }

                if spec.runtime_effects:
                    await self._run_runtime_sync(action_id, list(spec.runtime_effects), arguments=arguments)

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
            if spec is None or not spec.runtime_retryable:
                return {
                    "ok": False,
                    "error_code": "RUNTIME_RETRY_UNSAFE",
                    "error_message": (
                        "该操作包含不可安全重复的外部副作用。请先检查实际状态；"
                        "如需再次执行，请重新发起并确认一个新操作。"
                    ),
                    "action": action_to_dict(action),
                }
            action.runtime_sync_status = RUNTIME_SYNC_PENDING
            action.runtime_sync_error = None
            await db.commit()
        await self._run_runtime_sync(action_id, effects, arguments=args)
        async with AsyncSessionLocal() as db2:
            final = await db2.get(SystemAgentAction, action_id)
            return {"ok": True, "action": action_to_dict(final) if final else None}

    async def _compensate_plugin_fs_after_failed_commit(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> str | None:
        """插件安装类 execute 已落盘、但 Action 事务失败时删除孤儿目录。"""

        install_tools = {
            "plugins.install",
            "plugin_repos.install_plugin",
        }
        if tool_name not in install_tools:
            return None
        name = str(
            result.get("plugin_name")
            or arguments.get("plugin_name")
            or arguments.get("name")
            or arguments.get("plugin_key")
            or ""
        ).strip()
        if not name:
            return "无法确定插件目录"
        try:
            import shutil

            from ...services.remote_plugin_service import _existing_plugin_dir

            target = _existing_plugin_dir(name)
            if target.exists():
                shutil.rmtree(target)
                if target.exists():
                    return f"目录仍存在: {target}"
                log.warning("compensated orphan plugin dir after failed commit name=%s", name)
            return None
        except Exception as exc:  # noqa: BLE001
            log.exception("plugin FS compensation failed name=%s", name)
            return f"{type(exc).__name__}: {str(exc)[:300]}"

    async def _mark_failed(
        self,
        action_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            action = await lock_action(db, action_id)
            if action is None:
                return
            if action.status not in {ACTION_STATUS_PENDING, ACTION_STATUS_EXECUTING}:
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
                safe_error = self._safe_runtime_error(effect, exc, args)
                # Traceback 的最后一行会包含原始异常文本；插件已接触解密配置时
                # 不能用 exc_info=True 把它重新写回日志。
                log.warning(
                    "runtime sync failed action=%s effect=%s error=%s",
                    action_id,
                    effect,
                    safe_error,
                )
                errors.append(f"{effect}: {safe_error[:200]}")

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

    @staticmethod
    def _safe_runtime_error(
        effect: str,
        exc: Exception,
        arguments: dict[str, Any],
    ) -> str:
        from ...services.redactor import redact_text

        if effect == "plugin_config_action":
            return f"{type(exc).__name__}: 插件配置动作失败（敏感详情已隐藏）"
        message = str(exc)
        for key, value in arguments.items():
            normalized = str(key).lower()
            if not any(
                token in normalized
                for token in ("secret", "token", "password", "api_key", "credential", "config_json", "payload_json")
            ):
                continue
            if isinstance(value, str) and value:
                message = message.replace(value, "[REDACTED]")
        return redact_text(message)[:500] or type(exc).__name__

    async def _store_runtime_result(
        self,
        action_id: str,
        key: str,
        value: Any,
    ) -> None:
        """把提交后副作用的脱敏结果并回 Action，供确认卡片与后续查询展示。"""

        safe = summarize_tool_result(value, max_chars=4000)
        async with AsyncSessionLocal() as db:
            action = await db.get(SystemAgentAction, action_id)
            if action is None:
                return
            current = dict(action.result or {})
            current[key] = safe
            action.result = current
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
        if effect == "update_account_runtime":
            if not bool(args.get("_restart_required")):
                return
            effect = "restart_worker"

        if effect == "reload_feature_accounts":
            feature_key = str(args.get("feature_key") or args.get("key") or "").strip()
            if not feature_key:
                raise RuntimeError("reload_feature_accounts 缺少 feature_key")
            from ...services import feature_service

            async with AsyncSessionLocal() as db:
                await feature_service._notify_all_accounts_using_feature(  # noqa: SLF001
                    db, feature_key
                )
            return

        if effect == "reload_rate_limits_all":
            from ...redis_client import get_redis
            from ...worker.ipc import GCMD_RELOAD_GLOBAL, GLOBAL_CHANNEL, make_cmd

            await get_redis().publish(GLOBAL_CHANNEL, make_cmd(GCMD_RELOAD_GLOBAL))
            return

        if effect == "reload_global_settings":
            from ...redis_client import get_redis
            from ...services import event_trace
            from ...worker.ipc import GCMD_RELOAD_GLOBAL, GLOBAL_CHANNEL, make_cmd

            await get_redis().publish(GLOBAL_CHANNEL, make_cmd(GCMD_RELOAD_GLOBAL))
            await event_trace.refresh_trace_settings()
            try:
                from ...worker.supervisor import invalidate_log_retention_cache

                invalidate_log_retention_cache()
            except Exception:  # noqa: BLE001
                log.debug("invalidate log retention cache failed", exc_info=True)
            return

        if effect == "sync_rate_limit_overrides":
            if account_id is None:
                raise RuntimeError("sync_rate_limit_overrides 缺少 account_id")
            from sqlalchemy import select

            from ...db.models.rate_limit import ACTION_KEYS, RateLimitOverride
            from ...redis_client import get_redis
            from ...worker.ratelimit.overrides import _redis_key  # noqa: SLF001

            redis = get_redis()
            selected_action = str(args.get("action") or "").strip()
            actions = [selected_action] if selected_action else list(ACTION_KEYS)
            now = _now()
            async with AsyncSessionLocal() as db:
                for action_name in actions:
                    row = await db.scalar(
                        select(RateLimitOverride)
                        .where(
                            RateLimitOverride.account_id == int(account_id),
                            RateLimitOverride.action == action_name,
                            RateLimitOverride.expires_at > now,
                        )
                        .order_by(
                            RateLimitOverride.multiplier.asc(),
                            RateLimitOverride.expires_at.desc(),
                        )
                        .limit(1)
                    )
                    key = _redis_key(int(account_id), action_name)
                    if row is None:
                        await redis.delete(key)
                        continue
                    expires = row.expires_at
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    ttl = max(1, int((expires - now).total_seconds()))
                    await redis.set(key, str(float(row.multiplier)), ex=ttl)
            return

        if effect == "plugin_config_action":
            if account_id is None:
                raise RuntimeError("plugin_config_action 缺少 account_id")
            feature_key = str(args.get("feature_key") or "").strip()
            action_key = str(args.get("action_key") or "").strip()
            if not feature_key or not action_key:
                raise RuntimeError("plugin_config_action 缺少 feature_key 或 action_key")
            raw_payload = args.get("payload_json")
            if raw_payload in (None, ""):
                payload: dict[str, Any] = {}
            else:
                try:
                    parsed = json.loads(str(raw_payload))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("payload_json 不是合法 JSON") from exc
                if not isinstance(parsed, dict):
                    raise RuntimeError("payload_json 顶层必须是对象")
                payload = parsed

            from ...db.models.account import Account
            from ...db.models.feature import Feature
            from ...db.models.plugin import InstalledPlugin
            from ...services import feature_service
            from ...services.plugin_config_secrets import (
                config_secret_values,
                decrypt_config_secrets,
                redact_exact_secrets,
            )

            async with AsyncSessionLocal() as db:
                account = await db.get(Account, int(account_id))
                feature = await db.get(Feature, feature_key)
                installed = await db.get(InstalledPlugin, feature_key)
                if account is None or feature is None:
                    raise RuntimeError("账号或插件已不存在")
                effective = await feature_service.get_effective_plugin_config(
                    db, int(account_id), feature_key
                )
                raw_schema = (
                    feature.manifest.get("config_schema")
                    if isinstance(feature.manifest, dict)
                    else None
                )
                runtime_config = decrypt_config_secrets(
                    effective,
                    schema=raw_schema if isinstance(raw_schema, dict) else None,
                    strict=True,
                )
                known_config_secrets = config_secret_values(
                    runtime_config,
                    schema=raw_schema if isinstance(raw_schema, dict) else None,
                )
                if bool(args.get("background")):
                    from ...services.plugin_config_action_jobs import (
                        create_plugin_config_action_job,
                        job_response,
                    )

                    job = await create_plugin_config_action_job(
                        db,
                        account=account,
                        feature=feature,
                        action_key=action_key,
                        effective_config=runtime_config,
                        current_config=payload.get("config") or {},
                        action_input=payload.get("input") or {},
                        installed_plugin=installed,
                    )
                    result = job_response(job, logs=[]).model_dump(mode="json")
                else:
                    from ...services.plugin_config_actions import run_plugin_config_action

                    result = await run_plugin_config_action(
                        db,
                        account=account,
                        feature=feature,
                        action_key=action_key,
                        effective_config=runtime_config,
                        current_config=payload.get("config") or {},
                        action_input=payload.get("input") or {},
                        installed_plugin=installed,
                    )
            safe_result = redact_exact_secrets(result, known_config_secrets)
            await self._store_runtime_result(action_id, "config_action", safe_result)
            return

        if effect == "plugin_config_action_control":
            job_id = str(args.get("job_id") or "").strip()
            action_name = str(args.get("action") or "").strip()
            if not job_id or action_name not in {"pause", "cancel"}:
                raise RuntimeError("配置动作控制参数无效")
            from ...services.plugin_config_action_jobs import (
                control_plugin_config_action_job,
            )

            async with AsyncSessionLocal() as db:
                response = await control_plugin_config_action_job(
                    db, job_id, action=action_name
                )
            if response is None:
                raise RuntimeError("插件配置动作任务不存在")
            await self._store_runtime_result(
                action_id,
                "config_action_job",
                response.model_dump(mode="json"),
            )
            return

        if effect in {"reload_worker", "worker_reload", "reload_config"}:
            raw_ids = args.get("reload_account_ids")
            account_ids = [
                int(value)
                for value in (raw_ids if isinstance(raw_ids, list) else [])
            ]
            if not account_ids and account_id is not None:
                account_ids.append(int(account_id))
            if not account_ids:
                return
            from ...redis_client import get_redis
            from ...worker.ipc import CMD_RELOAD_CONFIG, publish_cmd_with_ack

            redis = get_redis()
            for affected_id in sorted(set(account_ids)):
                await publish_cmd_with_ack(redis, affected_id, CMD_RELOAD_CONFIG)
            return

        if effect == "reload_ignored":
            if account_id is None:
                raise RuntimeError("reload_ignored 缺少 account_id")
            from ...redis_client import get_redis
            from ...worker.ipc import CMD_RELOAD_IGNORED, publish_cmd_with_ack

            await publish_cmd_with_ack(
                get_redis(),
                int(account_id),
                CMD_RELOAD_IGNORED,
            )
            return

        if effect == "reload_commands":
            from ...services import command_service

            raw_ids = args.get("reload_account_ids")
            account_ids = [
                int(value)
                for value in (raw_ids if isinstance(raw_ids, list) else [])
            ]
            if bool(args.get("reload_ai_command_accounts")):
                async with AsyncSessionLocal() as db:
                    account_ids.extend(await command_service.list_aids_with_ai_commands(db))
            if not account_ids and account_id is not None:
                account_ids.append(int(account_id))
            if account_ids:
                await command_service.notify_reload(sorted(set(account_ids)))
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

        if effect == "restart_affected_workers":
            raw_ids = args.get("restart_account_ids")
            account_ids = sorted(
                {
                    int(value)
                    for value in (raw_ids if isinstance(raw_ids, list) else [])
                    if str(value).strip()
                }
            )
            if not account_ids:
                return
            from ...worker import supervisor

            failures: list[str] = []
            for affected_id in account_ids:
                try:
                    await supervisor.stop_worker(affected_id)
                    await supervisor.start_worker(affected_id)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"#{affected_id}: {type(exc).__name__}")
            if failures:
                raise RuntimeError(
                    "部分受影响账号 Worker 重启失败：" + ", ".join(failures)
                )
            return

        if effect == "scheduler_execute_now":
            rule_id = args.get("rule_id") or args.get("id")
            if account_id is None or rule_id in (None, ""):
                raise RuntimeError("scheduler_execute_now 缺少 account_id 或 rule_id")
            from ...services import rule_service

            await rule_service.execute_scheduler_rule_now(
                int(account_id),
                int(rule_id),
            )
            return

        if effect == "system_apply_update":
            from types import SimpleNamespace

            from ...api import system_health as sh
            from ...api.system_health import UpdateRequest

            payload = UpdateRequest(
                remote=args.get("remote"),
                branch=args.get("branch"),
                full=bool(args.get("force_full") or args.get("full") or False),
            )
            result = await sh.pull_update(
                _user=SimpleNamespace(id=0),
                payload=payload,
            )
            data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            if not bool(data.get("success")):
                raise RuntimeError(
                    str(data.get("error") or data.get("manual_command") or "系统更新未成功启动")
                )
            return

        if effect == "system_restart":
            from types import SimpleNamespace

            from ...api import system_health as sh

            result = await sh.restart_app(_user=SimpleNamespace(id=0))
            data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            if not bool(data.get("success")):
                raise RuntimeError(str(data.get("error") or "系统重启未成功下发"))
            return

        if effect == "platform_capability":
            module_key = str(args.get("module_key") or "").strip()
            if not module_key:
                raise RuntimeError("platform_capability 缺少 module_key")
            from ...services import platform_capabilities as caps

            if module_key not in caps.MODULE_DEFS:
                raise RuntimeError(f"未知平台模块：{module_key}")
            async with AsyncSessionLocal() as db:
                await caps.set_module_enabled(
                    db,
                    module_key,
                    bool(args.get("enabled")),
                    user_id=None,
                    notify_workers=True,
                    apply_local=True,
                )
            return

        if effect in {"account_bot_sync", "account_bot_restart"}:
            if account_id is None:
                raise RuntimeError(f"{effect} 缺少 account_id")
            from ...services import account_bot_runtime

            if effect == "account_bot_restart":
                await account_bot_runtime.restart_account_bot(int(account_id))
            else:
                await account_bot_runtime.sync_account_bot(int(account_id))
            return

        if effect == "account_bot_test_send":
            if account_id is None:
                raise RuntimeError("account_bot_test_send 缺少 account_id")
            from ...services import account_bot_service

            async with AsyncSessionLocal() as db:
                row = await account_bot_service.get_bot_config(
                    db, int(account_id), create=False
                )
                token = account_bot_service.decrypt_bot_token(row)
            targets = [int(value) for value in (args.get("target_chat_ids") or [])]
            if not targets:
                raise RuntimeError("account_bot_test_send 缺少已确认的 target_chat_ids")
            text_value = str(
                args.get("text") or "TelePilot 账号 Bot 测试消息发送成功。"
            )
            sent = 0
            errors: list[str] = []
            for target in targets:
                try:
                    await account_bot_service.send_message(token, target, text_value)
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        account_bot_service.sanitize_bot_error(exc, token=token)
                    )
            if sent == 0:
                raise RuntimeError(errors[0] if errors else "管理 Bot 测试发送失败")
            await self._store_runtime_result(
                action_id,
                "test_send",
                {"sent": sent, "errors": errors},
            )
            return

        if effect == "notification_test_send":
            bot_id = int(args.get("id") or args.get("bot_id") or 0)
            if bot_id <= 0:
                raise RuntimeError("notification_test_send 缺少 bot_id")
            from ...services import notify_service

            target_chat_id = int(args.get("target_chat_id") or 0)
            if target_chat_id == 0:
                raise RuntimeError("notification_test_send 缺少已确认的 target_chat_id")
            ok = await notify_service.send_to_bot_snapshot(
                bot_id,
                target_chat_id,
                str(args.get("text") or "TelePilot 通知通道测试"),
            )
            if not ok:
                raise RuntimeError("发送失败，请检查 Bot Token、默认 Chat ID 和网络")
            await self._store_runtime_result(
                action_id, "test_send", {"ok": True, "bot_id": bot_id}
            )
            return

        if effect == "message_template_test_send":
            from ...schemas.message_template import MessageTemplateTestSendRequest
            from ...services import message_template_service

            async with AsyncSessionLocal() as db:
                response = await message_template_service.send_test_message(
                    db,
                    MessageTemplateTestSendRequest(
                        account_id=int(args["account_id"]),
                        target_chat_id=int(args["target_chat_id"]),
                        text=str(args["text"]),
                        parse_mode=args.get("parse_mode", "HTML"),
                    ),
                )
            await self._store_runtime_result(
                action_id,
                "test_send",
                response.model_dump(mode="json"),
            )
            return

        if effect == "dispatch_enable_trace":
            if account_id is None:
                raise RuntimeError("dispatch_enable_trace 缺少 account_id")
            from ...services import account_bot_runtime

            result = await account_bot_runtime.set_router_debug_trace(
                int(account_id),
                plugin_key=args.get("plugin_key"),
                chat_id=args.get("chat_id"),
                ttl_seconds=max(
                    1, min(int(args.get("ttl_seconds") or 300), 3600)
                ),
            )
            await self._store_runtime_result(action_id, "debug_trace", result)
            return

        if effect == "interaction_bot_restart":
            if account_id is None:
                raise RuntimeError("interaction_bot_restart 缺少 account_id")
            from ...services import interaction_bot_runtime

            await interaction_bot_runtime.restart_interaction_bot(int(account_id))
            return

        if effect in {"account_bot_dlq_replay", "account_bot_dlq_discard"}:
            if account_id is None:
                raise RuntimeError(f"{effect} 缺少 account_id")
            loop = str(args.get("loop") or "").strip()
            update_id = int(args.get("update_id") or 0)
            from ...services import account_bot_runtime

            dlq_id = account_bot_runtime._polling_dlq_id(loop, update_id)  # noqa: SLF001
            if effect == "account_bot_dlq_replay":
                result = await account_bot_runtime._replay_polling_dead_letter(  # noqa: SLF001
                    int(account_id), dlq_id
                )
                if not bool(result.get("ok")):
                    raise RuntimeError(str(result.get("error") or "DLQ 重放失败"))
            else:
                deleted = await account_bot_runtime._discard_polling_dead_letter(  # noqa: SLF001
                    int(account_id), dlq_id
                )
                if not deleted:
                    raise RuntimeError("DLQ 条目不存在")
            return

        if effect == "kill_switch":
            from ...services import kill_switch_service

            async with AsyncSessionLocal() as db:
                await kill_switch_service.converge_runtime(
                    db, bool(args.get("enabled"))
                )
            return

        if effect == "plugin_update":
            plugin_name = str(
                args.get("plugin_name") or args.get("name") or args.get("plugin_key") or ""
            ).strip()
            if not plugin_name:
                raise RuntimeError("plugin_update 缺少 plugin_name")
            from ...services import remote_plugin_service as rps

            async with AsyncSessionLocal() as db:
                await rps.update(db, plugin_name)
                await db.commit()
            async with AsyncSessionLocal() as db:
                await rps.trigger_reload(db, plugin_name)
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

        if effect == "plugin_repo_cache_cleanup":
            repo_url = str(args.get("repo_url") or "").strip()
            if not repo_url:
                raise RuntimeError("plugin_repo_cache_cleanup 缺少 repo_url")
            from ...services.plugin_repo_service import cleanup_repo_cache

            await cleanup_repo_cache(repo_url)
            return

        if effect == "plugin_repo_bulk_update":
            repo_id = int(args.get("repo_id") or args.get("id") or 0)
            if repo_id <= 0:
                raise RuntimeError("plugin_repo_bulk_update 缺少 repo_id")
            from ...services import plugin_repo_service as repo_service
            from ...services import remote_plugin_service

            async with AsyncSessionLocal() as db:
                result = await repo_service.update_installed_plugins_from_repo(
                    db, repo_id
                )
                await db.commit()
                updated_names = [
                    item.name for item in result.items if item.status == "updated"
                ]
                reload_errors: list[str] = []
                for plugin_name in updated_names:
                    try:
                        await remote_plugin_service.trigger_reload(db, plugin_name)
                    except Exception as exc:  # noqa: BLE001
                        reload_errors.append(
                            f"{plugin_name}: {type(exc).__name__}"
                        )
            payload = result.model_dump(mode="json")
            payload["reload_errors"] = reload_errors
            await self._store_runtime_result(action_id, "bulk_update", payload)
            if reload_errors:
                raise RuntimeError(
                    "插件文件已更新，但部分 Worker 未确认重载："
                    + ", ".join(reload_errors)
                )
            return

        raise RuntimeError(f"未知运行时副作用：{effect}")


_EXECUTOR: ActionExecutor | None = None


def get_action_executor() -> ActionExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ActionExecutor()
    return _EXECUTOR


__all__ = ["ActionExecutor", "get_action_executor"]
