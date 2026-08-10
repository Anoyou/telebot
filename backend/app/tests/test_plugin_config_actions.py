from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db.models.log import PluginConfigActionJob, RuntimeLog
from app.schemas.feature import FeatureInfo
from app.services import plugin_config_action_jobs, plugin_config_actions
from app.services.plugin_config_action_jobs import create_plugin_config_action_job
from app.services.plugin_config_actions import declared_config_actions, run_plugin_config_action
from app.worker.plugins.base import Plugin, PluginContext
from app.worker.plugins.http_facade import PluginHTTP, PluginHTTPPolicyError


class DemoConfigActionPlugin(Plugin):
    key = "demo_action"
    display_name = "Demo Action"

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict,
    ) -> dict:
        assert action_key == "make_item"
        assert ctx.config["count"] == 3
        assert ctx.config["api_token"] == "real-token"
        return {
            "message": "已生成",
            "config_patch": {
                "items": [
                    {
                        "enabled": True,
                        "name": payload["input"]["name"],
                        "count": ctx.config["count"],
                    }
                ]
            },
        }


class LoggingConfigActionPlugin(Plugin):
    key = "logging_action"
    display_name = "Logging Action"

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict,
    ) -> dict:
        assert action_key == "make_item"
        if ctx.log:
            await ctx.log("info", "动作进度", step="demo")
        return {"message": "已完成", "config_patch": {"done": True}}


class OldInstalledConfigActionPlugin(Plugin):
    key = "stale_action"
    display_name = "Stale Action"
    _source = "installed"
    _loaded_at = 1.0

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict,
    ) -> dict:
        return {"message": "旧代码"}


class NetworkConfigActionPlugin(Plugin):
    key = "network_action"
    display_name = "Network Action"
    invoked = False

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict,
    ) -> dict:
        del ctx, action_key, payload
        type(self).invoked = True
        return {"message": "不应执行"}


class HTTPConfigActionPlugin(Plugin):
    key = "http_action"
    display_name = "HTTP Action"

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict,
    ) -> dict:
        del action_key, payload
        response = await ctx.http.get("https://api.example.com/v1")
        return {"message": f"HTTP {response.status_code}"}


class FreshInstalledConfigActionPlugin(Plugin):
    key = "stale_action"
    display_name = "Stale Action"
    _source = "installed"

    async def on_config_action(
        self,
        ctx: PluginContext,
        action_key: str,
        payload: dict,
    ) -> dict:
        return {"message": f"新代码：{payload['input']['name']}"}


class FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0
        self.flushed = False
        self.refreshed = []

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.commits += 1

    async def refresh(self, value):
        self.refreshed.append(value)

    async def get(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))


def test_declared_config_actions_reads_schema_metadata() -> None:
    feature = SimpleNamespace(
        manifest={
            "config_schema": {
                "x-config-actions": [
                    {"key": "make_item", "title": "生成"},
                    {"title": "缺少 key"},
                ]
            }
        }
    )

    actions = declared_config_actions(feature)

    assert actions == [{"key": "make_item", "title": "生成"}]


def test_declared_config_actions_reads_installed_manifest_metadata() -> None:
    feature = SimpleNamespace(
        manifest={
            "config_schema": {"type": "object"},
        }
    )
    installed = SimpleNamespace(
        manifest_json={
            "config_actions": [
                {"key": "generate_knowledge_base", "title": "获取并整理为题库"},
                {"title": "缺少 key"},
            ]
        }
    )

    actions = declared_config_actions(feature, installed_plugin=installed)

    assert actions == [
        {"key": "generate_knowledge_base", "title": "获取并整理为题库"}
    ]


def test_build_ai_facade_registers_usage_callback_in_web_process(monkeypatch) -> None:
    from app.services import llm_usage_service
    from app.worker.plugins.ai_facade import PluginAI

    registrations = []
    monkeypatch.setattr(
        llm_usage_service,
        "ensure_llm_usage_callback_registered",
        lambda: registrations.append("registered"),
    )

    facade = plugin_config_actions._build_ai_facade(
        7,
        "ai_redpacket",
        {"permissions": ["ai_text"]},
    )

    assert isinstance(facade, PluginAI)
    assert registrations == ["registered"]


def test_feature_info_reads_installed_manifest_config_actions() -> None:
    feature = SimpleNamespace(
        key="quick_qa",
        display_name="快问快答",
        is_builtin=False,
        version="1.2.0",
        manifest={
            "config_schema": {"type": "object"},
        },
    )
    installed = SimpleNamespace(
        source="repo",
        source_url="https://github.com/Anoyou/telebot-plugins/tree/0.33.x",
        source_label="Plugin Repo",
        signature_ok=None,
        manifest_json={
            "config_actions": [
                {
                    "key": "generate_knowledge_base",
                    "title": "获取并整理为题库",
                }
            ]
        },
        lint_warnings=[],
    )

    info = FeatureInfo.from_feature(feature, installed_plugin=installed)

    assert info.config_actions == [
        {
            "key": "generate_knowledge_base",
            "title": "获取并整理为题库",
        }
    ]


@pytest.mark.asyncio
async def test_run_plugin_config_action_merges_form_config_and_returns_patch(monkeypatch) -> None:
    feature = SimpleNamespace(
        key="demo_action",
        manifest={
            "permissions": [],
            "config_actions": [{"key": "make_item", "title": "生成"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=None)
    installed = SimpleNamespace(manifest_json={})
    monkeypatch.setattr(plugin_config_actions, "get_plugin", lambda key: DemoConfigActionPlugin)

    result = await run_plugin_config_action(
        FakeDB(),
        account=account,
        feature=feature,
        action_key="make_item",
        effective_config={"count": 1, "api_token": "real-token"},
        current_config={"count": 3, "api_token": "••••••••••••••••"},
        action_input={"name": "第一组"},
        installed_plugin=installed,
    )

    assert result["message"] == "已生成"
    assert result["config_patch"]["items"] == [
        {"enabled": True, "name": "第一组", "count": 3}
    ]


@pytest.mark.asyncio
async def test_config_action_without_http_does_not_query_account_proxy(monkeypatch) -> None:
    class _DB(FakeDB):
        async def get(self, model, *_args, **_kwargs):  # noqa: ANN001, ANN202
            if getattr(model, "__name__", "") == "Proxy":
                raise AssertionError("纯本地配置动作不应查询账号代理")
            return None

    feature = SimpleNamespace(
        key="network_action",
        manifest={
            "permissions": [],
            "config_actions": [{"key": "probe", "title": "探测"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=999)
    NetworkConfigActionPlugin.invoked = False
    monkeypatch.setattr(
        plugin_config_actions,
        "get_plugin",
        lambda _key: NetworkConfigActionPlugin,
    )

    result = await run_plugin_config_action(
        _DB(),
        account=account,
        feature=feature,
        action_key="probe",
        effective_config={},
        installed_plugin=SimpleNamespace(manifest_json={}),
    )

    assert result["message"] == "不应执行"
    assert NetworkConfigActionPlugin.invoked is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proxy", "error_pattern"),
    [
        (
            SimpleNamespace(
                id=9,
                type="mtproxy",
                host="proxy.example",
                port=443,
                username=None,
                password_enc=None,
            ),
            "不能用于插件 HTTP",
        ),
        (None, "不存在"),
        (
            SimpleNamespace(
                id=9,
                type="socks5",
                host="proxy.example",
                port=1080,
                username="user",
                password_enc="broken",
            ),
            "凭据无法解密",
        ),
    ],
)
async def test_config_action_account_proxy_error_fails_before_dns_or_transport(
    monkeypatch,
    proxy,
    error_pattern,
) -> None:
    class _DB(FakeDB):
        async def get(self, model, *_args, **_kwargs):  # noqa: ANN001, ANN202
            return proxy if getattr(model, "__name__", "") == "Proxy" else None

    resolved = 0
    requested = 0

    async def _resolver(_host: str, _port: int) -> list[str]:
        nonlocal resolved
        resolved += 1
        return ["93.184.216.34"]

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested += 1
        return httpx.Response(200, content=b"unexpected")

    original_from_context = PluginHTTP.from_context

    def _from_context(ctx, *, allowed_hosts, manifest_http=None):  # noqa: ANN001, ANN202
        return original_from_context(
            ctx,
            allowed_hosts=allowed_hosts,
            manifest_http=manifest_http,
            resolver=_resolver,
            transport=httpx.MockTransport(_handler),
        )

    feature = SimpleNamespace(
        key="http_action",
        manifest={
            "permissions": ["external_http"],
            "allowed_hosts": ["api.example.com"],
            "config_actions": [{"key": "probe", "title": "探测"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=9)
    monkeypatch.setattr(
        plugin_config_actions,
        "get_plugin",
        lambda _key: HTTPConfigActionPlugin,
    )
    monkeypatch.setattr(plugin_config_actions.PluginHTTP, "from_context", _from_context)
    if proxy is not None and proxy.password_enc:
        monkeypatch.setattr(
            plugin_config_actions,
            "decrypt_str",
            lambda _value: (_ for _ in ()).throw(ValueError("bad ciphertext")),
        )

    with pytest.raises(PluginHTTPPolicyError, match=error_pattern):
        await run_plugin_config_action(
            _DB(),
            account=account,
            feature=feature,
            action_key="probe",
            effective_config={},
            installed_plugin=SimpleNamespace(manifest_json={}),
        )

    assert resolved == 0
    assert requested == 0


@pytest.mark.asyncio
async def test_config_action_without_http_runs_with_socks4_account_proxy(monkeypatch) -> None:
    proxy = SimpleNamespace(
        id=10,
        type="socks4",
        host="proxy.example",
        port=1080,
        username=None,
        password_enc=None,
    )

    class _DB(FakeDB):
        async def get(self, model, *_args, **_kwargs):  # noqa: ANN001, ANN202
            return proxy if getattr(model, "__name__", "") == "Proxy" else None

    feature = SimpleNamespace(
        key="network_action",
        manifest={
            "permissions": [],
            "config_actions": [{"key": "probe", "title": "探测"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=10)
    NetworkConfigActionPlugin.invoked = False
    monkeypatch.setattr(
        plugin_config_actions,
        "get_plugin",
        lambda _key: NetworkConfigActionPlugin,
    )

    result = await run_plugin_config_action(
        _DB(),
        account=account,
        feature=feature,
        action_key="probe",
        effective_config={},
        installed_plugin=SimpleNamespace(manifest_json={}),
    )

    assert result["message"] == "不应执行"
    assert NetworkConfigActionPlugin.invoked is True


@pytest.mark.asyncio
async def test_config_action_http_with_socks4_fails_before_dns_or_transport(monkeypatch) -> None:
    proxy = SimpleNamespace(
        id=10,
        type="socks4",
        host="proxy.example",
        port=1080,
        username=None,
        password_enc=None,
    )

    class _DB(FakeDB):
        async def get(self, model, *_args, **_kwargs):  # noqa: ANN001, ANN202
            return proxy if getattr(model, "__name__", "") == "Proxy" else None

    feature = SimpleNamespace(
        key="http_action",
        manifest={
            "permissions": ["external_http"],
            "allowed_hosts": ["api.example.com"],
            "config_actions": [{"key": "probe", "title": "探测"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=10)
    monkeypatch.setattr(
        plugin_config_actions,
        "get_plugin",
        lambda _key: HTTPConfigActionPlugin,
    )

    with pytest.raises(PluginHTTPPolicyError, match="拒绝回落直连"):
        await run_plugin_config_action(
            _DB(),
            account=account,
            feature=feature,
            action_key="probe",
            effective_config={},
            installed_plugin=SimpleNamespace(manifest_json={}),
        )


@pytest.mark.asyncio
async def test_config_action_manifest_direct_http_can_ignore_legacy_proxy_limitation(monkeypatch) -> None:
    proxy = SimpleNamespace(
        id=10,
        type="mtproxy",
        host="proxy.example",
        port=443,
        username=None,
        password_enc=None,
    )

    class _DB(FakeDB):
        async def get(self, model, *_args, **_kwargs):  # noqa: ANN001, ANN202
            return proxy if getattr(model, "__name__", "") == "Proxy" else None

    requested = 0

    async def _resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested += 1
        return httpx.Response(200, content=b"ok")

    original_from_context = PluginHTTP.from_context

    def _from_context(ctx, *, allowed_hosts, manifest_http=None):  # noqa: ANN001, ANN202
        return original_from_context(
            ctx,
            allowed_hosts=allowed_hosts,
            manifest_http=manifest_http,
            resolver=_resolver,
            transport=httpx.MockTransport(_handler),
        )

    feature = SimpleNamespace(
        key="http_action",
        manifest={
            "permissions": ["external_http"],
            "allowed_hosts": ["api.example.com"],
            "http": {"allow_direct": True},
            "config_actions": [{"key": "probe", "title": "探测"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=10)
    monkeypatch.setattr(
        plugin_config_actions,
        "get_plugin",
        lambda _key: HTTPConfigActionPlugin,
    )
    monkeypatch.setattr(plugin_config_actions.PluginHTTP, "from_context", _from_context)

    result = await run_plugin_config_action(
        _DB(),
        account=account,
        feature=feature,
        action_key="probe",
        effective_config={"http": {"network_mode": "direct"}},
        installed_plugin=SimpleNamespace(manifest_json={}),
    )

    assert result["message"] == "HTTP 200"
    assert requested == 1


@pytest.mark.asyncio
async def test_run_plugin_config_action_accepts_installed_manifest_action(monkeypatch) -> None:
    feature = SimpleNamespace(
        key="demo_action",
        manifest={
            "permissions": [],
            "config_schema": {"type": "object"},
        },
    )
    installed = SimpleNamespace(
        manifest_json={
            "config_actions": [{"key": "make_item", "title": "生成"}],
        }
    )
    account = SimpleNamespace(id=7, proxy_id=None)
    monkeypatch.setattr(plugin_config_actions, "get_plugin", lambda key: DemoConfigActionPlugin)

    result = await run_plugin_config_action(
        FakeDB(),
        account=account,
        feature=feature,
        action_key="make_item",
        effective_config={"count": 1, "api_token": "real-token"},
        current_config={"count": 3},
        action_input={"name": "第二组"},
        installed_plugin=installed,
    )

    assert result["message"] == "已生成"
    assert result["config_patch"]["items"] == [
        {"enabled": True, "name": "第二组", "count": 3}
    ]


@pytest.mark.asyncio
async def test_run_plugin_config_action_injects_progress_log(monkeypatch) -> None:
    feature = SimpleNamespace(
        key="logging_action",
        manifest={
            "permissions": [],
            "config_actions": [{"key": "make_item", "title": "生成"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=None)
    logs: list[tuple[str, str, dict]] = []

    async def _log(level: str, message: str, **detail):
        logs.append((level, message, detail))

    monkeypatch.setattr(plugin_config_actions, "get_plugin", lambda key: LoggingConfigActionPlugin)

    result = await run_plugin_config_action(
        FakeDB(),
        account=account,
        feature=feature,
        action_key="make_item",
        effective_config={},
        current_config={},
        action_input={},
        installed_plugin=SimpleNamespace(manifest_json={}),
        log=_log,
    )

    assert result["config_patch"] == {"done": True}
    assert logs == [("info", "动作进度", {"step": "demo"})]


@pytest.mark.asyncio
async def test_run_plugin_config_action_reloads_stale_installed_plugin(monkeypatch) -> None:
    feature = SimpleNamespace(
        key="stale_action",
        manifest={
            "permissions": [],
            "config_actions": [{"key": "make_item", "title": "生成"}],
        },
    )
    account = SimpleNamespace(id=7, proxy_id=None)
    installed = SimpleNamespace(
        manifest_json={},
        version="0.1.5",
        updated_at=datetime.fromtimestamp(100, UTC),
    )
    cleared: list[str] = []
    loaded: list[str] = []

    def _get_plugin(key: str):
        assert key == "stale_action"
        return FreshInstalledConfigActionPlugin if cleared else OldInstalledConfigActionPlugin

    async def _authorize(*_args, **_kwargs):
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(plugin_config_actions, "get_plugin", _get_plugin)
    from app.worker.plugins import loader as plugin_loader

    monkeypatch.setattr(plugin_loader, "_builtin_plugin_path", lambda key: None)
    monkeypatch.setattr(plugin_loader, "_installed_plugin_exists", lambda key: True)
    monkeypatch.setattr(plugin_loader, "_authorize_installed_plugin", _authorize)
    monkeypatch.setattr(
        plugin_loader,
        "_clear_installed_module_cache",
        lambda key: cleared.append(key),
    )
    monkeypatch.setattr(
        plugin_loader,
        "_load_installed_plugin",
        lambda key: loaded.append(key) or {key: FreshInstalledConfigActionPlugin},
    )

    result = await run_plugin_config_action(
        FakeDB(),
        account=account,
        feature=feature,
        action_key="make_item",
        effective_config={},
        current_config={},
        action_input={"name": "第三组"},
        installed_plugin=installed,
    )

    assert result["message"] == "新代码：第三组"
    assert cleared == ["stale_action"]
    assert loaded == ["stale_action"]


@pytest.mark.asyncio
async def test_create_plugin_config_action_job_writes_runtime_log_and_starts_task(monkeypatch) -> None:
    db = FakeDB()
    feature = SimpleNamespace(
        key="demo_action",
        manifest={"config_actions": [{"key": "make_item", "title": "生成"}]},
    )
    account = SimpleNamespace(id=7)
    scheduled = []

    def _create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(plugin_config_action_jobs.asyncio, "create_task", _create_task)

    job = await create_plugin_config_action_job(
        db,
        account=account,
        feature=feature,
        action_key="make_item",
        effective_config={"count": 1},
        current_config={"count": 2},
        action_input={"name": "题库"},
        installed_plugin=SimpleNamespace(manifest_json={}),
    )

    assert isinstance(job, PluginConfigActionJob)
    assert job.status == "queued"
    assert job.account_id == 7
    assert job.plugin_key == "demo_action"
    assert db.flushed is True
    assert db.commits == 1
    assert scheduled
    runtime_logs = [item for item in db.added if isinstance(item, RuntimeLog)]
    assert len(runtime_logs) == 1
    assert runtime_logs[0].message == "配置动作已排队"
    assert runtime_logs[0].detail["config_action_job_id"] == job.job_id


@pytest.mark.asyncio
async def test_create_plugin_config_action_job_rejects_duplicate_running_action() -> None:
    existing = SimpleNamespace(job_id="pcaj_existing")

    class _DB(FakeDB):
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: existing))

    feature = SimpleNamespace(
        key="ai_redpacket",
        manifest={"config_actions": [{"key": "generate_question_bank", "title": "生成"}]},
    )
    with pytest.raises(plugin_config_actions.PluginConfigActionUnavailable, match="请先中断或终止"):
        await create_plugin_config_action_job(
            _DB(),
            account=SimpleNamespace(id=7),
            feature=feature,
            action_key="generate_question_bank",
            effective_config={},
        )


@pytest.mark.asyncio
async def test_create_plugin_config_action_job_serializes_same_action_key(monkeypatch) -> None:
    running = 0
    maximum_running = 0

    async def _create_locked(*_args, **_kwargs):
        nonlocal running, maximum_running
        running += 1
        maximum_running = max(maximum_running, running)
        await asyncio.sleep(0)
        running -= 1
        return SimpleNamespace(job_id=f"pcaj_{maximum_running}")

    monkeypatch.setattr(
        plugin_config_action_jobs,
        "_create_plugin_config_action_job_locked",
        _create_locked,
    )
    feature = SimpleNamespace(
        key="ai_redpacket",
        manifest={"config_actions": [{"key": "generate_question_bank", "title": "生成"}]},
    )
    kwargs = {
        "account": SimpleNamespace(id=77),
        "feature": feature,
        "action_key": "generate_question_bank",
        "effective_config": {},
    }
    try:
        await asyncio.gather(
            create_plugin_config_action_job(FakeDB(), **kwargs),
            create_plugin_config_action_job(FakeDB(), **kwargs),
        )
    finally:
        plugin_config_action_jobs._CREATE_LOCKS_BY_ACTION.pop(
            (77, "ai_redpacket", "generate_question_bank"),
            None,
        )

    assert maximum_running == 1


@pytest.mark.asyncio
async def test_startup_converges_stale_config_action_jobs(monkeypatch) -> None:
    jobs = [
        SimpleNamespace(
            job_id="pcaj_queued",
            account_id=7,
            plugin_key="demo",
            action_key="sync",
            status=plugin_config_action_jobs.STATUS_QUEUED,
            message=None,
            error_code=None,
            error_message=None,
            ended_at=None,
            updated_at=None,
        ),
        SimpleNamespace(
            job_id="pcaj_running",
            account_id=8,
            plugin_key="demo",
            action_key="sync",
            status=plugin_config_action_jobs.STATUS_RUNNING,
            message=None,
            error_code=None,
            error_message=None,
            ended_at=None,
            updated_at=None,
        ),
    ]

    class _Scalars:
        def all(self):
            return jobs

    class _Result:
        def scalars(self):
            return _Scalars()

    class _DB:
        def __init__(self):
            self.added = []
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return _Result()

        def add(self, row):
            self.added.append(row)

        async def commit(self):
            self.commits += 1

    db = _DB()
    monkeypatch.setattr(plugin_config_action_jobs, "AsyncSessionLocal", lambda: db)

    assert await plugin_config_action_jobs.startup_plugin_config_action_jobs() == 2
    assert db.commits == 1
    assert all(job.status == plugin_config_action_jobs.STATUS_FAILED for job in jobs)
    assert all(job.error_code == plugin_config_action_jobs.INTERRUPTED_ERROR_CODE for job in jobs)
    assert all(job.ended_at is not None for job in jobs)
    assert len([row for row in db.added if isinstance(row, RuntimeLog)]) == 2


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_owned_config_action_tasks(monkeypatch) -> None:
    started = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_long_running())
    plugin_config_action_jobs._ACTIVE_TASKS.add(task)
    await started.wait()
    converge = AsyncMock(return_value=1)
    monkeypatch.setattr(plugin_config_action_jobs, "_converge_interrupted_jobs", converge)

    assert await plugin_config_action_jobs.shutdown_plugin_config_action_jobs() == 1
    assert task.cancelled()
    assert task not in plugin_config_action_jobs._ACTIVE_TASKS or task.done()
    converge.assert_awaited_once()


@pytest.mark.asyncio
async def test_pause_config_action_job_cancels_task_and_records_resumable_state(monkeypatch) -> None:
    started = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_long_running())
    await started.wait()
    job = SimpleNamespace(
        job_id="pcaj_pause",
        account_id=7,
        plugin_key="ai_redpacket",
        action_key="generate_question_bank",
        status=plugin_config_action_jobs.STATUS_RUNNING,
        message=None,
        error_code=None,
        error_message=None,
        result={},
        config_patch={},
        created_at=None,
        started_at=None,
        ended_at=None,
        updated_at=None,
    )

    class _DB:
        def __init__(self):
            self.commits = 0

        async def refresh(self, _job):
            return None

        async def commit(self):
            self.commits += 1

    db = _DB()
    monkeypatch.setattr(plugin_config_action_jobs, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(plugin_config_action_jobs, "_load_job_logs", AsyncMock(return_value=[]))
    write_log = AsyncMock()
    monkeypatch.setattr(plugin_config_action_jobs, "_write_runtime_log", write_log)
    plugin_config_action_jobs._ACTIVE_TASKS.add(task)
    plugin_config_action_jobs._ACTIVE_TASKS_BY_JOB_ID[job.job_id] = task

    response = await plugin_config_action_jobs.control_plugin_config_action_job(
        db,
        job.job_id,
        action="pause",
    )

    assert task.cancelled()
    assert response is not None
    assert response.status == plugin_config_action_jobs.STATUS_PAUSED
    assert response.error_code == "CONFIG_ACTION_PAUSED"
    assert "继续" in (response.message or "")
    assert db.commits == 1
    write_log.assert_awaited_once()
    plugin_config_action_jobs._ACTIVE_TASKS.discard(task)
    plugin_config_action_jobs._ACTIVE_TASKS_BY_JOB_ID.pop(job.job_id, None)


@pytest.mark.asyncio
async def test_control_does_not_overwrite_job_that_just_succeeded(monkeypatch) -> None:
    job = SimpleNamespace(
        job_id="pcaj_finished_during_control",
        account_id=7,
        plugin_key="ai_redpacket",
        action_key="generate_question_bank",
        status=plugin_config_action_jobs.STATUS_RUNNING,
        message=None,
        error_code=None,
        error_message=None,
        result={},
        config_patch={},
        created_at=None,
        started_at=None,
        ended_at=None,
        updated_at=None,
    )

    class _DB:
        async def refresh(self, target):
            target.status = plugin_config_action_jobs.STATUS_SUCCEEDED
            target.message = "配置动作已完成"

        async def commit(self):
            raise AssertionError("已成功任务不应被控制请求覆盖")

    monkeypatch.setattr(plugin_config_action_jobs, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(plugin_config_action_jobs, "_load_job_logs", AsyncMock(return_value=[]))
    write_log = AsyncMock()
    monkeypatch.setattr(plugin_config_action_jobs, "_write_runtime_log", write_log)

    response = await plugin_config_action_jobs.control_plugin_config_action_job(
        _DB(),
        job.job_id,
        action="pause",
    )

    assert response is not None
    assert response.status == plugin_config_action_jobs.STATUS_SUCCEEDED
    write_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_action_job_applies_account_patch_without_revalidating_legacy_config(monkeypatch) -> None:
    feature = SimpleNamespace(
        manifest={
            "config_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ai_timeout_seconds": {
                        "type": "integer",
                        "minimum": 300,
                    },
                    "knowledge_bases": {
                        "type": "array",
                        "level": "account",
                        "items": {"type": "object"},
                    },
                },
                "required": ["ai_timeout_seconds", "knowledge_bases"],
            }
        }
    )
    existing = SimpleNamespace(
        account_id=7,
        feature_key="quick_qa",
        enabled=True,
        config={"ai_timeout_seconds": 90, "knowledge_bases": []},
    )
    job = SimpleNamespace(
        account_id=7,
        plugin_key="quick_qa",
        action_key="generate_knowledge_base",
        job_id="pcaj_demo",
    )

    class _Result:
        def scalar_one_or_none(self):
            return existing

    class _DB:
        def __init__(self) -> None:
            self.added = []
            self.commits = 0

        async def execute(self, _query):
            return _Result()

        async def get(self, *_args, **_kwargs):
            return None

        def add(self, row):
            self.added.append(row)

        async def commit(self):
            self.commits += 1

        async def flush(self):
            return None

        async def refresh(self, _row):
            return None

    notified: list[int] = []

    async def _notify_reload(aid: int) -> None:
        notified.append(aid)

    monkeypatch.setattr(plugin_config_action_jobs.feature_service, "_notify_reload", _notify_reload)

    applied = await plugin_config_action_jobs._apply_config_patch(
        _DB(),
        job,
        feature,
        {
            "knowledge_bases": [
                {
                    "title": "青蛙PT-wiki",
                    "questions": [{"question": "Q", "options": ["A", "B", "C"], "answer_index": 0}],
                }
            ]
        },
    )

    assert applied == ["knowledge_bases"]
    assert existing.config["ai_timeout_seconds"] == 90
    assert existing.config["knowledge_bases"][0]["title"] == "青蛙PT-wiki"
    assert notified == [7]


def test_config_action_success_message_marks_auto_saved() -> None:
    assert plugin_config_action_jobs._success_message(
        "已生成题库：青蛙PT-wiki（80 题），请保存配置后生效。",
        auto_saved=True,
    ) == "已生成题库：青蛙PT-wiki（80 题），已自动保存并通知插件热加载。"
