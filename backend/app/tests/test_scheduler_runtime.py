from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telethon.tl.types import InputPeerUser

from app.services.llm_client import LLMResult
from app.services.llm_dto import LLMProviderDTO
from app.worker import runtime as worker_runtime
from app.worker import scheduler_runtime
from app.worker.command import CommandContext, set_command_context
from app.worker.ipc import CMD_EXECUTE_RULE, IPCMessage
from app.worker.plugins.update_barrier import begin_plugin_update
from app.worker.scheduler_runtime import PlatformScheduler, SchedulerRuleExecutor, _croniter_next


async def _noop_log(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    return None


def _runtime() -> PlatformScheduler:
    paused = asyncio.Event()
    paused.set()
    return PlatformScheduler(
        account_id=42,
        client=AsyncMock(),
        redis=AsyncMock(),
        paused=paused,
        log_writer=_noop_log,
    )


def test_six_field_cron_uses_leading_seconds() -> None:
    base = datetime(2026, 5, 21, 2, 54, 20, tzinfo=UTC)

    next_fire = _croniter_next("0 5 11 * * *", base, None)

    assert next_fire == datetime(2026, 5, 21, 11, 5, 0, tzinfo=UTC)


def test_five_field_cron_keeps_classic_order() -> None:
    base = datetime(2026, 5, 21, 2, 54, 20, tzinfo=UTC)

    next_fire = _croniter_next("0 5 11 * *", base, None)

    assert next_fire == datetime(2026, 6, 11, 5, 0, 0, tzinfo=UTC)


def test_cron_resolves_stale_six_field_next_fire_after_parser_upgrade() -> None:
    executor = SchedulerRuleExecutor()
    now = datetime(2026, 5, 21, 2, 54, 20, tzinfo=UTC)
    cfg = {
        "kind": "cron",
        "cron": "0 5 11 * * *",
        "_last_cron": "0 5 11 * * *",
        "next_fire": "2026-06-11T05:00:00+00:00",
    }

    due, next_fire = executor.resolve_cron(cfg, now)

    assert due is False
    assert next_fire == datetime(2026, 5, 21, 11, 5, 0, tzinfo=UTC)
    assert cfg["_cron_seconds_mode"] is True
    assert cfg["_config_dirty"] is True


def test_cron_resolves_stale_next_fire_after_timezone_marker_added() -> None:
    executor = SchedulerRuleExecutor()
    now = datetime(2026, 5, 21, 2, 54, 20, tzinfo=UTC)
    tz = ZoneInfo("Asia/Shanghai")
    cfg = {
        "kind": "cron",
        "cron": "0 41 11 * * *",
        "_last_cron": "0 41 11 * * *",
        "_cron_seconds_mode": True,
        "next_fire": "2026-05-21T11:41:00+00:00",
    }

    due, next_fire = executor.resolve_cron(cfg, now, tz)

    assert due is False
    assert next_fire == datetime(2026, 5, 21, 3, 41, 0, tzinfo=UTC)
    assert cfg["_cron_timezone"] == "Asia/Shanghai"
    assert cfg["_config_dirty"] is True


@pytest.mark.asyncio
async def test_plugin_facade_registers_interval_job(monkeypatch) -> None:
    monkeypatch.setattr("app.worker.scheduler_runtime._get_system_tz", AsyncMock(return_value=None))

    runtime = _runtime()
    callback = AsyncMock()

    facade = runtime.for_plugin("demo", generation=1)
    facade.register("heartbeat", {"kind": "interval", "interval_sec": 60}, callback)

    await runtime.tick_runtime_jobs()

    callback.assert_awaited_once()
    job = callback.await_args.args[0]
    assert job.account_id == 42
    assert job.owner == "demo"
    assert job.job_id == "heartbeat"
    assert job.fire_count == 1

    jobs = facade.list_jobs()
    assert jobs[0]["fire_count"] == 1
    assert jobs[0]["config"]["last_result"] == "ok"
    assert jobs[0]["config"]["next_fire"] is not None


@pytest.mark.asyncio
async def test_unregister_owner_prevents_runtime_job(monkeypatch) -> None:
    monkeypatch.setattr("app.worker.scheduler_runtime._get_system_tz", AsyncMock(return_value=None))

    runtime = _runtime()
    callback = AsyncMock()
    facade = runtime.for_plugin("demo", generation=1)
    facade.register(
        "once",
        {
            "kind": "once",
            "fire_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
        callback,
    )

    removed = facade.unregister_all()
    await runtime.tick_runtime_jobs()

    assert removed == 1
    callback.assert_not_awaited()
    assert facade.list_jobs() == []


@pytest.mark.asyncio
async def test_plugin_update_barrier_prevents_stale_runtime_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.worker.scheduler_runtime._get_system_tz", AsyncMock(return_value=None))
    monkeypatch.setattr(scheduler_runtime.settings, "plugins_installed_dir", str(tmp_path))
    begin_plugin_update(tmp_path, "ai_redpacket")

    runtime = _runtime()
    callback = AsyncMock()
    runtime.for_plugin("ai_redpacket", generation=1).register(
        "settlement",
        {"kind": "interval", "interval_sec": 60},
        callback,
    )

    await runtime.tick_runtime_jobs()

    callback.assert_not_awaited()
    jobs = runtime.list_runtime_jobs()
    assert jobs[0]["fire_count"] == 0
    assert "last_result" not in jobs[0]["config"]


@pytest.mark.asyncio
async def test_runtime_job_failure_is_logged_and_kept(monkeypatch) -> None:
    monkeypatch.setattr("app.worker.scheduler_runtime._get_system_tz", AsyncMock(return_value=None))
    logs: list[tuple[tuple, dict]] = []

    async def _log(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        logs.append((args, kwargs))

    paused = asyncio.Event()
    paused.set()
    runtime = PlatformScheduler(
        account_id=42,
        client=AsyncMock(),
        redis=AsyncMock(),
        paused=paused,
        log_writer=_log,
    )

    async def _boom(_job) -> None:  # noqa: ANN001
        raise RuntimeError("boom")

    runtime.for_plugin("demo", generation=1).register(
        "broken",
        {"kind": "interval", "interval_sec": 60},
        _boom,
    )

    await runtime.tick_runtime_jobs()

    jobs = runtime.list_runtime_jobs()
    assert jobs[0]["fire_count"] == 0
    assert jobs[0]["last_error"] == "RuntimeError: boom"
    assert jobs[0]["config"]["last_result"] == "error"
    assert logs
    assert logs[0][1]["source"] == "plugin"
    assert logs[0][1]["plugin_key"] == "demo"


@pytest.mark.asyncio
async def test_scheduler_context_log_ignores_duplicate_plugin_key() -> None:
    logs: list[tuple[tuple, dict]] = []

    async def _log(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        logs.append((args, kwargs))

    paused = asyncio.Event()
    paused.set()
    runtime = PlatformScheduler(
        account_id=42,
        client=AsyncMock(),
        redis=AsyncMock(),
        paused=paused,
        log_writer=_log,
    )
    runtime.engine = object()

    ctx = await runtime._make_scheduler_context()
    assert ctx is not None

    await ctx.log("info", "scheduler log ok", plugin_key="external", source="plugin", entry_key="tick")

    assert logs
    assert logs[0][1]["source"] == "plugin"
    assert logs[0][1]["plugin_key"] == scheduler_runtime.FEATURE_SCHEDULER
    assert logs[0][1]["entry_key"] == "tick"


@pytest.mark.asyncio
async def test_action_call_llm_uses_shared_service_invoke(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    row = SimpleNamespace(
        id=7,
        name="primary",
        provider="openai",
        api_key_enc=None,
        base_url=None,
        default_model="gpt-4o",
        api_format=None,
        proxy_url=None,
        modality="text",
        tags=[],
        cost_tier=2,
    )
    fallback = SimpleNamespace(
        id=8,
        name="fallback",
        provider="openai",
        api_key_enc=None,
        base_url=None,
        default_model="gpt-4o-mini",
        api_format=None,
        proxy_url=None,
        modality="text",
        tags=[],
        cost_tier=2,
    )
    monkeypatch.setattr(executor, "get_provider_row", AsyncMock(return_value=row))
    monkeypatch.setattr(executor, "get_provider_rows", AsyncMock(return_value=[row, fallback]))
    send_mock = AsyncMock(return_value=object())
    monkeypatch.setattr(executor, "send_with_ratelimit", send_mock)

    result = LLMResult(text="done", model="gpt-4o", input_tokens=2, output_tokens=3)
    invoke_mock = AsyncMock(
        return_value=(
            result,
            LLMProviderDTO(id=7, name="primary", provider="openai", default_model="gpt-4o"),
            False,
        )
    )
    monkeypatch.setattr("app.worker.scheduler_runtime.invoke_ai_runtime", invoke_mock)

    ctx = SimpleNamespace(
        account_id=42,
        engine=SimpleNamespace(
            acquire=AsyncMock(
                return_value=SimpleNamespace(allowed=True, wait_seconds=0)
            )
        ),
        log=AsyncMock(),
    )
    action = {
        "provider_id": 7,
        "prompt": "hello",
        "target_chat_id": 123,
        "fallback_provider_id": 8,
        "triggered_by_account_id": 123,
        "system_prompt": "sys",
        "max_tokens": 32,
    }

    await executor.action_call_llm(ctx, action)

    invoke_mock.assert_awaited_once()
    provider_dto, provider_map, system, user = invoke_mock.await_args.args[:4]
    assert provider_dto.id == 7
    assert provider_map[8].name == "fallback"
    assert system == "sys"
    assert user == "hello"
    assert invoke_mock.await_args.kwargs["triggered_by_account_id"] == 123
    send_mock.assert_awaited_once()
    assert send_mock.await_args.args[:3] == (ctx, 123, "done")
    assert send_mock.await_args.kwargs["action"]["type"] == "call_llm"


@pytest.mark.asyncio
async def test_scheduler_send_message_records_trace_action(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    trace = "evt_scheduler_ok"
    start_trace = AsyncMock(return_value=trace)
    finish_trace = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "start_trace", start_trace)
    monkeypatch.setattr(scheduler_runtime, "finish_trace", finish_trace)
    monkeypatch.setattr(scheduler_runtime, "record_action", record_action)
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0))),
        client=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=88))),
        log=AsyncMock(),
    )

    ok = await executor.fire(ctx, 9, {"action": {"type": "send_message", "target_chat_id": 123, "text": "hello"}})

    assert ok is True
    start_trace.assert_awaited_once()
    record_action.assert_awaited_once()
    assert record_action.await_args.args[0]["trace_id"] == "evt_scheduler_ok"
    assert record_action.await_args.args[1]["type"] == "send_message"
    assert record_action.await_args.args[2] == scheduler_runtime.TRACE_STATUS_OK
    finish_trace.assert_awaited_once_with(trace, scheduler_runtime.TRACE_STATUS_OK, rule_id=9, action_type="send_message")


@pytest.mark.asyncio
async def test_scheduler_send_message_resolves_username_for_send_and_ratelimit(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "_record_scheduler_action", record_action)
    entity = InputPeerUser(user_id=8395686237, access_hash=123456)
    engine = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0))
    )
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value=entity),
        send_message=AsyncMock(return_value=SimpleNamespace(id=88)),
    )
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=engine,
        client=client,
        log=AsyncMock(),
    )

    await executor.send_with_ratelimit(
        ctx,
        "@qingbaobu",
        "hello",
        action={"type": "send_message", "send_via": "userbot_reply", "text": "hello"},
    )

    client.get_input_entity.assert_awaited_once_with("@qingbaobu")
    client.send_message.assert_awaited_once_with(entity, "hello")
    assert engine.acquire.await_args.kwargs["peer_id"] is None
    trace_action = record_action.await_args.args[1]
    assert trace_action["chat_id"] == 8395686237
    assert trace_action["target_ref"] == "@qingbaobu"


@pytest.mark.asyncio
async def test_scheduler_send_message_reports_username_resolution_failure(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "_record_scheduler_action", record_action)
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(
            acquire=AsyncMock(
                return_value=SimpleNamespace(allowed=True, wait_seconds=0)
            )
        ),
        client=SimpleNamespace(
            get_input_entity=AsyncMock(side_effect=ValueError("not found")),
            send_message=AsyncMock(),
        ),
        log=AsyncMock(),
    )

    with pytest.raises(
        scheduler_runtime.SchedulerTargetResolutionError,
        match="无法解析目标 @missingbot",
    ):
        await executor.send_with_ratelimit(ctx, "@missingbot", "hello")

    ctx.engine.acquire.assert_awaited_once()
    ctx.client.send_message.assert_not_awaited()
    assert record_action.await_args.kwargs["error_code"] == "telegram_entity_not_found"


@pytest.mark.asyncio
async def test_scheduler_keeps_resolving_username_to_complete_input_peer(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    monkeypatch.setattr(scheduler_runtime, "_record_scheduler_action", AsyncMock())
    entity = InputPeerUser(user_id=8395686237, access_hash=123456)
    action = {
        "type": "send_message",
        "target_chat_id": "@qingbaobu",
        "text": "hello",
    }
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(
            acquire=AsyncMock(
                return_value=SimpleNamespace(allowed=True, wait_seconds=0)
            )
        ),
        client=SimpleNamespace(
            get_input_entity=AsyncMock(return_value=entity),
            send_message=AsyncMock(return_value=SimpleNamespace(id=88)),
        ),
        log=AsyncMock(),
    )

    await executor.action_send_message(ctx, action)
    await executor.action_send_message(ctx, action)

    assert ctx.client.get_input_entity.await_count == 2
    ctx.client.get_input_entity.assert_awaited_with("@qingbaobu")
    assert ctx.client.send_message.await_args_list[0].args[0] == entity
    assert ctx.client.send_message.await_args_list[1].args[0] == entity
    assert action["target_chat_id_resolved"] == 8395686237
    assert action["target_chat_resolved_ref"] == "@qingbaobu"


@pytest.mark.asyncio
async def test_scheduler_checks_command_permission_before_username_resolution(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    monkeypatch.setattr(scheduler_runtime, "_record_scheduler_action", AsyncMock())
    monkeypatch.setattr(
        scheduler_runtime,
        "should_allow_auto_command_text",
        lambda _text: (False, "blocked"),
    )
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(acquire=AsyncMock()),
        client=SimpleNamespace(
            get_input_entity=AsyncMock(),
            send_message=AsyncMock(),
        ),
        log=AsyncMock(),
    )

    with pytest.raises(scheduler_runtime.SchedulerCommandBlockedError):
        await executor.send_with_ratelimit(ctx, "@qingbaobu", ",blocked")

    ctx.client.get_input_entity.assert_not_awaited()
    ctx.engine.acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_username_floodwait_updates_engine_and_backoff(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    monkeypatch.setattr(scheduler_runtime, "_record_scheduler_action", AsyncMock())
    monkeypatch.setattr(
        scheduler_runtime,
        "_scheduler_trace_enabled",
        AsyncMock(return_value=False),
    )
    flood_wait = scheduler_runtime.FloodWaitError(request=None, capture=90)
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(
            acquire=AsyncMock(
                return_value=SimpleNamespace(allowed=True, wait_seconds=0)
            ),
            on_flood_wait=AsyncMock(),
        ),
        client=SimpleNamespace(
            get_input_entity=AsyncMock(side_effect=flood_wait),
            send_message=AsyncMock(),
        ),
        log=AsyncMock(),
    )
    cfg = {
        "action": {
            "type": "send_message",
            "target_chat_id": "@qingbaobu",
            "text": "hello",
        }
    }

    assert await executor.fire(ctx, 9, cfg) is False
    assert await executor.fire(ctx, 9, cfg) is False

    ctx.client.get_input_entity.assert_awaited_once()
    ctx.engine.on_flood_wait.assert_awaited_once_with("send_message_group", flood_wait)
    ctx.engine.acquire.assert_awaited_once()
    assert cfg["_target_retry_at"]


@pytest.mark.asyncio
async def test_scheduler_call_llm_resolves_target_before_model_cost(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    invoke_mock = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "invoke_ai_runtime", invoke_mock)
    monkeypatch.setattr(scheduler_runtime, "_record_scheduler_action", AsyncMock())
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(
            acquire=AsyncMock(
                return_value=SimpleNamespace(allowed=True, wait_seconds=0)
            ),
            on_flood_wait=AsyncMock(),
        ),
        client=SimpleNamespace(
            get_input_entity=AsyncMock(side_effect=ValueError("not found")),
            send_message=AsyncMock(),
        ),
        log=AsyncMock(),
    )

    with pytest.raises(scheduler_runtime.SchedulerTargetNotFoundError):
        await executor.action_call_llm(
            ctx,
            {
                "provider_id": 7,
                "prompt": "hello",
                "target_chat_id": "@missingbot",
            },
        )

    invoke_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_send_message_respects_trace_enabled_switch(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    start_trace = AsyncMock(return_value="evt_scheduler_disabled")
    finish_trace = AsyncMock()
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "_scheduler_trace_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(scheduler_runtime, "start_trace", start_trace)
    monkeypatch.setattr(scheduler_runtime, "finish_trace", finish_trace)
    monkeypatch.setattr(scheduler_runtime, "record_action", record_action)
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0))),
        client=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=88))),
        log=AsyncMock(),
    )

    ok = await executor.fire(ctx, 9, {"action": {"type": "send_message", "target_chat_id": 123, "text": "hello"}})

    assert ok is True
    ctx.client.send_message.assert_awaited_once_with(123, "hello")
    start_trace.assert_not_awaited()
    finish_trace.assert_not_awaited()
    record_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_send_message_failure_records_failed_action(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    trace = "evt_scheduler_fail"
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(scheduler_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(scheduler_runtime, "record_action", record_action)
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(acquire=AsyncMock(return_value=SimpleNamespace(allowed=True, wait_seconds=0))),
        client=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("telegram down"))),
        log=AsyncMock(),
    )
    cfg = {"action": {"type": "send_message", "target_chat_id": 123, "text": "hello"}}

    ok = await executor.fire(ctx, 9, cfg)

    assert ok is False
    assert "telegram down" in cfg["last_error"]
    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == scheduler_runtime.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["error_code"] == "telegram_api_error"


@pytest.mark.asyncio
async def test_scheduler_ratelimit_drop_records_skipped_action(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    trace = "evt_scheduler_skip"
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "start_trace", AsyncMock(return_value=trace))
    monkeypatch.setattr(scheduler_runtime, "finish_trace", AsyncMock())
    monkeypatch.setattr(scheduler_runtime, "record_action", record_action)
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        engine=SimpleNamespace(
            acquire=AsyncMock(return_value=SimpleNamespace(allowed=False, wait_seconds=0, outcome="limited"))
        ),
        client=SimpleNamespace(send_message=AsyncMock()),
        log=AsyncMock(),
    )

    ok = await executor.fire(ctx, 9, {"action": {"type": "send_message", "target_chat_id": 123, "text": "hello"}})

    assert ok is True
    ctx.client.send_message.assert_not_awaited()
    record_action.assert_awaited_once()
    assert record_action.await_args.args[2] == scheduler_runtime.TRACE_STATUS_SKIPPED
    assert record_action.await_args.kwargs["error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_scheduler_delete_failure_records_failed_action(monkeypatch) -> None:
    executor = SchedulerRuleExecutor()
    record_action = AsyncMock()
    monkeypatch.setattr(scheduler_runtime, "record_action", record_action)
    monkeypatch.setattr(scheduler_runtime.asyncio, "sleep", AsyncMock())
    ctx = SimpleNamespace(
        account_id=42,
        feature_key="scheduler",
        client=SimpleNamespace(delete_messages=AsyncMock(side_effect=RuntimeError("forbidden"))),
        log=AsyncMock(),
    )
    msg = SimpleNamespace(peer_id=123, id=88)

    await executor.delete_message_after(ctx, msg, 1, action_context={"trace_id": "evt_scheduler_delete"})

    record_action.assert_awaited_once()
    assert record_action.await_args.args[1]["context"]["trace_id"] == "evt_scheduler_delete"
    assert record_action.await_args.args[1]["type"] == "delete_message"
    assert record_action.await_args.args[2] == scheduler_runtime.TRACE_STATUS_FAILED
    assert record_action.await_args.kwargs["error_code"] == "telegram_api_error"


@pytest.mark.asyncio
async def test_scheduler_send出口_blocks_non_whitelisted_command_text() -> None:
    set_command_context(
        CommandContext(
            account_id=42,
            templates={},
            providers={},
            command_prefix="。",
            scheduler_command_whitelist=["允许"],
        )
    )
    executor = SchedulerRuleExecutor()
    ctx = SimpleNamespace(account_id=42, engine=AsyncMock(), client=AsyncMock(), log=AsyncMock())
    cfg = {
        "action": {
            "type": "send_message",
            "target_chat_id": 123,
            "text": "。禁止",
        }
    }

    ok = await executor.fire(ctx, 9, cfg)

    assert ok is False
    assert "blocked by whitelist" in cfg["last_error"]
    ctx.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_execute_rule_ipc_handler_replies_with_result() -> None:
    redis = SimpleNamespace(publish=AsyncMock())
    platform_scheduler = SimpleNamespace(
        execute_rule=AsyncMock(
            return_value=scheduler_runtime.SchedulerExecutionResult(True)
        )
    )
    reply_to = "worker_reply:42:exec_rule:test"
    cmd = IPCMessage(
        type=CMD_EXECUTE_RULE,
        payload={"reply_to": reply_to, "rule_id": 9},
    )

    await worker_runtime._handle_execute_rule_command(
        redis,
        42,
        platform_scheduler,
        cmd,
        reply_to,
        9,
    )

    platform_scheduler.execute_rule.assert_awaited_once_with(9)
    redis.publish.assert_awaited_once()
    reply_channel, raw_message = redis.publish.await_args.args
    assert reply_channel == reply_to
    message = IPCMessage.decode(raw_message)
    assert message.type == CMD_EXECUTE_RULE
    assert message.payload == {"ok": True, "error": None}
