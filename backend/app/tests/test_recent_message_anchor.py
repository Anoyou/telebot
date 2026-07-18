from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerChannel, PeerUser

from app.services import recent_message_anchor


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None):
        self.data[key] = value
        self.set_calls.append((key, value, ex))
        return True


def _event(*, from_id, sender_id: int, chat_id: int = -1001, message_id: int = 55):  # noqa: ANN001
    return SimpleNamespace(
        chat_id=chat_id,
        sender_id=sender_id,
        message=SimpleNamespace(id=message_id, from_id=from_id),
    )


@pytest.mark.asyncio
async def test_cache_only_uses_message_peer_user_identity() -> None:
    redis = _Redis()

    cached = await recent_message_anchor.cache_incoming_user_message(
        redis,
        7,
        _event(from_id=PeerUser(123), sender_id=999),
    )

    assert cached is True
    assert redis.set_calls == [
        (
            recent_message_anchor.anchor_key(7, -1001, 123),
            "55",
            recent_message_anchor.ANCHOR_TTL_SECONDS,
        )
    ]
    assert recent_message_anchor.anchor_key(7, -1001, 999) not in redis.data


@pytest.mark.asyncio
async def test_anonymous_admin_and_channel_identity_never_create_user_anchor() -> None:
    redis = _Redis()

    anonymous = await recent_message_anchor.cache_incoming_user_message(
        redis,
        7,
        _event(from_id=PeerChannel(1001), sender_id=123),
    )
    missing_from_id = await recent_message_anchor.cache_incoming_user_message(
        redis,
        7,
        _event(from_id=None, sender_id=123),
    )

    assert anonymous is False
    assert missing_from_id is False
    assert redis.data == {}


@pytest.mark.asyncio
async def test_cached_anchor_wins_without_telegram_history_request() -> None:
    redis = _Redis()
    key = recent_message_anchor.anchor_key(7, -1001, 123)
    assert key is not None
    redis.data[key] = "88"

    class _Client:
        def iter_messages(self, *_args, **_kwargs):
            raise AssertionError("命中缓存时不应扫描 Telegram 历史")

    found = await recent_message_anchor.find_recent_message_id_for_user(
        _Client(),
        -1001,
        123,
        limit=5000,
        redis=redis,
        account_id=7,
    )

    assert found == 88


@pytest.mark.asyncio
async def test_exact_search_failure_falls_back_to_5000_strict_peer_user_messages() -> None:
    calls: list[dict[str, int]] = []

    class _Client:
        def iter_messages(self, _chat_id, **kwargs):  # noqa: ANN001, ANN003
            calls.append(dict(kwargs))

            async def _messages():
                if kwargs.get("from_user") is not None:
                    raise RuntimeError("Could not find input entity for PeerUser")
                yield SimpleNamespace(id=90, sender_id=123, from_id=PeerChannel(1001))
                yield SimpleNamespace(id=89, sender_id=123, from_id=PeerUser(123))

            return _messages()

    found = await recent_message_anchor.find_recent_message_id_for_user(
        _Client(),
        -1001,
        123,
        limit=recent_message_anchor.normalize_search_limit(None),
    )

    assert found == 89
    assert calls == [{"from_user": 123, "limit": 5000}, {"limit": 5000}]


def test_search_limit_defaults_and_caps_at_5000() -> None:
    assert recent_message_anchor.normalize_search_limit(None) == 5000
    assert recent_message_anchor.normalize_search_limit(9000) == 5000
    assert recent_message_anchor.normalize_search_limit(40) == 40
