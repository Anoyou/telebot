"""客户端身份 UA 版本配置（0.57.0 收口）契约测试。

覆盖：
- 版本号校验：合法 x.y.z / 预发布后缀通过；含空格/引号/控制字符的注入被拒。
- apply_version_overrides 只改版本号、重建目录，非法/未知键忽略、缺省回落默认。
- 版本覆盖只作用于 UA 版本段，不改 UA 结构与请求头字段名/值。
- 版本键元数据：有公共 registry 的可检测，Desktop 两段无 registry。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import commands
from app.schemas.command import ClientIdentityVersionsUpdateRequest
from app.services import llm_identity
from app.services.llm_identity import (
    CLIENT_IDENTITY_CLAUDE_CODE,
    CLIENT_IDENTITY_CODEX_TUI,
    apply_version_overrides,
    current_client_versions,
    default_client_versions,
    get_identity,
    is_valid_version,
    version_key_metadata,
)


def _reset() -> None:
    # 每个用例后恢复默认版本，避免污染其它测试的模块级目录。
    apply_version_overrides({})


def test_valid_version_accepts_semver_and_prerelease() -> None:
    assert is_valid_version("2.1.205") is True
    assert is_valid_version("0.143.0") is True
    assert is_valid_version("0.144.0-alpha.4") is True
    assert is_valid_version("0.146.0-alpha.3.1") is True
    assert is_valid_version("26.707.51957") is True


def test_valid_version_rejects_injection_and_junk() -> None:
    for bad in (
        None,
        "",
        "evil; rm -rf",
        "1.0 (Mac OS)",
        'x"y',
        "1.0\ninjected: header",
        "latest",
        "v2.1.205",
    ):
        assert is_valid_version(bad) is False, f"{bad!r} 不应通过版本校验"


def test_apply_overrides_changes_only_version_segment() -> None:
    try:
        before_headers = dict(get_identity(CLIENT_IDENTITY_CLAUDE_CODE).extra_headers)
        apply_version_overrides({"claude_code": "2.1.207"})
        ident = get_identity(CLIENT_IDENTITY_CLAUDE_CODE)
        # 版本号变了
        assert "2.1.207" in ident.user_agent
        # UA 结构未变（仍是 CLI 入口）
        assert ident.user_agent == "claude-cli/2.1.207 (external, cli)"
        # 请求头字段名/值未变
        assert dict(ident.extra_headers) == before_headers
        assert ident.extra_headers.get("x-app") == "cli"
    finally:
        _reset()


def test_apply_overrides_ignores_invalid_and_unknown_keys() -> None:
    try:
        result = apply_version_overrides(
            {
                "claude_code": "evil; DROP",  # 非法值 → 忽略，回落默认
                "unknown_key": "1.2.3",  # 未知键 → 忽略
                "codex_tui": "0.199.0",  # 合法 → 生效
            }
        )
        defaults = default_client_versions()
        assert result["claude_code"] == defaults["claude_code"]
        assert result["codex_tui"] == "0.199.0"
        assert "unknown_key" not in result
    finally:
        _reset()


def test_apply_empty_overrides_restores_defaults() -> None:
    apply_version_overrides({"codex_tui": "0.199.0"})
    assert current_client_versions()["codex_tui"] == "0.199.0"
    apply_version_overrides({})
    assert current_client_versions() == default_client_versions()


def test_codex_tui_ua_reflects_override() -> None:
    try:
        apply_version_overrides({"codex_tui": "0.199.0"})
        ident = get_identity(CLIENT_IDENTITY_CODEX_TUI)
        assert ident.user_agent.startswith("codex-tui/0.199.0 (")
        # originator 头不随版本变化
        assert ident.extra_headers.get("originator") == "codex-tui"
    finally:
        _reset()


def test_version_key_metadata_detectability() -> None:
    meta = version_key_metadata()
    # npm / PyPI / 固定只读 CLI 检测源均可检测
    assert meta["codex_tui"]["registry"] == "npm:@openai/codex"
    assert meta["claude_code"]["registry"] == "npm:@anthropic-ai/claude-code"
    assert meta["claude_sdk"]["registry"] == "npm:@anthropic-ai/sdk"
    assert meta["openai_sdk"]["registry"] == "pypi:openai"
    assert meta["grok_cli"]["registry"] == "cli:grok-update-check"
    assert meta["codex_desktop_core"]["registry"] is None
    assert meta["codex_desktop_build"]["registry"] is None


def test_parse_grok_update_check_prefers_remote_version() -> None:
    output = "A new version of Grok Build is available: 0.2.101 -> 0.2.102 [stable]"
    assert commands._parse_grok_update_check(output) == "0.2.102"
    assert commands._parse_grok_update_check("Grok Build is up to date: 0.2.102") == "0.2.102"
    assert (
        commands._parse_grok_update_check(
            '{"currentVersion":"0.2.101","latestVersion":"0.2.102","updateAvailable":true}'
        )
        == "0.2.102"
    )


@pytest.mark.asyncio
async def test_detect_grok_cli_latest_uses_read_only_check_command(monkeypatch) -> None:
    captured: list[tuple[str, ...]] = []

    class _Process:
        returncode = 0

        async def communicate(self):  # noqa: ANN201
            return b'{"currentVersion":"0.2.101","latestVersion":"0.2.102"}', None

    async def _create(*args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        captured.append(tuple(args))
        return _Process()

    monkeypatch.setattr(commands.asyncio, "create_subprocess_exec", _create)

    latest, error = await commands._detect_registry_latest("cli:grok-update-check")

    assert captured == [("grok", "update", "--check", "--json")]
    assert latest == "0.2.102"
    assert error is None


@pytest.mark.asyncio
async def test_detect_grok_cli_latest_falls_back_when_cli_missing(monkeypatch) -> None:
    async def _missing(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise FileNotFoundError

    fallback = AsyncMock(return_value=("0.2.102", None))
    monkeypatch.setattr(commands.asyncio, "create_subprocess_exec", _missing)
    monkeypatch.setattr(commands, "_detect_grok_stable_latest", fallback)

    latest, error = await commands._detect_grok_cli_latest()

    assert latest == "0.2.102"
    assert error is None
    fallback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_detect_grok_stable_latest_reads_official_channel(monkeypatch) -> None:
    requested: list[str] = []

    class _Response:
        status_code = 200
        text = "0.2.102\n"

    class _Client:
        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *args):  # noqa: ANN002, ANN003, ANN204
            return False

        async def get(self, url: str):  # noqa: ANN001, ANN201
            requested.append(url)
            return _Response()

    monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **_kwargs: _Client())

    latest, error = await commands._detect_grok_stable_latest()

    assert latest == "0.2.102"
    assert error is None
    assert requested == ["https://x.ai/cli/stable"]


@pytest.mark.asyncio
async def test_detect_grok_cli_latest_falls_back_after_timeout(monkeypatch) -> None:
    state = {"killed": False, "waited": False}

    class _Process:
        returncode = None

        async def communicate(self):  # noqa: ANN201
            raise TimeoutError

        def kill(self) -> None:
            state["killed"] = True

        async def wait(self) -> None:
            state["waited"] = True

    async def _create(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return _Process()

    fallback = AsyncMock(return_value=("0.2.102", None))
    monkeypatch.setattr(commands.asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr(commands, "_detect_grok_stable_latest", fallback)

    latest, error = await commands._detect_grok_cli_latest()

    assert latest == "0.2.102"
    assert error is None
    assert state == {"killed": True, "waited": True}
    fallback.assert_awaited_once_with()


def test_setting_key_is_stable() -> None:
    # 存储键固定，避免升级后读不到历史覆盖。
    assert llm_identity.CLIENT_IDENTITY_VERSIONS_SETTING_KEY == "llm_client_identity_versions"


def test_identity_version_routes_match_frontend_api_prefix() -> None:
    paths = {route.path for route in commands.router.routes}

    assert "/api/commands/llm-providers/identity-versions" in paths
    assert "/api/commands/llm-providers/identity-versions/detect" in paths


@pytest.mark.asyncio
async def test_identity_version_save_notifies_all_spawn_workers(monkeypatch) -> None:
    """保存进程内目录后必须广播 reload，让 spawn worker 重读同一份 DB 设置。"""

    events: list[object] = []
    row = SimpleNamespace(value={})

    class _DB:
        async def get(self, _model, _key):  # noqa: ANN001, ANN202
            return row

        async def commit(self) -> None:
            events.append("commit")

    async def _list_accounts(_db):  # noqa: ANN001, ANN202
        events.append("list_accounts")
        return [7, 9]

    async def _notify_reload(aids):  # noqa: ANN001, ANN202
        events.append(("notify_reload", aids))

    monkeypatch.setattr(commands.command_service, "list_all_account_ids", _list_accounts)
    monkeypatch.setattr(commands.command_service, "notify_reload", _notify_reload)

    try:
        response = await commands.update_client_identity_versions(
            ClientIdentityVersionsUpdateRequest(overrides={"codex_tui": "0.199.0"}),
            None,
            _DB(),
        )
        assert row.value == {"codex_tui": "0.199.0"}
        assert events == ["commit", "list_accounts", ("notify_reload", [7, 9])]
        assert next(item for item in response.items if item.key == "codex_tui").current == "0.199.0"
    finally:
        _reset()


@pytest.mark.asyncio
async def test_worker_command_context_refreshes_identity_versions_before_context_db(
    monkeypatch,
) -> None:
    """启动、IPC reload 与周期 reconcile 共用的刷新入口要先重建 worker 身份目录。"""

    from app.worker import runtime as worker_runtime

    load_overrides = AsyncMock(return_value={"codex_cli": "0.199.0"})
    monkeypatch.setattr(llm_identity, "load_version_overrides_from_db", load_overrides)

    class _FailingSession:
        async def __aenter__(self):  # noqa: ANN204
            raise RuntimeError("stop after identity refresh")

        async def __aexit__(self, *_args):  # noqa: ANN204
            return False

    monkeypatch.setattr(worker_runtime, "AsyncSessionLocal", _FailingSession)

    with pytest.raises(RuntimeError, match="stop after identity refresh"):
        await worker_runtime._refresh_command_context(7)

    load_overrides.assert_awaited_once_with()
