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

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 生产宿主没有 tomllib
    tomllib = None  # type: ignore[assignment]

_DOC_SUFFIXES = (".md", ".rst", ".txt")
_BACKEND_TEST_PREFIXES = ("backend/app/tests/", "backend/tests/")
_FRONTEND_TEST_PREFIXES = (
    "frontend/e2e/",
    "frontend/test-results/",
    "frontend/tests/",
)
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
    # OpenAPI JSON 是由后端 schema 生成的发布快照，不会进入运行容器。
    # 真正的运行影响由后端源码和生成的前端 API 类型各自触发。
    "openapi/telepilot.openapi.json",
    "scripts/bootstrap.sh",
    "scripts/install-server.sh",
    "scripts/prod-up.sh",
}
_FRONTEND_SOURCE_MIRROR_FILES = {
    "frontend/package.json",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
}
_UPDATER_FILES = {
    "deploy/updater/Dockerfile",
    "deploy/updater/server.py",
}
_KNOWN_SERVICES = {"postgres", "redis", "web", "frontend", "updater"}
_INFRA_SERVICES = {"postgres", "redis"}


@dataclass(frozen=True)
class UpdatePlan:
    changed_files: list[str]
    components: list[str]
    services: list[str]
    file_sync_services: list[str]
    rebuild_services: list[str]
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
    backend_dependencies_changed: bool | None = None,
) -> UpdatePlan:
    files = [_normalize(path) for path in changed_files if path.strip()]
    if not files:
        return UpdatePlan([], ["none"], [], [], [], False, False, False, [], [])

    components: set[str] = set()
    services: set[str] = set()
    file_sync_services: set[str] = set()
    rebuild_services: set[str] = set()
    reasons: list[str] = []
    requires_full_update = compose_inspection_failed
    requires_migration = any(path.startswith("backend/alembic/versions/") for path in files)
    requires_backup = requires_migration

    if compose_inspection_failed:
        components.add("full_update")
        reasons.append("无法比较 Compose 服务配置，转入完整更新")

    def require_file_sync(service: str) -> None:
        services.add(service)
        if service not in rebuild_services:
            file_sync_services.add(service)

    def require_rebuild(service: str) -> None:
        services.add(service)
        rebuild_services.add(service)
        file_sync_services.discard(service)

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
                requires_backup = True
                components.add("full_update")
                components.add("infrastructure")
                reasons.append("PostgreSQL、Redis 或未知 Compose 服务配置发生变化")
            for service in compose_changed_services & {"web", "frontend", "updater"}:
                require_rebuild(service)
                components.add("backend" if service == "web" else service)
            continue

        if path == "backend/app/util/update_plan.py":
            components.add("backend")
            require_file_sync("web")
            continue
        if path in _UPDATER_FILES:
            components.add("updater")
            require_rebuild("updater")
            continue

        if path == ".dockerignore":
            components.update({"backend", "frontend", "updater"})
            require_rebuild("web")
            require_rebuild("frontend")
            require_rebuild("updater")
            continue
        if path in {"backend/.dockerignore", "frontend/.dockerignore"}:
            # Compose 三个应用镜像均以仓库根为 context，子目录 ignore 不生效。
            continue

        if path == "backend/pyproject.toml":
            components.add("backend")
            if backend_dependencies_changed is False:
                require_file_sync("web")
            else:
                # 直接分类时缺少新旧内容，默认按依赖变化处理；build_update_plan
                # 会解析 project.dependencies，把纯版本号变化降为文件同步。
                require_rebuild("web")
            continue

        if path.startswith("deploy/") or path.startswith("scripts/"):
            # 备份、恢复、安装和人工部署脚本由挂载工作区直接读取，不改变运行容器。
            continue

        if path.startswith(_FRONTEND_TEST_PREFIXES) or path in {
            "frontend/playwright.config.ts",
            "frontend/vitest.config.ts",
        }:
            continue

        if path.startswith("frontend/"):
            components.add("frontend")
            require_rebuild("frontend")
            if (
                path in _FRONTEND_SOURCE_MIRROR_FILES
                or path.startswith("frontend/src/")
            ):
                # System Agent 的只读源码镜像位于 web 镜像中，前端源码变化时
                # 同步该快照，避免线上诊断读取到上一版代码。
                components.add("backend")
                require_file_sync("web")
            continue

        if path.startswith(_BACKEND_TEST_PREFIXES):
            continue
        if (
            path.startswith("backend/app/")
            or path.startswith("backend/alembic/")
            or path == "backend/alembic.ini"
        ):
            components.add("backend")
            require_file_sync("web")
            continue
        if path.startswith("plugins/"):
            # Git 跟踪的插件文件会定向同步到持久卷，随后重启 web/worker；
            # backend 镜像本身不 COPY 根 plugins，重建 web 无法部署这些改动。
            components.add("backend")
            require_file_sync("web")
            continue
        if path.startswith("backend/"):
            components.add("backend")
            require_rebuild("web")
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
        file_sync_services=_ordered(file_sync_services, ("web", "frontend", "updater")),
        rebuild_services=_ordered(rebuild_services, ("web", "frontend", "updater")),
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


def _backend_dependencies_changed(root: Path, old_revision: str, new_revision: str) -> bool:
    if tomllib is None:
        # 宿主只负责生成更新计划，未必安装后端要求的 Python 3.12。
        # 缺少 TOML 解析器时按依赖已变化处理，宁可重建 web，也不能让更新器崩溃。
        return True
    try:
        old_data = tomllib.loads(_git_show(root, old_revision, "backend/pyproject.toml"))
        new_data = tomllib.loads(_git_show(root, new_revision, "backend/pyproject.toml"))
        old_dependencies = (old_data.get("project") or {}).get("dependencies") or []
        new_dependencies = (new_data.get("project") or {}).get("dependencies") or []
        return old_dependencies != new_dependencies
    except (AttributeError, RuntimeError, tomllib.TOMLDecodeError):
        # 解析或读取失败时不能假设依赖层可复用。
        return True


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

    backend_dependencies_changed: bool | None = None
    if "backend/pyproject.toml" in changed_files:
        backend_dependencies_changed = _backend_dependencies_changed(
            root, old_revision, new_revision
        )

    return classify_changed_files(
        changed_files,
        compose_changed_services=compose_services,
        compose_inspection_failed=compose_failed,
        backend_dependencies_changed=backend_dependencies_changed,
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
