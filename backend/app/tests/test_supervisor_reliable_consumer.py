"""supervisor 可靠消费 helper 的单元测试。"""
from __future__ import annotations

import asyncio
import json

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.worker import supervisor


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, list[str]] = {}

    async def llen(self, key: str) -> int:
        await asyncio.sleep(0)
        return len(self.data.get(key, []))

    async def blmove(
        self,
        source: str,
        target: str,
        timeout: int = 0,
        src: str = "LEFT",
        destination: str = "RIGHT",
        **kwargs,  # noqa: ANN003
    ) -> str | None:
        await asyncio.sleep(0)
        del timeout
        dest_side = kwargs.get("dest", destination)
        return await self._move(source, target, src=src, dest_side=dest_side)

    async def lmove(
        self,
        source: str,
        target: str,
        src: str = "LEFT",
        destination: str = "RIGHT",
        **kwargs,  # noqa: ANN003
    ) -> str | None:
        await asyncio.sleep(0)
        dest_side = kwargs.get("dest", destination)
        return await self._move(source, target, src=src, dest_side=dest_side)

    async def _move(
        self,
        source: str,
        target: str,
        src: str = "LEFT",
        dest_side: str = "RIGHT",
    ) -> str | None:
        src_list = self.data.setdefault(source, [])
        if not src_list:
            return None
        if src == "LEFT":
            item = src_list.pop(0)
        else:
            item = src_list.pop()
        dst_list = self.data.setdefault(target, [])
        if dest_side == "LEFT":
            dst_list.insert(0, item)
        else:
            dst_list.append(item)
        return item

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        await asyncio.sleep(0)
        arr = self.data.get(key, [])
        if end == -1:
            return arr[start:]
        return arr[start : end + 1]

    async def lrem(self, key: str, count: int, value: str) -> int:
        await asyncio.sleep(0)
        if count != 1:
            raise AssertionError("测试 fake 只支持 count=1")
        arr = self.data.setdefault(key, [])
        try:
            idx = arr.index(value)
        except ValueError:
            return 0
        arr.pop(idx)
        return 1


class _TimeoutOnceRedis(_FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.timeout_once = True

    async def blmove(self, *args, **kwargs) -> str | None:  # noqa: ANN002, ANN003
        if self.timeout_once:
            self.timeout_once = False
            raise TimeoutError("idle")
        return await super().blmove(*args, **kwargs)


class _RedisTimeoutOnceRedis(_FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.timeout_once = True

    async def blmove(self, *args, **kwargs) -> str | None:  # noqa: ANN002, ANN003
        if self.timeout_once:
            self.timeout_once = False
            raise RedisTimeoutError("idle")
        return await super().blmove(*args, **kwargs)


class _FakeSession:
    def __init__(self, *, fail_commit: bool) -> None:
        self.fail_commit = fail_commit
        self.rows: list[object] = []

    def add_all(self, rows: list[object]) -> None:
        self.rows.extend(rows)

    async def commit(self) -> None:
        await asyncio.sleep(0)
        if self.fail_commit:
            raise RuntimeError("db down")


class _SessionFactory:
    def __init__(self, *, fail_commit: bool) -> None:
        self.fail_commit = fail_commit
        self.last_session: _FakeSession | None = None

    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        self.last_session = _FakeSession(fail_commit=self.fail_commit)
        return self.last_session

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


@pytest.mark.asyncio
async def test_reliable_consumer_keep_inflight_on_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    payload = json.dumps({"account_id": 1, "action": "send", "outcome": "ok"})
    redis.data["ratelimit_event_stream"] = [payload]
    factory = _SessionFactory(fail_commit=True)
    monkeypatch.setattr(supervisor, "get_redis", lambda: redis)
    monkeypatch.setattr(supervisor, "AsyncSessionLocal", factory)

    task = asyncio.create_task(
        supervisor._consume_stream_reliable(
            stream_key="ratelimit_event_stream",
            inflight_key="ratelimit_event_stream:inflight",
            build_row=supervisor._build_ratelimit_event_row,
            consumer_name="test",
            batch_size=10,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    await task

    assert redis.data["ratelimit_event_stream"] == []
    assert redis.data["ratelimit_event_stream:inflight"] == [payload]


@pytest.mark.asyncio
async def test_reliable_consumer_ack_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _FakeRedis()
    payload = json.dumps(
        {"account_id": 2, "level": "info", "source": "test", "message": "hello"}
    )
    redis.data["runtime_log_stream:inflight"] = [payload]
    factory = _SessionFactory(fail_commit=False)
    monkeypatch.setattr(supervisor, "get_redis", lambda: redis)
    monkeypatch.setattr(supervisor, "AsyncSessionLocal", factory)

    task = asyncio.create_task(
        supervisor._consume_stream_reliable(
            stream_key="runtime_log_stream",
            inflight_key="runtime_log_stream:inflight",
            build_row=supervisor._build_runtime_log_row,
            consumer_name="test",
            batch_size=10,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    await task

    assert redis.data["runtime_log_stream:inflight"] == []
    assert factory.last_session is not None
    assert len(factory.last_session.rows) == 1


@pytest.mark.asyncio
async def test_reliable_consumer_continues_after_redis_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _TimeoutOnceRedis()
    payload = json.dumps(
        {"account_id": 2, "level": "info", "source": "test", "message": "hello"}
    )
    redis.data["runtime_log_stream"] = [payload]
    factory = _SessionFactory(fail_commit=False)
    monkeypatch.setattr(supervisor, "get_redis", lambda: redis)
    monkeypatch.setattr(supervisor, "AsyncSessionLocal", factory)

    task = asyncio.create_task(
        supervisor._consume_stream_reliable(
            stream_key="runtime_log_stream",
            inflight_key="runtime_log_stream:inflight",
            build_row=supervisor._build_runtime_log_row,
            consumer_name="test",
            batch_size=10,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    await task

    assert redis.data["runtime_log_stream"] == []
    assert redis.data["runtime_log_stream:inflight"] == []
    assert factory.last_session is not None
    assert len(factory.last_session.rows) == 1


@pytest.mark.asyncio
async def test_reliable_consumer_treats_redis_client_timeout_as_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _RedisTimeoutOnceRedis()
    payload = json.dumps(
        {"account_id": 2, "level": "info", "source": "test", "message": "hello"}
    )
    redis.data["runtime_log_stream"] = [payload]
    factory = _SessionFactory(fail_commit=False)
    monkeypatch.setattr(supervisor, "get_redis", lambda: redis)
    monkeypatch.setattr(supervisor, "AsyncSessionLocal", factory)

    task = asyncio.create_task(
        supervisor._consume_stream_reliable(
            stream_key="runtime_log_stream",
            inflight_key="runtime_log_stream:inflight",
            build_row=supervisor._build_runtime_log_row,
            consumer_name="test",
            batch_size=10,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    await task

    assert redis.data["runtime_log_stream"] == []
    assert redis.data["runtime_log_stream:inflight"] == []
    assert factory.last_session is not None
    assert len(factory.last_session.rows) == 1


@pytest.mark.asyncio
async def test_runtime_log_row_applies_retention_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_cfg():
        return {
            "runtime_log_retention_days": 30,
            "runtime_log_max_message_chars": 20,
            "runtime_log_max_detail_chars": 30,
            "runtime_log_min_level": "info",
        }

    monkeypatch.setattr(supervisor, "_get_log_retention_config", _fake_cfg)
    payload = json.dumps(
        {
            "account_id": 3,
            "level": "info",
            "source": "plugin",
            "message": "x" * 80,
            "detail": {"plugin_key": "game24", "traceback": "y" * 100},
        }
    )

    row = await supervisor._build_runtime_log_row_with_retention(payload)

    assert row is not None
    assert len(row.message) < 80
    assert "已截断" in row.message
    assert row.detail["_truncated"] is True


@pytest.mark.asyncio
async def test_runtime_log_row_respects_min_level_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_cfg():
        return {
            "runtime_log_retention_days": 30,
            "runtime_log_max_message_chars": 2000,
            "runtime_log_max_detail_chars": 8000,
            "runtime_log_min_level": "warn",
        }

    monkeypatch.setattr(supervisor, "_get_log_retention_config", _fake_cfg)
    payload = json.dumps(
        {
            "account_id": 3,
            "level": "info",
            "source": "plugin",
            "message": "hello",
            "detail": {"plugin_key": "game24"},
        }
    )

    row = await supervisor._build_runtime_log_row_with_retention(payload)
    assert row is None


@pytest.mark.asyncio
async def test_runtime_log_row_redacts_message_and_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_cfg():
        return {
            "runtime_log_retention_days": 30,
            "runtime_log_max_message_chars": 2000,
            "runtime_log_max_detail_chars": 8000,
            "runtime_log_min_level": "info",
        }

    monkeypatch.setattr(supervisor, "_get_log_retention_config", _fake_cfg)
    payload = json.dumps(
        {
            "account_id": 7,
            "level": "info",
            "source": "plugin",
            "message": "Bearer abcdefghijklmn",
            "detail": {"access_token": "token-123456"},
        }
    )

    row = await supervisor._build_runtime_log_row_with_retention(payload)
    assert row is not None
    assert "abcdefghijklmn" not in row.message
    assert row.detail["access_token"] == "***"


@pytest.mark.asyncio
async def test_runtime_log_row_normalizes_numeric_level_to_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_cfg():
        return {
            "runtime_log_retention_days": 30,
            "runtime_log_max_message_chars": 2000,
            "runtime_log_max_detail_chars": 8000,
            "runtime_log_min_level": "debug",
        }

    monkeypatch.setattr(supervisor, "_get_log_retention_config", _fake_cfg)
    payload = json.dumps(
        {
            "account_id": 9,
            "level": 10,  # logging.DEBUG
            "source": "plugin",
            "message": "debug details",
            "detail": {"step": "send_bonus"},
        }
    )

    row = await supervisor._build_runtime_log_row_with_retention(payload)
    assert row is not None
    assert row.level == "debug"


@pytest.mark.asyncio
async def test_run_retention_cleanup_still_cleans_traces_when_runtime_log_retention_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime_log_retention_days=0 must not skip event_trace cleanup."""

    calls: dict[str, object] = {"runtime_deleted": False, "trace_kwargs": None}

    class _Result:
        rowcount = 3

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            calls["runtime_deleted"] = True
            return _Result()

        async def commit(self):
            return None

    async def _fake_cfg():
        return {
            "runtime_log_retention_days": 0,
            "trace_retention_days": 30,
            "trace_payload_snapshot_retention_days": 7,
            "native_raw_retention_days": 1,
        }

    async def _fake_cleanup_event_traces(**kwargs):
        calls["trace_kwargs"] = kwargs
        return {
            "deleted_traces": 4,
            "cleared_payload_snapshots": 9,
            "cleared_native_raw": 2,
        }

    monkeypatch.setattr(supervisor, "_get_log_retention_config", _fake_cfg)
    monkeypatch.setattr(supervisor, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(supervisor, "cleanup_event_traces", _fake_cleanup_event_traces)

    stats = await supervisor._run_retention_cleanup_once()

    assert calls["runtime_deleted"] is False
    assert calls["trace_kwargs"] == {
        "trace_retention_days": 30,
        "payload_snapshot_retention_days": 7,
        "native_raw_retention_days": 1,
    }
    assert stats["deleted_runtime_logs"] == 0
    assert stats["deleted_traces"] == 4
    assert stats["cleared_payload_snapshots"] == 9
    assert stats["cleared_native_raw"] == 2


@pytest.mark.asyncio
async def test_run_retention_cleanup_deletes_runtime_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Result:
        rowcount = 5

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _Result()

        async def commit(self):
            return None

    async def _fake_cfg():
        return {
            "runtime_log_retention_days": 30,
            "trace_retention_days": 30,
            "trace_payload_snapshot_retention_days": 7,
            "native_raw_retention_days": 1,
        }

    async def _fake_cleanup_event_traces(**_kwargs):
        return {
            "deleted_traces": 0,
            "cleared_payload_snapshots": 0,
            "cleared_native_raw": 0,
        }

    monkeypatch.setattr(supervisor, "_get_log_retention_config", _fake_cfg)
    monkeypatch.setattr(supervisor, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(supervisor, "cleanup_event_traces", _fake_cleanup_event_traces)

    stats = await supervisor._run_retention_cleanup_once()
    assert stats["deleted_runtime_logs"] == 5

