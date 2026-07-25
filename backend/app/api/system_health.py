"""系统健康概览 API。

提供：
  - ``GET /api/system/health-overview``  一次性返回所有运维向状态：
    DB 连通 + alembic 版本同步 / Redis / LLM provider 池 / 代理 / 账号 worker 状态分布

设计目标：
- 所有探测 ≤ 2s 超时；任一项失败不影响其他项；前端能在 Dashboard 一眼看清"系统健不健康"
- 不返回敏感字段（不含明文 api_key、不含 proxy 密码、不含 session_str）
- 老数据兼容：getattr 兜底，避免历史迁移没跑齐时直接 500
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from datetime import time as datetime_time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text

from .. import __version__
from ..crypto import decrypt_str
from ..db.base import AsyncSessionLocal
from ..db.models.account import Account, Proxy
from ..db.models.command import LLMProvider
from ..db.models.system import SystemSetting
from ..deps import CurrentUser
from ..redis_client import get_redis
from ..services import auth_service
from ..settings import settings
from ..util.update_plan import build_update_plan, classify_changed_files
from ..util.update_target import (
    normalize_update_branch,
    normalize_update_remote,
    normalize_update_target,
)

router = APIRouter(prefix="/api/system", tags=["system"])
log = logging.getLogger(__name__)
_APP_STARTED_AT = time.time()


# ════════════════════════════════════════════════════════════
# 0.4.2 版本号端点（public — 前端启动 / 未登录页都要能调）
# ════════════════════════════════════════════════════════════


class VersionInfo(BaseModel):
    """``GET /api/system/version`` 响应。

    前端启动时拉一次 + 每 60s 轮询，对比前端 ``APP_VERSION`` 检测前后端版本是否一致；
    不一致时 sidebar 顶部弹红条提示用户 `make restart` + 硬刷浏览器。

    public 接口（**无鉴权**）：未登录页也要能调，否则发现"前端是新版后端是旧版"
    会一直登不上去（旧 schema 拒新登录字段之类）。返回的字段都是公开的版本元数据。
    """

    version: str
    """SemVer 形式，如 ``0.4.2``"""
    stage: str | None = None
    """非正式标签，如 ``Sprint 4``；达到 1.0.0 后通常 None"""


@router.get("/version", response_model=VersionInfo)
async def get_version() -> VersionInfo:
    """返回后端版本号（无鉴权）。"""
    from .. import APP_STAGE

    return VersionInfo(version=__version__, stage=APP_STAGE)


# ════════════════════════════════════════════════════════════
# Schemas
# ════════════════════════════════════════════════════════════


class DbStatus(BaseModel):
    ok: bool
    version: str | None = None
    """形如 ``"PostgreSQL 16.1"``。失败时为 None；error 字段含原因。"""
    error: str | None = None


class AlembicStatus(BaseModel):
    ok: bool
    """``True`` 表示 DB 当前版本 == 代码 head；``False`` 表示需要跑 ``alembic upgrade head``。"""
    current: str | None = None
    """DB 里 ``alembic_version`` 表的版本字符串。"""
    head: str | None = None
    """代码仓库里 alembic 链的最新版本。"""
    pending: list[str] = Field(default_factory=list)
    """已经写在文件里、但还没 apply 到 DB 的迁移版本号列表（按时间序）。"""
    error: str | None = None


class RedisStatus(BaseModel):
    ok: bool
    error: str | None = None


class ProvidersStatus(BaseModel):
    total: int = 0
    with_api_key: int = 0
    """配齐了 api_key（或 ollama 本地）能直接被调的数量。"""
    with_proxy: int = 0
    """指定了出口代理的 provider 数量；其余走 DIRECT。"""
    by_modality: dict[str, int] = Field(default_factory=dict)
    """按 modality 计数，如 ``{"text":2,"vision":1,"multimodal":1}``。"""
    by_cost_tier: dict[str, int] = Field(default_factory=dict)
    """按 cost_tier 计数，如 ``{"1":1,"2":2,"3":1}``。key 是 str 是因为 JSON 不支持 int 键。"""


class ProxiesStatus(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    """如 ``{"socks5":2,"http":1}``。mtproxy 也算在内；前端展示"可用于 LLM 的"由前端过滤。"""
    used_by_llm: int = 0
    """被某个 LLMProvider.proxy_id 引用的代理数量（去重）。"""


class WorkersStatus(BaseModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    """如 ``{"active":3,"paused":1,"login_required":1,"dead":0,"floodwait":0}``。"""
    runtime_total: int = 0
    """supervisor 持有的 worker 句柄总数。"""
    runtime_alive: int = 0
    """当前子进程 alive=true 的数量。"""
    runtime_desired_running: int = 0
    """desired=running 的数量。"""
    runtime_desired_running_alive: int = 0
    """desired=running 且 alive=true 的数量。"""
    runtime_failing: int = 0
    """fail_count>0 的数量。"""
    db_connection_estimate: int = 0
    db_connection_budget: int = 0
    db_capacity_warning: str | None = None


class HealthOverview(BaseModel):
    """前端 Dashboard 用的一次性聚合状态。"""

    db: DbStatus
    alembic: AlembicStatus
    redis: RedisStatus
    providers: ProvidersStatus
    proxies: ProxiesStatus
    workers: WorkersStatus


class HostResource(BaseModel):
    cpu_percent: float | None = None
    memory_used_percent: float | None = None
    memory_total_mb: int | None = None
    disk_used_percent: float | None = None
    disk_free_gb: float | None = None
    sampled_at: int
    uptime_seconds: int | None = None


class ProcessResource(BaseModel):
    pid: int | None = None
    cpu_percent: float | None = None
    rss_mb: float | None = None
    uss_mb: float | None = None


class WorkerRuntimeResource(BaseModel):
    account_id: int
    pid: int | None = None
    alive: bool
    desired: str
    fail_count: int
    cpu_percent: float | None = None
    rss_mb: float | None = None
    uss_mb: float | None = None


class ContainerResource(BaseModel):
    id: str | None = None
    name: str
    service: str | None = None
    cpu_percent: float | None = None
    memory_mb: float | None = None
    memory_limit_mb: float | None = None
    memory_percent: float | None = None


class RuntimeLogStats(BaseModel):
    last_5m_total: int = 0
    last_5m_warn: int = 0
    last_5m_error: int = 0


class ResourceDashboard(BaseModel):
    host: HostResource
    main_process: ProcessResource
    project_total: ProcessResource
    app_uptime_seconds: int | None = None
    other_processes: list[ProcessResource] = Field(default_factory=list)
    containers: list[ContainerResource] = Field(default_factory=list)
    container_total: ProcessResource = Field(default_factory=ProcessResource)
    container_probe_error: str | None = None
    workers: list[WorkerRuntimeResource] = Field(default_factory=list)
    worker_alive: int = 0
    worker_desired_running: int = 0
    logs: RuntimeLogStats


# ════════════════════════════════════════════════════════════
# 各子探测
# ════════════════════════════════════════════════════════════


async def _probe_db() -> DbStatus:
    """``SELECT version()`` 顺手把 DB 版本号也带回来。"""
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(text("SELECT version()"))).scalar()
            ver_str = str(row or "").strip()
            # 把超长字符串截断；PostgreSQL 16.1 (Debian 16.1-1.pgdg120+1) on x86_64...
            if len(ver_str) > 80:
                ver_str = ver_str[:80].rstrip() + "..."
            return DbStatus(ok=True, version=ver_str)
    except Exception as e:  # noqa: BLE001
        return DbStatus(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")


async def _probe_redis() -> RedisStatus:
    try:
        r = get_redis()
        pong = await r.ping()
        if not pong:
            return RedisStatus(ok=False, error="PING returned falsy")
        return RedisStatus(ok=True)
    except Exception as e:  # noqa: BLE001
        return RedisStatus(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")


def _probe_alembic() -> AlembicStatus:
    """对比 DB 里 alembic_version 与代码仓库里的 head。

    同步实现（alembic API 都是同步）；调用方应在 ``asyncio.to_thread`` 里跑。
    """
    try:
        from pathlib import Path

        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine

        from ..settings import settings

        ini_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        if not ini_path.exists():
            return AlembicStatus(ok=False, error=f"alembic.ini 不存在：{ini_path}")

        cfg = Config(str(ini_path))
        script = ScriptDirectory.from_config(cfg)
        head_rev = script.get_current_head() or ""

        # 同步引擎读 alembic_version
        sync_engine = create_engine(settings.database_url_sync)
        try:
            with sync_engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                current = ctx.get_current_revision() or ""
        finally:
            sync_engine.dispose()

        in_sync = bool(head_rev) and current == head_rev
        pending: list[str] = []
        if not in_sync and head_rev:
            # 列出从 current 到 head 之间还差哪几个迁移
            try:
                for rev in script.walk_revisions(base="base", head=head_rev):
                    if rev.revision == current:
                        break
                    pending.append(rev.revision)
                pending.reverse()  # walk_revisions 默认 head→base，反过来变 base→head
            except Exception:
                pending = []
        return AlembicStatus(ok=in_sync, current=current or None, head=head_rev or None, pending=pending)
    except Exception as e:  # noqa: BLE001
        return AlembicStatus(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")


async def _probe_providers() -> ProvidersStatus:
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(LLMProvider))).scalars().all()
        total = len(rows)
        with_key = sum(1 for r in rows if r.api_key_enc or (r.provider or "").lower() == "ollama")
        with_proxy = sum(1 for r in rows if r.proxy_id is not None)
        by_modality: Counter[str] = Counter((getattr(r, "modality", None) or "text") for r in rows)
        by_cost_tier: Counter[str] = Counter(str(int(getattr(r, "cost_tier", None) or 2)) for r in rows)
        return ProvidersStatus(
            total=total,
            with_api_key=with_key,
            with_proxy=with_proxy,
            by_modality=dict(by_modality),
            by_cost_tier=dict(by_cost_tier),
        )
    except Exception:  # noqa: BLE001
        # 失败时返空统计而不是抛——alembic 不同步时 SELECT * 会爆，但 alembic 探测自己会标 ok=False
        return ProvidersStatus()


async def _probe_proxies() -> ProxiesStatus:
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Proxy))).scalars().all()
            # 被 LLMProvider 引用的 proxy id 集合
            used_ids = (
                (await db.execute(select(LLMProvider.proxy_id).where(LLMProvider.proxy_id.is_not(None))))
                .scalars()
                .all()
            )
        used_set = {x for x in used_ids if x is not None}
        by_type: Counter[str] = Counter((p.type or "?").lower() for p in rows)
        return ProxiesStatus(
            total=len(rows),
            by_type=dict(by_type),
            used_by_llm=len(used_set),
        )
    except Exception:  # noqa: BLE001
        return ProxiesStatus()


async def _probe_workers() -> WorkersStatus:
    """返回 DB 状态分布 + supervisor runtime 聚合。"""
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(select(Account.status, func.count(Account.id)).group_by(Account.status))
            ).all()
        total = sum(int(c) for _, c in rows)
        by_status = {str(s): int(c) for s, c in rows}
        runtime_total = 0
        runtime_alive = 0
        runtime_desired_running = 0
        runtime_desired_running_alive = 0
        runtime_failing = 0
        try:
            from ..worker.supervisor import get_worker_runtime_snapshot

            runtime_rows = get_worker_runtime_snapshot()
            runtime_total = len(runtime_rows)
            for row in runtime_rows:
                alive = bool(row.get("alive"))
                desired = str(row.get("desired") or "running")
                fail_count = int(row.get("fail_count") or 0)
                if alive:
                    runtime_alive += 1
                if desired == "running":
                    runtime_desired_running += 1
                    if alive:
                        runtime_desired_running_alive += 1
                if fail_count > 0:
                    runtime_failing += 1
        except Exception:
            # runtime 快照失败时不影响 DB 状态统计
            pass
        db_connection_budget = max(1, int(os.environ.get("POSTGRES_MAX_CONNECTIONS", "30") or 30))
        db_connection_estimate = (
            max(1, int(settings.db_pool_size or 1))
            + max(0, int(settings.db_max_overflow or 0))
            + runtime_desired_running
            * (
                max(1, int(settings.db_pool_size_worker or 1))
                + max(0, int(settings.db_max_overflow_worker or 0))
            )
        )
        db_capacity_warning = None
        if db_connection_estimate >= int(db_connection_budget * 0.8):
            db_capacity_warning = (
                f"预估 DB 连接峰值 {db_connection_estimate}/{db_connection_budget}，"
                "已达容量预警线；增加账号前请先扩容或收紧连接池。"
            )
        return WorkersStatus(
            total=total,
            by_status=by_status,
            runtime_total=runtime_total,
            runtime_alive=runtime_alive,
            runtime_desired_running=runtime_desired_running,
            runtime_desired_running_alive=runtime_desired_running_alive,
            runtime_failing=runtime_failing,
            db_connection_estimate=db_connection_estimate,
            db_connection_budget=db_connection_budget,
            db_capacity_warning=db_capacity_warning,
        )
    except Exception:  # noqa: BLE001
        return WorkersStatus()


def _read_memory_percent() -> tuple[float | None, int | None]:
    """读取系统内存占用百分比与总内存（MB）。

    优先用 psutil，避免 macOS ``vm_stat`` 里累计 pageins/pageouts 这类计数被误当
    成物理页总数；没有 psutil 时再按平台做轻量 fallback。
    """

    try:
        import psutil  # type: ignore[import-not-found]

        mem = psutil.virtual_memory()
        total_mb = int(float(mem.total) / (1024 * 1024))
        return round(float(mem.percent), 2), total_mb
    except Exception:
        pass

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            rows: dict[str, int] = {}
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                raw = value.strip().split()[0]
                rows[key] = int(raw)  # kB
            total_kb = rows.get("MemTotal")
            avail_kb = rows.get("MemAvailable")
            if total_kb and avail_kb is not None and total_kb > 0:
                used_percent = (1.0 - (avail_kb / total_kb)) * 100.0
                return round(max(0.0, min(100.0, used_percent)), 2), int(total_kb // 1024)
        except Exception:
            pass

    try:
        total_bytes = int(
            subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            ).strip()
        )
        out = subprocess.check_output(
            ["vm_stat"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
        page_size = 4096
        free_pages = 0
        for line in out.splitlines():
            if "page size of" in line:
                parts = line.split("page size of", 1)[1].strip().split()
                page_size = int(parts[0])
            elif ":" in line:
                k, v = line.split(":", 1)
                num = int(v.strip().rstrip(".").replace(".", ""))
                if (
                    k.startswith("Pages free")
                    or k.startswith("Pages inactive")
                    or k.startswith("Pages speculative")
                ):
                    free_pages += num
        if total_bytes > 0:
            free_bytes = free_pages * page_size
            used_percent = (1.0 - (free_bytes / total_bytes)) * 100.0
            return round(max(0.0, min(100.0, used_percent)), 2), int(total_bytes // (1024 * 1024))
    except Exception:
        pass

    return None, None


# PID -> psutil.Process 实例缓存。
#
# 重要：psutil.Process.cpu_percent(interval=None) 的第一次调用只是初始化采样窗口
# ——返回 0；下一次调用才能给出"自上次以来"的真实 CPU 使用率。
# 旧实现用 ``time.sleep(0.05)`` 强行造一个差分窗口，这在 1C 小机器上 5s 一次轮询
# 累积下来就是固定的额外阻塞。改成跨请求缓存 Process 实例后：
#   - 首次拉取一个 PID：返回 cpu=None（前端会展示 "-"）
#   - 后续拉取：返回真实差分 cpu%，不再 sleep
#
# 用 ``create_time`` 鉴别 PID 复用——两个进程 PID 相同但 create_time 不同时，
# 把缓存的旧 Process 替换。Worker exit 时也会因为下一次 stats 调用拿不到 process
# 而被 ``_purge_stale_process_cache`` 清掉。
_PROC_CACHE: dict[int, tuple[Any, float]] = {}


def _purge_stale_process_cache(active_pids: set[int]) -> None:
    """请求结束时把已退出的 worker 进程从缓存里清掉，防止字典无限增长。"""
    for pid in list(_PROC_CACHE.keys()):
        if pid not in active_pids:
            _PROC_CACHE.pop(pid, None)


def _read_process_stats_with_psutil(
    pids: list[int],
) -> dict[int, tuple[float | None, float | None, float | None]] | None:
    """用 psutil 读取 PID -> (cpu%, rssMB, ussMB)。

    Oracle / Linux 服务环境里 ``ps`` 的输出、权限与容器视图差异比较多；psutil 直接读
    procfs，稳定性更好。未安装或被系统限制时返回 None，让调用方走 ``ps`` fallback。
    """

    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        return None

    rows: dict[int, tuple[float | None, float | None, float | None]] = {}
    for pid in pids:
        try:
            proc = None
            create_time: float | None = None
            try:
                cached = _PROC_CACHE.get(pid)
                if cached is not None:
                    proc, create_time = cached
                    # 探活 + 反 PID-reuse：create_time 不同说明 PID 被复用
                    if not proc.is_running() or proc.create_time() != create_time:
                        proc = None
            except Exception:
                proc = None
            if proc is None:
                proc = psutil.Process(int(pid))
                # 初始化采样窗口；首次返回的 0 我们不展示，记一次后再用
                proc.cpu_percent(interval=None)
                _PROC_CACHE[pid] = (proc, proc.create_time())
                # 首次见到该 PID：rss 现在就能读，但 cpu% 此时无差分可言
                rss_mb, uss_mb = _read_process_memory_mb(proc)
                rows[pid] = (None, rss_mb, uss_mb)
                continue
            cpu = float(proc.cpu_percent(interval=None))
            rss_mb, uss_mb = _read_process_memory_mb(proc)
            rows[pid] = (round(max(0.0, cpu), 2), rss_mb, uss_mb)
        except Exception:
            _PROC_CACHE.pop(pid, None)
            continue
    return rows


def _read_process_memory_mb(proc: Any) -> tuple[float | None, float | None]:
    """读取进程 RSS/USS MB。USS 更接近进程独占内存；取不到时返回 None。"""

    rss_mb: float | None = None
    uss_mb: float | None = None
    try:
        rss_mb = float(proc.memory_info().rss) / (1024 * 1024)
    except Exception:
        rss_mb = None
    try:
        full = proc.memory_full_info()
        uss = getattr(full, "uss", None)
        if uss is not None:
            uss_mb = float(uss) / (1024 * 1024)
    except Exception:
        uss_mb = None
    return (
        round(max(0.0, rss_mb), 2) if rss_mb is not None else None,
        round(max(0.0, uss_mb), 2) if uss_mb is not None else None,
    )


def _read_process_stats_with_ps(
    pids: list[int],
) -> dict[int, tuple[float | None, float | None, float | None]]:
    """用系统 ``ps`` 读取 PID -> (cpu%, rssMB, ussMB)，作为 psutil fallback。"""

    if not pids:
        return {}
    try:
        args = ["ps", "-o", "pid=,pcpu=,rss=", "-p", ",".join(str(p) for p in pids)]
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True, timeout=2.0)
    except Exception:
        return {}
    rows: dict[int, tuple[float | None, float | None, float | None]] = {}
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            cpu = float(parts[1])
            rss_mb = float(parts[2]) / 1024.0
            rows[pid] = (round(cpu, 2), round(rss_mb, 2), None)
        except Exception:
            continue
    return rows


def _read_process_stats(pids: list[int]) -> dict[int, tuple[float | None, float | None, float | None]]:
    """读取 PID -> (cpu%, rssMB, ussMB)。优先 psutil，失败再 fallback 到 ps。"""

    if not pids:
        return {}
    rows = _read_process_stats_with_psutil(pids)
    if rows is not None:
        return rows
    return _read_process_stats_with_ps(pids)


def _snapshot_dashboard_host() -> HostResource:
    mem_percent, mem_total_mb = _read_memory_percent()
    du = shutil.disk_usage("/")
    disk_used_percent = round((du.used / du.total) * 100.0, 2) if du.total > 0 else None
    disk_free_gb = round(du.free / (1024**3), 2)

    return HostResource(
        cpu_percent=_read_host_cpu_percent(),
        memory_used_percent=mem_percent,
        memory_total_mb=mem_total_mb,
        disk_used_percent=disk_used_percent,
        disk_free_gb=disk_free_gb,
        sampled_at=int(time.time()),
        uptime_seconds=_read_host_uptime_seconds(),
    )


def _read_host_uptime_seconds() -> int | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return max(0, int(time.time() - float(psutil.boot_time())))
    except Exception:
        pass

    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            first = fh.read().split()[0]
        return max(0, int(float(first)))
    except Exception:
        return None


def _read_app_uptime_seconds() -> int | None:
    try:
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process(os.getpid())
        return max(0, int(time.time() - float(proc.create_time())))
    except Exception:
        return max(0, int(time.time() - _APP_STARTED_AT))


def _read_host_cpu_percent() -> float | None:
    """读取整机 CPU 利用率。psutil 可用时读真实 CPU%，否则回退 load average 压力值。"""

    try:
        import psutil  # type: ignore[import-not-found]

        return round(max(0.0, min(100.0, float(psutil.cpu_percent(interval=None)))), 2)
    except Exception:
        pass

    try:
        if hasattr(os, "getloadavg"):
            load1, _, _ = os.getloadavg()
            cpus = os.cpu_count() or 1
            return round(max(0.0, min(100.0, (load1 / cpus) * 100.0)), 2)
    except Exception:
        return None
    return None


def _sum_resource_values(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    if not known:
        return None
    return round(sum(known), 2)


def _sum_project_resource(
    main: ProcessResource,
    workers: list[WorkerRuntimeResource],
    other_processes: list[ProcessResource] | None = None,
) -> ProcessResource:
    extras = other_processes or []
    return ProcessResource(
        pid=None,
        cpu_percent=_sum_resource_values(
            [
                main.cpu_percent,
                *(w.cpu_percent for w in workers),
                *(p.cpu_percent for p in extras),
            ]
        ),
        rss_mb=_sum_resource_values([main.rss_mb, *(w.rss_mb for w in workers), *(p.rss_mb for p in extras)]),
        uss_mb=_sum_resource_values([main.uss_mb, *(w.uss_mb for w in workers), *(p.uss_mb for p in extras)]),
    )


_DOCKER_RESOURCE_CACHE: tuple[float, list[ContainerResource], str | None] = (0.0, [], None)
_DOCKER_RESOURCE_TTL = 12.0
_PROJECT_CONTAINER_SERVICES = {"postgres", "redis", "frontend"}
_WEB_CONTAINER_SERVICES = {"web", "backend"}


def _repo_root_for_container_match() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_docker_labels(raw: str | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not raw:
        return labels
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        labels[key.strip()] = value.strip()
    return labels


def _project_container_names() -> set[str]:
    root_name = _repo_root_for_container_match().name.lower()
    names = {root_name, "telepilot", "telebot"}
    env_name = os.getenv("COMPOSE_PROJECT_NAME")
    if env_name:
        names.add(env_name.lower())
    return {name for name in names if name}


def _looks_like_project_container(
    name: str,
    service: str | None,
    labels: dict[str, str],
) -> bool:
    service_l = (service or "").lower()
    name_l = name.lower()
    root = str(_repo_root_for_container_match())
    working_dir = labels.get("com.docker.compose.project.working_dir")
    config_files = labels.get("com.docker.compose.project.config_files", "")
    if service_l in _WEB_CONTAINER_SERVICES:
        return False
    if service_l in _PROJECT_CONTAINER_SERVICES and (working_dir == root or root in config_files):
        return True
    project = (labels.get("com.docker.compose.project") or "").lower()
    if service_l in _PROJECT_CONTAINER_SERVICES and project in _project_container_names():
        return True
    if name_l in {"telebot-postgres", "telebot-redis"}:
        return True
    for project_name in _project_container_names():
        for sep in ("-", "_"):
            for service_name in _PROJECT_CONTAINER_SERVICES:
                if name_l in {
                    f"{project_name}{sep}{service_name}",
                    f"{project_name}{sep}{service_name}{sep}1",
                }:
                    return True
    return False


def _docker_container_meta() -> tuple[dict[str, dict[str, str | None]], str | None]:
    """读取当前项目相关容器的 Docker 元数据。Docker 不可用时返回空。"""

    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{json .}}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except FileNotFoundError:
        return {}, "Docker CLI 不可用，未计入数据库/Redis/前端容器"
    except Exception:
        return {}, "Docker 不可用或无权限，未计入数据库/Redis/前端容器"

    meta: dict[str, dict[str, str | None]] = {}
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        cid = str(row.get("ID") or "").strip()
        name = str(row.get("Names") or row.get("Name") or "").strip()
        labels = _parse_docker_labels(row.get("Labels"))
        service = labels.get("com.docker.compose.service")
        if not name or not _looks_like_project_container(name, service, labels):
            continue
        item = {"id": cid or None, "name": name, "service": service}
        meta[name] = item
        if cid:
            meta[cid] = item
    return meta, None


def _parse_percent_value(raw: Any) -> float | None:
    try:
        text = str(raw).strip().rstrip("%")
        if not text:
            return None
        return round(float(text), 2)
    except Exception:
        return None


def _parse_size_to_mb(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b|b)$", text, re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1 / (1024 * 1024),
        "kb": 1000 / (1024 * 1024),
        "kib": 1 / 1024,
        "mb": (1000 * 1000) / (1024 * 1024),
        "mib": 1,
        "gb": (1000 * 1000 * 1000) / (1024 * 1024),
        "gib": 1024,
        "tb": (1000 * 1000 * 1000 * 1000) / (1024 * 1024),
        "tib": 1024 * 1024,
    }
    factor = factors.get(unit)
    if factor is None:
        return None
    return round(max(0.0, value * factor), 2)


def _parse_docker_memory_usage(raw: str | None) -> tuple[float | None, float | None]:
    if not raw or "/" not in raw:
        return None, None
    used_raw, limit_raw = raw.split("/", 1)
    return _parse_size_to_mb(used_raw), _parse_size_to_mb(limit_raw)


def _snapshot_project_containers() -> tuple[list[ContainerResource], str | None]:
    """读取数据库、Redis、前端等项目容器的资源占用。

    Web 容器内的主进程和 worker 已由进程明细覆盖，这里刻意不把 web 容器计入，
    避免把同一份 Python 进程内存重复统计。
    """

    global _DOCKER_RESOURCE_CACHE
    now = time.monotonic()
    cached_at, cached, cached_error = _DOCKER_RESOURCE_CACHE
    if now - cached_at < _DOCKER_RESOURCE_TTL:
        return cached, cached_error

    meta, meta_error = _docker_container_meta()
    if not meta:
        _DOCKER_RESOURCE_CACHE = (now, [], meta_error)
        return [], meta_error

    try:
        out = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.5,
        )
    except Exception:
        error = "Docker stats 不可用或超时，未计入数据库/Redis/前端容器"
        _DOCKER_RESOURCE_CACHE = (now, [], error)
        return [], error

    containers: list[ContainerResource] = []
    seen: set[str] = set()
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        lookup_keys = [
            str(row.get("Name") or "").strip(),
            str(row.get("Container") or "").strip(),
            str(row.get("ID") or "").strip(),
        ]
        item = next((meta[key] for key in lookup_keys if key in meta), None)
        if item is None:
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        memory_mb, memory_limit_mb = _parse_docker_memory_usage(row.get("MemUsage"))
        containers.append(
            ContainerResource(
                id=item.get("id"),
                name=name,
                service=item.get("service"),
                cpu_percent=_parse_percent_value(row.get("CPUPerc")),
                memory_mb=memory_mb,
                memory_limit_mb=memory_limit_mb,
                memory_percent=_parse_percent_value(row.get("MemPerc")),
            )
        )

    containers.sort(key=lambda c: c.memory_mb or 0.0, reverse=True)
    _DOCKER_RESOURCE_CACHE = (now, containers, None)
    return containers, None


def _sum_container_resource(containers: list[ContainerResource]) -> ProcessResource:
    return ProcessResource(
        pid=None,
        cpu_percent=_sum_resource_values([c.cpu_percent for c in containers]),
        rss_mb=_sum_resource_values([c.memory_mb for c in containers]),
        uss_mb=None,
    )


def _merge_project_and_container_resource(
    process_total: ProcessResource,
    container_total: ProcessResource,
) -> ProcessResource:
    container_memory = container_total.rss_mb
    return ProcessResource(
        pid=None,
        cpu_percent=_sum_resource_values([process_total.cpu_percent, container_total.cpu_percent]),
        rss_mb=_sum_resource_values([process_total.rss_mb, container_memory]),
        uss_mb=_sum_resource_values([process_total.uss_mb, container_memory]),
    )


def _discover_descendant_pids(root_pids: list[int]) -> list[int]:
    """发现 Web/worker 派生的额外子进程 PID，覆盖短期插件/安装任务等。"""

    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        return []

    roots = {int(pid) for pid in root_pids}
    found: set[int] = set()
    for pid in roots:
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                child_pid = int(child.pid)
                if child_pid not in roots:
                    found.add(child_pid)
        except Exception:
            continue
    return sorted(found)


async def _snapshot_dashboard_workers() -> tuple[
    list[WorkerRuntimeResource],
    ProcessResource,
    ProcessResource,
    list[ProcessResource],
]:
    try:
        from ..worker.supervisor import get_worker_runtime_snapshot

        runtime = get_worker_runtime_snapshot()
    except Exception:
        runtime = []

    main_pid = os.getpid()
    worker_pids = [int(r["pid"]) for r in runtime if isinstance(r.get("pid"), int)]
    other_pids = _discover_descendant_pids([main_pid, *worker_pids])
    all_pids = [main_pid, *worker_pids, *other_pids]
    stats = _read_process_stats(all_pids)
    _purge_stale_process_cache(set(all_pids))

    main_cpu, main_rss, main_uss = stats.get(main_pid, (None, None, None))
    main = ProcessResource(
        pid=main_pid,
        cpu_percent=main_cpu,
        rss_mb=main_rss,
        uss_mb=main_uss,
    )

    workers: list[WorkerRuntimeResource] = []
    for row in runtime:
        pid = int(row["pid"]) if isinstance(row.get("pid"), int) else None
        cpu, rss, uss = stats.get(pid, (None, None, None)) if pid is not None else (None, None, None)
        workers.append(
            WorkerRuntimeResource(
                account_id=int(row.get("account_id") or 0),
                pid=pid,
                alive=bool(row.get("alive")),
                desired=str(row.get("desired") or "running"),
                fail_count=int(row.get("fail_count") or 0),
                cpu_percent=cpu,
                rss_mb=rss,
                uss_mb=uss,
            )
        )

    workers.sort(
        key=lambda w: 0.0 if w.rss_mb is None else w.rss_mb,
        reverse=True,
    )
    other_processes: list[ProcessResource] = []
    for pid in other_pids:
        cpu, rss, uss = stats.get(pid, (None, None, None))
        other_processes.append(ProcessResource(pid=pid, cpu_percent=cpu, rss_mb=rss, uss_mb=uss))
    return workers, main, _sum_project_resource(main, workers, other_processes), other_processes


_RUNTIME_LOG_STATS_CACHE: tuple[float, RuntimeLogStats] = (0.0, RuntimeLogStats())
_RUNTIME_LOG_STATS_TTL = 10.0  # Dashboard 默认 15s+ 轮询，10s memo 几乎不影响数据新鲜度


async def _snapshot_runtime_log_stats() -> RuntimeLogStats:
    """读取过去 5 分钟 runtime_log 行数。Dashboard 高频轮询时短缓存避免 N+1 count。"""

    global _RUNTIME_LOG_STATS_CACHE
    now = time.monotonic()
    cached_at, cached = _RUNTIME_LOG_STATS_CACHE
    if cached and now - cached_at < _RUNTIME_LOG_STATS_TTL:
        return cached
    try:
        from datetime import UTC, timedelta

        from ..db.models.log import RuntimeLog

        since = datetime.now(UTC) - timedelta(minutes=5)
        async with AsyncSessionLocal() as db:
            total_stmt = select(func.count(RuntimeLog.id)).where(RuntimeLog.ts >= since)
            warn_stmt = select(func.count(RuntimeLog.id)).where(
                RuntimeLog.ts >= since,
                RuntimeLog.level.in_(("warn", "warning")),
            )
            err_stmt = select(func.count(RuntimeLog.id)).where(
                RuntimeLog.ts >= since,
                RuntimeLog.level == "error",
            )
            total = int((await db.execute(total_stmt)).scalar() or 0)
            warn = int((await db.execute(warn_stmt)).scalar() or 0)
            err = int((await db.execute(err_stmt)).scalar() or 0)
        result = RuntimeLogStats(last_5m_total=total, last_5m_warn=warn, last_5m_error=err)
        _RUNTIME_LOG_STATS_CACHE = (now, result)
        return result
    except Exception:
        return RuntimeLogStats()


# ════════════════════════════════════════════════════════════
# 路由
# ════════════════════════════════════════════════════════════


@router.get("/health-overview", response_model=HealthOverview)
async def get_health_overview(_user: CurrentUser) -> HealthOverview:
    """聚合一次性返所有运维状态。各子探测并行 + 各自带 2s 超时。"""

    async def _safe(coro: Any, fallback: Any) -> Any:
        try:
            return await asyncio.wait_for(coro, timeout=2.0)
        except (TimeoutError, Exception):
            return fallback

    db_t = _safe(_probe_db(), DbStatus(ok=False, error="timeout/exception"))
    redis_t = _safe(_probe_redis(), RedisStatus(ok=False, error="timeout/exception"))
    providers_t = _safe(_probe_providers(), ProvidersStatus())
    proxies_t = _safe(_probe_proxies(), ProxiesStatus())
    workers_t = _safe(_probe_workers(), WorkersStatus())
    # alembic 探测是同步阻塞，扔到线程池跑
    alembic_t = _safe(asyncio.to_thread(_probe_alembic), AlembicStatus(ok=False, error="timeout"))

    db, alembic, redis_, providers, proxies, workers = await asyncio.gather(
        db_t, alembic_t, redis_t, providers_t, proxies_t, workers_t
    )
    return HealthOverview(
        db=db,
        alembic=alembic,
        redis=redis_,
        providers=providers,
        proxies=proxies,
        workers=workers,
    )


@router.get("/resource-dashboard", response_model=ResourceDashboard)
async def get_resource_dashboard(_user: CurrentUser) -> ResourceDashboard:
    """V1 资源占用概览：主机 + Web 主进程 + worker + 项目容器 + 5 分钟日志量。"""

    host = _snapshot_dashboard_host()
    workers, main, process_total, other_processes = await _snapshot_dashboard_workers()
    containers, container_probe_error = _snapshot_project_containers()
    container_total = _sum_container_resource(containers)
    project_total = _merge_project_and_container_resource(process_total, container_total)
    logs = await _snapshot_runtime_log_stats()
    return ResourceDashboard(
        host=host,
        main_process=main,
        project_total=project_total,
        app_uptime_seconds=_read_app_uptime_seconds(),
        other_processes=other_processes[:8],
        containers=containers[:8],
        container_total=container_total,
        container_probe_error=container_probe_error,
        workers=workers[:8],
        worker_alive=sum(1 for w in workers if w.alive),
        worker_desired_running=sum(1 for w in workers if w.desired == "running"),
        logs=logs,
    )


# ════════════════════════════════════════════════════════════
# 检查更新 / 拉取更新 / 重启（分步确认式）
# ════════════════════════════════════════════════════════════


def _git_root() -> Path | None:
    """返回项目 git 仓库根目录（含 .git），找不到返回 None。"""
    try:
        p = Path(__file__).resolve()
        for parent in (p, *p.parents):
            if (parent / ".git").exists():
                return parent
    except Exception:
        pass
    return None


GIT_WORKTREE_UNAVAILABLE_MESSAGE = (
    "当前运行环境不是 Git 工作树，无法在应用容器内执行 git 更新。"
    "如果你使用 Docker / 一键部署，请在服务器上进入部署目录，重新拉取镜像或运行部署脚本更新。"
)

RUNTIME_LOCAL_SOURCE = "local_source"
RUNTIME_PROD_CONTAINER_WITH_UPDATER = "prod_container_with_updater"
RUNTIME_PROD_CONTAINER_MANUAL = "prod_container_manual"
RUNTIME_UNSUPPORTED = "unsupported"


def _run_git(*args: str, timeout: int = 30) -> tuple[str, str, int]:
    """同步执行 git 命令，返回 (stdout, stderr, returncode)。"""
    root = _git_root()
    if not root:
        return "", GIT_WORKTREE_UNAVAILABLE_MESSAGE, 1
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "git command timed out", 1
    except Exception as e:
        return "", str(e), 1


def _version_at_git_ref(ref: str) -> str | None:
    """从指定 Git ref 的后端版本源读取应用版本。"""
    out, _, rc = _run_git("show", f"{ref}:backend/app/__init__.py", timeout=10)
    if rc != 0:
        return None
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', out, re.MULTILINE)
    return match.group(1).strip() if match else None


def _default_update_remote_branch() -> tuple[str, str]:
    remote = (os.getenv("TELEPILOT_UPDATE_REMOTE") or "origin").strip() or "origin"
    env_branch = (os.getenv("TELEPILOT_UPDATE_BRANCH") or "").strip()
    if env_branch:
        return remote, env_branch
    upstream_out, _, upstream_rc = _run_git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=5
    )
    if upstream_rc == 0 and "/" in upstream_out:
        upstream_remote, upstream_branch = upstream_out.split("/", 1)
        return upstream_remote or remote, upstream_branch or "main"
    branch_out, _, branch_rc = _run_git("rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    if branch_rc == 0 and branch_out and branch_out != "HEAD":
        return remote, branch_out
    return remote, "main"


def _is_container_runtime() -> bool:
    """检测当前是否运行在容器内。"""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup")
        if cgroup.exists():
            text = cgroup.read_text(encoding="utf-8", errors="ignore").lower()
            return any(marker in text for marker in ("docker", "containerd", "kubepods", "podman"))
    except Exception:
        pass
    return False


def _resolve_host_updater() -> str | None:
    """返回可执行的宿主机更新器路径。"""
    candidates: list[str] = []
    env_path = (os.getenv("TELEPILOT_HOST_UPDATER") or "").strip()
    if env_path:
        candidates.append(env_path)
    candidates.append("/app/host-updater/prod-update")
    for path_str in candidates:
        path = Path(path_str)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def _resolve_http_updater() -> str | None:
    """返回可用的内部 updater sidecar URL。"""

    raw = (os.getenv("TELEPILOT_UPDATER_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    try:
        req = urllib.request.Request(f"{raw}/health", method="GET")
        with urllib.request.urlopen(req, timeout=0.8) as resp:  # noqa: S310 - internal configured URL
            if 200 <= int(resp.status) < 300:
                return raw
    except Exception:
        return None
    return None


def _updater_token() -> str:
    return (os.getenv("TELEPILOT_UPDATER_TOKEN") or "").strip()


def _updater_request(
    path: str, payload: dict[str, Any] | None = None, *, timeout: int = 30
) -> dict[str, Any]:
    url = _resolve_http_updater()
    if not url:
        raise RuntimeError("内部 updater 不可用")
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = _updater_token()
    if token:
        headers["X-TelePilot-Updater-Token"] = token
    req = urllib.request.Request(f"{url}{path}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - internal configured URL
            text = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"error": text or str(exc)}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", False)
            return parsed
        return {"ok": False, "error": str(exc)}
    parsed = json.loads(text) if text else {}
    return parsed if isinstance(parsed, dict) else {}


def _updater_get(path: str, *, timeout: int = 10) -> dict[str, Any]:
    url = _resolve_http_updater()
    if not url:
        raise RuntimeError("内部 updater 不可用")
    headers: dict[str, str] = {}
    token = _updater_token()
    if token:
        headers["X-TelePilot-Updater-Token"] = token
    req = urllib.request.Request(f"{url}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - internal configured URL
            text = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"error": text or str(exc)}
        if isinstance(parsed, dict):
            parsed.setdefault("ok", False)
            return parsed
        return {"ok": False, "error": str(exc)}
    parsed = json.loads(text) if text else {}
    return parsed if isinstance(parsed, dict) else {}


def _detect_runtime_mode() -> tuple[str, str | None, Path | None]:
    root = _git_root()
    in_container = _is_container_runtime()
    updater = _resolve_http_updater() or _resolve_host_updater()

    if not in_container and root and (root / "Makefile").exists():
        return RUNTIME_LOCAL_SOURCE, updater, root
    if in_container and updater:
        return RUNTIME_PROD_CONTAINER_WITH_UPDATER, updater, root
    if in_container:
        return RUNTIME_PROD_CONTAINER_MANUAL, updater, root
    return RUNTIME_UNSUPPORTED, updater, root


def _classify_changed_files(changed_files: list[str]) -> tuple[list[str], bool, bool]:
    plan = classify_changed_files(changed_files)
    return plan.components, plan.requires_full_update, plan.requires_backup


def _manual_command_for_runtime(runtime_mode: str, updater: str | None) -> str | None:
    if runtime_mode == RUNTIME_PROD_CONTAINER_MANUAL:
        return "cd /opt/telepilot && make prod-update"
    return None


def _action_required_for_plan(
    runtime_mode: str,
    has_update: bool,
    components: list[str],
    requires_full_update: bool,
) -> str:
    if not has_update:
        return "none"
    if runtime_mode == RUNTIME_PROD_CONTAINER_MANUAL:
        return "manual"
    if runtime_mode == RUNTIME_UNSUPPORTED:
        return "unsupported"
    if requires_full_update or "full_update" in components:
        return "full_update"
    if components == ["docs_only"] or "docs_only" in components:
        return "docs_only"
    has_backend = "backend" in components
    has_frontend = "frontend" in components
    has_updater = "updater" in components
    if sum((has_backend, has_frontend, has_updater)) > 1:
        return "mixed"
    if has_backend:
        return "backend"
    if has_frontend:
        return "frontend"
    if has_updater:
        return "updater"
    return "full_update"


def _plan_text(
    runtime_mode: str,
    has_update: bool,
    components: list[str],
    services: list[str],
    file_sync_services: list[str],
    rebuild_services: list[str],
    requires_full_update: bool,
    requires_backup: bool,
    requires_migration: bool,
    can_apply: bool,
) -> tuple[str, str]:
    if not has_update:
        return "已是最新版本", "当前代码与目标分支一致，无需更新。"

    label = "检测到可更新变更"
    detail_parts: list[str] = []
    if components and components != ["none"]:
        detail_parts.append(f"变更分类：{', '.join(components)}")
    if file_sync_services:
        detail_parts.append(
            f"直接同步目标 commit 文件并重启：{', '.join(file_sync_services)}。"
        )
    if rebuild_services:
        detail_parts.append(f"需要编译或镜像构建：{', '.join(rebuild_services)}。")
    if services and not file_sync_services and not rebuild_services:
        # 兼容尚未提供动作分类的旧 updater。
        detail_parts.append(f"仅切换服务：{', '.join(services)}；其余容器保持运行。")
    elif components == ["docs_only"]:
        detail_parts.append("不需要重建或重启运行服务。")
    if requires_backup:
        detail_parts.append("包含数据库迁移，将先备份再切换 web。")
    elif not requires_migration:
        detail_parts.append("不执行数据库备份或迁移。")
    if requires_full_update:
        detail_parts.append("涉及部署/依赖关键文件，建议完整更新流程。")

    if runtime_mode == RUNTIME_LOCAL_SOURCE:
        if can_apply:
            detail_parts.append("可直接在当前节点执行应用更新。")
            label = "可直接应用更新"
        else:
            detail_parts.append("当前变更建议走完整更新流程。")
            label = "建议完整更新"
    elif runtime_mode == RUNTIME_PROD_CONTAINER_WITH_UPDATER:
        detail_parts.append("当前运行于容器，需调用宿主机更新器。")
        label = "需调用宿主机更新器"
    elif runtime_mode == RUNTIME_PROD_CONTAINER_MANUAL:
        detail_parts.append(
            "当前运行于容器且无更新器，无法在容器内直接检查 Git 远程差异；需人工在宿主机执行更新。"
        )
        label = "需在宿主机更新"
    else:
        detail_parts.append("当前运行环境不支持自动更新。")
        label = "环境不支持自动更新"

    return label, " ".join(detail_parts)


def _check_response_from_plan(
    *,
    runtime_mode: str,
    updater: str | None,
    remote: str,
    branch: str,
    plan: dict[str, Any],
    can_apply: bool,
    manual_command: str | None,
) -> CheckUpdateResponse:
    if not plan.get("ok", True):
        return CheckUpdateResponse(
            remote=remote,
            branch=branch,
            runtime_mode=runtime_mode,
            update_executor=updater,
            can_apply=can_apply,
            manual_command=manual_command,
            plan_label="更新检查失败",
            plan_detail="执行远程更新检查失败，请查看错误信息。",
            error=str(plan.get("error") or "更新检查失败"),
        )
    has_update = bool(plan.get("has_update"))
    changed_files = [str(item) for item in plan.get("changed_files") or []]
    commit_titles = [
        str(item).strip()[:240]
        for item in plan.get("commit_titles") or []
        if str(item).strip()
    ][:30]
    components = [str(item) for item in plan.get("components") or ["none"]]
    services = [str(item) for item in plan.get("services") or []]
    file_sync_services = [str(item) for item in plan.get("file_sync_services") or []]
    rebuild_services = [str(item) for item in plan.get("rebuild_services") or []]
    requires_full_update = bool(plan.get("requires_full_update"))
    requires_backup = bool(plan.get("requires_backup"))
    requires_migration = bool(plan.get("requires_migration"))
    action_required = _action_required_for_plan(
        runtime_mode,
        has_update,
        components,
        requires_full_update,
    )
    plan_label, plan_detail = _plan_text(
        runtime_mode=runtime_mode,
        has_update=has_update,
        components=components,
        services=services,
        file_sync_services=file_sync_services,
        rebuild_services=rebuild_services,
        requires_full_update=requires_full_update,
        requires_backup=requires_backup,
        requires_migration=requires_migration,
        can_apply=can_apply,
    )
    return CheckUpdateResponse(
        has_update=has_update,
        current_version=str(plan.get("current_version") or "") or None,
        target_version=str(plan.get("target_version") or "") or None,
        current_commit=str(plan.get("current_commit") or "") or None,
        remote_commit=str(plan.get("remote_commit") or "") or None,
        ahead=int(plan.get("ahead") or 0),
        remote=remote,
        branch=branch,
        changed_files=changed_files,
        commit_titles=commit_titles,
        runtime_mode=runtime_mode,
        update_executor=updater,
        action_required=action_required,
        plan_label=plan_label,
        plan_detail=plan_detail,
        components=components,
        services=services,
        file_sync_services=file_sync_services,
        rebuild_services=rebuild_services,
        requires_full_update=requires_full_update,
        requires_backup=requires_backup,
        requires_migration=requires_migration,
        can_apply=can_apply,
        manual_command=manual_command,
    )


class CheckUpdateResponse(BaseModel):
    has_update: bool = False
    can_check: bool = True
    """False 表示当前环境无法在进程内自动比对远程差异（如容器内无 updater / 无工作树），
    此时 ``has_update`` 不代表真实结论，前端应展示"无法自动检查"而不是"有更新/已最新"。"""
    current_commit: str | None = None
    remote_commit: str | None = None
    current_version: str | None = None
    target_version: str | None = None
    ahead: int = 0
    remote: str = "origin"
    branch: str = "main"
    runtime_mode: str = RUNTIME_UNSUPPORTED
    update_executor: str | None = None
    action_required: str = "none"
    plan_label: str = ""
    plan_detail: str = ""
    changed_files: list[str] = Field(default_factory=list)
    commit_titles: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=lambda: ["none"])
    services: list[str] = Field(default_factory=list)
    file_sync_services: list[str] = Field(default_factory=list)
    rebuild_services: list[str] = Field(default_factory=list)
    requires_full_update: bool = False
    requires_backup: bool = False
    requires_migration: bool = False
    can_apply: bool = False
    manual_command: str | None = None
    error: str | None = None


class PullUpdateResponse(BaseModel):
    success: bool = False
    new_commit: str | None = None
    summary: str | None = None
    job_id: str | None = None
    status: str | None = None
    remote: str = "origin"
    branch: str = "main"
    runtime_mode: str = RUNTIME_UNSUPPORTED
    update_executor: str | None = None
    action_required: str = "none"
    plan_label: str = ""
    plan_detail: str = ""
    changed_files: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=lambda: ["none"])
    services: list[str] = Field(default_factory=list)
    file_sync_services: list[str] = Field(default_factory=list)
    rebuild_services: list[str] = Field(default_factory=list)
    requires_full_update: bool = False
    requires_backup: bool = False
    requires_migration: bool = False
    can_apply: bool = False
    manual_command: str | None = None
    error: str | None = None


class RestartResponse(BaseModel):
    success: bool = False
    error: str | None = None


class UpdateRequest(BaseModel):
    remote: str | None = None
    branch: str | None = None
    full: bool = False

    @field_validator("remote")
    @classmethod
    def validate_remote(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_update_remote(value)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_update_branch(value)


class UpdateJobStatusResponse(BaseModel):
    ok: bool = False
    job_id: str
    status: str = "unknown"
    created_at: int | None = None
    started_at: int | None = None
    finished_at: int | None = None
    returncode: int | None = None
    remote: str | None = None
    branch: str | None = None
    new_commit: str | None = None
    summary: str | None = None
    error: str | None = None
    progress: int | None = None
    phase: str | None = None
    detail: str | None = None
    logs: list[str] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    # WP-U1：各阶段耗时（phase + duration_ms）
    step_timings: list[dict[str, Any]] = Field(default_factory=list)


class UpdateTargetOptionsResponse(BaseModel):
    ok: bool = False
    remotes: list[str] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=list)
    remote: str | None = None
    error: str | None = None


async def _resolve_update_request(payload: UpdateRequest | None) -> tuple[str, str, bool]:
    if not isinstance(payload, UpdateRequest):
        payload = None
    default_remote, default_branch = _default_update_remote_branch()
    try:
        defaults = normalize_update_target({"remote": default_remote, "branch": default_branch})
    except ValueError:
        log.warning("忽略环境或 Git upstream 中的非法更新目标，回退 origin/main")
        defaults = {"remote": "origin", "branch": "main"}
    default_remote = defaults["remote"]
    default_branch = defaults["branch"]
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "app_update_target")
        if row is not None and isinstance(row.value, dict):
            try:
                saved = normalize_update_target(row.value)
            except ValueError:
                log.warning("忽略数据库中的非法 app_update_target，继续使用安全默认值")
            else:
                default_remote = saved["remote"]
                default_branch = saved["branch"]
    except Exception:  # noqa: BLE001
        pass
    candidate = payload or UpdateRequest()
    target = normalize_update_target(
        {
            "remote": candidate.remote or default_remote,
            "branch": candidate.branch or default_branch,
        }
    )
    return target["remote"], target["branch"], bool(candidate.full)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get("/update-target-options", response_model=UpdateTargetOptionsResponse)
async def get_update_target_options(
    _user: CurrentUser,
    remote: str | None = None,
) -> UpdateTargetOptionsResponse:
    """返回当前 Git 工作树可选的远端与远端分支。"""
    default_remote, _, _ = await _resolve_update_request(None)
    try:
        selected_remote = UpdateRequest(remote=remote or default_remote).remote or default_remote
    except ValueError as exc:
        return UpdateTargetOptionsResponse(error=str(exc))

    runtime_mode, updater, root = await asyncio.to_thread(_detect_runtime_mode)
    if runtime_mode == RUNTIME_PROD_CONTAINER_WITH_UPDATER and updater:
        result = await asyncio.to_thread(
            _updater_request,
            "/targets",
            {"remote": selected_remote},
            timeout=45,
        )
        return UpdateTargetOptionsResponse(
            ok=bool(result.get("ok")),
            remotes=[str(item) for item in result.get("remotes") or []],
            branches=[str(item) for item in result.get("branches") or []],
            remote=str(result.get("remote") or selected_remote),
            error=str(result.get("error") or "") or None,
        )

    if runtime_mode == RUNTIME_LOCAL_SOURCE and root is not None:

        def read_local_targets() -> UpdateTargetOptionsResponse:
            remotes_proc = subprocess.run(
                ["git", "remote"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            remotes = [item.strip() for item in remotes_proc.stdout.splitlines() if item.strip()]
            selected = (
                selected_remote
                if selected_remote in remotes
                else (remotes[0] if remotes else selected_remote)
            )
            heads_proc = subprocess.run(
                ["git", "ls-remote", "--heads", selected],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            branches = sorted(
                {
                    line.split("\t", 1)[-1].removeprefix("refs/heads/")
                    for line in heads_proc.stdout.splitlines()
                    if "refs/heads/" in line
                },
                key=lambda item: (item != "main", item),
            )
            return UpdateTargetOptionsResponse(
                ok=remotes_proc.returncode == 0 and heads_proc.returncode == 0,
                remotes=remotes,
                branches=branches,
                remote=selected,
                error=(heads_proc.stderr.strip() or remotes_proc.stderr.strip() or None),
            )

        try:
            return await asyncio.to_thread(read_local_targets)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return UpdateTargetOptionsResponse(
                remotes=[selected_remote],
                remote=selected_remote,
                error=f"读取 Git 目标失败: {type(exc).__name__}: {exc}",
            )

    return UpdateTargetOptionsResponse(
        remotes=[selected_remote],
        remote=selected_remote,
        error="当前运行环境无法读取 Git 远端列表",
    )


@router.post("/check-update", response_model=CheckUpdateResponse)
async def check_update(
    _user: CurrentUser,
    payload: UpdateRequest | None = Body(default=None),
) -> CheckUpdateResponse:
    """仅检查远程更新，不拉取代码。"""
    # _detect_runtime_mode 内含对 updater 的同步探活（urlopen 0.8s）与文件系统探测，
    # 放到线程池执行，避免阻塞事件循环（尤其 update-jobs 每 2s 轮询时）。
    runtime_mode, updater, root = await asyncio.to_thread(_detect_runtime_mode)
    can_apply = runtime_mode in {RUNTIME_LOCAL_SOURCE, RUNTIME_PROD_CONTAINER_WITH_UPDATER}
    manual_command = _manual_command_for_runtime(runtime_mode, updater)
    remote, branch, _force_full = await _resolve_update_request(payload)

    try:
        if (
            runtime_mode == RUNTIME_PROD_CONTAINER_WITH_UPDATER
            and updater
            and updater.startswith(("http://", "https://"))
        ):
            plan = await asyncio.to_thread(
                _updater_request,
                "/check",
                {"remote": remote, "branch": branch},
                timeout=120,
            )
            return _check_response_from_plan(
                runtime_mode=runtime_mode,
                updater=updater,
                remote=remote,
                branch=branch,
                plan=plan,
                can_apply=can_apply,
                manual_command=manual_command,
            )

        if runtime_mode == RUNTIME_LOCAL_SOURCE and root is None:
            return CheckUpdateResponse(
                remote=remote,
                branch=branch,
                runtime_mode=runtime_mode,
                update_executor=updater,
                can_apply=can_apply,
                manual_command=manual_command,
                plan_label="环境检测失败",
                plan_detail=GIT_WORKTREE_UNAVAILABLE_MESSAGE,
                error=GIT_WORKTREE_UNAVAILABLE_MESSAGE,
            )

        if root:
            remote_ref = f"refs/remotes/{remote}/{branch}"
            fetch_out, fetch_err, fetch_rc = await asyncio.to_thread(
                _run_git, "fetch", remote, f"+{branch}:{remote_ref}", timeout=30
            )
            if fetch_rc != 0:
                return CheckUpdateResponse(
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    can_apply=can_apply,
                    manual_command=manual_command,
                    plan_label="更新检查失败",
                    plan_detail="执行 git fetch 失败，请先排查仓库网络与权限。",
                    error=f"git fetch 失败: {fetch_err or fetch_out}",
                )

            head_out, _, head_rc = await asyncio.to_thread(_run_git, "rev-parse", "HEAD", timeout=10)
            if head_rc != 0:
                return CheckUpdateResponse(
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    can_apply=can_apply,
                    manual_command=manual_command,
                    plan_label="更新检查失败",
                    plan_detail="无法读取当前代码版本。",
                    error="无法获取当前 commit",
                )

            remote_out, _, remote_rc = await asyncio.to_thread(_run_git, "rev-parse", remote_ref, timeout=10)
            if remote_rc != 0:
                return CheckUpdateResponse(
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    can_apply=can_apply,
                    manual_command=manual_command,
                    plan_label="更新检查失败",
                    plan_detail=f"无法读取远程版本（{remote}/{branch}）。",
                    error=f"无法获取远程 commit（{remote}/{branch}）",
                )

            has_update = head_out != remote_out
            behind_out, _, behind_rc = await asyncio.to_thread(
                _run_git, "rev-list", "--count", f"{head_out}..{remote_out}", timeout=10
            )
            behind = int(behind_out) if not behind_rc else 0
            update_plan = await asyncio.to_thread(build_update_plan, root, head_out, remote_out)
            changed_files = update_plan.changed_files[:80]
            titles_out, _, titles_rc = await asyncio.to_thread(
                _run_git,
                "log",
                "--format=%<(240,trunc)%s",
                "--no-merges",
                "-n",
                "30",
                f"{head_out}..{remote_out}",
                timeout=10,
            )
            commit_titles = (
                [line.strip()[:240] for line in titles_out.splitlines() if line.strip()][:30]
                if titles_rc == 0
                else []
            )
            components = update_plan.components
            services = update_plan.services
            file_sync_services = update_plan.file_sync_services
            rebuild_services = update_plan.rebuild_services
            requires_full_update = update_plan.requires_full_update
            requires_backup = update_plan.requires_backup
            has_update = has_update and behind > 0
            if has_update and requires_full_update and runtime_mode == RUNTIME_LOCAL_SOURCE:
                can_apply = False
                manual_command = (
                    f"cd {shlex.quote(str(root))} && "
                    f"git pull --ff-only {shlex.quote(remote)} {shlex.quote(branch)} && make install && make restart"
                )
            action_required = _action_required_for_plan(
                runtime_mode, has_update, components, requires_full_update
            )
            plan_label, plan_detail = _plan_text(
                runtime_mode=runtime_mode,
                has_update=has_update,
                components=components,
                services=services,
                file_sync_services=file_sync_services,
                rebuild_services=rebuild_services,
                requires_full_update=requires_full_update,
                requires_backup=requires_backup,
                requires_migration=update_plan.requires_migration,
                can_apply=can_apply,
            )
            return CheckUpdateResponse(
                has_update=has_update,
                current_version=_version_at_git_ref(head_out) or __version__,
                target_version=_version_at_git_ref(remote_out),
                current_commit=head_out[:12],
                remote_commit=remote_out[:12],
                ahead=behind,
                remote=remote,
                branch=branch,
                changed_files=changed_files,
                commit_titles=commit_titles,
                runtime_mode=runtime_mode,
                update_executor=updater,
                action_required=action_required,
                plan_label=plan_label,
                plan_detail=plan_detail,
                components=components,
                services=services,
                file_sync_services=file_sync_services,
                rebuild_services=rebuild_services,
                requires_full_update=requires_full_update,
                requires_backup=requires_backup,
                requires_migration=update_plan.requires_migration,
                can_apply=can_apply,
                manual_command=manual_command,
            )

        # 走到这里说明：应用容器内没有 .git 工作树，也没有可查询的 http updater，
        # 无法在本进程内真正比对远程差异。诚实返回"无法自动检查"，不再无条件谎报有更新。
        if runtime_mode == RUNTIME_UNSUPPORTED:
            return CheckUpdateResponse(
                has_update=False,
                can_check=False,
                remote=remote,
                branch=branch,
                runtime_mode=runtime_mode,
                update_executor=updater,
                action_required="unsupported",
                plan_label="环境不支持自动更新检查",
                plan_detail="当前运行环境不支持自动更新检查，请人工执行部署流程。",
                components=["none"],
                requires_full_update=False,
                requires_backup=False,
                can_apply=can_apply,
                manual_command=manual_command,
                error="当前环境不支持自动更新检查，请人工执行部署流程。",
            )
        return CheckUpdateResponse(
            has_update=False,
            can_check=False,
            remote=remote,
            branch=branch,
            runtime_mode=runtime_mode,
            update_executor=updater,
            action_required="manual",
            plan_label="容器内无法自动检查更新",
            plan_detail="当前运行于容器且无法在容器内比对 Git 远程差异；请在服务器部署目录执行下方更新命令。",
            components=["none"],
            requires_full_update=False,
            requires_backup=False,
            can_apply=can_apply,
            manual_command=manual_command,
            error=None,
        )
    except Exception as e:  # noqa: BLE001
        return CheckUpdateResponse(
            remote=remote,
            branch=branch,
            runtime_mode=runtime_mode,
            update_executor=updater,
            can_apply=can_apply,
            manual_command=manual_command,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


@router.post("/pull-update", response_model=PullUpdateResponse)
async def pull_update(
    _user: CurrentUser,
    payload: UpdateRequest | None = Body(default=None),
) -> PullUpdateResponse:
    """执行应用更新（保留历史路由名 /pull-update）。"""
    runtime_mode, updater, _root = await asyncio.to_thread(_detect_runtime_mode)
    manual_command = _manual_command_for_runtime(runtime_mode, updater)
    can_apply = runtime_mode in {RUNTIME_LOCAL_SOURCE, RUNTIME_PROD_CONTAINER_WITH_UPDATER}
    remote, branch, force_full = await _resolve_update_request(payload)

    try:
        if (
            runtime_mode == RUNTIME_PROD_CONTAINER_WITH_UPDATER
            and updater
            and updater.startswith(("http://", "https://"))
        ):
            result = await asyncio.to_thread(
                _updater_request,
                "/jobs",
                {"remote": remote, "branch": branch, "full": force_full},
                timeout=10,
            )
            if not result.get("ok"):
                return PullUpdateResponse(
                    success=False,
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    action_required="full_update",
                    can_apply=True,
                    manual_command=manual_command,
                    plan_label="更新任务启动失败",
                    plan_detail="内部 updater 未能创建更新任务。",
                    error=str(result.get("error") or "updater job create failed"),
                )
            return PullUpdateResponse(
                success=True,
                job_id=str(result.get("job_id") or ""),
                status=str(result.get("status") or "queued"),
                remote=remote,
                branch=branch,
                runtime_mode=runtime_mode,
                update_executor=updater,
                action_required="restart",
                can_apply=True,
                plan_label="更新任务已启动",
                plan_detail="更新将在内部 updater 中执行，期间服务可能短暂重启；请观察任务日志。",
                summary=f"job_id={result.get('job_id')}",
                manual_command=manual_command,
            )

        if runtime_mode == RUNTIME_LOCAL_SOURCE:
            if _git_root() is None:
                return PullUpdateResponse(
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    can_apply=can_apply,
                    manual_command=manual_command,
                    plan_label="环境检测失败",
                    plan_detail=GIT_WORKTREE_UNAVAILABLE_MESSAGE,
                    error=GIT_WORKTREE_UNAVAILABLE_MESSAGE,
                )

            out, err, rc = await asyncio.to_thread(_run_git, "pull", "--ff-only", remote, branch, timeout=60)
            if rc != 0:
                return PullUpdateResponse(
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    can_apply=can_apply,
                    manual_command=manual_command,
                    plan_label="应用更新失败",
                    plan_detail="git pull 失败，请先处理冲突或网络问题。",
                    error=f"git pull 失败: {err or out}",
                )

            # 获取最新 commit
            head_out, _, _ = await asyncio.to_thread(_run_git, "rev-parse", "HEAD", timeout=10)
            # 获取简短 summary
            summary_out, _, _ = await asyncio.to_thread(_run_git, "log", "-1", "--oneline", timeout=10)
            root = _git_root()
            if root and (root / "Makefile").exists():
                subprocess.Popen(
                    ["make", "restart"],
                    cwd=str(root),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            return PullUpdateResponse(
                success=True,
                new_commit=head_out[:12] if head_out else None,
                summary=summary_out or None,
                remote=remote,
                branch=branch,
                runtime_mode=runtime_mode,
                update_executor=updater,
                action_required="none",
                plan_label="更新已应用",
                plan_detail="已执行 git pull --ff-only，并触发后台 make restart。",
                can_apply=True,
            )

        if runtime_mode == RUNTIME_PROD_CONTAINER_WITH_UPDATER and updater:
            result = await asyncio.to_thread(
                subprocess.run,
                [updater, "--full"] if force_full else [updater],
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, "TELEPILOT_UPDATE_REMOTE": remote, "TELEPILOT_UPDATE_BRANCH": branch},
            )
            merged = (result.stdout or "").strip() or (result.stderr or "").strip()
            if result.returncode != 0:
                return PullUpdateResponse(
                    remote=remote,
                    branch=branch,
                    runtime_mode=runtime_mode,
                    update_executor=updater,
                    action_required="full_update",
                    can_apply=True,
                    manual_command=manual_command,
                    plan_label="宿主机更新器执行失败",
                    plan_detail="请检查 updater 日志并在宿主机重试。",
                    summary=merged[:240] or None,
                    error=f"updater 失败，退出码 {result.returncode}",
                )
            return PullUpdateResponse(
                success=True,
                remote=remote,
                branch=branch,
                runtime_mode=runtime_mode,
                update_executor=updater,
                can_apply=True,
                plan_label="已触发宿主机更新器",
                plan_detail="更新器已执行，具体重启/部署结果请查看宿主机日志。",
                summary=merged[:240] or "updater executed",
                manual_command=manual_command,
            )

        if runtime_mode == RUNTIME_PROD_CONTAINER_MANUAL:
            return PullUpdateResponse(
                success=False,
                remote=remote,
                branch=branch,
                runtime_mode=runtime_mode,
                update_executor=updater,
                action_required="manual",
                can_apply=False,
                manual_command=manual_command,
                plan_label="容器内不可直接应用更新",
                plan_detail="当前容器没有可用更新器，请在宿主机执行完整更新流程。",
                error="当前容器内不支持自动更新",
            )

        return PullUpdateResponse(
            success=False,
            remote=remote,
            branch=branch,
            runtime_mode=runtime_mode,
            update_executor=updater,
            action_required="unsupported",
            can_apply=False,
            plan_label="环境不支持自动更新",
            plan_detail="请人工执行部署流程。",
            error="当前环境不支持自动更新",
        )
    except Exception as e:  # noqa: BLE001
        return PullUpdateResponse(
            remote=remote,
            branch=branch,
            runtime_mode=runtime_mode,
            update_executor=updater,
            can_apply=can_apply,
            manual_command=manual_command,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )


@router.get("/update-jobs/{job_id}", response_model=UpdateJobStatusResponse)
async def get_update_job(job_id: str, _user: CurrentUser) -> UpdateJobStatusResponse:
    """读取内部 updater 任务状态。"""
    runtime_mode, updater, _root = await asyncio.to_thread(_detect_runtime_mode)
    if (
        runtime_mode != RUNTIME_PROD_CONTAINER_WITH_UPDATER
        or not updater
        or not updater.startswith(("http://", "https://"))
    ):
        return UpdateJobStatusResponse(
            ok=False, job_id=job_id, status="unsupported", error="内部 updater 不可用"
        )
    try:
        result = await asyncio.to_thread(_updater_get, f"/jobs/{job_id}", timeout=10)
    except Exception as exc:  # noqa: BLE001
        return UpdateJobStatusResponse(
            ok=False, job_id=job_id, status="unknown", error=f"{type(exc).__name__}: {exc}"
        )
    if not result.get("ok"):
        return UpdateJobStatusResponse(
            ok=False,
            job_id=job_id,
            status=str(result.get("status") or "unknown"),
            error=str(result.get("error") or "读取更新任务失败"),
        )
    return UpdateJobStatusResponse(
        ok=True,
        job_id=str(result.get("job_id") or job_id),
        status=str(result.get("status") or "unknown"),
        created_at=_int_or_none(result.get("created_at")),
        started_at=_int_or_none(result.get("started_at")),
        finished_at=_int_or_none(result.get("finished_at")),
        returncode=_int_or_none(result.get("returncode")),
        remote=str(result.get("remote") or "") or None,
        branch=str(result.get("branch") or "") or None,
        new_commit=str(result.get("new_commit") or "") or None,
        summary=str(result.get("summary") or "") or None,
        error=str(result.get("error") or "") or None,
        progress=_int_or_none(result.get("progress")),
        phase=str(result.get("phase") or "") or None,
        detail=str(result.get("detail") or "") or None,
        logs=[str(line) for line in result.get("logs") or []],
        plan=result.get("plan") if isinstance(result.get("plan"), dict) else None,
        step_timings=[
            item
            for item in (result.get("step_timings") or [])
            if isinstance(item, dict)
        ],
    )


@router.post("/restart", response_model=RestartResponse)
async def restart_app(_user: CurrentUser) -> RestartResponse:
    """触发应用重启。使用 subprocess detach 避免阻塞当前进程。"""
    try:
        runtime_mode, updater, root = await asyncio.to_thread(_detect_runtime_mode)
        if runtime_mode == RUNTIME_LOCAL_SOURCE and root and (root / "Makefile").exists():
            subprocess.Popen(
                ["make", "restart"],
                cwd=str(root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return RestartResponse(success=True)

        if runtime_mode == RUNTIME_PROD_CONTAINER_WITH_UPDATER:
            command_hint = updater or "/app/host-updater/prod-update"
            return RestartResponse(error=f"容器内不执行 docker compose，请改用更新器：{command_hint}")
        if runtime_mode == RUNTIME_PROD_CONTAINER_MANUAL:
            return RestartResponse(
                error="容器内不执行 docker compose，请在宿主机部署目录手工执行更新与重启。"
            )
        return RestartResponse(error="当前环境不支持自动重启，请人工执行部署流程。")
    except Exception as e:  # noqa: BLE001
        return RestartResponse(error=f"{type(e).__name__}: {str(e)[:200]}")


# ════════════════════════════════════════════════════════════
# 配置备份与导出 / 导入
# ════════════════════════════════════════════════════════════

# 配置包格式版本。V1 是 0.56.7 及更早的无版本 JSON；V2 增加依赖拓扑和 ID 映射。
_CONFIG_BUNDLE_FORMAT = "telepilot-config"
_CONFIG_BUNDLE_VERSION = 2

# 每个类别定义：ORM 模型、敏感字段、自然标识、ID 映射与依赖关系。
_EXPORT_DEFS: dict[str, dict[str, Any]] = {
    "system_settings": {
        "model": "SystemSetting",
        "exclude_fields": set(),
        "id_fields": ("key",),
        "pk_field": "key",
    },
    "proxies": {
        "model": "Proxy",
        "exclude_fields": {"password_enc"},
        "id_fields": ("type", "host", "port", "username"),
        "nullable_identity_fields": {"username"},
        "pk_field": "id",
    },
    "device_profiles": {
        "model": "DeviceProfile",
        "exclude_fields": set(),
        "id_fields": ("name",),
        "pk_field": "id",
    },
    "feature_registry": {
        "model": "Feature",
        "exclude_fields": set(),
        "id_fields": ("key",),
        "pk_field": "key",
    },
    "plugin_global_configs": {
        "model": "PluginGlobalConfig",
        "exclude_fields": set(),
        "id_fields": ("plugin_key",),
        "dependencies": ("feature_registry",),
        "references": {"plugin_key": "feature_registry"},
    },
    "command_templates": {
        "model": "CommandTemplate",
        "exclude_fields": set(),
        "id_fields": ("name",),
        "pk_field": "id",
        "dependencies": ("llm_providers",),
    },
    "account_commands": {
        "model": "AccountCommandLink",
        "exclude_fields": set(),
        "id_fields": ("account_id", "template_id"),
        "dependencies": ("account_settings", "command_templates"),
        "references": {
            "account_id": "account_settings",
            "template_id": "command_templates",
        },
    },
    "llm_providers": {
        "model": "LLMProvider",
        "exclude_fields": {"api_key_enc", "request_headers_enc"},
        "id_fields": ("name",),
        "pk_field": "id",
        "dependencies": ("proxies",),
        "references": {"proxy_id": "proxies"},
    },
    "forward_rules": {
        "model": "Rule",
        "exclude_fields": set(),
        "filter": {"feature_key": "forward"},
        "id_fields": ("account_id", "feature_key", "name"),
        "pk_field": "id",
        "map_group": "rules",
        "dependencies": ("account_settings",),
        "references": {"account_id": "account_settings"},
    },
    "auto_reply_rules": {
        "model": "Rule",
        "exclude_fields": set(),
        "filter": {"feature_key": "auto_reply"},
        "id_fields": ("account_id", "feature_key", "name"),
        "pk_field": "id",
        "map_group": "rules",
        "dependencies": ("account_settings",),
        "references": {"account_id": "account_settings"},
    },
    "rate_limit_templates": {
        "model": "RateLimitTemplate",
        "exclude_fields": set(),
        "id_fields": ("name",),
        "pk_field": "id",
    },
    "rate_limit_rules": {
        "model": "RateLimitRule",
        "exclude_fields": set(),
        "id_fields": ("scope", "scope_id", "action"),
        "pk_field": "id",
        "dependencies": (
            "rate_limit_templates",
            "account_settings",
            "forward_rules",
            "auto_reply_rules",
        ),
    },
    "feature_config": {
        "model": "AccountFeature",
        "exclude_fields": set(),
        "id_fields": ("account_id", "feature_key"),
        "dependencies": ("account_settings", "feature_registry"),
        "references": {
            "account_id": "account_settings",
            "feature_key": "feature_registry",
        },
    },
    "account_settings": {
        "model": "Account",
        "exclude_fields": {"session_enc", "api_id_enc", "api_hash_enc", "phone"},
        "id_fields": ("phone",),
        "identity_candidates": (("phone",), ("tg_user_id",)),
        "pk_field": "id",
        "dependencies": ("rate_limit_templates", "proxies", "device_profiles"),
        "references": {
            "template_id": "rate_limit_templates",
            "proxy_id": "proxies",
            "device_profile_id": "device_profiles",
        },
    },
    "humanize_configs": {
        "model": "HumanizeConfig",
        "exclude_fields": set(),
        "id_fields": ("account_id",),
        "dependencies": ("account_settings",),
        "references": {"account_id": "account_settings"},
    },
    "ignored_peers": {
        "model": "IgnoredPeer",
        "exclude_fields": set(),
        "id_fields": ("account_id", "peer_id"),
        "pk_field": "id",
        "dependencies": ("account_settings",),
        "references": {"account_id": "account_settings"},
    },
    "notify_bots": {
        "model": "NotifyBot",
        "exclude_fields": {"bot_token_enc"},
        "id_fields": ("name",),
        "pk_field": "id",
    },
}

_IMPORT_TOPOLOGY = (
    "system_settings",
    "proxies",
    "device_profiles",
    "feature_registry",
    "plugin_global_configs",
    "rate_limit_templates",
    "account_settings",
    "humanize_configs",
    "llm_providers",
    "command_templates",
    "forward_rules",
    "auto_reply_rules",
    "feature_config",
    "ignored_peers",
    "notify_bots",
    "account_commands",
    "rate_limit_rules",
)

# 敏感字段的完整集合（include_sensitive=true 时不排除）
_ALL_SENSITIVE = {
    "session_enc",
    "api_key_enc",
    "request_headers_enc",
    "api_id_enc",
    "api_hash_enc",
    "phone",
    "bot_token_enc",
    "password_enc",
}
_SENSITIVE_SYSTEM_SETTING_KEYS = {"auth_login_recovery_code"}
_SENSITIVE_SYSTEM_SETTING_PREFIXES = (
    "account_webhooks:",
    "account_bot_transfer_notice:",
    "account_bot:interaction_runtime_state:",
    "account_bot:transfer_test_runtime_state:",
)


class ExportConfigRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)
    include_sensitive: bool = False
    password: str | None = None
    totp_code: str | None = None


def _row_to_dict(row: Any, exclude: set[str], include_sensitive: bool) -> dict[str, Any]:
    """将 ORM 行转为 dict，排除指定字段。"""
    data = {}
    for col in row.__table__.columns:
        name = col.name
        if include_sensitive or name not in exclude:
            val = getattr(row, name)
            # 处理不可序列化的类型
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            elif isinstance(val, (bytes, bytearray)):
                val = val.hex() if include_sensitive else None
            data[name] = val
    return data


def _system_setting_safe_for_export(key: Any) -> bool:
    normalized = str(key or "").strip()
    if normalized in _SENSITIVE_SYSTEM_SETTING_KEYS:
        return False
    return not any(normalized.startswith(prefix) for prefix in _SENSITIVE_SYSTEM_SETTING_PREFIXES)


def _config_model_map() -> dict[str, Any]:
    from ..db.models import (
        Account,
        AccountCommandLink,
        AccountFeature,
        CommandTemplate,
        Feature,
        HumanizeConfig,
        IgnoredPeer,
        LLMProvider,
        NotifyBot,
        PluginGlobalConfig,
        Proxy,
        RateLimitRule,
        RateLimitTemplate,
        Rule,
    )
    from ..db.models.account import DeviceProfile
    from ..db.models.system import SystemSetting

    return {
        "SystemSetting": SystemSetting,
        "Proxy": Proxy,
        "DeviceProfile": DeviceProfile,
        "Feature": Feature,
        "PluginGlobalConfig": PluginGlobalConfig,
        "CommandTemplate": CommandTemplate,
        "AccountCommandLink": AccountCommandLink,
        "LLMProvider": LLMProvider,
        "Rule": Rule,
        "RateLimitTemplate": RateLimitTemplate,
        "RateLimitRule": RateLimitRule,
        "AccountFeature": AccountFeature,
        "Account": Account,
        "HumanizeConfig": HumanizeConfig,
        "IgnoredPeer": IgnoredPeer,
        "NotifyBot": NotifyBot,
    }


def _expand_export_categories(categories: list[str]) -> list[str]:
    wanted: set[str] = set()

    def include(category: str) -> None:
        if category in wanted or category not in _EXPORT_DEFS:
            return
        for dependency in _EXPORT_DEFS[category].get("dependencies", ()):
            include(dependency)
        wanted.add(category)

    for category in categories:
        include(category)
    return [category for category in _IMPORT_TOPOLOGY if category in wanted]


async def _build_export_payload(
    *,
    categories: list[str],
    include_sensitive: bool,
    session_factory: Callable[[], Any] | None = None,
    model_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .. import __version__

    selected = [category for category in categories if category in _EXPORT_DEFS]
    included = _expand_export_categories(selected)
    models = model_map or _config_model_map()
    factory = session_factory or AsyncSessionLocal
    result: dict[str, Any] = {
        "_meta": {
            "format": _CONFIG_BUNDLE_FORMAT,
            "bundle_version": _CONFIG_BUNDLE_VERSION,
            "app_version": __version__,
            # 保留旧字段，方便旧版 UI 展示来源版本。
            "version": __version__,
            "exported_at": datetime.now().isoformat(),
            "include_sensitive": include_sensitive,
            "requested_categories": selected,
            "included_categories": included,
            "dependency_order": list(_IMPORT_TOPOLOGY),
        },
    }
    async with factory() as db:
        for category in included:
            definition = _EXPORT_DEFS[category]
            model_cls = models.get(definition["model"])
            if model_cls is None:
                continue
            query = select(model_cls)
            for key, value in (definition.get("filter") or {}).items():
                query = query.where(getattr(model_cls, key) == value)
            rows = (await db.execute(query)).scalars().all()
            if model_cls.__name__ == "SystemSetting" and not include_sensitive:
                rows = [row for row in rows if _system_setting_safe_for_export(getattr(row, "key", ""))]
            exclude = set() if include_sensitive else definition["exclude_fields"]
            result[category] = [_row_to_dict(row, exclude, include_sensitive) for row in rows]
    return result


@router.post("/export-config")
async def export_config(
    user: CurrentUser,
    body: ExportConfigRequest,
) -> JSONResponse:
    """导出配置为 JSON 文件下载。"""
    if body.include_sensitive:
        if not body.password or not auth_service.verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=403, detail="敏感配置导出需要重新验证当前密码")
        if user.totp_secret_enc:
            if not body.totp_code or not auth_service.verify_totp(
                decrypt_str(user.totp_secret_enc),
                body.totp_code,
            ):
                raise HTTPException(status_code=403, detail="敏感配置导出需要有效的 TOTP 动态验证码")
    try:
        result = await _build_export_payload(
            categories=body.categories,
            include_sensitive=body.include_sensitive,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="导出配置失败，未生成不完整备份") from exc

    filename = f"telepilot-config-{datetime.now().strftime('%Y-%m-%d')}.json"
    return JSONResponse(
        content=result,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ImportConfigResponse(BaseModel):
    imported: int = 0
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)
    bundle_version: int = _CONFIG_BUNDLE_VERSION
    affected_categories: list[str] = Field(default_factory=list)
    affected_accounts: list[int] = Field(default_factory=list)
    reloaded_accounts: list[int] = Field(default_factory=list)
    restart_required: bool = False
    id_mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ConfigImportError(RuntimeError):
    def __init__(self, message: str, *, imported: int = 0) -> None:
        super().__init__(message)
        self.imported = imported


def _coerce_import_value(column: Any, value: Any) -> Any:
    if value is None:
        return None
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return value
    if python_type is bytes and isinstance(value, str):
        return bytes.fromhex(value)
    if python_type is datetime and isinstance(value, str):
        return datetime.fromisoformat(value)
    if python_type is date and isinstance(value, str):
        return date.fromisoformat(value)
    if python_type is datetime_time and isinstance(value, str):
        return datetime_time.fromisoformat(value)
    return value


def _mapping_key(value: Any) -> str:
    return str(value)


def _lookup_mapping(
    mappings: dict[str, dict[str, Any]],
    category: str,
    source_value: Any,
) -> Any:
    mapped = mappings.get(category, {}).get(_mapping_key(source_value))
    if mapped is None:
        raise ConfigImportError(f"缺少依赖映射：{category} 的旧 ID {source_value}；请使用完整的 V2 配置包")
    return mapped


def _remap_command_config(
    config: Any,
    mappings: dict[str, dict[str, Any]],
) -> Any:
    if not isinstance(config, dict):
        return config
    remapped = dict(config)
    provider_id = remapped.get("provider_id")
    if provider_id is not None:
        remapped["provider_id"] = _lookup_mapping(mappings, "llm_providers", provider_id)
    fallback_ids = remapped.get("fallback_provider_ids")
    if isinstance(fallback_ids, list):
        remapped["fallback_provider_ids"] = [
            _lookup_mapping(mappings, "llm_providers", value) for value in fallback_ids
        ]
    return remapped


def _remap_import_row(
    category: str,
    row: dict[str, Any],
    mappings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    definition = _EXPORT_DEFS[category]
    remapped = dict(row)
    for field, dependency in definition.get("references", {}).items():
        value = remapped.get(field)
        if value is not None:
            remapped[field] = _lookup_mapping(mappings, dependency, value)
    if category == "rate_limit_rules":
        scope = str(remapped.get("scope") or "")
        dependency = {
            "template": "rate_limit_templates",
            "account": "account_settings",
            "rule": "rules",
        }.get(scope)
        if dependency and remapped.get("scope_id") is not None:
            remapped["scope_id"] = _lookup_mapping(mappings, dependency, remapped["scope_id"])
    if category == "command_templates" and "config" in remapped:
        remapped["config"] = _remap_command_config(remapped["config"], mappings)
    return remapped


async def _import_config_payload(
    payload: dict[str, Any],
    *,
    session_factory: Callable[[], Any] | None = None,
    model_map: dict[str, Any] | None = None,
) -> ImportConfigResponse:
    if not isinstance(payload, dict):
        raise ConfigImportError("配置包根节点必须是 JSON object")
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    bundle_format = meta.get("format")
    if bundle_format not in (None, _CONFIG_BUNDLE_FORMAT):
        raise ConfigImportError(f"不支持的配置包格式：{bundle_format}")
    raw_version = meta.get("bundle_version", 1)
    try:
        bundle_version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ConfigImportError("配置包版本不是整数") from exc
    if bundle_version < 1 or bundle_version > _CONFIG_BUNDLE_VERSION:
        raise ConfigImportError(f"不支持的配置包版本：{bundle_version}")
    if bundle_version >= 2 and bundle_format != _CONFIG_BUNDLE_FORMAT:
        raise ConfigImportError("V2 配置包缺少合法的 format 标识")
    payload_categories = {key for key in payload if key != "_meta"}
    unknown_categories = sorted(payload_categories - set(_EXPORT_DEFS))
    if unknown_categories:
        raise ConfigImportError(f"配置包包含未知类别：{', '.join(unknown_categories)}")
    if not payload_categories:
        raise ConfigImportError("配置包不包含任何可恢复类别")
    declared_categories = meta.get("included_categories")
    if isinstance(declared_categories, list):
        missing_categories = sorted({str(value) for value in declared_categories} - payload_categories)
        if missing_categories:
            raise ConfigImportError(f"配置包内容不完整，缺少声明类别：{', '.join(missing_categories)}")

    models = model_map or _config_model_map()
    factory = session_factory or AsyncSessionLocal
    mappings: dict[str, dict[str, Any]] = {}
    imported = 0
    skipped = 0
    warnings: list[str] = []
    affected_categories: set[str] = set()
    affected_accounts: set[int] = set()
    db = None

    try:
        async with factory() as db:
            for category in _IMPORT_TOPOLOGY:
                rows = payload.get(category)
                if rows is None:
                    continue
                if not isinstance(rows, list):
                    raise ConfigImportError(f"[{category}] 必须是数组")
                definition = _EXPORT_DEFS[category]
                model_cls = models.get(definition["model"])
                if model_cls is None:
                    raise ConfigImportError(f"[{category}] 当前运行版本缺少对应模型")
                table_columns = {column.name: column for column in model_cls.__table__.columns}
                category_map = mappings.setdefault(category, {})
                map_group = definition.get("map_group")
                group_map = mappings.setdefault(map_group, {}) if map_group else None

                for source in rows:
                    if not isinstance(source, dict):
                        raise ConfigImportError(f"[{category}] 行必须是 object")
                    source_pk = source.get(definition.get("pk_field", ""))
                    remapped = _remap_import_row(category, source, mappings)
                    filtered: dict[str, Any] = {}
                    exclude = set() if bool(meta.get("include_sensitive")) else definition["exclude_fields"]
                    for key, value in remapped.items():
                        if key not in table_columns or key in exclude:
                            continue
                        if key in {"created_at", "updated_at", "added_at", "installed_at"}:
                            continue
                        if key == definition.get("pk_field") and key == "id":
                            continue
                        filtered[key] = _coerce_import_value(table_columns[key], value)

                    if category == "system_settings" and filtered.get("key") == "app_update_target":
                        try:
                            filtered["value"] = normalize_update_target(filtered.get("value"))
                        except ValueError as exc:
                            raise ConfigImportError(
                                f"[system_settings] app_update_target 非法：{exc}"
                            ) from exc

                    # 导入旧 / 跨版本备份时，未知的客户端身份档案降级为 auto（不拒绝整份备份）。
                    if category == "llm_providers" and "client_identity_profile" in filtered:
                        from ..db.models.command import (
                            normalize_client_identity_profile,
                        )

                        original_identity = filtered["client_identity_profile"]
                        normalized_identity = normalize_client_identity_profile(original_identity)
                        if normalized_identity != original_identity:
                            warnings.append(
                                f"[llm_providers] 客户端身份档案 {original_identity!r} 未知，已降级为 auto"
                            )
                        filtered["client_identity_profile"] = normalized_identity

                    identity: dict[str, Any] | None = None
                    candidates = definition.get("identity_candidates", (definition["id_fields"],))
                    nullable_identity_fields = definition.get("nullable_identity_fields", set())
                    for candidate in candidates:
                        if all(
                            field in filtered
                            and (filtered[field] is not None or field in nullable_identity_fields)
                            for field in candidate
                        ):
                            identity = {field: filtered[field] for field in candidate}
                            break
                    if identity is None:
                        raise ConfigImportError(
                            f"[{category}] 缺少稳定标识字段：{', '.join(definition['id_fields'])}"
                        )
                    query = select(model_cls)
                    for field, value in identity.items():
                        query = query.where(getattr(model_cls, field) == value)
                    existing = (await db.execute(query.limit(1))).scalar_one_or_none()
                    if existing is not None:
                        skipped += 1
                        target_pk_field = definition.get("pk_field")
                        if source_pk is not None and target_pk_field:
                            target_pk = getattr(existing, target_pk_field)
                            category_map[_mapping_key(source_pk)] = target_pk
                            if group_map is not None:
                                group_map[_mapping_key(source_pk)] = target_pk
                        if hasattr(existing, "account_id"):
                            affected_accounts.add(int(existing.account_id))
                        continue

                    new_row = model_cls(**filtered)
                    db.add(new_row)
                    await db.flush()
                    imported += 1
                    affected_categories.add(category)
                    target_pk_field = definition.get("pk_field")
                    if source_pk is not None and target_pk_field:
                        target_pk = getattr(new_row, target_pk_field)
                        category_map[_mapping_key(source_pk)] = target_pk
                        if group_map is not None:
                            group_map[_mapping_key(source_pk)] = target_pk
                    if category == "account_settings":
                        affected_accounts.add(int(new_row.id))
                    elif hasattr(new_row, "account_id"):
                        affected_accounts.add(int(new_row.account_id))

            runtime_categories = {
                "system_settings",
                "llm_providers",
                "command_templates",
                "plugin_global_configs",
                "rate_limit_templates",
                "rate_limit_rules",
            }
            if affected_categories & runtime_categories and "Account" in models:
                account_ids = (await db.execute(select(models["Account"].id))).scalars().all()
                affected_accounts.update(int(account_id) for account_id in account_ids)
            await db.commit()
    except ConfigImportError:
        raise
    except Exception as exc:  # noqa: BLE001
        if db is not None:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
        raise ConfigImportError(f"配置导入事务失败：{type(exc).__name__}: {exc}", imported=0) from exc

    public_mappings = {
        category: values for category, values in mappings.items() if category in _EXPORT_DEFS and values
    }
    return ImportConfigResponse(
        imported=imported,
        skipped=skipped,
        warnings=warnings,
        bundle_version=bundle_version,
        affected_categories=[category for category in _IMPORT_TOPOLOGY if category in affected_categories],
        affected_accounts=sorted(affected_accounts),
        id_mappings=public_mappings,
    )


async def _reload_imported_runtime(account_ids: list[int]) -> tuple[list[int], bool]:
    if not account_ids:
        return [], False
    from ..worker.ipc import (
        CMD_RELOAD_COMMANDS,
        CMD_RELOAD_CONFIG,
        CMD_RELOAD_IGNORED,
        publish_cmd_with_ack,
    )

    try:
        redis = get_redis()
        semaphore = asyncio.Semaphore(4)

        async def reload_one(account_id: int) -> tuple[int, bool]:
            confirmed = True
            async with semaphore:
                for command in (
                    CMD_RELOAD_CONFIG,
                    CMD_RELOAD_COMMANDS,
                    CMD_RELOAD_IGNORED,
                ):
                    confirmed = bool(await publish_cmd_with_ack(redis, account_id, command)) and confirmed
            return account_id, confirmed

        results = await asyncio.gather(*(reload_one(value) for value in account_ids))
    except Exception:  # noqa: BLE001
        return [], True
    reloaded = [account_id for account_id, confirmed in results if confirmed]
    restart_required = any(not confirmed for _, confirmed in results)
    return reloaded, restart_required


@router.post("/import-config", response_model=ImportConfigResponse)
async def import_config(
    _user: CurrentUser,
    file: UploadFile = File(...),
) -> ImportConfigResponse:
    """从上传的 JSON 文件导入配置。冲突策略：同名/同 ID 跳过并记录。"""
    import json as _json

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="配置包超过 10MB")
    try:
        data = _json.loads(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="上传的文件不是合法的 JSON") from exc

    try:
        outcome = await _import_config_payload(data)
    except ConfigImportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    reloaded, restart_required = await _reload_imported_runtime(outcome.affected_accounts)
    return outcome.model_copy(
        update={
            "reloaded_accounts": reloaded,
            "restart_required": restart_required,
            "warnings": outcome.warnings
            + (["部分 Worker 未确认配置热重载，请重启应用后再继续操作"] if restart_required else []),
        }
    )


__all__ = ["router"]
