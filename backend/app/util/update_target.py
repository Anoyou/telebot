"""Application update target validation shared by settings, restore, and execution."""

from __future__ import annotations

import re
from typing import Any

_REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def normalize_update_remote(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _REMOTE_RE.fullmatch(normalized):
        raise ValueError("更新远端名称格式无效")
    return normalized


def normalize_update_branch(value: Any) -> str:
    normalized = str(value or "").strip()
    if (
        not _BRANCH_RE.fullmatch(normalized)
        or ".." in normalized
        or "//" in normalized
        or normalized.endswith(("/", "."))
    ):
        raise ValueError("更新分支格式无效")
    return normalized


def normalize_update_target(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("更新目标必须是 object")
    return {
        "remote": normalize_update_remote(value.get("remote") or "origin"),
        "branch": normalize_update_branch(value.get("branch") or "main"),
    }


__all__ = ["normalize_update_branch", "normalize_update_remote", "normalize_update_target"]
