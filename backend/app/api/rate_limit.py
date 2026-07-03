"""风控相关 REST API（PRD §9.4）。

涵盖：
  - 模板 CRUD + 模板下规则 CRUD
  - 账号级风控配置（含继承后的有效阈值）
  - 用量查询（实时 token bucket 用量）
  - 事件流查询
  - 一键调严 / override 列表
  - 拟人化配置 GET/PUT
  - 模拟测算（MVP 简化）
  - 全局总闸 + 全局每秒上限

写操作通过 ``_audit`` 写一条 ``AuditLog``；A Agent 的 audit 服务到位时可改为调用它。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import time as dtime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models.account import Account
from ..db.models.rate_limit import (
    ACTION_KEYS,
    SCOPE_ACCOUNT,
    SCOPE_TEMPLATE,
    RateLimitEvent,
    RateLimitTemplate,
)
from ..db.models.system import SystemSetting
from ..deps import CurrentUser, DBSession
from ..redis_client import get_redis
from ..schemas.rate_limit import (
    POLICIES,
    AccountRateLimitOut,
    EstimateRequest,
    EstimateResponse,
    GlobalLimitsRequest,
    HumanizeOut,
    HumanizeUpdate,
    KillSwitchRequest,
    RateLimitRuleConfig,
    StrictRequest,
    TemplateCreate,
    TemplateOut,
    UsageBucket,
    UsageResponse,
)
from ..services import audit as audit_svc
from ..services import auth_login_security, event_trace
from ..services import rate_limit_service as svc
from ..services.ai_feature import AI_ENABLED_SETTING_KEY, normalize_ai_enabled
from ..worker.ipc import GCMD_KILL_SWITCH, GCMD_RELOAD_GLOBAL, GLOBAL_CHANNEL, make_cmd
from ..worker.ratelimit.buckets import TokenBuckets
from ..worker.ratelimit.overrides import add_override, drop_override, list_active

router = APIRouter(tags=["rate-limit"])


# ─────────────────────────────────────────────────────
# 公用：审计写入（统一走 services.audit；本端补一次 commit）
# ─────────────────────────────────────────────────────
async def _audit(
    db, user_id: int | None, action: str, target: str | None = None, detail: dict | None = None
) -> None:
    """``services.audit.write`` 不内部 commit，这里补一次 commit 确保落库。"""
    await audit_svc.write(db, user_id, action, target=target, detail=detail)
    await db.commit()


def _bad(code: str, msg: str, http_status: int = 400) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, "message": msg})


def _validate_action(action: str) -> None:
    if action not in ACTION_KEYS:
        raise _bad("invalid_action", f"未知 action：{action}")


def _validate_policy(policy: str | None) -> None:
    if policy is not None and policy not in POLICIES:
        raise _bad("invalid_policy", f"未知 policy：{policy}")


def _normalize_command_echo_guard_limit(value: Any) -> int:
    try:
        return max(0, min(50, int(value)))
    except (TypeError, ValueError):
        return 8


def _parse_time(s: str | None) -> dtime | None:
    """``'HH:MM'`` 或 ``'HH:MM:SS'`` → ``datetime.time``。"""
    if not s:
        return None
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        sec = int(parts[2]) if len(parts) > 2 else 0
        return dtime(hour=h, minute=m, second=sec)
    except (ValueError, IndexError) as e:
        raise _bad("invalid_time", f"非法时间格式：{s}") from e


# ─────────────────────────────────────────────────────
# 模板
# ─────────────────────────────────────────────────────
@router.get("/api/rate-templates", response_model=list[TemplateOut])
async def list_templates(db: DBSession, _user: CurrentUser) -> list[TemplateOut]:
    return [TemplateOut.model_validate(t) for t in await svc.list_templates(db)]


@router.post("/api/rate-templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(payload: TemplateCreate, db: DBSession, user: CurrentUser) -> TemplateOut:
    tpl = await svc.create_template(db, name=payload.name, is_default=payload.is_default)
    await _audit(db, user.id, "create_rate_template", target=str(tpl.id), detail={"name": payload.name})
    return TemplateOut.model_validate(tpl)


@router.patch("/api/rate-templates/{tpl_id}", response_model=TemplateOut)
async def patch_template(tpl_id: int, payload: TemplateCreate, db: DBSession, user: CurrentUser) -> TemplateOut:
    tpl = await svc.update_template(db, tpl_id, name=payload.name, is_default=payload.is_default)
    if tpl is None:
        raise _bad("not_found", "模板不存在", 404)
    await _audit(db, user.id, "update_rate_template", target=str(tpl_id))
    return TemplateOut.model_validate(tpl)


@router.delete("/api/rate-templates/{tpl_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(tpl_id: int, db: DBSession, user: CurrentUser) -> None:
    ok = await svc.delete_template(db, tpl_id)
    if not ok:
        raise _bad("not_found", "模板不存在", 404)
    await _audit(db, user.id, "delete_rate_template", target=str(tpl_id))


@router.get("/api/rate-templates/{tpl_id}/rules", response_model=list[RateLimitRuleConfig])
async def list_template_rules(tpl_id: int, db: DBSession, _user: CurrentUser) -> list[RateLimitRuleConfig]:
    if await db.get(RateLimitTemplate, tpl_id) is None:
        raise _bad("not_found", "模板不存在", 404)
    return [RateLimitRuleConfig.model_validate(r) for r in await svc.list_rules(db, SCOPE_TEMPLATE, tpl_id)]


@router.patch("/api/rate-templates/{tpl_id}/rules/{action}", response_model=RateLimitRuleConfig)
async def patch_template_rule(
    tpl_id: int,
    action: str,
    payload: RateLimitRuleConfig,
    db: DBSession,
    user: CurrentUser,
) -> RateLimitRuleConfig:
    _validate_action(action)
    _validate_policy(payload.policy)
    if await db.get(RateLimitTemplate, tpl_id) is None:
        raise _bad("not_found", "模板不存在", 404)
    rule = await svc.upsert_rule(
        db,
        SCOPE_TEMPLATE,
        tpl_id,
        action,
        per_second=payload.per_second,
        per_minute=payload.per_minute,
        per_hour=payload.per_hour,
        per_day=payload.per_day,
        same_peer_per_minute=payload.same_peer_per_minute,
        policy=payload.policy,
        backoff_base_seconds=payload.backoff_base_seconds,
        backoff_max_seconds=payload.backoff_max_seconds,
        enabled=payload.enabled,
    )
    await _audit(db, user.id, "update_template_rule", target=f"tpl:{tpl_id}/{action}")
    await _broadcast_reload()
    return RateLimitRuleConfig.model_validate(rule)


# ─────────────────────────────────────────────────────
# 账号级风控
# ─────────────────────────────────────────────────────
@router.get("/api/accounts/{aid}/rate-limit", response_model=AccountRateLimitOut)
async def get_account_rate_limit(aid: int, db: DBSession, _user: CurrentUser) -> AccountRateLimitOut:
    acc = await db.get(Account, aid)
    if acc is None:
        raise _bad("not_found", "账号不存在", 404)
    rules: list[RateLimitRuleConfig] = []
    # 把每个 ACTION_KEYS 的"有效配置"返回，前端按行渲染并标注继承层
    for action in ACTION_KEYS:
        eff = await svc.get_effective(db, aid, action)
        rules.append(
            RateLimitRuleConfig(
                action=action,
                per_second=eff.per_second,
                per_minute=eff.per_minute,
                per_hour=eff.per_hour,
                per_day=eff.per_day,
                same_peer_per_minute=eff.same_peer_per_minute,
                policy=eff.policy,
                backoff_base_seconds=eff.backoff_base,
                backoff_max_seconds=eff.backoff_max,
                enabled=not eff.disabled,
            )
        )
    return AccountRateLimitOut(template_id=acc.template_id, rules=rules)


@router.put("/api/accounts/{aid}/rate-limit", response_model=AccountRateLimitOut)
async def put_account_rate_limit(
    aid: int,
    payload: list[RateLimitRuleConfig],
    db: DBSession,
    user: CurrentUser,
) -> AccountRateLimitOut:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    for cfg in payload:
        _validate_action(cfg.action)
        _validate_policy(cfg.policy)
        await svc.upsert_rule(
            db,
            SCOPE_ACCOUNT,
            aid,
            cfg.action,
            per_second=cfg.per_second,
            per_minute=cfg.per_minute,
            per_hour=cfg.per_hour,
            per_day=cfg.per_day,
            same_peer_per_minute=cfg.same_peer_per_minute,
            policy=cfg.policy,
            backoff_base_seconds=cfg.backoff_base_seconds,
            backoff_max_seconds=cfg.backoff_max_seconds,
            enabled=cfg.enabled,
        )
    await _audit(db, user.id, "put_account_rate_limit", target=f"acc:{aid}")
    await _broadcast_reload()
    return await get_account_rate_limit(aid, db, user)


@router.patch("/api/accounts/{aid}/rate-limit/{action}", response_model=RateLimitRuleConfig)
async def patch_account_rule(
    aid: int,
    action: str,
    payload: RateLimitRuleConfig,
    db: DBSession,
    user: CurrentUser,
) -> RateLimitRuleConfig:
    _validate_action(action)
    _validate_policy(payload.policy)
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    rule = await svc.upsert_rule(
        db,
        SCOPE_ACCOUNT,
        aid,
        action,
        per_second=payload.per_second,
        per_minute=payload.per_minute,
        per_hour=payload.per_hour,
        per_day=payload.per_day,
        same_peer_per_minute=payload.same_peer_per_minute,
        policy=payload.policy,
        backoff_base_seconds=payload.backoff_base_seconds,
        backoff_max_seconds=payload.backoff_max_seconds,
        enabled=payload.enabled,
    )
    await _audit(db, user.id, "patch_account_rule", target=f"acc:{aid}/{action}")
    await _broadcast_reload()
    return RateLimitRuleConfig.model_validate(rule)


@router.delete("/api/accounts/{aid}/rate-limit/{action}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_rule(aid: int, action: str, db: DBSession, user: CurrentUser) -> None:
    _validate_action(action)
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    await svc.delete_rule(db, SCOPE_ACCOUNT, aid, action)
    await _audit(db, user.id, "delete_account_rule", target=f"acc:{aid}/{action}")
    await _broadcast_reload()


# ─────────────────────────────────────────────────────
# 用量
# ─────────────────────────────────────────────────────
_WINDOW_TO_KEY = {"1m": "minute", "1h": "hour", "24h": "day", "1s": "second"}


@router.get("/api/accounts/{aid}/rate-limit/usage", response_model=UsageResponse)
async def get_usage(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
    window: str = Query("1m", pattern="^(1s|1m|1h|24h)$"),
) -> UsageResponse:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    redis = get_redis()
    buckets = TokenBuckets(redis)
    win_key = _WINDOW_TO_KEY[window]
    bucket_field = {
        "second": "per_second",
        "minute": "per_minute",
        "hour": "per_hour",
        "day": "per_day",
    }[win_key]

    out: list[UsageBucket] = []
    for action in ACTION_KEYS:
        eff = await svc.get_effective(db, aid, action)
        limit = getattr(eff, bucket_field, None)
        used = await buckets.usage(aid, action, win_key)
        pct = (used / limit * 100) if limit else 0.0
        out.append(UsageBucket(action=action, used=float(used), limit=limit, pct=round(pct, 2), warn=pct >= 80))

    actives = await list_active(db, aid)
    overrides = [
        {
            "action": o.action,
            "multiplier": float(o.multiplier),
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
            "reason": o.reason,
        }
        for o in actives
    ]
    return UsageResponse(window=window, buckets=out, active_overrides=overrides)


# ─────────────────────────────────────────────────────
# 事件流
# ─────────────────────────────────────────────────────
@router.get("/api/accounts/{aid}/rate-limit/events")
async def get_events(
    aid: int,
    db: DBSession,
    _user: CurrentUser,
    since: datetime | None = None,
    action: str | None = None,
    outcome: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> list[dict]:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    q = select(RateLimitEvent).where(RateLimitEvent.account_id == aid)
    if since is not None:
        q = q.where(RateLimitEvent.ts >= since)
    if action:
        q = q.where(RateLimitEvent.action == action)
    if outcome:
        q = q.where(RateLimitEvent.outcome == outcome)
    q = q.order_by(RateLimitEvent.ts.desc()).limit(limit)
    res = await db.execute(q)
    return [
        {
            "id": e.id,
            "ts": e.ts.isoformat() if e.ts else None,
            "action": e.action,
            "outcome": e.outcome,
            "detail": e.detail,
        }
        for e in res.scalars().all()
    ]


# ─────────────────────────────────────────────────────
# 一键调严 + override 列表
# ─────────────────────────────────────────────────────
@router.post("/api/accounts/{aid}/rate-limit/strict")
async def post_strict(
    aid: int,
    payload: StrictRequest,
    db: DBSession,
    user: CurrentUser,
) -> dict[str, Any]:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    redis = get_redis()
    # 对所有 action 写 override（一键全局调严）
    for action in ACTION_KEYS:
        await add_override(
            db,
            redis,
            aid,
            action,
            multiplier=float(payload.multiplier),
            ttl_seconds=int(payload.ttl_seconds),
            reason=f"manual_strict by user#{user.id}",
        )
    await _audit(
        db,
        user.id,
        "rate_limit_strict",
        target=f"acc:{aid}",
        detail={"multiplier": payload.multiplier, "ttl_seconds": payload.ttl_seconds},
    )
    return {"applied": len(ACTION_KEYS), "expires_in": payload.ttl_seconds}


@router.delete(
    "/api/accounts/{aid}/rate-limit/overrides/{action}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_override(aid: int, action: str, db: DBSession, user: CurrentUser) -> None:
    _validate_action(action)
    redis = get_redis()
    await drop_override(db, redis, aid, action)
    await _audit(db, user.id, "drop_override", target=f"acc:{aid}/{action}")


@router.get("/api/accounts/{aid}/rate-limit/overrides")
async def get_overrides(aid: int, db: DBSession, _user: CurrentUser) -> list[dict]:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    actives = await list_active(db, aid)
    return [
        {
            "id": o.id,
            "action": o.action,
            "multiplier": float(o.multiplier),
            "reason": o.reason,
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in actives
    ]


# ─────────────────────────────────────────────────────
# 拟人化
# ─────────────────────────────────────────────────────
@router.get("/api/accounts/{aid}/humanize", response_model=HumanizeOut)
async def get_humanize(aid: int, db: DBSession, _user: CurrentUser) -> HumanizeOut:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    cfg = await svc.get_humanize(db, aid)
    if cfg is None:
        # 返回默认值
        return HumanizeOut(
            jitter_pct=15,
            typing_simulate=True,
            typing_min_ms=1000,
            typing_max_ms=3000,
            typing_probability=80,
            read_before_reply=True,
            active_window_start=None,
            active_window_end=None,
            cold_start_days=7,
        )
    return HumanizeOut(
        jitter_pct=cfg.jitter_pct,
        typing_simulate=cfg.typing_simulate,
        typing_min_ms=cfg.typing_min_ms,
        typing_max_ms=cfg.typing_max_ms,
        typing_probability=cfg.typing_probability,
        read_before_reply=cfg.read_before_reply,
        active_window_start=cfg.active_window_start.isoformat() if cfg.active_window_start else None,
        active_window_end=cfg.active_window_end.isoformat() if cfg.active_window_end else None,
        cold_start_days=cfg.cold_start_days,
    )


@router.put("/api/accounts/{aid}/humanize", response_model=HumanizeOut)
async def put_humanize(
    aid: int,
    payload: HumanizeUpdate,
    db: DBSession,
    user: CurrentUser,
) -> HumanizeOut:
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    await svc.upsert_humanize(
        db,
        aid,
        jitter_pct=payload.jitter_pct,
        typing_simulate=payload.typing_simulate,
        typing_min_ms=payload.typing_min_ms,
        typing_max_ms=payload.typing_max_ms,
        typing_probability=payload.typing_probability,
        read_before_reply=payload.read_before_reply,
        active_window_start=_parse_time(payload.active_window_start),
        active_window_end=_parse_time(payload.active_window_end),
        cold_start_days=payload.cold_start_days,
    )
    await _audit(db, user.id, "update_humanize", target=f"acc:{aid}")
    await _broadcast_reload()
    return await get_humanize(aid, db, user)


# ─────────────────────────────────────────────────────
# 模拟测算（MVP 简化：只看 per_minute）
# ─────────────────────────────────────────────────────
@router.post("/api/accounts/{aid}/rate-limit/estimate", response_model=EstimateResponse)
async def estimate(
    aid: int,
    payload: EstimateRequest,
    db: DBSession,
    _user: CurrentUser,
) -> EstimateResponse:
    _validate_action(payload.action)
    if await db.get(Account, aid) is None:
        raise _bad("not_found", "账号不存在", 404)
    if payload.target_count <= 0 or payload.total_count <= 0:
        return EstimateResponse(eta_seconds=0, exceeds_limit=False)
    eff = await svc.get_effective(db, aid, payload.action)
    # 取最严的窗口估算（MVP）
    candidates: list[float] = []
    if eff.per_second:
        candidates.append(payload.total_count / float(eff.per_second))
    if eff.per_minute:
        candidates.append(payload.total_count / float(eff.per_minute) * 60.0)
    if eff.per_hour:
        candidates.append(payload.total_count / float(eff.per_hour) * 3600.0)
    if eff.per_day:
        candidates.append(payload.total_count / float(eff.per_day) * 86400.0)
    eta = max(candidates) if candidates else 0.0
    exceeds = bool(eff.per_day and payload.total_count > eff.per_day)
    return EstimateResponse(eta_seconds=int(eta), exceeds_limit=exceeds)


# ─────────────────────────────────────────────────────
# 全局总闸 + 全局每秒上限
# ─────────────────────────────────────────────────────
async def _get_setting(db, key: str, default) -> Any:
    row = await db.get(SystemSetting, key)
    return row.value if row else default


async def _set_setting(db, key: str, value: Any) -> None:
    row = await db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


@router.get("/api/system/kill-switch")
async def get_kill_switch(db: DBSession, _user: CurrentUser) -> dict[str, bool]:
    val = await _get_setting(db, "kill_switch", {"enabled": False})
    return {"enabled": bool(val.get("enabled", False)) if isinstance(val, dict) else bool(val)}


@router.post("/api/system/kill-switch")
async def post_kill_switch(payload: KillSwitchRequest, db: DBSession, user: CurrentUser) -> dict[str, bool]:
    enabled = bool(payload.enabled)
    await _set_setting(db, "kill_switch", {"enabled": enabled})
    await _audit(
        db,
        user.id,
        "kill_switch",
        target="system",
        detail={"enabled": enabled},
    )
    try:
        from ..worker import supervisor

        if enabled:
            await supervisor.stop_running_workers()
        else:
            await supervisor.start_active_workers()
    except Exception:  # noqa: BLE001
        pass
    # 全局广播给其它监听者 / 多进程场景
    try:
        redis = get_redis()
        await redis.publish(GLOBAL_CHANNEL, make_cmd(GCMD_KILL_SWITCH, enabled=enabled))
    except Exception:
        pass
    return {"enabled": enabled}


@router.get("/api/system/global-limits")
async def get_global_limits(db: DBSession, _user: CurrentUser) -> dict[str, int]:
    val = await _get_setting(db, "global_api_qps", {"api_qps_total": 0})
    qps = val.get("api_qps_total", 0) if isinstance(val, dict) else int(val)
    return {"api_qps_total": int(qps)}


@router.put("/api/system/global-limits")
async def put_global_limits(
    payload: GlobalLimitsRequest, db: DBSession, user: CurrentUser
) -> dict[str, int]:
    await _set_setting(db, "global_api_qps", {"api_qps_total": int(payload.api_qps_total)})
    await _audit(
        db,
        user.id,
        "set_global_limits",
        target="system",
        detail={"api_qps_total": payload.api_qps_total},
    )
    await _broadcast_reload()
    return {"api_qps_total": payload.api_qps_total}


# ─────────────────────────────────────────────────────
# /api/system/settings —— 通用系统设置（命令前缀等）
# 前端 Settings 页用：读 command_prefix；写后通过 IPC 让所有 worker 热加载
# ─────────────────────────────────────────────────────
@router.get("/api/system/settings")
async def get_system_settings(db: DBSession, _user: CurrentUser) -> dict[str, Any]:
    """返回当前生效的全局设置。"""
    prefix_val = await _get_setting(db, "command_prefix", None)
    if isinstance(prefix_val, dict):
        prefix = prefix_val.get("value", ",")
    elif prefix_val is None:
        # 回落到 .env 默认
        from ..settings import settings as app_settings
        prefix = app_settings.command_prefix
    else:
        prefix = str(prefix_val)
    kill_val = await _get_setting(db, "kill_switch", {"enabled": False})
    qps_val = await _get_setting(db, "global_api_qps", {"api_qps_total": 0})
    tz_val = await _get_setting(db, "timezone", {"value": "Asia/Shanghai"})
    remote_update_val = await _get_setting(
        db,
        "remote_plugin_update_check",
        {"enabled": True, "interval_minutes": 360},
    )
    ai_enabled_val = await _get_setting(db, AI_ENABLED_SETTING_KEY, {"enabled": True})
    llm_val = await _get_setting(db, "llm_limits", {})
    log_val = await _get_setting(db, "log_retention", {})
    login_security_val = await _get_setting(db, "login_security", {})
    sudo_val = await _get_setting(db, "sudo_enabled", {"enabled": False})
    prefix_required_val = await _get_setting(db, "command_prefix_required", {"enabled": True})
    echo_guard_val = await _get_setting(db, "command_echo_guard_previous_messages", None)
    if echo_guard_val is None:
        from ..settings import settings as app_settings

        echo_guard_source: Any = app_settings.command_echo_guard_previous_messages
    elif isinstance(echo_guard_val, dict):
        echo_guard_source = echo_guard_val.get("value", 8)
    else:
        echo_guard_source = echo_guard_val
    tz = str(tz_val.get("value", "")) if isinstance(tz_val, dict) else str(tz_val)
    llm_limits = llm_val if isinstance(llm_val, dict) else {}
    log_retention = log_val if isinstance(log_val, dict) else {}
    login_security = auth_login_security.normalize_login_security_config(
        login_security_val if isinstance(login_security_val, dict) else {}
    )
    remote_update = remote_update_val if isinstance(remote_update_val, dict) else {}
    try:
        remote_update_interval = int(remote_update.get("interval_minutes", 360) or 360)
    except (TypeError, ValueError):
        remote_update_interval = 360
    return {
        "command_prefix": prefix,
        "kill_switch": bool(kill_val.get("enabled", False)) if isinstance(kill_val, dict) else bool(kill_val),
        "api_qps_total": int(qps_val.get("api_qps_total", 0)) if isinstance(qps_val, dict) else int(qps_val),
        "timezone": tz or "Asia/Shanghai",
        "remote_plugin_update_check": {
            "enabled": bool(remote_update.get("enabled", True)),
            "interval_minutes": max(30, min(10080, remote_update_interval)),
        },
        "sudo_enabled": bool(sudo_val.get("enabled", False)) if isinstance(sudo_val, dict) else bool(sudo_val),
        "command_prefix_required": (
            bool(prefix_required_val.get("enabled", True))
            if isinstance(prefix_required_val, dict)
            else bool(prefix_required_val)
        ),
        "ai_enabled": normalize_ai_enabled(ai_enabled_val),
        "command_echo_guard_previous_messages": _normalize_command_echo_guard_limit(echo_guard_source),
        "login_security": auth_login_security.login_security_config_to_dict(login_security),
        "llm_limits": {
            "per_minute": max(0, int(llm_limits.get("per_minute", 0) or 0)),
            "daily_requests": max(0, int(llm_limits.get("daily_requests", 0) or 0)),
            "daily_tokens": max(0, int(llm_limits.get("daily_tokens", 0) or 0)),
            "premium_daily": max(0, int(llm_limits.get("premium_daily", 0) or 0)),
        },
        "log_retention": {
            "trace_enabled": bool(log_retention.get("trace_enabled", True)),
            "event_bus_delivery_enabled": bool(log_retention.get("event_bus_delivery_enabled", True)),
            "inline_updates_enabled": bool(log_retention.get("inline_updates_enabled", True)),
            "runtime_log_retention_days": max(
                0, int(log_retention.get("runtime_log_retention_days", 30) or 0)
            ),
            "runtime_log_max_message_chars": max(
                200, int(log_retention.get("runtime_log_max_message_chars", 2000) or 2000)
            ),
            "runtime_log_max_detail_chars": max(
                0, int(log_retention.get("runtime_log_max_detail_chars", 8000) or 0)
            ),
            "runtime_log_min_level": (
                str(log_retention.get("runtime_log_min_level", "info") or "info").lower()
                if str(log_retention.get("runtime_log_min_level", "info") or "info").lower()
                in {"debug", "info", "warn", "error"}
                else "info"
            ),
            "trace_retention_days": max(
                0, int(log_retention.get("trace_retention_days", 30) or 0)
            ),
            "trace_payload_snapshot_retention_days": max(
                0, int(log_retention.get("trace_payload_snapshot_retention_days", 7) or 0)
            ),
            "native_raw_persist_enabled": bool(log_retention.get("native_raw_persist_enabled", False)),
            "native_raw_retention_days": max(
                0, int(log_retention.get("native_raw_retention_days", 1) or 0)
            ),
        },
    }


class _LLMLimitsPatch(BaseModel):
    per_minute: int | None = None
    daily_requests: int | None = None
    daily_tokens: int | None = None
    premium_daily: int | None = None


class _LogRetentionPatch(BaseModel):
    trace_enabled: bool | None = None
    event_bus_delivery_enabled: bool | None = None
    inline_updates_enabled: bool | None = None
    runtime_log_retention_days: int | None = None
    runtime_log_max_message_chars: int | None = None
    runtime_log_max_detail_chars: int | None = None
    runtime_log_min_level: str | None = None
    trace_retention_days: int | None = None
    trace_payload_snapshot_retention_days: int | None = None
    native_raw_persist_enabled: bool | None = None
    native_raw_retention_days: int | None = None


class _RemotePluginUpdateCheckPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None


class _LoginSecurityPatch(BaseModel):
    notify_otp_enabled: bool | None = None
    notify_otp_failed_attempt_threshold: int | None = None
    notify_otp_fail_window_seconds: int | None = None
    notify_otp_ttl_seconds: int | None = None
    notify_otp_max_attempts: int | None = None
    totp_enabled: bool | None = None
    totp_mode: str | None = None
    totp_failed_attempt_threshold: int | None = None
    recovery_code_ttl_seconds: int | None = None


class _SettingsPatch(BaseModel):
    """前端只会传子集；未传字段保持不变。"""

    command_prefix: str | None = None
    timezone: str | None = None
    ai_enabled: bool | None = None
    sudo_enabled: bool | None = None
    command_prefix_required: bool | None = None
    command_echo_guard_previous_messages: int | None = None
    login_security: _LoginSecurityPatch | None = None
    remote_plugin_update_check: _RemotePluginUpdateCheckPatch | None = None
    llm_limits: _LLMLimitsPatch | None = None
    log_retention: _LogRetentionPatch | None = None


@router.patch("/api/system/settings")
async def patch_system_settings(
    payload: _SettingsPatch, db: DBSession, user: CurrentUser
) -> dict[str, Any]:
    if payload.command_prefix is not None:
        prefix = payload.command_prefix.strip()
        if not prefix:
            raise _bad("invalid_prefix", "命令前缀不能为空")
        if len(prefix) > 3:
            raise _bad("invalid_prefix", "命令前缀最长 3 个字符")
        await _set_setting(db, "command_prefix", {"value": prefix})
        await _audit(db, user.id, "set_command_prefix", target="system", detail={"value": prefix})
        # 让所有 worker 热加载新前缀
        await _broadcast_reload()
    if payload.timezone is not None:
        tz = payload.timezone.strip()
        # 校验：空字符串会回到默认上海时区，非空必须是合法 IANA 时区。
        if tz and tz not in __import__("zoneinfo").available_timezones():  # noqa: PLC0415
            raise _bad("invalid_timezone", f"无效时区：{tz}")
        await _set_setting(db, "timezone", {"value": tz or "Asia/Shanghai"})
    if payload.ai_enabled is not None:
        enabled = bool(payload.ai_enabled)
        await _set_setting(db, AI_ENABLED_SETTING_KEY, {"enabled": enabled})
        await _audit(db, user.id, "set_ai_enabled", target="system", detail={"enabled": enabled})
        await _broadcast_reload()
    if payload.sudo_enabled is not None:
        enabled = bool(payload.sudo_enabled)
        await _set_setting(db, "sudo_enabled", {"enabled": enabled})
        await _audit(db, user.id, "set_sudo_enabled", target="system", detail={"enabled": enabled})
        await _broadcast_reload()
    if payload.command_prefix_required is not None:
        enabled = bool(payload.command_prefix_required)
        await _set_setting(db, "command_prefix_required", {"enabled": enabled})
        await _audit(
            db,
            user.id,
            "set_command_prefix_required",
            target="system",
            detail={"enabled": enabled},
        )
        await _broadcast_reload()
    if payload.login_security is not None:
        current = await _get_setting(db, "login_security", {})
        current_config = auth_login_security.normalize_login_security_config(
            current if isinstance(current, dict) else {}
        )
        next_config = auth_login_security.login_security_config_to_dict(current_config)
        data = payload.login_security.model_dump(exclude_unset=True)
        bool_keys = {"notify_otp_enabled", "totp_enabled"}
        bounds = {
            "notify_otp_failed_attempt_threshold": (0, 50),
            "notify_otp_fail_window_seconds": (60, 86400),
            "notify_otp_ttl_seconds": (60, 1800),
            "notify_otp_max_attempts": (1, 10),
            "totp_failed_attempt_threshold": (1, 50),
            "recovery_code_ttl_seconds": (60, 86400),
        }
        for key, value in data.items():
            if value is None:
                continue
            if key in bool_keys:
                next_config[key] = bool(value)
                continue
            if key == "totp_mode":
                mode = str(value).strip().lower()
                if mode not in auth_login_security.TOTP_MODES:
                    raise _bad("invalid_login_security", "totp_mode 必须是 always 或 after_failures")
                next_config[key] = mode
                continue
            lo, hi = bounds[key]
            ivalue = int(value)
            if ivalue < lo or ivalue > hi:
                raise _bad("invalid_login_security", f"{key} 必须在 {lo}~{hi} 之间")
            next_config[key] = ivalue
        # 通知 OTP 没有阈值等于无效开启，直接收敛成关闭，避免误以为已保护。
        if int(next_config.get("notify_otp_failed_attempt_threshold", 0) or 0) <= 0:
            next_config["notify_otp_enabled"] = False
        await _set_setting(db, "login_security", next_config)
        await _audit(db, user.id, "set_login_security", target="system", detail=next_config)
    if payload.remote_plugin_update_check is not None:
        raw = payload.remote_plugin_update_check
        current = await _get_setting(
            db,
            "remote_plugin_update_check",
            {"enabled": True, "interval_minutes": 360},
        )
        current = current if isinstance(current, dict) else {}
        enabled = bool(current.get("enabled", True)) if raw.enabled is None else bool(raw.enabled)
        interval_source = (
            current.get("interval_minutes", 360)
            if raw.interval_minutes is None
            else raw.interval_minutes
        )
        interval = max(30, min(10080, int(interval_source or 360)))
        await _set_setting(
            db,
            "remote_plugin_update_check",
            {"enabled": enabled, "interval_minutes": interval},
        )
        await _audit(
            db,
            user.id,
            "set_remote_plugin_update_check",
            target="system",
            detail={"enabled": enabled, "interval_minutes": interval},
        )
    if payload.command_echo_guard_previous_messages is not None:
        limit = int(payload.command_echo_guard_previous_messages)
        if limit < 0 or limit > 50:
            raise _bad("invalid_command_echo_guard", "命令回声防误触检查条数必须在 0~50 之间")
        await _set_setting(db, "command_echo_guard_previous_messages", {"value": limit})
        await _audit(
            db,
            user.id,
            "set_command_echo_guard",
            target="system",
            detail={"previous_messages": limit},
        )
        await _broadcast_reload()
    if payload.llm_limits is not None:
        current = await _get_setting(db, "llm_limits", {})
        if not isinstance(current, dict):
            current = {}
        data = payload.llm_limits.model_dump(exclude_unset=True)
        next_limits = {
            "per_minute": max(0, int(current.get("per_minute", 0) or 0)),
            "daily_requests": max(0, int(current.get("daily_requests", 0) or 0)),
            "daily_tokens": max(0, int(current.get("daily_tokens", 0) or 0)),
            "premium_daily": max(0, int(current.get("premium_daily", 0) or 0)),
        }
        for key, value in data.items():
            if value is None:
                continue
            if value < 0:
                raise _bad("invalid_llm_limit", "LLM 限额不能为负数")
            next_limits[key] = int(value)
        await _set_setting(db, "llm_limits", next_limits)
        await _audit(db, user.id, "set_llm_limits", target="system", detail=next_limits)
    if payload.log_retention is not None:
        current = await _get_setting(db, "log_retention", {})
        if not isinstance(current, dict):
            current = {}
        data = payload.log_retention.model_dump(exclude_unset=True)
        next_retention = {
            "trace_enabled": bool(current.get("trace_enabled", True)),
            "event_bus_delivery_enabled": bool(current.get("event_bus_delivery_enabled", True)),
            "inline_updates_enabled": bool(current.get("inline_updates_enabled", True)),
            "runtime_log_retention_days": max(
                0, int(current.get("runtime_log_retention_days", 30) or 0)
            ),
            "runtime_log_max_message_chars": max(
                200, int(current.get("runtime_log_max_message_chars", 2000) or 2000)
            ),
            "runtime_log_max_detail_chars": max(
                0, int(current.get("runtime_log_max_detail_chars", 8000) or 0)
            ),
            "runtime_log_min_level": (
                str(current.get("runtime_log_min_level", "info") or "info").lower()
                if str(current.get("runtime_log_min_level", "info") or "info").lower()
                in {"debug", "info", "warn", "error"}
                else "info"
            ),
            "trace_retention_days": max(
                0, int(current.get("trace_retention_days", 30) or 0)
            ),
            "trace_payload_snapshot_retention_days": max(
                0, int(current.get("trace_payload_snapshot_retention_days", 7) or 0)
            ),
            "native_raw_persist_enabled": bool(current.get("native_raw_persist_enabled", False)),
            "native_raw_retention_days": max(
                0, int(current.get("native_raw_retention_days", 1) or 0)
            ),
        }
        bool_keys = {"trace_enabled", "event_bus_delivery_enabled", "inline_updates_enabled", "native_raw_persist_enabled"}
        bounds = {
            "runtime_log_retention_days": (0, 3650),
            "runtime_log_max_message_chars": (200, 20000),
            "runtime_log_max_detail_chars": (0, 50000),
            "trace_retention_days": (0, 3650),
            "trace_payload_snapshot_retention_days": (0, 3650),
            "native_raw_retention_days": (0, 30),
        }
        for key, value in data.items():
            if value is None:
                continue
            if key in bool_keys:
                next_retention[key] = bool(value)
                continue
            if key == "runtime_log_min_level":
                norm = str(value).strip().lower()
                if norm not in {"debug", "info", "warn", "error"}:
                    raise _bad(
                        "invalid_log_retention",
                        "runtime_log_min_level 必须是 debug/info/warn/error",
                    )
                next_retention[key] = norm
                continue
            lo, hi = bounds[key]
            ivalue = int(value)
            if ivalue < lo or ivalue > hi:
                raise _bad("invalid_log_retention", f"{key} 必须在 {lo}~{hi} 之间")
            next_retention[key] = ivalue
        await _set_setting(db, "log_retention", next_retention)
        await _audit(db, user.id, "set_log_retention", target="system", detail=next_retention)
        await event_trace.refresh_trace_settings()
        try:
            from ..worker.supervisor import invalidate_log_retention_cache

            invalidate_log_retention_cache()
        except Exception:  # noqa: BLE001
            pass
    return await get_system_settings(db, user)


# ─────────────────────────────────────────────────────
# 内部工具：广播 reload
# ─────────────────────────────────────────────────────
async def _broadcast_reload() -> None:
    """风控 / 拟人化变更后通知所有 worker 重新加载。

    异常吞掉：广播失败不应影响 API 写库结果。
    """
    try:
        redis = get_redis()
        await redis.publish(GLOBAL_CHANNEL, make_cmd(GCMD_RELOAD_GLOBAL))
    except Exception:
        # 无 redis 时（例如测试环境）静默
        await asyncio.sleep(0)
