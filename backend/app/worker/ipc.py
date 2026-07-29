"""主进程 ↔ worker 之间的 IPC 协议。

通信通道命名约定（Redis pub/sub）：
- ``worker_cmd:{account_id}``    主进程 → worker  下发指令
- ``worker_event:{account_id}``  worker → 主进程  上报事件 / 日志 / 限速事件
- ``worker_global``              广播指令（全员适用，例如 kill switch 切换）

消息使用 JSON，统一字段：
    { "type": "...", "ts": <epoch_ms>, "payload": { ... } }
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ── Channel 模板 ──────────────────────────────────────────────────
def cmd_channel(account_id: int) -> str:
    return f"worker_cmd:{account_id}"


def event_channel(account_id: int) -> str:
    return f"worker_event:{account_id}"


GLOBAL_CHANNEL = "worker_global"
RUNTIME_LOG_STREAM = "runtime_log_stream"          # 主进程消费此 list 落库
RATELIMIT_EVENT_STREAM = "ratelimit_event_stream"  # 主进程消费此 list 落库
RPC_RESULT_TTL_SECONDS = 3600


def rpc_result_key(request_id: str) -> str:
    return f"interaction_rpc:result:{request_id}"


# ── 指令类型（主→worker） ────────────────────────────────────────
CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_STOP = "stop"
CMD_RELOAD_CONFIG = "reload_config"        # 拉新风控/拟人化配置
CMD_RELOAD_PLUGIN = "reload_plugin"        # payload: {plugin_key}
# 自定义命令模板 / LLM provider 变化后通知 worker 热加载（无 payload）
CMD_RELOAD_COMMANDS = "reload_commands"
CMD_RUN_TG_COMMAND = "run_tg_command"      # 用于 Web 触发 TG 命令（可选）
CMD_PING = "ping"
# 让 worker 把当前账号头像下载到本地磁盘缓存（payload: {"path": "<绝对路径>"}）
# 主进程的 ensure_avatar 用 fire-and-forget 方式发送，worker 写盘后由下次请求读到
CMD_FETCH_AVATAR = "fetch_avatar"
# 通知 worker 重新拉取忽略名单（账号忽略 peer 增删后下发；payload 为空）
CMD_RELOAD_IGNORED = "reload_ignored"
# RPC：拉 worker 内存中的最近活跃 peer（payload: {"reply_to": <一次性应答频道>}）
CMD_GET_RECENT_PEERS = "get_recent_peers"
# RPC：手动执行 scheduler 规则（payload: {"rule_id": int, "reply_to": <应答频道>}）
CMD_EXECUTE_RULE = "execute_rule"
# RPC：交互 Bot 调用已加载插件声明的交互入口
CMD_RUN_INTERACTION_ENTRY = "run_interaction_entry"
# RPC：主进程请求账号 worker 用 userbot 身份执行交互动作（reply / file）
CMD_RUN_INTERACTION_ACTION = "run_interaction_action"
CMD_DISPATCH_SIMULATE = "dispatch_simulate"  # WP4：命中调试模拟预埋
CMD_WEBHOOK_DELIVER = "webhook_deliver"      # WP7：入站 Webhook 投递预埋
# RPC：System Agent 调用插件暴露的只读 agent_tools
CMD_AGENT_PLUGIN_TOOL = "agent_plugin_tool"

# ── 事件类型（worker→主） ──────────────────────────────────────
EVT_STATUS = "status"                      # payload: {status: active|paused|...}
EVT_LOG = "log"                            # payload: {level, source, message, detail}
EVT_RATELIMIT = "ratelimit"                # payload: {action, outcome, detail}
EVT_PLUGIN_STATE = "plugin_state"          # payload: {feature_key, state, last_error?}
EVT_LOGIN_REQUIRED = "login_required"      # session 失效
EVT_PONG = "pong"
EVT_ACK = "ack"                            # payload: {cmd_id, cmd_type, ok, error?}


# ── 全局指令 ────────────────────────────────────────────────
GCMD_KILL_SWITCH = "kill_switch"           # payload: {enabled: bool}
GCMD_RELOAD_GLOBAL = "reload_global"


@dataclass
class IPCMessage:
    """统一的 IPC 消息结构。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def encode(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def decode(cls, raw: str | bytes) -> IPCMessage:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        d = json.loads(raw)
        return cls(type=d["type"], payload=d.get("payload") or {}, ts=d.get("ts") or 0)


# ── 便捷构造函数 ──────────────────────────────────────────────
def make_cmd(type_: str, **payload: Any) -> str:
    return IPCMessage(type=type_, payload=payload).encode()


def make_event(type_: str, **payload: Any) -> str:
    return IPCMessage(type=type_, payload=payload).encode()


def ack_channel(account_id: int, cmd_id: str) -> str:
    return f"worker_ack:{account_id}:{cmd_id}"


async def publish_cmd_with_ack(
    redis: Any,
    account_id: int,
    type_: str,
    *,
    timeout: float = 2.0,
    ack_validator: Any | None = None,
    **payload: Any,
) -> bool:
    """发布 worker 指令并等待可选 ACK。

    worker 离线或旧版本 worker 不回 ACK 时返回 False；调用方可继续依赖
    DB 持久状态和周期 reconcile 收敛，不把用户请求硬失败。
    """
    cmd_id = str(uuid.uuid4())
    reply_to = ack_channel(account_id, cmd_id)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(reply_to)
        await redis.publish(
            cmd_channel(account_id),
            make_cmd(type_, cmd_id=cmd_id, reply_to=reply_to, **payload),
        )
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            msg = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                timeout=remaining + 0.1,
            )
            if not msg:
                continue
            ack = IPCMessage.decode(msg["data"])
            if ack.type == EVT_ACK and ack.payload.get("cmd_id") == cmd_id:
                if not bool(ack.payload.get("ok", False)):
                    return False
                return bool(ack_validator(ack.payload)) if ack_validator is not None else True
        return False
    except TimeoutError:
        return False
    finally:
        try:
            await pubsub.unsubscribe(reply_to)
        finally:
            close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
            if close is not None:
                ret = close()
                if hasattr(ret, "__await__"):
                    await ret


# Worker -> 主进程：限速事件结构（也用于直接写 RATELIMIT_EVENT_STREAM）
@dataclass
class RateLimitEventPayload:
    account_id: int
    action: str
    outcome: str
    detail: dict[str, Any] | None = None
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def encode(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


# Worker -> 主进程：运行日志结构
@dataclass
class RuntimeLogPayload:
    account_id: int | None
    level: Literal["debug", "info", "warn", "error"]
    source: str | None
    message: str
    detail: dict[str, Any] | None = None
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def encode(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)
