#!/usr/bin/env python3
"""TelePilot 插件脚手架 CLI（tp_plugin）。

面向插件开发者的本地工具，三条子命令：

    tp_plugin new <name> --profile session_game|command|passthrough [--dir <父目录>]
    tp_plugin register <dir>
    tp_plugin check <dir>

- ``new``     生成一个可直接跑起来的插件骨架（plugin.json + manifest.py + plugin.py
              + __init__.py + config_schema 样板 + README + CHANGELOG + pytest 样板）。
- ``register`` 把本地目录登记进 ``installed_plugin`` 台账，解决"手拷目录被 loader 拒载"
              的开发痛点。内部复用 ``plugin_repo_service.install_local_plugin``，
              目录不在 ``plugins/local_imports`` 时先拷进去再登记。
- ``check``   本地 lint：校验四文件齐全、plugin.json 结构，并复用
              ``event_bus.normalize_event_subscription`` 对声明的事件做白名单校验，
              打印 unknown_events / unknown_filter_keys 警告。

设计成"纯函数 + 薄 CLI"：``scaffold_plugin`` / ``check_plugin`` /
``register_plugin`` 都可在测试里直接调用；``main`` 只负责 argparse 分发与
（register 时）打开真实 ``AsyncSessionLocal`` 会话。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import pprint
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ── 让脚本在 backend/scripts 下直接运行也能 import app.* ──
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

PROFILES = ("session_game", "command", "passthrough")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

# 插件包必备的运行期文件（与 remote_plugin_service._validate_runtime_plugin_shape 一致）。
_REQUIRED_FILES = ("plugin.json", "manifest.py", "plugin.py", "__init__.py")
_COPY_IGNORE_DIRS = {".git", ".gitignore", "__pycache__"}

_PERMISSION_RISK: dict[str, tuple[str, str]] = {
    "read_chat": ("低风险", "读取聊天上下文"),
    "send_message": ("低风险", "发送消息"),
    "external_http": ("中风险", "访问外部 HTTP"),
    "edit_message": ("中风险", "编辑消息"),
    "delete_message": ("中风险", "删除消息"),
    "payout": ("高风险", "发起派奖/付款，必须显式声明并强确认用途"),
    "modify_identity": ("高风险", "修改身份信息，必须显式声明并强确认用途"),
}
_CTX_MESSAGE_METHOD_PERMISSIONS = {
    "send": "send_message",
    "reply": "send_message",
    "read": "read_chat",
    "get": "read_chat",
    "history": "read_chat",
    "edit": "edit_message",
    "delete": "delete_message",
    "payout": "payout",
}
_ACTION_TYPE_PERMISSIONS = {
    "send_message": "send_message",
    "send_photo": "send_message",
    "send_file": "send_message",
    "edit_message": "edit_message",
    "edit_caption": "edit_message",
    "delete_message": "delete_message",
    "payout": "payout",
}


# ─────────────────────────────────────────────────────
# 通用小工具
# ─────────────────────────────────────────────────────
def _class_name(name: str) -> str:
    """``demo_game`` / ``demo-game`` → ``DemoGamePlugin``。"""
    parts = [p for p in re.split(r"[_-]+", name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) + "Plugin"


def _entry_key(name: str) -> str:
    return f"start_{name}"


def _py_literal(obj: Any) -> str:
    """把 Python 对象渲染成可嵌入 manifest.py 的源码字面量。

    用 pprint 而非 json.dumps：JSON 的 true/false/null 不是合法 Python，
    pprint 输出的 True/False/None 才能直接写进 .py。
    """
    return pprint.pformat(obj, indent=4, width=100, sort_dicts=False)


# ─────────────────────────────────────────────────────
# 模板：各 profile 的元数据结构（plugin.json 与 manifest.py 共用同一份对象）
# ─────────────────────────────────────────────────────
def _config_schema(name: str, profile: str) -> dict[str, Any]:
    if profile == "passthrough":
        return {
            "type": "object",
            "x-ui-mode": "schema",
            "additionalProperties": False,
            "properties": {
                "keyword": {
                    "type": "string",
                    "title": "直通触发词",
                    "default": "ping",
                    "minLength": 1,
                    "maxLength": 32,
                },
                "reply": {
                    "type": "string",
                    "title": "命中后的回复",
                    "default": "pong",
                    "minLength": 1,
                    "maxLength": 200,
                },
            },
            "required": ["keyword", "reply"],
        }
    return {
        "type": "object",
        "x-ui-mode": "schema",
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "string",
                "title": "触发指令名",
                "default": name,
                "minLength": 1,
                "maxLength": 32,
                "pattern": "^\\S+$",
            },
            "timeout": {
                "type": "integer",
                "title": "答题限时（秒）",
                "default": 300,
                "minimum": 10,
                "maximum": 86400,
            },
        },
        "required": ["command", "timeout"],
    }


def _interaction_entries(name: str, profile: str) -> list[dict[str, Any]]:
    if profile == "session_game":
        return [
            {
                "key": _entry_key(name),
                "title": f"开始 {name}",
                "description": "由交互 Bot 在群内开启一局游戏，随后由群消息继续答题。",
                "interaction_profile": "session_game",
                "launch_mode": "hybrid",
                "session_scope": "chat",
                "events": [
                    "command",
                    "keyword",
                    "payment_confirmed",
                    "message",
                    "session_expired",
                    "session_close",
                ],
                "triggers": {"command": name},
                "preserve_command_trigger": True,
                "command_fallback": {"enabled": True, "command": name, "mode": "hint_only"},
                "result_contract": {
                    "actions": [
                        "start_session",
                        "send_message",
                        "update_session",
                        "payout",
                        "end_session",
                        "result",
                        "settlement",
                    ],
                    "send_via": ["interaction_bot", "userbot_reply"],
                },
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "answer": {"type": "string", "title": "口令答案", "default": "888"},
                        "prize": {"type": "integer", "title": "奖金", "default": 100, "minimum": 0},
                        "valid_seconds": {
                            "type": "integer",
                            "title": "平台会话有效期（秒）",
                            "default": 300,
                            "minimum": 30,
                            "maximum": 86400,
                        },
                    },
                },
            }
        ]
    if profile == "command":
        return [
            {
                "key": _entry_key(name),
                "title": f"运行 {name}",
                "description": "由交互 Bot 命令或管理员命令触发的一次性动作，不建立会话。",
                "interaction_profile": "utility_trigger",
                "launch_mode": "hybrid",
                "session_scope": "none",
                "events": ["command"],
                "triggers": {"command": name},
                "preserve_command_trigger": True,
                "command_fallback": {"enabled": True, "command": name, "mode": "hint_only"},
                "result_contract": {
                    "actions": ["send_message", "result", "end_session"],
                    "send_via": ["interaction_bot", "userbot_reply"],
                },
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "note": {"type": "string", "title": "附带说明", "default": ""},
                    },
                },
            }
        ]
    # passthrough：不走交互入口，靠 capabilities 直通，无 interaction_entries
    return []


def _capabilities(profile: str) -> dict[str, Any]:
    if profile == "passthrough":
        return {
            "telegram_direct_passthrough": {
                "enabled": True,
                "reason": "抢红包/秒杀类需要跳过标准链路的低延时场景；账号侧仍需二次手动开启。",
                "sources": ["userbot"],
                "directions": ["incoming"],
                "include_edited": False,
            }
        }
    return {}


def _profile_meta(profile: str) -> dict[str, str]:
    if profile == "session_game":
        return {"category": "interactive", "interaction_profile": "session_game"}
    if profile == "command":
        return {"category": "utility", "interaction_profile": "utility_trigger"}
    return {"category": "utility", "interaction_profile": "utility_trigger"}


def _usage(name: str, profile: str) -> str:
    if profile == "session_game":
        return f"安装并在账号启用后，用 {{prefix}}{name} 或群内关键词开局；群友直接发口令抢答，答对派奖并结算。"
    if profile == "command":
        return f"安装并在账号启用后，用 {{prefix}}{name} 触发一次性动作，插件回复一条消息并结束。"
    return "高级直通示例：账号侧手动开启直通后，私聊/群里命中触发词即由 on_direct_message 低延时回复。"


def _permissions(profile: str) -> list[str]:
    if profile == "session_game":
        return ["send_message", "edit_message", "read_chat"]
    return ["send_message", "read_chat"]


def _plugin_json_dict(name: str, profile: str) -> dict[str, Any]:
    meta = _profile_meta(profile)
    data: dict[str, Any] = {
        "name": name,
        "display_name": name,
        "description": f"{profile} 脚手架示例插件。",
        "author": "you",
        "version": "0.1.0",
        "entry": "plugin.py",
        "min_telepilot_version": "0.41.0",
        "category": meta["category"],
        "interaction_profile": meta["interaction_profile"],
        "permissions": _permissions(profile),
        "usage": _usage(name, profile),
        "config_schema": _config_schema(name, profile),
        "capabilities": _capabilities(profile),
    }
    entries = _interaction_entries(name, profile)
    if entries:
        data["interaction_entries"] = entries
    return data


# ─────────────────────────────────────────────────────
# 模板：源码文件
# ─────────────────────────────────────────────────────
def _manifest_py(name: str, profile: str) -> str:
    meta = _profile_meta(profile)
    entries = _interaction_entries(name, profile)
    header = f'''"""{name} 插件 Manifest（运行期读取；安装期禁止执行）。

字段应与同目录 plugin.json 保持一致。
"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest


CONFIG_SCHEMA = {_py_literal(_config_schema(name, profile))}
'''
    entries_block = ""
    entries_kwarg = ""
    if entries:
        entries_block = f"\n\nINTERACTION_ENTRIES = {_py_literal(entries)}\n"
        entries_kwarg = "    interaction_entries=INTERACTION_ENTRIES,\n"

    capabilities = _capabilities(profile)
    cap_kwarg = f"    capabilities={_py_literal(capabilities)},\n" if capabilities else "    capabilities={},\n"

    manifest_block = f'''

MANIFEST = Manifest(
    key="{name}",
    display_name="{name}",
    version="0.1.0",
    min_telepilot_version="0.41.0",
    author="you",
    description="{profile} 脚手架示例插件。",
    usage={_usage(name, profile)!r},
    category="{meta['category']}",
    interaction_profile="{meta['interaction_profile']}",
    permissions={_py_literal(_permissions(profile))},
    config_schema=CONFIG_SCHEMA,
{entries_kwarg}{cap_kwarg})


__all__ = ["MANIFEST"]
'''
    return header + entries_block + manifest_block


def _init_py(name: str) -> str:
    cls = _class_name(name)
    return (
        "from .manifest import MANIFEST\n"
        f"from .plugin import {cls}\n\n"
        f"PLUGIN_CLASS = {cls}\n\n"
        '__all__ = ["MANIFEST", "PLUGIN_CLASS"]\n'
    )


def _plugin_py_session_game(name: str) -> str:
    cls = _class_name(name)
    entry = _entry_key(name)
    return f'''"""{name}：session_game 脚手架示例。

演示"平台会话游戏"的最小闭环：开局建会话 → 群消息抢答 → 命中派奖并结算。
所有对局状态只存平台会话 ``session.data``，插件不维护内存局。
"""

from __future__ import annotations

import time
from typing import Any

from app.worker.command import current_command_prefix
from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import TelePilotEvent, event_from_interaction_payload


@register
class {cls}(Plugin):
    key = "{name}"
    display_name = "{name}"
    message_channels = {{"incoming", "outgoing"}}
    owner_only = False

    def __init__(self) -> None:
        super().__init__()
        self._command = "{name}"
        self._timeout = 300

    async def on_startup(self, ctx: PluginContext) -> None:
        cfg = ctx.config or {{}}
        self._command = str(cfg.get("command") or "{name}").strip() or "{name}"
        self._timeout = self._int(cfg.get("timeout"), 300, minimum=10, maximum=86400)
        if ctx.log:
            await ctx.log("info", f"[{name}] 已启动，交互指令：{{current_command_prefix(fallback=',')}}{{self._command}}")

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if entry_key != "{entry}":
            return None
        event = event_from_interaction_payload(payload)
        if event.type in {{"command", "keyword", "payment_confirmed"}}:
            return self._start_actions(event, payload, entry_key)
        if event.type == "message":
            return self._answer_actions(event, payload)
        if event.type in {{"session_expired", "session_close"}}:
            return self._expire_actions(event, payload)
        return []

    def _start_actions(
        self,
        event: TelePilotEvent,
        payload: dict[str, Any],
        entry_key: str,
    ) -> list[dict[str, Any]]:
        chat_id = event.message.chat_id
        if chat_id is None:
            return []
        answer = str(payload.get("answer") or "888").strip() or "888"
        prize = self._prize(event, payload)
        timeout = self._timeout_seconds(payload)
        state = {{
            "active": True,
            "answer": answer,
            "prize": prize,
            "timeout": timeout,
            "started_at": time.time(),
        }}
        # ── 开局时序：start_session 必须排在 send_message / update_session 之前 ──
        # 交互 Bot 的关键词 / 付款 / 命令通道当前都是"动作先应用、后落会话"，若直接
        # 发 update_session，会因会话尚未建立而悬空失败。平台会在其它动作之前单独
        # 处理 start_session 动作（account_bot_runtime._apply_interaction_start_session_actions
        # 先于 _apply_interaction_actions；worker loader 同样支持），所以显式 start_session
        # 是当前所有通道都安全的开局写法。
        # 注：待 bug 轮统一各通道时序后，本 start_session 可简化省略。
        return [
            {{"type": "start_session", "chat_id": chat_id, "entry_key": entry_key, "ttl_seconds": timeout}},
            {{
                "type": "send_message",
                "chat_id": chat_id,
                "text": self._start_text(state),
                "parse_mode": "plain",
                "reply_to_message_id": event.message.message_id,
            }},
            {{"type": "update_session", "data": state}},
        ]

    def _answer_actions(self, event: TelePilotEvent, payload: dict[str, Any]) -> list[dict[str, Any]]:
        state = self._session_data(event, payload)
        chat_id = event.message.chat_id
        if chat_id is None or not state.get("active"):
            return []
        text = str(event.message.text or event.message.caption or "").strip()
        if not text:
            return []

        if text == str(state.get("answer") or ""):
            prize = int(state.get("prize") or 0)
            player = event.actor.display_name or event.sender.display_name or "玩家"
            player_id = event.actor.user_id or event.sender.user_id
            message_id = event.message.message_id
            actions: list[dict[str, Any]] = [
                {{
                    "type": "send_message",
                    "chat_id": chat_id,
                    "text": f"答对了：{{player}}\\n口令：{{text}}\\n奖金：{{prize}}",
                    "parse_mode": "plain",
                    "reply_to_message_id": message_id,
                }}
            ]
            if prize > 0:
                # payout：由 userbot 发 "+金额" 文本、第三方记账 Bot 入账。
                actions.append(
                    {{
                        "type": "payout",
                        "chat_id": chat_id,
                        "amount": prize,
                        "text": f"+{{prize}}",
                        "parse_mode": "plain",
                        "reply_to_message_id": message_id,
                    }}
                )
            actions.append(
                {{
                    "type": "result",
                    "success": True,
                    "result": {{
                        "status": "winner",
                        "winner_user_id": player_id,
                        "winner_name": player,
                        "winner_message_id": message_id,
                        "answer": text,
                        "prize": prize,
                    }},
                    "settlement": {{
                        "mode": "auto",
                        "amount": prize,
                        "winner_user_id": player_id,
                        "winner_name": player,
                        "status": "payout_requested",
                    }},
                }}
            )
            actions.append({{"type": "end_session"}})
            return actions

        # 未命中：给提示并续期会话（update_session 此时会话已存在，安全）。
        return [
            {{
                "type": "send_message",
                "chat_id": chat_id,
                "text": "口令不对，再试试。",
                "parse_mode": "plain",
                "reply_to_message_id": event.message.message_id,
            }},
            {{"type": "update_session", "data": state}},
        ]

    def _expire_actions(self, event: TelePilotEvent, payload: dict[str, Any]) -> list[dict[str, Any]]:
        state = self._session_data(event, payload)
        chat_id = event.message.chat_id
        if chat_id is None or not state.get("active"):
            return []
        return [
            {{
                "type": "send_message",
                "chat_id": chat_id,
                "text": f"游戏超时，正确口令是 {{state.get('answer')}}。",
                "parse_mode": "plain",
            }},
            {{"type": "end_session"}},
        ]

    # ── 小工具 ──
    def _session_data(self, event: TelePilotEvent, payload: dict[str, Any]) -> dict[str, Any]:
        if event.session and isinstance(event.session.data, dict):
            return dict(event.session.data)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {{}}
        data = session.get("data") if isinstance(session.get("data"), dict) else {{}}
        return dict(data)

    def _prize(self, event: TelePilotEvent, payload: dict[str, Any]) -> int:
        payment_amount = event.payment.amount if event.payment else None
        return self._int(payload.get("prize") or payment_amount, 100, minimum=0, maximum=1_000_000)

    def _timeout_seconds(self, payload: dict[str, Any]) -> int:
        raw = payload.get("valid_seconds") or payload.get("timeout")
        return self._int(raw, self._timeout, minimum=30, maximum=86400)

    def _start_text(self, state: dict[str, Any]) -> str:
        return (
            "口令抢答开始\\n"
            f"奖金：+{{state['prize']}}\\n"
            f"限时：{{state.get('timeout', self._timeout)}} 秒\\n"
            "直接发送正确口令即可抢答。"
        )

    def _int(self, value: Any, default: int, *, minimum: int, maximum: int | None = None) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed


PLUGIN_CLASS = {cls}

__all__ = ["{cls}", "PLUGIN_CLASS"]
'''


def _plugin_py_command(name: str) -> str:
    cls = _class_name(name)
    entry = _entry_key(name)
    return f'''"""{name}：command 脚手架示例。

演示"一次性命令动作"：既保留 UserBot 原命令入口（on_command 兼容），
也支持交互 Bot 的 command 事件（on_interaction），返回一条消息后立即结束，不建会话。
"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.events import event_from_interaction_payload


@register
class {cls}(Plugin):
    key = "{name}"
    display_name = "{name}"

    def __init__(self) -> None:
        super().__init__()
        self._command = "{name}"

    async def on_startup(self, ctx: PluginContext) -> None:
        cfg = ctx.config or {{}}
        self._command = str(cfg.get("command") or "{name}").strip() or "{name}"

    async def on_command(
        self,
        ctx: PluginContext,
        cmd: str,
        args: list[str],
        event: Any,
    ) -> bool:
        """UserBot 原命令兼容入口；命中返回 True。"""
        if cmd != self._command:
            return False
        if ctx.log:
            await ctx.log("info", f"[{name}] 命令兼容入口命中：{{cmd}} {{args}}")
        return True

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if entry_key != "{entry}":
            return None
        event = event_from_interaction_payload(payload)
        if event.type != "command":
            return []
        chat_id = event.message.chat_id
        args = self._trigger_args(payload)
        suffix = f"（参数：{{' '.join(args)}}）" if args else ""
        return [
            {{
                "type": "send_message",
                "chat_id": chat_id,
                "text": f"{name} 已执行{{suffix}}",
                "parse_mode": "plain",
                "reply_to_message_id": event.message.message_id,
            }},
            {{
                "type": "result",
                "success": True,
                "result": {{"status": "ok", "entry_key": entry_key}},
            }},
            {{"type": "end_session"}},
        ]

    def _trigger_args(self, payload: dict[str, Any]) -> list[str]:
        trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {{}}
        args = trigger.get("args")
        if isinstance(args, list):
            return [str(item) for item in args]
        if isinstance(args, str):
            return args.split()
        return []


PLUGIN_CLASS = {cls}

__all__ = ["{cls}", "PLUGIN_CLASS"]
'''


def _plugin_py_passthrough(name: str) -> str:
    cls = _class_name(name)
    return f'''"""{name}：passthrough 脚手架示例。

演示高风险的"低延时直通"入口 ``on_direct_message``。只有 manifest 声明
``capabilities.telegram_direct_passthrough.enabled=true``，且账号侧
``AccountFeature.config.direct_passthrough.enabled=true`` 时才会启用。
命中直通后本条消息不再进入普通消息链路，务必谨慎使用。

回复仍走平台受控投递 ``ctx.messages``，不直接调用 Telethon，以便统一限流与审计。
"""

from __future__ import annotations

from typing import Any

from app.worker.plugins.base import Plugin, PluginContext, register


@register
class {cls}(Plugin):
    key = "{name}"
    display_name = "{name}"
    message_channels = {{"incoming"}}
    owner_only = False

    def __init__(self) -> None:
        super().__init__()
        self._keyword = "ping"
        self._reply = "pong"

    async def on_startup(self, ctx: PluginContext) -> None:
        cfg = ctx.config or {{}}
        self._keyword = str(cfg.get("keyword") or "ping").strip() or "ping"
        self._reply = str(cfg.get("reply") or "pong").strip() or "pong"

    async def on_direct_message(self, ctx: PluginContext, event: Any) -> None:
        text = str(getattr(event, "raw_text", "") or "").strip()
        if self._keyword not in text:
            return
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None or ctx.messages is None:
            return
        await ctx.messages.send(
            chat_id=chat_id,
            text=self._reply,
            reply_to_message_id=getattr(event, "id", None),
        )


PLUGIN_CLASS = {cls}

__all__ = ["{cls}", "PLUGIN_CLASS"]
'''


def _plugin_py(name: str, profile: str) -> str:
    if profile == "session_game":
        return _plugin_py_session_game(name)
    if profile == "command":
        return _plugin_py_command(name)
    return _plugin_py_passthrough(name)


def _readme_md(name: str, profile: str) -> str:
    return (
        f"# {name}\n\n"
        f"由 `tp_plugin new --profile {profile}` 生成的脚手架插件。\n\n"
        f"- profile：`{profile}`\n"
        f"- {_usage(name, profile)}\n\n"
        "## 本地开发\n\n"
        "```bash\n"
        f"# 校验骨架\n"
        f"backend/.venv/bin/python backend/scripts/tp_plugin.py check plugins/local_imports/{name}\n"
        f"# 登记进台账，让 loader 能加载\n"
        f"backend/.venv/bin/python backend/scripts/tp_plugin.py register plugins/local_imports/{name}\n"
        "```\n\n"
        "登记后回到插件中心，选择账号启用即可运行。\n"
    )


def _changelog_md(name: str) -> str:
    return f"# 更新日志\n\n## 0.1.0\n- 由 tp_plugin 脚手架生成 {name} 初始骨架。\n"


def _test_py(name: str, profile: str) -> str:
    cls = _class_name(name)
    entry = _entry_key(name)
    if profile == "session_game":
        return f'''"""{name} 骨架自测：直调 on_interaction 断言开局动作序列。"""

from __future__ import annotations

import pytest

from .plugin import {cls}


@pytest.mark.asyncio
async def test_start_actions_order() -> None:
    plugin = {cls}()
    actions = await plugin.on_interaction(
        None,
        "{entry}",
        {{
            "source": {{"type": "command", "chat_id": -100123, "message_id": 7}},
            "trigger": {{"type": "command", "command": "{name}", "args": []}},
            "session": {{"scope": "chat", "channel": "userbot", "data": {{}}}},
            "answer": "888",
            "prize": 100,
        }},
    )
    types = [a["type"] for a in actions]
    # start_session 必须排在最前、且先于 update_session（跨通道安全开局写法）。
    assert types[0] == "start_session"
    assert types.index("start_session") < types.index("update_session")


@pytest.mark.asyncio
async def test_answer_wins_and_pays_out() -> None:
    plugin = {cls}()
    actions = await plugin.on_interaction(
        None,
        "{entry}",
        {{
            "source": {{"type": "message", "chat_id": -100123, "message_id": 9, "text": "888"}},
            "actor": {{"user_id": 111, "display_name": "AAA"}},
            "session": {{"scope": "chat", "data": {{"active": True, "answer": "888", "prize": 100}}}},
        }},
    )
    types = [a["type"] for a in actions]
    assert "payout" in types
    assert types[-1] == "end_session"
'''
    if profile == "command":
        return f'''"""{name} 骨架自测：直调 on_interaction 断言一次性命令动作。"""

from __future__ import annotations

import pytest

from .plugin import {cls}


@pytest.mark.asyncio
async def test_command_interaction_replies_and_ends() -> None:
    plugin = {cls}()
    actions = await plugin.on_interaction(
        None,
        "{entry}",
        {{
            "source": {{"type": "command", "chat_id": -100123, "message_id": 7}},
            "trigger": {{"type": "command", "command": "{name}", "args": ["hi"]}},
        }},
    )
    types = [a["type"] for a in actions]
    assert types[0] == "send_message"
    assert types[-1] == "end_session"
'''
    return f'''"""{name} 骨架自测：直调 on_direct_message 断言受控回复。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .plugin import {cls}


class _RecordingMessages:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, **kwargs) -> None:
        self.sent.append(kwargs)


@pytest.mark.asyncio
async def test_direct_message_replies_on_keyword() -> None:
    plugin = {cls}()
    plugin._keyword = "ping"
    plugin._reply = "pong"
    messages = _RecordingMessages()
    ctx = SimpleNamespace(messages=messages, log=None, config={{}})
    event = SimpleNamespace(raw_text="ping", chat_id=-100123, id=9)
    await plugin.on_direct_message(ctx, event)
    assert messages.sent and messages.sent[0]["text"] == "pong"
'''


# ─────────────────────────────────────────────────────
# new：生成骨架
# ─────────────────────────────────────────────────────
def scaffold_files(name: str, profile: str) -> dict[str, str]:
    """返回 ``{相对文件名: 内容}``，不落盘（便于 dry-run 与测试）。"""
    if profile not in PROFILES:
        raise ValueError(f"未知 profile：{profile!r}，可选 {'/'.join(PROFILES)}")
    if not _NAME_RE.match(name):
        raise ValueError(f"插件名仅允许字母/数字/_/-，得到 {name!r}")
    files = {
        "plugin.json": json.dumps(_plugin_json_dict(name, profile), ensure_ascii=False, indent=2) + "\n",
        "manifest.py": _manifest_py(name, profile),
        "plugin.py": _plugin_py(name, profile),
        "__init__.py": _init_py(name),
        "README.md": _readme_md(name, profile),
        "CHANGELOG.md": _changelog_md(name),
        "test_plugin.py": _test_py(name, profile),
    }
    return files


def scaffold_plugin(name: str, profile: str, dest_dir: Path, *, force: bool = False) -> Path:
    """在 ``dest_dir`` 落盘骨架，返回插件目录。"""
    plugin_dir = Path(dest_dir)
    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        if not force:
            raise FileExistsError(f"目标目录非空：{plugin_dir}（加 --force 覆盖）")
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in scaffold_files(name, profile).items():
        (plugin_dir / filename).write_text(content, encoding="utf-8")
    return plugin_dir


# ─────────────────────────────────────────────────────
# check：本地 lint
# ─────────────────────────────────────────────────────
@dataclass
class CheckReport:
    plugin_dir: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_events: list[str] = field(default_factory=list)
    unknown_filter_keys: list[str] = field(default_factory=list)
    inferred_permissions: list[str] = field(default_factory=list)
    missing_permissions: list[str] = field(default_factory=list)
    extra_permissions: list[str] = field(default_factory=list)
    inferred_http_domains: list[str] = field(default_factory=list)
    dynamic_http_calls: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _risk_text(permission: str) -> str:
    level, detail = _PERMISSION_RISK.get(permission, ("中风险", "需人工确认用途"))
    return f"{permission}（{level}：{detail}）"


def _string_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        literal_parts: list[str] = []
        for part in node.values:
            if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                return None
            literal_parts.append(part.value)
        return "".join(literal_parts)
    return None


def _domain_from_url(text: str | None) -> str | None:
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return parsed.hostname.lower()
    return None


def _attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return parts


def _literal_action_type(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values, strict=False):
        if _string_literal(key) == "type":
            return _string_literal(value)
    return None


def _infer_permissions_from_plugin_py(plugin_py: Path) -> tuple[set[str], set[str], int]:
    """静态扫描 plugin.py 中可识别的 SDK 调用和 action type，返回权限草案。"""
    inferred: set[str] = set()
    http_domains: set[str] = set()
    dynamic_http_calls = 0
    tree = ast.parse(plugin_py.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        action_type = _literal_action_type(node)
        if action_type:
            permission = _ACTION_TYPE_PERMISSIONS.get(action_type)
            if permission:
                inferred.add(permission)

        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        chain = _attr_chain(node.func)
        if len(chain) >= 3 and chain[0] == "ctx" and chain[1] == "messages":
            permission = _CTX_MESSAGE_METHOD_PERMISSIONS.get(chain[2])
            if permission:
                inferred.add(permission)
        if len(chain) >= 3 and chain[0] == "ctx" and chain[1] == "http":
            inferred.add("external_http")
            url_node = node.args[0] if node.args else None
            domain = _domain_from_url(_string_literal(url_node))
            if domain:
                http_domains.add(domain)
            else:
                dynamic_http_calls += 1
        if len(chain) >= 3 and chain[0] == "ctx" and chain[1] == "ai":
            inferred.add("ai_agent" if chain[2] == "run_agent" else "ai_text")

    return inferred, http_domains, dynamic_http_calls


def _collect_permission_issues(report: CheckReport, data: dict[str, Any]) -> None:
    plugin_py = report.plugin_dir / "plugin.py"
    if not plugin_py.is_file():
        return
    try:
        inferred, domains, dynamic_http_calls = _infer_permissions_from_plugin_py(plugin_py)
    except SyntaxError as exc:
        report.warnings.append(f"权限推导跳过：plugin.py 语法错误：{exc}")
        return

    declared_raw = data.get("permissions") if isinstance(data, dict) else []
    declared = {str(item) for item in declared_raw or [] if isinstance(item, str) and item.strip()}
    missing = sorted(inferred - declared)
    extra = sorted(declared - inferred)

    report.inferred_permissions = sorted(inferred)
    report.missing_permissions = missing
    report.extra_permissions = extra
    report.inferred_http_domains = sorted(domains)
    report.dynamic_http_calls = dynamic_http_calls

    if inferred:
        report.warnings.append(f"permissions 草案：{', '.join(report.inferred_permissions)}")
    if missing:
        report.warnings.append(f"permissions 声明漏了：{', '.join(_risk_text(item) for item in missing)}")
    if extra:
        report.warnings.append(f"permissions 声明多了：{', '.join(extra)}")
    declared_high = sorted(item for item in inferred & declared if _PERMISSION_RISK.get(item, ("", ""))[0] == "高风险")
    for item in declared_high:
        report.warnings.append(f"permissions 高风险已显式声明：{_risk_text(item)}")
    if domains:
        report.warnings.append(f"external_http 域名草案：{', '.join(report.inferred_http_domains)}")
    if dynamic_http_calls:
        report.warnings.append(f"external_http 有 {dynamic_http_calls} 处动态 URL，需人工确认域名白名单")


def _collect_event_issues(report: CheckReport, data: dict[str, Any], plugin_key: str) -> None:
    """复用 event_bus.normalize_event_subscription 做事件白名单校验。"""
    from app.services.event_bus import normalize_event_subscription

    unknown_events: set[str] = set()
    unknown_filters: set[str] = set()

    # 1) event_subscriptions[]（source/events/filters 完整信封）
    for idx, raw in enumerate(data.get("event_subscriptions") or [], start=1):
        if not isinstance(raw, dict):
            report.warnings.append(f"event_subscriptions[{idx}] 必须是对象")
            continue
        sub = normalize_event_subscription(raw, plugin_key=plugin_key)
        unknown_events.update(sub.unknown_events)
        unknown_filters.update(sub.unknown_filter_keys)

    # 2) interaction_entries[].events（入口声明的事件）
    for idx, raw in enumerate(data.get("interaction_entries") or [], start=1):
        if not isinstance(raw, dict):
            report.warnings.append(f"interaction_entries[{idx}] 必须是对象")
            continue
        events = raw.get("events")
        if events is not None:
            sub = normalize_event_subscription(
                {"events": events, "filters": raw.get("filters")},
                plugin_key=plugin_key,
                entry_key=str(raw.get("key") or ""),
            )
            unknown_events.update(sub.unknown_events)
            unknown_filters.update(sub.unknown_filter_keys)

    report.unknown_events = sorted(unknown_events)
    report.unknown_filter_keys = sorted(unknown_filters)
    for evt in report.unknown_events:
        report.warnings.append(f"unknown_events: {evt}（不匹配任何当前支持的事件类型）")
    for key in report.unknown_filter_keys:
        report.warnings.append(f"unknown_filter_keys: {key}（该过滤条件不会生效）")


def check_plugin(plugin_dir: str | Path) -> CheckReport:
    """本地校验一个插件目录，返回 CheckReport（不抛异常，问题记进 report）。"""
    plugin_dir = Path(plugin_dir)
    report = CheckReport(plugin_dir=plugin_dir)

    if not plugin_dir.is_dir():
        report.errors.append(f"目录不存在：{plugin_dir}")
        return report

    # 1) 必备文件
    for filename in _REQUIRED_FILES:
        if not (plugin_dir / filename).is_file():
            report.errors.append(f"缺少运行期文件：{filename}")

    # 2) plugin.json 结构（复用平台校验）
    pj = plugin_dir / "plugin.json"
    data: dict[str, Any] = {}
    plugin_key = plugin_dir.name
    if pj.is_file():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.errors.append(f"plugin.json 解析失败：{exc}")
            data = {}
        if isinstance(data, dict):
            plugin_key = str(data.get("name") or data.get("key") or plugin_dir.name)
            try:
                from app.services.remote_plugin_service import _read_plugin_metadata

                _read_plugin_metadata(plugin_dir, fallback_name=plugin_dir.name)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"plugin.json 字段校验失败：{exc}")

    # 3) manifest.py 可被 AST 解析（安装期同样不执行它）
    mf = plugin_dir / "manifest.py"
    if mf.is_file():
        try:
            ast.parse(mf.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            report.errors.append(f"manifest.py 语法错误：{exc}")

    # 4) 事件白名单校验
    if isinstance(data, dict):
        _collect_event_issues(report, data, plugin_key)

    # 5) 权限推导审计：只报 diff，不改写 manifest / plugin.json。
    if isinstance(data, dict):
        _collect_permission_issues(report, data)

    # 6) 平台元数据 lint（usage / 硬编码前缀 / 内部 import 等），并入 warnings
    try:
        from app.services.remote_plugin_service import lint_plugin_metadata_files

        report.warnings.extend(lint_plugin_metadata_files(plugin_dir))
    except Exception:  # noqa: BLE001
        pass

    return report


# ─────────────────────────────────────────────────────
# register：登记进本地台账
# ─────────────────────────────────────────────────────
def _iter_copyable_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        rel_parts = path.relative_to(root).parts
        if any(part in _COPY_IGNORE_DIRS for part in rel_parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def _directory_digest(root: Path) -> tuple[dict[str, str], int]:
    digest: dict[str, str] = {}
    newest_mtime_ns = 0
    for path in _iter_copyable_files(root):
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        digest[rel] = h.hexdigest()
    return digest, newest_mtime_ns


def _is_same_plugin_copy(source_dir: Path, target: Path) -> tuple[bool, bool]:
    source_digest, source_mtime = _directory_digest(source_dir)
    target_digest, target_mtime = _directory_digest(target)
    return source_digest == target_digest, source_mtime > target_mtime


async def register_plugin(
    db: Any,
    source_dir: str | Path,
    *,
    default_enabled: bool = False,
    force: bool = False,
) -> Any:
    """把 ``source_dir`` 登记进 installed_plugin 台账。

    目录不在 ``plugins/local_imports`` 时先拷进去，再复用
    ``plugin_repo_service.install_local_plugin``。返回其 RemotePluginView。
    """
    from app.services import plugin_repo_service as repo

    source_dir = Path(source_dir).resolve()
    pj = source_dir / "plugin.json"
    if not pj.is_file():
        raise repo.PluginRepoError("PLUGIN_JSON_NOT_FOUND", f"目录缺少 plugin.json：{source_dir}")
    try:
        meta = json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise repo.PluginRepoError("BAD_PLUGIN_JSON", f"plugin.json 解析失败：{exc}") from exc
    name = str(meta.get("name") or meta.get("key") or source_dir.name)

    local_root = repo._local_import_root()  # noqa: SLF001
    target = (local_root / name).resolve()
    if source_dir != target:
        if target.exists():
            same_content, source_newer = _is_same_plugin_copy(source_dir, target)
            if not same_content and not force:
                mtime_hint = "；外部目录更新时间更新" if source_newer else ""
                raise repo.PluginRepoError(
                    "STALE_LOCAL_IMPORT_COPY",
                    "已存在旧副本，未更新；请直接在 local_imports 内开发或手动删除后重登"
                    f"{mtime_hint}：{target}",
                )
            if not same_content and force:
                shutil.rmtree(target)
                shutil.copytree(
                    source_dir,
                    target,
                    ignore=shutil.ignore_patterns(*_COPY_IGNORE_DIRS),
                )
        else:
            shutil.copytree(
                source_dir,
                target,
                ignore=shutil.ignore_patterns(*_COPY_IGNORE_DIRS),
            )

    return await repo.install_local_plugin(db, name, default_enabled=default_enabled)


async def _register_via_session(
    source_dir: str | Path,
    *,
    default_enabled: bool,
    force: bool = False,
) -> tuple[int, str]:
    """打开真实会话执行 register，返回 ``(exit_code, message)``。"""
    from app.db.base import AsyncSessionLocal
    from app.services import plugin_repo_service as repo

    async with AsyncSessionLocal() as db:
        try:
            view = await register_plugin(db, source_dir, default_enabled=default_enabled, force=force)
        except repo.DuplicatePluginName as exc:
            await db.rollback()
            return 1, f"该插件已登记，无需重复：{exc.message}"
        except repo.PluginRepoError as exc:
            await db.rollback()
            return 1, f"登记失败[{exc.code}]：{exc.message}"
        await db.commit()
    name = getattr(view, "name", None) or getattr(view, "key", "?")
    return 0, f"已登记 {name}；回到插件中心选择账号启用即可运行。"


# ─────────────────────────────────────────────────────
# CLI 分发
# ─────────────────────────────────────────────────────
def _cmd_new(args: argparse.Namespace) -> int:
    parent = Path(args.dir) if args.dir else (_BACKEND_ROOT.parent / "plugins" / "local_imports")
    plugin_dir = parent / args.name
    if args.dry_run:
        try:
            files = scaffold_files(args.name, args.profile)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        print(f"[dry-run] 将在 {plugin_dir} 生成 {len(files)} 个文件（profile={args.profile}）：")
        for filename in files:
            print(f"  - {filename}")
        return 0
    try:
        created = scaffold_plugin(args.name, args.profile, plugin_dir, force=args.force)
    except (ValueError, FileExistsError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"已生成 {args.profile} 骨架：{created}")
    print(f"下一步：tp_plugin check {created}  然后  tp_plugin register {created}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    report = check_plugin(args.dir)
    print(f"检查 {report.plugin_dir}")
    for err in report.errors:
        print(f"  [错误] {err}")
    for warn in report.warnings:
        print(f"  [警告] {warn}")
    if report.ok and not report.warnings:
        print("  通过：未发现问题。")
    elif report.ok:
        print(f"  通过（有 {len(report.warnings)} 条警告）。")
    else:
        print(f"  未通过：{len(report.errors)} 个错误。")
    return 0 if report.ok else 1


def _cmd_register(args: argparse.Namespace) -> int:
    code, message = asyncio.run(
        _register_via_session(args.dir, default_enabled=args.enable, force=args.force)
    )
    print(message)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tp_plugin", description="TelePilot 插件脚手架 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="生成一个插件骨架")
    p_new.add_argument("name", help="插件名（字母/数字/_/-）")
    p_new.add_argument("--profile", choices=PROFILES, default="session_game", help="骨架类型")
    p_new.add_argument("--dir", default=None, help="生成到该父目录下（默认 plugins/local_imports）")
    p_new.add_argument("--force", action="store_true", help="目标目录非空时覆盖")
    p_new.add_argument("--dry-run", action="store_true", help="只打印将生成的文件，不落盘")
    p_new.set_defaults(func=_cmd_new)

    p_reg = sub.add_parser("register", help="把本地目录登记进 installed_plugin 台账")
    p_reg.add_argument("dir", help="插件目录路径")
    p_reg.add_argument("--enable", action="store_true", help="登记后默认对所有账号启用（默认关）")
    p_reg.add_argument("--force", action="store_true", help="外部目录与 local_imports 旧副本不一致时覆盖旧副本")
    p_reg.set_defaults(func=_cmd_register)

    p_chk = sub.add_parser("check", help="本地校验插件目录（manifest + 事件白名单）")
    p_chk.add_argument("dir", help="插件目录路径")
    p_chk.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
