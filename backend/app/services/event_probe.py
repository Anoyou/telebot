"""Developer-facing probe report for TelePilot event traces.

The probe is intentionally read-only. It explains the already persisted
standard event envelope, routing decisions, and action records without touching
Telegram, worker state, or plugin runtime objects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .redactor import redact_text, redact_value

_TEXT_PREVIEW_LIMIT = 240


def build_event_probe_report(
    *,
    trace: Mapping[str, Any],
    raw_summary: Mapping[str, Any] | None = None,
    payload_snapshot: Mapping[str, Any] | None = None,
    native_raw_meta: Mapping[str, Any] | None = None,
    spans: Sequence[Any] = (),
    actions: Sequence[Any] = (),
) -> dict[str, Any]:
    """Build a compact event-probe report for one trace detail response."""

    payload = payload_snapshot if isinstance(payload_snapshot, Mapping) else {}
    source = _mapping(payload.get("source"))
    message = _mapping(payload.get("message"))
    chat = _mapping(payload.get("chat"))
    sender = _mapping(payload.get("sender") or payload.get("source_actor"))
    callback = _mapping(payload.get("callback"))
    inline_query = _mapping(payload.get("inline_query"))
    chosen_inline_result = _mapping(payload.get("chosen_inline_result"))
    payment = _mapping(payload.get("payment"))
    trigger = _mapping(payload.get("trigger"))
    raw = raw_summary if isinstance(raw_summary, Mapping) else {}

    event_type = _first_text(source.get("type"), payload.get("event_type"), trace.get("event_type")) or "message"
    source_channel = _first_text(source.get("channel"), source.get("bot_role"), trace.get("source_channel")) or "unknown"
    chat_id = _first_value(message.get("chat_id"), chat.get("id"), trace.get("chat_id"))
    message_id = _first_value(message.get("message_id"), trace.get("message_id"))
    text = _first_text(message.get("text"), payload.get("message_text"), trace.get("text_preview"))
    callback_data = _first_text(source.get("callback_data"), payload.get("callback_data"), callback.get("data"))
    callback_id = _first_text(source.get("callback_query_id"), payload.get("callback_query_id"), callback.get("id"))
    inline_query_id = _first_text(source.get("inline_query_id"), inline_query.get("id"))

    facts = _compact_items(
        [
            _item("事件类型", "payload.source.type", event_type),
            _item("来源通道", "payload.source.channel", source_channel),
            _item("驱动", "payload.source.driver", source.get("driver")),
            _item("账号", "payload.source.account_id", _first_value(source.get("account_id"), trace.get("account_id"))),
            _item("会话", "payload.message.chat_id", chat_id),
            _item("消息", "payload.message.message_id", message_id),
            _item("发送者", "payload.sender.user_id", _first_value(sender.get("user_id"), trace.get("sender_user_id"))),
            _item("显示名", "payload.sender.display_name", _first_text(sender.get("display_name"), trace.get("sender_name"))),
            _item("文本", "payload.message.text", text),
            _item("回调数据", "payload.callback.data", callback_data),
            _item("Inline Query", "payload.inline_query.query", inline_query.get("query")),
            _item("Chosen Result", "payload.chosen_inline_result.result_id", chosen_inline_result.get("result_id")),
            _item("付款金额", "payload.payment.amount", payment.get("amount")),
            _item("触发入口", "payload.trigger.entry_key", _first_text(trigger.get("entry_key"), payload.get("entry_key"))),
        ]
    )
    raw_facts = _raw_summary_facts(raw, payload)
    routing = _routing_decisions(spans)
    action_hints = _action_suggestions(
        event_type=event_type,
        source_channel=source_channel,
        chat_id=chat_id,
        message_id=message_id,
        sender_user_id=_first_value(sender.get("user_id"), trace.get("sender_user_id")),
        callback_id=callback_id,
        callback_data=callback_data,
        inline_query_id=inline_query_id,
        payment=payment,
    )
    capability_hints = _capability_hints(native_raw_meta, raw, payload, event_type)

    return {
        "version": 1,
        "headline": f"{event_type} / {source_channel}",
        "summary": {
            "event_type": event_type,
            "source_channel": source_channel,
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_user_id": _first_value(sender.get("user_id"), trace.get("sender_user_id")),
            "text_preview": _clip(text),
        },
        "field_paths": facts,
        "message_facts": raw_facts,
        "subscription_suggestions": _subscription_suggestions(
            event_type=event_type,
            source_channel=source_channel,
            text=text,
            callback_data=callback_data,
        ),
        "action_suggestions": action_hints,
        "capability_hints": capability_hints,
        "routing": routing,
        "warnings": _warnings(native_raw_meta, routing, actions),
    }


def _subscription_suggestions(
    *,
    event_type: str,
    source_channel: str,
    text: str | None,
    callback_data: str | None,
) -> list[dict[str, Any]]:
    scope = "inline_all" if event_type in {"inline_query", "chosen_inline_result"} else "all_allowed_chats"
    filters: dict[str, Any] = {}
    reason = "按当前事件来源和类型订阅同类消息。"
    if event_type == "callback_query" and callback_data:
        prefix = callback_data.split(":", 1)[0]
        filters["callback_data"] = [callback_data]
        reason = f"当前按钮 data 为 {callback_data!r}；若同一组按钮有前缀，可在插件内按 {prefix!r} 自行判断。"
    elif event_type == "command" and text:
        head = text.strip().split(maxsplit=1)[0].lstrip("/,.，!！")
        if head:
            filters["commands"] = [head]
            reason = "当前消息看起来是管理员命令，可先按命令名订阅。"
            scope = "owner_only"
    elif event_type == "message" and text:
        filters["contains"] = [text[:32]]
        reason = "当前是普通消息；真实插件可把 contains 改成关键词、正则或自己的业务判断。"
    suggestion = {
        "title": "推荐 event_subscriptions",
        "reason": reason,
        "manifest": {
            "events": [event_type],
            "source": [source_channel],
            "scope": scope,
        },
    }
    if filters:
        suggestion["manifest"]["filters"] = filters
    return [suggestion]


def _action_suggestions(
    *,
    event_type: str,
    source_channel: str,
    chat_id: Any,
    message_id: Any,
    sender_user_id: Any,
    callback_id: str | None,
    callback_data: str | None,
    inline_query_id: str | None,
    payment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    if event_type == "callback_query":
        suggestions.append(
            {
                "title": "先 ACK 按钮",
                "reason": "按钮回调需要尽快 answer_callback，避免 Telegram 客户端一直转圈。",
                "action": {
                    "type": "answer_callback",
                    "callback_query_id": callback_id or "<callback_query_id>",
                    "text": "已收到",
                    "show_alert": False,
                },
            }
        )
        if chat_id is not None and message_id is not None:
            suggestions.append(
                {
                    "title": "按需编辑原消息",
                    "reason": f"当前 callback_data={callback_data!r}，可编辑按钮所在消息展示新状态。",
                    "action": {
                        "type": "edit_message",
                        "send_via": "interaction_bot",
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": "<新的消息文本>",
                    },
                }
            )
    elif event_type == "inline_query":
        suggestions.append(
            {
                "title": "回答 Inline Query",
                "reason": "Inline 入口必须返回 answer_inline_query，由平台记录结果数量和失败原因。",
                "action": {
                    "type": "answer_inline_query",
                    "inline_query_id": inline_query_id or "<inline_query_id>",
                    "results": [],
                    "cache_time": 0,
                    "is_personal": True,
                },
            }
        )
    elif event_type == "payment_confirmed":
        suggestions.append(
            {
                "title": "记录结算或公告",
                "reason": "付款确认应走 settlement 或 userbot_reply，普通 Bot 只适合公告。",
                "action": {
                    "type": "settlement",
                    "mode": "confirm_only",
                    "amount": payment.get("amount"),
                    "currency": payment.get("currency"),
                    "status": payment.get("status") or "confirmed",
                },
            }
        )
    elif chat_id is not None:
        suggestions.append(
            {
                "title": "回复当前消息",
                "reason": f"从 {source_channel} 收到消息后，优先返回标准 send_message action，让平台选择实际通道并写 Trace。",
                "action": {
                    "type": "send_message",
                    "send_via": ["interaction_bot", "userbot_reply"],
                    "chat_id": chat_id,
                    "reply_to_message_id": message_id,
                    "text": "<回复文本>",
                },
            }
        )
    if event_type == "callback_query" and chat_id is not None:
        suggestions.append(
            {
                "title": "按钮参与者发奖锚点",
                "reason": "按钮点击者来自 payload.sender.user_id；需要 userbot 发奖时，可让平台按该 user_id 搜索群内最近发言并自动 reply。",
                "action": {
                    "type": "send_message",
                    "send_via": "userbot_reply",
                    "chat_id": chat_id,
                    "reply_to_user_id": sender_user_id or "<payload.sender.user_id>",
                    "reply_to_search_limit": 5000,
                    "text": "+<奖励金额>",
                },
            }
        )
    return suggestions


def _capability_hints(
    native_raw_meta: Mapping[str, Any] | None,
    raw_summary: Mapping[str, Any],
    payload: Mapping[str, Any],
    event_type: str,
) -> list[dict[str, Any]]:
    meta = native_raw_meta if isinstance(native_raw_meta, Mapping) else {}
    enabled = bool(meta.get("enabled"))
    stored = bool(meta.get("stored_in_trace"))
    reason_code = _first_text(meta.get("reason_code"))
    hints = [
        {
            "title": "标准信封优先",
            "level": "info",
            "capability": "standard_envelope",
            "reason": "插件主路径应读取 payload.source/message/chat/sender/callback/inline_query/payment，并通过 ctx.messages 或标准 action 输出。",
        }
    ]
    if enabled:
        hints.append(
            {
                "title": "native_raw 已授权",
                "level": "warn" if stored else "info",
                "capability": "telegram_native_raw",
                "reason": "该插件可读取原生 Telegram 摘要。业务逻辑仍应优先使用标准信封，native_raw 只做补充排障。",
                "stored_in_trace": stored,
            }
        )
    else:
        interesting = [
            key
            for key in ("entities", "caption_entities", "media", "reply_markup", "forward", "sender_chat", "via_bot")
            if raw_summary.get(key) or payload.get(key)
        ]
        hints.append(
            {
                "title": "native_raw 未下发",
                "level": "neutral",
                "capability": "telegram_native_raw",
                "reason": (
                    f"当前标准摘要已包含 {', '.join(interesting)}；只有需要完整原生结构时再声明 capability。"
                    if interesting
                    else f"{event_type} 当前没有必须读取原生结构的明显信号。"
                ),
                "reason_code": reason_code or "native_raw_not_allowed",
            }
        )
    if event_type in {"callback_query", "inline_query", "chosen_inline_result"}:
        hints.append(
            {
                "title": "Bot 侧事件",
                "level": "info",
                "capability": "interaction_bot",
                "reason": "按钮和 Inline 相关动作必须经 MessageOps / 标准 action，由平台处理 Bot token、失败记录和限流。",
            }
        )
    return hints


def _routing_decisions(spans: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for span in spans:
        phase = str(_get(span, "phase") or "")
        if "subscription" not in phase and phase not in {"route", "plugin_invoke", "plugin_return"}:
            continue
        reason_code = _get(span, "reason_code")
        status = str(_get(span, "status") or "")
        matched = reason_code == "matched" or (phase == "plugin_invoke" and status == "ok")
        out.append(
            {
                "phase": phase,
                "plugin_key": _get(span, "plugin_key"),
                "entry_key": _get(span, "entry_key"),
                "matched": matched,
                "status": status,
                "reason_code": reason_code,
                "message": _clip(_get(span, "message")),
                "filters": _mapping(_get(span, "detail")).get("filters"),
            }
        )
    return out[:20]


def _warnings(
    native_raw_meta: Mapping[str, Any] | None,
    routing: Sequence[Mapping[str, Any]],
    actions: Sequence[Any],
) -> list[str]:
    warnings: list[str] = []
    meta = native_raw_meta if isinstance(native_raw_meta, Mapping) else {}
    if meta.get("stored_in_trace"):
        warnings.append("完整 native_raw 已按设置进入 trace，注意保留期和隐私边界。")
    if routing and not any(item.get("matched") for item in routing):
        warnings.append("当前链路没有命中 Event Bus 订阅，优先检查 source/events/scope/filters。")
    deprecated = [
        _get(action, "requested_send_via")
        for action in actions
        if "notice" in str(_get(action, "requested_send_via") or "")
    ]
    if deprecated:
        warnings.append("检测到旧 notice/bbot_notice 通道请求，需迁移到 interaction_bot、userbot_reply 或 auto。")
    return warnings


def _raw_summary_facts(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _compact_items(
        [
            _item("聊天类型", "payload.chat.type", _nested(payload, "chat", "type")),
            _item("回复消息", "payload.reply_to.message_id", _nested(payload, "reply_to", "message_id")),
            _item("回复文本", "payload.reply_to.text", _nested(payload, "reply_to", "text")),
            _item("实体", "payload.raw.entities", raw.get("entities")),
            _item("媒体", "payload.raw.media", raw.get("media")),
            _item("按钮", "payload.raw.reply_markup", raw.get("reply_markup")),
            _item("转发", "payload.raw.forward", raw.get("forward")),
            _item("频道身份", "payload.raw.sender_chat", raw.get("sender_chat")),
            _item("Via Bot", "payload.raw.via_bot", raw.get("via_bot")),
            _item("话题/线程", "payload.raw.thread_id", raw.get("thread_id")),
        ]
    )


def _item(label: str, path: str, value: Any, note: str | None = None) -> dict[str, Any] | None:
    if value in (None, "", [], {}):
        return None
    out = {"label": label, "path": path, "value": redact_value(value)}
    if isinstance(out["value"], str):
        out["value"] = _clip(out["value"])
    if note:
        out["note"] = note
    return out


def _compact_items(items: Sequence[dict[str, Any] | None]) -> list[dict[str, Any]]:
    return [item for item in items if item is not None]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _nested(source: Mapping[str, Any], *path: str) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _first_text(*values: Any) -> str | None:
    value = _first_value(*values)
    if value is None:
        return None
    text = str(value).strip()
    return redact_text(text) if text else None


def _clip(value: Any, limit: int = _TEXT_PREVIEW_LIMIT) -> str | None:
    if value in (None, ""):
        return None
    text = redact_text(str(value).replace("\r", ""))
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...(+{len(text) - limit} chars)"


__all__ = ["build_event_probe_report"]
