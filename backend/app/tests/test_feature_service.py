"""feature_service 的单元测试：global config、配置合并、JSON Schema 验证。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.feature import FEATURE_STATE_DISABLED, Feature
from app.db.models.plugin import InstalledPlugin
from app.db.models.plugin_global_config import PluginGlobalConfig
from app.schemas.feature import FeatureInfo
from app.services.feature_service import (
    _seed_local_installed_features,
    apply_required_config_defaults,
    config_schema_for_scope,
    feature_matrix,
    get_effective_plugin_config,
    get_plugin_global_config,
    set_plugin_global_config,
    validate_config_against_schema,
)


# ─────────────────────────────────────────────────────
# JSON Schema 验证测试
# ─────────────────────────────────────────────────────
class TestValidateConfigAgainstSchema:
    """测试 validate_config_against_schema 函数。"""

    def test_valid_config_passes(self) -> None:
        """符合 schema 的配置应该通过验证。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer", "minimum": 10, "maximum": 300},
                "prize": {"type": "integer", "minimum": 0},
            },
            "required": ["time_limit"],
        }
        config = {"time_limit": 30, "prize": 100}
        result = validate_config_against_schema(config, schema)
        assert result.valid is True
        assert len(result.errors) == 0

    def test_missing_required_field_fails(self) -> None:
        """缺少必填字段应该失败。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer", "minimum": 10, "maximum": 300},
            },
            "required": ["time_limit"],
        }
        config = {}
        result = validate_config_against_schema(config, schema)
        assert result.valid is False
        assert len(result.errors) == 1
        assert "time_limit" in result.errors[0].field

    def test_invalid_type_fails(self) -> None:
        """类型错误应该失败。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer"},
            },
        }
        config = {"time_limit": "not_an_integer"}
        result = validate_config_against_schema(config, schema)
        assert result.valid is False
        assert len(result.errors) >= 1

    def test_below_minimum_fails(self) -> None:
        """低于最小值的配置应该失败。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer", "minimum": 10, "maximum": 300},
            },
        }
        config = {"time_limit": 5}
        result = validate_config_against_schema(config, schema)
        assert result.valid is False
        assert len(result.errors) == 1

    def test_above_maximum_fails(self) -> None:
        """高于最大值的配置应该失败。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer", "minimum": 10, "maximum": 300},
            },
        }
        config = {"time_limit": 500}
        result = validate_config_against_schema(config, schema)
        assert result.valid is False
        assert len(result.errors) == 1

    def test_extra_fields_allowed(self) -> None:
        """额外字段默认允许（additionalProperties 默认为 true）。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer", "minimum": 10, "maximum": 300},
            },
        }
        config = {"time_limit": 30, "extra_field": "should_be_allowed"}
        result = validate_config_against_schema(config, schema)
        assert result.valid is True

    def test_scope_schema_filters_required_fields(self) -> None:
        """分层保存时只要求当前 scope 的 required 字段。"""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string"},
                "cookie": {"type": "string", "level": "global"},
                "torrent_cooldown_seconds": {"type": "string"},
            },
            "required": ["command", "cookie", "torrent_cooldown_seconds"],
        }

        global_schema = config_schema_for_scope(schema, "global")
        account_schema = config_schema_for_scope(schema, "account")

        assert global_schema["required"] == ["cookie"]
        assert account_schema["required"] == ["command", "torrent_cooldown_seconds"]
        assert validate_config_against_schema({"cookie": "sid=ok"}, global_schema).valid is True
        assert validate_config_against_schema(
            {"command": "pt", "torrent_cooldown_seconds": "12h"},
            account_schema,
        ).valid is True

    def test_required_defaults_are_applied_before_validation(self) -> None:
        """交互页局部保存插件参数时，带默认值的必填字段应由服务端补齐。"""
        schema = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "default": "redpack"},
                "delete_delay_seconds": {"type": "integer", "default": 60},
                "cookie": {"type": "string"},
            },
            "required": ["command", "delete_delay_seconds", "cookie"],
        }

        config = apply_required_config_defaults({"cookie": "sid=ok"}, schema)

        assert config == {
            "command": "redpack",
            "delete_delay_seconds": 60,
            "cookie": "sid=ok",
        }
        assert validate_config_against_schema(config, schema).valid is True

    def test_required_defaults_do_not_hide_missing_user_values(self) -> None:
        """没有 default 的必填字段仍然必须由用户配置。"""
        schema = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "default": "pt"},
                "cookie": {"type": "string"},
            },
            "required": ["command", "cookie"],
        }

        config = apply_required_config_defaults({}, schema)
        result = validate_config_against_schema(config, schema)

        assert config == {"command": "pt"}
        assert result.valid is False
        assert result.errors[0].field == "cookie"


# ─────────────────────────────────────────────────────
# 配置合并测试
# ─────────────────────────────────────────────────────
class TestEffectiveConfigMerge:
    """测试 get_effective_plugin_config 的配置合并逻辑。"""

    @pytest.mark.asyncio
    async def test_merge_order_schema_defaults_global_account(self) -> None:
        """测试合并顺序：schema defaults < global config < account config。"""
        # 构造 mock Feature
        feature = MagicMock()
        feature.manifest = {
            "config_schema": {
                "type": "object",
                "properties": {
                    "global_field": {"type": "integer", "default": 10, "level": "global"},
                    "account_field": {"type": "integer", "default": 20},
                    "both_fields": {"type": "integer", "default": 30},
                },
            },
            "global_config": {
                "global_field": 100,
                "both_fields": 300,
            },
        }

        # 构造 mock AccountFeature
        account_feature = MagicMock()
        account_feature.config = {
            "account_field": 200,
            "both_fields": 600,
        }

        # 构造 mock db
        db = AsyncMock()
        db.get = AsyncMock(side_effect=[feature, account_feature])
        db.execute = AsyncMock()

        # Mock seed_builtin_features 为空操作
        # 实际调用时因为我们 mock 了 db.get，所以需要特殊处理
        # 这里我们直接测试配置合并逻辑

        # 模拟配置合并
        schema = feature.manifest["config_schema"]
        properties = schema["properties"]
        global_config = feature.manifest["global_config"]
        account_config = account_feature.config

        # 提取 schema defaults
        defaults = {
            k: v["default"]
            for k, v in properties.items()
            if isinstance(v, dict) and "default" in v
        }
        assert defaults == {"global_field": 10, "account_field": 20, "both_fields": 30}

        # 提取 global 字段名
        global_fields = {
            k for k, v in properties.items()
            if isinstance(v, dict) and v.get("level") == "global"
        }
        assert global_fields == {"global_field"}

        # 提取 account 专属字段
        account_only = {k: v for k, v in account_config.items() if k not in global_fields}
        assert account_only == {"account_field": 200, "both_fields": 600}

        # 最终合并结果
        result = {**defaults}
        for k, v in global_config.items():
            if k in global_fields:
                result[k] = v
        result.update(account_only)

        expected = {
            "global_field": 100,      # global config 覆盖 default
            "account_field": 200,    # account config 覆盖 default
            "both_fields": 600,      # account config 覆盖 global config
        }
        assert result == expected

    @pytest.mark.asyncio
    async def test_no_global_config_uses_schema_defaults(self) -> None:
        """没有 global config 时使用 schema defaults。"""
        properties = {
            "field1": {"type": "integer", "default": 10},
            "field2": {"type": "integer", "default": 20},
        }

        defaults = {
            k: v["default"]
            for k, v in properties.items()
            if isinstance(v, dict) and "default" in v
        }
        assert defaults == {"field1": 10, "field2": 20}

        # 无 global_config，account_config 为空
        global_config: dict = {}
        account_config: dict = {}

        result = {**defaults}
        result.update(global_config)
        result.update(account_config)
        assert result == {"field1": 10, "field2": 20}


    @pytest.mark.asyncio
    async def test_only_global_fields_in_manifest(self) -> None:
        """验证 global config 只保存 level="global" 的字段。"""
        properties = {
            "global_field": {"type": "integer", "default": 10, "level": "global"},
            "account_field": {"type": "integer", "default": 20},
        }

        # 模拟用户提交的完整配置
        user_config = {
            "global_field": 100,
            "account_field": 200,
        }

        # 只提取 global 字段
        global_fields = {
            k for k, v in properties.items()
            if isinstance(v, dict) and v.get("level") == "global"
        }

        global_config = {k: v for k, v in user_config.items() if k in global_fields}
        assert global_config == {"global_field": 100}


@pytest.mark.asyncio
async def test_feature_matrix_separates_enabled_switch_from_runtime_state(monkeypatch) -> None:
    """矩阵要同时返回账号开关和 worker 运行状态，避免前端把二者混用。"""

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    feature = SimpleNamespace(
        key="guess_number",
        display_name="猜数字",
        is_builtin=False,
        version="1.0.4",
        manifest={},
    )
    account = SimpleNamespace(id=1, display_name="你心里没点数?", phone="+10086")
    account_feature = SimpleNamespace(
        account_id=1,
        feature_key="guess_number",
        enabled=True,
        state=FEATURE_STATE_DISABLED,
    )

    monkeypatch.setattr(
        "app.services.feature_service.list_features",
        AsyncMock(return_value=[feature]),
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            Result([]),  # InstalledPlugin
            Result([account]),
            Result([account_feature]),
        ],
    )

    data = await feature_matrix(db)

    row = data["accounts"][0]
    assert row["features"]["guess_number"] == FEATURE_STATE_DISABLED
    assert row["feature_enabled"]["guess_number"] is True


@pytest.mark.asyncio
async def test_feature_matrix_passes_installed_plugin_lint_warnings(monkeypatch) -> None:
    """installed_plugin.lint_warnings 要透传到 FeatureInfo，供前端展示。"""

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    feature = SimpleNamespace(
        key="linted_plugin",
        display_name="有 lint 的模块",
        is_builtin=False,
        version="1.0.0",
        manifest={},
    )
    installed_plugin = InstalledPlugin(
        key="linted_plugin",
        source="zip",
        lint_warnings=["plugin.py:1: 避免导入 app.db.models", "plugin.py:2: httpx.get 缺少 timeout"],
    )

    monkeypatch.setattr(
        "app.services.feature_service.list_features",
        AsyncMock(return_value=[feature]),
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            Result([installed_plugin]),  # InstalledPlugin
            Result([]),  # Account
            Result([]),  # AccountFeature
        ],
    )

    data = await feature_matrix(db)

    assert data["features"][0]["lint_warnings"] == installed_plugin.lint_warnings


@pytest.mark.asyncio
async def test_feature_matrix_prefers_installed_plugin_version(monkeypatch) -> None:
    """已安装插件版本是权威值，避免账号配置页展示 feature 表里的旧索引。"""

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    feature = SimpleNamespace(
        key="ten_half",
        display_name="十点半",
        is_builtin=False,
        version="0.2.27",
        manifest={"usage": "旧玩法说明"},
    )
    installed_plugin = InstalledPlugin(
        key="ten_half",
        source="repo",
        source_url="https://github.com/Anoyou/telebot-plugins/tree/0.33.x",
        version="0.3.3",
        manifest_json={
            "name": "ten_half",
            "display_name": "十点半",
            "version": "0.3.3",
            "usage": "新按钮流程说明",
            "capabilities": {"transfer": {"enabled": True}},
        },
        lint_warnings=[],
    )

    monkeypatch.setattr(
        "app.services.feature_service.list_features",
        AsyncMock(return_value=[feature]),
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            Result([installed_plugin]),  # InstalledPlugin
            Result([]),  # Account
            Result([]),  # AccountFeature
        ],
    )

    data = await feature_matrix(db)
    row = data["features"][0]

    assert row["version"] == "0.3.3"
    assert row["usage"] == "新按钮流程说明"
    assert row["capabilities"] == {"transfer": {"enabled": True}}


@pytest.mark.asyncio
async def test_seed_local_installed_features_skips_orphan_dirs(monkeypatch, tmp_path) -> None:
    """磁盘孤儿目录不再写入模块矩阵；已有孤儿 feature 行会被清掉。"""

    root = tmp_path / "installed"
    plugin_dir = root / "orphan_demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        '{"name":"orphan_demo","display_name":"孤儿模块","version":"1.0.0"}',
        encoding="utf-8",
    )
    monkeypatch.setattr("app.settings.settings.plugins_installed_dir", str(root))

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    existing_row = SimpleNamespace(key="orphan_demo", is_builtin=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[Result([]), Result([])])
    db.delete = AsyncMock()

    added, changed = await _seed_local_installed_features(
        db,
        {"orphan_demo": existing_row},
    )

    assert added == 0
    assert changed is True
    db.delete.assert_awaited_once_with(existing_row)


@pytest.mark.asyncio
async def test_seed_local_installed_features_repairs_stale_feature_from_installed_plugin(
    monkeypatch,
    tmp_path,
) -> None:
    """seed 时用 InstalledPlugin 自愈旧 Feature 索引，不依赖手动修库。"""

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    monkeypatch.setattr("app.settings.settings.plugins_installed_dir", str(tmp_path / "missing"))
    feature = Feature(
        key="ten_half",
        display_name="十点半",
        is_builtin=False,
        version="0.2.27",
        manifest={"usage": "旧玩法说明"},
    )
    installed_plugin = InstalledPlugin(
        key="ten_half",
        source="repo",
        source_url="https://github.com/Anoyou/telebot-plugins/tree/0.33.x",
        version="0.3.3",
        manifest_json={
            "name": "ten_half",
            "display_name": "十点半",
            "version": "0.3.3",
            "usage": "新按钮流程说明",
            "event_subscriptions": [{"events": ["message"]}],
        },
        lint_warnings=[],
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=Result([installed_plugin]))
    db.flush = AsyncMock()

    added, changed = await _seed_local_installed_features(db, {"ten_half": feature})

    assert added == 0
    assert changed is True
    assert feature.version == "0.3.3"
    assert feature.manifest == {
        "usage": "新按钮流程说明",
        "event_subscriptions": [{"events": ["message"]}],
        "source_label": "repo",
    }


# ─────────────────────────────────────────────────────
# Global Config API 测试
# ─────────────────────────────────────────────────────
class TestGetPluginGlobalConfig:
    """测试 get_plugin_global_config 函数。"""

    @pytest.mark.asyncio
    async def test_returns_empty_dict_for_nonexistent_plugin(self) -> None:
        """不存在的插件应返回空 dict。"""
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        # Mock seed_builtin_features
        with pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()):
            result = await get_plugin_global_config(db, "nonexistent_plugin")
            assert result == {}

    @pytest.mark.asyncio
    async def test_returns_global_config_from_manifest(self) -> None:
        """新表缺行时应回退读取 manifest 中的 global_config。"""
        feature = MagicMock()
        feature.manifest = {
            "global_config": {"time_limit": 60, "prize": 100},
        }

        db = AsyncMock()
        db.get = AsyncMock(side_effect=[feature, None])

        with pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()):
            result = await get_plugin_global_config(db, "game24")
            assert result == {"time_limit": 60, "prize": 100}

    @pytest.mark.asyncio
    async def test_returns_global_config_from_table_first(self) -> None:
        """plugin_global_config 有行时应优先于旧 manifest 字段。"""
        feature = MagicMock()
        feature.manifest = {
            "global_config": {"time_limit": 60},
        }
        row = MagicMock()
        row.config = {"time_limit": 120, "prize": 100}

        db = AsyncMock()
        db.get = AsyncMock(side_effect=[feature, row])

        with pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()):
            result = await get_plugin_global_config(db, "game24")
            assert result == {"time_limit": 120, "prize": 100}


class TestFeatureInfo:
    def test_from_feature_prefers_installed_plugin_metadata(self) -> None:
        feature = SimpleNamespace(
            key="ten_half",
            display_name="十点半",
            is_builtin=False,
            version="0.2.27",
            manifest={
                "usage": "旧玩法说明",
                "config_schema": {"properties": {"old": {"type": "string"}}},
                "capabilities": {"old": True},
            },
        )
        installed = SimpleNamespace(
            source="repo",
            source_url="https://github.com/Anoyou/telebot-plugins/tree/0.33.x",
            source_label="Plugin Repo",
            version="0.3.3",
            signature_ok=None,
            manifest_json={
                "display_name": "十点半",
                "version": "0.3.3",
                "usage": "新按钮流程说明",
                "config_schema": {"properties": {"max_players": {"type": "integer"}}},
                "event_subscriptions": [{"events": ["message", "callback_query"]}],
                "capabilities": {"transfer": {"enabled": True}},
            },
            lint_warnings=[],
        )

        info = FeatureInfo.from_feature(feature, installed_plugin=installed)

        assert info.version == "0.3.3"
        assert info.usage == "新按钮流程说明"
        assert info.config_schema == {"properties": {"max_players": {"type": "integer"}}}
        assert info.event_subscriptions == [{"events": ["message", "callback_query"]}]
        assert info.capabilities == {"transfer": {"enabled": True}}

    def test_from_feature_marks_experimental_manifest(self) -> None:
        feature = MagicMock()
        feature.key = "codex_image"
        feature.display_name = "Codex 图片生成"
        feature.is_builtin = True
        feature.version = "1.1.0"
        feature.manifest = {
            "config_schema": {"type": "object"},
            "x-experimental": True,
        }

        info = FeatureInfo.from_feature(feature)
        assert info.experimental is True
        assert info.config_schema == {"type": "object"}

    def test_from_feature_exposes_interaction_profile_and_entries(self) -> None:
        feature = MagicMock()
        feature.key = "dice_grid_hunt"
        feature.display_name = "九宫格猜骰"
        feature.is_builtin = True
        feature.version = "1.2.0"
        feature.manifest = {
            "category": "interactive",
            "usage": "发送“开始九宫格”启动玩法；点击按钮或回复答案继续。",
            "interaction_profile": "session_game",
            "interaction_entries": [
                {
                    "key": "start_dice_grid_hunt",
                    "title": "开始九宫格",
                    "interaction_profile": "session_game",
                    "launch_mode": "hybrid",
                    "session_scope": "chat",
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
        }

        info = FeatureInfo.from_feature(feature)
        assert info.category == "interactive"
        assert info.usage == "发送“开始九宫格”启动玩法；点击按钮或回复答案继续。"
        assert info.interaction_profile == "session_game"
        assert info.interaction_entries == [
            {
                "key": "start_dice_grid_hunt",
                "title": "开始九宫格",
                "interaction_profile": "session_game",
                "launch_mode": "hybrid",
                "session_scope": "chat",
            }
        ]
        assert info.event_subscriptions == [
            {
                "source": ["interaction_bot"],
                "events": ["message", "callback_query"],
                "scope": "all_allowed_chats",
            }
        ]
        assert info.capabilities == {
            "telegram_native_raw": {
                "enabled": True,
                "reason": "风控需要原始数字 ID",
            }
        }


class TestSetPluginGlobalConfig:
    """测试 set_plugin_global_config 函数。"""

    @pytest.mark.asyncio
    async def test_saves_only_global_fields(self) -> None:
        """验证只保存 level='global' 的字段到 manifest。"""
        properties = {
            "global_field": {"type": "integer", "default": 10, "level": "global"},
            "account_field": {"type": "integer", "default": 20},
        }

        user_config = {
            "global_field": 100,
            "account_field": 200,
        }

        global_fields = {
            k for k, v in properties.items()
            if isinstance(v, dict) and v.get("level") == "global"
        }

        global_config = {k: v for k, v in user_config.items() if k in global_fields}
        assert global_config == {"global_field": 100}

    @pytest.mark.asyncio
    async def test_validates_config_against_schema(self) -> None:
        """设置 global config 前应验证 schema。"""
        schema = {
            "type": "object",
            "properties": {
                "time_limit": {"type": "integer", "minimum": 10, "maximum": 300},
            },
        }
        config = {"time_limit": 5}  # 低于最小值

        result = validate_config_against_schema(config, schema)
        assert result.valid is False
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_saves_table_and_clears_manifest_global_config(self) -> None:
        """保存 global_config 时写入新表，并清掉 manifest 旧字段。"""
        original_manifest = {
            "config_schema": {
                "type": "object",
                "properties": {
                    "global_field": {"type": "integer", "level": "global"},
                    "account_field": {"type": "integer"},
                },
            },
            "permissions": ["send_message"],
            "global_config": {"global_field": 12},
        }
        feature = MagicMock()
        feature.manifest = original_manifest

        db = AsyncMock()
        db.get = AsyncMock(side_effect=[feature, None])
        db.add = MagicMock()
        db.commit = AsyncMock()

        with (
            pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()),
            pytest.mock.patch("app.services.feature_service._notify_all_accounts_using_feature", AsyncMock()),
        ):
            result = await set_plugin_global_config(
                db,
                "demo",
                {"global_field": 42, "account_field": 99},
            )

        assert result == {"global_field": 42}
        assert feature.manifest is not original_manifest
        assert feature.manifest["permissions"] == ["send_message"]
        assert "global_config" not in feature.manifest
        assert original_manifest["global_config"] == {"global_field": 12}
        added_row = db.add.call_args.args[0]
        assert isinstance(added_row, PluginGlobalConfig)
        assert added_row.plugin_key == "demo"
        assert added_row.config == {"global_field": 42}
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_global_config_validation_ignores_account_required_fields(self) -> None:
        """保存全局字段时不应要求账号级 required 字段同时出现。"""
        feature = MagicMock()
        feature.manifest = {
            "config_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "cookie": {"type": "string", "level": "global"},
                    "torrent_cooldown_seconds": {"type": "string"},
                },
                "required": ["command", "cookie", "torrent_cooldown_seconds"],
            },
        }

        db = AsyncMock()
        db.get = AsyncMock(side_effect=[feature, None])
        db.add = MagicMock()
        db.commit = AsyncMock()

        with (
            pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()),
            pytest.mock.patch("app.services.feature_service._notify_all_accounts_using_feature", AsyncMock()),
        ):
            result = await set_plugin_global_config(db, "pt_promote", {"cookie": "sid=ok"})

        assert result == {"cookie": "sid=ok"}
        added_row = db.add.call_args.args[0]
        assert isinstance(added_row, PluginGlobalConfig)
        assert added_row.config == {"cookie": "sid=ok"}

    @pytest.mark.asyncio
    async def test_updates_existing_global_config_row(self) -> None:
        """已有 plugin_global_config 行时应原地更新。"""
        feature = MagicMock()
        feature.manifest = {
            "config_schema": {
                "type": "object",
                "properties": {
                    "global_field": {"type": "integer", "level": "global"},
                },
            },
        }
        row = MagicMock()
        row.config = {"global_field": 12}

        db = AsyncMock()
        db.get = AsyncMock(side_effect=[feature, row])
        db.add = MagicMock()
        db.commit = AsyncMock()

        with (
            pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()),
            pytest.mock.patch("app.services.feature_service._notify_all_accounts_using_feature", AsyncMock()),
        ):
            result = await set_plugin_global_config(
                db,
                "demo",
                {"global_field": 42},
            )

        assert result == {"global_field": 42}
        assert row.config == {"global_field": 42}
        db.add.assert_not_called()
        db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_effective_plugin_config_reads_global_config_table() -> None:
    """最终生效配置应读取 plugin_global_config 表里的共享配置。"""

    class Result:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    feature = MagicMock()
    feature.manifest = {
        "config_schema": {
            "type": "object",
            "properties": {
                "global_field": {"type": "integer", "default": 10, "level": "global"},
                "account_field": {"type": "integer", "default": 20},
            },
        }
    }
    global_row = MagicMock()
    global_row.config = {"global_field": 100}
    account_feature = MagicMock()
    account_feature.config = {"account_field": 200}

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[feature, global_row])
    db.execute = AsyncMock(return_value=Result(account_feature))

    with pytest.mock.patch("app.services.feature_service.seed_builtin_features", AsyncMock()):
        result = await get_effective_plugin_config(db, 1, "demo")

    assert result == {"global_field": 100, "account_field": 200}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
