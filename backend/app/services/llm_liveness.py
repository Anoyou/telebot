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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import llm_diagnostics as diag

# ── 并发默认值 ──────────────────────────────────────────────
DEFAULT_GLOBAL_CONCURRENCY = 8
DEFAULT_PROVIDER_CONCURRENCY = 2
ALLOWED_GLOBAL_CONCURRENCY = (2, 4, 8, 12)

# 全量测活默认输出上限（成本保护；用户可上调但需看到成本影响）。
DEFAULT_FULL_MAX_TOKENS = 256
# 超过该模型数触发前端二次确认（由前端消费；此处仅作为常量约定）。
LARGE_RUN_MODEL_THRESHOLD = 30


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
) -> LivenessPreview:
    """基于最新 Provider 行生成权威执行预览。"""
    plans: list[ProviderPlan] = []
    enabled_total = 0
    executable_total = 0
    for row in provider_rows:
        models = enabled_models_of(row)
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
    """协作式取消令牌。"""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


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
    - 取消：未开始任务标记为 ``cancelled``，不再发起新请求；已完成结果保留。
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
    running: set[asyncio.Task[Any]] = set()
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
            status, payload = await runner(task)
        except Exception as exc:  # noqa: BLE001
            status = diag.classify_exception(exc)
            payload = {"error": diag.redact(f"{type(exc).__name__}: {exc}")}
        return (task, status, payload)

    def _has_pending() -> bool:
        return any(queues.get(pid) for pid in order if pid not in stopped_providers)

    while True:
        # 取消：清空所有未开始任务，等待在途完成后退出。
        if cancel_token.cancelled:
            for pid in order:
                _drain_provider(pid, diag.DIAG_CANCELLED)

        # 尽量填满全局并发槽位。
        while (
            not cancel_token.cancelled
            and len(running) < global_concurrency
            and _has_pending()
        ):
            task = _next_task()
            if task is None:
                break
            inflight_by_provider[task.provider_id] += 1
            running.add(asyncio.ensure_future(_run(task)))

        if not running:
            if cancel_token.cancelled or not _has_pending():
                break
            # 无在途且无可调度（全部达到 per-provider 上限但仍有 pending）——
            # 理论上不会发生，因为 provider_cap>=1 且 inflight 会随完成回落。
            break

        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for fut in done:
            running.discard(fut)
            task, status, payload = fut.result()
            inflight_by_provider[task.provider_id] -= 1
            _record(task, status, payload)
            # 失败类别策略（不换身份、不改生产 cooldown）：
            if status == diag.DIAG_RATE_LIMITED:
                # 429：该 Provider 并发降到 1（本轮生效）。
                provider_cap[task.provider_id] = 1
            elif status == diag.DIAG_AUTH_FAILED:
                # 401：停止该 Provider 剩余任务，避免所有模型重复失败。
                stopped_providers.add(task.provider_id)
                _drain_provider(task.provider_id, diag.DIAG_AUTH_FAILED)

    return results


__all__ = [
    "ALLOWED_GLOBAL_CONCURRENCY",
    "DEFAULT_FULL_MAX_TOKENS",
    "DEFAULT_GLOBAL_CONCURRENCY",
    "DEFAULT_PROVIDER_CONCURRENCY",
    "LARGE_RUN_MODEL_THRESHOLD",
    "CancelToken",
    "LivenessPreview",
    "LivenessTask",
    "ProviderPlan",
    "build_preview",
    "build_task_pool",
    "enabled_models_of",
    "normalize_global_concurrency",
    "run_liveness_pool",
]
