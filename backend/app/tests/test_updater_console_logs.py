from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


def _load_updater_module():
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "deploy" / "updater" / "server.py"
    spec = importlib.util.spec_from_file_location("telepilot_updater_server_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_console_logs_use_host_compose_project_name(monkeypatch) -> None:
    updater = _load_updater_module()
    commands: list[list[str]] = []

    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))

    def fake_run(args, *, timeout=60, env=None):  # noqa: ANN001
        commands.append(args)
        return "telepilot-web-1 | ready\ntelepilot-frontend-1 | ok", "", 0

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._tail_console_logs("web", 20, None)

    assert result["ok"] is True
    assert result["project"] == "telepilot"
    assert commands[0][:4] == ["docker", "compose", "-p", "telepilot"]
    assert commands[0][4:7] == ["logs", "--no-color", "--timestamps"]
    assert commands[0][-1] == "web"
    assert result["lines"] == [
        "telepilot-web-1 | ready",
        "telepilot-frontend-1 | ok",
    ]


def test_console_logs_respects_explicit_compose_project_name(monkeypatch) -> None:
    updater = _load_updater_module()
    commands: list[list[str]] = []

    monkeypatch.setenv("TELEPILOT_COMPOSE_PROJECT_NAME", "custom_stack")
    monkeypatch.setattr(
        updater,
        "_run",
        lambda args, **_kw: commands.append(args) or ("custom-web-1 | ready", "", 0),
    )

    result = updater._tail_console_logs("all", 20, None)

    assert result["ok"] is True
    assert result["project"] == "custom_stack"
    assert commands[0][:4] == ["docker", "compose", "-p", "custom_stack"]


def test_resource_snapshot_includes_all_running_project_services(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))

    services = ("postgres", "redis", "web", "updater", "frontend", "plugin-runner")
    ps_rows = "\n".join(
        f"id-{service}|telepilot-{service}-1|telepilot|{service}|/TelePilot"
        for service in services
    )
    stats_rows = "\n".join(
        "{" + (
            f'"ID":"id-{service}","Name":"telepilot-{service}-1",'
            f'"CPUPerc":"{index + 1}.0%","MemUsage":"{(index + 1) * 10}MiB / 512MiB",'
            f'"MemPerc":"{index + 1}.5%","PIDs":"{index + 2}"'
        ) + "}"
        for index, service in enumerate(services)
    )

    def fake_run(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["docker", "ps"]:
            return ps_rows, "", 0
        if args[:3] == ["docker", "stats", "--no-stream"]:
            return stats_rows, "", 0
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._resource_snapshot()

    assert result["ok"] is True
    assert result["project"] == "telepilot"
    assert {item["service"] for item in result["containers"]} == set(services)
    assert next(item for item in result["containers"] if item["service"] == "web") == {
        "id": "id-web",
        "name": "telepilot-web-1",
        "service": "web",
        "cpu_percent": "3.0%",
        "memory_usage": "30MiB / 512MiB",
        "memory_percent": "3.5%",
        "pids": 4,
    }


def test_apply_job_env_uses_host_compose_project_name(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/opt/TelePilot"))
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("TELEPILOT_COMPOSE_PROJECT_NAME", raising=False)

    env = updater._apply_job_env("origin", "codex/0.33-interaction-framework")

    assert env["COMPOSE_PROJECT_NAME"] == "telepilot"
    assert env["TELEPILOT_UPDATE_REMOTE"] == "origin"
    assert env["TELEPILOT_UPDATE_BRANCH"] == "codex/0.33-interaction-framework"
    assert env["TELEPILOT_UPDATE_PREFETCHED"] == "1"
    assert env["DOCKER_BUILDKIT"] == "1"
    assert env["COMPOSE_DOCKER_CLI_BUILD"] == "1"


def test_apply_job_env_passes_job_id_to_updater_handoff(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/opt/TelePilot"))

    env = updater._apply_job_env("origin", "main", "0123456789ab")

    assert env["TELEPILOT_UPDATE_JOB_ID"] == "0123456789ab"


def test_apply_job_env_recovers_absolute_host_dir_from_container_label(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("."))
    monkeypatch.setenv("HOSTNAME", "container-id")
    monkeypatch.setattr(
        updater,
        "_run",
        lambda args, **_kw: (
            ("/TelePilot", "", 0) if args[:3] == ["docker", "inspect", "--format"] else ("", "", 1)
        ),
    )

    env = updater._apply_job_env("origin", "main")

    assert env["TELEPILOT_HOST_PROJECT_DIR"] == "/TelePilot"
    assert env["COMPOSE_PROJECT_NAME"] == "telepilot"


def test_apply_job_env_rejects_unresolved_relative_host_dir(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("."))
    monkeypatch.setenv("HOSTNAME", "container-id")
    monkeypatch.setattr(updater, "_run", lambda *_args, **_kw: ("", "missing", 1))

    try:
        updater._apply_job_env("origin", "main")
    except RuntimeError as exc:
        assert "绝对路径" in str(exc)
    else:
        raise AssertionError("relative host project dir must fail closed")


def test_update_job_survives_updater_restart(monkeypatch, tmp_path) -> None:
    updater = _load_updater_module()
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setattr(updater, "WORKSPACE", workspace)

    updater._set_job("job123", status="succeeded", logs=["done"])
    updater._jobs.clear()

    assert updater._job_snapshot("job123") == {
        "job_id": "job123",
        "status": "succeeded",
        "logs": ["done"],
    }


def test_handoff_marker_blocks_until_explicitly_removed(monkeypatch, tmp_path) -> None:
    updater = _load_updater_module()
    workspace = tmp_path / "repo"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    marker = git_dir / "telepilot-updater-handoff-active"
    marker.write_text("job 1\n", encoding="utf-8")
    monkeypatch.setattr(updater, "WORKSPACE", workspace)

    assert updater._handoff_active() is True

    marker.unlink()
    assert updater._handoff_active() is False


def test_incremental_script_never_recreates_web_with_updater() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")

    assert "docker compose up -d --build --no-deps --force-recreate updater web" not in script
    assert "docker compose up -d --build --no-deps" not in script
    assert "pull_verified_image" in script
    assert "switch_prebuilt_service" in script
    assert "org.opencontainers.image.revision" in script
    assert '-e COMPOSE_PROJECT_NAME="$project"' in script
    assert '-e TELEPILOT_HOST_PROJECT_DIR="$TELEPILOT_HOST_PROJECT_DIR"' in script
    assert "telepilot-updater-handoff.log" in script
    assert "com.docker.compose.project" in script
    assert script.index('mv "$pending_tmp" "$PENDING_FILE"') < script.index(
        'git pull --ff-only "$REMOTE" "$BRANCH"'
    )
    assert "persist_switched_services" in script
    assert "rollback_switched_services" in script
    assert "迁移边界内禁止自动恢复旧代码" in script
    assert 'pending_target:-}" == "$CHECKED_OUT_COMMIT"' in script
    assert 'git merge-base --is-ancestor "$pending_target" "$TARGET_COMMIT"' in script
    assert 'warn "分类依据：$reason"' in script
    assert "镜像不存在或构建未成功" in script


def test_runtime_content_bind_mount_uses_the_host_project_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    prod_up = (repo_root / "scripts" / "prod-up.sh").read_text(encoding="utf-8")
    prod_update = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")

    mount = "${TELEPILOT_HOST_PROJECT_DIR:-.}/.run/runtime-content:"
    assert compose.count(mount) == 2
    assert "${TELEPILOT_RUNTIME_CONTENT_DIR:-./.run/runtime-content}:" not in compose
    expected_export = (
        'export TELEPILOT_HOST_PROJECT_DIR="${TELEPILOT_HOST_PROJECT_DIR:-$ROOT_DIR}"'
    )
    assert expected_export in prod_up
    assert expected_export in prod_update


def test_incremental_script_waits_for_frontend_image_healthcheck() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")
    dockerfile = (repo_root / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "frontend_url" not in script
    assert 'wait_http "$(frontend_url)"' not in script
    assert "wait_compose_healthy docker-compose.yml frontend" in script
    assert (
        "HEALTHCHECK --interval=10s --timeout=3s "
        "CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1"
    ) in dockerfile
    assert "@@TELEPILOT_PROGRESS@@" in script


def test_incremental_script_reloads_target_logic_before_image_verification() -> None:
    """旧 updater 必须先让目标脚本接管，不能继续执行启动时加载的旧验签函数。"""
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")

    fast_forward = script.index('git pull --ff-only "$REMOTE" "$BRANCH"')
    reload_target = script.index('exec bash scripts/prod-update.sh "${ORIGINAL_ARGS[@]}"')
    prefetch_images = script.index(
        "# 目标版本更新逻辑接管后再确认所有必需镜像。"
    )
    reload_guard = script.rfind(
        "if (( HEAD_ALREADY_UPDATED == 0 )); then",
        fast_forward,
        reload_target,
    )

    assert 'ORIGINAL_ARGS=("$@")' in script
    assert script.count('exec bash scripts/prod-update.sh "${ORIGINAL_ARGS[@]}"') == 1
    assert fast_forward < reload_guard < reload_target < prefetch_images
    assert script.index("pull_verified_image web", prefetch_images) > reload_target


def test_incremental_script_syncs_backend_files_with_image_rollback() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")

    assert 'git archive "$NEW_COMMIT"' in script
    assert 'python -m compileall -q "$stage/backend/app"' in script
    assert "new_image_id=\"$(docker commit" in script
    assert '--message "TelePilot 文件同步' in script
    assert '--change "CMD $image_cmd"' in script
    assert "自定义 ENTRYPOINT" in script
    assert "rollback_web_runtime_image" in script
    assert 'runtime_ref="telepilot-web-runtime:${NEW_COMMIT}"' in script
    assert 'TELEPILOT_WEB_IMAGE="$WEB_SYNC_OLD_IMAGE_REF"' in script
    assert "文件级同步服务：web（无需执行 docker build）" in script
    assert 'if [[ -n "$WEB_SYNC_IMAGE_REF" ]]; then' in script
    assert 'if (( WEB_SYNC_IMAGE_REF != "" )); then' not in script


def test_tracked_plugin_sync_preserves_modified_user_directory_and_restarts_web() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")

    assert 'preserved_roots="$stage/preserved-roots"' in script
    assert 'if [[ "$actual" != "missing" && "$actual" != "$expected" ]]' in script
    assert 'warn "检测到用户修改过 $plugin_root，保留整个插件目录"' in script
    assert "docker compose restart web >/dev/null" in script
    assert "wait_compose_healthy docker-compose.yml web 120" in script


def test_production_defaults_to_prebuilt_images_with_explicit_source_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    prod_up = (repo_root / "scripts" / "prod-up.sh").read_text(encoding="utf-8")
    workflow = (repo_root / ".github" / "workflows" / "publish-images.yml").read_text(
        encoding="utf-8"
    )
    ci_workflow = (repo_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "TELEPILOT_WEB_IMAGE" in compose
    assert "TELEPILOT_FRONTEND_IMAGE" in compose
    assert "TELEPILOT_UPDATER_IMAGE" in compose
    assert 'docker pull "$image_ref"' in prod_up
    assert "docker compose up -d --no-build" in prod_up
    assert "--source-build" in prod_up
    assert "org.opencontainers.image.revision" in prod_up
    assert "org.opencontainers.image.source" in prod_up
    assert "RepoDigests" in prod_up
    assert "verify_image_attestation" in prod_up
    assert "linux/amd64,linux/arm64" in workflow
    assert "packages: write" in workflow
    assert "sha-${REVISION}" in workflow
    assert "workflow_call:" in workflow
    assert "actions/attest@" in workflow
    assert "attestations: write" in workflow
    assert "org.opencontainers.image.revision=${{ needs.scope.outputs.revision }}" in workflow
    assert "uses: ./.github/workflows/publish-images.yml" in ci_workflow
    assert "github.event_name == 'push'" in ci_workflow
    assert 'tags: ["v*"]' in ci_workflow
    assert "ref_type: ${{ github.ref_type }}" in ci_workflow


def test_legacy_alpine_updater_bootstraps_github_cli(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "apk-called"
    fake_id = fake_bin / "id"
    fake_apk = fake_bin / "apk"
    fake_gh = fake_bin / "gh"
    fake_id.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
    fake_gh.write_text(
        """#!/bin/sh
exit 1
""",
        encoding="utf-8",
    )
    fake_apk.write_text(
        """#!/bin/sh
set -eu
touch "$FAKE_APK_MARKER"
cat > "$FAKE_BIN/gh" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$FAKE_BIN/gh"
""",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)
    fake_gh.chmod(0o755)
    fake_apk.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source scripts/_lib.sh; ensure_github_cli; command -v gh',
        ],
        cwd=repo_root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_BIN": str(fake_bin),
            "FAKE_APK_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.is_file()
    assert str(fake_bin / "gh") in result.stdout


def test_image_attestation_verification_is_noninteractive_without_github_login(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-call"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/bin/sh
set -eu
if [ "${1:-}" = "attestation" ] && [ "${2:-}" = "verify" ] && [ "${3:-}" = "--help" ]; then
  exit 0
fi
{
  printf 'GH_TOKEN=%s\\n' "${GH_TOKEN:-}"
  printf 'GH_PROMPT_DISABLED=%s\\n' "${GH_PROMPT_DISABLED:-}"
  printf 'args='
  printf '%s ' "$@"
  printf '\\n'
} > "$FAKE_GH_CAPTURE"
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN"}
    }
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_GH_CAPTURE": str(capture),
        }
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source scripts/_lib.sh; "
                "verify_image_attestation "
                "'ghcr.io/anoyou/telepilot-web@sha256:deadbeef' "
                "'0123456789abcdef0123456789abcdef01234567'"
            ),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocation = capture.read_text(encoding="utf-8")
    assert "GH_TOKEN=telepilot-oci-attestation-verification" in invocation
    assert "GH_PROMPT_DISABLED=1" in invocation
    assert "--repo Anoyou/Telebot" in invocation
    assert "--bundle-from-oci" in invocation
    assert "--source-digest 0123456789abcdef0123456789abcdef01234567" in invocation


def test_image_attestation_verification_preserves_configured_github_token(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-token"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/bin/sh
set -eu
if [ "${1:-}" = "attestation" ] && [ "${2:-}" = "verify" ] && [ "${3:-}" = "--help" ]; then
  exit 0
fi
printf '%s\\n' "${GH_TOKEN:-}" > "$FAKE_GH_CAPTURE"
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source scripts/_lib.sh; "
                "verify_image_attestation "
                "'ghcr.io/anoyou/telepilot-web@sha256:deadbeef' "
                "'0123456789abcdef0123456789abcdef01234567'"
            ),
        ],
        cwd=repo_root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_GH_CAPTURE": str(capture),
            "GH_TOKEN": "configured-github-token",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").strip() == "configured-github-token"


def _install_legacy_updater_repair(
    tmp_path: Path,
    *,
    existing_wrapper: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], Path, Path, Path]:
    repo_root = Path(__file__).resolve().parents[3]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_gh = tmp_path / "usr" / "bin" / "gh"
    real_gh.parent.mkdir(parents=True)
    wrapper_gh = fake_bin / "gh"
    apk_marker = tmp_path / "apk-calls"
    gh_capture = tmp_path / "gh-call"
    if existing_wrapper is not None:
        wrapper_gh.write_text(existing_wrapper, encoding="utf-8")
        wrapper_gh.chmod(0o755)

    (fake_bin / "id").write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
    (fake_bin / "sha256sum").write_text(
        "#!/bin/sh\nprintf 'fake-wrapper-sha256  %s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    (fake_bin / "apk").write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_APK_MARKER"
cat > "$TELEPILOT_GH_REAL_PATH" <<'EOF'
#!/bin/sh
set -eu
if [ "${1:-}" = "--version" ]; then
  printf 'gh version test\\n'
  exit 0
fi
{
  printf 'GH_TOKEN=%s\\n' "${GH_TOKEN:-}"
  printf 'GH_PROMPT_DISABLED=%s\\n' "${GH_PROMPT_DISABLED:-}"
  printf 'args='
  printf '|%s' "$@"
  printf '\\n'
} > "$FAKE_GH_CAPTURE"
EOF
chmod 0755 "$TELEPILOT_GH_REAL_PATH"
""",
        encoding="utf-8",
    )
    for executable in ("id", "sha256sum", "apk"):
        (fake_bin / executable).chmod(0o755)

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_PROMPT_DISABLED"}
    }
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_APK_MARKER": str(apk_marker),
            "FAKE_GH_CAPTURE": str(gh_capture),
            "TELEPILOT_APK_BIN": str(fake_bin / "apk"),
            "TELEPILOT_GH_REAL_PATH": str(real_gh),
            "TELEPILOT_GH_WRAPPER_PATH": str(wrapper_gh),
        }
    )
    result = subprocess.run(
        ["sh", str(repo_root / "scripts" / "repair-legacy-updater.sh")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, env, wrapper_gh, apk_marker, gh_capture


def test_legacy_updater_repair_script_installs_auditable_wrapper(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "repair-legacy-updater.sh"
    result, _env, wrapper_gh, apk_marker, _gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )

    assert script.stat().st_mode & 0o111
    assert result.returncode == 0, result.stderr
    assert apk_marker.read_text(encoding="utf-8").strip() == (
        "add --no-cache github-cli"
    )
    wrapper = wrapper_gh.read_text(encoding="utf-8")
    assert "TelePilot legacy updater OCI attestation compatibility wrapper" in wrapper
    assert "无需执行 gh auth login" in result.stdout
    assert "包装器 SHA256：fake-wrapper-sha256" in result.stdout


def test_legacy_updater_repair_is_idempotent(tmp_path: Path) -> None:
    first, env, wrapper_gh, apk_marker, _gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    assert first.returncode == 0, first.stderr
    first_wrapper = wrapper_gh.read_text(encoding="utf-8")

    repo_root = Path(__file__).resolve().parents[3]
    second = subprocess.run(
        ["sh", str(repo_root / "scripts" / "repair-legacy-updater.sh")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert wrapper_gh.read_text(encoding="utf-8") == first_wrapper
    assert apk_marker.read_text(encoding="utf-8").splitlines() == [
        "add --no-cache github-cli"
    ]


def test_legacy_updater_repair_refuses_to_replace_foreign_wrapper(
    tmp_path: Path,
) -> None:
    result, _env, wrapper_gh, apk_marker, _gh_capture = (
        _install_legacy_updater_repair(
            tmp_path,
            existing_wrapper="#!/bin/sh\nexec /custom/gh \"$@\"\n",
        )
    )

    assert result.returncode == 1
    assert "不是 TelePilot 兼容包装器，拒绝覆盖" in result.stderr
    assert wrapper_gh.read_text(encoding="utf-8") == (
        "#!/bin/sh\nexec /custom/gh \"$@\"\n"
    )
    assert not apk_marker.exists()


def test_legacy_updater_repair_rejects_unsafe_paths(tmp_path: Path) -> None:
    _first, env, _wrapper_gh, _apk_marker, _gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    env["TELEPILOT_GH_REAL_PATH"] = "/usr/bin/gh;touch"
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["sh", str(repo_root / "scripts" / "repair-legacy-updater.sh")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "路径包含不安全字符" in result.stderr


def test_legacy_updater_repair_forces_noninteractive_oci_attestation(
    tmp_path: Path,
) -> None:
    result, env, wrapper_gh, _apk_marker, gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    assert result.returncode == 0, result.stderr

    invocation = subprocess.run(
        [
            str(wrapper_gh),
            "attestation",
            "verify",
            "oci://ghcr.io/anoyou/telepilot-web@sha256:deadbeef",
            "--repo",
            "Anoyou/Telebot",
            "--signer-workflow",
            "github.com/Anoyou/Telebot/.github/workflows/publish-images.yml",
            "--source-digest",
            "0123456789abcdef0123456789abcdef01234567",
            "--deny-self-hosted-runners",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert invocation.returncode == 0, invocation.stderr
    capture = gh_capture.read_text(encoding="utf-8")
    assert "GH_TOKEN=telepilot-oci-attestation-verification" in capture
    assert "GH_PROMPT_DISABLED=1" in capture
    assert "|--repo|Anoyou/Telebot" in capture
    assert "|--deny-self-hosted-runners" in capture
    assert capture.count("|--bundle-from-oci") == 1


def test_legacy_updater_repair_does_not_duplicate_oci_bundle_flag(
    tmp_path: Path,
) -> None:
    result, env, wrapper_gh, _apk_marker, gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    assert result.returncode == 0, result.stderr

    invocation = subprocess.run(
        [
            str(wrapper_gh),
            "attestation",
            "verify",
            "oci://ghcr.io/anoyou/telepilot-web@sha256:deadbeef",
            "--bundle-from-oci",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert invocation.returncode == 0, invocation.stderr
    assert (
        gh_capture.read_text(encoding="utf-8").count("|--bundle-from-oci") == 1
    )


def test_legacy_updater_repair_preserves_real_token(
    tmp_path: Path,
) -> None:
    result, env, wrapper_gh, _apk_marker, gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    env["GH_TOKEN"] = "configured-github-token"

    invocation = subprocess.run(
        [str(wrapper_gh), "attestation", "verify", "oci://example.invalid/image"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert invocation.returncode == 0, invocation.stderr
    assert "GH_TOKEN=configured-github-token" in gh_capture.read_text(
        encoding="utf-8"
    )


def test_legacy_updater_repair_maps_github_token_to_gh_token(
    tmp_path: Path,
) -> None:
    result, env, wrapper_gh, _apk_marker, gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    env["GITHUB_TOKEN"] = "configured-github-token"

    invocation = subprocess.run(
        [str(wrapper_gh), "attestation", "verify", "oci://example.invalid/image"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert invocation.returncode == 0, invocation.stderr
    capture = gh_capture.read_text(encoding="utf-8")
    assert "GH_TOKEN=configured-github-token" in capture


def test_legacy_updater_repair_passes_other_gh_commands_through(
    tmp_path: Path,
) -> None:
    result, env, wrapper_gh, _apk_marker, gh_capture = (
        _install_legacy_updater_repair(tmp_path)
    )
    assert result.returncode == 0, result.stderr

    invocation = subprocess.run(
        [str(wrapper_gh), "api", "user"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert invocation.returncode == 0, invocation.stderr
    capture = gh_capture.read_text(encoding="utf-8")
    assert "GH_TOKEN=\n" in capture
    assert "GH_PROMPT_DISABLED=\n" in capture
    assert "args=|api|user" in capture
    assert "--bundle-from-oci" not in capture


def test_updater_handoff_waits_for_health_and_rolls_back_image() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "prod-update.sh").read_text(encoding="utf-8")

    assert "TELEPILOT_TARGET_UPDATER_IMAGE" in script
    assert "TELEPILOT_OLD_UPDATER_IMAGE" in script
    assert "handoff rollback" in script
    assert "wait_compose_healthy docker-compose.yml updater 60" in script
    assert "finalize-update-job.py" in script
    assert 'emit_progress 98 "等待交接"' in script
    assert "while :; do" in script
    assert script.index('finalize succeeded "所有计划步骤与 updater handoff 已完成"') < script.index(
        '     rm -f "$pending_file"'
    )


def test_handoff_finalizer_atomically_marks_persisted_job(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    jobs = tmp_path / ".git" / "telepilot-update-jobs"
    jobs.mkdir(parents=True)
    job_path = jobs / "0123456789ab.json"
    job_path.write_text(
        json.dumps({"job_id": "0123456789ab", "status": "running", "progress": 96}),
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "finalize-update-job.py"),
            "--root",
            str(tmp_path),
            "--job-id",
            "0123456789ab",
            "--status",
            "succeeded",
            "--detail",
            "handoff 完成",
            "--commit",
            "a" * 40,
        ],
        check=True,
    )

    payload = json.loads(job_path.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert payload["progress"] == 100
    assert payload["new_commit"] == "a" * 12
    assert payload["error"] is None


def test_runtime_dockerfiles_preserve_incremental_build_caches() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    frontend_dockerfile = (repo_root / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    backend_dockerfile = (repo_root / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "id=telepilot-tsbuild" in frontend_dockerfile
    assert "pip install --no-deps ." not in backend_dockerfile


def test_updater_parses_structured_progress() -> None:
    updater = _load_updater_module()

    assert updater._parse_progress("@@TELEPILOT_PROGRESS@@78|健康检查|等待新容器 ready") == (
        78,
        "健康检查",
        "等待新容器 ready",
    )
    assert updater._parse_progress("ordinary log line") is None


def test_update_target_options_come_from_git(monkeypatch, tmp_path) -> None:
    updater = _load_updater_module()
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setattr(updater, "WORKSPACE", workspace)

    def fake_run(args, **_kwargs):  # noqa: ANN001
        if args == ["git", "remote"]:
            return "origin\nbackup", "", 0
        if args == ["git", "ls-remote", "--heads", "origin"]:
            return (
                "a\trefs/heads/main\nb\trefs/heads/codex/0.33-interaction-framework\n",
                "",
                0,
            )
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._update_target_options("origin")

    assert result == {
        "ok": True,
        "remotes": ["origin", "backup"],
        "branches": ["main", "codex/0.33-interaction-framework"],
        "remote": "origin",
    }


def test_console_logs_returns_partial_lines_when_compose_times_out(monkeypatch) -> None:
    updater = _load_updater_module()

    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda _args, **_kw: (
            "telepilot-web-1 | booting\ntelepilot-web-1 | ready",
            "command timed out",
            124,
        ),
    )

    result = updater._tail_console_logs("web", 20, None)

    assert result["ok"] is True
    assert result["project"] == "telepilot"
    assert result["lines"] == [
        "telepilot-web-1 | booting",
        "telepilot-web-1 | ready",
    ]
    assert "超时" in result["error"]


def test_console_logs_filters_internal_health_checks(monkeypatch) -> None:
    updater = _load_updater_module()

    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda _args, **_kw: (
            "\n".join(
                [
                    'updater-1  | 2026-07-08T13:57:45.254520371Z [updater] 127.0.0.1 "GET /health HTTP/1.1" 200 -',
                    'updater-1  | 2026-07-24T06:32:46Z [updater] 172.18.0.4 "GET /jobs/890e65215801 HTTP/1.1" 200 -',
                    'updater-1  | 2026-07-24T06:32:47Z [updater] 172.18.0.4 "GET /console-logs?service=all&tail=300 HTTP/1.1" 200 -',
                    'frontend-1 | 127.0.0.1 - - [08/Jul/2026:13:57:45 +0000] "GET / HTTP/1.1" 200 2662 "-" "Wget" "-"',
                    "web-1      | INFO:app.worker:真实业务日志",
                ]
            ),
            "",
            0,
        ),
    )

    result = updater._tail_console_logs("all", 20, None)

    assert result["ok"] is True
    assert result["lines"] == ["web-1      | INFO:app.worker:真实业务日志"]


def test_console_logs_filters_routine_info_noise_but_keeps_http_failures(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/TelePilot"))
    monkeypatch.setattr(
        updater,
        "_run",
        lambda _args, **_kw: (
            "\n".join(
                [
                    "web-1 | INFO:alembic.runtime.migration:Context impl PostgresqlImpl.",
                    "web-1 | INFO:alembic.runtime.migration:Running upgrade 0040 -> 0041",
                    "web-1 | 2026-07-23 22:45:28,309 [worker:1] INFO Got difference for channel 2304101980 updates",
                    'web-1 | INFO:httpx:HTTP Request: GET https://example.test/models "HTTP/1.1 204 No Content"',
                    'web-1 | INFO:httpx:HTTP Request: GET https://example.test/models "HTTP/1.1 503 Service Unavailable"',
                    "web-1 | WARNING:app.services.feature_service:配置异常",
                ]
            ),
            "",
            0,
        ),
    )

    result = updater._tail_console_logs("all", 20, None)

    assert result["lines"] == [
        "web-1 | INFO:alembic.runtime.migration:Running upgrade 0040 -> 0041",
        'web-1 | INFO:httpx:HTTP Request: GET https://example.test/models "HTTP/1.1 503 Service Unavailable"',
        "web-1 | WARNING:app.services.feature_service:配置异常",
    ]


def test_updater_handler_silences_only_successful_health_requests() -> None:
    updater = _load_updater_module()
    handler = object.__new__(updater.Handler)
    handler.path = "/health"
    handler.client_address = ("127.0.0.1", 8765)

    with patch("builtins.print") as mocked_print:
        updater.Handler.log_message(handler, '"GET /health HTTP/1.1" %s -', "200")
        mocked_print.assert_not_called()

        updater.Handler.log_message(handler, '"GET /health HTTP/1.1" %s -', "503")
        mocked_print.assert_called_once()


def test_updater_handler_silences_successful_internal_polling() -> None:
    updater = _load_updater_module()
    handler = object.__new__(updater.Handler)
    handler.client_address = ("172.18.0.4", 8765)

    with patch("builtins.print") as mocked_print:
        handler.path = "/jobs/890e65215801"
        updater.Handler.log_message(handler, '"GET /jobs/890e65215801 HTTP/1.1" %s -', "200")
        handler.path = "/console-logs?service=all&tail=300"
        updater.Handler.log_message(handler, '"GET /console-logs?service=all&tail=300 HTTP/1.1" %s -', "200")
        handler.path = "/resources"
        updater.Handler.log_message(handler, '"GET /resources HTTP/1.1" %s -', "200")
        mocked_print.assert_not_called()

        handler.path = "/jobs/890e65215801"
        updater.Handler.log_message(handler, '"GET /jobs/890e65215801 HTTP/1.1" %s -', "500")
        mocked_print.assert_called_once()


def test_console_logs_falls_back_to_labeled_containers_when_project_is_wrong(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path("/srv/TelePilot"))

    def fake_run(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["docker", "compose"]:
            return "", "", 0
        if args[:3] == ["docker", "ps", "-a"]:
            return "abc123|custom-web-1|custom|web|/srv/TelePilot", "", 0
        if args[:2] == ["docker", "logs"]:
            assert args[-1] == "abc123"
            return "2026-07-12T01:02:03Z INFO server ready", "", 0
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._tail_console_logs("all", 20, None)

    assert result["ok"] is True
    assert result["source"] == "docker_containers"
    assert result["project"] == "custom"
    assert result["services"] == ["web"]
    assert result["lines"] == ["web  | 2026-07-12T01:02:03Z INFO server ready"]


def test_console_logs_reports_ambiguous_compose_projects(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "HOST_PROJECT_DIR", Path())

    def fake_run(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["docker", "compose"]:
            return "", "", 0
        if args[:3] == ["docker", "ps", "-a"]:
            return (
                "\n".join(
                    [
                        "abc|one-web-1|one|web|/srv/one",
                        "def|two-web-1|two|web|/srv/two",
                    ]
                ),
                "",
                0,
            )
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._tail_console_logs("all", 20, None)

    assert result["ok"] is False
    assert "无法唯一识别" in result["error"]


def test_updater_rejects_requests_when_token_is_missing(monkeypatch) -> None:
    updater = _load_updater_module()
    handler = object.__new__(updater.Handler)
    handler.headers = {}
    monkeypatch.setattr(updater, "TOKEN", "")

    assert handler._authorized() is False


def test_updater_main_refuses_to_start_without_token(monkeypatch) -> None:
    updater = _load_updater_module()
    monkeypatch.setattr(updater, "TOKEN", "")

    try:
        updater.main()
    except SystemExit as exc:
        assert "UPDATER_TOKEN" in str(exc)
    else:
        raise AssertionError("updater must not start without UPDATER_TOKEN")


def test_updater_rejects_placeholder_and_short_tokens(monkeypatch) -> None:
    updater = _load_updater_module()

    for token in ("changeme-please-replace", "too-short"):
        monkeypatch.setattr(updater, "TOKEN", token)
        assert updater._token_configured() is False


def test_update_plan_retries_commit_with_pending_deployment(monkeypatch, tmp_path) -> None:
    updater = _load_updater_module()
    workspace = tmp_path / "repo"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "telepilot-deploy-pending").write_text("oldcommit targetcommit\n")
    monkeypatch.setattr(updater, "WORKSPACE", workspace)

    def fake_run(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["git", "fetch"]:
            return "", "", 0
        if args == ["git", "rev-parse", "HEAD"]:
            return "targetcommit", "", 0
        if args == ["git", "rev-parse", "refs/remotes/origin/main"]:
            return "targetcommit", "", 0
        if args == ["git", "rev-parse", "--git-path", "telepilot-deploy-pending"]:
            return ".git/telepilot-deploy-pending", "", 0
        if args[:3] == ["git", "cat-file", "-e"]:
            return "", "", 0
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return "", "", 0
        if args == ["git", "show", "targetcommit:backend/app/__init__.py"]:
            return '__version__ = "0.72.0-beta.2"', "", 0
        if args[:3] == ["git", "rev-list", "--count"]:
            return "0", "", 0
        if args[:2] == ["git", "log"]:
            assert args[-1] == "oldcommit..targetcommit"
            return "改进在线更新弹窗\n修复控制台噪声", "", 0
        if args[:2] == ["python", "backend/app/util/update_plan.py"]:
            assert args[-4:] == ["--old", "oldcommit", "--new", "targetcommit"]
            return (
                '{"changed_files":["scripts/prod-update.sh"],'
                '"components":["updater"],"services":["updater"],'
                '"requires_full_update":false,"requires_backup":false,'
                '"requires_migration":false}',
                "",
                0,
            )
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._check_plan("origin", "main")

    assert result["has_update"] is True
    assert result["deployment_pending"] is True
    assert result["deploy_from_commit"] == "oldcommit"
    assert result["current_version"] == "0.72.0-beta.2"
    assert result["target_version"] == "0.72.0-beta.2"
    assert result["commit_titles"] == ["改进在线更新弹窗", "修复控制台噪声"]


def test_update_plan_carries_pending_deployment_into_newer_target(
    monkeypatch, tmp_path
) -> None:
    updater = _load_updater_module()
    workspace = tmp_path / "repo"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "telepilot-deploy-pending").write_text(
        "beta8commit beta9commit\n", encoding="utf-8"
    )
    monkeypatch.setattr(updater, "WORKSPACE", workspace)

    def fake_run(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["git", "fetch"]:
            return "", "", 0
        if args == ["git", "rev-parse", "HEAD"]:
            return "beta9commit", "", 0
        if args == ["git", "rev-parse", "refs/remotes/origin/Beta"]:
            return "beta10commit", "", 0
        if args == ["git", "rev-parse", "--git-path", "telepilot-deploy-pending"]:
            return ".git/telepilot-deploy-pending", "", 0
        if args[:3] == ["git", "cat-file", "-e"]:
            return "", "", 0
        if args == [
            "git",
            "merge-base",
            "--is-ancestor",
            "beta9commit",
            "beta10commit",
        ]:
            return "", "", 0
        if args == ["git", "show", "beta9commit:backend/app/__init__.py"]:
            return '__version__ = "0.88.0-beta.9"', "", 0
        if args == ["git", "show", "beta10commit:backend/app/__init__.py"]:
            return '__version__ = "0.88.0-beta.10"', "", 0
        if args[:3] == ["git", "rev-list", "--count"]:
            return "1", "", 0
        if args[:2] == ["git", "log"]:
            assert args[-1] == "beta8commit..beta10commit"
            return "修复累计在线更新\n", "", 0
        if args[:2] == ["python", "backend/app/util/update_plan.py"]:
            assert args[-4:] == [
                "--old",
                "beta8commit",
                "--new",
                "beta10commit",
            ]
            return (
                '{"changed_files":["docker-compose.yml","frontend/src/App.tsx"],'
                '"components":["frontend"],"services":["frontend"],'
                '"requires_full_update":false,"requires_backup":false,'
                '"requires_migration":false}',
                "",
                0,
            )
        raise AssertionError(args)

    monkeypatch.setattr(updater, "_run", fake_run)

    result = updater._check_plan("origin", "Beta")

    assert result["has_update"] is True
    assert result["deployment_pending"] is True
    assert result["deploy_from_commit"] == "beta8commit"
    assert result["changed_files"] == [
        "docker-compose.yml",
        "frontend/src/App.tsx",
    ]
