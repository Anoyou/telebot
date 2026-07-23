"""全量已启用模型测活（0.57.0 阶段 C）。

在不新增常驻进程 / sidecar 的前提下，提供：

- 权威执行预览：基于最新数据库状态统计可执行 Provider、已启用模型、成本上限与
  跳过原因，供前端确认。
- 有界公平调度：全局并发上限 + 单 Provider 并发上限 + Provider 轮询取任务，避免
  单个大 Provider 独占队列。
- 逐项进度回调、可取消（协作式）、失败重测范围过滤。
- 429 只降低对应 Provider 并发；401 停止该 Provider 剩余任务。
- 手工测活**不修改**生产 runtime cooldown、不自动禁用模型；仅返回诊断结果。

安全红线：结果面向前端 / 审计，必须脱敏（不含 api_key / base_url / 代理）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from . import llm_diagnostics as diag

# ── 并发默认值 ──────────────────────────────────────────────
DEFAULT_GLOBAL_CONCURRENCY = 8
DEFAULT_PROVIDER_CONCURRENCY = 2
ALLOWED_GLOBAL_CONCURRENCY = (2, 4, 8, 12)

# 全量测活默认输出上限（成本保护；用户可上调但需看到成本影响）。
DEFAULT_FULL_MAX_TOKENS = 256
# 超过该模型数时，前端展示二次确认，后端也要求确认标记。
LARGE_RUN_MODEL_THRESHOLD = 30
# 跨 Job / 对话测活共享的进程级上游并发边界。
MAX_DIAGNOSTIC_CONCURRENCY = max(ALLOWED_GLOBAL_CONCURRENCY)
MAX_ACTIVE_LIVENESS_JOBS = 4
MAX_LIVENESS_TASKS = 2_000

_diagnostic_loop: asyncio.AbstractEventLoop | None = None
_diagnostic_semaphore: asyncio.Semaphore | None = None


def _diagnostic_limiter() -> asyncio.Semaphore:
    """返回当前事件循环专属的诊断并发器，兼容 pytest 多 loop。"""

    global _diagnostic_loop, _diagnostic_semaphore
    loop = asyncio.get_running_loop()
    if _diagnostic_loop is not loop or _diagnostic_semaphore is None:
        _diagnostic_loop = loop
        _diagnostic_semaphore = asyncio.Semaphore(MAX_DIAGNOSTIC_CONCURRENCY)
    return _diagnostic_semaphore


@asynccontextmanager
async def diagnostic_slot():
    """所有真实诊断上游调用共享的进程级并发槽位。"""

    async with _diagnostic_limiter():
        yield


@dataclass
class LivenessTask:
    """一个待测目标：Provider + 单个已启用模型。"""

    provider_id: int
    provider_name: str
    model_id: str


@dataclass
class ProviderPlan:
    """预览中单个 Provider 的目标模型与可执行性。"""

    provider_id: int
    provider_name: str
    enabled_models: list[str] = field(default_factory=list)
    skipped_reason: str | None = None  # None=可执行；否则为跳过原因（诊断状态）

    @property
    def executable(self) -> bool:
        return self.skipped_reason is None and bool(self.enabled_models)


@dataclass
class LivenessPreview:
    """全量测活执行预览（权威快照）。"""

    provider_total: int
    executable_provider_total: int
    enabled_model_total: int
    max_tokens: int
    global_concurrency: int
    provider_concurrency: int
    provider_plans: list[ProviderPlan]

    @property
    def task_total(self) -> int:
        return sum(len(p.enabled_models) for p in self.provider_plans if p.executable)

    @property
    def max_output_tokens(self) -> int:
        """最大可能输出 Token = 任务数 * max_tokens。"""
        return self.task_total * self.max_tokens

    @property
    def needs_confirmation(self) -> bool:
        return self.task_total > LARGE_RUN_MODEL_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_total": self.provider_total,
            "executable_provider_total": self.executable_provider_total,
            "enabled_model_total": self.enabled_model_total,
            "task_total": self.task_total,
            "max_tokens": self.max_tokens,
            "max_output_tokens": self.max_output_tokens,
            "global_concurrency": self.global_concurrency,
            "provider_concurrency": self.provider_concurrency,
            "needs_confirmation": self.needs_confirmation,
            "providers": [
                {
                    "provider_id": p.provider_id,
                    "provider_name": p.provider_name,
                    "enabled_models": list(p.enabled_models),
                    "executable": p.executable,
                    "skipped_reason": p.skipped_reason,
                }
                for p in self.provider_plans
            ],
        }


def normalize_global_concurrency(value: int | None) -> int:
    if value in ALLOWED_GLOBAL_CONCURRENCY:
        return int(value)
    return DEFAULT_GLOBAL_CONCURRENCY


def enabled_models_of(provider_row: Any) -> list[str]:
    """严格返回 ``models[].enabled == True`` 的模型 ID（不因 default_model 自动纳入）。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in getattr(provider_row, "models", None) or []:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled")):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
    return out


def _provider_has_credentials(provider_row: Any) -> bool:
    """Provider 是否具备可用凭据（ollama 本地部署例外）。"""
    if str(getattr(provider_row, "provider", "") or "").lower() == "ollama":
        return True
    return bool(getattr(provider_row, "api_key_enc", None))


def build_preview(
    provider_rows: list[Any],
    *,
    max_tokens: int = DEFAULT_FULL_MAX_TOKENS,
    global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
    provider_concurrency: int = DEFAULT_PROVIDER_CONCURRENCY,
    models_by_provider: dict[int, list[str]] | None = None,
) -> LivenessPreview:
    """基于最新 Provider 行生成权威执行预览。"""
    plans: list[ProviderPlan] = []
    enabled_total = 0
    executable_total = 0
    for row in provider_rows:
        models = enabled_models_of(row)
        if models_by_provider is not None:
            selected = set(models_by_provider.get(int(row.id), []))
            models = [model for model in models if model in selected]
        enabled_total += len(models)
        plan = ProviderPlan(
            provider_id=int(row.id),
            provider_name=str(row.name or ""),
            enabled_models=models,
        )
        if not _provider_has_credentials(row):
            plan.skipped_reason = diag.DIAG_CONFIG_ERROR
        elif not models:
            plan.skipped_reason = "no_enabled_models"
        if plan.executable:
            executable_total += 1
        plans.append(plan)
    return LivenessPreview(
        provider_total=len(provider_rows),
        executable_provider_total=executable_total,
        enabled_model_total=enabled_total,
        max_tokens=int(max_tokens),
        global_concurrency=normalize_global_concurrency(global_concurrency),
        provider_concurrency=int(provider_concurrency),
        provider_plans=plans,
    )


def build_task_pool(preview: LivenessPreview) -> list[LivenessTask]:
    """把预览展开为待测任务列表（仅可执行 Provider 的已启用模型）。"""
    tasks: list[LivenessTask] = []
    for plan in preview.provider_plans:
        if not plan.executable:
            continue
        for model_id in plan.enabled_models:
            tasks.append(
                LivenessTask(
                    provider_id=plan.provider_id,
                    provider_name=plan.provider_name,
                    model_id=model_id,
                )
            )
    return tasks


class CancelToken:
    """协作式取消令牌。

    ``cancel()`` 后调度器停止发起新任务，并取消在途 asyncio Task（触发
    httpx client 上下文退出，中断上游请求），避免前端已放弃仍继续烧 quota。
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        """阻塞直到被取消（供 watcher 与调度器共享）。"""
        await self._event.wait()


# 单个任务的执行器签名：接收 task，返回 (diagnostic_status, result_dict)。
TaskRunner = Callable[[LivenessTask], Awaitable[tuple[str, dict[str, Any]]]]


async def run_liveness_pool(
    tasks: list[LivenessTask],
    runner: TaskRunner,
    *,
    global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
    provider_concurrency: int = DEFAULT_PROVIDER_CONCURRENCY,
    on_result: Callable[[LivenessTask, str, dict[str, Any]], None] | None = None,
    cancel_token: CancelToken | None = None,
) -> list[dict[str, Any]]:
    """有界公平并发执行测活任务池。

    调度规则：
    - 全局并发 <= ``global_concurrency``；单 Provider 在途 <= ``provider_concurrency``。
    - Provider 轮询（round-robin）取任务，避免单个大 Provider 独占全局槽位。
    - ``429``（rate_limited）：把该 Provider 的并发上限降到 1，其它 Provider 继续。
    - ``401``（auth_failed）：停止该 Provider 剩余任务并标记为 config/auth 跳过。
    - 取消：未开始任务标记为 ``cancelled``；在途 asyncio Task 被 cancel，结果记
      ``cancelled``；已完成结果保留。
    - 已完成（含失败）不重试；重试由上层按范围重新构造任务池。

    返回每个任务的结果 dict（含 status / provider / model 等，已脱敏）。
    """
    global_concurrency = normalize_global_concurrency(global_concurrency)
    provider_concurrency = max(1, int(provider_concurrency))
    cancel_token = cancel_token or CancelToken()

    # 按 Provider 分组，保持轮询顺序。
    order: list[int] = []
    queues: dict[int, list[LivenessTask]] = {}
    for task in tasks:
        if task.provider_id not in queues:
            queues[task.provider_id] = []
            order.append(task.provider_id)
        queues[task.provider_id].append(task)

    inflight_by_provider: dict[int, int] = {pid: 0 for pid in order}
    provider_cap: dict[int, int] = {pid: provider_concurrency for pid in order}
    stopped_providers: set[int] = set()
    results: list[dict[str, Any]] = []
    # task_fut -> LivenessTask，取消在途时需要知道对应业务任务。
    running: dict[asyncio.Task[Any], LivenessTask] = {}
    rr_index = 0

    def _record(task: LivenessTask, status: str, payload: dict[str, Any]) -> None:
        entry = {
            "provider_id": task.provider_id,
            "provider_name": task.provider_name,
            "model_id": task.model_id,
            "status": status,
            **payload,
        }
        results.append(entry)
        if on_result is not None:
            on_result(task, status, entry)

    def _drain_provider(pid: int, status: str) -> None:
        """把某 Provider 剩余未开始任务标记为给定状态并清空队列。"""
        remaining = queues.get(pid) or []
        for task in remaining:
            _record(task, status, {"skipped": True})
        queues[pid] = []

    def _next_task() -> LivenessTask | None:
        """按 Provider 轮询选择下一个可执行任务，遵守单 Provider 并发上限。"""
        nonlocal rr_index
        n = len(order)
        for offset in range(n):
            pid = order[(rr_index + offset) % n]
            if pid in stopped_providers:
                continue
            if inflight_by_provider[pid] >= provider_cap[pid]:
                continue
            queue = queues.get(pid) or []
            if not queue:
                continue
            rr_index = (rr_index + offset + 1) % n
            task = queue.pop(0)
            return task
        return None

    async def _run(task: LivenessTask) -> tuple[LivenessTask, str, dict[str, Any]]:
        try:
            async with diagnostic_slot():
                status, payload = await runner(task)
        except asyncio.CancelledError:
            # 在途取消：记为 cancelled，不再向上抛以免调度器丢失结果。
            return (
                task,
                diag.DIAG_CANCELLED,
                {
                    "skipped": True,
                    "error": "cancelled",
                    "error_category": diag.DIAG_CANCELLED,
                    "suggestion": diag.suggestion_for(diag.DIAG_CANCELLED),
                },
            )
        except Exception as exc:  # noqa: BLE001
            status = diag.classify_exception(exc)
            payload = {"error": diag.redact(f"{type(exc).__name__}: {exc}")}
            return (task, status, payload)
        return (task, status, payload)

    def _has_pending() -> bool:
        return any(queues.get(pid) for pid in order if pid not in stopped_providers)

    def _cancel_inflight() -> None:
        """取消所有在途 asyncio Task（触发 httpx 上下文退出）。"""
        for fut in list(running):
            if not fut.done():
                fut.cancel()

    cancel_waiter = asyncio.create_task(cancel_token.wait())
    try:
        while True:
            # 取消：清空未开始任务，并中断在途请求。
            if cancel_token.cancelled:
                for pid in order:
                    _drain_provider(pid, diag.DIAG_CANCELLED)
                _cancel_inflight()

            # 尽量填满全局并发槽位。
            while not cancel_token.cancelled and len(running) < global_concurrency and _has_pending():
                task = _next_task()
                if task is None:
                    break
                inflight_by_provider[task.provider_id] += 1
                fut = asyncio.ensure_future(_run(task))
                running[fut] = task

            if not running:
                if cancel_token.cancelled or not _has_pending():
                    break
                break

            waiters = set(running.keys())
            if not cancel_token.cancelled:
                waiters.add(cancel_waiter)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if cancel_waiter in done:
                continue
            for fut in done:
                liveness_task = running.pop(fut)
                inflight_by_provider[liveness_task.provider_id] -= 1
                try:
                    task, status, payload = fut.result()
                except asyncio.CancelledError:
                    task = liveness_task
                    status = diag.DIAG_CANCELLED
                    payload = {
                        "skipped": True,
                        "error": "cancelled",
                        "error_category": diag.DIAG_CANCELLED,
                        "suggestion": diag.suggestion_for(diag.DIAG_CANCELLED),
                    }
                _record(task, status, payload)
                if status == diag.DIAG_RATE_LIMITED:
                    provider_cap[task.provider_id] = 1
                elif status == diag.DIAG_AUTH_FAILED:
                    stopped_providers.add(task.provider_id)
                    _drain_provider(task.provider_id, diag.DIAG_AUTH_FAILED)
    finally:
        cancel_waiter.cancel()
        await asyncio.gather(cancel_waiter, return_exceptions=True)

    return results


# ── 异步测活 Job 注册表（run_id / 轮询 / 取消 / 逐项进度） ────

_LIVENESS_JOB_TTL_SECONDS = 30 * 60  # 完成后保留 30 分钟供轮询
_LIVENESS_JOB_MAX = 64


class LivenessCapacityExceeded(RuntimeError):
    """进程内已有过多活跃测活任务，拒绝继续放大资源占用。"""


@dataclass
class LivenessJob:
    """一次全量测活运行的内存态（不落库；诊断窗口短期保留）。"""

    run_id: str
    status: str  # queued | running | completed | cancelled
    task_total: int
    created_at: float
    updated_at: float
    results: list[dict[str, Any]] = field(default_factory=list)
    cancel_token: CancelToken = field(default_factory=CancelToken)
    error: str | None = None
    bg_task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        items = list(self.results)
        healthy = sum(1 for r in items if r.get("status") == diag.DIAG_HEALTHY)
        cancelled = sum(1 for r in items if r.get("status") == diag.DIAG_CANCELLED)
        skipped = sum(1 for r in items if r.get("skipped") and r.get("status") != diag.DIAG_CANCELLED)
        failed = len(items) - healthy - cancelled - skipped
        return {
            "run_id": self.run_id,
            "status": self.status,
            "task_total": self.task_total,
            "completed": len(items),
            "healthy": healthy,
            "failed": max(0, failed),
            "skipped": skipped,
            "cancelled": cancelled,
            "results": items,
            "error": self.error,
        }


class LivenessJobRegistry:
    """进程内测活 Job 注册表。"""

    def __init__(self) -> None:
        self._jobs: dict[str, LivenessJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, task_total: int) -> LivenessJob:
        now = time.monotonic()
        job = LivenessJob(
            run_id=str(uuid.uuid4()),
            status="queued",
            task_total=int(task_total),
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._purge_locked(now)
            active = sum(existing.status in {"queued", "running"} for existing in self._jobs.values())
            if active >= MAX_ACTIVE_LIVENESS_JOBS:
                raise LivenessCapacityExceeded(f"已有 {active} 个测活任务运行中，请等待完成或先停止现有任务")
            # 容量保护：超限时剔除最旧已结束 job。
            while len(self._jobs) >= _LIVENESS_JOB_MAX:
                finished = [j for j in self._jobs.values() if j.status in {"completed", "cancelled"}]
                if not finished:
                    raise LivenessCapacityExceeded("测活任务历史已满，请稍后重试")
                oldest = min(finished, key=lambda j: j.updated_at)
                self._jobs.pop(oldest.run_id, None)
            self._jobs[job.run_id] = job
        return job

    def get(self, run_id: str) -> LivenessJob | None:
        return self._jobs.get(run_id)

    def cancel(self, run_id: str) -> LivenessJob | None:
        job = self._jobs.get(run_id)
        if job is None:
            return None
        job.cancel_token.cancel()
        job.updated_at = time.monotonic()
        return job

    def _purge_locked(self, now: float) -> None:
        expired = [
            rid
            for rid, job in self._jobs.items()
            if job.status in {"completed", "cancelled"} and (now - job.updated_at) > _LIVENESS_JOB_TTL_SECONDS
        ]
        for rid in expired:
            self._jobs.pop(rid, None)


# 模块级单例：API 进程内共享。
liveness_jobs = LivenessJobRegistry()


__all__ = [
    "ALLOWED_GLOBAL_CONCURRENCY",
    "DEFAULT_FULL_MAX_TOKENS",
    "DEFAULT_GLOBAL_CONCURRENCY",
    "DEFAULT_PROVIDER_CONCURRENCY",
    "LARGE_RUN_MODEL_THRESHOLD",
    "MAX_ACTIVE_LIVENESS_JOBS",
    "MAX_DIAGNOSTIC_CONCURRENCY",
    "MAX_LIVENESS_TASKS",
    "CancelToken",
    "LivenessCapacityExceeded",
    "LivenessJob",
    "LivenessJobRegistry",
    "LivenessPreview",
    "LivenessTask",
    "ProviderPlan",
    "build_preview",
    "build_task_pool",
    "enabled_models_of",
    "diagnostic_slot",
    "liveness_jobs",
    "normalize_global_concurrency",
    "run_liveness_pool",
]
