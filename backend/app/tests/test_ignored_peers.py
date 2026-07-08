"""允许 peer 名单（沿用 ignored_peer 表）+ 最近活跃 peer LRU 的单元测试。

覆盖：
- ``_record_recent_peer`` 写入 + LRU 上限 + move_to_end 行为
- ``_dispatch`` 在允许名单非空且不命中时短路，不调插件
- ``_dispatch`` 在允许名单命中时正常派发到插件
- ``reload_ignored_peers`` 从 fake DB 重新拉名单
- ``get_recent_peers`` 反向输出（最新在前）
- ``IgnoredPeerCreate.normalized_kind`` 异常 kind 归一化为 private
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.db.models.ignored_peer import PEER_KINDS
from app.schemas.ignored_peer import IgnoredPeerCreate
from app.worker.plugins import loader as loader_mod
from app.worker.plugins.base import Plugin, PluginContext, register
from app.worker.plugins.loader import (
    RECENT_PEERS_LIMIT,
    _AccountState,
    _record_recent_peer,
    get_recent_peers,
    load_plugins_for_account,
    reload_ignored_peers,
)


# ─────────────────────────────────────────────────────
# 通用 fake：DB / Redis / event
# ─────────────────────────────────────────────────────
class _FakeRedis:
    """loader 仅用 rpush 写日志；publish 也不会真到 worker。"""

    def __init__(self) -> None:
        self.list_pushes: list[tuple[str, str]] = []

    async def rpush(self, key: str, val: str) -> int:
        self.list_pushes.append((key, val))
        return len(self.list_pushes)

    async def publish(self, *_a, **_kw) -> int:
        return 0

    async def get(self, *_a, **_kw):
        return None

    async def set(self, *_a, **_kw):
        return True

    async def script_load(self, *_a, **_kw):
        return "fake-sha"

    async def evalsha(self, *_a, **_kw):
        return [1, 0, 0]


@dataclass
class _FakeAcc:
    id: int = 1
    cold_start_until: Any = None


@dataclass
class _FakeIgnored:
    """模拟 ignored_peer 表的一行，仅含 select 用到的字段。"""

    peer_id: int


class _FakeDB:
    """超薄 fake DB；仅响应本测试需要的 select。"""

    def __init__(
        self,
        accounts: dict[int, _FakeAcc] | None = None,
        humanize: dict[int, Any] | None = None,
        afs: list[Any] | None = None,
        rules: list[Any] | None = None,
        ignored_rows: list[_FakeIgnored] | None = None,
    ) -> None:
        self.accounts = accounts or {1: _FakeAcc(id=1)}
        self.humanize = humanize or {1: None}
        self.afs = afs or []
        self.rules = rules or []
        self.ignored_rows = ignored_rows or []

    async def get(self, model, pk):
        name = getattr(model, "__tablename__", None) or getattr(
            getattr(model, "__table__", None), "name", None
        )
        if name == "account":
            return self.accounts.get(pk)
        if name == "humanize_config":
            return self.humanize.get(pk)
        return None

    async def execute(self, stmt):
        text = str(stmt).lower()
        if "ignored_peer" in text:
            # SELECT peer_id FROM ignored_peer where account_id=...
            return _FakeResult([(r.peer_id,) for r in self.ignored_rows])
        if "account_feature" in text:
            return _FakeResult([(af,) for af in self.afs])
        if "rule" in text:
            return _FakeResult([(r,) for r in self.rules])
        if text.startswith("update"):
            return _FakeResult([])
        return _FakeResult([])

    async def commit(self):
        return None


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None

    def scalars(self):
        return _FakeScalars([r[0] for r in self._rows])


class _FakeScalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self):
        return list(self._items)


@asynccontextmanager
async def _fake_session_factory(db: _FakeDB):
    yield db


@dataclass
class _FakeChat:
    """模拟 telethon Chat 对象。"""

    title: str | None = None
    username: str | None = None
    first_name: str | None = None


@dataclass
class _FakeEvent:
    """模拟 telethon NewMessage event；只暴露 _dispatch / _record_recent_peer 用到的属性。"""

    chat_id: int
    is_private: bool = False
    is_group: bool = False
    is_channel: bool = False
    raw_text: str = ""
    chat: _FakeChat = field(default_factory=_FakeChat)

    async def get_chat(self):
        return self.chat


# ─────────────────────────────────────────────────────
# 用例 1：IgnoredPeerCreate.normalized_kind 归一化
# ─────────────────────────────────────────────────────
def test_create_normalizes_unknown_kind() -> None:
    """异常的 peer_kind 应当回退为 private。"""
    p = IgnoredPeerCreate(peer_id=42, peer_kind="weird-kind")
    assert p.normalized_kind() == "private"


def test_create_keeps_known_kind() -> None:
    """白名单内的 kind 原样保留。"""
    for k in PEER_KINDS:
        p = IgnoredPeerCreate(peer_id=42, peer_kind=k)
        assert p.normalized_kind() == k


# ─────────────────────────────────────────────────────
# 用例 2：_record_recent_peer 维护 LRU 上限 + 顺序
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_record_recent_peer_lru_cap_and_order() -> None:
    """连续写超出上限的 peer，应当只保留最近的 RECENT_PEERS_LIMIT 个，且顺序 = 最新在末尾。"""
    state = _AccountState(account_id=1)
    # 写入 RECENT_PEERS_LIMIT + 5 个不同 peer
    n = RECENT_PEERS_LIMIT + 5
    for i in range(n):
        ev = _FakeEvent(chat_id=1000 + i, is_private=True, chat=_FakeChat(first_name=f"u{i}"))
        await _record_recent_peer(state, ev)
    assert len(state.recent_peers) == RECENT_PEERS_LIMIT
    # 末尾应当是最后写入的那个
    last_pid, _ = next(reversed(state.recent_peers.items()))
    assert last_pid == 1000 + (n - 1)
    # 头部应当是 5 之后还能存活的那个（前 5 个被淘汰）
    first_pid, _ = next(iter(state.recent_peers.items()))
    assert first_pid == 1000 + 5


@pytest.mark.asyncio
async def test_record_recent_peer_move_to_end() -> None:
    """同一 peer 再次活跃，应该被挪到末尾（LRU "最近使用"语义）。"""
    state = _AccountState(account_id=1)
    for pid in (10, 20, 30):
        await _record_recent_peer(state, _FakeEvent(chat_id=pid, is_private=True))
    # 让 10 重新活跃
    await _record_recent_peer(state, _FakeEvent(chat_id=10, is_private=True))
    keys = list(state.recent_peers.keys())
    # 期望顺序：20, 30, 10（10 被挪到末尾）
    assert keys == [20, 30, 10]


@pytest.mark.asyncio
async def test_record_recent_peer_classifies_kinds() -> None:
    """不同 event 类型应被归类为 private/group/supergroup/channel。"""
    state = _AccountState(account_id=1)
    cases = [
        (_FakeEvent(chat_id=100, is_private=True), "private"),
        (_FakeEvent(chat_id=-200, is_group=True), "group"),
        (_FakeEvent(chat_id=-1001, is_group=True, is_channel=True), "supergroup"),
        (_FakeEvent(chat_id=-1002, is_channel=True), "channel"),
    ]
    for ev, expected_kind in cases:
        await _record_recent_peer(state, ev)
        assert state.recent_peers[ev.chat_id]["peer_kind"] == expected_kind


# ─────────────────────────────────────────────────────
# 用例 3：get_recent_peers 反向输出（最新在前）
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_recent_peers_returns_newest_first(monkeypatch) -> None:
    """get_recent_peers 应当返回最新写入的 peer 在数组前面。"""
    state = _AccountState(account_id=42)
    # 把 state 注入到全局 _STATES，模拟 worker 启动后状态
    monkeypatch.setitem(loader_mod._STATES, 42, state)

    for pid in (100, 200, 300):
        await _record_recent_peer(state, _FakeEvent(chat_id=pid, is_private=True))

    items = get_recent_peers(42)
    # 期望：300（最新）、200、100（最旧）
    assert [it["peer_id"] for it in items] == [300, 200, 100]
    # ts 必须是数字
    for it in items:
        assert isinstance(it["ts"], float)
        assert it["ts"] > 0


def test_get_recent_peers_unknown_account_returns_empty() -> None:
    """没有 worker 运行态的账号应返回空列表，不抛异常。"""
    items = get_recent_peers(999_999_999)
    assert items == []


# ─────────────────────────────────────────────────────
# 用例 4：reload_ignored_peers 从 DB 重拉名单
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reload_ignored_peers_pulls_new_set(monkeypatch) -> None:
    """从 DB 拉到 [10, 20, 30]，调 reload_ignored_peers 后 state.ignored_peers 应当是这三个。"""
    state = _AccountState(account_id=7)
    state.redis = _FakeRedis()
    monkeypatch.setitem(loader_mod._STATES, 7, state)

    fake_db = _FakeDB(ignored_rows=[_FakeIgnored(10), _FakeIgnored(20), _FakeIgnored(30)])
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    await reload_ignored_peers(7)
    assert state.ignored_peers == {10, 20, 30}


@pytest.mark.asyncio
async def test_reload_ignored_peers_can_log_debug_for_periodic_reconcile(monkeypatch) -> None:
    """周期性名单收敛可降为 debug，避免默认运行事件页重复刷成功日志。"""
    state = _AccountState(account_id=7)
    state.redis = _FakeRedis()
    monkeypatch.setitem(loader_mod._STATES, 7, state)

    fake_db = _FakeDB(ignored_rows=[_FakeIgnored(10), _FakeIgnored(20)])
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))

    await reload_ignored_peers(7, log_level="debug")

    payload = json.loads(state.redis.list_pushes[-1][1])
    assert payload["level"] == "debug"
    assert payload["message"] == "允许群组名单已热更新（共 2 个 peer）"


@pytest.mark.asyncio
async def test_reload_ignored_peers_can_stay_silent_for_periodic_reconcile(monkeypatch) -> None:
    """周期性名单兜底成功时可静默，避免运行事件页重复刷成功日志。"""
    state = _AccountState(account_id=7)
    state.redis = _FakeRedis()
    monkeypatch.setitem(loader_mod._STATES, 7, state)

    fake_db = _FakeDB(ignored_rows=[_FakeIgnored(10), _FakeIgnored(20)])
    monkeypatch.setattr(loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db))

    await reload_ignored_peers(7, log_level=None)

    assert state.ignored_peers == {10, 20}
    assert state.redis.list_pushes == []


@pytest.mark.asyncio
async def test_reload_ignored_peers_silent_when_no_state(monkeypatch) -> None:
    """worker 没起来时调 reload_ignored_peers 应静默——绝不能抛。"""
    monkeypatch.setattr(loader_mod, "_STATES", {})
    # 不应抛异常
    await reload_ignored_peers(account_id=12345)


# ─────────────────────────────────────────────────────
# 用例 5：_dispatch 在允许名单未命中时短路，不调用插件
# ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dispatch_skips_peer_not_in_allowed_list(monkeypatch) -> None:
    """允许名单非空时，不在名单里的 chat_id 进来应被丢弃。"""

    @register
    class _SpyPlugin(Plugin):
        key = "_test_ignored_spy_1"
        display_name = "测试占位"
        owner_only = False
        on_message_calls = 0

        async def on_startup(self, ctx: PluginContext) -> None:
            return None

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            type(self).on_message_calls += 1

    @dataclass
    class _FakeAF:
        account_id: int
        feature_key: str
        enabled: bool = True
        config: dict | None = None
        state: str = "disabled"
        last_error: str | None = None

    fake_db = _FakeDB(
        ignored_rows=[_FakeIgnored(-1001234)],
        afs=[_FakeAF(account_id=1, feature_key="_test_ignored_spy_1")],
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    # 捕获装饰器装上的 _dispatch 函数
    captured: dict[str, Any] = {}

    def _on(_filter):
        def _wrap(fn):
            captured["dispatch"] = fn
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()
    redis = _FakeRedis()

    await load_plugins_for_account(client, account_id=1, paused=paused, redis=redis)
    state = loader_mod._STATES[1]
    # 允许名单应已加载到 set
    assert -1001234 in state.ignored_peers

    dispatch = captured["dispatch"]
    # 未命中允许名单：插件不应被调用
    _SpyPlugin.on_message_calls = 0
    await dispatch(_FakeEvent(chat_id=-1009999, is_group=True))
    assert _SpyPlugin.on_message_calls == 0
    # 同时 recent_peers 仍然应该记录到了（早退发生在记录之后）
    assert -1009999 in state.recent_peers


@pytest.mark.asyncio
async def test_dispatch_passes_through_when_in_allowed_list(monkeypatch) -> None:
    """在允许名单内的 chat_id 应当正常派发到插件。"""

    @register
    class _SpyPlugin(Plugin):
        key = "_test_ignored_spy_2"
        display_name = "测试占位"
        owner_only = False
        on_message_calls = 0

        async def on_startup(self, ctx: PluginContext) -> None:
            return None

        async def on_message(self, ctx: PluginContext, event: Any) -> None:
            type(self).on_message_calls += 1

    @dataclass
    class _FakeAF:
        account_id: int
        feature_key: str
        enabled: bool = True
        config: dict | None = None
        state: str = "disabled"
        last_error: str | None = None

    fake_db = _FakeDB(
        ignored_rows=[_FakeIgnored(-100777)],   # 允许名单里只有这个
        afs=[_FakeAF(account_id=2, feature_key="_test_ignored_spy_2")],
    )
    monkeypatch.setattr(
        loader_mod, "AsyncSessionLocal", lambda: _fake_session_factory(fake_db)
    )

    captured: dict[str, Any] = {}

    def _on(_filter):
        def _wrap(fn):
            captured["dispatch"] = fn
            return fn

        return _wrap

    client = MagicMock()
    client.on = _on
    paused = asyncio.Event()
    paused.set()

    await load_plugins_for_account(client, account_id=2, paused=paused, redis=_FakeRedis())

    dispatch = captured["dispatch"]
    _SpyPlugin.on_message_calls = 0
    # 在允许名单里的 peer
    await dispatch(_FakeEvent(chat_id=-100777, is_group=True))
    assert _SpyPlugin.on_message_calls == 1
