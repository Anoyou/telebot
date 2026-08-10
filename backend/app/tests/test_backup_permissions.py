"""生产备份脚本的默认权限回归测试。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path


def _restore_volume_shell(repo_root: Path) -> str:
    script = (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8")
    start_marker = "    sh -eu -c '\n"
    end_marker = "\n    ' sh \"$archive_name\" \"$strip_components\""
    start = script.index(start_marker) + len(start_marker)
    end = script.index(end_marker, start)
    return script[start:end]


def test_updater_mounts_backup_directory_at_the_same_host_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    compose = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")

    backup_path = "${BACKUP_DIR:-/var/backups/telepilot}"
    assert f"BACKUP_DIR: {backup_path}" in compose
    assert f"- {backup_path}:{backup_path}" in compose


def test_backup_script_creates_private_directory_and_artifacts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2" == "compose version" ]]; then exit 0; fi
if [[ "$1 $2 $3 $4" == "compose ps -q web" ]]; then echo web-test; exit 0; fi
if [[ "$1" == "inspect" && "$3" == "--format" ]]; then
  if [[ "$4" == *State.Running* ]]; then echo true; else echo /tmp/fake-volume; fi
  exit 0
fi
if [[ "$1 $2 $3" == "compose exec -T" ]]; then echo '-- fake pg dump'; exit 0; fi
if [[ "$1" == "run" ]]; then
  backup_dir=""
  archive=""
  for arg in "$@"; do
    [[ "$arg" == *:/backup ]] && backup_dir="${arg%:/backup}"
    [[ "$arg" == /backup/* ]] && archive="${arg#/backup/}"
  done
  mkdir -p "$backup_dir/.empty"
  tar czf "$backup_dir/$archive" -C "$backup_dir/.empty" .
  exit 0
fi
echo "unexpected docker invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    backup_dir = tmp_path / "backups"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_RETENTION_DAYS": "30",
            "BACKUP_RETENTION_COUNT": "7",
        }
    )
    subprocess.run(
        ["bash", str(repo_root / "deploy" / "backup.sh")],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    artifacts = [path for path in backup_dir.iterdir() if path.is_file()]
    assert len(artifacts) == 5
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in artifacts)


def test_backup_script_keeps_only_recent_complete_sets(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for day in range(1, 9):
        stamp = f"202607{day:02d}-010101"
        for name in (
            f"db-{stamp}.sql",
            f"sessions-{stamp}.tgz",
            f"plugins-installed-{stamp}.tgz",
            f"plugin-repos-{stamp}.tgz",
            f"checksums-{stamp}.sha256",
        ):
            (backup_dir / name).write_text("fixture", encoding="utf-8")

    script = (repo_root / "deploy" / "backup.sh").read_text(encoding="utf-8")
    function_block = script[script.index("remove_backup_set()") : script.index("WEB_CONTAINER=")]
    command = f'set -euo pipefail\nDIR="{backup_dir}"\nRETENTION_COUNT=7\n{function_block}\n'
    subprocess.run(["bash", "-c", command], check=True)

    checksum_files = sorted(backup_dir.glob("checksums-*.sha256"))
    assert len(checksum_files) == 6
    assert checksum_files[0].name == "checksums-20260703-010101.sha256"


def test_restore_script_accepts_database_only_without_sessions_archive(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "compose ps -q web" ]]; then echo web-test; exit 0; fi
if [[ "${1:-} ${2:-}" == "compose stop" || "${1:-} ${2:-}" == "compose start" ]]; then exit 0; fi
if [[ "${1:-} ${2:-} ${3:-}" == "compose exec -T" ]]; then exit 0; fi
echo "unexpected docker invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump)],
        cwd=project_root,
        env=env,
        input="yes\n",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "恢复完成" in result.stdout


def test_restore_script_rejects_checksum_file_missing_selected_archive(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-20260810-010101.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    checksum = tmp_path / "checksums-20260810-010101.sha256"
    checksum.write_text(f"{'0' * 64}  sessions-20260810-010101.tgz\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "校验文件缺少条目" in result.stderr


def test_restore_script_rejects_duplicate_checksum_entry(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-20260810-011111.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    digest = hashlib.sha256(db_dump.read_bytes()).hexdigest()
    checksum = tmp_path / "checksums-20260810-011111.sha256"
    checksum.write_text(
        f"{digest}  {db_dump.name}\n{digest} *{db_dump.name}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "校验文件包含重复条目" in result.stderr


def test_restore_script_rejects_archive_path_traversal_before_docker(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    archive = tmp_path / "sessions-test.tgz"
    payload = tmp_path / "payload.txt"
    payload.write_text("unsafe\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="../outside.txt")

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), str(archive)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "归档包含越界路径" in result.stderr


def test_restore_script_rejects_symlink_archive_entry_before_docker(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    archive = tmp_path / "sessions-test.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        link = tarfile.TarInfo("plugin/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        bundle.addfile(link)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), str(archive)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "归档包含链接或特殊文件" in result.stderr


def test_restore_script_rejects_hardlink_archive_entry_before_docker(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    archive = tmp_path / "plugins-test.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        original = tarfile.TarInfo("plugin/original.txt")
        original.size = 0
        bundle.addfile(original)
        link = tarfile.TarInfo("plugin/hardlink.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = "plugin/original.txt"
        bundle.addfile(link)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), "-", str(archive)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "归档包含链接或特殊文件" in result.stderr


def test_restore_script_rejects_fifo_archive_entry_before_docker(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    archive = tmp_path / "plugins-test.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        fifo = tarfile.TarInfo("plugin/fifo")
        fifo.type = tarfile.FIFOTYPE
        bundle.addfile(fifo)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), "-", str(archive)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "归档包含链接或特殊文件" in result.stderr


def test_restore_script_rejects_corrupt_archive_before_docker(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    archive = tmp_path / "sessions-test.tgz"
    archive.write_bytes(b"not a gzip archive")

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), str(archive)],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "归档无法读取或已损坏" in result.stderr


def test_restore_script_rejects_empty_archive_before_docker(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    archive = tmp_path / "sessions-test.tgz"
    with tarfile.open(archive, "w:gz"):
        pass

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_marker = tmp_path / "docker-invoked"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
touch "$DOCKER_MARKER"
exit 99
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["DOCKER_MARKER"] = str(docker_marker)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), str(archive)],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "归档不包含任何成员，拒绝恢复" in result.stderr
    assert not docker_marker.exists()


def test_restore_script_skips_sessions_and_restores_plugins_archive(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-20260810-020202.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    plugin_archive = tmp_path / "plugins-installed-20260810-020202.tgz"
    payload = tmp_path / "plugin.txt"
    payload.write_text("plugin\n", encoding="utf-8")
    with tarfile.open(plugin_archive, "w:gz") as bundle:
        bundle.add(payload, arcname="./plugin.txt")
    checksum = tmp_path / "checksums-20260810-020202.sha256"
    checksum.write_text(
        "\n".join(
            (
                f"{hashlib.sha256(db_dump.read_bytes()).hexdigest()}  {db_dump.name}",
                f"{hashlib.sha256(plugin_archive.read_bytes()).hexdigest()}  {plugin_archive.name}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "compose ps -q web" ]]; then echo web-test; exit 0; fi
if [[ "${1:-} ${2:-}" == "compose stop" || "${1:-} ${2:-}" == "compose start" ]]; then exit 0; fi
if [[ "${1:-} ${2:-} ${3:-}" == "compose exec -T" ]]; then exit 0; fi
if [[ "${1:-}" == "inspect" ]]; then echo /tmp/fake-volume; exit 0; fi
if [[ "${1:-}" == "run" ]]; then exit 0; fi
echo "unexpected docker invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(docker_log)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump), "-", str(plugin_archive)],
        cwd=project_root,
        env=env,
        input="yes\n",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "恢复完成" in result.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert plugin_archive.name in docker_calls
    assert "sessions-" not in docker_calls
    run_call = docker_calls[docker_calls.index("run --rm") :]
    assert run_call.index("tar xzf") < run_call.index("phase=move_old")
    assert ".telepilot-restore-stage." in run_call
    assert ".telepilot-restore-previous." in run_call


def test_restore_volume_partial_old_move_restores_every_original_file(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backup"
    fake_bin = tmp_path / "bin"
    data_dir.mkdir()
    backup_dir.mkdir()
    fake_bin.mkdir()
    for name in ("old-a.txt", "old-b.txt", "old-c.txt"):
        (data_dir / name).write_text(f"{name}\n", encoding="utf-8")

    archive = backup_dir / "plugins.tgz"
    new_file = tmp_path / "new.txt"
    new_file.write_text("new\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(new_file, arcname="new.txt")

    real_mv = shutil.which("mv")
    assert real_mv is not None
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        f"""#!/bin/sh
count=$(cat "$MV_COUNT_FILE")
count=$((count + 1))
printf '%s\\n' "$count" > "$MV_COUNT_FILE"
if [ "$count" -eq 2 ]; then
  exit 73
fi
exec "{real_mv}" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    count_file = tmp_path / "mv-count"
    count_file.write_text("0\n", encoding="utf-8")

    volume_shell = _restore_volume_shell(repo_root)
    volume_shell = volume_shell.replace("/data", str(data_dir))
    volume_shell = volume_shell.replace("/backup", str(backup_dir))
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MV_COUNT_FILE"] = str(count_file)

    result = subprocess.run(
        ["sh", "-eu", "-c", volume_shell, "sh", archive.name, "0"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert sorted(path.name for path in data_dir.iterdir()) == [
        "old-a.txt",
        "old-b.txt",
        "old-c.txt",
    ]
    assert all(
        (data_dir / name).read_text(encoding="utf-8") == f"{name}\n"
        for name in ("old-a.txt", "old-b.txt", "old-c.txt")
    )


def test_restore_volume_partial_new_move_restores_every_original_file(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backup"
    fake_bin = tmp_path / "bin"
    data_dir.mkdir()
    backup_dir.mkdir()
    fake_bin.mkdir()
    for name in ("old-a.txt", "old-b.txt"):
        (data_dir / name).write_text(f"{name}\n", encoding="utf-8")

    archive = backup_dir / "plugins.tgz"
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    for name in ("new-a.txt", "new-b.txt"):
        (new_dir / name).write_text(f"{name}\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(new_dir.iterdir()):
            bundle.add(path, arcname=path.name)

    real_mv = shutil.which("mv")
    assert real_mv is not None
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        f"""#!/bin/sh
count=$(cat "$MV_COUNT_FILE")
count=$((count + 1))
printf '%s\\n' "$count" > "$MV_COUNT_FILE"
if [ "$count" -eq 4 ]; then
  exit 74
fi
exec "{real_mv}" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    count_file = tmp_path / "mv-count"
    count_file.write_text("0\n", encoding="utf-8")

    volume_shell = _restore_volume_shell(repo_root)
    volume_shell = volume_shell.replace("/data", str(data_dir))
    volume_shell = volume_shell.replace("/backup", str(backup_dir))
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MV_COUNT_FILE"] = str(count_file)

    result = subprocess.run(
        ["sh", "-eu", "-c", volume_shell, "sh", archive.name, "0"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert sorted(path.name for path in data_dir.iterdir()) == [
        "old-a.txt",
        "old-b.txt",
    ]
    assert all(
        (data_dir / name).read_text(encoding="utf-8") == f"{name}\n"
        for name in ("old-a.txt", "old-b.txt")
    )


def test_restore_volume_keeps_recovery_directories_when_rollback_move_fails(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backup"
    fake_bin = tmp_path / "bin"
    data_dir.mkdir()
    backup_dir.mkdir()
    fake_bin.mkdir()
    for name in ("old-a.txt", "old-b.txt"):
        (data_dir / name).write_text(f"{name}\n", encoding="utf-8")

    archive = backup_dir / "plugins.tgz"
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    for name in ("new-a.txt", "new-b.txt"):
        (new_dir / name).write_text(f"{name}\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(new_dir.iterdir()):
            bundle.add(path, arcname=path.name)

    real_mv = shutil.which("mv")
    assert real_mv is not None
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        f"""#!/bin/sh
count=$(cat "$MV_COUNT_FILE")
count=$((count + 1))
printf '%s\\n' "$count" > "$MV_COUNT_FILE"
if [ "$count" -eq 4 ] || [ "$count" -eq 5 ]; then
  exit 75
fi
exec "{real_mv}" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    count_file = tmp_path / "mv-count"
    count_file.write_text("0\n", encoding="utf-8")

    volume_shell = _restore_volume_shell(repo_root)
    volume_shell = volume_shell.replace("/data", str(data_dir))
    volume_shell = volume_shell.replace("/backup", str(backup_dir))
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MV_COUNT_FILE"] = str(count_file)

    result = subprocess.run(
        ["sh", "-eu", "-c", volume_shell, "sh", archive.name, "0"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "卷回滚未完整完成" in result.stderr
    stage_dirs = list(data_dir.glob(".telepilot-restore-stage.*"))
    previous_dirs = list(data_dir.glob(".telepilot-restore-previous.*"))
    assert len(stage_dirs) == 1
    assert len(previous_dirs) == 1
    recovered_old_files = {
        path.name
        for root in (data_dir, previous_dirs[0])
        for path in root.iterdir()
        if path.is_file() and path.name.startswith("old-")
    }
    assert recovered_old_files == {"old-a.txt", "old-b.txt"}


def test_restore_volume_term_rolls_back_and_exits_with_signal_status(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backup"
    fake_bin = tmp_path / "bin"
    data_dir.mkdir()
    backup_dir.mkdir()
    fake_bin.mkdir()
    for name in ("old-a.txt", "old-b.txt"):
        (data_dir / name).write_text(f"{name}\n", encoding="utf-8")

    archive = backup_dir / "plugins.tgz"
    new_file = tmp_path / "new.txt"
    new_file.write_text("new\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(new_file, arcname="new.txt")

    real_mv = shutil.which("mv")
    assert real_mv is not None
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        f"""#!/bin/sh
count=$(cat "$MV_COUNT_FILE")
count=$((count + 1))
printf '%s\\n' "$count" > "$MV_COUNT_FILE"
if [ "$count" -eq 1 ]; then
  kill -TERM "$PPID"
  exit 0
fi
exec "{real_mv}" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    count_file = tmp_path / "mv-count"
    count_file.write_text("0\n", encoding="utf-8")

    volume_shell = _restore_volume_shell(repo_root)
    volume_shell = volume_shell.replace("/data", str(data_dir))
    volume_shell = volume_shell.replace("/backup", str(backup_dir))
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MV_COUNT_FILE"] = str(count_file)

    result = subprocess.run(
        ["sh", "-eu", "-c", volume_shell, "sh", archive.name, "0"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 143
    assert sorted(path.name for path in data_dir.iterdir()) == [
        "old-a.txt",
        "old-b.txt",
    ]


def test_restore_volume_int_rolls_back_and_exits_with_signal_status(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backup"
    fake_bin = tmp_path / "bin"
    data_dir.mkdir()
    backup_dir.mkdir()
    fake_bin.mkdir()
    for name in ("old-a.txt", "old-b.txt"):
        (data_dir / name).write_text(f"{name}\n", encoding="utf-8")

    archive = backup_dir / "plugins.tgz"
    new_file = tmp_path / "new.txt"
    new_file.write_text("new\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(new_file, arcname="new.txt")

    real_mv = shutil.which("mv")
    assert real_mv is not None
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        f"""#!/bin/sh
count=$(cat "$MV_COUNT_FILE")
count=$((count + 1))
printf '%s\\n' "$count" > "$MV_COUNT_FILE"
if [ "$count" -eq 1 ]; then
  kill -INT "$PPID"
  exit 0
fi
exec "{real_mv}" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    count_file = tmp_path / "mv-count"
    count_file.write_text("0\n", encoding="utf-8")

    volume_shell = _restore_volume_shell(repo_root)
    volume_shell = volume_shell.replace("/data", str(data_dir))
    volume_shell = volume_shell.replace("/backup", str(backup_dir))
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["MV_COUNT_FILE"] = str(count_file)

    result = subprocess.run(
        ["sh", "-eu", "-c", volume_shell, "sh", archive.name, "0"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 130
    assert sorted(path.name for path in data_dir.iterdir()) == [
        "old-a.txt",
        "old-b.txt",
    ]


def test_restore_script_restarts_web_and_warns_after_database_restore_failure(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "compose ps -q web" ]]; then echo web-test; exit 0; fi
if [[ "${1:-} ${2:-}" == "compose stop" || "${1:-} ${2:-}" == "compose start" ]]; then exit 0; fi
if [[ "${1:-} ${2:-} ${3:-}" == "compose exec -T" ]]; then
  [[ "$*" == *"-d postgres"* ]] && exit 0
  exit 23
fi
echo "unexpected docker invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(docker_log)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump)],
        cwd=project_root,
        env=env,
        input="yes\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23
    assert "恢复未完成" in result.stderr
    assert "恢复完成" not in result.stdout
    assert "compose start web" in docker_log.read_text(encoding="utf-8")


def test_restore_script_reports_completed_data_when_initial_web_restart_fails(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    start_count = tmp_path / "start-count"
    start_count.write_text("0\n", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "compose ps -q web" ]]; then echo web-test; exit 0; fi
if [[ "${1:-} ${2:-}" == "compose stop" ]]; then exit 0; fi
if [[ "${1:-} ${2:-}" == "compose start" ]]; then
  count=$(cat "$START_COUNT_FILE")
  count=$((count + 1))
  printf '%s\n' "$count" > "$START_COUNT_FILE"
  [[ "$count" -gt 1 ]] && exit 0
  exit 31
fi
if [[ "${1:-} ${2:-} ${3:-}" == "compose exec -T" ]]; then exit 0; fi
echo "unexpected docker invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["START_COUNT_FILE"] = str(start_count)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump)],
        cwd=project_root,
        env=env,
        input="yes\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 31
    assert "数据恢复已完成，但首次启动 Web 失败" in result.stderr
    assert "已重试启动成功" in result.stderr
    assert "恢复未完成" not in result.stderr
    assert start_count.read_text(encoding="utf-8").strip() == "2"


def test_restore_script_reports_completed_data_when_both_web_starts_fail(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    project_root = tmp_path / "project"
    deploy_dir = project_root / "deploy"
    deploy_dir.mkdir(parents=True)
    restore_script = deploy_dir / "restore.sh"
    restore_script.write_text(
        (repo_root / "deploy" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_dump = tmp_path / "db-test.sql"
    db_dump.write_text("-- database fixture\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    start_count = tmp_path / "start-count"
    start_count.write_text("0\n", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "compose ps -q web" ]]; then echo web-test; exit 0; fi
if [[ "${1:-} ${2:-}" == "compose stop" ]]; then exit 0; fi
if [[ "${1:-} ${2:-}" == "compose start" ]]; then
  count=$(cat "$START_COUNT_FILE")
  count=$((count + 1))
  printf '%s\n' "$count" > "$START_COUNT_FILE"
  exit 31
fi
if [[ "${1:-} ${2:-} ${3:-}" == "compose exec -T" ]]; then exit 0; fi
echo "unexpected docker invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["START_COUNT_FILE"] = str(start_count)

    result = subprocess.run(
        ["bash", str(restore_script), str(db_dump)],
        cwd=project_root,
        env=env,
        input="yes\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 31
    assert "数据恢复已完成，但 Web 启动失败" in result.stderr
    assert "请手动启动并检查健康状态" in result.stderr
    assert "恢复未完成" not in result.stderr
    assert start_count.read_text(encoding="utf-8").strip() == "2"
