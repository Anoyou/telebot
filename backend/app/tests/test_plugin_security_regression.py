"""插件系统安全回归测试（阶段 E）。

覆盖：
1. 远程插件安装时 manifest.py 不会被执行
2. installed 插件无法访问 client.session
3. source_url scheme 白名单校验（只允许 https:// 和 git+ssh://）
4. 插件禁用后旧命令不再触发
5. InstalledPlugin.enabled=false 时即使 AccountFeature.enabled=true 也不加载
6. SandboxClient 反射防护（__class__, __dict__ 等）
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models.feature import FEATURE_STATE_DISABLED, AccountFeature, Feature
from app.db.models.plugin import InstalledPlugin
from app.db.models.plugin_global_config import PluginGlobalConfig
from app.db.models.plugin_repo import PluginRepo
from app.db.models.remote_plugin import RemotePlugin
from app.services import plugin_repo_service as repo_svc
from app.services import remote_plugin_service as svc


class TestRemotePluginSecurity:
    """远程插件安全测试。"""

    def test_plugin_data_dir_rejects_plugin_level_symlink(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        data_root = tmp_path / "installed" / "_data"
        other = data_root / "other_plugin"
        other.mkdir(parents=True)
        (data_root / "linked_plugin").symlink_to(other)

        with pytest.raises(svc.RemotePluginError, match="不得是符号链接"):
            svc._plugin_data_dir("linked_plugin")

    # ── 1. manifest.py 在安装阶段不执行 ──────────────────────────

    def test_install_phase_does_not_execute_manifest_py(self, monkeypatch, tmp_path):
        """验证 _read_plugin_metadata 绝对不执行 manifest.py。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))

        plugin_dir = tmp_path / "installed" / "evil"
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # 创建 plugin.json（合法）
        (plugin_dir / "plugin.json").write_text(
            '{"name": "evil", "version": "1.0.0"}',
            encoding="utf-8",
        )

        # 创建恶意 manifest.py（如果被执行会修改全局状态）
        import os as _os

        (plugin_dir / "manifest.py").write_text(
            'import os\n'
            'os.environ["EVIL_VAR"] = "pwned"\n'
            'MANIFEST = None\n',
            encoding="utf-8",
        )

        # 读取元数据（安装阶段）
        meta = svc._read_plugin_metadata(plugin_dir, fallback_name="evil")

        # 验证：manifest.py 不应该被执行
        assert meta.name == "evil"
        assert meta.version == "1.0.0"
        assert "EVIL_VAR" not in _os.environ

    def test_install_phase_rejects_missing_plugin_json(self, tmp_path, monkeypatch):
        """验证缺少 plugin.json 时抛出 InvalidPluginMetadata。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))

        plugin_dir = tmp_path / "installed" / "bad"
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # 只有 manifest.py，没有 plugin.json
        (plugin_dir / "manifest.py").write_text("MANIFEST = None\n", encoding="utf-8")

        with pytest.raises(svc.InvalidPluginMetadata) as ex:
            svc._read_plugin_metadata(plugin_dir, fallback_name="bad")
        assert ex.value.code == "PLUGIN_JSON_NOT_FOUND"

    def test_metadata_lint_warns_hardcoded_command_prefix_without_executing_manifest(self, tmp_path, monkeypatch):
        """安装/更新阶段静态 lint 能提示硬编码逗号前缀，且不执行 manifest.py。"""
        import os as _os

        monkeypatch.delenv("LINT_MANIFEST_EXECUTED", raising=False)

        plugin_dir = tmp_path / "installed" / "lint_demo"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            """
            {
              "name": "lint_demo",
              "version": "1.0.0",
              "config_schema": {
                "type": "object",
                "properties": {
                  "help_message_template": {
                    "type": "string",
                    "default": ",game 100 - 开始一局"
                  },
                  "stop_message_template": {
                    "type": "string",
                    "default": "<code>,{command} stop</code>"
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )
        (plugin_dir / "manifest.py").write_text(
            'import os\n'
            'os.environ["LINT_MANIFEST_EXECUTED"] = "1"\n'
            'MANIFEST = {"config_schema": {"properties": {"x": {"default": ",help"}}}}\n',
            encoding="utf-8",
        )

        warnings = svc.lint_plugin_metadata_files(plugin_dir)

        assert any("plugin.json" in item and ",game" in item for item in warnings)
        assert any("plugin.json" in item and ",{command}" in item for item in warnings)
        assert any("manifest.py" in item and ",help" in item for item in warnings)
        assert "LINT_MANIFEST_EXECUTED" not in _os.environ

    def test_metadata_lint_ignores_chinese_punctuation_and_csv_values(self, tmp_path):
        """普通中文逗号和逗号分隔值不能被误判成硬编码命令前缀。"""
        plugin_dir = tmp_path / "installed" / "lint_clean"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            """
            {
              "name": "lint_clean",
              "version": "1.0.0",
              "description": "群内小游戏，支持下注、开奖和历史统计",
              "config_schema": {
                "type": "object",
                "x-usage-guide": "发送 {prefix}{command} 100 开始一局，支持群内下注、开奖和历史统计。",
                "properties": {
                  "draw_numbers": {
                    "type": "string",
                    "default": "1,2,3,4,5,6"
                  },
                  "help_message_template": {
                    "type": "string",
                    "default": "{prefix}{command} 100 - 开始一局"
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )
        (plugin_dir / "manifest.py").write_text(
            'MANIFEST = {"description": "插件说明，支持多种玩法", '
            '"config_schema": {"properties": {"x": {"default": "A,B,C"}}}}\n',
            encoding="utf-8",
        )

        assert svc.lint_plugin_metadata_files(plugin_dir) == []

    def test_metadata_lint_warns_when_config_schema_lacks_usage_guide(self, tmp_path):
        """有配置页的插件必须声明自有使用说明。"""
        plugin_dir = tmp_path / "installed" / "missing_usage"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            """
            {
              "name": "missing_usage",
              "version": "1.0.0",
              "config_schema": {
                "type": "object",
                "properties": {
                  "command": {
                    "type": "string",
                    "default": "demo"
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )

        warnings = svc.lint_plugin_metadata_files(plugin_dir)
        assert any("高级规范警告" in item and "未声明详细使用说明" in item for item in warnings)

    def test_metadata_lint_accepts_usage_guide_declaration(self, tmp_path):
        """x-usage-guide 或 usage_preview 可满足配置页说明要求。"""
        plugin_dir = tmp_path / "installed" / "declared_usage"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            """
            {
              "name": "declared_usage",
              "version": "1.0.0",
              "config_schema": {
                "type": "object",
                "x-usage-guide": "发送 {prefix}{command} 文本即可触发插件。",
                "properties": {
                  "command": {
                    "type": "string",
                    "default": "demo"
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )

        warnings = svc.lint_plugin_metadata_files(plugin_dir)
        assert not any("未声明详细使用说明" in item for item in warnings)

    def test_metadata_lint_accepts_top_level_usage_declaration(self, tmp_path):
        """新版 plugin.json 顶层 usage 可满足插件使用说明要求。"""
        plugin_dir = tmp_path / "installed" / "declared_top_usage"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            """
            {
              "name": "declared_top_usage",
              "version": "1.0.0",
              "usage": "在允许会话内发送“开始演示”启动插件。"
            }
            """,
            encoding="utf-8",
        )

        warnings = svc.lint_plugin_metadata_files(plugin_dir)
        assert not any("未声明详细使用说明" in item for item in warnings)

    def test_runtime_discovery_does_not_execute_installed_by_default(self, monkeypatch, tmp_path):
        """worker 刷新 builtin 注册表时不能顺手执行 installed 插件代码。"""
        import os as _os

        from app.worker.plugins import loader as loader_mod

        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        monkeypatch.delenv("EVIL_RUNTIME_DISCOVERY", raising=False)

        plugin_dir = tmp_path / "installed" / "evil_runtime"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text(
            'import os\n'
            'os.environ["EVIL_RUNTIME_DISCOVERY"] = "pwned"\n'
            "PLUGIN_CLASS = None\n"
            "MANIFEST = None\n",
            encoding="utf-8",
        )

        loader_mod.discover_plugins()

        assert "EVIL_RUNTIME_DISCOVERY" not in _os.environ

    def test_remote_plugin_requires_runtime_package_files(self, tmp_path):
        """远程插件必须按新版文档提供完整运行期包结构。"""
        plugin_dir = tmp_path / "installed" / "single_file"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            """
            {
              "name": "single_file",
              "display_name": "单文件插件",
              "version": "1.0.0",
              "entry": "plugin.py",
              "permissions": ["send_message"]
            }
            """,
            encoding="utf-8",
        )
        (plugin_dir / "plugin.py").write_text(
            "from app.worker.plugins.base import Plugin, register\n"
            "@register\n"
            "class SingleFilePlugin(Plugin):\n"
            "    key = 'single_file'\n"
            "    display_name = '单文件插件'\n",
            encoding="utf-8",
        )

        meta = svc._read_plugin_metadata(plugin_dir, fallback_name="single_file")
        with pytest.raises(svc.InvalidPluginMetadata) as ex:
            svc._validate_runtime_plugin_shape(plugin_dir, meta)

        assert ex.value.code == "PLUGIN_RUNTIME_FILES_MISSING"
        assert "manifest.py" in ex.value.message
        assert "__init__.py" in ex.value.message
        assert "PLUGIN-REMOTE" in ex.value.message

    # ── 2. source_url scheme 白名单 ───────────────────────────────

    def test_validate_source_url_rejects_file_scheme(self):
        """file:// scheme 必须被拒绝。"""
        with pytest.raises(svc.InvalidSourceUrl) as ex:
            svc._validate_source_url("file:///etc/passwd")
        assert "https" in ex.value.message.lower() or "BAD_SOURCE_URL" in ex.value.code

    def test_validate_source_url_rejects_http_scheme(self):
        """http:// scheme 必须被拒绝（必须使用 https）。"""
        with pytest.raises(svc.InvalidSourceUrl) as ex:
            svc._validate_source_url("http://example.com/repo.git")
        assert "https" in ex.value.message.lower()

    def test_validate_source_url_allows_https(self):
        """https:// scheme 必须被允许。"""
        svc._validate_source_url("https://github.com/user/repo.git")
        svc._validate_source_url("https://gitlab.com/user/repo")
        svc._validate_source_url("https://github.com/user/repo/tree/feature/demo")

    def test_normalize_git_source_url_supports_github_tree_branch(self):
        """GitHub tree/<branch> 页面链接应转为 clone URL + 分支。"""
        source = svc.normalize_git_source_url(
            "https://github.com/user/repo/tree/feature/demo"
        )

        assert source.clone_url == "https://github.com/user/repo.git"
        assert source.ref == "feature/demo"
        assert svc._derive_name_from_url("https://github.com/user/repo/tree/feature/demo") == "repo"

    def test_validate_source_url_allows_git_ssh(self):
        """git+ssh:// scheme 必须被允许。"""
        svc._validate_source_url("git+ssh://git@github.com/user/repo.git")

    def test_validate_source_url_allows_scp_style(self):
        """git@host:path 格式必须被允许。"""
        svc._validate_source_url("git@github.com:user/repo.git")

    def test_validate_source_url_rejects_empty(self):
        """空 URL 必须被拒绝。"""
        with pytest.raises(svc.InvalidSourceUrl):
            svc._validate_source_url("")
        with pytest.raises(svc.InvalidSourceUrl):
            svc._validate_source_url("   ")

    # ── 3. git clone timeout ─────────────────────────────────────

    def test_run_git_timeout_works(self):
        """验证 _run_git 支持 timeout 参数。"""

        async def _test():
            with pytest.raises(svc.GitOperationFailed) as ex:
                # 使用 git alias 执行一个短暂等待命令，避免依赖网络或目标目录状态。
                await svc._run_git("-c", "alias.slow=!sleep 1", "slow", timeout=0.001)
            # 超时错误码应该是 GIT_TIMEOUT
            assert "TIMEOUT" in ex.value.code or "超时" in ex.value.message

        asyncio.run(_test())

    def test_run_git_reports_missing_binary(self, monkeypatch):
        """运行环境缺少 git 时必须返回可读错误，而不是冒泡成 500。"""

        async def _test():
            monkeypatch.setattr(svc.shutil, "which", lambda name: None)
            with pytest.raises(svc.GitOperationFailed) as ex:
                await svc._run_git("--version")
            assert ex.value.code == "GIT_NOT_FOUND"
            assert "缺少 git" in ex.value.message

        asyncio.run(_test())


class TestSandboxClientSecurity:
    """SandboxClient 安全测试。"""

    def _make_sandbox(self):
        """创建一个测试用的 SandboxClient。"""
        from app.worker.plugins.sandbox import SandboxClient

        # 创建一个假 client
        class FakeClient:
            def get_me(self):
                return None

            def send_message(self, *args, **kwargs):
                return None

            session = "REAL_SESSION_OBJECT"

        return SandboxClient(FakeClient(), ["send_message"], plugin_key="test")

    def test_sandbox_blocks_session_access(self):
        """installed 插件绝对不能访问 client.session。"""
        sandbox = self._make_sandbox()

        with pytest.raises(PermissionError) as ex:
            _ = sandbox.session
        assert "禁止访问" in str(ex.value) or "session" in str(ex.value)

    def test_sandbox_recognizes_payout_as_non_client_permission(self, caplog):
        """payout 由 MessageOps 处理，不能被 SandboxClient 误报为未知权限。"""
        from app.worker.plugins.sandbox import resolve_permissions

        with caplog.at_level("WARNING"):
            assert resolve_permissions(["payout"]) == frozenset()

        assert "未知权限名" not in caplog.text

    def test_sandbox_blocks_class_reflection(self):
        """禁止通过 __class__ 反射获取真实类型。"""
        sandbox = self._make_sandbox()

        with pytest.raises(PermissionError) as ex:
            _ = sandbox.__class__
        assert "禁止访问" in str(ex.value)

    def test_sandbox_blocks_dict_reflection(self):
        """禁止通过 __dict__ 获取真实属性。"""
        sandbox = self._make_sandbox()

        with pytest.raises(PermissionError) as ex:
            _ = sandbox.__dict__
        assert "禁止访问" in str(ex.value)

    @pytest.mark.asyncio
    async def test_wrap_event_for_context_wraps_sandbox_subclasses(self):
        """SandboxClient 子类也必须走 SandboxEvent 包装并继续执行权限校验。"""
        from app.worker.plugins.base import PluginContext
        from app.worker.plugins.loader import _wrap_event_for_context
        from app.worker.plugins.sandbox import SandboxClient, SandboxEvent

        class MySandboxClient(SandboxClient):
            pass

        class RawEvent:
            async def reply(self, *_args, **_kwargs):
                return "ok"

        sandbox_client = MySandboxClient(SimpleNamespace(), [], plugin_key="demo")
        ctx = PluginContext(account_id=7, feature_key="demo", client=sandbox_client)
        wrapped = _wrap_event_for_context(RawEvent(), ctx)

        assert isinstance(wrapped, SandboxEvent)
        with pytest.raises(PermissionError):
            await wrapped.reply("x")

    @pytest.mark.asyncio
    async def test_plugin_command_handler_receives_sandbox_client(self):
        """插件命令 handler 不能收到 dispatcher 传入的原始 Telethon client。"""
        from app.worker.plugins.base import PluginContext
        from app.worker.plugins.loader import _wrap_cmd
        from app.worker.plugins.sandbox import SandboxClient

        raw_client = SimpleNamespace(session="REAL_SESSION_OBJECT")
        sandbox = SandboxClient(raw_client, [], plugin_key="demo")
        seen = {}

        async def _handler(client, event, args, account_id, ctx):
            seen["client"] = client
            seen["account_id"] = account_id
            seen["ctx"] = ctx

        ctx = PluginContext(account_id=7, feature_key="demo", client=sandbox)
        wrapped = _wrap_cmd(_handler, ctx)
        await wrapped(raw_client, object(), [], 7)

        assert seen["client"] is sandbox
        assert seen["client"] is not raw_client
        assert seen["account_id"] == 7
        assert seen["ctx"] is not ctx
        assert seen["ctx"].client is sandbox
        assert seen["ctx"].account_id == ctx.account_id
        assert seen["ctx"].feature_key == ctx.feature_key

    def test_sandbox_blocks_private_attrs(self):
        """禁止访问私有属性。"""
        sandbox = self._make_sandbox()

        for attr in ("_client", "_api", "_state", "_connection"):
            with pytest.raises(PermissionError):
                getattr(sandbox, attr)

    def test_sandbox_allows_declared_permissions(self):
        """manifest 声明的权限必须被允许。"""
        sandbox = self._make_sandbox()

        # send_message 是声明的权限，必须可用
        assert callable(sandbox.send_message)

    def test_sandbox_resolve_entity_requires_explicit_permission(self):
        """实体解析必须单独声明 resolve_entity，不能混入普通 read_chat。"""
        from app.worker.plugins.sandbox import SandboxClient

        class FakeClient:
            def get_entity(self, *args, **kwargs):
                return None

        read_only = SandboxClient(FakeClient(), ["read_chat"], plugin_key="demo")
        with pytest.raises(PermissionError):
            _ = read_only.get_entity

        resolver = SandboxClient(FakeClient(), ["resolve_entity"], plugin_key="demo")
        assert callable(resolver.get_entity)

    def test_sandbox_forward_messages_uses_forward_message_permission(self):
        """原生转发使用 Telethon 复数方法，但 manifest 能力名保持 forward_message。"""
        from app.worker.plugins.sandbox import SandboxClient

        class FakeClient:
            def forward_messages(self, *args, **kwargs):
                return None

        sandbox = SandboxClient(FakeClient(), ["forward_message"], plugin_key="demo")
        assert callable(sandbox.forward_messages)

    @pytest.mark.asyncio
    async def test_sandbox_unpin_message_requires_delete_permission_and_records_trace(self, monkeypatch):
        """取消置顶属于消息清理能力，并且必须留下 event_action。"""
        from app.worker.plugins import loader as plugin_loader
        from app.worker.plugins.sandbox import SandboxClient

        class FakeClient:
            async def unpin_message(self, entity, message, **kwargs):
                return SimpleNamespace(id=message, chat_id=entity)

        denied = SandboxClient(FakeClient(), [], plugin_key="demo")
        with pytest.raises(PermissionError):
            _ = denied.unpin_message

        record_action = AsyncMock()
        monkeypatch.setattr(plugin_loader, "record_action", record_action)
        allowed = SandboxClient(FakeClient(), ["delete_message"], plugin_key="demo")
        traced = plugin_loader._trace_plugin_client(
            allowed,
            "evt_unpin",
            plugin_key="demo",
            component="sandbox_unpin_test",
        )

        await traced.unpin_message(-100123, 456)

        record_action.assert_awaited_once()
        assert record_action.await_args.args[1]["type"] == "unpin_message"
        assert record_action.await_args.args[1]["chat_id"] == -100123
        assert record_action.await_args.args[1]["message_id"] == 456
        assert record_action.await_args.args[2] == plugin_loader.TRACE_STATUS_OK
        assert record_action.await_args.kwargs["actual_send_via"] == "userbot_reply"

    def test_sandbox_blocks_undelared_attrs(self):
        """未声明的属性访问必须被拒绝。"""
        sandbox = self._make_sandbox()

        for attr in ("delete_messages", "forward_messages"):
            with pytest.raises(PermissionError):
                getattr(sandbox, attr)

    @pytest.mark.asyncio
    async def test_sandbox_moderate_chat_requires_permission(self):
        """成员管理方法必须声明 moderate_chat 后才能调用。"""
        from app.worker.plugins.sandbox import SandboxClient

        raw_client = SimpleNamespace()
        sandbox = SandboxClient(raw_client, [], plugin_key="demo")

        with pytest.raises(PermissionError) as ex:
            await sandbox.mute_user(-100123, 456, duration_seconds=60)
        assert "moderate_chat" in str(ex.value) or "缺少权限" in str(ex.value)

    @pytest.mark.asyncio
    async def test_sandbox_moderate_chat_exposes_controlled_methods(self):
        """moderate_chat 只开放受控封禁/踢出/禁言/解封包装方法。"""
        from app.worker.plugins.sandbox import SandboxClient

        calls: list[tuple[str, tuple, dict]] = []

        class FakeClient:
            async def edit_permissions(self, *args, **kwargs):
                calls.append(("edit_permissions", args, kwargs))
                return "edited"

            async def kick_participant(self, *args, **kwargs):
                calls.append(("kick_participant", args, kwargs))
                return "kicked"

        sandbox = SandboxClient(FakeClient(), ["moderate_chat"], plugin_key="demo")

        assert await sandbox.mute_user(-100123, 456, duration_seconds=60) == "edited"
        assert await sandbox.ban_user(-100123, 456) == "edited"
        assert await sandbox.kick_user(-100123, 456) == "kicked"
        assert await sandbox.unban_user(-100123, 456) == "edited"

        assert calls[0][0] == "edit_permissions"
        assert calls[0][1] == (-100123, 456)
        assert calls[0][2]["send_messages"] is False
        assert int(calls[0][2]["until_date"].total_seconds()) == 60
        assert calls[1][2]["view_messages"] is False
        assert calls[2] == ("kick_participant", (-100123, 456), {})
        assert calls[3] == ("edit_permissions", (-100123, 456), {})

    def test_sandbox_blocks_mtproto_call(self):
        """禁止 raw MTProto 调用。"""
        sandbox = self._make_sandbox()

        with pytest.raises(PermissionError) as ex:
            sandbox("fake_mtproto_call")
        assert "MTProto" in str(ex.value) or "__call__" in str(ex.value)

    @pytest.mark.asyncio
    async def test_sandbox_event_helpers_require_permissions(self):
        """installed 插件不能通过 event helper 绕过 SandboxClient 权限。"""
        from app.worker.plugins.sandbox import SandboxClient, SandboxEvent

        calls: list[tuple[str, tuple, dict]] = []

        class RawEvent:
            raw_text = "hello"
            chat_id = 123
            id = 777

            def __init__(self) -> None:
                self.message = SimpleNamespace(reply=self.reply, text="hello", chat_id=123, id=999)

            async def reply(self, *_args, **_kwargs):
                calls.append(("raw_reply", _args, _kwargs))

            async def edit(self, *_args, **_kwargs):
                calls.append(("raw_edit", _args, _kwargs))

            async def delete(self):
                calls.append(("raw_delete", (), {}))

            async def get_reply_message(self):
                calls.append(("get_reply_message", (), {}))
                return SimpleNamespace(raw_text="reply")

        class FakeClient:
            async def send_message(self, *args, **kwargs):
                calls.append(("send_message", args, kwargs))

            async def edit_message(self, *args, **kwargs):
                calls.append(("edit_message", args, kwargs))

            async def delete_messages(self, *args, **kwargs):
                calls.append(("delete_messages", args, kwargs))

        raw_event = RawEvent()
        denied = SandboxEvent(raw_event, SandboxClient(SimpleNamespace(), [], plugin_key="demo"), plugin_key="demo")

        assert denied.raw_text == "hello"
        assert denied.chat_id == 123
        with pytest.raises(PermissionError):
            await denied.reply("x")
        with pytest.raises(PermissionError):
            await denied.edit("x")
        with pytest.raises(PermissionError):
            await denied.delete()
        with pytest.raises(PermissionError):
            await denied.get_reply_message()
        with pytest.raises(PermissionError):
            await denied.message.reply("x")
        assert calls == []

        allowed = SandboxEvent(
            raw_event,
            SandboxClient(
                FakeClient(),
                ["send_message", "edit_message", "delete_message", "read_chat"],
                plugin_key="demo",
            ),
            plugin_key="demo",
        )
        await allowed.reply("x")
        await allowed.edit("x")
        await allowed.delete()
        replied = await allowed.get_reply_message()
        await allowed.message.reply("x")

        assert calls == [
            ("send_message", (123, "x"), {"reply_to": 777}),
            ("edit_message", (123, 777, "x"), {}),
            ("delete_messages", (123, 777), {}),
            ("get_reply_message", (), {}),
            ("send_message", (123, "x"), {"reply_to": 999}),
        ]
        with pytest.raises(PermissionError):
            _ = replied.__dict__

    @pytest.mark.asyncio
    async def test_sandbox_event_reply_routes_through_trace_client(self, monkeypatch):
        """允许的 event.reply 必须走 trace-aware client 并记录 event_action。"""
        from app.worker.plugins import loader as plugin_loader
        from app.worker.plugins.sandbox import SandboxClient, SandboxEvent

        class FakeClient:
            async def send_message(self, *_args, **_kwargs):
                return SimpleNamespace(id=42, chat_id=123)

        record_action = AsyncMock()
        monkeypatch.setattr(plugin_loader, "record_action", record_action)
        sandbox_client = SandboxClient(FakeClient(), ["send_message"], plugin_key="demo")
        trace_client = plugin_loader._trace_plugin_client(
            sandbox_client,
            "evt_sandbox_reply",
            plugin_key="demo",
            component="sandbox_event_test",
        )
        event = SandboxEvent(
            SimpleNamespace(raw_text="hello", chat_id=123, id=777),
            trace_client,
            plugin_key="demo",
        )

        await event.reply("x")

        record_action.assert_awaited_once()
        assert record_action.await_args.args[0]["trace_id"] == "evt_sandbox_reply"
        assert record_action.await_args.args[1]["type"] == "send_message"
        assert record_action.await_args.args[1]["reply_to_message_id"] == 777
        assert record_action.await_args.args[2] == plugin_loader.TRACE_STATUS_OK

    @pytest.mark.asyncio
    async def test_sandbox_event_message_layer_still_enforces_permissions(self):
        """嵌套 message 包装层也必须走权限校验。"""
        from app.worker.plugins.sandbox import SandboxClient, SandboxEvent

        class RawMessage:
            raw_text = "inner"

            async def reply(self, *_args, **_kwargs):
                raise AssertionError("should not be called when permission missing")

            async def edit(self, *_args, **_kwargs):
                raise AssertionError("should not be called when permission missing")

        raw_message = RawMessage()
        raw_event = SimpleNamespace(message=raw_message, raw_text="outer")
        sandbox_client = SandboxClient(SimpleNamespace(), [], plugin_key="demo")
        event = SandboxEvent(raw_event, sandbox_client, plugin_key="demo")

        nested = event.message
        assert nested.raw_text == "inner"
        with pytest.raises(PermissionError):
            await nested.reply("x")
        with pytest.raises(PermissionError):
            await nested.edit("x")

    def test_sandbox_event_exposes_sandbox_client_not_raw_client(self):
        """event.client 必须返回 SandboxClient，不能暴露原始 Telethon client。"""
        from app.worker.plugins.sandbox import SandboxClient, SandboxEvent

        raw_client = SimpleNamespace(session="REAL_SESSION_OBJECT")
        raw_event = SimpleNamespace(client=raw_client, raw_text="hello")
        sandbox_client = SandboxClient(raw_client, [], plugin_key="demo")
        event = SandboxEvent(raw_event, sandbox_client, plugin_key="demo")

        assert event.client is sandbox_client
        assert event.raw_text == "hello"
        with pytest.raises(PermissionError):
            _ = event._client

    def test_sandbox_event_blocks_unlisted_callable_helpers(self):
        """未列入白名单的 event callable 不能直接调用。"""
        from app.worker.plugins.sandbox import SandboxClient, SandboxEvent

        raw_event = SimpleNamespace(download_media=lambda: b"secret")
        event = SandboxEvent(
            raw_event,
            SandboxClient(SimpleNamespace(), ["read_chat"], plugin_key="demo"),
            plugin_key="demo",
        )

        with pytest.raises(PermissionError):
            _ = event.download_media


class TestPluginCommandLifecycle:
    """插件命令生命周期安全测试。"""

    def test_unregister_removes_plugin_command(self):
        """验证 unregister_plugin_command 能正确移除命令。"""
        from app.worker.command import (
            _PLUGIN_COMMANDS,
            register_plugin_command,
            unregister_plugin_command,
        )

        async def dummy_handler(*args, **kwargs):
            pass

        # 注册一个测试命令
        register_plugin_command("test_cmd_lifecycle", dummy_handler, owner_plugin_key="test", generation=1)
        assert "test_cmd_lifecycle" in _PLUGIN_COMMANDS

        # 注销
        unregister_plugin_command("test_cmd_lifecycle", owner_plugin_key="test")
        assert "test_cmd_lifecycle" not in _PLUGIN_COMMANDS

    def test_unregister_with_wrong_owner_keeps_command(self):
        """指定错误的 owner_plugin_key 时不注销命令。"""
        from app.worker.command import (
            _PLUGIN_COMMANDS,
            register_plugin_command,
            unregister_plugin_command,
        )

        async def dummy_handler(*args, **kwargs):
            pass

        # 注册属于 plugin_a 的命令
        register_plugin_command("test_cmd_owner", dummy_handler, owner_plugin_key="plugin_a", generation=1)

        # 用 plugin_b 尝试注销，应该不成功
        unregister_plugin_command("test_cmd_owner", owner_plugin_key="plugin_b")
        assert "test_cmd_owner" in _PLUGIN_COMMANDS

        # 用正确的 owner 注销
        unregister_plugin_command("test_cmd_owner", owner_plugin_key="plugin_a")
        assert "test_cmd_owner" not in _PLUGIN_COMMANDS

    def test_unregister_all_plugin_commands(self):
        """验证 unregister_all_plugin_commands 能注销所有该插件的命令。"""
        from app.worker.command import (
            _PLUGIN_COMMANDS,
            register_plugin_command,
            unregister_all_plugin_commands,
        )

        async def dummy_handler(*args, **kwargs):
            pass

        # 注册属于 test_plugin 的多个命令
        register_plugin_command("cmd1", dummy_handler, owner_plugin_key="test_plugin", generation=1)
        register_plugin_command("cmd2", dummy_handler, owner_plugin_key="test_plugin", generation=1)
        register_plugin_command("cmd3", dummy_handler, owner_plugin_key="other_plugin", generation=1)

        assert len(_PLUGIN_COMMANDS) >= 3

        # 注销所有 test_plugin 的命令
        unregister_all_plugin_commands(owner_plugin_key="test_plugin")

        # test_plugin 的命令应该被移除
        assert "cmd1" not in _PLUGIN_COMMANDS
        assert "cmd2" not in _PLUGIN_COMMANDS
        # other_plugin 的命令应该保留
        assert "cmd3" in _PLUGIN_COMMANDS


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return _FakeScalars(self._items)

    def all(self):
        return self._items


class _FakeRemotePluginDB:
    def __init__(self, *, account_features=None):
        self.remote = SimpleNamespace(name="idiom_chain", enabled=False)
        self.accounts = [1, 2]
        self.account_features = list(account_features or [])
        self.installed_rows: dict[str, InstalledPlugin] = {
            "idiom_chain": InstalledPlugin(key="idiom_chain", source="git", enabled=False)
        }
        self.added = []
        self.flush_count = 0

    async def get(self, model, pk):  # noqa: ANN001
        if model is InstalledPlugin:
            return self.installed_rows.get(pk)
        return None

    async def execute(self, stmt):
        text = str(stmt).lower()
        if "remote_plugin" in text:
            return _FakeResult([self.remote])
        if "account_feature" in text:
            return _FakeResult(self.account_features)
        if "from account" in text:
            return _FakeResult(self.accounts)
        return _FakeResult([])

    def add(self, row):
        self.added.append(row)
        if isinstance(row, AccountFeature):
            self.account_features.append(row)

    async def flush(self):
        self.flush_count += 1


class _FakeRemoteInstallDB:
    def __init__(self) -> None:
        self.remote_rows: dict[str, RemotePlugin] = {}
        self.installed_rows: dict[str, InstalledPlugin] = {}
        self.features: dict[str, Feature] = {}
        self.account_features: list[AccountFeature] = []
        self.flush_count = 0

    async def get(self, model, pk):  # noqa: ANN001
        if model is InstalledPlugin:
            return self.installed_rows.get(pk)
        return None

    async def execute(self, stmt):  # noqa: ANN001
        text = str(stmt).lower()
        if "remote_plugin" in text:
            return _FakeResult(list(self.remote_rows.values()))
        if "feature" in text and "account_feature" not in text:
            return _FakeResult(list(self.features.values()))
        if "account_feature" in text or "from account" in text:
            return _FakeResult(self.account_features)
        return _FakeResult([])

    def add(self, row):  # noqa: ANN001
        if isinstance(row, RemotePlugin):
            self.remote_rows[row.name] = row
        elif isinstance(row, InstalledPlugin):
            self.installed_rows[row.key] = row
        elif isinstance(row, Feature):
            self.features[row.key] = row

    async def delete(self, row):  # noqa: ANN001
        if isinstance(row, RemotePlugin):
            self.remote_rows.pop(row.name, None)
        elif isinstance(row, InstalledPlugin):
            self.installed_rows.pop(row.key, None)
        elif isinstance(row, Feature):
            self.features.pop(row.key, None)
        elif isinstance(row, AccountFeature):
            if row in self.account_features:
                self.account_features.remove(row)

    async def flush(self):
        self.flush_count += 1


class _FakePluginRepoDB:
    def __init__(self, repo: PluginRepo | None = None) -> None:
        self.repo = repo
        self.remote_rows: dict[str, RemotePlugin] = {}
        self.installed_rows: dict[str, InstalledPlugin] = {}
        self.features: dict[str, Feature] = {}
        self.account_features: list[AccountFeature] = []
        self.global_configs: dict[str, PluginGlobalConfig] = {}
        self.flush_count = 0

    async def get(self, model, pk):  # noqa: ANN001
        if model is InstalledPlugin:
            return self.installed_rows.get(pk)
        if model is Feature:
            return self.features.get(pk)
        if model is PluginGlobalConfig:
            return self.global_configs.get(pk)
        return None

    async def execute(self, stmt):  # noqa: ANN001
        text = str(stmt).lower()
        if "plugin_repo" in text:
            return _FakeResult([self.repo] if self.repo is not None else [])
        if "remote_plugin" in text:
            return _FakeResult(list(self.remote_rows.values()))
        if "feature" in text and "account_feature" not in text:
            return _FakeResult(list(self.features.values()))
        if "account_feature" in text:
            return _FakeResult(self.account_features)
        if "from account" in text:
            return _FakeResult([])
        return _FakeResult([])

    def add(self, row):  # noqa: ANN001
        if isinstance(row, RemotePlugin):
            self.remote_rows[row.name] = row
        elif isinstance(row, InstalledPlugin):
            self.installed_rows[row.key] = row
        elif isinstance(row, Feature):
            self.features[row.key] = row
        elif isinstance(row, AccountFeature):
            self.account_features.append(row)
        elif isinstance(row, PluginGlobalConfig):
            self.global_configs[row.plugin_key] = row

    async def flush(self):
        self.flush_count += 1


def _write_runtime_plugin(plugin_dir, *, key: str, version: str = "1.0.0", extra_py: str = "") -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        (
            "{"
            f'"name":"{key}",'
            f'"display_name":"{key}",'
            f'"version":"{version}"'
            "}"
        ),
        encoding="utf-8",
    )
    (plugin_dir / "manifest.py").write_text(
        "from app.worker.plugins.manifest import Manifest\n"
        f"MANIFEST = Manifest(key={key!r}, display_name={key!r}, version={version!r})\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from .plugin import DemoPlugin\nPLUGIN_CLASS = DemoPlugin\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(
        "from app.worker.plugins.base import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        f"    key = {key!r}\n"
        f"{extra_py}",
        encoding="utf-8",
    )


class TestRemotePluginEnableFlow:
    """远程插件启用流程测试。"""

    @pytest.mark.asyncio
    async def test_first_enable_creates_account_feature_rows(self, monkeypatch):
        """新远程插件首次启用后，应能直接被现有账号加载。"""

        async def _fail_reload(*_args, **_kwargs):
            raise AssertionError("reload must happen after commit in callers")

        monkeypatch.setattr(svc, "_trigger_reload", _fail_reload)
        db = _FakeRemotePluginDB()

        row = await svc.enable(db, "idiom_chain", bootstrap_accounts=True)

        assert row.enabled is True
        assert [(af.account_id, af.feature_key, af.enabled, af.state) for af in db.added] == [
            (1, "idiom_chain", True, FEATURE_STATE_DISABLED),
            (2, "idiom_chain", True, FEATURE_STATE_DISABLED),
        ]

    @pytest.mark.asyncio
    async def test_enable_preserves_existing_account_choices(self):
        """已有账号级记录时，不用全局启用覆盖用户选择。"""
        existing = AccountFeature(
            account_id=1,
            feature_key="idiom_chain",
            enabled=False,
            state=FEATURE_STATE_DISABLED,
        )
        db = _FakeRemotePluginDB(account_features=[existing])

        await svc.enable(db, "idiom_chain", bootstrap_accounts=True)

        assert db.added == []
        assert existing.enabled is False

    @pytest.mark.asyncio
    async def test_internal_enable_does_not_bootstrap_accounts_by_default(self):
        """账号级入口只打开全局开关，不应顺带启用所有账号。"""
        db = _FakeRemotePluginDB()

        await svc.enable(db, "idiom_chain")

        assert db.installed_rows["idiom_chain"].enabled is True
        assert db.added == []

    @pytest.mark.asyncio
    async def test_disable_updates_installed_plugin_row(self):
        """远程插件禁用时只更新统一安装表 enabled。"""
        db = _FakeRemotePluginDB()
        db.remote.enabled = True
        db.installed_rows["idiom_chain"].enabled = True

        await svc.disable(db, "idiom_chain")

        assert db.installed_rows["idiom_chain"].enabled is False

    @pytest.mark.asyncio
    async def test_install_writes_installed_plugin_with_lint_warnings(self, monkeypatch, tmp_path):
        """Git 安装成功后写 installed_plugin，并保存静态 lint warning。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))

        async def _fake_clone(*args, **_kwargs):  # noqa: ANN001
            target = args[-1]
            plugin_dir = tmp_path / "installed" / "git_demo.installing"
            assert str(plugin_dir) == str(target)
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.json").write_text(
                '{"name":"git_demo","display_name":"Git Demo","version":"1.2.3"}',
                encoding="utf-8",
            )
            (plugin_dir / "manifest.py").write_text(
                "from app.worker.plugins.manifest import Manifest\n"
                "MANIFEST = Manifest(key='git_demo', display_name='Git Demo', version='1.2.3')\n",
                encoding="utf-8",
            )
            (plugin_dir / "__init__.py").write_text("PLUGIN_CLASS = None\n", encoding="utf-8")
            (plugin_dir / "plugin.py").write_text(
                "import httpx\n"
                "from app.db.models.plugin import PluginInstall\n"
                "httpx.get('https://example.com')\n",
                encoding="utf-8",
            )
            return ""

        monkeypatch.setattr(svc, "_run_git", _fake_clone)
        db = _FakeRemoteInstallDB()

        row = await svc.install(db, "https://example.com/git_demo.git", enable=True)

        installed = db.installed_rows["git_demo"]
        assert row.name == "git_demo"
        assert installed.source == "git"
        assert installed.version == "1.2.3"
        assert installed.source_url == "https://example.com/git_demo.git"
        assert installed.installed_path == str(tmp_path / "installed" / "git_demo")
        assert installed.enabled is True
        assert installed.signature_ok is None
        assert installed.trust_tier == "community"
        assert installed.source_label == "Git"
        assert installed.last_install_error is None
        assert (tmp_path / "installed" / "git_demo").is_dir()
        assert not (tmp_path / "installed" / "git_demo.installing").exists()
        assert any("app.db.models.plugin" in item for item in installed.lint_warnings)
        assert any("httpx.get" in item and "timeout" in item for item in installed.lint_warnings)

    @pytest.mark.asyncio
    async def test_install_supports_github_tree_branch_url(self, monkeypatch, tmp_path):
        """GitHub tree/<branch> 分支链接安装时应 clone 仓库并指定分支。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        calls: list[tuple[str, ...]] = []

        async def _fake_clone(*args, **_kwargs):  # noqa: ANN001
            calls.append(tuple(args))
            plugin_dir = Path(args[-1])
            plugin_dir.mkdir(parents=True)
            _write_runtime_plugin(plugin_dir, key="git_demo", version="1.2.3")
            return ""

        monkeypatch.setattr(svc, "_run_git", _fake_clone)
        db = _FakeRemoteInstallDB()

        await svc.install(db, "https://github.com/example/git_demo/tree/feature/demo")

        assert calls[0] == (
            "clone",
            "--depth",
            "1",
            "--branch",
            "feature/demo",
            "--single-branch",
            "https://github.com/example/git_demo.git",
            str(tmp_path / "installed" / "git_demo.installing"),
        )
        assert db.installed_rows["git_demo"].source_url == "https://github.com/example/git_demo/tree/feature/demo"

    @pytest.mark.asyncio
    async def test_update_writes_installed_plugin(self, monkeypatch, tmp_path):
        """Git 更新成功后刷新 installed_plugin。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        plugin_dir = tmp_path / "installed" / "update_demo"
        (plugin_dir / ".git").mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            '{"name":"update_demo","display_name":"Update Demo","version":"1.0.0"}',
            encoding="utf-8",
        )
        (plugin_dir / "manifest.py").write_text(
            "from app.worker.plugins.manifest import Manifest\n"
            "MANIFEST = Manifest(key='update_demo', display_name='Update Demo', version='1.0.0')\n",
            encoding="utf-8",
        )
        (plugin_dir / "__init__.py").write_text("PLUGIN_CLASS = None\n", encoding="utf-8")
        (plugin_dir / "plugin.py").write_text("import requests\n", encoding="utf-8")

        async def _fake_pull(*_args, **_kwargs):  # noqa: ANN001
            (plugin_dir / "plugin.json").write_text(
                '{"name":"update_demo","display_name":"Update Demo","version":"1.1.0"}',
                encoding="utf-8",
            )
            (plugin_dir / "plugin.py").write_text(
                "import requests\nrequests.post('https://example.com')\n",
                encoding="utf-8",
            )
            return ""

        monkeypatch.setattr(svc, "_run_git", _fake_pull)
        db = _FakeRemoteInstallDB()
        db.installed_rows["update_demo"] = InstalledPlugin(
            key="update_demo",
            source="git",
            source_url="https://example.com/update_demo.git",
            installed_path=str(plugin_dir),
            version="1.0.0",
            enabled=False,
            trust_tier="community",
            source_label="Git",
        )

        row = await svc.update(db, "update_demo")

        installed = db.installed_rows["update_demo"]
        assert row.version == "1.1.0"
        assert installed.source == "git"
        assert installed.version == "1.1.0"
        assert installed.enabled is False
        assert installed.source_url == "https://example.com/update_demo.git"
        assert any("requests.post" in item and "timeout" in item for item in installed.lint_warnings)

    @pytest.mark.asyncio
    async def test_update_restores_missing_installed_plugin_dir(self, monkeypatch, tmp_path):
        """InstalledPlugin 切换后的旧记录缺目录时，更新应从 source_url 自动恢复。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        target = tmp_path / "installed" / "sum"
        clone_calls = 0

        async def _fake_clone(*args, **_kwargs):  # noqa: ANN001
            nonlocal clone_calls
            clone_calls += 1
            assert args[:3] == ("clone", "--depth", "1")
            repo_dir = Path(args[-1])
            _write_runtime_plugin(repo_dir / "sum", key="sum", version="1.1.11")
            return ""

        monkeypatch.setattr(svc, "_run_git", _fake_clone)
        db = _FakeRemoteInstallDB()
        db.installed_rows["sum"] = InstalledPlugin(
            key="sum",
            source="repo",
            source_url="https://example.com/plugin-repo.git",
            installed_path=str(target),
            version="1.1.10",
            enabled=True,
            trust_tier="community",
            source_label="Plugin Repo",
        )

        row = await svc.update(db, "sum")

        installed = db.installed_rows["sum"]
        assert clone_calls == 1
        assert target.is_dir()
        assert (target / "plugin.json").is_file()
        assert row.version == "1.1.11"
        assert installed.version == "1.1.11"
        assert installed.installed_path == str(target)
        assert installed.enabled is True
        assert not (tmp_path / "installed" / "sum.installing").exists()
        assert not (tmp_path / "installed" / "sum.bak-update").exists()

    @pytest.mark.asyncio
    async def test_uninstall_removes_installed_plugin_row(self, tmp_path, monkeypatch):
        """远程插件卸载时同步删除统一安装表，避免留下孤儿记录。"""
        monkeypatch.setattr(svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        plugin_dir = tmp_path / "installed" / "remove_demo"
        plugin_dir.mkdir(parents=True)
        db = _FakeRemoteInstallDB()
        db.remote_rows["remove_demo"] = RemotePlugin(
            name="remove_demo",
            display_name="Remove Demo",
            description="",
            author="",
            source_url="https://example.com/remove_demo.git",
            version="1.0.0",
            enabled=True,
        )
        db.installed_rows["remove_demo"] = InstalledPlugin(
            key="remove_demo",
            source="git",
            enabled=True,
        )
        db.features["remove_demo"] = Feature(key="remove_demo", display_name="Remove Demo", is_builtin=False)
        db.account_features.append(AccountFeature(account_id=1, feature_key="remove_demo", enabled=True))

        deleted = await svc.uninstall(db, "remove_demo")

        assert deleted is True
        assert "remove_demo" in db.remote_rows
        assert "remove_demo" not in db.installed_rows
        assert "remove_demo" not in db.features
        assert db.account_features == []
        assert not plugin_dir.exists()


class TestPluginRepoInstallFlow:
    """仓库/本地导入路径也要写统一安装表。"""

    @pytest.mark.asyncio
    async def test_install_plugin_from_repo_writes_installed_plugin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        repo_dir = tmp_path / "repo"
        _write_runtime_plugin(
            repo_dir / "repo_demo",
            key="repo_demo",
            version="2.0.0",
            extra_py="import httpx\nhttpx.get('https://example.com')\n",
        )

        async def _cached(_url: str, **_kwargs):
            return repo_dir

        monkeypatch.setattr(repo_svc, "_ensure_repo_cached_unlocked", _cached)
        db = _FakePluginRepoDB(PluginRepo(id=1, url="https://example.com/repo.git", name="Repo"))

        row = await repo_svc.install_plugin_from_repo(db, 1, "repo_demo", default_enabled=True)

        installed = db.installed_rows["repo_demo"]
        assert row.name == "repo_demo"
        assert installed.source == "repo"
        assert installed.source_url == "https://example.com/repo.git"
        assert installed.version == "2.0.0"
        assert installed.enabled is True
        assert installed.trust_tier == "community"
        assert installed.source_label == "Plugin Repo"
        assert (tmp_path / "installed" / "repo_demo").is_dir()
        assert not (tmp_path / "installed" / "repo_demo.installing").exists()
        assert any("httpx.get" in item and "timeout" in item for item in installed.lint_warnings)

    @pytest.mark.asyncio
    async def test_update_installed_plugins_from_repo_only_updates_newer_installed(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        repo_dir = tmp_path / "repo"
        _write_runtime_plugin(repo_dir / "update_demo", key="update_demo", version="1.2.0")
        _write_runtime_plugin(repo_dir / "same_demo", key="same_demo", version="1.0.0")
        _write_runtime_plugin(repo_dir / "new_demo", key="new_demo", version="9.9.9")
        _write_runtime_plugin(tmp_path / "installed" / "update_demo", key="update_demo", version="1.0.0")
        legacy_db = tmp_path / "installed" / "update_demo" / "runtime.sqlite3"
        legacy_writer = sqlite3.connect(legacy_db)
        legacy_writer.execute("PRAGMA journal_mode = WAL")
        legacy_writer.execute("CREATE TABLE question_bank(id INTEGER PRIMARY KEY, question TEXT NOT NULL)")
        legacy_writer.execute("INSERT INTO question_bank(question) VALUES ('更新前已存在')")
        legacy_writer.commit()
        legacy_json = tmp_path / "installed" / "update_demo" / "runtime.json"
        legacy_json.write_text('{"round": 7}', encoding="utf-8")

        async def _cached(_url: str, **_kwargs):
            return repo_dir

        monkeypatch.setattr(repo_svc, "_ensure_repo_cached_unlocked", _cached)
        db = _FakePluginRepoDB(PluginRepo(id=1, url="https://example.com/repo.git", name="Repo"))
        db.installed_rows["update_demo"] = InstalledPlugin(
            key="update_demo",
            source="repo",
            source_url="https://example.com/repo.git",
            installed_path=str(tmp_path / "installed" / "update_demo"),
            version="1.0.0",
            enabled=True,
            manifest_json={"name": "update_demo", "_telepilot_remote": {"default_enabled": True}},
        )
        db.installed_rows["same_demo"] = InstalledPlugin(
            key="same_demo",
            source="repo",
            source_url="https://example.com/repo.git",
            installed_path=str(tmp_path / "installed" / "same_demo"),
            version="1.0.0",
            enabled=False,
            manifest_json={"name": "same_demo"},
        )

        result = await repo_svc.update_installed_plugins_from_repo(db, 1)
        legacy_writer.execute("INSERT INTO question_bank(question) VALUES ('旧连接更新后继续写入')")
        legacy_writer.commit()
        legacy_writer.close()

        assert result.checked == 2
        assert result.update_available == 1
        assert result.updated == 1
        assert result.skipped == 1
        assert result.failed == 0
        assert [(item.name, item.status, item.from_version, item.to_version) for item in result.items] == [
            ("same_demo", "skipped", "1.0.0", "1.0.0"),
            ("update_demo", "updated", "1.0.0", "1.2.0"),
        ]
        updated = db.installed_rows["update_demo"]
        assert updated.version == "1.2.0"
        assert updated.source == "repo"
        assert updated.source_url == "https://example.com/repo.git"
        assert updated.enabled is True
        assert updated.manifest_json["version"] == "1.2.0"
        assert updated.manifest_json["_telepilot_remote"]["default_enabled"] is True
        assert updated.manifest_json["_telepilot_remote"]["runtime_revision_at"]
        assert "new_demo" not in db.installed_rows
        assert (tmp_path / "installed" / "update_demo" / "plugin.json").read_text(encoding="utf-8").find("1.2.0") >= 0
        migrated_db = tmp_path / "installed" / "_data" / "update_demo" / "runtime.sqlite3"
        assert migrated_db.is_file()
        assert (tmp_path / "installed" / "update_demo" / "runtime.sqlite3").is_symlink()
        with sqlite3.connect(migrated_db) as conn:
            assert [row[0] for row in conn.execute("SELECT question FROM question_bank ORDER BY id")] == [
                "更新前已存在",
                "旧连接更新后继续写入",
            ]
        migrated_json = tmp_path / "installed" / "_data" / "update_demo" / "runtime.json"
        assert migrated_json.read_text(encoding="utf-8") == '{"round": 7}'
        installed_json = tmp_path / "installed" / "update_demo" / "runtime.json"
        assert installed_json.is_symlink()
        installed_json.write_text('{"round": 8}', encoding="utf-8")
        assert migrated_json.read_text(encoding="utf-8") == '{"round": 8}'
        assert not (tmp_path / "installed" / "update_demo.installing").exists()
        assert not (tmp_path / "installed" / "update_demo.bak-update").exists()

    @pytest.mark.asyncio
    async def test_replace_installed_plugin_from_new_repo_preserves_config_and_other_plugins(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        repo_dir = tmp_path / "new-repo"
        _write_runtime_plugin(repo_dir / "ai_chat", key="ai_chat", version="2.0.0")
        install_dir = tmp_path / "installed" / "ai_chat"
        _write_runtime_plugin(install_dir, key="ai_chat", version="1.0.0")

        async def _cached(_url: str, **_kwargs):
            return repo_dir

        monkeypatch.setattr(repo_svc, "_ensure_repo_cached_unlocked", _cached)
        db = _FakePluginRepoDB(
            PluginRepo(id=2, url="https://example.com/new-plugins.git", name="New Repo")
        )
        db.installed_rows["ai_chat"] = InstalledPlugin(
            key="ai_chat",
            source="repo",
            source_url="https://example.com/deleted-plugins/tree/0.33.x",
            installed_path=str(install_dir),
            version="1.0.0",
            enabled=True,
            manifest_json={
                "name": "ai_chat",
                "_telepilot_remote": {"default_enabled": True},
            },
        )
        db.installed_rows["other_plugin"] = InstalledPlugin(
            key="other_plugin",
            source="repo",
            source_url="https://example.com/deleted-plugins/tree/0.33.x",
            installed_path=str(tmp_path / "installed" / "other_plugin"),
            version="9.9.9",
            enabled=False,
            manifest_json={"name": "other_plugin"},
        )
        db.features["ai_chat"] = Feature(
            key="ai_chat",
            display_name="AI Chat",
            is_builtin=False,
            version="1.0.0",
            manifest={"global_config": {"legacy": True}},
        )
        account_config = {"provider": "primary", "prompt": "保留"}
        account_feature = AccountFeature(
            account_id=1,
            feature_key="ai_chat",
            enabled=True,
            config=account_config,
            state="active",
            last_error=None,
        )
        global_config = PluginGlobalConfig(
            plugin_key="ai_chat",
            config={"model": "selected-model"},
        )
        db.account_features.append(account_feature)
        db.global_configs["ai_chat"] = global_config

        row = await repo_svc.install_plugin_from_repo(
            db,
            2,
            "ai_chat",
            replace_existing=True,
        )

        assert row.version == "2.0.0"
        assert db.installed_rows["ai_chat"].source_url == "https://example.com/new-plugins.git"
        assert db.installed_rows["ai_chat"].enabled is True
        assert db.installed_rows["ai_chat"].manifest_json["_telepilot_remote"]["default_enabled"] is True
        assert db.account_features == [account_feature]
        assert db.account_features[0].config == account_config
        assert db.global_configs["ai_chat"] is global_config
        assert db.global_configs["ai_chat"].config == {"model": "selected-model"}
        assert db.features["ai_chat"].manifest["global_config"] == {"legacy": True}
        assert db.installed_rows["other_plugin"].version == "9.9.9"
        assert db.installed_rows["other_plugin"].source_url.endswith("/0.33.x")
        assert '"2.0.0"' in (install_dir / "plugin.json").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_bulk_repo_update_does_not_take_over_same_name_from_another_repo(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        repo_dir = tmp_path / "new-repo"
        _write_runtime_plugin(repo_dir / "ai_chat", key="ai_chat", version="2.0.0")
        install_dir = tmp_path / "installed" / "ai_chat"
        _write_runtime_plugin(install_dir, key="ai_chat", version="1.0.0")

        async def _cached(_url: str, **_kwargs):
            return repo_dir

        monkeypatch.setattr(repo_svc, "_ensure_repo_cached_unlocked", _cached)
        db = _FakePluginRepoDB(
            PluginRepo(id=2, url="https://example.com/new-plugins.git", name="New Repo")
        )
        db.installed_rows["ai_chat"] = InstalledPlugin(
            key="ai_chat",
            source="repo",
            source_url="https://example.com/deleted-plugins/tree/0.33.x",
            installed_path=str(install_dir),
            version="1.0.0",
            enabled=True,
            manifest_json={"name": "ai_chat"},
        )

        result = await repo_svc.update_installed_plugins_from_repo(db, 2)

        assert result.checked == 0
        assert result.updated == 0
        assert db.installed_rows["ai_chat"].source_url.endswith("/0.33.x")
        assert '"1.0.0"' in (install_dir / "plugin.json").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_update_installed_plugins_from_repo_refreshes_stale_same_version_manifest(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        repo_dir = tmp_path / "repo"
        _write_runtime_plugin(repo_dir / "ten_half", key="ten_half", version="0.3.2")
        _write_runtime_plugin(tmp_path / "installed" / "ten_half", key="ten_half", version="0.3.2")

        async def _cached(_url: str, **_kwargs):
            return repo_dir

        monkeypatch.setattr(repo_svc, "_ensure_repo_cached_unlocked", _cached)
        db = _FakePluginRepoDB(PluginRepo(id=1, url="https://example.com/repo.git", name="Repo"))
        db.installed_rows["ten_half"] = InstalledPlugin(
            key="ten_half",
            source="repo",
            source_url="https://example.com/repo.git",
            installed_path=str(tmp_path / "installed" / "ten_half"),
            version="0.3.2",
            enabled=True,
            manifest_json={
                "name": "ten_half",
                "version": "0.3.1",
                "_telepilot_remote": {
                    "default_enabled": False,
                    "latest_version": "0.3.2",
                    "update_available": True,
                },
            },
        )

        result = await repo_svc.update_installed_plugins_from_repo(db, 1)

        assert result.checked == 1
        assert result.updated == 1
        assert result.skipped == 0
        assert [(item.name, item.status, item.message) for item in result.items] == [
            ("ten_half", "updated", "已同步元数据"),
        ]
        updated = db.installed_rows["ten_half"]
        assert updated.version == "0.3.2"
        assert updated.manifest_json["version"] == "0.3.2"
        assert updated.manifest_json["_telepilot_remote"]["update_available"] is False
        assert updated.manifest_json["_telepilot_remote"]["latest_version"] == "0.3.2"
        assert updated.manifest_json["_telepilot_remote"]["runtime_revision_at"]

    @pytest.mark.asyncio
    async def test_repo_update_link_failure_rolls_back_and_retry_keeps_database(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        repo_dir = tmp_path / "repo"
        _write_runtime_plugin(repo_dir / "retry_demo", key="retry_demo", version="1.1.0")
        install_dir = tmp_path / "installed" / "retry_demo"
        _write_runtime_plugin(install_dir, key="retry_demo", version="1.0.0")
        legacy_db = install_dir / "runtime.sqlite3"
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("CREATE TABLE events(value TEXT NOT NULL)")
            conn.execute("INSERT INTO events(value) VALUES ('before-failure')")

        async def _cached(_url: str, **_kwargs):
            return repo_dir

        monkeypatch.setattr(repo_svc, "_ensure_repo_cached_unlocked", _cached)
        db = _FakePluginRepoDB(PluginRepo(id=1, url="https://example.com/repo.git", name="Repo"))
        db.installed_rows["retry_demo"] = InstalledPlugin(
            key="retry_demo",
            source="repo",
            source_url="https://example.com/repo.git",
            installed_path=str(install_dir),
            version="1.0.0",
            enabled=True,
            manifest_json={"name": "retry_demo"},
        )
        real_attach = repo_svc._attach_legacy_plugin_sqlite_links
        monkeypatch.setattr(
            repo_svc,
            "_attach_legacy_plugin_sqlite_links",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("link failed")),
        )

        failed = await repo_svc.update_installed_plugins_from_repo(db, 1)

        assert failed.failed == 1
        assert "1.0.0" in (install_dir / "plugin.json").read_text(encoding="utf-8")
        assert not (tmp_path / "installed" / "retry_demo.bak-update").exists()
        with sqlite3.connect(legacy_db) as conn:
            conn.execute("INSERT INTO events(value) VALUES ('after-failure')")

        monkeypatch.setattr(repo_svc, "_attach_legacy_plugin_sqlite_links", real_attach)
        succeeded = await repo_svc.update_installed_plugins_from_repo(db, 1)

        assert succeeded.updated == 1
        persisted = tmp_path / "installed" / "_data" / "retry_demo" / "runtime.sqlite3"
        with sqlite3.connect(persisted) as conn:
            assert [row[0] for row in conn.execute("SELECT value FROM events ORDER BY rowid")] == [
                "before-failure",
                "after-failure",
            ]

    @pytest.mark.asyncio
    async def test_install_local_plugin_writes_installed_plugin(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        local_root = tmp_path / "local_imports"
        _write_runtime_plugin(local_root / "local_demo", key="local_demo", version="3.0.0")
        monkeypatch.setattr(repo_svc, "_local_import_root", lambda: local_root)
        db = _FakePluginRepoDB()

        row = await repo_svc.install_local_plugin(db, "local_demo", default_enabled=False)

        installed = db.installed_rows["local_demo"]
        assert row.name == "local_demo"
        assert installed.source == "local"
        assert installed.source_url == "local://local_imports/local_demo"
        assert installed.version == "3.0.0"
        assert installed.enabled is False
        assert installed.trust_tier == "local"
        assert installed.source_label == "Local"
        assert (tmp_path / "installed" / "local_demo").is_dir()
        assert not (tmp_path / "installed" / "local_demo.installing").exists()

    @pytest.mark.asyncio
    async def test_install_official_plugin_ignores_local_bundled_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        official_root = tmp_path / "official"
        _write_runtime_plugin(official_root / "official_demo", key="official_demo", version="4.0.0")
        monkeypatch.setattr(repo_svc, "_official_plugin_root", lambda: official_root)
        db = _FakePluginRepoDB()

        with pytest.raises(repo_svc.PluginNotInRepo):
            await repo_svc.install_official_plugin(db, "official_demo", default_enabled=False)

        assert "official_demo" not in db.installed_rows
        assert not (tmp_path / "installed" / "official_demo").exists()

    @pytest.mark.asyncio
    async def test_remote_official_repo_lists_only_official_tagged_plugins(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc, "_official_plugin_root", lambda: tmp_path / "empty-official")
        remote_root = tmp_path / "remote-official"
        _write_runtime_plugin(remote_root / "official_remote", key="official_remote", version="4.1.0")
        _write_runtime_plugin(remote_root / "community_remote", key="community_remote", version="1.0.0")
        (remote_root / "official_remote" / "plugin.json").write_text(
            '{"name":"official_remote","display_name":"Official Remote","version":"4.1.0","tags":["official"]}',
            encoding="utf-8",
        )

        async def _remote_root(*, force_refresh: bool = False):  # noqa: ARG001
            return remote_root

        monkeypatch.setattr(repo_svc, "_official_remote_plugin_root", _remote_root)

        rows = await repo_svc.list_official_plugins(_FakePluginRepoDB())

        assert [row.name for row in rows] == ["official_remote"]

    @pytest.mark.asyncio
    async def test_install_remote_official_plugin_writes_repo_record(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        monkeypatch.setattr(repo_svc.settings, "official_plugin_repo_url", "https://github.com/Anoyou/telebot-plugins")
        monkeypatch.setattr(repo_svc, "_official_plugin_root", lambda: tmp_path / "empty-official")
        remote_root = tmp_path / "remote-official"
        _write_runtime_plugin(remote_root / "official_remote", key="official_remote", version="4.1.0")
        (remote_root / "official_remote" / "plugin.json").write_text(
            '{"name":"official_remote","display_name":"Official Remote","version":"4.1.0","tags":["official"]}',
            encoding="utf-8",
        )

        async def _remote_root(*, force_refresh: bool = False):  # noqa: ARG001
            return remote_root

        monkeypatch.setattr(repo_svc, "_official_remote_plugin_root", _remote_root)
        db = _FakePluginRepoDB()

        row = await repo_svc.install_official_plugin(db, "official_remote", default_enabled=False)

        installed = db.installed_rows["official_remote"]
        assert row.name == "official_remote"
        assert installed.source == "repo"
        assert installed.source_url == "https://github.com/Anoyou/telebot-plugins"
        assert installed.version == "4.1.0"
        assert installed.signature_ok is None
        assert installed.trust_tier == "community"
        assert installed.source_label == "Plugin Repo"
        assert (tmp_path / "installed" / "official_remote").is_dir()

    @pytest.mark.asyncio
    async def test_install_remote_official_plugin_updates_existing_version(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        monkeypatch.setattr(repo_svc.settings, "official_plugin_repo_url", "https://github.com/Anoyou/telebot-plugins")
        monkeypatch.setattr(repo_svc, "_official_plugin_root", lambda: tmp_path / "empty-official")
        remote_root = tmp_path / "remote-official"
        _write_runtime_plugin(remote_root / "official_remote", key="official_remote", version="4.1.0")
        (remote_root / "official_remote" / "plugin.json").write_text(
            '{"name":"official_remote","display_name":"Official Remote","version":"4.1.0","tags":["official"]}',
            encoding="utf-8",
        )

        async def _remote_root(*, force_refresh: bool = False):  # noqa: ARG001
            return remote_root

        monkeypatch.setattr(repo_svc, "_official_remote_plugin_root", _remote_root)
        install_dir = tmp_path / "installed" / "official_remote"
        _write_runtime_plugin(install_dir, key="official_remote", version="4.0.0")
        db = _FakePluginRepoDB()
        db.installed_rows["official_remote"] = InstalledPlugin(
            key="official_remote",
            source="official",
            source_url="https://github.com/Anoyou/telebot-plugins",
            installed_path=str(install_dir),
            version="4.0.0",
            enabled=True,
        )

        row = await repo_svc.install_official_plugin(db, "official_remote", default_enabled=False)

        installed = db.installed_rows["official_remote"]
        assert row.version == "4.1.0"
        assert installed.source == "repo"
        assert installed.version == "4.1.0"
        assert installed.enabled is True
        assert (install_dir / "plugin.json").read_text(encoding="utf-8").count("4.1.0") == 1

    @pytest.mark.asyncio
    async def test_official_update_link_failure_restores_previous_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_svc.settings, "plugins_installed_dir", str(tmp_path / "installed"))
        monkeypatch.setattr(repo_svc, "_official_plugin_root", lambda: tmp_path / "empty-official")
        remote_root = tmp_path / "remote-official"
        _write_runtime_plugin(remote_root / "official_retry", key="official_retry", version="2.0.0")
        (remote_root / "official_retry" / "plugin.json").write_text(
            '{"name":"official_retry","display_name":"Official Retry","version":"2.0.0","tags":["official"]}',
            encoding="utf-8",
        )

        async def _remote_root(*, force_refresh: bool = False):  # noqa: ARG001
            return remote_root

        monkeypatch.setattr(repo_svc, "_official_remote_plugin_root", _remote_root)
        install_dir = tmp_path / "installed" / "official_retry"
        _write_runtime_plugin(install_dir, key="official_retry", version="1.0.0")
        (install_dir / "runtime.sqlite3").touch()
        db = _FakePluginRepoDB()
        db.installed_rows["official_retry"] = InstalledPlugin(
            key="official_retry",
            source="official",
            source_url="https://example.com/official.git",
            installed_path=str(install_dir),
            version="1.0.0",
            enabled=True,
        )
        monkeypatch.setattr(
            repo_svc,
            "_attach_legacy_plugin_sqlite_links",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("link failed")),
        )

        with pytest.raises(repo_svc.PluginRepoError, match="link failed"):
            await repo_svc.install_official_plugin(db, "official_retry", default_enabled=False)

        assert "1.0.0" in (install_dir / "plugin.json").read_text(encoding="utf-8")
        assert not (tmp_path / "installed" / "official_retry.bak-official").exists()


class TestPluginMetadataSchema:
    """PluginMetadataSchema Pydantic 校验测试。"""

    def test_valid_plugin_json(self):
        """合法的 plugin.json 应该通过校验。"""
        from app.services.remote_plugin_service import PluginMetadataSchema

        data = {
            "name": "my_plugin",
            "display_name": "My Plugin",
            "version": "1.0.0",
            "author": "Test Author",
            "permissions": ["send_message"],
        }
        schema = PluginMetadataSchema(**data)
        assert schema.name == "my_plugin"
        assert schema.version == "1.0.0"

    def test_key_as_fallback(self):
        """没有 name 时，key 作为备选。"""
        from app.services.remote_plugin_service import PluginMetadataSchema

        data = {"key": "fallback_key", "version": "1.0.0"}
        schema = PluginMetadataSchema(**data)
        assert schema.name == "fallback_key"

    def test_rejects_path_traversal_in_name(self):
        """name 中包含路径穿越字符必须被拒绝。"""
        from pydantic import ValidationError

        from app.services.remote_plugin_service import PluginMetadataSchema

        for bad_name in ("../etc", "foo/bar", "..", "foo\\bar", "foo\0bar"):
            with pytest.raises(ValidationError):
                PluginMetadataSchema(name=bad_name, version="1.0.0")

    def test_rejects_invalid_version(self):
        """版本号格式不正确必须被拒绝。"""
        from pydantic import ValidationError

        from app.services.remote_plugin_service import PluginMetadataSchema

        with pytest.raises(ValidationError):
            PluginMetadataSchema(name="test", version="latest")

    def test_author_length_limit(self):
        """author 字段长度限制。"""
        from pydantic import ValidationError

        from app.services.remote_plugin_service import PluginMetadataSchema

        with pytest.raises(ValidationError):
            PluginMetadataSchema(name="test", version="1.0.0", author="x" * 300)

    def test_interaction_fields_are_accepted(self):
        from app.services.remote_plugin_service import PluginMetadataSchema

        data = {
            "name": "game24",
            "version": "1.0.0",
            "category": "interactive",
            "interaction_profile": "session_game",
            "interaction_entries": [
                {
                    "key": "start_game24",
                    "session_scope": "chat",
                    "preserve_command_trigger": True,
                    "result_contract": {"send_via": ["interaction_bot", "userbot_reply"]},
                }
            ],
            "event_subscriptions": [
                {
                    "source": ["interaction_bot"],
                    "events": ["message", "callback_query"],
                    "scope": "all_allowed_chats",
                }
            ],
            "capabilities": {
                "telegram_native_raw": {
                    "enabled": True,
                    "reason": "风控需要原始数字 ID",
                }
            },
            "config_actions": [
                {
                    "key": "generate_knowledge_base",
                    "title": "获取并整理为题库",
                    "placement": "field:knowledge_bases",
                }
            ],
        }
        schema = PluginMetadataSchema(**data)
        assert schema.category == "interactive"
        assert schema.interaction_profile == "session_game"
        assert schema.interaction_entries[0]["key"] == "start_game24"
        assert schema.event_subscriptions[0]["events"] == ["message", "callback_query"]
        assert schema.capabilities["telegram_native_raw"]["enabled"] is True
        assert schema.config_actions[0]["key"] == "generate_knowledge_base"

    def test_manifest_json_from_remote_meta_keeps_interaction_fields(self):
        from app.services.remote_plugin_service import (
            PluginMetadata,
            _feature_manifest_from_meta,
            _manifest_json_from_remote_meta,
        )

        meta = PluginMetadata(
            name="dice_grid_hunt",
            display_name="九宫格猜骰",
            usage="发送“开始九宫格”启动玩法；点击按钮或回复答案继续。",
            version="1.0.0",
            category="interactive",
            interaction_profile="session_game",
            interaction_entries=[
                {
                    "key": "start_dice_grid_hunt",
                    "session_scope": "chat",
                    "preserve_command_trigger": True,
                }
            ],
            event_subscriptions=[
                {
                    "source": ["interaction_bot"],
                    "events": ["message", "callback_query"],
                    "scope": "all_allowed_chats",
                }
            ],
            capabilities={
                "telegram_native_raw": {
                    "enabled": True,
                    "reason": "风控需要原始数字 ID",
                }
            },
            config_actions=[
                {
                    "key": "generate_knowledge_base",
                    "title": "获取并整理为题库",
                    "placement": "field:knowledge_bases",
                }
            ],
        )

        manifest_json = _manifest_json_from_remote_meta(meta)
        feature_manifest = _feature_manifest_from_meta(meta)
        assert manifest_json["usage"] == "发送“开始九宫格”启动玩法；点击按钮或回复答案继续。"
        assert manifest_json["category"] == "interactive"
        assert manifest_json["interaction_profile"] == "session_game"
        assert manifest_json["config_actions"] == [
            {
                "key": "generate_knowledge_base",
                "title": "获取并整理为题库",
                "placement": "field:knowledge_bases",
            }
        ]
        assert manifest_json["interaction_entries"] == [
            {
                "key": "start_dice_grid_hunt",
                "session_scope": "chat",
                "preserve_command_trigger": True,
            }
        ]
        assert manifest_json["event_subscriptions"] == [
            {
                "source": ["interaction_bot"],
                "events": ["message", "callback_query"],
                "scope": "all_allowed_chats",
            }
        ]
        assert manifest_json["capabilities"]["telegram_native_raw"]["enabled"] is True
        assert feature_manifest is not None
        assert feature_manifest["config_actions"][0]["key"] == "generate_knowledge_base"


def test_lint_plugin_metadata_files_warns_on_bad_interaction_contract(tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        """
        {
          "name": "bad_interaction",
          "version": "1.0.0",
          "interaction_entries": [
            {
              "key": "start_bad",
              "session_scope": "group",
              "result_contract": {
                "send_via": ["interaction_bot", "unknown"]
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    warnings = svc.lint_plugin_metadata_files(plugin_dir)
    assert any("session_scope" in item for item in warnings)
    assert any("events" in item for item in warnings)
    assert any("result_contract.send_via" in item for item in warnings)
    assert any("interaction_profile" in item for item in warnings)


def test_lint_plugin_metadata_files_warns_event_subscription_consequences(tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        """
        {
          "name": "bad_subscription",
          "version": "1.0.0",
          "event_subscriptions": [
            {
              "events": ["message", "ghost_event"],
              "filters": {
                "keywords": ["开始"],
                "mystery": true
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    warnings = svc.lint_plugin_metadata_files(plugin_dir)

    assert any("filters 含未知 key: mystery" in item and "不会生效" in item for item in warnings)
    assert any("events 含未知类型: ghost_event" in item and "不会匹配任何当前支持的事件" in item for item in warnings)


def test_lint_plugin_metadata_files_rejects_removed_notice_channel(tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        """
        {
          "name": "good_interaction",
          "version": "1.0.0",
          "interaction_entries": [
            {
              "key": "start_good",
              "session_scope": "chat",
              "events": ["keyword", "message"],
              "interaction_profile": "session_game",
              "result_contract": {
                "send_via": ["bot", {"prefer": ["userbot", "notice"], "fallback": true}, "auto"]
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    warnings = svc.lint_plugin_metadata_files(plugin_dir)
    assert any("result_contract.send_via" in item for item in warnings)


def test_lint_plugin_metadata_files_accepts_standard_channel_aliases(tmp_path) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        """
        {
          "name": "good_interaction",
          "version": "1.0.0",
          "interaction_entries": [
            {
              "key": "start_good",
              "session_scope": "chat",
              "events": ["keyword", "message"],
              "interaction_profile": "session_game",
              "result_contract": {
                "send_via": ["bot", {"prefer": ["userbot"], "fallback": true}, "auto"]
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    warnings = svc.lint_plugin_metadata_files(plugin_dir)
    assert not any("result_contract.send_via" in item for item in warnings)
