"""账号绑定向导（``services/login_service.py``）的状态机单元测试。

不连真 Telethon、不连 DB：mock ``TelegramClient`` 验证状态机分支。
端到端 HTTP 测试需要 PG 方言（``ARRAY/BYTEA``），整合阶段再开。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from app.crypto import decrypt_bytes, decrypt_str, encrypt_bytes, encrypt_str
from app.db.models.account import ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_LOGIN_REQUIRED, Account
from app.services import login_service
from app.services.device_profile import HARDCODED_FALLBACK


# ── 公共 fixture ───────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_pending_table():
    """每个测试都从干净的 _PENDING / 锁状态开始。"""
    login_service._PENDING.clear()
    yield
    login_service._PENDING.clear()


@pytest.fixture(autouse=True)
def _stub_device_profile():
    """跳过 device_profile.get_default 的真实 DB 查询，直接返回 HARDCODED_FALLBACK。

    Sprint 2 #1 把 device_profile.get_default 接入 login_service.start_login 后，
    这些用 AsyncMock(db) 的测试会让 ``db.execute(...).scalar_one_or_none()`` 返回
    一个 truthy 的 AsyncMock，导致 ``_from_row(row)`` 取属性时拿到 coroutine。
    在测试环境直接 stub 掉，让登录状态机的测试聚焦在登录逻辑本身。

    login_service 用局部 import（``from .device_profile import get_default``），
    必须 patch 源模块本身。
    """
    with patch(
        "app.services.device_profile.get_default",
        AsyncMock(return_value=HARDCODED_FALLBACK),
    ), patch(
        "app.services.device_profile.get_by_id",
        AsyncMock(return_value=HARDCODED_FALLBACK),
    ):
        yield


def _make_fake_client(*, connect_exc=None, send_code_exc=None) -> AsyncMock:
    """构造一个 mock TelegramClient；可注入 send_code_request 抛出的异常。"""
    client = AsyncMock()
    if connect_exc is not None:
        client.connect = AsyncMock(side_effect=connect_exc)
    else:
        client.connect = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    if send_code_exc is not None:
        client.send_code_request = AsyncMock(side_effect=send_code_exc)
    else:
        sent = AsyncMock()
        sent.phone_code_hash = "fake_hash_xyz"
        client.send_code_request = AsyncMock(return_value=sent)
    return client


# ── start_login ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_login_success_returns_token():
    """正常路径：发码成功，返回 login_token，并在 _PENDING 中可查到。"""
    fake_client = _make_fake_client()
    db = AsyncMock()
    # 不带 proxy_id，所以不会查 DB
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(
            db, api_id=1, api_hash="hash", phone="+8613800000000"
        )
    assert isinstance(token, str) and token
    pending = await login_service.get_pending(token)
    assert pending is not None
    assert pending.phone == "+8613800000000"
    assert pending.phone_code_hash == "fake_hash_xyz"
    fake_client.connect.assert_awaited_once()
    fake_client.send_code_request.assert_awaited_once_with("+8613800000000")


@pytest.mark.asyncio
async def test_start_login_relogin_rejects_phone_mismatch():
    """重登必须锁定原手机号，避免覆盖成另一个 Telegram 账号。"""

    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=7, phone="+8613800000000"))

    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.start_login(
            db,
            api_id=1,
            api_hash="h",
            phone="+8613900000000",
            account_id=7,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ACCOUNT_PHONE_MISMATCH"


@pytest.mark.asyncio
async def test_start_login_phone_invalid_disconnects_and_raises():
    """PhoneNumberInvalidError 须回收 client 并向上抛 PHONE_INVALID。"""
    fake_client = _make_fake_client(send_code_exc=PhoneNumberInvalidError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        with pytest.raises(login_service.HTTPException) as exc_info:
            await login_service.start_login(db, api_id=1, api_hash="h", phone="bad")
    assert exc_info.value.detail["code"] == "PHONE_INVALID"
    fake_client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_start_login_connect_error_is_structured_and_disconnects():
    """Telegram 连接失败应返回结构化错误，不再冒泡成 500。"""
    fake_client = _make_fake_client(connect_exc=ConnectionError("Connection to Telegram failed 5 time(s)"))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        with pytest.raises(login_service.HTTPException) as exc_info:
            await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "LOGIN_START_FAILED"
    assert "检查代理与标识" in exc_info.value.detail["message"]
    fake_client.disconnect.assert_awaited()


# ── confirm_code ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_confirm_code_success_no_2fa():
    """验证码正确且账号未启用 2FA：返回 (False, pending)。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(return_value=None)
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    require_2fa, pending = await login_service.confirm_code(token, "12345")
    assert require_2fa is False
    assert pending.require_2fa is False
    fake_client.sign_in.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_code_claim_rejects_concurrent_duplicate() -> None:
    """同一个 token 的验证码校验未结束时，第二个请求必须被原子拒绝。"""

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sign_in(**_kwargs) -> None:  # noqa: ANN003
        started.set()
        await release.wait()

    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=slow_sign_in)
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(
            AsyncMock(),
            api_id=1,
            api_hash="h",
            phone="+1",
        )

    first = asyncio.create_task(login_service.confirm_code(token, "12345"))
    await started.wait()
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_code(token, "12345")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "LOGIN_TOKEN_IN_USE"

    release.set()
    require_2fa, pending = await first
    assert require_2fa is False
    assert pending.claimed is True
    assert fake_client.sign_in.await_count == 1


@pytest.mark.asyncio
async def test_confirm_code_unexpected_error_releases_claim() -> None:
    """网络类未知异常不能永久锁死 token，用户应可重试。"""

    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=ConnectionError("temporary"))
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(
            AsyncMock(),
            api_id=1,
            api_hash="h",
            phone="+1",
        )

    with pytest.raises(ConnectionError):
        await login_service.confirm_code(token, "12345")

    assert login_service._PENDING[token].claimed is False


@pytest.mark.asyncio
async def test_finalize_failure_releases_claim_for_retry() -> None:
    """落库前读取 Telegram 身份失败时，token 不应永久锁死。"""

    fake_client = _make_fake_client()
    fake_client.get_me = AsyncMock(side_effect=ConnectionError("temporary"))
    pending = login_service._PendingLogin(
        client=fake_client,
        api_id=1,
        api_hash="h",
        phone="+1",
        claimed=True,
    )
    token = "finalize-retry-token"
    login_service._PENDING[token] = pending

    with pytest.raises(ConnectionError):
        await login_service.finalize(AsyncMock(), token, pending)

    assert pending.claimed is False


@pytest.mark.asyncio
async def test_confirm_code_2fa_required():
    """sign_in 抛 SessionPasswordNeededError → require_2fa=True 且 pending 仍在。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    require_2fa, pending = await login_service.confirm_code(token, "12345")
    assert require_2fa is True
    assert pending.require_2fa is True
    # pending 仍可查（等下一步 confirm_2fa）
    assert await login_service.get_pending(token) is pending


@pytest.mark.asyncio
async def test_confirm_code_invalid_keeps_pending():
    """PhoneCodeInvalidError → 抛 CODE_INVALID，pending 仍保留可重试。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_code(token, "wrong")
    assert exc_info.value.detail["code"] == "CODE_INVALID"
    # 仍可重试
    assert await login_service.get_pending(token) is not None


@pytest.mark.asyncio
async def test_confirm_code_invalid_exceeded_clears_pending_and_disconnects():
    """验证码连续错误超限：返回 LOGIN_ATTEMPTS_EXCEEDED，token 作废并回收 client。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    for _ in range(4):
        with pytest.raises(login_service.HTTPException) as exc_info:
            await login_service.confirm_code(token, "wrong")
        assert exc_info.value.detail["code"] == "CODE_INVALID"
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_code(token, "wrong")
    assert exc_info.value.detail["code"] == "LOGIN_ATTEMPTS_EXCEEDED"
    assert await login_service.get_pending(token) is None
    fake_client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_code_expired_clears_pending():
    """PhoneCodeExpiredError → 整个会话作废，pending 被清掉。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=PhoneCodeExpiredError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_code(token, "expired")
    assert exc_info.value.detail["code"] == "CODE_EXPIRED"
    assert await login_service.get_pending(token) is None


@pytest.mark.asyncio
async def test_confirm_code_unknown_token():
    """token 不存在（已过期或从未发起）→ LOGIN_TOKEN_EXPIRED。"""
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_code("no-such-token", "12345")
    assert exc_info.value.detail["code"] == "LOGIN_TOKEN_EXPIRED"


# ── confirm_2fa ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_confirm_2fa_success():
    """正确密码：sign_in 成功，返回 pending。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(return_value=None)
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    pending = await login_service.confirm_2fa(token, "my-2fa")
    assert pending is not None
    fake_client.sign_in.assert_awaited_with(password="my-2fa")


@pytest.mark.asyncio
async def test_confirm_2fa_invalid_password():
    """密码错 → PASSWORD_INVALID。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=PasswordHashInvalidError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_2fa(token, "wrong")
    assert exc_info.value.detail["code"] == "PASSWORD_INVALID"


@pytest.mark.asyncio
async def test_confirm_2fa_invalid_exceeded_clears_pending_and_disconnects():
    """2FA 密码连续错误超限：返回 LOGIN_ATTEMPTS_EXCEEDED，token 作废并回收 client。"""
    fake_client = _make_fake_client()
    fake_client.sign_in = AsyncMock(side_effect=PasswordHashInvalidError(request=None))
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    for _ in range(4):
        with pytest.raises(login_service.HTTPException) as exc_info:
            await login_service.confirm_2fa(token, "wrong")
        assert exc_info.value.detail["code"] == "PASSWORD_INVALID"
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.confirm_2fa(token, "wrong")
    assert exc_info.value.detail["code"] == "LOGIN_ATTEMPTS_EXCEEDED"
    assert await login_service.get_pending(token) is None
    fake_client.disconnect.assert_awaited()


# ── 后台 TTL 清理 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pending_ttl_cleanup_logic():
    """直接验证 TTL 清理逻辑：把 created_at 调到 31 分钟前，再手动跑一轮清理。"""
    from datetime import datetime

    fake_client = _make_fake_client()
    fake_client.disconnect = AsyncMock()
    db = AsyncMock()
    with patch.object(login_service, "TelegramClient", return_value=fake_client):
        token = await login_service.start_login(db, api_id=1, api_hash="h", phone="+1")
    pending = login_service._PENDING[token]
    pending.created_at = datetime.now(UTC) - timedelta(minutes=31)

    # 复刻 cleanup_expired_loop 内的清理段
    now = datetime.now(UTC)
    expired = []
    async with login_service._LOCK:
        for tok, p in list(login_service._PENDING.items()):
            if now - p.created_at > login_service._PENDING_TTL:
                expired.append(p)
                login_service._PENDING.pop(tok, None)
    for p in expired:
        await login_service._safe_disconnect(p.client)

    assert token not in login_service._PENDING
    fake_client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_start_login_rejects_when_pending_limit_exceeded(monkeypatch):
    """挂起登录达到上限时，start_login 应直接返回 429。"""
    monkeypatch.setattr(login_service.settings, "max_pending_logins", 1)
    login_service._PENDING["occupied"] = login_service._PendingLogin(
        client=AsyncMock(),
        api_id=1,
        api_hash="h",
        phone="+1",
    )
    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.start_login(
            AsyncMock(),
            api_id=2,
            api_hash="h2",
            phone="+8613800000000",
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "LOGIN_PENDING_LIMITED"


@pytest.mark.asyncio
async def test_finalize_relogin_overwrites_session_and_keeps_account_id():
    """重登已有账号时覆盖 session/API 凭据，不新建账号也不清配置。"""

    old = Account(
        id=7,
        phone="+8613800000000",
        display_name="旧账号",
        tg_user_id=12345,
        api_id_enc=encrypt_str("old-api-id"),
        api_hash_enc=encrypt_str("old-api-hash"),
        session_enc=encrypt_bytes(b"old-session"),
        status=ACCOUNT_STATUS_LOGIN_REQUIRED,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=old)
    db.commit = AsyncMock()
    fake_client = AsyncMock()
    fake_client.get_me = AsyncMock(
        return_value=SimpleNamespace(id=12345, username="new_name", first_name="新名字")
    )
    fake_client.session = SimpleNamespace(save=lambda: "new-session")
    fake_client.disconnect = AsyncMock(return_value=None)
    pending = login_service._PendingLogin(
        client=fake_client,
        api_id=999,
        api_hash="new-api-hash",
        phone="+8613800000000",
        account_id=7,
        proxy_id=3,
    )
    token = "relogin-token"
    login_service._PENDING[token] = pending

    with patch.object(login_service, "get_redis", side_effect=RuntimeError("no redis")):
        aid = await login_service.finalize(db, token, pending)

    assert aid == 7
    assert old.status == ACCOUNT_STATUS_ACTIVE
    assert old.proxy_id == 3
    assert old.tg_username == "new_name"
    assert decrypt_str(old.api_id_enc) == "999"
    assert decrypt_str(old.api_hash_enc) == "new-api-hash"
    assert decrypt_bytes(old.session_enc).decode() == "new-session"
    assert token not in login_service._PENDING


@pytest.mark.asyncio
async def test_finalize_relogin_rejects_identity_mismatch():
    """已知 tg_user_id 的老账号不能被另一个 Telegram 用户覆盖。"""

    old = Account(
        id=7,
        phone="+8613800000000",
        display_name="旧账号",
        tg_user_id=12345,
        api_id_enc=encrypt_str("old-api-id"),
        api_hash_enc=encrypt_str("old-api-hash"),
        session_enc=encrypt_bytes(b"old-session"),
        status=ACCOUNT_STATUS_LOGIN_REQUIRED,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=old)
    fake_client = AsyncMock()
    fake_client.get_me = AsyncMock(return_value=SimpleNamespace(id=99999, username="wrong"))
    fake_client.session = SimpleNamespace(save=lambda: "wrong-session")
    fake_client.disconnect = AsyncMock(return_value=None)
    pending = login_service._PendingLogin(
        client=fake_client,
        api_id=999,
        api_hash="new-api-hash",
        phone="+8613800000000",
        account_id=7,
    )
    token = "wrong-token"
    login_service._PENDING[token] = pending

    with pytest.raises(login_service.HTTPException) as exc_info:
        await login_service.finalize(db, token, pending)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ACCOUNT_IDENTITY_MISMATCH"
    assert decrypt_bytes(old.session_enc) == b"old-session"
    assert token not in login_service._PENDING
    fake_client.disconnect.assert_awaited()


# ── 端到端（占位，等整合阶段连真 PG 时启用） ──────────────────────
@pytest.mark.skip(reason="端到端 HTTP 测试需要 PG 方言；整合阶段补全 DB fixture")
async def test_login_wizard_e2e():
    """演示 /login/start → /login/code → finalize 全链路，整合阶段补全。"""
    pass
