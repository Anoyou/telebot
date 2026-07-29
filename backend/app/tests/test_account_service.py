"""account_service 的单元测试（不连真 DB/Redis）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.account import AccountUpdateRequest
from app.services import account_service
from app.worker import supervisor


@pytest.mark.asyncio
async def test_update_account_proxy_changed_triggers_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """当 proxy_id/template_id 发生变化时，必须触发 worker 重启。"""
    acc = SimpleNamespace(id=1, proxy_id=10, template_id=20, display_name="old")
    db = AsyncMock()
    db.get = AsyncMock(return_value=acc)
    db.commit = AsyncMock(return_value=None)

    published: list[tuple[str, str]] = []

    async def _fake_publish(channel: str, payload: str) -> None:
        published.append((channel, payload))

    monkeypatch.setattr(account_service, "_publish", _fake_publish)
    monkeypatch.setattr(
        account_service,
        "get_account",
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )

    await account_service.update_account(
        db,
        1,
        AccountUpdateRequest(proxy_id=11, template_id=20, display_name="new"),
    )

    assert acc.proxy_id == 11
    assert len(published) == 2
    channel0, payload0 = published[0]
    channel1, payload1 = published[1]
    assert channel0 == account_service.cmd_channel(1)
    assert '"type":"stop"' in payload0
    assert channel1 == account_service.GLOBAL_CHANNEL
    assert '"type":"start_worker"' in payload1
    assert '"account_id":1' in payload1


@pytest.mark.asyncio
async def test_update_account_non_runtime_fields_no_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅改 display_name/notes/tags 等非运行时字段时，不应通知 reload。"""
    acc = SimpleNamespace(id=2, proxy_id=10, template_id=20, display_name="old")
    db = AsyncMock()
    db.get = AsyncMock(return_value=acc)
    db.commit = AsyncMock(return_value=None)

    publish_mock = AsyncMock()
    monkeypatch.setattr(account_service, "_publish", publish_mock)
    monkeypatch.setattr(
        account_service,
        "get_account",
        AsyncMock(return_value=SimpleNamespace(id=2)),
    )

    await account_service.update_account(
        db,
        2,
        AccountUpdateRequest(display_name="new-name", notes="n"),
    )

    publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_account_not_found() -> None:
    """账号不存在应返回 ACCOUNT_NOT_FOUND。"""
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(account_service.HTTPException) as exc_info:
        await account_service.update_account(db, 404, AccountUpdateRequest(display_name="x"))
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "ACCOUNT_NOT_FOUND"


@pytest.mark.asyncio
async def test_pause_stops_supervisor_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """暂停账号必须真正停止 supervisor 托管的 worker。"""
    acc = SimpleNamespace(id=3, status=account_service.ACCOUNT_STATUS_ACTIVE)
    db = AsyncMock()
    db.get = AsyncMock(return_value=acc)
    db.commit = AsyncMock(return_value=None)

    stop_worker = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(supervisor, "stop_worker", stop_worker)
    monkeypatch.setattr(account_service, "_publish", publish)

    await account_service.pause(db, 3)

    assert acc.status == account_service.ACCOUNT_STATUS_PAUSED
    db.commit.assert_awaited_once()
    stop_worker.assert_awaited_once_with(3)
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_starts_supervisor_worker_when_kill_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复账号时，总闸关闭才会立即拉起对应 worker。"""
    acc = SimpleNamespace(id=4, status=account_service.ACCOUNT_STATUS_PAUSED)
    db = AsyncMock()
    db.get = AsyncMock(return_value=acc)
    db.commit = AsyncMock(return_value=None)

    start_worker = AsyncMock()
    monkeypatch.setattr(supervisor, "start_worker", start_worker)
    monkeypatch.setattr(account_service, "_kill_switch_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(account_service, "ensure_account_secrets_decryptable", lambda _acc: None)

    await account_service.resume(db, 4)

    assert acc.status == account_service.ACCOUNT_STATUS_ACTIVE
    db.commit.assert_awaited_once()
    start_worker.assert_awaited_once_with(4)


@pytest.mark.asyncio
async def test_resume_does_not_start_worker_when_kill_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """紧急停用开启时，恢复账号只写 active 状态，不绕过总闸启动 worker。"""
    acc = SimpleNamespace(id=5, status=account_service.ACCOUNT_STATUS_PAUSED)
    db = AsyncMock()
    db.get = AsyncMock(return_value=acc)
    db.commit = AsyncMock(return_value=None)

    start_worker = AsyncMock()
    monkeypatch.setattr(supervisor, "start_worker", start_worker)
    monkeypatch.setattr(account_service, "_kill_switch_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(account_service, "ensure_account_secrets_decryptable", lambda _acc: None)

    await account_service.resume(db, 5)

    assert acc.status == account_service.ACCOUNT_STATUS_ACTIVE
    db.commit.assert_awaited_once()
    start_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_marks_login_required_when_session_cannot_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """MASTER_KEY 不匹配时，恢复账号应直接提示重新登录，而不是启动后反复 down。"""
    acc = SimpleNamespace(
        id=6,
        status=account_service.ACCOUNT_STATUS_PAUSED,
        session_enc=b"bad-session",
        api_id_enc="bad-api-id",
        api_hash_enc="bad-api-hash",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=acc)
    db.commit = AsyncMock(return_value=None)

    monkeypatch.setattr(
        account_service,
        "decrypt_bytes",
        lambda _value: (_ for _ in ()).throw(ValueError("解密失败：可能 MASTER_KEY 已变更")),
    )

    with pytest.raises(account_service.HTTPException) as exc_info:
        await account_service.resume(db, 6)

    assert acc.status == account_service.ACCOUNT_STATUS_LOGIN_REQUIRED
    db.commit.assert_awaited_once()
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "ACCOUNT_SESSION_DECRYPT_FAILED"
