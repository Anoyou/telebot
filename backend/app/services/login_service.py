"""Telethon 多步登录状态机：start → code → 2fa → finalize。

核心难点：Telethon 的 ``auth_key`` 与 ``phone_code_hash`` 都挂在 ``TelegramClient`` 实例
内部，跨请求重建会丢失中间态。所以这里在主进程内存里保留同一个 client 实例（按
``login_token`` 索引），30 分钟未完成由后台清理。
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from ..crypto import decrypt_str, encrypt_bytes, encrypt_str
from ..db.models.account import (
    ACCOUNT_STATUS_ACTIVE,
    Account,
    HumanizeConfig,
    Proxy,
)
from ..redis_client import get_redis
from ..settings import settings
from ..worker.ipc import GLOBAL_CHANNEL, make_cmd


# ── 进程内挂起登录态 ────────────────────────────────────────────
@dataclass
class _PendingLogin:
    """单个挂起登录会话（持有未完成绑定的 TelegramClient）。"""

    client: TelegramClient
    api_id: int
    # api_hash 仅驻内存到 finalize；不落盘除非加密
    api_hash: str
    phone: str
    phone_code_hash: str | None = None
    require_2fa: bool = False
    # 重新登录老账号场景才有 account_id；新建则为空
    account_id: int | None = None
    proxy_id: int | None = None
    # 启动绑定时选定的设备伪装 profile id；为空则用系统默认
    device_profile_id: int | None = None
    # 验证码 / 2FA 错误重试次数（超限后 token 作废）
    code_attempts: int = 0
    twofa_attempts: int = 0
    # token 从验证码/2FA 校验开始到 finalize 完成前只允许一个请求持有。
    claimed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# 所有挂起登录的全局表，key=login_token；TTL 30 分钟
_PENDING: dict[str, _PendingLogin] = {}
_PENDING_TTL = timedelta(minutes=30)
_MAX_PENDING_LOGINS = 100
_MAX_CODE_ATTEMPTS = 5
_MAX_2FA_ATTEMPTS = 5
# 串行化对 _PENDING 的读写，避免并发请求踩到同一个 token
_LOCK = asyncio.Lock()


# ── 内部工具 ──────────────────────────────────────────────────────
def _err(code: str, message: str, status: int = 400) -> HTTPException:
    """构造统一格式的错误响应。"""
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _phone_digits(value: str | None) -> str:
    """只保留数字，用于判断 +86 / 86 这类格式差异下是否为同一号码。"""

    return "".join(ch for ch in str(value or "") if ch.isdigit())


async def _build_proxy_tuple(db: AsyncSession, proxy_id: int | None):
    """根据 proxy_id 构造 Telethon 所需的 proxy 元组。

    proxy_id 为空时，回落到 ``settings.tg_default_proxy`` 全局代理；
    仍未配置则真正直连（适用于宿主机能直连 TG 的网络）。
    """
    if proxy_id is None:
        from ..util.proxy import get_default_proxy_tuple
        try:
            return get_default_proxy_tuple()
        except ValueError as exc:
            raise _err("PROXY_TYPE_UNSUPPORTED", str(exc), 422) from exc
    proxy = await db.get(Proxy, proxy_id)
    if not proxy:
        raise _err("PROXY_NOT_FOUND", f"代理 #{proxy_id} 不存在", 404)
    proxy_type = str(proxy.type or "").lower()
    if proxy_type not in {"socks5", "socks4", "http", "https"}:
        raise _err(
            "PROXY_TYPE_UNSUPPORTED",
            f"代理类型 {proxy.type!r} 当前不受支持，请改用 SOCKS5、SOCKS4、HTTP 或 HTTPS",
            422,
        )
    try:
        password = decrypt_str(proxy.password_enc) if proxy.password_enc else None
    except Exception as exc:  # noqa: BLE001 - 坏凭据必须在构造 TelegramClient 前拒绝
        raise _err(
            "PROXY_CONFIG_INVALID",
            "代理凭据无法解密，请重新保存或更换代理",
            422,
        ) from exc
    if "://" in proxy.host:
        from ..util.proxy import ProxyConfigError, parse_proxy_url

        try:
            parsed = parse_proxy_url(proxy.host)
        except ProxyConfigError as exc:
            raise _err("PROXY_CONFIG_INVALID", str(exc), 422) from exc
        if parsed is not None:
            ptype, host, port, rdns, parsed_user, parsed_password = parsed
            return (ptype, host, port, rdns, proxy.username or parsed_user, password or parsed_password)
    return (
        "http" if proxy_type == "https" else proxy_type,
        proxy.host,
        proxy.port,
        True,              # rdns（远端解析 DNS，避免泄漏）
        proxy.username,
        password,
    )


# ── 对外 API：状态机三步 + finalize ───────────────────────────────
async def start_login(
    db: AsyncSession,
    *,
    api_id: int,
    api_hash: str,
    phone: str,
    account_id: int | None = None,
    proxy_id: int | None = None,
    device_profile_id: int | None = None,
) -> str:
    """第 1 步：建 client → 连接 → 发验证码，返回 login_token。"""
    max_pending = int(getattr(settings, "max_pending_logins", _MAX_PENDING_LOGINS) or _MAX_PENDING_LOGINS)
    async with _LOCK:
        if len(_PENDING) >= max_pending:
            raise _err("LOGIN_PENDING_LIMITED", "当前登录请求过多，请稍后再试", 429)

    if account_id is not None:
        acc = await db.get(Account, account_id)
        if not acc:
            raise _err("ACCOUNT_NOT_FOUND", "账号不存在", 404)
        if _phone_digits(acc.phone) != _phone_digits(phone):
            raise _err(
                "ACCOUNT_PHONE_MISMATCH",
                "重登手机号必须与当前账号一致，避免把别人的 session 覆盖到这个账号。",
                409,
            )

    proxy_tuple = await _build_proxy_tuple(db, proxy_id)
    # 解析设备伪装：调用方指定 → 系统默认 → 硬编码兜底
    from .device_profile import get_by_id, get_default
    profile = None
    if device_profile_id is not None:
        profile = await get_by_id(db, device_profile_id)
    if profile is None:
        profile = await get_default(db)
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        proxy=proxy_tuple,
        **profile.telethon_kwargs(),
    )
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        await _safe_disconnect(client)
        raise _err("FLOOD_WAIT", f"请求过于频繁，请等待 {e.seconds} 秒", 429) from e
    except PhoneNumberInvalidError as e:
        await _safe_disconnect(client)
        raise _err("PHONE_INVALID", "手机号无效") from e
    except Exception as e:  # noqa: BLE001
        # 其它错误（网络、API 凭据错等）也要先回收 client，再向上抛
        await _safe_disconnect(client)
        raise _err(
            "LOGIN_START_FAILED",
            f"发起登录失败：无法连接 Telegram，请检查代理与标识里的代理配置或服务器网络。原始错误：{e}",
            502,
        ) from e

    token = secrets.token_urlsafe(24)
    async with _LOCK:
        _PENDING[token] = _PendingLogin(
            client=client,
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
            account_id=account_id,
            proxy_id=proxy_id,
            device_profile_id=device_profile_id,
        )
    return token


async def confirm_code(token: str, code: str) -> tuple[bool, _PendingLogin]:
    """第 2 步：提交短信/Telegram 验证码。

    返回 ``(require_2fa, pending)``：
    - ``require_2fa=True``：账号启用了两步验证，需要继续走 ``confirm_2fa``。
    - ``require_2fa=False``：可以直接进入 finalize。
    """
    pending = await _claim_pending(token)
    try:
        await pending.client.sign_in(
            phone=pending.phone,
            code=code,
            phone_code_hash=pending.phone_code_hash,
        )
    except SessionPasswordNeededError:
        # 账号启用了两步验证，停在此步等 2fa
        pending.require_2fa = True
        await _release_claim(token, pending)
        return True, pending
    except PhoneCodeInvalidError as e:
        pending.code_attempts += 1
        if pending.code_attempts >= _MAX_CODE_ATTEMPTS:
            await _cleanup(token, disconnect=True)
            raise _err("LOGIN_ATTEMPTS_EXCEEDED", "验证码错误次数过多，请重新发起绑定", 429) from e
        await _release_claim(token, pending)
        raise _err("CODE_INVALID", "验证码错误") from e
    except PhoneCodeExpiredError as e:
        # 验证码过期视为整个会话作废，回收 client
        await _cleanup(token, disconnect=True)
        raise _err("CODE_EXPIRED", "验证码已过期，请重新发起绑定") from e
    except BaseException:
        await _release_claim(token, pending)
        raise
    return False, pending


async def confirm_2fa(token: str, password: str) -> _PendingLogin:
    """第 3 步：提交两步验证密码。"""
    pending = await _claim_pending(token)
    try:
        await pending.client.sign_in(password=password)
    except PasswordHashInvalidError as e:
        pending.twofa_attempts += 1
        if pending.twofa_attempts >= _MAX_2FA_ATTEMPTS:
            await _cleanup(token, disconnect=True)
            raise _err("LOGIN_ATTEMPTS_EXCEEDED", "两步密码错误次数过多，请重新发起绑定", 429) from e
        await _release_claim(token, pending)
        raise _err("PASSWORD_INVALID", "两步密码错误") from e
    except BaseException:
        await _release_claim(token, pending)
        raise
    return pending


async def finalize(db: AsyncSession, token: str, pending: _PendingLogin) -> int:
    """登录成功后落库 + 通知 supervisor 拉起 worker。返回 account_id。"""
    async with _LOCK:
        current = _PENDING.get(token)
        if current is not pending:
            raise _err("LOGIN_TOKEN_EXPIRED", "登录会话已过期，请重新发起绑定")
        if not pending.claimed:
            pending.claimed = True
    try:
        return await _finalize_claimed(db, token, pending)
    except BaseException:
        await _release_claim(token, pending)
        raise


async def _finalize_claimed(db: AsyncSession, token: str, pending: _PendingLogin) -> int:
    """执行已持有 token claim 的落库流程。"""

    me = await pending.client.get_me()
    session_str = pending.client.session.save()

    # me.username 不含 @；可能为 None（用户未设置用户名）
    tg_user_id = getattr(me, "id", None)
    tg_username = getattr(me, "username", None) or None

    if pending.account_id is None:
        # 新建账号
        acc = Account(
            phone=pending.phone,
            display_name=(me.first_name or me.username or pending.phone),
            tg_user_id=tg_user_id,
            tg_username=tg_username,
            api_id_enc=encrypt_str(str(pending.api_id)),
            api_hash_enc=encrypt_str(pending.api_hash),
            session_enc=encrypt_bytes(session_str.encode()),
            status=ACCOUNT_STATUS_ACTIVE,
            proxy_id=pending.proxy_id,
            device_profile_id=pending.device_profile_id,
        )
        db.add(acc)
        await db.flush()
        # 默认拟人化配置（PRD §L.3 默认值由模型 default 提供）
        db.add(HumanizeConfig(account_id=acc.id))
        await db.commit()
        account_id = acc.id
    else:
        # 重新登录已有账号：替换 session、状态置回 active；顺手回填 tg 身份
        acc = await db.get(Account, pending.account_id)
        if not acc:
            await _safe_disconnect(pending.client)
            await _cleanup(token)
            raise _err("ACCOUNT_NOT_FOUND", "账号不存在", 404)
        if acc.tg_user_id is not None and tg_user_id is not None and acc.tg_user_id != tg_user_id:
            await _safe_disconnect(pending.client)
            await _cleanup(token)
            raise _err(
                "ACCOUNT_IDENTITY_MISMATCH",
                "登录到的 Telegram 用户与当前账号不一致，已拒绝覆盖 session。",
                409,
            )
        acc.phone = pending.phone
        acc.api_id_enc = encrypt_str(str(pending.api_id))
        acc.api_hash_enc = encrypt_str(pending.api_hash)
        acc.session_enc = encrypt_bytes(session_str.encode())
        acc.status = ACCOUNT_STATUS_ACTIVE
        acc.proxy_id = pending.proxy_id
        if tg_user_id is not None:
            acc.tg_user_id = tg_user_id
        # username 即使为 None 也覆盖：用户可能在 TG 主动清掉了用户名
        acc.tg_username = tg_username
        # 重新登录时如果显式选了新的设备伪装，更新绑定（这次重登会用新的 profile 注册到 TG）
        if pending.device_profile_id is not None:
            acc.device_profile_id = pending.device_profile_id
        await db.commit()
        account_id = acc.id

    await _safe_disconnect(pending.client)

    # 通知 supervisor 拉起该 worker（B Agent 的 supervisor 监听 worker_global 频道）
    try:
        redis = get_redis()
        await redis.publish(GLOBAL_CHANNEL, make_cmd("start_worker", account_id=account_id))
    except Exception:  # noqa: BLE001
        # Redis 暂不可用不应阻塞登录成功落库；supervisor 启动时会扫表自动拉起
        pass

    await _cleanup(token)
    return account_id


# ── 内部状态维护 ──────────────────────────────────────────────────
async def _cleanup(token: str, *, disconnect: bool = False) -> None:
    """从挂起表移除一个 token 的状态；可选回收 Telethon client。"""
    pending: _PendingLogin | None = None
    async with _LOCK:
        pending = _PENDING.pop(token, None)
    if disconnect and pending is not None:
        await _safe_disconnect(pending.client)


async def _claim_pending(token: str) -> _PendingLogin:
    """原子占用一个登录 token，避免验证码、2FA 或 finalize 被并发执行。"""

    async with _LOCK:
        pending = _PENDING.get(token)
        if pending is None:
            raise _err("LOGIN_TOKEN_EXPIRED", "登录会话已过期，请重新发起绑定")
        if pending.claimed:
            raise _err("LOGIN_TOKEN_IN_USE", "登录会话正在处理中，请勿重复提交", 409)
        pending.claimed = True
        return pending


async def _release_claim(token: str, pending: _PendingLogin) -> None:
    """仅释放仍指向同一挂起对象的 claim，避免误解锁已替换 token。"""

    async with _LOCK:
        if _PENDING.get(token) is pending:
            pending.claimed = False


async def get_pending(token: str) -> _PendingLogin | None:
    """读取当前挂起态（只读）。"""
    async with _LOCK:
        return _PENDING.get(token)


async def _safe_disconnect(client: TelegramClient) -> None:
    """无论是否处于已连接状态，都尝试断开（异常吞掉）。"""
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass


async def cleanup_expired_loop() -> None:
    """主进程 lifespan 中 spawn 的后台守护任务：每 60s 清理一次过期 pending。"""
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(UTC)
            expired: list[_PendingLogin] = []
            async with _LOCK:
                for tok, p in list(_PENDING.items()):
                    if now - p.created_at > _PENDING_TTL:
                        expired.append(p)
                        _PENDING.pop(tok, None)
            for p in expired:
                await _safe_disconnect(p.client)
        except asyncio.CancelledError:
            # 进程退出时正常终止
            break
        except Exception:  # noqa: BLE001
            # 守护循环不能因为偶发异常而退出
            pass
