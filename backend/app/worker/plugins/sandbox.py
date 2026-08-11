"""插件运行时沙箱（阶段 C，阶段 E 安全加固）。

目标：限制安装型插件（含远程、本地和仓库插件）能调用的 Telethon API 范围；
核心 builtin 兼容代码直接拿到原 ``TelegramClient``，不走沙箱。

安全设计（阶段 E 修复）：
- 移除 ``_ALWAYS_ALLOWED`` 中的 ``session``，防止第三方插件访问真实 session
- 禁止通过 ``__class__`` 反射获取真实对象
- 禁止通过 ``__getattr__`` 访问私有属性（以 _ 开头）
- 禁止通过 ``__dict__`` 绕过权限检查
- 禁止通过 ``__globals__`` / ``__code__`` 等获取运行时信息

设计：
- ``ALLOWED_API`` 把 manifest 中声明的"能力名" (e.g. ``send_message``) 映射到一组
  允许调用的 ``TelegramClient`` 方法名。
- ``SandboxClient`` 是一个动态代理：``__getattr__`` 时检查目标属性是否在允许集中，
  否则抛 ``PermissionError``。
- ``_log_call``：每次调用都会写一条 debug 日志（非 await，避免污染主流程）。

权限名清单（一期）：
- ``send_message``    : ``send_message`` / ``respond`` / ``reply``
- ``edit_message``    : ``edit`` / ``edit_message``
- ``read_chat``       : ``get_messages`` / ``get_chat`` / ``iter_messages``
- ``resolve_entity``  : ``get_entity``
- ``send_file``       : ``send_file``
- ``forward_message`` : ``forward_messages``
- ``join_chat``       : ``join_chat``
- ``delete_message``  : ``delete_messages`` / ``unpin_message``
- ``moderate_chat``   : ``ban_user`` / ``kick_user`` / ``mute_user`` / ``unban_user``

约束：
- 仅拦截顶层 ``getattr``；插件取到方法后多次调用都不再过 check（性能权衡）
- 私有属性（`_` 前缀）默认拒绝，避免拿到真实 client 内部对象绕过白名单
- 调用方 (loader) 在 ``installed`` 源 plugin 启动时把 ``ctx.client`` 替成
  ``SandboxClient(real, perms)``；核心 ``builtin`` 兼容代码不变
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ..command import acquire_userbot_action_rate_limit

log = logging.getLogger(__name__)


# 能力名 → 允许的 TelegramClient 方法集
ALLOWED_API: dict[str, frozenset[str]] = {
    "send_message": frozenset({"send_message", "respond", "reply"}),
    "edit_message": frozenset({"edit", "edit_message"}),
    "read_chat": frozenset({"get_messages", "get_chat", "iter_messages"}),
    "resolve_entity": frozenset({"get_entity"}),
    "send_file": frozenset({"send_file"}),
    "forward_message": frozenset({"forward_messages"}),
    "join_chat": frozenset({"join_chat"}),
    "delete_message": frozenset({"delete_messages", "unpin_message"}),
    "moderate_chat": frozenset({"ban_user", "kick_user", "mute_user", "unban_user"}),
}

# 非 TelegramClient 能力由其它 facade 处理，不应让 SandboxClient 误报未知权限。
_NON_CLIENT_PERMISSIONS: frozenset[str] = frozenset(
    {
        "external_http",
        "external_http_bypass_proxy",
        "ai_text",
        "ai_agent",
        "ai_vision",
        "ai_image",
        "ai_stt",
        "payout",
        "click_bot_button",
    }
)


# 默认放行集合：连接 / 关闭 / 自身查询等，不属于业务 API，避免插件起步崩
# 注意：阶段 E 移除了 "session"，防止第三方插件访问真实 session 对象
_ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {
        "connect",
        "disconnect",
        "is_connected",
        "is_user_authorized",
        "loop",
        "get_me",
        # Telethon Helper 上下文管理器
        "__aenter__",
        "__aexit__",
    }
)

# 危险属性黑名单：这些属性绝对禁止第三方插件访问
_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        # 敏感对象
        "session",
        "session_name",
        # 反射相关
        "__class__",
        "__dict__",
        "__getattribute__",
        "__getattr__",  # 已覆盖但显式列禁止
        "__setattr__",
        "__globals__",
        "__code__",
        "__closure__",
        "__func__",
        "__module__",
        "__builtins__",
        "__subclasshook__",
        "__mro__",
        # 私有属性变体（插件可能尝试 _xxx 或 __xxx）
        "_client",
        "_api",
        "_sender",
        "_state",
        "_connection",
        "_dcs",
        "api",
        "sender",
        "state",
    }
)

_EVENT_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        "__class__",
        "__dict__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__globals__",
        "__code__",
        "__closure__",
        "__func__",
        "__module__",
        "__builtins__",
        "__subclasshook__",
        "__mro__",
        "_client",
        "_entities",
        "_event",
        "_sender",
        "_chat",
        "original_update",
    }
)

_EVENT_METHOD_TO_CLIENT_METHOD: dict[str, str] = {
    "respond": "send_message",
    "reply": "send_message",
    "edit": "edit_message",
    "delete": "delete_messages",
    "get_reply_message": "get_messages",
    "get_chat": "get_chat",
    "get_sender": "get_chat",
}
_EVENT_READ_HELPERS: frozenset[str] = frozenset({"get_reply_message", "get_chat", "get_sender"})


def resolve_permissions(perms: list[str] | None) -> frozenset[str]:
    """把权限名列表展开成允许的方法名集合（去重）。

    未识别的权限名只写 warn 日志，不抛异常——插件 manifest 写错时业务可降级。
    """
    out: set[str] = set()
    for p in perms or []:
        methods = ALLOWED_API.get(p)
        if methods is None:
            if p in _NON_CLIENT_PERMISSIONS:
                continue
            log.warning("manifest 引用未知权限名 %r", p)
            continue
        out |= methods
    return frozenset(out)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _duration_to_until(duration_seconds: int | float | None) -> timedelta | None:
    if duration_seconds is None:
        return None
    try:
        seconds = int(duration_seconds)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def _require_allowed_method(client: SandboxClient, method_name: str) -> Any:
    allowed = object.__getattribute__(client, "_allowed")
    if method_name not in allowed:
        plugin_key = object.__getattribute__(client, "_plugin_key")
        perms = object.__getattribute__(client, "_perms")
        raise PermissionError(
            f"插件 {plugin_key!r} 缺少权限调用 client.{method_name}; "
            f"请在 manifest.permissions 中声明对应能力（持有: {perms}）"
        )
    return object.__getattribute__(client, "_real")


def _require_event_method(client: SandboxClient, plugin_key: str, event_method: str) -> None:
    required = _EVENT_METHOD_TO_CLIENT_METHOD[event_method]
    allowed = object.__getattribute__(client, "_allowed")
    if required not in allowed:
        perms = object.__getattribute__(client, "_perms")
        raise PermissionError(
            f"插件 {plugin_key!r} 缺少权限调用 event.{event_method}; "
            f"请在 manifest.permissions 中声明对应能力（持有: {perms}）"
        )


def _event_chat_id(real: Any) -> Any:
    message = getattr(real, "message", None)
    return (
        getattr(real, "chat_id", None)
        or getattr(message, "chat_id", None)
        or getattr(real, "peer_id", None)
        or getattr(message, "peer_id", None)
    )


def _event_message_id(real: Any) -> Any:
    message = getattr(real, "message", None)
    return (
        getattr(real, "id", None)
        or getattr(real, "message_id", None)
        or getattr(message, "id", None)
        or getattr(message, "message_id", None)
    )


def _event_text_arg(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, tuple[Any, ...]]:
    if args:
        return args[0], args[1:]
    if "message" in kwargs:
        return kwargs.pop("message"), ()
    if "text" in kwargs:
        return kwargs.pop("text"), ()
    return None, ()


class SandboxClient:
    """``TelegramClient`` 的最小化代理：只放行 manifest 声明的方法。

    **安全设计**：
    - 禁止访问 ``session`` 等敏感属性
    - 禁止通过 ``__class__`` / ``__dict__`` 等反射获取真实对象
    - 禁止访问私有属性（以 _ 开头）
    - 每次 ``__getattr__`` 调用都会经过权限检查
    """

    is_sandbox_client = True
    _is_sandboxed = True
    __slots__ = ("_real", "_allowed", "_plugin_key", "_perms", "_account_id", "_last_rate_limit_detail")

    def __init__(
        self,
        real: Any,
        perms: list[str] | None,
        *,
        plugin_key: str = "?",
        account_id: int | None = None,
    ) -> None:
        self._real = real
        # frozenset 避免被插件 mutate
        self._allowed = resolve_permissions(perms)
        self._plugin_key = plugin_key
        self._perms = list(perms or [])
        self._account_id = account_id
        self._last_rate_limit_detail: dict[str, Any] | None = None

    @property
    def __class__(self):  # type: ignore[override]
        """阻断通过 __class__ 反射获取真实对象类型。"""
        raise PermissionError(
            f"插件 {self._plugin_key!r} 禁止访问 client.__class__"
        )

    @property
    def __dict__(self) -> dict:  # type: ignore[override]
        """阻断通过 __dict__ 反射获取真实对象属性。"""
        raise PermissionError(
            f"插件 {self._plugin_key!r} 禁止访问 client.__dict__"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """阻断 raw MTProto 路径：client(functions.xxx(...))."""
        plugin_key = object.__getattribute__(self, "_plugin_key")
        raise PermissionError(
            f"插件 {plugin_key!r} 禁止调用 client.__call__ (raw MTProto)"
        )

    def __getattribute__(self, name: str) -> Any:
        """元属性访问（__slots__ 字段走此路径）。"""
        if name == "_is_sandboxed":
            return type(self)._is_sandboxed
        # 危险属性直接拒绝
        if name in _BLOCKED_ATTRS or name.startswith("_"):
            plugin_key = object.__getattribute__(self, "_plugin_key")
            raise PermissionError(
                f"插件 {plugin_key!r} 禁止访问 client.{name}"
            )
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        """主拦截点：每次插件取属性都会过检查。

        允许逻辑：
        1. 黑名单属性 → 拒绝
        2. _ 开头私有属性 → 拒绝
        3. _ALWAYS_ALLOWED 基础方法 → 放行
        4. manifest 声明的权限方法 → 放行
        5. 其它 → 拒绝
        """
        # 黑名单二次检查（即使上面 __getattribute__ 已经处理，这里作为纵深防御）
        plugin_key = object.__getattribute__(self, "_plugin_key")
        if name in _BLOCKED_ATTRS:
            raise PermissionError(
                f"插件 {plugin_key!r} 禁止访问 client.{name}"
            )
        # 私有属性
        if name.startswith("_"):
            raise PermissionError(
                f"插件 {plugin_key!r} 禁止访问私有属性 client.{name}"
            )
        allowed = object.__getattribute__(self, "_allowed")
        if name in _ALWAYS_ALLOWED or name in allowed:
            real = object.__getattribute__(self, "_real")
            return getattr(real, name)
        # 不在允许集内 → 抛 PermissionError
        perms = object.__getattribute__(self, "_perms")
        raise PermissionError(
            f"插件 {plugin_key!r} 缺少权限调用 client.{name}; "
            f"请在 manifest.permissions 中声明对应能力（持有: {perms}）"
        )

    async def send_message(self, chat_id: Any = None, message: Any = None, *args: Any, **kwargs: Any) -> Any:
        """受控发送消息：权限校验后接入 UserBot 动作限速。"""

        real = _require_allowed_method(self, "send_message")
        if chat_id is None and "entity" in kwargs:
            chat_id = kwargs.pop("entity")
        text = message if message is not None else kwargs.pop("message", None)
        object.__setattr__(self, "_last_rate_limit_detail", None)
        allowed, detail = await acquire_userbot_action_rate_limit(
            object.__getattribute__(self, "_account_id"),
            "send_message",
            chat_id,
        )
        if not allowed:
            object.__setattr__(self, "_last_rate_limit_detail", detail)
            return None
        return await _maybe_await(real.send_message(chat_id, text, *args, **kwargs))

    async def send_file(self, chat_id: Any = None, file: Any = None, *args: Any, **kwargs: Any) -> Any:
        """受控发送文件：权限校验后接入 UserBot 动作限速。"""

        real = _require_allowed_method(self, "send_file")
        if chat_id is None and "entity" in kwargs:
            chat_id = kwargs.pop("entity")
        object.__setattr__(self, "_last_rate_limit_detail", None)
        allowed, detail = await acquire_userbot_action_rate_limit(
            object.__getattribute__(self, "_account_id"),
            "send_file",
            chat_id,
        )
        if not allowed:
            object.__setattr__(self, "_last_rate_limit_detail", detail)
            return None
        return await _maybe_await(real.send_file(chat_id, file, *args, **kwargs))

    async def ban_user(
        self,
        entity: Any,
        user: Any,
        *,
        duration_seconds: int | float | None = None,
    ) -> Any:
        """封禁指定成员，仅在 manifest 声明 ``moderate_chat`` 后可用。"""
        real = _require_allowed_method(self, "ban_user")
        return await _maybe_await(
            real.edit_permissions(
                entity,
                user,
                until_date=_duration_to_until(duration_seconds),
                view_messages=False,
            )
        )

    async def kick_user(self, entity: Any, user: Any) -> Any:
        """踢出指定成员，仅在 manifest 声明 ``moderate_chat`` 后可用。"""
        real = _require_allowed_method(self, "kick_user")
        return await _maybe_await(real.kick_participant(entity, user))

    async def mute_user(
        self,
        entity: Any,
        user: Any,
        *,
        duration_seconds: int | float | None = None,
    ) -> Any:
        """禁言指定成员，仅在 manifest 声明 ``moderate_chat`` 后可用。"""
        real = _require_allowed_method(self, "mute_user")
        return await _maybe_await(
            real.edit_permissions(
                entity,
                user,
                until_date=_duration_to_until(duration_seconds),
                send_messages=False,
                send_media=False,
                send_stickers=False,
                send_gifs=False,
                send_games=False,
                send_inline=False,
                embed_link_previews=False,
                send_polls=False,
            )
        )

    async def unban_user(self, entity: Any, user: Any) -> Any:
        """解除指定成员限制，仅在 manifest 声明 ``moderate_chat`` 后可用。"""
        real = _require_allowed_method(self, "unban_user")
        return await _maybe_await(real.edit_permissions(entity, user))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        plugin_key = object.__getattribute__(self, "_plugin_key")
        perms = object.__getattribute__(self, "_perms")
        return f"<SandboxClient plugin={plugin_key} perms={perms}>"


class SandboxEvent:
    """Telethon event proxy that routes helper methods through manifest permissions."""

    __slots__ = ("_real", "_client", "_plugin_key")

    def __init__(self, real: Any, client: SandboxClient, *, plugin_key: str = "?") -> None:
        self._real = real
        self._client = client
        self._plugin_key = plugin_key

    @property
    def __class__(self):  # type: ignore[override]
        raise PermissionError(
            f"插件 {self._plugin_key!r} 禁止访问 event.__class__"
        )

    @property
    def __dict__(self) -> dict:  # type: ignore[override]
        raise PermissionError(
            f"插件 {self._plugin_key!r} 禁止访问 event.__dict__"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        plugin_key = object.__getattribute__(self, "_plugin_key")
        raise PermissionError(f"插件 {plugin_key!r} 禁止调用 event.__call__")

    def __getattribute__(self, name: str) -> Any:
        if name in _EVENT_BLOCKED_ATTRS or name.startswith("_"):
            plugin_key = object.__getattribute__(self, "_plugin_key")
            raise PermissionError(f"插件 {plugin_key!r} 禁止访问 event.{name}")
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        plugin_key = object.__getattribute__(self, "_plugin_key")
        if name in _EVENT_BLOCKED_ATTRS or name.startswith("_"):
            raise PermissionError(f"插件 {plugin_key!r} 禁止访问 event.{name}")

        client = object.__getattribute__(self, "_client")
        if name == "client":
            return client

        real = object.__getattribute__(self, "_real")
        if name == "message":
            return _wrap_event_child(getattr(real, "message", None), client, plugin_key)
        if name == "buttons":
            return _wrap_message_buttons(getattr(real, "buttons", None), plugin_key)
        if name == "reply_markup":
            return _wrap_reply_markup(getattr(real, "reply_markup", None), plugin_key)

        if name in _EVENT_METHOD_TO_CLIENT_METHOD:
            _require_event_method(client, plugin_key, name)
            def _call(*args: Any, **kwargs: Any) -> Any:
                if name == "respond":
                    call_kwargs = dict(kwargs)
                    chat_id = call_kwargs.pop("entity", None) or call_kwargs.pop("chat_id", None) or _event_chat_id(real)
                    text, rest = _event_text_arg(args, call_kwargs)
                    return client.send_message(chat_id, text, *rest, **call_kwargs)

                if name == "reply":
                    call_kwargs = dict(kwargs)
                    chat_id = call_kwargs.pop("entity", None) or call_kwargs.pop("chat_id", None) or _event_chat_id(real)
                    text, rest = _event_text_arg(args, call_kwargs)
                    call_kwargs.setdefault("reply_to", _event_message_id(real))
                    return client.send_message(chat_id, text, *rest, **call_kwargs)

                if name == "edit":
                    call_kwargs = dict(kwargs)
                    entity = call_kwargs.pop("entity", None) or call_kwargs.pop("chat_id", None) or _event_chat_id(real)
                    message_id = call_kwargs.pop("message_id", None) or call_kwargs.pop("message", None) or _event_message_id(real)
                    text, rest = _event_text_arg(args, call_kwargs)
                    return client.edit_message(entity, message_id, text, *rest, **call_kwargs)

                if name == "delete":
                    call_kwargs = dict(kwargs)
                    entity = call_kwargs.pop("entity", None) or call_kwargs.pop("chat_id", None) or _event_chat_id(real)
                    message_ids = call_kwargs.pop("message_ids", None) or call_kwargs.pop("message", None) or _event_message_id(real)
                    return client.delete_messages(entity, message_ids, *args, **call_kwargs)

                method = getattr(real, name)
                if not callable(method):
                    return method

                async def _await_and_wrap() -> Any:
                    result = method(*args, **kwargs)
                    value = await _maybe_await(result)
                    return _wrap_event_child(value, client, plugin_key)

                return _await_and_wrap()

            return _call

        value = getattr(real, name)
        if callable(value):
            raise PermissionError(
                f"插件 {plugin_key!r} 禁止直接调用 event.{name}; "
                "请改用 ctx.client 或声明后使用受控 event helper"
            )
        return value

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        plugin_key = object.__getattribute__(self, "_plugin_key")
        return f"<SandboxEvent plugin={plugin_key}>"


def _wrap_event_child(value: Any, client: SandboxClient, plugin_key: str) -> Any:
    if value is None:
        return None
    return SandboxEvent(value, client, plugin_key=plugin_key)


class SandboxButton:
    """只读按钮视图；不泄露 Telethon MessageButton、client 或 callback data。"""

    __slots__ = ("_kind", "_text")

    def __init__(self, button: Any, *, plugin_key: str) -> None:
        raw = getattr(button, "button", button)
        class_name = type(raw).__name__
        object.__setattr__(
            self,
            "_text",
            str(getattr(button, "text", None) or getattr(raw, "text", None) or ""),
        )
        object.__setattr__(
            self,
            "_kind",
            {
                "KeyboardButtonCallback": "callback",
                "KeyboardButtonUrl": "url",
                "KeyboardButtonGame": "game",
                "KeyboardButtonSwitchInline": "switch_inline",
                "KeyboardButtonRequestPhone": "request_phone",
                "KeyboardButtonRequestGeoLocation": "request_location",
            }.get(class_name, "reply"),
        )

    @property
    def text(self) -> str:
        return object.__getattribute__(self, "_text")

    @property
    def kind(self) -> str:
        return object.__getattribute__(self, "_kind")

    def __setattr__(self, name: str, _value: Any) -> None:
        raise PermissionError(f"插件按钮视图为只读，禁止修改 {name}")

    async def click(self, *_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(
            "event.message.buttons[…][…].click() 已被安全阻断；"
            "请声明 click_bot_button 权限并使用 "
            "ctx.messages.click_callback_button(...)"
        )


class SandboxButtonRow:
    """只读按钮行；只保留已净化的按钮视图。"""

    __slots__ = ("_buttons",)

    def __init__(self, row: Any, *, plugin_key: str) -> None:
        object.__setattr__(
            self,
            "_buttons",
            tuple(
                SandboxButton(button, plugin_key=plugin_key)
                for button in (getattr(row, "buttons", None) or ())
            ),
        )

    @property
    def buttons(self) -> tuple[SandboxButton, ...]:
        return object.__getattribute__(self, "_buttons")

    def __setattr__(self, name: str, _value: Any) -> None:
        raise PermissionError(f"插件按钮行视图为只读，禁止修改 {name}")


class SandboxReplyMarkup:
    """只读 Inline/Reply keyboard 视图；不保留原始 reply_markup 对象。"""

    __slots__ = ("_rows",)

    def __init__(self, markup: Any, *, plugin_key: str) -> None:
        object.__setattr__(
            self,
            "_rows",
            tuple(
                SandboxButtonRow(row, plugin_key=plugin_key)
                for row in (getattr(markup, "rows", None) or ())
            ),
        )

    @property
    def rows(self) -> tuple[SandboxButtonRow, ...]:
        return object.__getattribute__(self, "_rows")

    def __setattr__(self, name: str, _value: Any) -> None:
        raise PermissionError(f"插件按钮布局视图为只读，禁止修改 {name}")


def _wrap_message_buttons(value: Any, plugin_key: str) -> tuple[tuple[SandboxButton, ...], ...] | None:
    if value is None:
        return None
    return tuple(
        tuple(SandboxButton(button, plugin_key=plugin_key) for button in (row or ()))
        for row in (value or ())
    )


def _wrap_reply_markup(value: Any, plugin_key: str) -> SandboxReplyMarkup | None:
    if value is None:
        return None
    return SandboxReplyMarkup(value, plugin_key=plugin_key)


__all__ = [
    "ALLOWED_API",
    "SandboxButton",
    "SandboxButtonRow",
    "SandboxClient",
    "SandboxEvent",
    "SandboxReplyMarkup",
    "resolve_permissions",
]
