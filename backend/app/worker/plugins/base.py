"""插件框架：``PluginContext`` + ``Plugin`` 基类 + 全局注册表。

设计要点：
- ``Plugin`` 是基类，所有内置 / 第三方插件继承它并通过 ``@register`` 注册到全局表。
- 注册表存放的是 **类对象**（不是实例），每账号在 loader 里各自实例化一次，避免共享状态。
- ``PluginContext`` 是给插件运行期使用的"上下文容器"：账号 id、配置、规则、Telethon
      client、风控引擎、redis、持久化 storage、日志写入器、平台调度器、安全 HTTP facade；
      插件实现各 hook 时只需读它就够了。
- 严格遵循 ``CONTRACTS.md`` 的"插件 Hook"段；所有 hook 默认实现为 no-op，子类按需重写。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.types import ChannelParticipantsAdmins

from .manifest import Manifest


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def public_entity_display_name(
    entity: Any,
    *,
    fallback_id: int | str | None = None,
    default: str = "用户",
    include_at: bool = False,
) -> str:
    """Return a display label that avoids leaking local contact remarks.

    Telethon user entities can expose the account owner's saved contact name
    through first_name/last_name. For saved contacts, prefer public username or
    numeric id instead of rendering that local-only name.
    """

    if entity is not None:
        title = _clean_text(getattr(entity, "title", None))
        if title:
            return title

        username = _clean_text(getattr(entity, "username", None)).lstrip("@")
        if username:
            return f"@{username}" if include_at else username

        entity_id = getattr(entity, "id", None)
        is_contact = bool(getattr(entity, "contact", False))
        if not is_contact:
            name = " ".join(
                part
                for part in (
                    _clean_text(getattr(entity, "first_name", None)),
                    _clean_text(getattr(entity, "last_name", None)),
                )
                if part
            )
            if name:
                return name
        if entity_id not in (None, ""):
            return str(entity_id)

    if fallback_id not in (None, ""):
        return str(fallback_id)
    return default


@dataclass(frozen=True)
class PublicSenderIdentity:
    """Identity-safe group display fields; callers must still escape markup."""

    user_id: int
    display_name: str
    is_anonymous_admin: bool
    tag: str | None
    resolved: bool


class PluginIdentityFacade:
    """Resolve group-safe identities without exposing the raw Telegram client."""

    __slots__ = ("_bot_member_resolver", "_client")

    def __init__(
        self,
        client: Any,
        *,
        bot_member_resolver: Callable[[int, int], Awaitable[Mapping[str, Any] | None]] | None = None,
    ) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_bot_member_resolver", bot_member_resolver)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_"):
            raise PermissionError(f"禁止访问身份解析器私有属性 {name}")
        return object.__getattribute__(self, name)

    async def resolve(
        self,
        *,
        chat_id: int,
        user_id: int,
        fallback_display_name: str = "",
        unresolved_display_name: str = "匿名用户",
        anonymous_admin_display_name: str = "匿名管理员",
    ) -> PublicSenderIdentity:
        return await _resolve_public_sender_identity(
            object.__getattribute__(self, "_client"),
            bot_member_resolver=object.__getattribute__(self, "_bot_member_resolver"),
            chat_id=chat_id,
            user_id=user_id,
            fallback_display_name=fallback_display_name,
            unresolved_display_name=unresolved_display_name,
            anonymous_admin_display_name=anonymous_admin_display_name,
        )

    async def resolve_many(
        self,
        *,
        chat_id: int,
        senders: Mapping[int, str],
        unresolved_display_name: str = "匿名用户",
        anonymous_admin_display_name: str = "匿名管理员",
    ) -> dict[int, PublicSenderIdentity]:
        return await _resolve_public_sender_identities(
            object.__getattribute__(self, "_client"),
            bot_member_resolver=object.__getattribute__(self, "_bot_member_resolver"),
            chat_id=chat_id,
            senders=senders,
            unresolved_display_name=unresolved_display_name,
            anonymous_admin_display_name=anonymous_admin_display_name,
        )


async def resolve_public_sender_identity(
    ctx: PluginContext,
    *,
    chat_id: int,
    user_id: int,
    fallback_display_name: str = "",
    unresolved_display_name: str = "匿名用户",
    anonymous_admin_display_name: str = "匿名管理员",
) -> PublicSenderIdentity:
    """Resolve a callback actor without exposing anonymous administrators.

    Callback queries always carry the real clicking user. Plugins may use that
    id for authorization, idempotency and payout, but group-visible text should
    use this helper. Member tags never replace ordinary users' names; an admin
    tag replaces the name only while the member has anonymous mode enabled.
    Lookup failures fail closed so a transient Telegram error cannot disclose a
    protected identity.
    """

    identities = getattr(ctx, "identities", None)
    resolve = getattr(identities, "resolve", None) if identities is not None else None
    if callable(resolve):
        return await resolve(
            chat_id=chat_id,
            user_id=user_id,
            fallback_display_name=fallback_display_name,
            unresolved_display_name=unresolved_display_name,
            anonymous_admin_display_name=anonymous_admin_display_name,
        )
    return await _resolve_public_sender_identity(
        getattr(ctx, "client", None),
        chat_id=chat_id,
        user_id=user_id,
        fallback_display_name=fallback_display_name,
        unresolved_display_name=unresolved_display_name,
        anonymous_admin_display_name=anonymous_admin_display_name,
    )


async def _resolve_public_sender_identity(
    client: Any,
    *,
    bot_member_resolver: Callable[[int, int], Awaitable[Mapping[str, Any] | None]] | None = None,
    chat_id: int,
    user_id: int,
    fallback_display_name: str,
    unresolved_display_name: str,
    anonymous_admin_display_name: str,
) -> PublicSenderIdentity:
    if client is None:
        return _unresolved_public_sender_identity(user_id, unresolved_display_name)
    try:
        admins = await _anonymous_admin_participants(client, chat_id)
        identity = _public_sender_identity_from_admins(
            user_id,
            fallback_display_name,
            admins,
            unresolved_display_name=unresolved_display_name,
            anonymous_admin_display_name=anonymous_admin_display_name,
        )
        if identity is not None and identity.resolved:
            return identity
    except Exception:
        pass
    try:
        return await _public_sender_identity_from_permissions(
            client,
            chat_id,
            user_id,
            fallback_display_name,
            anonymous_admin_display_name=anonymous_admin_display_name,
        )
    except Exception:
        try:
            return await _public_sender_identity_from_bot_member(
                bot_member_resolver,
                chat_id,
                user_id,
                fallback_display_name,
                anonymous_admin_display_name=anonymous_admin_display_name,
            )
        except Exception:
            return _unresolved_public_sender_identity(user_id, unresolved_display_name)


async def resolve_public_sender_identities(
    ctx: PluginContext,
    *,
    chat_id: int,
    senders: Mapping[int, str],
    unresolved_display_name: str = "匿名用户",
    anonymous_admin_display_name: str = "匿名管理员",
) -> dict[int, PublicSenderIdentity]:
    """Batch variant that resolves one administrator directory per chat."""

    identities = getattr(ctx, "identities", None)
    resolve_many = getattr(identities, "resolve_many", None) if identities is not None else None
    if callable(resolve_many):
        return await resolve_many(
            chat_id=chat_id,
            senders=senders,
            unresolved_display_name=unresolved_display_name,
            anonymous_admin_display_name=anonymous_admin_display_name,
        )
    return await _resolve_public_sender_identities(
        getattr(ctx, "client", None),
        chat_id=chat_id,
        senders=senders,
        unresolved_display_name=unresolved_display_name,
        anonymous_admin_display_name=anonymous_admin_display_name,
    )


async def _resolve_public_sender_identities(
    client: Any,
    *,
    bot_member_resolver: Callable[[int, int], Awaitable[Mapping[str, Any] | None]] | None = None,
    chat_id: int,
    senders: Mapping[int, str],
    unresolved_display_name: str,
    anonymous_admin_display_name: str,
) -> dict[int, PublicSenderIdentity]:
    clean_senders = {int(user_id): str(display_name or "") for user_id, display_name in senders.items()}
    if not clean_senders:
        return {}
    if client is None:
        return {
            user_id: _unresolved_public_sender_identity(user_id, unresolved_display_name)
            for user_id in clean_senders
        }
    try:
        admins = await _anonymous_admin_participants(client, chat_id)
        resolved = {
            user_id: _public_sender_identity_from_admins(
                user_id,
                display_name,
                admins,
                unresolved_display_name=unresolved_display_name,
                anonymous_admin_display_name=anonymous_admin_display_name,
            )
            for user_id, display_name in clean_senders.items()
        }
        if all(identity is not None and identity.resolved for identity in resolved.values()):
            return {user_id: identity for user_id, identity in resolved.items() if identity is not None}
        clean_senders = {
            user_id: clean_senders[user_id]
            for user_id, identity in resolved.items()
            if identity is None or not identity.resolved
        }
        confirmed = {
            user_id: identity
            for user_id, identity in resolved.items()
            if identity is not None and identity.resolved
        }
    except Exception:
        confirmed = {}

    semaphore = asyncio.Semaphore(8)

    async def resolve(user_id: int, display_name: str) -> tuple[int, PublicSenderIdentity]:
        async with semaphore:
            try:
                identity = await _public_sender_identity_from_permissions(
                    client,
                    chat_id,
                    user_id,
                    display_name,
                    anonymous_admin_display_name=anonymous_admin_display_name,
                )
            except Exception:
                try:
                    identity = await _public_sender_identity_from_bot_member(
                        bot_member_resolver,
                        chat_id,
                        user_id,
                        display_name,
                        anonymous_admin_display_name=anonymous_admin_display_name,
                    )
                except Exception:
                    identity = _unresolved_public_sender_identity(user_id, unresolved_display_name)
            return user_id, identity

    confirmed.update(
        dict(
            await asyncio.gather(
                *(resolve(user_id, display_name) for user_id, display_name in clean_senders.items())
            )
        )
    )
    return confirmed


async def _anonymous_admin_participants(client: Any, chat_id: int) -> dict[int, Any]:
    iter_participants = getattr(client, "iter_participants", None)
    if not callable(iter_participants):
        raise LookupError("administrator listing unavailable")
    admins: dict[int, Any] = {}
    async for entity in iter_participants(chat_id, filter=ChannelParticipantsAdmins()):
        entity_id = int(getattr(entity, "id", 0) or 0)
        if entity_id:
            admins[entity_id] = getattr(entity, "participant", None)
    if not admins:
        raise LookupError("administrator listing is empty")
    return admins


def _public_sender_identity_from_admins(
    user_id: int,
    fallback_display_name: str,
    admins: Mapping[int, Any],
    *,
    unresolved_display_name: str,
    anonymous_admin_display_name: str,
) -> PublicSenderIdentity | None:
    if user_id not in admins:
        # Telegram may omit anonymous administrators from the directory. Absence
        # is not proof that exposing a callback actor's real name is safe.
        return None
    participant = admins[user_id]
    admin_rights = getattr(participant, "admin_rights", None)
    if admin_rights is None or not hasattr(admin_rights, "anonymous"):
        return _unresolved_public_sender_identity(user_id, unresolved_display_name)
    return _resolved_public_sender_identity(
        user_id,
        fallback_display_name,
        participant,
        bool(admin_rights.anonymous),
        anonymous_admin_display_name,
    )


async def _public_sender_identity_from_permissions(
    client: Any,
    chat_id: int,
    user_id: int,
    fallback_display_name: str,
    *,
    anonymous_admin_display_name: str,
) -> PublicSenderIdentity:
    get_permissions = getattr(client, "get_permissions", None)
    if not callable(get_permissions):
        raise LookupError("member permissions unavailable")
    permissions = await get_permissions(chat_id, user_id)
    if permissions is None or not hasattr(permissions, "anonymous"):
        raise LookupError("anonymous administrator state unavailable")
    return _resolved_public_sender_identity(
        user_id,
        fallback_display_name,
        getattr(permissions, "participant", None),
        bool(permissions.anonymous),
        anonymous_admin_display_name,
    )


async def _public_sender_identity_from_bot_member(
    resolver: Callable[[int, int], Awaitable[Mapping[str, Any] | None]] | None,
    chat_id: int,
    user_id: int,
    fallback_display_name: str,
    *,
    anonymous_admin_display_name: str,
) -> PublicSenderIdentity:
    if not callable(resolver):
        raise LookupError("interaction bot member lookup unavailable")
    member = await resolver(chat_id, user_id)
    if not isinstance(member, Mapping):
        raise LookupError("interaction bot member lookup failed")
    status = _clean_text(member.get("status"))
    active = status in {"creator", "administrator", "member"} or (
        status == "restricted" and bool(member.get("is_member"))
    )
    if not active:
        raise LookupError("interaction bot did not confirm an active member")
    if status in {"creator", "administrator"} and "is_anonymous" not in member:
        raise LookupError("interaction bot omitted anonymous administrator state")
    is_anonymous_admin = status in {"creator", "administrator"} and bool(member.get("is_anonymous"))
    tag = _clean_text(member.get("custom_title")) or None
    clean_fallback = _clean_text(fallback_display_name) or str(user_id)
    return PublicSenderIdentity(
        user_id=user_id,
        display_name=(tag or anonymous_admin_display_name) if is_anonymous_admin else clean_fallback,
        is_anonymous_admin=is_anonymous_admin,
        tag=tag,
        resolved=True,
    )


def _resolved_public_sender_identity(
    user_id: int,
    fallback_display_name: str,
    participant: Any,
    is_anonymous_admin: bool,
    anonymous_admin_display_name: str,
) -> PublicSenderIdentity:
    tag = _clean_text(getattr(participant, "rank", None)) or None
    clean_fallback = _clean_text(fallback_display_name) or str(user_id)
    return PublicSenderIdentity(
        user_id=user_id,
        display_name=(tag or anonymous_admin_display_name) if is_anonymous_admin else clean_fallback,
        is_anonymous_admin=is_anonymous_admin,
        tag=tag,
        resolved=True,
    )


def _unresolved_public_sender_identity(user_id: int, display_name: str) -> PublicSenderIdentity:
    return PublicSenderIdentity(
        user_id=user_id,
        display_name=display_name,
        is_anonymous_admin=False,
        tag=None,
        resolved=False,
    )


# ─────────────────────────────────────────────────────
# 运行时上下文（每个 [账号 × feature] 一份）
# ─────────────────────────────────────────────────────
@dataclass
class PluginContext:
    """插件运行上下文。

    字段：
      - ``account_id``：当前 worker 服务的账号 id
      - ``feature_key``：插件对应的 feature key（与 ``Plugin.key`` 一致）
      - ``config``：合并后的插件配置（schema defaults < global config < account config）
      - ``account_config``：``account_feature.config`` 原始账号级配置（不含 schema/global 合并值）
      - ``rules``：该 [账号 × feature] 下所有 ``enabled=True`` 的 ``Rule``，按 priority 倒序
      - ``client``：Telethon 客户端（loader 注入）
      - ``engine``：风控引擎（C Agent 提供，支持 ``acquire`` 与各 ``on_*`` 回调）
      - ``redis``：异步 Redis 客户端
      - ``storage``：按 [账号 × 插件] 隔离的持久化 key-value facade
      - ``data_dir``：插件独享、更新插件代码时不会被覆盖的持久化文件目录
      - ``log``：写运行日志的协程；签名 ``async (level, message, **detail)``
      - ``scheduler``：平台调度器 facade，可在插件内注册 cron / interval / once 任务
      - ``http``：声明 ``external_http`` 和 ``allowed_hosts`` 后注入的安全 HTTP facade
      - ``ai``：声明 ``ai_text`` 或独立 ``ai_agent`` 后注入的安全 LLM facade
      - ``messages``：标准消息动作 facade；交互入口内为缓冲动作，后台任务/命令中为实时受控投递
      - ``identities``：群内安全公开身份 facade；匿名管理员只返回标签，不暴露原始客户端

    为避免循环 import，``rules`` / ``engine`` / ``redis`` 都用 ``Any`` 标注。
    """

    account_id: int
    feature_key: str
    config: dict[str, Any] = field(default_factory=dict)
    account_config: dict[str, Any] = field(default_factory=dict)
    rules: list[Any] = field(default_factory=list)  # list[Rule] —— 这里用 Any 防循环引用
    client: TelegramClient | None = None
    engine: Any = None  # RateLimitEngine
    redis: Any = None  # redis.asyncio.Redis
    storage: Any = None  # PluginStorage
    data_dir: Path | None = None
    log: Callable[..., Awaitable[None]] | None = None
    scheduler: Any = None  # SchedulerFacade
    http: Any = None  # PluginHTTP
    ai: Any = None  # PluginAI
    messages: Any = None
    identities: Any = None  # PluginIdentityFacade
    generation: int = 0
    account_proxy_url: str | None = None
    event: Any | None = None
    args: list[str] = field(default_factory=list)
    command: str = ""

    @asynccontextmanager
    async def conversation(self, peer: Any, timeout: float = 30.0) -> AsyncIterator[Any]:
        """创建与 peer 的对话会话。

        用法::

            async with ctx.conversation("@BotFather") as conv:
                await conv.send("/newbot")
                resp = await conv.get_response()
        """
        from ..conversation import conversation as _conv

        if self.client is None:
            raise RuntimeError("PluginContext.client 未初始化")
        async with _conv(self.client, peer, timeout) as conv:
            yield conv

    async def reply(self, text: Any, *args: Any, **kwargs: Any) -> Any:
        """Reply to the current command event from simple-mode plugins."""

        if self.event is not None:
            reply = getattr(self.event, "reply", None)
            if callable(reply):
                return await reply(text, *args, **kwargs)
            respond = getattr(self.event, "respond", None)
            if callable(respond):
                return await respond(text, *args, **kwargs)
        if self.messages is not None:
            chat_id = getattr(self.event, "chat_id", None) if self.event is not None else None
            if chat_id is not None:
                return await self.messages.send(chat_id=chat_id, text=text, **kwargs)
        if self.client is not None and self.event is not None:
            chat_id = getattr(self.event, "chat_id", None)
            if chat_id is not None:
                return await self.client.send_message(chat_id, text, *args, **kwargs)
        raise RuntimeError("ctx.reply 需要命令事件或可发送消息的上下文")


# ─────────────────────────────────────────────────────
# 插件基类
# ─────────────────────────────────────────────────────
class Plugin:
    """插件基类。

    子类必须设置类属性 ``key`` / ``display_name``；可重写以下 hook：
      - ``on_startup``：[账号 × feature] 被激活时调用一次
      - ``on_shutdown``：禁用 / 卸载 / 热重载前调用一次
      - ``on_message``：消息派发回调，具体接收哪些方向由 ``message_channels`` 声明
      - ``on_message_edited``：可选的消息编辑事件回调；未重写时不会收到编辑消息
      - ``on_command``：插件可声明的"账号内命令"；返回 True 表示已处理

    ``message_channels`` 控制 loader 向该插件派发哪些方向的消息：
      - ``"incoming"``（默认）：群/私聊中别人发的消息
      - ``"outgoing"``：自己发送的消息
      插件可设 ``{"incoming", "outgoing"}`` 同时监听两个方向，
      在 ``on_message`` 内通过 ``event.outgoing`` 判断消息来源。

    插件如要追加 TG 内命令，可在类属性 ``commands`` 里登记
    （key 是命令名，value 是 ``async fn(client, event, args, account_id, ctx)``），
    loader 会在 ``on_startup`` 后通过 ``register_plugin_command`` 暴露给命令分发。
    """

    key: str = ""
    display_name: str = ""
    # 声明插件需要监听的消息方向；loader 据此决定是否向该插件派发对应事件
    message_channels: set[str] = {"incoming"}
    # 默认只允许账号本人/授权 sudo 触发 on_message；需要处理群内普通成员消息的插件应显式设为 False
    owner_only: bool = True
    # 插件想暴露的 TG 内命令：cmd_name -> async handler。
    # 可变命令必须在 __init__ 里赋值为实例属性，避免修改类属性污染其它账号实例。
    # handler 签名: (client, event, args, account_id, ctx) -> None
    commands: dict[str, Callable[..., Awaitable[None]]] = {}

    async def on_startup(self, ctx: PluginContext) -> None:
        """[账号 × feature] 激活时的钩子；默认 no-op。"""
        return None

    async def on_shutdown(self, ctx: PluginContext) -> None:
        """[账号 × feature] 关停时的钩子；默认 no-op。"""
        return None

    async def on_message(self, ctx: PluginContext, event: events.NewMessage.Event) -> None:
        """消息事件回调；默认 no-op。

        接收的方向由 ``message_channels`` 类属性控制，
        可通过 ``event.outgoing`` 区分消息来源。
        """

    async def on_message_edited(self, ctx: PluginContext, event: events.MessageEdited.Event) -> None:
        """消息编辑事件回调；默认 no-op。

        loader 只会把编辑消息派发给显式重写该方法的插件，避免改变既有
        ``on_message`` 语义。
        """

    async def on_direct_message(
        self,
        ctx: PluginContext,
        event: events.NewMessage.Event | events.MessageEdited.Event,
    ) -> None:
        """低延迟直通消息入口；默认 no-op。

        这是高风险高级入口，只会在插件 manifest 显式声明
        ``capabilities.telegram_direct_passthrough.enabled=true``，且账号级
        ``AccountFeature.config.direct_passthrough.enabled=true`` 时启用。运行时会在
        白名单/暂停检查后、Trace/Event Bus/legacy 包装前，把原始 Telethon event
        交给插件；一旦命中直通插件，本条消息不会继续进入普通消息链路。
        """

    async def on_command(
        self,
        ctx: PluginContext,
        cmd: str,
        args: list[str],
        event: events.NewMessage.Event,
    ) -> bool:
        """命令派发回调；返回 True 表示已处理，否则继续向后传。默认 no-op 返回 False。"""
        return False

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """交互 Bot 入口；返回平台标准动作列表，默认表示未实现。

        平台会提供标准事件信封：
        - ``source`` / ``message`` / ``chat`` / ``sender`` / ``actor`` /
          ``source_actor`` / ``player`` / ``payment`` / ``reply_to`` /
          ``trigger`` / ``session`` / ``raw`` 是新插件主路径
        - ``event`` 和 ``event_type`` / ``message_text`` / ``sender_name`` 等
          平铺字段只作为历史兼容来源

        标准动作约定：
        - ``send_message`` / ``send_rich_message`` / ``send_photo`` / ``send_file``
        - ``edit_message`` 编辑纯文本消息；``edit_caption`` 编辑图片/文件 caption
        - 普通发送动作默认继承会话通道；``send_via`` / ``channel`` /
          ``channel_selector`` 只用于跨通道公告、管理提示和迁移兼容等高级覆盖
        - ``end_session`` / ``close_session`` / ``no_session`` / ``result``
        - 可选 ``settlement``，供平台记录和后续结算

        新插件可优先使用 ``ctx.messages`` 生成受控消息动作，例如
        ``await ctx.messages.send(chat_id=..., text="...")``；需要标题、任务列表、
        折叠详情或表格时使用仅限 Interaction Bot 的
        ``await ctx.messages.send_rich(html="...")``。
        这些动作不会直接调用 Bot API 或 Telethon，而是随本 hook 的返回结果交给
        平台统一校验、限流、审计和发送。
        """
        return None

    async def on_event(
        self,
        ctx: PluginContext,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Event Bus 主入口；新插件优先实现这个 hook。

        ``payload`` 是 TelePilot 标准事件信封。插件可直接返回标准 action，
        或使用 ``ctx.messages`` 缓冲发送、编辑、删除、置顶、callback ACK、
        inline answer 等动作，由平台统一执行和记录 Trace。
        """
        return None

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """配置页动作入口；默认未实现。

        插件可在 manifest/config_schema 中声明配置动作，由 TelePilot 配置页渲染按钮。
        用户点击后平台会构造一个不带 Telegram client 的受控 ``PluginContext``，
        注入当前表单配置、``ctx.http`` 和 ``ctx.ai`` 等安全 facade，然后调用此 hook。

        返回值应是普通 dict，常用字段：
        - ``config_patch``：要合并回当前表单的字段值，例如 ``{"rules": [...]}``
        - ``message`` / ``toast``：给管理员展示的短反馈
        - ``result``：可选的结构化结果，供更高级前端组件消费
        """
        return None


# ─────────────────────────────────────────────────────
# 全局注册表
# ─────────────────────────────────────────────────────
# feature_key -> Plugin 子类（不是实例！每账号都要新实例）
_REGISTRY: dict[str, type[Plugin]] = {}


@dataclass(frozen=True)
class SimpleCommandSpec:
    """A command declared through the simple-mode SDK."""

    name: str
    handler: Callable[[PluginContext], Awaitable[Any]]
    module_name: str


_SIMPLE_COMMANDS: dict[str, list[SimpleCommandSpec]] = {}


def _normalize_command_name(name: str) -> str:
    command = str(name or "").strip().lstrip("/")
    if not command:
        raise ValueError("@plugin.command 需要非空命令名")
    return command


def register_simple_command(name: str, fn: Callable[[PluginContext], Awaitable[Any]]) -> Callable:
    """Register a simple-mode command function for loader-time manifest inference."""

    command = _normalize_command_name(name)
    module_name = str(getattr(fn, "__module__", "") or "").strip()
    if not module_name:
        raise ValueError("@plugin.command 只能用于可导入模块中的函数")
    specs = _SIMPLE_COMMANDS.setdefault(module_name, [])
    specs[:] = [item for item in specs if item.name != command]
    specs.append(SimpleCommandSpec(name=command, handler=fn, module_name=module_name))
    return fn


class PluginSDK:
    """Public decorator namespace exposed as ``from telepilot import plugin``."""

    def command(self, name: str) -> Callable[[Callable[[PluginContext], Awaitable[Any]]], Callable]:
        """Declare a single-function userbot command plugin."""

        def decorator(fn: Callable[[PluginContext], Awaitable[Any]]) -> Callable:
            return register_simple_command(name, fn)

        return decorator


plugin = PluginSDK()


def _simple_specs_for_module(module_name: str) -> list[SimpleCommandSpec]:
    prefix = f"{module_name}."
    specs: list[SimpleCommandSpec] = []
    for spec_module, module_specs in _SIMPLE_COMMANDS.items():
        if spec_module == module_name or spec_module.startswith(prefix):
            specs.extend(module_specs)
    return specs


def clear_simple_commands_for_module(module_name: str) -> None:
    """Drop simple-mode declarations owned by a plugin module before reload."""

    prefix = f"{module_name}."
    for spec_module in list(_SIMPLE_COMMANDS):
        if spec_module == module_name or spec_module.startswith(prefix):
            _SIMPLE_COMMANDS.pop(spec_module, None)


def build_implicit_plugin(
    *,
    module_name: str,
    plugin_key: str,
    display_name: str | None = None,
) -> tuple[type[Plugin], Manifest] | None:
    """Build ``PLUGIN_CLASS`` and ``MANIFEST`` from simple-mode decorators."""

    specs = _simple_specs_for_module(module_name)
    if not specs:
        return None
    commands: dict[str, Callable[..., Awaitable[None]]] = {}

    for spec in specs:
        async def _run(_client, event, args, _account_id, ctx, *, _spec=spec):  # noqa: ANN001
            call_ctx = replace(
                ctx,
                event=event,
                args=list(args or []),
                command=_spec.name,
            )
            await _spec.handler(call_ctx)

        commands[spec.name] = _run

    display = display_name or plugin_key.replace("_", " ").replace("-", " ").title()
    cls = type(
        f"{plugin_key.title().replace('_', '').replace('-', '')}SimplePlugin",
        (Plugin,),
        {
            "key": plugin_key,
            "display_name": display,
            "commands": commands,
            "owner_only": True,
        },
    )
    manifest = Manifest(
        key=plugin_key,
        display_name=display,
        description=f"{display} simple command plugin",
        category="utility",
        permissions=["read_event", "send_message"],
    )
    return cls, manifest


def register(plugin_cls: type[Plugin]) -> type[Plugin]:
    """装饰器：把一个 ``Plugin`` 子类注册到全局表。

    用法：
        @register
        class DemoPlugin(Plugin):
            key = "demo"
            ...
    """
    if not getattr(plugin_cls, "key", ""):
        raise ValueError("Plugin.key 必须先设置")
    _REGISTRY[plugin_cls.key] = plugin_cls
    return plugin_cls


def get_plugin(key: str) -> type[Plugin] | None:
    """按 feature key 查找已注册的插件类，不存在返回 None。"""
    return _REGISTRY.get(key)


def all_plugins() -> dict[str, type[Plugin]]:
    """返回当前已注册的全部插件（拷贝）。"""
    return dict(_REGISTRY)


__all__ = [
    "Plugin",
    "PluginContext",
    "all_plugins",
    "build_implicit_plugin",
    "clear_simple_commands_for_module",
    "get_plugin",
    "plugin",
    "public_entity_display_name",
    "register",
    "register_simple_command",
]
