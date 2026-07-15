"""Worker 级平台调度器。

这个模块把定时任务从“普通插件 tick loop”提升为 worker 常驻基础能力：

- GUI 创建的 scheduler 规则仍然存放在 ``Rule(feature_key="scheduler")``；
- 插件可以通过 ``ctx.scheduler`` 注册运行期任务；
- 插件禁用 / 热重载时由 loader 清理其注册的任务，避免旧 callback 继续触发。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import CroniterBadCronError, croniter
from sqlalchemy import select
from telethon.errors import FloodWaitError

from app.db.base import AsyncSessionLocal
from app.db.models.command import LLMProvider
from app.db.models.feature import FEATURE_SCHEDULER
from app.db.models.rule import Rule
from app.db.models.system import SystemSetting
from app.services.event_trace import (
    TRACE_STATUS_FAILED,
    TRACE_STATUS_OK,
    TRACE_STATUS_SKIPPED,
    finish_trace,
    record_action,
    start_trace,
    trace_log_context,
)
from app.services.llm_client import LLMCallFailed, LLMError
from app.services.llm_dto import LLMProviderDTO
from app.services.llm_invoke import invoke as invoke_ai_runtime
from app.worker.command import should_allow_auto_command_text
from app.worker.plugins.base import PluginContext

log = logging.getLogger(__name__)

SCHEDULER_TICK_SECONDS = 30
_MAX_MESSAGE_LEN = 3900
DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(slots=True)
class ScheduledJob:
    """插件注册任务的回调入参。"""

    account_id: int
    owner: str
    job_id: str
    config: dict[str, Any]
    fired_at: datetime
    fire_count: int


@dataclass(slots=True)
class SchedulerExecutionResult:
    ok: bool
    error: str | None = None


ScheduleCallback = Callable[[ScheduledJob], Awaitable[None] | None]
LogWriter = Callable[..., Awaitable[None]]


class SchedulerCommandBlockedError(RuntimeError):
    """scheduler 尝试触发不在白名单中的命令。"""


def _scheduler_trace_context(ctx: PluginContext, *, action: dict[str, Any] | None = None) -> dict[str, Any]:
    trace = getattr(ctx, "_scheduler_trace", None)
    plugin_key = str(getattr(ctx, "feature_key", "") or FEATURE_SCHEDULER)
    entry_key = str(getattr(ctx, "_scheduler_entry_key", "") or "").strip() or None
    context = trace_log_context(trace, plugin_key=plugin_key, entry_key=entry_key)
    if action is not None:
        action.setdefault("context", context)
    return context


async def _scheduler_trace_enabled() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "log_retention")
        raw = row.value if row is not None and isinstance(row.value, dict) else {}
        return bool(raw.get("trace_enabled", True))
    except Exception:  # noqa: BLE001
        log.debug("load scheduler trace switch failed, using default", exc_info=True)
        return True


async def _record_scheduler_action(
    ctx: PluginContext,
    action: dict[str, Any],
    status: str,
    **detail: Any,
) -> None:
    context = _scheduler_trace_context(ctx, action=action)
    if not context.get("trace_id") and isinstance(action.get("context"), dict):
        context = dict(action["context"])
    if not context.get("trace_id"):
        return
    detail.setdefault("plugin_key", str(getattr(ctx, "feature_key", "") or FEATURE_SCHEDULER))
    detail.setdefault("component", "scheduler")
    await record_action(context, action, status, **detail)


@dataclass
class _RuntimeJob:
    owner: str
    generation: int
    job_id: str
    config: dict[str, Any]
    callback: ScheduleCallback
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fire_count: int = 0
    last_error: str | None = None


async def _get_system_tz() -> ZoneInfo | None:
    """从 system_setting 读取用户配置的时区，未配置时默认按上海时区。"""
    try:
        from app.db.models.system import SystemSetting

        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "timezone")
            if row and isinstance(row.value, dict):
                tz_str = str(row.value.get("value", "")).strip()
                if tz_str:
                    return ZoneInfo(tz_str)
    except Exception:
        pass
    return ZoneInfo(DEFAULT_TIMEZONE)


def _croniter_next(
    expr: str, start_utc: datetime, tz: ZoneInfo | None
) -> datetime | None:
    """在指定时区下计算 cron 的下一次触发时间，返回 UTC。"""
    try:
        if tz is not None:
            local_now = start_utc.astimezone(tz)
            next_local: datetime = _croniter(expr, local_now).get_next(datetime)
            return next_local.astimezone(UTC)
        return _croniter(expr, start_utc).get_next(datetime)
    except (CroniterBadCronError, ValueError):
        return None


def _croniter_prev(
    expr: str, start_utc: datetime, tz: ZoneInfo | None
) -> datetime | None:
    """在指定时区下计算 cron 的上一次触发时间，返回 UTC。"""
    try:
        if tz is not None:
            local_now = start_utc.astimezone(tz)
            prev_local: datetime = _croniter(expr, local_now).get_prev(datetime)
            return prev_local.astimezone(UTC)
        return _croniter(expr, start_utc).get_prev(datetime)
    except (CroniterBadCronError, ValueError):
        return None


def _croniter(expr: str, start: datetime) -> croniter:
    """Create croniter with explicit 6-field support.

    TelePilot accepts classic 5-field cron as ``minute hour day month weekday``.
    For 6/7 fields, use Quartz-style leading seconds:
    ``second minute hour day month weekday [year]``.
    """
    kwargs = {"second_at_beginning": True} if _cron_uses_leading_seconds(expr) else {}
    return croniter(expr, start, **kwargs)


def _cron_uses_leading_seconds(expr: str) -> bool:
    return len(str(expr or "").split()) in (6, 7)


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def _to_positive_int(raw: Any) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 0
    return v if v > 0 else 0


class SchedulerRuleExecutor:
    """平台调度器使用的规则算法与内置 action 执行器。

    这个类不再是普通插件；GUI 的 scheduler 规则和手动执行都由 PlatformScheduler
    调它完成。builtin scheduler 插件仅继承它作为历史兼容壳。
    """

    async def tick_rules_once(self, ctx: PluginContext) -> None:
        now = datetime.now(UTC)
        tz = await _get_system_tz()
        for rule in ctx.rules:
            cfg = dict(rule.config or {})
            due, next_fire = self._resolve_due(cfg, now, tz)

            changed = False
            next_iso = _to_iso(next_fire)
            if cfg.get("next_fire") != next_iso:
                cfg["next_fire"] = next_iso
                changed = True
            if cfg.pop("_config_dirty", False):
                changed = True

            if not due:
                if changed:
                    rule.config = cfg
                    await self._persist_rule_config(rule.id, cfg)
                continue

            fired_at = datetime.now(UTC)
            ok = await self._fire(ctx, rule.id, cfg)
            if ok:
                cfg["last_fire"] = _to_iso(fired_at)
                cfg["last_result"] = "ok"
                cfg["last_error"] = None
                self._advance_after_fire(cfg, fired_at, tz)
            else:
                cfg["last_result"] = "error"

            rule.config = cfg
            await self._persist_rule_config(rule.id, cfg)

    def resolve_due(
        self, cfg: dict[str, Any], now: datetime, tz: ZoneInfo | None = None
    ) -> tuple[bool, datetime | None]:
        kind = str(cfg.get("kind") or "cron").lower()
        if kind == "once":
            return self.resolve_once(cfg, now)
        if kind == "interval":
            return self.resolve_interval(cfg, now)
        return self.resolve_cron(cfg, now, tz)

    def resolve_once(self, cfg: dict[str, Any], now: datetime) -> tuple[bool, datetime | None]:
        fire_at = _parse_dt(cfg.get("fire_at"))
        if fire_at is None:
            return False, None
        if cfg.get("last_fire"):
            return False, fire_at
        return fire_at <= now, fire_at

    def resolve_interval(self, cfg: dict[str, Any], now: datetime) -> tuple[bool, datetime | None]:
        interval = _to_positive_int(cfg.get("interval_sec"))
        if interval <= 0:
            return False, None
        last = _parse_dt(cfg.get("last_fire"))
        if last is None:
            return True, now
        next_fire = last + timedelta(seconds=interval)
        return next_fire <= now, next_fire

    def resolve_cron(
        self, cfg: dict[str, Any], now: datetime, tz: ZoneInfo | None = None
    ) -> tuple[bool, datetime | None]:
        expr = str(cfg.get("cron") or "").strip()
        if not expr:
            return False, None

        next_fire = _parse_dt(cfg.get("next_fire"))
        last_cron = cfg.get("_last_cron")
        cron_changed = (last_cron is not None) and (last_cron != expr)
        seconds_mode = _cron_uses_leading_seconds(expr)
        previous_seconds_mode = cfg.get("_cron_seconds_mode")
        cron_mode_changed = previous_seconds_mode is not None and bool(previous_seconds_mode) != seconds_mode
        missing_mode_marker = previous_seconds_mode is None
        tz_key = getattr(tz, "key", None) or "UTC"
        previous_tz = cfg.get("_cron_timezone")
        cron_timezone_changed = previous_tz is not None and previous_tz != tz_key
        missing_timezone_marker = previous_tz is None

        if last_cron != expr:
            cfg["_last_cron"] = expr
            cfg["_config_dirty"] = True
        if previous_seconds_mode != seconds_mode:
            cfg["_cron_seconds_mode"] = seconds_mode
            cfg["_config_dirty"] = True
        if previous_tz != tz_key:
            cfg["_cron_timezone"] = tz_key
            cfg["_config_dirty"] = True

        if (
            cron_changed
            or cron_mode_changed
            or cron_timezone_changed
            or ((missing_mode_marker or missing_timezone_marker) and next_fire is not None)
        ):
            nf = _croniter_next(expr, now, tz)
            if nf is None:
                return False, None
            prev = _croniter_prev(expr, now, tz)
            if prev is not None and prev <= now and (now - prev) <= timedelta(seconds=SCHEDULER_TICK_SECONDS + 5):
                return True, nf
            if nf <= now + timedelta(seconds=SCHEDULER_TICK_SECONDS):
                return True, nf
            return False, nf

        if next_fire is None:
            nf = _croniter_next(expr, now, tz)
            if nf is None:
                return False, None
            return False, nf

        return next_fire <= now, next_fire

    def advance_after_fire(
        self, cfg: dict[str, Any], fired_at: datetime, tz: ZoneInfo | None = None
    ) -> None:
        kind = str(cfg.get("kind") or "cron").lower()
        if kind == "once":
            cfg["enabled"] = False
            cfg["next_fire"] = None
            return
        if kind == "interval":
            interval = _to_positive_int(cfg.get("interval_sec"))
            cfg["next_fire"] = _to_iso(fired_at + timedelta(seconds=max(interval, 1)))
            return

        expr = str(cfg.get("cron") or "").strip()
        if not expr:
            cfg["next_fire"] = None
            return
        nf = _croniter_next(expr, fired_at, tz)
        cfg["next_fire"] = _to_iso(nf)

    async def fire(self, ctx: PluginContext, rule_id: int, cfg: dict[str, Any]) -> bool:
        action = cfg.get("action")
        if not isinstance(action, dict):
            if ctx.log is not None:
                await ctx.log("error", f"[scheduler] rule={rule_id} missing action")
            cfg["last_error"] = "missing action"
            return False

        action_type = str(action.get("type") or "send_message").lower()
        trace = None
        if await _scheduler_trace_enabled():
            trace = await start_trace(
                {
                    "event_type": "scheduler_fire",
                    "source": {
                        "type": "scheduler_fire",
                        "channel": "scheduler",
                        "account_id": ctx.account_id,
                    },
                    "message": {},
                    "chat": {},
                    "sender": {},
                    "trigger": {
                        "rule_id": rule_id,
                        "action_type": action_type,
                        "feature_key": getattr(ctx, "feature_key", FEATURE_SCHEDULER),
                    },
                    "raw": {"action": action},
                }
            )
        previous_trace = getattr(ctx, "_scheduler_trace", None)
        previous_entry_key = getattr(ctx, "_scheduler_entry_key", None)
        ctx._scheduler_trace = trace
        ctx._scheduler_entry_key = str(rule_id)
        try:
            if action_type == "send_message":
                await self.action_send_message(ctx, action)
            elif action_type == "run_command":
                await self.action_run_command(ctx, action)
            elif action_type == "call_llm":
                await self.action_call_llm(ctx, action)
            else:
                raise ValueError(f"unknown action.type={action_type}")
            if trace is not None:
                await finish_trace(trace, TRACE_STATUS_OK, rule_id=rule_id, action_type=action_type)
            return True
        except SchedulerCommandBlockedError as exc:
            cfg["last_error"] = str(exc)
            if ctx.log is not None:
                await ctx.log("info", f"[scheduler] rule={rule_id} blocked: {exc}")
            if trace is not None:
                await finish_trace(trace, TRACE_STATUS_FAILED, rule_id=rule_id, action_type=action_type, error=str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            cfg["last_error"] = f"{type(exc).__name__}: {exc}"
            if ctx.log is not None:
                await ctx.log("error", f"[scheduler] rule={rule_id} fire failed: {type(exc).__name__}: {exc}")
            if trace is not None:
                await finish_trace(
                    trace,
                    TRACE_STATUS_FAILED,
                    rule_id=rule_id,
                    action_type=action_type,
                    error=f"{type(exc).__name__}: {exc}",
                )
            return False
        finally:
            ctx._scheduler_trace = previous_trace
            ctx._scheduler_entry_key = previous_entry_key

    async def action_send_message(self, ctx: PluginContext, action: dict[str, Any]) -> None:
        target = int(action["target_chat_id"])
        text = str(action.get("text") or "").strip()
        if not text:
            raise ValueError("send_message requires non-empty text")
        trace_action = {
            "type": "send_message",
            "send_via": "userbot_reply",
            "chat_id": target,
            "text": text,
        }
        msg = await self.send_with_ratelimit(ctx, target, text, action=trace_action)
        delete_after = _to_positive_int(action.get("delete_after"))
        if delete_after > 0 and msg is not None:
            asyncio.create_task(self.delete_message_after(ctx, msg, delete_after, action_context=_scheduler_trace_context(ctx)))

    async def action_run_command(self, ctx: PluginContext, action: dict[str, Any]) -> None:
        target = int(action.get("target_chat_id") or 0)
        command = str(action.get("command") or action.get("text") or "").strip()
        if not command:
            raise ValueError("run_command requires command/text")
        trace_action = {
            "type": "run_command",
            "send_via": "userbot_reply",
            "chat_id": target or None,
            "text": command,
        }
        msg = await self.send_with_ratelimit(ctx, target or "me", command, action=trace_action)
        delete_after = _to_positive_int(action.get("delete_after"))
        if delete_after > 0 and msg is not None:
            asyncio.create_task(self.delete_message_after(ctx, msg, delete_after, action_context=_scheduler_trace_context(ctx)))

    async def action_call_llm(self, ctx: PluginContext, action: dict[str, Any]) -> None:
        provider_id = int(action["provider_id"])
        prompt = str(action.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("call_llm requires prompt")

        row = await self.get_provider_row(provider_id)
        if row is None:
            raise ValueError(f"provider_id={provider_id} not found")

        dto = LLMProviderDTO.from_orm_row(row)
        system_prompt = str(action.get("system_prompt") or "你是简洁有用的中文助手。")
        max_tokens = _to_positive_int(action.get("max_tokens")) or 256

        provider_rows = await self.get_provider_rows()
        provider_dtos = {int(p.id): LLMProviderDTO.from_orm_row(p) for p in provider_rows}
        provider_dtos[dto.id] = dto
        fallback_id = _to_positive_int(action.get("fallback_provider_id")) or None
        # TODO(interactive-bot): 上游交互 Bot 调度触发时传入真实 trigger account id。
        triggered_by_account_id = _to_positive_int(action.get("triggered_by_account_id")) or None

        try:
            result, _, _ = await invoke_ai_runtime(
                dto,
                provider_dtos,
                system_prompt,
                prompt,
                override_model=action.get("model"),
                max_tokens=max_tokens,
                fallback_provider_id=fallback_id,
                account_id=ctx.account_id,
                triggered_by_account_id=triggered_by_account_id,
                source="scheduler",
                matched_tag="scheduler",
            )
        except (LLMError, LLMCallFailed):
            raise

        text = (result.text or "").strip() or "(empty)"
        target = int(action["target_chat_id"])
        trace_action = {
            "type": "call_llm",
            "send_via": "userbot_reply",
            "chat_id": target,
            "text": text[:_MAX_MESSAGE_LEN],
        }
        msg = await self.send_with_ratelimit(ctx, target, text[:_MAX_MESSAGE_LEN], action=trace_action)
        delete_after = _to_positive_int(action.get("delete_after"))
        if delete_after > 0 and msg is not None:
            asyncio.create_task(self.delete_message_after(ctx, msg, delete_after, action_context=_scheduler_trace_context(ctx)))

    async def delete_message_after(
        self,
        ctx: PluginContext,
        msg: Any,
        seconds: int,
        *,
        action_context: dict[str, Any] | None = None,
    ) -> None:
        action = {
            "type": "delete_message",
            "send_via": "userbot_reply",
            "chat_id": getattr(msg, "peer_id", None) or getattr(msg, "chat_id", None),
            "message_id": getattr(msg, "id", None) or getattr(msg, "message_id", None),
            "context": action_context or _scheduler_trace_context(ctx),
        }
        try:
            await asyncio.sleep(seconds)
            await ctx.client.delete_messages(msg.peer_id, msg.id)
            await _record_scheduler_action(
                ctx,
                action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result={"message_id": getattr(msg, "id", None) or getattr(msg, "message_id", None)},
            )
        except Exception:  # noqa: BLE001
            await _record_scheduler_action(
                ctx,
                action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="telegram_api_error",
                error_message=f"delete_message failed (msg_id={getattr(msg, 'id', '?')})",
            )
            if ctx.log is not None:
                await ctx.log("warn", f"[scheduler] delete_message failed (msg_id={getattr(msg, 'id', '?')})")

    async def get_provider_row(self, provider_id: int) -> LLMProvider | None:
        async with AsyncSessionLocal() as db:
            return await db.get(LLMProvider, provider_id)

    async def get_provider_rows(self) -> list[LLMProvider]:
        async with AsyncSessionLocal() as db:
            return list((await db.execute(select(LLMProvider))).scalars().all())

    async def persist_rule_config(self, rid: int, cfg: dict[str, Any]) -> None:
        async with AsyncSessionLocal() as db:
            row = await db.get(Rule, rid)
            if row is None:
                return
            row.config = cfg
            await db.commit()

    async def send_with_ratelimit(
        self,
        ctx: PluginContext,
        peer: int | str,
        text: str,
        *,
        action: dict[str, Any] | None = None,
    ) -> Any | None:
        trace_action = action or {
            "type": "send_message",
            "send_via": "userbot_reply",
            "chat_id": peer if isinstance(peer, int) else None,
            "text": text,
        }
        allowed, command_key = should_allow_auto_command_text(text)
        if not allowed:
            await _record_scheduler_action(
                ctx,
                trace_action,
                TRACE_STATUS_FAILED,
                actual_send_via="userbot_reply",
                error_code="permission_denied",
                error_message=f"auto command blocked by whitelist: {command_key}",
            )
            raise SchedulerCommandBlockedError(
                f"auto command blocked by whitelist: {command_key}"
            )
        peer_id = int(peer) if isinstance(peer, int) else None
        decision = await ctx.engine.acquire(
            ctx.account_id,
            "send_message_group",
            peer_id=peer_id,
        )
        if not decision.allowed:
            await _record_scheduler_action(
                ctx,
                trace_action,
                TRACE_STATUS_SKIPPED,
                actual_send_via="userbot_reply",
                error_code="rate_limited",
                result={"outcome": decision.outcome},
            )
            if ctx.log is not None:
                await ctx.log("info", f"[scheduler] ratelimited drop outcome={decision.outcome}")
            return None
        if decision.wait_seconds and decision.wait_seconds > 0:
            await asyncio.sleep(float(decision.wait_seconds))

        try:
            msg = await ctx.client.send_message(peer, text)
            await _record_scheduler_action(
                ctx,
                trace_action,
                TRACE_STATUS_OK,
                actual_send_via="userbot_reply",
                result={"message_id": getattr(msg, "id", None) or getattr(msg, "message_id", None)},
            )
            return msg
        except Exception as exc:
            if not isinstance(exc, FloodWaitError) and not hasattr(exc, "seconds"):
                await _record_scheduler_action(
                    ctx,
                    trace_action,
                    TRACE_STATUS_FAILED,
                    actual_send_via="userbot_reply",
                    error_code="telegram_api_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            await ctx.engine.on_flood_wait("send_message_group", exc)
            await asyncio.sleep(min(int(getattr(exc, "seconds", 0) or 0), 60))
            try:
                msg = await ctx.client.send_message(peer, text)
                await _record_scheduler_action(
                    ctx,
                    trace_action,
                    TRACE_STATUS_OK,
                    actual_send_via="userbot_reply",
                    result={"message_id": getattr(msg, "id", None) or getattr(msg, "message_id", None)},
                    retry_after_flood_wait=True,
                )
                return msg
            except Exception as retry_exc:
                if not isinstance(retry_exc, FloodWaitError) and not hasattr(retry_exc, "seconds"):
                    await _record_scheduler_action(
                        ctx,
                        trace_action,
                        TRACE_STATUS_FAILED,
                        actual_send_via="userbot_reply",
                        error_code="telegram_api_error",
                        error=f"{type(retry_exc).__name__}: {retry_exc}",
                    )
                    raise
                await _record_scheduler_action(
                    ctx,
                    trace_action,
                    TRACE_STATUS_SKIPPED,
                    actual_send_via="userbot_reply",
                    error_code="rate_limited",
                    error=f"{type(retry_exc).__name__}: {retry_exc}",
                )
                if ctx.log is not None:
                    await ctx.log("warn", "[scheduler] send_message still flood-waited after retry; drop once")
                return None

    # 旧测试和外部历史调用仍可能访问下划线方法；保留别名，真正实现走平台方法。
    _tick_once = tick_rules_once
    _resolve_due = resolve_due
    _resolve_once = resolve_once
    _resolve_interval = resolve_interval
    _resolve_cron = resolve_cron
    _advance_after_fire = advance_after_fire
    _fire = fire
    _action_send_message = action_send_message
    _action_run_command = action_run_command
    _action_call_llm = action_call_llm
    _delete_message_after = delete_message_after
    _get_provider_row = get_provider_row
    _get_provider_rows = get_provider_rows
    _persist_rule_config = persist_rule_config
    _send_with_ratelimit = send_with_ratelimit


class SchedulerFacade:
    """暴露给插件的最小调度能力。

    插件只能注册 / 注销自己名下的任务，不能查看或操作其他插件的任务。
    """

    def __init__(self, runtime: PlatformScheduler, owner: str, generation: int) -> None:
        self._runtime = runtime
        self._owner = owner
        self._generation = generation

    def register(
        self,
        job_id: str,
        schedule: dict[str, Any],
        callback: ScheduleCallback,
        *,
        replace: bool = True,
    ) -> None:
        """注册一个运行期任务。

        ``schedule`` 使用和平台定时任务一致的字段：
        ``{"kind": "cron", "cron": "0 * * * *"}``、
        ``{"kind": "interval", "interval_sec": 300}`` 或
        ``{"kind": "once", "fire_at": "2026-05-11T10:00:00+00:00"}``。
        """

        self._runtime.register_job(
            self._owner,
            self._generation,
            job_id,
            schedule,
            callback,
            replace=replace,
        )

    def unregister(self, job_id: str) -> bool:
        """注销当前插件名下的一个任务。"""

        return self._runtime.unregister_job(self._owner, job_id)

    def unregister_all(self) -> int:
        """注销当前插件名下的全部任务。"""

        return self._runtime.unregister_owner(self._owner)

    def list_jobs(self) -> list[dict[str, Any]]:
        """返回当前插件名下任务快照，主要用于插件调试日志。"""

        return [
            item for item in self._runtime.list_runtime_jobs()
            if item.get("owner") == self._owner
        ]


class PlatformScheduler:
    """每账号一个的平台调度器实例。"""

    def __init__(
        self,
        *,
        account_id: int,
        client: Any,
        redis: Any,
        paused: asyncio.Event,
        log_writer: LogWriter | None = None,
        tick_seconds: int = SCHEDULER_TICK_SECONDS,
    ) -> None:
        self.account_id = account_id
        self.client = client
        self.redis = redis
        self.paused = paused
        self.tick_seconds = max(int(tick_seconds), 1)
        self.engine: Any = None
        self._log_writer = log_writer
        self._executor = SchedulerRuleExecutor()
        self._jobs: dict[str, _RuntimeJob] = {}
        self._missing_engine_logged = False

    def attach_engine(self, engine: Any) -> None:
        """注入 worker 的风控引擎。loader 创建 engine 后调用。"""

        self.engine = engine

    def for_plugin(self, owner: str, generation: int) -> SchedulerFacade:
        """创建绑定到某个插件实例的 facade。"""

        return SchedulerFacade(self, owner, generation)

    def register_job(
        self,
        owner: str,
        generation: int,
        job_id: str,
        schedule: dict[str, Any],
        callback: ScheduleCallback,
        *,
        replace: bool = True,
    ) -> None:
        owner = str(owner or "").strip()
        job_id = str(job_id or "").strip()
        if not owner:
            raise ValueError("scheduler job owner 不能为空")
        if not job_id:
            raise ValueError("scheduler job_id 不能为空")
        if not callable(callback):
            raise TypeError("scheduler callback 必须可调用")

        key = self._job_key(owner, job_id)
        if key in self._jobs and not replace:
            raise ValueError(f"scheduler job 已存在: {owner}.{job_id}")

        cfg = dict(schedule or {})
        cfg.setdefault("enabled", True)
        self._jobs[key] = _RuntimeJob(
            owner=owner,
            generation=int(generation or 0),
            job_id=job_id,
            config=cfg,
            callback=callback,
        )

    def unregister_job(self, owner: str, job_id: str) -> bool:
        return self._jobs.pop(self._job_key(owner, job_id), None) is not None

    def unregister_owner(self, owner: str, generation: int | None = None) -> int:
        keys = [
            key for key, job in self._jobs.items()
            if job.owner == owner and (generation is None or job.generation == generation)
        ]
        for key in keys:
            self._jobs.pop(key, None)
        return len(keys)

    def list_runtime_jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "owner": job.owner,
                "generation": job.generation,
                "job_id": job.job_id,
                "config": dict(job.config),
                "fire_count": job.fire_count,
                "last_error": job.last_error,
                "created_at": job.created_at.isoformat(),
            }
            for job in self._jobs.values()
        ]

    async def run(self) -> None:
        """常驻调度循环，由 worker runtime 启动。"""

        await self._emit_log("info", "[scheduler-runtime] 平台调度器已启动")
        while True:
            try:
                if self.paused.is_set():
                    await self.tick_once()
                await asyncio.sleep(self.tick_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._emit_log(
                    "error",
                    f"[scheduler-runtime] 本轮调度出错：{type(exc).__name__}: {exc}",
                )
                await asyncio.sleep(self.tick_seconds)

    async def tick_once(self) -> None:
        """执行一轮平台调度：先跑 GUI 规则，再跑插件运行期任务。"""

        await self.tick_persisted_rules()
        await self.tick_runtime_jobs()

    async def tick_persisted_rules(self) -> None:
        """执行 GUI 配置的 scheduler 规则。"""

        ctx = await self._make_scheduler_context()
        if ctx is None:
            return
        async with AsyncSessionLocal() as db:
            rules = (
                await db.execute(
                    select(Rule)
                    .where(
                        Rule.account_id == self.account_id,
                        Rule.feature_key == FEATURE_SCHEDULER,
                        Rule.enabled.is_(True),
                    )
                    .order_by(Rule.priority.desc(), Rule.id.asc())
                )
            ).scalars().all()
        ctx.rules = list(rules)
        await self._executor.tick_rules_once(ctx)

    async def tick_runtime_jobs(self) -> None:
        """执行插件通过 ``ctx.scheduler`` 注册的运行期任务。"""

        if not self._jobs:
            return
        now = datetime.now(UTC)
        tz = await _get_system_tz()
        for key, job in list(self._jobs.items()):
            cfg = job.config
            if cfg.get("enabled") is False:
                continue

            due, next_fire = self._executor.resolve_due(cfg, now, tz)
            next_iso = _to_iso(next_fire)
            if cfg.get("next_fire") != next_iso:
                cfg["next_fire"] = next_iso
            if cfg.pop("_config_dirty", False):
                job.config = cfg

            if not due:
                continue

            fired_at = datetime.now(UTC)
            payload = ScheduledJob(
                account_id=self.account_id,
                owner=job.owner,
                job_id=job.job_id,
                config=dict(cfg),
                fired_at=fired_at,
                fire_count=job.fire_count + 1,
            )
            try:
                maybe_awaitable = job.callback(payload)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception as exc:  # noqa: BLE001
                job.last_error = f"{type(exc).__name__}: {exc}"
                cfg["last_result"] = "error"
                cfg["last_error"] = job.last_error
                await self._emit_log(
                    "error",
                    (
                        f"插件 {job.owner} 的定时任务 {job.job_id} 执行失败："
                        f"{type(exc).__name__}: {exc}"
                    ),
                    source="plugin",
                    plugin_key=job.owner,
                    scheduler_job_id=job.job_id,
                )
                continue

            job.fire_count += 1
            job.last_error = None
            cfg["last_fire"] = _to_iso(fired_at)
            cfg["last_result"] = "ok"
            cfg["last_error"] = None
            self._executor.advance_after_fire(cfg, fired_at, tz)
            job.config = cfg
            if key not in self._jobs:
                continue

    async def execute_rule(self, rule_id: int) -> SchedulerExecutionResult:
        """手动执行一条 GUI scheduler 规则。"""

        ctx = await self._make_scheduler_context()
        if ctx is None:
            return SchedulerExecutionResult(False, "定时任务调度器尚未初始化")

        async with AsyncSessionLocal() as db:
            rule_row = await db.get(Rule, rule_id)
        if rule_row is None or rule_row.account_id != self.account_id:
            return SchedulerExecutionResult(False, f"rule {rule_id} 不存在或不属于该账号")
        if rule_row.feature_key != FEATURE_SCHEDULER:
            return SchedulerExecutionResult(False, "只能手动执行 scheduler 规则")

        cfg = dict(rule_row.config or {})
        fired_at = datetime.now(UTC)
        ok = await self._executor.fire(ctx, rule_id, cfg)
        if ok:
            tz = await _get_system_tz()
            cfg["last_fire"] = fired_at.isoformat()
            cfg["last_result"] = "ok"
            cfg["last_error"] = None
            self._executor.advance_after_fire(cfg, fired_at, tz)
            result = SchedulerExecutionResult(True)
        else:
            result = SchedulerExecutionResult(False, cfg.get("last_error", "执行失败"))

        async with AsyncSessionLocal() as db:
            row = await db.get(Rule, rule_id)
            if row is not None:
                row.config = cfg
                await db.commit()
        return result

    async def _make_scheduler_context(self) -> PluginContext | None:
        if self.engine is None:
            if not self._missing_engine_logged:
                self._missing_engine_logged = True
                await self._emit_log("warn", "[scheduler-runtime] 风控引擎尚未就绪，暂不执行定时任务")
            return None

        async def ctx_log(level: str, message: str, **detail: Any) -> None:
            detail.pop("plugin_key", None)
            await self._emit_log(
                level,
                message,
                source=str(detail.pop("source", "plugin")),
                plugin_key=FEATURE_SCHEDULER,
                **detail,
            )

        return PluginContext(
            account_id=self.account_id,
            feature_key=FEATURE_SCHEDULER,
            config={},
            rules=[],
            client=self.client,
            engine=self.engine,
            redis=self.redis,
            log=ctx_log,
            scheduler=None,
            generation=0,
        )

    async def _emit_log(
        self,
        level: str,
        message: str,
        *,
        source: str = "system",
        **detail: Any,
    ) -> None:
        if self._log_writer is None:
            log.log(getattr(logging, level.upper(), logging.INFO), message)
            return
        try:
            await self._log_writer(
                self.redis,
                self.account_id,
                level,
                message,
                source=source,
                **detail,
            )
        except Exception:  # noqa: BLE001
            log.exception("写 scheduler runtime 日志失败 account=%s", self.account_id)

    @staticmethod
    def _job_key(owner: str, job_id: str) -> str:
        return f"{owner}:{job_id}"


__all__ = [
    "PlatformScheduler",
    "SCHEDULER_TICK_SECONDS",
    "ScheduledJob",
    "SchedulerExecutionResult",
    "SchedulerFacade",
    "SchedulerRuleExecutor",
    "_croniter_next",
    "_croniter_prev",
    "_get_system_tz",
    "_parse_dt",
    "_to_iso",
    "_to_positive_int",
]
