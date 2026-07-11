"""风控引擎：三层叠加 + 5 抑制策略 + Telegram 异常自动响应。

⚠ 延迟处理铁律
─────────────────────────────────────────────────────
1. ``acquire()`` 返回 ``wait_seconds`` 给调用方；调用方在真正发请求前 ``await asyncio.sleep(wait_seconds)``。
2. ``policy=queue``：``wait_seconds = 触发窗口的 retry_after + 拟人化抖动``，``allowed=True``。
3. ``policy=backoff``：``wait_seconds = backoff_base * 2^(streak-1)``（封顶到 backoff_max）+ 抖动；``allowed=True``。
4. ``policy=drop``：``wait_seconds=0、allowed=False、outcome=drop``，调用方应直接 return。
5. ``policy=pause``：``wait_seconds=+inf、allowed=False、outcome=pause``；engine 同时把 account.status 改 paused 并广播 IPC 事件。
6. ``policy=notify``：仅落事件不阻塞，``allowed=True、wait_seconds=0、outcome=ok``。

⚠ 异常处理铁律
─────────────────────────────────────────────────────
1. ``FloodWaitError``：写 ``RateLimitOverride(action, multiplier=0.7, ttl=30min)`` + ``OUTCOME_FLOODWAIT`` 事件 +
   把 account.status 改成 ``floodwait`` 并广播；后续 ``acquire`` 自动按 0.7× 折扣阈值。
2. ``PeerFloodError``：写 ``RateLimitOverride(action="dm_stranger", multiplier=0.0, ttl=24h)`` →
   等同停用陌生人私聊 24 小时；落 ``OUTCOME_PEERFLOOD`` 事件。
3. ``SlowModeWaitError``：不写 override；只对该 peer/动作排队 ``e.seconds``；落 ``OUTCOME_SLOWMODE`` 事件。
4. ``AuthKeyUnregistered/SessionRevoked/UserDeactivated``：让 worker 上抛 ``EVT_LOGIN_REQUIRED``，
   engine 自身只落事件（``outcome=drop``，detail 携带异常名）。
5. ``PhoneNumberFloodError``：与 FloodWait 同处理，但 multiplier=0.5、TTL=2h（更严格）。
6. 其他 ``RPCError``：不当作风控触发，向上抛由插件层处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import update

from ...db.base import AsyncSessionLocal
from ...db.models.account import (
    ACCOUNT_STATUS_FLOODWAIT,
    ACCOUNT_STATUS_PAUSED,
    Account,
)
from ...db.models.rate_limit import (
    OUTCOME_BACKOFF,
    OUTCOME_DROP,
    OUTCOME_FLOODWAIT,
    OUTCOME_OK,
    OUTCOME_PAUSE,
    OUTCOME_PEERFLOOD,
    OUTCOME_QUEUED,
    OUTCOME_SLOWMODE,
    POLICY_BACKOFF,
    POLICY_DROP,
    POLICY_NOTIFY,
    POLICY_PAUSE,
    POLICY_QUEUE,
)
from ...redis_client import get_redis
from ..ipc import (
    EVT_LOGIN_REQUIRED,
    EVT_RATELIMIT,
    EVT_STATUS,
    RATELIMIT_EVENT_STREAM,
    RateLimitEventPayload,
    event_channel,
    make_event,
)
from .buckets import TokenBuckets
from .exceptions import AccountPaused, FloodWaitTriggered
from .humanize import HumanizeOpts, cold_start_factor, in_active_window, jitter, seconds_until_active_window
from .overrides import add_override, get_multiplier

# Telethon 异常按需 lazy import：本模块在 API 层也会被 import，避免 telethon 缺失时崩溃。
try:  # pragma: no cover - 仅在缺失 telethon 的极端环境跳过
    from telethon.errors import (  # type: ignore[import-not-found]
        AuthKeyUnregisteredError,
        FloodWaitError,
        PeerFloodError,
        PhoneNumberFloodError,
        SessionRevokedError,
        SlowModeWaitError,
        UserDeactivatedError,
    )
except Exception:  # pragma: no cover
    # 占位：纯文档/单测环境下（无 telethon 时）这些类只用作 isinstance 判断
    class _Missing(Exception):
        seconds: int = 0

    AuthKeyUnregisteredError = _Missing  # type: ignore[assignment, misc]
    FloodWaitError = _Missing  # type: ignore[assignment, misc]
    PeerFloodError = _Missing  # type: ignore[assignment, misc]
    PhoneNumberFloodError = _Missing  # type: ignore[assignment, misc]
    SessionRevokedError = _Missing  # type: ignore[assignment, misc]
    SlowModeWaitError = _Missing  # type: ignore[assignment, misc]
    UserDeactivatedError = _Missing  # type: ignore[assignment, misc]


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 公开数据结构（CONTRACTS.md 强约束）
# ─────────────────────────────────────────────────────
@dataclass
class RateLimitDecision:
    """``acquire`` 的返回值，外部 plugin 直接消费。

    字段语义：
      - ``allowed``: 是否允许调用方继续发请求；False 时 plugin 应立即停止本次动作。
      - ``wait_seconds``: 调用方在发请求前应 sleep 多久；inf 表示账号已暂停。
      - ``outcome``: 与 DB ``rate_limit_event.outcome`` 同义，会被落到事件流。
      - ``reason``: 给日志/UI 看的中文原因（可选）。
    """

    allowed: bool
    wait_seconds: float
    outcome: str
    reason: str | None = None


@dataclass
class EffectiveLimits:
    """三层合并 + override 折算后的最终阈值（service 层填充，engine 消费）。

    把 ``policy``、``backoff_*`` 也带在这里，避免 engine 单独再查一遍 rule 表。
    ``disabled=True`` 表示该账号该动作被显式禁用（rule.enabled=False 或 multiplier=0）。
    """

    per_second: int | None = None
    per_minute: int | None = None
    per_hour: int | None = None
    per_day: int | None = None
    same_peer_per_minute: int | None = None
    policy: str = POLICY_QUEUE
    backoff_base: int = 5
    backoff_max: int = 1800
    disabled: bool = False


# 兼容旧命名（plan 里写的是 _EffectiveLimits）
_EffectiveLimits = EffectiveLimits


# ─────────────────────────────────────────────────────
# 主动动作识别（用于"活跃时段"判断；被动接收无视该限制）
# ─────────────────────────────────────────────────────
_PROACTIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "send_message_private",
        "send_message_group",
        "edit_message",
        "delete_message",
        "forward_message",
        "callback_query",
        "join_chat",
        "leave_chat",
        "create_chat",
        "invite_user",
        "dm_stranger",
        "update_profile",
        "upload_file",
    }
)


def _is_proactive(action: str) -> bool:
    return action in _PROACTIVE_ACTIONS


def _scale_limits(eff: EffectiveLimits, mult: float) -> EffectiveLimits:
    """按 multiplier 折算阈值。mult==1.0 直接返回原对象避免无谓拷贝。"""
    if mult >= 0.999 and mult <= 1.001:
        return eff

    def _s(v: int | None) -> int | None:
        if v is None:
            return None
        # 折算到 0 时返回 0；engine 把 0 视作"该窗口禁用"
        return max(0, int(v * mult))

    return EffectiveLimits(
        per_second=_s(eff.per_second),
        per_minute=_s(eff.per_minute),
        per_hour=_s(eff.per_hour),
        per_day=_s(eff.per_day),
        same_peer_per_minute=_s(eff.same_peer_per_minute),
        policy=eff.policy,
        backoff_base=eff.backoff_base,
        backoff_max=eff.backoff_max,
        disabled=eff.disabled or mult <= 0.0,
    )


# 类型别名：service 层提供的"取有效阈值"协程
GetEffectiveFn = Callable[[int, str], Awaitable[EffectiveLimits]]


class RateLimitEngine:
    """风控引擎，每个 worker 进程实例化一次。

    构造参数：
      - ``account_id``: 当前 worker 服务的账号
      - ``humanize``: ``HumanizeOpts``（由 service 从 ``HumanizeConfig`` 加载）
      - ``get_effective``: 协程，``(account_id, action) -> EffectiveLimits``，
        由 ``services.rate_limit_service.get_effective_factory`` 提供，避免 engine
        直接耦合 DB session。
      - ``redis``: 可选注入；不传则取全局 ``get_redis()``（测试时方便用 fakeredis）。
    """

    def __init__(
        self,
        account_id: int,
        humanize: HumanizeOpts,
        get_effective: GetEffectiveFn,
        redis=None,
    ) -> None:
        self.account_id = account_id
        self.humanize = humanize
        self.get_effective = get_effective
        # 允许测试注入 fakeredis
        self.redis = redis if redis is not None else get_redis()
        self.buckets = TokenBuckets(self.redis)
        # 每个 action 维护"连续失败"计数，policy=backoff 时用于指数退避
        self._backoff_streak: dict[str, int] = {}
        # 内存里的暂停标记，避免后续频繁查 DB
        self._paused = False

    # ─────────────────────────────────────────────────────
    # 主接口：调用方在发请求前调一次
    # ─────────────────────────────────────────────────────
    async def acquire(
        self,
        account_id: int,
        action: str,
        peer_id: int | None = None,
    ) -> RateLimitDecision:
        """检查能否继续；返回 decision。

        调用约定见模块顶部"延迟处理铁律"。
        """
        # 防御：对错的 account_id 直接 drop（一般不会发生）
        if account_id != self.account_id:
            log.warning(
                "engine.acquire 收到 account_id 不一致：传入 %s，本 engine %s",
                account_id,
                self.account_id,
            )
            return RateLimitDecision(False, 0.0, OUTCOME_DROP, reason="account mismatch")

        # 内存级暂停：直接拒绝
        if self._paused:
            return RateLimitDecision(False, float("inf"), OUTCOME_PAUSE, reason="账号已被风控暂停")

        # ── 1. 活跃时段（仅对主动动作生效）──
        if _is_proactive(action) and not in_active_window(self.humanize):
            wait = seconds_until_active_window(self.humanize)
            await self._emit(action, OUTCOME_QUEUED, detail={"reason": "out_of_active_window", "wait": wait})
            return RateLimitDecision(False, wait, OUTCOME_QUEUED, reason="不在活跃时段")

        # ── 2. 取有效阈值（service 层做三层合并）──
        try:
            eff = await self.get_effective(account_id, action)
        except Exception as exc:
            # 取阈值失败时不能让流量穿透：保守按 queue + 1s 处理
            log.exception("get_effective 失败 action=%s: %s", action, exc)
            await self._emit(action, OUTCOME_QUEUED, detail={"reason": "config_unavailable"})
            return RateLimitDecision(True, 1.0, OUTCOME_QUEUED, reason="风控配置暂不可用")

        if eff.disabled:
            await self._emit(action, OUTCOME_DROP, detail={"reason": "disabled"})
            return RateLimitDecision(False, 0.0, OUTCOME_DROP, reason="该动作已禁用")

        # ── 3. 应用 override（FloodWait 等临时折扣）+ 冷启动渐进 ──
        try:
            mult_action = await get_multiplier(self.redis, account_id, action)
        except Exception:
            mult_action = 1.0
        cold_factor = cold_start_factor(self.humanize)
        eff = _scale_limits(eff, mult_action * cold_factor)
        if eff.disabled:
            # 折算后等同禁用
            await self._emit(action, OUTCOME_DROP, detail={"reason": "override_zero"})
            return RateLimitDecision(False, 0.0, OUTCOME_DROP, reason="临时衰减已将阈值压到 0")

        # ── 4. 双扣检查：先不扣减预演（per-action + api_total），全过再扣 ──
        allowed_a, retry_a, idx_a = await self.buckets.check_and_consume(
            account_id,
            action,
            eff.per_second,
            eff.per_minute,
            eff.per_hour,
            eff.per_day,
            eff.same_peer_per_minute,
            peer_id=peer_id,
            consume=False,
        )
        # api_total 兜底桶（所有 MTProto 调用合计）
        try:
            eff_total_raw = await self.get_effective(account_id, "api_total")
        except Exception:
            eff_total_raw = EffectiveLimits()
        try:
            mult_total = await get_multiplier(self.redis, account_id, "api_total")
        except Exception:
            mult_total = 1.0
        eff_total = _scale_limits(eff_total_raw, mult_total * cold_factor)
        allowed_t, retry_t, idx_t = await self.buckets.check_and_consume(
            account_id,
            "api_total",
            eff_total.per_second,
            eff_total.per_minute,
            eff_total.per_hour,
            eff_total.per_day,
            None,
            peer_id=None,
            consume=False,
        )

        if allowed_a and allowed_t:
            # 真正消费令牌（对 per-action 与 api_total 各扣一次）
            await self.buckets.check_and_consume(
                account_id,
                action,
                eff.per_second,
                eff.per_minute,
                eff.per_hour,
                eff.per_day,
                eff.same_peer_per_minute,
                peer_id=peer_id,
                consume=True,
            )
            await self.buckets.check_and_consume(
                account_id,
                "api_total",
                eff_total.per_second,
                eff_total.per_minute,
                eff_total.per_hour,
                eff_total.per_day,
                None,
                peer_id=None,
                consume=True,
            )
            # 命中即重置该 action 的连续失败计数
            self._backoff_streak[action] = 0
            # 拟人化抖动：基线 200ms 加 ±jitter
            wait = jitter(0.2, self.humanize.jitter_pct)
            return RateLimitDecision(True, wait, OUTCOME_OK)

        # ── 5. 超限 → 走策略分支 ──
        retry = max(retry_a, retry_t)
        # 给 retry 也加抖动，避免多 worker 在同一秒同步重试
        retry = jitter(retry, self.humanize.jitter_pct)
        hit_window = idx_a if not allowed_a else idx_t
        return await self._apply_policy(action, eff, retry, hit_window)

    # ─────────────────────────────────────────────────────
    # 策略分支
    # ─────────────────────────────────────────────────────
    async def _apply_policy(
        self,
        action: str,
        eff: EffectiveLimits,
        retry: float,
        hit_window: int,
    ) -> RateLimitDecision:
        """根据 ``eff.policy`` 决定 wait_seconds 与是否阻塞。"""
        detail = {"hit_window": hit_window, "retry_after": retry}

        if eff.policy == POLICY_DROP:
            await self._emit(action, OUTCOME_DROP, detail=detail)
            return RateLimitDecision(False, 0.0, OUTCOME_DROP, reason="超限丢弃")

        if eff.policy == POLICY_BACKOFF:
            # 指数退避：每次未通过 streak +1，wait = base * 2^(streak-1)，封顶到 max
            self._backoff_streak[action] = self._backoff_streak.get(action, 0) + 1
            base = eff.backoff_base * (2 ** (self._backoff_streak[action] - 1))
            wait = min(int(base), int(eff.backoff_max))
            # 取 backoff 与 retry_after 的较大值（避免 backoff 还没等够桶就重试）
            wait = jitter(max(float(wait), retry), self.humanize.jitter_pct)
            await self._emit(
                action,
                OUTCOME_BACKOFF,
                detail={**detail, "wait": wait, "streak": self._backoff_streak[action]},
            )
            # backoff 同样要求调用方 sleep 后重试 → allowed=True
            return RateLimitDecision(True, wait, OUTCOME_BACKOFF, reason="指数退避")

        if eff.policy == POLICY_PAUSE:
            await self._pause_account(reason="超限自动暂停")
            await self._emit(action, OUTCOME_PAUSE, detail=detail)
            return RateLimitDecision(False, float("inf"), OUTCOME_PAUSE, reason="超限自动暂停")

        if eff.policy == POLICY_NOTIFY:
            # 仅告警不阻塞：落事件 outcome=ok 但 detail 标 notify_only
            await self._emit(action, OUTCOME_OK, detail={**detail, "notify_only": True})
            return RateLimitDecision(True, 0.0, OUTCOME_OK, reason="仅告警未抑制")

        # 默认 queue：等到桶恢复
        await self._emit(action, OUTCOME_QUEUED, detail={**detail, "wait": retry})
        return RateLimitDecision(True, retry, OUTCOME_QUEUED, reason="排队等待")

    # ─────────────────────────────────────────────────────
    # Telegram 异常自动响应（worker 在 except 中调用）
    # ─────────────────────────────────────────────────────
    async def on_flood_wait(self, action: str, exc: Exception) -> None:
        """``FloodWaitError`` 触发：写 override 让同动作阈值 ×0.7，TTL 30 分钟。

        同时把 account.status 改 ``floodwait`` 并通过 IPC 通知主进程更新 UI。
        """
        seconds = int(getattr(exc, "seconds", 0) or 0)
        log.warning("FloodWait %ds on action=%s account=%s", seconds, action, self.account_id)
        try:
            async with AsyncSessionLocal() as db:
                await add_override(
                    db,
                    self.redis,
                    self.account_id,
                    action=action,
                    multiplier=0.7,
                    ttl_seconds=30 * 60,
                    reason=f"FloodWait {seconds}s",
                )
                # 把账号置 floodwait 状态便于前端展示（不阻塞执行）
                await db.execute(
                    update(Account)
                    .where(Account.id == self.account_id)
                    .values(status=ACCOUNT_STATUS_FLOODWAIT)
                )
                await db.commit()
        except Exception:
            log.exception("写 FloodWait override 失败")
        await self._emit(
            action,
            OUTCOME_FLOODWAIT,
            detail={"seconds": seconds, "multiplier": 0.7, "ttl_seconds": 30 * 60},
        )
        await self._publish_status(ACCOUNT_STATUS_FLOODWAIT, reason=f"FloodWait {seconds}s")

    async def on_peer_flood(self, action: str = "dm_stranger") -> None:
        """``PeerFloodError`` 触发：停用陌生人私聊 24 小时。"""
        log.warning("PeerFlood account=%s 停用 %s 24h", self.account_id, action)
        try:
            async with AsyncSessionLocal() as db:
                await add_override(
                    db,
                    self.redis,
                    self.account_id,
                    action=action,
                    multiplier=0.0,
                    ttl_seconds=24 * 3600,
                    reason="PeerFlood 自动停用 24h",
                )
        except Exception:
            log.exception("写 PeerFlood override 失败")
        await self._emit(
            action,
            OUTCOME_PEERFLOOD,
            detail={"action_disabled_for": "24h", "multiplier": 0.0},
        )

    async def on_slow_mode(self, action: str, exc: Exception, peer_id: int | None) -> None:
        """``SlowModeWaitError`` 触发：本次单 peer 等待 ``e.seconds`` 秒；不写 override。"""
        seconds = int(getattr(exc, "seconds", 0) or 0)
        log.info(
            "SlowMode %ds on action=%s peer=%s account=%s",
            seconds,
            action,
            peer_id,
            self.account_id,
        )
        await self._emit(
            action,
            OUTCOME_SLOWMODE,
            detail={"seconds": seconds, "peer_id": peer_id},
        )

    async def on_phone_flood(self, action: str, exc: Exception) -> None:
        """``PhoneNumberFloodError``：比 FloodWait 更严重，按 0.5×、TTL 2h 处理。"""
        seconds = int(getattr(exc, "seconds", 0) or 0)
        log.warning("PhoneNumberFlood account=%s action=%s", self.account_id, action)
        try:
            async with AsyncSessionLocal() as db:
                await add_override(
                    db,
                    self.redis,
                    self.account_id,
                    action=action,
                    multiplier=0.5,
                    ttl_seconds=2 * 3600,
                    reason="PhoneNumberFlood 自动收紧",
                )
        except Exception:
            log.exception("写 PhoneNumberFlood override 失败")
        await self._emit(
            action,
            OUTCOME_FLOODWAIT,
            detail={
                "seconds": seconds,
                "kind": "phone_number_flood",
                "multiplier": 0.5,
                "ttl_seconds": 2 * 3600,
            },
        )

    async def on_session_invalid(self, action: str, exc: Exception) -> None:
        """session 失效：engine 仅落事件 + 广播 ``EVT_LOGIN_REQUIRED``，由 supervisor 处置账号状态。"""
        exc_name = type(exc).__name__
        log.error("session 失效 account=%s action=%s exc=%s", self.account_id, action, exc_name)
        await self._emit(
            action,
            OUTCOME_DROP,
            detail={"reason": "session_invalid", "exc": exc_name},
        )
        # 广播 login_required 事件，supervisor 会监听此事件改 account.status
        try:
            await self.redis.publish(
                event_channel(self.account_id),
                make_event(EVT_LOGIN_REQUIRED, exc=exc_name, action=action),
            )
        except Exception:
            log.exception("广播 EVT_LOGIN_REQUIRED 失败")

    # ─────────────────────────────────────────────────────
    # 内部
    # ─────────────────────────────────────────────────────
    async def _pause_account(self, reason: str = "rate_limit") -> None:
        """把账号写为 paused 状态并广播 IPC（幂等）。"""
        if self._paused:
            return
        self._paused = True
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Account)
                    .where(Account.id == self.account_id)
                    .values(status=ACCOUNT_STATUS_PAUSED)
                )
                await db.commit()
        except Exception:
            log.exception("更新 account.status=paused 失败")
        await self._publish_status(ACCOUNT_STATUS_PAUSED, reason=reason)

    async def _publish_status(self, status: str, reason: str | None = None) -> None:
        """通知主进程更新 UI 状态。"""
        try:
            await self.redis.publish(
                event_channel(self.account_id),
                make_event(EVT_STATUS, status=status, reason=reason),
            )
        except Exception:
            log.exception("广播状态变更失败 account=%s status=%s", self.account_id, status)

    async def _emit(self, action: str, outcome: str, detail: dict | None = None) -> None:
        """落事件流（list 落库由主进程消费）+ 实时广播给监听者。

        任何异常都吞掉：风控事件不应反过来影响业务。
        """
        payload = RateLimitEventPayload(
            account_id=self.account_id,
            action=action,
            outcome=outcome,
            detail=detail,
        )
        try:
            pipe = getattr(self.redis, "pipeline", None)
            if callable(pipe):
                p = self.redis.pipeline()
                p.rpush(RATELIMIT_EVENT_STREAM, payload.encode())
                p.ltrim(RATELIMIT_EVENT_STREAM, -2000, -1)
                await p.execute()
            else:
                await self.redis.rpush(RATELIMIT_EVENT_STREAM, payload.encode())
                ltrim = getattr(self.redis, "ltrim", None)
                if callable(ltrim):
                    await ltrim(RATELIMIT_EVENT_STREAM, -2000, -1)
        except Exception:
            log.exception("写 RATELIMIT_EVENT_STREAM 失败")
        try:
            await self.redis.publish(
                event_channel(self.account_id),
                make_event(EVT_RATELIMIT, action=action, outcome=outcome, detail=detail),
            )
        except Exception:
            log.exception("广播 EVT_RATELIMIT 失败")


# ─────────────────────────────────────────────────────
# 装饰器：把 acquire + 异常映射封成一行用法
# ─────────────────────────────────────────────────────
def rate_limited(action: str):
    """装饰一个发起 TG API 调用的协程方法（需要 ``self.engine`` 或 ``engine`` kwarg）。

    用法：
        class MyPlugin:
            @rate_limited("send_message_group")
            async def send(self, peer_id, text):
                await self.client.send_message(peer_id, text)

    行为：
      - 调 ``engine.acquire(action, peer_id=...)`` 拿决策
      - ``decision.allowed=False`` 且 outcome=drop → 直接返回 None
      - ``decision.allowed=False`` 且 outcome=pause → 抛 ``AccountPaused``
      - ``decision.wait_seconds>0`` → ``await asyncio.sleep(wait)``
      - 真正调被装饰函数；按 Telegram 异常分支调对应 ``on_*`` 回调，再视情况向上抛
    """

    def deco(fn):
        async def wrapper(self, *args, **kwargs):
            engine: RateLimitEngine | None = getattr(self, "engine", None) or kwargs.get("engine")
            assert engine is not None, "rate_limited 需要 self.engine 或 engine kwarg"

            # peer_id 推断：优先 kwargs，再尝试位置参数（约定第一个位置参数是 peer）
            peer_id: int | None = kwargs.get("peer_id")
            if peer_id is None and args:
                first = args[0]
                if isinstance(first, int):
                    peer_id = first
            if not isinstance(peer_id, int):
                peer_id = None

            decision = await engine.acquire(engine.account_id, action, peer_id=peer_id)
            if not decision.allowed:
                if decision.outcome == OUTCOME_DROP:
                    return None
                if decision.outcome == OUTCOME_PAUSE:
                    raise AccountPaused(decision.reason or "账号被风控暂停")
                # queued/backoff 在 allowed=True 分支处理；走到这里属于异常返回
                log.warning("意外的 not-allowed outcome=%s", decision.outcome)
                return None
            if decision.wait_seconds > 0:
                await asyncio.sleep(decision.wait_seconds)

            try:
                return await fn(self, *args, **kwargs)
            except FloodWaitError as e:
                await engine.on_flood_wait(action, e)
                raise FloodWaitTriggered(int(getattr(e, "seconds", 0) or 0), action) from e
            except PeerFloodError:
                await engine.on_peer_flood("dm_stranger")
                raise
            except SlowModeWaitError as e:
                await engine.on_slow_mode(action, e, peer_id)
                # 不主动重试，把异常透传给调用方决定
                raise
            except PhoneNumberFloodError as e:
                await engine.on_phone_flood(action, e)
                raise
            except (
                AuthKeyUnregisteredError,
                SessionRevokedError,
                UserDeactivatedError,
            ) as e:
                await engine.on_session_invalid(action, e)
                raise

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        wrapper.__name__ = getattr(fn, "__name__", "rate_limited_wrapper")
        return wrapper

    return deco


__all__ = [
    "EffectiveLimits",
    "GetEffectiveFn",
    "RateLimitDecision",
    "RateLimitEngine",
    "rate_limited",
]
