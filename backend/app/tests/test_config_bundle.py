"""Config Bundle 导出 / dry-run 单元测试。"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from app.api import config_bundle as config_bundle_api
from app.db.models.feature import Feature
from app.schemas.config_bundle import (
    ConfigBundleCommandLinkItem,
    ConfigBundleExport,
    ConfigBundleFeatureItem,
    ConfigBundleIgnoredPeerItem,
    ConfigBundleRuleItem,
    ConfigBundleSourceAccount,
)
from app.services import platform_capabilities
from app.services.config_bundle_service import (
    BundleConfirmError,
    BundleTooLarge,
    apply_bundle_confirm,
    assert_bundle_size,
    build_config_bundle,
    build_preview_context_digest,
    build_preview_signature,
    compare_bundles,
)


def test_build_config_bundle_redacts_sensitive_fields() -> None:
    account = SimpleNamespace(id=1, display_name="alpha", phone="+100")
    feature_rows = [
        SimpleNamespace(
            feature_key="codex_image",
            enabled=True,
            config={
                "access_token": "tok",
                "safe": 1,
                "nested": {"bot_token": "secret", "keep": 2},
            },
        )
    ]
    rule_rows = [
        SimpleNamespace(
            feature_key="scheduler",
            name="night",
            enabled=True,
            priority=10,
            config={"chat_id": 123, "provider": "p1", "command": "send", "secret": "hide"},
        )
    ]
    command_link_rows = [
        (
            SimpleNamespace(template_id=7),
            SimpleNamespace(id=7, name="ping", aliases=["p"], type="reply_text"),
        )
    ]
    ignored_peer_rows = [
        SimpleNamespace(peer_id=-100123, peer_kind="supergroup", peer_label="测试群")
    ]

    bundle = build_config_bundle(
        account,
        feature_rows,
        rule_rows,
        command_link_rows,
        ignored_peer_rows,
    )

    assert bundle.source_account.id == 1
    assert bundle.features["codex_image"].config == {"safe": 1, "nested": {"keep": 2}}
    assert bundle.rules[0].config == {"chat_id": 123, "provider": "p1", "command": "send"}
    assert bundle.command_links[0].template_name == "ping"
    assert bundle.ignored_peers[0].peer_id == -100123
    assert bundle.ignored_peers[0].peer_label == "测试群"


def test_preview_signature_changes_when_target_snapshot_changes() -> None:
    source = ConfigBundleExport(source_account=ConfigBundleSourceAccount(id=1, label="src"))
    body = source.model_dump_json().encode("utf-8")
    empty_target = ConfigBundleExport(source_account=ConfigBundleSourceAccount(id=2, label="dst"))
    changed_target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        features={
            "scheduler": ConfigBundleFeatureItem(
                feature_key="scheduler",
                enabled=True,
                config={"timezone": "Asia/Shanghai"},
            )
        },
    )
    common = {
        "account_id": 2,
        "file_content": body,
        "apply_conflicts": False,
        "confirm_chat_id_conflicts": False,
    }
    first = build_preview_signature(
        **common,
        preview_context_digest=build_preview_context_digest(
            target=empty_target,
            available_features={"scheduler": "Scheduler"},
            available_command_templates={},
        ),
    )
    second = build_preview_signature(
        **common,
        preview_context_digest=build_preview_context_digest(
            target=changed_target,
            available_features={"scheduler": "Scheduler"},
            available_command_templates={},
        ),
    )
    assert first != second


def test_assert_bundle_size_rejects_over_1mb() -> None:
    bundle = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="alpha"),
        features={
            "large": ConfigBundleFeatureItem(
                feature_key="large",
                enabled=True,
                config={"blob": "x" * 1_050_000},
            )
        },
        rules=[],
        command_links=[],
    )

    with pytest.raises(BundleTooLarge) as exc_info:
        assert_bundle_size(bundle)
    assert exc_info.value.size_bytes > 1_048_576


def test_compare_bundles_returns_add_skip_and_conflict() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        features={
            "auto_reply": ConfigBundleFeatureItem(
                feature_key="auto_reply",
                enabled=True,
                config={"scope": "all", "chat_id": 1},
            ),
            "forward": ConfigBundleFeatureItem(
                feature_key="forward",
                enabled=True,
                config={"provider": "A"},
            ),
        },
        rules=[
            ConfigBundleRuleItem(
                feature_key="auto_reply",
                name="hello",
                enabled=True,
                priority=100,
                config={"chat_id": 111, "provider": "x", "command": "say"},
            ),
            ConfigBundleRuleItem(
                feature_key="scheduler",
                name="job",
                enabled=True,
                priority=50,
                config={"command": "run"},
            ),
        ],
        command_links=[
            ConfigBundleCommandLinkItem(
                template_id=1,
                template_name="reply_hello",
                aliases=[],
                type="reply_text",
                enabled=True,
            ),
            ConfigBundleCommandLinkItem(
                template_id=2,
                template_name="missing_cmd",
                aliases=[],
                type="reply_text",
                enabled=True,
            ),
            ConfigBundleCommandLinkItem(
                template_id=3,
                template_name="changed_cmd",
                aliases=["a"],
                type="reply_text",
                enabled=True,
            ),
        ],
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        features={
            "auto_reply": ConfigBundleFeatureItem(
                feature_key="auto_reply",
                enabled=True,
                config={"scope": "all", "chat_id": 1},
            ),
            "forward": ConfigBundleFeatureItem(
                feature_key="forward",
                enabled=True,
                config={"provider": "B"},
            ),
        },
        rules=[
            ConfigBundleRuleItem(
                feature_key="auto_reply",
                name="hello",
                enabled=True,
                priority=100,
                config={"chat_id": 111, "provider": "x", "command": "say"},
            )
        ],
        command_links=[
            ConfigBundleCommandLinkItem(
                template_id=1,
                template_name="reply_hello",
                aliases=[],
                type="reply_text",
                enabled=True,
            )
        ],
    )

    report = compare_bundles(
        source,
        target,
        available_features={
            "auto_reply": "Auto Reply",
            "forward": "Forward",
            "scheduler": "Scheduler",
        },
        available_command_templates={
            "reply_hello": {"template_name": "reply_hello", "aliases": [], "type": "reply_text"},
            "changed_cmd": {"template_name": "changed_cmd", "aliases": ["b"], "type": "reply_text"},
        },
    )

    assert report.counts.add == 1
    assert report.counts.skip == 3
    assert report.counts.conflict == 3
    assert any(i.entity == "feature" and i.key == "forward" and i.action == "conflict" for i in report.items)
    assert any(i.entity == "rule" and i.key == "scheduler:job" and i.action == "add" for i in report.items)
    assert any(i.entity == "command_link" and i.key == "missing_cmd" and i.action == "conflict" for i in report.items)


def test_compare_bundles_includes_ignored_peers() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        ignored_peers=[
            ConfigBundleIgnoredPeerItem(peer_id=-1001, peer_kind="supergroup", peer_label="同名群"),
            ConfigBundleIgnoredPeerItem(peer_id=-1002, peer_kind="supergroup", peer_label="新增群"),
            ConfigBundleIgnoredPeerItem(peer_id=-1003, peer_kind="channel", peer_label="改名频道"),
        ],
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        ignored_peers=[
            ConfigBundleIgnoredPeerItem(peer_id=-1001, peer_kind="supergroup", peer_label="同名群"),
            ConfigBundleIgnoredPeerItem(peer_id=-1003, peer_kind="channel", peer_label="旧频道名"),
        ],
    )

    report = compare_bundles(
        source,
        target,
        available_features={},
        available_command_templates={},
    )

    assert report.counts.add == 1
    assert report.counts.skip == 1
    assert report.counts.conflict == 1
    assert any(i.entity == "ignored_peer" and i.key == "-1002" and i.action == "add" for i in report.items)
    assert any(
        i.entity == "ignored_peer" and i.key == "-1003" and i.action == "conflict" and i.fields == ["peer_label"]
        for i in report.items
    )


@pytest.mark.asyncio
async def test_available_feature_map_is_read_only_without_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rows = [SimpleNamespace(key="auto_reply", display_name="Auto Reply")]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return fake_rows

    db = SimpleNamespace(execute=AsyncMock(return_value=_Result()))
    select_spy = Mock(return_value=Feature)
    monkeypatch.setattr(config_bundle_api, "select", select_spy)
    await config_bundle_api._available_feature_map(db)
    select_spy.assert_called_once_with(Feature)


@pytest.mark.asyncio
async def test_dry_run_rejects_oversize_before_reading_body(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace()
    user = SimpleNamespace()
    file = UploadFile(file=BytesIO(b"{}"), filename="bundle.json", headers=Headers({}))
    file.read = AsyncMock(side_effect=AssertionError("should not read body"))  # type: ignore[method-assign]
    request = SimpleNamespace(headers={"content-length": "1048577"})

    with pytest.raises(HTTPException) as exc_info:
        await config_bundle_api.dry_run_config_bundle(
            aid=1,
            db=db,
            _user=user,
            request=request,  # type: ignore[arg-type]
            file=file,
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["code"] == "BUNDLE_TOO_LARGE"


@pytest.mark.asyncio
async def test_confirm_rejects_oversize_before_reading_body() -> None:
    db = SimpleNamespace()
    user = SimpleNamespace()
    file = UploadFile(file=BytesIO(b"{}"), filename="bundle.json", headers=Headers({}))
    file.read = AsyncMock(side_effect=AssertionError("should not read body"))  # type: ignore[method-assign]
    request = SimpleNamespace(headers={"content-length": "1048577"})

    with pytest.raises(HTTPException) as exc_info:
        await config_bundle_api.confirm_config_bundle(
            aid=1,
            db=db,
            user=user,
            request=request,  # type: ignore[arg-type]
            file=file,
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["code"] == "BUNDLE_TOO_LARGE"


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarResult(self._rows)


class _FakeDB:
    def __init__(self, templates):
        self.templates = templates
        self.execute = AsyncMock(return_value=_FakeExecuteResult(templates))
        self.added = []

    def add(self, obj):
        self.added.append(obj)


@pytest.mark.asyncio
async def test_apply_bundle_confirm_requires_chat_id_ack() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        features={},
        rules=[
            ConfigBundleRuleItem(
                feature_key="auto_reply",
                name="hello",
                enabled=True,
                priority=10,
                config={"chat_id": 123},
            )
        ],
        command_links=[],
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        features={},
        rules=[
            ConfigBundleRuleItem(
                feature_key="auto_reply",
                name="hello",
                enabled=True,
                priority=10,
                config={"chat_id": 999},
            )
        ],
        command_links=[],
    )
    dry_run = compare_bundles(
        source,
        target,
        available_features={"auto_reply": "Auto Reply"},
        available_command_templates={},
    )
    db = _FakeDB([])

    with pytest.raises(BundleConfirmError) as exc_info:
        await apply_bundle_confirm(
            db,
            account_id=2,
            source=source,
            dry_run=dry_run,
            available_command_templates={},
            apply_conflicts=True,
            confirm_chat_id_conflicts=False,
        )
    assert exc_info.value.code == "CHAT_ID_CONFIRM_REQUIRED"


@pytest.mark.asyncio
async def test_apply_bundle_confirm_only_add_when_conflicts_disabled() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        features={
            "forward": ConfigBundleFeatureItem(
                feature_key="forward",
                enabled=True,
                config={"mode": "copy"},
            )
        },
        rules=[],
        command_links=[],
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        features={
            "forward": ConfigBundleFeatureItem(
                feature_key="forward",
                enabled=False,
                config={"mode": "move"},
            )
        },
        rules=[],
        command_links=[],
    )
    dry_run = compare_bundles(
        source,
        target,
        available_features={"forward": "Forward"},
        available_command_templates={},
    )
    db = _FakeDB([])
    imported, skipped, conflicts, _warnings = await apply_bundle_confirm(
        db,
        account_id=2,
        source=source,
        dry_run=dry_run,
        available_command_templates={},
        apply_conflicts=False,
        confirm_chat_id_conflicts=False,
    )
    assert imported == 0
    assert skipped == 0
    assert conflicts == 1


@pytest.mark.asyncio
async def test_apply_bundle_confirm_preflights_enabled_plugin_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        features={
            "lottery_plus": ConfigBundleFeatureItem(
                feature_key="lottery_plus",
                enabled=True,
                config={},
            )
        },
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst")
    )
    dry_run = compare_bundles(
        source,
        target,
        available_features={"lottery_plus": "Lottery Plus"},
        available_command_templates={},
    )
    db = _FakeDB([])
    blocked = platform_capabilities.PluginCapabilityBlocked(
        "lottery_plus", "ledger", "值守预设"
    )
    ensure = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(platform_capabilities, "ensure_plugin_capabilities", ensure)

    with pytest.raises(platform_capabilities.PluginCapabilityBlocked):
        await apply_bundle_confirm(
            db,
            account_id=2,
            source=source,
            dry_run=dry_run,
            available_command_templates={},
            apply_conflicts=False,
            confirm_chat_id_conflicts=False,
            web_user_id=73,
        )

    ensure.assert_awaited_once_with(
        db,
        "lottery_plus",
        triggered_by_user_id=73,
    )
    assert db.added == []


@pytest.mark.asyncio
async def test_apply_bundle_confirm_imports_ignored_peers() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        ignored_peers=[
            ConfigBundleIgnoredPeerItem(
                peer_id=-100222,
                peer_kind="supergroup",
                peer_label="搬家的群",
            )
        ],
    )
    target = ConfigBundleExport(source_account=ConfigBundleSourceAccount(id=2, label="dst"))
    dry_run = compare_bundles(
        source,
        target,
        available_features={},
        available_command_templates={},
    )
    db = _FakeDB([])

    imported, skipped, conflicts, warnings = await apply_bundle_confirm(
        db,
        account_id=2,
        source=source,
        dry_run=dry_run,
        available_command_templates={},
        apply_conflicts=False,
        confirm_chat_id_conflicts=False,
    )

    assert (imported, skipped, conflicts, warnings) == (1, 0, 0, [])
    added = db.added[0]
    assert added.account_id == 2
    assert added.peer_id == -100222
    assert added.peer_label == "搬家的群"


@pytest.mark.asyncio
async def test_apply_bundle_confirm_applies_conflicts_with_ack() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        features={},
        rules=[
            ConfigBundleRuleItem(
                feature_key="scheduler",
                name="night",
                enabled=True,
                priority=30,
                config={"chat_id": -1001, "command": "run"},
            )
        ],
        command_links=[],
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        features={},
        rules=[
            ConfigBundleRuleItem(
                feature_key="scheduler",
                name="night",
                enabled=True,
                priority=30,
                config={"chat_id": -1002, "command": "run"},
            )
        ],
        command_links=[],
    )
    dry_run = compare_bundles(
        source,
        target,
        available_features={"scheduler": "Scheduler"},
        available_command_templates={},
    )
    db = _FakeDB([])
    imported, skipped, conflicts, warnings = await apply_bundle_confirm(
        db,
        account_id=2,
        source=source,
        dry_run=dry_run,
        available_command_templates={},
        apply_conflicts=True,
        confirm_chat_id_conflicts=True,
    )
    assert imported == 1
    assert skipped == 0
    assert conflicts == 0
    assert warnings == []
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_apply_bundle_confirm_requires_chat_id_ack_for_nested_target_chat_id() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        rules=[
            ConfigBundleRuleItem(
                feature_key="scheduler",
                name="nested-chat",
                enabled=True,
                priority=10,
                config={"action": {"target_chat_id": -10001, "cmd": "run"}},
            )
        ],
    )
    target = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=2, label="dst"),
        rules=[
            ConfigBundleRuleItem(
                feature_key="scheduler",
                name="nested-chat",
                enabled=True,
                priority=10,
                config={"action": {"target_chat_id": -10002, "cmd": "run"}},
            )
        ],
    )
    dry_run = compare_bundles(
        source,
        target,
        available_features={"scheduler": "Scheduler"},
        available_command_templates={},
    )
    db = _FakeDB([])
    with pytest.raises(BundleConfirmError) as exc_info:
        await apply_bundle_confirm(
            db,
            account_id=2,
            source=source,
            dry_run=dry_run,
            available_command_templates={},
            apply_conflicts=True,
            confirm_chat_id_conflicts=False,
        )
    assert exc_info.value.code == "CHAT_ID_CONFIRM_REQUIRED"


@pytest.mark.asyncio
async def test_apply_bundle_confirm_does_not_apply_blocked_conflict() -> None:
    source = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        command_links=[
            ConfigBundleCommandLinkItem(
                template_id=99,
                template_name="missing-template",
                aliases=[],
                type="reply_text",
                enabled=True,
            )
        ],
    )
    target = ConfigBundleExport(source_account=ConfigBundleSourceAccount(id=2, label="dst"))
    dry_run = compare_bundles(
        source,
        target,
        available_features={},
        available_command_templates={},
    )
    db = _FakeDB([])
    imported, skipped, conflicts, warnings = await apply_bundle_confirm(
        db,
        account_id=2,
        source=source,
        dry_run=dry_run,
        available_command_templates={},
        apply_conflicts=True,
        confirm_chat_id_conflicts=True,
    )
    assert imported == 0
    assert skipped == 0
    assert conflicts == 1
    assert warnings == []


@pytest.mark.asyncio
async def test_confirm_config_bundle_success_triggers_worker_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    source_bundle = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
        rules=[
            ConfigBundleRuleItem(
                feature_key="scheduler",
                name="job",
                enabled=True,
                priority=1,
                config={"chat_id": -10001},
            )
        ],
    )
    body = source_bundle.model_dump_json().encode("utf-8")
    target_bundle = ConfigBundleExport(source_account=ConfigBundleSourceAccount(id=2, label="dst"))
    available_features = {"scheduler": "Scheduler"}
    expected_signature = build_preview_signature(
        account_id=2,
        file_content=body,
        apply_conflicts=True,
        confirm_chat_id_conflicts=True,
        preview_context_digest=build_preview_context_digest(
            target=target_bundle,
            available_features=available_features,
            available_command_templates={},
        ),
    )

    load_bundle = AsyncMock(
        side_effect=[
            target_bundle,
            target_bundle,
        ]
    )
    monkeypatch.setattr(config_bundle_api, "_load_bundle", load_bundle)
    monkeypatch.setattr(config_bundle_api, "_available_feature_map", AsyncMock(return_value=available_features))
    monkeypatch.setattr(config_bundle_api, "_available_command_templates", AsyncMock(return_value={}))
    monkeypatch.setattr(config_bundle_api, "apply_bundle_confirm", AsyncMock(return_value=(1, 0, 0, [])))
    monkeypatch.setattr(config_bundle_api.audit, "write", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(config_bundle_api, "_notify_worker_reload", notify)

    db = SimpleNamespace(commit=AsyncMock())
    req = SimpleNamespace(headers={})
    file = UploadFile(file=BytesIO(body), filename="bundle.json", headers=Headers({}))
    user = SimpleNamespace(id=11)
    resp = await config_bundle_api.confirm_config_bundle(
        aid=2,
        db=db,
        user=user,
        request=req,  # type: ignore[arg-type]
        file=file,
        apply_conflicts=True,
        confirm_chat_id_conflicts=True,
        preview_signature=expected_signature,
    )
    assert resp.imported == 1
    notify.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_confirm_config_bundle_rejects_mismatched_preview_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bundle = ConfigBundleExport(
        source_account=ConfigBundleSourceAccount(id=1, label="src"),
    )
    body = source_bundle.model_dump_json().encode("utf-8")
    load_bundle = AsyncMock(
        return_value=ConfigBundleExport(source_account=ConfigBundleSourceAccount(id=2, label="dst"))
    )
    monkeypatch.setattr(config_bundle_api, "_load_bundle", load_bundle)
    monkeypatch.setattr(config_bundle_api, "_available_feature_map", AsyncMock(return_value={}))
    monkeypatch.setattr(config_bundle_api, "_available_command_templates", AsyncMock(return_value={}))
    db = SimpleNamespace()
    req = SimpleNamespace(headers={})
    file = UploadFile(file=BytesIO(body), filename="bundle.json", headers=Headers({}))
    user = SimpleNamespace(id=11)
    with pytest.raises(HTTPException) as exc_info:
        await config_bundle_api.confirm_config_bundle(
            aid=2,
            db=db,
            user=user,
            request=req,  # type: ignore[arg-type]
            file=file,
            apply_conflicts=False,
            confirm_chat_id_conflicts=False,
            preview_signature="bad-signature",
        )
    assert exc_info.value.detail["code"] == "PREVIEW_SIGNATURE_MISMATCH"
