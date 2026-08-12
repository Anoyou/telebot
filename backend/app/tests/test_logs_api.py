from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api import logs as logs_api
from app.api.logs import (
    EventTraceSummary,
    get_runtime_log_stats,
    list_event_traces,
    list_log_messages,
    list_runtime_logs,
    list_system_console_logs,
)


def test_system_console_filter_removes_only_successful_internal_health_noise() -> None:
    payload = {
        "ok": True,
        "lines": [
            'updater-1 | 2026-07-23T21:42:58Z [updater] 127.0.0.1 "GET /health HTTP/1.1" 200 -',
            'updater-1 | 2026-07-24T06:32:46Z [updater] 172.18.0.4 "GET /jobs/890e65215801 HTTP/1.1" 200 -',
            'updater-1 | 2026-07-24T06:32:47Z [updater] 172.18.0.4 "GET /console-logs?service=all&tail=300 HTTP/1.1" 200 -',
            'updater-1 | 2026-07-23T21:43:13Z [updater] 127.0.0.1 "GET /health HTTP/1.1" 503 -',
            "web-1 | ERROR:app.services.system_agent.runtime:真实错误",
        ],
    }

    result = logs_api._filter_console_payload(payload, None)

    assert result["lines"] == [
        'updater-1 | 2026-07-23T21:43:13Z [updater] 127.0.0.1 "GET /health HTTP/1.1" 503 -',
        "web-1 | ERROR:app.services.system_agent.runtime:真实错误",
    ]


def test_system_console_filter_removes_routine_info_noise_but_keeps_failures() -> None:
    payload = {
        "lines": [
            "web-1 | INFO:alembic.runtime.migration:Context impl PostgresqlImpl.",
            "web-1 | INFO:alembic.runtime.migration:Will assume transactional DDL.",
            "web-1 | INFO:alembic.runtime.migration:Running upgrade 0040 -> 0041",
            "web-1 | 2026-07-23 22:45:28,309 [worker:1] INFO Got difference for channel 2304101980 updates",
            'web-1 | INFO:httpx:HTTP Request: POST https://example.test/v1/responses "HTTP/1.1 200 OK"',
            'web-1 | INFO:httpx:HTTP Request: POST https://example.test/v1/responses "HTTP/1.1 429 Too Many Requests"',
            "web-1 | WARNING:alembic.runtime.migration:数据库迁移状态异常",
            "web-1 | ERROR:app.worker.runtime:频道差异同步失败",
        ]
    }

    result = logs_api._filter_console_payload(payload, None)

    assert result["lines"] == [
        "web-1 | INFO:alembic.runtime.migration:Running upgrade 0040 -> 0041",
        'web-1 | INFO:httpx:HTTP Request: POST https://example.test/v1/responses "HTTP/1.1 429 Too Many Requests"',
        "web-1 | WARNING:alembic.runtime.migration:数据库迁移状态异常",
        "web-1 | ERROR:app.worker.runtime:频道差异同步失败",
    ]


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


def test_trace_summary_projects_chat_title() -> None:
    row = _trace_row(payload_snapshot={"chat": {"id": -1001, "title": "测试群"}})

    summary = EventTraceSummary.from_row(row)

    assert summary.chat_title == "测试群"


def test_system_console_search_accepts_post_only() -> None:
    route = next(route for route in logs_api.router.routes if route.path == "/api/logs/system-console")

    assert route.methods == {"POST"}


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


class _StatsResult:
    def __init__(self, *, scalar=None, rows=None) -> None:  # noqa: ANN001
        self._scalar = scalar
        self._rows = list(rows or [])

    def scalar_one(self):  # noqa: ANN201
        return self._scalar

    def all(self) -> list:
        return list(self._rows)


class _StatsDB:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt):  # noqa: ANN001, ANN201
        sql = str(stmt)
        self.statements.append(sql)
        call = len(self.statements)
        if call == 1:
            return _StatsResult(scalar=8)
        if call == 2:
            return _StatsResult(
                rows=[
                    ("info", 2),
                    ("warning", 2),
                    ("error", 1),
                    ("custom", 3),
                ]
            )
        if call == 3:
            return _StatsResult(rows=[(1, 5), (None, 3)])
        return _StatsResult(rows=[("plugin", 6), (None, 2)])


class _FakeRedis:
    def __init__(self, cached: str | None = None, *, fail: bool = False) -> None:
        self.cached = cached
        self.fail = fail
        self.keys: list[str] = []
        self.writes: list[tuple[str, int, str]] = []

    async def get(self, key: str) -> str | None:
        self.keys.append(key)
        if self.fail:
            raise ConnectionError("redis unavailable")
        return self.cached

    async def setex(self, key: str, ttl: int, value: str) -> None:
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.writes.append((key, ttl, value))


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
async def test_runtime_log_stats_counts_unknown_levels_without_exposing_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _StatsDB()
    redis = _FakeRedis()
    monkeypatch.setattr(logs_api, "get_redis", lambda: redis)

    result = await get_runtime_log_stats(
        db=db,
        _user=object(),
        account_id=None,
        level=None,
        plugin_key=None,
        keyword="private-search-value",
        source=None,
        since=None,
        until=None,
    )

    assert result.total == 8
    assert (result.info, result.warn, result.error) == (2, 2, 1)
    assert result.by_account[1].key == "system"
    assert result.by_source[1].key == "unknown"
    assert len(db.statements) == 4
    assert redis.writes[0][1] == 20
    assert "private-search-value" not in redis.keys[0]


@pytest.mark.asyncio
async def test_runtime_log_stats_uses_cached_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = logs_api.RuntimeLogStatsOut(
        total=3,
        debug=0,
        info=3,
        warn=0,
        error=0,
    ).model_dump_json()
    redis = _FakeRedis(cached)
    db = _StatsDB()
    monkeypatch.setattr(logs_api, "get_redis", lambda: redis)

    result = await get_runtime_log_stats(
        db=db,
        _user=object(),
        account_id=7,
        level=None,
        plugin_key=None,
        keyword=None,
        source=None,
        since=None,
        until=None,
    )

    assert result.total == 3
    assert result.cached is True
    assert db.statements == []


@pytest.mark.asyncio
async def test_runtime_log_stats_falls_back_when_redis_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _StatsDB()
    monkeypatch.setattr(logs_api, "get_redis", lambda: _FakeRedis(fail=True))

    result = await get_runtime_log_stats(
        db=db,
        _user=object(),
        account_id=None,
        level=None,
        plugin_key=None,
        keyword=None,
        source="plugin",
        since=None,
        until=None,
    )

    assert result.total == 8
    assert result.cached is False
    assert len(db.statements) == 4


@pytest.mark.asyncio
async def test_system_console_logs_uses_updater_and_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, int]] = []

    def fake_fetch(service: str, tail: int):
        captured.append((service, tail))
        return {
            "ok": True,
            "source": "docker_compose",
            "services": [service],
            "tail": tail,
            "lines": ["web | token=abc12345 ready", "web | normal line"],
        }

    monkeypatch.setattr("app.api.logs._fetch_updater_console_logs", fake_fetch)

    result = await list_system_console_logs(
        _user=object(),
        body=logs_api.SystemConsoleLogsRequest(service="web", tail=100),
    )

    assert result.ok is True
    assert result.source == "docker_compose"
    assert result.services == ["web"]
    assert result.lines[0] == "web | token=*** ready"
    assert captured == [("web", 100)]


@pytest.mark.asyncio
async def test_system_console_keyword_is_filtered_without_forwarding_to_updater(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, int]] = []

    def fake_fetch(service: str, tail: int):
        captured.append((service, tail))
        return {
            "ok": True,
            "source": "docker_compose",
            "services": [service],
            "tail": tail,
            "lines": ["web | alpha", "web | private needle", "web | omega"],
        }

    monkeypatch.setattr("app.api.logs._fetch_updater_console_logs", fake_fetch)

    result = await list_system_console_logs(
        _user=object(),
        body=logs_api.SystemConsoleLogsRequest(service="web", keyword="needle", tail=100),
    )

    assert result.lines == ["web | private needle"]
    assert captured == [("web", 100)]


@pytest.mark.asyncio
async def test_system_console_logs_falls_back_to_local_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    backend_log = tmp_path / "backend.log"
    backend_log.write_text("line one\nline two\n", encoding="utf-8")

    def fake_fetch(*_args, **_kwargs):
        raise RuntimeError("updater down")

    monkeypatch.setattr("app.api.logs._fetch_updater_console_logs", fake_fetch)
    monkeypatch.setitem(logs_api._LOCAL_CONSOLE_FILES, "web", backend_log)

    result = await list_system_console_logs(
        _user=object(),
        body=logs_api.SystemConsoleLogsRequest(service="web", keyword="two", tail=100),
    )

    assert result.ok is True
    assert result.source == "local_files"
    assert result.lines == ["web  | line two"]


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
    assert rows[0].plugin_keys == ["math10"]
    assert rows[0].funel.sent == "fail"
    sql = "\n".join(db.statements)
    assert sql.count("FROM event_span") == 1
    assert sql.count("FROM event_action") == 1


class _PagedMessageDB(_MessageDB):
    def __init__(self, *, trace_pages, spans, actions) -> None:
        super().__init__(traces=[], spans=spans, actions=actions)
        self.trace_pages = list(trace_pages)

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        if "FROM event_span" in sql:
            trace_ids = {span.trace_id for span in self.spans}
            return _ListScalarResult([span for span in self.spans if span.trace_id in trace_ids])
        if "FROM event_action" in sql:
            trace_ids = {action.trace_id for action in self.actions}
            return _ListScalarResult([action for action in self.actions if action.trace_id in trace_ids])
        return _ListScalarResult(self.trace_pages.pop(0) if self.trace_pages else [])


@pytest.mark.asyncio
async def test_list_log_messages_verdict_scans_later_pages_until_match() -> None:
    first_page = [
        _trace_row(
            id=index, trace_id=f"evt_noise_{index}", status="ok", ended_at=datetime(2026, 6, 29, tzinfo=UTC)
        )
        for index in range(1, 4)
    ]
    target = _trace_row(
        id=99,
        trace_id="evt_deep_failed",
        status="ok",
        ended_at=datetime(2026, 6, 28, tzinfo=UTC),
    )
    failed_action = SimpleNamespace(
        id=99,
        action_id="act_deep",
        trace_id="evt_deep_failed",
        plugin_key="math10",
        action_type="send_message",
        status="failed",
        error_code="telegram_api_error",
        error_message="bad request",
    )
    db = _PagedMessageDB(trace_pages=[first_page, [target]], spans=[], actions=[failed_action])

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
        limit=1,
    )

    assert [row.trace_id for row in rows] == ["evt_deep_failed"]
