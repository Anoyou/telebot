#!/usr/bin/env python3
"""计算高频 beta 提交需要运行的 CI 范围。仅依赖标准库。"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CiScope:
    backend: bool
    frontend: bool
    browser: bool
    plugin: bool
    contract: bool
    version: bool
    docs_only: bool
    full: bool

    def to_outputs(self) -> dict[str, str]:
        outputs = {key: "true" if value else "false" for key, value in asdict(self).items()}
        outputs["publish"] = "false" if self.docs_only else "true"
        return outputs


_DOC_FILES = {
    ".env.example",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
}
_VERSION_FILES = {
    "CHANGELOG.md",
    "backend/app/__init__.py",
    "backend/pyproject.toml",
    "frontend/package.json",
    "frontend/src/lib/version.ts",
}
_BROWSER_PREFIXES = (
    "frontend/e2e/",
    "frontend/public/",
    "frontend/src/",
    "frontend/tests/",
)


def _normalize(path: str) -> str:
    value = path.strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def _is_docs_only_path(path: str) -> bool:
    return (
        path in _DOC_FILES
        or path.startswith("docs/")
        or Path(path).suffix.lower() in {".md", ".rst", ".txt"}
    )


def classify_paths(paths: list[str], *, force_full: bool = False) -> CiScope:
    files = [_normalize(path) for path in paths if path.strip()]
    full = force_full or not files or any(
        path == "scripts/ci_change_scope.py" or path.startswith(".github/workflows/")
        for path in files
    )
    if full:
        return CiScope(True, True, True, True, True, True, False, True)

    backend = any(
        path.startswith(("backend/", "deploy/", "plugins/", "scripts/"))
        or path in {"docker-compose.yml", ".dockerignore", "Makefile"}
        for path in files
    )
    frontend = any(path.startswith("frontend/") for path in files)
    browser = any(
        (
            path.startswith(_BROWSER_PREFIXES)
            and path != "frontend/src/lib/version.ts"
            and not path.endswith((".test.ts", ".test.tsx", ".test.js", ".test.cjs"))
        )
        or path in {
            "frontend/index.html",
            "frontend/playwright.config.ts",
            "frontend/vite.config.ts",
        }
        for path in files
    )
    plugin = any(
        path.startswith(("plugins/", "backend/app/worker/plugins/", "examples/", "schemas/"))
        or path in {
            "scripts/export-plugin-schema.py",
            "scripts/validate-plugin-examples.py",
            "scripts/validate-installed-interaction-plugins.py",
        }
        for path in files
    )
    contract = any(
        path.startswith(("backend/app/api/", "backend/app/schemas/"))
        or path
        in {
            "backend/app/deps.py",
            "backend/app/main.py",
            "backend/app/openapi_contract.py",
            "frontend/src/api/schema.ts",
            "openapi/telepilot.openapi.json",
            "schemas/plugin.schema.json",
            "scripts/export-openapi.py",
            "scripts/export-plugin-schema.py",
        }
        for path in files
    )
    version = any(path in _VERSION_FILES for path in files)
    docs_only = all(_is_docs_only_path(path) for path in files)
    return CiScope(backend, frontend, browser, plugin, contract, version, docs_only, False)


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        raise RuntimeError("缺少可比较的上一提交")
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--name-only", "-z", f"{base}..{head}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff 失败")
    return [path for path in result.stdout.split("\0") if path]


def main() -> None:
    parser = argparse.ArgumentParser(description="计算 TelePilot CI 变更范围")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        paths = changed_paths(args.root.resolve(), args.base, args.head)
        scope = classify_paths(paths, force_full=args.force_full)
    except RuntimeError as error:
        print(f"无法可靠计算变更范围，转为完整门禁：{error}")
        scope = classify_paths([], force_full=True)

    outputs = scope.to_outputs()
    for key, value in outputs.items():
        print(f"{key}={value}")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
