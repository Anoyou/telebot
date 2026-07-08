from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.logs import (
    EventTraceSummary,
    list_event_traces,
    list_log_messages,
    list_runtime_logs,
)


def _trace_row(**overrides):
    data = {
        "id": 1,
        "trace_id": "evt_test",
        "account_id": 1,
        "source_channel": "interaction_bot",
        "event_type": "message",
        "chat_id": None,
        "message_id": None,
        "update_id": 11,
        "callback_query_id": None,
        "sender_user_id": 1001,
        "sender_name": "Alice",
        "text_preview": None,
        "status": "ok",
        "started_at": datetime(2026, 6, 29, tzinfo=UTC),
        "ended_at": None,
        "duration_ms": None,
        "native_raw_meta": None,
        "raw_summary": None,
        "payload_snapshot": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_trace_summary_projects_inline_query_fields() -> None:
    row = _trace_row(
        event_type="inline_query",
        text_preview="fallback query",
        raw_summary={"query": "raw query"},
        payload_snapshot={"inline_query": {"query": "payload query"}},
    )

    summary = EventTraceSummary.from_row(row)

    assert summary.inline_query == "payload query"
    assert summary.chosen_inline_result_id is None
    assert summary.chosen_inline_query is None


def test_trace_summary_projects_chosen_inline_result_fields() -> None:
    row = _trace_row(
        event_type="chosen_inline_result",
        text_preview="fallback chosen query",
        raw_summary={"query": "raw chosen query"},
        payload_snapshot={"chosen_inline_result": {"result_id": "result-1", "query": "payload chosen query"}},
    )

    summary = EventTraceSummary.from_row(row)

    assert summary.inline_query is None
    assert summary.chosen_inline_result_id == "result-1"
    assert summary.chosen_inline_query == "payload chosen query"


class _EmptyScalarResult:
    def scalar_one(self):
        return 0

    def scalars(self):
        return self

    def all(self):
        return []


class _CaptureDB:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _EmptyScalarResult()


class _ListScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _MessageDB:
    def __init__(self, *, traces, spans, actions) -> None:
        self.traces = traces
        self.spans = spans
        self.actions = actions
        self.statements: list[str] = []

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        if "FROM event_span" in sql:
            return _ListScalarResult(self.spans)
        if "FROM event_action" in sql:
            return _ListScalarResult(self.actions)
        return _ListScalarResult(self.traces)


@pytest.mark.asyncio
async def test_list_event_traces_filters_by_trace_and_reason_code() -> None:
    db = _CaptureDB()

    rows = await list_event_traces(
        db=db,
        _user=object(),
        trace_id="evt_test",
        reason_code="send_channel_deprecated",
        limit=100,
    )

    assert rows == []
    sql = "\n".join(db.statements)
    assert "event_trace.trace_id" in sql
    assert "event_span.reason_code" in sql
    assert "event_action.error_code" in sql


@pytest.mark.asyncio
async def test_list_runtime_logs_filters_by_keyword() -> None:
    db = _CaptureDB()

    rows = await list_runtime_logs(
        db=db,
        _user=object(),
        account_id=None,
        level=None,
        plugin_key=None,
        keyword="telegram_api_error",
        source=None,
        since=None,
        limit=100,
    )

    assert rows == []
    sql = "\n".join(db.statements)
    assert "runtime_log.message" in sql
    assert "runtime_log.level" in sql
    assert "runtime_log.source" in sql
    assert "runtime_log.detail" in sql


@pytest.mark.asyncio
async def test_list_log_messages_batches_spans_actions_and_filters_verdict() -> None:
    traces = [
        _trace_row(
            id=1,
            trace_id="evt_ok",
            text_preview="hello",
            status="ok",
            ended_at=datetime(2026, 6, 29, tzinfo=UTC),
        ),
        _trace_row(
            id=2,
            trace_id="evt_failed",
            text_preview="pay",
            status="ok",
            ended_at=datetime(2026, 6, 29, tzinfo=UTC),
        ),
    ]
    spans = [
        SimpleNamespace(
            id=1,
            span_id="sp_1",
            trace_id="evt_ok",
            phase="route",
            component="interaction_rule",
            plugin_key=None,
            entry_key=None,
            status="ok",
            reason_code="matched",
            message=None,
            detail=None,
            started_at=datetime(2026, 6, 29, tzinfo=UTC),
            ended_at=datetime(2026, 6, 29, tzinfo=UTC),
            duration_ms=1,
        ),
        SimpleNamespace(
            id=2,
            span_id="sp_2",
            trace_id="evt_failed",
            phase="plugin_invoke",
            component="interaction_module",
            plugin_key="math10",
            entry_key=None,
            status="ok",
            reason_code=None,
            message=None,
            detail=None,
            started_at=datetime(2026, 6, 29, tzinfo=UTC),
            ended_at=datetime(2026, 6, 29, tzinfo=UTC),
            duration_ms=1,
        ),
    ]
    actions = [
        SimpleNamespace(
            id=1,
            action_id="act_1",
            trace_id="evt_ok",
            plugin_key="math10",
            action_type="send_message",
            status="ok",
            error_code=None,
            error_message=None,
        ),
        SimpleNamespace(
            id=2,
            action_id="act_2",
            trace_id="evt_failed",
            plugin_key="math10",
            action_type="send_message",
            status="failed",
            error_code="telegram_api_error",
            error_message="bad request",
        ),
    ]
    db = _MessageDB(traces=traces, spans=spans, actions=actions)

    rows = await list_log_messages(
        db=db,
        _user=object(),
        account_id=None,
        source_channel=None,
        event_type=None,
        chat_id=None,
        message_id=None,
        update_id=None,
        sender_user_id=None,
        plugin_key=None,
        status=None,
        trace_id=None,
        reason_code=None,
        verdict="failed",
        keyword=None,
        since=None,
        until=None,
        limit=100,
    )

    assert [row.trace_id for row in rows] == ["evt_failed"]
    assert rows[0].verdict == "failed"
    assert rows[0].funel.sent == "fail"
    sql = "\n".join(db.statements)
    assert sql.count("FROM event_span") == 1
    assert sql.count("FROM event_action") == 1
