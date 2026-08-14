"""账号 CRUD 业务层。

为 ``api/accounts.py`` 提供：
- 列表 / 详情 / 修改 / 删除
- 暂停 / 恢复
- 复制配置（account_feature + 关联 rule）
- 头像懒加载（本地磁盘缓存 + IPC 通知 worker 拉新）

只做 DB 与 IPC 协调，登录绑定向导在 ``login_service.py``。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.sessions import StringSession

from ..crypto import decrypt_bytes, decrypt_str
from ..db.models.account import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_LOGIN_REQUIRED,
    ACCOUNT_STATUS_PAUSED,
    Account,
    Proxy,
)
from ..db.models.feature import AccountFeature
from ..db.models.rule import Rule
from ..db.models.system import SystemSetting
from ..redis_client import get_redis
from ..schemas.account import (
    AccountDetail,
    AccountSummary,
    AccountUpdateRequest,
    ProxySummary,
)
from ..settings import settings
from ..worker.ipc import (
    CMD_FETCH_AVATAR,
    CMD_PAUSE,
    CMD_RESUME,
    CMD_STOP,
    GLOBAL_CHANNEL,
    cmd_channel,
    make_cmd,
)

log = logging.getLogger(__name__)

# 头像缓存 TTL：超过这个时长就让 worker 重拉
_AVATAR_TTL_SECONDS = 24 * 3600


# ── 错误工具 ──────────────────────────────────────────────────────
def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _not_found() -> HTTPException:
    return _err("ACCOUNT_NOT_FOUND", "账号不存在", 404)


# ── 查询 ──────────────────────────────────────────────────────────
async def list_accounts(db: AsyncSession) -> list[AccountSummary]:
    """列出全部账号 + 已启用功能数 + 绑定的代理摘要 + 代理 last-probe 缓存。

    左连接 Proxy 一次性把 proxy 行也带回来——避免前端每张卡片各自再查一遍。
    用 ``mget`` 批读探测缓存，避免 N+1。
    """
    from . import proxy_probe_cache

    # 子查询：每账号 enabled=true 的 account_feature 计数
    enabled_count_sq = (
        select(
            AccountFeature.account_id.label("aid"),
            func.count(AccountFeature.feature_key).label("cnt"),
        )
        .where(AccountFeature.enabled.is_(True))
        .group_by(AccountFeature.account_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Account, func.coalesce(enabled_count_sq.c.cnt, 0), Proxy)
            .outerjoin(enabled_count_sq, enabled_count_sq.c.aid == Account.id)
            .outerjoin(Proxy, Proxy.id == Account.proxy_id)
            .order_by(Account.id)
        )
    ).all()
    # 一次拉所有有代理的账号的探测缓存（Redis mget；空集合也安全）
    proxy_ids = [prx.id for _acc, _cnt, prx in rows if prx is not None]
    probes = await proxy_probe_cache.get_probes_bulk(proxy_ids)

    out: list[AccountSummary] = []
    for acc, cnt, prx in rows:
        out.append(
            AccountSummary(
                id=acc.id,
                phone=acc.phone,
                display_name=acc.display_name,
                tg_user_id=acc.tg_user_id,
                tg_username=acc.tg_username,
                status=acc.status,
                tags=acc.tags,
                enabled_features=int(cnt or 0),
                cold_start_until=acc.cold_start_until,
                created_at=acc.created_at,
                proxy=_proxy_summary(prx, probes.get(prx.id) if prx else None),
            )
        )
    return out


def _proxy_summary(
    prx: Proxy | None,
    probe: dict | None = None,
) -> ProxySummary | None:
    """ORM Proxy → 前端用的 ProxySummary。

    ``probe`` 为 ``proxy_probe_cache.get_probe`` 的返回；非 None 时把出口字段填上。
    给 None 表示"无缓存"——前端会显示"未探测"或自己点刷新。
    """
    if prx is None:
        return None
    return ProxySummary(
        id=prx.id,
        type=prx.type or "?",
        host=prx.host or "",
        port=int(prx.port or 0),
        label=f"{prx.host}:{prx.port}",
        exit_country=(probe or {}).get("country"),
        exit_ip=(probe or {}).get("exit_ip"),
        probed_at=(probe or {}).get("probed_at"),
        probe_ok=(probe or {}).get("ok"),
    )


async def _enabled_count(db: AsyncSession, aid: int) -> int:
    """单账号的已启用功能计数（用于详情）。"""
    cnt = (
        await db.execute(
            select(func.count(AccountFeature.feature_key)).where(
                AccountFeature.account_id == aid, AccountFeature.enabled.is_(True)
            )
        )
    ).scalar_one()
    return int(cnt or 0)


async def get_account(db: AsyncSession, aid: int) -> AccountDetail:
    """读取账号详情。"""
    from . import proxy_probe_cache

    acc = await db.get(Account, aid)
    if not acc:
        raise _not_found()
    cnt = await _enabled_count(db, aid)
    prx = await db.get(Proxy, acc.proxy_id) if acc.proxy_id else None
    probe = await proxy_probe_cache.get_probe(prx.id) if prx else None
    return AccountDetail(
        id=acc.id,
        phone=acc.phone,
        display_name=acc.display_name,
        tg_user_id=acc.tg_user_id,
        tg_username=acc.tg_username,
        status=acc.status,
        tags=acc.tags,
        enabled_features=cnt,
        cold_start_until=acc.cold_start_until,
        created_at=acc.created_at,
        notes=acc.notes,
        template_id=acc.template_id,
        proxy_id=acc.proxy_id,
        device_profile_id=acc.device_profile_id,
        proxy=_proxy_summary(prx, probe),
    )


# ── 修改 ──────────────────────────────────────────────────────────
async def update_account(db: AsyncSession, aid: int, data: AccountUpdateRequest) -> AccountDetail:
    """PATCH 账号字段（display_name / notes / tags / template_id / proxy_id）。"""
    acc = await db.get(Account, aid)
    if not acc:
        raise _not_found()
    # 仅赋值用户显式给出的字段（exclude_unset 区分 None 与 未传）
    payload = data.model_dump(exclude_unset=True)
    proxy_id = payload.get("proxy_id")
    if proxy_id is not None:
        proxy = await db.get(Proxy, int(proxy_id))
        if proxy is None:
            raise _err("PROXY_NOT_FOUND", f"代理 #{proxy_id} 不存在", 404)
        if str(proxy.type or "").lower() not in {"socks5", "http", "https"}:
            raise _err(
                "PROXY_TYPE_UNSUPPORTED",
                f"代理类型 {proxy.type!r} 当前不受支持，请先在代理库中改为 SOCKS5、HTTP 或 HTTPS",
                422,
            )
    need_restart_worker = False
    for k, v in payload.items():
        # proxy/template 发生实际变化后，重启 worker 让 Telethon client 重新按新配置建连。
        if k in {"proxy_id", "template_id"} and getattr(acc, k) != v:
            need_restart_worker = True
        setattr(acc, k, v)
    await db.commit()
    if need_restart_worker:
        await _publish(cmd_channel(aid), make_cmd(CMD_STOP))
        await _publish(GLOBAL_CHANNEL, make_cmd("start_worker", account_id=aid))
    return await get_account(db, aid)


# ── 暂停 / 恢复 ───────────────────────────────────────────────────
async def pause(db: AsyncSession, aid: int) -> None:
    """暂停账号：状态置 paused，并让 supervisor 停止对应 worker。"""
    acc = await db.get(Account, aid)
    if not acc:
        raise _not_found()
    acc.status = ACCOUNT_STATUS_PAUSED
    await db.commit()
    try:
        from ..worker import supervisor

        await supervisor.stop_worker(aid)
    except Exception:  # noqa: BLE001
        log.warning("通过 supervisor 停止 worker 失败，回退到 IPC pause aid=%s", aid, exc_info=True)
        await _publish(cmd_channel(aid), make_cmd(CMD_PAUSE))


async def resume(db: AsyncSession, aid: int) -> None:
    """恢复账号：状态置 active，并让 supervisor 拉起对应 worker。"""
    acc = await db.get(Account, aid)
    if not acc:
        raise _not_found()
    try:
        ensure_account_secrets_decryptable(acc)
    except ValueError as exc:
        acc.status = ACCOUNT_STATUS_LOGIN_REQUIRED
        await db.commit()
        raise _err(
            "ACCOUNT_SESSION_DECRYPT_FAILED",
            "账号登录凭据无法解密，通常是 MASTER_KEY 已变更。请恢复原 MASTER_KEY，或重新登录该账号。",
            422,
        ) from exc
    acc.status = ACCOUNT_STATUS_ACTIVE
    await db.commit()
    if await _kill_switch_enabled(db):
        return
    try:
        from ..worker import supervisor

        await supervisor.start_worker(aid)
    except Exception:  # noqa: BLE001
        log.warning("通过 supervisor 启动 worker 失败，回退到 IPC resume/start aid=%s", aid, exc_info=True)
        await _publish(cmd_channel(aid), make_cmd(CMD_RESUME))
        await _publish(GLOBAL_CHANNEL, make_cmd("start_worker", account_id=aid))


async def _kill_switch_enabled(db: AsyncSession) -> bool:
    row = await db.get(SystemSetting, "kill_switch")
    if row is None:
        return False
    value = row.value
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return bool(value)


def ensure_account_secrets_decryptable(acc: Account) -> None:
    """恢复前先验证账号核心密钥，避免 worker 启动后立刻 down。"""

    decrypt_bytes(acc.session_enc)
    decrypt_str(acc.api_id_enc)
    decrypt_str(acc.api_hash_enc)


# ── 删除 ──────────────────────────────────────────────────────────
async def delete_account(db: AsyncSession, aid: int) -> None:
    """删除账号：先发 STOP；再尝试 log_out（best effort）；最后 DELETE FROM account。"""
    acc = await db.get(Account, aid)
    if not acc:
        raise _not_found()

    # 1. 通知 worker 自杀
    await _publish(cmd_channel(aid), make_cmd(CMD_STOP))

    # 2. best effort：用现有 session 调 client.log_out() 让 TG 撤销 session
    try:
        await _logout_best_effort(db, acc)
    except Exception:  # noqa: BLE001
        # 撤销失败也不阻塞 DELETE（账号可能已 dead 或网络不可达）
        pass

    # 3. DELETE FROM account（cascade 会带走 humanize_config / account_feature / rule / 日志）
    # SystemSetting 没有账号外键；显式清理交互 Bot 配置，避免删除后启动扫描反复
    # 对不存在的账号调用 restart_interaction_bot 并触发 ACCOUNT_NOT_FOUND。
    await db.execute(
        delete(SystemSetting).where(
            SystemSetting.key == f"account_bot_transfer_notice:{int(aid)}"
        )
    )
    await db.delete(acc)
    await db.commit()


async def _logout_best_effort(db: AsyncSession, acc: Account) -> None:
    """尝试用账号自身的 session 在 TG 服务端撤销登录。失败静默。"""
    api_id = int(decrypt_str(acc.api_id_enc))
    api_hash = decrypt_str(acc.api_hash_enc)
    session_str = decrypt_bytes(acc.session_enc).decode()

    proxy = await db.get(Proxy, acc.proxy_id) if acc.proxy_id else None
    if acc.proxy_id is not None and proxy is None:
        from ..util.proxy import ProxyConfigError

        raise ProxyConfigError(f"账号绑定的代理 #{acc.proxy_id} 不存在，拒绝回落直连")
    from ..worker.tg_client import build_proxy_tuple

    # 与登录/worker 共用严格解析：未绑定时尊重全局代理，显式坏配置则在构造
    # TelegramClient 前抛错，由 delete_account 的 best-effort 边界吞掉。
    proxy_tuple = build_proxy_tuple(proxy)

    client = TelegramClient(StringSession(session_str), api_id, api_hash, proxy=proxy_tuple)
    try:
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


# ── 复制配置 ──────────────────────────────────────────────────────
async def clone_config(
    db: AsyncSession,
    src_aid: int,
    dst_aid: int,
    features: Iterable[str] | None = None,
    *,
    notify: bool = True,
    commit: bool = True,
    web_user_id: int | None = None,
) -> dict[str, int]:
    """把源账号的 ``account_feature`` 与对应 ``rule`` 复制到目标账号。

    :param features: 指定要复制的 feature_key 列表；为空表示全部。
    :return: ``{"features": N, "rules": M}`` 复制条数统计。
    """
    if src_aid == dst_aid:
        raise _err("CLONE_SAME_ACCOUNT", "源账号和目标账号相同")

    # 校验两个账号都存在
    src = await db.get(Account, src_aid)
    dst = await db.get(Account, dst_aid)
    if not src or not dst:
        raise _not_found()

    feature_filter = list(features) if features else None

    # 1) 复制 account_feature
    af_q = select(AccountFeature).where(AccountFeature.account_id == src_aid)
    if feature_filter:
        af_q = af_q.where(AccountFeature.feature_key.in_(feature_filter))
    src_afs = (await db.execute(af_q)).scalars().all()

    # 先完整预检所有即将启用的叶，再覆盖目标账号，避免被修枝剪阻断时留下半份配置。
    from .platform_capabilities import ensure_plugin_capabilities

    source_enabled_keys = {str(af.feature_key) for af in src_afs if bool(af.enabled)}
    target_enabled_keys: set[str] = set()
    if source_enabled_keys:
        target_enabled_keys = set(
            (
                await db.execute(
                    select(AccountFeature.feature_key).where(
                        AccountFeature.account_id == dst_aid,
                        AccountFeature.feature_key.in_(source_enabled_keys),
                        AccountFeature.enabled.is_(True),
                    )
                )
            ).scalars()
        )
    for feature_key in sorted(
        source_enabled_keys - target_enabled_keys
    ):
        await ensure_plugin_capabilities(
            db,
            feature_key,
            triggered_by_user_id=web_user_id,
        )

    # 先清掉目标账号同 key 的 account_feature，保证幂等
    if src_afs:
        keys_to_overwrite = [af.feature_key for af in src_afs]
        await db.execute(
            delete(AccountFeature).where(
                AccountFeature.account_id == dst_aid,
                AccountFeature.feature_key.in_(keys_to_overwrite),
            )
        )
        # 同样删掉这些 feature 在目标账号的 rule
        await db.execute(
            delete(Rule).where(
                Rule.account_id == dst_aid,
                Rule.feature_key.in_(keys_to_overwrite),
            )
        )

    feat_n = 0
    for af in src_afs:
        db.add(
            AccountFeature(
                account_id=dst_aid,
                feature_key=af.feature_key,
                enabled=af.enabled,
                config=dict(af.config or {}),
                state=af.state,
            )
        )
        feat_n += 1

    # 2) 复制 rule
    rule_q = select(Rule).where(Rule.account_id == src_aid)
    if feature_filter:
        rule_q = rule_q.where(Rule.feature_key.in_(feature_filter))
    src_rules = (await db.execute(rule_q)).scalars().all()
    rule_n = 0
    for r in src_rules:
        copied_rule_config = dict(r.config or {})
        if r.feature_key == "scheduler":
            from .rule_service import bind_scheduler_agent_owner

            copied_rule_config = bind_scheduler_agent_owner(
                copied_rule_config, web_user_id
            )
        db.add(
            Rule(
                account_id=dst_aid,
                feature_key=r.feature_key,
                name=r.name,
                enabled=r.enabled,
                priority=r.priority,
                config=copied_rule_config,
            )
        )
        rule_n += 1

    if commit:
        await db.commit()
    else:
        await db.flush()

    # 通知目标 worker 重新加载配置（若在跑）
    if notify:
        await _publish(cmd_channel(dst_aid), make_cmd("reload_config"))

    return {"features": feat_n, "rules": rule_n}


# ── 头像懒加载 ────────────────────────────────────────────────────
def _avatar_path(aid: int) -> Path:
    """返回 ``data/avatars/{aid}.jpg`` 的绝对路径（不保证存在）。"""
    return Path(settings.avatars_dir).resolve() / f"{aid}.jpg"


async def ensure_avatar(db: AsyncSession, aid: int) -> Path | None:
    """检查本地头像缓存：

    - 文件存在且未过期（24h）→ 直接返；
    - 文件不存在 / 过期 → fire-and-forget 发 IPC 让 worker 写盘，本次返当前
      路径（可能为 None）；
    - 账号不存在 → 抛 404。

    worker 离线时 IPC 没人接收，本次仍返 None；前端会走首字母 fallback，
    下次刷新（等 worker 起来）就能看到。
    """
    acc = await db.get(Account, aid)
    if acc is None:
        raise _not_found()

    path = _avatar_path(aid)
    fresh = False
    if path.exists():
        try:
            mtime = path.stat().st_mtime
            fresh = (time.time() - mtime) < _AVATAR_TTL_SECONDS
        except OSError:
            fresh = False

    if not fresh:
        # 不阻塞请求：把绝对路径告诉 worker，worker 写盘后下次请求就能读到
        await _publish(
            cmd_channel(aid),
            make_cmd(CMD_FETCH_AVATAR, path=str(path)),
        )

    return path if path.exists() else None


# ── IPC 工具 ──────────────────────────────────────────────────────
async def _publish(channel: str, payload: str) -> None:
    """对 Redis publish 失败时静默；保证业务路径优先成功。"""
    try:
        redis = get_redis()
        await redis.publish(channel, payload)
    except Exception:  # noqa: BLE001
        pass
