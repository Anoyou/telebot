"""忽略 peer 名单业务层。

供 ``api/ignored_peers.py`` 使用：
- ``list_ignored``  返回某账号的忽略名单
- ``add_ignored``   加入一条；幂等（已存在则返回原行）
- ``remove_ignored``删除一条
- ``fetch_recent``  通过 IPC RPC 向 worker 请求最近活跃 peer 列表

写操作完成后通过 IPC ``CMD_RELOAD_IGNORED`` 通知 worker 重新拉取名单。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.account import Account
from ..db.models.ignored_peer import IgnoredPeer
from ..redis_client import get_redis
from ..schemas.ignored_peer import IgnoredPeerCreate
from ..worker.ipc import (
    CMD_GET_RECENT_PEERS,
    CMD_RELOAD_IGNORED,
    IPCMessage,
    cmd_channel,
    make_cmd,
)

log = logging.getLogger(__name__)

# 一次 RPC 默认超时；worker 离线 / 高负载时返回空列表，不阻塞前端
_RECENT_PEERS_TIMEOUT = 1.5


# ── 错误工具 ──────────────────────────────────────────────────────
def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _ensure_account(db: AsyncSession, aid: int) -> Account:
    """校验账号存在；不存在抛 404。"""
    acc = await db.get(Account, aid)
    if acc is None:
        raise _err("ACCOUNT_NOT_FOUND", "账号不存在", 404)
    return acc


# ── 列表 / 增 / 删 ────────────────────────────────────────────────
async def list_ignored(db: AsyncSession, aid: int) -> list[IgnoredPeer]:
    """返回该账号当前的所有忽略 peer，按 added_at 倒序。"""
    await _ensure_account(db, aid)
    rows = (
        await db.execute(
            select(IgnoredPeer)
            .where(IgnoredPeer.account_id == aid)
            .order_by(IgnoredPeer.added_at.desc(), IgnoredPeer.id.desc())
        )
    ).scalars().all()
    return list(rows)


async def add_ignored(
    db: AsyncSession, aid: int, payload: IgnoredPeerCreate
) -> IgnoredPeer:
    """加入忽略名单；同 (account_id, peer_id) 已存在则返回原行（幂等）。"""
    await _ensure_account(db, aid)

    # 先查一遍——多数情况下用户点过一次"加入忽略"就不会重复，不必走异常路径
    existing = (
        await db.execute(
            select(IgnoredPeer).where(
                IgnoredPeer.account_id == aid,
                IgnoredPeer.peer_id == payload.peer_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    try:
        # 用 SAVEPOINT 隔离并发唯一键冲突，不能 rollback 调用方（System Agent
        # Action 等复合事务）的整个事务。
        async with db.begin_nested():
            row = IgnoredPeer(
                account_id=aid,
                peer_id=payload.peer_id,
                peer_kind=payload.normalized_kind(),
                peer_label=payload.peer_label,
            )
            db.add(row)
            await db.flush()
    except IntegrityError:
        # 并发情况下 UNIQUE 约束兜底：SAVEPOINT 已回滚，只查询已存在行。
        row = (
            await db.execute(
                select(IgnoredPeer).where(
                    IgnoredPeer.account_id == aid,
                    IgnoredPeer.peer_id == payload.peer_id,
                )
            )
        ).scalar_one()
    return row


async def remove_ignored(db: AsyncSession, aid: int, ignored_id: int) -> None:
    """删除忽略名单中的一行；找不到抛 404。"""
    await _ensure_account(db, aid)
    row = (
        await db.execute(
            select(IgnoredPeer).where(
                IgnoredPeer.id == ignored_id,
                IgnoredPeer.account_id == aid,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _err("IGNORED_PEER_NOT_FOUND", "忽略项不存在", 404)
    await db.execute(
        delete(IgnoredPeer).where(
            IgnoredPeer.id == ignored_id,
            IgnoredPeer.account_id == aid,
        )
    )


# ── IPC 通知 ─────────────────────────────────────────────────────
async def notify_reload(aid: int) -> None:
    """忽略名单变更后通知 worker 重拉。Redis 不可达静默吞掉。"""
    try:
        redis = get_redis()
        await redis.publish(cmd_channel(aid), make_cmd(CMD_RELOAD_IGNORED))
    except Exception:  # noqa: BLE001
        log.debug("通知 worker reload_ignored 失败 aid=%s", aid, exc_info=True)


# ── RPC：拉最近活跃 peers ────────────────────────────────────────
async def fetch_recent(aid: int) -> tuple[bool, list[dict[str, Any]]]:
    """通过 Redis pub/sub 做一次性 RPC。

    返回 ``(worker_alive, items)``：
    - ``worker_alive=True``  且 ``items=[...]`` → worker 正常应答，可能为空也可能有数据
    - ``worker_alive=False`` 且 ``items=[]``    → 超时 / Redis 故障 / 任何异常

    流程：
    1. 主进程生成一个一次性 ``reply_to`` 频道名（含随机串），订阅它
    2. 向 ``worker_cmd:{aid}`` 发布 ``get_recent_peers``，payload 带上 ``reply_to``
    3. 等待 ``_RECENT_PEERS_TIMEOUT`` 内 worker 的应答；超时即视为 worker 不在跑
    4. 一定退订并关 pubsub，避免连接泄漏
    """
    reply_channel = f"worker_reply:{aid}:recent_peers:{secrets.token_hex(8)}"
    try:
        redis = get_redis()
    except Exception:  # noqa: BLE001
        return False, []

    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_channel)
        # 订阅完成后再发请求，否则有竞态：worker 可能在我们订阅之前就回包了
        await redis.publish(
            cmd_channel(aid),
            make_cmd(CMD_GET_RECENT_PEERS, reply_to=reply_channel),
        )

        deadline = asyncio.get_event_loop().time() + _RECENT_PEERS_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False, []
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                    timeout=remaining,
                )
            except TimeoutError:
                return False, []
            if msg is None:
                continue
            if msg.get("type") != "message":
                continue
            try:
                payload = IPCMessage.decode(msg["data"]).payload
            except Exception:  # noqa: BLE001
                return False, []
            items = payload.get("items") or []
            if not isinstance(items, list):
                return True, []
            return True, items
    except Exception:  # noqa: BLE001
        return False, []
    finally:
        try:
            await pubsub.unsubscribe(reply_channel)
        except Exception:  # noqa: BLE001
            pass
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            try:
                # 老版本 redis-py 用 close()，新版本用 aclose()
                await pubsub.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "add_ignored",
    "fetch_recent",
    "list_ignored",
    "notify_reload",
    "remove_ignored",
]
