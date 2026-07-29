"""安全读取 Web/容器控制台日志，供日志 API 与 System Agent 共用。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from ..settings import PROJECT_ROOT
from .redactor import redact_text

SYSTEM_CONSOLE_SERVICES = frozenset({"all", "web", "frontend", "postgres", "redis", "updater"})
LOCAL_CONSOLE_FILES: dict[str, Path] = {
    "web": PROJECT_ROOT / "logs" / "backend.log",
    "frontend": PROJECT_ROOT / "logs" / "frontend.log",
}

ConsoleFetcher = Callable[[str, int], dict[str, Any]]


def _updater_token() -> str:
    return (os.getenv("TELEPILOT_UPDATER_TOKEN") or "").strip()


def fetch_updater_console_logs(service: str, tail: int) -> dict[str, Any]:
    raw_url = (os.getenv("TELEPILOT_UPDATER_URL") or "").strip().rstrip("/")
    if not raw_url:
        raise RuntimeError("内部 updater 未配置")
    params = urllib_parse.urlencode({"service": service, "tail": str(tail)})
    headers: dict[str, str] = {}
    token = _updater_token()
    if token:
        headers["X-TelePilot-Updater-Token"] = token
    req = urllib_request.Request(f"{raw_url}/console-logs?{params}", headers=headers, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=12) as resp:  # noqa: S310 - internal configured URL
            response_text = resp.read().decode("utf-8", errors="ignore")
    except urllib_error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(response_text)
        except Exception:
            parsed = {"ok": False, "error": response_text or str(exc)}
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": str(exc)}
    parsed = json.loads(response_text) if response_text else {}
    return parsed if isinstance(parsed, dict) else {}


def is_console_noise_line(line: str) -> bool:
    lowered = line.lower()
    is_alembic_info = ("info:" in lowered or "info  [" in lowered) and "alembic.runtime." in lowered
    if is_alembic_info and any(
        marker in lowered
        for marker in (
            "context impl postgresqlimpl",
            "will assume transactional ddl",
            "setup plugin alembic.autogenerate",
        )
    ):
        return True
    if re.search(r"\[worker:\d+\]\s+info\s+got difference for channel \d+ updates", lowered):
        return True
    if "info:httpx:http request:" in lowered:
        return bool(re.search(r'"http/[^"]+\s+2\d\d(?:\s+[^"]+)?"', lowered))
    is_updater_poll = "[updater]" in lowered and (
        '"get /health http/' in lowered
        or '"get /console-logs?' in lowered
        or bool(re.search(r'"get /jobs/[^ ]+ http/', lowered))
    )
    is_internal_healthz = "127.0.0.1" in lowered and '"get /healthz http/' in lowered
    if not (is_updater_poll or is_internal_healthz):
        return False
    return any(f" {status} " in lowered for status in range(200, 300))


def filter_console_payload(payload: dict[str, Any], keyword: str | None) -> dict[str, Any]:
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        return payload
    lines = [line for line in raw_lines if not is_console_noise_line(str(line))]
    query = (keyword or "").strip().lower()
    if query:
        lines = [line for line in lines if query in str(line).lower()]
    return {**payload, "lines": lines}


def _tail_file(path: Path, max_lines: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as file_handle:
        return [line.rstrip("\n") for line in deque(file_handle, maxlen=max_lines)]


def read_local_console_logs(
    service: str,
    tail: int,
    keyword: str | None,
    *,
    local_files: dict[str, Path] | None = None,
) -> dict[str, Any]:
    files = local_files if local_files is not None else LOCAL_CONSOLE_FILES
    services = ["web", "frontend"] if service == "all" else [service]
    lines: list[str] = []
    for name in services:
        path = files.get(name)
        if path is None:
            continue
        prefix = f"{name}  | "
        lines.extend(f"{prefix}{line}" for line in _tail_file(path, tail))
    query = (keyword or "").strip().lower()
    if query:
        lines = [line for line in lines if query in line.lower()]
    redacted = [redact_text(line) for line in lines[-tail:]]
    if redacted:
        return {
            "ok": True,
            "source": "local_files",
            "services": [name for name in services if name in files],
            "tail": tail,
            "lines": redacted,
            "error": None,
        }
    return {
        "ok": False,
        "source": "unavailable",
        "services": services,
        "tail": tail,
        "lines": [],
        "error": (
            "当前运行环境没有可读取的系统控制台日志源。生产环境需要内部 updater，"
            "开发环境需要 logs/backend.log 或 logs/frontend.log。"
        ),
    }


def system_console_response(payload: dict[str, Any], *, service: str, tail: int) -> dict[str, Any]:
    raw_lines = payload.get("lines")
    lines = [redact_text(str(line)) for line in raw_lines] if isinstance(raw_lines, list) else []
    raw_services = payload.get("services")
    services = (
        [str(item) for item in raw_services]
        if isinstance(raw_services, list)
        else ([service] if service != "all" else [])
    )
    return {
        "ok": bool(payload.get("ok")),
        "source": str(payload.get("source") or "updater"),
        "services": services,
        "tail": int(payload.get("tail") or tail),
        "lines": lines,
        "error": redact_text(str(payload.get("error"))) if payload.get("error") else None,
    }


async def read_system_console_logs(
    service: str,
    tail: int,
    keyword: str | None = None,
    *,
    fetcher: ConsoleFetcher = fetch_updater_console_logs,
    local_files: dict[str, Path] | None = None,
) -> dict[str, Any]:
    normalized_service = service.strip().lower() or "all"
    if normalized_service not in SYSTEM_CONSOLE_SERVICES:
        raise ValueError("不支持的系统日志服务")
    try:
        payload = await asyncio.to_thread(fetcher, normalized_service, tail)
    except Exception:
        return read_local_console_logs(
            normalized_service,
            tail,
            keyword,
            local_files=local_files,
        )
    payload = filter_console_payload(payload, keyword)
    if payload.get("ok") or payload.get("error"):
        return system_console_response(payload, service=normalized_service, tail=tail)
    return read_local_console_logs(
        normalized_service,
        tail,
        keyword,
        local_files=local_files,
    )


__all__ = [
    "LOCAL_CONSOLE_FILES",
    "SYSTEM_CONSOLE_SERVICES",
    "fetch_updater_console_logs",
    "filter_console_payload",
    "is_console_noise_line",
    "read_local_console_logs",
    "read_system_console_logs",
    "system_console_response",
]
