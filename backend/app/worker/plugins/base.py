"""插件框架：``PluginContext`` + ``Plugin`` 基类 + 全局注册表。

设计要点：
- ``Plugin`` 是基类，所有内置 / 第三方插件继承它并通过 ``@register`` 注册到全局表。
- 注册表存放的是 **类对象**（不是实例），每账号在 loader 里各自实例化一次，避免共享状态。
- ``PluginContext`` 是给插件运行期使用的"上下文容器"：账号 id、配置、规则、Telethon
      client、风控引擎、redis、日志写入器、平台调度器、安全 HTTP facade；
      插件实现各 hook 时只需读它就够了。
- 严格遵循 ``CONTRACTS.md`` 的"插件 Hook"段；所有 hook 默认实现为 no-op，子类按需重写。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient, events


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
      - ``log``：写运行日志的协程；签名 ``async (level, message, **detail)``
      - ``scheduler``：平台调度器 facade，可在插件内注册 cron / interval / once 任务
      - ``http``：声明 ``external_http`` 和 ``allowed_hosts`` 后注入的安全 HTTP facade
      - ``ai``：声明 ``ai_text`` 后注入的安全文本 LLM facade
      - ``messages``：标准消息动作 facade；交互入口内为缓冲动作，后台任务/命令中为实时受控投递

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
    log: Callable[..., Awaitable[None]] | None = None
    scheduler: Any = None  # SchedulerFacade
    http: Any = None  # PluginHTTP
    ai: Any = None  # PluginAI
    messages: Any = None
    generation: int = 0
    account_proxy_url: str | None = None

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
        - ``send_message`` / ``send_photo`` / ``send_file``
        - 普通发送动作默认继承会话通道；``send_via`` / ``channel`` /
          ``channel_selector`` 只用于跨通道公告、管理提示和迁移兼容等高级覆盖
        - ``end_session`` / ``close_session`` / ``no_session`` / ``result``
        - 可选 ``settlement``，供平台记录和后续结算

        新插件可优先使用 ``ctx.messages`` 生成受控消息动作，例如
        ``await ctx.messages.send(chat_id=..., text="...")``。
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
    "get_plugin",
    "public_entity_display_name",
    "register",
]
