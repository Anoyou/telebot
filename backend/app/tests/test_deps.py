from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import deps


class _FakeResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _FakeDB:
    def __init__(self, user):
        self._user = user
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self._user)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_get_current_user_requires_auth_cookie():
    with pytest.raises(deps.HTTPException) as exc_info:
        await deps.get_current_user(_FakeDB(None), None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_get_current_user_invalid_token_claims(monkeypatch):
    monkeypatch.setattr(deps, "_decode_token_claims", lambda _t: None)
    with pytest.raises(deps.HTTPException) as exc_info:
        await deps.get_current_user(_FakeDB(None), "bad-token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_INVALID"


@pytest.mark.asyncio
async def test_get_current_user_rejects_pwd_version_mismatch(monkeypatch):
    monkeypatch.setattr(deps, "_decode_token_claims", lambda _t: {"sub": "1", "pwd_v": 1})
    db = _FakeDB(SimpleNamespace(id=1, pwd_version=2))
    with pytest.raises(deps.HTTPException) as exc_info:
        await deps.get_current_user(db, "token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_INVALIDATED"
    assert db.committed is False


@pytest.mark.asyncio
async def test_get_current_user_success(monkeypatch):
    monkeypatch.setattr(deps, "_decode_token_claims", lambda _t: {"sub": "1", "pwd_v": 2})
    user = SimpleNamespace(id=1, pwd_version=2)
    db = _FakeDB(user)
    got = await deps.get_current_user(db, "token")
    assert got is user
    assert db.committed is True
