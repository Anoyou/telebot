from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import plugins as plugins_api


def test_legacy_plugin_entry_rejects_non_builtin_with_scoped_message(monkeypatch) -> None:
    monkeypatch.setattr(plugins_api, "_is_builtin", lambda _plugin_key: False)

    with pytest.raises(HTTPException) as exc_info:
        plugins_api._ensure_builtin_or_501("demo")

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == {
        "code": "NOT_IMPLEMENTED",
        "message": "该旧入口仅支持内置插件: demo",
    }
