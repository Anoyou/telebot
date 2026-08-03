from __future__ import annotations

import subprocess
from pathlib import Path

from app.util import update_plan
from app.util.update_plan import build_update_plan, classify_changed_files


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
    assert plan.file_sync_services == []
    assert plan.rebuild_services == ["web", "frontend"]
    assert plan.requires_full_update is False
    assert plan.requires_backup is False


def test_backup_script_change_does_not_rebuild_runtime_services() -> None:
    plan = classify_changed_files(["deploy/backup.sh", "deploy/README.md"])

    assert plan.components == ["docs_only"]
    assert plan.services == []
    assert plan.file_sync_services == []
    assert plan.rebuild_services == []
    assert plan.requires_full_update is False


def test_compose_change_only_rebuilds_changed_application_service() -> None:
    plan = classify_changed_files(
        ["docker-compose.yml"],
        compose_changed_services={"updater"},
    )

    assert plan.components == ["updater"]
    assert plan.services == ["updater"]
    assert plan.file_sync_services == []
    assert plan.rebuild_services == ["updater"]
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
    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == []
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
    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == ["updater"]
    assert plan.requires_full_update is False


def test_unknown_runtime_file_fails_closed() -> None:
    plan = classify_changed_files(["runtime-new-format.toml"])

    assert plan.components == ["full_update"]
    assert plan.requires_full_update is True
    assert plan.reasons == ["无法确定运行影响范围：runtime-new-format.toml"]


def test_generated_openapi_snapshot_has_no_independent_runtime_impact() -> None:
    plan = classify_changed_files(["openapi/telepilot.openapi.json"])

    assert plan.components == ["docs_only"]
    assert plan.services == []
    assert plan.requires_full_update is False
    assert plan.reasons == []


def test_backend_source_change_uses_file_sync_without_image_rebuild() -> None:
    plan = classify_changed_files(
        [
            "backend/app/services/system_agent/tools/web.py",
            "backend/app/main.py",
        ]
    )

    assert plan.components == ["backend"]
    assert plan.services == ["web"]
    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == []


def test_backend_dependency_change_still_rebuilds_web_image() -> None:
    plan = classify_changed_files(["backend/pyproject.toml"])

    assert plan.components == ["backend"]
    assert plan.services == ["web"]
    assert plan.file_sync_services == []
    assert plan.rebuild_services == ["web"]


def test_direct_frontend_version_classification_fails_closed() -> None:
    plan = classify_changed_files(["frontend/src/lib/version.ts"])

    assert plan.services == ["web", "frontend"]
    assert plan.rebuild_services == ["frontend"]


def test_missing_tomllib_fails_closed_to_dependency_rebuild(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(update_plan, "tomllib", None)

    assert update_plan._backend_dependencies_changed(tmp_path, "old", "new") is True


def test_build_plan_treats_version_only_pyproject_change_as_file_sync(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "backend").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    pyproject = root / "backend/pyproject.toml"
    pyproject.write_text(
        '[project]\nversion = "1.0.0"\ndependencies = ["fastapi>=1"]\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "old"], cwd=root, check=True)
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    pyproject.write_text(
        '[project]\nversion = "1.0.1"\ndependencies = ["fastapi>=1"]\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "new"], cwd=root, check=True)
    new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    plan = build_update_plan(root, old, new)

    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == []


def test_build_plan_rebuilds_frontend_for_release_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "frontend" / "src" / "lib").mkdir(parents=True)
    (root / "openapi").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "backend" / "app" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (root / "backend" / "pyproject.toml").write_text(
        '[project]\nversion = "1.0.0"\ndependencies = ["fastapi>=1"]\n'
    )
    (root / "frontend" / "package.json").write_text(
        '{"name":"telepilot","version":"1.0.0","dependencies":{"react":"1"}}\n'
    )
    (root / "frontend" / "src" / "lib" / "version.ts").write_text(
        'export const APP_VERSION = "1.0.0";\n'
    )
    (root / "openapi" / "telepilot.openapi.json").write_text(
        '{"info":{"version":"1.0.0"}}\n'
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "old"], cwd=root, check=True)
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    for path in (
        root / "backend" / "app" / "__init__.py",
        root / "backend" / "pyproject.toml",
        root / "frontend" / "package.json",
        root / "frontend" / "src" / "lib" / "version.ts",
        root / "openapi" / "telepilot.openapi.json",
    ):
        path.write_text(path.read_text().replace("1.0.0", "1.0.1"))
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "new"], cwd=root, check=True)
    new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    plan = build_update_plan(root, old, new)

    assert plan.components == ["backend", "frontend"]
    assert plan.services == ["web", "frontend"]
    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == ["frontend"]


def test_all_runtime_plugin_docs_and_changelog_are_docs_only() -> None:
    plan = classify_changed_files(
        [
            "CHANGELOG.md",
            "docs/PLUGIN-AI.md",
            "docs/PLUGIN-API-REFERENCE.md",
            "docs/PLUGIN-CHEATSHEET.md",
            "docs/PLUGIN-DEV-GUIDE.md",
            "docs/PLUGIN-HTTP.md",
            "docs/PLUGIN-OVERVIEW.md",
            "docs/PLUGIN-QUICKSTART.md",
            "docs/PLUGIN-REMOTE.md",
            "docs/PLUGIN-RULES.md",
            "docs/PLUGIN-SAFETY.md",
        ]
    )

    assert plan.components == ["docs_only"]
    assert plan.services == []
    assert plan.rebuild_services == []


def test_root_dockerignore_affects_all_application_images() -> None:
    plan = classify_changed_files([".dockerignore"])

    assert plan.services == ["web", "frontend", "updater"]
    assert plan.rebuild_services == ["web", "frontend", "updater"]


def test_subdirectory_dockerignore_files_do_not_affect_root_context() -> None:
    plan = classify_changed_files(["backend/.dockerignore", "frontend/.dockerignore"])

    assert plan.components == ["docs_only"]
    assert plan.services == []


def test_frontend_test_only_change_does_not_deploy_runtime() -> None:
    plan = classify_changed_files(
        ["frontend/e2e/app.spec.ts", "frontend/playwright.config.ts"]
    )

    assert plan.components == ["docs_only"]
    assert plan.services == []


def test_tracked_plugin_change_uses_volume_sync_not_web_rebuild() -> None:
    plan = classify_changed_files(["plugins/installed/lottery_plus/plugin.py"])

    assert plan.components == ["backend"]
    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == []


def test_infrastructure_compose_change_requires_backup() -> None:
    plan = classify_changed_files(
        ["docker-compose.yml"], compose_changed_services={"postgres"}
    )

    assert plan.requires_full_update is True
    assert plan.requires_backup is True


def test_frontend_source_rebuilds_frontend_and_refreshes_agent_source_snapshot() -> None:
    plan = classify_changed_files(
        ["frontend/src/pages/Assistant/Index.tsx", "frontend/vite.config.ts"]
    )

    assert plan.components == ["backend", "frontend"]
    assert plan.services == ["web", "frontend"]
    assert plan.file_sync_services == ["web"]
    assert plan.rebuild_services == ["frontend"]


def test_dotfile_and_unicode_docs_are_not_misclassified_as_runtime(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / ".env.example").write_text("A=1\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "审查.md").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "old"], cwd=root, check=True)
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    (root / ".env.example").write_text("A=2\n", encoding="utf-8")
    (docs / "审查.md").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "new"], cwd=root, check=True)
    new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    plan = build_update_plan(root, old, new)

    assert plan.changed_files == [".env.example", "docs/审查.md"]
    assert plan.components == ["docs_only"]
    assert plan.requires_full_update is False
