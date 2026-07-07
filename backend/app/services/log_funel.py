"""消息漏斗判定。

这里是日志中心的唯一判据：输入一条 trace 的 spans/actions/status，
输出收到、匹配、执行、发送四段状态和最终排查结论。函数保持无 DB 依赖，
方便 API 聚合和单元测试共用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

StageStatus = Literal["pass", "skip", "stuck", "fail", "none"]
MessageVerdict = Literal["responded", "no_response_normal", "stuck", "failed"]


@dataclass(slots=True)
class MessageFunel:
    received: StageStatus
    routed: StageStatus
    ran: StageStatus
    sent: StageStatus
    verdict: MessageVerdict
    stuck_at: Literal["routed", "ran", "sent"] | None
    reason_code: str | None
    reason_text: str
    next_step: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


NORMAL_NO_RESPONSE_REASON_CODES = {
    "account_not_matched",
    "bot_self_message",
    "command_not_matched",
    "event_type_not_subscribed",
    "filter_not_matched",
    "interaction_rule_owned",
    "scope_not_matched",
    "source_not_subscribed",
    "subscription_not_matched",
    "userbot_command_message",
}

NORMAL_REASON_CODES = {
    "callback_query",
    "command_matched",
    "matched",
    "session_control_action",
}

REASON_LABELS: dict[str, str] = {
    "account_not_matched": "账号不匹配",
    "account_bot_user_unauthorized": "账号 Bot 用户未授权",
    "action_failed": "动作执行失败",
    "action_limit_exceeded": "动作数量超限",
    "already_acked": "已确认过的回调",
    "bot_not_configured": "交互 Bot 未配置",
    "bot_self_message": "忽略交互 Bot 自身消息",
    "bot_token_missing": "Bot token 缺失",
    "callback_query": "按钮回调",
    "callback_query_id_missing": "按钮回调 ID 缺失",
    "command_matched": "命令已命中",
    "command_not_matched": "未命中命令",
    "command_unauthorized": "命令权限不足",
    "contract_failed": "契约失败",
    "contract_warning": "契约告警",
    "empty_message_text": "消息文本为空",
    "entry_key_missing": "入口缺失",
    "event_bus_delivery_disabled": "Event Bus 投递已关闭",
    "event_type_not_subscribed": "事件类型不在触发入口内",
    "filter_not_matched": "过滤条件未命中",
    "handler_error": "处理器异常",
    "inline_disabled": "Inline 已关闭",
    "inline_query_answer_failed": "Inline 回答失败",
    "inline_query_id_missing": "Inline Query ID 缺失",
    "interaction_rule_owned": "交互规则归属账号不匹配",
    "manifest_invalid": "Manifest 不合法",
    "matched": "已命中",
    "media_payload_empty": "媒体内容为空",
    "media_payload_invalid": "媒体内容格式无效",
    "media_payload_missing": "媒体内容缺失",
    "native_raw_not_allowed": "未声明原生数据能力",
    "native_raw_skipped": "原生数据未下发",
    "payout_failed": "结算付款失败",
    "permission_denied": "权限不足",
    "plugin_disabled": "插件未启用",
    "plugin_load_failed": "插件加载失败",
    "plugin_not_installed": "插件未安装",
    "plugin_runtime_error": "插件运行异常",
    "rate_limited": "触发频控",
    "scope_not_matched": "范围不匹配",
    "send_channel_deprecated": "发送通道已废弃",
    "session_control_action": "会话控制动作",
    "session_expired": "会话已过期",
    "session_not_found": "会话不存在",
    "settlement_requires_userbot": "结算需要 UserBot",
    "source_not_subscribed": "来源不在触发入口内",
    "subscription_load_failed": "触发入口加载失败",
    "subscription_not_matched": "触发入口未命中",
    "synthetic_callback": "合成按钮回调",
    "target_message_id_missing": "目标消息 ID 缺失",
    "telegram_api_error": "Telegram API 错误",
    "trace_write_failed": "Trace 写入降级",
    "unsupported_send_via": "发送通道不支持",
    "userbot_command_message": "UserBot 命令消息已让路",
    "userbot_offline": "UserBot 离线",
}


def reason_label(code: str | None) -> str:
    if not code:
        return ""
    return REASON_LABELS.get(code, code)


def reason_display(code: str | None) -> str:
    if not code:
        return ""
    label = reason_label(code)
    return f"{label} ({code})" if label and label != code else code


def build_message_funel(
    trace: Any,
    spans: list[Any] | tuple[Any, ...],
    actions: list[Any] | tuple[Any, ...],
) -> MessageFunel:
    trace_status = _norm(_get(trace, "status"))
    route_spans = [span for span in spans if _is_route_span(span)]
    plugin_spans = [span for span in spans if _is_plugin_span(span)]

    failed_action = next((action for action in actions if _is_failed_action(action)), None)
    if failed_action is not None:
        reason_code = _text(_get(failed_action, "error_code")) or "action_failed"
        return MessageFunel(
            received="pass",
            routed="pass" if route_spans or plugin_spans or actions else "skip",
            ran="pass" if plugin_spans else "skip",
            sent="fail",
            verdict="failed",
            stuck_at="sent",
            reason_code=reason_code,
            reason_text=_action_reason_text(failed_action, reason_code),
            next_step="先看发送动作的实际通道、目标会话和 Telegram API 错误；插件逻辑可能已执行完，是落地发送失败。",
        )

    failed_span = next((span for span in spans if _is_failed_status(_get(span, "status"))), None)
    if failed_span is not None:
        failed_at = _span_stage(failed_span)
        reason_code = _text(_get(failed_span, "reason_code")) or "handler_error"
        return MessageFunel(
            received="pass",
            routed="fail" if failed_at == "routed" else "pass",
            ran="fail" if failed_at == "ran" else "skip" if failed_at == "routed" else "pass",
            sent="none",
            verdict="failed",
            stuck_at=failed_at,
            reason_code=reason_code,
            reason_text=_span_reason_text(failed_span, reason_code),
            next_step=_failed_next_step(failed_at),
        )

    normal_skip_span = next(
        (
            span
            for span in route_spans
            if _text(_get(span, "reason_code")) in NORMAL_NO_RESPONSE_REASON_CODES
            or (
                _is_warn_status(_get(span, "status"))
                and _text(_get(span, "reason_code")) not in NORMAL_REASON_CODES
                and not plugin_spans
                and not actions
            )
        ),
        None,
    )
    if normal_skip_span is not None and not plugin_spans and not actions:
        reason_code = _text(_get(normal_skip_span, "reason_code")) or "subscription_not_matched"
        return MessageFunel(
            received="pass",
            routed="skip",
            ran="skip",
            sent="none",
            verdict="no_response_normal",
            stuck_at=None,
            reason_code=reason_code,
            reason_text=f"这不是故障：没有插件关心这条消息，系统正常跳过。原因：{reason_display(reason_code)}",
            next_step="如果本来应该响应，检查关键词、事件类型、来源通道、会话范围和插件启用状态。",
        )

    if _is_failed_status(trace_status):
        return MessageFunel(
            received="pass",
            routed="pass" if route_spans or plugin_spans or actions else "skip",
            ran="pass" if plugin_spans else "skip",
            sent="none",
            verdict="failed",
            stuck_at="ran" if plugin_spans else "routed",
            reason_code=None,
            reason_text="链路标记为失败，但 span/action 没有给出更具体的失败原因。",
            next_step="用 trace_id 搜索运行日志，确认是否是插件异常、Trace 写入降级或框架兜底失败。",
        )

    if _looks_stuck(trace_status, plugin_spans, actions):
        return MessageFunel(
            received="pass",
            routed="pass" if route_spans else "skip",
            ran="stuck",
            sent="none",
            verdict="stuck",
            stuck_at="ran",
            reason_code=None,
            reason_text="消息进入了插件执行阶段，但没有看到完成状态或发送动作。",
            next_step="优先查看插件是否卡在 await、外部请求或长耗时逻辑；必要时用 trace_id 搜索插件运行日志。",
        )

    if actions:
        return MessageFunel(
            received="pass",
            routed="pass" if route_spans or plugin_spans else "skip",
            ran="pass" if plugin_spans else "skip",
            sent="pass",
            verdict="responded",
            stuck_at=None,
            reason_code=None,
            reason_text="消息已进入动作发送链路并完成记录。",
            next_step="如果群里仍看不到响应，展开详情确认实际发送通道、目标会话和 Telegram message id。",
        )

    if plugin_spans and _is_completed_trace(trace_status, trace):
        return MessageFunel(
            received="pass",
            routed="pass" if route_spans else "skip",
            ran="pass",
            sent="none",
            verdict="responded",
            stuck_at=None,
            reason_code=None,
            reason_text="消息已被插件处理完成，但插件没有产生发送动作。",
            next_step="如果本来应该回复，检查插件是否返回 send_message 等动作，或是否被配置成只处理不回复。",
        )

    if _is_warn_status(trace_status):
        reason_code = _first_reason_code(spans) or _first_error_code(actions)
        return MessageFunel(
            received="pass",
            routed="skip" if not plugin_spans else "pass",
            ran="skip" if not plugin_spans else "pass",
            sent="none",
            verdict="no_response_normal" if not plugin_spans else "stuck",
            stuck_at=None if not plugin_spans else "ran",
            reason_code=reason_code,
            reason_text=(
                f"链路被正常跳过。原因：{reason_display(reason_code)}"
                if not plugin_spans
                else f"链路有告警：{reason_display(reason_code) or trace_status}"
            ),
            next_step="若这条消息应该响应，检查触发入口、过滤条件和插件启用状态。",
        )

    return MessageFunel(
        received="pass",
        routed="pass" if route_spans or plugin_spans else "skip",
        ran="pass" if plugin_spans else "skip",
        sent="none",
        verdict="responded" if plugin_spans else "no_response_normal",
        stuck_at=None,
        reason_code=None,
        reason_text="消息链路已结束，没有发现失败阶段。",
        next_step="如果仍觉得不符合预期，展开详情查看关键时间线和原始字段。",
    )


def _get(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_failed_status(value: Any) -> bool:
    return _norm(value) in {"failed", "error"}


def _is_warn_status(value: Any) -> bool:
    return _norm(value) in {"warning", "warn", "skipped"}


def _is_completed_trace(status: str, trace: Any) -> bool:
    return status in {"ok", "success", "skipped"} or _get(trace, "ended_at") is not None


def _is_failed_action(action: Any) -> bool:
    return (
        _is_failed_status(_get(action, "status"))
        or bool(_text(_get(action, "error_code")))
        or bool(_text(_get(action, "error_message")))
    )


def _is_route_span(span: Any) -> bool:
    phase = _norm(_get(span, "phase"))
    component = _norm(_get(span, "component"))
    return (
        "route" in phase
        or "subscription" in phase
        or "guard" in component
        or "dispatcher" in component
        or "event_bus" in component
    )


def _is_plugin_span(span: Any) -> bool:
    phase = _norm(_get(span, "phase"))
    return bool(_text(_get(span, "plugin_key"))) or "plugin" in phase


def _span_stage(span: Any) -> Literal["routed", "ran", "sent"]:
    phase = _norm(_get(span, "phase"))
    component = _norm(_get(span, "component"))
    if "delivery" in phase or "send" in phase or "settlement" in phase:
        return "sent"
    if _is_route_span(span) and "plugin" not in phase and not _text(_get(span, "plugin_key")):
        return "routed"
    if "contract" in component and not _text(_get(span, "plugin_key")):
        return "routed"
    return "ran"


def _looks_stuck(trace_status: str, plugin_spans: list[Any], actions: list[Any] | tuple[Any, ...]) -> bool:
    if actions or not plugin_spans:
        return False
    if trace_status in {"running", "received", "normalized", "matched", "delivered", ""}:
        return True
    return any(_get(span, "ended_at") is None for span in plugin_spans)


def _first_reason_code(spans: list[Any] | tuple[Any, ...]) -> str | None:
    for span in spans:
        code = _text(_get(span, "reason_code"))
        if code:
            return code
    return None


def _first_error_code(actions: list[Any] | tuple[Any, ...]) -> str | None:
    for action in actions:
        code = _text(_get(action, "error_code"))
        if code:
            return code
    return None


def _action_reason_text(action: Any, reason_code: str) -> str:
    message = _text(_get(action, "error_message"))
    label = reason_display(reason_code)
    return f"{label}：{message}" if message and message != reason_label(reason_code) else label or "发送动作失败"


def _span_reason_text(span: Any, reason_code: str) -> str:
    message = _text(_get(span, "message"))
    label = reason_display(reason_code)
    return f"{label}：{message}" if message and message != reason_label(reason_code) else label or "链路阶段失败"


def _failed_next_step(stage: Literal["routed", "ran", "sent"]) -> str:
    if stage == "routed":
        return "先看触发入口、权限、会话范围和 Contract Guard；消息还没有稳定进入插件逻辑。"
    if stage == "sent":
        return "先看发送通道、目标会话、UserBot 在线状态和 Telegram API 错误。"
    return "先看插件的 handler_error、运行日志和最近一次调用状态；消息已经进入插件执行阶段。"
