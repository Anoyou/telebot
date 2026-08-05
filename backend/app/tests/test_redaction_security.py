from __future__ import annotations

import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.features import (
    _preserve_existing_read_only_values,
    _preserve_existing_sensitive_values,
    _sanitize_config,
)
from app.api.logs import RuntimeLogItem, list_audit_logs
from app.logging_redaction import SensitiveDataLogFilter, configure_dependency_log_levels
from app.services import audit
from app.services.redactor import redact_text, redact_value


def test_redactor_masks_text_and_nested_fields() -> None:
    src = {
        "access_token": "abc123456789",
        "proxy_url": "http://user:pass@example.com:8080",
        "nested": {"api_key": "sk-test-1234567890"},
    }
    out = redact_value(src)
    assert out["access_token"] == "***"
    assert out["nested"]["api_key"] == "***"
    assert out["proxy_url"] == "http://***:***@example.com:8080"
    assert redact_text("Bearer abcdefghijklmnop") == "Bearer ***"
    assert redact_text("Authorization: Basic eC1hY2Nlc3MtdG9rZW46Z2hwX3NlY3JldDEyMw==") == "Authorization: Basic ***"
    assert redact_text("socks5://user:pass@127.0.0.1:1080") == "socks5://***:***@127.0.0.1:1080"
    bot_url = "https://api.telegram.org/bot123456:secret-token/getUpdates"
    redacted_url = redact_text(bot_url)
    assert "123456:secret-token" not in redacted_url
    assert redacted_url == "https://api.telegram.org/bot***/getUpdates"


def test_redactor_preserves_non_secret_token_counters() -> None:
    out = redact_value(
        {
            "max_tokens": 4096,
            "daily_tokens": 123,
            "token_budget": 50,
            "bot_token": "123456789:secret",
            "accessToken": "abc123456789",
        }
    )
    assert out["max_tokens"] == 4096
    assert out["daily_tokens"] == 123
    assert out["token_budget"] == 50
    assert out["bot_token"] == "***"
    assert out["accessToken"] == "***"


def test_redactor_preserves_boolean_secret_presence_flags_only() -> None:
    out = redact_value(
        {
            "has_api_key": True,
            "has_token": False,
            "has_password": "yes",
            "api_key": "sk-secret-value",
        }
    )
    assert out["has_api_key"] is True
    assert out["has_token"] is False
    assert out["has_password"] == "***"
    assert out["api_key"] == "***"


def test_redactor_masks_common_provider_api_keys() -> None:
    values = (
        "xai-abcdefghijklmnopqrstuvwxyz123456",
        "gsk_abcdefghijklmnopqrstuvwxyz123456",
        "AIzaabcdefghijklmnopqrstuvwxyz123456",
    )

    for value in values:
        assert value not in redact_text(f"provider key: {value}")


def test_redactor_masks_quoted_env_repr_and_cookie_values() -> None:
    secret = "0123456789abcdef0123456789abcdef"
    samples = (
        f"TELEPILOT_UPDATER_TOKEN='{secret}'",
        f"{{'authorization': '{secret}'}}",
        f'cookie="sessionid={secret}"',
        f'config={{"api_key": "{secret}"}}',
        f"custom_service_refresh_token={secret}",
        f"Authorization: Token {secret}",
        f"Authorization: ApiKey {secret}",
        f"Authorization: Digest {secret}",
    )

    for sample in samples:
        redacted = redact_text(sample)
        assert secret not in redacted
        assert "***" in redacted


@pytest.mark.parametrize(
    "text",
    (
        ("a-" * 30) + "token:",
        ("a-" * 30) + "authorization:",
    ),
)
def test_redactor_handles_pathological_key_prefix_quickly(text: str) -> None:
    """未匹配的长键前缀不能让正则回溯卡住 worker。"""
    script = (
        "import sys\n"
        "from app.services.redactor import redact_text\n"
        "value = sys.argv[1]\n"
        "result = redact_text(value)\n"
        "assert result == value\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, text],
        cwd=Path(__file__).parents[2],
        check=True,
        timeout=1,
    )


def test_log_filter_redacts_telegram_bot_url_args() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "HTTP/1.1 200 OK"',
        args=("POST", "https://api.telegram.org/bot123456:secret-token/getUpdates"),
        exc_info=None,
    )
    assert SensitiveDataLogFilter().filter(record)
    rendered = record.getMessage()
    assert "123456:secret-token" not in rendered
    assert "https://api.telegram.org/bot***/getUpdates" in rendered


def test_dependency_http_request_logs_default_to_warning() -> None:
    previous_httpx = logging.getLogger("httpx").level
    previous_httpcore = logging.getLogger("httpcore").level
    try:
        configure_dependency_log_levels()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        logging.getLogger("httpx").setLevel(previous_httpx)
        logging.getLogger("httpcore").setLevel(previous_httpcore)


@pytest.mark.asyncio
async def test_audit_write_redacts_detail() -> None:
    class _FakeDB:
        def __init__(self) -> None:
            self.rows: list[object] = []

        def add(self, row: object) -> None:
            self.rows.append(row)

    db = _FakeDB()
    await audit.write(
        db, 1, "feature.config.update", detail={"token": "abcd1234", "safe": "ok"}
    )
    row = db.rows[0]
    assert row.detail["token"] == "***"
    assert row.detail["safe"] == "ok"


def test_feature_config_preserve_sensitive_values() -> None:
    merged = _preserve_existing_sensitive_values(
        {"access_token": "old", "command": "cximg"},
        {"access_token": "", "command": "new-cmd"},
    )
    assert merged["access_token"] == "old"
    assert merged["command"] == "new-cmd"
    assert _sanitize_config({"access_token": "real"})["access_token"] == "***"


def test_feature_config_preserves_server_owned_read_only_values() -> None:
    merged = _preserve_existing_read_only_values(
        {
            "question_bank_status": "已生成：测试题库（200 题）",
            "question_bank_count": 200,
            "question_bank_id": "bank-1",
        },
        {
            "question_bank_status": "伪造状态",
            "question_bank_count": 999,
            "question_bank_id": "bank-2",
        },
        {
            "config_schema": {
                "properties": {
                    "question_bank_status": {"type": "string", "readOnly": True},
                    "question_bank_count": {"type": "integer", "readOnly": True},
                    "question_bank_id": {"type": "string"},
                }
            }
        },
    )

    assert merged["question_bank_status"] == "已生成：测试题库（200 题）"
    assert merged["question_bank_count"] == 200
    assert merged["question_bank_id"] == "bank-2"

    first_save = _preserve_existing_read_only_values(
        None,
        {"question_bank_status": "伪造状态", "question_bank_id": "bank-2"},
        {
            "config_schema": {
                "properties": {
                    "question_bank_status": {"type": "string", "readOnly": True},
                    "question_bank_id": {"type": "string"},
                }
            }
        },
    )
    assert "question_bank_status" not in first_save
    assert first_save["question_bank_id"] == "bank-2"


def test_runtime_log_item_redacts_message_and_detail() -> None:
    row = SimpleNamespace(
        id=1,
        ts=datetime.now(UTC),
        account_id=2,
        level="info",
        source="plugin",
        message="token=abcdef123456",
        detail={"api_key": "sk-1234567890"},
    )
    item = RuntimeLogItem.from_row(row)  # type: ignore[arg-type]
    assert "abcdef123456" not in item.message
    assert item.detail["api_key"] == "***"


@pytest.mark.asyncio
async def test_list_audit_logs_redacts_response_detail() -> None:
    ts = datetime.now(UTC)
    row = SimpleNamespace(
        id=1,
        ts=ts,
        user_id=1,
        action="x",
        target="y",
        detail={"password": "secret123"},
    )

    result_proxy = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [row]),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    items = await list_audit_logs(db=db, _user=SimpleNamespace(id=1), limit=10)
    assert items[0].detail["password"] == "***"
