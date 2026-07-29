#!/usr/bin/env python3
"""由独立 updater handoff 容器原子收尾持久化更新任务。"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path


def finalize_job(
    root: Path,
    job_id: str,
    status: str,
    detail: str,
    commit: str | None,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ValueError("job_id 格式无效")
    if status not in {"succeeded", "failed"}:
        raise ValueError("status 必须是 succeeded 或 failed")
    path = root.resolve() / ".git" / "telepilot-update-jobs" / f"{job_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("任务状态不是 JSON 对象")
    now = int(time.time())
    payload.update(
        {
            "status": status,
            "finished_at": now,
            "returncode": 0 if status == "succeeded" else 1,
            "progress": 100 if status == "succeeded" else int(payload.get("progress") or 96),
            "phase": "更新完成" if status == "succeeded" else "更新失败",
            "detail": detail,
            "summary": "更新完成。" if status == "succeeded" else "更新失败，请查看日志。",
            "error": None if status == "succeeded" else detail,
        }
    )
    if commit:
        payload["new_commit"] = commit[:12]
    logs = list(payload.get("logs") or [])
    logs.append(f"[handoff] {detail}")
    payload["logs"] = logs[-240:]
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="收尾 TelePilot updater handoff 任务")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--status", required=True, choices=("succeeded", "failed"))
    parser.add_argument("--detail", required=True)
    parser.add_argument("--commit")
    args = parser.parse_args()
    finalize_job(args.root, args.job_id, args.status, args.detail, args.commit)


if __name__ == "__main__":
    main()
