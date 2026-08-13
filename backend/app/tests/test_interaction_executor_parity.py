"""三执行体（E1/E2/E3）防漂移 parity 快照测试。

拓扑（锚点以函数名为准，均已 grep 复核）：
- E1 = worker 直执行：``worker/plugins/loader.py::_apply_userbot_event_bus_actions``
- E2 = bot delivery：``services/interaction/delivery.py::InteractionDeliveryExecutor.apply``
- E3 = worker RPC 动作面：``worker/runtime.py::_run_interaction_userbot_action``（E2 userbot_reply 的真正落点）

快照口径：执行器与插件 facade 必须保持相同的会话抓取与去重语义：
- 快照拍“当前代码现状”。已知有意差异与漂移都直接编码成 ``expect_worker`` / ``expect_bot``
  两列不同期望——**快照即规格**；任一侧行为变化会以表格 diff 显式暴露。
- 断言归一：从两侧 ``record_action`` 的 ``await_args_list`` 抽
  ``(status, error_code, actual_send_via)`` 比对（``action_type`` 用于定位记录）。
- E1 驱动：直调 ``_apply_userbot_event_bus_actions``；``state.engine=None`` 隔离限速，
  mock client / redis / record_action，``payout_limit.check_and_consume`` → (True, None)。
- E2+E3 全链路：executor 的 ``run_worker_action`` 用 in-process lambda 直调
  ``_run_interaction_userbot_action``（等价复刻 RPC 落点 ``_handle_run_interaction_action_command``
  的 (ok, error, result) 映射，无真 pubsub）；patch delivery 命名空间的 ``record_action``。
- 全 mock、毫秒级，不依赖真实 Redis / Telegram。
"""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonRow,
    ReplyInlineMarkup,
)

from app.services import account_bot_runtime, account_bot_service, platform_capabilities, userbot_rich_message
from app.services import payout_limit as payout_limit_mod
from app.services.event_trace import TRACE_STATUS_FAILED, TRACE_STATUS_OK, TRACE_STATUS_SKIPPED
from app.services.interaction import delivery as delivery_mod
from app.services.interaction.delivery import InteractionDeliveryExecutor
from app.services.payout_limit import PayoutLimitExceeded
from app.worker import runtime as worker_runtime
from app.worker.plugins import loader as loader_mod

CHAT = -100
B64 = base64.b64encode(b"parity-media").decode("ascii")
SESSION_KEY = "tp:isession:parity:1"


@pytest.fixture(autouse=True)
def _enable_ledger_actions_for_parity_tests() -> None:
    platform_capabilities._reset_for_tests()
    platform_capabilities._CACHE_READY = True
    platform_capabilities._DESIRED["ledger"] = True
    platform_capabilities._RUNTIME["ledger"] = "ready"
    yield
    platform_capabilities._reset_for_tests()
# 更新会话成功需要预置一条“userbot 观测到的”活跃会话。
SEED_SESSION: dict[str, Any] = {
    "account_id": 1,
    "chat_id": CHAT,
    "channel": "userbot",
    "rule_id": "r1",
    "data": {},
    "created_at": 1.0,
    "updated_at": 1.0,
    "expires_at": 9_999_999_999.0,
}
# 供 start_session（E1 需要 rule finder 命中）复用的最小规则。
_RULE: dict[str, Any] = {
    "id": "r1",
    "name": "parity",
    "module_key": "guess",
    "module_action": "play",
    "action": "module",
    "valid_seconds": 600,
}

# 完整性守卫①的“真源”：与 action_core.CANONICAL_ACTION_TYPES 对齐。
# 新增动作类型应先改 action_core，再补矩阵行。
from app.services.interaction.action_core import CANONICAL_ACTION_TYPES  # noqa: E402

Expect = tuple  # (status, error_code, actual_send_via)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.pushed: list[tuple[str, Any]] = []

    async def get(self, key: str):  # noqa: ANN201
        return self.data.get(key)

    async def set(self, key: str, value: str, **_kwargs: Any):  # noqa: ANN201
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                deleted += 1
        return deleted

    async def rpush(self, key: str, value: Any) -> int:
        self.pushed.append((key, value))
        return len(self.pushed)

    async def publish(self, _channel: str, _payload: Any) -> int:
        return 0


class _FakeClient:
    """Telethon-ish client：记录调用、返回带 id 的伪 message。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("send_message", chat_id, text, kwargs))
        return SimpleNamespace(id=101)

    async def edit_message(self, chat_id, message_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("edit_message", chat_id, message_id, text, kwargs))
        return SimpleNamespace(id=message_id)

    async def send_file(self, chat_id, file, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("send_file", chat_id, file, kwargs))
        return SimpleNamespace(id=102)

    async def delete_messages(self, chat_id, message_ids):  # noqa: ANN001
        self.calls.append(("delete_messages", chat_id, message_ids))
        return True

    async def pin_message(self, chat_id, message_id, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("pin_message", chat_id, message_id, kwargs))
        return True

    async def get_messages(self, chat_id, *, ids):  # noqa: ANN001
        self.calls.append(("get_messages", chat_id, ids))
        return SimpleNamespace(
            id=ids,
            empty=False,
            sender=SimpleNamespace(id=456, bot=True),
            sender_id=456,
            reply_markup=ReplyInlineMarkup(
                rows=[
                    KeyboardButtonRow(
                        buttons=[KeyboardButtonCallback(text="确认", data=b"server-owned")]
                    )
                ]
            ),
        )

    async def __call__(self, request):  # noqa: ANN001
        self.calls.append(("request", request))
        return SimpleNamespace(cache_time=0, message="ok", alert=False, url=None)

    def iter_messages(self, _chat_id, **_kwargs):  # noqa: ANN001, ANN003
        async def _gen():
            if False:  # pragma: no cover - empty async generator
                yield None

        return _gen()


class _DenyEngine:
    """限速引擎：一律拒绝（用于编码限流拒绝分叉漂移⑤）。"""

    async def acquire(self, _account_id, _action, *, peer_id=None):  # noqa: ANN001
        return SimpleNamespace(allowed=False, outcome="rate_limited", reason=None, wait_seconds=0.0)

    async def on_flood_wait(self, *_a, **_k):  # noqa: ANN002, ANN003
        return None

    async def on_peer_flood(self, *_a, **_k):  # noqa: ANN002, ANN003
        return None


def _mock_payout_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loader_mod.payout_compensation,
        "claim_payout_delivery",
        AsyncMock(
            return_value=loader_mod.payout_compensation.PayoutDeliveryClaim(
                status="acquired",
                row_id=1,
                claim_token="parity-token",
            )
        ),
    )
    monkeypatch.setattr(
        loader_mod.payout_compensation,
        "complete_payout_delivery",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        loader_mod.payout_compensation,
        "release_payout_delivery_claim",
        AsyncMock(),
    )


# ---------------------------------------------------------------------------
# matrix
# ---------------------------------------------------------------------------
@dataclass
class ParityCase:
    case_id: str
    actions: list[dict[str, Any]]
    assert_type: str
    expect_worker: Expect
    expect_bot: Expect
    #: 该行代表的 canonical 动作类型（成功/代表路填写；错误变体行留 None）。
    covers: str | None = None
    #: 说明该行编码了哪条已知漂移（None = parity 成立，无已知漂移）。
    drift: str | None = None
    engine_denies: bool = False
    seed_session: bool = False
    xfail_reason: str | None = None


def _row(*args: Any, **kwargs: Any) -> ParityCase:
    return ParityCase(*args, **kwargs)


PARITY_MATRIX: list[ParityCase] = [
    # ── 13 个语义动作类型 + 会话控制类型的“成功/代表路” ──────────────────
    _row(
        "send_message_userbot_ok",
        [{"type": "send_message", "send_via": "userbot_reply", "text": "hi", "chat_id": CHAT}],
        "send_message",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="send_message",
    ),
    _row(
        "send_rich_message_interaction_bot_ok",
        [
            {
                "type": "send_rich_message",
                "send_via": "interaction_bot",
                "rich_message": {"html": "<h1>状态</h1><table><tr><td>正常</td></tr></table>"},
                "chat_id": CHAT,
            }
        ],
        "send_rich_message",
        (TRACE_STATUS_OK, None, "interaction_bot"),
        (TRACE_STATUS_OK, None, "interaction_bot"),
        covers="send_rich_message",
    ),
    _row(
        "send_rich_message_userbot_ok",
        [
            {
                "type": "send_rich_message",
                "send_via": "userbot_reply",
                "rich_message": {"html": "<h1>状态</h1>"},
                "chat_id": CHAT,
            }
        ],
        "send_rich_message",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
    ),
    _row(
        "send_photo_userbot_ok",
        [{"type": "send_photo", "send_via": "userbot_reply", "photo_base64": B64, "chat_id": CHAT}],
        "send_photo",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="send_photo",
    ),
    _row(
        "send_file_userbot_ok",
        [{"type": "send_file", "send_via": "userbot_reply", "file_base64": B64, "chat_id": CHAT}],
        "send_file",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="send_file",
    ),
    _row(
        "edit_message_userbot_ok",
        [{"type": "edit_message", "send_via": "userbot_reply", "text": "e", "message_id": 7, "chat_id": CHAT}],
        "edit_message",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="edit_message",
    ),
    _row(
        "edit_caption_userbot_ok",
        [{"type": "edit_caption", "send_via": "userbot_reply", "caption": "c", "message_id": 7, "chat_id": CHAT}],
        "edit_caption",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="edit_caption",
    ),
    # delete/pin 走 userbot_reply：E2 通过 run_worker_action 落到 E3，与 E1 行为一致。
    _row(
        "delete_message_userbot_ok",
        [{"type": "delete_message", "send_via": "userbot_reply", "message_id": 7, "chat_id": CHAT}],
        "delete_message",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="delete_message",
    ),
    _row(
        "pin_message_userbot_ok",
        [{"type": "pin_message", "send_via": "userbot_reply", "message_id": 7, "chat_id": CHAT}],
        "pin_message",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="pin_message",
    ),
    _row(
        "click_callback_button_userbot_only",
        [
            {
                "type": "click_callback_button",
                "chat_id": CHAT,
                "message_id": 7,
                "row": 0,
                "column": 0,
                "expected_bot_id": 456,
                "expected_button_text": "确认",
            }
        ],
        "click_callback_button",
        (TRACE_STATUS_OK, None, "userbot_callback"),
        (TRACE_STATUS_SKIPPED, "unsupported_send_via", None),
        covers="click_callback_button",
        drift="有意设计：仅 UserBot 执行链路支持，Interaction Bot delivery 不代点第三方按钮",
    ),
    _row(
        "answer_callback_ok",
        [{"type": "answer_callback", "callback_query_id": "cbq", "text": "ok"}],
        "answer_callback",
        (TRACE_STATUS_OK, None, "interaction_bot"),
        (TRACE_STATUS_OK, None, "interaction_bot"),
        covers="answer_callback",
    ),
    _row(
        "answer_inline_query_ok",
        [{"type": "answer_inline_query", "inline_query_id": "iq", "results": []}],
        "answer_inline_query",
        (TRACE_STATUS_OK, None, "interaction_bot"),
        (TRACE_STATUS_OK, None, "interaction_bot"),
        covers="answer_inline_query",
    ),
    _row(
        "payout_ok",
        [{"type": "payout", "amount": 100, "text": "+100", "chat_id": CHAT}],
        "payout",
        (TRACE_STATUS_OK, None, "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        covers="payout",
    ),
    _row(
        "update_session_ok",
        [{"type": "update_session", "session_key": SESSION_KEY, "data": {"n": 2}}],
        "update_session",
        (TRACE_STATUS_OK, None, "interaction_session"),
        (TRACE_STATUS_OK, None, "interaction_session"),
        covers="update_session",
        seed_session=True,
    ),
    # start_session：E1 内联建会话（OK）；E2 delivery.apply 把 start_session 当控制类
    # 型 SKIPPED（真正落盘在 _apply_interaction_start_session_action，见独立用例）。
    # 这是**有意设计**（内联 vs 外层），非漂移。
    _row(
        "start_session_design_split",
        [{"type": "start_session", "entry_key": "play", "chat_id": CHAT, "data": {"b": 2}, "started_by_user_id": 5}],
        "start_session",
        (TRACE_STATUS_OK, None, "interaction_session"),
        (TRACE_STATUS_SKIPPED, "session_control_action", "interaction_session"),
        covers="start_session",
        drift="有意设计：start_session 内联(E1) vs 外层控制(E2.apply)",
    ),
    _row(
        "settlement_ok",
        [{"type": "settlement", "settlement": {"winner_user_id": 5}}],
        "settlement",
        (TRACE_STATUS_OK, None, "settlement"),
        (TRACE_STATUS_OK, None, "settlement"),
        covers="settlement",
    ),
    _row(
        "result_control",
        [{"type": "result"}],
        "result",
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        covers="result",
    ),
    _row(
        "end_session_control",
        [{"type": "end_session"}],
        "end_session",
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        covers="end_session",
    ),
    _row(
        "close_session_control",
        [{"type": "close_session"}],
        "close_session",
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        covers="close_session",
    ),
    _row(
        "no_session_control",
        [{"type": "no_session"}],
        "no_session",
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        (TRACE_STATUS_SKIPPED, "session_control_action", None),
        covers="no_session",
    ),
    # ── 错误变体：缺参 / 空文本 / 非法 base64 / deprecated / 未知 / 截断 ───
    _row(
        "send_message_empty_text",
        [{"type": "send_message", "send_via": "userbot_reply", "text": "", "chat_id": CHAT}],
        "send_message",
        (TRACE_STATUS_FAILED, "empty_message_text", None),
        (TRACE_STATUS_FAILED, "empty_message_text", None),
    ),
    _row(
        "edit_message_missing_message_id",
        [{"type": "edit_message", "send_via": "userbot_reply", "text": "e", "chat_id": CHAT}],
        "edit_message",
        (TRACE_STATUS_FAILED, "target_message_id_missing", None),
        (TRACE_STATUS_FAILED, "target_message_id_missing", None),
    ),
    _row(
        "send_photo_invalid_base64",
        [{"type": "send_photo", "send_via": "userbot_reply", "photo_base64": "@@not-base64@@", "chat_id": CHAT}],
        "send_photo",
        (TRACE_STATUS_FAILED, "media_payload_invalid", None),
        (TRACE_STATUS_FAILED, "media_payload_invalid", None),
    ),
    _row(
        "send_message_deprecated_channel",
        [{"type": "send_message", "channel": "notice", "text": "x", "chat_id": CHAT}],
        "send_message",
        (TRACE_STATUS_FAILED, "send_channel_deprecated", None),
        (TRACE_STATUS_FAILED, "send_channel_deprecated", None),
    ),
    _row(
        "unknown_action_type",
        [{"type": "frobnicate", "chat_id": CHAT}],
        "frobnicate",
        (TRACE_STATUS_SKIPPED, "unsupported_send_via", None),
        (TRACE_STATUS_SKIPPED, "unsupported_send_via", None),
        drift="⑨ 记录级一致，但 E1 置 failed=True 进 plugin_runtime_status、E2 仅日志（聚合不对称，本归一不可见）",
    ),
    # payout 金额错误码分叉（漂移④a）
    _row(
        "payout_zero_amount",
        [{"type": "payout", "amount": 0, "text": "+0", "chat_id": CHAT}],
        "payout",
        (TRACE_STATUS_FAILED, "invalid_payout_amount", "userbot_reply"),
        (TRACE_STATUS_FAILED, "action_failed", "userbot_reply"),
        drift="④ payout 金额错误码分叉（E1 invalid_payout_amount vs E2 action_failed）",
    ),
    # payout 空白文本行为分叉（漂移④b）：E1 直接 FAILED，E2 自动回退 "+{amount}" 成功
    _row(
        "payout_blank_text",
        [{"type": "payout", "amount": 50, "text": "   ", "chat_id": CHAT}],
        "payout",
        (TRACE_STATUS_FAILED, "empty_message_text", "userbot_reply"),
        (TRACE_STATUS_OK, None, "userbot_reply"),
        drift="④ payout 空白文本行为分叉（E1 FAILED vs E2 回退成功）",
    ),
    # update_session 校验严格度/错误码族分叉（漂移⑥/⑦）：缺 session_key
    _row(
        "update_session_missing_key",
        [{"type": "update_session", "data": {"n": 1}}],
        "update_session",
        (TRACE_STATUS_FAILED, "session_not_found", None),
        (TRACE_STATUS_FAILED, "interaction_session_error", "interaction_session"),
        drift="⑥/⑦ update_session 错误码族 + actual_send_via 分叉",
    ),
    # 限流拒绝：E1 SKIPPED rate_limited；E3 raise rate_limited → E2 FAILED rate_limited（波次一已对齐错误码）
    _row(
        "send_message_rate_limited",
        [{"type": "send_message", "send_via": "userbot_reply", "text": "hi", "chat_id": CHAT}],
        "send_message",
        (TRACE_STATUS_SKIPPED, "rate_limited", "userbot_reply"),
        (TRACE_STATUS_FAILED, "rate_limited", "userbot_reply"),
        engine_denies=True,
        drift="⑤ 限流拒绝状态分叉（E1 SKIPPED vs E2 FAILED；error_code 均为 rate_limited）",
    ),
    # 超 10 条截断：第 11 条被丢弃，两侧均记 action_limit_exceeded
    _row(
        "action_limit_truncation",
        [{"type": "result"} for _ in range(10)]
        + [{"type": "pin_message", "send_via": "userbot_reply", "message_id": 9, "chat_id": CHAT}],
        "pin_message",
        (TRACE_STATUS_FAILED, "action_limit_exceeded", None),
        (TRACE_STATUS_FAILED, "action_limit_exceeded", None),
    ),
]


# ---------------------------------------------------------------------------
# drivers
# ---------------------------------------------------------------------------
def _extract(rec: AsyncMock, action_type: str) -> Expect:
    """从 record_action mock 抽取指定 action_type 的终态记录归一三元组。"""
    matched = []
    for call in rec.await_args_list:
        args = call.args
        if len(args) >= 2 and isinstance(args[1], dict) and str(args[1].get("type") or "").strip() == action_type:
            matched.append(call)
    if not matched:
        return ("<no-record>", None, None)
    call = matched[-1]
    status = call.args[2] if len(call.args) >= 3 else call.kwargs.get("status", "pending")
    return (status, call.kwargs.get("error_code"), call.kwargs.get("actual_send_via"))


def _make_run_worker_action(client: _FakeClient, engine: Any, mem: _MemRedis):
    """in-process 复刻 RPC 落点 ``_handle_run_interaction_action_command`` 的
    (ok, error, result) 映射：调用 E3、把 raise 归一成 delivery 期望的三元组。"""

    async def run(incoming: Any, *, payload: dict[str, Any]):
        try:
            result_payload = await worker_runtime._run_interaction_userbot_action(  # noqa: SLF001
                client,
                payload,
                account_id=incoming.account_id,
                engine=engine,
                redis=mem,
            )
            return True, None, result_payload
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, platform_capabilities.LedgerActionsFailedClosed):
                error_code = platform_capabilities.LEDGER_ACTIONS_FAILED_CLOSED_ERROR_CODE
            elif isinstance(exc, PayoutLimitExceeded):
                error_code = "payout_limit_exceeded"
            else:
                error_code = worker_runtime._interaction_action_error_code(error)  # noqa: SLF001
            result_payload = worker_runtime._interaction_action_failure_result(  # noqa: SLF001
                payload, error=error, error_code=error_code
            )
            if isinstance(exc, platform_capabilities.LedgerActionsFailedClosed):
                result_payload["audit_status"] = exc.audit_status
                result_payload["deny_reasons"] = list(exc.reasons)
            return False, error, result_payload

    return run


async def _drive_worker(monkeypatch: pytest.MonkeyPatch, case: ParityCase) -> Expect:
    rec = AsyncMock()
    monkeypatch.setattr(loader_mod, "record_action", rec)
    monkeypatch.setattr(loader_mod, "_interaction_bot_token_for_account", AsyncMock(return_value="tok"))
    monkeypatch.setattr(loader_mod, "_find_interaction_rule_for_plugin_session", AsyncMock(return_value=dict(_RULE)))
    monkeypatch.setattr(payout_limit_mod, "check_and_consume", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(account_bot_service, "answer_callback", AsyncMock(return_value={}))
    monkeypatch.setattr(account_bot_service, "answer_inline_query", AsyncMock(return_value={}))
    monkeypatch.setattr(
        account_bot_service,
        "send_rich_message",
        AsyncMock(return_value={"message_id": 103, "chat_id": CHAT}),
    )
    monkeypatch.setattr(
        userbot_rich_message,
        "send_rich_message",
        AsyncMock(return_value={"message_id": 104, "chat_id": CHAT}),
    )
    _mock_payout_delivery(monkeypatch)

    mem = _MemRedis()
    if case.seed_session:
        mem.data[SESSION_KEY] = json.dumps(SEED_SESSION)
    state = loader_mod._AccountState(1)  # noqa: SLF001
    state.redis = mem
    state.client = _FakeClient()
    state.instances["guess"] = type(
        "_ParityActivePlugin",
        (loader_mod.Plugin,),
        {"key": "guess", "display_name": "parity active plugin"},
    )()
    # 与 E3 一致：非限流用例挂 AllowEngine，避免 loader 本地降级改写快照。
    state.engine = _DenyEngine() if case.engine_denies else _AllowEngine()

    await loader_mod._apply_userbot_event_bus_actions(  # noqa: SLF001
        state,
        None,
        SimpleNamespace(chat_id=CHAT),
        plugin_key="guess",
        entry_key="play",
        actions=copy.deepcopy(case.actions),
        redis=mem,
        session_key=None,
        session=None,
    )
    return _extract(rec, case.assert_type)


class _AllowEngine:
    """限速引擎：一律放行（parity 用例默认不测限流时使用，避免本地降级桶介入）。"""

    async def acquire(self, _account_id, _action, *, peer_id=None):  # noqa: ANN001
        return SimpleNamespace(allowed=True, outcome="ok", reason=None, wait_seconds=0.0)

    async def on_flood_wait(self, *_a, **_k):  # noqa: ANN002, ANN003
        return None

    async def on_peer_flood(self, *_a, **_k):  # noqa: ANN002, ANN003
        return None


async def _drive_bot(monkeypatch: pytest.MonkeyPatch, case: ParityCase) -> Expect:
    rec = AsyncMock()
    monkeypatch.setattr(delivery_mod, "record_action", rec)
    monkeypatch.setattr(account_bot_service, "answer_callback", AsyncMock(return_value={}))
    monkeypatch.setattr(account_bot_service, "answer_inline_query", AsyncMock(return_value={}))
    monkeypatch.setattr(
        account_bot_service,
        "send_rich_message",
        AsyncMock(return_value={"message_id": 103, "chat_id": CHAT}),
    )
    monkeypatch.setattr(
        userbot_rich_message,
        "send_rich_message",
        AsyncMock(return_value={"message_id": 104, "chat_id": CHAT}),
    )
    # E3 通过 from-import 绑定 _check_payout_limit，必须在 worker_runtime 命名空间打桩。
    monkeypatch.setattr(worker_runtime, "_check_payout_limit", AsyncMock(return_value=(True, None)))
    _mock_payout_delivery(monkeypatch)

    mem = _MemRedis()
    if case.seed_session:
        mem.data[SESSION_KEY] = json.dumps(SEED_SESSION)
    client = _FakeClient()
    # engine is None 会走本地降级 fail-closed（payout）或本地桶，污染非限流用例快照。
    engine = _DenyEngine() if case.engine_denies else _AllowEngine()

    incoming = account_bot_runtime.Incoming(
        account_id=1,
        token="123:token",
        update_id=1,
        user_id=5,
        chat_id=CHAT,
        message_id=10,
        text="",
    )
    executor = InteractionDeliveryExecutor(
        incoming=incoming,
        write_log=AsyncMock(),
        run_worker_action=_make_run_worker_action(client, engine, mem),
        log_context=account_bot_runtime._interaction_log_context,  # noqa: SLF001
        trace_context=account_bot_runtime._interaction_trace_context,  # noqa: SLF001
        get_redis_client=lambda: mem,
    )
    await executor.apply(copy.deepcopy(case.actions))
    return _extract(rec, case.assert_type)


# ---------------------------------------------------------------------------
# matrix test
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", PARITY_MATRIX, ids=[c.case_id for c in PARITY_MATRIX])
async def test_executor_parity_snapshot(monkeypatch: pytest.MonkeyPatch, case: ParityCase) -> None:
    if case.xfail_reason:
        pytest.xfail(case.xfail_reason)
    worker_actual = await _drive_worker(monkeypatch, case)
    bot_actual = await _drive_bot(monkeypatch, case)
    assert worker_actual == case.expect_worker, (
        f"[{case.case_id}] worker(E1) 快照漂移: got={worker_actual} expected={case.expect_worker}"
    )
    assert bot_actual == case.expect_bot, (
        f"[{case.case_id}] bot(E2+E3) 快照漂移: got={bot_actual} expected={case.expect_bot}"
    )


# ---------------------------------------------------------------------------
# completeness guard ①：canonical 动作 frozenset ↔ 矩阵代表性覆盖
# ---------------------------------------------------------------------------
def test_guard_canonical_action_coverage() -> None:
    """防“新增动作只加一侧”：canonical frozenset 必须与矩阵代表行一一对应。

    新增执行体动作类型时，必须同步 (a) 更新 CANONICAL_ACTION_TYPES 与 (b) 补一条
    代表行——而代表行强制填 expect_worker + expect_bot 两列，任何“只在一侧实现”的
    改动都会立刻在两列 diff 中暴露。
    """
    representatives = [c for c in PARITY_MATRIX if c.covers is not None]
    covered = [c.covers for c in representatives]
    # 每个 canonical 类型恰好一条代表行（无遗漏、无重复）。
    assert sorted(covered) == sorted(CANONICAL_ACTION_TYPES), (
        f"代表行覆盖与 canonical 集合不一致: 多={sorted(set(covered) - CANONICAL_ACTION_TYPES)} "
        f"缺={sorted(CANONICAL_ACTION_TYPES - set(covered))}"
    )
    assert len(covered) == len(set(covered)), f"存在重复代表行: {covered}"
    # 代表行的 assert_type 必须就是它声明覆盖的类型。
    for case in representatives:
        assert case.assert_type == case.covers, f"[{case.case_id}] assert_type 与 covers 不符"
    # 矩阵里出现的所有 action 类型不得越出 canonical ∪ {未知哨兵 frobnicate}。
    seen_types = {str(a.get("type") or "").strip() for c in PARITY_MATRIX for a in c.actions}
    assert seen_types <= (CANONICAL_ACTION_TYPES | {"frobnicate"}), (
        f"矩阵出现越界动作类型: {sorted(seen_types - CANONICAL_ACTION_TYPES - {'frobnicate'})}"
    )


# ---------------------------------------------------------------------------
# completeness guard ②：三份 failure_result 复制体形状一致（先锁形状后合并）
# ---------------------------------------------------------------------------
def test_guard_failure_result_shapes_consistent() -> None:
    """E1/E2/E3 三份 failure_result 构造器喂同一伪输入，键集合必须一致。

    这三份如今是各写一份的复制体（漂移⑧）。本守卫锁住它们的“形状”，为将来合并
    成单一实现兜底：任何一侧新增/删减字段都会立刻失败。
    """
    payload = {
        "type": "payout",
        "action_type": "payout",
        "chat_id": CHAT,
        "amount": 5,
        "reply_to_message_id": 7,
        "reply_to_user_id": 9,
        "reply_to_search_limit": 15,
    }
    expected_keys = {
        "chat_id",
        "amount",
        "reply_to_message_id",
        "reply_to_user_id",
        "reply_to_search_limit",
        "error",
        "error_code",
        "worker_offline",
        "reply_anchor_missing",
    }
    e1 = loader_mod._userbot_action_failure_result(  # noqa: SLF001
        payload, target_chat_id=CHAT, error_code="boom_code", error="boom"
    )
    e2 = delivery_mod._userbot_action_failure_result(  # noqa: SLF001
        payload, error="boom", error_code="boom_code", result=None
    )
    e3 = worker_runtime._interaction_action_failure_result(  # noqa: SLF001
        payload, error="boom", error_code="boom_code"
    )
    assert set(e1) == expected_keys, f"E1 failure_result 形状漂移: {sorted(set(e1) ^ expected_keys)}"
    assert set(e2) == expected_keys, f"E2 failure_result 形状漂移: {sorted(set(e2) ^ expected_keys)}"
    assert set(e3) == expected_keys, f"E3 failure_result 形状漂移: {sorted(set(e3) ^ expected_keys)}"
    assert set(e1) == set(e2) == set(e3)
    # error_code 派生的语义标志三侧一致。
    for detail in (e1, e2, e3):
        assert detail["worker_offline"] is False
        assert detail["reply_anchor_missing"] is False


# ---------------------------------------------------------------------------
# drift ①（会话数据合并）：start_session 落盘 data 的 E1(loader) vs bot 侧一致性
# ---------------------------------------------------------------------------
async def test_start_session_data_merge_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """漂移①快照：worker(E1 ``_apply_userbot_start_session_action``)与 bot 侧
    ``_apply_interaction_start_session_action`` 落盘的会话 ``data`` 都应 merge
    ``{**existing, **action.data}``（抢会话单落地后的目标语义，两列一致）。

    若并行“抢会话单”尚未落地导致 bot 侧丢 data，则给本用例加
    ``pytest.mark.xfail(reason="等待抢会话单落地", strict=False)``。
    （落地前的复核：当前分支 ``_apply_interaction_start_session_action`` 已 merge，
    故本用例应为绿，无需 xfail。）
    """
    rule = dict(_RULE)
    key = account_bot_runtime._interaction_session_key(1, rule, CHAT, 5)  # noqa: SLF001
    existing = {
        "account_id": 1,
        "chat_id": CHAT,
        "channel": "userbot",
        "rule_id": rule["id"],
        "data": {"a": 1},
        "created_at": 1.0,
        "expires_at": 9_999_999_999.0,
        "started_by_user_id": 5,
    }
    action = {"type": "start_session", "entry_key": "play", "chat_id": CHAT, "data": {"b": 2}, "started_by_user_id": 5}

    # worker 侧（E1）
    mem_w = _MemRedis()
    mem_w.data[key] = json.dumps(existing)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(loader_mod, "_find_interaction_rule_for_plugin_session", AsyncMock(return_value=dict(rule)))
    state = loader_mod._AccountState(1)  # noqa: SLF001
    state.redis = mem_w
    await loader_mod._apply_userbot_start_session_action(  # noqa: SLF001
        state, copy.deepcopy(action), plugin_key="guess", entry_key="play", redis=mem_w
    )
    data_worker = json.loads(mem_w.data[key])["data"]

    # bot 侧（_apply_interaction_start_session_action）
    mem_b = _MemRedis()
    mem_b.data[key] = json.dumps(existing)
    monkeypatch.setattr(account_bot_runtime, "record_action", AsyncMock())
    monkeypatch.setattr(account_bot_runtime, "get_redis", lambda: mem_b)
    incoming = account_bot_runtime.Incoming(
        account_id=1, token="t", update_id=1, user_id=5, chat_id=CHAT, message_id=10, text=""
    )
    await account_bot_runtime._apply_interaction_start_session_action(  # noqa: SLF001
        incoming, dict(rule), copy.deepcopy(action)
    )
    data_bot = json.loads(mem_b.data[key])["data"]

    assert data_worker == {"a": 1, "b": 2}, f"E1 start_session 未 merge data: {data_worker}"
    assert data_bot == {"a": 1, "b": 2}, f"bot start_session 未 merge data（抢会话单未落地？）: {data_bot}"
    assert data_worker == data_bot


# ---------------------------------------------------------------------------
# drift ②（reply_markup）：userbot_reply 下 E1/E3 都做文本按钮降级
# ---------------------------------------------------------------------------
async def test_userbot_reply_markup_button_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """漂移②回归：同一带 inline 按钮的 send_message 走 userbot_reply 时，
    E1/E3 都应把按钮渲染成“回复序号选择”文本降级。
    """
    markup = {"inline_keyboard": [[{"text": "选项A", "callback_data": "a"}]]}

    # worker(E1)
    monkeypatch.setattr(loader_mod, "record_action", AsyncMock())
    monkeypatch.setattr(payout_limit_mod, "check_and_consume", AsyncMock(return_value=(True, None)))
    state = loader_mod._AccountState(1)  # noqa: SLF001
    state.redis = _MemRedis()
    state.client = _FakeClient()
    state.engine = None
    await loader_mod._apply_userbot_send_message_action(  # noqa: SLF001
        state,
        SimpleNamespace(chat_id=CHAT),
        {"type": "send_message", "send_via": "userbot_reply", "text": "Q", "reply_markup": markup, "chat_id": CHAT},
        redis=state.redis,
    )
    worker_text = state.client.calls[-1][2]
    assert "选项A" in worker_text and "请回复序号选择" in worker_text, (
        f"E1 未做文本按钮降级: {worker_text!r}"
    )

    # E3
    e3_client = _FakeClient()
    await worker_runtime._run_interaction_userbot_action(  # noqa: SLF001
        e3_client,
        {"action_type": "send_message", "chat_id": CHAT, "text": "Q", "reply_markup": markup, "parse_mode": "plain"},
        account_id=1,
        engine=None,
        redis=_MemRedis(),
    )
    e3_text = e3_client.calls[-1][2]
    assert e3_text == worker_text
    assert "选项A" in e3_text and "请回复序号选择" in e3_text, f"E3 未做文本按钮降级: {e3_text!r}"
