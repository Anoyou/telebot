"""三层继承合并：默认 ← 模板 ← 账号 ← 规则。

engine 不直接读 DB，由本服务负责把 ``RateLimitTemplate`` / ``Account`` / ``Rule`` 三层
``RateLimitRule`` 拼成 ``EffectiveLimits``，并提供：
  - ``get_effective_factory(db_factory)``：给 worker 用的便利工厂
  - 模板 / 账号风控 CRUD（API 层调）
  - 拟人化 CRUD（API 层调）
"""

from __future__ import annotations

from datetime import time as dtime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.account import Account, HumanizeConfig
from ..db.models.rate_limit import (
    POLICY_QUEUE,
    SCOPE_ACCOUNT,
    SCOPE_TEMPLATE,
    RateLimitRule,
    RateLimitTemplate,
)
from ..worker.ratelimit.engine import EffectiveLimits
from ..worker.ratelimit.humanize import HumanizeOpts

# ─────────────────────────────────────────────────────
# 默认阈值（PRD §L.1 起点建议）
# ─────────────────────────────────────────────────────
_DEFAULTS: dict[str, dict] = {
    "send_message_private": {"per_second": 1, "per_minute": 20, "per_hour": 500},
    "send_message_group": {"per_second": 1, "per_minute": 30, "per_hour": 1000},
    "same_peer_send": {"same_peer_per_minute": 3},
    "edit_message": {"per_minute": 5},
    "delete_message": {"per_minute": 30},
    "forward_message": {"per_minute": 20},
    "callback_query": {"per_minute": 6, "per_hour": 60},
    "read_history": {"per_minute": 30},
    "join_chat": {"per_hour": 5, "per_day": 20},
    "leave_chat": {"per_hour": 5},
    "create_chat": {"per_day": 2},
    "invite_user": {"per_hour": 10, "per_day": 50},
    "dm_stranger": {"per_hour": 3, "per_day": 20},
    "update_profile": {"per_hour": 3},
    "upload_file": {"per_minute": 5},
    "download_file": {"per_minute": 10},
    "search": {"per_minute": 10},
    "webhook_deliver": {"per_second": 2, "per_minute": 60, "per_hour": 1000, "per_day": 5000},
    "api_total": {"per_second": 30, "per_minute": 1000},
}


def default_for(action: str) -> dict:
    """暴露给 API 层用：某 action 的默认阈值（用于"显示继承自默认"提示）。"""
    return dict(_DEFAULTS.get(action, {}))


# ─────────────────────────────────────────────────────
# 三层合并
# ─────────────────────────────────────────────────────
async def get_effective(db: AsyncSession, account_id: int, action: str) -> EffectiveLimits:
    """合并：默认 ← 模板 ← 账号级 ← 规则级（规则级当前未启用）。"""
    out = EffectiveLimits(policy=POLICY_QUEUE, backoff_base=5, backoff_max=1800)
    _apply_dict(out, _DEFAULTS.get(action, {}))

    acc = await db.get(Account, account_id)
    if acc is None:
        return out

    # 模板层
    if acc.template_id:
        rule = (
            await db.execute(
                select(RateLimitRule).where(
                    RateLimitRule.scope == SCOPE_TEMPLATE,
                    RateLimitRule.scope_id == acc.template_id,
                    RateLimitRule.action == action,
                    RateLimitRule.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if rule is not None:
            _apply_rule(out, rule)
        else:
            # 若模板内显式禁用了该 action（enabled=False）→ 视作 disabled
            disabled_rule = (
                await db.execute(
                    select(RateLimitRule).where(
                        RateLimitRule.scope == SCOPE_TEMPLATE,
                        RateLimitRule.scope_id == acc.template_id,
                        RateLimitRule.action == action,
                        RateLimitRule.enabled.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if disabled_rule is not None:
                out.disabled = True

    # 账号层
    rule = (
        await db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_ACCOUNT,
                RateLimitRule.scope_id == account_id,
                RateLimitRule.action == action,
                RateLimitRule.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if rule is not None:
        _apply_rule(out, rule)
    else:
        disabled_rule = (
            await db.execute(
                select(RateLimitRule).where(
                    RateLimitRule.scope == SCOPE_ACCOUNT,
                    RateLimitRule.scope_id == account_id,
                    RateLimitRule.action == action,
                    RateLimitRule.enabled.is_(False),
                )
            )
        ).scalar_one_or_none()
        if disabled_rule is not None:
            out.disabled = True

    # 规则层（按 rule_id 查 SCOPE_RULE）当前 MVP 不传入，预留接口

    return out


def _apply_dict(out: EffectiveLimits, d: dict) -> None:
    """把 dict 字段覆盖到 ``EffectiveLimits``（仅覆盖存在的字段）。"""
    for k, v in d.items():
        if hasattr(out, k):
            setattr(out, k, v)


def _apply_rule(out: EffectiveLimits, rule: RateLimitRule) -> None:
    """把一条 ``RateLimitRule`` 覆盖到 ``EffectiveLimits``。

    阈值字段 None 表示"继承上层"；不为 None 才覆盖。policy / backoff_* 永远以最深一层为准。
    """
    for f in ("per_second", "per_minute", "per_hour", "per_day", "same_peer_per_minute"):
        v = getattr(rule, f)
        if v is not None:
            setattr(out, f, v)
    if rule.policy:
        out.policy = rule.policy
    out.backoff_base = int(rule.backoff_base_seconds)
    out.backoff_max = int(rule.backoff_max_seconds)


def get_effective_factory(db_factory):
    """给 worker 用的便利工厂：返回一个 ``(account_id, action) -> EffectiveLimits`` 协程。

    ``db_factory`` 通常就是 ``AsyncSessionLocal``。
    """

    async def _f(aid: int, action: str) -> EffectiveLimits:
        async with db_factory() as db:
            return await get_effective(db, aid, action)

    return _f


# ─────────────────────────────────────────────────────
# 模板 CRUD（给 API 用）
# ─────────────────────────────────────────────────────
async def list_templates(db: AsyncSession) -> list[RateLimitTemplate]:
    res = await db.execute(select(RateLimitTemplate).order_by(RateLimitTemplate.id.asc()))
    return list(res.scalars().all())


async def create_template(db: AsyncSession, name: str, is_default: bool = False) -> RateLimitTemplate:
    # 若设为默认：把其它模板的 default 清掉，保证唯一
    if is_default:
        await _clear_default(db)
    tpl = RateLimitTemplate(name=name, is_default=is_default)
    db.add(tpl)
    await db.commit()
    await db.refresh(tpl)
    return tpl


async def update_template(
    db: AsyncSession,
    tpl_id: int,
    name: str | None = None,
    is_default: bool | None = None,
) -> RateLimitTemplate | None:
    tpl = await db.get(RateLimitTemplate, tpl_id)
    if tpl is None:
        return None
    if name is not None:
        tpl.name = name
    if is_default is not None:
        if is_default:
            await _clear_default(db, exclude_id=tpl_id)
        tpl.is_default = is_default
    await db.commit()
    await db.refresh(tpl)
    return tpl


async def delete_template(db: AsyncSession, tpl_id: int) -> bool:
    tpl = await db.get(RateLimitTemplate, tpl_id)
    if tpl is None:
        return False
    # 同步删模板下所有 rule
    rules = (
        await db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == SCOPE_TEMPLATE,
                RateLimitRule.scope_id == tpl_id,
            )
        )
    ).scalars().all()
    for r in rules:
        await db.delete(r)
    await db.delete(tpl)
    await db.commit()
    return True


async def _clear_default(db: AsyncSession, exclude_id: int | None = None) -> None:
    res = await db.execute(select(RateLimitTemplate).where(RateLimitTemplate.is_default.is_(True)))
    for tpl in res.scalars():
        if exclude_id is not None and tpl.id == exclude_id:
            continue
        tpl.is_default = False
    await db.flush()


# ─────────────────────────────────────────────────────
# RateLimitRule 读写（按 scope）
# ─────────────────────────────────────────────────────
async def list_rules(db: AsyncSession, scope: str, scope_id: int) -> list[RateLimitRule]:
    res = await db.execute(
        select(RateLimitRule)
        .where(RateLimitRule.scope == scope, RateLimitRule.scope_id == scope_id)
        .order_by(RateLimitRule.action.asc())
    )
    return list(res.scalars().all())


async def upsert_rule(
    db: AsyncSession,
    scope: str,
    scope_id: int,
    action: str,
    *,
    per_second: int | None = None,
    per_minute: int | None = None,
    per_hour: int | None = None,
    per_day: int | None = None,
    same_peer_per_minute: int | None = None,
    policy: str | None = None,
    backoff_base_seconds: int | None = None,
    backoff_max_seconds: int | None = None,
    enabled: bool | None = None,
) -> RateLimitRule:
    """新建或覆盖一条 ``RateLimitRule``（按 scope + scope_id + action 唯一）。"""
    rule = (
        await db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == scope,
                RateLimitRule.scope_id == scope_id,
                RateLimitRule.action == action,
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        rule = RateLimitRule(
            scope=scope,
            scope_id=scope_id,
            action=action,
            policy=policy or POLICY_QUEUE,
        )
        db.add(rule)
    if per_second is not None:
        rule.per_second = per_second
    if per_minute is not None:
        rule.per_minute = per_minute
    if per_hour is not None:
        rule.per_hour = per_hour
    if per_day is not None:
        rule.per_day = per_day
    if same_peer_per_minute is not None:
        rule.same_peer_per_minute = same_peer_per_minute
    if policy is not None:
        rule.policy = policy
    if backoff_base_seconds is not None:
        rule.backoff_base_seconds = backoff_base_seconds
    if backoff_max_seconds is not None:
        rule.backoff_max_seconds = backoff_max_seconds
    if enabled is not None:
        rule.enabled = enabled
    await db.commit()
    await db.refresh(rule)
    return rule


async def delete_rule(db: AsyncSession, scope: str, scope_id: int, action: str) -> bool:
    """删除某 scope 下某 action 的覆盖（恢复继承）。"""
    rule = (
        await db.execute(
            select(RateLimitRule).where(
                RateLimitRule.scope == scope,
                RateLimitRule.scope_id == scope_id,
                RateLimitRule.action == action,
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        return False
    await db.delete(rule)
    await db.commit()
    return True


# ─────────────────────────────────────────────────────
# 拟人化配置 CRUD
# ─────────────────────────────────────────────────────
async def get_humanize(db: AsyncSession, account_id: int) -> HumanizeConfig | None:
    return await db.get(HumanizeConfig, account_id)


async def get_humanize_opts(db: AsyncSession, account_id: int) -> HumanizeOpts:
    """把 ORM 模型转为 ``HumanizeOpts``（engine 用）。

    若账号未配置，返回默认值；并把 ``Account.cold_start_until`` 一并带上。
    """
    cfg = await db.get(HumanizeConfig, account_id)
    acc = await db.get(Account, account_id)
    cold_until = acc.cold_start_until if acc is not None else None
    if cfg is None:
        return HumanizeOpts(cold_start_until=cold_until)
    return HumanizeOpts(
        jitter_pct=cfg.jitter_pct,
        typing_simulate=cfg.typing_simulate,
        typing_min_ms=cfg.typing_min_ms,
        typing_max_ms=cfg.typing_max_ms,
        typing_probability=cfg.typing_probability,
        read_before_reply=cfg.read_before_reply,
        active_window_start=cfg.active_window_start,
        active_window_end=cfg.active_window_end,
        cold_start_days=cfg.cold_start_days,
        cold_start_until=cold_until,
    )


async def upsert_humanize(
    db: AsyncSession,
    account_id: int,
    *,
    jitter_pct: int | None = None,
    typing_simulate: bool | None = None,
    typing_min_ms: int | None = None,
    typing_max_ms: int | None = None,
    typing_probability: int | None = None,
    read_before_reply: bool | None = None,
    active_window_start: dtime | None = None,
    active_window_end: dtime | None = None,
    cold_start_days: int | None = None,
) -> HumanizeConfig:
    cfg = await db.get(HumanizeConfig, account_id)
    if cfg is None:
        cfg = HumanizeConfig(account_id=account_id)
        db.add(cfg)
    for f, v in (
        ("jitter_pct", jitter_pct),
        ("typing_simulate", typing_simulate),
        ("typing_min_ms", typing_min_ms),
        ("typing_max_ms", typing_max_ms),
        ("typing_probability", typing_probability),
        ("read_before_reply", read_before_reply),
        ("active_window_start", active_window_start),
        ("active_window_end", active_window_end),
        ("cold_start_days", cold_start_days),
    ):
        if v is not None:
            setattr(cfg, f, v)
    await db.commit()
    await db.refresh(cfg)
    return cfg


__all__ = [
    "EffectiveLimits",
    "default_for",
    "delete_rule",
    "delete_template",
    "get_effective",
    "get_effective_factory",
    "get_humanize",
    "get_humanize_opts",
    "list_rules",
    "list_templates",
    "create_template",
    "update_template",
    "upsert_humanize",
    "upsert_rule",
]
