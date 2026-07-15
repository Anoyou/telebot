from __future__ import annotations

import pytest

from app.db.models.log import RuntimeLog
from app.services import event_trace


@pytest.fixture(autouse=True)
async def _trace_writer_isolation():
    await event_trace.stop_trace_writer()
    event_trace._NATIVE_RAW_TRACE_POLICY_CACHE = dict(event_trace._NATIVE_RAW_TRACE_POLICY_DEFAULTS)
    yield
    await event_trace.stop_trace_writer()
    event_trace._NATIVE_RAW_TRACE_POLICY_CACHE = dict(event_trace._NATIVE_RAW_TRACE_POLICY_DEFAULTS)


@pytest.mark.asyncio
async def test_start_trace_failure_writes_runtime_log(monkeypatch) -> None:
    added: list[object] = []

    class _FailSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("trace table unavailable")

        async def commit(self):
            return None

    class _LogSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def add(self, row):
            added.append(row)

        async def commit(self):
            return None

    sessions = [_FailSession(), _LogSession()]

    def _session_factory():
        return sessions.pop(0)

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", _session_factory)

    ctx = await event_trace.start_trace(
        {
            "source": {"account_id": 1, "type": "message", "channel": "interaction_bot"},
            "message": {"text": "hello"},
        }
    )
    await event_trace.flush_trace_writes()

    assert ctx.trace_id.startswith(event_trace.TRACE_ID_PREFIX)
    assert len(added) == 1
    assert isinstance(added[0], RuntimeLog)
    assert added[0].level == "error"
    assert added[0].source == "system"
    assert added[0].detail["component"] == "event_trace"
    assert added[0].detail["reason_code"] == event_trace.TRACE_WRITE_FAILED_REASON_CODE


@pytest.mark.asyncio
async def test_start_trace_omits_native_raw_from_payload_snapshot_by_default(monkeypatch) -> None:
    added: list[object] = []

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _Result()

        def add(self, row):
            added.append(row)

        async def commit(self):
            return None

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: _Session())

    await event_trace.start_trace(
        {
            "source": {"account_id": 1, "type": "message", "channel": "interaction_bot"},
            "message": {"text": "hello"},
            "native_raw_meta": {"enabled": True, "stored_in_trace": False},
            "native_raw": {"message": {"text": "raw secret"}},
        }
    )
    await event_trace.flush_trace_writes()

    assert len(added) == 1
    assert added[0].payload_snapshot["native_raw"] == "[omitted]"
    assert added[0].native_raw_meta == {"enabled": True, "stored_in_trace": False, "retention_days": 1}


@pytest.mark.asyncio
async def test_start_trace_persists_native_raw_when_enabled(monkeypatch) -> None:
    added: list[object] = []

    class _Result:
        def __init__(self, value=None):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Setting:
        value = {"native_raw_persist_enabled": True, "native_raw_retention_days": 1}

    class _Session:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return _Result(_Setting() if self.calls == 1 else None)

        def add(self, row):
            added.append(row)

        async def commit(self):
            return None

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: _Session())
    await event_trace.refresh_trace_settings()

    await event_trace.start_trace(
        {
            "source": {"account_id": 1, "type": "message", "channel": "interaction_bot"},
            "message": {"text": "hello"},
            "native_raw_meta": {"enabled": True, "stored_in_trace": False},
            "native_raw": {"message": {"text": "raw secret"}},
        }
    )
    await event_trace.flush_trace_writes()

    assert len(added) == 1
    assert added[0].payload_snapshot["native_raw"] == {"message": {"text": "raw secret"}}
    assert added[0].native_raw_meta == {"enabled": True, "stored_in_trace": True, "retention_days": 1}


@pytest.mark.asyncio
async def test_trace_writes_are_buffered_until_flush(monkeypatch) -> None:
    added: list[object] = []
    executed: list[object] = []
    commits = {"count": 0}

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, stmt, *_args, **_kwargs):
            executed.append(stmt)
            return _Result()

        def add(self, row):
            added.append(row)

        async def commit(self):
            commits["count"] += 1

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: _Session())

    trace = await event_trace.start_trace(
        {
            "source": {"account_id": 1, "type": "message", "channel": "interaction_bot"},
            "message": {"text": "hello"},
        }
    )
    await event_trace.record_span(trace, "normalize", event_trace.TRACE_STATUS_OK, component="event_bus")
    await event_trace.record_action(trace, {"type": "send_message"}, event_trace.TRACE_STATUS_OK)
    await event_trace.finish_trace(trace, event_trace.TRACE_STATUS_OK)

    assert added == []
    assert executed == []
    assert commits["count"] == 0
    assert event_trace.trace_writer_stats()["queued"] == 4

    await event_trace.flush_trace_writes()

    assert len(added) == 3
    assert len(executed) == 1
    assert commits["count"] == 1
    assert event_trace.trace_writer_stats()["queued"] == 0


@pytest.mark.asyncio
async def test_supplied_trace_id_is_deduped_in_batch(monkeypatch) -> None:
    added: list[object] = []
    commits = {"count": 0}

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _Result()

        def add(self, row):
            added.append(row)

        async def commit(self):
            commits["count"] += 1

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: _Session())

    await event_trace.start_trace({"trace_id": "evt_same", "message": {"text": "first"}})
    await event_trace.start_trace({"trace_id": "evt_same", "message": {"text": "second"}})
    await event_trace.flush_trace_writes()

    assert [getattr(row, "trace_id", None) for row in added] == ["evt_same"]
    assert commits["count"] == 1


def test_clear_native_raw_snapshot_marks_expired() -> None:
    class _Row:
        payload_snapshot = {"message": {"text": "hello"}, "native_raw": {"message": {"text": "raw secret"}}}
        native_raw_meta = {"enabled": True, "stored_in_trace": True}

    row = _Row()

    assert event_trace._clear_native_raw_snapshot(row) is True
    assert row.payload_snapshot["native_raw"] == "[expired]"
    assert row.native_raw_meta["stored_in_trace"] is False
    assert row.native_raw_meta["expired_from_trace"] is True
