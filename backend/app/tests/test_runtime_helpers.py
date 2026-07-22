"""worker.runtime 私有 helper 的单元测试（不连真 DB）。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.worker import runtime
from app.worker.plugins.base import (
    PluginContext,
    PluginIdentityFacade,
    public_entity_display_name,
    resolve_public_sender_identities,
    resolve_public_sender_identity,
    sanitize_public_display_name,
)
from app.worker.plugins.sandbox import SandboxClient
from app.worker.runtime import _build_proxy_url


def test_public_entity_display_name_keeps_non_contact_public_name() -> None:
    entity = SimpleNamespace(id=42, first_name="公开名", last_name="尾名", username="public_user", contact=False)

    assert public_entity_display_name(entity) == "公开名尾名"


def test_sanitize_public_display_name_removes_unicode_invisible_characters() -> None:
    raw = "\u206a\u200c\u200f\u206a \u206a\u200c\u200f\u206a人"

    assert sanitize_public_display_name(raw) == "人"
    assert sanitize_public_display_name("\u2800\u3164\uffa0") == "匿名用户"


def test_sanitize_public_display_name_removes_whitespace_and_limits_to_ten_characters() -> None:
    assert sanitize_public_display_name(" 张\u00a0三\u3000\t长名字ABCDEFGHIJK") == "张三长名字ABCDE"


def test_public_entity_display_name_hides_contact_remark_name() -> None:
    entity = SimpleNamespace(id=42, first_name="我给他的备注", last_name="", username="public_user", contact=True)

    assert public_entity_display_name(entity) == "public_user"


def test_public_entity_display_name_contact_without_username_falls_back_to_id() -> None:
    entity = SimpleNamespace(id=42, first_name="我给他的备注", last_name="", username=None, contact=True)

    assert public_entity_display_name(entity) == "42"


async def test_identity_facade_bypasses_plugin_sandbox_without_exposing_raw_client() -> None:
    class RawClient:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            assert chat_id == -1001
            assert user_id == 44
            return SimpleNamespace(anonymous=False, participant=SimpleNamespace(rank="普通成员标签"))

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                yield SimpleNamespace(
                    id=42,
                    participant=SimpleNamespace(
                        rank="匿名标签",
                        admin_rights=SimpleNamespace(anonymous=True),
                    ),
                )
                yield SimpleNamespace(
                    id=43,
                    participant=SimpleNamespace(
                        rank="普通管理员标签",
                        admin_rights=SimpleNamespace(anonymous=False),
                    ),
                )

            return stream()

    raw_client = RawClient()
    facade = PluginIdentityFacade(raw_client)
    ctx = PluginContext(
        account_id=1,
        feature_key="ai_redpacket",
        client=SandboxClient(raw_client, ["read_chat"], plugin_key="ai_redpacket"),
        identities=facade,
    )

    anonymous = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=42,
        fallback_display_name="匿名管理员真实姓名",
    )
    visible = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=43,
        fallback_display_name="非匿名管理员姓名",
    )
    settlement_names = await resolve_public_sender_identities(
        ctx,
        chat_id=-1001,
        senders={
            42: "匿名管理员真实姓名",
            43: "非匿名管理员姓名",
            44: "普通成员姓名",
        },
    )

    assert anonymous.display_name == "匿名标签"
    assert anonymous.is_anonymous_admin is True
    assert visible.display_name == "非匿名管理员姓名"
    assert visible.is_anonymous_admin is False
    assert settlement_names[42].display_name == "匿名标签"
    assert settlement_names[43].display_name == "非匿名管理员姓名"
    assert settlement_names[44].display_name == "普通成员姓名"
    try:
        leaked_client = facade._client  # type: ignore[attr-defined]
    except PermissionError:
        pass
    else:
        raise AssertionError(f"identity facade exposed its raw client: {leaked_client!r}")


async def test_resolve_public_sender_identity_uses_tag_only_for_anonymous_admin() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            assert (chat_id, user_id) == (-1001, 42)
            return SimpleNamespace(
                anonymous=True,
                participant=SimpleNamespace(rank="值班管理员"),
            )

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="真实姓名",
    )

    assert identity.user_id == 42
    assert identity.display_name == "值班管理员"
    assert identity.is_anonymous_admin is True
    assert identity.is_admin is True
    assert identity.tag == "值班管理员"
    assert identity.resolved is True


async def test_resolve_public_sender_identity_ignores_regular_member_tag() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                anonymous=False,
                participant=SimpleNamespace(rank="普通成员标签"),
            )

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="普通成员姓名",
    )

    assert identity.display_name == "普通成员姓名"
    assert identity.is_anonymous_admin is False
    assert identity.is_admin is False
    assert identity.tag == "普通成员标签"
    assert identity.resolved is True


async def test_resolve_public_sender_identity_marks_visible_userbot_admin() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                anonymous=False,
                is_admin=True,
                is_creator=False,
                participant=SimpleNamespace(rank="公开管理员标签"),
            )

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="公开管理员姓名",
    )

    assert identity.display_name == "公开管理员姓名"
    assert identity.is_anonymous_admin is False
    assert identity.is_admin is True
    assert identity.tag == "公开管理员标签"
    assert identity.resolved is True


async def test_resolve_public_sender_identity_does_not_cache_member_status() -> None:
    calls = 0

    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                anonymous=False,
                is_admin=calls == 2,
                is_creator=False,
                participant=SimpleNamespace(rank="实时标签" if calls == 2 else ""),
            )

    ctx = SimpleNamespace(client=Client())
    first = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=42,
        fallback_display_name="实时姓名",
    )
    second = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=42,
        fallback_display_name="实时姓名",
    )

    assert calls == 2
    assert first.is_admin is False
    assert first.tag is None
    assert second.is_admin is True
    assert second.tag == "实时标签"


async def test_resolve_public_sender_identity_sanitizes_public_name_and_anonymous_tag() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                anonymous=user_id == 43,
                participant=SimpleNamespace(
                    rank="\u206a 匿名\u200c管理员标签ABCDEFGHIJK" if user_id == 43 else "普通成员标签"
                ),
            )

    ctx = SimpleNamespace(client=Client())
    visible = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=42,
        fallback_display_name="\u206a 张\u00a0三\u200c ",
    )
    anonymous = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=43,
        fallback_display_name="不应公开的真实姓名",
    )

    assert visible.display_name == "张三"
    assert visible.is_anonymous_admin is False
    assert anonymous.display_name == "匿名管理员标签ABC"
    assert anonymous.tag == "匿名管理员标签ABC"
    assert anonymous.is_anonymous_admin is True


async def test_resolve_public_sender_identity_retries_with_userbot_entity() -> None:
    calls: list[object] = []
    entity = SimpleNamespace(id=42, first_name="普通成员姓名", username="ordinary_user")

    class Client:
        async def get_permissions(self, chat_id: int, user: object) -> SimpleNamespace:
            calls.append(user)
            if isinstance(user, int):
                raise ValueError("input entity is not cached")
            assert user is entity
            return SimpleNamespace(
                anonymous=False,
                participant=SimpleNamespace(rank="普通成员标签"),
            )

    async def resolve_entity(chat_id: int, user_id: int) -> object:
        assert (chat_id, user_id) == (-1001, 42)
        return entity

    identity = await PluginIdentityFacade(
        Client(),
        user_entity_resolver=resolve_entity,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="匿名用户",
    )

    assert calls == [42, entity]
    assert identity.display_name == "普通成员姓名"
    assert identity.is_anonymous_admin is False
    assert identity.resolved is True


async def test_resolve_public_sender_identity_entity_retry_keeps_anonymous_admin_safe() -> None:
    entity = SimpleNamespace(id=42, first_name="匿名管理员真实姓名")

    class Client:
        async def get_permissions(self, chat_id: int, user: object) -> SimpleNamespace:
            if isinstance(user, int):
                raise ValueError("input entity is not cached")
            return SimpleNamespace(
                anonymous=True,
                participant=SimpleNamespace(rank="匿名管理员标签"),
            )

    async def resolve_entity(chat_id: int, user_id: int) -> object:
        assert (chat_id, user_id) == (-1001, 42)
        return entity

    identity = await PluginIdentityFacade(
        Client(),
        user_entity_resolver=resolve_entity,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="匿名管理员真实姓名",
    )

    assert identity.display_name == "匿名管理员标签"
    assert identity.is_anonymous_admin is True
    assert identity.tag == "匿名管理员标签"


async def test_resolve_public_sender_identity_refreshes_saved_anonymous_user_name() -> None:
    entity = SimpleNamespace(
        id=42,
        first_name="恢复后的",
        last_name="公开姓名",
        username="public_user",
        contact=False,
    )

    class Client:
        async def get_permissions(self, chat_id: int, user: object) -> SimpleNamespace:
            return SimpleNamespace(anonymous=False, participant=SimpleNamespace(rank=""))

    async def resolve_entity(chat_id: int, user_id: int) -> object:
        assert (chat_id, user_id) == (-1001, 42)
        return entity

    identity = await PluginIdentityFacade(
        Client(),
        user_entity_resolver=resolve_entity,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="匿名用户",
    )

    assert identity.display_name == "恢复后的公开姓名"
    assert identity.is_anonymous_admin is False
    assert identity.resolved is True


async def test_resolve_public_sender_identity_fails_closed() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            raise RuntimeError("telegram unavailable")

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的姓名",
    )

    assert identity.display_name == "匿名用户"
    assert identity.is_anonymous_admin is False
    assert identity.tag is None
    assert identity.resolved is False


async def test_resolve_public_sender_identity_uses_admin_interaction_bot_for_hidden_admin() -> None:
    class UserNotParticipantError(Exception):
        pass

    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            raise UserNotParticipantError("anonymous administrator is hidden")

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        assert (chat_id, user_id) == (-1001, 42)
        return {
            "status": "creator",
            "is_anonymous": True,
            "custom_title": "匿名小尾巴测试",
        }

    ctx = SimpleNamespace(
        identities=PluginIdentityFacade(
            Client(),
            bot_member_resolver=resolve_bot_member,
        )
    )
    identity = await resolve_public_sender_identity(
        ctx,
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的真实姓名",
    )

    assert identity.display_name == "匿名小尾巴测试"
    assert identity.is_anonymous_admin is True
    assert identity.tag == "匿名小尾巴测试"
    assert identity.resolved is True


async def test_interaction_bot_member_lookup_does_not_replace_visible_admin_name_with_title() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            raise LookupError("userbot lookup unavailable")

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        return {
            "status": "administrator",
            "is_anonymous": False,
            "custom_title": "普通管理员标签",
            "user": {"id": 42, "first_name": "当前", "last_name": "公开姓名"},
        }

    identity = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=resolve_bot_member,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="公开姓名",
    )

    assert identity.display_name == "当前公开姓名"
    assert identity.is_anonymous_admin is False
    assert identity.is_admin is True
    assert identity.tag == "普通管理员标签"
    assert identity.resolved is True


async def test_interaction_bot_public_name_replaces_userbot_contact_remark() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(anonymous=False, participant=SimpleNamespace(rank=""))

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        assert (chat_id, user_id) == (-1001, 42)
        return {
            "status": "member",
            "user": {
                "id": 42,
                "first_name": "用户当前",
                "last_name": "真实姓名",
                "username": "public_user",
            },
        }

    identity = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=resolve_bot_member,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="UserBot联系人备注",
    )

    assert identity.display_name == "用户当前真实姓名"
    assert identity.is_anonymous_admin is False
    assert identity.resolved is True


async def test_userbot_only_identity_keeps_contact_name_without_calling_bot() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(anonymous=False, participant=SimpleNamespace(rank=""))

    async def resolve_entity(chat_id: int, user_id: int) -> object:
        return SimpleNamespace(
            id=user_id,
            first_name="联系人原始姓名",
            last_name="",
            username=None,
            contact=True,
        )

    bot_lookup = AsyncMock(side_effect=AssertionError("UserBot-only lookup must not call Bot API"))
    identity = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=bot_lookup,
        user_entity_resolver=resolve_entity,
    ).resolve_userbot(
        chat_id=-1001,
        user_id=42,
    )

    assert identity.display_name == "联系人原始姓名"
    assert identity.is_anonymous_admin is False
    assert identity.resolved is True
    bot_lookup.assert_not_awaited()


async def test_userbot_only_identity_still_hides_anonymous_admin_name() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                anonymous=True,
                participant=SimpleNamespace(rank="匿名值班"),
            )

    identity = await PluginIdentityFacade(Client()).resolve_userbot(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的 UserBot 姓名",
    )

    assert identity.display_name == "匿名值班"
    assert identity.is_anonymous_admin is True


async def test_interaction_bot_anonymous_state_hides_userbot_visible_identity() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(anonymous=False, participant=SimpleNamespace(rank=""))

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        return {
            "status": "administrator",
            "is_anonymous": True,
            "custom_title": "匿名值班",
            "user": {"id": 42, "first_name": "不应公开"},
        }

    identity = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=resolve_bot_member,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的联系人备注",
    )

    assert identity.display_name == "匿名值班"
    assert identity.is_anonymous_admin is True


async def test_batch_identity_lookup_uses_interaction_bot_for_hidden_admin() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            raise LookupError("anonymous administrator is hidden")

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                yield SimpleNamespace(
                    id=99,
                    participant=SimpleNamespace(
                        rank="其他管理员",
                        admin_rights=SimpleNamespace(anonymous=False),
                    ),
                )

            return stream()

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        return {
            "status": "administrator",
            "is_anonymous": True,
            "custom_title": "结算匿名标签",
        }

    identities = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=resolve_bot_member,
    ).resolve_many(
        chat_id=-1001,
        senders={42: "不应进入结算的真实姓名"},
    )

    assert identities[42].display_name == "结算匿名标签"
    assert identities[42].is_anonymous_admin is True


async def test_resolve_public_sender_identity_without_anonymous_field_fails_closed() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(participant=SimpleNamespace(rank="可能暴露身份的标签"))

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的姓名",
    )

    assert identity.display_name == "匿名用户"
    assert identity.is_admin is False
    assert identity.resolved is False


async def test_resolve_public_sender_identity_anonymous_admin_without_tag_uses_generic_label() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(anonymous=True, participant=SimpleNamespace(rank=""))

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="真实姓名",
    )

    assert identity.display_name == "匿名管理员"
    assert identity.is_anonymous_admin is True
    assert identity.resolved is True


async def test_anonymous_admin_without_userbot_tag_is_enriched_by_interaction_bot() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(anonymous=True, participant=SimpleNamespace(rank=""))

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        return {
            "status": "administrator",
            "is_anonymous": True,
            "custom_title": "交互 Bot 标签",
        }

    identity = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=resolve_bot_member,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的真实姓名",
    )

    assert identity.display_name == "交互Bot标签"
    assert identity.tag == "交互Bot标签"
    assert identity.is_anonymous_admin is True


async def test_anonymous_admin_is_not_declassified_by_stale_interaction_bot_state() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                anonymous=True,
                participant=SimpleNamespace(rank="UserBot 匿名标签"),
            )

    async def resolve_bot_member(chat_id: int, user_id: int) -> dict[str, object]:
        return {
            "status": "administrator",
            "is_anonymous": False,
            "custom_title": "普通管理员标签",
        }

    identity = await PluginIdentityFacade(
        Client(),
        bot_member_resolver=resolve_bot_member,
    ).resolve(
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的真实姓名",
    )

    assert identity.display_name == "UserBot匿名标"
    assert identity.tag == "UserBot匿名标"
    assert identity.is_anonymous_admin is True


async def test_resolve_public_sender_identity_falls_back_to_admin_listing() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            raise ValueError("input entity is not cached")

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                yield SimpleNamespace(
                    id=42,
                    participant=SimpleNamespace(
                        rank="匿名值班",
                        admin_rights=SimpleNamespace(anonymous=True),
                    ),
                )

            return stream()

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="真实姓名",
    )

    assert identity.display_name == "匿名值班"
    assert identity.is_anonymous_admin is True
    assert identity.tag == "匿名值班"
    assert identity.resolved is True


async def test_resolve_public_sender_identity_admin_listing_absence_fails_closed() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            raise ValueError("input entity is not cached")

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                yield SimpleNamespace(
                    id=99,
                    participant=SimpleNamespace(
                        rank="其他管理员",
                        admin_rights=SimpleNamespace(anonymous=True),
                    ),
                )

            return stream()

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="普通成员姓名",
    )

    assert identity.display_name == "匿名用户"
    assert identity.is_anonymous_admin is False
    assert identity.tag is None
    assert identity.resolved is False


async def test_resolve_public_sender_identity_admin_listing_absence_checks_permissions() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            assert (chat_id, user_id) == (-1001, 42)
            return SimpleNamespace(
                anonymous=False,
                participant=SimpleNamespace(rank="普通成员标签"),
            )

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                yield SimpleNamespace(
                    id=99,
                    participant=SimpleNamespace(
                        rank="其他管理员",
                        admin_rights=SimpleNamespace(anonymous=True),
                    ),
                )

            return stream()

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="普通成员姓名",
    )

    assert identity.display_name == "普通成员姓名"
    assert identity.is_anonymous_admin is False
    assert identity.tag == "普通成员标签"
    assert identity.resolved is True


async def test_resolve_public_sender_identity_admin_listing_omission_preserves_anonymous_tag() -> None:
    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            assert (chat_id, user_id) == (-1001, 42)
            return SimpleNamespace(
                anonymous=True,
                participant=SimpleNamespace(rank="值班标签"),
            )

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                yield SimpleNamespace(
                    id=99,
                    participant=SimpleNamespace(
                        rank="其他管理员",
                        admin_rights=SimpleNamespace(anonymous=False),
                    ),
                )

            return stream()

    identity = await resolve_public_sender_identity(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        user_id=42,
        fallback_display_name="不应公开的真实姓名",
    )

    assert identity.display_name == "值班标签"
    assert identity.is_anonymous_admin is True
    assert identity.tag == "值班标签"
    assert identity.resolved is True


async def test_resolve_public_sender_identities_reads_admin_listing_once() -> None:
    calls = 0

    class Client:
        async def get_permissions(self, chat_id: int, user_id: int) -> SimpleNamespace:
            assert (chat_id, user_id) == (-1001, 77)
            return SimpleNamespace(
                anonymous=False,
                participant=SimpleNamespace(rank="普通成员标签"),
            )

        def iter_participants(self, chat_id: int, *, filter: object):
            async def stream():
                nonlocal calls
                calls += 1
                yield SimpleNamespace(
                    id=42,
                    participant=SimpleNamespace(
                        rank="匿名管理员标签",
                        admin_rights=SimpleNamespace(anonymous=True),
                    ),
                )

            return stream()

    identities = await resolve_public_sender_identities(
        SimpleNamespace(client=Client()),
        chat_id=-1001,
        senders={42: "不应公开的姓名", 77: "普通成员姓名"},
    )

    assert calls == 1
    assert identities[42].display_name == "匿名管理员标签"
    assert identities[42].is_anonymous_admin is True
    assert identities[77].display_name == "普通成员姓名"
    assert identities[77].is_anonymous_admin is False


def test_build_proxy_url_socks5_with_auth() -> None:
    out = _build_proxy_url("socks5", "127.0.0.1", 1080, "alice", "p@ss")
    # urllib.parse.quote 会把 @ 转 %40
    assert out == "socks5://alice:p%40ss@127.0.0.1:1080"


def test_build_proxy_url_socks5_no_auth() -> None:
    assert _build_proxy_url("socks5", "10.0.0.1", 1080, None, "") == "socks5://10.0.0.1:1080"


def test_build_proxy_url_http_uppercase_type() -> None:
    """type 大小写应不敏感（前端可能传 SOCKS5）。"""
    assert _build_proxy_url("HTTP", "p.example.com", 8080, None, "") == "http://p.example.com:8080"


def test_build_proxy_url_https_falls_to_http_scheme() -> None:
    """https 类型也走 HTTP CONNECT 形式（httpx 用 http:// 前缀拨 CONNECT 隧道）。"""
    assert _build_proxy_url("https", "p.example.com", 443, None, "") == "http://p.example.com:443"


def test_build_proxy_url_username_only() -> None:
    """有 user 无 pass 时不能拼出 ``user:@host``——urllib 行为是 ``user@host``，httpx 接受。"""
    out = _build_proxy_url("socks5", "10.0.0.1", 1080, "alice", "")
    assert out == "socks5://alice@10.0.0.1:1080"


def test_build_proxy_url_mtproxy_not_supported() -> None:
    """mtproxy 类型 httpx 不支持 → 返 None。"""
    assert _build_proxy_url("mtproxy", "x", 443, None, "") is None


def test_build_proxy_url_unknown_type_returns_none() -> None:
    assert _build_proxy_url("ftp", "x", 21, None, "") is None


def test_worker_main_installs_sensitive_log_filter_after_logging_setup(monkeypatch) -> None:
    calls: list[object] = []
    worker_result = object()

    monkeypatch.setattr(runtime.logging, "basicConfig", lambda **kwargs: calls.append(("logging", kwargs)))
    monkeypatch.setattr(runtime, "install_sensitive_log_filter", lambda: calls.append(("redaction", None)))
    monkeypatch.setattr(runtime, "run_worker", lambda account_id: worker_result)
    monkeypatch.setattr(runtime.asyncio, "run", lambda value: calls.append(("run", value)))

    runtime.worker_main(7)

    assert [item[0] for item in calls] == ["logging", "redaction", "run"]
    assert calls[0][1]["format"] == "%(asctime)s [worker:7] %(levelname)s %(message)s"
    assert calls[2][1] is worker_result


@pytest.mark.asyncio
async def test_worker_capability_bootstrap_failure_is_fail_closed(monkeypatch) -> None:
    from app.services import platform_capabilities

    redis = object()
    log_call = AsyncMock()
    monkeypatch.setattr(runtime, "_log", log_call)
    monkeypatch.setattr(
        platform_capabilities,
        "bootstrap_from_db",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )

    assert await runtime._bootstrap_platform_capabilities(7, redis) is False
    log_call.assert_awaited_once()
    assert "fail-closed" in log_call.await_args.args[3]
