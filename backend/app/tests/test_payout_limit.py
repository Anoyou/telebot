"""payout 限额服务测试：mock DB 限额读取 + 假 Redis（复刻 Lua check-and-consume 语义）。

覆盖：
  - 单笔上限：超限直接拒，且不触碰 Redis（不消费日累计）
  - 日累计上限：累计到上限后拒，被拒的那笔不计入
  - 0 = 不限：single_max/daily_max 都为 0 时放行且不触碰 Redis
  - Redis 故障：fail-open 放行
  - 金额非正 / 缺账号：直接放行
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import payout_limit


class _FakeRedis:
    """复刻 _CONSUME_SCRIPT 的 check-and-consume 语义，便于跨调用断言日累计。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, int] = {}
        self.fail = fail
        self.eval_calls = 0

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        return True

    async def eval(self, _script: str, _numkeys: int, *args: Any):
        self.eval_calls += 1
        key = str(args[0])
        daily_max = int(args[1])
        amount = int(args[2])
        used = int(self.store.get(key, 0))
        if daily_max > 0 and (used + amount) > daily_max:
            return [0, used, daily_max]
        self.store[key] = used + amount
        return [1, used + amount, daily_max]


def _patch(monkeypatch, *, limits: dict[str, int] | None = None, redis: _FakeRedis | None = None) -> _FakeRedis:
    async def _fake_load() -> dict[str, int]:
        return dict(limits or {"single_max": 0, "daily_max": 0})

    fake_redis = redis or _FakeRedis()
    monkeypatch.setattr(payout_limit, "_load_payout_limits", _fake_load)
    monkeypatch.setattr(payout_limit, "get_redis", lambda: fake_redis)
    return fake_redis


@pytest.mark.asyncio
async def test_single_max_rejects_without_consuming_redis(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 100, "daily_max": 0})

    ok, reason = await payout_limit.check_and_consume(1, 150)

    assert ok is False
    assert reason is not None and "单笔" in reason
    assert "150" in reason and "100" in reason
    # 单笔超限必须在触碰 Redis 之前短路，避免误消费日累计
    assert redis.eval_calls == 0


@pytest.mark.asyncio
async def test_single_max_allows_within_limit_and_consumes_daily(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 100, "daily_max": 500})

    ok, reason = await payout_limit.check_and_consume(7, 50)

    assert ok is True
    assert reason is None
    assert redis.eval_calls == 1
    assert list(redis.store.values()) == [50]


@pytest.mark.asyncio
async def test_daily_max_rejects_when_exceeded_and_keeps_prior_usage(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 0, "daily_max": 100})

    first_ok, _ = await payout_limit.check_and_consume(2, 60)
    second_ok, reason = await payout_limit.check_and_consume(2, 60)

    assert first_ok is True
    assert second_ok is False
    assert reason is not None and "日累计" in reason
    assert "60" in reason and "100" in reason
    # 被拒的那笔不计入，日累计停在首笔 60
    assert list(redis.store.values()) == [60]


@pytest.mark.asyncio
async def test_zero_limits_mean_unlimited_and_skip_redis(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 0, "daily_max": 0})

    ok, reason = await payout_limit.check_and_consume(3, 999_999)

    assert ok is True
    assert reason is None
    assert redis.eval_calls == 0


@pytest.mark.asyncio
async def test_redis_failure_is_fail_open(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 0, "daily_max": 100}, redis=_FakeRedis(fail=True))

    ok, reason = await payout_limit.check_and_consume(4, 50)

    assert ok is True
    assert reason is None
    # ping 抛错走 fail-open，store 不应被写入
    assert redis.store == {}


@pytest.mark.asyncio
async def test_limit_load_failure_is_fail_open(monkeypatch) -> None:
    async def _boom() -> dict[str, int]:
        raise RuntimeError("db down")

    monkeypatch.setattr(payout_limit, "_load_payout_limits", _boom)

    ok, reason = await payout_limit.check_and_consume(5, 50)

    assert ok is True
    assert reason is None


@pytest.mark.asyncio
async def test_non_positive_amount_or_missing_account_passes_through(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 10, "daily_max": 10})

    assert await payout_limit.check_and_consume(6, 0) == (True, None)
    assert await payout_limit.check_and_consume(6, -5) == (True, None)
    assert await payout_limit.check_and_consume(None, 5) == (True, None)
    assert redis.eval_calls == 0
