"""System Agent 持久队列、Durable Run 与可恢复事件订阅。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...crypto import decrypt_str, encrypt_str
from ...db.base import AsyncSessionLocal
from ...db.models.system_agent import (
    AGENT_RUN_CANCELLED,
    AGENT_RUN_FAILED,
    AGENT_RUN_KIND_MESSAGE,
    AGENT_RUN_KIND_REGENERATE,
    AGENT_RUN_KIND_RETRY,
    AGENT_RUN_OPEN_STATUSES,
    AGENT_RUN_QUEUED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_STREAM_FINAL_STATUSES,
    AGENT_RUN_SUCCEEDED,
    AGENT_RUN_TERMINAL_STATUSES,
    AGENT_RUN_WAITING_APPROVAL,
    AGENT_RUN_WAITING_INPUT,
    CHANNEL_BOT,
    CHANNEL_WEB,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MESSAGE_RUN_COMPLETED,
    MESSAGE_RUN_FAILED,
    MESSAGE_RUN_PENDING,
    MESSAGE_RUN_SUCCEEDED,
    PENDING_TURN_CANCELLED,
    PENDING_TURN_DISPATCHED,
    PENDING_TURN_DISPATCHING,
    PENDING_TURN_PAUSED,
    PENDING_TURN_PENDING,
    RUN_INPUT_APPLIED,
    RUN_INPUT_APPROVAL,
    RUN_INPUT_PENDING,
    RUN_INPUT_STEER,
    RUN_INPUT_USER,
    SystemAgentMessage,
    SystemAgentPendingTurn,
    SystemAgentRun,
    SystemAgentRunEvent,
    SystemAgentRunInput,
    SystemAgentSession,
)
from .redactor import redact_content
from .secrets import extract_plaintext_secrets, redact_known_secrets
from .service import get_system_agent_service, is_latest_completed_pair

log = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    """请求与已有幂等键、队列容量或当前运行状态冲突。"""


class RunNotFoundError(RuntimeError):
    """运行、会话或排队项不存在。"""


@dataclass(frozen=True, slots=True)
class _RunRequest:
    session_id: str
    web_user_id: int | None
    bot_tg_user_id: int | None
    channel: str
    role: str
    text: str
    kind: str
    retry_message_id: int | None = None
    regenerate_message_id: int | None = None
    regenerate_assistant_message_id: int | None = None
    fallback_provider_id: int | None = None
    approved_tools: tuple[str, ...] = ()
    chat_secrets: tuple[str, ...] = ()
    after_message_id: int = 0
    model_selection: dict[str, Any] | None = None
    read_only_only: bool = False


class _RunCancelled(Exception):
    pass


class _WorkerLeaseLost(Exception):
    """当前执行协程已失去数据库 claim，必须静默退出并让新 worker 接管。"""


class SystemAgentRunManager:
    """数据库队列为事实源，进程内 task 只是当前 worker 的执行载体。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        service_factory: Callable[[], Any] = get_system_agent_service,
        poll_interval: float = 0.2,
        queue_limit: int = 10,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 5.0,
        recovery_seconds: float | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory
        self._poll_interval = poll_interval
        self._queue_limit = max(1, int(queue_limit))
        self._lease_seconds = max(3.0, float(lease_seconds))
        self._heartbeat_seconds = max(0.5, float(heartbeat_seconds))
        self._recovery_seconds = max(
            0.5,
            float(
                recovery_seconds
                if recovery_seconds is not None
                else min(self._lease_seconds / 2, 5.0)
            ),
        )
        self._worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._recovery_task: asyncio.Task[None] | None = None
        self._ready = False
        self._ready_lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        """恢复过期 lease，并继续所有可执行的 queued Run。"""

        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            session_ids = await self._recover_expired_and_queued_runs()
            self._ready = True
            self._start_recovery_loop()
            for session_id in session_ids:
                self._kick_session(session_id)

    async def shutdown(self) -> None:
        """停止本进程拥有的调度与执行任务，运行中的 Run 回退为可恢复队列。"""

        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)

        dispatch_tasks = list(self._dispatch_tasks.values())
        for task in dispatch_tasks:
            task.cancel()
        if dispatch_tasks:
            await asyncio.gather(*dispatch_tasks, return_exceptions=True)

        run_tasks = list(self._tasks.values())
        for task in run_tasks:
            task.cancel()
        if run_tasks:
            await asyncio.gather(*run_tasks, return_exceptions=True)

        # 调度器可能已提交 claim、但尚未来得及注册进程内 task；以数据库事实源兜底。
        async with self._session_factory() as db:
            claimed_result = await db.execute(
                select(SystemAgentRun.id).where(
                    SystemAgentRun.status == AGENT_RUN_RUNNING,
                    SystemAgentRun.claimed_by == self._worker_id,
                )
            )
            claimed_run_ids = list(claimed_result.scalars())
        for run_id in claimed_run_ids:
            await self._requeue_claimed_run(run_id)

        self._dispatch_tasks.clear()
        self._tasks.clear()
        self._cancel_events.clear()
        self._ready = False

    def _start_recovery_loop(self) -> None:
        current = self._recovery_task
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._recovery_loop())
        self._recovery_task = task
        task.add_done_callback(self._recovery_done)

    async def _recovery_loop(self) -> None:
        """持续接管其它进程遗留的过期 lease 与提交后未调度的 queued Run。"""

        while True:
            await asyncio.sleep(self._recovery_seconds)
            try:
                session_ids = await self._recover_expired_and_queued_runs()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception(
                    "system agent durable run recovery scan failed worker=%s",
                    self._worker_id,
                )
                continue
            for session_id in session_ids:
                self._kick_session(session_id)

    async def _recover_expired_and_queued_runs(self) -> set[str]:
        now = datetime.now(UTC)
        session_ids: set[str] = set()
        async with self._session_factory() as db:
            recoverable_result = await db.execute(
                select(SystemAgentRun)
                .where(
                    (SystemAgentRun.status == AGENT_RUN_QUEUED)
                    | (
                        (SystemAgentRun.status == AGENT_RUN_RUNNING)
                        & (
                            SystemAgentRun.lease_expires_at.is_(None)
                            | (SystemAgentRun.lease_expires_at <= now)
                        )
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            for row in recoverable_result.scalars():
                # PostgreSQL 在锁等待后通常会重检 WHERE；这里再校验一次，
                # 同时保护其它方言和未来隔离级别调整。
                lease = _as_utc(row.lease_expires_at)
                if (
                    row.status == AGENT_RUN_RUNNING
                    and lease is not None
                    and lease > now
                ):
                    continue
                pending = None
                if row.pending_turn_id:
                    pending_result = await db.execute(
                        select(SystemAgentPendingTurn)
                        .where(SystemAgentPendingTurn.id == row.pending_turn_id)
                        .with_for_update(skip_locked=True)
                    )
                    pending = pending_result.scalar_one_or_none()
                    # 其它事务若已锁住 PendingTurn，本轮不能在只持有 Run 锁时
                    # 反向等待，避免与会话调度形成 Run/PendingTurn 死锁。
                    if pending is None:
                        continue
                if (
                    row.status == AGENT_RUN_QUEUED
                    and pending is not None
                    and pending.status == PENDING_TURN_PAUSED
                ):
                    row.phase = "paused"
                    row.paused_reason = pending.blocked_reason
                    continue
                interrupted = (
                    row.status == AGENT_RUN_RUNNING or row.phase == "recovering"
                )
                if row.status == AGENT_RUN_RUNNING and row.cancel_requested:
                    stop_replace = row.paused_reason == "stop_replace"
                    await self._cancel_without_worker(db, row)
                    if stop_replace:
                        row.paused_reason = "stop_replace"
                        await self._set_following_queue_state(
                            db,
                            session_id=row.session_id,
                            resume=True,
                            reason=None,
                        )
                        session_ids.add(row.session_id)
                    else:
                        await self._set_following_queue_state(
                            db,
                            session_id=row.session_id,
                            resume=False,
                            reason=AGENT_RUN_CANCELLED,
                        )
                    row.updated_at = now
                    continue
                if interrupted:
                    await self._link_interrupted_user_message(db, row, pending)
                if await self._converge_persisted_success(db, row, pending, now):
                    continue
                if (
                    interrupted
                    and await self._converge_committed_message_result(
                        db,
                        row,
                        pending,
                        now,
                    )
                ):
                    continue
                session_ids.add(row.session_id)
                if interrupted:
                    await self._prepare_interrupted_message_retry(
                        db,
                        row,
                        pending,
                    )
                row.status = AGENT_RUN_QUEUED
                row.phase = "recovering"
                row.claimed_by = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                row.started_at = None
                row.updated_at = now
                if pending is not None:
                    pending.status = PENDING_TURN_PENDING
                    pending.blocked_reason = None
                    pending.updated_at = now
            await db.commit()
        return session_ids

    async def _link_interrupted_user_message(
        self,
        db: AsyncSession,
        row: SystemAgentRun,
        pending: SystemAgentPendingTurn | None,
    ) -> None:
        """补齐消息已提交、但 Run 尚未关联的崩溃窗口。"""

        if row.user_message_id is not None or pending is None:
            return
        payload = dict(pending.request_payload or {})
        existing_id = _optional_int(payload.get("retry_message_id")) or _optional_int(
            payload.get("regenerate_message_id")
        )
        if existing_id is not None:
            row.user_message_id = existing_id
            return
        after_message_id = int(payload.get("after_message_id") or 0)
        result = await db.execute(
            select(SystemAgentMessage)
            .where(
                SystemAgentMessage.session_id == row.session_id,
                SystemAgentMessage.role == MESSAGE_ROLE_USER,
                SystemAgentMessage.id > after_message_id,
            )
            .order_by(desc(SystemAgentMessage.id))
            .limit(1)
            .with_for_update()
        )
        message = result.scalar_one_or_none()
        if message is not None:
            row.user_message_id = message.id

    async def _converge_persisted_success(
        self,
        db: AsyncSession,
        row: SystemAgentRun,
        pending: SystemAgentPendingTurn | None,
        now: datetime,
    ) -> bool:
        """事件已确认成功时直接收敛，避免崩溃恢复后重复执行副作用。"""

        if row.last_seq <= 0:
            return False
        result = await db.execute(
            select(SystemAgentRunEvent.event).where(
                SystemAgentRunEvent.run_id == row.id,
                SystemAgentRunEvent.seq == row.last_seq,
            )
        )
        event = result.scalar_one_or_none()
        if not (
            isinstance(event, dict)
            and event.get("type") == "done"
            and event.get("ok") is True
        ):
            return False
        row.status = AGENT_RUN_SUCCEEDED
        row.phase = AGENT_RUN_SUCCEEDED
        row.error_code = None
        row.error_message = None
        row.finished_at = now
        row.claimed_by = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.updated_at = now
        if pending is not None:
            pending.status = PENDING_TURN_DISPATCHED
            pending.blocked_reason = AGENT_RUN_SUCCEEDED
            pending.updated_at = now
        return True

    async def _converge_committed_message_result(
        self,
        db: AsyncSession,
        row: SystemAgentRun,
        pending: SystemAgentPendingTurn | None,
        now: datetime,
    ) -> bool:
        """业务消息已提交成功时补齐事件，避免恢复后重复调用模型。"""

        if row.user_message_id is None:
            return False
        user_message = await db.get(SystemAgentMessage, row.user_message_id)
        if (
            user_message is None
            or user_message.session_id != row.session_id
            or user_message.run_status != MESSAGE_RUN_SUCCEEDED
        ):
            return False
        result = await db.execute(
            select(SystemAgentMessage)
            .where(
                SystemAgentMessage.session_id == row.session_id,
                SystemAgentMessage.role == MESSAGE_ROLE_ASSISTANT,
                SystemAgentMessage.run_status == MESSAGE_RUN_COMPLETED,
                SystemAgentMessage.id > user_message.id,
            )
            .order_by(desc(SystemAgentMessage.id))
        )
        assistant_message = next(
            (
                message
                for message in result.scalars()
                if isinstance(message.usage, dict)
                and str(message.usage.get("run_id") or "") == row.id
            ),
            None,
        )
        if assistant_message is None:
            return False
        event_result = await db.execute(
            select(SystemAgentRunEvent.event)
            .where(SystemAgentRunEvent.run_id == row.id)
            .order_by(asc(SystemAgentRunEvent.seq))
        )
        event_types = {
            str(event.get("type") or "")
            for event in event_result.scalars()
            if isinstance(event, dict)
        }
        if "assistant_message" not in event_types:
            content = (
                assistant_message.content
                if isinstance(assistant_message.content, dict)
                else {}
            )
            row.last_seq += 1
            recovered_event = {
                "type": "assistant_message",
                "content": str(content.get("text") or ""),
                "usage": redact_content(assistant_message.usage or {}),
                "run_id": row.id,
                "session_id": row.session_id,
                "seq": row.last_seq,
                "recovered": True,
            }
            reasoning = str(content.get("reasoning") or "")
            if reasoning:
                recovered_event["reasoning"] = reasoning
            db.add(
                SystemAgentRunEvent(
                    run_id=row.id,
                    seq=row.last_seq,
                    event=recovered_event,
                )
            )
        row.last_seq += 1
        db.add(
            SystemAgentRunEvent(
                run_id=row.id,
                seq=row.last_seq,
                event={
                    "type": "done",
                    "ok": True,
                    "run_id": row.id,
                    "session_id": row.session_id,
                    "seq": row.last_seq,
                    "recovered": True,
                },
            )
        )
        row.status = AGENT_RUN_SUCCEEDED
        row.phase = AGENT_RUN_SUCCEEDED
        row.error_code = None
        row.error_message = None
        row.finished_at = now
        row.claimed_by = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.updated_at = now
        if pending is not None:
            pending.status = PENDING_TURN_DISPATCHED
            pending.blocked_reason = AGENT_RUN_SUCCEEDED
            pending.updated_at = now
        return True

    async def _prepare_interrupted_message_retry(
        self,
        db: AsyncSession,
        row: SystemAgentRun,
        pending: SystemAgentPendingTurn | None,
    ) -> None:
        """将已落库但未完成的首次消息改成原位重试，防止恢复时重复追加。"""

        if row.user_message_id is None or row.kind == AGENT_RUN_KIND_REGENERATE:
            return
        message = await db.get(
            SystemAgentMessage,
            row.user_message_id,
            with_for_update=True,
        )
        if message is None or message.session_id != row.session_id:
            return
        if message.run_status == MESSAGE_RUN_PENDING:
            message.run_status = MESSAGE_RUN_FAILED
            message.error_code = "AGENT_STREAM_INTERRUPTED"
            message.error_message = "运行进程中断，正在恢复本轮。"
        if message.run_status != MESSAGE_RUN_FAILED or pending is None:
            return
        payload = dict(pending.request_payload or {})
        payload["retry_message_id"] = message.id
        payload["regenerate_message_id"] = None
        payload["regenerate_assistant_message_id"] = None
        pending.kind = AGENT_RUN_KIND_RETRY
        pending.request_payload = payload
        row.kind = AGENT_RUN_KIND_RETRY

    async def start_run(
        self,
        *,
        session_id: str,
        web_user_id: int | None,
        client_request_id: str,
        text: str,
        account_id: int | None = None,
        bot_tg_user_id: int | None = None,
        channel: str = CHANNEL_WEB,
        role: str = "admin",
        retry_message_id: int | None = None,
        regenerate_message_id: int | None = None,
        regenerate_assistant_message_id: int | None = None,
        fallback_provider_id: int | None = None,
        approved_tools: list[str] | None = None,
        model_selection: dict[str, Any] | None = None,
        read_only_only: bool = False,
        priority: bool = False,
    ) -> SystemAgentRun:
        """持久化一条待处理输入；同会话已有 Run 时进入队列而不是返回 409。"""

        await self.ensure_ready()
        regenerating = (
            regenerate_message_id is not None
            or regenerate_assistant_message_id is not None
        )
        if regenerating and (
            regenerate_message_id is None or regenerate_assistant_message_id is None
        ):
            raise RunConflictError("原位重新生成需要同时指定用户消息和助手消息")
        if regenerating and retry_message_id is not None:
            raise RunConflictError("失败重试与原位重新生成不能同时执行")
        kind = (
            AGENT_RUN_KIND_REGENERATE
            if regenerating
            else AGENT_RUN_KIND_RETRY
            if retry_message_id is not None
            else AGENT_RUN_KIND_MESSAGE
        )
        clean_text = str(text or "").strip()
        if kind == AGENT_RUN_KIND_MESSAGE and not clean_text:
            raise RunConflictError("助手消息不能为空")
        normalized_role = str(role or "viewer").strip().lower() or "viewer"
        approved = tuple(str(item) for item in (approved_tools or []) if str(item))
        selection = dict(model_selection) if isinstance(model_selection, dict) else None
        request_hash = _request_hash(
            kind=kind,
            text=clean_text,
            channel=channel,
            role=normalized_role,
            account_id=account_id,
            web_user_id=web_user_id,
            bot_tg_user_id=bot_tg_user_id,
            retry_message_id=retry_message_id,
            regenerate_message_id=regenerate_message_id,
            regenerate_assistant_message_id=regenerate_assistant_message_id,
            fallback_provider_id=fallback_provider_id,
            approved_tools=approved,
            model_selection=selection,
            read_only_only=read_only_only,
        )
        run_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        should_dispatch = False

        async with self._session_factory() as db:
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(SystemAgentSession.id == session_id)
                .with_for_update()
            )
            session = session_result.scalar_one_or_none()
            if session is None:
                raise RunNotFoundError(f"session:{session_id}")
            _validate_session_owner(
                session,
                channel=channel,
                web_user_id=web_user_id,
                bot_tg_user_id=bot_tg_user_id,
                account_id=account_id,
            )
            existing = await self._find_by_request(
                db,
                session_id=session_id,
                client_request_id=client_request_id,
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise RunConflictError("同一个请求标识不能用于不同的助手输入")
                return existing

            queued_count = int(
                (
                    await db.execute(
                        select(func.count(SystemAgentPendingTurn.id)).where(
                            SystemAgentPendingTurn.session_id == session_id,
                            SystemAgentPendingTurn.status.in_(
                                {PENDING_TURN_PENDING, PENDING_TURN_PAUSED}
                            ),
                        )
                    )
                ).scalar_one()
                or 0
            )
            if queued_count >= self._queue_limit:
                raise RunConflictError(
                    f"当前会话最多排队 {self._queue_limit} 条消息，请先处理或删除部分任务"
                )

            open_result = await db.execute(
                select(SystemAgentRun)
                .where(
                    SystemAgentRun.session_id == session_id,
                    SystemAgentRun.status.in_(AGENT_RUN_OPEN_STATUSES),
                )
                .order_by(asc(SystemAgentRun.created_at), asc(SystemAgentRun.id))
            )
            open_runs = list(open_result.scalars())
            active = next(
                (
                    row
                    for row in open_runs
                    if row.status
                    in {
                        AGENT_RUN_RUNNING,
                        AGENT_RUN_WAITING_INPUT,
                        AGENT_RUN_WAITING_APPROVAL,
                    }
                ),
                None,
            )
            if (regenerating or retry_message_id is not None) and open_runs:
                raise RunConflictError("重试或重新生成需等待当前会话队列处理完成")

            if regenerating:
                pair_is_valid = await is_latest_completed_pair(
                    db,
                    session_id=session_id,
                    user_message_id=int(regenerate_message_id),
                    assistant_message_id=int(regenerate_assistant_message_id),
                )
                if not pair_is_valid:
                    raise RunConflictError("只能编辑或重新生成当前会话最新完成的一轮")

            after_message_id = 0
            if retry_message_id is None and not regenerating:
                latest_result = await db.execute(
                    select(SystemAgentMessage.id)
                    .where(
                        SystemAgentMessage.session_id == session_id,
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                    )
                    .order_by(desc(SystemAgentMessage.id))
                    .limit(1)
                )
                after_message_id = latest_result.scalar_one_or_none() or 0

            if (
                channel == CHANNEL_WEB
                and account_id is not None
                and session.account_id != account_id
            ):
                session.account_id = account_id

            positions = await db.execute(
                select(
                    func.min(SystemAgentPendingTurn.position),
                    func.max(SystemAgentPendingTurn.position),
                ).where(SystemAgentPendingTurn.session_id == session_id)
            )
            min_position, max_position = positions.one()
            position = (
                int(min_position or 0) - 1
                if priority
                else int(max_position or 0) + 1
            )
            paused = active is not None and active.status in {
                AGENT_RUN_WAITING_INPUT,
                AGENT_RUN_WAITING_APPROVAL,
            }
            pending = SystemAgentPendingTurn(
                id=turn_id,
                session_id=session_id,
                web_user_id=web_user_id,
                bot_tg_user_id=bot_tg_user_id,
                account_id=account_id,
                channel=channel,
                kind=kind,
                position=position,
                status=PENDING_TURN_PAUSED if paused else PENDING_TURN_PENDING,
                blocked_reason=active.status if paused and active is not None else None,
                client_request_id=client_request_id,
                request_hash=request_hash,
                content_enc=encrypt_str(clean_text),
                request_payload={
                    "retry_message_id": retry_message_id,
                    "regenerate_message_id": regenerate_message_id,
                    "regenerate_assistant_message_id": regenerate_assistant_message_id,
                    "fallback_provider_id": fallback_provider_id,
                    "approved_tools": list(approved),
                    "after_message_id": after_message_id,
                    "model_selection": selection,
                    "read_only_only": bool(read_only_only),
                    "role": normalized_role,
                },
                dispatch_run_id=run_id,
            )
            db.add(pending)
            await db.flush()
            row = SystemAgentRun(
                id=run_id,
                session_id=session_id,
                web_user_id=web_user_id,
                bot_tg_user_id=bot_tg_user_id,
                channel=channel,
                pending_turn_id=turn_id,
                user_message_id=regenerate_message_id or retry_message_id,
                client_request_id=client_request_id,
                request_hash=request_hash,
                kind=kind,
                status=AGENT_RUN_QUEUED,
                phase="paused" if paused else "queued",
                paused_reason=active.status if paused and active is not None else None,
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
                return existing
            await db.refresh(row)
            should_dispatch = active is None and not paused

        if should_dispatch:
            self._kick_session(session_id)
        return row

    async def get_run(self, run_id: str) -> SystemAgentRun:
        await self.ensure_ready()
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            if row is None:
                raise RunNotFoundError(run_id)
            return row

    async def list_runs(
        self,
        *,
        web_user_id: int,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        include_bot: bool = False,
    ) -> list[SystemAgentRun]:
        await self.ensure_ready()
        owner_condition = (
            or_(
                SystemAgentRun.web_user_id == web_user_id,
                SystemAgentRun.channel == CHANNEL_BOT,
            )
            if include_bot
            else SystemAgentRun.web_user_id == web_user_id
        )
        conditions = [owner_condition]
        if status:
            conditions.append(SystemAgentRun.status == status)
        if since is not None:
            conditions.append(SystemAgentRun.created_at >= since)
        if until is not None:
            conditions.append(SystemAgentRun.created_at <= until)
        async with self._session_factory() as db:
            result = await db.execute(
                select(SystemAgentRun)
                .where(*conditions)
                .order_by(desc(SystemAgentRun.created_at), desc(SystemAgentRun.id))
                .limit(max(1, min(int(limit), 500)))
            )
            return list(result.scalars())

    async def list_queue(
        self,
        *,
        web_user_id: int,
        session_id: str | None = None,
        include_bot: bool = False,
    ) -> list[dict[str, Any]]:
        """返回当前用户的待处理队列；只在鉴权后短暂解密正文。"""

        await self.ensure_ready()
        owner_condition = (
            or_(
                SystemAgentPendingTurn.web_user_id == web_user_id,
                SystemAgentPendingTurn.channel == CHANNEL_BOT,
            )
            if include_bot
            else SystemAgentPendingTurn.web_user_id == web_user_id
        )
        conditions = [
            owner_condition,
            SystemAgentPendingTurn.status.in_(
                {
                    PENDING_TURN_PENDING,
                    PENDING_TURN_DISPATCHING,
                    PENDING_TURN_PAUSED,
                }
            ),
        ]
        if session_id is not None:
            conditions.append(SystemAgentPendingTurn.session_id == session_id)
        async with self._session_factory() as db:
            result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(*conditions)
                .order_by(
                    asc(SystemAgentPendingTurn.session_id),
                    asc(SystemAgentPendingTurn.position),
                    asc(SystemAgentPendingTurn.created_at),
                )
            )
            return [_pending_turn_dict(row) for row in result.scalars()]

    async def update_queue_item(
        self,
        turn_id: str,
        *,
        web_user_id: int,
        content: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        async with self._session_factory() as db:
            snapshot_result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.id == turn_id,
                    SystemAgentPendingTurn.web_user_id == web_user_id,
                )
            )
            snapshot = snapshot_result.scalar_one_or_none()
            if snapshot is None:
                raise RunNotFoundError(f"queue:{turn_id}")
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(
                    SystemAgentSession.id == snapshot.session_id,
                    SystemAgentSession.web_user_id == web_user_id,
                )
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                raise RunNotFoundError(f"queue:{turn_id}")
            result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.id == turn_id,
                    SystemAgentPendingTurn.web_user_id == web_user_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            row = result.scalar_one_or_none()
            if row is None or row.status not in {
                PENDING_TURN_PENDING,
                PENDING_TURN_PAUSED,
            }:
                raise RunNotFoundError(f"queue:{turn_id}")
            if content is not None:
                text = str(content).strip()
                if not text:
                    raise RunConflictError("排队消息不能为空")
                payload = dict(row.request_payload or {})
                approved = tuple(
                    str(item) for item in payload.get("approved_tools", []) if str(item)
                )
                request_hash = _request_hash(
                    kind=row.kind,
                    text=text,
                    channel=row.channel,
                    role=str(payload.get("role") or "viewer"),
                    account_id=row.account_id,
                    web_user_id=row.web_user_id,
                    bot_tg_user_id=row.bot_tg_user_id,
                    retry_message_id=_optional_int(payload.get("retry_message_id")),
                    regenerate_message_id=_optional_int(
                        payload.get("regenerate_message_id")
                    ),
                    regenerate_assistant_message_id=_optional_int(
                        payload.get("regenerate_assistant_message_id")
                    ),
                    fallback_provider_id=_optional_int(
                        payload.get("fallback_provider_id")
                    ),
                    approved_tools=approved,
                    model_selection=(
                        payload.get("model_selection")
                        if isinstance(payload.get("model_selection"), dict)
                        else None
                    ),
                    read_only_only=bool(payload.get("read_only_only")),
                )
                row.content_enc = encrypt_str(text)
                row.request_hash = request_hash
                run = await db.get(SystemAgentRun, row.dispatch_run_id)
                if run is not None:
                    run.request_hash = request_hash
                    run.updated_at = datetime.now(UTC)
            if pinned is not None:
                positions = await db.execute(
                    select(
                        func.min(SystemAgentPendingTurn.position),
                        func.max(SystemAgentPendingTurn.position),
                    ).where(SystemAgentPendingTurn.session_id == row.session_id)
                )
                minimum, maximum = positions.one()
                row.position = (
                    int(minimum or 0) - 1
                    if pinned
                    else int(maximum or 0) + 1
                )
            row.updated_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(row)
            return _pending_turn_dict(row)

    async def delete_queue_item(
        self,
        turn_id: str,
        *,
        web_user_id: int,
    ) -> SystemAgentRun:
        await self.ensure_ready()
        async with self._session_factory() as db:
            snapshot_result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.id == turn_id,
                    SystemAgentPendingTurn.web_user_id == web_user_id,
                )
            )
            snapshot = snapshot_result.scalar_one_or_none()
            if snapshot is None:
                raise RunNotFoundError(f"queue:{turn_id}")
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(
                    SystemAgentSession.id == snapshot.session_id,
                    SystemAgentSession.web_user_id == web_user_id,
                )
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                raise RunNotFoundError(f"queue:{turn_id}")
            result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.id == turn_id,
                    SystemAgentPendingTurn.web_user_id == web_user_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            pending = result.scalar_one_or_none()
            if pending is None or pending.status not in {
                PENDING_TURN_PENDING,
                PENDING_TURN_PAUSED,
            }:
                raise RunNotFoundError(f"queue:{turn_id}")
            run = await db.get(SystemAgentRun, pending.dispatch_run_id)
            if run is None:
                raise RunNotFoundError(f"run:{pending.dispatch_run_id}")
            now = datetime.now(UTC)
            pending.status = PENDING_TURN_CANCELLED
            pending.blocked_reason = "deleted"
            pending.updated_at = now
            run.status = AGENT_RUN_CANCELLED
            run.phase = "cancelled"
            run.error_code = "AGENT_QUEUE_ITEM_DELETED"
            run.error_message = "排队消息已删除。"
            run.finished_at = now
            run.updated_at = now
            await db.commit()
            await db.refresh(run)
            return run

    async def reorder_queue(
        self,
        *,
        session_id: str,
        web_user_id: int,
        turn_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        await self.ensure_ready()
        async with self._session_factory() as db:
            session = await db.execute(
                select(SystemAgentSession)
                .where(
                    SystemAgentSession.id == session_id,
                    SystemAgentSession.web_user_id == web_user_id,
                )
                .with_for_update()
            )
            if session.scalar_one_or_none() is None:
                raise RunNotFoundError(f"session:{session_id}")
            result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.session_id == session_id,
                    SystemAgentPendingTurn.status.in_(
                        {PENDING_TURN_PENDING, PENDING_TURN_PAUSED}
                    ),
                )
                .order_by(asc(SystemAgentPendingTurn.position))
                .with_for_update()
            )
            rows = list(result.scalars())
            by_id = {row.id: row for row in rows}
            requested = [str(item) for item in turn_ids]
            if len(requested) != len(set(requested)) or any(
                item not in by_id for item in requested
            ):
                raise RunConflictError("队列排序包含重复或不存在的项目")
            ordered = [by_id[item] for item in requested]
            ordered.extend(row for row in rows if row.id not in set(requested))
            now = datetime.now(UTC)
            for position, row in enumerate(ordered, start=1):
                row.position = position
                row.updated_at = now
            await db.commit()
            return [_pending_turn_dict(row) for row in ordered]

    async def clear_queue(
        self,
        *,
        session_id: str,
        web_user_id: int,
    ) -> int:
        """在单事务内清空可编辑队列项，避免并发操作留下半清状态。"""

        await self.ensure_ready()
        async with self._session_factory() as db:
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(
                    SystemAgentSession.id == session_id,
                    SystemAgentSession.web_user_id == web_user_id,
                )
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                raise RunNotFoundError(f"session:{session_id}")
            pending_result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.session_id == session_id,
                    SystemAgentPendingTurn.web_user_id == web_user_id,
                    SystemAgentPendingTurn.status.in_(
                        {PENDING_TURN_PENDING, PENDING_TURN_PAUSED}
                    ),
                )
                .with_for_update()
            )
            rows = list(pending_result.scalars())
            now = datetime.now(UTC)
            for pending in rows:
                pending.status = PENDING_TURN_CANCELLED
                pending.blocked_reason = "cleared"
                pending.updated_at = now
                run = await db.get(
                    SystemAgentRun,
                    pending.dispatch_run_id,
                    with_for_update=True,
                )
                if run is None or run.status != AGENT_RUN_QUEUED:
                    continue
                run.status = AGENT_RUN_CANCELLED
                run.phase = "cancelled"
                run.error_code = "AGENT_QUEUE_CLEARED"
                run.error_message = "排队消息已清空。"
                run.finished_at = now
                run.updated_at = now
            await db.commit()
            return len(rows)

    async def resume_queue(
        self,
        *,
        session_id: str,
        web_user_id: int,
    ) -> int:
        await self.ensure_ready()
        async with self._session_factory() as db:
            result = await db.execute(
                select(SystemAgentSession)
                .where(
                    SystemAgentSession.id == session_id,
                    SystemAgentSession.web_user_id == web_user_id,
                )
                .with_for_update()
            )
            if result.scalar_one_or_none() is None:
                raise RunNotFoundError(f"session:{session_id}")
            pending_result = await db.execute(
                select(SystemAgentPendingTurn).where(
                    SystemAgentPendingTurn.session_id == session_id,
                    SystemAgentPendingTurn.status == PENDING_TURN_PAUSED,
                ).with_for_update()
            )
            rows = list(pending_result.scalars())
            now = datetime.now(UTC)
            for pending in rows:
                pending.status = PENDING_TURN_PENDING
                pending.blocked_reason = None
                pending.updated_at = now
                run = await db.get(SystemAgentRun, pending.dispatch_run_id)
                if run is not None and run.status == AGENT_RUN_QUEUED:
                    run.phase = "queued"
                    run.paused_reason = None
                    run.updated_at = now
            await db.commit()
        if rows:
            self._kick_session(session_id)
        return len(rows)

    async def resume_bot_queue(
        self,
        *,
        session_id: str,
        bot_tg_user_id: int,
        account_id: int,
    ) -> int:
        """恢复 Bot 会话中因失败、取消或等待状态而暂停的后续任务。"""

        await self.ensure_ready()
        async with self._session_factory() as db:
            session = await db.get(
                SystemAgentSession,
                session_id,
                with_for_update=True,
            )
            if session is None:
                raise RunNotFoundError(f"session:{session_id}")
            _validate_session_owner(
                session,
                channel=CHANNEL_BOT,
                web_user_id=None,
                bot_tg_user_id=bot_tg_user_id,
                account_id=account_id,
            )
            pending_result = await db.execute(
                select(SystemAgentPendingTurn).where(
                    SystemAgentPendingTurn.session_id == session_id,
                    SystemAgentPendingTurn.status == PENDING_TURN_PAUSED,
                ).with_for_update()
            )
            rows = list(pending_result.scalars())
            now = datetime.now(UTC)
            for pending in rows:
                pending.status = PENDING_TURN_PENDING
                pending.blocked_reason = None
                pending.updated_at = now
                run = await db.get(SystemAgentRun, pending.dispatch_run_id)
                if run is not None and run.status == AGENT_RUN_QUEUED:
                    run.phase = "queued"
                    run.paused_reason = None
                    run.updated_at = now
            await db.commit()
        if rows:
            self._kick_session(session_id)
        return len(rows)

    async def get_active_run_for_bot(
        self,
        *,
        session_id: str,
        bot_tg_user_id: int,
        account_id: int,
    ) -> SystemAgentRun | None:
        """返回 Bot 会话当前正在执行或等待恢复的 Run。"""

        await self.ensure_ready()
        async with self._session_factory() as db:
            session = await db.get(SystemAgentSession, session_id)
            if session is None:
                raise RunNotFoundError(f"session:{session_id}")
            _validate_session_owner(
                session,
                channel=CHANNEL_BOT,
                web_user_id=None,
                bot_tg_user_id=bot_tg_user_id,
                account_id=account_id,
            )
            result = await db.execute(
                select(SystemAgentRun)
                .where(
                    SystemAgentRun.session_id == session_id,
                    SystemAgentRun.bot_tg_user_id == bot_tg_user_id,
                    SystemAgentRun.channel == CHANNEL_BOT,
                    SystemAgentRun.status.in_(
                        {
                            AGENT_RUN_RUNNING,
                            AGENT_RUN_WAITING_INPUT,
                            AGENT_RUN_WAITING_APPROVAL,
                        }
                    ),
                )
                .order_by(asc(SystemAgentRun.created_at), asc(SystemAgentRun.id))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_queue_position(self, run_id: str) -> int | None:
        """返回 Run 在等待队列中的 1-based 位置；已开始或可立即执行时返回 None。"""

        await self.ensure_ready()
        async with self._session_factory() as db:
            run = await db.get(SystemAgentRun, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            if run.status != AGENT_RUN_QUEUED or run.pending_turn_id is None:
                return None
            pending = await db.get(SystemAgentPendingTurn, run.pending_turn_id)
            if pending is None or pending.status not in {
                PENDING_TURN_PENDING,
                PENDING_TURN_PAUSED,
            }:
                return None
            active_result = await db.execute(
                select(SystemAgentRun.id)
                .where(
                    SystemAgentRun.session_id == run.session_id,
                    SystemAgentRun.id != run.id,
                    SystemAgentRun.status.in_(
                        {
                            AGENT_RUN_RUNNING,
                            AGENT_RUN_WAITING_INPUT,
                            AGENT_RUN_WAITING_APPROVAL,
                        }
                    ),
                )
                .limit(1)
            )
            earlier_count = int(
                (
                    await db.execute(
                        select(func.count(SystemAgentPendingTurn.id)).where(
                            SystemAgentPendingTurn.session_id == run.session_id,
                            SystemAgentPendingTurn.status.in_(
                                {
                                    PENDING_TURN_PENDING,
                                    PENDING_TURN_PAUSED,
                                }
                            ),
                            SystemAgentPendingTurn.id != pending.id,
                            SystemAgentPendingTurn.position < pending.position,
                        )
                    )
                ).scalar_one()
                or 0
            )
            has_active = active_result.scalar_one_or_none() is not None
            if not has_active and earlier_count == 0:
                return None
            return earlier_count + 1

    async def add_run_input(
        self,
        run_id: str,
        *,
        kind: str,
        client_request_id: str,
        payload: dict[str, Any],
    ) -> SystemAgentRunInput:
        """写入 steer / waiting_input / waiting_approval 收件箱并保证幂等。"""

        await self.ensure_ready()
        if kind not in {RUN_INPUT_STEER, RUN_INPUT_USER, RUN_INPUT_APPROVAL}:
            raise RunConflictError("不支持的运行输入类型")
        payload = dict(payload or {})
        content = str(payload.get("content") or "").strip()
        if kind == RUN_INPUT_STEER and not content:
            raise RunConflictError("Steer 内容不能为空")
        if kind == RUN_INPUT_USER and not content and payload.get("fallback_provider_id") is None:
            raise RunConflictError("请提供补充说明或备用模型供应商")
        if kind == RUN_INPUT_APPROVAL:
            approved_tools = [
                str(item).strip()
                for item in payload.get("approved_tools", [])
                if str(item).strip()
            ]
            approved = payload.get("approved") is not False
            if approved and not approved_tools:
                raise RunConflictError("请选择要批准的工具，或明确拒绝本次调用")
            payload["approved"] = approved
            payload["approved_tools"] = approved_tools
        if "content" in payload:
            payload["content"] = content
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        def ensure_same_request(existing: SystemAgentRunInput) -> None:
            try:
                existing_payload = json.loads(decrypt_str(existing.payload_enc))
                existing_canonical = json.dumps(
                    existing_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (ValueError, json.JSONDecodeError):
                existing_canonical = ""
            if existing.kind != kind or existing_canonical != canonical_payload:
                raise RunConflictError("同一 client_request_id 不能提交不同的运行输入")

        should_dispatch = False
        async with self._session_factory() as db:
            snapshot = await db.get(SystemAgentRun, run_id)
            if snapshot is None:
                raise RunNotFoundError(run_id)
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(SystemAgentSession.id == snapshot.session_id)
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                raise RunNotFoundError(run_id)
            run = await db.get(
                SystemAgentRun,
                run_id,
                with_for_update=True,
                populate_existing=True,
            )
            if run is None:
                raise RunNotFoundError(run_id)
            existing_result = await db.execute(
                select(SystemAgentRunInput).where(
                    SystemAgentRunInput.run_id == run_id,
                    SystemAgentRunInput.client_request_id == client_request_id,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing is not None:
                ensure_same_request(existing)
                return existing
            if kind == RUN_INPUT_STEER and run.status != AGENT_RUN_RUNNING:
                raise RunConflictError("只有正在运行的任务可以 Steer")
            expected_waiting_status = {
                RUN_INPUT_USER: AGENT_RUN_WAITING_INPUT,
                RUN_INPUT_APPROVAL: AGENT_RUN_WAITING_APPROVAL,
            }.get(kind)
            if expected_waiting_status is not None and run.status != expected_waiting_status:
                raise RunConflictError("当前任务没有等待补充输入或审批")
            row = SystemAgentRunInput(
                run_id=run_id,
                kind=kind,
                payload_enc=encrypt_str(canonical_payload),
                status=RUN_INPUT_PENDING,
                client_request_id=client_request_id,
            )
            db.add(row)
            await db.flush()
            if kind != RUN_INPUT_STEER:
                await self._apply_waiting_input(
                    db,
                    run=run,
                    item=row,
                    payload=payload,
                )
                should_dispatch = run.status == AGENT_RUN_QUEUED
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                result = await db.execute(
                    select(SystemAgentRunInput).where(
                        SystemAgentRunInput.run_id == run_id,
                        SystemAgentRunInput.client_request_id == client_request_id,
                    )
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    raise
                ensure_same_request(existing)
                return existing
            await db.refresh(row)
        if should_dispatch:
            self._kick_session(snapshot.session_id)
        return row

    async def stop_and_replace(
        self,
        run_id: str,
        *,
        web_user_id: int,
        client_request_id: str,
        text: str,
        model_selection: dict[str, Any] | None = None,
    ) -> SystemAgentRun:
        """原子地请求停止当前 Run，并将替代消息插到同会话队首。"""

        await self.ensure_ready()
        clean_text = str(text or "").strip()
        if not clean_text:
            raise RunConflictError("替代消息不能为空")
        selection = dict(model_selection) if isinstance(model_selection, dict) else None
        replacement_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        session_id = ""
        current_was_running = False

        async with self._session_factory() as db:
            current_snapshot = await db.get(SystemAgentRun, run_id)
            if current_snapshot is None or current_snapshot.web_user_id != web_user_id:
                raise RunNotFoundError(run_id)
            session_id = current_snapshot.session_id
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(
                    SystemAgentSession.id == session_id,
                    SystemAgentSession.web_user_id == web_user_id,
                    SystemAgentSession.channel == CHANNEL_WEB,
                )
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                raise RunNotFoundError(run_id)
            current = await db.get(
                SystemAgentRun,
                run_id,
                with_for_update=True,
                populate_existing=True,
            )
            if current is None or current.web_user_id != web_user_id:
                raise RunNotFoundError(run_id)

            request_hash = _request_hash(
                kind=AGENT_RUN_KIND_MESSAGE,
                text=clean_text,
                channel=current.channel,
                role="admin",
                account_id=None,
                web_user_id=web_user_id,
                bot_tg_user_id=None,
                retry_message_id=None,
                regenerate_message_id=None,
                regenerate_assistant_message_id=None,
                fallback_provider_id=None,
                approved_tools=(),
                model_selection=selection,
                read_only_only=False,
            )
            existing = await self._find_by_request(
                db,
                session_id=session_id,
                client_request_id=client_request_id,
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise RunConflictError("同一个请求标识不能用于不同的助手输入")
                replacement = existing
            else:
                if current.status not in {
                    AGENT_RUN_RUNNING,
                    AGENT_RUN_WAITING_INPUT,
                    AGENT_RUN_WAITING_APPROVAL,
                }:
                    raise RunConflictError("当前任务已结束，不能停止并替换")
                if current.cancel_requested:
                    raise RunConflictError("当前任务已在停止，不能重复创建替代任务")
                queued_count = int(
                    (
                        await db.execute(
                            select(func.count(SystemAgentPendingTurn.id)).where(
                                SystemAgentPendingTurn.session_id == session_id,
                                SystemAgentPendingTurn.status.in_(
                                    {PENDING_TURN_PENDING, PENDING_TURN_PAUSED}
                                ),
                            )
                        )
                    ).scalar_one()
                    or 0
                )
                if queued_count >= self._queue_limit:
                    raise RunConflictError(
                        f"当前会话最多排队 {self._queue_limit} 条消息，请先处理或删除部分任务"
                    )
                latest_result = await db.execute(
                    select(SystemAgentMessage.id)
                    .where(
                        SystemAgentMessage.session_id == session_id,
                        SystemAgentMessage.role == MESSAGE_ROLE_USER,
                    )
                    .order_by(desc(SystemAgentMessage.id))
                    .limit(1)
                )
                after_message_id = latest_result.scalar_one_or_none() or 0
                minimum_result = await db.execute(
                    select(func.min(SystemAgentPendingTurn.position)).where(
                        SystemAgentPendingTurn.session_id == session_id
                    )
                )
                position = int(minimum_result.scalar_one_or_none() or 0) - 1
                pending = SystemAgentPendingTurn(
                    id=turn_id,
                    session_id=session_id,
                    web_user_id=web_user_id,
                    bot_tg_user_id=None,
                    account_id=None,
                    channel=current.channel,
                    kind=AGENT_RUN_KIND_MESSAGE,
                    position=position,
                    status=PENDING_TURN_PENDING,
                    blocked_reason=None,
                    client_request_id=client_request_id,
                    request_hash=request_hash,
                    content_enc=encrypt_str(clean_text),
                    request_payload={
                        "retry_message_id": None,
                        "regenerate_message_id": None,
                        "regenerate_assistant_message_id": None,
                        "fallback_provider_id": None,
                        "approved_tools": [],
                        "after_message_id": after_message_id,
                        "model_selection": selection,
                        "read_only_only": False,
                        "role": "admin",
                    },
                    dispatch_run_id=replacement_id,
                )
                db.add(pending)
                await db.flush()
                replacement = SystemAgentRun(
                    id=replacement_id,
                    session_id=session_id,
                    web_user_id=web_user_id,
                    bot_tg_user_id=None,
                    channel=current.channel,
                    pending_turn_id=turn_id,
                    client_request_id=client_request_id,
                    request_hash=request_hash,
                    kind=AGENT_RUN_KIND_MESSAGE,
                    status=AGENT_RUN_QUEUED,
                    phase="queued",
                )
                db.add(replacement)

            if current.status in {
                AGENT_RUN_RUNNING,
                AGENT_RUN_WAITING_INPUT,
                AGENT_RUN_WAITING_APPROVAL,
            }:
                current_was_running = current.status == AGENT_RUN_RUNNING
                current.paused_reason = "stop_replace"
                current.cancel_requested = True
                current.updated_at = datetime.now(UTC)
                if not current_was_running:
                    await self._cancel_without_worker(db, current)
                    current.paused_reason = "stop_replace"
                    await self._set_following_queue_state(
                        db,
                        session_id=current.session_id,
                        resume=True,
                        reason=None,
                    )

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
                replacement = existing
            await db.refresh(replacement)

        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()
        if not current_was_running:
            self._kick_session(session_id)
        return replacement

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
        cursor = max(0, after_seq)
        while True:
            rows = await self.list_events(run_id, after_seq=cursor)
            for row in rows:
                cursor = row.seq
                yield dict(row.event or {})
            run = await self.get_run(run_id)
            if run.status in AGENT_RUN_STREAM_FINAL_STATUSES and cursor >= run.last_seq:
                return
            await asyncio.sleep(self._poll_interval)

    async def cancel_run(self, run_id: str) -> SystemAgentRun:
        """取消 queued/waiting/running Run；远端 worker 由 heartbeat 观察请求。"""

        await self.ensure_ready()
        should_dispatch = False
        session_id = ""
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id, with_for_update=True)
            if row is None:
                raise RunNotFoundError(run_id)
            if row.status in AGENT_RUN_TERMINAL_STATUSES:
                return row
            session_id = row.session_id
            was_running = row.status == AGENT_RUN_RUNNING
            should_dispatch = row.status == AGENT_RUN_QUEUED
            row.cancel_requested = True
            row.updated_at = datetime.now(UTC)
            if not was_running:
                await self._cancel_without_worker(db, row)
            await db.commit()
            await db.refresh(row)

        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()
        if should_dispatch:
            self._kick_session(session_id)
        return row

    async def _cancel_without_worker(
        self,
        db: AsyncSession,
        row: SystemAgentRun,
    ) -> None:
        now = datetime.now(UTC)
        row.status = AGENT_RUN_CANCELLED
        row.phase = "cancelled"
        row.error_code = "AGENT_RUN_CANCELLED"
        row.error_message = "本轮请求已取消。"
        row.finished_at = now
        row.claimed_by = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        if row.pending_turn_id:
            pending = await db.get(SystemAgentPendingTurn, row.pending_turn_id)
            if pending is not None:
                pending.status = PENDING_TURN_CANCELLED
                pending.blocked_reason = "cancelled"
                pending.updated_at = now

    def _kick_session(self, session_id: str) -> None:
        current = self._dispatch_tasks.get(session_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._dispatch_session(session_id))
        self._dispatch_tasks[session_id] = task
        task.add_done_callback(
            lambda done, sid=session_id: self._dispatch_done(sid, done)
        )

    async def _dispatch_session(self, session_id: str) -> None:
        run_id: str | None = None
        request: _RunRequest | None = None
        cancel_event: asyncio.Event | None = None
        async with self._session_factory() as db:
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(SystemAgentSession.id == session_id)
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                return
            active_result = await db.execute(
                select(SystemAgentRun.id).where(
                    SystemAgentRun.session_id == session_id,
                    SystemAgentRun.status.in_(
                        {
                            AGENT_RUN_RUNNING,
                            AGENT_RUN_WAITING_INPUT,
                            AGENT_RUN_WAITING_APPROVAL,
                        }
                    ),
                )
            )
            if active_result.first() is not None:
                return
            pending_result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.session_id == session_id,
                    SystemAgentPendingTurn.status == PENDING_TURN_PENDING,
                )
                .order_by(
                    asc(SystemAgentPendingTurn.position),
                    asc(SystemAgentPendingTurn.created_at),
                )
                .limit(1)
                .with_for_update()
            )
            pending = pending_result.scalar_one_or_none()
            if pending is None:
                return
            run_result = await db.execute(
                select(SystemAgentRun)
                .where(
                    SystemAgentRun.id == pending.dispatch_run_id,
                    SystemAgentRun.status == AGENT_RUN_QUEUED,
                )
                .with_for_update()
            )
            run = run_result.scalar_one_or_none()
            if run is None:
                pending.status = PENDING_TURN_CANCELLED
                pending.blocked_reason = "run_missing"
                await db.commit()
                return
            now = datetime.now(UTC)
            pending.status = PENDING_TURN_DISPATCHING
            pending.blocked_reason = None
            pending.updated_at = now
            run.status = AGENT_RUN_RUNNING
            run.phase = "starting"
            run.paused_reason = None
            run.claimed_by = self._worker_id
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            run.started_at = run.started_at or now
            run.finished_at = None
            run.updated_at = now
            await db.commit()
            run_id = run.id
            request = _request_from_pending(pending)

        if run_id is None or request is None:
            return
        cancel_event = asyncio.Event()
        self._cancel_events[run_id] = cancel_event
        task = asyncio.create_task(self._execute(run_id, request, cancel_event))
        self._tasks[run_id] = task
        task.add_done_callback(lambda done, rid=run_id: self._task_done(rid, done))

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
        waiting_status: str | None = None
        heartbeat = asyncio.create_task(self._heartbeat(run_id, cancel_event))
        try:
            async with self._session_factory() as db:
                session = await db.get(SystemAgentSession, request.session_id)
                if session is None:
                    raise RunNotFoundError(f"session:{request.session_id}")
                retry_message = None
                if request.retry_message_id is not None:
                    retry_message = await db.get(
                        SystemAgentMessage,
                        request.retry_message_id,
                    )
                    if retry_message is None or retry_message.session_id != request.session_id:
                        raise RunNotFoundError(f"message:{request.retry_message_id}")
                regenerate_message = None
                regenerate_assistant_message = None
                if request.regenerate_message_id is not None:
                    regenerate_message = await db.get(
                        SystemAgentMessage,
                        request.regenerate_message_id,
                    )
                    regenerate_assistant_message = await db.get(
                        SystemAgentMessage,
                        request.regenerate_assistant_message_id,
                    )
                    if (
                        regenerate_message is None
                        or regenerate_assistant_message is None
                        or regenerate_message.session_id != request.session_id
                        or regenerate_assistant_message.session_id != request.session_id
                    ):
                        raise RunNotFoundError(
                            f"message:{request.regenerate_message_id}"
                        )

                stream = self._service_factory().stream_message(
                    db,
                    session=session,
                    text=request.text,
                    role=request.role,
                    channel=request.channel,
                    web_user_id=request.web_user_id,
                    bot_tg_user_id=request.bot_tg_user_id,
                    retry_message=retry_message,
                    regenerate_message=regenerate_message,
                    regenerate_assistant_message=regenerate_assistant_message,
                    fallback_provider_id=request.fallback_provider_id,
                    approved_tools=list(request.approved_tools),
                    run_id=run_id,
                    run_input_provider=lambda: self._consume_steers(run_id),
                    model_selection=request.model_selection,
                    read_only_only=request.read_only_only,
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
                        error_code = str(
                            persisted.get("code") or "AGENT_RUN_FAILED"
                        )[:64]
                        error_message = str(
                            persisted.get("message") or "助手运行失败"
                        )[:1024]
                        if error_code == "AGENT_TOOL_APPROVAL_REQUIRED":
                            waiting_status = AGENT_RUN_WAITING_APPROVAL
                        elif error_code == "AGENT_PROVIDER_SWITCH_REQUIRED":
                            waiting_status = AGENT_RUN_WAITING_INPUT
                    elif event_type == "assistant_message":
                        usage = (
                            persisted.get("usage")
                            if isinstance(persisted.get("usage"), dict)
                            else None
                        )
                        if usage is not None:
                            await self._update_usage(run_id, usage)
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
            if waiting_status is not None:
                await self._finish_run(
                    run_id,
                    status=waiting_status,
                    error_code=error_code,
                    error_message=error_message,
                )
                await self._pause_following(request.session_id, waiting_status)
            else:
                final_status = (
                    AGENT_RUN_SUCCEEDED
                    if done_seen and done_ok
                    else AGENT_RUN_FAILED
                )
                await self._finish_run(
                    run_id,
                    status=final_status,
                    error_code=(
                        None
                        if final_status == AGENT_RUN_SUCCEEDED
                        else error_code or "AGENT_RUN_FAILED"
                    ),
                    error_message=(
                        None
                        if final_status == AGENT_RUN_SUCCEEDED
                        else error_message or "助手运行失败"
                    ),
                )
                if final_status == AGENT_RUN_SUCCEEDED:
                    self._kick_session(request.session_id)
                else:
                    await self._pause_following(
                        request.session_id,
                        AGENT_RUN_FAILED,
                    )
        except _RunCancelled:
            if not await self._owns_worker_claim(run_id):
                log.info(
                    "system agent worker claim lost before cancellation handling "
                    "run=%s worker=%s",
                    run_id,
                    self._worker_id,
                )
                return
            await self._link_user_message(run_id, request)
            await self._append_terminal_events(
                run_id,
                code="AGENT_RUN_CANCELLED",
                message="本轮请求已取消。",
            )
            replace_requested = await self._is_stop_replace(run_id)
            await self._finish_run(
                run_id,
                status=AGENT_RUN_CANCELLED,
                error_code="AGENT_RUN_CANCELLED",
                error_message="本轮请求已取消。",
            )
            if replace_requested:
                await self._resume_all_pending(request.session_id)
                self._kick_session(request.session_id)
            else:
                await self._pause_following(
                    request.session_id,
                    AGENT_RUN_CANCELLED,
                )
        except asyncio.CancelledError:
            # 进程正常关闭时保留 queued+lease 恢复语义，不写伪失败终态。
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await self._requeue_claimed_run(run_id)
            raise
        except _WorkerLeaseLost:
            log.info(
                "system agent worker lease lost; stale execution stopped run=%s worker=%s",
                run_id,
                self._worker_id,
            )
        except RunNotFoundError:
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
            await self._pause_following(
                request.session_id,
                AGENT_RUN_FAILED,
            )
        finally:
            if not heartbeat.done():
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _heartbeat(
        self,
        run_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            async with self._session_factory() as db:
                row = await db.get(SystemAgentRun, run_id)
                if (
                    row is None
                    or row.status != AGENT_RUN_RUNNING
                    or row.claimed_by != self._worker_id
                ):
                    cancel_event.set()
                    return
                now = datetime.now(UTC)
                row.heartbeat_at = now
                row.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                row.updated_at = now
                should_cancel = bool(row.cancel_requested)
                await db.commit()
            if should_cancel:
                cancel_event.set()

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

    async def _consume_steers(self, run_id: str) -> list[str]:
        values: list[str] = []
        async with self._session_factory() as db:
            snapshot = await db.get(SystemAgentRun, run_id)
            if snapshot is None or snapshot.pending_turn_id is None:
                return values
            session_result = await db.execute(
                select(SystemAgentSession)
                .where(SystemAgentSession.id == snapshot.session_id)
                .with_for_update()
            )
            if session_result.scalar_one_or_none() is None:
                return values
            pending_result = await db.execute(
                select(SystemAgentPendingTurn)
                .where(
                    SystemAgentPendingTurn.id == snapshot.pending_turn_id,
                    SystemAgentPendingTurn.session_id == snapshot.session_id,
                )
                .with_for_update()
            )
            pending = pending_result.scalar_one_or_none()
            run_result = await db.execute(
                select(SystemAgentRun)
                .where(
                    SystemAgentRun.id == run_id,
                    SystemAgentRun.pending_turn_id == snapshot.pending_turn_id,
                    SystemAgentRun.status == AGENT_RUN_RUNNING,
                    SystemAgentRun.claimed_by == self._worker_id,
                )
                .with_for_update()
            )
            run = run_result.scalar_one_or_none()
            if pending is None or run is None:
                return values
            result = await db.execute(
                select(SystemAgentRunInput)
                .where(
                    SystemAgentRunInput.run_id == run_id,
                    SystemAgentRunInput.kind == RUN_INPUT_STEER,
                    SystemAgentRunInput.status == RUN_INPUT_PENDING,
                )
                .order_by(asc(SystemAgentRunInput.id))
                .with_for_update()
            )
            rows = list(result.scalars())
            now = datetime.now(UTC)
            for row in rows:
                try:
                    payload = json.loads(decrypt_str(row.payload_enc))
                except (ValueError, json.JSONDecodeError):
                    payload = {}
                text = str(payload.get("content") or "").strip()
                if text:
                    values.append(text)
                row.status = RUN_INPUT_APPLIED
                row.applied_at = now
            if rows:
                if values:
                    original = decrypt_str(pending.content_enc)
                    steer_context = "\n\n".join(
                        f"运行中调整：{value}" for value in values
                    )
                    pending.content_enc = encrypt_str(
                        f"{original}\n\n{steer_context}" if original else steer_context
                    )
                    pending.updated_at = now
                await db.commit()
        return values

    async def _apply_waiting_input(
        self,
        db: AsyncSession,
        *,
        run: SystemAgentRun,
        item: SystemAgentRunInput,
        payload: dict[str, Any],
    ) -> None:
        if run.pending_turn_id is None:
            raise RunNotFoundError(run.id)
        pending = await db.get(
            SystemAgentPendingTurn,
            run.pending_turn_id,
            with_for_update=True,
        )
        if pending is None:
            raise RunNotFoundError(f"queue:{run.pending_turn_id}")
        request_payload = dict(pending.request_payload or {})
        if item.kind == RUN_INPUT_APPROVAL:
            if payload.get("approved") is False:
                now = datetime.now(UTC)
                item.status = RUN_INPUT_APPLIED
                item.applied_at = now
                run.status = AGENT_RUN_CANCELLED
                run.phase = "cancelled"
                run.error_code = "AGENT_TOOL_APPROVAL_REJECTED"
                run.error_message = "用户拒绝了本次工具调用。"
                run.finished_at = now
                run.claimed_by = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                run.updated_at = now
                pending.status = PENDING_TURN_CANCELLED
                pending.blocked_reason = "approval_rejected"
                pending.updated_at = now
                return
            approved = [
                str(value)
                for value in payload.get("approved_tools", [])
                if str(value)
            ]
            request_payload["approved_tools"] = approved
        if payload.get("fallback_provider_id") is not None:
            request_payload["fallback_provider_id"] = int(
                payload["fallback_provider_id"]
            )
        content = str(payload.get("content") or "").strip()
        if content:
            original = decrypt_str(pending.content_enc)
            pending.content_enc = encrypt_str(
                f"{original}\n\n补充说明：{content}" if original else content
            )
        request_payload["retry_message_id"] = run.user_message_id
        request_payload["regenerate_message_id"] = None
        request_payload["regenerate_assistant_message_id"] = None
        pending.kind = AGENT_RUN_KIND_RETRY
        pending.request_payload = request_payload
        pending.status = PENDING_TURN_PENDING
        pending.blocked_reason = None
        now = datetime.now(UTC)
        pending.updated_at = now
        run.kind = AGENT_RUN_KIND_RETRY
        run.status = AGENT_RUN_QUEUED
        run.phase = "queued"
        run.paused_reason = None
        run.cancel_requested = False
        run.claimed_by = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        run.finished_at = None
        run.error_code = None
        run.error_message = None
        run.updated_at = now
        item.status = RUN_INPUT_APPLIED
        item.applied_at = now

    async def _link_user_message(
        self,
        run_id: str,
        request: _RunRequest,
    ) -> None:
        async with self._session_factory() as db:
            run = await db.get(SystemAgentRun, run_id, with_for_update=True)
            if run is None:
                raise RunNotFoundError(run_id)
            self._assert_worker_claim(run)
            if run.user_message_id is not None:
                return
            if (
                request.retry_message_id is not None
                or request.regenerate_message_id is not None
            ):
                run.user_message_id = (
                    request.retry_message_id or request.regenerate_message_id
                )
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
            self._assert_worker_claim(run)
            seq = run.last_seq + 1
            event = _redact_event(source, known_secrets)
            source_run_id = event.get("run_id")
            if source_run_id and source_run_id != run_id:
                event["runtime_run_id"] = source_run_id
            event["run_id"] = run_id
            event["session_id"] = run.session_id
            event["seq"] = seq
            event_type = str(event.get("type") or "")
            phase = _phase_from_event(event_type)
            if phase is not None:
                run.phase = phase
            db.add(SystemAgentRunEvent(run_id=run_id, seq=seq, event=event))
            run.last_seq = seq
            run.updated_at = datetime.now(UTC)
            await db.commit()
            return event

    async def _append_terminal_events(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        run = await self.get_run(run_id)
        rows = await self.list_events(
            run_id,
            after_seq=max(0, run.last_seq - 1),
            limit=2,
        )
        if any((row.event or {}).get("type") == "done" for row in rows):
            return
        if not any((row.event or {}).get("type") == "error" for row in rows):
            await self._append_event(
                run_id,
                {"type": "error", "code": code, "message": message},
            )
        await self._append_event(run_id, {"type": "done", "ok": False})

    async def _update_usage(self, run_id: str, usage: dict[str, Any]) -> None:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id, with_for_update=True)
            if row is None:
                raise RunNotFoundError(run_id)
            self._assert_worker_claim(row)
            row.usage = redact_content(usage)
            elapsed = usage.get("elapsed_ms")
            if isinstance(elapsed, (int, float)):
                row.elapsed_ms = max(0, int(elapsed))
            row.updated_at = datetime.now(UTC)
            await db.commit()

    async def _finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id, with_for_update=True)
            if row is None:
                raise RunNotFoundError(run_id)
            self._assert_worker_claim(row)
            now = datetime.now(UTC)
            row.status = status
            row.phase = (
                "waiting"
                if status in {AGENT_RUN_WAITING_INPUT, AGENT_RUN_WAITING_APPROVAL}
                else status
            )
            row.paused_reason = (
                error_code
                if status in {AGENT_RUN_WAITING_INPUT, AGENT_RUN_WAITING_APPROVAL}
                else row.paused_reason
            )
            row.error_code = error_code
            row.error_message = error_message
            if status in AGENT_RUN_TERMINAL_STATUSES:
                row.finished_at = now
            row.claimed_by = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            if row.started_at is not None:
                started = _as_utc(row.started_at)
                if started is not None:
                    row.elapsed_ms = max(
                        int(row.elapsed_ms or 0),
                        int((now - started).total_seconds() * 1000),
                    )
            row.updated_at = now
            if row.pending_turn_id:
                pending = await db.get(SystemAgentPendingTurn, row.pending_turn_id)
                if pending is not None:
                    pending.status = PENDING_TURN_DISPATCHED
                    pending.blocked_reason = status
                    pending.updated_at = now
            await db.commit()

    async def _pause_following(self, session_id: str, reason: str) -> None:
        async with self._session_factory() as db:
            session = await db.get(
                SystemAgentSession,
                session_id,
                with_for_update=True,
            )
            if session is None:
                return
            changed = await self._set_following_queue_state(
                db,
                session_id=session_id,
                resume=False,
                reason=reason,
            )
            if changed:
                await db.commit()

    async def _resume_all_pending(self, session_id: str) -> None:
        async with self._session_factory() as db:
            session = await db.get(
                SystemAgentSession,
                session_id,
                with_for_update=True,
            )
            if session is None:
                return
            changed = await self._set_following_queue_state(
                db,
                session_id=session_id,
                resume=True,
                reason=None,
            )
            if changed:
                await db.commit()

    async def _set_following_queue_state(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        resume: bool,
        reason: str | None,
    ) -> int:
        source_status = PENDING_TURN_PAUSED if resume else PENDING_TURN_PENDING
        target_status = PENDING_TURN_PENDING if resume else PENDING_TURN_PAUSED
        result = await db.execute(
            select(SystemAgentPendingTurn)
            .where(
                SystemAgentPendingTurn.session_id == session_id,
                SystemAgentPendingTurn.status == source_status,
            )
            .with_for_update()
        )
        rows = list(result.scalars())
        now = datetime.now(UTC)
        for pending in rows:
            pending.status = target_status
            pending.blocked_reason = reason
            pending.updated_at = now
            run = await db.get(SystemAgentRun, pending.dispatch_run_id)
            if run is not None and run.status == AGENT_RUN_QUEUED:
                run.phase = "queued" if resume else "paused"
                run.paused_reason = reason
                run.updated_at = now
        return len(rows)

    async def _is_stop_replace(self, run_id: str) -> bool:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            return bool(row is not None and row.paused_reason == "stop_replace")

    async def _requeue_claimed_run(self, run_id: str) -> None:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id, with_for_update=True)
            if (
                row is None
                or row.status != AGENT_RUN_RUNNING
                or row.claimed_by != self._worker_id
            ):
                return
            pending = (
                await db.get(
                    SystemAgentPendingTurn,
                    row.pending_turn_id,
                    with_for_update=True,
                )
                if row.pending_turn_id
                else None
            )
            now = datetime.now(UTC)
            if row.cancel_requested:
                stop_replace = row.paused_reason == "stop_replace"
                await self._cancel_without_worker(db, row)
                if stop_replace:
                    row.paused_reason = "stop_replace"
                    await self._set_following_queue_state(
                        db,
                        session_id=row.session_id,
                        resume=True,
                        reason=None,
                    )
                else:
                    await self._set_following_queue_state(
                        db,
                        session_id=row.session_id,
                        resume=False,
                        reason=AGENT_RUN_CANCELLED,
                    )
                row.updated_at = now
                await db.commit()
                return
            await self._link_interrupted_user_message(db, row, pending)
            if await self._converge_persisted_success(db, row, pending, now):
                await db.commit()
                return
            if await self._converge_committed_message_result(
                db,
                row,
                pending,
                now,
            ):
                await db.commit()
                return
            await self._prepare_interrupted_message_retry(db, row, pending)
            row.status = AGENT_RUN_QUEUED
            row.phase = "recovering"
            row.claimed_by = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            row.updated_at = now
            if pending is not None:
                pending.status = PENDING_TURN_PENDING
                pending.blocked_reason = None
                pending.updated_at = now
            await db.commit()

    def _assert_worker_claim(self, row: SystemAgentRun) -> None:
        lease = _as_utc(row.lease_expires_at)
        if (
            row.status != AGENT_RUN_RUNNING
            or row.claimed_by != self._worker_id
            or lease is None
            or lease <= datetime.now(UTC)
        ):
            raise _WorkerLeaseLost(row.id)

    async def _owns_worker_claim(self, run_id: str) -> bool:
        async with self._session_factory() as db:
            row = await db.get(SystemAgentRun, run_id)
            if row is None:
                return False
            try:
                self._assert_worker_claim(row)
            except _WorkerLeaseLost:
                return False
            return True

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

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:  # noqa: BLE001
                log.exception("reading system agent run task result failed run=%s", run_id)

    def _dispatch_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.pop(session_id, None)
        if not task.cancelled():
            try:
                task.exception()
            except Exception:  # noqa: BLE001
                log.exception(
                    "system agent queue dispatch failed session=%s",
                    session_id,
                )

    def _recovery_done(self, task: asyncio.Task[None]) -> None:
        if self._recovery_task is task:
            self._recovery_task = None
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:  # noqa: BLE001
            log.exception(
                "reading system agent recovery task result failed worker=%s",
                self._worker_id,
            )


def _request_from_pending(pending: SystemAgentPendingTurn) -> _RunRequest:
    payload = dict(pending.request_payload or {})
    text = decrypt_str(pending.content_enc)
    return _RunRequest(
        session_id=pending.session_id,
        web_user_id=pending.web_user_id,
        bot_tg_user_id=pending.bot_tg_user_id,
        channel=pending.channel,
        role=str(payload.get("role") or "viewer"),
        text=text,
        kind=pending.kind,
        retry_message_id=_optional_int(payload.get("retry_message_id")),
        regenerate_message_id=_optional_int(payload.get("regenerate_message_id")),
        regenerate_assistant_message_id=_optional_int(
            payload.get("regenerate_assistant_message_id")
        ),
        fallback_provider_id=_optional_int(payload.get("fallback_provider_id")),
        approved_tools=tuple(
            str(item) for item in payload.get("approved_tools", []) if str(item)
        ),
        chat_secrets=tuple(extract_plaintext_secrets(text)),
        after_message_id=int(payload.get("after_message_id") or 0),
        model_selection=(
            payload.get("model_selection")
            if isinstance(payload.get("model_selection"), dict)
            else None
        ),
        read_only_only=bool(payload.get("read_only_only")),
    )


def _pending_turn_dict(row: SystemAgentPendingTurn) -> dict[str, Any]:
    try:
        content = decrypt_str(row.content_enc)
    except ValueError:
        content = ""
    return {
        "id": row.id,
        "session_id": row.session_id,
        "run_id": row.dispatch_run_id,
        "web_user_id": row.web_user_id,
        "bot_tg_user_id": row.bot_tg_user_id,
        "channel": row.channel,
        "kind": row.kind,
        "position": row.position,
        "status": row.status,
        "blocked_reason": row.blocked_reason,
        "content": content,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _phase_from_event(event_type: str) -> str | None:
    return {
        "run_started": "understanding",
        "model_capability_check": "checking_model",
        "provider_selected": "selecting_model",
        "route_selected": "routing",
        "skill_selected": "selecting_skill",
        "model_attempt": "thinking",
        "assistant_delta": "responding",
        "assistant_reasoning_delta": "thinking",
        "tool_started": "using_tool",
        "tool_finished": "thinking",
        "steer_applied": "steered",
        "assistant_message": "finalizing",
        "done": "finishing",
    }.get(event_type)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _request_hash(
    *,
    kind: str,
    text: str,
    channel: str,
    role: str,
    account_id: int | None = None,
    web_user_id: int | None = None,
    bot_tg_user_id: int | None = None,
    retry_message_id: int | None,
    regenerate_message_id: int | None = None,
    regenerate_assistant_message_id: int | None = None,
    fallback_provider_id: int | None,
    approved_tools: tuple[str, ...],
    model_selection: dict | None = None,
    read_only_only: bool = False,
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "text": text,
            "channel": channel,
            "role": role,
            "account_id": account_id,
            "web_user_id": web_user_id,
            "bot_tg_user_id": bot_tg_user_id,
            "retry_message_id": retry_message_id,
            "regenerate_message_id": regenerate_message_id,
            "regenerate_assistant_message_id": regenerate_assistant_message_id,
            "fallback_provider_id": fallback_provider_id,
            "approved_tools": sorted(approved_tools),
            "model_selection": model_selection or {"mode": "auto"},
            "read_only_only": bool(read_only_only),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_session_owner(
    session: SystemAgentSession,
    *,
    channel: str,
    web_user_id: int | None,
    bot_tg_user_id: int | None,
    account_id: int | None,
) -> None:
    """在 Manager 边界统一校验渠道与所有者，避免内部调用绕过 API 鉴权。"""

    if session.channel != channel:
        raise RunNotFoundError(f"session:{session.id}")
    if channel == CHANNEL_WEB:
        if web_user_id is None or session.web_user_id != web_user_id:
            raise RunNotFoundError(f"session:{session.id}")
        return
    if channel == CHANNEL_BOT:
        if (
            bot_tg_user_id is None
            or account_id is None
            or session.bot_tg_user_id != bot_tg_user_id
            or session.account_id != account_id
        ):
            raise RunNotFoundError(f"session:{session.id}")
        return
    raise RunConflictError(f"不支持的助手渠道：{channel}")


def _redact_event(
    source: dict[str, Any],
    known_secrets: tuple[str, ...],
) -> dict[str, Any]:
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
