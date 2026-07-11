"""主进程内：账号 worker 子进程的拉起 / 监控 / 重启 / 停止。

被 main.py 的 lifespan 调用：
  - startup:  ``await start_supervisor()``
  - shutdown: ``await stop_all_workers()``

接收 ``worker_global`` 上的 ``"start_worker"`` 指令（A Agent 在登录完成时发）。
进程崩溃 → 指数退避重启（5/10/20/60/300s），连续 5 次失败置 ``status='dead'``。

主进程同时在这里**消费** Redis stream（runtime_log / ratelimit_event）落库。
（也可以拆到独立模块；此处合并，便于一个 supervisor 启动协程管全部。）

⚠ 子进程通过 ``_MP_CTX.Process`` 拉起，``_MP_CTX`` 固定为 spawn context（不是
默认的 mp.Process / Linux fork），见模块内 ``_MP_CTX`` 注释解释为什么不能 fork。

⚠ 关停保护：注册 ``atexit`` 与 ``SIGTERM/SIGINT`` 处理，确保即使 lifespan 来不及
跑（被 ``kill -9 uvicorn`` 等暴力中断），也尽量先把所有 worker 子进程 terminate
掉，避免遗留同 session 的孤儿 worker（多实例 worker 会争抢同一个 TG client 事件）。

⚠ 启动孤儿清理：``kill -9 uvicorn`` 时 atexit / signal handler 都不会跑，子进程
就成了 PPID=1 的孤儿继续连着 TG。下次启动时多个 worker 抢同一个账号事件会写出
互相覆盖的回答（典型症状："新 provider 后第 1-2 次能找到，第 3 次又找不到"）。
所以 ``start_supervisor`` 现在会扫描 PID 文件目录，把上次启动留下的、命令行能
对上号的 worker 进程显式 SIGTERM 掉，再起新的。
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import multiprocessing as mp
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import delete, select, update

from ..db.base import AsyncSessionLocal
from ..db.models.account import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DEAD,
    Account,
)
from ..db.models.log import RuntimeLog
from ..db.models.rate_limit import RateLimitEvent
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..services.event_trace import cleanup_event_traces
from ..services.redactor import redact_text, redact_value
from .entry import worker_entry
from .ipc import (
    CMD_PAUSE,
    CMD_RESUME,
    CMD_STOP,
    GCMD_KILL_SWITCH,
    GLOBAL_CHANNEL,
    RATELIMIT_EVENT_STREAM,
    RUNTIME_LOG_STREAM,
    IPCMessage,
    cmd_channel,
    make_cmd,
)

log = logging.getLogger(__name__)

_LOG_RETENTION_CACHE: tuple[float, dict[str, int]] = (0.0, {})
_LAST_RUNTIME_LOG_CLEANUP_AT = 0.0
_RUNTIME_LOG_CLEANUP_INTERVAL = 3600.0


def invalidate_log_retention_cache() -> None:
    """让新的运行日志保留/等级设置立即对后续日志落库生效。"""

    global _LOG_RETENTION_CACHE
    _LOG_RETENTION_CACHE = (0.0, {})


async def _get_log_retention_config() -> dict[str, int]:
    """读取运行日志保留设置；短缓存避免每条日志都查 system_setting。"""

    global _LOG_RETENTION_CACHE
    now = time.monotonic()
    cached_at, cached = _LOG_RETENTION_CACHE
    if cached and now - cached_at < 60:
        return cached
    defaults = {
        "runtime_log_retention_days": 30,
        "runtime_log_max_message_chars": 2000,
        "runtime_log_max_detail_chars": 8000,
        "runtime_log_min_level": "info",
        "trace_retention_days": 30,
        "trace_payload_snapshot_retention_days": 7,
        "native_raw_persist_enabled": False,
        "native_raw_retention_days": 1,
    }
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "log_retention")
        raw = row.value if row is not None and isinstance(row.value, dict) else {}
        cfg = {
            "runtime_log_retention_days": max(
                0,
                int(raw.get("runtime_log_retention_days", defaults["runtime_log_retention_days"]) or 0),
            ),
            "runtime_log_max_message_chars": max(
                200,
                int(
                    raw.get(
                        "runtime_log_max_message_chars",
                        defaults["runtime_log_max_message_chars"],
                    )
                    or defaults["runtime_log_max_message_chars"]
                ),
            ),
            "runtime_log_max_detail_chars": max(
                0,
                int(raw.get("runtime_log_max_detail_chars", defaults["runtime_log_max_detail_chars"]) or 0),
            ),
            "runtime_log_min_level": (
                str(raw.get("runtime_log_min_level", defaults["runtime_log_min_level"]) or "info").lower()
                if str(raw.get("runtime_log_min_level", defaults["runtime_log_min_level"]) or "info").lower()
                in {"debug", "info", "warn", "error"}
                else "info"
            ),
            "trace_retention_days": max(
                0,
                int(raw.get("trace_retention_days", defaults["trace_retention_days"]) or 0),
            ),
            "trace_payload_snapshot_retention_days": max(
                0,
                int(
                    raw.get(
                        "trace_payload_snapshot_retention_days",
                        defaults["trace_payload_snapshot_retention_days"],
                    )
                    or 0
                ),
            ),
            "native_raw_persist_enabled": bool(
                raw.get("native_raw_persist_enabled", defaults["native_raw_persist_enabled"])
            ),
            "native_raw_retention_days": max(
                0,
                int(raw.get("native_raw_retention_days", defaults["native_raw_retention_days"]) or 0),
            ),
        }
    except Exception:
        log.debug("读取 log_retention 失败，使用默认值", exc_info=True)
        cfg = defaults
    _LOG_RETENTION_CACHE = (now, cfg)
    return cfg


def _truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 40)] + f"...（已截断，原始长度 {len(value)} 字符）"


def _truncate_detail(value: object, max_chars: int) -> object:
    if max_chars <= 0 or value is None:
        return None
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        raw = str(value)
    if len(raw) <= max_chars:
        return value
    return {
        "_truncated": True,
        "preview": raw[: max(0, max_chars - 80)],
        "original_chars": len(raw),
    }


def _normalize_runtime_log_level(level: object) -> str:
    # 兼容插件传 logging 数值级别（10/20/30/40）或对应字符串数字。
    try:
        if isinstance(level, (int, float)) or (isinstance(level, str) and str(level).strip().isdigit()):
            iv = int(float(level))
            if iv >= 40:
                return "error"
            if iv >= 30:
                return "warn"
            if iv >= 20:
                return "info"
            return "debug"
    except Exception:
        pass

    raw = str(level or "info").strip().lower()
    if raw == "warning":
        return "warn"
    if raw in {"critical", "fatal"}:
        return "error"
    if raw in {"debug", "info", "warn", "error"}:
        return raw
    return "info"


def _runtime_log_level_allowed(level: str, min_level: str) -> bool:
    order = {"debug": 10, "info": 20, "warn": 30, "error": 40}
    return order.get(level, 20) >= order.get(min_level, 20)


async def _cleanup_runtime_logs_if_due() -> None:
    """按 log_retention 定期清理过期运行日志和 Trace；0 天表示不自动删除。"""

    global _LAST_RUNTIME_LOG_CLEANUP_AT
    now = time.monotonic()
    if now - _LAST_RUNTIME_LOG_CLEANUP_AT < _RUNTIME_LOG_CLEANUP_INTERVAL:
        return
    _LAST_RUNTIME_LOG_CLEANUP_AT = now
    cfg = await _get_log_retention_config()
    days = int(cfg.get("runtime_log_retention_days", 30) or 0)
    if days <= 0:
        return
    cutoff = datetime.now(UTC) - timedelta(days=days)
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(RuntimeLog).where(RuntimeLog.ts < cutoff))
            await db.commit()
    except Exception:
        log.debug("清理过期 runtime_log 失败", exc_info=True)
    try:
        await cleanup_event_traces(
            trace_retention_days=int(cfg.get("trace_retention_days", 30) or 0),
            payload_snapshot_retention_days=int(
                cfg.get("trace_payload_snapshot_retention_days", 7) or 0
            ),
            native_raw_retention_days=int(cfg.get("native_raw_retention_days", 1) or 0),
        )
    except Exception:
        log.debug("清理过期 event_trace 失败", exc_info=True)

# ⚠ 强制 spawn 启动方式（不要 fork）
#
# Linux 默认是 fork，会把父进程的 asyncio event loop / SQLAlchemy engine /
# Redis 连接池 / 已打开的 socket 一并继承到 worker 子进程，引发：
#   - 子进程复用父 loop / engine 上的 fd，运行时随机炸 IPC
#   - SQLAlchemy 异步引擎在 fork 后状态错乱
#   - Redis 连接被两个进程同时使用，命令乱序
#
# spawn 起的是干净的 Python 解释器，符合本项目「主进程 + worker 完全独立」的契约。
# 我们使用 ``get_context("spawn")`` 拿到一个独立 context 而不是
# ``set_start_method`` 全局改，避免和宿主（uvicorn / pytest 等）冲突。
_MP_CTX = mp.get_context("spawn")

# 指数退避重启间隔（秒），用尽后置账号为 dead
_BACKOFF = [5, 10, 20, 60, 300]
# worker 连续存活达到该窗口后，才把之前的崩溃次数视为已经恢复。
# 否则「启动后短暂存活」会把 fail_count 清零，形成无限快速重启。
_STABLE_WINDOW_SECONDS = 60.0
# runtime_log 通知 Telegram 的有界并发（避免崩溃刷屏时 task 洪水）。
_RUNTIME_LOG_NOTIFY_QUEUE: asyncio.Queue[Any] | None = None
_RUNTIME_LOG_NOTIFY_WORKERS: list[asyncio.Task[None]] = []
_RUNTIME_LOG_NOTIFY_WORKER_COUNT = 4
_RUNTIME_LOG_NOTIFY_QUEUE_MAX = 200
_RUNTIME_LOG_NOTIFY_DROPPED = 0


async def _ensure_runtime_log_notify_workers() -> asyncio.Queue[Any]:
    """有界队列 + 固定 worker，替代每条日志 create_task。"""

    global _RUNTIME_LOG_NOTIFY_QUEUE, _RUNTIME_LOG_NOTIFY_WORKERS
    if _RUNTIME_LOG_NOTIFY_QUEUE is None:
        _RUNTIME_LOG_NOTIFY_QUEUE = asyncio.Queue(maxsize=_RUNTIME_LOG_NOTIFY_QUEUE_MAX)
    alive = [t for t in _RUNTIME_LOG_NOTIFY_WORKERS if not t.done()]
    _RUNTIME_LOG_NOTIFY_WORKERS = alive
    while len(_RUNTIME_LOG_NOTIFY_WORKERS) < _RUNTIME_LOG_NOTIFY_WORKER_COUNT:
        _RUNTIME_LOG_NOTIFY_WORKERS.append(
            asyncio.create_task(_runtime_log_notify_worker(), name="runtime-log-notify")
        )
    return _RUNTIME_LOG_NOTIFY_QUEUE


async def _runtime_log_notify_worker() -> None:
    from ..services.account_bot_runtime import notify_runtime_log

    assert _RUNTIME_LOG_NOTIFY_QUEUE is not None
    while True:
        row = await _RUNTIME_LOG_NOTIFY_QUEUE.get()
        try:
            if isinstance(row, RuntimeLog):
                await notify_runtime_log(row)
        except Exception:  # noqa: BLE001
            log.debug("account bot runtime log notify failed", exc_info=True)
        finally:
            _RUNTIME_LOG_NOTIFY_QUEUE.task_done()


async def _enqueue_runtime_log_notifies(rows: list[RuntimeLog]) -> None:
    global _RUNTIME_LOG_NOTIFY_DROPPED
    if not rows:
        return
    queue = await _ensure_runtime_log_notify_workers()
    for row in rows:
        try:
            queue.put_nowait(row)
        except asyncio.QueueFull:
            _RUNTIME_LOG_NOTIFY_DROPPED += 1
            if _RUNTIME_LOG_NOTIFY_DROPPED % 50 == 1:
                log.warning(
                    "runtime_log notify queue full; dropped=%s",
                    _RUNTIME_LOG_NOTIFY_DROPPED,
                )


# ── PID 文件：用于跨 uvicorn 重启识别+回收孤儿 worker ──────────────
#
# 放在用户目录而不是项目根，避免容器内只读 mount 之类的边界场景；目录不存在就自动建。
# 命名 ``worker-{aid}.pid``，内容是该 worker 子进程的 PID。spawn 后立刻写入，
# stop_worker 末尾删除——所以"还在的 PID 文件 + 文件里的 PID 还活着"= 上次启动遗留。
_PID_DIR = Path.home() / ".telepilot" / "worker-pids"
_LEGACY_PID_DIR = Path.home() / ".telebot" / "worker-pids"


def _pid_file(account_id: int) -> Path:
    return _PID_DIR / f"worker-{account_id}.pid"


def _write_pid_file(account_id: int, pid: int) -> None:
    """worker spawn 成功后调一次；失败静默（PID 文件只是清理用，丢了也只是少一次清理）。"""
    try:
        _PID_DIR.mkdir(parents=True, exist_ok=True)
        _pid_file(account_id).write_text(str(pid), encoding="ascii")
    except OSError as exc:
        log.warning("写 PID 文件失败 aid=%s: %s", account_id, exc)


def _remove_pid_file(account_id: int) -> None:
    """worker 优雅停止后清理；文件不存在视为已清理。"""
    try:
        _pid_file(account_id).unlink(missing_ok=True)
    except OSError:
        pass


def _is_our_worker_process(pid: int) -> bool:
    """通过 ``ps`` 看命令行是否含本项目特征——避免 PID 复用误杀别人的进程。

    macOS / Linux 通用：``ps -p PID -o command=`` 输出该进程完整命令行；
    如果含 ``multiprocessing.spawn`` 或 ``app.worker.runtime`` 字样，认为是我们的。
    任何 OSError / non-zero 退出码都视为"无法判定" → 谨慎不杀。
    """
    ps = shutil.which("ps")
    if not ps:
        return False
    try:
        out = subprocess.check_output(
            [ps, "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    cmd = out.strip()
    if not cmd:
        return False
    # spawn 出来的 worker cmdline 长这样（第一行 head 截断也包含）：
    #   /path/python -c from multiprocessing.spawn import spawn_main; ...
    # 老版本可能是 ``app.worker.runtime`` ——保留作兼容
    return ("multiprocessing.spawn" in cmd) or ("app.worker.runtime" in cmd)


def _kill_stale_workers() -> int:
    """startup 时调一次：回收上一次 uvicorn 留下的 PID 文件指向的、还活着的 worker。

    对每个 PID：
      1. ``os.kill(pid, 0)`` 探活——失败说明进程已死，直接清 PID 文件
      2. ``_is_our_worker_process(pid)`` 双确认是我们的 worker（防 PID 复用误杀）
      3. SIGTERM；等 2s；仍活着就 SIGKILL
      4. 不论 kill 是否成功，把 PID 文件删掉——下次又在这位置写新 PID

    返回真正杀掉的个数（用于 startup 日志）。
    """
    pid_dirs = [p for p in (_PID_DIR, _LEGACY_PID_DIR) if p.exists()]
    if not pid_dirs:
        return 0
    killed = 0
    for pid_dir in pid_dirs:
        for f in list(pid_dir.glob("worker-*.pid")):
            try:
                pid_text = f.read_text(encoding="ascii").strip()
                pid = int(pid_text)
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            # 1) 探活
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                # 进程存在但跨用户——不是我们启的，skip
                alive = False
            if not alive:
                f.unlink(missing_ok=True)
                continue
            # 2) 命令行验证（防 PID 复用：上次杀完，PID 被另一个无关进程拿走）
            if not _is_our_worker_process(pid):
                log.warning(
                    "PID 文件 %s 指向的 PID=%s 已被复用（命令行不匹配本项目），仅删 PID 文件",
                    f.name, pid,
                )
                f.unlink(missing_ok=True)
                continue
            # 3) 杀
            log.warning("发现孤儿 worker pid=%s (file=%s)，发送 SIGTERM", pid, f.name)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                f.unlink(missing_ok=True)
                continue
            except PermissionError:
                log.warning("SIGTERM pid=%s 被拒（PermissionError），跳过", pid)
                f.unlink(missing_ok=True)
                continue
            # 等 2s 给 worker 走 try/finally
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            # 仍活着 → SIGKILL
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
                log.warning("孤儿 worker pid=%s 不响应 SIGTERM，已 SIGKILL", pid)
            except ProcessLookupError:
                pass
            f.unlink(missing_ok=True)
            killed += 1
    return killed


@dataclass
class _WorkerHandle:
    """单个账号 worker 的运行时句柄。"""

    account_id: int
    process: mp.Process | None = None
    fail_count: int = 0
    next_retry_at: float = 0.0
    started_at: float = 0.0
    desired: str = "running"   # running | stopped


# 全局状态：account_id → handle
_WORKERS: dict[int, _WorkerHandle] = {}
_WORKER_LOCKS: dict[int, asyncio.Lock] = {}
# 后台协程列表（global listener / monitor / 两个 stream 消费者）
_BG_TASKS: list[asyncio.Task] = []


def get_worker_runtime_snapshot() -> list[dict[str, int | str | bool | None]]:
    """返回当前 worker 运行时快照（只读）。"""

    rows: list[dict[str, int | str | bool | None]] = []
    for aid, handle in _WORKERS.items():
        proc = handle.process
        rows.append(
            {
                "account_id": int(aid),
                "pid": int(proc.pid) if proc and proc.pid else None,
                "alive": bool(proc and proc.is_alive()),
                "desired": handle.desired,
                "fail_count": int(handle.fail_count),
            }
        )
    return rows


async def start_supervisor() -> None:
    """FastAPI lifespan startup 调用。"""
    # 0. 清理上次留下的孤儿 worker（kill -9 uvicorn 等情况下产生）。
    #    必须在拉起新 worker **之前** 做，否则新老 worker 都会连同一个 TG account
    #    并互相覆写命令回应。
    try:
        n_killed = _kill_stale_workers()
        if n_killed:
            log.warning("启动前清理了 %d 个孤儿 worker 进程", n_killed)
    except Exception:  # noqa: BLE001
        log.exception("清理孤儿 worker 失败（不阻塞启动，但可能存在并发响应问题）")

    # 1. 拉起所有 active 账号；若 kill switch 已开启，只记录托管状态，不启动 worker。
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Account).where(Account.status == ACCOUNT_STATUS_ACTIVE)
            )
        ).scalars().all()
    if await _kill_switch_enabled():
        log.warning("kill switch 已开启，启动阶段跳过 %d 个 active worker", len(rows))
    else:
        for acc in rows:
            await start_worker(acc.id)

    # 2. 启动后台监听协程
    _BG_TASKS.append(asyncio.create_task(_listen_global()))
    _BG_TASKS.append(asyncio.create_task(_monitor_loop()))
    _BG_TASKS.append(asyncio.create_task(_consume_runtime_log()))
    _BG_TASKS.append(asyncio.create_task(_consume_ratelimit_event()))

    # 3. 注册退出/信号 hook，避免 uvicorn 被暴力杀时遗留孤儿 worker
    _install_kill_hooks()

    log.info("supervisor 启动完成，托管 %d 个账号", len(rows))


def _terminate_all_children_blocking() -> None:
    """同步版本的 worker 进程清理：直接 terminate + join。

    用于 atexit / signal handler 等无法 await 的场景，确保即使主进程异常退出也不留孤儿。
    """
    for h in list(_WORKERS.values()):
        p = h.process
        if p is None:
            continue
        try:
            if p.is_alive():
                p.terminate()
        except Exception:  # noqa: BLE001
            pass
    # 给一点点时间走 SIGTERM 触发的 try/finally；之后 kill
    deadline = time.time() + 2
    for h in list(_WORKERS.values()):
        p = h.process
        if p is None:
            continue
        try:
            timeout = max(0.0, deadline - time.time())
            p.join(timeout=timeout)
            if p.is_alive():
                p.kill()
                p.join(timeout=1)
        except Exception:  # noqa: BLE001
            pass
    # 进程已经下去，PID 文件没用了，顺手清——不删也无害（下次启动 _kill_stale_workers
    # 探到 PID 已死会自己 unlink），但清掉能让目录干净点。
    for h in list(_WORKERS.values()):
        _remove_pid_file(h.account_id)


_HOOKS_INSTALLED = False


def _install_kill_hooks() -> None:
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    atexit.register(_terminate_all_children_blocking)

    def _on_signal(signum, _frame):  # noqa: ANN001
        log.warning("supervisor 收到信号 %s，正在 terminate 所有 worker…", signum)
        _terminate_all_children_blocking()
        # 让默认行为继续退出
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # 非主线程或受限环境会失败，忽略
            pass
    _HOOKS_INSTALLED = True


async def stop_all_workers() -> None:
    """FastAPI lifespan shutdown 调用：停止所有 worker 与后台协程。"""
    aids = list(_WORKERS.keys())
    if aids:
        # 并行关停，总耗时接近最慢单 worker，而不是 N×5s 串行。
        await asyncio.gather(*(stop_worker(aid) for aid in aids), return_exceptions=True)
    for t in _BG_TASKS:
        t.cancel()
    _BG_TASKS.clear()


async def start_worker(account_id: int) -> None:
    """拉起或恢复指定账号的 worker 子进程；幂等。"""
    if await _kill_switch_enabled():
        h = _WORKERS.setdefault(account_id, _WorkerHandle(account_id=account_id))
        h.desired = "stopped"
        log.info("kill switch 已开启，拒绝启动 worker: account=%d", account_id)
        return
    lock = _WORKER_LOCKS.setdefault(account_id, asyncio.Lock())
    async with lock:
        h = _WORKERS.get(account_id)
        if not h:
            h = _WorkerHandle(account_id=account_id)
            _WORKERS[account_id] = h
        h.desired = "running"
        if h.process and h.process.is_alive():
            return
        p = _MP_CTX.Process(target=worker_entry, args=(account_id,), daemon=False)
        p.start()
        h.process = p
        h.started_at = time.monotonic()
        h.next_retry_at = 0.0
        # 写 PID 文件——下次启动时 ``_kill_stale_workers`` 据此回收孤儿。
        # 失败不阻塞启动（最多失去一次孤儿清理机会）。
        if p.pid is not None:
            _write_pid_file(account_id, p.pid)
        log.info("worker 启动: account=%d pid=%s", account_id, p.pid)


async def stop_worker(account_id: int) -> None:
    """停止指定账号的 worker：先发 IPC stop，等 5s 优雅退出，否则 terminate。"""
    lock = _WORKER_LOCKS.setdefault(account_id, asyncio.Lock())
    async with lock:
        h = _WORKERS.get(account_id)
        if not h:
            return
        h.desired = "stopped"
        try:
            redis = get_redis()
            await redis.publish(cmd_channel(account_id), make_cmd(CMD_STOP))
        except Exception:  # noqa: BLE001
            # Redis 只是优雅关停通道，不能成为本地终止子进程的前置条件。
            log.warning(
                "发送 worker stop IPC 失败，改为本地终止: account=%d",
                account_id,
                exc_info=True,
            )
        if h.process:
            # 等 5s 优雅退出（每 100ms 探测一次）
            for _ in range(50):
                if not h.process.is_alive():
                    break
                await asyncio.sleep(0.1)
            if h.process.is_alive():
                h.process.terminate()
                h.process.join(timeout=2)
            if h.process.is_alive():
                h.process.kill()
                h.process.join(timeout=1)
            h.process = None
        h.started_at = 0.0
        h.next_retry_at = 0.0
        # 进程已确认死掉 → 删 PID 文件，避免被下次启动当孤儿误杀（PID 复用情形）
        _remove_pid_file(account_id)
        log.info("worker 停止: account=%d", account_id)


async def pause_worker(account_id: int) -> None:
    """通过 IPC 让 worker 暂停主动动作。"""
    redis = get_redis()
    await redis.publish(cmd_channel(account_id), make_cmd(CMD_PAUSE))


async def resume_worker(account_id: int) -> None:
    """通过 IPC 让 worker 恢复主动动作。"""
    redis = get_redis()
    await redis.publish(cmd_channel(account_id), make_cmd(CMD_RESUME))


async def stop_running_workers() -> None:
    """停止当前 supervisor 托管的全部 worker，但不取消后台监听任务。"""
    for aid in list(_WORKERS.keys()):
        await stop_worker(aid)


async def start_active_workers() -> None:
    """启动 DB 中状态为 active 的账号 worker。"""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Account.id).where(Account.status == ACCOUNT_STATUS_ACTIVE)
            )
        ).scalars().all()
    for aid in rows:
        await start_worker(int(aid))


async def _kill_switch_enabled() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(SystemSetting, "kill_switch")
        if row is None:
            return False
        value = row.value
        if isinstance(value, dict):
            return bool(value.get("enabled", False))
        return bool(value)
    except Exception:  # noqa: BLE001
        return False


async def _account_status(account_id: int) -> str | None:
    try:
        async with AsyncSessionLocal() as db:
            return await db.scalar(select(Account.status).where(Account.id == account_id))
    except Exception:  # noqa: BLE001
        return None


async def _listen_global() -> None:
    """监听 ``worker_global``：A Agent 登录完成会广播 ``start_worker`` 指令。"""
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            try:
                cmd = IPCMessage.decode(msg["data"])
            except Exception:
                continue
            if cmd.type == "start_worker":
                aid = cmd.payload.get("account_id")
                if aid:
                    await start_worker(int(aid))
            elif cmd.type == GCMD_KILL_SWITCH:
                if cmd.payload.get("enabled"):
                    await stop_running_workers()
                else:
                    await start_active_workers()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await pubsub.unsubscribe(GLOBAL_CHANNEL)
            await pubsub.close()
        except Exception:
            pass


async def _monitor_loop() -> None:
    """每 2s 检查每个 worker 是否活着；崩溃则按指数退避重启。"""
    try:
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            for aid, h in list(_WORKERS.items()):
                if h.desired != "running":
                    continue
                if h.process and h.process.is_alive():
                    # 只有连续存活达到稳定窗口才清零；短暂存活仍属于同一轮崩溃。
                    if (
                        h.fail_count
                        and h.started_at
                        and now - h.started_at >= _STABLE_WINDOW_SECONDS
                    ):
                        h.fail_count = 0
                        h.next_retry_at = 0.0
                    continue
                # 进程死了
                status = await _account_status(aid)
                if status != ACCOUNT_STATUS_ACTIVE:
                    h.desired = "stopped"
                    log.info("worker %d 已退出且账号状态为 %s，停止自动重启", aid, status)
                    continue
                if h.next_retry_at:
                    if now < h.next_retry_at:
                        continue
                    # 已等满上一轮计算出的退避时间，此时才真正重启。
                    try:
                        await start_worker(aid)
                    except Exception:  # noqa: BLE001
                        # spawn 本身失败也属于一次失败，交回下一轮重新计数和退避。
                        h.next_retry_at = 0.0
                        log.exception("worker %d 重启失败", aid)
                    continue

                # 首次观察到本轮退出：稳定运行过则开启新的失败序列。
                if h.started_at and now - h.started_at >= _STABLE_WINDOW_SECONDS:
                    h.fail_count = 0
                if h.process is not None:
                    try:
                        h.process.join(timeout=0)
                    except Exception:  # noqa: BLE001
                        pass
                    h.process = None
                    _remove_pid_file(aid)
                h.started_at = 0.0
                h.fail_count += 1
                if h.fail_count > len(_BACKOFF):
                    log.error(
                        "worker %d 连续失败 %d 次，置 dead", aid, h.fail_count
                    )
                    async with AsyncSessionLocal() as db:
                        await db.execute(
                            update(Account)
                            .where(Account.id == aid)
                            .values(status=ACCOUNT_STATUS_DEAD)
                        )
                        await db.commit()
                    # 2-D: account dead 告警（若未配置 alert/default channel，send 返回 False）
                    try:
                        from ..services import notify_service

                        await notify_service.send(
                            "alert",
                            f"⚠️ account {aid} crashed: 连续失败 {h.fail_count} 次，已置 dead",
                        )
                    except Exception:
                        log.exception("发送 account dead 告警失败: aid=%d", aid)
                    try:
                        from ..services.account_bot_runtime import notify_account

                        await notify_account(
                            aid,
                            f"⚠️ <b>账号 worker 已停止</b>\n账号：<code>{aid}</code>\n"
                            f"连续失败：<code>{h.fail_count}</code> 次，状态已置为 dead。",
                        )
                    except Exception:
                        log.exception("发送 account bot dead 告警失败: aid=%d", aid)
                    h.desired = "stopped"
                    continue
                wait = _BACKOFF[min(h.fail_count - 1, len(_BACKOFF) - 1)]
                h.next_retry_at = now + wait
                log.warning(
                    "worker %d 崩溃，将在 %ds 后进行第 %d 次重启",
                    aid,
                    wait,
                    h.fail_count,
                )
    except asyncio.CancelledError:
        pass


async def _consume_runtime_log() -> None:
    """从 Redis stream 可靠消费运行日志，落库 runtime_log。"""
    await _consume_stream_reliable(
        stream_key=RUNTIME_LOG_STREAM,
        inflight_key=f"{RUNTIME_LOG_STREAM}:inflight",
        build_row=_build_runtime_log_row_with_retention,
        consumer_name="runtime_log",
    )


async def _consume_ratelimit_event() -> None:
    """从 Redis stream 可靠消费风控事件，落库 rate_limit_event。"""
    await _consume_stream_reliable(
        stream_key=RATELIMIT_EVENT_STREAM,
        inflight_key=f"{RATELIMIT_EVENT_STREAM}:inflight",
        build_row=_build_ratelimit_event_row,
        consumer_name="ratelimit_event",
    )


def _build_runtime_log_row(raw: str) -> RuntimeLog | None:
    """把 runtime_log 原始 JSON 转换成 ORM 行，坏数据返回 None。"""
    try:
        d = json.loads(raw)
        return RuntimeLog(
            account_id=d.get("account_id"),
            level=d.get("level", "info"),
            source=d.get("source"),
            message=redact_text(str(d.get("message", ""))),
            detail=redact_value(d.get("detail")) if d.get("detail") is not None else None,
        )
    except Exception:
        return None


async def _build_runtime_log_row_with_retention(raw: str) -> RuntimeLog | None:
    """把 runtime_log 原始 JSON 转为 ORM 行，并按设置截断内容。"""

    try:
        d = json.loads(raw)
        cfg = await _get_log_retention_config()
        level = _normalize_runtime_log_level(d.get("level", "info"))
        min_level = str(cfg.get("runtime_log_min_level", "info") or "info")
        if not _runtime_log_level_allowed(level, min_level):
            return None
        return RuntimeLog(
            account_id=d.get("account_id"),
            level=level,
            source=d.get("source"),
            message=_truncate_text(
                redact_text(str(d.get("message", ""))),
                int(cfg.get("runtime_log_max_message_chars", 2000) or 2000),
            ),
            detail=_truncate_detail(
                redact_value(d.get("detail")) if d.get("detail") is not None else None,
                int(cfg.get("runtime_log_max_detail_chars", 8000) or 0),
            ),
        )
    except Exception:
        return None


def _build_ratelimit_event_row(raw: str) -> RateLimitEvent | None:
    """把 ratelimit_event 原始 JSON 转换成 ORM 行，坏数据返回 None。"""
    try:
        d = json.loads(raw)
        return RateLimitEvent(
            account_id=d["account_id"],
            action=d["action"],
            outcome=d["outcome"],
            detail=d.get("detail"),
        )
    except Exception:
        return None


async def _consume_stream_reliable(
    *,
    stream_key: str,
    inflight_key: str,
    build_row: Callable[[str], object | None],
    consumer_name: str,
    batch_size: int = 50,
) -> None:
    """可靠消费通用 helper：LMOVE/BLMOVE 到 inflight，提交 DB 后 LREM ack。

    语义：
    - 从待消费队列原子移动到 inflight（避免处理前丢失）
    - DB 提交成功后再从 inflight 删除（ack）
    - DB 失败时 inflight 保留，下一轮重试
    """
    redis = get_redis()
    try:
        while True:
            try:
                inflight_len = await redis.llen(inflight_key)
                if inflight_len == 0:
                    try:
                        first = await redis.blmove(
                            stream_key,
                            inflight_key,
                            timeout=5,
                            src="LEFT",
                            dest="RIGHT",
                        )
                    except (TimeoutError, RedisTimeoutError):
                        continue
                    if first is None:
                        continue
                for _ in range(batch_size - 1):
                    moved = await redis.lmove(
                        stream_key,
                        inflight_key,
                        src="LEFT",
                        dest="RIGHT",
                    )
                    if moved is None:
                        break
                items = await redis.lrange(inflight_key, 0, batch_size - 1)
                if not items:
                    continue
                rows: list[object] = []
                ack_items: list[str] = []
                for raw in items:
                    row = build_row(raw)
                    if hasattr(row, "__await__"):
                        row = await row
                    if row is None:
                        # 无法解析的数据直接丢弃，避免毒消息永久阻塞队列。
                        await redis.lrem(inflight_key, 1, raw)
                        continue
                    rows.append(row)
                    ack_items.append(raw)
                if not rows:
                    continue
                async with AsyncSessionLocal() as db:
                    db.add_all(rows)
                    await db.commit()
                if consumer_name == "runtime_log":
                    await _cleanup_runtime_logs_if_due()
                if consumer_name == "runtime_log":
                    try:
                        await _enqueue_runtime_log_notifies(
                            [row for row in rows if isinstance(row, RuntimeLog)]
                        )
                    except Exception:
                        log.debug("account bot runtime log notify skipped", exc_info=True)
                for raw in ack_items:
                    await redis.lrem(inflight_key, 1, raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("%s 消费失败: %s", consumer_name, e)
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
