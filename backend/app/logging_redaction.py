"""Runtime logging redaction for third-party and application logs."""

from __future__ import annotations

import logging
from typing import Any

from .services.redactor import redact_text, redact_value


class SensitiveDataLogFilter(logging.Filter):
    """Redact tokens from log records before handlers format them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            record.args = _redact_log_args(record.args)
        return True


def install_sensitive_log_filter() -> None:
    """Install the redaction filter on current root handlers once."""

    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SensitiveDataLogFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataLogFilter())


def _redact_log_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {key: _redact_log_arg(value) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_log_arg(value) for value in args)
    return _redact_log_arg(args)


def _redact_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, (dict, list, tuple)):
        return redact_value(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


__all__ = ["SensitiveDataLogFilter", "install_sensitive_log_filter"]
