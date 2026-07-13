"""系统配置备份恢复的事务、拓扑与 ID 映射回归测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.api import system_health
from app.services import auth_service
from app.worker import ipc


class _Base(DeclarativeBase):
    pass


@pytest.mark.asyncio
async def test_sensitive_export_requires_password_reauthentication(monkeypatch) -> None:
    user = SimpleNamespace(
        id=1,
        password_hash=auth_service.hash_password("correct-password"),
        totp_secret_enc=None,
    )
    build = AsyncMock(return_value={"_meta": {"include_sensitive": True}})
    monkeypatch.setattr(system_health, "_build_export_payload", build)

    with pytest.raises(HTTPException) as exc_info:
        await system_health.export_config(
            user,
            system_health.ExportConfigRequest(
                categories=["account_settings"],
                include_sensitive=True,
            ),
        )
    assert exc_info.value.status_code == 403
    build.assert_not_awaited()

    response = await system_health.export_config(
        user,
        system_health.ExportConfigRequest(
            categories=["account_settings"],
            include_sensitive=True,
            password="correct-password",
        ),
    )
    assert response.status_code == 200
    build.assert_awaited_once()


class _Account(_Base):
    __tablename__ = "backup_test_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")


class _CommandTemplate(_Base):
    __tablename__ = "backup_test_command_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class _AccountCommandLink(_Base):
    __tablename__ = "backup_test_account_command_link"

    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("backup_test_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    template_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("backup_test_command_template.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class _LLMProvider(_Base):
    __tablename__ = "backup_test_llm_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    client_identity_profile: Mapped[str] = mapped_column(String, nullable=False, default="auto")


_MODEL_MAP = {
    "Account": _Account,
    "CommandTemplate": _CommandTemplate,
    "AccountCommandLink": _AccountCommandLink,
}


@asynccontextmanager
async def _session_factory(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        yield session


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _factory(maker: async_sessionmaker[AsyncSession]) -> Callable[[], Any]:
    return lambda: _session_factory(maker)


def _payload() -> dict[str, Any]:
    return {
        "_meta": {
            "format": "telepilot-config",
            "bundle_version": 2,
            "include_sensitive": True,
        },
        # 故意把依赖类别放在前面，导入器必须使用自己的拓扑，不能依赖 JSON key 顺序。
        "account_commands": [
            {"account_id": 7, "template_id": 11, "enabled": True},
        ],
        "command_templates": [
            {
                "id": 11,
                "name": "hello",
                "type": "reply_text",
                "config": {"text": "hello"},
            },
        ],
        "account_settings": [
            {"id": 7, "phone": "+10001", "status": "active"},
        ],
    }


@pytest.mark.asyncio
async def test_config_import_round_trip_into_fresh_database() -> None:
    engine, maker = await _database()
    try:
        outcome = await system_health._import_config_payload(  # noqa: SLF001
            _payload(),
            session_factory=_factory(maker),
            model_map=_MODEL_MAP,
        )

        assert outcome.imported == 3
        assert outcome.skipped == 0
        assert outcome.id_mappings["account_settings"]["7"] == 1
        assert outcome.id_mappings["command_templates"]["11"] == 1

        async with maker() as db:
            link = (await db.execute(select(_AccountCommandLink))).scalar_one()
            assert link.account_id == 1
            assert link.template_id == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_config_import_warns_when_unknown_identity_falls_back_to_auto() -> None:
    engine, maker = await _database()
    try:
        outcome = await system_health._import_config_payload(  # noqa: SLF001
            {
                "_meta": {
                    "format": "telepilot-config",
                    "bundle_version": 2,
                    "include_sensitive": True,
                },
                "llm_providers": [
                    {"id": 9, "name": "legacy", "client_identity_profile": "future_client"}
                ],
            },
            session_factory=_factory(maker),
            model_map={**_MODEL_MAP, "LLMProvider": _LLMProvider},
        )
        assert outcome.warnings == [
            "[llm_providers] 客户端身份档案 'future_client' 未知，已降级为 auto"
        ]
        async with maker() as db:
            row = (await db.execute(select(_LLMProvider))).scalar_one()
            assert row.client_identity_profile == "auto"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_config_import_remaps_when_target_ids_belong_to_other_rows() -> None:
    engine, maker = await _database()
    try:
        async with maker() as db:
            db.add(_Account(id=7, phone="+99999", status="active"))
            db.add(
                _CommandTemplate(
                    id=11,
                    name="other",
                    type="reply_text",
                    config={},
                )
            )
            await db.commit()

        outcome = await system_health._import_config_payload(  # noqa: SLF001
            _payload(),
            session_factory=_factory(maker),
            model_map=_MODEL_MAP,
        )

        mapped_account = outcome.id_mappings["account_settings"]["7"]
        mapped_template = outcome.id_mappings["command_templates"]["11"]
        assert mapped_account != 7
        assert mapped_template != 11

        async with maker() as db:
            link = (await db.execute(select(_AccountCommandLink))).scalar_one()
            assert link.account_id == mapped_account
            assert link.template_id == mapped_template
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_config_import_commit_failure_rolls_back_and_never_counts_success() -> None:
    engine, maker = await _database()

    @asynccontextmanager
    async def broken_factory() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            async def broken_commit() -> None:
                raise RuntimeError("commit acknowledgement lost")

            db.commit = broken_commit  # type: ignore[method-assign]
            yield db

    try:
        with pytest.raises(system_health.ConfigImportError) as exc_info:
            await system_health._import_config_payload(  # noqa: SLF001
                _payload(),
                session_factory=broken_factory,
                model_map=_MODEL_MAP,
            )

        assert exc_info.value.imported == 0
        async with maker() as db:
            assert (await db.execute(select(_Account))).scalars().all() == []
            assert (await db.execute(select(_CommandTemplate))).scalars().all() == []
            assert (await db.execute(select(_AccountCommandLink))).scalars().all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_config_import_cross_category_failure_rolls_back_everything() -> None:
    engine, maker = await _database()
    payload = _payload()
    payload["account_commands"][0]["template_id"] = 999
    try:
        with pytest.raises(system_health.ConfigImportError, match="缺少依赖映射"):
            await system_health._import_config_payload(  # noqa: SLF001
                payload,
                session_factory=_factory(maker),
                model_map=_MODEL_MAP,
            )

        async with maker() as db:
            assert (await db.execute(select(_Account))).scalars().all() == []
            assert (await db.execute(select(_CommandTemplate))).scalars().all() == []
            assert (await db.execute(select(_AccountCommandLink))).scalars().all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_export_uses_versioned_bundle_and_expands_dependencies() -> None:
    engine, maker = await _database()
    try:
        async with maker() as db:
            db.add(_Account(id=7, phone="+10001", status="active"))
            db.add(
                _CommandTemplate(
                    id=11,
                    name="hello",
                    type="reply_text",
                    config={"text": "hello"},
                )
            )
            db.add(_AccountCommandLink(account_id=7, template_id=11, enabled=True))
            await db.commit()

        payload = await system_health._build_export_payload(  # noqa: SLF001
            categories=["account_commands"],
            include_sensitive=True,
            session_factory=_factory(maker),
            model_map=_MODEL_MAP,
        )

        assert payload["_meta"]["format"] == "telepilot-config"
        assert payload["_meta"]["bundle_version"] == 2
        assert payload["_meta"]["requested_categories"] == ["account_commands"]
        assert list(payload)[-3:] == [
            "account_settings",
            "command_templates",
            "account_commands",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_reload_contract_marks_unconfirmed_worker_for_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Redis:
        pass

    async def publish_with_ack(_redis: Any, account_id: int, command: str) -> bool:
        return account_id == 1 or command != ipc.CMD_RELOAD_IGNORED

    monkeypatch.setattr(system_health, "get_redis", lambda: _Redis())
    monkeypatch.setattr(ipc, "publish_cmd_with_ack", publish_with_ack)

    reloaded, restart_required = await system_health._reload_imported_runtime([1, 2])  # noqa: SLF001

    assert reloaded == [1]
    assert restart_required is True


@pytest.mark.asyncio
async def test_config_import_rejects_unknown_or_incomplete_v2_bundle() -> None:
    engine, maker = await _database()
    try:
        unknown = _payload()
        unknown["account_command"] = unknown.pop("account_commands")
        with pytest.raises(system_health.ConfigImportError, match="未知类别"):
            await system_health._import_config_payload(  # noqa: SLF001
                unknown,
                session_factory=_factory(maker),
                model_map=_MODEL_MAP,
            )

        incomplete = _payload()
        incomplete["_meta"]["included_categories"] = [
            "account_settings",
            "command_templates",
            "account_commands",
            "llm_providers",
        ]
        with pytest.raises(system_health.ConfigImportError, match="内容不完整"):
            await system_health._import_config_payload(  # noqa: SLF001
                incomplete,
                session_factory=_factory(maker),
                model_map=_MODEL_MAP,
            )
    finally:
        await engine.dispose()
