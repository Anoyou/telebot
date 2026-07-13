from __future__ import annotations

import pytest

from app.services.interaction.dedupe import (
    INTERACTION_MESSAGE_CLAIM_TTL_SECONDS,
    claim_interaction_message,
    interaction_message_claim_key,
    release_interaction_message,
)


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def set(self, key: str, value: str, **kwargs):
        self.calls.append((key, value, dict(kwargs)))
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str):
        self.values.pop(key, None)
        return 1


class _BrokenRedis:
    async def set(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("redis down")

    async def delete(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("redis down")


def test_interaction_message_claim_key_includes_rule_id() -> None:
    assert interaction_message_claim_key(1, -100, 25, "rule-a") == (
        "account_bot:interaction_msg_claim:1:-100:25:rule-a"
    )
    assert interaction_message_claim_key(1, -100, 25, "rule-b") == (
        "account_bot:interaction_msg_claim:1:-100:25:rule-b"
    )
    assert interaction_message_claim_key(1, -100, None, "rule-a") is None


def test_callback_claim_key_includes_callback_identity() -> None:
    first = interaction_message_claim_key(1, -100, 25, "rule-a", "callback:one")
    second = interaction_message_claim_key(1, -100, 25, "rule-a", "callback:two")

    assert first != second
    assert first == "account_bot:interaction_msg_claim:1:-100:25:rule-a:callback:one"


@pytest.mark.asyncio
async def test_claim_interaction_message_uses_nx_and_clamped_ttl() -> None:
    redis = _Redis()

    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=redis,
        ttl_seconds=999,
    ) is True
    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=redis,
    ) is False

    assert redis.calls[0][2] == {"ex": 120, "nx": True}
    assert redis.calls[1][2] == {"ex": INTERACTION_MESSAGE_CLAIM_TTL_SECONDS, "nx": True}


@pytest.mark.asyncio
async def test_claim_interaction_message_allows_missing_message_id_and_fail_open() -> None:
    redis = _Redis()
    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=None,
        rule_id="rule-a",
        redis=redis,
    ) is True
    assert redis.calls == []

    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=_BrokenRedis(),
    ) is True
    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=26,
        rule_id="rule-a",
        redis=_BrokenRedis(),
        fail_open=False,
    ) is False


@pytest.mark.asyncio
async def test_release_interaction_message_allows_same_key_to_be_claimed_again() -> None:
    redis = _Redis()

    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=redis,
    ) is True
    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=redis,
    ) is False

    await release_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=redis,
    )

    assert await claim_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=redis,
    ) is True


@pytest.mark.asyncio
async def test_release_interaction_message_ignores_missing_message_id_and_redis_errors() -> None:
    redis = _Redis()
    await release_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=None,
        rule_id="rule-a",
        redis=redis,
    )
    assert redis.calls == []

    await release_interaction_message(
        account_id=1,
        chat_id=-100,
        message_id=25,
        rule_id="rule-a",
        redis=_BrokenRedis(),
    )
