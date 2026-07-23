"""生产更新影响范围计算。

更新计划以运行中的 Compose 服务为边界，而不是把任意部署文件变化都
提升为全栈重建。该模块只依赖标准库，供 Web、updater 和 Shell 更新脚本
共享同一套判断。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_DOC_SUFFIXES = (".md", ".rst", ".txt")
_BACKEND_TEST_PREFIXES = ("backend/app/tests/", "backend/tests/")
_NO_RUNTIME_PREFIXES = (
    ".github/",
    "docs/",
    "examples/",
)
_NO_RUNTIME_FILES = {
    ".env.example",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "docker-compose.dev.yml",
    "scripts/bootstrap.sh",
    "scripts/install-server.sh",
    "scripts/prod-up.sh",
}
_FRONTEND_BUNDLED_FILES = {"CHANGELOG.md", "docs/PLUGIN-DEV-GUIDE.md"}
_UPDATER_FILES = {
    "backend/app/util/update_plan.py",
    "deploy/updater/Dockerfile",
    "deploy/updater/server.py",
    "scripts/_lib.sh",
    "scripts/prod-update.sh",
}
_KNOWN_SERVICES = {"postgres", "redis", "web", "frontend", "updater"}
_INFRA_SERVICES = {"postgres", "redis"}


@dataclass(frozen=True)
class UpdatePlan:
    changed_files: list[str]
    components: list[str]
    services: list[str]
    requires_full_update: bool
    requires_backup: bool
    requires_migration: bool
    compose_changed_services: list[str]
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(path: str) -> str:
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_docs_file(path: str) -> bool:
    normalized = _normalize(path)
    lowered = normalized.lower()
    if normalized in _FRONTEND_BUNDLED_FILES:
        return False
    return (
        normalized in _NO_RUNTIME_FILES
        or normalized.startswith(_NO_RUNTIME_PREFIXES)
        or Path(lowered).name.endswith(_DOC_SUFFIXES)
    )


def _ordered(items: set[str], order: tuple[str, ...]) -> list[str]:
    return [item for item in order if item in items]


def classify_changed_files(
    changed_files: list[str],
    *,
    compose_changed_services: set[str] | None = None,
    compose_inspection_failed: bool = False,
) -> UpdatePlan:
    files = [_normalize(path) for path in changed_files if path.strip()]
    if not files:
        return UpdatePlan([], ["none"], [], False, False, False, [], [])

    components: set[str] = set()
    services: set[str] = set()
    reasons: list[str] = []
    requires_full_update = compose_inspection_failed
    requires_migration = any(path.startswith("backend/alembic/versions/") for path in files)
    requires_backup = requires_migration

    if compose_inspection_failed:
        components.add("full_update")
        reasons.append("无法比较 Compose 服务配置，转入完整更新")

    for path in files:
        if path == "docker-compose.yml":
            if compose_changed_services is None:
                requires_full_update = True
                components.add("full_update")
                reasons.append("Compose 变化未完成服务级比较")
                continue
            unknown = compose_changed_services - _KNOWN_SERVICES
            if unknown or compose_changed_services & _INFRA_SERVICES:
                requires_full_update = True
                components.add("full_update")
                components.add("infrastructure")
                reasons.append("PostgreSQL、Redis 或未知 Compose 服务配置发生变化")
            for service in compose_changed_services & {"web", "frontend", "updater"}:
                services.add(service)
                components.add("backend" if service == "web" else service)
            continue

        if path == "backend/app/util/update_plan.py":
            components.update({"backend", "updater"})
            services.update({"web", "updater"})
            continue
        if path in _UPDATER_FILES:
            components.add("updater")
            services.add("updater")
            continue

        if path == ".dockerignore":
            components.update({"frontend", "updater"})
            services.update({"frontend", "updater"})
            continue
        if path == "backend/.dockerignore":
            components.add("backend")
            services.add("web")
            continue
        if path == "frontend/.dockerignore":
            components.add("frontend")
            services.add("frontend")
            continue

        if path.startswith("deploy/") or path.startswith("scripts/"):
            # 备份、恢复、安装和人工部署脚本由挂载工作区直接读取，不改变运行容器。
            continue

        if path in _FRONTEND_BUNDLED_FILES or path.startswith("frontend/"):
            components.add("frontend")
            services.add("frontend")
            if path == "CHANGELOG.md":
                components.add("backend")
                services.add("web")
            continue

        if path.startswith(_BACKEND_TEST_PREFIXES):
            continue
        if path.startswith("backend/") or path.startswith("plugins/"):
            components.add("backend")
            services.add("web")
            continue

        if _is_docs_file(path):
            continue

        requires_full_update = True
        components.add("full_update")
        reasons.append(f"无法确定运行影响范围：{path}")

    if requires_migration:
        components.add("migration")
        components.add("backend")
        services.add("web")
        reasons.append("包含数据库迁移，切换 web 前需要备份")

    if requires_full_update:
        components.add("full_update")
    if not components:
        components.add("docs_only")

    return UpdatePlan(
        changed_files=files,
        components=_ordered(
            components,
            ("full_update", "infrastructure", "migration", "backend", "frontend", "updater", "docs_only"),
        ),
        services=_ordered(services, ("web", "frontend", "updater")),
        requires_full_update=requires_full_update,
        requires_backup=requires_backup,
        requires_migration=requires_migration,
        compose_changed_services=_ordered(compose_changed_services or set(), tuple(sorted(_KNOWN_SERVICES))),
        reasons=reasons,
    )


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _git_show(root: Path, revision: str, path: str) -> str:
    result = _run(["git", "show", f"{revision}:{path}"], cwd=root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"无法读取 {revision}:{path}")
    return result.stdout


def _compose_config(root: Path, revision: str) -> dict[str, Any]:
    content = _git_show(root, revision, "docker-compose.yml")
    with tempfile.NamedTemporaryFile("w", suffix=".yml", encoding="utf-8", delete=False) as handle:
        handle.write(content)
        compose_path = Path(handle.name)
    try:
        result = _run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(root),
                "-f",
                str(compose_path),
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            env={"UPDATER_TOKEN": os.getenv("UPDATER_TOKEN", "update-plan-placeholder-token-000000000000")},
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "docker compose config 失败")
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError("docker compose config 未返回对象")
        return parsed
    finally:
        compose_path.unlink(missing_ok=True)


def _compose_changes(root: Path, old_revision: str, new_revision: str) -> tuple[set[str], bool]:
    old_config = _compose_config(root, old_revision)
    new_config = _compose_config(root, new_revision)
    old_services = old_config.get("services") or {}
    new_services = new_config.get("services") or {}
    service_names = set(old_services) | set(new_services)
    changed_services = {
        name for name in service_names if old_services.get(name) != new_services.get(name)
    }
    top_level_keys = {"volumes", "networks", "configs", "secrets"}
    top_level_changed = any(old_config.get(key) != new_config.get(key) for key in top_level_keys)
    return changed_services, top_level_changed


def build_update_plan(root: Path, old_revision: str, new_revision: str) -> UpdatePlan:
    root = root.resolve()
    diff = _run(
        ["git", "-c", "core.quotePath=false", "diff", "--name-only", "-z", f"{old_revision}..{new_revision}"],
        cwd=root,
    )
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.strip() or "git diff 失败")
    changed_files = [path for path in diff.stdout.split("\0") if path.strip()]

    compose_services: set[str] | None = None
    compose_failed = False
    if "docker-compose.yml" in changed_files:
        try:
            compose_services, top_level_changed = _compose_changes(root, old_revision, new_revision)
            compose_failed = top_level_changed
        except (OSError, RuntimeError, json.JSONDecodeError):
            compose_failed = True

    return classify_changed_files(
        changed_files,
        compose_changed_services=compose_services,
        compose_inspection_failed=compose_failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 TelePilot 服务级生产更新计划")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    args = parser.parse_args()
    print(json.dumps(build_update_plan(args.root, args.old, args.new).to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
