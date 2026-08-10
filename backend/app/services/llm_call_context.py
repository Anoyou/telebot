"""Privacy-preserving runtime context for simulated LLM client identities."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..settings import settings

_METADATA_KEY = "_client_runtime"


def _pseudonym(provider_scope: str, kind: str, value: str) -> str:
    material = f"telepilot-llm:{provider_scope}:{kind}:{value}".encode()
    return hmac.new(
        settings.master_key.encode(),
        material,
        hashlib.sha256,
    ).hexdigest()[:32]


@dataclass(frozen=True)
class ClientRuntimeContext:
    """Call-local identity state.

    Raw TelePilot session/account/Telegram identifiers never leave this object.
    Stable upstream identifiers are provider/profile-scoped pseudonyms, while
    ``request_id`` is fresh for every HTTP attempt.
    """

    session_id: str
    run_id: str
    turn_id: str
    request_id: str
    turn_index: int = 1
    ephemeral: bool = False

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any] | None,
        *,
        provider_scope: str,
    ) -> ClientRuntimeContext:
        raw = (metadata or {}).get(_METADATA_KEY)
        values = raw if isinstance(raw, Mapping) else {}
        ephemeral = not bool(values)
        raw_session = str(values.get("session_id") or uuid.uuid4())
        raw_run = str(values.get("run_id") or raw_session)
        raw_turn = str(values.get("turn_id") or raw_run)
        try:
            turn_index = max(1, int(values.get("turn_index") or 1))
        except (TypeError, ValueError):
            turn_index = 1
        return cls(
            session_id=_pseudonym(provider_scope, "session", raw_session),
            run_id=_pseudonym(provider_scope, "run", raw_run),
            turn_id=_pseudonym(provider_scope, "turn", raw_turn),
            request_id=str(uuid.uuid4()),
            turn_index=turn_index,
            ephemeral=ephemeral,
        )

    def headers_for_identity(self, profile: str, *, model: str) -> dict[str, str]:
        if profile == "codex_tui":
            return {
                "session-id": self.session_id,
                "thread-id": self.session_id,
                "x-client-request-id": self.request_id,
            }
        if profile == "codex_desktop":
            return {
                "session_id": self.session_id,
                "x-client-request-id": self.request_id,
            }
        if profile == "claude_code":
            return {"X-Claude-Code-Session-Id": self.session_id}
        if profile == "grok_cli":
            # Deliberately omit x-grok-agent-id and all authentication/device
            # assertions.  Those are not safe client simulation fields.
            headers = {
                "x-grok-conv-id": self.session_id,
                "x-grok-session-id": self.session_id,
                "x-grok-req-id": self.request_id,
                "x-grok-turn-idx": str(self.turn_index),
            }
            safe_model = str(model or "").strip()
            if safe_model and "\r" not in safe_model and "\n" not in safe_model:
                headers["x-grok-model-override"] = safe_model[:256]
            return headers
        return {}


def runtime_metadata(
    *,
    session_id: str | int,
    run_id: str,
    turn_id: str | None = None,
    turn_index: int = 1,
) -> dict[str, Any]:
    """Build the local-only metadata fragment consumed by adapters."""

    return {
        _METADATA_KEY: {
            "session_id": str(session_id),
            "run_id": str(run_id),
            "turn_id": str(turn_id or run_id),
            "turn_index": max(1, int(turn_index)),
        }
    }


__all__ = ["ClientRuntimeContext", "runtime_metadata"]
