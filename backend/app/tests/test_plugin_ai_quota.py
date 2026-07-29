from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.services import plugin_ai_quota


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.zsets: dict[str, dict[str, int]] = {}
        self.lock = asyncio.Lock()

    async def ping(self) -> bool:
        return True

    async def eval(self, script: str, numkeys: int, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        async with self.lock:
            if "minute_used" in script:
                minute_key, minute_amount_key, daily_key = keys
                estimate, per_minute, daily = map(int, argv[:3])
                now_ms = int(argv[5])
                window_ms = int(argv[6])
                reservation_id = str(argv[7])
                zset = self.zsets.setdefault(minute_key, {})
                amounts = self.hashes.setdefault(minute_amount_key, {})
                for rid, score in list(zset.items()):
                    if score <= now_ms - window_ms:
                        zset.pop(rid, None)
                        amounts.pop(rid, None)
                minute_used = sum(amounts.get(rid, 0) for rid in zset)
                daily_used = self.values.get(daily_key, 0)
                if per_minute > 0 and minute_used + estimate > per_minute:
                    return [0, "per_minute", minute_used, per_minute]
                if daily > 0 and daily_used + estimate > daily:
                    return [0, "daily", daily_used, daily]
                zset[reservation_id] = now_ms
                amounts[reservation_id] = estimate
                self.values[minute_key] = minute_used + estimate
                self.values[daily_key] = daily_used + estimate
                return [1, "ok", self.values[minute_key], self.values[daily_key]]

            delta = int(argv[0])
            reservation_id = str(argv[1])
            minute_key, minute_amount_key, daily_key = keys
            current = self.hashes.setdefault(minute_amount_key, {}).get(reservation_id)
            if current is not None:
                next_amount = current + delta
                if next_amount <= 0:
                    self.hashes[minute_amount_key].pop(reservation_id, None)
                    self.zsets.setdefault(minute_key, {}).pop(reservation_id, None)
                else:
                    self.hashes[minute_amount_key][reservation_id] = next_amount
                self.values[minute_key] = sum(
                    self.hashes[minute_amount_key].get(rid, 0)
                    for rid in self.zsets.setdefault(minute_key, {})
                )
            if daily_key in self.values:
                self.values[daily_key] = max(0, self.values.get(daily_key, 0) + delta)
            return [1]


class _FrozenDateTime(datetime):
    current: datetime = datetime(2026, 1, 1, 23, 59, 50, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        if tz is None:
            return cls.current
        return cls.current.astimezone(tz)


@pytest.mark.asyncio
async def test_acquire_uses_redis_pre_reservation_for_concurrent_limit(monkeypatch) -> None:
    fake = _FakeRedis()

    async def _limits(_plugin_key: str):
        return {"per_minute": 100, "daily": 1000}

    async def _noop_usage(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(plugin_ai_quota, "get_redis", lambda: fake)
    monkeypatch.setattr(plugin_ai_quota, "_load_quota_limits", _limits)
    monkeypatch.setattr(plugin_ai_quota, "_write_quota_error_usage", _noop_usage)

    results = await asyncio.gather(
        plugin_ai_quota.acquire("demo", 7, 60),
        plugin_ai_quota.acquire("demo", 7, 60),
        return_exceptions=True,
    )

    tickets = [item for item in results if isinstance(item, plugin_ai_quota.PluginAIQuotaTicket)]
    errors = [item for item in results if isinstance(item, plugin_ai_quota.PluginAIQuotaExceeded)]
    assert len(tickets) == 1
    assert len(errors) == 1
    assert tickets[0].backend == "redis"

    await plugin_ai_quota.release(tickets[0], actual_tokens=20)
    assert fake.values[tickets[0].minute_key] == 20
    assert fake.values[tickets[0].daily_key] == 20


@pytest.mark.asyncio
async def test_release_uses_acquire_day_across_midnight(monkeypatch) -> None:
    fake = _FakeRedis()

    async def _limits(_plugin_key: str):
        return {"per_minute": 100, "daily": 1000}

    async def _noop_usage(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(plugin_ai_quota, "get_redis", lambda: fake)
    monkeypatch.setattr(plugin_ai_quota, "_load_quota_limits", _limits)
    monkeypatch.setattr(plugin_ai_quota, "_write_quota_error_usage", _noop_usage)
    monkeypatch.setattr(plugin_ai_quota, "datetime", _FrozenDateTime)

    _FrozenDateTime.current = datetime(2026, 1, 1, 23, 59, 50, tzinfo=UTC)
    ticket = await plugin_ai_quota.acquire("demo", 7, 60)
    assert ticket.daily_key == "plugin_ai_quota:7:demo:d:20260101"
    assert fake.values[ticket.daily_key] == 60

    _FrozenDateTime.current = datetime(2026, 1, 2, 0, 0, 10, tzinfo=UTC)
    today_minute_key, today_amount_key, today_daily_key = plugin_ai_quota._quota_keys("demo", 7)
    assert today_daily_key == "plugin_ai_quota:7:demo:d:20260102"

    await plugin_ai_quota.release(ticket, actual_tokens=0)

    assert fake.values[ticket.daily_key] == 0
    assert fake.values.get(today_daily_key, 0) == 0
    assert today_daily_key not in fake.values
    assert today_minute_key == ticket.minute_key
    assert today_amount_key == ticket.minute_amount_key


@pytest.mark.asyncio
async def test_acquire_allows_when_quota_disabled(monkeypatch) -> None:
    async def _limits(_plugin_key: str):
        return {"per_minute": 0, "daily": 0}

    monkeypatch.setattr(plugin_ai_quota, "_load_quota_limits", _limits)

    ticket = await plugin_ai_quota.acquire("demo", 7, 999999)

    assert ticket.limited is False
    assert ticket.backend == "disabled"
    assert ticket.minute_key is None


@pytest.mark.asyncio
async def test_db_fallback_counts_plugin_agent_usage_source(monkeypatch) -> None:
    statements = []

    class _DB:
        async def scalar(self, statement):  # noqa: ANN001
            statements.append(statement)
            return 0

    class _Session:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
            return False

    monkeypatch.setattr(plugin_ai_quota, "AsyncSessionLocal", lambda: _Session())

    await plugin_ai_quota._check_db_usage(
        "demo",
        account_id=7,
        estimate=10,
        per_minute=100,
        daily=1000,
    )

    assert len(statements) == 2
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "llm_usage.source = 'plugin:demo'" in sql
    assert "llm_usage.source LIKE 'plugin:demo:' || '%%'" in sql
