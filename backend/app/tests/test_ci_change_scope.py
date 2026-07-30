from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.ci_change_scope import changed_paths, classify_paths  # noqa: E402


def test_docs_only_commit_runs_no_heavy_jobs() -> None:
    scope = classify_paths(["CHANGELOG.md", "docs/PLUGIN-DEV-GUIDE.md"])

    assert scope.docs_only is True
    assert scope.backend is False
    assert scope.frontend is False
    assert scope.browser is False
    assert scope.to_outputs()["publish"] == "false"


def test_backend_commit_runs_backend_without_browser() -> None:
    scope = classify_paths(["backend/app/api/system_health.py"])

    assert scope.backend is True
    assert scope.frontend is False
    assert scope.browser is False
    assert scope.contract is True
    assert scope.to_outputs()["publish"] == "true"


def test_changed_paths_covers_the_entire_push_range(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "TelePilot CI"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@telepilot.invalid"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "app.py").write_text("runtime = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "backend/app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "runtime"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("docs last\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "docs"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    assert changed_paths(tmp_path, base, head) == ["README.md", "backend/app.py"]


def test_frontend_ui_commit_runs_frontend_and_browser() -> None:
    scope = classify_paths(["frontend/src/pages/Extensions.tsx"])

    assert scope.backend is False
    assert scope.frontend is True
    assert scope.browser is True


def test_frontend_app_lib_and_browser_fixtures_run_browser_gate() -> None:
    scope = classify_paths(
        [
            "frontend/src/App.tsx",
            "frontend/src/lib/runtime-version.ts",
            "frontend/tests/visual/baseline.spec.ts",
        ]
    )

    assert scope.frontend is True
    assert scope.browser is True


def test_frontend_unit_test_only_does_not_start_browser_gate() -> None:
    scope = classify_paths(["frontend/src/lib/runtime-version.test.ts"])

    assert scope.frontend is True
    assert scope.browser is False


def test_plugin_runtime_commit_runs_backend_and_plugin_validation() -> None:
    scope = classify_paths(["backend/app/worker/plugins/loader.py"])

    assert scope.backend is True
    assert scope.plugin is True


def test_workflow_or_scope_script_change_fails_open_to_full_gate() -> None:
    workflow = classify_paths([".github/workflows/ci.yml"])
    classifier = classify_paths(["scripts/ci_change_scope.py"])

    assert workflow.full is True
    assert classifier.full is True
    assert workflow.backend and workflow.frontend and workflow.browser
    assert workflow.contract and classifier.contract


def test_image_publish_job_evaluates_after_conditional_jobs_are_skipped() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "if: always() && github.event_name == 'push' "
        "&& needs.ci-gate.result == 'success'"
    ) in workflow


def test_release_metadata_is_identified_without_forcing_browser() -> None:
    scope = classify_paths(
        [
            "backend/app/__init__.py",
            "backend/pyproject.toml",
            "frontend/package.json",
            "frontend/src/lib/version.ts",
            "CHANGELOG.md",
        ]
    )

    assert scope.version is True
    assert scope.backend is True
    assert scope.frontend is True
    assert scope.browser is False


def test_version_sync_gate_includes_openapi_snapshot_version() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'openapi_snapshot = json.loads(Path("openapi/telepilot.openapi.json").read_text())' in workflow
    assert '"openapi/telepilot.openapi.json": openapi_snapshot["info"]["version"]' in workflow
