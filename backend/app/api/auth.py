"""认证 API：注册（首次部署）/ 登录 / 注销 / 当前用户 / TOTP 启用与校验。"""

from __future__ import annotations

import logging
import time
import unicodedata

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..crypto import decrypt_str, encrypt_str
from ..db.models.user import WebUser
from ..deps import CurrentUser, DBSession
from ..schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    TotpDisableRequest,
    TotpEnableResponse,
    TotpVerifyRequest,
)
from ..schemas.auth import (
    CurrentUser as CurrentUserSchema,
)
from ..services import audit, auth_login_security, auth_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# Cookie key 与 deps.get_current_user 中读取的 alias 必须保持一致
_COOKIE_NAME = "auth_token"


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _set_auth_cookie(resp: Response, token: str, max_age: int) -> None:
    """统一的 cookie 设置：HttpOnly + SameSite=Lax + (可选)Secure。

    生产 HTTPS 部署在 .env 里设 ``COOKIE_SECURE=true``，会在响应里直接打 Secure
    标记；本地 HTTP 调试默认 false。
    """
    from ..settings import settings as _s

    resp.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_s.cookie_secure,
    )


# ── 登录限速 ──────────────────────────────────────────────────────
# 用 Redis INCR + EXPIRE 实现一个 per-minute 滑动窗口（粗粒度，简单可靠）。
# 双维度计数：IP 和 username 各算一份，任一超限即拒。

# 进程内 fallback：Redis 故障时仍能扛住基础暴力破解
# 结构：key -> (count, window_start_ts)；窗口固定 60s
_LOCAL_RL_BUCKETS: dict[str, tuple[int, float]] = {}


def _client_ip(request: Request) -> str:
    """提取客户端 IP。

    仅当 ``settings.trust_forwarded_for=true``（部署在可信反代后）时，才信任
    ``X-Forwarded-For``；否则一律取 TCP 直连地址，避免攻击者塞任意 header
    伪造新 IP 绕过限速。
    """
    from ..settings import settings as _s

    if _s.trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # X-Forwarded-For: client, proxy1, proxy2 → 取最左
            ip = fwd.split(",", 1)[0].strip()
            if ip:
                return ip
    return request.client.host if request.client else "?"


def _normalize_rl_username(raw: str) -> str:
    """限速 key 用的 username 归一化。

    - NFKC：全角 / 兼容字符归到标准形式（避免 ＡＤＭＩＮ vs ADMIN 算两个 key）
    - strip + lower：避免大小写 / 前后空格差异
    - 字节截断 64：防恶意超长 username 撑爆 Redis key 空间
    """
    s = unicodedata.normalize("NFKC", raw or "").strip().lower()
    return s.encode("utf-8", "ignore")[:64].decode("utf-8", "ignore")


def _local_rl_check(key: str, limit: int) -> bool:
    """进程内固定窗口计数器；返回 True=放行，False=超限。

    用于 Redis 故障时兜底（fail-degraded 而非 fail-open）。每进程独立，多实例
    部署下保护力会下降，但仍比完全无限速强；Redis 恢复后自动切回。
    """
    now = time.time()
    cnt, start = _LOCAL_RL_BUCKETS.get(key, (0, now))
    if now - start >= 60.0:
        cnt, start = 0, now
    cnt += 1
    _LOCAL_RL_BUCKETS[key] = (cnt, start)
    # 顺手清理太旧的桶，防内存泄漏（最多保留 1000 个活跃 key）
    if len(_LOCAL_RL_BUCKETS) > 1000:
        cutoff = now - 120.0
        for k in list(_LOCAL_RL_BUCKETS.keys()):
            if _LOCAL_RL_BUCKETS[k][1] < cutoff:
                _LOCAL_RL_BUCKETS.pop(k, None)
    return cnt <= limit


async def _enforce_login_rate_limit(request: Request, username: str) -> None:
    from ..redis_client import get_redis
    from ..settings import settings as _s

    limit = int(_s.login_rate_limit_per_min or 0)
    if limit <= 0:
        return

    ip = _client_ip(request)
    user_norm = _normalize_rl_username(username)
    keys = [
        f"auth_rl:ip:{ip}",
        f"auth_rl:user:{user_norm}",
    ]

    redis = get_redis()
    for k in keys:
        try:
            count = await redis.incr(k)
            if count == 1:
                await redis.expire(k, 60)
            if count > limit:
                raise _err(
                    "RATE_LIMITED",
                    "登录尝试过于频繁，请稍后再试",
                    429,
                )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            # Redis 故障：降级到进程内计数器，至少不让暴力破解完全无人看管
            log.warning("login rate-limit Redis fail-degraded: %s; using local bucket", e)
            if not _local_rl_check(k, limit):
                raise _err(
                    "RATE_LIMITED",
                    "登录尝试过于频繁，请稍后再试",
                    429,
                ) from None


# ── 注册（仅首次） ────────────────────────────────────────────────
@router.post("/register", response_model=LoginResponse)
async def register(
    req: RegisterRequest, request: Request, response: Response, db: DBSession
) -> LoginResponse:
    """系统首次部署时创建超管账号；只要 ``web_user`` 表非空，本接口禁用。"""
    # 注册接口同样限速，防止暴力枚举 / DoS
    await _enforce_login_rate_limit(request, req.username.strip())

    cnt = (await db.execute(select(func.count(WebUser.id)))).scalar_one()
    if cnt and int(cnt) > 0:
        raise _err("REGISTER_DISABLED", "系统已存在用户，注册接口已禁用", 403)

    user = WebUser(
        username=req.username.strip(),
        password_hash=auth_service.hash_password(req.password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # COUNT 仅用于快速拒绝；真正的并发安全由 web_user.singleton_key 唯一约束保证。
        await db.rollback()
        raise _err("REGISTER_DISABLED", "系统已存在用户，注册接口已禁用", 403) from exc
    await audit.write(db, user.id, "auth.register", target=f"user:{user.id}")
    await db.commit()

    # 直接颁发 cookie，节省一次 login 请求
    from ..settings import settings as _s  # 局部 import 减小模块顶部依赖

    token = auth_service.issue_jwt_token(user.id, getattr(user, "pwd_version", 0) or 0)
    _set_auth_cookie(response, token, _s.jwt_expire_seconds)
    return LoginResponse(ok=True, require_totp=False)


# ── 登录 ──────────────────────────────────────────────────────────
@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request, response: Response, db: DBSession) -> LoginResponse:
    """用户名密码（+ 可选 TOTP）登录，成功后写 HttpOnly cookie。

    特殊情况：``web_user`` 表为空（首次部署还没注册）时返回 ``NO_USER``，
    前端据此切到注册页，避免一直提示"用户名或密码错误"让人摸不着头脑。
    """
    # 登录限速（IP + 用户名 双维度，任一超限即拒）
    await _enforce_login_rate_limit(request, req.username.strip())
    ip = _client_ip(request)
    username_norm = _normalize_rl_username(req.username)

    # 系统尚未创建任何用户：直接返回 NO_USER，前端会引导到注册流程
    user_count = (await db.execute(select(func.count(WebUser.id)))).scalar_one()
    if not user_count or int(user_count) == 0:
        raise _err("NO_USER", "系统尚未创建管理员账号，请先注册", 401)

    user = (
        await db.execute(select(WebUser).where(WebUser.username == req.username.strip()))
    ).scalar_one_or_none()
    login_security = await auth_login_security.get_login_security_config(db)
    # 用户不存在或密码错都返回相同错误；不存在用户时也做一次等价密码校验，
    # 降低用户名枚举的时序侧信道风险。
    if not auth_service.verify_password_with_sentinel(
        req.password, user.password_hash if user else None
    ):
        await auth_login_security.record_login_failure(ip, username_norm, config=login_security)
        raise _err("AUTH_INVALID", "用户名或密码错误", 401)
    if user is None:
        await auth_login_security.record_login_failure(ip, username_norm, config=login_security)
        raise _err("AUTH_INVALID", "用户名或密码错误", 401)

    used_recovery = False
    used_otp = False

    # 服务器一次性恢复码是防锁死兜底：只能在密码正确后使用，可绕过 OTP/TOTP。
    if req.recovery_code:
        recovery_ok = await auth_login_security.verify_recovery_code(
            db,
            user=user,
            code=req.recovery_code,
        )
        if not recovery_ok:
            raise _err("RECOVERY_CODE_INVALID", "一次性恢复码无效或已过期", 401)
        used_recovery = True

    # TOTP 支持"每次登录"与"连续失败后"两种策略。
    if (
        user.totp_secret_enc
        and not used_recovery
        and await auth_login_security.should_require_totp(
            ip,
            username_norm,
            config=login_security,
        )
    ):
        if not req.totp_code:
            return LoginResponse(ok=False, require_totp=True)
        secret = decrypt_str(user.totp_secret_enc)
        if not auth_service.verify_totp(secret, req.totp_code):
            raise _err("TOTP_INVALID", "动态验证码错误", 401)

    # 密码失败次数过多后，正确密码还需要通知 Bot OTP；通知 Bot 不可用时提示使用恢复码。
    if not used_recovery and await auth_login_security.should_require_login_otp(
        ip,
        username_norm,
        config=login_security,
    ):
        if req.otp_token or req.otp_code:
            otp_ok = await auth_login_security.verify_login_otp(
                token=req.otp_token,
                code=req.otp_code,
                user=user,
                config=login_security,
            )
            if not otp_ok:
                raise _err("OTP_INVALID", "登录验证码错误或已过期", 401)
            used_otp = True
        else:
            challenge = await auth_login_security.issue_login_otp_challenge(
                user=user,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
                config=login_security,
            )
            return LoginResponse(
                ok=False,
                require_otp=True,
                otp_token=challenge.token,
                otp_delivery=challenge.delivery,
                otp_message=challenge.message,
                otp_ttl_seconds=challenge.ttl_seconds,
                recovery_available=True,
            )

    await auth_login_security.clear_login_failures(ip, username_norm)
    action = "auth.login.recovery" if used_recovery else "auth.login.otp" if used_otp else "auth.login"
    await audit.write(db, user.id, action, target=f"user:{user.id}")
    await db.commit()

    from ..settings import settings as _s

    token = auth_service.issue_jwt_token(user.id, getattr(user, "pwd_version", 0) or 0)
    _set_auth_cookie(response, token, _s.jwt_expire_seconds)
    return LoginResponse(ok=True, require_totp=False)


# ── 注销 ──────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    """清除 auth_token cookie。"""
    from ..settings import settings as _s

    # delete_cookie 必须把 path/samesite/secure 等属性匹配上，浏览器才会真正清理
    response.delete_cookie(
        _COOKIE_NAME,
        samesite="lax",
        secure=_s.cookie_secure,
        httponly=True,
    )
    return {"ok": True}


# ── 当前用户 ──────────────────────────────────────────────────────
@router.get("/me", response_model=CurrentUserSchema)
async def me(user: CurrentUser) -> CurrentUserSchema:
    """返回当前登录用户信息。"""
    return CurrentUserSchema(
        id=user.id,
        username=user.username,
        has_totp=bool(user.totp_secret_enc),
    )


# ── TOTP 启用：先生成 secret，暂存到 Redis，verify 通过后再落库 ────────────────
# key 按 user.id 隔离，TTL 5 分钟；避免把 base32 secret 放到浏览器 cookie。
_PENDING_TOTP_REDIS_KEY_PREFIX = "auth:pending_totp"


@router.post("/totp/enable", response_model=TotpEnableResponse)
async def totp_enable(
    user: CurrentUser, response: Response, request: Request
) -> TotpEnableResponse:
    """生成新的 TOTP secret 与 otpauth url；尚未校验前不会落库到 user 表。"""
    from ..redis_client import get_redis

    secret = auth_service.generate_totp_secret()
    otpauth_url = auth_service.make_otpauth_url(user.username, secret)
    redis = get_redis()
    key = f"{_PENDING_TOTP_REDIS_KEY_PREFIX}:{user.id}"
    await redis.set(key, secret, ex=300)
    # 兼容清理旧实现可能残留的 pending_totp cookie
    response.delete_cookie("pending_totp")
    return TotpEnableResponse(secret=secret, otpauth_url=otpauth_url)


@router.post("/totp/verify")
async def totp_verify(
    req: TotpVerifyRequest,
    user: CurrentUser,
    db: DBSession,
    request: Request,
    response: Response,
) -> dict[str, bool]:
    """用待启用的 secret 校验一次 6 位码，通过则把加密后的 secret 写入 user。"""
    from ..redis_client import get_redis

    redis = get_redis()
    key = f"{_PENDING_TOTP_REDIS_KEY_PREFIX}:{user.id}"
    pending_secret = await redis.get(key)
    if not pending_secret:
        raise _err("TOTP_PENDING_MISSING", "请先调用 /totp/enable 生成密钥")
    if not auth_service.verify_totp(pending_secret, req.code):
        raise _err("TOTP_INVALID", "动态验证码错误", 401)

    user.totp_secret_enc = encrypt_str(pending_secret)
    await audit.write(db, user.id, "auth.totp.enable", target=f"user:{user.id}")
    await db.commit()

    await redis.delete(key)
    response.delete_cookie("pending_totp")
    return {"ok": True}


# ── 修改密码 ──────────────────────────────────────────────────────
# 必须验旧密码（防 cookie 被偷后默默改密）；改后强制清 cookie 让用户重登。
@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: CurrentUser,
    db: DBSession,
    response: Response,
) -> dict[str, bool]:
    """修改当前用户密码。

    - 旧密码错误：返回 ``AUTH_INVALID``
    - 旧新相同：返回 ``PWD_SAME``
    - 成功后清 ``auth_token`` cookie，让用户用新密码重新登录
    """
    if not auth_service.verify_password(req.old_password, user.password_hash):
        raise _err("AUTH_INVALID", "旧密码错误", 401)
    if req.old_password == req.new_password:
        raise _err("PWD_SAME", "新密码不能与旧密码相同")

    user.password_hash = auth_service.hash_password(req.new_password)
    user.pwd_version = int(getattr(user, "pwd_version", 0) or 0) + 1
    await audit.write(db, user.id, "auth.change_password", target=f"user:{user.id}")
    await db.commit()

    # 强制下线：清当前 session 的 cookie，前端拦到 401 会跳登录页
    from ..settings import settings as _s

    response.delete_cookie(
        _COOKIE_NAME,
        samesite="lax",
        secure=_s.cookie_secure,
        httponly=True,
    )
    return {"ok": True}


# ── 禁用 TOTP ────────────────────────────────────────────────────
# 要求当前 TOTP 码做最后一次校验，避免 cookie 被劫持后被悄悄关闭 2FA
@router.post("/totp/disable")
async def totp_disable(
    req: TotpDisableRequest,
    user: CurrentUser,
    db: DBSession,
) -> dict[str, bool]:
    """禁用当前用户的 TOTP（必须提供有效的当前 TOTP 码）。"""
    if not user.totp_secret_enc:
        raise _err("TOTP_NOT_ENABLED", "尚未启用 TOTP")
    secret = decrypt_str(user.totp_secret_enc)
    if not auth_service.verify_totp(secret, req.code):
        raise _err("TOTP_INVALID", "动态验证码错误", 401)

    user.totp_secret_enc = None
    await audit.write(db, user.id, "auth.totp.disable", target=f"user:{user.id}")
    await db.commit()
    return {"ok": True}
