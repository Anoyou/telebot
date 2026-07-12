"""启动期迁移与部署配置告警的最小单测。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app import main


def test_migration_lock_is_held_until_upgrade_finishes() -> None:
    """Postgres advisory lock 必须覆盖整个 Alembic upgrade，而非提前释放。"""
    fake_conn = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar.return_value = True
    fake_conn.execute.return_value = fake_result

    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn

    def assert_connection_still_open(*_args, **_kwargs) -> None:
        fake_conn.close.assert_not_called()

    with (
        patch.object(main.settings, "database_url", "postgresql+asyncpg://u:p@h:5432/db"),
        patch("sqlalchemy.create_engine", return_value=fake_engine),
        patch("pathlib.Path.exists", return_value=True),
        patch("alembic.command.upgrade", side_effect=assert_connection_still_open) as upgrade,
    ):
        main._run_alembic_upgrade()

    upgrade.assert_called_once()
    fake_conn.close.assert_called_once()
    executed_sql = [str(call.args[0]) for call in fake_conn.execute.call_args_list]
    assert any("pg_try_advisory_lock" in sql for sql in executed_sql)
    assert any("pg_advisory_unlock" in sql for sql in executed_sql)


def test_run_alembic_upgrade_skips_when_lock_not_acquired() -> None:
    """未拿到锁时必须跳过迁移执行，避免并发重复跑 upgrade。"""
    fake_conn = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar.return_value = False
    fake_conn.execute.return_value = fake_result
    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn

    with (
        patch.object(main.settings, "database_url", "postgresql+asyncpg://u:p@h:5432/db"),
        patch("sqlalchemy.create_engine", return_value=fake_engine),
        patch("alembic.command.upgrade") as upgrade,
    ):
        main._run_alembic_upgrade()
    upgrade.assert_not_called()


def test_warn_if_forwarded_for_misconfigured_in_container(caplog) -> None:
    """容器环境 + TRUST_FORWARDED_FOR=false 时输出明确 WARN。"""
    with (
        patch("app.main._is_container_env", return_value=True),
        patch.object(main.settings, "trust_forwarded_for", False),
    ):
        main._warn_if_forwarded_for_misconfigured()
    assert "TRUST_FORWARDED_FOR=false" in caplog.text
