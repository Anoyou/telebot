"""Web 登录二次校验与本地恢复码。

设计目标是先保护登录入口，又不把账号主人锁在系统外：

- 密码错误次数达到阈值后，下一次正确密码需要通知 Bot OTP。
- 通知 Bot 不可用时，SSH 到服务器生成一次性恢复码即可兜底登录。
- 恢复码只在密码正确后校验，可绕过 OTP/TOTP 这一层，但不能绕过密码。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.system import SystemSetting
from ..db.models.user import WebUser
from ..settings import settings
from . import notify_service

log = logging.getLogger(__name__)

RECOVERY_SETTING_KEY = "auth_login_recovery_code"

_LOCAL_FAILS: dict[str, tuple[int, float]] = {}
_LOCAL_CHALLENGES: dict[str, dict[str, Any]] = {}


@dataclass(slots=True)
class LoginSecurityConfig:
    notify_otp_enabled: bool
    notify_otp_failed_attempt_threshold: int
    notify_otp_fail_window_seconds: int
    notify_otp_ttl_seconds: int
    notify_otp_max_attempts: int
    totp_enabled: bool
    recovery_code_ttl_seconds: int


@dataclass(slots=True)
class LoginOtpChallenge:
    token: str | None
    sent: bool
    delivery: str
    message: str
    ttl_seconds: int


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        out = int(value)
    except Exception:  # noqa: BLE001
        out = default
    return max(minimum, min(maximum, out))


def normalize_login_security_config(raw: Any | None = None) -> LoginSecurityConfig:
    data = raw if isinstance(raw, dict) else {}
    notify_enabled = bool(data.get("notify_otp_enabled", False))
    threshold_default = int(getattr(settings, "login_otp_failed_attempt_threshold", 0) or 0)
    return LoginSecurityConfig(
        notify_otp_enabled=notify_enabled,
        notify_otp_failed_attempt_threshold=_clamp_int(
            data.get("notify_otp_failed_attempt_threshold", threshold_default),
            default=threshold_default,
            minimum=0,
            maximum=50,
        ),
        notify_otp_fail_window_seconds=_clamp_int(
            data.get(
                "notify_otp_fail_window_seconds",
                getattr(settings, "login_otp_fail_window_seconds", 900),
            ),
            default=900,
            minimum=60,
            maximum=86400,
        ),
        notify_otp_ttl_seconds=_clamp_int(
            data.get("notify_otp_ttl_seconds", getattr(settings, "login_otp_ttl_seconds", 300)),
            default=300,
            minimum=60,
            maximum=1800,
        ),
        notify_otp_max_attempts=_clamp_int(
            data.get("notify_otp_max_attempts", getattr(settings, "login_otp_max_attempts", 3)),
            default=3,
            minimum=1,
            maximum=10,
        ),
        totp_enabled=bool(data.get("totp_enabled", False)),
        recovery_code_ttl_seconds=_clamp_int(
            data.get("recovery_code_ttl_seconds", 900),
            default=900,
            minimum=60,
            maximum=86400,
        ),
    )


def login_security_config_to_dict(config: LoginSecurityConfig) -> dict[str, Any]:
    return {
        "notify_otp_enabled": config.notify_otp_enabled,
        "notify_otp_failed_attempt_threshold": config.notify_otp_failed_attempt_threshold,
        "notify_otp_fail_window_seconds": config.notify_otp_fail_window_seconds,
        "notify_otp_ttl_seconds": config.notify_otp_ttl_seconds,
        "notify_otp_max_attempts": config.notify_otp_max_attempts,
        "totp_enabled": config.totp_enabled,
        "recovery_code_ttl_seconds": config.recovery_code_ttl_seconds,
    }


async def get_login_security_config(db: AsyncSession) -> LoginSecurityConfig:
    row = await db.get(SystemSetting, "login_security")
    raw = row.value if row is not None else None
    return normalize_login_security_config(raw)


def _now_ts() -> int:
    return int(time.time())


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _safe_ttl(value: int | None, fallback: int) -> int:
    try:
        out = int(value or 0)
    except Exception:  # noqa: BLE001
        out = 0
    return out if out > 0 else fallback


def _hash_secret(secret: str, salt: str) -> str:
    payload = f"{salt}:{secret}".encode()
    return hashlib.sha256(payload).hexdigest()


def _token_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"auth:login_otp:{digest}"


def _fail_keys(ip: str, username_norm: str) -> list[str]:
    return [f"auth:login_fail:ip:{ip}", f"auth:login_fail:user:{username_norm}"]


def _loads(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        data = json.loads(str(raw))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _get_redis():
    from ..redis_client import get_redis

    return get_redis()


def _local_incr(key: str, ttl: int) -> int:
    now = time.time()
    count, expires_at = _LOCAL_FAILS.get(key, (0, now + ttl))
    if expires_at <= now:
        count, expires_at = 0, now + ttl
    count += 1
    _LOCAL_FAILS[key] = (count, expires_at)
    if len(_LOCAL_FAILS) > 2000:
        for k, (_, exp) in list(_LOCAL_FAILS.items()):
            if exp <= now:
                _LOCAL_FAILS.pop(k, None)
    return count


def _local_get_count(key: str) -> int:
    item = _LOCAL_FAILS.get(key)
    if not item:
        return 0
    count, expires_at = item
    if expires_at <= time.time():
        _LOCAL_FAILS.pop(key, None)
        return 0
    return int(count)


async def record_login_failure(
    ip: str,
    username_norm: str,
    *,
    config: LoginSecurityConfig | None = None,
) -> None:
    """记录一次登录密码失败。Redis 故障时降级到进程内计数。"""

    config = config or normalize_login_security_config()
    if not config.notify_otp_enabled:
        return

    ttl = config.notify_otp_fail_window_seconds
    keys = _fail_keys(ip, username_norm)
    try:
        redis = _get_redis()
        for key in keys:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, ttl)
    except Exception as exc:  # noqa: BLE001
        log.warning("auth login failure counter Redis fail-degraded: %s", exc)
        for key in keys:
            _local_incr(key, ttl)


async def clear_login_failures(ip: str, username_norm: str) -> None:
    keys = _fail_keys(ip, username_norm)
    try:
        redis = _get_redis()
        await redis.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        log.warning("auth login failure counter clear failed: %s", exc)
    for key in keys:
        _LOCAL_FAILS.pop(key, None)


async def should_require_login_otp(
    ip: str,
    username_norm: str,
    *,
    config: LoginSecurityConfig | None = None,
) -> bool:
    config = config or normalize_login_security_config()
    if not config.notify_otp_enabled:
        return False
    threshold = int(config.notify_otp_failed_attempt_threshold or 0)
    if threshold <= 0:
        return False
    keys = _fail_keys(ip, username_norm)
    try:
        redis = _get_redis()
        counts: list[int] = []
        for key in keys:
            raw = await redis.get(key)
            try:
                counts.append(int(raw or 0))
            except Exception:  # noqa: BLE001
                counts.append(0)
        return max(counts or [0]) >= threshold
    except Exception as exc:  # noqa: BLE001
        log.warning("auth login failure counter read failed: %s", exc)
        return max((_local_get_count(key) for key in keys), default=0) >= threshold


def _make_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _normalize_otp(raw: str | None) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())[:12]


async def _store_challenge(key: str, payload: dict[str, Any], ttl: int) -> None:
    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        redis = _get_redis()
        await redis.set(key, payload_raw, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        log.warning("auth login OTP Redis store failed; using local challenge: %s", exc)
        _LOCAL_CHALLENGES[key] = payload


async def _load_challenge(key: str) -> dict[str, Any] | None:
    try:
        redis = _get_redis()
        data = _loads(await redis.get(key))
        if data is not None:
            return data
    except Exception as exc:  # noqa: BLE001
        log.warning("auth login OTP Redis read failed; using local challenge: %s", exc)

    data = _LOCAL_CHALLENGES.get(key)
    if not data:
        return None
    if int(data.get("expires_at") or 0) <= _now_ts():
        _LOCAL_CHALLENGES.pop(key, None)
        return None
    return dict(data)


async def _delete_challenge(key: str) -> None:
    try:
        redis = _get_redis()
        await redis.delete(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("auth login OTP Redis delete failed: %s", exc)
    _LOCAL_CHALLENGES.pop(key, None)


async def issue_login_otp_challenge(
    *,
    user: WebUser,
    ip: str,
    user_agent: str | None,
    config: LoginSecurityConfig | None = None,
) -> LoginOtpChallenge:
    """创建并发送一次通知 Bot 登录验证码。"""

    config = config or normalize_login_security_config()
    ttl = config.notify_otp_ttl_seconds
    code = _make_otp_code()
    token = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    key = _token_key(token)
    now = _now_ts()
    payload = {
        "v": 1,
        "user_id": int(user.id),
        "username": str(user.username),
        "ip": ip,
        "user_agent": (user_agent or "")[:300],
        "code_hash": _hash_secret(code, salt),
        "salt": salt,
        "attempts": 0,
        "created_at": now,
        "expires_at": now + ttl,
    }
    await _store_challenge(key, payload, ttl)

    text = (
        "<b>TelePilot 登录验证码</b>\n"
        f"验证码：<code>{code}</code>\n"
        f"有效期：{ttl // 60} 分钟\n"
        f"账号：<code>{escape(str(user.username))}</code>\n"
        f"来源 IP：<code>{escape(str(ip))}</code>\n\n"
        "如果这不是你本人操作，请立即修改后台密码。"
    )
    sent = await notify_service.send(None, text)
    if not sent:
        await _delete_challenge(key)
        return LoginOtpChallenge(
            token=None,
            sent=False,
            delivery="recovery_code",
            message="通知 Bot 不可用，请使用服务器一次性恢复码完成登录",
            ttl_seconds=ttl,
        )

    return LoginOtpChallenge(
        token=token,
        sent=True,
        delivery="notify_bot",
        message="已通过通知 Bot 发送登录验证码",
        ttl_seconds=ttl,
    )


async def verify_login_otp(
    *,
    token: str | None,
    code: str | None,
    user: WebUser,
    config: LoginSecurityConfig | None = None,
) -> bool:
    token = str(token or "").strip()
    code_norm = _normalize_otp(code)
    if not token or len(code_norm) != 6:
        return False

    key = _token_key(token)
    payload = await _load_challenge(key)
    if not payload:
        return False
    if int(payload.get("expires_at") or 0) <= _now_ts():
        await _delete_challenge(key)
        return False
    if int(payload.get("user_id") or 0) != int(user.id):
        return False

    config = config or normalize_login_security_config()
    max_attempts = int(config.notify_otp_max_attempts or 3)
    attempts = int(payload.get("attempts") or 0) + 1
    payload["attempts"] = attempts
    expected = str(payload.get("code_hash") or "")
    salt = str(payload.get("salt") or "")
    ok = bool(expected and salt) and hmac.compare_digest(expected, _hash_secret(code_norm, salt))
    if ok:
        await _delete_challenge(key)
        return True
    if attempts >= max_attempts:
        await _delete_challenge(key)
        return False

    ttl = max(1, int(payload.get("expires_at") or _now_ts()) - _now_ts())
    await _store_challenge(key, payload, ttl)
    return False


_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_recovery_code() -> str:
    groups = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(5))
        for _ in range(4)
    ]
    return "TP-" + "-".join(groups)


def normalize_recovery_code(raw: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


async def create_recovery_code(
    db: AsyncSession,
    *,
    user: WebUser,
    ttl_seconds: int = 900,
) -> tuple[str, datetime]:
    """生成一次性恢复码并写入 system_setting，只保存哈希。"""

    ttl_seconds = _safe_ttl(ttl_seconds, 900)
    code = generate_recovery_code()
    salt = secrets.token_hex(16)
    now = _now_utc()
    expires_at = now + timedelta(seconds=ttl_seconds)
    payload = {
        "v": 1,
        "user_id": int(user.id),
        "username": str(user.username),
        "code_hash": _hash_secret(normalize_recovery_code(code), salt),
        "salt": salt,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "used_at": None,
    }

    row = await db.get(SystemSetting, RECOVERY_SETTING_KEY)
    if row is None:
        row = SystemSetting(key=RECOVERY_SETTING_KEY, value=payload)
        db.add(row)
    else:
        row.value = payload
    return code, expires_at


def _parse_dt(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def verify_recovery_code(
    db: AsyncSession,
    *,
    user: WebUser,
    code: str | None,
) -> bool:
    """校验并消费一次性恢复码。调用方负责最终 commit。"""

    code_norm = normalize_recovery_code(code)
    if not code_norm:
        return False
    row = await db.get(SystemSetting, RECOVERY_SETTING_KEY)
    payload = row.value if row is not None and isinstance(row.value, dict) else None
    if not payload:
        return False
    if int(payload.get("user_id") or 0) != int(user.id):
        return False
    if payload.get("used_at"):
        return False
    expires_at = _parse_dt(payload.get("expires_at"))
    if expires_at is None or expires_at <= _now_utc():
        return False

    expected = str(payload.get("code_hash") or "")
    salt = str(payload.get("salt") or "")
    if not expected or not salt:
        return False
    if not hmac.compare_digest(expected, _hash_secret(code_norm, salt)):
        return False

    payload = dict(payload)
    payload["used_at"] = _now_utc().isoformat()
    row.value = payload
    return True


async def select_recovery_user(db: AsyncSession, username: str | None = None) -> WebUser | None:
    stmt = select(WebUser)
    if username:
        stmt = stmt.where(WebUser.username == username.strip())
    stmt = stmt.order_by(WebUser.id.asc())
    return (await db.execute(stmt)).scalars().first()
