"""Unified interaction session envelope.

E1 (userbot) and E2 (interaction bot) historically rebuilt session JSON in
several places with slightly different fields. ``SessionRecord`` is the
canonical in-memory / Redis shape; adapters still own when to persist.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

SESSION_CHANNELS = frozenset({"interaction_bot", "userbot", "interaction_session"})


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_empty(value: Any) -> str:
    return str(value or "").strip()


@dataclass(slots=True)
class SessionRecord:
    """Canonical session document stored under interaction session Redis keys."""

    account_id: int
    chat_id: int
    module_key: str
    entry_key: str
    channel: str = "interaction_bot"
    rule_id: str = "legacy"
    rule_name: str = ""
    started_by_user_id: int | None = None
    source_user_id: int | None = None
    started_by_message_id: int | None = None
    event_type: str = "message"
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    revision: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    paid_user_ids: list[int] = field(default_factory=list)
    participant_user_ids: list[int] = field(default_factory=list)
    payer_user_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # paid/participant 空列表对 paid_pool 有语义（尚未有人入池），保留。
        if self.payer_user_id is None:
            payload.pop("payer_user_id", None)
        if self.revision <= 0:
            payload.pop("revision", None)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SessionRecord | None:
        if not isinstance(raw, dict) or not raw:
            return None
        account_id = _int_or_none(raw.get("account_id"))
        chat_id = _int_or_none(raw.get("chat_id"))
        module_key = _str_or_empty(raw.get("module_key"))
        entry_key = _str_or_empty(raw.get("entry_key"))
        if account_id is None or chat_id is None or not module_key or not entry_key:
            return None
        channel = _str_or_empty(raw.get("channel")) or "interaction_bot"
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        paid = raw.get("paid_user_ids") if isinstance(raw.get("paid_user_ids"), list) else []
        participants = (
            raw.get("participant_user_ids")
            if isinstance(raw.get("participant_user_ids"), list)
            else []
        )
        return cls(
            account_id=account_id,
            chat_id=chat_id,
            module_key=module_key,
            entry_key=entry_key,
            channel=channel,
            rule_id=_str_or_empty(raw.get("rule_id")) or "legacy",
            rule_name=_str_or_empty(raw.get("rule_name")),
            started_by_user_id=_int_or_none(raw.get("started_by_user_id")),
            source_user_id=_int_or_none(raw.get("source_user_id")),
            started_by_message_id=_int_or_none(raw.get("started_by_message_id")),
            event_type=_str_or_empty(raw.get("event_type")) or "message",
            created_at=_float_or_none(raw.get("created_at")) or 0.0,
            updated_at=_float_or_none(raw.get("updated_at")) or 0.0,
            expires_at=_float_or_none(raw.get("expires_at")) or 0.0,
            revision=_int_or_none(raw.get("revision")) or 0,
            data=dict(data),
            paid_user_ids=[int(x) for x in paid if _int_or_none(x) is not None],
            participant_user_ids=[int(x) for x in participants if _int_or_none(x) is not None],
            payer_user_id=_int_or_none(raw.get("payer_user_id")),
        )

    def touch(self, *, now: float | None = None, ttl_seconds: int | None = None) -> None:
        ts = time.time() if now is None else float(now)
        if self.created_at <= 0:
            self.created_at = ts
        self.updated_at = ts
        if ttl_seconds is not None and ttl_seconds > 0:
            self.expires_at = ts + int(ttl_seconds)
        self.revision = max(1, int(self.revision) + 1)

    def is_active(self, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else float(now)
        try:
            return float(self.expires_at) > ts
        except (TypeError, ValueError):
            return False


__all__ = ["SESSION_CHANNELS", "SessionRecord"]
