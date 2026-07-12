"""生产备份脚本的默认权限回归测试。"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


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
