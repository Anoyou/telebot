"""Shared pytest compatibility helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not hasattr(pytest, "mock"):
    pytest.mock = mock  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_local_userbot_rate_limit_state() -> None:
    """避免进程内本地限流桶在测试间泄漏（波次一 fail-closed 降级）。"""

    try:
        from app.worker import command as command_mod

        command_mod.reset_local_rate_limit_buckets()
        yield
        command_mod.reset_local_rate_limit_buckets()
    except Exception:  # noqa: BLE001
        yield
