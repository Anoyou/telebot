"""阶段 C：全量已启用模型测活测试。

覆盖：
- 预览只统计 models[].enabled == True；缺凭据 / 无启用模型标记不可执行。
- 公平调度：单个大 Provider 不独占全局槽位。
- 全局并发上限与单 Provider 并发上限。
- 取消：未开始任务标记 cancelled。
- 429 只降低对应 Provider 并发；401 停止该 Provider 剩余任务。
- 结果脱敏（redact 不含 api_key / base_url）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import llm_diagnostics as diag
from app.services import llm_liveness as lv


class _Row:
    def __init__(self, pid, name, models, *, provider="openai", api_key_enc="enc", api_format="chat_completions"):
        self.id = pid
        self.name = name
        self.models = models
        self.provider = provider
        self.api_key_enc = api_key_enc
        self.api_format = api_format


def _model(mid, enabled):
    return {"id": mid, "enabled": enabled}


# ── 预览：只数已启用模型 ────────────────────────────────────


def test_preview_counts_only_enabled_models() -> None:
    rows = [
        _Row(1, "p1", [_model("a", True), _model("b", False), _model("c", True)]),
        _Row(2, "p2", [_model("x", False)]),  # 无启用模型 → 不可执行
        _Row(3, "p3", [_model("y", True)], api_key_enc=None),  # 缺凭据 → 不可执行
    ]
    preview = lv.build_preview(rows)
    assert preview.enabled_model_total == 3  # a,c,y (y 统计但 provider 不可执行)
    # 只有 p1 可执行（2 个模型）。
    assert preview.executable_provider_total == 1
    assert preview.task_total == 2
    p2 = next(p for p in preview.provider_plans if p.provider_id == 2)
    assert p2.skipped_reason == "no_enabled_models"
    p3 = next(p for p in preview.provider_plans if p.provider_id == 3)
    assert p3.skipped_reason == diag.DIAG_CONFIG_ERROR


def test_preview_ollama_needs_no_key() -> None:
    rows = [_Row(1, "local", [_model("llama", True)], provider="ollama", api_key_enc=None)]
    preview = lv.build_preview(rows)
    assert preview.executable_provider_total == 1
    assert preview.task_total == 1


def test_preview_max_output_tokens_cost_estimate() -> None:
    rows = [_Row(1, "p", [_model("a", True), _model("b", True)])]
    preview = lv.build_preview(rows, max_tokens=100)
    assert preview.max_output_tokens == 2 * 100


def test_preview_needs_confirmation_for_large_run() -> None:
    models = [_model(f"m{i}", True) for i in range(lv.LARGE_RUN_MODEL_THRESHOLD + 1)]
    rows = [_Row(1, "big", models)]
    preview = lv.build_preview(rows)
    assert preview.needs_confirmation is True


# ── 调度：并发上限 / 公平 / 取消 / 429 / 401 ────────────────


@pytest.mark.asyncio
async def test_global_concurrency_cap_respected() -> None:
    tasks = [lv.LivenessTask(p, f"p{p}", f"m{p}-{i}") for p in range(1, 5) for i in range(5)]
    peak = 0
    current = 0
    lock = asyncio.Lock()

    async def runner(task):
        nonlocal peak, current
        async with lock:
            current += 1
            peak = max(peak, current)
        await asyncio.sleep(0.01)
        async with lock:
            current -= 1
        return (diag.DIAG_HEALTHY, {"latency_ms": 1})

    await lv.run_liveness_pool(tasks, runner, global_concurrency=4, provider_concurrency=2)
    assert peak <= 4


@pytest.mark.asyncio
async def test_provider_concurrency_cap_respected() -> None:
    tasks = [lv.LivenessTask(1, "p1", f"m{i}") for i in range(6)]
    peak = 0
    current = 0
    lock = asyncio.Lock()

    async def runner(task):
        nonlocal peak, current
        async with lock:
            current += 1
            peak = max(peak, current)
        await asyncio.sleep(0.01)
        async with lock:
            current -= 1
        return (diag.DIAG_HEALTHY, {})

    await lv.run_liveness_pool(tasks, runner, global_concurrency=8, provider_concurrency=2)
    assert peak <= 2


@pytest.mark.asyncio
async def test_fair_scheduling_no_single_provider_monopoly() -> None:
    # 一个大 Provider（20 任务）+ 一个小 Provider（2 任务）。
    tasks = [lv.LivenessTask(1, "big", f"b{i}") for i in range(20)]
    tasks += [lv.LivenessTask(2, "small", f"s{i}") for i in range(2)]
    completion_order: list[int] = []

    async def runner(task):
        await asyncio.sleep(0.005)
        completion_order.append(task.provider_id)
        return (diag.DIAG_HEALTHY, {})

    await lv.run_liveness_pool(tasks, runner, global_concurrency=4, provider_concurrency=2)
    # 小 Provider 的两个任务不应等到大 Provider 全部完成才开始：
    # 它们应在前若干个完成里出现（轮询保证）。
    first_ten = completion_order[:10]
    assert 2 in first_ten


@pytest.mark.asyncio
async def test_cancel_marks_unstarted_as_cancelled() -> None:
    tasks = [lv.LivenessTask(1, "p1", f"m{i}") for i in range(10)]
    token = lv.CancelToken()
    started = 0

    async def runner(task):
        nonlocal started
        started += 1
        if started == 2:
            token.cancel()
        await asyncio.sleep(0.01)
        return (diag.DIAG_HEALTHY, {})

    results = await lv.run_liveness_pool(
        tasks, runner, global_concurrency=2, provider_concurrency=2, cancel_token=token
    )
    cancelled = [r for r in results if r["status"] == diag.DIAG_CANCELLED]
    assert len(cancelled) > 0
    # 真实取消会中断在途请求，因此本例不应留下健康结果。
    healthy = [r for r in results if r["status"] == diag.DIAG_HEALTHY]
    assert healthy == []
    assert len(results) == len(tasks)


@pytest.mark.asyncio
async def test_cancel_interrupts_inflight_tasks() -> None:
    tasks = [lv.LivenessTask(1, "p1", f"m{i}") for i in range(4)]
    token = lv.CancelToken()
    entered = asyncio.Event()
    interrupted = 0

    async def runner(task):
        nonlocal interrupted
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            interrupted += 1
            raise
        return (diag.DIAG_HEALTHY, {})

    pool_task = asyncio.create_task(
        lv.run_liveness_pool(
            tasks, runner, global_concurrency=2, provider_concurrency=2, cancel_token=token
        )
    )
    await entered.wait()
    token.cancel()
    results = await asyncio.wait_for(pool_task, timeout=1)
    assert interrupted == 2
    assert len(results) == len(tasks)
    assert all(r["status"] == diag.DIAG_CANCELLED for r in results)


@pytest.mark.asyncio
async def test_job_cancel_stays_running_until_background_finishes() -> None:
    registry = lv.LivenessJobRegistry()
    job = await registry.create(task_total=1)
    job.status = "running"
    cancelled = registry.cancel(job.run_id)
    assert cancelled is job
    assert job.cancel_token.cancelled is True
    assert job.status == "running"


@pytest.mark.asyncio
async def test_429_reduces_only_that_provider_concurrency() -> None:
    tasks = [lv.LivenessTask(1, "p1", f"m{i}") for i in range(8)]
    peak_after_429 = 0
    current = 0
    seen_429 = False
    lock = asyncio.Lock()

    async def runner(task):
        nonlocal peak_after_429, current, seen_429
        async with lock:
            current += 1
            if seen_429:
                peak_after_429 = max(peak_after_429, current)
        await asyncio.sleep(0.01)
        async with lock:
            current -= 1
        if not seen_429:
            seen_429 = True
            return (diag.DIAG_RATE_LIMITED, {})
        return (diag.DIAG_HEALTHY, {})

    await lv.run_liveness_pool(tasks, runner, global_concurrency=4, provider_concurrency=3)
    # 429 后该 Provider 并发降到 1。
    assert peak_after_429 <= 1


@pytest.mark.asyncio
async def test_401_stops_remaining_tasks_of_provider() -> None:
    tasks = [lv.LivenessTask(1, "p1", f"m{i}") for i in range(6)]
    tasks += [lv.LivenessTask(2, "p2", f"n{i}") for i in range(3)]

    async def runner(task):
        await asyncio.sleep(0.005)
        if task.provider_id == 1:
            return (diag.DIAG_AUTH_FAILED, {})
        return (diag.DIAG_HEALTHY, {})

    results = await lv.run_liveness_pool(tasks, runner, global_concurrency=4, provider_concurrency=1)
    p1 = [r for r in results if r["provider_id"] == 1]
    # p1 第一个 401 后，其余标记 auth_failed 且未真正执行（skipped）。
    auth_failed = [r for r in p1 if r["status"] == diag.DIAG_AUTH_FAILED]
    assert len(auth_failed) == 6
    # 至少有一部分是被 drain 掉的 skipped。
    assert any(r.get("skipped") for r in p1)
    # p2 不受影响，全部 healthy。
    p2 = [r for r in results if r["provider_id"] == 2]
    assert all(r["status"] == diag.DIAG_HEALTHY for r in p2)
    assert len(p2) == 3


# ── 脱敏 ────────────────────────────────────────────────────


def test_redact_strips_key_and_url() -> None:
    text = "error with sk-abcdefghijklmnop at https://api.secret.example/v1 Bearer tok-xyz"
    out = diag.redact(text, api_key="sk-abcdefghijklmnop", base_url="https://api.secret.example/v1")
    assert "sk-abcdefghijklmnop" not in out
    assert "api.secret.example" not in out
    assert "tok-xyz" not in out
