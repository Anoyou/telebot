from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_cross_process_span_waits_until_parent_trace_is_visible(monkeypatch) -> None:
    """跨进程子 Span 不能在父 Trace 的 200ms 批量提交窗口内抢先写库。"""

    invalid_commits = {"count": 0}
    persisted: list[object] = []

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return list(self._values)

    class _Result:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _Scalars(self._values)

    class _Session:
        def __init__(self, *, parent_visible: bool, reject_commit: bool = False):
            self.parent_visible = parent_visible
            self.reject_commit = reject_commit

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, *_args, **_kwargs):
            return _Result(["evt_cross_process"] if self.parent_visible else [])

        def add(self, row):
            persisted.append(row)

        async def commit(self):
            if self.reject_commit:
                invalid_commits["count"] += 1
                raise RuntimeError("event_span_trace_id_fkey")

    sessions = [
        _Session(parent_visible=False, reject_commit=True),
        _Session(parent_visible=True),
        _Session(parent_visible=True),
    ]
    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: sessions.pop(0))
    monkeypatch.setattr(event_trace, "TRACE_PARENT_VISIBILITY_POLL_SECONDS", 0.0)

    span = SimpleNamespace(trace_id="evt_cross_process")
    await event_trace._flush_trace_batch([event_trace._TraceWrite(kind="span", payload=span)])

    assert invalid_commits["count"] == 0
    assert persisted == [span]
    assert sessions == []


@pytest.mark.asyncio
async def test_missing_parent_trace_is_reported_without_invalid_child_insert(monkeypatch) -> None:
    """父 Trace 永久缺失时应放弃子写入并记录原因，不能制造外键异常。"""

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
    monkeypatch.setattr(event_trace, "TRACE_PARENT_VISIBILITY_TIMEOUT_SECONDS", 0.0)

    span = SimpleNamespace(trace_id="evt_never_created")
    await event_trace._flush_trace_batch([event_trace._TraceWrite(kind="span", payload=span)])

    assert span not in added
    assert len(added) == 1
    assert isinstance(added[0], RuntimeLog)
    assert added[0].message == "event trace parent missing after wait"
    assert added[0].detail["trace_id"] == "evt_never_created"
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



@pytest.mark.asyncio
async def test_cleanup_event_traces_orchestrates_batched_helpers(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    async def _native(*, cutoff, batch_size):
        calls.append(("native", batch_size))
        return 3

    async def _payload(*, cutoff, batch_size):
        calls.append(("payload", batch_size))
        return 11

    async def _delete(*, cutoff, batch_size):
        calls.append(("delete", batch_size))
        return 7

    monkeypatch.setattr(event_trace, "_cleanup_native_raw_snapshots_batched", _native)
    monkeypatch.setattr(event_trace, "_cleanup_payload_snapshots_batched", _payload)
    monkeypatch.setattr(event_trace, "_cleanup_old_traces_batched", _delete)

    stats = await event_trace.cleanup_event_traces(
        trace_retention_days=30,
        payload_snapshot_retention_days=7,
        native_raw_retention_days=1,
        batch_size=2,
    )

    assert [name for name, _ in calls] == ["native", "payload", "delete"]
    assert calls[0][1] == 2  # orm batch follows batch_size
    assert calls[1][1] >= 2  # id batch at least orm batch
    assert stats == {
        "deleted_traces": 7,
        "cleared_payload_snapshots": 11,
        "cleared_native_raw": 3,
    }


@pytest.mark.asyncio
async def test_cleanup_native_raw_snapshots_respects_batch_size(monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    rows = [
        type(
            "Row",
            (),
            {
                "id": idx,
                "started_at": now - timedelta(days=3),
                "payload_snapshot": {
                    "message": {"text": f"m{idx}"},
                    "native_raw": {"secret": f"s{idx}"},
                },
                "native_raw_meta": {"stored_in_trace": True},
            },
        )()
        for idx in range(1, 6)
    ]
    max_orm_rows = {"value": 0}
    seen_ids: list[int] = []

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return list(self._values)

    class _Result:
        def __init__(self, values):
            self._values = values
            self.rowcount = 0

        def scalars(self):
            return _Scalars(self._values)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, stmt, *_args, **_kwargs):
            # Always return the next unsent slice; production code advances last_id.
            remaining = [row for row in rows if row.id not in seen_ids]
            # Extract lower bound from where clause is hard; use call progression via seen_ids.
            chunk = remaining[:2]
            max_orm_rows["value"] = max(max_orm_rows["value"], len(chunk))
            for row in chunk:
                seen_ids.append(row.id)
            return _Result(chunk)

        async def commit(self):
            return None

        async def rollback(self):
            return None

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(event_trace, "flag_modified", lambda *_args, **_kwargs: None)

    cleared = await event_trace._cleanup_native_raw_snapshots_batched(
        cutoff=now - timedelta(days=1),
        batch_size=2,
    )

    assert cleared == 5
    assert max_orm_rows["value"] <= 2
    assert seen_ids == [1, 2, 3, 4, 5]
    assert event_trace.estimate_cleanup_candidate_memory(max_orm_rows["value"]) < 16 * 1024 * 1024
    assert all(row.payload_snapshot["native_raw"] == "[expired]" for row in rows)


@pytest.mark.asyncio
async def test_cleanup_payload_snapshots_batched_nulls_by_id_batches(monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    ids = [10, 11, 12, 13, 14]
    emitted: list[list[int]] = []
    updated_batches: list[list[int]] = []

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return list(self._values)

    class _Result:
        def __init__(self, values=None, rowcount=0):
            self._values = values or []
            self.rowcount = rowcount

        def scalars(self):
            return _Scalars(self._values)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, stmt, *_args, **_kwargs):
            sql = str(stmt).lower()
            if sql.startswith("update") or "update" in type(stmt).__name__.lower():
                # pull ids from IN clause via compiled params if present
                try:
                    compiled = stmt.compile()
                    params = dict(compiled.params)
                    batch = [int(v) for k, v in sorted(params.items()) if "id" in k.lower() or True]
                    # fallback: track via sequential emission
                except Exception:
                    batch = []
                # Use previously emitted id chunk
                batch = emitted[-1] if emitted else []
                updated_batches.append(list(batch))
                return _Result([], rowcount=len(batch))
            # select ids
            already = {i for batch in emitted for i in batch}
            remaining = [i for i in ids if i not in already]
            chunk = remaining[:2]
            emitted.append(chunk)
            return _Result(chunk)

        async def commit(self):
            return None

    monkeypatch.setattr(event_trace, "AsyncSessionLocal", lambda: _Session())

    cleared = await event_trace._cleanup_payload_snapshots_batched(
        cutoff=datetime.now(UTC) - timedelta(days=7),
        batch_size=2,
    )

    assert cleared == 5
    assert all(len(batch) <= 2 for batch in emitted if batch)
    assert sorted(i for batch in updated_batches for i in batch) == ids


def test_estimate_cleanup_candidate_memory_scales_with_rows() -> None:
    small = event_trace.estimate_cleanup_candidate_memory(200)
    large = event_trace.estimate_cleanup_candidate_memory(130_000)
    assert small < 2 * 1024 * 1024
    assert large > 200 * 1024 * 1024
