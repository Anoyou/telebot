"""RateLimitEngine 测试：mock get_effective + fake redis，验证 5 种 policy 输出。

不接真 DB / redis；engine 内部 DB 写入（_pause_account / on_flood_wait）通过 monkeypatch
``AsyncSessionLocal`` + 替换 ``add_override`` 实现旁路。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models.rate_limit import (
    OUTCOME_BACKOFF,
    OUTCOME_DROP,
    OUTCOME_FLOODWAIT,
    OUTCOME_OK,
    OUTCOME_PAUSE,
    OUTCOME_PEERFLOOD,
    OUTCOME_QUEUED,
    POLICY_BACKOFF,
    POLICY_DROP,
    POLICY_NOTIFY,
    POLICY_PAUSE,
    POLICY_QUEUE,
)
from app.worker.ratelimit import overrides as overrides_mod
from app.worker.ratelimit.engine import (
    EffectiveLimits,
    RateLimitEngine,
)
from app.worker.ratelimit.exceptions import AccountPaused, FloodWaitTriggered
from app.worker.ratelimit.humanize import HumanizeOpts


# ─────────────────────────────────────────────────────
# 极简 fake redis（engine 用到的方法集合）
# ─────────────────────────────────────────────────────
class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.list_pushes: list[tuple[str, str]] = []
        self.kv: dict[str, str] = {}

    async def script_load(self, _script: str) -> str:
        return "fake-sha"

    async def evalsha(self, _sha, numkeys, *args):
        # 默认放行，让 engine 走 OK 路径；某些用例会替换此方法返回挡板
        return [1, 0, 0]

    async def get(self, key: str):
        if key.startswith("rlovr:"):
            return self.kv.get(key, "1.0")
        return self.kv.get(key)

    async def set(self, key: str, val: str, ex: int = 0) -> bool:
        self.kv[key] = val
        return True

    async def delete(self, key: str) -> int:
        return int(self.kv.pop(key, None) is not None)

    async def rpush(self, key: str, val: str) -> int:
        self.list_pushes.append((key, val))
        return len(self.list_pushes)

    async def publish(self, channel: str, msg: str) -> int:
        self.published.append((channel, msg))
        return 1

    async def zremrangebyscore(self, *_a, **_kw) -> int:
        return 0

    async def zcard(self, *_a, **_kw) -> int:
        return 0


def _make_get_effective(eff: EffectiveLimits, total: EffectiveLimits | None = None):
    """构造 get_effective 协程：对指定 action 返 eff，其它走 total（默认无限制）。"""
    total = total or EffectiveLimits()

    async def _f(_aid: int, action: str) -> EffectiveLimits:
        if action == "api_total":
            return total
        return eff

    return _f


def _engine(eff: EffectiveLimits, redis=None, opts: HumanizeOpts | None = None) -> RateLimitEngine:
    return RateLimitEngine(
        account_id=1,
        humanize=opts or HumanizeOpts(jitter_pct=0),
        get_effective=_make_get_effective(eff),
        redis=redis or _FakeRedis(),
    )


@pytest.mark.asyncio
async def test_override_cache_miss_rehydrates_from_database(monkeypatch) -> None:
    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _stmt):
            return SimpleNamespace(
                multiplier=0.5,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

    redis = _FakeRedis()
    redis.kv.pop("rlovr:1:send_message_group", None)
    original_get = redis.get

    async def _missing_once(key: str):
        if key.startswith("rlovr:") and key not in redis.kv:
            return None
        return await original_get(key)

    redis.get = _missing_once  # type: ignore[method-assign]
    monkeypatch.setattr(overrides_mod, "AsyncSessionLocal", lambda: _DB())

    assert await overrides_mod.get_multiplier(redis, 1, "send_message_group") == 0.5
    assert redis.kv["rlovr:1:send_message_group"] == "0.5"


# ─────────────────────────────────────────────────────
# 默认 evalsha 全部放行 → 命中 OK 路径
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_acquire_ok_returns_jitter_only() -> None:
    eff = EffectiveLimits(per_minute=10, policy=POLICY_QUEUE)
    eng = _engine(eff)
    d = await eng.acquire(1, "send_message_group")
    assert d.allowed
    assert d.outcome == OUTCOME_OK
    assert d.wait_seconds >= 0
    assert d.wait_seconds <= 0.21  # 基线 0.2s + 0% 抖动


@pytest.mark.asyncio
async def test_acquire_disabled_drops() -> None:
    eff = EffectiveLimits(disabled=True)
    eng = _engine(eff)
    d = await eng.acquire(1, "send_message_group")
    assert not d.allowed
    assert d.outcome == OUTCOME_DROP


@pytest.mark.asyncio
async def test_acquire_account_mismatch_drops() -> None:
    eng = _engine(EffectiveLimits())
    d = await eng.acquire(account_id=999, action="x")
    assert not d.allowed
    assert d.outcome == OUTCOME_DROP


# ─────────────────────────────────────────────────────
# 模拟 evalsha 返回挡板：验证 5 种 policy 分支
# ─────────────────────────────────────────────────────
class _BlockingRedis(_FakeRedis):
    """所有 evalsha 都返回 (0, retry, 1) → 必挡。"""

    def __init__(self, retry: float = 12.5) -> None:
        super().__init__()
        self.retry = retry

    async def evalsha(self, _sha, numkeys, *args):
        return [0, self.retry, 1]


@pytest.mark.asyncio
async def test_policy_drop_returns_drop() -> None:
    eff = EffectiveLimits(per_minute=1, policy=POLICY_DROP)
    eng = _engine(eff, redis=_BlockingRedis())
    d = await eng.acquire(1, "x")
    assert not d.allowed
    assert d.wait_seconds == 0
    assert d.outcome == OUTCOME_DROP


@pytest.mark.asyncio
async def test_policy_queue_returns_retry_after() -> None:
    eff = EffectiveLimits(per_minute=1, policy=POLICY_QUEUE)
    eng = _engine(eff, redis=_BlockingRedis(retry=10.0))
    d = await eng.acquire(1, "x")
    assert d.allowed  # queue 模式下 allowed=True，由调用方 sleep
    assert d.outcome == OUTCOME_QUEUED
    assert d.wait_seconds >= 10.0


@pytest.mark.asyncio
async def test_policy_backoff_exponential() -> None:
    eff = EffectiveLimits(per_minute=1, policy=POLICY_BACKOFF, backoff_base=5, backoff_max=1800)
    eng = _engine(eff, redis=_BlockingRedis(retry=0))
    d1 = await eng.acquire(1, "x")
    d2 = await eng.acquire(1, "x")
    d3 = await eng.acquire(1, "x")
    assert d1.outcome == OUTCOME_BACKOFF
    # streak: 1 → 5s, 2 → 10s, 3 → 20s（jitter_pct=0 时严格相等）
    assert d1.wait_seconds == pytest.approx(5.0, abs=0.01)
    assert d2.wait_seconds == pytest.approx(10.0, abs=0.01)
    assert d3.wait_seconds == pytest.approx(20.0, abs=0.01)


@pytest.mark.asyncio
async def test_policy_backoff_capped() -> None:
    """指数退避封顶到 backoff_max。"""
    eff = EffectiveLimits(per_minute=1, policy=POLICY_BACKOFF, backoff_base=10, backoff_max=15)
    eng = _engine(eff, redis=_BlockingRedis(retry=0))
    for _ in range(5):
        d = await eng.acquire(1, "x")
    assert d.wait_seconds <= 15.0 + 0.01


@pytest.mark.asyncio
async def test_policy_pause_sets_paused_and_inf_wait() -> None:
    eff = EffectiveLimits(per_minute=1, policy=POLICY_PAUSE)
    eng = _engine(eff, redis=_BlockingRedis())
    # 旁路掉 _pause_account 里的 DB 写入
    with patch.object(RateLimitEngine, "_pause_account", new=AsyncMock()) as mock_pause:
        d = await eng.acquire(1, "x")
    assert not d.allowed
    assert d.wait_seconds == float("inf")
    assert d.outcome == OUTCOME_PAUSE
    mock_pause.assert_awaited_once()


@pytest.mark.asyncio
async def test_policy_notify_does_not_block() -> None:
    eff = EffectiveLimits(per_minute=1, policy=POLICY_NOTIFY)
    eng = _engine(eff, redis=_BlockingRedis())
    d = await eng.acquire(1, "x")
    assert d.allowed
    assert d.wait_seconds == 0
    assert d.outcome == OUTCOME_OK


@pytest.mark.asyncio
async def test_paused_engine_short_circuits() -> None:
    """已被暂停的 engine：后续 acquire 直接返 pause、wait=inf。"""
    eng = _engine(EffectiveLimits())
    eng._paused = True
    d = await eng.acquire(1, "x")
    assert not d.allowed
    assert d.wait_seconds == float("inf")
    assert d.outcome == OUTCOME_PAUSE


# ─────────────────────────────────────────────────────
# 异常自动响应
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_on_flood_wait_writes_override_and_emits_event() -> None:
    redis = _FakeRedis()
    eng = _engine(EffectiveLimits(), redis=redis)

    class _Exc(Exception):
        seconds = 60

    with patch("app.worker.ratelimit.engine.add_override", new=AsyncMock()) as add_ovr, patch(
        "app.worker.ratelimit.engine.AsyncSessionLocal"
    ) as ssm:
        ssm.return_value.__aenter__.return_value = AsyncMock()
        ssm.return_value.__aexit__.return_value = False
        await eng.on_flood_wait("send_message_group", _Exc())

    add_ovr.assert_awaited_once()
    args, kwargs = add_ovr.call_args
    assert kwargs["multiplier"] == 0.7
    assert kwargs["ttl_seconds"] == 30 * 60
    # 事件流必须收到一条 floodwait
    assert any(OUTCOME_FLOODWAIT in v for _, v in redis.list_pushes)


@pytest.mark.asyncio
async def test_on_peer_flood_disables_dm_stranger() -> None:
    redis = _FakeRedis()
    eng = _engine(EffectiveLimits(), redis=redis)
    with patch("app.worker.ratelimit.engine.add_override", new=AsyncMock()) as add_ovr, patch(
        "app.worker.ratelimit.engine.AsyncSessionLocal"
    ) as ssm:
        ssm.return_value.__aenter__.return_value = AsyncMock()
        ssm.return_value.__aexit__.return_value = False
        await eng.on_peer_flood()
    args, kwargs = add_ovr.call_args
    assert kwargs["multiplier"] == 0.0
    assert kwargs["ttl_seconds"] == 24 * 3600
    assert kwargs["action"] == "dm_stranger"
    assert any(OUTCOME_PEERFLOOD in v for _, v in redis.list_pushes)


@pytest.mark.asyncio
async def test_on_slow_mode_emits_event_only() -> None:
    redis = _FakeRedis()
    eng = _engine(EffectiveLimits(), redis=redis)

    class _Exc(Exception):
        seconds = 30

    # SlowMode 不应该写 override
    with patch("app.worker.ratelimit.engine.add_override", new=AsyncMock()) as add_ovr:
        await eng.on_slow_mode("send_message_group", _Exc(), peer_id=42)
    add_ovr.assert_not_awaited()
    # 落事件 outcome=slowmode
    assert any("slowmode" in v for _, v in redis.list_pushes)


@pytest.mark.asyncio
async def test_on_session_invalid_publishes_login_required() -> None:
    redis = _FakeRedis()
    eng = _engine(EffectiveLimits(), redis=redis)
    await eng.on_session_invalid("any", RuntimeError("dead"))
    # 必有一条 publish 是 login_required
    assert any('"type":"login_required"' in m for _, m in redis.published)


# ─────────────────────────────────────────────────────
# 活跃时段
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_out_of_active_window_queues() -> None:
    from datetime import time as dtime

    opts = HumanizeOpts(
        jitter_pct=0,
        active_window_start=dtime(9, 0),
        active_window_end=dtime(10, 0),
    )

    eng = _engine(EffectiveLimits(per_minute=10, policy=POLICY_QUEUE), opts=opts)
    # 强行 patch in_active_window 返 False
    with patch("app.worker.ratelimit.engine.in_active_window", return_value=False), patch(
        "app.worker.ratelimit.engine.seconds_until_active_window", return_value=42.0
    ):
        d = await eng.acquire(1, "send_message_group")
    assert not d.allowed
    assert d.outcome == OUTCOME_QUEUED
    assert d.wait_seconds == 42.0


# ─────────────────────────────────────────────────────
# 装饰器
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_decorator_drops_stops_call() -> None:
    """decision.outcome=drop 时被装饰函数不应被调用。"""
    from app.worker.ratelimit.engine import rate_limited

    eff = EffectiveLimits(per_minute=1, policy=POLICY_DROP)
    eng = _engine(eff, redis=_BlockingRedis())

    called = []

    class _P:
        engine = eng

        @rate_limited("send_message_group")
        async def go(self):
            called.append(1)
            return "done"

    out = await _P().go()
    assert out is None
    assert called == []


@pytest.mark.asyncio
async def test_decorator_pause_raises_account_paused() -> None:
    from app.worker.ratelimit.engine import rate_limited

    eff = EffectiveLimits(per_minute=1, policy=POLICY_PAUSE)
    eng = _engine(eff, redis=_BlockingRedis())

    class _P:
        engine = eng

        @rate_limited("x")
        async def go(self):
            return "done"

    with patch.object(RateLimitEngine, "_pause_account", new=AsyncMock()):
        with pytest.raises(AccountPaused):
            await _P().go()


@pytest.mark.asyncio
async def test_decorator_floodwait_calls_on_flood_wait_and_raises() -> None:
    """装饰器捕获 FloodWaitError → 调 engine.on_flood_wait → 抛 FloodWaitTriggered。

    通过把 engine 模块里的 ``FloodWaitError`` 替换成自定义异常类绕开 Telethon 真实
    构造器签名差异（不同版本 Telethon 的 FloodWaitError.__init__ 签名不一致）。
    """
    from app.worker.ratelimit import engine as eng_mod
    from app.worker.ratelimit.engine import rate_limited

    class _FW(Exception):
        seconds = 60

    eff = EffectiveLimits(per_minute=10, policy=POLICY_QUEUE)
    eng = _engine(eff)

    class _P:
        engine = eng

        @rate_limited("send_message_group")
        async def go(self):
            raise _FW()

    eng.on_flood_wait = AsyncMock()  # type: ignore[method-assign]
    with patch.object(eng_mod, "FloodWaitError", _FW):
        with pytest.raises(FloodWaitTriggered) as ei:
            await _P().go()
    assert ei.value.seconds == 60
    eng.on_flood_wait.assert_awaited_once()


# ─────────────────────────────────────────────────────
# get_effective 失败时降级为 queue 1s
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_effective_failure_degrades_safely() -> None:
    async def _broken(_aid, _action):
        raise RuntimeError("db down")

    eng = RateLimitEngine(
        account_id=1,
        humanize=HumanizeOpts(jitter_pct=0),
        get_effective=_broken,
        redis=_FakeRedis(),
    )
    d = await eng.acquire(1, "x")
    assert d.allowed
    assert d.wait_seconds == 1.0
    assert d.outcome == OUTCOME_QUEUED
