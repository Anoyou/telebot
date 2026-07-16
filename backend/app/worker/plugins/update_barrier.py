"""Cross-process barrier that blocks stale plugin instances during code replacement."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

_SAFE_PLUGIN_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _safe_key(plugin_key: str) -> str:
    key = str(plugin_key or "").strip()
    if not _SAFE_PLUGIN_KEY.fullmatch(key):
        raise ValueError(f"invalid plugin key: {plugin_key!r}")
    return key


def _marker_path(root: Path, plugin_key: str) -> Path:
    return root / f".{_safe_key(plugin_key)}.update.json"


def _ack_path(root: Path, plugin_key: str, account_id: int) -> Path:
    return root / f".{_safe_key(plugin_key)}.update.{int(account_id)}.ack"


def _read_update_id(root: Path, plugin_key: str) -> str | None:
    marker = _marker_path(root, plugin_key)
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    update_id = str(payload.get("update_id") or "").strip() if isinstance(payload, dict) else ""
    return update_id or None


def begin_plugin_update(root: Path, plugin_key: str, *, target_version: str = "") -> str:
    """Publish the barrier before replacing code and invalidate prior worker ACKs."""

    root.mkdir(parents=True, exist_ok=True)
    key = _safe_key(plugin_key)
    update_id = uuid.uuid4().hex
    for ack in root.glob(f".{key}.update.*.ack"):
        ack.unlink(missing_ok=True)
    marker = _marker_path(root, key)
    temporary = root / f".{key}.update.{uuid.uuid4().hex}.tmp"
    payload = {
        "update_id": update_id,
        "plugin_key": key,
        "target_version": str(target_version or ""),
        "started_at": time.time(),
        "pid": os.getpid(),
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)
    return update_id


def plugin_update_token(root: Path, plugin_key: str) -> str | None:
    """Return the current update token captured before one worker reloads code."""

    return _read_update_id(root, plugin_key)


def acknowledge_plugin_update(
    root: Path,
    plugin_key: str,
    account_id: int,
    update_id: str | None,
) -> None:
    """Allow one account only after its worker has activated the replacement."""

    token = str(update_id or "").strip()
    if not token or _read_update_id(root, plugin_key) != token:
        return
    ack = _ack_path(root, plugin_key, account_id)
    temporary = ack.with_name(f"{ack.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(token, encoding="ascii")
        temporary.replace(ack)
    finally:
        temporary.unlink(missing_ok=True)


def plugin_update_in_progress(root: Path, plugin_key: str, account_id: int) -> bool:
    """Return True while this account still owns an old in-memory instance."""

    marker = _marker_path(root, plugin_key)
    if not marker.is_file():
        return False
    update_id = _read_update_id(root, plugin_key)
    if update_id is None:
        return True
    try:
        acknowledged_id = _ack_path(root, plugin_key, account_id).read_text(encoding="ascii").strip()
    except OSError:
        return True
    return acknowledged_id != update_id


def plugin_update_active(root: Path, plugin_key: str) -> bool:
    """Return whether any replacement barrier currently exists for this plugin."""

    return _marker_path(root, plugin_key).is_file()


def clear_plugin_update(root: Path, plugin_key: str) -> None:
    """Remove the shared barrier after every targeted worker confirmed reload."""

    key = _safe_key(plugin_key)
    _marker_path(root, key).unlink(missing_ok=True)
    for ack in root.glob(f".{key}.update.*.ack"):
        ack.unlink(missing_ok=True)
