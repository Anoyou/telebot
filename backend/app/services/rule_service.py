"""Rule / Scheduler CRUD service（供 System Agent 与 API 复用，仅 flush）。"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.account import Account
from ..db.models.feature import BUILTIN_FEATURES, FEATURE_SCHEDULER, Feature
from ..db.models.rule import Rule
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..worker.ipc import CMD_EXECUTE_RULE, IPCMessage, cmd_channel, make_cmd
from .scheduler_target import (
    SchedulerTargetError,
    normalize_scheduler_action_target,
)


class RuleServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def ensure_account(db: AsyncSession, account_id: int) -> Account:
    acc = await db.get(Account, int(account_id))
    if acc is None:
        raise RuleServiceError("ACCOUNT_NOT_FOUND", f"账号 {account_id} 不存在")
    return acc


async def ensure_feature(db: AsyncSession, feature_key: str) -> None:
    if feature_key in BUILTIN_FEATURES:
        return
    if await db.get(Feature, feature_key) is None:
        raise RuleServiceError("FEATURE_NOT_FOUND", f"未知 feature: {feature_key}")


async def get_rule(db: AsyncSession, rule_id: int) -> Rule | None:
    return await db.get(Rule, int(rule_id))


async def list_rules(
    db: AsyncSession,
    *,
    account_id: int | None = None,
    feature_key: str | None = None,
    enabled_only: bool = False,
    limit: int = 100,
) -> list[Rule]:
    q = select(Rule).order_by(Rule.account_id.asc(), Rule.priority.asc(), Rule.id.asc()).limit(
        max(1, min(limit, 500))
    )
    if account_id is not None:
        q = q.where(Rule.account_id == int(account_id))
    if feature_key:
        q = q.where(Rule.feature_key == feature_key)
    if enabled_only:
        q = q.where(Rule.enabled.is_(True))
    result = await db.execute(q)
    return list(result.scalars().all())


def _parse_scheduler_dt(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


async def _system_tz(db: AsyncSession) -> ZoneInfo:
    row = await db.get(SystemSetting, "timezone")
    name = "UTC"
    if row is not None and row.value is not None:
        value = row.value
        if isinstance(value, dict):
            name = str(value.get("value") or value.get("timezone") or "UTC")
        else:
            name = str(value or "UTC")
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


async def normalize_scheduler_config(db: AsyncSession, config: dict[str, Any]) -> dict[str, Any]:
    """与 api/rules 保存语义对齐的 next_fire 刷新。"""

    try:
        cfg = normalize_scheduler_action_target(config)
    except SchedulerTargetError as exc:
        raise RuleServiceError("INVALID_SCHEDULER_TARGET", str(exc)) from exc
    kind = str(cfg.get("kind") or "cron").lower()
    now = datetime.now(UTC)
    tz = await _system_tz(db)

    if kind == "once":
        fire = _parse_scheduler_dt(cfg.get("fire_at") or cfg.get("run_at") or cfg.get("at"))
        cfg["next_fire"] = fire.isoformat() if fire else None
        return cfg

    if kind == "interval":
        try:
            interval = int(cfg.get("interval_sec") or cfg.get("interval_seconds") or cfg.get("seconds") or 0)
        except (TypeError, ValueError):
            interval = 0
        last_fire = _parse_scheduler_dt(cfg.get("last_fire") or cfg.get("last_run_at"))
        if interval <= 0:
            cfg["next_fire"] = None
        elif last_fire is not None:
            cfg["next_fire"] = (last_fire + timedelta(seconds=interval)).isoformat()
        else:
            cfg["next_fire"] = now.isoformat()
        return cfg

    expr = str(cfg.get("cron") or "").strip()
    cfg["_last_cron"] = expr
    cfg["_cron_seconds_mode"] = len(expr.split()) in (6, 7)
    cfg["_cron_timezone"] = getattr(tz, "key", None) or "UTC"
    cfg.pop("_config_dirty", None)
    if not expr:
        cfg["next_fire"] = None
        return cfg
    try:
        local_now = now.astimezone(tz)
        nxt = croniter(expr, local_now).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=tz)
        cfg["next_fire"] = nxt.astimezone(UTC).isoformat()
    except Exception:  # noqa: BLE001
        cfg["next_fire"] = None
    return cfg


async def create_rule(
    db: AsyncSession,
    *,
    account_id: int,
    feature_key: str,
    name: str,
    enabled: bool = True,
    priority: int = 100,
    config: dict[str, Any] | None = None,
) -> Rule:
    if feature_key == "interaction":
        raise RuleServiceError("WRONG_TOOL", "交互规则请使用 interaction.* 工具")
    await ensure_account(db, account_id)
    await ensure_feature(db, feature_key)
    cfg = dict(config or {})
    if feature_key == FEATURE_SCHEDULER:
        cfg = await normalize_scheduler_config(db, cfg)
    rule = Rule(
        account_id=int(account_id),
        feature_key=feature_key,
        name=str(name or "").strip()[:128] or "未命名规则",
        enabled=bool(enabled),
        priority=int(priority),
        config=cfg,
    )
    db.add(rule)
    await db.flush()
    return rule


async def update_rule(
    db: AsyncSession,
    rule_id: int,
    *,
    fields: dict[str, Any],
) -> Rule:
    rule = await get_rule(db, rule_id)
    if rule is None:
        raise RuleServiceError("RULE_NOT_FOUND", f"规则 {rule_id} 不存在")
    if rule.feature_key == "interaction":
        raise RuleServiceError("WRONG_TOOL", "交互规则请使用 interaction.* 工具")

    data = dict(fields or {})
    if "name" in data and data["name"] is not None:
        rule.name = str(data["name"]).strip()[:128] or rule.name
    if "enabled" in data and data["enabled"] is not None:
        rule.enabled = bool(data["enabled"])
    if "priority" in data and data["priority"] is not None:
        rule.priority = int(data["priority"])
    if "config" in data and data["config"] is not None:
        cfg = dict(data["config"])
        if rule.feature_key == FEATURE_SCHEDULER:
            cfg = await normalize_scheduler_config(db, cfg)
        # 明确字段合并：若调用方只传部分 config，应自行合并后传入
        rule.config = cfg
    await db.flush()
    return rule


async def set_enabled(db: AsyncSession, rule_id: int, enabled: bool) -> Rule:
    return await update_rule(db, rule_id, fields={"enabled": bool(enabled)})


async def delete_rule(db: AsyncSession, rule_id: int) -> dict[str, Any]:
    rule = await get_rule(db, rule_id)
    if rule is None:
        raise RuleServiceError("RULE_NOT_FOUND", f"规则 {rule_id} 不存在")
    info = {
        "id": rule.id,
        "account_id": rule.account_id,
        "feature_key": rule.feature_key,
        "name": rule.name,
    }
    await db.delete(rule)
    await db.flush()
    return info


async def execute_scheduler_rule_now(
    account_id: int,
    rule_id: int,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """通过 Worker IPC 立即执行 Scheduler 规则，并等待确定性结果。"""

    try:
        redis = get_redis()
    except Exception as exc:  # noqa: BLE001
        raise RuleServiceError("NO_REDIS", "Redis 不可用，无法连接账号 Worker") from exc

    reply_channel = (
        f"worker_reply:{int(account_id)}:exec_rule:{secrets.token_hex(8)}"
    )
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_channel)
        subscribers = await redis.publish(
            cmd_channel(int(account_id)),
            make_cmd(
                CMD_EXECUTE_RULE,
                rule_id=int(rule_id),
                reply_to=reply_channel,
            ),
        )
        if int(subscribers or 0) <= 0:
            raise RuleServiceError("WORKER_OFFLINE", "账号 Worker 未在线")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(timeout_seconds))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuleServiceError(
                    "WORKER_TIMEOUT",
                    "Worker 响应超时，任务是否已执行未知",
                )
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=remaining,
                    ),
                    timeout=remaining + 0.1,
                )
            except TimeoutError as exc:
                raise RuleServiceError(
                    "WORKER_TIMEOUT",
                    "Worker 响应超时，任务是否已执行未知",
                ) from exc
            if not msg or msg.get("type") != "message":
                continue
            payload = IPCMessage.decode(msg["data"]).payload
            if not bool(payload.get("ok")):
                raise RuleServiceError(
                    "EXECUTE_FAILED",
                    str(payload.get("error") or "Scheduler 任务执行失败"),
                )
            return {
                "ok": True,
                "account_id": int(account_id),
                "rule_id": int(rule_id),
            }
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
        except Exception:  # noqa: BLE001
            pass
        close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "RuleServiceError",
    "create_rule",
    "delete_rule",
    "execute_scheduler_rule_now",
    "ensure_account",
    "ensure_feature",
    "get_rule",
    "list_rules",
    "normalize_scheduler_config",
    "set_enabled",
    "update_rule",
]
