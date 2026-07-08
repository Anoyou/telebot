"""TelePilot internal production updater.

This service is intentionally only exposed on the Docker Compose private
network.  The public Web UI calls the authenticated backend; the backend then
talks to this sidecar with a shared token.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

WORKSPACE = Path(os.getenv("TELEPILOT_WORKSPACE", "/workspace")).resolve()
DEFAULT_REMOTE = os.getenv("TELEPILOT_UPDATE_REMOTE", "origin")
DEFAULT_BRANCH = os.getenv("TELEPILOT_UPDATE_BRANCH", "").strip() or "main"
TOKEN = os.getenv("UPDATER_TOKEN", "").strip()
MAX_LOG_LINES = 240
MAX_CONSOLE_LOG_LINES = 1000
CONSOLE_LOG_SERVICES = ("web", "frontend", "postgres", "redis", "updater")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_DOC_SUFFIXES = (".md", ".rst", ".txt")
_FULL_UPDATE_BASENAMES = {
    ".dockerignore",
    "docker-compose.yml",
    "docker-compose.dev.yml",
    "docker-compose.prod.yml",
    "Dockerfile",
    "Makefile",
    ".npmrc",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
}
_FULL_UPDATE_PREFIXES = ("deploy/", "scripts/", "scripts/deploy", "scripts/prod")

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_apply_lock = threading.Lock()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _run(args: list[str], *, timeout: int = 60, env: dict[str, str] | None = None) -> tuple[str, str, int]:
    try:
        result = subprocess.run(
            args,
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return result.stdout.strip(), result.stderr.strip(), int(result.returncode)
    except subprocess.TimeoutExpired:
        return "", "command timed out", 124
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}", 1


def _int_query(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _console_services(raw: str | None) -> list[str]:
    if not raw or raw == "all":
        return []
    services = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [item for item in services if item not in CONSOLE_LOG_SERVICES]
    if unknown:
        raise ValueError(f"不支持的服务: {', '.join(unknown)}")
    return services


def _tail_console_logs(service: str | None, tail: int, keyword: str | None) -> dict[str, Any]:
    try:
        services = _console_services(service)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "services": list(CONSOLE_LOG_SERVICES), "lines": []}

    cmd = ["docker", "compose", "logs", "--no-color", "--timestamps", f"--tail={tail}", *services]
    out, err, rc = _run(cmd, timeout=12)
    if rc != 0:
        return {
            "ok": False,
            "error": err or out or f"docker compose logs 退出码 {rc}",
            "services": services or list(CONSOLE_LOG_SERVICES),
            "lines": [],
        }
    lines = [_ANSI_RE.sub("", line.rstrip()) for line in out.splitlines()]
    q = (keyword or "").strip().lower()
    if q:
        lines = [line for line in lines if q in line.lower()]
    return {
        "ok": True,
        "source": "docker_compose",
        "services": services or list(CONSOLE_LOG_SERVICES),
        "tail": tail,
        "lines": lines[-MAX_CONSOLE_LOG_LINES:],
    }


def _normalize_changed_file(path: str) -> str:
    return path.strip().lstrip("./")


def _is_docs_file(path: str) -> bool:
    normalized = _normalize_changed_file(path)
    lowered = normalized.lower()
    name = Path(normalized).name.lower()
    if normalized in {"CHANGELOG.md", "docs/PLUGIN-DEV-GUIDE.md"}:
        return False
    return lowered.startswith("docs/") or lowered.startswith("readme") or name.endswith(_DOC_SUFFIXES)


def _is_full_update_file(path: str) -> bool:
    normalized = _normalize_changed_file(path)
    name = Path(normalized).name
    if name in _FULL_UPDATE_BASENAMES:
        return True
    return any(normalized.startswith(prefix) for prefix in _FULL_UPDATE_PREFIXES)


def _classify_changed_files(changed_files: list[str]) -> tuple[list[str], bool, bool]:
    files = [_normalize_changed_file(path) for path in changed_files if path.strip()]
    if not files:
        return ["none"], False, False

    requires_full_update = any(_is_full_update_file(path) for path in files)
    requires_backup = any(path.startswith("backend/alembic/versions/") for path in files)
    if all(_is_docs_file(path) for path in files):
        components = ["docs_only"]
    else:
        components: list[str] = []
        if any(path.startswith("frontend/") or path in {"CHANGELOG.md", "docs/PLUGIN-DEV-GUIDE.md"} for path in files):
            components.append("frontend")
        if any(path.startswith("backend/") or path.startswith("plugins/") for path in files):
            components.append("backend")
        if not components:
            components.append("docs_only")
    if requires_full_update:
        components = ["full_update", *[x for x in components if x != "full_update"]]
    return components, requires_full_update, requires_backup


def _check_plan(remote: str, branch: str) -> dict[str, Any]:
    if not (WORKSPACE / ".git").exists():
        return {
            "ok": False,
            "error": f"{WORKSPACE} 不是 Git 工作树，无法自更新。",
            "runtime_mode": "prod_container_with_updater",
        }
    remote_ref = f"refs/remotes/{remote}/{branch}"
    out, err, rc = _run(["git", "fetch", remote, f"{branch}:{remote_ref}"], timeout=120)
    if rc != 0:
        return {"ok": False, "error": f"git fetch 失败: {err or out}"}
    current_out, err, rc = _run(["git", "rev-parse", "HEAD"], timeout=10)
    if rc != 0:
        return {"ok": False, "error": f"读取当前 commit 失败: {err or current_out}"}
    target_out, err, rc = _run(["git", "rev-parse", remote_ref], timeout=10)
    if rc != 0:
        return {"ok": False, "error": f"读取远程 commit 失败: {err or target_out}"}
    behind_out, _, behind_rc = _run(["git", "rev-list", "--count", f"{current_out}..{target_out}"], timeout=10)
    behind = int(behind_out) if behind_rc == 0 and behind_out.isdigit() else 0
    changed_out, _, changed_rc = _run(["git", "diff", "--name-only", f"{current_out}..{target_out}"], timeout=30)
    changed_files = changed_out.splitlines()[:120] if changed_rc == 0 and changed_out else []
    components, requires_full_update, requires_backup = _classify_changed_files(changed_files)
    return {
        "ok": True,
        "remote": remote,
        "branch": branch,
        "current_commit": current_out[:12],
        "remote_commit": target_out[:12],
        "has_update": current_out != target_out and behind > 0,
        "ahead": behind,
        "changed_files": changed_files,
        "components": components,
        "requires_full_update": requires_full_update,
        "requires_backup": requires_backup,
    }


def _job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _set_job(job_id: str, **updates: Any) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {"job_id": job_id, "logs": []})
        job.update(updates)


def _append_job_log(job_id: str, line: str) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {"job_id": job_id, "logs": []})
        logs = list(job.get("logs") or [])
        logs.append(line.rstrip())
        job["logs"] = logs[-MAX_LOG_LINES:]


def _run_apply_job(job_id: str, remote: str, branch: str, force_full: bool) -> None:
    if not _apply_lock.acquire(blocking=False):
        _set_job(job_id, status="failed", finished_at=int(time.time()), error="已有更新任务正在执行。")
        return
    try:
        _set_job(job_id, status="running", started_at=int(time.time()), remote=remote, branch=branch)
        plan = _check_plan(remote, branch)
        _set_job(job_id, plan=plan)
        if not plan.get("ok"):
            _set_job(job_id, status="failed", finished_at=int(time.time()), error=plan.get("error") or "更新检查失败")
            return
        if not plan.get("has_update"):
            _set_job(job_id, status="succeeded", finished_at=int(time.time()), returncode=0, summary="当前已是最新版本。")
            return
        env = {
            "TELEPILOT_UPDATE_REMOTE": remote,
            "TELEPILOT_UPDATE_BRANCH": branch,
            "TELEPILOT_HOST_PROJECT_DIR": os.getenv("TELEPILOT_HOST_PROJECT_DIR", str(WORKSPACE)),
            "TELEPILOT_SKIP_UPDATER_RECREATE": "1",
        }
        cmd = ["bash", "scripts/prod-update.sh"]
        if force_full:
            cmd.append("--full")
        _append_job_log(job_id, f"$ {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=str(WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, **env},
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_job_log(job_id, line)
        rc = proc.wait()
        head, _, _ = _run(["git", "rev-parse", "HEAD"], timeout=10)
        _set_job(
            job_id,
            status="succeeded" if rc == 0 else "failed",
            finished_at=int(time.time()),
            returncode=rc,
            new_commit=head[:12] if head else None,
            summary="更新完成。" if rc == 0 else "更新失败，请查看日志。",
            error=None if rc == 0 else f"prod-update 退出码 {rc}",
        )
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, status="failed", finished_at=int(time.time()), error=f"{type(exc).__name__}: {exc}")
    finally:
        _apply_lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "TelePilotUpdater/1.0"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("X-TelePilot-Updater-Token", "") == TOKEN

    def _read_json(self) -> dict[str, Any]:
        raw_len = int(self.headers.get("Content-Length") or "0")
        if raw_len <= 0:
            return {}
        data = self.rfile.read(raw_len)
        try:
            parsed = json.loads(data.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            _json_response(self, 200, {"ok": True})
            return
        if path == "/console-logs":
            if not self._authorized():
                _json_response(self, 403, {"ok": False, "error": "forbidden"})
                return
            qs = parse_qs(parsed.query)
            tail = _int_query((qs.get("tail") or [None])[0], 300, 20, MAX_CONSOLE_LOG_LINES)
            service = (qs.get("service") or [None])[0]
            keyword = (qs.get("keyword") or [None])[0]
            result = _tail_console_logs(service, tail, keyword)
            _json_response(self, 200 if result.get("ok") else 400, result)
            return
        if path.startswith("/jobs/"):
            if not self._authorized():
                _json_response(self, 403, {"ok": False, "error": "forbidden"})
                return
            job_id = path.rsplit("/", 1)[-1]
            job = _job_snapshot(job_id)
            if job is None:
                _json_response(self, 404, {"ok": False, "error": "job not found"})
                return
            _json_response(self, 200, {"ok": True, **job})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            _json_response(self, 403, {"ok": False, "error": "forbidden"})
            return
        payload = self._read_json()
        remote = str(payload.get("remote") or DEFAULT_REMOTE).strip() or DEFAULT_REMOTE
        branch = str(payload.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
        if self.path == "/check":
            _json_response(self, 200, _check_plan(remote, branch))
            return
        if self.path == "/jobs":
            job_id = uuid.uuid4().hex[:12]
            _set_job(
                job_id,
                status="queued",
                created_at=int(time.time()),
                remote=remote,
                branch=branch,
                logs=[],
            )
            thread = threading.Thread(
                target=_run_apply_job,
                args=(job_id, remote, branch, bool(payload.get("full"))),
                daemon=True,
            )
            thread.start()
            _json_response(self, 202, {"ok": True, "job_id": job_id, "status": "queued"})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[updater] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    host = os.getenv("UPDATER_HOST", "0.0.0.0")
    port = int(os.getenv("UPDATER_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"TelePilot updater listening on {host}:{port}, workspace={WORKSPACE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
