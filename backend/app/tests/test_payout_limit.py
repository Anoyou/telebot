"""payout 限额服务测试：mock DB 限额读取 + 假 Redis，另含可选真 Redis Lua 回归。

覆盖：
  - 单笔上限：超限直接拒，且不触碰 Redis（不消费日累计）
  - 日累计上限：累计到上限后拒，被拒的那笔不计入
  - 0 = 不限：single_max/daily_max 都为 0 时放行且不触碰 Redis
  - Redis 故障：fail-open 放行
  - 金额非正 / 缺账号：直接放行
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
import redis.asyncio as redis_async

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
        counted_key = str(args[1])
        daily_max = int(args[2])
        amount = int(args[3])
        use_counted = int(args[5])
        used = int(self.store.get(key, 0))
        if use_counted == 1 and counted_key in self.store:
            return [1, used, daily_max]
        if daily_max > 0 and (used + amount) > daily_max:
            return [0, used, daily_max]
        self.store[key] = used + amount
        if use_counted == 1:
            self.store[counted_key] = 1
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
    assert [value for key, value in redis.store.items() if ":daily:" in key] == [50]


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
    assert [value for key, value in redis.store.items() if ":daily:" in key] == [60]


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


@pytest.mark.asyncio
async def test_idempotency_key_counts_daily_usage_once(monkeypatch) -> None:
    redis = _patch(monkeypatch, limits={"single_max": 0, "daily_max": 100})

    first_ok, first_reason = await payout_limit.check_and_consume(8, 60, idempotency_key="pay_same")
    replay_ok, replay_reason = await payout_limit.check_and_consume(8, 60, idempotency_key="pay_same")
    next_ok, next_reason = await payout_limit.check_and_consume(8, 60, idempotency_key="pay_other")

    assert (first_ok, first_reason) == (True, None)
    assert (replay_ok, replay_reason) == (True, None)
    assert next_ok is False
    assert next_reason is not None and "日累计" in next_reason
    daily_values = [value for key, value in redis.store.items() if ":daily:" in key]
    assert daily_values == [60]


@pytest.mark.asyncio
async def test_consume_script_real_redis_accumulates_atomically_and_short_circuits_counted() -> None:
    redis_url = os.environ.get("TELEPILOT_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("set TELEPILOT_TEST_REDIS_URL to run the real Redis Lua eval regression")
    client = redis_async.Redis.from_url(redis_url, decode_responses=True)
    try:
        try:
            await client.ping()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"test Redis unavailable: {exc}")

        token = uuid4().hex
        daily_key = f"test:payout_limit:{token}:daily"
        counted_daily_key = f"test:payout_limit:{token}:counted_daily"
        counted_same_key = f"test:payout_limit:{token}:counted:same"
        counted_other_key = f"test:payout_limit:{token}:counted:other"
        unused_key = f"test:payout_limit:{token}:unused"
        try:
            concurrent_results = await asyncio.gather(
                client.eval(payout_limit._CONSUME_SCRIPT, 2, daily_key, unused_key, 100, 60, 60, 0),
                client.eval(payout_limit._CONSUME_SCRIPT, 2, daily_key, unused_key, 100, 60, 60, 0),
            )

            assert sorted(int(item[0]) for item in concurrent_results) == [0, 1]
            assert await client.get(daily_key) == "60"

            first = await client.eval(
                payout_limit._CONSUME_SCRIPT,
                2,
                counted_daily_key,
                counted_same_key,
                100,
                60,
                60,
                1,
            )
            replay = await client.eval(
                payout_limit._CONSUME_SCRIPT,
                2,
                counted_daily_key,
                counted_same_key,
                100,
                60,
                60,
                1,
            )
            other = await client.eval(
                payout_limit._CONSUME_SCRIPT,
                2,
                counted_daily_key,
                counted_other_key,
                100,
                60,
                60,
                1,
            )

            assert [int(value) for value in first] == [1, 60, 100]
            assert [int(value) for value in replay] == [1, 60, 100]
            assert [int(value) for value in other] == [0, 60, 100]
            assert await client.get(counted_daily_key) == "60"
        finally:
            await client.delete(daily_key, counted_daily_key, counted_same_key, counted_other_key, unused_key)
    finally:
        await client.aclose()
