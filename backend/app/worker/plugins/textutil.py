"""文本辅助工具。"""

from __future__ import annotations

from typing import Any

from ...services.account_bot_service import html_text


def html_escape(value: Any) -> str:
    return html_text(value)


__all__ = ["html_escape"]
