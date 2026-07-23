"""System Agent 持久运行与可恢复事件订阅。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...db.base import AsyncSessionLocal
from ...db.models.system_agent import (
    AGENT_RUN_ACTIVE_STATUSES,
    AGENT_RUN_CANCELLED,
    AGENT_RUN_FAILED,
    AGENT_RUN_KIND_MESSAGE,
    AGENT_RUN_KIND_RETRY,
    AGENT_RUN_QUEUED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_SUCCEEDED,
    AGENT_RUN_TERMINAL_STATUSES,
    MESSAGE_ROLE_USER,
    SystemAgentMessage,
    SystemAgentRun,
    SystemAgentRunEvent,
    SystemAgentSession,
)
from .redactor import redact_content
from .secrets import extract_plaintext_secrets, redact_known_secrets
from .service import get_system_agent_service

log = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    """同一会话已有不同请求在运行。"""


class RunNotFoundError(RuntimeError):
    """运行记录不存在。"""


@dataclass(frozen=True, slots=True)
class _RunRequest:
    session_id: str
    web_user_id: int
    text: str
    kind: str
    retry_message_id: int | None = None
    fallback_provider_id: int | None = None
    approved_tools: tuple[str, ...] = ()
    chat_secrets: tuple[str, ...] = ()
    after_message_id: int = 0


class _RunCancelled(Exception):
    pass


class SystemAgentRunManager:
    """用进程内 task 执行 Agent，以数据库事件作为订阅边界。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        service_factory: Callable[[], Any] = get_system_agent_service,
        poll_interval: float = 0.2,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory
        self._poll_interval = poll_interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._ready = False
        self._ready_lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        """首次使用时将上个进程遗留的未完成运行转为可重试失败。"""

        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            async with self._session_factory() as db:
                result = await db.execute(
                    select(SystemAgentRun).where(
                        SystemAgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES)
                    )
                )
                rows = list(result.scalars())
                for row in rows:
                    await self._reconcile_interrupted(db, row)
                await db.commit()
            self._ready = True

    async def start_run(
        self,
        *,
        session_id: str,
        web_user_id: int,
        client_request_id: str,
        text: str,
        retry_message_id: int | None = None,
        fallback_provider_id: int | None = None,
        approved_tools: list[str] | None = None,
    ) -> SystemAgentRun:
        await self.ensure_ready()
        kind = AGENT_RUN_KIND_RETRY if retry_message_id is not None else AGENT_RUN_KIND_MESSAGE
        approved = tuple(str(item) for item in (approved_tools or []))
        after_message_id = 0
        request_hash = _request_hash(
            kind=kind,
            text=text,
            retry_message_id=retry_message_id,
            fallback_provider_id=fallback_provider_id,
            approved_tools=approved,
        )

        async with self._session_factory() as db:
            session_result = await db.execute(
                select(SystemAgentSession.id)
                .where(SystemAgentSession.id == session_id)
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                raise RunNotFoundError(f"session:{session_id}")
            existing = await self._find_by_request(
                db,
                session_id=session_id,
                client_request_id=client_request_id,
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise RunConflictError("同一个请求标识不能用于不同的助手输入")
                existing_id = existing.id
                await db.rollback()
                return await self._wait_for_user_message(existing_id)

            active_result = await db.execute(
                select(SystemAgentRun)
                .where(
                    SystemAgentRun.session_id == session_id,
                    SystemAgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
                )
                .order_by(desc(SystemAgentRun.created_at))
                .limit(1)
            )
            active = active_result.scalar_one_or_none()
            if active is not None:
                if active.request_hash == request_hash:
                    active_id = active.id
                    await db.rollback()
                    return await self._wait_for_user_message(active_id)
                raise RunConflictError("当前会话已有一轮助手请求正在执行")

            if retry_message_id is None:
                latest_message_result = await db.execute(
                    select(SystemAgentMessage.id)
                    .where(
                        SystemAgentMessage.session_id == session_id,
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                    )
                    .order_by(desc(SystemAgentMessage.id))
                    .limit(1)
                )
                after_message_id = latest_message_result.scalar_one_or_none() or 0

            row = SystemAgentRun(
                id=str(uuid.uuid4()),
                session_id=session_id,
                web_user_id=web_user_id,
                user_message_id=retry_message_id,
                client_request_id=client_request_id,
                request_hash=request_hash,
                kind=kind,
                status=AGENT_RUN_QUEUED,
            )
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await self._find_by_request(
                    db,
                    session_id=session_id,
                    client_request_id=client_request_id,
                )
                if existing is None or existing.request_hash != request_hash:
                    raise RunConflictError("助手请求已被并发创建，请刷新后重试") from None
                existing_id = existing.id
                await db.rollback()
                return await self._wait_for_user_message(existing_id)
            await db.refresh(row)

        request = _RunRequest(
            session_id=session_id,
            web_user_id=web_user_id,
            text=text,
            kind=kind,
            retry_message_id=retry_message_id,
            fallback_provider_id=fallback_provider_id,
            approved_tools=approved,
            chat_secrets=tuple(extract_plaintext_secrets(text)),
            after_message_id=after_message_id,
        )
        cancel_event = asyncio.Event()
        self._cancel_events[row.id] = cancel_event
        task = asyncio.create_task(self._execute(row.id, request, cancel_event))
        self._tasks[row.id] = task
        task.add_done_callback(lambda done, run_id=row.id: self._task_done(run_id, done))

        return await self._wait_for_user_message(row.id)

    async def _wait_for_user_message(self, run_id: str) -> SystemAgentRun:
        # 现有 service 在调用 Provider 前会先提交用户消息，短暂等待可以让
        # 首次创建与幂等复用都返回稳定的 user_message_id。
        for _ in range(500):
            current = await self.get_run(run_id)
            if current.user_message_id is not None or current.status in AGENT_RUN_TERMINAL_STATUSES:
                return current
            await asyncio.sleep(0.01)
        return await self.get_run(run_id)

    async def get_run(self, run_id: str) -> SystemAgentRun:
        await self.ensure_ready()
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            if row is None:
                raise RunNotFoundError(run_id)
            return row

    async def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[SystemAgentRunEvent]:
        await self.ensure_ready()
        async with self._session_factory() as db:
            result = await db.execute(
                select(SystemAgentRunEvent)
                .where(
                    SystemAgentRunEvent.run_id == run_id,
                    SystemAgentRunEvent.seq > max(0, after_seq),
                )
                .order_by(SystemAgentRunEvent.seq)
                .limit(limit)
            )
            return list(result.scalars())

    async def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """订阅者退出只结束本迭代器，不会触碰后台执行 task。"""

        cursor = max(0, after_seq)
        while True:
            rows = await self.list_events(run_id, after_seq=cursor)
            for row in rows:
                cursor = row.seq
                yield dict(row.event or {})
            run = await self.get_run(run_id)
            if run.status in AGENT_RUN_TERMINAL_STATUSES and cursor >= run.last_seq:
                return
            await asyncio.sleep(self._poll_interval)

    async def cancel_run(self, run_id: str) -> SystemAgentRun:
        """幂等请求取消；本进程任务由协作信号把取消注入 service 生成器。"""

        await self.ensure_ready()
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            if row is None:
                raise RunNotFoundError(run_id)
            if row.status in AGENT_RUN_TERMINAL_STATUSES:
                return row
            row.cancel_requested = True
            row.updated_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(row)

        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()
            return row

        # 运行不属于当前进程时无法安全恢复执行，直接落为已取消。
        await self._append_terminal_events(
            run_id,
            code="AGENT_RUN_CANCELLED",
            message="本轮请求已取消。",
        )
        await self._finish_run(
            run_id,
            status=AGENT_RUN_CANCELLED,
            error_code="AGENT_RUN_CANCELLED",
            error_message="本轮请求已取消。",
        )
        return await self.get_run(run_id)

    async def _execute(
        self,
        run_id: str,
        request: _RunRequest,
        cancel_event: asyncio.Event,
    ) -> None:
        done_seen = False
        done_ok = False
        error_code: str | None = None
        error_message: str | None = None
        try:
            await self._mark_running(run_id)
            async with self._session_factory() as db:
                session = await db.get(SystemAgentSession, request.session_id)
                if session is None:
                    raise RunNotFoundError(f"session:{request.session_id}")
                retry_message = None
                if request.retry_message_id is not None:
                    retry_message = await db.get(SystemAgentMessage, request.retry_message_id)
                    if retry_message is None or retry_message.session_id != request.session_id:
                        raise RunNotFoundError(f"message:{request.retry_message_id}")

                stream = self._service_factory().stream_message(
                    db,
                    session=session,
                    text=request.text,
                    role="admin",
                    channel="web",
                    web_user_id=request.web_user_id,
                    retry_message=retry_message,
                    fallback_provider_id=request.fallback_provider_id,
                    approved_tools=list(request.approved_tools),
                    run_id=run_id,
                )
                while True:
                    try:
                        event = await self._next_event(stream, cancel_event)
                    except StopAsyncIteration:
                        break
                    await self._link_user_message(run_id, request)
                    persisted = await self._append_event(
                        run_id,
                        event,
                        known_secrets=request.chat_secrets,
                    )
                    event_type = persisted.get("type")
                    if event_type == "error":
                        error_code = str(persisted.get("code") or "AGENT_RUN_FAILED")[:64]
                        error_message = str(persisted.get("message") or "助手运行失败")[:1024]
                    elif event_type == "done":
                        done_seen = True
                        done_ok = bool(persisted.get("ok"))

            await self._link_user_message(run_id, request)
            if not done_seen:
                error_code = error_code or "AGENT_STREAM_INCOMPLETE"
                error_message = error_message or "助手响应提前结束，请重试本轮。"
                await self._append_terminal_events(
                    run_id,
                    code=error_code,
                    message=error_message,
                )
            await self._finish_run(
                run_id,
                status=AGENT_RUN_SUCCEEDED if done_seen and done_ok else AGENT_RUN_FAILED,
                error_code=None if done_seen and done_ok else error_code or "AGENT_RUN_FAILED",
                error_message=None if done_seen and done_ok else error_message or "助手运行失败",
            )
        except _RunCancelled:
            await self._link_user_message(run_id, request)
            await self._append_terminal_events(
                run_id,
                code="AGENT_RUN_CANCELLED",
                message="本轮请求已取消。",
            )
            await self._finish_run(
                run_id,
                status=AGENT_RUN_CANCELLED,
                error_code="AGENT_RUN_CANCELLED",
                error_message="本轮请求已取消。",
            )
        except asyncio.CancelledError:
            await self._append_terminal_events(
                run_id,
                code="AGENT_RUN_INTERRUPTED",
                message="助手进程已停止，本轮可重试。",
            )
            await self._finish_run(
                run_id,
                status=AGENT_RUN_FAILED,
                error_code="AGENT_RUN_INTERRUPTED",
                error_message="助手进程已停止，本轮可重试。",
            )
            raise
        except RunNotFoundError:
            # 会话被用户删除时外键会级联清理 Run；后台任务无需再补写终态。
            log.info("system agent durable run removed while executing run=%s", run_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("system agent durable run failed run=%s", run_id)
            await self._append_terminal_events(
                run_id,
                code="AGENT_RUN_MANAGER_FAILED",
                message=f"助手后台运行失败（{type(exc).__name__}），请重试本轮。",
            )
            await self._finish_run(
                run_id,
                status=AGENT_RUN_FAILED,
                error_code="AGENT_RUN_MANAGER_FAILED",
                error_message=f"助手后台运行失败（{type(exc).__name__}），请重试本轮。",
            )

    async def _next_event(
        self,
        stream: AsyncIterator[dict[str, Any]],
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        if cancel_event.is_set():
            raise _RunCancelled
        next_task = asyncio.create_task(anext(stream))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {next_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_event.is_set():
                next_task.cancel()
                try:
                    await next_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                raise _RunCancelled
            return await next_task
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
            if not next_task.done():
                next_task.cancel()

    async def _mark_running(self, run_id: str) -> None:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            if row is None:
                raise RunNotFoundError(run_id)
            row.status = AGENT_RUN_RUNNING
            row.started_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            await db.commit()

    async def _link_user_message(self, run_id: str, request: _RunRequest) -> None:
        async with self._session_factory() as db:
            run = await db.get(SystemAgentRun, run_id)
            if run is None or run.user_message_id is not None:
                return
            if request.retry_message_id is not None:
                run.user_message_id = request.retry_message_id
            else:
                result = await db.execute(
                    select(SystemAgentMessage)
                    .where(
                        SystemAgentMessage.session_id == request.session_id,
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                        SystemAgentMessage.id > request.after_message_id,
                    )
                    .order_by(desc(SystemAgentMessage.id))
                    .limit(1)
                )
                message = result.scalar_one_or_none()
                if message is None:
                    return
                run.user_message_id = message.id
            run.updated_at = datetime.now(UTC)
            await db.commit()

    async def _append_event(
        self,
        run_id: str,
        source: dict[str, Any],
        *,
        known_secrets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        async with self._session_factory() as db:
            run = await db.get(SystemAgentRun, run_id, with_for_update=True)
            if run is None:
                raise RunNotFoundError(run_id)
            seq = run.last_seq + 1
            event = _redact_event(source, known_secrets)
            source_run_id = event.get("run_id")
            if source_run_id and source_run_id != run_id:
                event["runtime_run_id"] = source_run_id
            event["run_id"] = run_id
            event["session_id"] = run.session_id
            event["seq"] = seq
            db.add(SystemAgentRunEvent(run_id=run_id, seq=seq, event=event))
            run.last_seq = seq
            run.updated_at = datetime.now(UTC)
            await db.commit()
            return event

    async def _append_terminal_events(self, run_id: str, *, code: str, message: str) -> None:
        run = await self.get_run(run_id)
        rows = await self.list_events(run_id, after_seq=max(0, run.last_seq - 1), limit=2)
        if any((row.event or {}).get("type") == "done" for row in rows):
            return
        if not any((row.event or {}).get("type") == "error" for row in rows):
            await self._append_event(
                run_id,
                {"type": "error", "code": code, "message": message},
            )
        await self._append_event(run_id, {"type": "done", "ok": False})

    async def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            if row is None:
                return
            row.status = status
            row.error_code = error_code
            row.error_message = error_message
            row.finished_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            await db.commit()

    async def _find_by_request(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        client_request_id: str,
    ) -> SystemAgentRun | None:
        result = await db.execute(
            select(SystemAgentRun).where(
                SystemAgentRun.session_id == session_id,
                SystemAgentRun.client_request_id == client_request_id,
            )
        )
        return result.scalar_one_or_none()

    async def _reconcile_interrupted(self, db: AsyncSession, row: SystemAgentRun) -> None:
        last_result = await db.execute(
            select(SystemAgentRunEvent)
            .where(SystemAgentRunEvent.run_id == row.id)
            .order_by(desc(SystemAgentRunEvent.seq))
            .limit(1)
        )
        last_event_row = last_result.scalar_one_or_none()
        last_event = last_event_row.event if last_event_row is not None else {}
        if isinstance(last_event, dict) and last_event.get("type") == "done":
            succeeded = bool(last_event.get("ok"))
            row.status = AGENT_RUN_SUCCEEDED if succeeded else AGENT_RUN_FAILED
            row.error_code = None if succeeded else row.error_code or "AGENT_RUN_FAILED"
            row.error_message = None if succeeded else row.error_message or "助手运行失败"
            row.finished_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            return

        code = "AGENT_RUN_INTERRUPTED"
        message = "服务重启导致本轮未完成，请重试本轮。"
        seq = row.last_seq
        for event in (
            {"type": "error", "code": code, "message": message},
            {"type": "done", "ok": False},
        ):
            seq += 1
            payload = {
                **event,
                "run_id": row.id,
                "session_id": row.session_id,
                "seq": seq,
            }
            db.add(SystemAgentRunEvent(run_id=row.id, seq=seq, event=payload))
        row.last_seq = seq
        row.status = AGENT_RUN_FAILED
        row.error_code = code
        row.error_message = message
        row.finished_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:  # noqa: BLE001
                log.exception("reading system agent run task result failed run=%s", run_id)


def _request_hash(
    *,
    kind: str,
    text: str,
    retry_message_id: int | None,
    fallback_provider_id: int | None,
    approved_tools: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "text": text,
            "retry_message_id": retry_message_id,
            "fallback_provider_id": fallback_provider_id,
            "approved_tools": sorted(approved_tools),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_event(source: dict[str, Any], known_secrets: tuple[str, ...]) -> dict[str, Any]:
    value = redact_content(source)

    def replace_known(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key): replace_known(child) for key, child in item.items()}
        if isinstance(item, list):
            return [replace_known(child) for child in item]
        if isinstance(item, str):
            return redact_known_secrets(item, list(known_secrets)).replace(
                "***",
                "[REDACTED]",
            )
        return item

    redacted = replace_known(value)
    return redacted if isinstance(redacted, dict) else {}


_RUN_MANAGER: SystemAgentRunManager | None = None


def get_system_agent_run_manager() -> SystemAgentRunManager:
    global _RUN_MANAGER
    if _RUN_MANAGER is None:
        _RUN_MANAGER = SystemAgentRunManager()
    return _RUN_MANAGER


__all__ = [
    "RunConflictError",
    "RunNotFoundError",
    "SystemAgentRunManager",
    "get_system_agent_run_manager",
]
