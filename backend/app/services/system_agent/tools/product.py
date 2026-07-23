"""产品信息只读工具。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .... import __version__
from ....settings import PROJECT_ROOT
from ..context import ToolContext
from ..registry import ToolRegistry, ToolSpec
from ._helpers import clamp_limit

_CHANGELOG_HEADING = re.compile(r"^##\s+\[(.+?)\].*$")


def _changelog_candidates() -> tuple[Path, ...]:
    # 本地开发时 PROJECT_ROOT 是 backend；容器构建时 CHANGELOG 会被复制到 /app。
    return (PROJECT_ROOT / "CHANGELOG.md", PROJECT_ROOT.parent / "CHANGELOG.md")


def _read_changelog_sections(raw: str, limit: int) -> list[dict[str, str]]:
    lines = raw.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _CHANGELOG_HEADING.match(line)
        if match and match.group(1).strip().lower() != "unreleased":
            starts.append((index, line.removeprefix("## ").strip()))

    sections: list[dict[str, str]] = []
    for index, (start, title) in enumerate(starts[:limit]):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip()
        if body:
            sections.append({"title": title, "body": body})
    return sections


def _find_changelog() -> Path | None:
    return next((path for path in _changelog_candidates() if path.is_file()), None)


async def get_changelog(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    del ctx
    limit = clamp_limit(args.get("limit"), default=4, maximum=8)
    path = _find_changelog()
    if path is None:
        return {
            "version": __version__,
            "available": False,
            "message": "当前运行包未携带 CHANGELOG.md。",
            "mobile_path": "打开左上角菜单，点击侧栏底部的‘更新日志’。",
            "desktop_path": "点击左侧栏底部的‘更新日志’。",
        }

    raw = path.read_text(encoding="utf-8")
    return {
        "version": __version__,
        "available": True,
        "source": "CHANGELOG.md",
        "sections": _read_changelog_sections(raw, limit),
        "mobile_path": "打开左上角菜单，点击侧栏底部的‘更新日志’。",
        "desktop_path": "点击左侧栏底部的‘更新日志’。",
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="product.get_changelog",
            description="读取 TelePilot 最近版本更新日志，并说明桌面端和移动端在哪里打开。",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回最近几个正式版本，默认 4 个，最多 8 个。",
                    }
                },
                "additionalProperties": False,
            },
            read_only=True,
            min_role="viewer",
            read_handler=get_changelog,
        )
    )


__all__ = ["get_changelog", "register"]
