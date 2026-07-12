from __future__ import annotations

from app.util.update_plan import classify_changed_files


def test_version_bumps_are_service_scoped() -> None:
    plan = classify_changed_files(
        [
            "backend/app/__init__.py",
            "backend/pyproject.toml",
            "frontend/package.json",
            "frontend/src/lib/version.ts",
        ]
    )

    assert plan.components == ["backend", "frontend"]
    assert plan.services == ["web", "frontend"]
    assert plan.requires_full_update is False
    assert plan.requires_backup is False


def test_backup_script_change_does_not_rebuild_runtime_services() -> None:
    plan = classify_changed_files(["deploy/backup.sh", "deploy/README.md"])

    assert plan.components == ["docs_only"]
    assert plan.services == []
    assert plan.requires_full_update is False


def test_compose_change_only_rebuilds_changed_application_service() -> None:
    plan = classify_changed_files(
        ["docker-compose.yml"],
        compose_changed_services={"updater"},
    )

    assert plan.components == ["updater"]
    assert plan.services == ["updater"]
    assert plan.requires_full_update is False


def test_compose_database_change_requires_infrastructure_update() -> None:
    plan = classify_changed_files(
        ["docker-compose.yml"],
        compose_changed_services={"postgres"},
    )

    assert plan.components == ["full_update", "infrastructure"]
    assert plan.services == []
    assert plan.requires_full_update is True


def test_migration_requires_backup_without_restarting_database_service() -> None:
    plan = classify_changed_files(["backend/alembic/versions/0041_example.py"])

    assert plan.components == ["migration", "backend"]
    assert plan.services == ["web"]
    assert plan.requires_backup is True
    assert plan.requires_migration is True
    assert plan.requires_full_update is False


def test_updater_control_files_only_rebuild_updater() -> None:
    plan = classify_changed_files(
        [
            "backend/app/util/update_plan.py",
            "deploy/updater/server.py",
            "scripts/prod-update.sh",
        ]
    )

    assert plan.components == ["backend", "updater"]
    assert plan.services == ["web", "updater"]
    assert plan.requires_full_update is False


def test_unknown_runtime_file_fails_closed() -> None:
    plan = classify_changed_files(["runtime-new-format.toml"])

    assert plan.components == ["full_update"]
    assert plan.requires_full_update is True
    assert plan.reasons == ["无法确定运行影响范围：runtime-new-format.toml"]
